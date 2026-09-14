# -*- coding: utf-8 -*-
"""
tests/test_e2e.py —— 端到端链路验证
=====================================
每条用例都真实拉起 **内网后端 + 服务端 + 客户端**，走真实 TCP，不做 mock。
覆盖清单：

1. 基础打通                  公网端口请求能到达内网后端并原样返回
2. 并发 20 请求              不同 conn_id 不会串流量
3. 1MB 大包                  长连接大流量不丢字节
4. 流式响应                  分块到达顺序正确、没被整段缓冲
5. 无在线客户端              502 No client online
6. 客户端掉线                502
7. 内网后端未启动            conn_error 上报 + 502（不是空回复）
8. 端口独占                  第二个客户端认领同一端口被拒，老客户端不受影响
9. 动态映射                  set_mapping 加端口立即可用、删端口立即失效
10. 无映射规则               404（竞态窗口分支）
11. 未映射的端口             根本不监听，连接直接被拒
12. 控制连接断开             客户端自动重连并重新认领端口
13. 鉴权通过                 服务端开鉴权 + 客户端带对令牌 → 正常注册并转发流量
14. 令牌错误                 403 → 客户端**只拨号一次**、状态 stopped、CONTROL_LOST(fatal=True)
15. 令牌缺失                 同上；不配令牌的客户端不会陷入"每 60 秒撞一次墙"
16. 容量已满                 503 属于**暂时性**失败 → 客户端继续退避重试，绝不当作 fatal
17. 带宽限速                 服务端按客户端限速后，同样大小的响应要花明显更久，且字节不丢
18. 并发配额                 单客户端在途数超上限 → 访客收到 429（不是 502），在途的那个不受影响
19. 在线客户端数             ServerStats.clients_online 随上下线变化
20. 运行期映射落盘           客户端 set_mapping 的变更**当场写进持久化文件**
21. 重启后映射仍在           服务端重启（配置文件给的是另一个端口）→ 仍监听持久化文件里的端口
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

from config import MappingRule, ServerConfig
from localtonet.client.core import TunnelClient
from localtonet.core.events import EventType
from localtonet.server.core import TunnelServer
from localtonet.server.mapping import FileMappingStore
from tests.helpers import TunnelHarness, free_ports, http_request, http_stream_chunks

# --------------------------------------------------------------------------- #
# 1 ~ 4：正向链路
# --------------------------------------------------------------------------- #


def test_basic_request_round_trip() -> None:
    async def scenario() -> None:
        async with TunnelHarness() as harness:
            response = await http_request(harness.public_port, "/")
            assert response.status == 200
            payload = json.loads(response.text)
            assert payload["ok"] is True
            assert payload["path"] == "/"

            echoed = await http_request(harness.public_port, "/echo?msg=hello%20tunnel")
            assert echoed.status == 200
            assert echoed.body == b"hello tunnel\n"

    asyncio.run(scenario())


def test_concurrent_requests_keep_their_own_responses() -> None:
    """每条请求必须拿回自己的响应——这是 conn_id 配对是否正确的硬指标。"""

    async def scenario() -> None:
        async with TunnelHarness() as harness:
            paths = [f"/echo?msg=req-{index:02d}" for index in range(20)]
            responses = await asyncio.gather(*(http_request(harness.public_port, path) for path in paths))

            for index, response in enumerate(responses):
                assert response.status == 200, f"第 {index} 条请求状态码异常"
                assert response.body == f"req-{index:02d}\n".encode("utf-8"), f"第 {index} 条请求串流了"

            assert harness.server is not None
            assert harness.server.stats.requests_total >= 20
            assert harness.server.stats.requests_failed == 0

    asyncio.run(scenario())


def test_one_megabyte_payload_is_intact() -> None:
    async def scenario() -> None:
        async with TunnelHarness() as harness:
            response = await http_request(harness.public_port, "/big?kb=1024", timeout=30.0)

            assert response.status == 200
            assert len(response.body) == 1024 * 1024
            block = bytes(index % 251 for index in range(1024))
            assert response.body[:1024] == block
            assert response.body[-1024:] == block

            # 转发协程在连接收尾后才回填计数，所以这里等统计追上而不是立刻断言
            assert harness.server is not None
            await harness.wait_until(
                lambda: harness.server.stats.bytes_download >= 1024 * 1024,  # type: ignore[union-attr]
                what="服务端下行字节统计",
            )

    asyncio.run(scenario())


def test_streaming_chunks_arrive_in_order() -> None:
    """流式场景：既要顺序对，也要真的分批到达（说明隧道没有把整段缓冲后再吐）。"""

    async def scenario() -> None:
        async with TunnelHarness() as harness:
            chunks = await http_stream_chunks(
                harness.public_port,
                "/stream?chunks=20&interval=0.02",
                chunk_size=32,
            )
            raw = b"".join(chunks)
            _, _, body = raw.partition(b"\r\n\r\n")
            lines = [line for line in body.decode("utf-8").splitlines() if line]

            assert lines == [f"chunk-{index:04d}" for index in range(20)]
            assert len(chunks) >= 3, f"应分多次到达，实际只收到 {len(chunks)} 段"

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# 5 ~ 7：错误兜底
# --------------------------------------------------------------------------- #


def test_502_when_no_client_online() -> None:
    async def scenario() -> None:
        async with TunnelHarness(start_client=False) as harness:
            response = await http_request(harness.public_port, "/")
            assert response.status == 502
            assert "No client online" in response.text

            assert harness.server is not None
            assert harness.server.stats.requests_failed == 1

    asyncio.run(scenario())


def test_502_after_client_goes_offline() -> None:
    async def scenario() -> None:
        async with TunnelHarness() as harness:
            assert (await http_request(harness.public_port, "/")).status == 200

            assert harness.client is not None
            await harness.client.stop()
            await harness.wait_offline()

            response = await http_request(harness.public_port, "/")
            assert response.status == 502

            assert harness.server is not None
            assert harness.server.registry.owner_of(harness.backend.port) is None

    asyncio.run(scenario())


def test_conn_error_is_reported_when_backend_is_down() -> None:
    """内网后端没启动时，访客必须拿到 502，且理由要指向**真正的失败原因**。

    这里刻意把客户端的 ``connect_timeout`` 压到 0.5s、服务端的 ``pair_timeout`` 放宽到 5s——
    两者的大小关系就是这条用例要守住的约束：服务端等不到配对时，
    必须已经收到客户端的 ``conn_error``，而不是自己先超时并报一句含糊的"配对超时"。
    """
    dead_port = free_ports(1)[0]

    def configure_server(config) -> None:
        config.mapping[0].local_port = dead_port
        config.timeouts.pair_timeout = 5.0

    def configure_client(config) -> None:
        config.timeouts.connect_timeout = 0.5

    async def scenario() -> None:
        async with TunnelHarness(
            local_ports=[dead_port],
            configure_server=configure_server,
            configure_client=configure_client,
        ) as harness:
            assert harness.server is not None
            reports = []
            harness.server.events.on(EventType.CONN_ERROR, lambda **payload: reports.append(payload))

            response = await http_request(harness.public_port, "/")

            assert response.status == 502
            assert "内网后端" in response.text, f"应当透传真实原因，实际为：{response.text!r}"
            assert reports, "服务端应当收到客户端的 conn_error 上报"
            assert reports[0]["local_port"] == dead_port

            assert harness.client is not None
            await harness.wait_until(
                lambda: harness.client.stats.forwards_failed >= 1,  # type: ignore[union-attr]
                what="客户端转发失败计数",
            )

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# 8 ~ 9：多客户端与动态映射
# --------------------------------------------------------------------------- #


def test_second_client_cannot_steal_an_owned_port() -> None:
    async def scenario() -> None:
        async with TunnelHarness() as harness:
            assert harness.client is not None and harness.server is not None

            second_config = harness.build_client_config(client_id="test-second")
            second = TunnelClient(second_config)
            task = asyncio.create_task(second.run(), name="second-client")
            try:
                await harness.wait_until(
                    lambda: harness.server is not None and harness.server.registry.has("test-second"),
                    what="第二个客户端注册",
                )

                assert second.claimed_ports == []
                assert second.conflicted_ports == [harness.backend.port]
                assert harness.server.registry.owner_of(harness.backend.port) == harness.client.client_id

                # 老客户端完全不受影响
                response = await http_request(harness.public_port, "/echo?msg=still-mine")
                assert response.status == 200
                assert response.body == b"still-mine\n"
            finally:
                await second.stop()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_set_mapping_adds_and_removes_guest_ports() -> None:
    async def scenario() -> None:
        async with TunnelHarness() as harness:
            assert harness.client is not None
            extra_port = free_ports(1)[0]

            added = await harness.client.set_mapping(
                [
                    MappingRule(public_port=harness.public_port, local_port=harness.backend.port, host="127.0.0.1"),
                    MappingRule(public_port=extra_port, local_port=harness.backend.port, host="127.0.0.1"),
                ]
            )
            assert added["ok"] is True
            assert extra_port in added["diff"]["added"]

            await harness.wait_until(
                lambda: len(harness.client.remote_mapping) == 2 if harness.client else False,
                what="客户端收到新映射表",
            )

            dynamic = await http_request(extra_port, "/echo?msg=dynamic")
            assert dynamic.status == 200
            assert dynamic.body == b"dynamic\n"

            removed = await harness.client.set_mapping(
                [MappingRule(public_port=extra_port, local_port=harness.backend.port, host="127.0.0.1")]
            )
            assert removed["ok"] is True
            assert harness.public_port in removed["diff"]["removed"]

            with pytest.raises(OSError):
                await http_request(harness.public_port, "/", timeout=3.0)

            # 保留的那个端口仍然可用
            assert (await http_request(extra_port, "/echo?msg=kept")).body == b"kept\n"

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# 10 ~ 12：边界与自愈
# --------------------------------------------------------------------------- #


def test_404_when_port_has_no_mapping_rule() -> None:
    """404 只会在"连接已建立、映射紧接着被摘掉"的竞态窗口里出现。

    要稳定复现这个窗口，就用一个中转监听把连接直接喂给服务端的访客处理函数——
    除了入参端口是假的，走的是完全真实的处理路径与真实 socket。
    """

    async def scenario() -> None:
        async with TunnelHarness() as harness:
            assert harness.server is not None
            unmapped_port = free_ports(1)[0]

            async def relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
                await harness.server._handle_visitor(reader, writer, unmapped_port)  # type: ignore[union-attr]

            relay_port = free_ports(1)[0]
            relay_server = await asyncio.start_server(relay, "127.0.0.1", relay_port)
            try:
                response = await http_request(relay_port, "/")
                assert response.status == 404
                assert "No mapping for this port" in response.text
            finally:
                relay_server.close()
                await relay_server.wait_closed()

    asyncio.run(scenario())


def test_unmapped_port_is_not_listened_at_all() -> None:
    """没有被映射的端口，服务端根本不会去监听——访问时是连接被拒，而不是 404。"""

    async def scenario() -> None:
        async with TunnelHarness() as harness:
            assert harness.server is not None
            assert harness.server.mapping.listen_ports() == [harness.public_port]

            with pytest.raises(OSError):
                await http_request(free_ports(1)[0], "/", timeout=3.0)

    asyncio.run(scenario())


def test_client_reconnects_after_control_connection_drops() -> None:
    async def scenario() -> None:
        async with TunnelHarness() as harness:
            assert harness.client is not None and harness.server is not None
            assert (await http_request(harness.public_port, "/")).status == 200

            reconnected = asyncio.Event()
            harness.server.events.on(EventType.CLIENT_CONNECTED, lambda **_: reconnected.set())

            # 从服务端一侧粗暴掐断控制连接，模拟网络抖动 / 对端进程被强杀
            session = harness.server.registry.get(harness.client.client_id)
            assert session is not None
            session.writer.close()

            await asyncio.wait_for(reconnected.wait(), timeout=8.0)
            await harness.wait_online(timeout=8.0)

            assert harness.client.stats.reconnects >= 1
            assert harness.server.registry.owner_of(harness.backend.port) == harness.client.client_id

            recovery = await http_request(harness.public_port, "/echo?msg=back")
            assert recovery.status == 200
            assert recovery.body == b"back\n"

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# 13 ~ 16：鉴权与容量语义
#
# 这两类拒绝必须**分开处理**，混同任何一边都是 bug：
#   403 = 凭据不对，重试多少次都一样   → 永久失败，立刻停
#   503 = 容量暂时满了，回头可能就好了 → 暂时失败，继续退避重试
# --------------------------------------------------------------------------- #

AUTH_TOKEN = "s3cret-token"


def _enable_server_auth(config) -> None:
    config.auth.enabled = True
    config.auth.token = AUTH_TOKEN


def test_auth_token_allows_registration_and_traffic() -> None:
    def use_token(config) -> None:
        config.auth_token = AUTH_TOKEN

    async def scenario() -> None:
        async with TunnelHarness(
            configure_server=_enable_server_auth,
            configure_client=use_token,
        ) as harness:
            assert harness.server is not None and harness.client is not None
            assert harness.server.registry.has(harness.client.client_id)
            assert harness.server.registry.owner_of(harness.backend.port) == harness.client.client_id
            assert harness.server.stats.registrations_rejected == 0

            response = await http_request(harness.public_port, "/echo?msg=hello%20auth")
            assert response.status == 200
            assert response.body == b"hello auth\n"

    asyncio.run(scenario())


async def _assert_rejected_permanently(harness: TunnelHarness) -> None:
    """共用的断言体：注册被 403 拒后，客户端必须停手且只拨号过一次。"""
    assert harness.server is not None and harness.client is not None
    client = harness.client

    connects: List[int] = []
    lost: List[Dict[str, Any]] = []
    client.events.on(EventType.CONTROL_CONNECTED, lambda **_: connects.append(1))
    client.events.on(EventType.CONTROL_LOST, lambda **payload: lost.append(payload))

    await harness.wait_until(lambda: client.state == "stopped", what="客户端进入 stopped")

    assert connects == [1], f"403 之后不该再拨号，实际拨号 {len(connects)} 次"
    assert client.stats.reconnects == 0
    assert [event.get("fatal") for event in lost] == [True], f"应恰好有一条 fatal 的 CONTROL_LOST，实际 {lost}"
    reason = str(lost[0].get("reason"))
    assert "403" in reason, f"停止重试的理由应指向 403，实际 {reason!r}"
    assert reason.count("[403]") == 1, f"回执的 msg 已带 code，客户端不该再叠一层前缀：{reason!r}"
    assert harness.server.stats.registrations_rejected == 1
    assert harness.server.registry.has(client.client_id) is False

    # 再等若干个退避周期复检：确认没有"偷偷重试"，而不只是"还没来得及重试"
    await asyncio.sleep(0.4)
    assert client.state == "stopped"
    assert client.stats.reconnects == 0
    assert connects == [1]
    assert harness.server.stats.registrations_rejected == 1


def test_wrong_token_is_rejected_permanently() -> None:
    def use_wrong_token(config) -> None:
        config.auth_token = "definitely-not-the-token"

    async def scenario() -> None:
        async with TunnelHarness(
            configure_server=_enable_server_auth,
            configure_client=use_wrong_token,
            expect_online=False,
        ) as harness:
            await _assert_rejected_permanently(harness)

    asyncio.run(scenario())


def test_missing_token_is_rejected_permanently() -> None:
    """客户端完全不带令牌（``auth_token`` 留空）时，症状必须与令牌错误完全一致。"""

    async def scenario() -> None:
        async with TunnelHarness(
            configure_server=_enable_server_auth,
            expect_online=False,
        ) as harness:
            await _assert_rejected_permanently(harness)

    asyncio.run(scenario())


def test_client_capacity_limit_is_retriable_not_fatal() -> None:
    """503 是"暂时没位置"，不是"你没资格"——客户端必须继续退避重试。"""

    def limit_to_one_client(config) -> None:
        config.limits.max_clients = 1

    async def scenario() -> None:
        async with TunnelHarness(configure_server=limit_to_one_client) as harness:
            assert harness.server is not None and harness.client is not None
            first = harness.client

            second_config = harness.build_client_config(client_id="test-over-capacity")
            second = TunnelClient(second_config)
            lost: List[Dict[str, Any]] = []
            second.events.on(EventType.CONTROL_LOST, lambda **payload: lost.append(payload))
            task = asyncio.create_task(second.run(), name="over-capacity-client")
            try:
                await harness.wait_until(lambda: second.stats.reconnects >= 1, what="第二个客户端退避重试")

                assert second.state != "stopped", "503 不该被当成 fatal 而停止重试"
                assert all(not event.get("fatal") for event in lost), f"不应出现 fatal 事件：{lost}"
                assert harness.server.registry.has("test-over-capacity") is False
                assert harness.server.stats.registrations_rejected >= 1

                # 已在线的那一个完全不受影响
                response = await http_request(harness.public_port, "/echo?msg=still-mine")
                assert response.status == 200
                assert response.body == b"still-mine\n"
                assert harness.server.registry.owner_of(harness.backend.port) == first.client_id
            finally:
                await second.stop()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# 17 ~ 21：生产化（限流 / 配额 / 观测 / 持久化）
# --------------------------------------------------------------------------- #


def test_download_bandwidth_limit_slows_transfer_without_losing_bytes() -> None:
    """限速要真的起作用，且**不许改变字节内容**。

    对照测两条同大小的响应：不限速的那条是基线，限速的那条必须明显更慢。
    只断言"慢了"是不够的——慢有可能是机器卡，有基线对照才说明是限速造成的。
    """
    rate = 64 * 1024  # 64 KB/s

    def throttle(config: ServerConfig) -> None:
        config.limits.per_client_download_bps = rate

    async def scenario() -> None:
        async with TunnelHarness() as baseline_harness:
            started = time.monotonic()
            baseline = await http_request(baseline_harness.public_port, "/big?kb=64", timeout=30.0)
            baseline_cost = time.monotonic() - started
        assert baseline.status == 200
        assert len(baseline.body) == 64 * 1024

        async with TunnelHarness(configure_server=throttle) as harness:
            started = time.monotonic()
            throttled = await http_request(harness.public_port, "/big?kb=64", timeout=30.0)
            cost = time.monotonic() - started

            assert harness.server is not None
            # 桶初始有 16KB 的突发余量，剩余约 48KB 按 64KB/s 排 → 约 0.75s
            assert cost >= 0.5, f"限速后只花了 {cost:.3f}s，限速没起作用"
            assert cost > baseline_cost * 2, (
                f"限速 {cost:.3f}s 与不限速 {baseline_cost:.3f}s 差距太小，无法证明是限速造成的"
            )
            assert harness.server.stats.throttled_seconds >= 0.4
            assert harness.server.stats.bytes_download >= 64 * 1024

        # 限速只影响节奏，不影响内容
        assert throttled.status == 200
        assert len(throttled.body) == 64 * 1024
        assert throttled.body[:1024] == baseline.body[:1024]
        assert throttled.body[-1024:] == baseline.body[-1024:]

    asyncio.run(scenario())


def test_per_client_concurrency_quota_returns_429() -> None:
    """单客户端在途数超上限 → 访客收到 **429**（而不是 502），在途的那个不受影响。"""

    def limit_to_one(config: ServerConfig) -> None:
        config.limits.max_conns_per_client = 1

    async def scenario() -> None:
        async with TunnelHarness(configure_server=limit_to_one) as harness:
            assert harness.server is not None and harness.client is not None
            client_id = harness.client.client_id

            slow = asyncio.create_task(
                http_request(harness.public_port, "/stream?chunks=10&interval=0.1")
            )
            try:
                await harness.wait_until(
                    lambda: harness.server.pending.count_for_client(client_id) >= 1,  # type: ignore[union-attr]
                    what="长请求进入在途",
                )

                rejected = await http_request(harness.public_port, "/echo?msg=nope")

                assert rejected.status == 429
                assert "Too Many Requests" in rejected.raw.decode("latin-1")
                assert "concurrency limit" in rejected.text
                assert "client_id" not in rejected.text, "对外不该泄露内部客户端标识"
                assert harness.server.stats.requests_rejected == 1
                # 配额拒绝不等于转发失败，两者必须分开计数
                assert harness.server.stats.requests_failed == 0
            finally:
                response = await slow
            assert response.status == 200, "已经在一半的那个请求不该被配额牵连"

    asyncio.run(scenario())


def test_stats_expose_online_client_count() -> None:
    async def scenario() -> None:
        async with TunnelHarness() as harness:
            assert harness.server is not None and harness.client is not None
            assert harness.server.stats.clients_online == 1
            assert harness.server.snapshot()["stats"]["clients_online"] == 1

            await harness.client.stop()
            await harness.wait_offline()

            assert harness.server.stats.clients_online == 0
            # 累计计数不受影响，只动 gauge
            assert harness.server.stats.clients_registered == 1

    asyncio.run(scenario())


def test_runtime_mapping_change_is_persisted(tmp_path: Path) -> None:
    """客户端提交的映射变更要**当场落盘**，而不是等进程退出才写。"""
    path = tmp_path / "mappings.json"

    def configure(config: ServerConfig) -> None:
        config.mapping_store.type = "file"
        config.mapping_store.path = str(path)

    async def scenario() -> None:
        async with TunnelHarness(configure_server=configure) as harness:
            assert harness.client is not None
            extra_port = free_ports(1)[0]
            keep = MappingRule(
                public_port=harness.public_port, local_port=harness.backend.port, host="127.0.0.1"
            )
            added = MappingRule(
                public_port=extra_port, local_port=harness.backend.port, host="127.0.0.1"
            )

            result = await harness.client.set_mapping([keep, added])
            assert result["ok"] is True

            persisted = json.loads(path.read_text(encoding="utf-8"))
            assert sorted(item["public_port"] for item in persisted) == sorted(
                [harness.public_port, extra_port]
            )
            assert (await http_request(extra_port, "/echo?msg=on-disk")).body == b"on-disk\n"

    asyncio.run(scenario())


def test_mapping_survives_server_restart(tmp_path: Path) -> None:
    """重启后映射仍在：配置文件里给的是另一个端口，但以持久化文件为准。"""
    persisted_port, seed_port = free_ports(2)
    path = tmp_path / "mappings.json"
    FileMappingStore(path).replace(
        [MappingRule(public_port=persisted_port, local_port=8000, host="127.0.0.1")]
    )

    async def scenario() -> None:
        config = ServerConfig(
            name="restart-test",
            mapping=[MappingRule(public_port=seed_port, local_port=8000, host="127.0.0.1")],
        )
        config.control.host = "127.0.0.1"
        config.data.host = "127.0.0.1"
        config.control.port, config.data.port = free_ports(2)
        config.mapping_store.type = "file"
        config.mapping_store.path = str(path)
        config.validate()

        server = TunnelServer(config)
        await server.start()
        try:
            assert server.mapping.listen_ports() == [persisted_port]
        finally:
            await server.stop()

    asyncio.run(scenario())
