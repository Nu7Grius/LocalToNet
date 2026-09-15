# -*- coding: utf-8 -*-
"""
tests/test_visitor_tls.py —— 访客端口可选 TLS 终止（per-port 开关，默认明文）
==============================================================================
这是**扩展**，不在原始需求文档里，范围由用户拍板：访客端口（公网入口那一跳）
可以逐端口选择是否由服务端做 TLS 终止，默认明文；证书字段独立、**绝不回落**到
``tls.cert``；开关可热切，证书路径仅重启生效。

链路回顾（本文件守的是第①跳）::

    访客 --① 本次实施，可选 TLS--> 服务端 --② 上一轮已做--> 客户端 --③ 永远明文--> 内网后端

本文件守六条线：

1. **正向**：per-port 开了 TLS 的访客端口能正常往返；TLS 端口与明文端口可以并存于
   同一张映射表；大流量不丢字节。
2. **证伪**：明文访客打 TLS 端口、TLS 访客打明文端口，都必须拿不到数据——
   否则"配置里多了几个字段但连接还走明文"这种半成品会蒙混过关。
3. **默认行为一字不变**：不配任何 visitor 字段就是全明文；手写缺 ``tls`` 键的老
   ``mappings.json`` 照旧能加载。
4. **回滚安全**：``MappingRule.to_dict()`` 不能把 ``tls=None`` 写进文件——
   写了它，文件拿回旧版本会被 ``_check_unknown`` 直接拒绝启动。
5. **不静默降级**：证书缺失一律报错，per-port 开了 TLS 却没证书必须**启动失败**，
   绝不能悄悄退回明文。
6. **热切换**：切 ``tls`` 要真的重建监听（``diff.changed`` 有它），且缺证书时
   **在停监听之前**就被拒——不留"旧已停、新没起"的不一致。

证书沿用 ``tests/certs/``（SAN 含 ``IP:127.0.0.1``），不另造一套。
"""

from __future__ import annotations

import asyncio
import json
import ssl
from pathlib import Path
from typing import Any, Dict, List

import pytest

from config import ConfigError, MappingRule, ServerTlsConfig
from localtonet.core.events import EventType
from localtonet.server.mapping import FileMappingStore
from tests.helpers import (
    HttpResponse,
    TunnelHarness,
    cert_path,
    close_quietly,
    free_ports,
    http_request,
    key_path,
    parse_response,
)

REQUEST_TIMEOUT = 15.0
"""访客侧读写超时。比 harness 里配的 pair_timeout 宽裕，避免测试自己先超时。"""


# --------------------------------------------------------------------------- #
# 访客侧工具：本文件刻意不复用 helpers.http_request，因为它只会发明文
# --------------------------------------------------------------------------- #


def _visitor_ssl_context() -> ssl.SSLContext:
    """访客（浏览器）那一侧的上下文：信任测试 CA，并校验主机名 ``127.0.0.1``。"""
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=cert_path("ca"))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def _http_payload(path: str) -> bytes:
    return (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: 127.0.0.1\r\n"
        f"User-Agent: localtonet-tests\r\n"
        f"Connection: close\r\n"
        f"\r\n"
    ).encode("latin-1")


async def _read_all(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    payload: bytes,
    *,
    tolerant: bool,
) -> bytes:
    chunks: List[bytes] = []
    try:
        writer.write(payload)
        await writer.drain()
        while True:
            piece = await asyncio.wait_for(reader.read(65536), timeout=REQUEST_TIMEOUT)
            if not piece:
                break
            chunks.append(piece)
    except (OSError, asyncio.TimeoutError):
        # 错配场景下"读不到东西"正是要断言的结果，不能让它变成测试错误
        if not tolerant:
            raise
    finally:
        await close_quietly(writer)
    return b"".join(chunks)


async def _visitor_exchange(port: int, payload: bytes, *, tolerant: bool = False) -> bytes:
    """走 TLS 连访客端口。``tolerant=True`` 时把握手失败也当作"拿不到数据"。"""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", port, ssl=_visitor_ssl_context(), server_hostname="127.0.0.1"),
            REQUEST_TIMEOUT,
        )
    except (OSError, asyncio.TimeoutError):  # ssl.SSLError 是 OSError 子类
        if not tolerant:
            raise
        return b""
    return await _read_all(reader, writer, payload, tolerant=tolerant)


async def _plain_exchange(port: int, payload: bytes, *, tolerant: bool = False) -> bytes:
    """走明文连访客端口。"""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", port), REQUEST_TIMEOUT
        )
    except (OSError, asyncio.TimeoutError):
        if not tolerant:
            raise
        return b""
    return await _read_all(reader, writer, payload, tolerant=tolerant)


async def _tls_get(port: int, path: str) -> HttpResponse:
    return parse_response(await _visitor_exchange(port, _http_payload(path)))


# --------------------------------------------------------------------------- #
# 配置注入辅助
# --------------------------------------------------------------------------- #


def _visitor_certs_only(config: Any) -> None:
    """只装访客证书，**不**打开全局默认（``visitor_enabled`` 保持 false）。

    这正是 per-port 用法：证书齐备（"能用"），但默认明文（"不默认用"）。
    """
    config.tls = ServerTlsConfig(
        visitor_cert=cert_path("server"),
        visitor_key=key_path("server"),
        handshake_timeout=2.0,
    )


def _per_port_tls_true(config: Any) -> None:
    config.mapping[0].tls = True


# --------------------------------------------------------------------------- #
# 1. 正向：TLS 访客端口能通
# --------------------------------------------------------------------------- #


def test_visitor_tls_end_to_end_round_trip() -> None:
    """per-port 开 TLS 后，TLS 访客请求能穿越三跳拿到后端响应。"""

    async def scenario() -> None:
        async with TunnelHarness(
            configure_server=lambda c: (_visitor_certs_only(c), _per_port_tls_true(c)),
        ) as harness:
            response = await _tls_get(harness.public_port, "/echo?msg=visitor-tls-ok")
            assert response.status == 200
            assert response.body == b"visitor-tls-ok\n"

    asyncio.run(scenario())


def test_tls_and_plaintext_ports_coexist_in_one_table() -> None:
    """同一张映射表里 TLS 端口与明文端口并存，各自用自己的协议都能通。

    两条规则指向同一个内网端口：TLS 的不该影响明文的，反之亦然。
    """
    extra_port_holder: List[int] = []

    def configure(config: Any) -> None:
        _visitor_certs_only(config)
        _per_port_tls_true(config)
        # 新增一条**明文**规则，指向同一个内网后端。端口要避开已占用的三个，
        # 否则 config.validate() 会以"重复/与保留端口冲突"报错。
        taken = {config.control.port, config.data.port, config.mapping[0].public_port}
        candidate = free_ports(1)[0]
        while candidate in taken:
            candidate = free_ports(1)[0]
        extra_port_holder.append(candidate)
        config.mapping.append(
            MappingRule(
                public_port=candidate,
                local_port=config.mapping[0].local_port,
                host="127.0.0.1",
                local_host="127.0.0.1",
                remark="plaintext",
                tls=False,
            )
        )

    async def scenario() -> None:
        async with TunnelHarness(configure_server=configure) as harness:
            tls_response = await _tls_get(harness.public_port, "/echo?msg=via-tls")
            assert tls_response.status == 200
            assert tls_response.body == b"via-tls\n"

            plain_response = await http_request(extra_port_holder[0], "/echo?msg=via-plain")
            assert plain_response.status == 200
            assert plain_response.body == b"via-plain\n"

    asyncio.run(scenario())


def test_visitor_tls_large_transfer_no_byte_loss() -> None:
    """TLS 访客端口下大流量不丢字节——证 TLS 包装没吃掉或重排字节。"""

    async def scenario() -> None:
        async with TunnelHarness(
            configure_server=lambda c: (_visitor_certs_only(c), _per_port_tls_true(c)),
        ) as harness:
            response = await _tls_get(harness.public_port, "/big?kb=512")
            assert response.status == 200
            assert len(response.body) == 512 * 1024

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# 2. 证伪：配了却没起作用必须暴露
# --------------------------------------------------------------------------- #


def test_plaintext_visitor_on_tls_port_gets_nothing() -> None:
    """端口开了 TLS，明文访客必须拿不到数据。

    这是"TLS 真的生效"的证伪用例：若实现只是"配置里多了几个字段但连接还走明文"，
    这里会立刻读到 200 + 回显，用例变红。
    """

    async def scenario() -> None:
        async with TunnelHarness(
            configure_server=lambda c: (_visitor_certs_only(c), _per_port_tls_true(c)),
        ) as harness:
            data = await _plain_exchange(
                harness.public_port, _http_payload("/echo?msg=wrong-proto"), tolerant=True
            )
            assert b"wrong-proto" not in data
            assert not data.startswith(b"HTTP/")

    asyncio.run(scenario())


def test_tls_visitor_on_plaintext_port_gets_nothing() -> None:
    """端口是明文时，TLS 访客也拿不到数据（握手根本完不成）。

    与上一条互为反向：默认明文不是"顺便也接受 TLS"，而是只有明文这一条路。
    """

    async def scenario() -> None:
        async with TunnelHarness() as harness:  # 默认：无访客证书、全明文
            data = await _visitor_exchange(
                harness.public_port, _http_payload("/echo?msg=wrong-proto"), tolerant=True
            )
            assert b"wrong-proto" not in data
            assert not data.startswith(b"HTTP/")

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# 3. 默认行为：不配就是明文，与 beaaf5f 一字不差
# --------------------------------------------------------------------------- #


def test_visitor_tls_is_off_by_default() -> None:
    """不配任何 visitor 字段 → 明文访客正常，TLS 访客拿不到东西。"""

    async def scenario() -> None:
        async with TunnelHarness() as harness:
            response = await http_request(harness.public_port, "/echo?msg=default-plain")
            assert response.status == 200
            assert response.body == b"default-plain\n"
            assert await _visitor_exchange(harness.public_port, _http_payload("/"), tolerant=True) == b""

    asyncio.run(scenario())


def test_old_mappings_json_without_tls_key_still_loads(tmp_path: Path) -> None:
    """手写缺 ``tls`` 键的老 ``mappings.json`` 必须照旧能加载（兼容回归）。

    老文件里没有这个键 → 解析成 ``None`` → 跟随默认（false＝明文），行为一字不变。
    这是"升级不改行为"的硬要求。
    """
    legacy = [
        {"public_port": 9101, "local_port": 8101, "host": "0.0.0.0", "local_host": "127.0.0.1", "remark": ""},
        {"public_port": 9102, "local_port": 8102, "host": "127.0.0.1", "local_host": "127.0.0.1", "remark": "x"},
    ]
    path = tmp_path / "legacy-mappings.json"
    path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")

    store = FileMappingStore(path)
    rules = store.all()

    assert [rule.public_port for rule in rules] == [9101, 9102]
    assert all(rule.tls is None for rule in rules), "缺字段必须解析成 None（跟随默认），不是 False"

    # 回写也不该把 tls 键带进文件，否则老文件被"升级"污染
    store.replace(rules)
    written = json.loads(path.read_text(encoding="utf-8"))
    assert all("tls" not in item for item in written)


# --------------------------------------------------------------------------- #
# 4. 回滚安全：to_dict 不能写出 tls=None
# --------------------------------------------------------------------------- #


def test_mapping_rule_to_dict_omits_tls_when_none() -> None:
    """``tls=None`` 不落盘、``tls=False`` 必须落盘。

    前者是为了让新版本写出的文件还能被 ``beaaf5f`` 读取（老版本的
    ``_check_unknown`` 只拒绝"多出来的键"）；后者是"显式关"表态，
    丢了它就会在全局默认打开时静默变成 TLS。
    """
    assert "tls" not in MappingRule(9028, 8000).to_dict()

    explicit_on = MappingRule(9028, 8000, tls=True).to_dict()
    assert explicit_on["tls"] is True

    explicit_off = MappingRule(9028, 8000, tls=False).to_dict()
    assert "tls" in explicit_off, "False 是显式表态，不能当 None 一起丢掉"
    assert explicit_off["tls"] is False


# --------------------------------------------------------------------------- #
# 5. 校验：缺证书一律报错，不静默降级
# --------------------------------------------------------------------------- #


def test_visitor_enabled_without_certs_is_rejected() -> None:
    """``visitor_enabled=true`` 却没给证书 → 配置阶段就报错。"""
    with pytest.raises(ConfigError) as excinfo:
        ServerTlsConfig(visitor_enabled=True).validate()

    assert "visitor_cert" in str(excinfo.value)


def test_visitor_cert_and_key_must_be_paired() -> None:
    """证书与私钥必须成对；给一个不给另一个 → 报错（两个方向都测）。"""
    with pytest.raises(ConfigError) as only_cert:
        ServerTlsConfig(visitor_cert=cert_path("server")).validate()
    assert "成对" in str(only_cert.value)

    with pytest.raises(ConfigError) as only_key:
        ServerTlsConfig(visitor_key=key_path("server")).validate()
    assert "成对" in str(only_key.value)


def test_per_port_tls_without_certs_fails_startup() -> None:
    """per-port ``tls=true`` 但没配访客证书 → **启动失败**，而不是静默明文。

    同时钉死"失败得干净"：一个监听都不能留下（``listen_ports()`` 为空）。
    """

    async def scenario() -> None:
        harness = TunnelHarness(
            configure_server=_per_port_tls_true,
            expect_online=False,
        )
        with pytest.raises(ConfigError):
            await harness.start()
        assert harness.server is not None
        assert harness.server.mapping.listen_ports() == [], "校验失败不该留下半启动的监听"
        await harness.stop()

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# 6. 热切换：开关改了要真生效
# --------------------------------------------------------------------------- #


def test_toggling_tls_rebuilds_listener_and_flips_protocol() -> None:
    """把端口从明文切到 TLS：``diff.changed`` 要有它，且握手协议真的换了。

    只断言 diff 不够——``_listener_changed`` 漏了 TLS 这一项时 diff 不报、
    监听不重建，端口仍是明文，用户以为切成功。所以必须实测协议。
    """

    async def scenario() -> None:
        async with TunnelHarness(configure_server=_visitor_certs_only) as harness:
            plain_before = await http_request(harness.public_port, "/echo?msg=before")
            assert plain_before.body == b"before\n"

            changes: List[Dict[str, Any]] = []
            harness.server.events.on(EventType.MAPPING_CHANGED, lambda **kw: changes.append(kw))

            target = MappingRule(
                public_port=harness.public_port,
                local_port=harness.backend.port,
                host="127.0.0.1",
                local_host="127.0.0.1",
                tls=True,
            )
            diff = await harness.server.mapping.apply([target])

            assert diff.changed == [harness.public_port], "切 TLS 必须被算成「需要重建监听」"
            assert changes and harness.public_port in changes[-1]["diff"]["changed"]

            # 协议真的换了：TLS 能通，明文拿不到东西
            response = await _tls_get(harness.public_port, "/echo?msg=after-tls")
            assert response.status == 200
            assert response.body == b"after-tls\n"

            data = await _plain_exchange(
                harness.public_port, _http_payload("/echo?msg=after-plain"), tolerant=True
            )
            assert b"after-plain" not in data

    asyncio.run(scenario())


def test_missing_certs_rejected_before_listener_is_stopped() -> None:
    """缺证书的更新必须在**停监听之前**被拒，端口仍然可用。

    ``apply()`` 的 ``except`` 只捕 ``OSError``；若让 ``ConfigError`` 从 ``_listen``
    穿透出去，端口会先被停掉、store 已被替换、新监听没起——最难查的那种中间态。
    """

    async def scenario() -> None:
        # 完全不配访客证书：start 走明文（合法），随后想切成 TLS 就必须被拒
        async with TunnelHarness() as harness:
            target = MappingRule(
                public_port=harness.public_port,
                local_port=harness.backend.port,
                host="127.0.0.1",
                local_host="127.0.0.1",
                tls=True,
            )
            with pytest.raises(ConfigError):
                await harness.server.mapping.apply([target])

            # 端口还在监听，而且是明文——拒绝之后什么都没变
            assert harness.public_port in harness.server.mapping.listen_ports()
            response = await http_request(harness.public_port, "/echo?msg=still-alive")
            assert response.status == 200
            assert response.body == b"still-alive\n"

    asyncio.run(scenario())
