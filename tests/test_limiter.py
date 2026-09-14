# -*- coding: utf-8 -*-
"""
tests/test_limiter.py —— 令牌桶与限速器单测
=============================================
全部用**假时钟**驱动：``clock`` 与 ``sleep`` 一起注入，``sleep`` 推进假时钟。
这样"该等多久"是可以精确断言的，不依赖真实挂钟，也不会让测试变慢。

重点守三条：

1. 令牌不足时必须 ``await``（不忙等）——假 sleep 会记录每一次等待并设上限，
   真出现忙等循环会撞上限直接失败，而不是把测试挂死。
2. 单次请求量超过桶容量必须能完成（拆段），不能死等。
3. 不限速的方向/客户端零开销：不创建桶，返回 0。
"""

from __future__ import annotations

import asyncio
from typing import List

import pytest

from localtonet.core.limiter import MIN_BURST, ClientRateLimiter, TokenBucket
from localtonet.core.pipe import DOWNLOAD_DIRECTION, UPLOAD_DIRECTION


class FakeClock:
    """可注入的假时钟：``sleep`` 把时间往前推，顺带记录每次等待。"""

    MAX_SLEEPS = 500

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: List[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        if delay <= 0:
            raise AssertionError(f"sleep({delay}) 不是有效等待，疑似忙等")
        if len(self.sleeps) >= self.MAX_SLEEPS:
            raise AssertionError(f"等待次数超过 {self.MAX_SLEEPS}，疑似死循环")
        self.sleeps.append(delay)
        self.now += delay


def make_bucket(rate: float, **kwargs) -> tuple[TokenBucket, FakeClock]:
    clock = FakeClock()
    bucket = TokenBucket(rate, clock=clock, sleep=clock.sleep, **kwargs)
    return bucket, clock


def make_limiter(*, upload_bps: float = 0, download_bps: float = 0, burst: float = 1000.0):
    """造一个限速器，所有桶都按**精确容量**创建。

    生产默认路径的容量带 ``MIN_BURST`` 下限（16KB），用它做断言会看不见"桶用光了"这件事；
    这里显式给容量，断言就能精确到具体秒数。
    """
    clock = FakeClock()

    def factory(rate: float) -> TokenBucket:
        return TokenBucket(rate, burst=burst, clock=clock, sleep=clock.sleep)

    limiter = ClientRateLimiter(
        upload_bps=upload_bps, download_bps=download_bps, bucket_factory=factory
    )
    return limiter, clock


# --------------------------------------------------------------------------- #
# TokenBucket
# --------------------------------------------------------------------------- #


def test_bucket_starts_full_and_acquire_within_capacity_does_not_wait() -> None:
    async def scenario() -> None:
        bucket, clock = make_bucket(1000, burst=1000)
        assert bucket.capacity == 1000

        waited = await bucket.acquire(1000)

        assert waited == 0.0
        assert clock.sleeps == []
        assert bucket.tokens == pytest.approx(0.0)

    asyncio.run(scenario())


def test_acquire_waits_exactly_the_deficit_over_rate() -> None:
    """桶空了之后取 500 字节、速率 1000 B/s → 必须等 0.5s。"""

    async def scenario() -> None:
        bucket, clock = make_bucket(1000, burst=1000)
        await bucket.acquire(1000)

        waited = await bucket.acquire(500)

        assert waited == pytest.approx(0.5)
        assert clock.sleeps == [pytest.approx(0.5)]

    asyncio.run(scenario())


def test_amount_larger_than_capacity_is_split_and_completes() -> None:
    """一次取 2500 而桶容量只有 1000：拆成 1000+1000+500，总计等 1.5s，绝不能死等。"""

    async def scenario() -> None:
        bucket, clock = make_bucket(1000, burst=1000)

        waited = await bucket.acquire(2500)

        assert waited == pytest.approx(1.5)
        assert len(clock.sleeps) == 2  # 首段用满桶，余下两段各等 1 段
        assert bucket.tokens == pytest.approx(0.0)

    asyncio.run(scenario())


def test_acquire_non_positive_amount_is_free() -> None:
    async def scenario() -> None:
        bucket, clock = make_bucket(1000, burst=1000)
        assert await bucket.acquire(0) == 0.0
        assert await bucket.acquire(-5) == 0.0
        assert clock.sleeps == []

    asyncio.run(scenario())


def test_default_capacity_has_a_floor() -> None:
    """只给速率时容量有下限，避免低速场景把一次搬运拆成几百段。"""

    async def scenario() -> None:
        bucket, _ = make_bucket(64)  # 64 B/s * 0.25s = 16B，被下限抬到 MIN_BURST
        assert bucket.capacity == float(MIN_BURST)

    asyncio.run(scenario())


def test_non_positive_rate_is_rejected() -> None:
    with pytest.raises(ValueError, match="rate 必须为正数"):
        TokenBucket(0)


def test_tiny_rate_still_completes_without_deadlock() -> None:
    """速率低到容量不足 1 字节时也必须能完成——容量会被抬到 1。"""

    async def scenario() -> None:
        bucket, _ = make_bucket(0.5, burst=0.1)
        waited = await bucket.acquire(2)
        # 容量 1 字节，首字节预置在桶里，第二字节要等 1/0.5 = 2s
        assert waited == pytest.approx(2.0)

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# ClientRateLimiter
# --------------------------------------------------------------------------- #


def test_disabled_limiter_never_creates_buckets() -> None:
    async def scenario() -> None:
        limiter = ClientRateLimiter(upload_bps=0, download_bps=0)
        assert limiter.enabled is False

        assert await limiter.wait("client-a", UPLOAD_DIRECTION, 64 * 1024) == 0.0
        assert await limiter.wait("client-a", DOWNLOAD_DIRECTION, 64 * 1024) == 0.0
        assert limiter.active_keys() == []

    asyncio.run(scenario())


def test_limiter_is_per_client_not_global() -> None:
    """一个客户端用光了配额，不该影响另一个客户端——限速单位是客户端。"""

    async def scenario() -> None:
        limiter, _ = make_limiter(upload_bps=1000, burst=1000)
        await limiter.wait("client-a", UPLOAD_DIRECTION, 1000)

        assert await limiter.wait("client-a", UPLOAD_DIRECTION, 500) == pytest.approx(0.5)
        assert await limiter.wait("client-b", UPLOAD_DIRECTION, 500) == 0.0

    asyncio.run(scenario())


def test_upload_and_download_use_separate_buckets() -> None:
    """上下行分开计费：下载打满不该掐死同客户端的请求上传。"""

    async def scenario() -> None:
        limiter, _ = make_limiter(upload_bps=1000, download_bps=4000, burst=4000)
        await limiter.wait("client-a", DOWNLOAD_DIRECTION, 4000)

        assert await limiter.wait("client-a", DOWNLOAD_DIRECTION, 1000) == pytest.approx(0.25)
        assert await limiter.wait("client-a", UPLOAD_DIRECTION, 1000) == 0.0

    asyncio.run(scenario())


def test_unknown_direction_is_unlimited() -> None:
    async def scenario() -> None:
        limiter = ClientRateLimiter(upload_bps=1000)
        assert await limiter.wait("client-a", "sideways", 10_000) == 0.0

    asyncio.run(scenario())


def test_forget_releases_the_bucket() -> None:
    """客户端下线要能释放桶，否则长期运行会随客户端增删慢慢泄漏。"""

    async def scenario() -> None:
        limiter, _ = make_limiter(upload_bps=1000, burst=1000)
        await limiter.wait("client-a", UPLOAD_DIRECTION, 1000)
        assert limiter.active_keys() == ["client-a"]

        limiter.forget("client-a")

        assert limiter.active_keys() == []
        # 释放后重新开始，配额是满的
        assert await limiter.wait("client-a", UPLOAD_DIRECTION, 1000) == 0.0

    asyncio.run(scenario())
