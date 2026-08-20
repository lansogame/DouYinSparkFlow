import re
import traceback
import unicodedata
from utils.logger import setup_logger
from utils.config import get_config, get_userData
from core.msg_builder import build_message, build_message_with_openai
from core.browser import get_browser
import time

# ===== 融合说明（v3：创作者中心私信直达 + 逐会话重进 + 发送校验）=====
# 原项目 main 分支死磕 creator.douyin.com 的「互动管理」（抖音已下架），导致超时卡死。
# dev 分支改用 www.douyin.com/chat，但云机房 IP 一访问就被抖音弹「验证码中间页」(IP 风控)。
# v3 关键经验（实测踩坑）：
#   1) 创作者中心无 IP 验证码，可绕开 www 的云机房风控，走私信直达 URL 最稳；
#   2) 点开一个会话后，会话列表会被【隐藏】（单栏布局），必须每个会话重新进入列表再点；
#   3) 输入框是 contenteditable div，页面存在多个，必须用探针实测「能打字」；
#   4) 发送必须校验「输入框被清空 = 真正发出」，否则会假报成功（旧版就栽在这）。

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


def _conversation_item_from_span(span_loc):
    """从昵称 span 上溯到真正可点击的列表项（semi-list-item-body 或 semi-list-item）。

    UoUoio 优化版的经验：点击 span 有时不会触发列表项的点击事件，
    应点击外层列表项 div。若找不到祖先，则退而点击 span 本身。
    """
    try:
        item = span_loc.locator("xpath=ancestor::div[contains(@class, 'semi-list-item-body')]")
        if item.count() > 0:
            return item.first
        item = span_loc.locator("xpath=ancestor::div[contains(@class, 'semi-list-item')]")
        if item.count() > 0:
            return item.first
    except Exception:
        pass
    return span_loc


def _find_conversation(page, nickname):
    """在私信列表中定位目标好友，优先匹配会话昵称 span，其次全局文本"""
    candidates = [nickname, _normalize_name(nickname)]
    seen = set()
    for name in candidates:
        if not name or name in seen:
            continue
        seen.add(name)

        # 优先：会话列表里的昵称 span（class 含 item-header-name，抖音实际 class 多为 item-header-name-xxxx）
        try:
            loc = page.locator('[class*="item-header-name"]').filter(
                has_text=re.compile(re.escape(name), re.IGNORECASE)
            )
            if loc.count() > 0:
                logger.debug(f"通过会话昵称 span 匹配到「{nickname}」/「{name}」")
                return _conversation_item_from_span(loc.first)
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


def _find_editor(page, label):
    """找到【真正能打字】的输入框（创作者中心私信输入框是 contenteditable div）。

    参考 UoUoio 优化版：优先使用 class 前缀为 chat-input- 的 div（创作者中心私信输入框的
    稳定 class 前缀），再用 contenteditable/textarea 兜底。所有候选都必须通过探针字符
    实测「输入能读回」才算数，避免点到搜索框、AI 输入框等假输入框。
    """
    # 先等聊天面板把输入框加载出来（最长 15s）。优先等待 UoUoio 验证过的 chat-input-。
    for sel in ("//div[contains(@class, 'chat-input-')]", "[contenteditable='true']", "textarea"):
        try:
            if sel.startswith("//"):
                page.locator("xpath=" + sel).first.wait_for(state="attached", timeout=15000)
            else:
                page.locator(sel).first.wait_for(state="attached", timeout=15000)
            break
        except Exception:
            continue

    candidates = []
    # 1) UoUoio 版最稳选择器：class 前缀 chat-input-
    try:
        loc = page.locator("xpath=//div[contains(@class, 'chat-input-')]")
        if loc.count() > 0:
            candidates.extend([loc.nth(i) for i in range(min(loc.count(), 3))])
    except Exception:
        pass
    # 2) contenteditable 且 class 含 editor
    try:
        loc = page.locator('[contenteditable="true"][class*="editor" i]')
        if loc.count() > 0:
            candidates.append(loc.last)
    except Exception:
        pass
    # 3) 兜底：contenteditable / textarea
    for sel in ('[contenteditable="true"]', "textarea"):
        try:
            loc = page.locator(sel)
            if loc.count() > 0:
                candidates.append(loc.last)  # 聊天输入框通常位于 DOM 尾部
        except Exception:
            continue

    if not candidates:
        raise RuntimeError(f"「{label}」页面中未找到任何输入框")

    for box in candidates:
        try:
            box.wait_for(state="attached", timeout=3000)
            box.click()
            time.sleep(0.3)
            box.press_sequentially("_probe_")
            time.sleep(0.3)
            got = ""
            try:
                got = (box.inner_text() or "").strip()
            except Exception:
                pass
            # 清空探针字符（JS selectAll + delete，对 contenteditable 最稳）
            try:
                box.evaluate(
                    "el => { el.focus(); document.execCommand('selectAll'); "
                    "document.execCommand('delete'); }"
                )
            except Exception:
                pass
            time.sleep(0.2)
            if "_probe_" in got:
                logger.debug(f"「{label}」输入框验证通过（实测可打字）")
                return box
            logger.debug(f"「{label}」候选输入框无法输入（读回为空），换下一个")
        except Exception as e:
            logger.debug(f"「{label}」候选输入框不可用: {e}")
            continue
    raise RuntimeError(f"「{label}」未能找到可输入的输入框")


def _type_message(input_box, message):
    """清空输入框后逐行输入消息；输入后读回校验，没打进去就用 JS insertText 兜底。

    参考 UoUoio 优化版：发送前必须用 fill('') 或 JS selectAll+delete 清空旧内容，
    避免上次残留的占位符/旧消息混进本次发送。
    """
    input_box.click()
    time.sleep(0.2)

    # 1) 清空输入框（UoUoio：chat_input.fill('')）
    try:
        input_box.fill("")
    except Exception:
        # contenteditable 不一定支持 fill，用 JS selectAll + delete 兜底
        try:
            input_box.evaluate(
                "el => { el.focus(); document.execCommand('selectAll'); "
                "document.execCommand('delete'); }"
            )
        except Exception:
            pass
    time.sleep(0.2)

    # 2) 逐行输入
    lines = message.split("\\n")
    for idx, line in enumerate(lines):
        if idx > 0:
            input_box.press("Shift+Enter")
            time.sleep(0.1)
        input_box.press_sequentially(line, delay=30)
    time.sleep(0.4)

    # 3) 读回校验
    got = ""
    try:
        got = (input_box.inner_text() or "").strip()
    except Exception:
        pass
    first_line = lines[0].strip() if lines else ""
    if first_line and first_line not in got:
        # press_sequentially 没打进去 → JS 直接插入整段文本（contenteditable 最可靠）
        logger.warning(f"按键输入未生效（读回={got[:40]!r}），改用 JS insertText 插入")
        try:
            input_box.evaluate(
                "(el, t) => { el.focus(); document.execCommand('insertText', false, t); }",
                message.replace("\\n", "\n"),
            )
            time.sleep(0.4)
            got = (input_box.inner_text() or "").strip()
        except Exception as e:
            logger.error(f"JS insertText 也失败: {e}")
    logger.debug(f"输入框最终内容: {got[:80]!r}")
    return got


def _send_message(page, input_box, label):
    """发送消息并【校验真正发出】。

    参考 UoUoio 优化版的三重校验：
      1) 发送后等待错误 toast（如「发送失败」「消息不能为空」）出现；
      2) 输入框被清空（即消息已发出）；
      3) 若 Enter 未触发，兜底点击「发送」按钮。
    任一环节报异常或输入框未清空，都返回 False，拒绝假成功。
    """
    error_toast_selector = '[class*="semi-toast-content"]'

    input_box.press("Enter")
    time.sleep(1.2)

    # 1) 检查错误 toast
    try:
        for toast in page.locator(error_toast_selector).all():
            toast_text = toast.inner_text().strip()
            if toast_text:
                logger.error(f"「{label}」发送后检测到错误提示：{toast_text}")
                return False
    except Exception:
        pass

    # 2) 检查输入框是否清空
    remains = ""
    try:
        remains = (input_box.inner_text() or "").strip()
    except Exception:
        pass
    if not remains:
        logger.debug(f"「{label}」Enter 发送成功（输入框已清空）")
        return True

    # 3) Enter 没触发，兜底点「发送」按钮
    logger.debug(f"「{label}」Enter 后输入框仍有内容，改为点击「发送」按钮")
    clicked = False
    try:
        btn = page.get_by_role("button", name="发送").filter(visible=True).last
        if btn.count() > 0 and btn.is_enabled():
            btn.click(force=True)
            clicked = True
            time.sleep(1.2)
    except Exception as e:
        logger.debug(f"「{label}」点击发送按钮失败: {e}")

    if clicked:
        # 再次检查错误 toast
        try:
            for toast in page.locator(error_toast_selector).all():
                toast_text = toast.inner_text().strip()
                if toast_text:
                    logger.error(f"「{label}」点击发送后检测到错误提示：{toast_text}")
                    return False
        except Exception:
            pass
        try:
            remains = (input_box.inner_text() or "").strip()
        except Exception:
            remains = ""
        if not remains:
            logger.debug(f"「{label}」点击「发送」按钮后输入框已清空")
            return True

    logger.warning(f"「{label}」发送后输入框仍残留: {remains[:60]!r}")
    return False


def _open_and_send(page, message, label):
    """在当前已打开的聊天会话中：找可输入框 -> 输入并校验 -> 发送并校验。

    参考 UoUoio 优化版：单条消息支持好友级重试（输入+发送整体重试），
    因为页面偶尔会因网络抖动导致输入框未就绪或发送按钮未响应。
    """
    def _do_send():
        input_box = _find_editor(page, label)
        got = _type_message(input_box, message)
        if not got.strip():
            _snapshot(page, prefix=f"type_fail_{label}")
            raise RuntimeError(f"「{label}」输入消息失败（输入框内容为空）")
        ok = _send_message(page, input_box, label)
        if not ok:
            _snapshot(page, prefix=f"send_fail_{label}")
            raise RuntimeError(f"「{label}」发送失败（输入框未被清空，消息未发出）")
        logger.debug(f"「{label}」消息已确认发送")

    retry_operation(
        f"「{label}」发送消息",
        _do_send,
        retries=config["taskRetryTimes"],
        delay=2,
    )


def _send_top_n(page, n, message, username):
    """按私信列表【置顶顺序】发前 n 个会话，完全不依赖昵称/抖音号/备注。

    适用前提：用户已在抖音私信里把要续火花的好友全部置顶，且它们排在列表最前面。
    置顶项在 DOM 中自然排在最前，取前 n 个即可，规避花体昵称/符号/备注不可见等问题。

    关键：点开一个会话后，创作者中心会把会话列表【隐藏】（变成单栏聊天布局），
    之前正是因此导致「点完第一个就找不到第二个」。所以每个会话都【重新进入私信列表】再点。
    """
    sent = 0
    for i in range(n):
        # 每个会话都重新进入私信列表，规避「点开会话后列表被隐藏」
        retry_operation(
            f"进入私信列表(第{i+1}轮)",
            page.goto,
            retries=config["taskRetryTimes"],
            delay=5,
            url=IM_URL,
            wait_until="domcontentloaded",
        )
        _wait_for_im_list(page)
        time.sleep(1)
        name_spans = page.locator('[class*="item-header-name"]')
        total = name_spans.count()
        if total == 0:
            _snapshot(page, prefix=f"empty_list_{i}")
            raise RuntimeError("私信列表没有任何会话项，可能未登录或列表未加载")
        if i >= total:
            logger.warning(f"会话总数只有 {total} 个，目标第 {i+1} 个不存在，停止")
            break

        span = name_spans.nth(i)
        try:
            nick = span.inner_text(timeout=3000)
        except Exception:
            nick = f"第{i+1}位"
        logger.debug(f"准备发第 {i+1}/{n} 个会话（昵称：{nick}）")
        try:
            # 点击外层列表项（而非 span），避免 span 不触发会话切换
            item = _conversation_item_from_span(span)
            try:
                item.scroll_into_view_if_needed(timeout=3000)
            except Exception:
                pass
            item.click(force=True)
            time.sleep(0.8)  # 等右侧聊天面板切换
            _open_and_send(page, message, nick)
            logger.info(f"账号 {username} -> 已给第 {i+1} 位会话（{nick}）发送消息")
            sent += 1
        except Exception as e:
            logger.error(f"账号 {username} 第 {i+1} 位会话（{nick}）发送失败: {e}")
            _snapshot(page, prefix=f"send_fail_{nick}")
            traceback.print_exc()
        time.sleep(1.5)
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
            # 昵称匹配模式（默认）：按 targets 里的昵称定位会话。
            # 注意：点开会话后列表会被隐藏，因此每个目标都重新进入私信列表再定位。
            matched = set()
            for target in targets:
                nickname, _ = _parse_target(target)
                try:
                    retry_operation(
                        "回到私信列表",
                        page.goto,
                        retries=config["taskRetryTimes"],
                        delay=5,
                        url=IM_URL,
                        wait_until="domcontentloaded",
                    )
                    _wait_for_im_list(page)
                    time.sleep(1)
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
