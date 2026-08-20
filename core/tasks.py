import re
import traceback
import unicodedata
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
# 匹配模式（MATCH_MODE 环境变量）：
#   nickname（默认）：按 targets 里的好友昵称定位会话；对花体/特殊 Unicode 会自动 NFKD 归一化。
#   top / topN：不按文本匹配，直接按私信列表【置顶顺序】发前 N 个会话（N = targets 数量）。
#               适用前提：要续火花的好友已全部置顶且排在最前。创作者中心不显示备注/抖音号，
#               昵称又常有花体符号，此模式最省心、最稳。
# targets 写法：
#   字符串昵称：["粉芋球", "Serendipity🌟"]（nickname 模式）
#   对象（推荐 nickname 模式）：[{"nickname": "粉芋球", "short_id": "xxx"}, ...]
#   top 模式：targets 只需数量 = 目标数，内容随意（如 ["", "", ""] 或占位昵称）。
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


def _normalize_name(name):
    """把数学花体、手写体等特殊 Unicode 昵称归一化为普通 ASCII 文本，便于匹配"""
    if not name:
        return ""
    # NFKD 拆分兼容字符（例如 𝒗𝒆𝒓𝒔𝒕𝒂𝒑𝒑𝒆𝒏 → verstappen）
    return unicodedata.normalize("NFKD", name).strip()


def _parse_target(target):
    """解析 target：支持字符串昵称或 {nickname, short_id} 对象"""
    if isinstance(target, dict):
        nickname = target.get("nickname", "")
        short_id = target.get("short_id", "") or target.get("unique_id", "")
        return str(nickname), str(short_id) if short_id else ""
    return str(target), ""


def _find_conversation(page, nickname):
    """在私信列表中定位目标好友，优先匹配会话昵称 span，其次全局文本"""
    candidates = [nickname, _normalize_name(nickname)]
    seen = set()
    for name in candidates:
        if not name or name in seen:
            continue
        seen.add(name)

        # 优先：会话列表里的昵称 span（class 含 item-header-name）
        # 用正则忽略大小写，避免特殊字符导致 Playwright text selector 异常
        try:
            loc = page.locator('[class*="item-header-name"]').filter(
                has_text=re.compile(re.escape(name), re.IGNORECASE)
            )
            if loc.count() > 0:
                logger.debug(f"通过会话昵称 span 匹配到「{nickname}」/「{name}」")
                return loc.first
        except Exception as e:
            logger.debug(f"匹配昵称 span 失败: {e}")

        # 兜底：全局文本（子串匹配）
        try:
            loc = page.get_by_text(name, exact=False)
            if loc.count() > 0:
                logger.debug(f"通过全局文本匹配到「{nickname}」/「{name}」")
                return loc.first
        except Exception as e:
            logger.debug(f"全局文本匹配失败: {e}")

    return None


def _wait_for_im_list(page, timeout=90000):
    """等待私信会话列表真正渲染（白屏加载后，昵称 span 才出现）。

    注意：创作者中心私信页会白屏加载好一会，期间没有任何「加载中」文本，
    不能用固定 sleep，必须等昵称 span 元素真正挂载到 DOM。
    """
    try:
        page.locator('[class*="item-header-name"]').first.wait_for(
            state="attached", timeout=timeout
        )
        logger.debug("私信会话列表已渲染（昵称 span 出现）")
        return True
    except Exception:
        logger.warning("等待私信列表渲染超时，仍尝试继续")
        return False


def _send_to(page, target, message):
    """按昵称定位会话 -> 滚动点击 -> 确认进入聊天详情 -> 输入框发送 -> Enter + 兜底发送按钮"""
    nickname, short_id = _parse_target(target)
    if not nickname:
        raise ValueError("target 缺少昵称")

    logger.debug(f"开始给「{nickname}」发消息（short_id={short_id or '无'}）")

    conv = _find_conversation(page, nickname)
    if not conv:
        raise RuntimeError(f"私信列表中未找到「{nickname}」（归一化后：{_normalize_name(nickname)}）")

    # 元素可能在当前视口外或被标记 hidden，只要 DOM 存在即可，滚动后 force 点击
    conv.wait_for(state="attached", timeout=config["browserTimeout"])
    conv.scroll_into_view_if_needed()
    time.sleep(0.5)
    conv.click(force=True)
    logger.debug(f"已点击会话「{nickname}」")
    _open_and_send(page, message, nickname)


def _open_and_send(page, message, label):
    """在当前已打开的聊天会话中：等输入框 -> 逐行输入 -> Enter + 兜底发送按钮"""
    # 等待右侧聊天详情加载：先等一个可见的输入框出现
    input_box = None
    try:
        input_box = page.locator("textarea").filter(visible=True).first
        input_box.wait_for(timeout=15000)
    except Exception:
        try:
            input_box = page.locator("[contenteditable='true']").filter(visible=True).first
            input_box.wait_for(timeout=15000)
        except Exception as e:
            raise RuntimeError(f"未找到聊天输入框: {e}")

    try:
        tag = input_box.evaluate("el => el.tagName")
    except Exception:
        tag = "未知"
    logger.debug(f"「{label}」已定位输入框，tag={tag}")

    # 聚焦并逐行输入
    input_box.click()
    time.sleep(0.3)
    lines = message.split("\\n")
    for idx, line in enumerate(lines):
        if idx > 0:
            input_box.press("Shift+Enter")
            time.sleep(0.1)
        input_box.press_sequentially(line)
    time.sleep(0.5)

    # 发送：先按 Enter，再兜底点「发送」按钮（防止 Enter 没触发）
    input_box.press("Enter")
    logger.debug(f"已按 Enter 发送给「{label}」")
    time.sleep(0.8)

    try:
        send_btn = page.get_by_role("button", name="发送").filter(visible=True).first
        if send_btn.count() > 0:
            send_btn.click(force=True)
            logger.debug(f"已兜底点击「发送」按钮给「{label}」")
    except Exception:
        pass


def _send_top_n(page, n, message, username):
    """按私信列表【置顶顺序】发前 n 个会话，完全不依赖昵称/抖音号/备注。

    适用前提：用户已在抖音私信里把要续火花的好友全部置顶，且它们排在列表最前面。
    置顶项在 DOM 中自然排在最前，取前 n 个即可，规避花体昵称/符号/备注不可见等问题。
    """
    # 双保险：确保列表已渲染（白屏加载场景）
    _wait_for_im_list(page)
    time.sleep(1)
    name_spans = page.locator('[class*="item-header-name"]')
    total = name_spans.count()
    logger.debug(f"私信列表共 {total} 个会话，准备按置顶顺序发前 {n} 个")
    if total == 0:
        raise RuntimeError("私信列表没有任何会话项，可能未登录或列表未加载")
    if n > total:
        logger.warning(f"目标数 {n} 大于实际会话数 {total}，将只发前 {total} 个")
        n = total

    sent = 0
    for i in range(n):
        span = name_spans.nth(i)
        try:
            nick = span.inner_text(timeout=3000)
        except Exception:
            nick = f"第{i+1}位"
        logger.debug(f"准备发第 {i+1}/{n} 个会话（昵称：{nick}）")
        span.scroll_into_view_if_needed()
        time.sleep(0.4)
        span.click(force=True)
        try:
            _open_and_send(page, message, nick)
            logger.info(f"账号 {username} -> 已给第 {i+1} 位会话（{nick}）发送消息")
            sent += 1
        except Exception as e:
            logger.error(f"账号 {username} 第 {i+1} 位会话（{nick}）发送失败: {e}")
            _snapshot(page, prefix=f"send_fail_{nick}")
            traceback.print_exc()
        time.sleep(2)
    logger.info(f"账号 {username} 按置顶顺序共发送 {sent}/{n} 个会话")


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
        # 私信页会白屏加载好一会（无「加载中」文本），必须等会话列表真正渲染
        _wait_for_im_list(page)
        time.sleep(1.5)  # 等列表稳定
        logger.debug(f"私信列表 URL={page.url} title={_safe_title(page)}")
        logger.debug(f"私信列表 body 前 500 字: {_safe_body(page)!r}")

        # 风控识别（创作者中心若仍被云 IP 风控，会在此拦截）
        _detect_risk(page, username, "私信列表页")

        message = build_message()
        if matchMode in ("top", "topN"):
            # 置顶顺序模式：发前 len(targets) 个置顶会话，不依赖任何文本匹配
            logger.info(
                f"匹配模式=置顶顺序：按私信列表前 {len(targets)} 个置顶会话发送"
                f"（请确保要续火花的好友已全部置顶且排在最前面，targets 数量 = 目标数）"
            )
            _send_top_n(page, len(targets), message, username)
        else:
            # 昵称匹配模式（默认）：按 targets 里的昵称定位会话
            matched = set()
            for target in targets:
                nickname, _ = _parse_target(target)
                try:
                    _send_to(page, target, message)
                    matched.add(nickname)
                    logger.info(f"账号 {username} -> 已给好友「{nickname}」发送消息")
                    time.sleep(2)
                except Exception as e:
                    logger.error(f"账号 {username} 给好友「{nickname}」发送失败: {e}")
                    _snapshot(page, prefix=f"send_fail_{nickname}")
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
