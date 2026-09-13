# -*- coding: utf-8 -*-
"""
tests/test_gui_bridge.py —— asyncio 线程与界面线程之间那座桥的测试
==================================================================
这座桥是 GUI 里唯一涉及**线程**的地方，也是最容易出"偶发崩溃"的地方，
所以不能只靠肉眼。这里验证三件事：

1. 后台线程里的事件循环真的能跑，并且能跑**真实 TCP I/O**
   （Windows 的 proactor 循环在非主线程里同样要正常，这点必须实测）；
2. 界面线程取消息的顺序与批量上限符合预期；
3. 队列满时**丢弃并计数**，绝不阻塞——投递发生在事件循环线程里，
   一旦阻塞就等于阻塞整条隧道的数据转发。
"""

from __future__ import annotations

import asyncio
import socket

import pytest

from localtonet.gui.bridge import LoopThread, UiBridge


# --------------------------------------------------------------------------- #
# LoopThread
# --------------------------------------------------------------------------- #


def test_loop_thread_runs_coroutine_and_returns_result() -> None:
    loop_thread = LoopThread().start()
    try:
        assert loop_thread.running
        assert loop_thread.submit(asyncio.sleep(0, result=42)).result(timeout=5) == 42
    finally:
        loop_thread.stop()
    assert not loop_thread.running


def test_loop_thread_runs_in_its_own_thread() -> None:
    loop_thread = LoopThread().start()
    try:
        import threading

        assert loop_thread.submit(asyncio.to_thread(threading.current_thread)).result(timeout=5) is not None
        assert loop_thread.in_loop_thread is False
        assert loop_thread.loop.is_running()
    finally:
        loop_thread.stop()


def test_loop_thread_stop_is_idempotent() -> None:
    loop_thread = LoopThread().start()
    loop_thread.stop()
    loop_thread.stop()
    assert not loop_thread.running


def test_submit_before_start_raises_and_closes_coroutine() -> None:
    loop_thread = LoopThread()
    coro = asyncio.sleep(0)
    with pytest.raises(RuntimeError, match="事件循环尚未启动"):
        loop_thread.submit(coro)
    assert coro.cr_frame is None  # 已关闭，不会留下 "never awaited" 警告
    loop_thread.stop()


def test_background_loop_serves_real_tcp_io() -> None:
    """在后台循环里起 echo 服务，再从测试线程用阻塞 socket 连它。

    这是"桥能不能用"的最终判据：事件循环跑起来了但 I/O 不通，
    界面照样什么都收不到。
    """

    async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        data = await reader.read(100)
        writer.write(bytes(data).upper())
        await writer.drain()
        writer.close()

    async def start_echo() -> tuple[asyncio.AbstractServer, int]:
        server = await asyncio.start_server(echo, "127.0.0.1", 0)
        return server, int(server.sockets[0].getsockname()[1])

    async def shutdown(server: asyncio.AbstractServer) -> None:
        server.close()
        await server.wait_closed()

    loop_thread = LoopThread().start()
    try:
        server, port = loop_thread.submit(start_echo()).result(timeout=5)
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            sock.sendall(b"ping")
            assert sock.recv(100) == b"PING"
        loop_thread.submit(shutdown(server)).result(timeout=5)
    finally:
        loop_thread.stop()


def test_stop_cancels_leftover_tasks() -> None:
    """残留任务必须在关停时被取消，否则解释器退出会刷一屏 "Task was destroyed"。"""
    loop_thread = LoopThread().start()
    started = asyncio.Event()

    async def sleeper() -> None:
        started.set()
        await asyncio.sleep(30)

    future = loop_thread.submit(sleeper())
    loop_thread.submit(_wait(started)).result(timeout=5)

    loop_thread.stop()

    assert future.cancelled() or future.done()


async def _wait(event: asyncio.Event) -> None:
    await event.wait()


# --------------------------------------------------------------------------- #
# UiBridge
# --------------------------------------------------------------------------- #


def test_ui_bridge_drain_preserves_order_and_respects_limit() -> None:
    bridge = UiBridge()
    for index in range(5):
        bridge.post("event", n=index)

    first = bridge.drain(limit=3)

    assert [payload["n"] for _, payload in first] == [0, 1, 2]
    assert bridge.pending == 2
    assert [payload["n"] for _, payload in bridge.drain()] == [3, 4]
    assert bridge.pending == 0


def test_ui_bridge_drops_instead_of_blocking_when_full() -> None:
    bridge = UiBridge(maxsize=2)
    for index in range(5):
        bridge.post("event", n=index)

    assert bridge.pending == 2
    assert bridge.dropped == 3
    assert [payload["n"] for _, payload in bridge.drain()] == [0, 1]


def test_ui_bridge_close_discards_and_stops_accepting() -> None:
    bridge = UiBridge()
    bridge.post("event", n=1)

    bridge.close()
    assert bridge.pending == 0

    bridge.post("event", n=2)
    assert bridge.pending == 0


def test_ui_bridge_receives_messages_posted_from_loop_thread() -> None:
    """跨线程投递：工作线程写、界面线程读，两边不能互相等。"""
    loop_thread = LoopThread().start()
    bridge = UiBridge()
    try:
        async def produce() -> None:
            for index in range(10):
                bridge.post("event", n=index)

        loop_thread.submit(produce()).result(timeout=5)
        assert [payload["n"] for _, payload in bridge.drain()] == list(range(10))
    finally:
        loop_thread.stop()
