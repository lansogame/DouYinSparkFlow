import traceback
from utils.logger import setup_logger
from utils.config import get_config, get_userData
from core.msg_builder import build_message, build_message_with_openai
from core.browser import get_browser
import time

# ===== 融合说明（v2：创作者中心私信路径）=====
# 原项目 main 分支死磕 creator.douyin.com 的「互动管理」（抖音已下架），导致超时卡死。
# dev 分支改用 www.douyin.com/chat，但云机房 IP 一访问就被抖音弹「验证码中间页」(IP 风控)。
# 现改为走 creator.douyin.com 的「私信管理」入口：
#   1) 创作者中心目前无 IP 验证码，可绕开 www 的云机房风控；
#   2) 私信管理入口在首页下滑后才显示，需滚动找到「私信管理」文本再点击；
#   3) 进入后用【好友昵称直接定位会话】，不依赖抖音会变的哈希 class 名，更鲁棒。

config = get_config()
userData = get_userData()
logger = setup_logger(level=config.get("logLevel", "Info"))
# 按「会话列表显示的好友名」匹配，故 nickname 最稳。
# 若你填的是抖音号(short_id)，请改成 nickname 并在 targets 里填好友【原始昵称】。
matchMode = config.get("matchMode", "nickname")

# 私信列表直达 URL（用户验证：创作者中心首页下滑可见「互动管理」，其内「私信管理」
# 按钮需异步加载；该 URL 可直接进入私信列表，绕开首页滚动 + 异步加载，优先尝试）
IM_URL = "https://creator.douyin.com/creator-micro/data/following/chat"
# 私信管理入口的链接/按钮文本（作为 IM_URL 直达失败、被弹回首页时的回退）
IM_ENTRY_TEXT = "私信管理"


def retry_operation(name, operation, retries=3, delay=2, *args, **kwargs):
    """通用的重试逻辑"""
    for attempt in range(retries):
        try:
            return operation(*args, **kwargs)
        except Exception as e:
            if attempt < retries - 1:
                logger.warning(f"{name} 失败，正在重试第 {attempt + 1} 次，错误：{e}")
                time.sleep(delay)
            else:
                logger.error(f"{name} 失败，已达到最大重试次数，错误：{e}")
                raise


def _safe_title(page):
    try:
        return page.title()
    except Exception as e:
        return f"(获取失败: {e})"


def _safe_body(page, limit=500):
    try:
        return page.locator("body").inner_text(timeout=5000)[:limit]
    except Exception as e:
        logger.warning(f"读取页面 body 文本失败: {e}")
        return ""


def _snapshot(page, prefix="diag"):
    """保存截图和 HTML，方便排查页面实际状态"""
    import os
    os.makedirs("logs", exist_ok=True)
    ts = int(time.time())
    try:
        ss_path = f"logs/{prefix}_{ts}.png"
        page.screenshot(path=ss_path, full_page=True)
        logger.info(f"已保存截图: {ss_path}")
    except Exception as e:
        logger.error(f"截图失败: {e}")
    try:
        html_path = f"logs/{prefix}_{ts}.html"
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(page.content())
        logger.info(f"已保存 HTML: {html_path}")
    except Exception as e:
        logger.error(f"保存 HTML 失败: {e}")


def _detect_risk(page, username, label):
    """快速风控识别：一旦出现验证码/登录墙，立刻结论退出（不再干等）"""
    body_text = _safe_body(page)
    title = _safe_title(page)
    html_lower = ""
    try:
        html_lower = page.content().lower()
    except Exception:
        pass
    if page.locator("text=验证").count() > 0 or "验证" in body_text or "验证码" in title or "captcha" in html_lower:
        _snapshot(page, prefix="risk")
        logger.error(
            "【IP 风控】页面出现「验证码中间页」/ 人机验证。GitHub Actions 的云机房 IP "
            "被抖音风控，无法自动通过。请改用：①住宅代理(PROXY_ADDRESS) ②本机/家庭服务器(住宅IP)运行。"
        )
        raise RuntimeError("IP 被抖音风控，终止任务")
    if page.locator("text=登录").count() > 0 or "登录" in body_text:
        _snapshot(page, prefix="no_login")
        logger.error("页面仍在登录态，cookie 未生效。请确认从 www.douyin.com 登录后导出、且 session 未过期。")
        raise RuntimeError("Cookie 未生效，终止任务")


def _enter_im(page, username):
    """回退路径：当 IM_URL 直达被弹回首页时使用。
    在创作者中心首页滚动到「互动管理」板块，等「私信管理」入口异步加载并点击。"""
    # 互动管理板块在首页一直存在，但里面的私信入口需要等异步请求回来才显示
    logger.debug("等待「互动管理」板块渲染...")
    try:
        page.get_by_text("互动管理", exact=True).first.wait_for(timeout=config["browserTimeout"])
    except Exception as e:
        _snapshot(page, prefix="no_interact_section")
        logger.error(f"未找到「互动管理」板块: {e}")
        raise RuntimeError("未找到「互动管理」板块")

    logger.debug("等待「私信管理」入口异步加载...")
    for i in range(30):
        try:
            entry = page.get_by_text("私信管理", exact=False).first
            if entry.count() > 0:
                entry.click()
                logger.debug("已点击「私信管理」")
                # 等待私信管理页面加载
                try:
                    page.wait_for_load_state("networkidle", timeout=config["browserTimeout"])
                except Exception:
                    pass
                time.sleep(2)
                return
        except Exception:
            pass
        # 入口可能还没加载出来，小步滚动并等待
        page.mouse.wheel(0, 300)
        time.sleep(1)

    _snapshot(page, prefix="no_im_entry")
    logger.error(
        "互动管理板块内未等到「私信管理」入口。可能：①未登录 ②入口文本不是「私信管理」"
        "③异步加载超时。请把 logs/no_im_entry.html 发我确认实际入口。"
    )
    raise RuntimeError("未找到「私信管理」入口")


def _send_to(page, name, message):
    """按昵称定位会话 -> 点击进入 -> 找输入框 -> 发送"""
    conv = page.get_by_text(name, exact=False).first
    conv.wait_for(timeout=config["browserTimeout"])
    conv.click()
    time.sleep(1.5)  # 等待右侧聊天框加载

    # 找输入框：优先 textarea，其次 contenteditable 富文本
    input_box = None
    try:
        input_box = page.locator("textarea").first
        input_box.wait_for(timeout=config["browserTimeout"])
    except Exception:
        input_box = page.locator("[contenteditable='true']").first
        input_box.wait_for(timeout=config["browserTimeout"])

    input_box.click()
    # 还原模板中的 \n 为换行：逐行输入，行间用 Shift+Enter
    lines = message.split("\\n")
    for idx, line in enumerate(lines):
        input_box.press_sequentially(line)
        if idx != len(lines) - 1:
            input_box.press("Shift+Enter")
    time.sleep(0.5)
    page.keyboard.press("Enter")  # 发送


def do_user_task(browser, username, cookies, targets):
    """单账号任务：进创作者中心 -> 私信管理 -> 按昵称匹配会话 -> 发消息"""
    # 视口设宽，避免 headless 下塌成手机版布局
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    context.set_default_navigation_timeout(config["browserTimeout"])
    context.set_default_timeout(config["browserTimeout"])

    page = context.new_page()

    try:
        # 关键：先注入 Cookie 再导航，且【跳过裸访问主页】
        # 抖音对云机房 IP 会在裸主页弹「验证码中间页」(IP 风控)，直奔创作者中心带登录态可避免触发
        context.add_cookies(cookies)
        cookie_names = [c.get("name") for c in cookies]
        logger.debug(f"已注入 {len(cookies)} 条 cookie")
        # 仅打印关键鉴权 cookie 是否【存在】（不打印值，避免泄露），用于排查登录失效
        for key in ("sessionid", "sid_tt", "ttwid", "odin_ticket", "sid_ucp_v1", "ssid_ucp_v1", "uid_tt"):
            logger.debug(f"  鉴权 cookie {key}: {'存在' if key in cookie_names else '缺失'}")

        # 直接进入私信列表（用户提供的直达 URL，绕开首页滚动 + 异步加载）
        retry_operation(
            "导航到私信列表",
            page.goto,
            retries=config["taskRetryTimes"],
            delay=5,
            url=IM_URL,
            wait_until="domcontentloaded",
        )
        # 给列表一点异步加载时间
        time.sleep(3)
        logger.debug(f"私信列表 URL={page.url} title={_safe_title(page)}")
        logger.debug(f"私信列表 body 前 500 字: {_safe_body(page)!r}")

        # 风控识别（创作者中心若仍被云 IP 风控，会在此拦截）
        _detect_risk(page, username, "私信列表页")

        message = build_message()
        matched = set()
        for name in targets:
            try:
                _send_to(page, name, message)
                matched.add(name)
                logger.info(f"账号 {username} -> 已给好友「{name}」发送消息")
                time.sleep(2)
            except Exception as e:
                logger.error(f"账号 {username} 给好友「{name}」发送失败: {e}")
                _snapshot(page, prefix=f"send_fail_{name}")
                traceback.print_exc()

        if not matched:
            _snapshot(page, prefix="no_match")
            logger.warning(
                f"账号 {username} 未匹配到任何目标好友。请确认 MATCH_MODE 与 targets "
                f"是否为好友【原始昵称】，且这些好友在私信管理的最近会话列表中。"
            )
        else:
            logger.info(f"账号 {username} 共向 {len(matched)} 位好友发送消息: {matched}")
    finally:
        context.close()  # 任务完成后关闭上下文


def runTasks():
    playwright, browser = get_browser()
    try:
        logger.info("开始执行任务")
        logger.debug(f"当前配置如下：")
        logger.debug(f"消息模板: {config.get('messageTemplate', '未找到消息模板')}")
        logger.debug(f"匹配模式: {matchMode}")
        for user in userData:
            logger.debug(f"用户: {user.get('username', '未知用户')}, 目标好友: {user['targets']}")

        for user in userData:
            cookies = user["cookies"]
            targets = user["targets"]
            username = user.get("username", "未知用户")
            logger.info(f"开始处理账号 {username}")
            try:
                do_user_task(browser, username, cookies, targets)
                logger.info(f"账号 {username} 任务完成")
            except Exception as e:
                # 顶层兜底：任何未在 do_user_task 内部捕获的异常都记录到日志（含文件日志），避免静默崩溃
                logger.error(f"账号 {username} 任务异常终止: {e}")
                traceback.print_exc()
    finally:
        browser.close()
        playwright.stop()
