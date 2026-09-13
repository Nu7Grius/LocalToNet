# -*- coding: utf-8 -*-
"""
localtonet.core.backoff —— 指数退避
====================================
控制连接断开后，客户端按 ``1s → 2s → 4s → … → 60s`` 重连，封顶后一直用 60s 重试。
**连接成功后必须调 ``reset()``**，否则短暂抖动累积出来的大延迟会一直跟着客户端。

``jitter`` 为 0 时序列完全确定，便于测试；大于 0 时在延迟上叠加
``[0, jitter)`` 的随机量，避免大量客户端同时重连造成惊群。
"""

from __future__ import annotations

import asyncio
import random
from typing import Callable, Iterator, Optional

from config import ReconnectPolicy

__all__ = ["Backoff"]


class Backoff:
    """按 :class:`config.ReconnectPolicy` 生成退避延迟。"""

    def __init__(
        self,
        policy: ReconnectPolicy,
        *,
        random_fn: Optional[Callable[[], float]] = None,
    ) -> None:
        self._policy = policy
        self._random = random_fn or random.random
        self._attempt = 0
        self._current = policy.initial_delay

    @property
    def attempt(self) -> int:
        """已经等待过的失败次数。"""
        return self._attempt

    @property
    def policy(self) -> ReconnectPolicy:
        return self._policy

    def reset(self) -> None:
        """连接成功后调用，让下一次断线从头开始。"""
        self._attempt = 0
        self._current = self._policy.initial_delay

    def peek(self) -> float:
        """预看下一次延迟，不推进状态。"""
        return self._with_jitter(self._current)

    def next_delay(self) -> float:
        """取出下一次延迟并推进状态。"""
        delay = self._with_jitter(self._current)
        self._attempt += 1
        self._current = min(self._current * self._policy.multiplier, self._policy.max_delay)
        return delay

    def delays(self, count: int) -> Iterator[float]:
        """生成 count 个**不推进自身状态**的延迟序列，便于自检与文档示例。"""
        current = self._policy.initial_delay
        for _ in range(count):
            yield self._with_jitter(current)
            current = min(current * self._policy.multiplier, self._policy.max_delay)

    async def sleep(self) -> float:
        """按当前退避值睡一觉，返回实际睡了多久。"""
        delay = self.next_delay()
        await asyncio.sleep(delay)
        return delay

    def _with_jitter(self, delay: float) -> float:
        jitter = self._policy.jitter
        if jitter <= 0:
            return delay
        return delay + self._random() * jitter

    def __repr__(self) -> str:
        return (
            f"Backoff(attempt={self._attempt}, next={self.peek():.1f}s, "
            f"max={self._policy.max_delay:.1f}s)"
        )
