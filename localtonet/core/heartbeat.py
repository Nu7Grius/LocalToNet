# -*- coding: utf-8 -*-
"""
localtonet.core.heartbeat —— 心跳与看门狗
==========================================
两个方向的保活机制，互为保险：

``HeartbeatTask``（客户端主动探测）
    每 ``interval`` 秒发一条 ping，然后在 ``pong_timeout`` 秒内等 pong。
    等不到就判定控制连接已经废掉——**不等 TCP 自己超时**，
    因为"网线被拔"这种半开连接靠内核可能要几分钟才报错。

``Watchdog``（服务端被动巡检）
    每 ``interval`` 秒扫一遍注册表，把 ``last_seen`` 过旧的对象交给回调清理。
    防止客户端进程被强杀后，服务端一直留着一具"僵尸连接"占着端口归属。

关键约束：``HeartbeatTask`` **只发不读**。同一连接上只允许一个协程读 socket，
pong 由主读循环收到后调 ``note_pong()`` 通知它，避免两个协程争抢同一个 reader。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, List, Optional, TypeVar

from logging_setup import get_logger

__all__ = ["HeartbeatTask", "Watchdog"]

T = TypeVar("T")


class HeartbeatTask:
    """客户端心跳任务。"""

    def __init__(
        self,
        *,
        interval: float,
        pong_timeout: float,
        send_ping: Callable[[], Awaitable[None]],
        name: str = "heartbeat",
        logger: Optional[logging.Logger] = None,
    ) -> None:
        if pong_timeout >= interval:
            raise ValueError("pong_timeout 必须小于 interval，否则心跳永远等不到超时")
        self._interval = interval
        self._pong_timeout = pong_timeout
        self._send_ping = send_ping
        self._name = name
        self._log = logger or get_logger("heartbeat")

        self._pong_event = asyncio.Event()
        self._stop_event = asyncio.Event()
        self._pings = 0
        self._pongs = 0
        self._timeouts = 0

    @property
    def stats(self) -> dict:
        return {
            "pings": self._pings,
            "pongs": self._pongs,
            "timeouts": self._timeouts,
            "interval": self._interval,
            "pong_timeout": self._pong_timeout,
        }

    def note_pong(self) -> None:
        """主读循环收到 pong 时调用。"""
        self._pongs += 1
        self._pong_event.set()

    def stop(self) -> None:
        """请求心跳协程退出。"""
        self._stop_event.set()

    async def run(self, *, on_lost: Callable[[str], None]) -> None:
        """心跳主循环。判定连接失效时调一次 ``on_lost(原因)`` 然后退出。"""
        self._log.debug("%s 启动：每 %.1fs 发 ping，%.1fs 内等 pong", self._name, self._interval, self._pong_timeout)
        try:
            while not self._stop_event.is_set():
                if await self._sleep_until_next_round():
                    return

                self._pong_event.clear()
                self._pings += 1
                try:
                    await self._send_ping()
                except (OSError, ConnectionError) as exc:
                    on_lost(f"发送 ping 失败：{exc}")
                    return

                try:
                    await asyncio.wait_for(self._pong_event.wait(), timeout=self._pong_timeout)
                except TimeoutError:
                    self._timeouts += 1
                    on_lost(f"{self._pong_timeout:.0f}s 内未收到 pong，判定控制连接已失效")
                    return
        finally:
            self._log.debug("%s 退出，累计 ping=%d pong=%d 超时=%d", self._name, self._pings, self._pongs, self._timeouts)

    async def _sleep_until_next_round(self) -> bool:
        """睡到下一轮；期间若被 stop 则返回 True 表示该退出了。"""
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=self._interval)
        except TimeoutError:
            return False
        return True


class Watchdog:
    """周期性巡检，把过期对象交给回调清理。"""

    def __init__(
        self,
        *,
        interval: float,
        collect: Callable[[], List[T]],
        expire: Callable[[T], Awaitable[None]],
        name: str = "watchdog",
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._interval = interval
        self._collect = collect
        self._expire = expire
        self._name = name
        self._log = logger or get_logger("watchdog")
        self._stop_event = asyncio.Event()
        self.reaped = 0

    def stop(self) -> None:
        self._stop_event.set()

    async def run(self) -> None:
        self._log.debug("%s 启动：每 %.1fs 巡检一次", self._name, self._interval)
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._interval)
                return
            except TimeoutError:
                pass

            try:
                expired = self._collect()
            except Exception:  # noqa: BLE001 - 巡检不该因为一次收集失败整体停摆
                self._log.exception("%s 收集过期对象失败", self._name)
                continue

            for item in expired:
                try:
                    await self._expire(item)
                    self.reaped += 1
                except Exception:  # noqa: BLE001 - 单个对象清理失败不影响其余
                    self._log.exception("%s 清理 %r 失败", self._name, item)

        self._log.debug("%s 退出，累计清理 %d 个对象", self._name, self.reaped)
