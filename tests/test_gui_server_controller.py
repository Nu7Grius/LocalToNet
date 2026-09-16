# -*- coding: utf-8 -*-
"""
tests/test_gui_server_controller.py —— 服务端管理台的端到端测试
================================================================
这组用例把管理台真正接进真实链路里跑：**真实内网后端 + 真实服务端 + 真实客户端**，
唯一被替换掉的是窗口（tkinter 需要显示器）。

与客户端侧 :mod:`tests.test_gui_controller` 有一个**结构性差别**，也是本文件
最容易写错的地方：

客户端侧测试里，``TunnelHarness`` 的服务端跑在**当前事件循环**上，只有客户端被搬进
``LoopThread``。而管理台**自己拥有服务端**——TunnelServer 跑在后台线程的事件循环里，
测试主线程只跑"内网后端 + 真客户端"。所以：

* 对 ``loop_thread.submit(...)`` 返回的 future，**绝不能**在测试里直接 ``.result()``
  之外还指望后台服务端继续干活——正确姿势是 ``await asyncio.wait_for(asyncio.wrap_future(...))``，
  等待期间主循环照常处理客户端的收发；
* 想观察服务端状态，不能直接摸 ``controller.server`` 的内部字段（跨线程写读），
  只能走 ``snapshot()``（经邮筒）或对服务端发真实请求。

守六条线：启动后**端口真的在监听**；客户端上线后**在线表真的出现它**；
管理台改映射**真的对所有客户端生效**（新端口可访问 + 客户端收到广播）；
非法映射**被拒且不影响现状**；停止**真的关端口**且能重启；没有 tkinter 时给出可读错误。
"""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional, Tuple

import pytest

from config import ClientConfig, MappingRule, ServerConfig
from examples.demo_backend import DemoBackend
from localtonet.client.core import TunnelClient
from localtonet.gui.bridge import LoopThread, UiBridge
from localtonet.gui.server_controller import ServerController
from localtonet.gui.server_viewmodel import REMOTE_STALE_NOTICE, ServerViewModel
from localtonet.server.core import TunnelServer
from tests.helpers import free_ports, http_request

ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #


def _apply_fast_timeouts(config: Any) -> None:
    """压缩时间尺度：测试里等不起默认的 30s 心跳。"""
    config.timeouts.heartbeat_interval = 5.0
    config.timeouts.pong_timeout = 2.0
    config.timeouts.client_idle_timeout = 30.0
    config.timeouts.watchdog_interval = 0.2
    config.timeouts.pair_timeout = 5.0
    config.timeouts.connect_timeout = 1.0
    config.reconnect.initial_delay = 0.05
    config.reconnect.max_delay = 0.2
    config.reconnect.jitter = 0.0


def build_server_config(*, backend_port: int, ports: Tuple[int, int, int]) -> ServerConfig:
    control_port, data_port, public_port = ports
    config = ServerConfig(
        name="admin-test",
        mapping=[
            MappingRule(
                public_port=public_port,
                local_port=backend_port,
                host="127.0.0.1",
                local_host="127.0.0.1",
                remark="admin",
            )
        ],
    )
    config.control.host = "127.0.0.1"
    config.control.port = control_port
    config.data.host = "127.0.0.1"
    config.data.port = data_port
    config.advertise_host = "127.0.0.1"
    _apply_fast_timeouts(config)
    config.validate()
    return config


def build_client_config(*, ports: Tuple[int, int], backend_port: int, client_id: str) -> ClientConfig:
    control_port, data_port = ports
    config = ClientConfig(
        server_host="127.0.0.1",
        control_port=control_port,
        data_port=data_port,
        client_id=client_id,
        local_host="127.0.0.1",
        local_ports=[backend_port],
    )
    _apply_fast_timeouts(config)
    config.validate()
    return config


@contextlib.contextmanager
def admin_runtime(config: ServerConfig, *, snapshot_interval: float = 0.05):
    """起一个 LoopThread + ServerController，退出时保证收干净。"""
    loop_thread = LoopThread(name="admin-test-loop").start()
    bridge = UiBridge()
    controller = ServerController(
        config,
        loop_thread=loop_thread,
        bridge=bridge,
        snapshot_interval=snapshot_interval,
    )
    try:
        yield loop_thread, bridge, controller
    finally:
        # 关停服务端只是本地收尾（取消任务 + 关 socket），不等对端应答，
        # 所以这里阻塞等待是安全的。
        with contextlib.suppress(Exception):
            loop_thread.submit(controller.stop()).result(timeout=5)
        loop_thread.stop()


async def await_thread(future: Any, *, timeout: float = 10.0) -> Any:
    """等后台线程里的任务完成，**不阻塞当前事件循环**（见模块文档）。"""
    return await asyncio.wait_for(asyncio.wrap_future(future), timeout=timeout)


async def pump(vm: ServerViewModel, bridge: UiBridge) -> bool:
    return vm.apply_all(bridge.drain())


async def pump_until(
    vm: ServerViewModel,
    bridge: UiBridge,
    predicate,
    *,
    timeout: float = 10.0,
    what: str = "条件",
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        await pump(vm, bridge)
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"{timeout}s 内{what}未满足（当前在线 {vm.state.client_count} 个客户端）")


async def wait_client_online(client: TunnelClient, server_snapshot, *, timeout: float = 10.0) -> None:
    """等服务端真的登记了这个客户端（用 snapshot 判，不碰内部字段）。"""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        clients = server_snapshot().get("clients") or []
        for item in clients:
            if item.get("client_id") == client.client_id and item.get("local_ports"):
                return
        await asyncio.sleep(0.02)
    raise AssertionError(f"{timeout}s 内客户端 {client.client_id} 未在服务端上线")


# --------------------------------------------------------------------------- #
# 端到端
# --------------------------------------------------------------------------- #


def test_admin_starts_server_and_sees_client_identity(run_async) -> None:
    """管理台拉起的服务端**真的在监听**，且客户端上线后在线表里出现它。"""

    async def scenario() -> None:
        backend = DemoBackend("127.0.0.1", 0)
        backend_port = await backend.start()
        ports = free_ports(3)
        config = build_server_config(backend_port=backend_port, ports=tuple(ports))

        with admin_runtime(config) as (loop_thread, bridge, controller):
            vm = ServerViewModel(idle_timeout=config.timeouts.client_idle_timeout)
            await await_thread(loop_thread.submit(controller.start()))

            # ① 服务端真的起来了：快照经邮筒到达，且控制端口在监听
            await pump_until(vm, bridge, lambda: vm.state.listening, what="管理台收到启动快照")
            assert vm.state.name == "admin-test"
            assert vm.state.auth_mode == "none"
            assert ports[2] in vm.state.listening
            assert vm.state.stat("clients_online") == 0

            # ② 真客户端连上来
            client = TunnelClient(
                build_client_config(
                    ports=(ports[0], ports[1]), backend_port=backend_port, client_id="admin-c1"
                )
            )
            client_task = asyncio.create_task(client.run(), name="admin-test-client")
            try:
                await wait_client_online(client, controller.snapshot)
                await pump_until(
                    vm, bridge, lambda: vm.state.client_count == 1, what="在线表出现客户端"
                )

                row = vm.state.clients[0]
                assert row.client_id == "admin-c1"
                assert row.ports == (backend_port,)
                assert row.identity == "anonymous"  # 不配鉴权时的身份标签
                # 事件也到了：日志里有"上线"，且带身份
                assert any("客户端上线" in entry.text for entry in vm.state.log)
                assert any("anonymous" in entry.text for entry in vm.state.log)

                # ③ 访客端口真的能把请求送到内网后端
                response = await http_request(ports[2], "/echo?msg=admin-ok")
                assert response.status == 200
                assert response.text.strip() == "admin-ok"
            finally:
                await client.stop()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(client_task, timeout=5)

        await backend.stop()

    run_async(scenario)


def test_admin_mapping_change_reaches_every_client(run_async) -> None:
    """管理台提交映射后：新端口立即可用，**且在线客户端的映射表跟着变**。

    后半句是管理台与客户端界面的关键差别——客户端提交只影响自己，
    管理台提交是全局的，必须广播。只验"新端口能用"会漏掉广播这条路径。
    """

    async def scenario() -> None:
        backend = DemoBackend("127.0.0.1", 0)
        backend_port = await backend.start()
        ports = free_ports(3)
        config = build_server_config(backend_port=backend_port, ports=tuple(ports))

        with admin_runtime(config) as (loop_thread, bridge, controller):
            vm = ServerViewModel(idle_timeout=config.timeouts.client_idle_timeout)
            await await_thread(loop_thread.submit(controller.start()))
            await pump_until(vm, bridge, lambda: vm.state.listening, what="管理台收到启动快照")

            client = TunnelClient(
                build_client_config(
                    ports=(ports[0], ports[1]), backend_port=backend_port, client_id="admin-c2"
                )
            )
            client_task = asyncio.create_task(client.run(), name="admin-test-client2")
            try:
                await wait_client_online(client, controller.snapshot)
                await pump_until(vm, bridge, lambda: vm.table.row_count == 1, what="映射表到达界面")

                # 界面上加一条映射并提交
                new_public = free_ports(1)[0]
                vm.table.add_row(public_port=new_public, local_port=backend_port)
                assert vm.table.is_dirty
                result = await await_thread(loop_thread.submit(controller.submit_mapping(vm.table.rules())))
                assert result["ok"] is True, result
                assert "新增" in result["msg"]

                await pump_until(vm, bridge, lambda: not vm.table.is_dirty, what="提交结果到达界面")
                assert "已生效" in vm.state.notice

                # ① 服务端侧：新端口真的在映射表里
                assert sorted(item["public_port"] for item in controller.snapshot()["mapping"]) == sorted(
                    [ports[2], new_public]
                )
                # ② 公网侧：新端口真的可用
                response = await http_request(new_public, "/echo?msg=admin-mapping")
                assert response.status == 200
                assert response.text.strip() == "admin-mapping"
                # ③ 客户端侧：广播真的到了（客户端界面靠这个刷新）
                deadline = asyncio.get_running_loop().time() + 5.0
                while asyncio.get_running_loop().time() < deadline:
                    if {item.get("public_port") for item in client.remote_mapping} == {ports[2], new_public}:
                        break
                    await asyncio.sleep(0.02)
                else:
                    raise AssertionError("客户端没有收到管理台的映射表广播")
            finally:
                await client.stop()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(client_task, timeout=5)

        await backend.stop()

    run_async(scenario)


def test_admin_rejects_invalid_mapping_without_touching_the_live_table(run_async) -> None:
    """非法映射要被拒，且**现状一点不变**（不能出现"改坏了才报错"）。

    用"逐端口开了 TLS 却没配访客证书"来构造失败：它在
    ``MappingManager.apply()`` 里、**停监听之前**就被 ``_require_certs`` 拦住，
    正好验证"拒绝发生在现状被改动之前"。

    刻意**不**用 ``public_port=0`` 当"非法"——在 asyncio 里 ``port=0`` 是
    "随便挑一个空闲端口"，那样写出来的用例会假装通过。
    """

    async def scenario() -> None:
        backend = DemoBackend("127.0.0.1", 0)
        backend_port = await backend.start()
        ports = free_ports(3)
        config = build_server_config(backend_port=backend_port, ports=tuple(ports))

        with admin_runtime(config) as (loop_thread, bridge, controller):
            vm = ServerViewModel()
            await await_thread(loop_thread.submit(controller.start()))
            await pump_until(vm, bridge, lambda: vm.state.listening, what="管理台收到启动快照")

            before = sorted(item["public_port"] for item in controller.snapshot()["mapping"])

            bad = MappingRule(public_port=ports[2], local_port=backend_port, host="127.0.0.1", tls=True)
            result = await await_thread(loop_thread.submit(controller.submit_mapping([bad])))
            assert result["ok"] is False
            assert result["msg"]  # 原因必须可读，不能是空串
            assert sorted(item["public_port"] for item in controller.snapshot()["mapping"]) == before

            # 原端口不受影响，照常服务
            await pump_until(vm, bridge, lambda: "提交失败" in vm.state.notice, what="失败结果到达界面")
            assert vm.state.log[-1].level == "error"
            assert [item["public_port"] for item in controller.snapshot()["mapping"]] == [ports[2]]

        await backend.stop()

    run_async(scenario)


def test_admin_start_failure_is_reported_not_raised(run_async) -> None:
    """启动失败（端口被占）不许抛进界面线程，而要变成一条看得见的日志 + failure。"""

    async def scenario() -> None:
        backend = DemoBackend("127.0.0.1", 0)
        backend_port = await backend.start()
        ports = free_ports(3)

        # 先用主循环占住控制端口，让后台线程里的服务端起不来
        blocker = await asyncio.start_server(lambda r, w: None, "127.0.0.1", ports[0])
        try:
            config = build_server_config(backend_port=backend_port, ports=tuple(ports))
            with admin_runtime(config) as (loop_thread, bridge, controller):
                vm = ServerViewModel()
                await await_thread(loop_thread.submit(controller.start()))

                assert controller.failure  # 记下了原因，没有静默
                assert "监听失败" in controller.failure

                await pump_until(vm, bridge, lambda: bool(vm.state.log), what="失败原因到达界面")
                assert "服务端启动失败" in vm.state.log[-1].text
                assert vm.state.log[-1].level == "error"
                assert "启动失败" in vm.state.notice

                # 启动失败不能留下半个监听：访客端口也不该还在
                with pytest.raises(OSError):
                    await asyncio.open_connection("127.0.0.1", ports[2])
                # 服务端对象仍在，快照可读（界面不会因为拿不到状态而白屏）
                assert controller.snapshot()["stats"]["clients_online"] == 0
        finally:
            blocker.close()
            await blocker.wait_closed()

        await backend.stop()

    run_async(scenario)


def test_admin_stop_closes_ports_and_can_start_again(run_async) -> None:
    """停止要真的关端口；再次启动要真的重新监听（重建实例，不是"看着活着"）。"""

    async def scenario() -> None:
        backend = DemoBackend("127.0.0.1", 0)
        backend_port = await backend.start()
        ports = free_ports(3)
        config = build_server_config(backend_port=backend_port, ports=tuple(ports))

        with admin_runtime(config) as (loop_thread, bridge, controller):
            vm = ServerViewModel()
            await await_thread(loop_thread.submit(controller.start()))
            await pump_until(vm, bridge, lambda: vm.state.listening, what="管理台收到启动快照")

            first_server: TunnelServer = controller.server

            await await_thread(loop_thread.submit(controller.stop()))
            await pump_until(vm, bridge, lambda: not vm.state.listening, what="停止后快照更新")

            # 访客端口与控制端口都该关掉
            with pytest.raises(OSError):
                await asyncio.open_connection("127.0.0.1", ports[2])

            # 再启动：必须是**新实例**，不是拿旧实例假装重启
            await await_thread(loop_thread.submit(controller.start()))
            assert controller.failure == ""
            assert controller.server is not first_server  # 旧实例的 _stopped 已置位
            assert first_server.snapshot()["listening"] == []  # 旧实例确实停着

            await pump_until(
                vm, bridge, lambda: bool(vm.state.listening), what="重启后管理台重新收到监听"
            )

            # 重新监听之后，客户端要能重新连上并真的转发
            client = TunnelClient(
                build_client_config(
                    ports=(ports[0], ports[1]), backend_port=backend_port, client_id="admin-c3"
                )
            )
            client_task = asyncio.create_task(client.run(), name="admin-test-client3")
            try:
                await wait_client_online(client, controller.snapshot)
                response = await http_request(ports[2], "/echo?msg=restarted")
                assert response.status == 200
                assert response.text.strip() == "restarted"
            finally:
                await client.stop()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(client_task, timeout=5)

        await backend.stop()

    run_async(scenario)


def test_injected_server_without_factory_cannot_restart(run_async) -> None:
    """注入的实例没有工厂可重建时，重启要**明确失败**而不是假装成功。"""

    async def scenario() -> None:
        backend = DemoBackend("127.0.0.1", 0)
        backend_port = await backend.start()
        ports = free_ports(3)
        config = build_server_config(backend_port=backend_port, ports=tuple(ports))

        loop_thread = LoopThread(name="admin-test-injected").start()
        bridge = UiBridge()
        injected = TunnelServer(config)
        controller = ServerController(
            config,
            loop_thread=loop_thread,
            bridge=bridge,
            server=injected,
            snapshot_interval=0.05,
        )
        try:
            await await_thread(loop_thread.submit(controller.start()))
            await await_thread(loop_thread.submit(controller.stop()))
            assert controller.server is injected

            await await_thread(loop_thread.submit(controller.start()))
            assert "不支持重启" in controller.failure
            assert controller.snapshot()["listening"] == []

            vm = ServerViewModel()
            await pump_until(
                vm, bridge, lambda: any("不支持重启" in e.text for e in vm.state.log), what="失败原因到达界面"
            )
        finally:
            with contextlib.suppress(Exception):
                loop_thread.submit(controller.stop()).result(timeout=5)
            loop_thread.stop()

        await backend.stop()

    run_async(scenario)


def test_server_side_mapping_table_keeps_user_edit_on_remote_change(run_async) -> None:
    """服务端管理台也要"保留用户输入"：远端变了只提示，不覆盖正在编辑的内容。

    客户端靠 ``MAPPING_CHANGED`` 事件；**服务端根本不发这个事件**，映射表只能靠周期快照
    带过来。所以要验证的不只是"不覆盖"，还有"每 0.5 秒一次的采样不会把提示栏刷成噪声"。

    这里改服务端映射表**刻意不走** ``controller.submit_mapping``：那条路会把
    ``mapping_result`` 回灌给界面，界面会合理地认为"这是我自己提交的"，于是把工作副本
    当成新的基线。要模拟的是**另一个客户端通过 set_mapping 改了映射表**——
    那才是真实世界里"远端变了而我正在编辑"的场景。
    """

    async def scenario() -> None:
        backend = DemoBackend("127.0.0.1", 0)
        backend_port = await backend.start()
        ports = free_ports(3)
        config = build_server_config(backend_port=backend_port, ports=tuple(ports))

        with admin_runtime(config) as (loop_thread, bridge, controller):
            server = controller.server  # 外部改动走服务端本体，绕开界面的提交路径
            vm = ServerViewModel()
            await await_thread(loop_thread.submit(controller.start()))
            await pump_until(vm, bridge, lambda: vm.table.row_count == 1, what="映射表到达界面")

            # 用户开始编辑（还没提交）
            mine = free_ports(1)[0]
            vm.table.add_row(public_port=mine, local_port=backend_port)
            assert vm.table.is_dirty

            # 采样会周期到达（0.02s 一轮倒邮筒 × 若干轮 > 0.05s 的采样间隔）：
            # 远端内容没变时**一次都不该**提示，否则提示栏每 0.5 秒被刷一遍
            for _ in range(12):
                await pump(vm, bridge)
                await asyncio.sleep(0.02)
                assert vm.notice != REMOTE_STALE_NOTICE, "远端没变却报了'远端已更新'"

            # 外部改了映射表（等价于另一个客户端提交 set_mapping）
            other = free_ports(1)[0]
            diff = await await_thread(
                loop_thread.submit(
                    server.submit_mapping(
                        [
                            MappingRule(public_port=ports[2], local_port=backend_port, host="127.0.0.1"),
                            MappingRule(public_port=other, local_port=backend_port, host="127.0.0.1"),
                        ]
                    )
                )
            )
            assert other in diff.added

            await pump_until(vm, bridge, lambda: vm.notice == REMOTE_STALE_NOTICE, what="远端变化提示")
            assert vm.table.is_dirty  # 用户输入还在
            assert mine in [row.public_port for row in vm.table.rows]  # 用户那一行没被冲掉
            assert vm.table.remote_stale is True
            # 但"已保存"的参照物已经跟着远端更新了（否则用户放弃修改会回到更旧的版本）
            assert sorted(row.public_port for row in vm.table.snapshot) == sorted([ports[2], other])

        await backend.stop()

    run_async(scenario)


# --------------------------------------------------------------------------- #
# 无界面可用性
# --------------------------------------------------------------------------- #


def test_create_server_app_without_tkinter_raises_readable_error() -> None:
    """真的把 ``tkinter`` 从 ``sys.modules`` 里挖掉，证明那条 import 边界不是纸面承诺。"""
    script = """
import sys
sys.modules["tkinter"] = None
for name in [m for m in sys.modules if m.startswith("tkinter.")]:
    del sys.modules[name]
from config import ServerConfig
from localtonet.gui import create_server_app
assert "localtonet.gui.server_app" not in sys.modules
try:
    create_server_app(ServerConfig())
except RuntimeError as exc:
    assert "tkinter" in str(exc), str(exc)
    assert "server.py" in str(exc), str(exc)
    raise SystemExit(0)
raise SystemExit("没有 tkinter 时 create_server_app 竟然没有报错")
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_server_admin_headless_layers_import_without_tkinter() -> None:
    """无头层（model / viewmodel / controller）在没装 tkinter 的机器上照常可用。"""
    assert "tkinter" not in sys.modules
    assert "localtonet.gui.server_app" not in sys.modules
    assert "localtonet.gui.widgets" not in sys.modules


def test_server_gui_entry_reuses_server_cli_parser() -> None:
    """入口与命令行服务端共用同一套参数，不另立一份（否则迟早两边不一致）。"""
    import server_gui
    from server import build_parser

    parser = server_gui.build_gui_parser()
    server_actions = {option for action in build_parser()._actions for option in action.option_strings}
    gui_actions = {option for action in parser._actions for option in action.option_strings}
    assert server_actions <= gui_actions
    assert "--no-autostart" in gui_actions
