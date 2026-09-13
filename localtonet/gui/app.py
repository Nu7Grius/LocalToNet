# -*- coding: utf-8 -*-
"""
localtonet.gui.app —— tkinter 界面（唯一依赖 tkinter 的模块）
==============================================================
界面只负责**画**和**收事件**：

* 每 100ms 把 :class:`localtonet.gui.bridge.UiBridge` 里的消息倒给
  :class:`localtonet.gui.viewmodel.GuiViewModel`，再把结果画出来；
* 按钮点了什么，就调 :class:`localtonet.gui.controller.GuiController` 的哪个方法。

映射的增删改查**没有一行业务逻辑在这里**——校验走
:func:`localtonet.core.rules.parse_mapping`（与服务端同一个函数），
提交走 :meth:`TunnelClient.set_mapping`。界面坏了顶多是看不清，
绝不会出现"界面说没问题、服务端报错"这种两边规则不一致的怪事。

界面线程与事件循环的边界见 :mod:`localtonet.gui.bridge`；这里只需要记住一条铁律：
**别在这里等异步结果**（不要对 ``concurrent.futures.Future`` 调 ``.result()``），
一等就会把整个窗口冻住。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any, Dict, Optional

from config import ClientConfig, ConfigError
from localtonet.gui.bridge import LoopThread, UiBridge
from localtonet.gui.controller import GuiController
from localtonet.gui.model import MappingRow
from localtonet.gui.viewmodel import GuiViewModel
from logging_setup import get_logger

__all__ = ["TunnelGuiApp", "PUMP_INTERVAL_MS"]

PUMP_INTERVAL_MS = 100
"""界面刷新间隔。100ms 足够跟手，又不会让空闲时的 CPU 占用难看。"""

_LOG_COLORS = {
    "ok": "#1a7f37",
    "info": "#3a3a3a",
    "warn": "#b26a00",
    "error": "#c0392b",
}

_DIRTY_TAG_BG = "#fff4c2"
"""有未提交改动的行用一种柔和的琥珀色标出来——比加一列"是否修改"更直观。"""


class TunnelGuiApp:
    """内网穿透客户端的图形外壳。"""

    def __init__(
        self,
        config: ClientConfig,
        *,
        autostart: bool = True,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._config = config
        self._log = logger or get_logger("gui.app")
        self._closed = False
        self._after_id: Optional[str] = None
        self._table_signature: Optional[tuple] = None
        self._rendered_log_total = 0
        self._want_running = False
        """界面上"用户希望客户端处于运行状态"的意图。

        按钮可用性用它而不是去读客户端内部字段：那是另一个线程正在改的对象，
        跨线程读它属于数据竞争。真实状态仍由 ``snapshot()`` 通过邮筒回传，
        两者短暂不一致时以快照为准（例如鉴权失败后客户端自行停止）。
        """

        self._loop_thread = LoopThread().start()
        self._bridge = UiBridge()
        self._controller = GuiController(config, loop_thread=self._loop_thread, bridge=self._bridge)
        self._vm = GuiViewModel(
            default_local_port=config.local_ports[0] if config.local_ports else 8000,
            default_local_host=config.local_host,
        )

        self._root = tk.Tk()
        self._root.title(f"LocalToNet 客户端 · {config.client_id or '自动标识'}")
        self._root.geometry("980x620")
        self._root.minsize(820, 520)

        self._build_toolbar()
        self._build_table()
        self._build_log()
        self._build_statusbar()

        self._root.protocol("WM_DELETE_WINDOW", self.close)
        self._render()
        self._pump()

        if autostart:
            self._root.after(200, self._on_connect)

    # ------------------------------------------------------------------ #
    # 界面搭建
    # ------------------------------------------------------------------ #

    def _build_toolbar(self) -> None:
        bar = ttk.Frame(self._root, padding=(10, 10, 10, 4))
        bar.pack(fill=tk.X)

        self._buttons: Dict[str, ttk.Button] = {}

        def add(key: str, text: str, command) -> None:
            button = ttk.Button(bar, text=text, command=command, width=12)
            button.pack(side=tk.LEFT, padx=2)
            self._buttons[key] = button

        add("connect", "连接", self._on_connect)
        add("disconnect", "断开", self._on_disconnect)
        ttk.Separator(bar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)
        add("add", "新增映射", self._on_add)
        add("duplicate", "复制", self._on_duplicate)
        add("remove", "删除", self._on_remove)
        ttk.Separator(bar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)
        add("submit", "提交", self._on_submit)
        add("revert", "放弃修改", self._on_revert)

        ttk.Label(bar, text="（双击任意一行即可编辑）", foreground="#666666").pack(side=tk.LEFT, padx=8)

    def _build_table(self) -> None:
        frame = ttk.LabelFrame(self._root, text="映射表（公网端口 → 内网端口）", padding=8)
        frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=4)

        columns = ("public_port", "local_port", "host", "local_host", "remark", "state")
        headings = ("公网端口", "内网端口", "监听地址", "内网地址", "备注", "状态")
        widths = (100, 100, 130, 130, 260, 90)

        self._tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse")
        for column, heading, width in zip(columns, headings, widths):
            self._tree.heading(column, text=heading)
            self._tree.column(column, width=width, anchor=tk.W, stretch=(column == "remark"))
        self._tree.tag_configure("dirty", background=_DIRTY_TAG_BG)

        scrollbar = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self._tree.yview)
        self._tree.configure(yscrollcommand=scrollbar.set)
        self._tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self._tree.bind("<Double-1>", self._on_row_double_click)

    def _build_log(self) -> None:
        frame = ttk.LabelFrame(self._root, text="事件日志", padding=8)
        frame.pack(fill=tk.BOTH, padx=10, pady=4)

        self._log_view = tk.Text(frame, height=10, wrap=tk.NONE, state=tk.DISABLED, background="#fbfbfb")
        scrollbar = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self._log_view.yview)
        self._log_view.configure(yscrollcommand=scrollbar.set)
        self._log_view.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        for level, color in _LOG_COLORS.items():
            self._log_view.tag_configure(level, foreground=color)

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
        self._render_table()
        self._render_log()
        self._render_buttons()
        self._status_var.set(self._vm.status_line() + (f"　|　{self._vm.notice}" if self._vm.notice else ""))

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
                values=row.as_cells() + ("已修改" if is_dirty else "",),
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
        state = self._vm.state
        table = self._vm.table
        running = self._want_running and not state.fatal
        has_selection = self._selected_index() is not None

        self._buttons["connect"].configure(state=tk.DISABLED if running else tk.NORMAL)
        self._buttons["disconnect"].configure(state=tk.NORMAL if running else tk.DISABLED)
        self._buttons["submit"].configure(
            state=tk.NORMAL if (table.is_dirty and state.online and not state.fatal) else tk.DISABLED
        )
        self._buttons["revert"].configure(state=tk.NORMAL if table.is_dirty else tk.DISABLED)
        self._buttons["add"].configure(state=tk.NORMAL)
        for key in ("duplicate", "remove"):
            self._buttons[key].configure(state=tk.NORMAL if has_selection else tk.DISABLED)

    # ------------------------------------------------------------------ #
    # 动作：连接
    # ------------------------------------------------------------------ #

    def _on_connect(self) -> None:
        if self._want_running and not self._vm.state.fatal:
            self._notice("info", "客户端已在运行")
            return
        self._want_running = True
        self._notice("info", "正在连接服务端…")
        self._submit_coroutine(self._controller.start(), "启动客户端")

    def _on_disconnect(self) -> None:
        if not self._want_running:
            self._notice("info", "客户端本来就未运行")
            return
        self._want_running = False
        self._submit_coroutine(self._controller.stop(), "停止客户端")

    # ------------------------------------------------------------------ #
    # 动作：映射表编辑
    # ------------------------------------------------------------------ #

    def _on_add(self) -> None:
        draft = MappingRow(
            public_port=self._vm.table.next_free_public_port(),
            local_port=self._config.local_ports[0] if self._config.local_ports else 8000,
            local_host=self._config.local_host,
        )
        values = self._ask_row("新增映射", draft)
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
        values = self._ask_row(f"编辑映射 {row.describe()}", row)
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
            rules = self._vm.table.rules()  # 本地先过一遍，与服务器同一条规则
        except ConfigError as exc:
            self._notice("error", f"映射不合法：{exc}")
            return
        if not self._vm.state.online:
            self._notice("warn", "尚未连接服务端，无法提交")
            return
        self._notice("info", f"正在提交 {len(rules)} 条映射…")
        self._submit_coroutine(self._controller.submit_mapping(rules), "提交映射")

    # ------------------------------------------------------------------ #
    # 跨线程调用
    # ------------------------------------------------------------------ #

    def _submit_coroutine(self, coro: Any, what: str) -> None:
        """把协程交给后台事件循环，**不在这里等结果**。

        结果通过邮筒回来（事件、回执），异常在回调里变成一条日志。
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

    def _mutate(self, action, verb: str) -> None:
        """执行一次表格编辑，把校验失败原样显示给用户。"""
        try:
            action()
        except ConfigError as exc:
            self._notice("error", f"{verb}失败：{exc}")
            return
        self._table_signature = None  # 强制重绘
        self._notice("info", f"已{verb}（还没提交，点“提交”后生效）")
        self._render()

    def _index_at_event(self, event: Any) -> Optional[int]:
        iid = self._tree.identify_row(event.y)
        if not iid:
            return None
        return int(iid)

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

    def _ask_row(self, title: str, initial: MappingRow) -> Optional[Dict[str, Any]]:
        """弹出编辑对话框。返回值只含**字符串转好的原始值**，校验仍由模型层负责。"""
        dialog = tk.Toplevel(self._root)
        dialog.title(title)
        dialog.transient(self._root)
        dialog.resizable(False, False)

        fields = (
            ("public_port", "公网端口", str(initial.public_port)),
            ("local_port", "内网端口", str(initial.local_port)),
            ("host", "监听地址", initial.host),
            ("local_host", "内网地址", initial.local_host),
            ("remark", "备注", initial.remark),
        )
        entries: Dict[str, tk.Entry] = {}
        for row_index, (key, label, value) in enumerate(fields):
            ttk.Label(dialog, text=label).grid(row=row_index, column=0, sticky=tk.W, padx=10, pady=6)
            entry = ttk.Entry(dialog, width=36)
            entry.insert(0, value)
            entry.grid(row=row_index, column=1, sticky=tk.EW, padx=10, pady=6)
            entries[key] = entry
        entries["public_port"].focus_set()

        result: Dict[str, Any] = {}

        def confirm() -> None:
            try:
                result.update(
                    public_port=int(entries["public_port"].get().strip()),
                    local_port=int(entries["local_port"].get().strip()),
                    host=entries["host"].get().strip() or "0.0.0.0",
                    local_host=entries["local_host"].get().strip() or "127.0.0.1",
                    remark=entries["remark"].get().strip(),
                )
            except ValueError:
                messagebox.showerror("格式错误", "端口必须是 1-65535 的整数", parent=dialog)
                return
            dialog.destroy()

        def cancel() -> None:
            dialog.destroy()

        buttons = ttk.Frame(dialog)
        buttons.grid(row=len(fields), column=0, columnspan=2, pady=(4, 10))
        ttk.Button(buttons, text="确定", command=confirm, width=10).pack(side=tk.LEFT, padx=6)
        ttk.Button(buttons, text="取消", command=cancel, width=10).pack(side=tk.LEFT, padx=6)
        dialog.bind("<Return>", lambda _event: confirm())
        dialog.bind("<Escape>", lambda _event: cancel())

        dialog.grab_set()
        self._root.wait_window(dialog)
        return result or None

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    def run(self) -> int:
        """进入 tkinter 主循环，直到窗口关闭。"""
        self._root.mainloop()
        return 0

    def close(self) -> None:
        """关窗口：停掉后台循环并释放资源。重复调用安全。"""
        if self._closed:
            return
        self._closed = True
        if self._after_id is not None:
            try:
                self._root.after_cancel(self._after_id)
            except tk.TclError:  # pragma: no cover - 窗口已销毁
                pass
            self._after_id = None

        # 不等结果：关窗口时用户要的是"立刻关掉"，残留任务由 LoopThread.stop 取消。
        with contextlib.suppress(Exception):
            self._loop_thread.submit(self._controller.stop())
        self._bridge.close()
        self._loop_thread.stop(timeout=2.0)
        try:
            self._root.destroy()
        except tk.TclError:  # pragma: no cover
            pass
        self._log.info("界面已关闭")


def create_app(config: ClientConfig, **kwargs: Any) -> TunnelGuiApp:
    """构造并返回界面应用（供 :func:`localtonet.gui.create_app` 调用）。"""
    return TunnelGuiApp(config, **kwargs)
