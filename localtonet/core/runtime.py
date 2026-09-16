# -*- coding: utf-8 -*-
"""
localtonet.core.runtime —— 运行期小工具
========================================
* ``spawn``：创建后台任务并**保证异常不会静默丢失**。
  裸的 ``asyncio.create_task`` 如果任务内部抛异常而没人 await，只会得到一条
  "Task exception was never retrieved" 警告，排查时非常痛苦。
* ``cancel_all``：批量取消并等待，用于关停时收拾干净。
* ``peer_name``：把 ``peername`` 格式化成日志友好的 ``ip:port``。
* ``peer_host``：只取对端 **IP**，给"按访客 IP 分桶"这类用例用。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Optional, Set, Tuple

from logging_setup import get_logger

__all__ = ["spawn", "cancel_all", "peer_name", "peer_host"]

T = asyncio.Task


def spawn(
    coro: Awaitable[object],
    *,
    name: Optional[str] = None,
    logger: Optional[logging.Logger] = None,
    track: Optional[Set[asyncio.Task]] = None,
) -> asyncio.Task:
    """把协程挂成后台任务，异常自动落日志。

    传 ``track`` 时任务会被登记进该集合，结束时自动移除——
    这让组件可以在关停时精确取消自己创建的所有任务，不必依赖全局状态。
    """
    log = logger or get_logger("runtime")
    task = asyncio.create_task(coro, name=name)  # type: ignore[arg-type]
    if track is not None:
        track.add(task)

    def _on_done(finished: asyncio.Task) -> None:
        if track is not None:
            track.discard(finished)
        if finished.cancelled():
            return
        exc = finished.exception()
        if exc is not None:
            log.error("后台任务 %s 异常退出：%r", finished.get_name(), exc, exc_info=exc)

    task.add_done_callback(_on_done)
    return task


async def cancel_all(tasks: Set[asyncio.Task], *, logger: Optional[logging.Logger] = None) -> None:
    """取消一组任务并等待它们真正结束。"""
    if not tasks:
        return
    log = logger or get_logger("runtime")
    targets = list(tasks)
    for task in targets:
        task.cancel()
    results = await asyncio.gather(*targets, return_exceptions=True)
    for task, result in zip(targets, results):
        if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
            log.debug("取消任务 %s 时收到 %r", task.get_name(), result)
    tasks.clear()


def peer_name(writer: asyncio.StreamWriter) -> str:
    """取对端 ``ip:port``；拿不到时返回 ``unknown``。"""
    info: object = writer.get_extra_info("peername")
    if isinstance(info, tuple) and len(info) >= 2:
        host, port = info[0], info[1]
        return f"{host}:{port}"
    if info:
        return str(info)
    return "unknown"


def peer_host(writer: asyncio.StreamWriter) -> str:
    """取对端 **IP**（不含端口）；拿不到时返回 ``unknown``。

    为什么单独给一个"不带端口"的函数：按访客 IP 分桶时限速 key 里**必须**只有 IP。
    用 ``peer_name`` 的结果去分桶，等于每条访客连接都带一个不同的临时端口，
    桶会变成"一条连接一个"——限速看着生效，实则只限住了单条连接。

    TLS 下这里拿到的**仍然是 TCP 对端**（访客侧 TLS 在服务端终止，不引入代理层级），
    所以有无 TLS 的分桶口径一致。
    """
    info: object = writer.get_extra_info("peername")
    if isinstance(info, tuple) and len(info) >= 1:
        return str(info[0])
    if info:
        return str(info)
    return "unknown"


def local_addr(writer: asyncio.StreamWriter) -> Tuple[str, int]:
    """取本端被连接的 ``(host, port)``，用于识别请求落在哪个访客端口上。"""
    info: object = writer.get_extra_info("sockname")
    if isinstance(info, tuple) and len(info) >= 2:
        return str(info[0]), int(info[1])
    return "", 0
