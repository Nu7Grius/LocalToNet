# -*- coding: utf-8 -*-
"""
tests/test_logging_setup.py —— 日志父目录必须自动创建（回归保护）
===================================================================
守的是一个"看不见"的缺陷：``logging.FileHandler`` **不会创建父目录**，
父目录不存在时直接抛 ``FileNotFoundError``。

为什么值得单独立文件守：本项目四个入口（``client.py`` / ``server.py`` / ``gui.py`` /
``server_gui.py``）都支持无控制台运行（Windows 用 ``pythonw.exe`` + 启动文件夹快捷方式开机自启，
服务端用 systemd）。一旦 ``logs/`` 目录缺失（被清理、换机器重新 clone 仓库），
进程会**静默死亡**——没有控制台、没有日志、没有任何可见症状。

本文件守四条线：

1. **父目录缺失时被创建**：多级路径 ``a/b/c/x.log``，目录建出来且日志**真能写进去**。
2. **父目录已存在不报错**：``exist_ok=True`` 语义；文件按追加模式打开，旧内容不被清空。
3. **裸文件名不炸**：无目录部分的相对名按**当前工作目录**解析（``os.path.abspath`` 的作用）。
4. **回归保护**：``log_file=""`` 时只挂 console handler，**不创建任何文件**。

隔离铁律（否则会污染同进程内其它用例）：
``setup_logging`` 会清掉旧 handler 并把 ``localtonet.propagate`` 置为 ``False``——
``tests/test_auth_tokens.py:132`` 与 ``tests/test_mapping_store.py:210`` 的注释都记了这个坑。
所以每个用例都走 ``isolated_logger`` 夹具复原 logger 状态，并 ``close()`` 自己加的 handler
（Windows 上不关会占住文件句柄，``tmp_path`` 清理报 ``PermissionError``）。
"""

from __future__ import annotations

import logging
from typing import Iterator

import pytest

from logging_setup import LOGGER_NAME, get_logger, setup_logging


@pytest.fixture
def isolated_logger() -> Iterator[logging.Logger]:
    """快照 ``localtonet`` 父日志器，用例结束复原 handlers / level / propagate。"""
    logger = logging.getLogger(LOGGER_NAME)
    saved_handlers = list(logger.handlers)
    saved_level = logger.level
    saved_propagate = logger.propagate
    try:
        yield logger
    finally:
        # close() 是必须的：否则 Windows 上句柄被占，tmp_path 清理报 PermissionError。
        for handler in list(logger.handlers):
            if handler not in saved_handlers:
                logger.removeHandler(handler)
                handler.close()
        for handler in saved_handlers:
            if isinstance(handler, logging.StreamHandler) and handler.stream is None:
                # setup_logging 已经 close 过它（stream 置 None），挂回去只会产出噪声。
                continue
            logger.addHandler(handler)
        logger.setLevel(saved_level)
        logger.propagate = saved_propagate


def test_creates_missing_parent_directories(tmp_path, isolated_logger) -> None:
    """父目录整体缺失（多级）时自动建目录，并且日志真的落到了盘上。"""
    target = tmp_path / "a" / "b" / "c" / "x.log"
    assert not target.parent.exists()

    setup_logging("INFO", str(target))

    assert target.parent.is_dir()
    assert target.is_file()
    assert len(isolated_logger.handlers) == 2  # console + file

    get_logger("tests.logging").info("落盘验证")
    for handler in isolated_logger.handlers:
        handler.flush()

    assert "落盘验证" in target.read_text(encoding="utf-8")


def test_existing_parent_directory_is_reused(tmp_path, isolated_logger) -> None:
    """父目录已存在时再调一次仍成功，且是追加而不是截断。"""
    target = tmp_path / "logs" / "client.log"
    setup_logging("INFO", str(target))
    first_handlers = list(isolated_logger.handlers)
    assert len(first_handlers) == 2

    get_logger("tests.logging").info("第一行")
    for handler in first_handlers:
        handler.flush()

    setup_logging("INFO", str(target))  # 第二次：exist_ok=True 语义

    assert target.parent.is_dir()
    assert len(isolated_logger.handlers) == 2
    assert isolated_logger.handlers != first_handlers  # 旧 handler 被换掉，不会双写

    get_logger("tests.logging").info("第二行")
    for handler in isolated_logger.handlers:
        handler.flush()

    content = target.read_text(encoding="utf-8")
    assert "第一行" in content  # 旧内容没被清空
    assert "第二行" in content
    assert content.count("第一行") == 1  # 旧句柄已摘除，没有重复输出


def test_bare_filename_resolves_to_cwd(tmp_path, monkeypatch, isolated_logger) -> None:
    """无目录部分的裸文件名不炸：按当前工作目录解析（abspath 的唯一用途）。"""
    monkeypatch.chdir(tmp_path)

    setup_logging("INFO", "client.log")  # 不该抛异常

    assert (tmp_path / "client.log").is_file()
    assert len(isolated_logger.handlers) == 2


def test_empty_log_file_keeps_console_only(tmp_path, monkeypatch, isolated_logger) -> None:
    """回归保护：``log_file=""`` 只挂 console handler，不落任何文件。"""
    monkeypatch.chdir(tmp_path)

    setup_logging("INFO", "")

    handlers = isolated_logger.handlers
    assert len(handlers) == 1
    assert not isinstance(handlers[0], logging.FileHandler)
    assert list(tmp_path.iterdir()) == []
