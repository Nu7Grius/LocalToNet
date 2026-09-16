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
    MAPPING_REJECTED = "mapping_rejected"
    """客户端的 ``set_mapping`` 因**身份无映射表写权限**被拒。

    只有"没权限"这一种原因会发它；mapping 格式非法之类仍只进日志——
    前者是安全事件（有价值，管理台要看），后者是调用方自己的 bug（会刷屏）。"""

    REGISTRATION_REJECTED = "registration_rejected"
    """注册被拒（**所有**拒绝原因共用一个事件）。

    载荷固定带 ``code`` / ``retryable`` / ``msg``，以及 ``client_id`` / ``peer`` /
    ``identity``（鉴权失败时身份还不存在，该键为空串）。

    为什么是**一个**事件而不是按 ``code`` 拆成多个：``register_ack`` 里已经用
    ``code`` + ``retryable`` 表达了完整语义，事件再拆一套等价枚举就会出现
    "两处名表各自生长、漏改一处"的接缝（白名单已经有两处要同步改了）。
    订阅方要分类，读载荷里的 ``code`` 即可——与客户端读 ``register_ack`` 的判断同源。

    与 ``MAPPING_REJECTED`` 不同，这里**不做去重/限频**：拒绝是 WARNING 级罕见事件，
    客户端也有自己的退避间隔；而"某台机器一直上不了线"正是要靠**每一次**留痕来定位。
    累计次数另有 ``stats.registrations_rejected``（管理台状态栏已在显示）。"""

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
