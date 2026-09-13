# -*- coding: utf-8 -*-
"""
localtonet.gui.controller —— 异步侧：客户端生命周期与事件转发
================================================================
把 :class:`localtonet.client.core.TunnelClient` 接到界面上，只做三件事：

1. **生命周期**：:meth:`GuiController.start` 拉起 ``client.run()``，
   :meth:`GuiController.stop` 停掉它。界面上的"连接/断开"按钮背后就是这两个方法。
2. **事件转发**：订阅事件总线，把事件原样投进邮筒。**不做业务判断**——
   判断在 :mod:`localtonet.gui.viewmodel` 里，那里可以无头测试。
3. **提交映射**：:meth:`GuiController.submit_mapping` 直接调用既有的
   :meth:`TunnelClient.set_mapping`（协议与重传语义早就封装好了），
   把结果（或异常）统一成一份回执投给界面。

两条容易踩的线，这里明确一下：

* 心跳、重连、退避、映射 diff **全部是既有实现**，本模块一行都不重复。
  GUI 的价值是"少改 JSON、少重启进程"，不是重新发明隧道。
* 界面线程不许直接 ``await``。所有异步入口都是
  ``loop_thread.submit(controller.xxx())``。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Dict, List, Optional, Sequence

from config import ClientConfig, MappingRule
from localtonet.client.core import TunnelClient
from localtonet.core.events import EventType
from localtonet.errors import TunnelError
from localtonet.gui.bridge import LoopThread, UiBridge
from logging_setup import get_logger

__all__ = ["SUBSCRIPTIONS", "GuiController"]

SUBSCRIPTIONS: Sequence[str] = (
    EventType.CONTROL_CONNECTED,
    EventType.CLIENT_REGISTERED,
    EventType.RECONNECTING,
    EventType.CONTROL_LOST,
    EventType.MAPPING_CHANGED,
    EventType.REQUEST_END,
    EventType.CONN_ERROR,
)
"""转发给界面的事件白名单。

刻意**不含** ``REQUEST_START`` / ``DATA_CHANNEL_OPENED``：它们每次请求都触发，
而界面上的"活跃转发""请求数""上下行字节"已经由 ``snapshot()`` 覆盖。
把高频事件也塞进邮筒，只会让日志面板被自己刷爆。"""


class GuiController:
    """界面 ↔ 客户端之间的异步适配层。"""

    def __init__(
        self,
        config: ClientConfig,
        *,
        loop_thread: LoopThread,
        bridge: UiBridge,
        client: Optional[TunnelClient] = None,
        client_factory: Optional[Callable[[ClientConfig], TunnelClient]] = None,
        snapshot_interval: float = 0.5,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._config = config
        self._loop_thread = loop_thread
        self._bridge = bridge
        self._log = logger or get_logger("gui.controller")
        factory = client_factory or (lambda cfg: TunnelClient(cfg))
        self._client = client if client is not None else factory(config)
        self._snapshot_interval = snapshot_interval

        self._unsubscribes: List[Callable[[], None]] = []
        self._client_task: Optional[asyncio.Task] = None
        self._snapshot_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------ #
    # 只读
    # ------------------------------------------------------------------ #

    @property
    def client(self) -> TunnelClient:
        return self._client

    @property
    def config(self) -> ClientConfig:
        return self._config

    @property
    def running(self) -> bool:
        return self._client_task is not None and not self._client_task.done()

    @property
    def online(self) -> bool:
        return self._client.state == "online"

    def snapshot(self) -> Dict[str, Any]:
        """当前快照（只应在事件循环线程里调用）。"""
        return self._client.snapshot()

    # ------------------------------------------------------------------ #
    # 生命周期（在事件循环线程里执行）
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        """订阅事件、开始周期快照、拉起客户端主循环。幂等。"""
        if self.running:
            self._log.debug("客户端已在运行，忽略重复的 start()")
            return

        self._subscribe()
        # 先给界面一张"起跑线快照"，否则状态栏要空等一个采样周期
        self._bridge.post("snapshot", snapshot=self._client.snapshot())
        self._client_task = asyncio.create_task(self._client.run(), name="gui-client-run")
        self._snapshot_task = asyncio.create_task(self._snapshot_loop(), name="gui-snapshot-loop")
        self._log.info("GUI 已启动客户端 %s", self._client.client_id)

    async def stop(self) -> None:
        """停掉客户端与所有附属任务。幂等，可重复调用（窗口关闭时会再调一次）。"""
        client_task, self._client_task = self._client_task, None
        snapshot_task, self._snapshot_task = self._snapshot_task, None

        await self._client.stop()

        for task in (client_task, snapshot_task):
            if task is None:
                continue
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self._unsubscribe_all()
        self._bridge.post("snapshot", snapshot=self._client.snapshot())
        self._log.info("GUI 已停止客户端 %s", self._client.client_id)

    async def submit_mapping(self, rules: Sequence[MappingRule]) -> Dict[str, Any]:
        """提交映射表并把回执投给界面。

        失败（控制连接没建立、服务端拒绝、回执超时）会被统一成
        ``{"ok": False, "msg": ...}``——界面只需要处理**一种**回执形状，
        不必到处 ``try/except``。真实的失败原因原样保留在 ``msg`` 里。
        """
        try:
            result = dict(await self._client.set_mapping(rules))
        except (TunnelError, TimeoutError, OSError) as exc:
            self._log.warning("提交映射失败：%s", exc)
            result = {"ok": False, "msg": f"提交失败：{exc}"}

        self._bridge.post("mapping_result", result=result)
        return result

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    def _subscribe(self) -> None:
        if self._unsubscribes:
            return
        bus = self._client.events
        for name in SUBSCRIPTIONS:
            self._unsubscribes.append(bus.on(name, self._make_forwarder(name)))

    def _make_forwarder(self, name: str) -> Callable[..., None]:
        """生成事件转发器。

        这个函数在**事件循环线程**里被同步调用，所以投递必须非阻塞——
        :meth:`UiBridge.post` 用的就是 ``put_nowait``。
        """

        def forward(**payload: Any) -> None:
            self._bridge.post("event", name=name, payload=payload)

        return forward

    def _unsubscribe_all(self) -> None:
        for unsubscribe in self._unsubscribes:
            try:
                unsubscribe()
            except Exception:  # noqa: BLE001 - 退订失败不该影响关停
                self._log.exception("取消事件订阅失败")
        self._unsubscribes.clear()

    async def _snapshot_loop(self) -> None:
        """周期把 ``client.snapshot()`` 投给界面。

        为什么不让界面直接读 ``client``？因为那是**跨线程**读一个正在被网络回调
        改写的对象。走邮筒之后，状态只在事件循环线程里被读取，
        界面拿到的是不可变的字典副本，"读到一半被改掉"的竞态不存在。
        """
        try:
            while True:
                self._bridge.post("snapshot", snapshot=self._client.snapshot())
                await asyncio.sleep(self._snapshot_interval)
        except asyncio.CancelledError:
            raise

    def __repr__(self) -> str:
        return f"GuiController(client={self._client.client_id!r}, running={self.running})"
