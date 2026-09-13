# -*- coding: utf-8 -*-
"""
localtonet.core.dispatcher —— 指令分发
========================================
解决的问题：主循环里如果写一长串 ``if msg["type"] == "...": ... elif ...``，
每加一条指令都要改主循环，而且容易漏改、容易越写越长。

这里换成**注册表**：指令名 → 处理函数。新增指令 = 新增一个带 ``@handler`` 的方法，
主循环不动一行。这就是本项目的"指令扩展点"。

用法::

    class Client:
        @handler(MsgType.NEW_CONN)
        async def _on_new_conn(self, msg: dict) -> None:
            ...

    dispatcher = MessageDispatcher.from_object(client)
    await dispatcher.dispatch(msg)

未注册的指令默认只记一条日志；也可以给 ``unknown_handler`` 传自己的兜底逻辑
（例如给对端回一条"协议不识别"，为将来的协议版本协商留出空间）。
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict, Optional

from logging_setup import get_logger

__all__ = ["handler", "HandlerType", "MessageDispatcher"]

HandlerType = Callable[..., Awaitable[None]]

_ATTR = "__lt_msg_type__"


def handler(msg_type: str) -> Callable[[HandlerType], HandlerType]:
    """把方法标记为某个指令的处理函数。"""

    def decorate(func: HandlerType) -> HandlerType:
        setattr(func, _ATTR, msg_type)
        return func

    return decorate


class MessageDispatcher:
    """指令注册表 + 分发器。"""

    def __init__(
        self,
        *,
        unknown_handler: Optional[HandlerType] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._handlers: Dict[str, HandlerType] = {}
        self._unknown_handler = unknown_handler
        self._log = logger or get_logger("dispatcher")

    # ------------------------------------------------------------------ #
    # 注册
    # ------------------------------------------------------------------ #

    def register(self, msg_type: str, func: HandlerType) -> None:
        """手动注册。重复注册同名指令会直接报错，避免"以为改了其实没生效"。"""
        if msg_type in self._handlers:
            raise ValueError(f"指令 {msg_type!r} 已注册，不允许覆盖")
        self._handlers[msg_type] = func

    def replace(self, msg_type: str, func: HandlerType) -> None:
        """显式覆盖注册，用于测试注入替身。"""
        self._handlers[msg_type] = func

    @classmethod
    def from_object(
        cls,
        target: Any,
        *,
        unknown_handler: Optional[HandlerType] = None,
        logger: Optional[logging.Logger] = None,
    ) -> "MessageDispatcher":
        """扫描对象的 ``@handler`` 标记方法并全部注册。"""
        dispatcher = cls(unknown_handler=unknown_handler, logger=logger)
        found = 0
        for name in dir(target):
            try:
                member = getattr(target, name)
            except AttributeError:  # pragma: no cover - property 抛错时跳过
                continue
            msg_type = getattr(member, _ATTR, None)
            if msg_type is not None and callable(member):
                dispatcher.register(msg_type, member)
                found += 1
        if not found:
            dispatcher._log.warning(
                "%s 上没有任何 @handler 标记的处理函数，请检查是否漏了装饰器",
                type(target).__name__,
            )
        return dispatcher

    # ------------------------------------------------------------------ #
    # 分发
    # ------------------------------------------------------------------ #

    def handles(self, msg_type: str) -> bool:
        return msg_type in self._handlers or self._unknown_handler is not None

    def registered_types(self) -> tuple[str, ...]:
        return tuple(sorted(self._handlers))

    async def dispatch(self, msg: Dict[str, Any], *args: Any, **kwargs: Any) -> None:
        """按 ``msg["type"]`` 找到处理函数并调用。"""
        msg_type = msg.get("type")
        if not isinstance(msg_type, str) or not msg_type:
            raise ValueError(f"消息缺少合法的 type 字段：{msg!r}")

        func = self._handlers.get(msg_type, self._unknown_handler)
        if func is None:
            self._log.warning("收到未注册的指令 %r，已忽略", msg_type)
            return

        await func(msg, *args, **kwargs)
