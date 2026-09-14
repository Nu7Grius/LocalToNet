# -*- coding: utf-8 -*-
"""
tests/test_tls.py —— 传输加密（TLS）
=====================================
TLS 是**扩展**，不在原始需求文档里，范围由用户拍板：本轮只加密「控制 + 数据」两跳，
访客端口 TLS 终止留作下一轮；默认单向认证、mTLS 可选；协议帧格式零改动。

本文件守四条验收线：

1. **明文与 TLS 两套端到端用例并存且都绿**——加密不取代明文，明文路径一行没删。
2. **证伪用例**：服务端只开 TLS 时，明文客户端必须连不上（证明加密真在生效，
   而不是"配了但没起作用"）。
3. **证书校验失败必须显式报错、不静默降级**；``skip_verify`` 必须显式开启才生效。
4. **TLS 握手失败 = 永久性失败**：客户端立即停手、退出码 1，不再退避重试。

证书来自 ``tests/certs/``（一次性材料，见其 README）。``server-badhost`` 的 SAN 只写
``DNS:localto.net.invalid``，用来造"证书链有效、主机名不匹配"这一种校验失败，
它和"自签但不给 CA"（证书链不受信任）是**不同层级**的失败，两种都要测。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from config import ClientTlsConfig, ServerTlsConfig
from localtonet.client.core import TunnelClient
from localtonet.core.events import EventType
from localtonet.server.core import TunnelServer
from tests.helpers import TunnelHarness, cert_path, http_request, key_path


def _server_tls() -> ServerTlsConfig:
    return ServerTlsConfig(
        enabled=True,
        cert=cert_path("server"),
        key=key_path("server"),
        handshake_timeout=2.0,
    )


def _client_tls() -> ClientTlsConfig:
    return ClientTlsConfig(enabled=True, ca=cert_path("ca"))


# --------------------------------------------------------------------------- #
# 正向：TLS 下三通道全通
# --------------------------------------------------------------------------- #


def test_tls_end_to_end_round_trip() -> None:
    """服务端与客户端都开 TLS 时，访客请求能正常穿越（控制 + 数据两跳都加密）。"""

    async def scenario() -> None:
        async with TunnelHarness(
            configure_server=lambda c: setattr(c, "tls", _server_tls()),
            configure_client=lambda c: setattr(c, "tls", _client_tls()),
        ) as harness:
            response = await http_request(harness.public_port, "/echo?msg=tls-ok")
            assert response.status == 200
            assert response.body == b"tls-ok\n"

    asyncio.run(scenario())


def test_tls_large_transfer_no_byte_loss() -> None:
    """TLS 下大流量转发不丢字节——证数据通道的加密包装没有吃掉/重排字节。"""

    async def scenario() -> None:
        async with TunnelHarness(
            configure_server=lambda c: setattr(c, "tls", _server_tls()),
            configure_client=lambda c: setattr(c, "tls", _client_tls()),
        ) as harness:
            response = await http_request(harness.public_port, "/big?kb=512")
            assert response.status == 200
            assert len(response.body) == 512 * 1024

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# 证伪：配了却没起作用必须暴露
# --------------------------------------------------------------------------- #


def test_plaintext_client_cannot_reach_tls_server() -> None:
    """服务端只开 TLS 时，**明文**客户端必须连不上。

    这是"加密真的生效"的证伪用例：若实现只是"配置里多几个字段但连接仍走明文"，
    这个用例立刻会红。

    注意断言的是"**上不了线**"而不是"客户端一定 stopped"：明文客户端不知道对端是
    TLS，它只知道"连不上"，表现为退避重试或协议错误——这本身是正确语义。
    真正的判据是服务端 registry 里**始终没有**这个客户端。
    """

    async def scenario() -> None:
        async with TunnelHarness(
            configure_server=lambda c: setattr(c, "tls", _server_tls()),
            configure_client=lambda c: setattr(c, "tls", ClientTlsConfig()),  # 明文客户端
            expect_online=False,
        ) as harness:
            # 明文流量被 TLS 服务端拒之门外：无论客户端重试多少次，服务端都不该登记它
            await asyncio.sleep(0.5)  # 给客户端几次退避重试的机会
            assert harness.client is not None
            assert harness.server.registry.get(harness.client.client_id) is None
            # 客户端侧佐证：它从未进入 online 状态（控制通道都通不过）
            assert harness.client.state != "online"

    asyncio.run(scenario())


def test_client_plaintext_server_and_tls_client_fatal() -> None:
    """反向：服务端明文、客户端配了 TLS 也会失败并停手（证书/协议对不上）。"""
    events: list = []

    async def scenario() -> None:
        async with TunnelHarness(
            configure_client=lambda c: setattr(c, "tls", _client_tls()),
            expect_online=False,
        ) as harness:
            harness.client.events.on(EventType.CONTROL_LOST, lambda **kw: events.append(kw))
            await harness.wait_until(lambda: harness.client.state == "stopped", what="客户端停止")
            assert harness.client.fatal is True
            assert harness.client.stats.reconnects == 0

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# 证书校验失败必须显式报错、不静默降级
# --------------------------------------------------------------------------- #


def test_unknown_ca_is_rejected_not_silently_downgraded() -> None:
    """自签服务端证书 + 客户端**不给** CA → 必须失败，绝不能静默降级成明文。"""
    events: list = []

    async def scenario() -> None:
        async with TunnelHarness(
            configure_server=lambda c: setattr(c, "tls", _server_tls()),
            # 客户端开 TLS 但不给 CA：走系统信任库，自签证书必然不被信任
            configure_client=lambda c: setattr(
                c, "tls", ClientTlsConfig(enabled=True, ca="")
            ),
            expect_online=False,
        ) as harness:
            harness.client.events.on(EventType.CONTROL_LOST, lambda **kw: events.append(kw))
            await harness.wait_until(lambda: harness.client.state == "stopped", what="客户端停止")
            assert harness.client.fatal is True
            # 失败原因必须点破是证书/握手，而不是笼统的"连接失败"
            assert events and "TLS" in events[0].get("reason", "")

    asyncio.run(scenario())


def test_hostname_mismatch_is_rejected() -> None:
    """证书链有效但主机名不匹配（连 127.0.0.1，证书 SAN 是 localto.net.invalid）→ 失败。"""

    async def scenario() -> None:
        # 服务端用 badhost 证书，客户端信任 CA 但 hostname 对不上
        async with TunnelHarness(
            configure_server=lambda c: setattr(
                c,
                "tls",
                ServerTlsConfig(
                    enabled=True,
                    cert=cert_path("server-badhost"),
                    key=key_path("server-badhost"),
                    handshake_timeout=2.0,
                ),
            ),
            configure_client=lambda c: setattr(c, "tls", _client_tls()),
            expect_online=False,
        ) as harness:
            await harness.wait_until(lambda: harness.client.state == "stopped", what="客户端停止")
            assert harness.client.fatal is True

    asyncio.run(scenario())


def test_skip_verify_must_be_explicit_to_work() -> None:
    """``skip_verify`` 显式开启后，badhost 证书才能连上——证明它是"显式逃生门"。"""

    async def scenario() -> None:
        async with TunnelHarness(
            configure_server=lambda c: setattr(
                c,
                "tls",
                ServerTlsConfig(
                    enabled=True,
                    cert=cert_path("server-badhost"),
                    key=key_path("server-badhost"),
                    handshake_timeout=2.0,
                ),
            ),
            configure_client=lambda c: setattr(
                c,
                "tls",
                ClientTlsConfig(enabled=True, ca=cert_path("ca"), skip_verify=True, check_hostname=False),
            ),
        ) as harness:
            response = await http_request(harness.public_port, "/echo?msg=skip-ok")
            assert response.status == 200
            assert response.body == b"skip-ok\n"

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# mTLS：双向认证
# --------------------------------------------------------------------------- #


def test_mtls_requires_client_certificate() -> None:
    """服务端要求客户端证书时，客户端出示正确证书能连上。"""

    async def scenario() -> None:
        server_tls = ServerTlsConfig(
            enabled=True,
            cert=cert_path("server"),
            key=key_path("server"),
            require_client_cert=True,
            client_ca=cert_path("ca"),
            handshake_timeout=2.0,
        )
        client_tls = ClientTlsConfig(
            enabled=True,
            ca=cert_path("ca"),
            cert=cert_path("client"),
            key=key_path("client"),
        )
        async with TunnelHarness(
            configure_server=lambda c: setattr(c, "tls", server_tls),
            configure_client=lambda c: setattr(c, "tls", client_tls),
        ) as harness:
            response = await http_request(harness.public_port, "/echo?msg=mtls-ok")
            assert response.status == 200
            assert response.body == b"mtls-ok\n"

    asyncio.run(scenario())


def test_mtls_without_client_certificate_is_rejected() -> None:
    """服务端要求客户端证书，但客户端没配证书 → 握手失败、停手。"""
    events: list = []

    async def scenario() -> None:
        server_tls = ServerTlsConfig(
            enabled=True,
            cert=cert_path("server"),
            key=key_path("server"),
            require_client_cert=True,
            client_ca=cert_path("ca"),
            handshake_timeout=2.0,
        )
        async with TunnelHarness(
            configure_server=lambda c: setattr(c, "tls", server_tls),
            configure_client=lambda c: setattr(c, "tls", _client_tls()),  # 只验服务端，不带自己证书
            expect_online=False,
        ) as harness:
            harness.client.events.on(EventType.CONTROL_LOST, lambda **kw: events.append(kw))
            await harness.wait_until(lambda: harness.client.state == "stopped", what="客户端停止")
            assert harness.client.fatal is True

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# 默认关闭：不配就是明文，与 TLS 落地前一致
# --------------------------------------------------------------------------- #


def test_tls_disabled_by_default() -> None:
    """不配任何 TLS 字段时，服务端/客户端上下文都是 None（明文），行为不变。"""
    from localtonet.core.tls import build_client_context, build_server_context

    assert build_server_context(ServerTlsConfig()) is None
    assert build_client_context(ClientTlsConfig()) is None
