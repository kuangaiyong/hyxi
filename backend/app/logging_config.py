"""统一日志配置"""

import logging
import sys
import os
from logging.handlers import RotatingFileHandler
from app.config import settings

_logger = None


def get_logger(name: str = "hyxi") -> logging.Logger:
    """获取全局 logger 实例"""
    global _logger
    if _logger is not None:
        return _logger

    _logger = logging.getLogger(name)
    _logger.setLevel(logging.INFO)

    # 控制台 handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter(
        "[%(asctime)s] %(levelname)-7s %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    _logger.addHandler(console_handler)

    # 文件 handler（滚动日志，每个文件最大 5MB，保留 3 个）
    try:
        log_dir = os.path.join(settings.data_dir, "logs")
        os.makedirs(log_dir, exist_ok=True)
        file_handler = RotatingFileHandler(
            os.path.join(log_dir, "app.log"),
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(
            "[%(asctime)s] %(levelname)-7s %(name)s | %(filename)s:%(lineno)d | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        _logger.addHandler(file_handler)
    except Exception:
        _logger.warning("无法创建日志文件 handler，仅输出到控制台")

    return _logger
