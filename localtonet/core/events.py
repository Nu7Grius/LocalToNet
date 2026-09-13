# -*- coding: utf-8 -*-
"""
localtonet.core.events —— 事件总线
====================================
**扩展点**：核心链路只负责"发生了什么"，不关心"谁想知道"。
GUI 的表格刷新、指标统计、审计日志都作为订阅者挂上来，
因此后续加 tkinter 界面或 Prometheus 指标时，服务端/客户端主逻辑一行都不用改。

约定：
* 订阅者的异常**必须被吞掉并记日志**。一个写坏的 GUI 回调不该拖垮整条隧道。
* ``emit`` 是同步调用，订阅者内部要发异步操作请自己 ``create_task``。
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any, Callable, Dict, List, Optional

from logging_setup import get_logger

__all__ = ["EventType", "EventBus"]


class EventType:
    """事件名常量。"""

    SERVER_STARTED = "server_started"
    SERVER_STOPPED = "server_stopped"

    CLIENT_CONNECTED = "client_connected"
    CLIENT_DISCONNECTED = "client_disconnected"

    MAPPING_CHANGED = "mapping_changed"

    REQUEST_START = "request_start"
    REQUEST_END = "request_end"
    CONN_ERROR = "conn_error"

    CLIENT_REGISTERED = "client_registered"
    RECONNECTING = "reconnecting"
    CONTROL_CONNECTED = "control_connected"
    CONTROL_LOST = "control_lost"

    DATA_CHANNEL_OPENED = "data_channel_opened"


class EventBus:
    """极简同步事件总线。"""

    def __init__(self, logger: Optional[logging.Logger] = None) -> None:
        self._subscribers: Dict[str, List[Callable[..., None]]] = defaultdict(list)
        self._log = logger or get_logger("events")

    def on(self, event: str, handler: Callable[..., None]) -> Callable[[], None]:
        """订阅事件，返回一个"取消订阅"的可调用对象。"""
        self._subscribers[event].append(handler)

        def unsubscribe() -> None:
            try:
                self._subscribers[event].remove(handler)
            except ValueError:
                pass

        return unsubscribe

    def once(self, event: str, handler: Callable[..., None]) -> Callable[[], None]:
        """只触发一次的订阅。"""
        unsubscribe: Callable[[], None] = lambda: None

        def wrapper(**payload: Any) -> None:
            unsubscribe()
            handler(**payload)

        unsubscribe = self.on(event, wrapper)
        return unsubscribe

    def emit(self, event: str, **payload: Any) -> None:
        """广播事件。订阅者报错只记日志，不影响发布方。"""
        for handler in tuple(self._subscribers.get(event, ())):
            try:
                handler(**payload)
            except Exception:  # noqa: BLE001 - 订阅者是外部代码，必须隔离
                self._log.exception("事件订阅者处理 %s 时异常：%r", event, handler)

    def subscriber_count(self, event: str) -> int:
        return len(self._subscribers.get(event, ()))

    def clear(self) -> None:
        self._subscribers.clear()
