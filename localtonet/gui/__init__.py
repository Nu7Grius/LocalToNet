# -*- coding: utf-8 -*-
"""
localtonet.gui —— 可视化界面（tkinter）：客户端界面 + 服务端管理台
==================================================================
GUI 是**壳**：它自己不实现任何映射、连接或协议逻辑，只做三件事——

1. 把 :meth:`~localtonet.client.core.TunnelClient.snapshot`（或服务端的
   :meth:`~localtonet.server.core.TunnelServer.snapshot`）的公开状态画出来；
2. 订阅 :class:`localtonet.core.events.EventBus`，把事件变成日志行；
3. 用户点"提交"时调用已经封装好的写入口（客户端走 ``set_mapping`` 指令，
   管理台直接调 ``TunnelServer.submit_mapping``）。

因此两期界面**一行协议都没改**：新增界面不需要对端配合。

模块划分（关键约束：tkinter 只允许出现在 ``*_app.py`` 与 ``widgets`` 里）：

| 模块 | 职责 | 依赖 tkinter |
| --- | --- | --- |
| :mod:`~localtonet.gui.model` | 客户端：映射表编辑缓冲区 + 界面状态（纯逻辑） | 否 |
| :mod:`~localtonet.gui.bridge` | asyncio 线程 ↔ tkinter 线程的桥（两端共用） | 否 |
| :mod:`~localtonet.gui.controller` | 拉起客户端、订阅事件、提交映射 | 否 |
| :mod:`~localtonet.gui.viewmodel` | 邮筒消息 → 表格与状态栏（可无头测试） | 否 |
| :mod:`~localtonet.gui.server_model` | 服务端：在线客户端行 + 管理台状态（纯逻辑） | 否 |
| :mod:`~localtonet.gui.server_controller` | 拉起/关停服务端、订阅事件、提交映射 | 否 |
| :mod:`~localtonet.gui.server_viewmodel` | 邮筒消息 → 在线表与映射表（可无头测试） | 否 |
| :mod:`~localtonet.gui.app` | 客户端窗口、表格、按钮、状态栏 | **是** |
| :mod:`~localtonet.gui.server_app` | 管理台窗口（在线客户端表 + 映射表） | **是** |
| :mod:`~localtonet.gui.widgets` | 两个界面共用的构件（日志面板、编辑对话框、颜色） | **是** |

正因为本文件**不 import 任何 ``*_app`` 模块**，在没有 tkinter 的机器上
``import localtonet.gui`` 依然成功——"无界面环境核心包照常可用"是这条 import
边界守住的，而不是靠承诺。
"""

from __future__ import annotations

from localtonet.gui.bridge import LoopThread, UiBridge
from localtonet.gui.controller import GuiController
from localtonet.gui.model import (
    GuiState,
    LogEntry,
    MappingRow,
    MappingTableModel,
    apply_event,
    note_mapping_result,
)
from localtonet.gui.server_model import ClientRow, ServerState
from localtonet.gui.server_viewmodel import ServerViewModel
from localtonet.gui.viewmodel import GuiViewModel

__all__ = [
    "ClientRow",
    "GuiController",
    "GuiState",
    "GuiViewModel",
    "LogEntry",
    "LoopThread",
    "MappingRow",
    "MappingTableModel",
    "ServerState",
    "ServerViewModel",
    "UiBridge",
    "apply_event",
    "create_app",
    "create_server_app",
    "note_mapping_result",
]


def create_app(*args: object, **kwargs: object) -> object:
    """延迟构造客户端界面。``tkinter`` 缺失时抛出可读的 :class:`RuntimeError`。

    刻意不在模块顶层 import ``app``：那样会让"没有 tkinter 的机器"
    连 ``localtonet.gui`` 都导不进来，把可选依赖变成了硬依赖。
    """
    try:
        from localtonet.gui.app import TunnelGuiApp
    except ImportError as exc:  # pragma: no cover - 仅在无 tkinter 环境触发
        raise RuntimeError(
            "当前 Python 没有可用的 tkinter，无法启动图形界面。"
            "Windows/macOS 官方发行版自带；Debian/Ubuntu 需要安装系统包 python3-tk。"
            "命令行客户端不受影响：python client.py --server <host> --local-ports <port>"
        ) from exc
    return TunnelGuiApp(*args, **kwargs)


def create_server_app(*args: object, **kwargs: object) -> object:
    """延迟构造服务端管理台。``tkinter`` 缺失时的处理与 :func:`create_app` 一致。"""
    try:
        from localtonet.gui.server_app import ServerGuiApp
    except ImportError as exc:  # pragma: no cover - 仅在无 tkinter 环境触发
        raise RuntimeError(
            "当前 Python 没有可用的 tkinter，无法启动管理台。"
            "Windows/macOS 官方发行版自带；Debian/Ubuntu 需要安装系统包 python3-tk。"
            "命令行服务端不受影响：python server.py --mapping 9028:8000"
        ) from exc
    return ServerGuiApp(*args, **kwargs)
