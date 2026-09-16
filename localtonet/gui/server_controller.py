# -*- coding: utf-8 -*-
"""
localtonet.gui.server_controller —— 管理台 ↔ 服务端之间的异步适配层
====================================================================
与客户端侧 :class:`~localtonet.gui.controller.GuiController` 同一套结构
（事件循环在后台线程、跨线程只走邮筒），但有一处**本质差别**必须写在这儿：

客户端 GUI 是"界面拉着一条客户端"——``TunnelClient`` 自己不监听任何公网端口，
关掉窗口就等于停掉客户端，没人在意。管理台则相反：**服务端是运维真正在托管的东西**，
窗口只是贴在它身上的一只眼睛。所以本控制器明确承担服务端的生命周期所有权
（:meth:`start` 拉起、:meth:`stop` 优雅关停），而不只是一个状态转发器。

推论也要写进文档：**关掉管理台窗口 = 服务端下线**（访客端口全部关闭）。
要"服务端继续跑、窗口随时可开"，那是**远程管理**（协议扩展）那条路，不在本轮范围。

第二处差别是**没有回执报文**。客户端提交映射走 ``set_mapping`` 指令、等服务端回执；
管理台就在服务端进程里，直接调 :meth:`TunnelServer.submit_mapping` 拿返回值即可。
所以界面消息 ``mapping_result`` 的载荷由本地结果拼出来，形状与客户端侧保持一致
（``{"ok": bool, "msg": str}``），界面层不必区分两套。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Dict, List, Optional, Sequence

from config import ConfigError, MappingRule, ServerConfig
from localtonet.core.events import EventType
from localtonet.errors import TunnelError
from localtonet.gui.bridge import LoopThread, UiBridge
from localtonet.server.core import TunnelServer
from logging_setup import get_logger

__all__ = ["ServerController", "SERVER_SUBSCRIPTIONS"]

SERVER_SUBSCRIPTIONS = (
    EventType.SERVER_STARTED,
    EventType.SERVER_STOPPED,
    EventType.CLIENT_CONNECTED,
    EventType.CLIENT_DISCONNECTED,
    EventType.CONN_ERROR,
    EventType.MAPPING_REJECTED,
    EventType.REGISTRATION_REJECTED,
)
"""转发给管理台的事件白名单。

刻意**不含** ``REQUEST_START`` / ``REQUEST_END`` / ``DATA_CHANNEL_OPENED``：
服务端的 ``REQUEST_END`` 载荷里没有成功标志，写不出有信息量的日志，
而它每次请求都触发——塞进邮筒只会让面板被自己刷爆，
真正的计数已经由 ``snapshot()`` 覆盖。

``MAPPING_REJECTED`` 属于**安全事件**：它罕见、且运维必须看见
（"某个身份一直改不动映射表"要能一眼定位到是权限没给，而不是界面上瞎猜）。

``REGISTRATION_REJECTED`` 同理，但它是**运维排障的主力**：状态栏那个
"被拒 N"只回答"拒了几次"，回答不了"谁被拒、为什么"——客户端表现为
"一直连不上、日志里只有重连"，而原因（令牌失效 / 端口未授权 / 容量满 /
参数写错）全在这条事件里。拒绝本身不频繁（客户端有退避），不会刷屏。"""


class ServerController:
    """管理台 ↔ 服务端之间的异步适配层。"""

    def __init__(
        self,
        config: ServerConfig,
        *,
        loop_thread: LoopThread,
        bridge: UiBridge,
        server: Optional[TunnelServer] = None,
        server_factory: Optional[Callable[[ServerConfig], TunnelServer]] = None,
        snapshot_interval: float = 0.5,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._config = config
        self._loop_thread = loop_thread
        self._bridge = bridge
        self._log = logger or get_logger("gui.server_controller")
        self._server_factory = server_factory
        self._owns_server = server is None
        self._server = server if server is not None else self._new_server()
        self._snapshot_interval = snapshot_interval

        self._unsubscribes: List[Callable[[], None]] = []
        self._serve_task: Optional[asyncio.Task] = None
        self._snapshot_task: Optional[asyncio.Task] = None
        self._failure = ""
        self._stopped_once = False

    # ------------------------------------------------------------------ #
    # 只读
    # ------------------------------------------------------------------ #

    @property
    def server(self) -> TunnelServer:
        return self._server

    @property
    def config(self) -> ServerConfig:
        return self._config

    @property
    def running(self) -> bool:
        return self._serve_task is not None and not self._serve_task.done()

    @property
    def failure(self) -> str:
        """启动失败的原始消息；空串表示没失败过。界面据此决定要不要报警。"""
        return self._failure

    def snapshot(self) -> Dict[str, Any]:
        """当前快照（只应在事件循环线程里调用）。

        与客户端侧同理：**不让界面直接读 ``server``**——那是在另一个线程里读一个
        正被网络回调改写的对象。走邮筒之后，界面拿到的是不可变的字典副本。
        """
        return self._server.snapshot()

    # ------------------------------------------------------------------ #
    # 生命周期（在事件循环线程里执行）
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        """订阅事件、拉起服务端、开始周期快照。幂等，且**失败/停止后可以重来**。

        启动失败（端口被占、逐端口 TLS 缺证书、映射文件损坏……）**不抛给界面层**：
        界面在另一个线程里，抛出去只会变成一条没人看的堆栈。改为记下
        :attr:`failure` 并把原因投进日志面板——窗口留着，用户看得见为什么没起来，
        修好之后再点一次"启动"就能真重试（所以这里的早退只认 ``running``，不认 ``failure``）。

        :meth:`stop` 之后再次 :meth:`start`，会**新建一个 TunnelServer 实例**：
        ``TunnelServer.stop()`` 会把 ``_stopped`` 事件置位、把看门狗停掉，
        同一个实例再 ``start()`` 时 ``serve_forever()`` 会立刻返回——
        拿它重启只会得到一个"看起来活着其实不干活"的服务端。
        新建实例顺带复刻了命令行版"重启即新进程"的语义（映射持久化文件照常生效）。
        """
        if self.running:
            self._log.debug("服务端已在运行，忽略重复的 start()")
            return

        self._failure = ""
        if self._stopped_once:
            if not (self._owns_server or self._server_factory is not None):
                # 测试里注入的实例没有工厂，无法重建；如实记一条而不是假装启动成功
                self._failure = "注入的 TunnelServer 实例不支持重启"
                self._log.warning("%s，忽略 start()", self._failure)
                self._bridge.post("local", level="error", text=self._failure)
                return
            self._unsubscribe_all()
            self._server = self._new_server()
            self._log.info("已为重启创建新的 TunnelServer 实例")

        # 先订阅：SERVER_STARTED 是在 server.start() 里发出的，晚一步就漏了第一条
        self._subscribe()
        try:
            await self._server.start()
        except (ConfigError, TunnelError, OSError) as exc:
            self._failure = getattr(exc, "message", None) or str(exc)
            self._log.error("服务端启动失败：%s", exc)
            self._bridge.post("local", level="error", text=f"服务端启动失败：{self._failure}")
            self._bridge.post("snapshot", snapshot=self._server.snapshot())
            return

        self._bridge.post("snapshot", snapshot=self._server.snapshot())
        self._serve_task = asyncio.create_task(self._server.serve_forever(), name="gui-server-serve")
        self._snapshot_task = asyncio.create_task(self._snapshot_loop(), name="gui-server-snapshot")
        self._log.info("管理台已拉起服务端 %s", self._config.name)

    async def stop(self) -> None:
        """优雅关停服务端与所有附属任务。幂等，可重复调用（窗口关闭时会再调一次）。"""
        snapshot_task, self._snapshot_task = self._snapshot_task, None
        serve_task, self._serve_task = self._serve_task, None

        if snapshot_task is not None:
            snapshot_task.cancel()
            await asyncio.gather(snapshot_task, return_exceptions=True)

        # 服务端没起来过（或起了一半就失败）也要能安全收尾：stop() 自身幂等
        await self._server.stop()

        if serve_task is not None:
            if not serve_task.done():
                serve_task.cancel()
            await asyncio.gather(serve_task, return_exceptions=True)

        self._unsubscribe_all()
        self._bridge.post("snapshot", snapshot=self._server.snapshot())
        self._stopped_once = True
        self._log.info("管理台已停止服务端 %s", self._config.name)

    async def submit_mapping(self, rules: Sequence[MappingRule]) -> Dict[str, Any]:
        """提交映射表并把结果投给界面。

        校验失败（端口非法、逐端口 TLS 缺证书）会被统一成 ``{"ok": False, "msg": ...}``，
        界面只需要处理**一种**结果形状。``msg`` 用 ``exc.message``（不带 ``[code]`` 前缀）：
        这段文字直接显示在提示栏里，前缀会变成噪声。
        """
        try:
            diff = await self._server.submit_mapping(rules)
        except (ConfigError, TunnelError) as exc:
            self._log.warning("管理台提交映射被拒绝：%s", exc)
            result: Dict[str, Any] = {
                "ok": False,
                "msg": getattr(exc, "message", None) or str(exc),
            }
        else:
            result = {"ok": True, "msg": diff.describe()}

        self._bridge.post("mapping_result", result=result)
        return result

    async def kick_client(self, client_id: str) -> Dict[str, Any]:
        """踢出一个在线客户端并把结果投给界面。

        与 :meth:`submit_mapping` 同一套结果形状（``{"ok": bool, "msg": str}``），
        界面因此只需要处理一种形状。``ok=False`` 有两种来由，界面上都表现为一句可读的话：

        * **目标已经不在线**（界面每 0.5 秒采样一次，用户点下去时它可能刚自己掉线）——
          这是正常竞态，不是错误，所以不抛异常、不弹错误框；
        * 服务端拒绝（如 ``kick_cooldown`` 配上非法值导致配置错误），转成日志。

        踢掉之后**不需要**在这里手工发事件或刷新表格：断开走 ``TunnelServer`` 的下线内核，
        它会发 ``CLIENT_DISCONNECTED``（已在转发白名单里）并广播映射表，
        界面下一次采样就看不到这台机器了。
        """
        try:
            kicked = await self._server.kick_client(client_id)
        except (ConfigError, TunnelError) as exc:
            self._log.warning("管理台踢出客户端 %s 被拒绝：%s", client_id, exc)
            result: Dict[str, Any] = {
                "ok": False,
                "msg": getattr(exc, "message", None) or str(exc),
            }
        else:
            result = {
                "ok": kicked,
                "msg": (
                    f"已踢出 {client_id}"
                    if kicked
                    else f"客户端 {client_id} 已经不在线，无需踢出"
                ),
            }

        self._bridge.post("kick_result", result=result)
        return result

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    def _new_server(self) -> TunnelServer:
        """造一个新的 ``TunnelServer``（重启用；复刻命令行版"重启即新进程"的语义）。"""
        factory = self._server_factory or (lambda cfg: TunnelServer(cfg))
        return factory(self._config)

    def _subscribe(self) -> None:
        if self._unsubscribes:
            return
        bus = self._server.events
        for name in SERVER_SUBSCRIPTIONS:
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
        """周期把 ``server.snapshot()`` 投给界面（理由见 :meth:`snapshot`）。"""
        try:
            while True:
                self._bridge.post("snapshot", snapshot=self._server.snapshot())
                await asyncio.sleep(self._snapshot_interval)
        except asyncio.CancelledError:
            raise

    def __repr__(self) -> str:
        return f"ServerController(server={self._config.name!r}, running={self.running})"
