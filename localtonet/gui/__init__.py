# -*- coding: utf-8 -*-
"""
localtonet.gui —— 可视化客户端（tkinter）
==========================================
GUI 是**壳**：它自己不实现任何映射、连接或协议逻辑，只做三件事——

1. 把 :class:`localtonet.client.core.TunnelClient` 的公开状态（``snapshot()``、
   ``remote_mapping``）画出来；
2. 订阅它的 :class:`localtonet.core.events.EventBus`，把事件变成日志行；
3. 用户点"提交"时调用它已经封装好的 :meth:`TunnelClient.set_mapping`。

因此本轮**一行协议都没改**：新增界面不需要服务端配合。

模块划分（关键约束：tkinter 只允许出现在 ``app`` 里）：

| 模块 | 职责 | 依赖 tkinter |
| --- | --- | --- |
| :mod:`~localtonet.gui.model` | 映射表编辑缓冲区 + 界面状态（纯逻辑、可无头测试） | 否 |
| :mod:`~localtonet.gui.bridge` | asyncio 线程 ↔ tkinter 线程的桥 | 否 |
| :mod:`~localtonet.gui.controller` | 拉起客户端、订阅事件、提交映射 | 否 |
| :mod:`~localtonet.gui.viewmodel` | 邮筒消息 → 表格与状态栏（可无头测试） | 否 |
| :mod:`~localtonet.gui.app` | 窗口、表格、按钮、状态栏、日志面板 | **是** |

正因为本文件**不 import app**，在没有 tkinter 的机器上 ``import localtonet.gui``
依然成功——"无界面环境核心包照常可用"是这条 import 边界守住的，而不是靠承诺。
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
from localtonet.gui.viewmodel import GuiViewModel

__all__ = [
    "GuiController",
    "GuiState",
    "GuiViewModel",
    "LogEntry",
    "LoopThread",
    "MappingRow",
    "MappingTableModel",
    "UiBridge",
    "apply_event",
    "note_mapping_result",
    "create_app",
]


def create_app(*args: object, **kwargs: object) -> object:
    """延迟构造 tkinter 应用。``tkinter`` 缺失时抛出可读的 :class:`RuntimeError`。

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
