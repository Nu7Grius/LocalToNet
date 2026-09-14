# -*- coding: utf-8 -*-
"""
localtonet.core.pipe —— 双向字节搬运
======================================
一次转发需要两个方向同时搬：访客→后端、后端→访客。
两个方向各占一个协程，用 ``asyncio.wait(FIRST_COMPLETED)`` 等：

* 任意一个方向读到 EOF 或抛异常 → 取消另一个方向；
* 结束前对两个写端做**半关闭**（``write_eof``），让对端能及时读到 EOF 而不是干等到超时；
* 返回搬运字节数，供观测与将来的带宽限流使用。

**扩展点**：带宽限流的钩子已经挂好（``rate_limit`` 参数），实现见
:mod:`localtonet.core.limiter`；要加传输加密，在建立连接处包一层即可，本函数不感知。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional, Protocol

from logging_setup import get_logger

__all__ = [
    "CHUNK_SIZE",
    "UPLOAD_DIRECTION",
    "DOWNLOAD_DIRECTION",
    "RateLimitHook",
    "PipeStats",
    "pipe_both",
    "close_writer",
    "close_write_side",
]

CHUNK_SIZE = 64 * 1024
"""单次搬运的字节数。过小会放大系统调用开销，过大则增加内存占用与首字节延迟。"""

UPLOAD_DIRECTION = "a->b"
"""上行：访客 → 数据通道 → 内网后端。也是 ``pump`` 的 tag 与统计字段名。"""

DOWNLOAD_DIRECTION = "b->a"
"""下行：内网后端 → 数据通道 → 访客。"""


class RateLimitHook(Protocol):
    """写入前的限速钩子。

    ``pipe`` 刻意不认识"客户端""配额"这类业务概念：它只把调用方给的 ``key``
    与方向原样转交出去，怎么按 key 汇总由钩子自己决定
    （服务端实现见 :class:`localtonet.core.limiter.ClientRateLimiter`）。
    """

    async def wait(self, key: str, direction: str, amount: int) -> float:
        """在写入 ``amount`` 字节前取配额，返回本次等待的秒数（0 表示没等）。"""


@dataclass
class PipeStats:
    """一次双向搬运的统计结果。"""

    upload: int = 0
    """访客 → 数据通道 → 内网后端 的字节数。"""
    download: int = 0
    """内网后端 → 数据通道 → 访客 的字节数。"""
    throttled: float = 0.0
    """因限速累计等待的秒数（按速率换算，不是实测墙钟）。0 表示全程没被限速。"""
    stopped_by: str = ""

    @property
    def total(self) -> int:
        return self.upload + self.download

    def to_dict(self) -> dict:
        return {
            "upload": self.upload,
            "download": self.download,
            "total": self.total,
            "throttled": round(self.throttled, 4),
            "stopped_by": self.stopped_by,
        }


def close_write_side(writer: object) -> None:
    """半关闭写端，向对端发送 EOF。

    SSL 之类的传输层不支持 ``write_eof``，此时静默跳过——
    后面的整体关闭仍能收尾，只是对端可能晚一点感知到 EOF。
    """
    try:
        can_write_eof = getattr(writer, "can_write_eof", None)
        if callable(can_write_eof) and can_write_eof():
            writer.write_eof()  # type: ignore[attr-defined]
    except (OSError, RuntimeError, NotImplementedError):
        pass


async def close_writer(writer: Optional[asyncio.StreamWriter]) -> None:
    """安全关闭一个 StreamWriter（幂等，不向调用方抛异常）。"""
    if writer is None:
        return
    try:
        writer.close()
    except (OSError, RuntimeError):
        return
    try:
        await writer.wait_closed()
    except (OSError, RuntimeError, TimeoutError):
        pass


async def pipe_both(
    a_reader: asyncio.StreamReader,
    a_writer: asyncio.StreamWriter,
    b_reader: asyncio.StreamReader,
    b_writer: asyncio.StreamWriter,
    *,
    label: str = "",
    logger: Optional[logging.Logger] = None,
    rate_limit: Optional[RateLimitHook] = None,
    limit_key: str = "",
) -> PipeStats:
    """在 (a_reader/a_writer) 与 (b_reader/b_writer) 之间做双向搬运。

    ``a`` 是访客侧，``b`` 是数据通道侧。**配对关系是交叉的**：

    * ``a_reader`` → ``b_writer``（上行：访客请求送进隧道）
    * ``b_reader`` → ``a_writer``（下行：隧道带回访客响应）

    写成 ``a_reader → a_writer`` 就成了原地回环——数据读出来又写回自己，
    表现为"看着有流量但对方永远收不到"，是个很有迷惑性的坑。

    ``rate_limit`` 是本函数预留的**带宽限流挂载点**：每个方向在写入前先向它取配额，
    ``limit_key`` 是汇总单位（服务端传 ``client_id``）。传 ``None`` 即完全不过桶、
    走原路径，既有调用方与性能都不受影响。
    """
    log = logger or get_logger("pipe")
    stats = PipeStats()
    finished_first = {"tag": ""}

    async def pump(
        src: asyncio.StreamReader,
        dst: asyncio.StreamWriter,
        tag: str,
        counter_attr: str,
    ) -> None:
        try:
            while True:
                chunk = await src.read(CHUNK_SIZE)
                if not chunk:
                    break
                if rate_limit is not None:
                    # 限速必须在写入**之前**：目的是给 drain 一个节奏，
                    # 而不是让写缓冲先堆积起来再慢慢吐。
                    stats.throttled += await rate_limit.wait(limit_key, tag, len(chunk))
                setattr(stats, counter_attr, getattr(stats, counter_attr) + len(chunk))
                dst.write(chunk)
                await dst.drain()
        except asyncio.CancelledError:
            raise
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError) as exc:
            log.debug("%s 方向 %s 连接中断：%s", label, tag, exc)
        except OSError as exc:
            log.debug("%s 方向 %s 读写异常：%s", label, tag, exc)
        finally:
            if not finished_first["tag"]:
                finished_first["tag"] = tag
            close_write_side(dst)

    tasks = [
        asyncio.create_task(pump(a_reader, b_writer, UPLOAD_DIRECTION, "upload")),
        asyncio.create_task(pump(b_reader, a_writer, DOWNLOAD_DIRECTION, "download")),
    ]

    try:
        _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        stats.stopped_by = "cancelled"
        raise

    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)

    stats.stopped_by = finished_first["tag"] or "unknown"
    log.debug("%s 转发结束（%s），上行 %d 字节 / 下行 %d 字节", label, stats.stopped_by, stats.upload, stats.download)
    return stats
