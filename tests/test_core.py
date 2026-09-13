# -*- coding: utf-8 -*-
"""
tests/test_core.py —— 核心机制单测
====================================
这里放的是"单靠端到端测试不容易定位"的那部分逻辑：

* ``pipe_both`` 的**交叉配对**（曾经的 bug：读出来又写回同一侧，形成原地回环，
  表现为"日志里明明有流量，对端却永远收不到"）
* ``PendingConn`` 的 ``ready`` 语义（超时不能取消它，否则迟到的数据通道会撞上 InvalidStateError）
* 端口归属的认领 / 冲突 / 释放 / 路由
* 心跳超时判定与看门狗巡检
* 注册表式指令分发、事件总线的异常隔离、鉴权器
"""

from __future__ import annotations

import asyncio
import socket
from typing import Any, List, Tuple

import pytest

from config import AuthConfig, MappingRule, ReconnectPolicy
from localtonet.core.backoff import Backoff
from localtonet.core.dispatcher import MessageDispatcher, handler
from localtonet.core.events import EventBus, EventType
from localtonet.core.heartbeat import HeartbeatTask, Watchdog
from localtonet.core.pipe import pipe_both
from localtonet.errors import AuthError
from localtonet.server.auth import NoneAuthenticator, TokenAuthenticator, build_authenticator
from localtonet.server.pending import PendingConn, PendingTable
from localtonet.server.registry import ClientRegistry, ClientSession
from protocol import MsgType

# --------------------------------------------------------------------------- #
# pipe_both：双向搬运
# --------------------------------------------------------------------------- #


async def _wrap(sock: socket.socket) -> Tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    sock.setblocking(False)
    return await asyncio.open_connection(sock=sock)


def test_pipe_both_moves_bytes_across_not_around() -> None:
    """上行/下行必须交叉配对。

    如果误写成 ``a_reader -> a_writer``，数据会在同一侧原地打转，
    测试里的对端一个字节都收不到——这正是本项目开发中踩过的坑。
    """

    async def scenario() -> None:
        a_proxy, a_peer = socket.socketpair()
        b_proxy, b_peer = socket.socketpair()
        a_reader, a_writer = await _wrap(a_proxy)
        b_reader, b_writer = await _wrap(b_proxy)
        a_peer_reader, a_peer_writer = await _wrap(a_peer)
        b_peer_reader, b_peer_writer = await _wrap(b_peer)

        pipe_task = asyncio.create_task(pipe_both(a_reader, a_writer, b_reader, b_writer, label="unit"))
        try:
            b_peer_writer.write(b"from-b-longer")
            await b_peer_writer.drain()
            assert await asyncio.wait_for(a_peer_reader.readexactly(13), timeout=3.0) == b"from-b-longer"

            a_peer_writer.write(b"from-a")
            await a_peer_writer.drain()
            assert await asyncio.wait_for(b_peer_reader.readexactly(6), timeout=3.0) == b"from-a"

            # a 侧关掉写端后，b 侧必须读到 EOF（半关闭要正确传递），pipe 也随之收尾
            a_peer_writer.close()
            assert await asyncio.wait_for(b_peer_reader.read(), timeout=3.0) == b""

            stats = await asyncio.wait_for(pipe_task, timeout=3.0)
        finally:
            for writer in (a_peer_writer, b_peer_writer):
                writer.close()
            if not pipe_task.done():
                pipe_task.cancel()
                await asyncio.gather(pipe_task, return_exceptions=True)

        assert stats.upload == 6, "上行应统计 a->b 的字节数"
        assert stats.download == 13, "下行应统计 b->a 的字节数"
        assert stats.total == 19
        assert stats.stopped_by == "a->b"

    asyncio.run(scenario())


def test_pipe_both_returns_when_peer_closes() -> None:
    """对端直接断连时不能卡死，必须能被取消协程调用方安全收尾。"""

    async def scenario() -> None:
        a_proxy, a_peer = socket.socketpair()
        b_proxy, b_peer = socket.socketpair()
        a_reader, a_writer = await _wrap(a_proxy)
        b_reader, b_writer = await _wrap(b_proxy)
        a_peer.close()
        b_peer.close()

        stats = await asyncio.wait_for(pipe_both(a_reader, a_writer, b_reader, b_writer), timeout=3.0)
        assert stats.total == 0

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# Backoff
# --------------------------------------------------------------------------- #


def test_backoff_follows_exponential_sequence_capped_at_max() -> None:
    policy = ReconnectPolicy(initial_delay=1.0, max_delay=60.0, multiplier=2.0, jitter=0.0)
    backoff = Backoff(policy)
    delays = [backoff.next_delay() for _ in range(9)]
    assert delays == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0, 60.0]
    assert backoff.attempt == 9


def test_backoff_reset_after_successful_connect() -> None:
    backoff = Backoff(ReconnectPolicy(jitter=0.0))
    for _ in range(5):
        backoff.next_delay()
    assert backoff.attempt == 5
    backoff.reset()
    assert backoff.attempt == 0
    assert backoff.next_delay() == 1.0


def test_backoff_jitter_stays_within_bound() -> None:
    backoff = Backoff(ReconnectPolicy(jitter=0.5), random_fn=lambda: 1.0)
    assert backoff.next_delay() == 1.5


# --------------------------------------------------------------------------- #
# 端口归属与路由
# --------------------------------------------------------------------------- #


def _session(client_id: str) -> ClientSession:
    return ClientSession(client_id=client_id, reader=None, writer=None, peer="127.0.0.1:1")  # type: ignore[arg-type]


def test_registry_claims_and_releases_ports() -> None:
    registry = ClientRegistry()
    registry.add(_session("alpha"))

    claimed, conflicts = registry.claim_ports("alpha", [8000, 8001])
    assert claimed == [8000, 8001]
    assert conflicts == []
    assert registry.owner_of(8000) == "alpha"

    # 同一个客户端重复认领是幂等的
    assert registry.claim_ports("alpha", [8000]) == ([8000], [])

    registry.remove("alpha")
    assert registry.owner_of(8000) is None
    assert registry.client_count == 0


def test_registry_rejects_port_owned_by_another_client() -> None:
    registry = ClientRegistry()
    registry.add(_session("alpha"))
    registry.add(_session("beta"))
    registry.claim_ports("alpha", [8000, 8001])

    claimed, conflicts = registry.claim_ports("beta", [8001, 8002])
    assert claimed == [8002]
    assert conflicts == [8001]
    assert registry.owner_of(8001) == "alpha"


def test_registry_routes_to_port_owner_first() -> None:
    registry = ClientRegistry()
    registry.add(_session("alpha"))
    registry.add(_session("beta"))
    registry.claim_ports("beta", [9000])

    assert registry.pick_client(9000).client_id == "beta"  # type: ignore[union-attr]
    # 没人认领的端口 → 退回第一个在线客户端
    assert registry.pick_client(9999).client_id == "alpha"  # type: ignore[union-attr]


def test_registry_falls_back_when_owner_offline() -> None:
    registry = ClientRegistry()
    registry.add(_session("alpha"))
    registry.add(_session("beta"))
    registry.claim_ports("beta", [9000])
    registry.remove("beta")

    assert registry.pick_client(9000).client_id == "alpha"  # type: ignore[union-attr]


def test_registry_routing_strategy_is_injectable() -> None:
    """路由是扩展点：塞一个自定义策略进来就能改派发规则。"""

    def always_last(reg: ClientRegistry, local_port: Any) -> Any:
        return reg.sessions()[-1]

    registry = ClientRegistry(routing=always_last)
    registry.add(_session("alpha"))
    registry.add(_session("beta"))
    assert registry.pick_client(9999).client_id == "beta"  # type: ignore[union-attr]


def test_registry_replaces_session_with_same_client_id() -> None:
    registry = ClientRegistry()
    old = _session("alpha")
    registry.add(old)
    registry.claim_ports("alpha", [8000])

    new = _session("alpha")
    previous = registry.add(new)
    assert previous is old
    assert registry.client_count == 1
    # 顶号时旧会话的端口归属被清掉，等新连接重新认领
    assert registry.owner_of(8000) is None


def test_registry_collects_idle_sessions() -> None:
    registry = ClientRegistry()
    session = _session("alpha")
    registry.add(session)
    session.last_seen -= 999
    assert registry.collect_expired(idle_timeout=120.0) == [session]
    assert registry.collect_expired(idle_timeout=10000.0) == []


# --------------------------------------------------------------------------- #
# PendingConn 配对
# --------------------------------------------------------------------------- #


def _pending(conn_id: str = "c1", client_id: str = "alpha") -> PendingConn:
    return PendingConn(
        conn_id=conn_id,
        public_port=9028,
        local_port=8000,
        client_id=client_id,
        visitor_reader=None,  # type: ignore[arg-type]
        visitor_writer=None,  # type: ignore[arg-type]
    )


def test_pending_timeout_does_not_cancel_ready_future() -> None:
    """超时只是"等不到就走开"，不能把 ready 取消掉——否则迟到的 attach 会炸 InvalidStateError。"""

    async def scenario() -> None:
        pending = _pending()
        assert await pending.wait_ready(0.01) is False
        assert not pending.ready.done()

        assert pending.attach(None, object()) is True  # type: ignore[arg-type]
        assert await pending.wait_ready(0.5) is True
        assert pending.paired

    asyncio.run(scenario())


def test_pending_rejects_duplicate_and_late_attach() -> None:
    async def scenario() -> None:
        pending = _pending()
        assert pending.attach(None, object()) is True  # type: ignore[arg-type]
        assert pending.attach(None, object()) is False  # type: ignore[arg-type]

        failed = _pending("c2")
        failed.fail("后端连不上")
        assert failed.closed is True
        assert await failed.wait_ready(0.5) is False
        assert failed.attach(None, object()) is False  # type: ignore[arg-type]
        assert failed.error == "后端连不上"

    asyncio.run(scenario())


def test_pending_finish_unblocks_done_waiters() -> None:
    async def scenario() -> None:
        pending = _pending()
        waiter = asyncio.create_task(pending.wait_done())
        await asyncio.sleep(0)
        assert not waiter.done()
        pending.finish()
        await asyncio.wait_for(waiter, timeout=1.0)

    asyncio.run(scenario())


def test_pending_table_fails_all_requests_of_a_client() -> None:
    async def scenario() -> None:
        table = PendingTable()
        mine = table.create(_pending("c1", "alpha"))
        other = table.create(_pending("c2", "beta"))

        affected = table.fail_all_for_client("alpha", "客户端已离线")
        assert affected == [mine]
        assert mine.closed and mine.error == "客户端已离线"
        assert not other.closed
        assert table.attach("c1", None, object()) is None  # type: ignore[arg-type]
        assert table.discard("c1") is mine

    asyncio.run(scenario())


def test_pending_table_attach_unknown_conn_id() -> None:
    table = PendingTable()
    assert table.attach("nope", None, object()) is None  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# 指令分发
# --------------------------------------------------------------------------- #


class _FakeEndpoint:
    def __init__(self) -> None:
        self.seen: List[dict] = []

    @handler(MsgType.PING)
    async def _on_ping(self, msg: dict, extra: str = "") -> None:
        self.seen.append({"type": msg["type"], "extra": extra})

    async def _not_a_handler(self, msg: dict) -> None:  # pragma: no cover - 不该被注册
        raise AssertionError("没有 @handler 标记的方法不应被注册")


def test_dispatcher_auto_registers_decorated_methods() -> None:
    endpoint = _FakeEndpoint()
    dispatcher = MessageDispatcher.from_object(endpoint)
    assert dispatcher.registered_types() == (MsgType.PING,)
    assert not dispatcher.handles(MsgType.NEW_CONN)


def test_dispatcher_passes_extra_context_arguments() -> None:
    async def scenario() -> None:
        endpoint = _FakeEndpoint()
        dispatcher = MessageDispatcher.from_object(endpoint)
        await dispatcher.dispatch({"type": MsgType.PING}, "session-x")
        await dispatcher.dispatch({"type": MsgType.NEW_CONN})

        assert endpoint.seen == [{"type": "ping", "extra": "session-x"}]

    asyncio.run(scenario())


def test_dispatcher_unknown_type_uses_fallback_when_provided() -> None:
    async def scenario() -> None:
        fallback: List[dict] = []

        async def unknown(msg: dict) -> None:
            fallback.append(msg)

        dispatcher = MessageDispatcher(unknown_handler=unknown)
        await dispatcher.dispatch({"type": "brand_new_command"})
        assert fallback == [{"type": "brand_new_command"}]

    asyncio.run(scenario())


def test_dispatcher_rejects_duplicate_and_missing_type() -> None:
    async def scenario() -> None:
        dispatcher = MessageDispatcher()
        dispatcher.register("x", _FakeEndpoint()._on_ping)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="已注册"):
            dispatcher.register("x", _FakeEndpoint()._on_ping)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="type"):
            await dispatcher.dispatch({"no_type": 1})

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# 事件总线
# --------------------------------------------------------------------------- #


def test_event_bus_isolates_broken_subscribers() -> None:
    """一个写坏的订阅者不能把发事件的一方带崩。"""
    bus = EventBus()
    received: List[int] = []

    def broken(**_: Any) -> None:
        raise RuntimeError("我是坏的订阅者")

    bus.on(EventType.REQUEST_START, broken)
    bus.on(EventType.REQUEST_START, lambda **payload: received.append(payload["conn_id"]))

    bus.emit(EventType.REQUEST_START, conn_id=7)
    assert received == [7]
    assert bus.subscriber_count(EventType.REQUEST_START) == 2


def test_event_bus_unsubscribe_via_returned_callable() -> None:
    bus = EventBus()
    hits: List[str] = []
    off = bus.on("evt", lambda **_: hits.append("x"))
    bus.emit("evt")
    off()
    bus.emit("evt")
    assert hits == ["x"]


# --------------------------------------------------------------------------- #
# 心跳与看门狗
# --------------------------------------------------------------------------- #


def test_heartbeat_reports_lost_when_pong_never_comes() -> None:
    async def scenario() -> None:
        async def send_ping() -> None:
            return None

        heartbeat = HeartbeatTask(interval=0.02, pong_timeout=0.01, send_ping=send_ping)
        lost: List[str] = []
        await asyncio.wait_for(heartbeat.run(on_lost=lost.append), timeout=2.0)

        assert len(lost) == 1
        assert "未收到 pong" in lost[0]
        assert heartbeat.stats["timeouts"] == 1

    asyncio.run(scenario())


def test_heartbeat_stays_alive_while_pongs_arrive() -> None:
    async def scenario() -> None:
        heartbeat: HeartbeatTask

        async def send_ping() -> None:
            # 模拟对端立刻回 pong
            heartbeat.note_pong()

        heartbeat = HeartbeatTask(interval=0.05, pong_timeout=0.02, send_ping=send_ping)
        lost: List[str] = []
        task = asyncio.create_task(heartbeat.run(on_lost=lost.append))
        await asyncio.sleep(0.2)
        heartbeat.stop()
        await asyncio.wait_for(task, timeout=1.0)

        assert lost == []
        assert heartbeat.stats["pings"] >= 1
        assert heartbeat.stats["timeouts"] == 0

    asyncio.run(scenario())


def test_heartbeat_rejects_pong_timeout_not_smaller_than_interval() -> None:
    async def scenario() -> None:
        async def send_ping() -> None:
            return None

        with pytest.raises(ValueError, match="pong_timeout"):
            HeartbeatTask(interval=1.0, pong_timeout=1.0, send_ping=send_ping)

    asyncio.run(scenario())


def test_watchdog_reaps_collected_objects() -> None:
    async def scenario() -> None:
        items = ["stale-1", "stale-2"]
        reaped: List[str] = []

        async def expire(item: str) -> None:
            reaped.append(item)

        watchdog = Watchdog(interval=0.02, collect=lambda: list(items), expire=expire)
        task = asyncio.create_task(watchdog.run())
        await asyncio.sleep(0.06)
        watchdog.stop()
        await asyncio.wait_for(task, timeout=1.0)

        assert "stale-1" in reaped and "stale-2" in reaped
        assert watchdog.reaped >= 2

    asyncio.run(scenario())


def test_watchdog_swallows_expire_errors() -> None:
    async def scenario() -> None:
        attempts: List[str] = []

        async def expire(item: str) -> None:
            attempts.append(item)
            raise RuntimeError("清理失败")

        watchdog = Watchdog(interval=0.02, collect=lambda: ["x"], expire=expire)
        task = asyncio.create_task(watchdog.run())
        await asyncio.sleep(0.05)
        watchdog.stop()
        await asyncio.wait_for(task, timeout=1.0)

        assert len(attempts) >= 2, "单个对象清理失败不该让巡检整体停摆"

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# 鉴权扩展点
# --------------------------------------------------------------------------- #


def test_none_authenticator_lets_everything_through() -> None:
    assert NoneAuthenticator().verify({"client_id": "a"}, "127.0.0.1:1") is None


def test_token_authenticator_accepts_only_matching_token() -> None:
    auth = TokenAuthenticator("s3cret")
    assert auth.verify({"token": "s3cret"}, "127.0.0.1:1") is None

    with pytest.raises(AuthError):
        auth.verify({"token": "wrong"}, "127.0.0.1:1")
    with pytest.raises(AuthError):
        auth.verify({}, "127.0.0.1:1")
    with pytest.raises(AuthError):
        auth.verify({"token": 123}, "127.0.0.1:1")


def test_build_authenticator_follows_config() -> None:
    assert isinstance(build_authenticator(None), NoneAuthenticator)
    assert isinstance(build_authenticator(AuthConfig(enabled=False, token="x")), NoneAuthenticator)
    assert isinstance(build_authenticator(AuthConfig(enabled=True, token="x")), TokenAuthenticator)


def test_mapping_rule_roundtrip() -> None:
    rule = MappingRule(public_port=9028, local_port=8000, host="127.0.0.1", remark="demo")
    assert MappingRule.from_dict(rule.to_dict()) == rule
