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

**汇总口径是可配的**（见 :data:`RATE_LIMIT_SCOPES`）：默认按客户端，也可以按公网端口
或按访客 IP 分桶。三种口径共用同一份桶表，区别只在 key 怎么编（:func:`compose_limit_key`）——
桶本身完全不知道自己是按什么口径分的。

``pipe`` 那边刻意不认识"客户端"这类业务概念：它只把调用方给的 ``key`` 原样转交，
怎么按 key 汇总由本模块决定（见 :class:`ClientRateLimiter` 与 ``pipe.RateLimitHook``）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from typing import Awaitable, Callable, Dict, List, Optional

from localtonet.core.pipe import DOWNLOAD_DIRECTION, UPLOAD_DIRECTION
from logging_setup import get_logger

__all__ = [
    "MIN_BURST",
    "DEFAULT_BURST_SECONDS",
    "DEFAULT_MAX_KEYS",
    "KEY_SEPARATOR",
    "RATE_LIMIT_SCOPE_CLIENT",
    "RATE_LIMIT_SCOPE_PORT",
    "RATE_LIMIT_SCOPE_VISITOR",
    "RATE_LIMIT_SCOPES",
    "TokenBucket",
    "ClientRateLimiter",
    "compose_limit_key",
    "limit_key_owner",
    "describe_limit_key",
]

MIN_BURST = 16 * 1024
"""桶容量下限（字节）。给"只给速率"的默认路径兜底，避免低速时容量小到要拆成几百段。"""

DEFAULT_BURST_SECONDS = 0.25
"""桶容量按 ``rate * 该系数`` 取，等于"允许突发这么多秒的流量"。"""

DEFAULT_MAX_KEYS = 4096
"""桶表条目数上限（全局，不是每客户端）。超了淘汰"最久未用"的那条。

为什么必须有上限：按访客口径分桶时，条目数由**访客 IP 的个数**决定，
客户端下线时的回收管不到"一个客户端在线期间被海量 IP 访问"这种增长。
桶被淘汰的后果很轻（那个单位重新拿到一个满桶），比"内存慢慢被吃满"好得多。
"""

KEY_SEPARATOR = ":"
"""汇总 key 里的字段分隔符。**只用于切分，不用于前缀匹配**（见 :func:`compose_limit_key`）。"""

RATE_LIMIT_SCOPE_CLIENT = "client"
"""按客户端汇总：该客户端的所有端口与所有访客共用一份额度（历史行为）。"""

RATE_LIMIT_SCOPE_PORT = "port"
"""按公网端口汇总：每个公网端口各自一份额度（访客分不到，同一个端口的访客共享）。"""

RATE_LIMIT_SCOPE_VISITOR = "visitor"
"""按访客 IP 汇总：每个访客 IP 各自一份额度（**同一个客户端内**先分客户端再分访客）。"""

RATE_LIMIT_SCOPES = (
    RATE_LIMIT_SCOPE_CLIENT,
    RATE_LIMIT_SCOPE_PORT,
    RATE_LIMIT_SCOPE_VISITOR,
)
"""全部可选口径。配置侧（``limits.rate_limit_scope``）直接用它做取值校验。"""

_EVICT_LOG_EVERY = 100
"""桶表淘汰的日志节流：第 1 次与之后每第 N 次才记一条 WARNING。

淘汰是"攻击特征"而不是"日常噪声"：某个人可以拿一堆 IP 把日志灌满，
所以要给日志本身也设个上限（同 A2-20 对非法 ``client_id`` 的处理）。
"""


def compose_limit_key(
    scope: str,
    client_id: str,
    *,
    public_port: Optional[int] = None,
    visitor_host: str = "",
) -> str:
    """把 ``(客户端, 分片)`` 编成一个限速汇总 key。

    **为什么不是 ``client_id + "|" + 分片`` 这种朴素拼接**：``client_id`` 由客户端自己提供，
    服务端只校验"非空字符串"，里面完全可以带分隔符。朴素拼接下，
    ``client_id="a"`` 的端口分片 ``a|p80`` 会与 ``client_id="a|p80"`` 的客户端级 key
    撞成同一个桶——两个客户端共享配额，而且回收时还会**误删别人的桶**。

    这里的编码把 key 做成**长度前缀**的：先写 ``client_id`` 的字符数、再写分隔符、再写原值。
    切分点只由长度决定，于是对**任意** ``client_id`` 都能无歧义地拆回原主；
    分片里含分隔符（IPv6 的 ``::1``）也不影响解析。
    """
    if scope == RATE_LIMIT_SCOPE_CLIENT:
        shard = ""
    elif scope == RATE_LIMIT_SCOPE_PORT:
        if public_port is None:
            raise ValueError("按端口限速需要 public_port")
        shard = f"p{public_port}"
    elif scope == RATE_LIMIT_SCOPE_VISITOR:
        if not visitor_host:
            raise ValueError("按访客限速需要 visitor_host")
        shard = f"v{visitor_host}"
    else:
        raise ValueError(f"未知的限速口径 {scope!r}，可选 {list(RATE_LIMIT_SCOPES)}")
    return f"{len(client_id)}{KEY_SEPARATOR}{client_id}{KEY_SEPARATOR}{shard}"


def limit_key_owner(key: str) -> str:
    """从汇总 key 里拆回它属于哪个客户端。

    key 只能由 :func:`compose_limit_key` 生成；拿到别的形状说明有人绕过了那个函数，
    属于编程错误，直接抛 ``ValueError`` 而不是猜一个 owner 出来。
    """
    head, separator, rest = key.partition(KEY_SEPARATOR)
    if not separator or not head.isdigit():
        raise ValueError(f"无法识别的限速 key：{key!r}")
    return rest[: int(head)]


def describe_limit_key(key: str) -> str:
    """key 的**人类可读**形式（只用于日志）：``client_id`` 或 ``client_id|分片``。

    存储用的 key 带长度前缀，直接打日志很难读；日志里用这个形式，
    读不出来的（不是本模块生成的）原样返回，不抛异常——日志路径不该再制造失败。
    """
    head, separator, rest = key.partition(KEY_SEPARATOR)
    if not separator or not head.isdigit():
        return key
    length = int(head)
    owner = rest[:length]
    shard = rest[length + 1 :]
    return f"{owner}|{shard}" if shard else owner

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
    """按调用方给的汇总单位限速的令牌桶集合。

    钩子接口与 :class:`localtonet.core.pipe.RateLimitHook` 一致：
    ``await limiter.wait(key, direction, amount)``。

    * ``key`` —— 汇总单位，由 :func:`compose_limit_key` 按 ``scope`` 编出来。
    * ``direction`` —— ``"a->b"``（上行）或 ``"b->a"``（下行），沿用 ``pipe`` 的 tag 空间。
    * 返回本次等待秒数，0 表示没等。

    ``upload_bps`` / ``download_bps`` 为 ``0`` 表示该方向不限速；两个都是 0 时
    :attr:`enabled` 为 False，调用方应当干脆不要传这个限速器，让 ``pipe`` 走原路径。

    ``scope`` 只影响 **key 怎么编**（:meth:`key_for`），桶本身一视同仁；
    ``max_keys`` 是桶表条目数上限，满了淘汰最久未用的那条（见 :data:`DEFAULT_MAX_KEYS`）。
    """

    def __init__(
        self,
        *,
        upload_bps: float = 0,
        download_bps: float = 0,
        burst_seconds: float = DEFAULT_BURST_SECONDS,
        scope: str = RATE_LIMIT_SCOPE_CLIENT,
        max_keys: int = DEFAULT_MAX_KEYS,
        logger: Optional[logging.Logger] = None,
        bucket_factory: Callable[[float], TokenBucket] = TokenBucket,
    ) -> None:
        if scope not in RATE_LIMIT_SCOPES:
            raise ValueError(f"未知的限速口径 {scope!r}，可选 {list(RATE_LIMIT_SCOPES)}")
        if max_keys <= 0:
            raise ValueError("max_keys 必须为正数；不设上限会让桶表无界增长")
        self._upload_bps = float(upload_bps)
        self._download_bps = float(download_bps)
        self._burst_seconds = burst_seconds
        self._scope = scope
        self._max_keys = int(max_keys)
        self._log = logger or get_logger("limiter")
        self._factory = bucket_factory
        # OrderedDict 而不是普通 dict：淘汰要按"最久未用"来，而 LRU 需要记住访问顺序
        self._buckets: "OrderedDict[str, Dict[str, TokenBucket]]" = OrderedDict()
        self._evicted = 0

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

    @property
    def scope(self) -> str:
        """当前的汇总口径。"""
        return self._scope

    @property
    def max_keys(self) -> int:
        return self._max_keys

    @property
    def key_count(self) -> int:
        """桶表里现有多少个 key（比 :meth:`active_keys` 便宜，快照路径用它）。"""
        return len(self._buckets)

    @property
    def evicted(self) -> int:
        """累计因桶表满而淘汰的 key 数。非 0 说明 :data:`DEFAULT_MAX_KEYS` 该调大了。"""
        return self._evicted

    def active_keys(self) -> List[str]:
        """当前已分配过桶的 key（客户端下线时应当 :meth:`forget_client` 掉）。"""
        return list(self._buckets)

    # ------------------------------------------------------------------ #

    def key_for(
        self,
        client_id: str,
        *,
        public_port: Optional[int] = None,
        visitor_host: str = "",
    ) -> str:
        """按本限速器的口径，为一次转发编出汇总 key。

        把口径收在限速器内部（而不是让调用方各自拼 key），是为了让"口径"只有一个出处：
        调用方只管把"这次的端口与访客是谁"递进来。
        """
        return compose_limit_key(
            self._scope,
            client_id,
            public_port=public_port,
            visitor_host=visitor_host,
        )

    async def wait(self, key: str, direction: str, amount: int) -> float:
        """在写入 ``amount`` 字节前取配额。不限速的方向直接返回 0。"""
        if amount <= 0:
            return 0.0
        bucket = self._bucket_for(key, direction)
        if bucket is None:
            return 0.0
        return await bucket.acquire(amount)

    def forget(self, key: str) -> None:
        """释放**单个** key 的桶。"""
        if self._buckets.pop(key, None) is not None:
            self._log.debug("已释放 %s 的限速桶", describe_limit_key(key))

    def forget_client(self, client_id: str) -> int:
        """释放某个客户端的**全部**桶（含按端口/按访客的分片），返回释放的 key 数。

        客户端下线时必须调，否则长期运行会慢慢泄漏：一旦口径不是"按客户端"，
        桶的 key 就不再等于 ``client_id``，:meth:`forget` 永远也匹配不上。

        这里**刻意不做前缀匹配**：``client_id`` 里可以含分隔符，
        ``"a|p80"`` 这种名字的前缀恰好是 ``"a"``，按前缀删会连带删掉别的客户端的桶
        （见 :func:`compose_limit_key`）。所以逐条解析出归属、只删自己那些。
        条目数受 ``max_keys`` 封顶，这次全表扫描的代价是有界的。
        """
        mine = [key for key in self._buckets if limit_key_owner(key) == client_id]
        for key in mine:
            del self._buckets[key]
        if mine:
            self._log.debug("已释放客户端 %s 的 %d 个限速桶", client_id, len(mine))
        return len(mine)

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
            self._evict_if_full()
            buckets = {}
            self._buckets[key] = buckets
        else:
            # 记一次"最近使用"，淘汰时才有依据可依
            self._buckets.move_to_end(key)
        bucket = buckets.get(direction)
        if bucket is None:
            bucket = self._factory(rate)
            buckets[direction] = bucket
        return bucket

    def _evict_if_full(self) -> None:
        """表满时淘汰最久未用的 key。临界区内不含 await。"""
        if len(self._buckets) < self._max_keys:
            return
        key, _ = self._buckets.popitem(last=False)
        self._evicted += 1
        if self._evicted == 1 or self._evicted % _EVICT_LOG_EVERY == 0:
            self._log.warning(
                "限速桶表已达上限 %d，淘汰最久未用的 %s（累计淘汰 %d 个）",
                self._max_keys,
                describe_limit_key(key),
                self._evicted,
            )

    def __repr__(self) -> str:
        return (
            f"ClientRateLimiter(upload={self._upload_bps:g}/s, download={self._download_bps:g}/s, "
            f"scope={self._scope}, keys={len(self._buckets)}/{self._max_keys})"
        )
