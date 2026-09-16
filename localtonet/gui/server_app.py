# -*- coding: utf-8 -*-
"""
localtonet.gui.server_app —— 服务端管理台的 tkinter 窗口
========================================================
版式：工具栏 → 在线客户端（只读）→ 映射表（可编辑）→ 事件日志 → 状态栏。

与客户端界面（:mod:`localtonet.gui.app`）的三处差别，都是**服务端视角**决定的：

1. **多一张只读的"在线客户端"表**。客户端界面只看得到自己，服务端才看得到
   "谁连上来了、身份是谁、认领了哪些端口"。这是本轮管理台存在的首要理由——
   在此之前服务端是个黑盒，只有一行行日志。
2. **生命周期按钮是"启动/停止服务端"**，不是"连接/断开"。
   客户端界面停掉的是自己；管理台停掉的是**运维正在托管的东西**（见 server_controller）。
3. **映射表提交后生效的范围是所有人**。客户端提交只影响自己认领的端口；
   管理台改的是服务端映射表，改完会广播给所有在线客户端，它们的界面会跟着变。

铁律照旧：**别在这里等异步结果**。按钮只投协程，结果由邮筒回传。

在线客户端表有**唯一一个写操作**：踢出选中客户端。它的门槛不是"权限模型"——
能打开这个窗口的人本来就能停服务端、改全局映射表，权限边界是"本机同进程"这件事本身
（见 README 的"管理台是本机同进程的"）。它要防的是**误点**：
确认框里把身份、对端、认领的端口列全，并说明端口归属会立刻释放、
冷却期内该客户端无法重连；真正的判定与执行都在服务端（``TunnelServer.kick_client``）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any, Dict, Optional

from config import ConfigError, ServerConfig
from localtonet.gui.bridge import LoopThread, UiBridge
from localtonet.gui.model import MappingRow, describe_tls, fmt_ports
from localtonet.gui.server_controller import ServerController
from localtonet.gui.server_model import ANONYMOUS_IDENTITY, ClientRow
from localtonet.gui.server_viewmodel import ServerViewModel
from localtonet.gui.widgets import (
    DIRTY_TAG_BG,
    PUMP_INTERVAL_MS,
    ask_mapping_row,
    build_log_panel,
)
from logging_setup import get_logger

__all__ = ["ServerGuiApp"]


class ServerGuiApp:
    """内网穿透服务端的本地管理台。"""

    def __init__(
        self,
        config: ServerConfig,
        *,
        autostart: bool = True,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._config = config
        self._log = logger or get_logger("gui.server_app")
        self._closed = False
        self._after_id: Optional[str] = None
        self._client_signature: Optional[Any] = None
        self._table_signature: Optional[Any] = None
        self._rendered_log_total = 0
        self._want_running = False
        """界面上"用户希望服务端处于运行状态"的意图。

        按钮可用性用它而不是去读 ``controller.running``：那是另一个线程正在改的字段，
        跨线程读它属于数据竞争。真实状态由 ``snapshot()`` 通过邮筒回传。
        """

        self._loop_thread = LoopThread(name="localtonet-server-gui-loop").start()
        self._bridge = UiBridge()
        self._controller = ServerController(config, loop_thread=self._loop_thread, bridge=self._bridge)
        self._vm = ServerViewModel(idle_timeout=config.timeouts.client_idle_timeout)

        self._root = tk.Tk()
        self._root.title(f"LocalToNet 管理台 · {config.name}")
        self._root.geometry("1080x760")
        self._root.minsize(900, 620)

        self._build_toolbar()
        self._build_clients()
        self._build_table()
        self._build_log()
        self._build_statusbar()

        self._root.protocol("WM_DELETE_WINDOW", self.close)
        self._render()
        self._pump()

        if autostart:
            self._root.after(200, self._on_start)

    # ------------------------------------------------------------------ #
    # 界面搭建
    # ------------------------------------------------------------------ #

    def _build_toolbar(self) -> None:
        bar = ttk.Frame(self._root, padding=(10, 10, 10, 4))
        bar.pack(fill=tk.X)

        self._buttons: Dict[str, ttk.Button] = {}

        def add(key: str, text: str, command, width: int = 13) -> None:
            button = ttk.Button(bar, text=text, command=command, width=width)
            button.pack(side=tk.LEFT, padx=2)
            self._buttons[key] = button

        add("start", "启动服务端", self._on_start)
        add("stop", "停止服务端", self._on_stop)
        ttk.Separator(bar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)
        add("add", "新增映射", self._on_add)
        add("duplicate", "复制", self._on_duplicate, width=8)
        add("remove", "删除", self._on_remove, width=8)
        ttk.Separator(bar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)
        add("submit", "提交映射", self._on_submit)
        add("revert", "放弃修改", self._on_revert)

        ttk.Label(bar, text="（双击映射表任意一行即可编辑）", foreground="#666666").pack(side=tk.LEFT, padx=8)

    def _build_clients(self) -> None:
        frame = ttk.LabelFrame(self._root, text="在线客户端", padding=8)
        frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=4)

        # 底部动作条必须**先** pack：tk 的 pack 是先到先得，先塞一个
        # fill=BOTH / expand=True 的 Treeview 会把剩余空间吃光，
        # 之后再 pack(side=BOTTOM) 的动作条只剩 0 高度（按钮在界面上"消失"）
        actions = ttk.Frame(frame)
        actions.pack(side=tk.BOTTOM, fill=tk.X, pady=(6, 0))
        self._buttons["kick"] = ttk.Button(
            actions, text="踢出选中客户端", command=self._on_kick, width=16
        )
        self._buttons["kick"].pack(side=tk.LEFT)
        ttk.Label(
            actions,
            text="（踢出会释放它认领的端口，并在冷却期内拒绝该客户端重连）",
            foreground="#666666",
        ).pack(side=tk.LEFT, padx=8)

        # 列序与 ClientRow.as_cells() 一致
        columns = ("identity", "client_id", "peer", "ports", "online", "idle")
        widths = (140, 160, 180, 220, 100, 120)

        self._clients = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse", height=6)
        for column, heading, width in zip(columns, ClientRow.COLUMNS, widths):
            self._clients.heading(column, text=heading)
            self._clients.column(column, width=width, anchor=tk.W, stretch=(column == "ports"))

        scrollbar = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self._clients.yview)
        self._clients.configure(yscrollcommand=scrollbar.set)
        self._clients.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        # 选中变化要**立刻**反映到按钮可用性：_render() 只在邮筒非空时跑，
        # 单靠它会出现"选中了一行但按钮还是灰的"（要等下一次快照/事件到达才更新）
        self._clients.bind("<<TreeviewSelect>>", lambda _event: self._render_buttons())

    def _build_table(self) -> None:
        frame = ttk.LabelFrame(self._root, text="映射表（公网端口 → 内网端口，提交后对所有客户端生效）", padding=8)
        frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=4)

        # 列顺序必须与 MappingRow.as_cells() 一致（外加末尾的"状态"列）
        columns = ("public_port", "local_port", "host", "local_host", "remark", "visitor_tls", "state")
        headings = ("公网端口", "内网端口", "监听地址", "内网地址", "备注", "访客 TLS", "状态")
        widths = (100, 100, 130, 130, 200, 80, 90)

        self._tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse")
        for column, heading, width in zip(columns, headings, widths):
            self._tree.heading(column, text=heading)
            self._tree.column(column, width=width, anchor=tk.W, stretch=(column == "remark"))
        self._tree.tag_configure("dirty", background=DIRTY_TAG_BG)

        scrollbar = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self._tree.yview)
        self._tree.configure(yscrollcommand=scrollbar.set)
        self._tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self._tree.bind("<Double-1>", self._on_row_double_click)

    def _build_log(self) -> None:
        self._log_view = build_log_panel(self._root, height=9)

    def _build_statusbar(self) -> None:
        self._status_var = tk.StringVar(value="就绪")
        ttk.Label(
            self._root,
            textvariable=self._status_var,
            anchor=tk.W,
            padding=(10, 6),
            relief=tk.SUNKEN,
        ).pack(fill=tk.X, side=tk.BOTTOM)

    # ------------------------------------------------------------------ #
    # 主循环：邮筒 → ViewModel → 界面
    # ------------------------------------------------------------------ #

    def _pump(self) -> None:
        if self._closed:
            return
        messages = self._bridge.drain()
        if messages:
            self._vm.apply_all(messages)
            self._render()
        self._after_id = self._root.after(PUMP_INTERVAL_MS, self._pump)

    def _render(self) -> None:
        self._render_clients()
        self._render_table()
        self._render_log()
        self._render_buttons()
        self._status_var.set(self._vm.status_line() + (f"　|　{self._vm.notice}" if self._vm.notice else ""))

    def _render_clients(self) -> None:
        state = self._vm.state
        rows = state.clients
        signature = tuple(row.as_cells(idle_timeout=state.idle_timeout) for row in rows)
        if signature == self._client_signature:
            return
        self._client_signature = signature

        self._clients.delete(*self._clients.get_children())
        for index, row in enumerate(rows):
            self._clients.insert(
                "",
                tk.END,
                iid=str(index),
                values=row.as_cells(idle_timeout=state.idle_timeout),
            )

    def _render_table(self) -> None:
        table = self._vm.table
        rows = table.rows
        dirty = set(table.dirty_indexes())
        signature = (
            tuple((row.signature(), index in dirty) for index, row in enumerate(rows)),
            table.is_dirty,
            table.remote_stale,
        )
        if signature == self._table_signature:
            return
        self._table_signature = signature

        selected = self._selected_public_port()
        self._tree.delete(*self._tree.get_children())
        for index, row in enumerate(rows):
            is_dirty = index in dirty
            self._tree.insert(
                "",
                tk.END,
                iid=str(index),
                values=row.as_cells() + (describe_tls(row.tls), "已修改" if is_dirty else ""),
                tags=("dirty",) if is_dirty else (),
            )
        if selected is not None:
            index = table.index_of(selected)
            if 0 <= index < len(rows):
                self._tree.selection_set(str(index))

    def _render_log(self) -> None:
        state = self._vm.state
        total = state.log_total
        if total == self._rendered_log_total:
            return
        # 用"累计条数"而不是 len(log) 算增量：日志环形截断后长度会停在上限，
        # 用长度判断会导致面板从此不再刷新。
        new_count = min(total - self._rendered_log_total, len(state.log))
        fresh = state.log[len(state.log) - new_count :] if new_count else ()

        self._log_view.configure(state=tk.NORMAL)
        for entry in fresh:
            self._log_view.insert(tk.END, f"{entry.ts}  {entry.text}\n", entry.level)
        self._rendered_log_total = total
        self._log_view.see(tk.END)
        self._log_view.configure(state=tk.DISABLED)

    def _render_buttons(self) -> None:
        table = self._vm.table
        running = self._want_running
        has_selection = self._selected_index() is not None

        self._buttons["start"].configure(state=tk.DISABLED if running else tk.NORMAL)
        self._buttons["stop"].configure(state=tk.NORMAL if running else tk.DISABLED)
        # 提交必须等服务端真的在跑：映射表的 apply() 要起监听，
        # 在没启动的服务端上提交会得到一份"监听已起但控制通道没开"的半截状态
        self._buttons["submit"].configure(state=tk.NORMAL if (table.is_dirty and running) else tk.DISABLED)
        self._buttons["revert"].configure(state=tk.NORMAL if table.is_dirty else tk.DISABLED)
        self._buttons["add"].configure(state=tk.NORMAL)
        for key in ("duplicate", "remove"):
            self._buttons[key].configure(state=tk.NORMAL if has_selection else tk.DISABLED)
        # 踢人：必须选中一行 + 服务端真的在跑。不额外要求"有权限"——
        # 能打开这个窗口的人本来就能停服务端、改全局映射表（见模块文档的边界说明）
        self._buttons["kick"].configure(
            state=tk.NORMAL if (running and self._selected_client() is not None) else tk.DISABLED
        )

    # ------------------------------------------------------------------ #
    # 动作：生命周期
    # ------------------------------------------------------------------ #

    def _on_start(self) -> None:
        if self._want_running:
            self._notice("info", "服务端已在运行")
            return
        self._want_running = True
        self._notice("info", "正在启动服务端…")
        self._submit_coroutine(self._controller.start(), "启动服务端")

    def _on_stop(self) -> None:
        if not self._want_running:
            self._notice("info", "服务端本来就未运行")
            return
        if not messagebox.askyesno(
            "确认停止",
            "停止服务端会断开所有在线客户端、关闭全部访客端口。\n确定要停止吗？",
        ):
            return
        self._want_running = False
        self._notice("info", "正在停止服务端…")
        self._submit_coroutine(self._controller.stop(), "停止服务端")

    # ------------------------------------------------------------------ #
    # 动作：踢出客户端
    # ------------------------------------------------------------------ #

    def _on_kick(self) -> None:
        """踢掉在线表里选中的那个客户端。

        这是本窗口**唯一直接断人**的操作，所以门槛刻意比"删除映射"更高一档：
        确认框里把它会掐断什么（身份、对端、认领的端口）完整列出来再问一次。
        真正的判定与执行都在服务端（``TunnelServer.kick_client``），
        界面只负责"确认 + 投协程"，不在这一侧做"它到底还该不该被踢"的判断——
        采样每 0.5 秒一次，界面手上的名单天生是滞后的。
        """
        row = self._selected_client()
        if row is None:
            self._notice("warn", "请先选中一个在线客户端")
            return
        if not self._want_running:
            self._notice("warn", "服务端未运行，无法踢出客户端")
            return
        if not messagebox.askyesno("确认踢出客户端", self._kick_confirm_text(row), default=messagebox.NO):
            return
        self._notice("info", f"正在踢出 {row.client_id}…")
        self._submit_coroutine(self._controller.kick_client(row.client_id), "踢出客户端")

    @staticmethod
    def _kick_confirm_text(row: ClientRow) -> str:
        """确认框文案：把"点下去会掐断什么"写在点之前。

        这一段刻意照抄运维的排查顺序（是谁 → 从哪来 → 占了哪些端口 → 之后会怎样），
        因为误点一个"踢出"按钮的代价是正在服务的隧道被掐断，
        而且用户在表格里看到的名单可能已经滞后一个采样周期了。
        """
        return (
            "确定要踢出这个客户端吗？\n\n"
            f"身份：{row.identity or ANONYMOUS_IDENTITY}\n"
            f"客户端：{row.client_id or '-'}\n"
            f"对端：{row.peer or '-'}\n"
            f"认领端口：{fmt_ports(row.ports)}\n\n"
            "踢出后：\n"
            "· 它的控制连接立刻断开，正在通过它转发的请求会失败；\n"
            "· 上面这些端口的归属立即释放，别的客户端可以抢占；\n"
            "· 冷却期内（admin.kick_cooldown）它无法重新注册，冷却结束后会自己回来。"
        )

    # ------------------------------------------------------------------ #
    # 动作：映射表编辑
    # ------------------------------------------------------------------ #

    def _on_add(self) -> None:
        draft = MappingRow(
            public_port=self._vm.table.next_free_public_port(),
            local_port=self._default_local_port(),
            local_host=self._default_local_host(),
        )
        values = ask_mapping_row(self._root, "新增映射", draft)
        if values is None:
            return
        self._mutate(lambda: self._vm.table.add_row(**values), "新增")

    def _on_duplicate(self) -> None:
        index = self._selected_index()
        if index is None:
            self._notice("warn", "请先选中一行")
            return
        self._mutate(lambda: self._vm.table.duplicate_row(index), "复制")

    def _on_remove(self) -> None:
        index = self._selected_index()
        if index is None:
            self._notice("warn", "请先选中一行")
            return
        row = self._vm.table.row(index)
        if row is not None and not messagebox.askyesno(
            "确认删除", f"确定要删除映射 {row.describe()} 吗？\n提交后该公网端口将停止监听。"
        ):
            return
        self._mutate(lambda: self._vm.table.remove_row(index), "删除")

    def _on_row_double_click(self, event: Any) -> None:
        index = self._index_at_event(event)
        if index is None:
            return
        row = self._vm.table.row(index)
        if row is None:
            return
        values = ask_mapping_row(self._root, f"编辑映射 {row.describe()}", row)
        if values is None:
            return
        self._mutate(lambda: self._vm.table.update_row(index, **values), "修改")

    def _on_revert(self) -> None:
        self._vm.table.reset()
        self._notice("info", "已放弃未提交的修改")
        self._render()

    # ------------------------------------------------------------------ #
    # 动作：提交
    # ------------------------------------------------------------------ #

    def _on_submit(self) -> None:
        try:
            rules = self._vm.table.rules()  # 本地先过一遍，与服务端同一条规则
        except ConfigError as exc:
            self._notice("error", f"映射不合法：{exc}")
            return
        if not self._want_running:
            self._notice("warn", "服务端未运行，无法提交")
            return
        self._notice("info", f"正在提交 {len(rules)} 条映射…")
        self._submit_coroutine(self._controller.submit_mapping(rules), "提交映射")

    # ------------------------------------------------------------------ #
    # 跨线程调用
    # ------------------------------------------------------------------ #

    def _submit_coroutine(self, coro: Any, what: str) -> None:
        """把协程交给后台事件循环，**不在这里等结果**。

        结果通过邮筒回来（事件、快照、提交结果），异常在回调里变成一条日志。
        在界面线程等异步结果会把窗口冻住，这是 tkinter + asyncio 最常见的死法。
        """
        try:
            future = self._loop_thread.submit(coro)
        except RuntimeError as exc:
            self._notice("error", f"{what}失败：{exc}")
            return
        future.add_done_callback(lambda fut: self._on_coroutine_done(fut, what))

    def _on_coroutine_done(self, future: Any, what: str) -> None:
        """协程结束回调。**在工作线程里被调用**，所以只往邮筒里投消息。"""
        try:
            future.result()
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001 - 界面必须把异常显示出来而不是崩掉
            self._log.exception("%s 时异常", what)
            self._bridge.post("local", level="error", text=f"{what}出错：{exc}")

    # ------------------------------------------------------------------ #
    # 辅助
    # ------------------------------------------------------------------ #

    def _default_local_port(self) -> int:
        """新增映射时的默认内网端口：沿用服务端当前第一条规则，没有就给 8000。

        服务端配置里没有"客户端认领哪些内网端口"的概念（那是客户端自己的事），
        所以这里只能从已有映射里猜一个合理的起点。
        """
        rows = self._vm.table.rows
        return rows[0].local_port if rows else 8000

    def _default_local_host(self) -> str:
        rows = self._vm.table.rows
        return rows[0].local_host if rows else "127.0.0.1"

    def _mutate(self, action, verb: str) -> None:
        """执行一次表格编辑，把校验失败原样显示给用户。"""
        try:
            action()
        except ConfigError as exc:
            self._notice("error", f"{verb}失败：{exc}")
            return
        self._table_signature = None  # 强制重绘
        self._notice("info", f"已{verb}（还没提交，点“提交映射”后生效）")
        self._render()

    def _index_at_event(self, event: Any) -> Optional[int]:
        iid = self._tree.identify_row(event.y)
        if not iid:
            return None
        return int(iid)

    def _selected_client(self) -> Optional[ClientRow]:
        """在线表里当前选中的那一行；没选中或已被刷新掉时返回 ``None``。

        iid 就是行号（``_render_clients`` 如此插入），所以每份新快照重建表格后
        iid 会变、选中态会丢——这是"选中态天生可能失效"的原因，
        这里返回 ``None`` 让调用方走"请先选中一行"的提示，而不是抛索引错误。
        """
        selection = self._clients.selection()
        if not selection:
            return None
        rows = self._vm.state.clients
        index = int(selection[0])
        return rows[index] if 0 <= index < len(rows) else None

    def _selected_index(self) -> Optional[int]:
        selection = self._tree.selection()
        if not selection:
            return None
        index = int(selection[0])
        return index if self._vm.table.row(index) is not None else None

    def _selected_public_port(self) -> Optional[int]:
        index = self._selected_index()
        row = self._vm.table.row(index) if index is not None else None
        return row.public_port if row is not None else None

    def _notice(self, level: str, text: str) -> None:
        """界面自己产生的提示：走 ViewModel 统一落进日志与状态栏。"""
        self._log.debug("%s", text)
        self._vm.apply(("local", {"level": level, "text": text}))
        self._render()

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    def run(self) -> int:
        """进入 tkinter 主循环，直到窗口关闭。"""
        self._root.mainloop()
        return 0

    def close(self) -> None:
        """关窗口：停掉服务端与后台循环。重复调用安全。"""
        if self._closed:
            return
        self._closed = True
        if self._after_id is not None:
            try:
                self._root.after_cancel(self._after_id)
            except tk.TclError:  # pragma: no cover - 窗口已销毁
                pass
            self._after_id = None

        # 不等结果：关窗口时用户要的是"立刻关掉"。
        # 服务端的访客端口由 stop() 关闭，LoopThread.stop 只负责取消残留任务。
        with contextlib.suppress(Exception):
            self._loop_thread.submit(self._controller.stop())
        self._bridge.close()
        self._loop_thread.stop(timeout=2.0)
        try:
            self._root.destroy()
        except tk.TclError:  # pragma: no cover
            pass
        self._log.info("管理台已关闭")


def create_server_app(config: ServerConfig, **kwargs: Any) -> ServerGuiApp:
    """构造并返回管理台应用（供 :func:`localtonet.gui.create_server_app` 调用）。"""
    return ServerGuiApp(config, **kwargs)
