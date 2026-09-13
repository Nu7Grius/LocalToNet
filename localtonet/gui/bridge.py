# -*- coding: utf-8 -*-
"""
localtonet.gui.bridge —— asyncio 线程 ↔ tkinter 线程的桥
==========================================================
界面和网络有两条**互斥**的硬约束：

* tkinter 的所有控件调用必须发生在**创建它的那个线程**（主线程），否则随机崩溃；
* ``asyncio`` 的事件循环一旦 ``run_forever`` 就会独占所在线程。

结论只有一个：**事件循环必须搬到后台线程**，两个世界之间只留一条受控的接缝。
本模块就是那条接缝，只有两个对象：

:class:`LoopThread`
    在独立线程里跑事件循环。提供 :meth:`LoopThread.submit`（提交协程，
    返回 ``concurrent.futures.Future``）与 :meth:`LoopThread.stop`（干净收尾）。
    界面线程想调异步 API，只有这一个入口。

:class:`UiBridge`
    工作线程 → 界面线程的**单向**邮筒。订阅者在事件循环线程里被同步调用，
    因此投递必须是**非阻塞**的：界面卡住时宁可丢显示消息，也绝不能把网络线程一起拖住。
    界面线程用 :meth:`UiBridge.drain` 批量取走。

本模块不 import tkinter，所以可以无头测试。界面线程"谁来定期取邮件"由
:mod:`localtonet.gui.app` 用 ``root.after`` 驱动——不用 ``event_generate``：
窗口销毁瞬间推事件会撞 TclError，而 ``after`` 可以在关闭时被干净地取消。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import queue
import threading
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from logging_setup import get_logger

__all__ = ["UiMessage", "LoopThread", "UiBridge"]

UiMessage = Tuple[str, Dict[str, Any]]
"""一条界面消息：``(kind, payload)``。``kind`` 取值见 :data:`UiBridge.KINDS`。"""


class LoopThread:
    """把一个 ``asyncio`` 事件循环放进独立的后台线程。"""

    def __init__(
        self,
        *,
        name: str = "localtonet-gui-loop",
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._name = name
        self._log = logger or get_logger("gui.bridge")
        self._loop = asyncio.new_event_loop()
        self._thread: Optional[threading.Thread] = None
        self._started = threading.Event()

    # ------------------------------------------------------------------ #

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        return self._loop

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and self._loop.is_running()

    @property
    def in_loop_thread(self) -> bool:
        """当前线程是不是事件循环线程（用于避免自锁）。"""
        return threading.current_thread() is self._thread

    def start(self, *, timeout: float = 5.0) -> "LoopThread":
        if self._thread is not None:
            return self
        self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
        self._thread.start()
        if not self._started.wait(timeout):  # pragma: no cover - 极端情况下的防御
            raise RuntimeError(f"事件循环线程 {self._name} 在 {timeout}s 内没有启动")
        return self

    def stop(self, *, timeout: float = 3.0) -> None:
        """停掉循环并等线程退出。幂等，且可从循环线程内部调用（此时只发停止信号）。"""
        thread, self._thread = self._thread, None
        if thread is None or not thread.is_alive():
            self._close_loop()
            return

        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except RuntimeError:  # pragma: no cover - 循环已关闭
            pass

        if not self.in_loop_thread:
            thread.join(timeout)
            if thread.is_alive():  # pragma: no cover - 只可能出现在收尾被卡住时
                self._log.warning("事件循环线程 %s 在 %.1fs 内没有退出", self._name, timeout)
        self._close_loop()

    def submit(self, coro: Awaitable[Any]) -> "concurrent.futures.Future[Any]":
        """提交一个协程到后台循环，返回可 ``.result()`` / ``.cancel()`` 的句柄。

        这是界面线程启动/停止客户端、提交映射的唯一通道。
        """
        if not self.running:
            close = getattr(coro, "close", None)
            if callable(close):  # 不关掉的话会留下 "coroutine was never awaited"
                close()
            raise RuntimeError("事件循环尚未启动，无法提交任务")
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def call(self, fn: Callable[[], None]) -> None:
        """在循环线程里执行一个同步回调（例如把事件投进邮筒）。"""
        if not self.running:  # pragma: no cover - 关停竞态
            return
        try:
            self._loop.call_soon_threadsafe(fn)
        except RuntimeError:  # pragma: no cover - 循环已关闭
            pass

    # ------------------------------------------------------------------ #

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._started.set()
        try:
            self._loop.run_forever()
        finally:
            self._drain_pending()
            try:
                self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            except (RuntimeError, OSError):  # pragma: no cover
                pass

    def _drain_pending(self) -> None:
        """停止前取消残留任务，否则解释器退出时会刷一屏 "Task was destroyed"。"""
        try:
            pending = [task for task in asyncio.all_tasks(self._loop) if not task.done()]
        except RuntimeError:  # pragma: no cover
            return
        for task in pending:
            task.cancel()
        if pending:
            try:
                self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except (RuntimeError, OSError):  # pragma: no cover
                pass

    def _close_loop(self) -> None:
        if self._loop.is_closed():
            return
        try:
            self._loop.close()
        except (RuntimeError, OSError):  # pragma: no cover
            pass

    def __repr__(self) -> str:
        return f"LoopThread(name={self._name!r}, running={self.running})"


class UiBridge:
    """工作线程 → 界面线程的单向邮筒。

    投递用 ``put_nowait``：队列满时**丢弃并计数**，绝不阻塞。
    理由很直接——投递发生在事件循环线程里，阻塞它等于阻塞整条隧道的数据转发，
    而代价只是界面少显示一条日志。
    """

    KINDS = ("event", "snapshot", "mapping_result", "local")
    """``kind`` 的合法取值。界面侧按这个分发。"""

    def __init__(
        self,
        *,
        maxsize: int = 4096,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._queue: "queue.Queue[UiMessage]" = queue.Queue(maxsize=maxsize)
        self._dropped = 0
        self._closed = False
        self._log = logger or get_logger("gui.bridge")

    # ------------------------------------------------------------------ #

    def post(self, kind: str, **payload: Any) -> None:
        """投递一条消息（线程安全、非阻塞）。"""
        if self._closed:
            return
        try:
            self._queue.put_nowait((kind, payload))
        except queue.Full:
            self._dropped += 1
            if self._dropped % 100 == 1:
                self._log.warning("界面消息队列已满，累计丢弃 %d 条（界面线程可能被卡住）", self._dropped)

    def drain(self, limit: int = 256) -> List[UiMessage]:
        """取走最多 ``limit`` 条消息（只在界面线程调用）。"""
        items: List[UiMessage] = []
        for _ in range(limit):
            try:
                items.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return items

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    @property
    def dropped(self) -> int:
        return self._dropped

    def close(self) -> None:
        """关闭邮筒：清空残留消息，之后的投递直接丢弃。"""
        self._closed = True
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

    def __repr__(self) -> str:
        return f"UiBridge(pending={self.pending}, dropped={self._dropped})"
