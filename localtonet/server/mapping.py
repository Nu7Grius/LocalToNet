# -*- coding: utf-8 -*-
"""
localtonet.server.mapping —— 映射表与访客端口监听的生命周期
=============================================================
两件事分开：

``MappingStore``（纯数据，**扩展点**）
    映射表的读写。MVP 是内存实现；将来要"服务重启后映射还在"，
    换成 JSON 文件 / SQLite / Redis 实现即可，``MappingManager`` 不用改。

``MappingManager``（生命周期）
    负责 ``asyncio.start_server`` 的起停。动态改映射不是"改个变量"就完了——
    新增端口要真的去 bind，删除端口要真的去 close，**端口是操作系统资源**。

动态更新的正确姿势（教程 4.3）：先算 diff，再停旧、起新，最后广播。
本项目在此基础上补了一层**回滚**：新端口如果 bind 失败（比如被别的进程占了），
不能把服务端留在"一半新一半旧"的中间态——已停的旧监听要尽力恢复，然后如实报错。
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, List, Optional, Sequence

from config import MappingRule
from localtonet.core.events import EventBus, EventType
from localtonet.core.runtime import cancel_all, spawn
from localtonet.errors import TunnelError
from logging_setup import get_logger

__all__ = ["MappingStore", "InMemoryMappingStore", "MappingDiff", "MappingManager"]

VisitorHandler = Callable[[asyncio.StreamReader, asyncio.StreamWriter, int], Awaitable[None]]
"""访客连接回调：``(reader, writer, public_port)``。

刻意只传 ``public_port`` 而不是整个规则对象——规则可能在连接建立之后被改掉，
由回调内部实时查表才是正确语义（否则闭包会攥着一份过期的端口映射）。
"""


class MappingStore(ABC):
    """映射表存储抽象。"""

    @abstractmethod
    def all(self) -> List[MappingRule]:
        """返回全部映射规则（按公网端口升序）。"""

    @abstractmethod
    def get(self, public_port: int) -> Optional[MappingRule]:
        """按公网端口查规则。"""

    @abstractmethod
    def replace(self, rules: Sequence[MappingRule]) -> None:
        """整体替换映射表。"""


class InMemoryMappingStore(MappingStore):
    """内存实现（MVP 默认）。进程重启即丢失。"""

    def __init__(self, rules: Sequence[MappingRule] = ()) -> None:
        self._rules: Dict[int, MappingRule] = {rule.public_port: rule for rule in rules}

    def all(self) -> List[MappingRule]:
        return [self._rules[port] for port in sorted(self._rules)]

    def get(self, public_port: int) -> Optional[MappingRule]:
        return self._rules.get(public_port)

    def replace(self, rules: Sequence[MappingRule]) -> None:
        self._rules = {rule.public_port: rule for rule in rules}


@dataclass
class MappingDiff:
    """一次映射变更的差异描述，也作为 ``mapping_result`` 的载荷回给客户端。"""

    added: List[int] = field(default_factory=list)
    removed: List[int] = field(default_factory=list)
    changed: List[int] = field(default_factory=list)
    unchanged: List[int] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.removed or self.changed)

    def describe(self) -> str:
        parts = []
        if self.added:
            parts.append("新增 " + ",".join(str(p) for p in self.added))
        if self.removed:
            parts.append("移除 " + ",".join(str(p) for p in self.removed))
        if self.changed:
            parts.append("重建 " + ",".join(str(p) for p in self.changed))
        return "；".join(parts) if parts else "映射无变化"

    def to_dict(self) -> Dict[str, List[int]]:
        return {
            "added": self.added,
            "removed": self.removed,
            "changed": self.changed,
            "unchanged": self.unchanged,
        }


class MappingManager:
    """访客端口监听的起停与动态更新。"""

    def __init__(
        self,
        *,
        store: MappingStore,
        on_visitor: VisitorHandler,
        host: str = "0.0.0.0",
        backlog: int = 128,
        logger: Optional[logging.Logger] = None,
        events: Optional[EventBus] = None,
    ) -> None:
        self._store = store
        self._on_visitor = on_visitor
        self._host = host
        self._backlog = backlog
        self._log = logger or get_logger("server.mapping")
        self._events = events or EventBus(self._log)
        self._servers: Dict[int, asyncio.AbstractServer] = {}
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #

    @property
    def store(self) -> MappingStore:
        return self._store

    def rules(self) -> List[MappingRule]:
        return self._store.all()

    def rule_for(self, public_port: int) -> Optional[MappingRule]:
        return self._store.get(public_port)

    def listen_ports(self) -> List[int]:
        return sorted(self._servers)

    def bound_addresses(self) -> Dict[int, str]:
        """实际绑定到的地址（端口写 0 时能拿到内核分配的真实端口）。"""
        result: Dict[int, str] = {}
        for port, server in self._servers.items():
            sockets = getattr(server, "sockets", None) or []
            if sockets:
                info = sockets[0].getsockname()
                result[port] = f"{info[0]}:{info[1]}"
            else:  # pragma: no cover - 正常不会走到
                result[port] = f"?:{port}"
        return result

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    async def start(self, rules: Sequence[MappingRule]) -> MappingDiff:
        """首次启动：按给定规则起全部监听。任一起不来则整体失败，不留在半启动状态。"""
        previous = self._store.all()
        self._store.replace(rules)
        diff = MappingDiff(added=[rule.public_port for rule in rules], removed=[rule.public_port for rule in previous])

        started: List[int] = []
        try:
            for rule in rules:
                await self._listen(rule)
                started.append(rule.public_port)
        except OSError as exc:
            self._log.error("访客端口启动失败：%s，正在回滚已启动的 %s", exc, started)
            for port in started:
                await self.stop_port(port)
            self._store.replace(previous)
            raise TunnelError(f"访客端口监听启动失败：{exc}") from exc

        self._log.info("访客端口已监听：%s", self.bound_addresses())
        self._events.emit(EventType.MAPPING_CHANGED, mapping=[rule.to_dict() for rule in self.rules()], diff=diff.to_dict())
        return diff

    async def apply(self, rules: Sequence[MappingRule]) -> MappingDiff:
        """动态更新映射表：先算 diff，再停旧、起新；失败则回滚。"""
        current = {rule.public_port: rule for rule in self._store.all()}
        target = {rule.public_port: rule for rule in rules}

        added = sorted(set(target) - set(current))
        removed = sorted(set(current) - set(target))
        changed = sorted(
            port for port in set(current) & set(target) if self._listener_changed(current[port], target[port])
        )
        unchanged = sorted(set(current) & set(target) - set(changed))
        diff = MappingDiff(added=added, removed=removed, changed=changed, unchanged=unchanged)

        # 只有 local_port / local_host / remark 变了：监听端口不用动，换掉规则即可
        if not (added or removed or changed):
            self._store.replace(rules)
            self._log.info("映射规则已更新（%s），监听端口无需重建", diff.describe())
            self._events.emit(
                EventType.MAPPING_CHANGED,
                mapping=[rule.to_dict() for rule in self.rules()],
                diff=diff.to_dict(),
            )
            return diff

        to_stop = removed + changed
        for port in to_stop:
            await self.stop_port(port)
        self._store.replace(rules)

        started: List[int] = []
        try:
            for port in added + changed:
                await self._listen(target[port])
                started.append(port)
        except OSError as exc:
            for port in started:
                await self.stop_port(port)
            self._store.replace(list(current.values()))
            restored = await self._restore(current, to_stop)
            raise TunnelError(
                f"映射更新失败（{exc}）；已回滚，恢复监听的端口：{restored}"
            ) from exc

        self._log.info("映射已更新：%s", diff.describe())
        self._events.emit(EventType.MAPPING_CHANGED, mapping=[rule.to_dict() for rule in self.rules()], diff=diff.to_dict())
        return diff

    async def stop_port(self, public_port: int) -> None:
        server = self._servers.pop(public_port, None)
        if server is None:
            return
        server.close()
        try:
            await server.wait_closed()
        except (OSError, RuntimeError) as exc:  # pragma: no cover - 关闭本身很少失败
            self._log.debug("关闭端口 %d 的监听时出现 %r", public_port, exc)
        self._log.debug("访客端口 %d 已停止监听", public_port)

    async def stop(self) -> None:
        """关停所有监听与在建的访客任务。"""
        await cancel_all(self._tasks, logger=self._log)
        for port in list(self._servers):
            await self.stop_port(port)

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    async def _listen(self, rule: MappingRule) -> None:
        host = rule.host or self._host
        server = await asyncio.start_server(
            self._make_callback(rule.public_port),
            host,
            rule.public_port,
            backlog=self._backlog,
        )
        self._servers[rule.public_port] = server

    def _make_callback(self, public_port: int):
        """生成访客连接回调。

        asyncio 传入的是普通可调用对象；这里把它包成一个"生任务"的同步函数，
        好处是访客处理逻辑的异常会被 ``spawn`` 统一记录下来，
        而不是变成 asyncio 的 "Task exception was never retrieved"。
        """

        def _callback(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            spawn(
                self._on_visitor(reader, writer, public_port),
                name=f"visitor:{public_port}",
                logger=self._log,
                track=self._tasks,
            )

        return _callback

    @staticmethod
    def _listener_changed(old: MappingRule, new: MappingRule) -> bool:
        """是否需要重建监听：只有**绑定地址**变了才需要。"""
        return (old.host or "") != (new.host or "")

    async def _restore(self, current: Dict[int, MappingRule], ports: Sequence[int]) -> List[int]:
        restored: List[int] = []
        for port in ports:
            rule = current.get(port)
            if rule is None:
                continue
            try:
                await self._listen(rule)
                restored.append(port)
            except OSError as exc:
                self._log.error("回滚时无法恢复端口 %d 的监听：%s", port, exc)
        return restored

    def __repr__(self) -> str:
        return f"MappingManager(listening={self.listen_ports()})"
