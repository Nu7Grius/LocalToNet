# -*- coding: utf-8 -*-
"""
localtonet.server.core —— 服务端编排（公网侧）
================================================
三个监听端口各司其职：

===========  ============================  ============================================
端口          处理函数                       职责
===========  ============================  ============================================
control       ``_handle_control``           客户端注册、心跳、动态映射等轻量 JSON 指令
data          ``_handle_data``              按请求建立，靠 ``conn_id`` 与访客连接配对
访客端口       ``_handle_visitor``           公网访客入口，一个映射端口一个监听
===========  ============================  ============================================

为什么要把控制和数据拆成两条通道？
控制通道上跑的是"开一条转发"这种**必须及时送达**的小指令。如果所有流量都挤在一条
TCP 上，大文件传输时的写缓冲堆积会把指令堵在后面，表现为"点开新页面要等上一个下载完"。
拆开之后，数据面的拥塞只影响它自己那一条连接，指令通道始终畅通。

错误兜底（对应教程 4.5）：

* 端口没有映射            → ``404 No mapping for this port``（只在"连接已建立、映射刚被摘掉"的窗口里出现）
* 服务端上没有在线客户端  → ``502 No client online``
* 客户端连不上内网后端    → 客户端上报 ``conn_error``，服务端立即回 ``502 Backend connect failed``
  （不这么做的话，访客侧 curl 会看到 ``Empty reply from server``，非常难排查）
* 配对超时                → ``502``，并带上原因
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from config import ConfigError, LimitsConfig, MappingRule, ServerConfig
from localtonet.core.dispatcher import MessageDispatcher, handler
from localtonet.core.events import EventBus, EventType
from localtonet.core.heartbeat import Watchdog
from localtonet.core.limiter import ClientRateLimiter
from localtonet.core.pipe import close_write_side, close_writer, pipe_both
from localtonet.core.rules import parse_mapping, parse_ports
from localtonet.core.runtime import cancel_all, peer_host, peer_name, spawn
from localtonet.core.tls import (
    build_server_context,
    build_visitor_context,
    describe_server_tls,
    describe_visitor_tls,
)
from localtonet.errors import AuthError, QuotaExceededError, TunnelError
from localtonet.server.auth import Authenticator, build_authenticator
from localtonet.server.mapping import (
    MappingDiff,
    MappingManager,
    MappingStore,
    build_mapping_store,
)
from localtonet.server.pending import PendingConn, PendingTable
from localtonet.server.registry import ClientRegistry, ClientSession
from logging_setup import get_logger
from protocol import FrameLengthError, MsgType, ProtocolError, make_msg, recv_msg, send_msg

__all__ = ["TunnelServer", "ServerStats"]

_HTTP_PHRASES = {
    400: "Bad Request",
    403: "Forbidden",
    404: "Not Found",
    409: "Conflict",
    429: "Too Many Requests",
    500: "Internal Server Error",
    502: "Bad Gateway",
    503: "Service Unavailable",
}

_KICK_COOLDOWN_MAX_ENTRIES = 1024
"""冷却期表的条目上限。刻意不是"每客户端"而是**全局**，理由同 ``rate_limit_max_keys``：
它是一条长期驻留的表（条目只在冷却期满后遇到同一个 client_id 再注册时才清掉，
被踢过就再没回来的机器会一直占着一行），不设上限就是一条缓慢的内存增长路径。
正常部署里管理台踢过的不同 client_id 是个位数量级，撞到上限说明有人在反复踢不同的机器。"""


def _kick_reason(cooldown: float) -> str:
    """踢人这条下线原因的文案。

    它会被写进 INFO 日志、塞进 ``CLIENT_DISCONNECTED`` 事件、再显示到管理台日志面板，
    所以必须自解释：运维看到"被管理台踢出"时，下一个问题必然是"它多久能回来"。
    """
    if cooldown <= 0:
        return "被管理台踢出（未设冷却期，它可以立刻重连）"
    return f"被管理台踢出（{cooldown:g}s 内拒绝该 client_id 重连）"


@dataclass
class PortTraffic:
    """按**公网端口**累计的转发量。``snapshot()["bandwidth"]`` 的数据源。

    与 :class:`ServerStats` 的全局计数同语义：启动至今累计、不清零。
    条目只在真的转发过流量时创建；映射表里删掉某个端口后旧条目仍在（当作历史），
    所以条目数由"进程生命周期内映射过多少个端口"决定，**不受访客数量影响**
    （访客维度的读取在 ``snapshot()["rate_limit"]["keys"]``，那个是有上限的）。
    """

    requests: int = 0
    """已派发给客户端的请求数。口径与 ``REQUEST_START`` 事件一致：
    被 429/502 挡在配对之前的请求不计入——那些压根没有流量。"""
    upload: int = 0
    """访客 → 隧道 → 内网后端 的字节数。"""
    download: int = 0
    """内网后端 → 隧道 → 访客 的字节数。"""
    throttled: float = 0.0
    """这个端口上因限速累计等待的秒数。"""

    @property
    def total(self) -> int:
        return self.upload + self.download

    def to_dict(self) -> Dict[str, Any]:
        return {
            "requests": self.requests,
            "upload": self.upload,
            "download": self.download,
            "total": self.total,
            "throttled": round(self.throttled, 4),
        }


@dataclass
class ServerStats:
    """服务端累计计数。启动至今不清零，给日志与未来的指标接口用。

    ``clients_online`` 是唯一的 **gauge**（当前值），其余都是累计值。
    ``requests_rejected`` 与 ``requests_failed`` 刻意分开：
    前者是"压根没开始转发"（配额拒绝），后者是"转了但失败了"。
    ``mapping_rejected`` 只统计**因身份无写权限**被拒的 ``set_mapping``——
    mapping 内容非法属于调用方 bug，不记在这里，免得安全事件淹没在噪声里。

    ``by_port`` 是"按端口"那一维，**刻意不进** :meth:`to_dict`：
    ``stats`` 是给状态栏读的一层扁平标量表（界面按名字逐个取值），
    嵌一张表进去会把两种读取方式混在一起；它由 ``snapshot()["bandwidth"]`` 单独暴露。
    """

    clients_registered: int = 0
    registrations_rejected: int = 0
    mapping_rejected: int = 0
    clients_online: int = 0
    requests_total: int = 0
    requests_failed: int = 0
    requests_rejected: int = 0
    bytes_upload: int = 0
    bytes_download: int = 0
    throttled_seconds: float = 0.0
    """因带宽限速累计等待的秒数，用来回答"限速到底有没有在起作用"。"""
    by_port: Dict[int, PortTraffic] = field(default_factory=dict)
    """按公网端口分开的转发量，见 :class:`PortTraffic`。"""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "clients_registered": self.clients_registered,
            "registrations_rejected": self.registrations_rejected,
            "mapping_rejected": self.mapping_rejected,
            "clients_online": self.clients_online,
            "requests_total": self.requests_total,
            "requests_failed": self.requests_failed,
            "requests_rejected": self.requests_rejected,
            "bytes_upload": self.bytes_upload,
            "bytes_download": self.bytes_download,
            "throttled_seconds": round(self.throttled_seconds, 4),
        }

    # ------------------------------------------------------------------ #

    def note_request(self, public_port: int) -> None:
        """记一次"请求已派发出去"。"""
        self._port(public_port).requests += 1

    def add_traffic(self, public_port: int, *, upload: int, download: int, throttled: float) -> None:
        """记一次转发结束时的字节量与限速等待。"""
        entry = self._port(public_port)
        entry.upload += upload
        entry.download += download
        entry.throttled += throttled

    def _port(self, public_port: int) -> PortTraffic:
        """取（必要时创建）某个端口的计数条目。临界区内不含 await。"""
        entry = self.by_port.get(public_port)
        if entry is None:
            entry = PortTraffic()
            self.by_port[public_port] = entry
        return entry


class TunnelServer:
    """内网穿透服务端。"""

    def __init__(
        self,
        config: ServerConfig,
        *,
        events: Optional[EventBus] = None,
        logger: Optional[logging.Logger] = None,
        authenticator: Optional[Authenticator] = None,
        registry: Optional[ClientRegistry] = None,
        mapping_store: Optional[MappingStore] = None,
        visitor_plain_override: bool = False,
    ) -> None:
        self._config = config
        self._log = logger or get_logger("server")
        self._events = events or EventBus(self._log)
        # 令牌表在这里载入（auth.file 模式）：文件损坏 / 缺失会直接抛 ConfigError，
        # 由 server.py 的 serve() 归到"配置错误→退出码 2"。绝不静默降级为放行。
        self._auth = authenticator or build_authenticator(config.auth, logger=self._log)
        # TLS 上下文在这里建一次、全程复用。绝不放到连接路径上现建——
        # 数据通道是"每个请求一条 TCP"，每条都新建 context 会让 TLS 1.3 的
        # 会话票据缓存彻底失效，等于每个请求都付一次完整握手。
        self._tls = build_server_context(config.tls, logger=self._log)
        # 访客端口另起一份上下文（证书独立、绝不回落）。同样只建一次：
        # 开关热切时只换"用不用"，不重建 context。
        self._visitor_tls = build_visitor_context(config.tls, logger=self._log)
        self._registry = registry or ClientRegistry(logger=self._log)
        self._pending = PendingTable(logger=self._log)
        self._limiter = self._build_limiter(config.limits)
        self._mapping = MappingManager(
            # 存储后端由配置决定：memory（默认）或 file（重启后映射还在）
            store=mapping_store or build_mapping_store(config.mapping_store, logger=self._log),
            on_visitor=self._handle_visitor,
            host=config.control.host,
            logger=self._log,
            events=self._events,
            visitor_tls=self._visitor_tls,
            visitor_default=config.tls.visitor_enabled,
            visitor_forced_plain=visitor_plain_override,
            visitor_handshake_timeout=config.tls.handshake_timeout,
        )
        self._dispatcher = MessageDispatcher.from_object(self, logger=self._log)
        self._stats = ServerStats()

        # 管理台踢人后的冷却期：``client_id -> 冷却截止时间（monotonic）``。
        # **只有管理台主动踢人才登记**——被顶号、心跳超时、控制连接断开都不登记：
        # 那些是"这条连接没了"，不是"这台机器被要求离开一会儿"，给它们加冷却会变成
        # "客户端网络抖一下就再也上不来"，与看门狗的意图正好相反。
        # 用 OrderedDict 而不是普通 dict：满了要淘汰**最早**登记的那条（见 _remember_kick）。
        self._kick_cooldowns: "OrderedDict[str, float]" = OrderedDict()

        self._control_server: Optional[asyncio.AbstractServer] = None
        self._data_server: Optional[asyncio.AbstractServer] = None
        self._watchdog = Watchdog(
            interval=config.timeouts.watchdog_interval,
            collect=self._collect_idle_sessions,
            expire=self._expire_session,
            name="server-watchdog",
            logger=self._log,
        )
        self._tasks: set[asyncio.Task] = set()
        self._watchdog_task: Optional[asyncio.Task] = None
        self._stopped = asyncio.Event()

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        cfg = self._config
        self._log.info(
            "启动服务端 %s（鉴权：%s，传输：%s）",
            cfg.name,
            self._auth.name,
            describe_server_tls(cfg.tls),
        )
        # 访客端口 TLS 逐端口状态由 MappingManager._listen 打；这里只说全局默认与证书有无
        self._log.info("%s", describe_visitor_tls(cfg.tls))

        await self._mapping.start(cfg.mapping)

        # ssl_handshake_timeout 只在挂了 ssl 时才允许传，所以参数得动态拼。
        # 显式收紧它是必要的：默认 60s 会让"明文客户端打 TLS 端口"这类必然失败
        # 的握手白占连接一分钟，测试收尾会明显变慢。
        tls_kwargs: Dict[str, Any] = {}
        if self._tls is not None:
            tls_kwargs["ssl"] = self._tls
            tls_kwargs["ssl_handshake_timeout"] = cfg.tls.handshake_timeout

        try:
            self._control_server = await asyncio.start_server(
                self._handle_control, cfg.control.host, cfg.control.port, **tls_kwargs
            )
            self._data_server = await asyncio.start_server(
                self._handle_data, cfg.data.host, cfg.data.port, **tls_kwargs
            )
        except OSError as exc:
            await self._mapping.stop()
            raise TunnelError(f"控制/数据通道监听失败：{exc}") from exc

        self._watchdog_task = spawn(
            self._watchdog.run(), name="server-watchdog", logger=self._log, track=self._tasks
        )
        self._log.info(
            "控制通道 %s:%d ｜ 数据通道 %s:%d ｜ 访客端口 %s",
            cfg.control.host,
            cfg.control.port,
            cfg.data.host,
            cfg.data.port,
            [f"{rule.public_port}->{rule.local_port}" for rule in self._mapping.rules()],
        )
        self._events.emit(
            EventType.SERVER_STARTED,
            name=cfg.name,
            control_port=cfg.control.port,
            data_port=cfg.data.port,
            mapping=[rule.to_dict() for rule in self._mapping.rules()],
        )

    async def serve_forever(self) -> None:
        """一直服务，直到 :meth:`stop` 被调用。"""
        await self._stopped.wait()

    async def stop(self) -> None:
        """优雅关停：先停接入，再断连接，最后收拾残留任务。"""
        if self._stopped.is_set():
            return
        self._log.info("正在关停服务端…")

        for server in (self._control_server, self._data_server):
            if server is not None:
                server.close()
        for server in (self._control_server, self._data_server):
            if server is not None:
                try:
                    await server.wait_closed()
                except (OSError, RuntimeError):
                    pass

        self._watchdog.stop()
        await self._mapping.stop()
        await cancel_all(self._tasks, logger=self._log)

        for pending in self._pending.drain():
            pending.fail("服务端正在关停")
            pending.finish()
            await close_writer(pending.data_writer)

        for session in self._registry.sessions():
            self._registry.remove(session.client_id)
            await close_writer(session.writer)

        self._stopped.set()
        self._events.emit(EventType.SERVER_STOPPED, stats=self._stats.to_dict())
        self._log.info("服务端已关停，累计统计：%s", self._stats.to_dict())

    # ------------------------------------------------------------------ #
    # 对外查询（给 GUI / 管理接口 / 测试用）
    # ------------------------------------------------------------------ #

    @property
    def stats(self) -> ServerStats:
        return self._stats

    @property
    def events(self) -> EventBus:
        return self._events

    @property
    def registry(self) -> ClientRegistry:
        return self._registry

    @property
    def mapping(self) -> MappingManager:
        return self._mapping

    @property
    def pending(self) -> PendingTable:
        return self._pending

    @property
    def limiter(self) -> Optional[ClientRateLimiter]:
        """带宽限速器；``None`` 表示配置里两个方向都没限速。"""
        return self._limiter

    def snapshot(self) -> Dict[str, Any]:
        """一次性拿全服务端状态。GUI 表格、健康检查都可以直接用这个。"""
        return {
            "name": self._config.name,
            "auth": self._auth.name,
            "clients": self._registry.snapshot(),
            "port_owner": self._registry.port_owner_snapshot(),
            "mapping": [rule.to_dict() for rule in self._mapping.rules()],
            "listening": self._mapping.listen_ports(),
            "pending": len(self._pending),
            "stats": self._stats.to_dict(),
            "bandwidth": self._bandwidth_snapshot(),
            "rate_limit": self._rate_limit_snapshot(),
            "kick_cooldowns": self._kick_cooldown_snapshot(),
        }

    def _bandwidth_snapshot(self) -> Dict[int, Dict[str, Any]]:
        """按公网端口分开的转发量。键是端口号（与 ``port_owner`` 同风格）。"""
        return {port: traffic.to_dict() for port, traffic in sorted(self._stats.by_port.items())}

    def _rate_limit_snapshot(self) -> Dict[str, Any]:
        """限速的**当前口径**与桶表水位。

        口径必须看得见：``scope`` 一旦不是 ``client``，``per_client_*_bps`` 的含义就从
        "客户端总额度"变成了"每个汇总单位各自的额度"——这是配置语义被改掉的那一类开关，
        跟 ``auth.shared_can_manage_mapping`` 一样，收紧/放宽都要在状态里读得出来。
        ``keys``/``evicted`` 则回答"按访客分桶之后到底涨了多少、有没有撞上限"。
        """
        limiter = self._limiter
        limits = self._config.limits
        return {
            "scope": limits.rate_limit_scope,
            "upload_bps": limits.per_client_upload_bps,
            "download_bps": limits.per_client_download_bps,
            "max_keys": limits.rate_limit_max_keys,
            "keys": limiter.key_count if limiter is not None else 0,
            "evicted": limiter.evicted if limiter is not None else 0,
        }

    async def submit_mapping(self, rules: Sequence[MappingRule]) -> MappingDiff:
        """应用一份映射表，并把新表广播给所有在线客户端，返回本次差异。

        这是**管理台唯一的写入口**，与 ``set_mapping`` 指令共用同一段内核
        （``MappingManager.apply`` → ``_broadcast_mapping_list``）。
        刻意不复制第二份实现：两个入口若各写一遍，迟早出现"走指令能改、走管理台改不动"
        这类只在一条路径上复现的 bug。

        调用者必须已经在事件循环里（界面侧走 ``LoopThread.submit``）。
        校验失败抛 :class:`ConfigError`/:class:`TunnelError`，由调用方决定怎么呈现。
        """
        diff = await self._mapping.apply(rules)
        await self._broadcast_mapping_list()
        return diff

    async def kick_client(self, client_id: str) -> bool:
        """把一个**在线**客户端踢下线（管理台的动作），返回是否真的踢掉了。

        与 :meth:`submit_mapping` 不同，这不是"改状态"而是"断人"：调用之后该客户端的
        控制连接立刻断开、它认领的端口归属立即释放、排队中的访客收到失败，
        并且 ``admin.kick_cooldown`` 秒内拒绝它重新注册（否则等于没踢，
        客户端自己的重连退避初始延迟本来就是个位数秒级）。

        复用 :meth:`_disconnect_session`：顶号、心跳超时、管理台踢出三条路要做的清理
        完全一样（摘注册表、放端口、回收限速桶、唤醒挂起项、关连接、发事件、广播映射），
        各写一遍迟早出现"这条路径忘了回收限速桶"这类只在一种入口复现的泄漏。

        ``client_id`` 不在线时返回 ``False`` 且**不登记冷却期**：管理台每 0.5 秒采样一次，
        用户点下去时目标可能刚掉线，这是正常竞态而非错误——更不该顺手给它安一个冷却期，
        那会把"它自己掉了"变成"它被禁止重连"。
        """
        session = self._registry.get(client_id)
        if session is None:
            return False

        # 冷却期必须在 await **之前**登记：_disconnect_session 里有 await（关连接、广播映射），
        # 先断连接再登记的话，客户端抢在登记完成前重连就能从这个窗口里钻进来。
        # 窗口只有毫秒级，但"踢完立刻又在线"正是这个功能要消灭的现象。
        cooldown = self._config.admin.kick_cooldown
        self._remember_kick(client_id, cooldown)
        await self._disconnect_session(client_id, reason=_kick_reason(cooldown), session=session)
        return True

    def _remember_kick(self, client_id: str, cooldown: float) -> None:
        """登记一段冷却期。临界区内不含 await。"""
        # 先 pop 再插：同一个 client_id 再次被踢时位置要挪到队尾
        # （淘汰按"最早登记的先走"，别让一条老记录挡住新记录）
        self._kick_cooldowns.pop(client_id, None)
        if cooldown <= 0:
            # 0 秒＝不冷却。语义是"只断一下"，那就不要留下任何状态，
            # 免得表里堆一批立刻过期的条目
            return
        self._kick_cooldowns[client_id] = time.monotonic() + cooldown
        self._evict_kick_cooldowns()

    def _evict_kick_cooldowns(self) -> None:
        """把冷却期表压回上限以内：先清过期的，再淘汰最早登记的。临界区内不含 await。"""
        now = time.monotonic()
        for key in [key for key, deadline in self._kick_cooldowns.items() if deadline <= now]:
            self._kick_cooldowns.pop(key, None)
        while len(self._kick_cooldowns) > _KICK_COOLDOWN_MAX_ENTRIES:
            self._kick_cooldowns.popitem(last=False)

    def _cooldown_remaining(self, client_id: str) -> float:
        """还剩多少秒冷却；``<= 0`` 表示不在冷却期（顺手清掉过期条目）。临界区内不含 await。"""
        deadline = self._kick_cooldowns.get(client_id)
        if deadline is None:
            return 0.0
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            self._kick_cooldowns.pop(client_id, None)
            return 0.0
        return remaining

    def _kick_cooldown_snapshot(self) -> Dict[str, float]:
        """还在冷却期里的 ``client_id -> 剩余秒数``。

        与 ``bandwidth`` 同理单独暴露、**不进** ``stats.to_dict()``（``stats`` 是给状态栏
        按名字取值的扁平标量表）。冷却期属于"看不见就会误判"的状态：没有它，
        界面只能显示"那台机器一直没上来"，看起来像配置坏了。
        """
        result: Dict[str, float] = {}
        for key in list(self._kick_cooldowns):
            remaining = self._cooldown_remaining(key)
            if remaining > 0:
                result[key] = round(remaining, 1)
        return result

    # ------------------------------------------------------------------ #
    # 控制通道
    # ------------------------------------------------------------------ #

    def _explain_frame_length_error(self, exc: ProtocolError, peer: str) -> None:
        """给"长度头非法"补一条能直接定位的提示。

        不新增协议字段的前提下，"客户端配了 TLS 而服务端是明文"（或反之）
        唯一的症状就是帧长度头被解析成一个天文数字然后被拒——行为是对的（fail fast），
        但日志只说"长度非法"，运维得自己想到加密方式不匹配这一层。
        TLS 记录头（``16 03 …``）被当成大端长度必然越界，所以这个信号是可靠的。
        """
        if self._tls is not None:
            # 本端已经开了 TLS，说明对端要么证书不对（握手阶段就失败了）、
            # 要么根本不是 TLS 连接；与"本端明文"是两回事，不套用这条提示
            return
        if not isinstance(exc, FrameLengthError):
            return
        self._log.warning(
            "提示：控制连接 %s 的长度头非法，常见原因是**客户端配置了 TLS 而本端是明文**"
            "（TLS 记录头被当成了帧长度头）。请核对两端的 tls 配置是否一致",
            peer,
        )

    async def _handle_control(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = peer_name(writer)
        session: Optional[ClientSession] = None
        try:
            try:
                first = await asyncio.wait_for(
                    recv_msg(reader), timeout=self._config.timeouts.connect_timeout
                )
            except TimeoutError:
                self._log.warning("控制连接 %s 在 %.0fs 内没有注册，断开", peer, self._config.timeouts.connect_timeout)
                return
            except ProtocolError as exc:
                self._log.warning("控制连接 %s 读取出错：%s", peer, exc)
                self._explain_frame_length_error(exc, peer)
                return

            if first.get("type") != MsgType.REGISTER_CLIENT:
                self._log.warning("控制连接 %s 首条消息不是 register_client（收到 %r），断开", peer, first.get("type"))
                return

            session = await self._register_client(reader, writer, first, peer)
            if session is None:
                return

            while True:
                msg = await recv_msg(reader)
                session.touch()
                await self._dispatcher.dispatch(msg, session)

        except ProtocolError as exc:
            self._log.info("控制连接 %s 结束：%s", peer, exc)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 单条连接出错不该拖垮整个服务端
            self._log.exception("控制连接 %s 处理时发生未预期异常", peer)
        finally:
            if session is not None:
                await self._disconnect_session(session.client_id, reason="控制连接断开", session=session)
            else:
                await close_writer(writer)

    async def _register_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        msg: Dict[str, Any],
        peer: str,
    ) -> Optional[ClientSession]:
        """校验并登记客户端。返回 None 表示注册被拒（调用方负责关连接）。"""
        client_id = msg.get("client_id")
        if not isinstance(client_id, str) or not client_id.strip():
            # 刻意**不带 client_id** 上报：这里的 client_id 未经校验，可能是任意类型、
            # 也可能是客户端塞进来的超长串（上限是 max_msg_len，10MB），
            # 原样落日志/事件等于给对手一个日志放大器。msg 里已经说明哪里错了。
            await self._reject_register(
                writer,
                code=400,
                msg="client_id 必须是非空字符串",
                retryable=True,
                peer=peer,
            )
            return None
        client_id = client_id.strip()

        try:
            local_ports = parse_ports(msg.get("local_ports"))
        except ConfigError as exc:
            # 400＝客户端自己的参数写错了：改完配置重试有意义，所以归到可重试
            await self._reject_register(
                writer,
                code=400,
                msg=exc.message,
                retryable=True,
                client_id=client_id,
                peer=peer,
            )
            return None

        try:
            # verify 返回**身份**（是谁 + 允许认领哪些内网端口），失败抛 AuthError。
            # 令牌表的热重载就在它内部的第一步，所以"改文件 → 下一次注册尝试即生效"。
            identity = self._auth.verify(msg, peer)
        except AuthError as exc:
            # 此处**没有** identity 可用：verify 抛错意味着身份从未确立。
            # 事件里 identity 留空，订阅方（管理台）要按"缺身份"渲染，
            # 不能套用 ANONYMOUS —— 那会把"冒充/令牌失效"显示成"匿名用户"。
            await self._reject_register(
                writer,
                code=exc.code,
                msg=exc.message,
                retryable=exc.retryable,
                client_id=client_id,
                peer=peer,
            )
            return None

        # 冷却期：管理台刚把这台机器踢掉，短时间内不许它重新上车。
        # 判定刻意排在**鉴权之后**：这条拒绝也走 REGISTRATION_REJECTED，
        # 若排在鉴权之前，"令牌已失效"会被渲染成"你在冷却期"，排查方向直接被指错。
        # 又排在**容量之前**：整机满了是所有人的问题，而"你被踢了"是针对这一台的，
        # 更具体的那个原因先报（同"端口未授权排在容量之后"的取舍，方向相反而已）。
        remaining = self._cooldown_remaining(client_id)
        if remaining > 0:
            await self._reject_register(
                writer,
                code=403,
                msg=(
                    f"该客户端刚被管理台踢出，冷却期还剩 {remaining:.0f}s"
                    f"（admin.kick_cooldown={self._config.admin.kick_cooldown:g}，"
                    "冷却结束前无法重新注册）"
                ),
                # retryable=True：这是"暂时不行，等会儿再来"，与"端口未授权"同一类。
                # 客户端会按退避策略继续重试，冷却一过自动上车。
                # 刻意不用 503（那在本项目里是"服务端整体没位置"，
                # 会把"这台机器被踢了"说成"服务器满了"）也不用 429（那是单客户端配额）。
                retryable=True,
                client_id=client_id,
                peer=peer,
                identity=identity.name,
            )
            return None

        if not self._registry.has(client_id) and self._registry.client_count >= self._config.limits.max_clients:
            reason = f"在线客户端数已达上限 {self._config.limits.max_clients}"
            # 503＝"服务端没位置了"，回头可能就好 → 可重试
            await self._reject_register(
                writer,
                code=503,
                msg=reason,
                retryable=True,
                client_id=client_id,
                peer=peer,
                identity=identity.name,
            )
            return None

        # 端口授权：刻意放在容量判定**之后**、登记会话**之前**。
        # 之后＝容量满时先报"整机满了"（503）比"你没这个端口的权限"更贴近实情；
        # 之前＝此时 registry 还没动过，拒绝不留任何脏状态（不必回滚端口归属）。
        unauthorized = [port for port in local_ports if not identity.allows(port)]
        if unauthorized:
            # **整体拒绝，绝不部分接受**："这台机器只暴露了一半端口"是极难排查的状态。
            # retryable=True 是热重载闭环的落点：运维把端口加进令牌表后，
            # 同一个客户端进程下一轮退避重试就会自动上车，不需要重启客户端。
            reason = (
                f"端口未授权：{unauthorized}（身份 {identity.name} 允许的内网端口："
                f"{identity.ports or '不限'}）"
            )
            await self._reject_register(
                writer,
                code=403,
                msg=reason,
                retryable=True,
                client_id=client_id,
                peer=peer,
                identity=identity.name,
            )
            return None

        # ⚠️ 注册消息里带着**令牌明文**，绝不能原样存进会话（MEMORY 不变量 1）。
        # 会话对象会被快照、被调试器、被将来的管理 API 顺手打出来；存一份令牌
        # 等于把明文扩散到内存各处，还会顺着日志与事件漏出去。
        # 要身份请读 session.identity —— 那是令牌表里的 name，不是密钥。
        sanitized = {key: value for key, value in msg.items() if key != "token"}
        session = ClientSession(
            client_id=client_id,
            reader=reader,
            writer=writer,
            peer=peer,
            identity=identity.name,
            # 写权限在注册时**快照**进会话：令牌到此已被抹掉，之后没有可回查的凭据；
            # 也正是"改令牌表 → 重连生效"这条语义的落点（详见 ClientSession 字段注释）
            can_manage_mapping=identity.can_manage_mapping,
            register_msg=sanitized,
        )

        previous = self._registry.add(session)
        if previous is not None:
            self._log.warning("客户端 %s 重复注册，旧连接（%s）被顶替", client_id, previous.peer)
            self._pending.fail_all_for_client(client_id, "该客户端已被新的连接顶替")
            await close_writer(previous.writer)

        claimed, conflicts = self._registry.claim_ports(client_id, local_ports)
        self._stats.clients_registered += 1
        self._sync_online()

        ack = make_msg(
            MsgType.REGISTER_ACK,
            ok=True,
            msg="注册成功",
            # 成功路径也显式给出 retryable=False：客户端不必"缺字段就推导"，
            # 日志与界面可以直接读它，语义只有一处（is_fatal）
            retryable=False,
            # 身份标签（不是令牌）——可观测性：日志/事件/GUI 能回答"在线的是谁"
            identity=identity.name,
            client_id=client_id,
            claimed=claimed,
            conflicts=conflicts,
            data_port=self._config.data.port,
            data_host=self._advertised_host(writer),
            heartbeat_interval=self._config.timeouts.heartbeat_interval,
            pair_timeout=self._config.timeouts.pair_timeout,
        )
        if not await self._send(session, ack):
            self._registry.remove(client_id)
            self._sync_online()
            return None

        self._log.info(
            "客户端 %s（%s）已上线，身份 %s，认领端口 %s%s",
            client_id,
            peer,
            identity.name,
            claimed or "无",
            f"，冲突被拒 {conflicts}" if conflicts else "",
        )
        self._events.emit(
            EventType.CLIENT_CONNECTED,
            client_id=client_id,
            peer=peer,
            identity=identity.name,
            claimed=claimed,
            conflicts=conflicts,
        )
        await self._broadcast_mapping_list()
        return session

    async def _reject_register(
        self,
        writer: asyncio.StreamWriter,
        *,
        code: int,
        msg: str,
        retryable: bool,
        client_id: Optional[str] = None,
        peer: Optional[str] = None,
        identity: Optional[str] = None,
    ) -> None:
        """回一条"注册被拒"的 ``register_ack``，并记账 + 记日志 + 发事件。

        ``retryable`` 是 ``403`` 的细分（见 :class:`RegistrationError`）：
        ``400``（客户端参数错）、``503``（容量满）与"端口未授权"都是**暂时**失败，
        客户端应当继续退避重试；只有"令牌无效 / 被吊销 / ``client_id`` 冒充"是**永久**失败。
        集中在这里生成回执，就不会出现"某个分支忘了带 retryable"的静默退化。

        **日志与事件也在这一个点上做**，三个理由：

        * 调用方各自 ``self._log.warning`` 时，``400`` 两条分支（client_id 非法、
          local_ports 非法）**根本没有日志**——那正是最需要看到的一类拒绝
          （配置写错了，运维只看得到"客户端一直上不了线"）。
        * 五条分支的文案会各自漂移，事件载荷却必须是同一套键。
        * 每次拒绝「一条 WARNING + 一个事件 + 一个计数」一一对应，管理台看到的
          与 stderr 里翻到的是同一批事实。

        ``client_id`` / ``peer`` / ``identity`` 允许缺省：

        * ``identity`` 在**鉴权失败**时必然为空——``verify`` 抛错意味着身份从未确立，
          这里给空串而不是 ``anonymous``，否则"冒充/令牌失效"会被渲染成"匿名用户"。
        * ``client_id`` 在 ``400``（非法 client_id）时给 ``None``：那个值未经校验、
          长度上限是 ``max_msg_len``（10MB），落进日志等于给对手一个日志放大器。
        """
        self._stats.registrations_rejected += 1
        self._log.warning(
            "拒绝注册：%s 来自 %s → [%s] %s（retryable=%s，身份 %s）",
            client_id or "(未提供)",
            peer or "未知来源",
            code,
            msg,
            retryable,
            identity or "未确立",
        )
        # 事件里固定给出全部键（空串而非缺键）：订阅方少一层 ``if "x" in payload``，
        # 也不会出现"某个分支少发一个键"的静默差异。与 MAPPING_REJECTED 同一风格。
        self._events.emit(
            EventType.REGISTRATION_REJECTED,
            code=code,
            retryable=retryable,
            msg=msg,
            client_id=client_id or "",
            peer=peer or "",
            identity=identity or "",
        )
        await self._send_raw(
            writer,
            make_msg(
                MsgType.REGISTER_ACK,
                ok=False,
                code=code,
                msg=msg,
                retryable=retryable,
                claimed=[],
                conflicts=[],
            ),
        )

    async def _disconnect_session(
        self,
        client_id: str,
        *,
        reason: str,
        session: Optional[ClientSession] = None,
    ) -> None:
        """下线一个客户端：释放端口、唤醒等待中的访客、关闭连接。"""
        current = self._registry.get(client_id)
        if session is not None and current is not None and current is not session:
            # 该身份已经被新连接顶替，不要误删新会话
            self._log.debug("客户端 %s 的旧连接关闭，保留当前会话", client_id)
            return
        if current is None and session is None:
            return

        removed = self._registry.remove(client_id)
        if removed is None:
            return

        self._sync_online()
        if self._limiter is not None:
            # 释放该客户端的限速桶（含按端口/按访客的分片），否则客户端增删会慢慢把内存吃满。
            # 必须用 forget_client 而不是 forget(client_id)：口径一变，桶的 key 就不再等于
            # client_id，单键释放会一条都匹配不上（桶随访客数 × 端口数无界增长）。
            self._limiter.forget_client(client_id)
        self._pending.fail_all_for_client(client_id, "客户端已离线")
        await close_writer(removed.writer)
        self._log.info("客户端 %s 已下线（%s），端口 %s 归属已释放", client_id, reason, sorted(removed.local_ports))
        self._events.emit(
            EventType.CLIENT_DISCONNECTED,
            client_id=client_id,
            reason=reason,
            ports=sorted(removed.local_ports),
        )
        await self._broadcast_mapping_list()

    # ------------------------------------------------------------------ #
    # 控制通道：指令处理（注册表自动收集）
    # ------------------------------------------------------------------ #

    @handler(MsgType.PING)
    async def _on_ping(self, msg: Dict[str, Any], session: ClientSession) -> None:
        await self._send(session, make_msg(MsgType.PONG, ts=time.time()))

    @handler(MsgType.PONG)
    async def _on_pong(self, msg: Dict[str, Any], session: ClientSession) -> None:
        return None

    @handler(MsgType.CONN_ERROR)
    async def _on_conn_error(self, msg: Dict[str, Any], session: ClientSession) -> None:
        """客户端连不上内网后端时的上报。立刻让等待中的访客收到 502。"""
        conn_id = msg.get("conn_id")
        if not isinstance(conn_id, str):
            return
        reason = msg.get("reason")
        detail = str(reason) if reason else "客户端无法连接内网后端"
        pending = self._pending.fail(conn_id, detail)
        if pending is None:
            self._log.debug("收到 %s 的 conn_error，但该请求已不在等待队列中", conn_id)
            return
        self._log.warning("客户端 %s 上报 conn_error（conn=%s）：%s", session.client_id, conn_id, detail)
        self._events.emit(
            EventType.CONN_ERROR,
            conn_id=conn_id,
            client_id=session.client_id,
            local_port=pending.local_port,
            reason=detail,
        )

    @handler(MsgType.SET_MAPPING)
    async def _on_set_mapping(self, msg: Dict[str, Any], session: ClientSession) -> None:
        """动态修改映射表。**需要该身份具备写权限**（``Identity.can_manage_mapping``）。"""
        # 权限判定刻意排在**格式校验之前**：一个改不动映射表的客户端，
        # 连"你的 mapping 格式哪里不对"都不必知道——少一处信息泄露面，也少一次白做的解析。
        #
        # 这条判定**只覆盖客户端指令**。管理台走 ``submit_mapping()``：它在本机同进程里，
        # 没有"身份"可言，给它加判定等于把管理台自己锁死（README 已写明该边界）。
        if not session.can_manage_mapping:
            reason = (
                f"身份 {session.identity or 'anonymous'} 没有修改映射表的权限"
                "（需要该令牌表条目 can_manage_mapping: true；改完令牌表需该客户端重连才生效）"
            )
            self._stats.mapping_rejected += 1
            self._log.warning(
                "拒绝客户端 %s（身份 %s）的 set_mapping：无映射表写权限",
                session.client_id,
                session.identity or "anonymous",
            )
            self._events.emit(
                EventType.MAPPING_REJECTED,
                client_id=session.client_id,
                identity=session.identity or "anonymous",
                reason="无映射表写权限",
            )
            # 回执专门带 code=403（与 register_ack 同一套 HTTP 语义），
            # 让客户端与 GUI 能机器区分"没权限"与"参数非法"；msg 本身也写得可以直接照做。
            await self._send(
                session, make_msg(MsgType.MAPPING_RESULT, ok=False, code=403, msg=reason)
            )
            return

        raw = msg.get("mapping")
        if not isinstance(raw, list):
            await self._send(session, make_msg(MsgType.MAPPING_RESULT, ok=False, msg="mapping 必须是数组"))
            return

        try:
            rules = parse_mapping(raw)
            # 与管理台共用同一内核（apply + 广播），校验也走同一份 parse_mapping
            diff = await self.submit_mapping(rules)
        except (ConfigError, TunnelError) as exc:
            self._log.warning("客户端 %s 提交的映射更新被拒绝：%s", session.client_id, exc)
            # 回执里用 exc.message（不带 [code] 前缀），只有日志才用 str(exc)——
            # 回执另有独立的 code 语义，塞前缀会出现 "[500] [500] ..." 叠字
            await self._send(session, make_msg(MsgType.MAPPING_RESULT, ok=False, msg=exc.message))
            return

        await self._send(
            session,
            make_msg(MsgType.MAPPING_RESULT, ok=True, msg=diff.describe(), diff=diff.to_dict()),
        )

    # ------------------------------------------------------------------ #
    # 数据通道
    # ------------------------------------------------------------------ #

    async def _handle_data(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = peer_name(writer)
        try:
            try:
                msg = await asyncio.wait_for(
                    recv_msg(reader), timeout=self._config.timeouts.connect_timeout
                )
            except TimeoutError:
                self._log.warning("数据通道 %s 在超时前没有 register，断开", peer)
                return
            except ProtocolError as exc:
                self._log.warning("数据通道 %s 读取出错：%s", peer, exc)
                return

            if msg.get("type") != MsgType.REGISTER:
                self._log.warning("数据通道 %s 首条消息不是 register（收到 %r），断开", peer, msg.get("type"))
                return

            conn_id = msg.get("conn_id")
            if not isinstance(conn_id, str) or not conn_id:
                self._log.warning("数据通道 %s 的 conn_id 非法，断开", peer)
                return

            pending = self._pending.attach(conn_id, reader, writer)
            if pending is None:
                return

            self._log.debug("数据通道已配对 conn=%s（%s）", conn_id, peer)
            self._events.emit(
                EventType.DATA_CHANNEL_OPENED,
                conn_id=conn_id,
                client_id=pending.client_id,
                peer=peer,
            )
            # 搬运由访客协程驱动，这里只等它收尾
            await pending.wait_done()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            self._log.exception("数据通道 %s 处理异常", peer)
        finally:
            await close_writer(writer)

    # ------------------------------------------------------------------ #
    # 访客通道
    # ------------------------------------------------------------------ #

    async def _handle_visitor(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        public_port: int,
    ) -> None:
        """一次访客请求的完整生命周期。"""
        peer = peer_name(writer)
        # 只取 IP、不带端口：按访客口径分桶时带上临时端口就等于"每条连接一个桶"
        visitor_host = peer_host(writer)
        conn_id = uuid.uuid4().hex
        pending: Optional[PendingConn] = None
        started = time.monotonic()

        try:
            rule = self._mapping.rule_for(public_port)
            if rule is None:
                # 只在"连接已建立、映射紧接着被摘掉"的窗口里出现
                self._log.warning("端口 %d 没有映射规则，回 404", public_port)
                await self._reply_http(reader, writer, 404, "No mapping for this port")
                return

            session = self._registry.pick_client(rule.local_port)
            if session is None:
                self._stats.requests_failed += 1
                self._log.warning("端口 %d 收到请求但没有在线客户端，回 502", public_port)
                await self._reply_http(reader, writer, 502, "No client online")
                return

            # 单客户端并发配额：超了就**直接拒**，不排队。
            # 排队会让访客连接白占着，还可能拖到 pair_timeout 才失败——
            # 症状比 429 难查得多，而且对客户端也没有任何好处。
            limit = self._config.limits.max_conns_per_client
            if limit > 0:
                inflight = self._pending.count_for_client(session.client_id)
                if inflight >= limit:
                    exc = QuotaExceededError(
                        f"客户端 {session.client_id} 的并发转发数已达上限 {limit}"
                    )
                    self._stats.requests_rejected += 1
                    self._log.warning("端口 %d 收到请求但客户端超配额，回 429：%s", public_port, exc)
                    # 对外只给通用文案：公网访客不该看到内部 client_id
                    await self._reply_http(reader, writer, exc.code, "Per-client concurrency limit reached")
                    return

            pending = PendingConn(
                conn_id=conn_id,
                public_port=public_port,
                local_port=rule.local_port,
                client_id=session.client_id,
                visitor_reader=reader,
                visitor_writer=writer,
            )
            self._pending.create(pending)
            self._stats.requests_total += 1
            self._stats.note_request(public_port)
            self._log.debug(
                "访客 %s 请求 %d -> %d，派给客户端 %s，conn=%s",
                peer,
                public_port,
                rule.local_port,
                session.client_id,
                conn_id,
            )
            self._events.emit(
                EventType.REQUEST_START,
                conn_id=conn_id,
                peer=peer,
                public_port=public_port,
                local_port=rule.local_port,
                client_id=session.client_id,
            )

            notified = await self._send(
                session,
                make_msg(
                    MsgType.NEW_CONN,
                    conn_id=conn_id,
                    local_port=rule.local_port,
                    public_port=public_port,
                ),
            )
            if not notified:
                self._stats.requests_failed += 1
                await self._reply_http(reader, writer, 502, "No client online")
                return

            if not await pending.wait_ready(self._config.timeouts.pair_timeout):
                self._stats.requests_failed += 1
                if not pending.closed:
                    pending.fail(f"等待数据通道超时（{self._config.timeouts.pair_timeout:.0f}s）")
                self._log.warning("conn=%s 配对失败：%s", conn_id, pending.error)
                await self._reply_http(reader, writer, 502, pending.error or "Data channel not ready")
                return

            if pending.data_reader is None or pending.data_writer is None:  # pragma: no cover - 防御
                self._stats.requests_failed += 1
                await self._reply_http(reader, writer, 502, "Data channel gone")
                return

            stats = await pipe_both(
                reader,
                writer,
                pending.data_reader,
                pending.data_writer,
                label=f"conn={conn_id}",
                logger=self._log,
                # 汇总单位由 limits.rate_limit_scope 决定：默认按 client_id 汇总，
                # 一个客户端开多条连接也绕不开自己的总配额；按端口/按访客则各自一份额度
                rate_limit=self._limiter,
                limit_key=self._limit_key(
                    session.client_id,
                    public_port=public_port,
                    visitor_host=visitor_host,
                ),
            )
            self._stats.bytes_upload += stats.upload
            self._stats.bytes_download += stats.download
            self._stats.throttled_seconds += stats.throttled
            self._stats.add_traffic(
                public_port,
                upload=stats.upload,
                download=stats.download,
                throttled=stats.throttled,
            )

        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 单个请求出错不能影响其他请求
            self._stats.requests_failed += 1
            self._log.exception("处理访客请求 conn=%s 时发生未预期异常", conn_id)
            await self._reply_http(reader, writer, 502, "Internal tunnel error")
        finally:
            if pending is not None:
                self._pending.discard(conn_id)
                pending.finish()
                await close_writer(pending.data_writer)
            await close_writer(writer)
            self._events.emit(
                EventType.REQUEST_END,
                conn_id=conn_id,
                public_port=public_port,
                duration=round(time.monotonic() - started, 4),
            )

    # ------------------------------------------------------------------ #
    # 看门狗
    # ------------------------------------------------------------------ #

    def _collect_idle_sessions(self) -> List[ClientSession]:
        return self._registry.collect_expired(self._config.timeouts.client_idle_timeout)

    async def _expire_session(self, session: ClientSession) -> None:
        self._log.warning(
            "看门狗：客户端 %s 已 %.0fs 无任何消息，强制断开",
            session.client_id,
            session.idle_for(),
        )
        await self._disconnect_session(session.client_id, reason="心跳超时", session=session)

    # ------------------------------------------------------------------ #
    # 辅助
    # ------------------------------------------------------------------ #

    @staticmethod
    def _build_limiter(limits: LimitsConfig) -> Optional[ClientRateLimiter]:
        """按配置装配限速器。

        两个方向都不限速时返回 ``None``，让 ``pipe_both`` 走无钩子的原路径——
        默认配置下这条链路的开销必须是零。

        ``scope`` 在这里定格：口径决定 key 怎么编，中途换口径只会让新旧两种 key
        同时存在（旧桶要等客户端下线才回收），而配置本来就是启动时读一次的。
        """
        limiter = ClientRateLimiter(
            upload_bps=limits.per_client_upload_bps,
            download_bps=limits.per_client_download_bps,
            scope=limits.rate_limit_scope,
            max_keys=limits.rate_limit_max_keys,
        )
        return limiter if limiter.enabled else None

    def _limit_key(self, client_id: str, *, public_port: int, visitor_host: str) -> str:
        """按 ``limits.rate_limit_scope`` 编出本次转发的限速汇总 key。

        不限速时返回 ``client_id``（该值不参与任何计算，只是保持原样便于排查）。
        """
        if self._limiter is None:
            return client_id
        return self._limiter.key_for(client_id, public_port=public_port, visitor_host=visitor_host)

    def _sync_online(self) -> None:
        """把"当前在线客户端数"这个 gauge 同步进统计。"""
        self._stats.clients_online = self._registry.client_count

    async def _send(self, session: ClientSession, msg: Dict[str, Any]) -> bool:
        """向某个客户端发一条控制指令。失败返回 False（不抛异常）。"""
        return await self._send_raw(session.writer, msg, label=session.client_id)

    async def _send_raw(
        self,
        writer: asyncio.StreamWriter,
        msg: Dict[str, Any],
        *,
        label: str = "",
    ) -> bool:
        try:
            await send_msg(writer, msg)
            return True
        except (OSError, ConnectionError, RuntimeError) as exc:
            self._log.debug("向 %s 发送 %r 失败：%s", label or "对端", msg.get("type"), exc)
            return False

    async def _broadcast_mapping_list(self) -> None:
        """把当前映射表广播给所有在线客户端，让各端的表格保持同步。"""
        sessions = self._registry.sessions()
        if not sessions:
            return
        payload = make_msg(MsgType.MAPPING_LIST, mapping=[rule.to_dict() for rule in self._mapping.rules()])
        for session in sessions:
            if not await self._send(session, payload):
                continue

    def _advertised_host(self, writer: asyncio.StreamWriter) -> str:
        """告诉客户端：数据通道该连哪个地址。

        配置里写了 ``advertise_host`` 就用它；没写则复用客户端连入时看到的本地地址，
        于是同一份配置在本机演示与公网部署下都能直接跑。
        """
        if self._config.advertise_host:
            return self._config.advertise_host
        info: object = writer.get_extra_info("sockname")
        if isinstance(info, tuple) and info:
            return str(info[0])
        return self._config.control.host

    @staticmethod
    async def _reply_http(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        status: int,
        message: str,
    ) -> None:
        """给访客回一个最小的 HTTP 错误响应。

        服务端不做完整的 HTTP 解析——转发是纯字节搬运，只在**无法转发**时才
        自己造响应。手写状态行比让 curl 收到空回复要好得多。

        顺序上有两点很讲究：

        1. 写完先 ``write_eof()`` 发 FIN，明确告诉对端"我说完了"；
        2. 再把对端已经发来的请求字节**读掉**，然后才允许上层 close。

        第 2 步不是可有可无的：带着未读的接收数据 close，TCP 会退化成发 RST 而不是 FIN，
        而 Windows 在收到 RST 时会**丢弃客户端接收缓冲里尚未读走的响应**——
        表现为"访客收到空回复"。curl 因为读得够快常常侥幸读全，脚本化的客户端则经常拿到 0 字节，
        排查起来非常费劲。
        """
        phrase = _HTTP_PHRASES.get(status, "Error")
        body = f"{status} {message}\n".encode("utf-8")
        head = (
            f"HTTP/1.1 {status} {phrase}\r\n"
            f"Content-Type: text/plain; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        ).encode("latin-1")
        try:
            writer.write(head + body)
            await writer.drain()
            close_write_side(writer)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(reader.read(65536), timeout=0.1)
        except (OSError, ConnectionError, RuntimeError):
            pass

    def __repr__(self) -> str:
        return (
            f"TunnelServer(name={self._config.name!r}, clients={self._registry.client_count}, "
            f"listening={self._mapping.listen_ports()})"
        )
