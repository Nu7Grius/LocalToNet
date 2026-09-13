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
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from config import ConfigError, MappingRule, ServerConfig, check_port
from localtonet.core.dispatcher import MessageDispatcher, handler
from localtonet.core.events import EventBus, EventType
from localtonet.core.heartbeat import Watchdog
from localtonet.core.pipe import close_writer, pipe_both
from localtonet.core.runtime import cancel_all, peer_name, spawn
from localtonet.errors import AuthError, TunnelError
from localtonet.server.auth import Authenticator, build_authenticator
from localtonet.server.mapping import InMemoryMappingStore, MappingDiff, MappingManager, MappingStore
from localtonet.server.pending import PendingConn, PendingTable
from localtonet.server.registry import ClientRegistry, ClientSession
from logging_setup import get_logger
from protocol import MsgType, ProtocolError, make_msg, recv_msg, send_msg

__all__ = ["TunnelServer", "ServerStats"]

_HTTP_PHRASES = {
    400: "Bad Request",
    403: "Forbidden",
    404: "Not Found",
    409: "Conflict",
    500: "Internal Server Error",
    502: "Bad Gateway",
    503: "Service Unavailable",
}


@dataclass
class ServerStats:
    """服务端累计计数。启动至今不清零，给日志与未来的指标接口用。"""

    clients_registered: int = 0
    requests_total: int = 0
    requests_failed: int = 0
    bytes_upload: int = 0
    bytes_download: int = 0

    def to_dict(self) -> Dict[str, int]:
        return {
            "clients_registered": self.clients_registered,
            "requests_total": self.requests_total,
            "requests_failed": self.requests_failed,
            "bytes_upload": self.bytes_upload,
            "bytes_download": self.bytes_download,
        }


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
    ) -> None:
        self._config = config
        self._log = logger or get_logger("server")
        self._events = events or EventBus(self._log)
        self._auth = authenticator or build_authenticator(config.auth)
        self._registry = registry or ClientRegistry(logger=self._log)
        self._pending = PendingTable(logger=self._log)
        self._mapping = MappingManager(
            store=mapping_store or InMemoryMappingStore(),
            on_visitor=self._handle_visitor,
            host=config.control.host,
            logger=self._log,
            events=self._events,
        )
        self._dispatcher = MessageDispatcher.from_object(self, logger=self._log)
        self._stats = ServerStats()

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
        self._log.info("启动服务端 %s（鉴权：%s）", cfg.name, self._auth.name)

        await self._mapping.start(cfg.mapping)

        try:
            self._control_server = await asyncio.start_server(
                self._handle_control, cfg.control.host, cfg.control.port
            )
            self._data_server = await asyncio.start_server(self._handle_data, cfg.data.host, cfg.data.port)
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

    def snapshot(self) -> Dict[str, Any]:
        """一次性拿全服务端状态。GUI 表格、健康检查都可以直接用这个。"""
        return {
            "name": self._config.name,
            "clients": self._registry.snapshot(),
            "port_owner": self._registry.port_owner_snapshot(),
            "mapping": [rule.to_dict() for rule in self._mapping.rules()],
            "listening": self._mapping.listen_ports(),
            "pending": len(self._pending),
            "stats": self._stats.to_dict(),
        }

    # ------------------------------------------------------------------ #
    # 控制通道
    # ------------------------------------------------------------------ #

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
            await self._send_raw(
                writer,
                make_msg(MsgType.REGISTER_ACK, ok=False, code=400, msg="client_id 必须是非空字符串", claimed=[], conflicts=[]),
            )
            return None
        client_id = client_id.strip()

        try:
            local_ports = self._parse_ports(msg.get("local_ports"))
        except ConfigError as exc:
            await self._send_raw(
                writer,
                make_msg(MsgType.REGISTER_ACK, ok=False, code=400, msg=str(exc), claimed=[], conflicts=[]),
            )
            return None

        try:
            self._auth.verify(msg, peer)
        except AuthError as exc:
            self._log.warning("拒绝客户端 %s（%s）：%s", client_id, peer, exc)
            await self._send_raw(
                writer,
                make_msg(MsgType.REGISTER_ACK, ok=False, code=exc.code, msg=str(exc), claimed=[], conflicts=[]),
            )
            return None

        if not self._registry.has(client_id) and self._registry.client_count >= self._config.limits.max_clients:
            reason = f"在线客户端数已达上限 {self._config.limits.max_clients}"
            self._log.warning("拒绝客户端 %s：%s", client_id, reason)
            await self._send_raw(
                writer,
                make_msg(MsgType.REGISTER_ACK, ok=False, code=503, msg=reason, claimed=[], conflicts=[]),
            )
            return None

        session = ClientSession(client_id=client_id, reader=reader, writer=writer, peer=peer, register_msg=dict(msg))

        previous = self._registry.add(session)
        if previous is not None:
            self._log.warning("客户端 %s 重复注册，旧连接（%s）被顶替", client_id, previous.peer)
            self._pending.fail_all_for_client(client_id, "该客户端已被新的连接顶替")
            await close_writer(previous.writer)

        claimed, conflicts = self._registry.claim_ports(client_id, local_ports)
        self._stats.clients_registered += 1

        ack = make_msg(
            MsgType.REGISTER_ACK,
            ok=True,
            msg="注册成功",
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
            return None

        self._log.info(
            "客户端 %s（%s）已上线，认领端口 %s%s",
            client_id,
            peer,
            claimed or "无",
            f"，冲突被拒 {conflicts}" if conflicts else "",
        )
        self._events.emit(
            EventType.CLIENT_CONNECTED,
            client_id=client_id,
            peer=peer,
            claimed=claimed,
            conflicts=conflicts,
        )
        await self._broadcast_mapping_list()
        return session

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
        """动态修改映射表。"""
        raw = msg.get("mapping")
        if not isinstance(raw, list):
            await self._send(session, make_msg(MsgType.MAPPING_RESULT, ok=False, msg="mapping 必须是数组"))
            return

        try:
            rules = self._parse_mapping(raw)
            diff = await self._mapping.apply(rules)
        except (ConfigError, TunnelError) as exc:
            self._log.warning("客户端 %s 提交的映射更新被拒绝：%s", session.client_id, exc)
            await self._send(session, make_msg(MsgType.MAPPING_RESULT, ok=False, msg=str(exc)))
            return

        await self._send(
            session,
            make_msg(MsgType.MAPPING_RESULT, ok=True, msg=diff.describe(), diff=diff.to_dict()),
        )
        await self._broadcast_mapping_list()

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
        conn_id = uuid.uuid4().hex
        pending: Optional[PendingConn] = None
        started = time.monotonic()

        try:
            rule = self._mapping.rule_for(public_port)
            if rule is None:
                # 只在"连接已建立、映射紧接着被摘掉"的窗口里出现
                self._log.warning("端口 %d 没有映射规则，回 404", public_port)
                await self._reply_http(writer, 404, "No mapping for this port")
                return

            session = self._registry.pick_client(rule.local_port)
            if session is None:
                self._stats.requests_failed += 1
                self._log.warning("端口 %d 收到请求但没有在线客户端，回 502", public_port)
                await self._reply_http(writer, 502, "No client online")
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
                await self._reply_http(writer, 502, "No client online")
                return

            if not await pending.wait_ready(self._config.timeouts.pair_timeout):
                self._stats.requests_failed += 1
                if not pending.closed:
                    pending.fail(f"等待数据通道超时（{self._config.timeouts.pair_timeout:.0f}s）")
                self._log.warning("conn=%s 配对失败：%s", conn_id, pending.error)
                await self._reply_http(writer, 502, pending.error or "Data channel not ready")
                return

            if pending.data_reader is None or pending.data_writer is None:  # pragma: no cover - 防御
                self._stats.requests_failed += 1
                await self._reply_http(writer, 502, "Data channel gone")
                return

            stats = await pipe_both(
                reader,
                writer,
                pending.data_reader,
                pending.data_writer,
                label=f"conn={conn_id}",
                logger=self._log,
            )
            self._stats.bytes_upload += stats.upload
            self._stats.bytes_download += stats.download

        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 单个请求出错不能影响其他请求
            self._stats.requests_failed += 1
            self._log.exception("处理访客请求 conn=%s 时发生未预期异常", conn_id)
            await self._reply_http(writer, 502, "Internal tunnel error")
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
    def _parse_ports(raw: Any) -> List[int]:
        if not isinstance(raw, list) or not raw:
            raise ConfigError("local_ports 必须是非空端口数组")
        ports: List[int] = []
        for index, item in enumerate(raw):
            if isinstance(item, bool) or not isinstance(item, int):
                raise ConfigError(f"local_ports[{index}] 必须是整数端口")
            ports.append(check_port(item, f"local_ports[{index}]"))
        if len(set(ports)) != len(ports):
            raise ConfigError("local_ports 存在重复端口")
        return ports

    @staticmethod
    def _parse_mapping(raw: List[Any]) -> List[MappingRule]:
        if not raw:
            raise ConfigError("mapping 不能为空，至少保留一条映射")
        rules = []
        seen: Dict[int, int] = {}
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                raise ConfigError(f"mapping[{index}] 必须是对象")
            rule = MappingRule.from_dict(item, f"mapping[{index}]")
            if rule.public_port in seen:
                raise ConfigError(
                    f"mapping[{index}].public_port={rule.public_port} 与 mapping[{seen[rule.public_port]}] 重复"
                )
            seen[rule.public_port] = index
            rules.append(rule)
        return rules

    @staticmethod
    async def _reply_http(writer: asyncio.StreamWriter, status: int, message: str) -> None:
        """给访客回一个最小的 HTTP 错误响应。

        MVP 不做完整的 HTTP 解析——转发是纯字节搬运，服务端只在**无法转发**时才
        自己造响应。手写状态行比让 curl 收到空回复要好得多。
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
        except (OSError, ConnectionError, RuntimeError):
            pass

    def __repr__(self) -> str:
        return (
            f"TunnelServer(name={self._config.name!r}, clients={self._registry.client_count}, "
            f"listening={self._mapping.listen_ports()})"
        )
