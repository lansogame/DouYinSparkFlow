import logging
import os
from logging.handlers import RotatingFileHandler

# 创建 logs 文件夹（如果不存在）
if not os.path.exists("logs"):
    os.makedirs("logs")

# 日志格式
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s"

# 日志文件路径
LOG_FILE = "logs/app.log"

# 配置日志
def setup_logger(name="app", level="Info"):
    """
    配置日志记录器
    :param name: 日志记录器名称
    :param level: 日志级别（大小写不敏感：Debug/debug/DEBUG 均可）
    :return: 配置好的日志记录器
    """
    # 大小写不敏感映射，避免 "Debug"/"debug"/"DEBUG" 因精确匹配失败而退回 INFO
    level_map = {
        "debug": logging.DEBUG,
        "info": logging.INFO,
        "warning": logging.WARNING,
        "error": logging.ERROR,
    }
    if isinstance(level, str):
        level = level_map.get(level.strip().lower(), logging.INFO)

    logger = logging.getLogger(name)
    logger.setLevel(level)

    # 防止重复添加处理器
    if not logger.handlers:
        # 控制台日志处理器
        console_handler = logging.StreamHandler()
        console_formatter = logging.Formatter(LOG_FORMAT)
        console_handler.setFormatter(console_formatter)

        # 文件日志处理器（带日志轮转）
        file_handler = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
        file_formatter = logging.Formatter(LOG_FORMAT)
        file_handler.setFormatter(file_formatter)

        # 添加处理器到日志记录器
        logger.addHandler(console_handler)
        logger.addHandler(file_handler)

    # 无论首次还是后续调用，都按最新 level 同步所有 handler，避免级别被首次调用锁定
    for handler in logger.handlers:
        handler.setLevel(level)

    return logger


# 示例：使用日志记录器
if __name__ == "__main__":
    logger = setup_logger(level="Debug")
    logger.debug("这是一个调试信息")
    logger.info("这是一个普通信息")
    logger.warning("这是一个警告信息")
    logger.error("这是一个错误信息")
    logger.critical("这是一个严重错误信息")
