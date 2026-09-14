# -*- coding: utf-8 -*-
"""
tests/test_client_cli.py —— 客户端命令行退出码
================================================
只钉一件事：**退出码要如实反映结局**。

起因是一个真实缺陷：``run()`` 在"被 403 永久拒绝"分支直接 ``return``，
``main()`` 于是正常返回 0。脚本化场景里

    client.py --token wrong && echo ok

会打印 ``ok``——"凭据被拒"被当成了成功。现在约定：

* ``0`` 正常结束
* ``1`` 运行期失败（含被永久拒绝的 403）
* ``2`` 配置错误

服务端用一个**假服务端**顶替：它只读一条 ``register_client`` 然后回指定 code 的
``register_ack``，不需要真实客户端/映射那一整套。子进程用 ``asyncio.to_thread``
跑，避免阻塞正在监听测试端口的事件循环。
"""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
import sys
from pathlib import Path
from typing import Any

from protocol import MsgType, make_msg, recv_msg, send_msg
from tests.helpers import free_ports

REPO_ROOT = Path(__file__).resolve().parent.parent
CLIENT = REPO_ROOT / "client.py"


def _rejecting_handler(code: int, message: str):
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await recv_msg(reader)  # register_client
            await send_msg(
                writer,
                make_msg(
                    MsgType.REGISTER_ACK,
                    ok=False,
                    code=code,
                    msg=message,
                    claimed=[],
                    conflicts=[],
                ),
            )
        except Exception:  # noqa: BLE001 - 测试收尾噪音一律吞掉
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    return handler


def run_client(args: list[str], timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CLIENT), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_wrong_token_exits_with_code_1() -> None:
    """被 403 永久拒绝 → 退出码非 0，脚本里的 ``&&`` 不能再把失败当成功。"""

    async def scenario() -> subprocess.CompletedProcess[str]:
        port = free_ports(1)[0]
        server = await asyncio.start_server(_rejecting_handler(403, "客户端提供的 token 不合法"), "127.0.0.1", port)
        try:
            return await asyncio.to_thread(
                run_client,
                ["--server", f"127.0.0.1:{port}", "--local-ports", "8000", "--token", "wrong"],
            )
        finally:
            server.close()
            await server.wait_closed()

    proc = asyncio.run(scenario())

    assert proc.returncode == 1, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert "403" in proc.stdout


def test_missing_config_file_exits_with_code_2(tmp_path: Path) -> None:
    """配置错误归 2，与"运行期失败"分开——调用方能据此决定要不要重试。"""
    proc = run_client(["-c", str(tmp_path / "no-such.json"), "--local-ports", "8000"])

    assert proc.returncode == 2
    assert "配置文件不存在" in proc.stderr


def test_capacity_rejection_keeps_client_running() -> None:
    """503 是暂时性失败 → 客户端必须**继续重试**，进程不许退出。

    与上面第一条配对：403 停手、503 续跑。把它们统一成任何一种都是 bug。
    """

    async def scenario() -> Any:
        port = free_ports(1)[0]
        server = await asyncio.start_server(
            _rejecting_handler(503, "在线客户端数已达上限 1"), "127.0.0.1", port
        )
        proc: subprocess.Popen[str] | None = None
        try:
            proc = subprocess.Popen(
                [
                    sys.executable,
                    str(CLIENT),
                    "--server",
                    f"127.0.0.1:{port}",
                    "--local-ports",
                    "8000",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            # client.json 的 reconnect.initial_delay = 1s，等它至少退避重试一轮
            await asyncio.sleep(1.5)
            assert proc.poll() is None, "503 之后客户端不该退出"
        finally:
            if proc is not None and proc.poll() is None:
                proc.terminate()
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(proc.wait, 10)
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_rejection_message_carries_no_duplicated_code_prefix() -> None:
    """回执日志里不能出现 ``[403] [403]`` 叠字——客户端不该给 msg 再套一层前缀。"""

    async def scenario() -> subprocess.CompletedProcess[str]:
        port = free_ports(1)[0]
        server = await asyncio.start_server(_rejecting_handler(403, "客户端提供的 token 不合法"), "127.0.0.1", port)
        try:
            return await asyncio.to_thread(
                run_client,
                ["--server", f"127.0.0.1:{port}", "--local-ports", "8000", "--token", "wrong"],
            )
        finally:
            server.close()
            await server.wait_closed()

    proc = asyncio.run(scenario())
    for stream, text in (("stdout", proc.stdout), ("stderr", proc.stderr)):
        assert text.count("[403]") <= 1, f"{stream} 里出现了叠字前缀：{text!r}"


def test_tls_client_against_plaintext_server_exits_with_code_1() -> None:
    """客户端配了 TLS 却连明文服务端 → 永久失败，退出码 1（不是无限退避重试）。

    这是 TLS 落地新增的一条退出码契约：TLS 错配与 403 同类，都是"配置错、重试无用"。
    """
    certs = REPO_ROOT / "tests" / "certs"

    async def scenario() -> subprocess.CompletedProcess[str]:
        port = free_ports(1)[0]
        # 明文假服务端：只读一条就关，不解析 TLS
        async def plain_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                await asyncio.wait_for(reader.read(100), timeout=1)
            except Exception:  # noqa: BLE001
                pass
            finally:
                with contextlib.suppress(Exception):
                    writer.close()

        server = await asyncio.start_server(plain_handler, "127.0.0.1", port)
        try:
            return await asyncio.to_thread(
                run_client,
                [
                    "--server",
                    f"127.0.0.1:{port}",
                    "--local-ports",
                    "8000",
                    "--tls-ca",
                    str(certs / "ca.pem"),
                ],
                timeout=30.0,
            )
        finally:
            server.close()
            await server.wait_closed()

    proc = asyncio.run(scenario())

    assert proc.returncode == 1, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
