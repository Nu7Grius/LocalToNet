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

六期再补三条（汇总口径 + key 空间）：

4. **key 对任意 client_id 都能无歧义拆回原主**——client_id 由客户端提供、
   只校验非空，可以含分隔符；朴素拼接会让"a 的端口分片"与"a|p80 的客户端 key"撞桶。
5. **回收按"归属"逐条删，不按前缀删**：前缀删会连带删掉同名开头的别人的桶。
6. 桶表有上限，满了淘汰最久未用的那条（按访客口径时桶数由访客 IP 个数决定）。
"""

from __future__ import annotations

import asyncio
from typing import List

import pytest

from localtonet.core.limiter import (
    DEFAULT_MAX_KEYS,
    RATE_LIMIT_SCOPE_CLIENT,
    RATE_LIMIT_SCOPE_PORT,
    RATE_LIMIT_SCOPE_VISITOR,
    MIN_BURST,
    ClientRateLimiter,
    TokenBucket,
    compose_limit_key,
    describe_limit_key,
    limit_key_owner,
)
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


def make_limiter(
    *,
    upload_bps: float = 0,
    download_bps: float = 0,
    burst: float = 1000.0,
    scope: str = RATE_LIMIT_SCOPE_CLIENT,
    max_keys: int = DEFAULT_MAX_KEYS,
):
    """造一个限速器，所有桶都按**精确容量**创建。

    生产默认路径的容量带 ``MIN_BURST`` 下限（16KB），用它做断言会看不见"桶用光了"这件事；
    这里显式给容量，断言就能精确到具体秒数。
    """
    clock = FakeClock()

    def factory(rate: float) -> TokenBucket:
        return TokenBucket(rate, burst=burst, clock=clock, sleep=clock.sleep)

    limiter = ClientRateLimiter(
        upload_bps=upload_bps,
        download_bps=download_bps,
        scope=scope,
        max_keys=max_keys,
        bucket_factory=factory,
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


# --------------------------------------------------------------------------- #
# 汇总口径与 key 空间（六期）
# --------------------------------------------------------------------------- #


def test_default_scope_keeps_one_bucket_per_client() -> None:
    """默认口径下升级前后**完全一致**：端口与访客都不参与分桶。

    这是本轮的兼容性红线：老配置（不写 ``rate_limit_scope``）下
    ``per_client_*_bps`` 仍然只是"该客户端总额度"。
    """

    limiter = ClientRateLimiter(upload_bps=1000)
    assert limiter.scope == RATE_LIMIT_SCOPE_CLIENT

    same = limiter.key_for("client-a", public_port=80, visitor_host="203.0.113.7")
    assert same == limiter.key_for("client-a", public_port=81, visitor_host="198.51.100.9")
    assert limit_key_owner(same) == "client-a"


def test_port_scope_shards_by_public_port() -> None:
    key = compose_limit_key(RATE_LIMIT_SCOPE_PORT, "client-a", public_port=8080)

    assert limit_key_owner(key) == "client-a"
    assert describe_limit_key(key) == "client-a|p8080"
    assert key != compose_limit_key(RATE_LIMIT_SCOPE_PORT, "client-a", public_port=8081)

    # 按端口分桶却不给端口 = 调用方 bug，必须立刻炸，不能悄悄退化成按客户端
    with pytest.raises(ValueError):
        compose_limit_key(RATE_LIMIT_SCOPE_PORT, "client-a")


def test_visitor_scope_shards_by_host_and_survives_ipv6_colons() -> None:
    key = compose_limit_key(RATE_LIMIT_SCOPE_VISITOR, "client-a", visitor_host="::1")

    # 分片本身含分隔符（IPv6）也不能影响"拆回原主"——切分点只由长度决定
    assert limit_key_owner(key) == "client-a"
    assert describe_limit_key(key) == "client-a|v::1"

    with pytest.raises(ValueError):
        compose_limit_key(RATE_LIMIT_SCOPE_VISITOR, "client-a")


def test_unknown_scope_is_rejected_loudly() -> None:
    with pytest.raises(ValueError):
        compose_limit_key("sideways", "client-a")
    with pytest.raises(ValueError):
        ClientRateLimiter(upload_bps=1000, scope="sideways")


def test_key_space_cannot_be_forged_by_a_hostile_client_id() -> None:
    """核心不变量：client_id 由客户端提供、只校验非空，**可以含分隔符**。

    朴素拼接（``client_id + "|" + 分片``）下，``client_id="a"`` 的端口分片 ``a|p8080``
    会与 ``client_id="a|p8080"`` 的客户端级 key 撞成同一个桶；长度前缀编码必须让两者分开。
    """

    victim = compose_limit_key(RATE_LIMIT_SCOPE_PORT, "a", public_port=8080)
    hostile = compose_limit_key(RATE_LIMIT_SCOPE_CLIENT, "a|p8080")

    assert victim != hostile
    assert limit_key_owner(victim) == "a"
    assert limit_key_owner(hostile) == "a|p8080"


def test_forget_client_drops_all_shards_but_spares_lookalike_owners() -> None:
    """下线回收必须是"按归属逐条删"，不是"按前缀删"。"""

    async def scenario() -> None:
        limiter, _ = make_limiter(upload_bps=1000, burst=1000, scope=RATE_LIMIT_SCOPE_PORT)
        mine = [limiter.key_for("a", public_port=port) for port in (80, 81)]
        lookalike = limiter.key_for("a|p80", public_port=80)
        other = limiter.key_for("b", public_port=80)
        for key in (*mine, lookalike, other):
            await limiter.wait(key, UPLOAD_DIRECTION, 1000)
        assert limiter.key_count == 4

        assert limiter.forget_client("a") == 2

        assert limiter.key_count == 2
        assert set(limiter.active_keys()) == {lookalike, other}
        # 冒名者的桶还在，而且配额**没被重置**（桶真被删掉的话这里会是 0.0）
        assert await limiter.wait(lookalike, UPLOAD_DIRECTION, 500) == pytest.approx(0.5)

    asyncio.run(scenario())


def test_port_scope_gives_each_port_its_own_quota() -> None:
    """同一个客户端的不同公网端口各有一份额度——这正是"按端口"与"按客户端"的区别。"""

    async def scenario() -> None:
        limiter, _ = make_limiter(upload_bps=1000, burst=1000, scope=RATE_LIMIT_SCOPE_PORT)
        port_80 = limiter.key_for("client-a", public_port=80)
        port_81 = limiter.key_for("client-a", public_port=81)

        await limiter.wait(port_80, UPLOAD_DIRECTION, 1000)

        assert await limiter.wait(port_80, UPLOAD_DIRECTION, 500) == pytest.approx(0.5)
        assert await limiter.wait(port_81, UPLOAD_DIRECTION, 500) == 0.0
        assert limiter.key_count == 2

    asyncio.run(scenario())


def test_visitor_scope_gives_each_visitor_ip_its_own_quota() -> None:
    """按访客分桶：换访客各自一份额度；口径边界在**客户端**这一层。

    跨客户端的同一个访客**不合并**计数——桶按客户端生命周期创建与回收
    （客户端下线要能一次性全部释放），全局访客桶需要另一套过期策略，不在本轮内。
    """

    async def scenario() -> None:
        limiter, _ = make_limiter(upload_bps=1000, burst=1000, scope=RATE_LIMIT_SCOPE_VISITOR)
        first = limiter.key_for("client-a", public_port=80, visitor_host="203.0.113.7")
        second = limiter.key_for("client-a", public_port=80, visitor_host="203.0.113.8")

        await limiter.wait(first, UPLOAD_DIRECTION, 1000)

        assert await limiter.wait(first, UPLOAD_DIRECTION, 500) == pytest.approx(0.5)
        assert await limiter.wait(second, UPLOAD_DIRECTION, 500) == 0.0
        # 同一个访客换端口仍是同一个桶（访客口径下端口不参与）
        assert limiter.key_for("client-a", public_port=81, visitor_host="203.0.113.7") == first
        assert limiter.key_for("client-b", public_port=80, visitor_host="203.0.113.7") != first

    asyncio.run(scenario())


def test_bucket_table_evicts_the_least_recently_used_key() -> None:
    """桶表满时淘汰最久未用的——"按访客分桶"必须有个上限兜底。"""

    async def scenario() -> None:
        limiter, _ = make_limiter(upload_bps=1000, burst=1000, scope=RATE_LIMIT_SCOPE_PORT, max_keys=2)
        first = limiter.key_for("a", public_port=80)
        second = limiter.key_for("b", public_port=80)
        third = limiter.key_for("c", public_port=80)

        await limiter.wait(first, UPLOAD_DIRECTION, 1000)  # 把 a 的桶掏空
        await limiter.wait(second, UPLOAD_DIRECTION, 1)
        await limiter.wait(first, UPLOAD_DIRECTION, 1)  # 再摸一下 a：a 变成"最近使用"
        assert limiter.key_count == 2 and limiter.evicted == 0

        await limiter.wait(third, UPLOAD_DIRECTION, 1)  # 插入第三个 → 淘汰最久未用的 b

        assert set(limiter.active_keys()) == {first, third}
        assert limiter.evicted == 1
        # a 的桶是**被保住**的那个：它还记得自己的账（不是重新建出来的满桶）
        assert await limiter.wait(first, UPLOAD_DIRECTION, 1000) > 0.9

    asyncio.run(scenario())


def test_max_keys_must_be_positive() -> None:
    with pytest.raises(ValueError):
        ClientRateLimiter(upload_bps=1000, max_keys=0)


def test_limit_key_owner_rejects_foreign_keys() -> None:
    """不是本模块编出来的 key 直接拒绝——宁可炸，也不要猜一个 owner 出来乱删。"""

    for bad in ("client-a", "abc:def", "1"):
        with pytest.raises(ValueError):
            limit_key_owner(bad)
    # 日志路径不抛异常：读不出来的原样返回
    assert describe_limit_key("client-a") == "client-a"
