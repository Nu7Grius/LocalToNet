# -*- coding: utf-8 -*-
"""
localtonet.client.core —— 客户端控制长连接
============================================
客户端的一生：**连上 → 注册 → 保活 → 断线重连**，循环往复。

关键设计：

1. **单读者原则**
   一条控制连接上只允许一个协程读 socket。心跳只发不收，pong 交给主读循环
   收到后再通知心跳对象。两个协程抢同一个 reader 会丢消息，且极难排查。

2. **失败要分类**
   * 网络抖动（连不上、对端消失）→ 指数退避重连，1s 起步 60s 封顶。
   * 鉴权失败（403）→ **永久性失败**，直接停掉，不要每 60 秒再去撞一次墙。
   业务上把这两种情况混在一起处理，是"重试风暴"的常见来源。

3. **重连后必须重新注册并重新认领端口**
   服务端在客户端掉线时会释放端口归属，所以每次会话都走完整的注册流程，
   而不是假设"上次认领过就还在"。

4. **指令处理走注册表**（见 :mod:`localtonet.core.dispatcher`）
   新增指令 = 加一个 ``@handler`` 方法，主循环不动。
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Dict, List, Optional, Sequence

from config import ClientConfig, MappingRule
from localtonet import __version__
from localtonet.client.forwarder import DataChannelForwarder, ForwardResult
from localtonet.core.backoff import Backoff
from localtonet.core.dispatcher import MessageDispatcher, handler
from localtonet.core.events import EventBus, EventType
from localtonet.core.heartbeat import HeartbeatTask
from localtonet.core.pipe import close_writer
from localtonet.core.runtime import cancel_all, spawn
from localtonet.errors import RegistrationError, TunnelError
from logging_setup import get_logger
from protocol import MsgType, ProtocolError, make_msg, recv_msg, send_msg

__all__ = ["TunnelClient", "ClientStats", "generate_client_id"]

MAX_CONCURRENT_FORWARDS = 256
"""同时进行中的转发上限。防止客户端被突发流量拖垮——
超出时立刻上报 ``conn_error``，让访客拿到明确的 502 而不是无限等待。"""


def generate_client_id() -> str:
    """生成默认客户端标识：主机名 + 随机后缀。

    带随机后缀是为了同一台机器上跑多个客户端实例时不会互相顶号。
    """
    host = socket.gethostname() or "client"
    return f"{host}-{uuid.uuid4().hex[:6]}"


@dataclass
class ClientStats:
    """客户端累计计数。"""

    forwards_total: int = 0
    forwards_failed: int = 0
    bytes_upload: int = 0
    bytes_download: int = 0
    reconnects: int = 0

    def to_dict(self) -> Dict[str, int]:
        return {
            "forwards_total": self.forwards_total,
            "forwards_failed": self.forwards_failed,
            "bytes_upload": self.bytes_upload,
            "bytes_download": self.bytes_download,
            "reconnects": self.reconnects,
        }


class TunnelClient:
    """内网穿透客户端。"""

    def __init__(
        self,
        config: ClientConfig,
        *,
        events: Optional[EventBus] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._config = config
        self._log = logger or get_logger("client")
        self._events = events or EventBus(self._log)
        self._dispatcher = MessageDispatcher.from_object(self, logger=self._log)
        self._backoff = Backoff(config.reconnect)

        self._client_id = config.client_id or generate_client_id()
        self._control_writer: Optional[asyncio.StreamWriter] = None
        self._heartbeat: Optional[HeartbeatTask] = None

        self._data_host: str = config.server_host
        self._data_port: int = config.data_port
        self._claimed: List[int] = []
        self._conflicts: List[int] = []
        self._mapping_view: List[Dict[str, Any]] = []

        self._stats = ClientStats()
        self._state = "idle"
        self._fatal = False
        self._last_error = ""
        self._running = False
        self._stop_event = asyncio.Event()
        self._mapping_waiter: Optional[asyncio.Future] = None

        self._tasks: set[asyncio.Task] = set()
        self._forward_tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ #
    # 对外状态
    # ------------------------------------------------------------------ #

    @property
    def client_id(self) -> str:
        return self._client_id

    @property
    def state(self) -> str:
        return self._state

    @property
    def stats(self) -> ClientStats:
        return self._stats

    @property
    def events(self) -> EventBus:
        return self._events

    @property
    def claimed_ports(self) -> List[int]:
        return list(self._claimed)

    @property
    def conflicted_ports(self) -> List[int]:
        return list(self._conflicts)

    @property
    def remote_mapping(self) -> List[Dict[str, Any]]:
        """服务端下发的映射表（GUI 表格直接绑定它）。"""
        return list(self._mapping_view)

    def snapshot(self) -> Dict[str, Any]:
        return {
            "client_id": self._client_id,
            "state": self._state,
            "server": f"{self._config.server_host}:{self._config.control_port}",
            "local_ports": list(self._config.local_ports),
            "claimed": list(self._claimed),
            "conflicts": list(self._conflicts),
            "mapping": list(self._mapping_view),
            "active_forwards": len(self._forward_tasks),
            "stats": self._stats.to_dict(),
            "heartbeat": self._heartbeat.stats if self._heartbeat else None,
        }

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    async def run(self) -> None:
        """主循环：连上 → 服务 → 断了退避重连，直到 :meth:`stop` 被调用。"""
        self._running = True
        self._log.info(
            "客户端 %s 启动，目标 %s:%d，认领本地端口 %s",
            self._client_id,
            self._config.server_host,
            self._config.control_port,
            self._config.local_ports,
        )

        while self._running:
            try:
                await self._session()
                if not self._running:
                    break
                reason = self._last_error or "控制连接已断开"
            except asyncio.CancelledError:
                raise
            except RegistrationError as exc:
                if exc.is_fatal:
                    self._state = "stopped"
                    self._log.error("注册被永久拒绝（%s），停止重试", exc)
                    self._events.emit(EventType.CONTROL_LOST, reason=str(exc), fatal=True)
                    return
                reason = f"注册失败：{exc}"
            except (OSError, TimeoutError) as exc:
                reason = f"连接服务端失败：{exc}"
            except ProtocolError as exc:
                reason = f"控制通道协议错误：{exc}"
            except TunnelError as exc:
                reason = f"客户端错误：{exc}"

            if not self._running:
                break

            self._stats.reconnects += 1
            delay = self._backoff.next_delay()
            self._state = "reconnecting"
            self._log.warning("控制连接不可用（%s），%.1fs 后重连（第 %d 次）", reason, delay, self._backoff.attempt)
            self._events.emit(
                EventType.RECONNECTING,
                reason=reason,
                delay=round(delay, 2),
                attempt=self._backoff.attempt,
            )
            if await self._sleep_or_stop(delay):
                break

        self._state = "stopped"
        self._log.info("客户端 %s 已停止，累计统计：%s", self._client_id, self._stats.to_dict())

    async def stop(self) -> None:
        """停止客户端并清理所有连接与任务。"""
        self._running = False
        self._stop_event.set()

        heartbeat = self._heartbeat
        if heartbeat is not None:
            heartbeat.stop()

        writer = self._control_writer
        if writer is not None and not writer.is_closing():
            writer.close()

        await cancel_all(self._forward_tasks, logger=self._log)
        await cancel_all(self._tasks, logger=self._log)

        waiter = self._mapping_waiter
        if waiter is not None and not waiter.done():
            waiter.cancel()

    # ------------------------------------------------------------------ #
    # 单次会话
    # ------------------------------------------------------------------ #

    async def _session(self) -> None:
        """建立一次控制连接并服务到断开。"""
        cfg = self._config
        self._state = "connecting"
        self._last_error = ""

        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(cfg.server_host, cfg.control_port),
            timeout=cfg.timeouts.connect_timeout,
        )
        self._control_writer = writer
        self._backoff.reset()
        self._state = "online"
        self._log.info("已连上控制通道 %s:%d", cfg.server_host, cfg.control_port)
        self._events.emit(EventType.CONTROL_CONNECTED, host=cfg.server_host, port=cfg.control_port)

        hb_task: Optional[asyncio.Task] = None
        try:
            await self._register_session(reader, writer)

            self._heartbeat = HeartbeatTask(
                interval=cfg.timeouts.heartbeat_interval,
                pong_timeout=cfg.timeouts.pong_timeout,
                send_ping=self._send_ping,
                name=f"heartbeat:{self._client_id}",
                logger=self._log,
            )
            hb_task = spawn(
                self._heartbeat.run(on_lost=self._on_heartbeat_lost),
                name="client-heartbeat",
                logger=self._log,
                track=self._tasks,
            )

            while not self._stop_event.is_set():
                msg = await recv_msg(reader)
                await self._dispatcher.dispatch(msg)

        finally:
            heartbeat = self._heartbeat
            if heartbeat is not None:
                heartbeat.stop()
                self._heartbeat = None
            if hb_task is not None:
                await cancel_all({hb_task}, logger=self._log)
            await cancel_all(self._forward_tasks, logger=self._log)
            await close_writer(writer)
            self._control_writer = None
            if self._state == "online":
                self._state = "idle"

    async def _register_session(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        cfg = self._config
        await send_msg(
            writer,
            make_msg(
                MsgType.REGISTER_CLIENT,
                client_id=self._client_id,
                local_ports=list(cfg.local_ports),
                token=cfg.auth_token,
                version=__version__,
                hostname=socket.gethostname(),
            ),
        )

        ack = await asyncio.wait_for(recv_msg(reader), timeout=cfg.timeouts.connect_timeout)
        if ack.get("type") != MsgType.REGISTER_ACK:
            raise RegistrationError(f"注册回执类型异常：{ack.get('type')!r}")

        if not ack.get("ok"):
            code = ack.get("code")
            raise RegistrationError(
                str(ack.get("msg") or "服务端拒绝了注册"),
                code=code if isinstance(code, int) else 500,
            )

        self._claimed = [port for port in (ack.get("claimed") or []) if isinstance(port, int)]
        self._conflicts = [port for port in (ack.get("conflicts") or []) if isinstance(port, int)]
        if isinstance(ack.get("data_port"), int) and ack["data_port"] > 0:
            self._data_port = ack["data_port"]
        advertised = ack.get("data_host")
        if isinstance(advertised, str) and advertised:
            self._data_host = advertised

        self._log.info(
            "注册成功：认领端口 %s%s，数据通道 %s:%d",
            self._claimed or "无",
            f"，冲突被拒 {self._conflicts}" if self._conflicts else "",
            self._data_host,
            self._data_port,
        )
        if not self._claimed:
            self._log.warning("本次注册没有认领到任何端口，访客流量不会被派发到本客户端")
        self._events.emit(
            EventType.CLIENT_REGISTERED,
            client_id=self._client_id,
            claimed=list(self._claimed),
            conflicts=list(self._conflicts),
            data_host=self._data_host,
            data_port=self._data_port,
        )

    async def _sleep_or_stop(self, delay: float) -> bool:
        """等待重连延迟；若期间收到 stop 则返回 True。"""
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
        except TimeoutError:
            return False
        return True

    # ------------------------------------------------------------------ #
    # 指令处理
    # ------------------------------------------------------------------ #

    @handler(MsgType.NEW_CONN)
    async def _on_new_conn(self, msg: Dict[str, Any]) -> None:
        """服务端通知：开一条数据通道接这次请求。"""
        conn_id = msg.get("conn_id")
        local_port = msg.get("local_port")
        if not isinstance(conn_id, str) or not conn_id:
            self._log.warning("收到缺少 conn_id 的 new_conn：%r", msg)
            return
        if isinstance(local_port, bool) or not isinstance(local_port, int):
            self._log.warning("收到 local_port 非法的 new_conn：%r", msg)
            await self.report_conn_error(conn_id, "new_conn 指令缺少合法的 local_port")
            return

        if len(self._forward_tasks) >= MAX_CONCURRENT_FORWARDS:
            reason = f"客户端并发转发数已达上限 {MAX_CONCURRENT_FORWARDS}"
            self._log.warning("拒绝 conn=%s：%s", conn_id, reason)
            await self.report_conn_error(conn_id, reason)
            return

        forwarder = DataChannelForwarder(
            server_host=self._data_host,
            data_port=self._data_port,
            local_host=self._config.local_host,
            connect_timeout=self._config.timeouts.connect_timeout,
            logger=self._log,
            events=self._events,
        )
        spawn(
            self._run_forward(forwarder, conn_id, local_port),
            name=f"forward:{conn_id}",
            logger=self._log,
            track=self._forward_tasks,
        )

    @handler(MsgType.PONG)
    async def _on_pong(self, msg: Dict[str, Any]) -> None:
        if self._heartbeat is not None:
            self._heartbeat.note_pong()

    @handler(MsgType.PING)
    async def _on_ping(self, msg: Dict[str, Any]) -> None:
        """协议对称性保留：服务端若主动探测，客户端照样回 pong。"""
        writer = self._control_writer
        if writer is not None and not writer.is_closing():
            await send_msg(writer, make_msg(MsgType.PONG, ts=time.time()))

    @handler(MsgType.MAPPING_LIST)
    async def _on_mapping_list(self, msg: Dict[str, Any]) -> None:
        raw = msg.get("mapping")
        mapping = [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
        self._mapping_view = mapping
        self._log.info("收到映射表更新，共 %d 条：%s", len(mapping), describe_mapping(mapping))
        self._events.emit(EventType.MAPPING_CHANGED, mapping=mapping, source="server")

    @handler(MsgType.MAPPING_RESULT)
    async def _on_mapping_result(self, msg: Dict[str, Any]) -> None:
        waiter = self._mapping_waiter
        if waiter is not None and not waiter.done():
            waiter.set_result(dict(msg))
        self._log.info("映射修改回执：ok=%s %s", msg.get("ok"), msg.get("msg"))

    # ------------------------------------------------------------------ #
    # 对外动作
    # ------------------------------------------------------------------ #

    async def set_mapping(self, rules: Sequence[MappingRule], *, timeout: float = 5.0) -> Dict[str, Any]:
        """动态修改服务端映射表，返回 ``mapping_result`` 的内容。

        这是 GUI 表格"提交"按钮背后的接口。
        """
        writer = self._control_writer
        if writer is None or writer.is_closing():
            raise RegistrationError("控制连接尚未建立，无法修改映射")

        loop = asyncio.get_running_loop()
        waiter: asyncio.Future = loop.create_future()
        previous, self._mapping_waiter = self._mapping_waiter, waiter
        if previous is not None and not previous.done():
            previous.cancel()

        try:
            await send_msg(
                writer,
                make_msg(MsgType.SET_MAPPING, mapping=[rule.to_dict() for rule in rules]),
            )
            return await asyncio.wait_for(waiter, timeout=timeout)
        finally:
            if self._mapping_waiter is waiter:
                self._mapping_waiter = None

    async def report_conn_error(self, conn_id: str, reason: str) -> None:
        """上报"这次转发失败了"，让服务端立刻给访客回 502。"""
        writer = self._control_writer
        if writer is None or writer.is_closing():
            self._log.warning("控制连接不可用，无法上报 conn_error（conn=%s）", conn_id)
            return
        try:
            await send_msg(writer, make_msg(MsgType.CONN_ERROR, conn_id=conn_id, reason=reason))
        except (OSError, ConnectionError, RuntimeError) as exc:
            self._log.warning("上报 conn_error 失败（conn=%s）：%s", conn_id, exc)

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    async def _run_forward(
        self,
        forwarder: DataChannelForwarder,
        conn_id: str,
        local_port: int,
    ) -> None:
        result: ForwardResult = await forwarder.run(
            conn_id,
            local_port,
            report_error=self.report_conn_error,
        )
        self._stats.forwards_total += 1
        if not result.ok:
            self._stats.forwards_failed += 1
        self._stats.bytes_upload += result.upload
        self._stats.bytes_download += result.download
        self._events.emit(EventType.REQUEST_END, **result.to_dict())

    async def _send_ping(self) -> None:
        writer = self._control_writer
        if writer is None or writer.is_closing():
            raise ConnectionError("控制连接已关闭")
        await send_msg(writer, make_msg(MsgType.PING, ts=time.time()))

    def _on_heartbeat_lost(self, reason: str) -> None:
        """心跳判定连接已废。关掉 writer，让主读循环立刻收到 EOF 并退出。"""
        self._last_error = reason
        self._log.warning("心跳判定控制连接失效：%s", reason)
        self._events.emit(EventType.CONTROL_LOST, reason=reason)
        writer = self._control_writer
        if writer is not None and not writer.is_closing():
            writer.close()

    def __repr__(self) -> str:
        return (
            f"TunnelClient(id={self._client_id!r}, state={self._state}, "
            f"claimed={self._claimed}, forwards={len(self._forward_tasks)})"
        )


def describe_mapping(mapping: Sequence[Dict[str, Any]]) -> str:
    """把映射表压成一行可读文本，用于日志。"""
    if not mapping:
        return "（空）"
    parts = []
    for item in mapping:
        parts.append(f"{item.get('public_port')}->{item.get('local_port')}")
    return ", ".join(parts)
