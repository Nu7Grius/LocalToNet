# -*- coding: utf-8 -*-
"""
localtonet.server.pending —— 配对挂起
======================================
时序上的固有矛盾：**访客先到，数据通道后到**。

访客连上 9028 时，服务端手上只有一条来自浏览器的 TCP 连接，
却没有可以和它对接的另一端——客户端此刻还没建立数据通道。
所以服务端先把访客连接"寄存"起来，并通过控制通道通知客户端开通道：

    访客协程                                    数据通道协程
      │ 创建 PendingConn                          │
      │ 发 new_conn ───────────────────────────────► 客户端连本机后端
      │                                            │ 连 7001 并 register(conn_id)
      │◄──── attach() 把 data_* 赋值，ready 置位 ────┤
      │ pipe_both(访客, 数据)  ──── 字节搬运 ────►   │
      │     │                                       │ await done
      │     └── finish()：done 置位 ────────────────►│ 退出
      └─ 超时未配对 / 客户端上报 conn_error ──► ready 置 False，访客收到 502

两个 future 分工明确：
* ``ready``  —— 配对**是否成功**（True 成功 / False 失败），负责让访客协程决定要不要开始搬运。
* ``done``   —— 搬运**是否结束**，负责让数据通道协程知道可以退出了。

``wait_ready`` 刻意用 ``asyncio.wait`` 而不是 ``asyncio.wait_for``：
``wait_for`` 超时会**取消**它等待的 future，而 ``ready`` 是共享对象，
被取消后迟到的 ``attach`` 就没法再置位了。用 ``wait`` 只是"等不到就走开"，不改动对象状态。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from logging_setup import get_logger

__all__ = ["PendingConn", "PendingTable"]


@dataclass
class PendingConn:
    """一次等待配对的访客转发会话。"""

    conn_id: str
    public_port: int
    local_port: int
    client_id: str
    visitor_reader: asyncio.StreamReader = field(repr=False)
    visitor_writer: asyncio.StreamWriter = field(repr=False)
    created_at: float = field(default_factory=time.monotonic)

    data_reader: Optional[asyncio.StreamReader] = field(default=None, repr=False)
    data_writer: Optional[asyncio.StreamWriter] = field(default=None, repr=False)
    error: str = ""

    _ready: asyncio.Future = field(init=False, repr=False, default=None)  # type: ignore[assignment]
    _done: asyncio.Future = field(init=False, repr=False, default=None)  # type: ignore[assignment]
    _closed: bool = field(init=False, default=False)

    def __post_init__(self) -> None:
        loop = asyncio.get_running_loop()
        self._ready = loop.create_future()
        self._done = loop.create_future()

    # ------------------------------------------------------------------ #
    # 状态查询
    # ------------------------------------------------------------------ #

    @property
    def ready(self) -> asyncio.Future:
        return self._ready

    @property
    def done(self) -> asyncio.Future:
        return self._done

    @property
    def paired(self) -> bool:
        return self.data_writer is not None

    @property
    def closed(self) -> bool:
        return self._closed

    def elapsed(self) -> float:
        return time.monotonic() - self.created_at

    # ------------------------------------------------------------------ #
    # 状态迁移
    # ------------------------------------------------------------------ #

    def attach(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> bool:
        """数据通道注册上来时调用。配对成功返回 True。

        以下情况一律拒绝：已经配对过（重复注册）、已被判定失败、已经收尾。
        """
        if self._closed or self._ready.done():
            return False
        self.data_reader = reader
        self.data_writer = writer
        self._ready.set_result(True)
        return True

    def fail(self, reason: str) -> None:
        """判定配对失败，唤醒等待中的访客协程（它会回 502）。"""
        self.error = reason
        self._closed = True
        if not self._ready.done():
            self._ready.set_result(False)

    def finish(self) -> None:
        """搬运结束，通知数据通道协程退出。"""
        if not self._done.done():
            self._done.set_result(True)

    async def wait_ready(self, timeout: float) -> bool:
        """等待配对结果。

        超时返回 False，且**不会取消** ``ready``——
        这样迟到的数据通道仍然会被 ``attach`` 正确拒绝（而不是撞上 InvalidStateError）。
        """
        done, _ = await asyncio.wait({self._ready}, timeout=timeout)
        if not done:
            return False
        return self._ready.result() is True

    async def wait_done(self) -> None:
        await self._done

    def __repr__(self) -> str:
        return (
            f"PendingConn(conn_id={self.conn_id!r}, {self.public_port}->{self.local_port}, "
            f"client={self.client_id!r}, paired={self.paired})"
        )


class PendingTable:
    """``conn_id -> PendingConn`` 的登记表。"""

    def __init__(self, *, logger: Optional[logging.Logger] = None) -> None:
        self._items: Dict[str, PendingConn] = {}
        self._log = logger or get_logger("server.pending")

    def create(self, pending: PendingConn) -> PendingConn:
        self._items[pending.conn_id] = pending
        return pending

    def get(self, conn_id: str) -> Optional[PendingConn]:
        return self._items.get(conn_id)

    def attach(
        self,
        conn_id: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> Optional[PendingConn]:
        """数据通道注册：找到挂起对象并配对。找不到或已被配对/已失败则返回 None。"""
        pending = self._items.get(conn_id)
        if pending is None:
            self._log.warning("数据通道注册了未知的 conn_id=%s，可能是访客已超时放弃", conn_id)
            return None
        if not pending.attach(reader, writer):
            self._log.warning("conn_id=%s 已配对或已放弃，拒绝重复注册", conn_id)
            return None
        return pending

    def fail(self, conn_id: str, reason: str) -> Optional[PendingConn]:
        pending = self._items.get(conn_id)
        if pending is None:
            return None
        pending.fail(reason)
        return pending

    def fail_all_for_client(self, client_id: str, reason: str) -> List[PendingConn]:
        """客户端掉线时，把它名下所有还在等的访客请求立刻判失败。

        不做这一步的话，这些访客要干等满 ``pair_timeout`` 才收到 502。
        """
        affected = [item for item in self._items.values() if item.client_id == client_id and not item.closed]
        for pending in affected:
            pending.fail(reason)
        if affected:
            self._log.info("客户端 %s 离线，%d 个等待中的请求提前判定失败", client_id, len(affected))
        return affected

    def discard(self, conn_id: str) -> Optional[PendingConn]:
        return self._items.pop(conn_id, None)

    def count_for_client(self, client_id: str) -> int:
        """该客户端当前进行中的转发数（含尚未配对的）。

        配额判定要用它。方法体内部不含 ``await``，与 registry / mapping 一样
        靠单事件循环的无抢占保证原子性。
        """
        return sum(1 for item in self._items.values() if item.client_id == client_id)

    def drain(self) -> List[PendingConn]:
        """取走全部挂起对象（关停时用）。"""
        items = list(self._items.values())
        self._items.clear()
        return items

    def conn_ids(self) -> List[str]:
        return list(self._items.keys())

    def __len__(self) -> int:
        return len(self._items)

    def __repr__(self) -> str:
        return f"PendingTable(pending={len(self._items)})"
