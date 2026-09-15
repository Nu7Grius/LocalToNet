# -*- coding: utf-8 -*-
"""
localtonet.server.mapping —— 映射表与访客端口监听的生命周期
=============================================================
两件事分开：

``MappingStore``（纯数据，**扩展点**）
    映射表的读写。两个实现：:class:`InMemoryMappingStore`（默认）与
    :class:`FileMappingStore`（JSON 持久化，重启后映射还在），
    由 :func:`build_mapping_store` 按配置挑选，``MappingManager`` 不用改。

    注意持久化带来的语义变化：store 里已有内容时，``MappingManager.start``
    的 ``seed``（配置文件里的 mapping）**不再覆盖**它，只当首次种子。

``MappingManager``（生命周期）
    负责 ``asyncio.start_server`` 的起停。动态改映射不是"改个变量"就完了——
    新增端口要真的去 bind，删除端口要真的去 close，**端口是操作系统资源**。

动态更新的正确姿势（教程 4.3）：先算 diff，再停旧、起新，最后广播。
本项目在此基础上补了一层**回滚**：新端口如果 bind 失败（比如被别的进程占了），
不能把服务端留在"一半新一半旧"的中间态——已停的旧监听要尽力恢复，然后如实报错。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import ssl
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence

from config import ConfigError, MappingRule, MappingStoreConfig
from localtonet.core.events import EventBus, EventType
from localtonet.core.runtime import cancel_all, spawn
from localtonet.errors import TunnelError
from logging_setup import get_logger

__all__ = [
    "MappingStore",
    "InMemoryMappingStore",
    "FileMappingStore",
    "build_mapping_store",
    "MappingDiff",
    "MappingManager",
]

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


DEFAULT_MAPPING_FILE = "mappings.json"
"""``mapping_store.type=file`` 且没给 path 时的默认文件名（相对当前工作目录）。"""


class FileMappingStore(MappingStore):
    """JSON 文件持久化实现：服务端重启后映射表还在。

    三条行为约定（都在 MEMORY.md 里记着，改之前先看这里）：

    1. **谁说了算**：file 模式下以文件为准。配置文件里的 ``mapping`` 退化成
       "首次种子"，只在文件不存在或内容为空时生效（见 :meth:`MappingManager.start`）。
       否则每次重启都会把持久化的内容冲掉，持久化等于没做。
    2. **加载失败要 fail fast**：文件损坏时直接抛错、拒绝启动，绝不静默退回内存。
       静默降级是最坏的失败模式——运维会以为持久化在工作，实际每次重启都丢映射。
    3. **写盘失败要降级**：磁盘满 / 没权限时记 ERROR 日志、继续用内存态，
       绝不因为"写不进文件"就让一次映射变更整体失败。原因留在 :attr:`persist_error`。

    写盘用"同目录临时文件 + ``os.replace``"：同卷替换是原子的，
    不会出现"写了一半"的文件被下一次启动读到（Windows 上同样成立）。
    """

    def __init__(self, path: str | os.PathLike[str], *, logger: Optional[logging.Logger] = None) -> None:
        self._path = Path(path)
        self._log = logger or get_logger("server.mapping")
        self._persist_error = ""
        self._rules: Dict[int, MappingRule] = self._load()
        if self._rules:
            self._log.info("已从 %s 载入 %d 条映射", self._path, len(self._rules))

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #

    @property
    def path(self) -> Path:
        return self._path

    @property
    def persist_error(self) -> str:
        """最近一次写盘失败的原因，空串表示一切正常。"""
        return self._persist_error

    def all(self) -> List[MappingRule]:
        return [self._rules[port] for port in sorted(self._rules)]

    def get(self, public_port: int) -> Optional[MappingRule]:
        return self._rules.get(public_port)

    # ------------------------------------------------------------------ #
    # 写入
    # ------------------------------------------------------------------ #

    def replace(self, rules: Sequence[MappingRule]) -> None:
        self._rules = {rule.public_port: rule for rule in rules}
        self._persist()

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    def _load(self) -> Dict[int, MappingRule]:
        if not self._path.is_file():
            self._log.debug("映射文件 %s 不存在，按空表启动（首次播种时创建）", self._path)
            return {}
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"无法读取映射文件 {self._path}：{exc}") from exc
        if not raw.strip():
            return {}

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError(
                f"映射文件 {self._path} 不是合法 JSON：{exc}。"
                f"可删掉该文件以回退到配置文件里的 mapping，或把 mapping_store.type 改回 memory"
            ) from exc
        if not isinstance(data, list):
            raise ConfigError(
                f"映射文件 {self._path} 的顶层必须是数组，实际为 {type(data).__name__}。"
                f"可删掉该文件以回退到配置文件里的 mapping，或把 mapping_store.type 改回 memory"
            )

        rules: Dict[int, MappingRule] = {}
        for index, item in enumerate(data):
            if not isinstance(item, dict):
                raise ConfigError(f"映射文件 {self._path} 的第 {index} 项必须是对象")
            rule = MappingRule.from_dict(item, f"{self._path.name}[{index}]")
            if rule.public_port in rules:
                raise ConfigError(f"映射文件 {self._path} 里 public_port={rule.public_port} 重复")
            rules[rule.public_port] = rule
        return rules

    def _persist(self) -> None:
        payload = [rule.to_dict() for rule in self.all()]
        tmp = self._path.with_name(self._path.name + ".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self._path)
        except OSError as exc:
            self._persist_error = str(exc)
            self._log.error(
                "映射表写入 %s 失败：%s（内存中的映射仍然生效，只是没能持久化）", self._path, exc
            )
            with contextlib.suppress(OSError):
                tmp.unlink()
            return
        self._persist_error = ""
        self._log.debug("映射表已写入 %s（%d 条）", self._path, len(payload))

    def __repr__(self) -> str:
        return f"FileMappingStore(path={str(self._path)!r}, rules={len(self._rules)})"


def build_mapping_store(
    config: MappingStoreConfig,
    *,
    logger: Optional[logging.Logger] = None,
) -> MappingStore:
    """按配置挑存储后端。

    服务端只认这一个入口，将来加 SQLite / Redis 实现也不必改调用方。
    """
    if config.type == "file":
        return FileMappingStore(config.path.strip() or DEFAULT_MAPPING_FILE, logger=logger)
    return InMemoryMappingStore()


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
    """访客端口监听的起停与动态更新。

    **访客端口 TLS**（本轮扩展）由四个构造参数决定，它们的分工是刻意的：

    * ``visitor_tls`` —— 已建好的 ``SSLContext``，**没有**则为 ``None``（＝用不了）。
      判据是"能不能用"（证书齐备），不是"要不要用"。
    * ``visitor_default`` —— 未显式表态的端口跟不跟着开（``tls.visitor_enabled``）。
    * ``visitor_forced_plain`` —— CLI ``--no-visitor-tls`` 逃生门，一票否决全部端口。
    * ``visitor_handshake_timeout`` —— 复用 ``tls.handshake_timeout``。

    上下文在这里**只被引用、不被构建**：它必须是"建一次全程复用"的那一份，
    热切换开关时也不重建（见 ``_listen``）。
    """

    def __init__(
        self,
        *,
        store: MappingStore,
        on_visitor: VisitorHandler,
        host: str = "0.0.0.0",
        backlog: int = 128,
        logger: Optional[logging.Logger] = None,
        events: Optional[EventBus] = None,
        visitor_tls: Optional[ssl.SSLContext] = None,
        visitor_default: bool = False,
        visitor_forced_plain: bool = False,
        visitor_handshake_timeout: float = 10.0,
    ) -> None:
        self._store = store
        self._on_visitor = on_visitor
        self._host = host
        self._backlog = backlog
        self._log = logger or get_logger("server.mapping")
        self._events = events or EventBus(self._log)
        self._servers: Dict[int, asyncio.AbstractServer] = {}
        self._tasks: set[asyncio.Task] = set()
        self._visitor_tls = visitor_tls
        self._visitor_default = visitor_default
        self._visitor_forced_plain = visitor_forced_plain
        self._visitor_handshake_timeout = visitor_handshake_timeout

    # ------------------------------------------------------------------ #
    # 访客端口 TLS 判定
    # ------------------------------------------------------------------ #

    def effective_tls(self, rule: MappingRule) -> bool:
        """某条规则**实际**是否做 TLS 终止。

        三态语义：``rule.tls`` 为 ``None`` 时跟随 ``visitor_default``，
        否则以端口自己的表态为准。逃生门在最外层，一票否决。
        """
        if self._visitor_forced_plain:
            return False
        return self._visitor_default if rule.tls is None else rule.tls

    def _require_certs(self, rules: Sequence[MappingRule]) -> None:
        """确认"要开 TLS 的端口"都有证书，否则报错。

        **必须在停监听/替换 store 之前调用**：``apply()`` 的 ``except`` 只捕 ``OSError``，
        若让 ``ConfigError`` 从 ``_listen`` 里穿透出去，就会留下
        "旧监听已停、store 已替换、新监听没起"的不一致状态。
        ``start()`` 同理会留下半启动的映射表。
        """
        if self._visitor_forced_plain:
            return
        needing = [rule.public_port for rule in rules if self.effective_tls(rule)]
        if needing and self._visitor_tls is None:
            raise ConfigError(
                f"端口 {needing} 启用了访客 TLS，但未配置 tls.visitor_cert/visitor_key"
                "（访客端口证书必须独立提供，不会回落到 tls.cert）"
            )

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

    async def start(self, seed: Sequence[MappingRule]) -> MappingDiff:
        """首次启动：按**生效的**规则起全部监听。任一起不来则整体失败，不留在半启动状态。

        ``seed`` 是配置文件里的 ``mapping``，但它**只在 store 为空时生效**：
        store 里已经有内容（例如从持久化文件载入的映射）时以 store 为准。

        这条"store 优先"是持久化能成立的前提——若照旧用 seed 覆盖 store，
        每次重启都会把持久化下来的映射冲掉，持久化就等于没做。
        store 与 seed 不一致时会打 WARNING，免得使用者以为改了配置文件却没反应。
        """
        previous = self._store.all()
        effective = list(previous) or list(seed)
        if previous and [rule.to_dict() for rule in previous] != [rule.to_dict() for rule in seed]:
            self._log.warning(
                "已存在的映射表（%d 条）优先于配置文件里的 mapping（%d 条）：以已有内容为准，"
                "配置文件仅在映射表为空时作为初始种子",
                len(previous),
                len(seed),
            )

        # 访客 TLS 可用性必须先校验：此时一个监听都还没起、store 还没动，
        # 抛错出去不会留下任何"一半新一半旧"的状态
        self._require_certs(effective)

        self._store.replace(effective)
        diff = MappingDiff(
            added=[rule.public_port for rule in effective],
            removed=[rule.public_port for rule in previous],
        )

        started: List[int] = []
        try:
            for rule in effective:
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

        # 同样必须在停监听之前：apply() 的 except 只捕 OSError，ConfigError 穿透出去
        # 会留下"旧监听已停、store 已替换、新监听没起"的三不管状态。
        self._require_certs(list(target.values()))

        # TLS 被关掉的端口单独告警："明文"是安全边界的变化，不能只体现在 diff 里
        for port in changed:
            if self.effective_tls(current[port]) and not self.effective_tls(target[port]):
                self._log.warning("访客端口 %d 的 TLS 已关闭，该端口从此明文传输", port)

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
        # ssl_handshake_timeout 只在挂了 ssl 时才允许传，所以参数得动态拼
        # （同 server/core.py 的控制/数据通道）。访客 TLS 与明文端口可以任意混排。
        kwargs: Dict[str, Any] = {"backlog": self._backlog}
        use_tls = self.effective_tls(rule)
        if use_tls:
            kwargs["ssl"] = self._visitor_tls
            kwargs["ssl_handshake_timeout"] = self._visitor_handshake_timeout
        server = await asyncio.start_server(
            self._make_callback(rule.public_port),
            host,
            rule.public_port,
            **kwargs,
        )
        self._servers[rule.public_port] = server
        # 逐端口打一行状态：这是"改了开关却没生效"唯一的可见证据。
        # 刻意不做运行时协议探测——_handle_visitor 配对前一个字节都不读（纯透传），
        # 要探测就得改 pipe_both 加 peek + 前缀回放，等于侵入字节透传路径。
        self._log.info(
            "访客端口 %d 监听于 %s：%s",
            rule.public_port,
            host,
            "TLS" if use_tls else "明文",
        )

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

    def _listener_changed(self, old: MappingRule, new: MappingRule) -> bool:
        """是否需要重建监听：**绑定地址**或**访客 TLS 开关**变了才算。

        必须是实例方法：判定 effective TLS 要读 ``visitor_default`` /
        ``visitor_forced_plain``。漏掉 TLS 这一项就是"改了开关不生效"的静默缺陷——
        用户以为切换成功，实际监听还是老样子。
        """
        return (old.host or "") != (new.host or "") or self.effective_tls(old) != self.effective_tls(new)

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
