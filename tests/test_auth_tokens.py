# -*- coding: utf-8 -*-
"""
tests/test_auth_tokens.py —— 鉴权二期：令牌表 + 按内网端口授权 + 文件热重载
============================================================================
鉴权二期的需求是**扩展**（不在 `内网穿透工具-代码分析.md` 里），范围由用户拍板：

把"一个共享令牌，谁拿到都能认领任意端口，换令牌要重启"升级为
"一张令牌表：每个令牌带身份与允许认领的内网端口；改文件即生效，无需重启"。

本文件守五条验收线，每条都比"能跑通"更严格：

1. **兼容**：不配 ``auth.file`` 时行为与鉴权一期一字不差（单令牌路径一行没删）。
2. **授权粒度是内网端口**：未授权 → **整体拒绝**注册（不静默丢弃、不部分接受）。
3. **热重载闭环**：未授权时客户端继续退避重试，运维改完令牌文件后**同一个客户端进程**
   自动上车——这条是本轮唯一能证明"闭环真的闭合"的用例，不是只断言一个字段。
4. **安全铁律**：重载失败（损坏 / 消失）**保留旧表**，绝不降级为放行；
   令牌明文**一次都不出现**在日志、事件与 ``ClientSession.register_msg`` 里。
5. **时序侧信道**：令牌比较必须走完全部条目（含被吊销的），用假比较函数数调用次数来钉。

全程用 ``tests.helpers.TunnelHarness`` 拉真实三件套走真实 TCP，不 mock。
注册注定失败的用例一律 ``expect_online=False``，断言一律"轮询到达条件 + 死线"，
**不写 sleep 一觉再断言**（Windows sleep 粒度约 15ms，全量跑会偶发红）。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional, Sequence

import pytest

from config import AuthConfig, ConfigError, MappingRule, ServerConfig
from localtonet.client.core import TunnelClient
from localtonet.core.events import EventType
from localtonet.errors import AuthError
from localtonet.server.auth import (
    Identity,
    LegacyTokenAuthenticator,
    NoneAuthenticator,
    TokenAuthenticator,
    TokenFileAuthenticator,
    build_authenticator,
)
from localtonet.server.core import TunnelServer
from localtonet.server.tokenstore import TokenStore, compare_secret
from tests.helpers import TunnelHarness, free_ports, http_request

ALICE = "alice-token-1a2b"
BOB = "bob-token-3c4d"
CAROL = "carol-token-5e6f"


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #


def write_table(path: Path, *entries: Dict[str, Any], version: int = 1) -> Path:
    """写一份令牌表文件。返回路径方便链式调用。"""
    payload = {"version": version, "tokens": list(entries)}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def sha256_of(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def auth_file_server(table: Path):
    """服务端配置回调：开启令牌表鉴权。"""

    def configure(config: ServerConfig) -> None:
        config.auth.enabled = True
        config.auth.file = str(table)

    return configure


def token_client(token: str, ports: Optional[Sequence[int]] = None):
    """客户端配置回调：带令牌（可选覆盖声明端口）。"""

    def configure(config: Any) -> None:
        config.auth_token = token
        if ports is not None:
            config.local_ports = list(ports)

    return configure


@contextlib.asynccontextmanager
async def extra_client(
    harness: TunnelHarness,
    *,
    client_id: str,
    token: str,
    local_ports: Optional[Sequence[int]] = None,
) -> AsyncIterator[TunnelClient]:
    """在已有 harness 上再挂一个客户端，用来测多身份 / 授权 / 热重载。

    刻意不走 ``harness.start_client``：那只保留一个客户端引用，起第二个会把它顶掉。
    这里自己建 ``TunnelClient`` 并负责收尾，harness 只提供对齐的配置模板。
    """
    config = harness.build_client_config(client_id=client_id, local_ports=local_ports)
    config.auth_token = token
    client = TunnelClient(config)
    task = asyncio.create_task(client.run(), name=f"extra-client-{client_id}")
    try:
        yield client
    finally:
        await client.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@contextlib.contextmanager
def capture_logs() -> Any:
    """抓取 ``localtonet`` 层级下的**全部**日志文本（含 DEBUG 与异常栈）。

    挂在父日志器上而不是 root：``setup_logging`` 会把 ``localtonet.propagate`` 置 False，
    挂 root 会漏。所有子日志器默认向上冒泡到 ``localtonet``，所以这里能收全。
    """
    captured: List[str] = []

    class _Sink(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(self.format(record))

    sink = _Sink()
    sink.setLevel(logging.DEBUG)
    sink.setFormatter(logging.Formatter("%(levelname)s [%(name)s] %(message)s"))

    logger = logging.getLogger("localtonet")
    previous_level = logger.level
    logger.addHandler(sink)
    logger.setLevel(logging.DEBUG)
    try:
        yield captured
    finally:
        logger.removeHandler(sink)
        logger.setLevel(previous_level)


# --------------------------------------------------------------------------- #
# 兼容：不配 auth.file 时行为与鉴权一期一字不差
# --------------------------------------------------------------------------- #


def test_shared_token_still_works_end_to_end() -> None:
    """单令牌路径（``auth.token``）**必须**全链路照旧——本轮一行没删。"""

    def server_cfg(config: ServerConfig) -> None:
        config.auth.enabled = True
        config.auth.token = "legacy-shared"

    async def scenario() -> None:
        async with TunnelHarness(
            configure_server=server_cfg,
            configure_client=token_client("legacy-shared"),
        ) as harness:
            assert harness.server is not None and harness.client is not None
            session = harness.server.registry.get(harness.client.client_id)
            assert session is not None
            assert session.identity == "shared"
            assert harness.server.stats.registrations_rejected == 0

            response = await http_request(harness.public_port, "/echo?msg=legacy")
            assert response.status == 200
            assert response.body == b"legacy\n"

    asyncio.run(scenario())


def test_build_authenticator_selects_three_implementations(tmp_path: Path) -> None:
    """三种配置各选对一个类；``auth.file`` 优先于 ``auth.token``（互斥由 validate 兜底）。"""
    table = write_table(tmp_path / "tokens.json", {"name": "alice", "token": ALICE})

    assert isinstance(build_authenticator(None), NoneAuthenticator)
    assert isinstance(build_authenticator(AuthConfig(enabled=False, token="x")), NoneAuthenticator)
    assert isinstance(build_authenticator(AuthConfig(enabled=True, token="x")), LegacyTokenAuthenticator)
    assert isinstance(
        build_authenticator(AuthConfig(enabled=True, file=str(table))), TokenFileAuthenticator
    )


def test_legacy_class_name_alias_survives() -> None:
    """旧类名 ``TokenAuthenticator`` 必须仍是同一个对象，外部 import 不许碎。"""
    assert TokenAuthenticator is LegacyTokenAuthenticator
    assert TokenAuthenticator("x").verify({"token": "x"}, "p").name == "shared"


# --------------------------------------------------------------------------- #
# 正向：多令牌、端口白名单、client_id 绑定
# --------------------------------------------------------------------------- #


def test_two_tokens_connect_with_distinct_identities(tmp_path: Path) -> None:
    table = write_table(
        tmp_path / "tokens.json",
        {"name": "alice", "token": ALICE},
        {"name": "bob", "token": BOB},
    )
    bob_port = free_ports(1)[0]

    async def scenario() -> None:
        async with TunnelHarness(
            configure_server=auth_file_server(table),
            configure_client=token_client(ALICE),
        ) as harness:
            assert harness.server is not None and harness.client is not None
            alice = harness.server.registry.get(harness.client.client_id)
            assert alice is not None and alice.identity == "alice"

            async with extra_client(
                harness, client_id="bob-1", token=BOB, local_ports=[bob_port]
            ) as bob:
                await harness.wait_until(
                    lambda: harness.server is not None and harness.server.registry.has("bob-1"),
                    what="bob 完成注册",
                )
                bob_session = harness.server.registry.get("bob-1")
                assert bob_session is not None
                assert bob_session.identity == "bob"
                assert harness.server.registry.owner_of(bob_port) == "bob-1"
                assert harness.server.registry.client_count == 2
                assert bob.client_id == "bob-1"

    asyncio.run(scenario())


def test_ports_whitelist_allows_claim(tmp_path: Path) -> None:
    allowed = free_ports(1)[0]
    table = write_table(
        tmp_path / "tokens.json",
        {"name": "alice", "token": ALICE, "ports": [allowed]},
    )

    async def scenario() -> None:
        async with TunnelHarness(
            configure_server=auth_file_server(table),
            configure_client=token_client(ALICE, ports=[allowed]),
        ) as harness:
            assert harness.server is not None and harness.client is not None
            assert harness.server.registry.owner_of(allowed) == harness.client.client_id

    asyncio.run(scenario())


def test_empty_ports_means_unlimited(tmp_path: Path) -> None:
    """``ports: []`` ＝不限（与 ``limits`` 的 ``0`` ＝不限一脉相承），**不是**禁止。"""
    table = write_table(tmp_path / "tokens.json", {"name": "alice", "token": ALICE, "ports": []})

    async def scenario() -> None:
        async with TunnelHarness(
            configure_server=auth_file_server(table),
            configure_client=token_client(ALICE),
        ) as harness:
            assert harness.server is not None and harness.client is not None
            # 认领的是 harness 后端端口，属于"没被列进 ports"的端口，照样通过
            assert harness.server.registry.owner_of(harness.backend.port) == harness.client.client_id

    asyncio.run(scenario())


def test_client_id_binding_passes_when_it_matches(tmp_path: Path) -> None:
    table = write_table(
        tmp_path / "tokens.json",
        {"name": "alice", "token": ALICE, "client_id": "pinned-alice"},
    )

    async def scenario() -> None:
        async with TunnelHarness(
            client_id="pinned-alice",
            configure_server=auth_file_server(table),
            configure_client=token_client(ALICE),
        ) as harness:
            assert harness.server.registry.has("pinned-alice")
            session = harness.server.registry.get("pinned-alice")
            assert session is not None and session.identity == "alice"

    asyncio.run(scenario())


def test_sha256_token_is_accepted(tmp_path: Path) -> None:
    """``token_sha256`` 是推荐的生产存法（文件里不落明文）。"""
    table = write_table(
        tmp_path / "tokens.json",
        {"name": "bob", "token_sha256": sha256_of(BOB)},
    )

    async def scenario() -> None:
        async with TunnelHarness(
            configure_server=auth_file_server(table),
            configure_client=token_client(BOB),
        ) as harness:
            assert harness.server is not None and harness.client is not None
            session = harness.server.registry.get(harness.client.client_id)
            assert session is not None and session.identity == "bob"

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# 证伪：无效 / 吊销 / 冒充 —— 一律**永久**失败，客户端立刻停手
# --------------------------------------------------------------------------- #


async def _assert_rejected_permanently(harness: TunnelHarness, *, expect_reason: str = "403") -> None:
    """共用断言体：被永久拒绝后客户端必须停手，且只拨号过一次。"""
    assert harness.server is not None and harness.client is not None
    client = harness.client

    connects: List[int] = []
    lost: List[Dict[str, Any]] = []
    client.events.on(EventType.CONTROL_CONNECTED, lambda **_: connects.append(1))
    client.events.on(EventType.CONTROL_LOST, lambda **payload: lost.append(payload))

    await harness.wait_until(lambda: client.state == "stopped", what="客户端进入 stopped")

    assert connects == [1], f"永久拒绝之后不该再拨号，实际拨号 {len(connects)} 次"
    assert client.stats.reconnects == 0
    assert [event.get("fatal") for event in lost] == [True], f"应恰好一条 fatal 的 CONTROL_LOST：{lost}"
    assert expect_reason in str(lost[0].get("reason"))
    assert harness.server.stats.registrations_rejected == 1
    assert harness.server.registry.has(client.client_id) is False

    # 再等若干个退避周期复检：确认不是"还没来得及重试"
    await asyncio.sleep(0.4)
    assert client.state == "stopped"
    assert client.stats.reconnects == 0
    assert connects == [1]
    assert harness.server.stats.registrations_rejected == 1


def test_unknown_token_is_rejected_permanently(tmp_path: Path) -> None:
    table = write_table(tmp_path / "tokens.json", {"name": "alice", "token": ALICE})

    async def scenario() -> None:
        async with TunnelHarness(
            configure_server=auth_file_server(table),
            configure_client=token_client("not-in-the-table"),
            expect_online=False,
        ) as harness:
            await _assert_rejected_permanently(harness)

    asyncio.run(scenario())


def test_revoked_token_is_rejected_permanently(tmp_path: Path) -> None:
    """``enabled: false`` ＝吊销。条目仍在表里（参与比较），但必须拒之门外。"""
    table = write_table(
        tmp_path / "tokens.json",
        {"name": "alice", "token": ALICE, "enabled": False},
    )

    async def scenario() -> None:
        async with TunnelHarness(
            configure_server=auth_file_server(table),
            configure_client=token_client(ALICE),
            expect_online=False,
        ) as harness:
            await _assert_rejected_permanently(harness)

    asyncio.run(scenario())


def test_client_id_mismatch_is_rejected_permanently(tmp_path: Path) -> None:
    """令牌对但 ``client_id`` 不匹配 ＝冒充，永久拒绝（防"偷令牌换身份"）。"""
    table = write_table(
        tmp_path / "tokens.json",
        {"name": "alice", "token": ALICE, "client_id": "pinned-alice"},
    )

    async def scenario() -> None:
        async with TunnelHarness(
            client_id="someone-else",
            configure_server=auth_file_server(table),
            configure_client=token_client(ALICE),
            expect_online=False,
        ) as harness:
            await _assert_rejected_permanently(harness)

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# 授权：整体拒绝，不静默丢弃、不部分接受
# --------------------------------------------------------------------------- #


def test_unauthorized_port_rejects_whole_registration(tmp_path: Path) -> None:
    allowed, denied = free_ports(2)
    table = write_table(
        tmp_path / "tokens.json",
        {"name": "alice", "token": ALICE, "ports": [allowed]},
    )

    async def scenario() -> None:
        async with TunnelHarness(
            configure_server=auth_file_server(table),
            configure_client=token_client(ALICE, ports=[allowed, denied]),
            expect_online=False,
        ) as harness:
            assert harness.server is not None
            client = harness.client
            assert client is not None

            # 先证明它**确实试过并被打回**（不是"还没来得及"）：退避重连 ≥ 1 次
            await harness.wait_until(
                lambda: client.stats.reconnects >= 1, what="被拒后客户端开始退避重试"
            )
            assert harness.server.stats.registrations_rejected >= 1
            assert harness.server.registry.has(client.client_id) is False

            # 整体拒绝：**连授权过的那个端口也没被认领**（不部分接受）
            assert harness.server.registry.owner_of(allowed) is None
            assert harness.server.registry.owner_of(denied) is None
            assert harness.server.registry.port_owner_snapshot() == {}
            # 客户端侧：可重试的 403，所以没进 fatal，状态是重连中而不是 stopped
            assert client.state != "stopped"

    asyncio.run(scenario())


def test_rejected_ports_can_be_claimed_by_another_client(tmp_path: Path) -> None:
    """被整体拒绝后，那些端口必须**干净地**留给别人——证明拒绝没留脏状态。"""
    allowed, denied = free_ports(2)
    table = write_table(
        tmp_path / "tokens.json",
        {"name": "alice", "token": ALICE, "ports": [allowed]},
        {"name": "bob", "token": BOB, "ports": [allowed, denied]},
    )

    async def scenario() -> None:
        async with TunnelHarness(
            configure_server=auth_file_server(table),
            configure_client=token_client(ALICE, ports=[allowed, denied]),
            expect_online=False,
        ) as harness:
            assert harness.server is not None
            assert harness.server.registry.port_owner_snapshot() == {}

            async with extra_client(
                harness, client_id="bob-1", token=BOB, local_ports=[allowed, denied]
            ):
                await harness.wait_until(
                    lambda: harness.server is not None and harness.server.registry.has("bob-1"),
                    what="授权齐备的 bob 完成注册",
                )
                assert harness.server.registry.owner_of(allowed) == "bob-1"
                assert harness.server.registry.owner_of(denied) == "bob-1"

    asyncio.run(scenario())


def test_partial_authorization_rejects_everything(tmp_path: Path) -> None:
    """``ports`` 只准了 A、却声明 ``[A, B]`` → 整体拒绝，A 也不许偷偷认领。

    "部分成功"会造出"这台机器只暴露了一半端口"这种极难排查的状态，是刻意拒绝的。
    """
    a_port, b_port = free_ports(2)
    table = write_table(
        tmp_path / "tokens.json",
        {"name": "alice", "token": ALICE, "ports": [a_port]},
    )

    async def scenario() -> None:
        async with TunnelHarness(
            configure_server=auth_file_server(table),
            configure_client=token_client(ALICE, ports=[a_port, b_port]),
            expect_online=False,
        ) as harness:
            assert harness.server is not None
            assert harness.client is not None
            await harness.wait_until(
                lambda: harness.client is not None and harness.client.stats.reconnects >= 1,
                what="第一次被拒",
            )
            assert harness.server.registry.owner_of(a_port) is None
            assert harness.server.registry.has(harness.client.client_id) is False

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# 热重载闭环：本轮的"闭合证明"
# --------------------------------------------------------------------------- #


def test_authorization_hot_reload_lets_same_client_board(tmp_path: Path) -> None:
    """未授权 → 运维改令牌文件 → **同一个客户端进程**自动上车，无需重启任何一端。

    这条是本轮唯一能证明"闭环真的闭合"的用例：只断言 ``retryable`` 字段是不够的，
    那最多证明"服务端说了可重试"，证明不了"重试真的能上车"。
    """
    a_port, b_port = free_ports(2)
    table = tmp_path / "tokens.json"
    write_table(table, {"name": "alice", "token": ALICE, "ports": [a_port]})

    async def scenario() -> None:
        async with TunnelHarness(
            configure_server=auth_file_server(table),
            configure_client=token_client(ALICE, ports=[a_port, b_port]),
            expect_online=False,
        ) as harness:
            assert harness.server is not None
            client = harness.client
            assert client is not None

            # 第一幕：b_port 未授权 → 整体拒绝，客户端不死（重试中）
            await harness.wait_until(lambda: client.stats.reconnects >= 1, what="第一次被拒")
            assert harness.server.registry.port_owner_snapshot() == {}
            assert client.state != "stopped"
            rejects_before = harness.server.stats.registrations_rejected
            assert rejects_before >= 1

            # 第二幕：改服务端令牌文件（唯一的外部动作，不重启、不碰客户端）
            write_table(table, {"name": "alice", "token": ALICE, "ports": [a_port, b_port]})

            # 第三幕：同一个客户端进程自己上车
            await harness.wait_online(timeout=5.0)
            session = harness.server.registry.get(client.client_id)
            assert session is not None
            assert session.identity == "alice"
            assert session.local_ports == {a_port, b_port}
            assert harness.server.registry.owner_of(b_port) == client.client_id
            assert harness.server.stats.registrations_rejected >= rejects_before

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# 热重载：生效时点、失败保留旧表、fail fast
# --------------------------------------------------------------------------- #


def test_new_token_takes_effect_without_restart(tmp_path: Path) -> None:
    """文件里加一条令牌 → 新令牌的客户端立刻能连，服务端一行没重启。"""
    table = tmp_path / "tokens.json"
    write_table(table, {"name": "alice", "token": ALICE})
    bob_port = free_ports(1)[0]

    async def scenario() -> None:
        async with TunnelHarness(
            configure_server=auth_file_server(table),
            configure_client=token_client(ALICE),
        ) as harness:
            assert harness.server is not None
            write_table(
                table,
                {"name": "alice", "token": ALICE},
                {"name": "bob", "token": BOB},
            )

            async with extra_client(
                harness, client_id="bob-1", token=BOB, local_ports=[bob_port]
            ):
                await harness.wait_until(
                    lambda: harness.server is not None and harness.server.registry.has("bob-1"),
                    what="新令牌（未重启服务端）生效",
                )
                session = harness.server.registry.get("bob-1")
                assert session is not None and session.identity == "bob"

    asyncio.run(scenario())


async def _assert_table_still_in_force(harness: TunnelHarness, *, extra_port: int) -> None:
    """令牌表出问题期间的两件事必须同时成立：旧令牌仍能连、未知令牌仍不能连。

    只测前半句不够——"旧令牌能连"在"降级为放行"的实现下**也**成立，
    必须配上"未知令牌仍被拒"才能证伪 fail-open。
    """
    assert harness.server is not None
    async with extra_client(harness, client_id="alice-2", token=ALICE, local_ports=[extra_port]):
        await harness.wait_until(
            lambda: harness.server is not None and harness.server.registry.has("alice-2"),
            what="旧令牌仍可注册",
        )
    rejects_before = harness.server.stats.registrations_rejected
    async with extra_client(
        harness, client_id="carol-1", token=CAROL, local_ports=[free_ports(1)[0]]
    ):
        await harness.wait_until(
            lambda: harness.server is not None
            and harness.server.stats.registrations_rejected > rejects_before,
            what="未知令牌被拒",
        )
        assert not harness.server.registry.has("carol-1")


def test_broken_table_keeps_old_tokens(tmp_path: Path) -> None:
    """令牌表损坏 → **保留旧表**：旧令牌照旧能连，未知令牌照旧不能连。

    这是本轮的安全铁律：重载失败绝不允许降级为放行。
    """
    table = tmp_path / "tokens.json"
    write_table(table, {"name": "alice", "token": ALICE})
    extra_port = free_ports(1)[0]

    async def scenario() -> None:
        async with TunnelHarness(
            configure_server=auth_file_server(table),
            configure_client=token_client(ALICE),
        ) as harness:
            assert harness.server is not None
            # 把文件写坏（唯一的外部动作）
            table.write_text("{ 这不是 JSON", encoding="utf-8")

            await _assert_table_still_in_force(harness, extra_port=extra_port)

    asyncio.run(scenario())


def test_deleted_table_keeps_old_tokens(tmp_path: Path) -> None:
    """文件被删 → 与损坏同理：保留旧表，绝不降级放行。"""
    table = tmp_path / "tokens.json"
    write_table(table, {"name": "alice", "token": ALICE})
    extra_port = free_ports(1)[0]

    async def scenario() -> None:
        async with TunnelHarness(
            configure_server=auth_file_server(table),
            configure_client=token_client(ALICE),
        ) as harness:
            assert harness.server is not None
            table.unlink()

            await _assert_table_still_in_force(harness, extra_port=extra_port)

    asyncio.run(scenario())


def test_reload_if_changed_reports_and_keeps_entries(tmp_path: Path) -> None:
    """单测层面把 ``reload_if_changed`` 的三态钉死：没变 / 变了 / 变坏了。"""
    table = tmp_path / "tokens.json"
    write_table(table, {"name": "alice", "token": ALICE})
    store = TokenStore.load(table)
    assert store.entry_count == 1
    assert store.reload_if_changed() is False, "文件没动就不该重载"

    write_table(table, {"name": "alice", "token": ALICE}, {"name": "bob", "token": BOB})
    assert store.reload_if_changed() is True
    assert store.entry_count == 2

    table.write_text("}", encoding="utf-8")
    assert store.reload_if_changed() is False, "坏文件必须返回 False"
    assert store.entry_count == 2, "坏文件必须保留旧表"
    assert store.lookup(BOB) is not None, "旧表仍在生效"


def test_empty_tokens_array_fails_fast(tmp_path: Path) -> None:
    """空 ``tokens`` ＝配置错误（会拒掉所有人，却长得像"配好了"），必须 fail fast。"""
    table = write_table(tmp_path / "tokens.json")

    with pytest.raises(ConfigError, match="空数组"):
        TokenStore.load(table)
    with pytest.raises(ConfigError, match="空数组"):
        build_authenticator(AuthConfig(enabled=True, file=str(table)))

    # 服务端构造时就该炸，而不是启动到一半才发现"谁都连不上"
    config = ServerConfig(mapping=[MappingRule(9028, 8000)])
    config.auth.enabled = True
    config.auth.file = str(table)
    with pytest.raises(ConfigError):
        TunnelServer(config)


def test_missing_table_file_fails_fast(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="不存在"):
        TokenStore.load(tmp_path / "no-such-tokens.json")


def test_malformed_json_reports_config_error(tmp_path: Path) -> None:
    table = tmp_path / "tokens.json"
    table.write_text("{ not json", encoding="utf-8")
    with pytest.raises(ConfigError, match="不是合法 JSON"):
        TokenStore.load(table)


# --------------------------------------------------------------------------- #
# 安全：令牌明文绝不落日志 / 事件 / 会话
# --------------------------------------------------------------------------- #


def test_token_never_leaks_into_logs_events_or_session(tmp_path: Path) -> None:
    """令牌明文一次都不许出现：日志（含 DEBUG 与异常栈）、事件载荷、``session.register_msg``。

    ⚠️ 这条是**安全验收线**，不是清洁工作：``register_msg=dict(msg)`` 曾经把令牌
    原样存进服务端内存，一旦有人把会话快照打进日志（或调试器随手一展开）就整批泄漏。
    """
    table = write_table(
        tmp_path / "tokens.json",
        {"name": "alice", "token": ALICE},
        {"name": "bob", "token": BOB},
    )
    bob_port = free_ports(1)[0]
    collected: Dict[str, Dict[str, Any]] = {}

    async def scenario() -> None:
        with capture_logs() as logs:
            async with TunnelHarness(
                configure_server=auth_file_server(table),
                configure_client=token_client(ALICE),
            ) as harness:
                assert harness.server is not None and harness.client is not None

                payloads: List[Dict[str, Any]] = []
                harness.server.events.on(
                    EventType.CLIENT_CONNECTED, lambda **payload: payloads.append(payload)
                )

                async with extra_client(
                    harness, client_id="bob-1", token=BOB, local_ports=[bob_port]
                ):
                    await harness.wait_until(
                        lambda: harness.server is not None
                        and harness.server.registry.has("bob-1"),
                        what="bob 完成注册",
                    )
                    # 会话对象只在客户端在线期间存在，必须在 with 块内取证
                    for cid in (harness.client.client_id, "bob-1"):
                        session = harness.server.registry.get(cid)
                        assert session is not None
                        collected[cid] = dict(session.register_msg)

                assert payloads, "至少要抓到一条 CLIENT_CONNECTED 事件载荷"
                assert payloads[-1].get("identity") == "bob"

        assert logs, "日志抓取本身要有效（抓不到日志的证伪没有意义）"
        # 把日志文本与事件载荷一起看：任何一处出现令牌子串都算泄漏
        blob = "\n".join(logs) + "\n" + json.dumps(payloads, ensure_ascii=False)
        assert ALICE not in blob, "令牌明文泄漏到了日志或事件里"
        assert BOB not in blob, "令牌明文泄漏到了日志或事件里"

    asyncio.run(scenario())

    for cid, register_msg in collected.items():
        assert "token" not in register_msg, f"{cid} 的 register_msg 里不该有 token 键"
        assert ALICE not in json.dumps(register_msg, ensure_ascii=False)
        assert BOB not in json.dumps(register_msg, ensure_ascii=False)


def test_sanitized_register_msg_keeps_other_fields(tmp_path: Path) -> None:
    """抹掉令牌时别把别的字段一起抹了——注册消息是排查现场的第一手材料。"""
    table = write_table(tmp_path / "tokens.json", {"name": "alice", "token": ALICE})

    async def scenario() -> None:
        async with TunnelHarness(
            configure_server=auth_file_server(table),
            configure_client=token_client(ALICE),
        ) as harness:
            assert harness.server is not None and harness.client is not None
            session = harness.server.registry.get(harness.client.client_id)
            assert session is not None
            assert "local_ports" in session.register_msg
            assert "hostname" in session.register_msg
            assert session.register_msg.get("client_id") == harness.client.client_id

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# 时序侧信道
# --------------------------------------------------------------------------- #


def test_lookup_compares_every_entry_including_revoked(tmp_path: Path) -> None:
    """比较次数必须等于条目数——命中即 ``return`` 会泄漏"第几条匹配上了"。"""
    table = write_table(
        tmp_path / "tokens.json",
        {"name": "alice", "token": ALICE},
        {"name": "revoked", "token": "revoked-token", "enabled": False},
        {"name": "bob", "token": BOB},
    )

    calls: List[str] = []

    def counting(left: str, right: str) -> bool:
        calls.append(left)
        return compare_secret(left, right)

    store = TokenStore.load(table, compare=counting)

    assert store.lookup(ALICE) is not None  # 第 1 条就命中
    assert len(calls) == 3, f"命中在第 1 条也必须把所有条目比完，实际只比了 {len(calls)} 次"

    calls.clear()
    assert store.lookup("no-such-token") is None
    assert len(calls) == 3, "未命中同样要比完全部条目"

    calls.clear()
    assert store.lookup(ALICE) is not None
    # 被吊销的条目也参与比较（否则"存在但被吊销"与"不存在"在耗时上可区分）
    assert len(calls) == store.entry_count == 3


def test_compare_secret_handles_non_ascii() -> None:
    """非 ASCII 令牌不许把服务端打成 500：``hmac.compare_digest(str)`` 会抛 TypeError。"""
    assert compare_secret("中文令牌⚡", "中文令牌⚡") is True
    assert compare_secret("中文令牌⚡", "中文令牌") is False


def test_lookup_survives_non_string_token(tmp_path: Path) -> None:
    table = write_table(tmp_path / "tokens.json", {"name": "alice", "token": ALICE})
    store = TokenStore.load(table)

    assert store.lookup("") is None
    assert store.lookup(None) is None  # type: ignore[arg-type]
    assert store.lookup({"token": ALICE}) is None  # type: ignore[arg-type]


def test_identity_allows_empty_ports_means_unlimited() -> None:
    assert Identity(name="x").allows(1) is True
    assert Identity(name="x", ports=(80,)).allows(80) is True
    assert Identity(name="x", ports=(80,)).allows(81) is False


# --------------------------------------------------------------------------- #
# 令牌表格式：fail fast 的各种形状
# --------------------------------------------------------------------------- #


def test_plaintext_and_hash_together_is_rejected(tmp_path: Path) -> None:
    table = write_table(
        tmp_path / "tokens.json",
        {"name": "alice", "token": ALICE, "token_sha256": sha256_of(ALICE)},
    )
    with pytest.raises(ConfigError, match="二选一"):
        TokenStore.load(table)


def test_entry_without_any_secret_is_rejected(tmp_path: Path) -> None:
    table = write_table(tmp_path / "tokens.json", {"name": "alice"})
    with pytest.raises(ConfigError, match="token_sha256"):
        TokenStore.load(table)


def test_bad_hash_format_is_rejected(tmp_path: Path) -> None:
    table = write_table(tmp_path / "tokens.json", {"name": "alice", "token_sha256": "not-a-hash"})
    with pytest.raises(ConfigError, match="64 位十六进制"):
        TokenStore.load(table)


def test_duplicate_names_are_rejected(tmp_path: Path) -> None:
    table = write_table(
        tmp_path / "tokens.json",
        {"name": "alice", "token": ALICE},
        {"name": "alice", "token": BOB},
    )
    with pytest.raises(ConfigError, match="重复"):
        TokenStore.load(table)


def test_unknown_root_and_entry_fields_are_rejected(tmp_path: Path) -> None:
    """未知字段必须报错，不许静默忽略——写错字段名却照跑最难排查。"""
    bad_root = tmp_path / "root.json"
    bad_root.write_text(
        json.dumps({"version": 1, "tokens": [{"name": "a", "token": "x"}], "extra": 1}),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="未知字段"):
        TokenStore.load(bad_root)

    bad_entry = tmp_path / "entry.json"
    write_table(bad_entry, {"name": "alice", "token": ALICE, "port": [80]})
    with pytest.raises(ConfigError, match="未知字段"):
        TokenStore.load(bad_entry)


def test_wrong_version_and_bad_ports_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="version"):
        TokenStore.load(write_table(tmp_path / "v.json", {"name": "a", "token": "x"}, version=2))

    with pytest.raises(ConfigError, match="整数端口"):
        TokenStore.load(
            write_table(tmp_path / "p.json", {"name": "a", "token": "x", "ports": ["8000"]})
        )

    with pytest.raises(ConfigError, match="数组"):
        TokenStore.load(
            write_table(tmp_path / "p2.json", {"name": "a", "token": "x", "ports": 8000})
        )


def test_token_file_authenticator_name_is_readable(tmp_path: Path) -> None:
    """启动日志那行 ``鉴权：token-file(N 条)`` —— 三态描述不能含糊。"""
    table = write_table(
        tmp_path / "tokens.json",
        {"name": "alice", "token": ALICE},
        {"name": "bob", "token": BOB},
    )
    auth = build_authenticator(AuthConfig(enabled=True, file=str(table)))
    assert auth.name == "token-file(2 条)"
    assert auth.store.names == ("alice", "bob")


def test_auth_error_retryable_defaults_to_permanent() -> None:
    """``AuthError`` 默认永久失败（鉴权一期语义），可重试必须显式说明。"""
    assert AuthError("令牌无效").retryable is False
    assert AuthError("端口未授权", retryable=True).retryable is True
