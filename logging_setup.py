# -*- coding: utf-8 -*-
"""
logging_setup.py —— 统一日志配置
=================================
约定（与团队规范一致）：

* 业务代码**禁止 print**，一律走 `get_logger("模块名")`。
* 日志器层级统一挂在 ``localtonet`` 之下，便于一处控制全局级别。
* 消息格式固定为 ``时间 级别 [模块] 内容``，不加颜色、不加多余装饰，
  方便直接重定向到文件后 grep。
"""

from __future__ import annotations

import logging
import os
import sys
from typing import IO, Optional

__all__ = ["LOGGER_NAME", "setup_logging", "get_logger"]

LOGGER_NAME = "localtonet"

_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
_DATE_FORMAT = "%H:%M:%S"


def setup_logging(
    level: str = "INFO",
    log_file: str = "",
    stream: Optional[IO[str]] = None,
) -> logging.Logger:
    """初始化根日志器。重复调用会先清理旧 handler，避免日志重复输出。"""
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level.upper())
    logger.propagate = False

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(_FORMAT, datefmt=_DATE_FORMAT)

    console = logging.StreamHandler(stream if stream is not None else sys.stdout)
    console.setFormatter(formatter)
    logger.addHandler(console)

    if log_file:
        # FileHandler 不会创建父目录；父目录缺失会直接抛 FileNotFoundError。
        # 无控制台运行（pythonw / systemd）时那就是"静默死亡"，这里先补上目录。
        directory = os.path.dirname(os.path.abspath(log_file))
        if directory:
            os.makedirs(directory, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger


def get_logger(suffix: str = "") -> logging.Logger:
    """取得子日志器，例如 ``get_logger("server.core")``。"""
    return logging.getLogger(f"{LOGGER_NAME}.{suffix}" if suffix else LOGGER_NAME)
