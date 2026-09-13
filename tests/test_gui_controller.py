# -*- coding: utf-8 -*-
"""
tests/test_gui_controller.py —— GUI 异步侧 + 无界面可用性的端到端测试
=====================================================================
这组用例把 GUI 真正接进真实链路里跑：**真实后端 + 真实服务端 + 真实客户端**
（:class:`tests.helpers.TunnelHarness`），唯一被替换掉的是窗口——因为 tkinter
需要显示器，而"把邮筒倒进 ViewModel"这段逻辑本来就不需要。

因此这里验证的是 GUI 的**实际行为**，不是「应该能行」：

1. 启动 → 客户端上线 → 界面拿到认领端口与映射表（都来自既有 API）；
2. 界面上加一条映射 → 提交 → **公网端口真的能访问到内网后端**；
3. 界面上删掉它 → 提交 → 该端口**真的不再监听**（连接被拒）；
4. 连接不可用时提交 → 返回一种统一的失败回执，而不是抛异常；
5. 没有 tkinter 的机器上，核心包与 GUI 的无头部分照常可用。

第 5 条用子进程验证：只有真的把 ``tkinter`` 从 ``sys.modules`` 里挖掉，
才能证明那条 import 边界不是纸面承诺。
"""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
import sys
from pathlib import Path

import pytest

from config import ClientConfig, ConfigError, MappingRule
from localtonet.gui.bridge import LoopThread, UiBridge
from localtonet.gui.controller import GuiController
from localtonet.gui.viewmodel import GuiViewModel
from tests.helpers import TunnelHarness, free_ports, http_request

ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# 无头驱动工具：模拟界面线程里那个 "root.after" 循环
# --------------------------------------------------------------------------- #


async def pump(vm: GuiViewModel, bridge: UiBridge) -> bool:
    """把邮筒里的消息倒进 ViewModel 一次。"""
    return vm.apply_all(bridge.drain())


async def pump_until(
    vm: GuiViewModel,
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
    raise AssertionError(f"{timeout}s 内{what}未满足（当前状态：{vm.state.connection}）")


async def wait_for_thread(future, *, timeout: float = 10.0):
    """等后台线程里的任务完成，**不阻塞当前事件循环**。

    这一条是本文件最容易踩的坑：``TunnelHarness`` 的服务端与内网后端就跑在
    **当前这个事件循环**上，若在主线程里对 ``concurrent.futures.Future`` 调
    ``.result()``，等于把整个服务端冻住——界面提交的映射永远等不到回执，
    直到客户端 5s 超时才失败，而失败之后服务端才终于读到那条指令。
    症状是"提交总是超时，但服务端日志显示映射改了"。

    正确姿势是交给事件循环去等（``asyncio.wrap_future``），
    等待期间服务端照常收发。
    """
    return await asyncio.wait_for(asyncio.wrap_future(future), timeout=timeout)


@contextlib.contextmanager
def gui_runtime(config: ClientConfig, *, snapshot_interval: float = 0.05):
    """起一个 LoopThread + GuiController，退出时保证收干净。"""
    loop_thread = LoopThread().start()
    bridge = UiBridge()
    controller = GuiController(
        config,
        loop_thread=loop_thread,
        bridge=bridge,
        snapshot_interval=snapshot_interval,
    )
    try:
        yield loop_thread, bridge, controller
    finally:
        # 这里可以放心用阻塞等待：关停客户端只是本地取消任务 + 关 socket，
        # 不需要服务端应答，因此不会把主线程上的事件循环卡住。
        with contextlib.suppress(Exception):
            loop_thread.submit(controller.stop()).result(timeout=5)
        loop_thread.stop()


# --------------------------------------------------------------------------- #
# 端到端：GVI 完成"改映射表不必手改 JSON + 重启进程"
# --------------------------------------------------------------------------- #


def test_gui_mapping_crud_end_to_end(run_async) -> None:
    async def scenario() -> None:
        harness = TunnelHarness(start_client=False)
        await harness.start()
        try:
            backend_port = harness.backend.port
            config = harness.build_client_config(client_id="gui-1", local_ports=[backend_port])

            with gui_runtime(config) as (loop_thread, bridge, controller):
                vm = GuiViewModel(default_local_port=backend_port)
                await wait_for_thread(loop_thread.submit(controller.start()))

                await harness.wait_until(
                    lambda: harness.server.registry.get("gui-1") is not None,
                    timeout=10.0,
                    what="GUI 客户端上线",
                )
                # 界面先要看到"在线 + 认领端口 + 服务端映射表"
                await pump_until(
                    vm,
                    bridge,
                    lambda: vm.state.online and vm.table.row_count > 0,
                    what="界面收到在线快照与服务端映射表",
                )
                assert vm.state.client_id == "gui-1"
                assert vm.state.claimed == [backend_port]
                assert [row.public_port for row in vm.table.rows] == [harness.public_port]
                assert not vm.table.is_dirty

                # ① 新增一条映射并提交
                new_public = free_ports(1)[0]
                vm.table.add_row(public_port=new_public, local_port=backend_port)
                assert vm.table.is_dirty

                result = await wait_for_thread(loop_thread.submit(controller.submit_mapping(vm.table.rules())))
                assert result["ok"] is True, result

                await pump_until(vm, bridge, lambda: not vm.table.is_dirty, what="提交回执到达界面")
                assert "新增" in vm.state.notice
                assert sorted(rule.public_port for rule in harness.server.mapping.rules()) == sorted(
                    [harness.public_port, new_public]
                )

                # ② 新映射端口真的可用
                response = await http_request(new_public, "/echo?msg=gui-ok")
                assert response.status == 200
                assert response.text.strip() == "gui-ok"

                # ③ 删掉它并提交，端口应立即停止监听
                index = vm.table.index_of(new_public)
                vm.table.remove_row(index)
                result = await wait_for_thread(loop_thread.submit(controller.submit_mapping(vm.table.rules())))
                assert result["ok"] is True, result
                assert [rule.public_port for rule in harness.server.mapping.rules()] == [harness.public_port]

                await pump_until(vm, bridge, lambda: not vm.table.is_dirty, what="删除回执到达界面")

                with pytest.raises(OSError):
                    await asyncio.open_connection("127.0.0.1", new_public)

                # ④ 原端口不受影响，界面计数也已经把成功请求统计进去
                original = await http_request(harness.public_port, "/echo?msg=still-ok")
                assert original.status == 200
                await pump_until(
                    vm,
                    bridge,
                    lambda: vm.state.stat("forwards_total") >= 2,
                    what="界面统计到转发次数",
                )
                assert vm.state.stat("forwards_failed") == 0
        finally:
            await harness.stop()

    run_async(lambda: scenario())


def test_gui_rejects_empty_mapping_before_submitting(run_async) -> None:
    """删光映射表在界面层就被挡住，不会白跑一趟服务端——两边用的是同一条规则。"""

    async def scenario() -> None:
        harness = TunnelHarness(start_client=False)
        await harness.start()
        try:
            config = harness.build_client_config(client_id="gui-2", local_ports=[harness.backend.port])
            with gui_runtime(config) as (loop_thread, bridge, controller):
                vm = GuiViewModel(default_local_port=harness.backend.port)
                await wait_for_thread(loop_thread.submit(controller.start()))
                await pump_until(vm, bridge, lambda: vm.table.row_count > 0, what="映射表下发")

                with pytest.raises(ConfigError, match="不能为空"):
                    vm.table.remove_row(0)
                assert vm.table.row_count == 1
        finally:
            await harness.stop()

    run_async(lambda: scenario())


def test_gui_submit_reports_uniform_failure_when_not_connected(run_async) -> None:
    """控制连接没建立时提交：返回统一的失败回执，界面不必到处 try/except。"""

    async def scenario() -> None:
        control_port, data_port, local_port = free_ports(3)
        config = ClientConfig(
            server_host="127.0.0.1",
            control_port=control_port,
            data_port=data_port,
            client_id="gui-offline",
            local_ports=[local_port],
        )
        config.timeouts.connect_timeout = 0.2
        config.reconnect.initial_delay = 0.05
        config.reconnect.max_delay = 0.1
        config.validate()

        with gui_runtime(config) as (loop_thread, bridge, controller):
            vm = GuiViewModel(default_local_port=local_port)
            await wait_for_thread(loop_thread.submit(controller.start()))

            result = await wait_for_thread(
                loop_thread.submit(
                    controller.submit_mapping([MappingRule(public_port=9028, local_port=local_port)])
                )
            )

            assert result["ok"] is False
            assert "控制连接尚未建立" in result["msg"]

            await pump_until(vm, bridge, lambda: bool(vm.state.notice), what="失败回执到达界面")
            assert "提交失败" in vm.state.notice

    run_async(lambda: scenario())


def test_gui_controller_start_is_idempotent(run_async) -> None:
    async def scenario() -> None:
        harness = TunnelHarness(start_client=False)
        await harness.start()
        try:
            config = harness.build_client_config(client_id="gui-3", local_ports=[harness.backend.port])
            with gui_runtime(config) as (loop_thread, _bridge, controller):
                await wait_for_thread(loop_thread.submit(controller.start()))
                await wait_for_thread(loop_thread.submit(controller.start()))
                assert controller.running
                assert controller.client.client_id == "gui-3"
        finally:
            await harness.stop()

    run_async(lambda: scenario())


# --------------------------------------------------------------------------- #
# 入口脚本：复用命令行客户端的参数定义
# --------------------------------------------------------------------------- #


def test_gui_entry_reuses_client_arguments() -> None:
    """``gui.py`` 的参数与 ``client.py`` 是同一套，命令行用户不必重新学。"""
    from gui import build_gui_parser, load_config

    args = build_gui_parser().parse_args(
        ["--server", "1.2.3.4:7000", "--local-ports", "8000,8080", "--token", "tk", "--no-autostart"]
    )
    assert args.no_autostart is True

    config = load_config(args)

    assert (config.server_host, config.control_port) == ("1.2.3.4", 7000)
    assert config.local_ports == [8000, 8080]
    assert config.auth_token == "tk"


# --------------------------------------------------------------------------- #
# 无界面可用的硬约束（子进程实测）
# --------------------------------------------------------------------------- #

_HEADLESS_SCRIPT = """
import sys

# 把 tkinter 彻底挖掉，模拟一台没有图形栈的机器
sys.modules["tkinter"] = None
for name in [m for m in sys.modules if m.startswith("tkinter.")]:
    del sys.modules[name]

import localtonet.gui as gui
import localtonet.gui.bridge
import localtonet.gui.controller
import localtonet.gui.model
import localtonet.gui.viewmodel
from config import ClientConfig
from localtonet.client.core import TunnelClient
from localtonet.server.core import TunnelServer

# 核心链路照常可用
client = TunnelClient(ClientConfig(local_ports=[8000]))
assert client.state == "idle"
assert TunnelServer.__name__ == "TunnelServer"
assert "localtonet.gui.app" not in sys.modules

# 只有真正要开窗口时才会报错，且提示可读
try:
    gui.create_app()
except RuntimeError as exc:
    assert "tkinter" in str(exc), str(exc)
else:
    raise SystemExit("没有 tkinter 时 create_app 竟然没有报错")

print("HEADLESS_OK")
"""


def test_gui_and_core_work_without_tkinter() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _HEADLESS_SCRIPT],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "HEADLESS_OK" in result.stdout
