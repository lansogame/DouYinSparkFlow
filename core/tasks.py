import traceback
from utils.logger import setup_logger
from utils.config import get_config, get_userData
from core.msg_builder import build_message, build_message_with_openai
from core.browser import get_browser
import time
import json

# ===== 融合说明 =====
# 原项目 main 分支死磕 creator.douyin.com 的「互动管理」（抖音已下架），
# dev 分支改用 www.douyin.com/chat 但选择器已失效。
# 这里把「进入聊天页 + 找会话 + 发消息」替换为目录项目 DkoBot 中
# 已被实测稳定可用的选择器（翻译成 Playwright），其余配置系统原样保留。

config = get_config()
userData = get_userData()
logger = setup_logger(level=config.get("logLevel", "Info"))
# DkoBot 的实测逻辑是按「会话列表显示的好友名」匹配，故 nickname 最稳。
# 若你填的是抖音号(short_id)，请改成 nickname 并在 targets 里填好友【原始昵称】。
matchMode = config.get("matchMode", "nickname")

# 稳定的 chat 入口（DkoBot 实测可用）
CHAT_URL = "https://www.douyin.com/chat?isPopup=1"

# 会话列表容器（用 contains 容忍抖音的哈希类名后缀，如 ...Listwrapper-3k2fA）
LIST_WRAPPER = 'xpath=//div[contains(@class, "conversationConversationListwrapper")]'
# 单个会话项
CONV_ITEM = 'xpath=//div[contains(@class, "conversationConversationListwrapper")]/div/div/div'
# 会话项内的好友名（DkoBot 验证过的路径）
CONV_NAME = 'xpath=./div[1]/div[2]/div[1]/div[1]'
# 消息输入框（DkoBot 验证过的路径）
CHAT_INPUT = 'xpath=//div[contains(@class, "messageEditorimChatEditorContainer")]'


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


def get_conversations(page):
    """读取当前可见的会话列表，返回 [(locator, 好友名), ...]"""
    items = page.locator(CONV_ITEM).all()
    convs = []
    for item in items:
        name = ""
        try:
            name = item.locator(CONV_NAME).inner_text().strip()
        except Exception:
            try:
                # 兜底：直接取整项首行文本作为昵称
                name = item.inner_text().strip().split("\n")[0]
            except Exception:
                name = ""
        if name:
            convs.append((item, name))
    return convs


def scroll_conversation_list(page):
    """尝试滚动会话列表容器以加载更多（应对好友较多的情况）"""
    try:
        container = page.locator(LIST_WRAPPER).element_handle()
        if not container:
            return
        for _ in range(4):
            before = page.evaluate("(el) => el.scrollTop", container)
            page.evaluate("(el) => el.scrollTop += 800", container)
            time.sleep(0.5)
            after = page.evaluate("(el) => el.scrollTop", container)
            if before == after:
                break
        time.sleep(1)
    except Exception:
        pass


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


def do_user_task(browser, username, cookies, targets):
    """单账号任务：进 chat 页 -> 按昵称匹配会话 -> 发消息"""
    # 视口设宽，避免 headless 下塌成手机版布局（DkoBot 桌面验证过）
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    context.set_default_navigation_timeout(config["browserTimeout"])
    context.set_default_timeout(config["browserTimeout"])

    page = context.new_page()

    try:
        # 先访问主页再注入 Cookie（与 DkoBot 一致：www.douyin.com 域）
        retry_operation(
            "打开抖音主页",
            page.goto,
            retries=config["taskRetryTimes"],
            delay=5,
            url="https://www.douyin.com/",
            wait_until="domcontentloaded",
        )
        # 避免在页面继续导航时读 title() 触发 "Execution context was destroyed"
        try:
            home_title = page.title()
        except Exception as e:
            home_title = f"(获取失败: {e})"
        logger.debug(f"主页加载后 URL={page.url} title={home_title}")

        # 注入 Cookie（douyin.com 各子域通用）
        context.add_cookies(cookies)
        logger.debug(f"已注入 {len(cookies)} 条 cookie")

        # 进入 chat 页面（DkoBot 实测稳定入口）
        retry_operation(
            "导航到聊天页",
            page.goto,
            retries=config["taskRetryTimes"],
            delay=5,
            url=CHAT_URL,
            wait_until="domcontentloaded",
        )
        try:
            chat_title = page.title()
        except Exception as e:
            chat_title = f"(获取失败: {e})"
        logger.debug(f"聊天页加载后 URL={page.url} title={chat_title}")

        # 如果 cookie 生效，这里应该会重定向/显示聊天列表；如果没生效，可能还在登录页
        body_text = ""
        try:
            body_text = page.locator("body").inner_text(timeout=5000)[:500]
        except Exception as e:
            logger.warning(f"读取聊天页 body 文本失败: {e}")
        logger.debug(f"聊天页 body 前 500 字: {body_text!r}")

        logger.debug(f"账号 {username} 等待会话列表加载")
        try:
            page.wait_for_selector(LIST_WRAPPER, timeout=config["browserTimeout"])
        except Exception:
            logger.error(f"账号 {username} 等待会话列表超时，准备诊断截图")
            _snapshot(page, prefix="timeout")
            # 再检查常见阻塞：登录按钮 / 验证码 / 空白页
            if page.locator("text=登录").count() > 0 or "登录" in body_text:
                logger.error("页面仍在登录态，cookie 未生效。请重新导出 www.douyin.com 的 Cookie 并更新 Secret。")
            elif page.locator("text=验证").count() > 0 or "验证" in body_text or "captcha" in page.content().lower():
                logger.error("页面出现人机验证/滑块，GitHub Actions IP 被风控。")
            raise

        time.sleep(config["friendListTimeout"] / 1000)

        scroll_conversation_list(page)
        convs = get_conversations(page)
        logger.debug(f"账号 {username} 当前可见会话 {len(convs)} 个: {[n for _, n in convs]}")

        matched = set()
        for item, name in convs:
            # 两种模式都按「会话显示名」匹配（DkoBot 实测逻辑）
            if name not in targets:
                continue
            if name in matched:
                continue
            try:
                item.click()
                time.sleep(1.5)  # 等待右侧聊天框加载（DkoBot 实测等待时长）
                # 等待输入框出现
                page.wait_for_selector(CHAT_INPUT, timeout=config["browserTimeout"])
                chat_input = page.locator(CHAT_INPUT)
                chat_input.click()

                message = build_message()
                # 还原模板中的 \n 为换行：逐行输入，行间用 Shift+Enter
                lines = message.split("\\n")
                for idx, line in enumerate(lines):
                    chat_input.press_sequentially(line)
                    if idx != len(lines) - 1:
                        chat_input.press("Shift+Enter")
                time.sleep(0.5)
                page.keyboard.press("Enter")  # 发送
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
                f"账号 {username} 未匹配到任何目标好友（共扫描 {len(convs)} 个会话）。"
                f"请确认 MATCH_MODE 与 targets 是否为好友【原始昵称】，且这些好友在最近会话列表中。"
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
