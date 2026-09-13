# -*- coding: utf-8 -*-
"""
localtonet.client.forwarder —— 数据通道与内网后端
===================================================
处理一条 ``new_conn`` 指令，也就是"有一次访客请求要接进来"。

**连接顺序很关键**：先连内网后端，再开数据通道。

如果反过来——先开通道再连后端——后端没启动时我们会白占一条 TCP 连接、
在服务端留下一个正在等待的挂起项，然后才上报失败。先连后端的做法让失败**尽早暴露**，
还能省掉一次无意义的往返。客户端随后通过控制通道上报 ``conn_error``，
服务端据此立刻给访客回 502，而不是让访客一直等到配对超时。

**扩展点**：限速/限额的挂载点是 ``pipe_both`` 内部的写入循环，
本模块只负责建立连接与收尾，不掺业务策略。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from localtonet.core.events import EventBus
from localtonet.core.pipe import close_writer, pipe_both
from logging_setup import get_logger
from protocol import MsgType, make_msg, send_msg

__all__ = ["ForwardResult", "DataChannelForwarder"]

ReportErrorFn = Callable[[str, str], Awaitable[None]]
"""上报失败的函数签名：``(conn_id, reason) -> None``。"""


@dataclass
class ForwardResult:
    """一次转发的结局。"""

    conn_id: str
    local_port: int
    ok: bool
    reason: str = ""
    upload: int = 0
    download: int = 0
    duration: float = 0.0

    def to_dict(self) -> dict:
        return {
            "conn_id": self.conn_id,
            "local_port": self.local_port,
            "ok": self.ok,
            "reason": self.reason,
            "upload": self.upload,
            "download": self.download,
            "duration": round(self.duration, 4),
        }


class DataChannelForwarder:
    """一次请求的转发执行器。

    每个 ``conn_id`` 用一个实例，实例内不保存跨请求状态——
    因此将来要做连接池或并发限流，改这里不会波及控制通道。
    """

    def __init__(
        self,
        *,
        server_host: str,
        data_port: int,
        local_host: str,
        connect_timeout: float,
        logger: Optional[logging.Logger] = None,
        events: Optional[EventBus] = None,
    ) -> None:
        self._server_host = server_host
        self._data_port = data_port
        self._local_host = local_host
        self._connect_timeout = connect_timeout
        self._log = logger or get_logger("client.forwarder")
        self._events = events or EventBus(self._log)

    async def run(
        self,
        conn_id: str,
        local_port: int,
        *,
        report_error: ReportErrorFn,
    ) -> ForwardResult:
        started = time.monotonic()
        backend_writer: Optional[asyncio.StreamWriter] = None
        data_writer: Optional[asyncio.StreamWriter] = None

        try:
            # ① 先连内网后端
            try:
                backend_reader, backend_writer = await asyncio.wait_for(
                    asyncio.open_connection(self._local_host, local_port),
                    timeout=self._connect_timeout,
                )
            except (OSError, TimeoutError) as exc:
                reason = f"无法连接内网后端 {self._local_host}:{local_port}（{exc}）"
                self._log.warning("conn=%s %s", conn_id, reason)
                await report_error(conn_id, reason)
                return self._fail(conn_id, local_port, reason, started)

            # ② 再开数据通道并注册
            try:
                data_reader, data_writer = await asyncio.wait_for(
                    asyncio.open_connection(self._server_host, self._data_port),
                    timeout=self._connect_timeout,
                )
            except (OSError, TimeoutError) as exc:
                reason = f"无法建立数据通道 {self._server_host}:{self._data_port}（{exc}）"
                self._log.warning("conn=%s %s", conn_id, reason)
                await report_error(conn_id, reason)
                return self._fail(conn_id, local_port, reason, started)

            await send_msg(data_writer, make_msg(MsgType.REGISTER, conn_id=conn_id))

            # ③ 注册之后数据通道就是纯字节流了，交给 pipe_both 搬运
            stats = await pipe_both(
                backend_reader,
                backend_writer,
                data_reader,
                data_writer,
                label=f"conn={conn_id}",
                logger=self._log,
            )
            result = ForwardResult(
                conn_id=conn_id,
                local_port=local_port,
                ok=True,
                upload=stats.upload,
                download=stats.download,
                duration=time.monotonic() - started,
            )
            self._log.debug("conn=%s 转发完成：%s", conn_id, result.to_dict())
            return result

        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 单次转发失败不能影响客户端主循环
            reason = f"转发过程中出现未预期异常：{exc!r}"
            self._log.exception("conn=%s %s", conn_id, reason)
            await report_error(conn_id, reason)
            return self._fail(conn_id, local_port, reason, started)

        finally:
            await close_writer(data_writer)
            await close_writer(backend_writer)

    @staticmethod
    def _fail(conn_id: str, local_port: int, reason: str, started: float) -> ForwardResult:
        return ForwardResult(
            conn_id=conn_id,
            local_port=local_port,
            ok=False,
            reason=reason,
            duration=time.monotonic() - started,
        )
