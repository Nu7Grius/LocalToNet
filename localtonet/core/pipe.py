# -*- coding: utf-8 -*-
"""
localtonet.core.pipe —— 双向字节搬运
======================================
一次转发需要两个方向同时搬：访客→后端、后端→访客。
两个方向各占一个协程，用 ``asyncio.wait(FIRST_COMPLETED)`` 等：

* 任意一个方向读到 EOF 或抛异常 → 取消另一个方向；
* 结束前对两个写端做**半关闭**（``write_eof``），让对端能及时读到 EOF 而不是干等到超时；
* 返回搬运字节数，供观测与将来的带宽限流使用。

**扩展点**：要加带宽限流，只需在 ``pump`` 内写入前挂一个令牌桶，
不必改动调用方；要加传输加密，在建立连接处包一层即可，本函数不感知。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

from logging_setup import get_logger

__all__ = ["CHUNK_SIZE", "PipeStats", "pipe_both", "close_writer", "close_write_side"]

CHUNK_SIZE = 64 * 1024
"""单次搬运的字节数。过小会放大系统调用开销，过大则增加内存占用与首字节延迟。"""


@dataclass
class PipeStats:
    """一次双向搬运的统计结果。"""

    upload: int = 0
    """访客 → 数据通道 → 内网后端 的字节数。"""
    download: int = 0
    """内网后端 → 数据通道 → 访客 的字节数。"""
    stopped_by: str = ""

    @property
    def total(self) -> int:
        return self.upload + self.download

    def to_dict(self) -> dict:
        return {
            "upload": self.upload,
            "download": self.download,
            "total": self.total,
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
) -> PipeStats:
    """在 (a_reader/a_writer) 与 (b_reader/b_writer) 之间做双向搬运。

    ``a`` 是访客侧，``b`` 是数据通道侧。**配对关系是交叉的**：

    * ``a_reader`` → ``b_writer``（上行：访客请求送进隧道）
    * ``b_reader`` → ``a_writer``（下行：隧道带回访客响应）

    写成 ``a_reader → a_writer`` 就成了原地回环——数据读出来又写回自己，
    表现为"看着有流量但对方永远收不到"，是个很有迷惑性的坑。
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
        asyncio.create_task(pump(a_reader, b_writer, "a->b", "upload")),
        asyncio.create_task(pump(b_reader, a_writer, "b->a", "download")),
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
