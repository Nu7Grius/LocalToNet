# -*- coding: utf-8 -*-
"""
localtonet.core.limiter —— 令牌桶限速
======================================
带宽限速的落点只有一个：:func:`localtonet.core.pipe.pipe_both` 内层写入循环。
本模块提供那个循环需要的桶，以及"按客户端汇总"的一层封装。

**为什么用令牌桶，而不是"每 N 字节 sleep 一次"？**
固定间隔只能约束平均速率，遇到突发会把每条连接都拖慢；令牌桶允许短时突发（桶容量），
长期平均速率才被钳住——这正是"限住带宽、但不把交互体验一并掐死"需要的形状。

两个容易写错的地方，这里都处理了：

1. **不许忙等**。令牌不足时 ``await asyncio.sleep(缺口 / rate)``，让出控制权。
   写成 ``while tokens < n: pass`` 会把整个事件循环钉死。
2. **单次请求量超过桶容量不能死等**。桶容量取 ``max(rate * burst_seconds, MIN_BURST)``，
   而一次搬运的 chunk 是 64KB；如果 rate 很小、容量不足 64KB，
   "等满 64KB 再取"会永远等不满。所以 ``acquire`` 按容量**拆段**处理。

**为什么"每客户端汇总"而不是"每连接一个桶"？**
限速要防的是"开一堆并发连接绕开单连接限额"，每连接一个桶在那种场景下形同虚设。
代价是同一客户端的多条连接共享一份配额、先到先得，不保证公平——对"防打满"这个目标够用。

``pipe`` 那边刻意不认识"客户端"这类业务概念：它只把调用方给的 ``key`` 原样转交，
怎么按 key 汇总由本模块决定（见 :class:`ClientRateLimiter` 与 ``pipe.RateLimitHook``）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, Dict, List, Optional

from localtonet.core.pipe import DOWNLOAD_DIRECTION, UPLOAD_DIRECTION
from logging_setup import get_logger

__all__ = ["MIN_BURST", "DEFAULT_BURST_SECONDS", "TokenBucket", "ClientRateLimiter"]

MIN_BURST = 16 * 1024
"""桶容量下限（字节）。给"只给速率"的默认路径兜底，避免低速时容量小到要拆成几百段。"""

DEFAULT_BURST_SECONDS = 0.25
"""桶容量按 ``rate * 该系数`` 取，等于"允许突发这么多秒的流量"。"""

Clock = Callable[[], float]
Sleeper = Callable[[float], Awaitable[None]]


class TokenBucket:
    """异步令牌桶，单位是字节。

    ``clock`` / ``sleep`` 可注入，单测便能用假时钟精确断言等待时长，不靠挂钟。

    ``burst`` 与 ``burst_seconds`` 是两种指定容量的方式：
    前者是**精确值**（完全按调用方给的来，单测靠它可控），
    后者是**相对速率的余量系数**，带 ``MIN_BURST`` 下限（生产默认路径）。
    """

    def __init__(
        self,
        rate: float,
        *,
        burst: Optional[float] = None,
        burst_seconds: float = DEFAULT_BURST_SECONDS,
        clock: Clock = time.monotonic,
        sleep: Sleeper = asyncio.sleep,
    ) -> None:
        if rate <= 0:
            raise ValueError("rate 必须为正数；不限速请不要创建令牌桶")
        self._rate = float(rate)
        if burst is not None:
            capacity = float(burst)
        else:
            capacity = max(self._rate * burst_seconds, float(MIN_BURST))
        # 容量至少 1 字节：容量 < 1 时单次取用永远等不满，会变成死循环
        self._capacity = max(capacity, 1.0)
        self._tokens = self._capacity
        self._clock = clock
        self._sleep = sleep
        self._updated = clock()

    # ------------------------------------------------------------------ #

    @property
    def rate(self) -> float:
        return self._rate

    @property
    def capacity(self) -> float:
        return self._capacity

    @property
    def tokens(self) -> float:
        """当前可用令牌数（读取会先按流逝时间补充，不产生副作用）。"""
        self._refill()
        return self._tokens

    @property
    def wait_ratio(self) -> float:
        """桶里现存令牌占比，调试用。"""
        return self._tokens / self._capacity

    # ------------------------------------------------------------------ #

    async def acquire(self, amount: int) -> float:
        """取走 ``amount`` 个令牌，返回**按速率换算出的等待时长**（秒）。

        返回理论值而不是实测墙钟：它就是 sleep 进去的那个数，便于统计与断言；
        真实耗时会略大（事件循环调度开销），不应作为断言依据。
        """
        if amount <= 0:
            return 0.0
        waited = 0.0
        remaining = int(amount)
        step = max(1, int(self._capacity))
        while remaining > 0:
            take = min(remaining, step)
            waited += await self._take(take)
            remaining -= take
        return waited

    async def _take(self, amount: float) -> float:
        """取走不超过桶容量的一份令牌。"""
        waited = 0.0
        while True:
            self._refill()
            if self._tokens >= amount:
                self._tokens -= amount
                return waited
            delay = (amount - self._tokens) / self._rate
            await self._sleep(delay)
            waited += delay

    def _refill(self) -> None:
        """按流逝时间补充令牌，最多补到桶容量。临界区内不含 await。"""
        now = self._clock()
        elapsed = now - self._updated
        if elapsed <= 0:
            return
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        self._updated = now

    def __repr__(self) -> str:
        return f"TokenBucket(rate={self._rate:g}/s, capacity={self._capacity:g}, tokens={self._tokens:.0f})"


class ClientRateLimiter:
    """按客户端汇总的上下行限速器。

    钩子接口与 :class:`localtonet.core.pipe.RateLimitHook` 一致：
    ``await limiter.wait(key, direction, amount)``。

    * ``key`` —— 汇总单位，服务端传 ``client_id``。
    * ``direction`` —— ``"a->b"``（上行）或 ``"b->a"``（下行），沿用 ``pipe`` 的 tag 空间。
    * 返回本次等待秒数，0 表示没等。

    ``upload_bps`` / ``download_bps`` 为 ``0`` 表示该方向不限速；两个都是 0 时
    :attr:`enabled` 为 False，调用方应当干脆不要传这个限速器，让 ``pipe`` 走原路径。
    """

    def __init__(
        self,
        *,
        upload_bps: float = 0,
        download_bps: float = 0,
        burst_seconds: float = DEFAULT_BURST_SECONDS,
        logger: Optional[logging.Logger] = None,
        bucket_factory: Callable[[float], TokenBucket] = TokenBucket,
    ) -> None:
        self._upload_bps = float(upload_bps)
        self._download_bps = float(download_bps)
        self._burst_seconds = burst_seconds
        self._log = logger or get_logger("limiter")
        self._factory = bucket_factory
        self._buckets: Dict[str, Dict[str, TokenBucket]] = {}

    # ------------------------------------------------------------------ #

    @property
    def enabled(self) -> bool:
        return self._upload_bps > 0 or self._download_bps > 0

    @property
    def upload_bps(self) -> float:
        return self._upload_bps

    @property
    def download_bps(self) -> float:
        return self._download_bps

    def active_keys(self) -> List[str]:
        """当前已分配过桶的 key（客户端下线时应当 :meth:`forget` 掉）。"""
        return list(self._buckets)

    # ------------------------------------------------------------------ #

    async def wait(self, key: str, direction: str, amount: int) -> float:
        """在写入 ``amount`` 字节前取配额。不限速的方向直接返回 0。"""
        if amount <= 0:
            return 0.0
        bucket = self._bucket_for(key, direction)
        if bucket is None:
            return 0.0
        return await bucket.acquire(amount)

    def forget(self, key: str) -> None:
        """释放某个 key 的桶。客户端下线时必须调，否则长期运行会慢慢泄漏。"""
        if self._buckets.pop(key, None) is not None:
            self._log.debug("已释放 %s 的限速桶", key)

    def clear(self) -> None:
        self._buckets.clear()

    # ------------------------------------------------------------------ #

    def _bucket_for(self, key: str, direction: str) -> Optional[TokenBucket]:
        """懒创建桶。临界区内不含 await，符合单事件循环无锁约定。"""
        if direction == UPLOAD_DIRECTION:
            rate = self._upload_bps
        elif direction == DOWNLOAD_DIRECTION:
            rate = self._download_bps
        else:
            self._log.debug("未知方向 %r，不限速", direction)
            return None
        if rate <= 0:
            return None

        buckets = self._buckets.get(key)
        if buckets is None:
            buckets = {}
            self._buckets[key] = buckets
        bucket = buckets.get(direction)
        if bucket is None:
            bucket = self._factory(rate)
            buckets[direction] = bucket
        return bucket

    def __repr__(self) -> str:
        return (
            f"ClientRateLimiter(upload={self._upload_bps:g}/s, download={self._download_bps:g}/s, "
            f"keys={len(self._buckets)})"
        )
