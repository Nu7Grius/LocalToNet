# -*- coding: utf-8 -*-
"""
localtonet.gui.widgets —— 两个界面共享的 tkinter 构件与常量
============================================================
客户端界面（:mod:`localtonet.gui.app`）与服务端管理台
（:mod:`localtonet.gui.server_app`）都要画日志面板、都要编辑同一种映射行，
**差别只在标签文案**。所以这些构件放在这里共享，而不是各写一份。

为什么值得单独开一个模块：编辑对话框里那个"访客 TLS 必须三态"的坑
（``None`` 是独立语义、"跟随服务端默认"不能被两态复选框吃掉）是**同一条**
业务约束。复制一份就意味着以后改 ``MappingRow`` 字段时只改一处，
另一处静默少一个字段——这种 bug 只会被用户发现。

本模块属于"界面层"，tkinter 允许出现在这里；但
:mod:`localtonet.gui` 的 ``__init__`` **不 import 它**，
所以没有 tkinter 的机器上导入核心包与无头层照常成功。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any, Dict, Optional, Tuple

from localtonet.gui.model import MappingRow

__all__ = [
    "DIRTY_TAG_BG",
    "LOG_COLORS",
    "PUMP_INTERVAL_MS",
    "TLS_BY_LABEL",
    "TLS_LABEL_BY_VALUE",
    "TLS_OPTIONS",
    "ask_mapping_row",
    "build_log_panel",
]

PUMP_INTERVAL_MS = 100
"""界面刷新间隔。100ms 足够跟手，又不会让空闲时的 CPU 占用难看。"""

LOG_COLORS = {
    "ok": "#1a7f37",
    "info": "#3a3a3a",
    "warn": "#b26a00",
    "error": "#c0392b",
}

DIRTY_TAG_BG = "#fff4c2"
"""有未提交改动的行用一种柔和的琥珀色标出来——比加一列"是否修改"更直观。"""

TLS_OPTIONS: Tuple[Tuple[str, Optional[bool]], ...] = (
    ("跟随服务端默认", None),
    ("开（TLS 终止）", True),
    ("关（明文）", False),
)
"""访客端口 TLS 的下拉选项。

**三态缺一不可**：``None``（跟随）是独立语义，不是"没说"。用两态复选框会让
"跟随默认"这个状态无法表达，用户一打开编辑框就把端口的表态改掉了。
"""

TLS_BY_LABEL = {label: value for label, value in TLS_OPTIONS}
TLS_LABEL_BY_VALUE = {value: label for label, value in TLS_OPTIONS}


def build_log_panel(parent: tk.Misc, *, title: str = "事件日志", height: int = 10) -> tk.Text:
    """搭一个只读、带滚动条、按级别着色的日志面板，返回那个 ``Text`` 控件。"""
    frame = ttk.LabelFrame(parent, text=title, padding=8)
    frame.pack(fill=tk.BOTH, padx=10, pady=4)

    view = tk.Text(frame, height=height, wrap=tk.NONE, state=tk.DISABLED, background="#fbfbfb")
    scrollbar = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=view.yview)
    view.configure(yscrollcommand=scrollbar.set)
    view.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
    for level, color in LOG_COLORS.items():
        view.tag_configure(level, foreground=color)
    return view


def ask_mapping_row(parent: tk.Misc, title: str, initial: MappingRow) -> Optional[Dict[str, Any]]:
    """弹出映射行编辑对话框，返回**原始值字典**（校验仍由模型层负责）。

    返回 ``None`` 表示用户取消。端口只做"是不是整数"的格式检查——
    范围校验是 :func:`localtonet.core.rules.parse_mapping` 的职责，
    这里再写一遍就会与服务端规则分叉。
    """
    dialog = tk.Toplevel(parent)
    dialog.title(title)
    dialog.transient(parent)
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

    # 访客 TLS 是**三态**，用只读下拉而不是复选框（见 TLS_OPTIONS 的说明）
    tls_row = len(fields)
    ttk.Label(dialog, text="访客 TLS").grid(row=tls_row, column=0, sticky=tk.W, padx=10, pady=6)
    tls_box = ttk.Combobox(
        dialog,
        values=[label for label, _ in TLS_OPTIONS],
        state="readonly",
        width=33,
    )
    tls_box.set(TLS_LABEL_BY_VALUE[initial.tls])
    tls_box.grid(row=tls_row, column=1, sticky=tk.EW, padx=10, pady=6)

    result: Dict[str, Any] = {}

    def confirm() -> None:
        try:
            result.update(
                public_port=int(entries["public_port"].get().strip()),
                local_port=int(entries["local_port"].get().strip()),
                host=entries["host"].get().strip() or "0.0.0.0",
                local_host=entries["local_host"].get().strip() or "127.0.0.1",
                remark=entries["remark"].get().strip(),
                tls=TLS_BY_LABEL[tls_box.get()],
            )
        except ValueError:
            messagebox.showerror("格式错误", "端口必须是 1-65535 的整数", parent=dialog)
            return
        dialog.destroy()

    def cancel() -> None:
        dialog.destroy()

    buttons = ttk.Frame(dialog)
    buttons.grid(row=tls_row + 1, column=0, columnspan=2, pady=(4, 10))
    ttk.Button(buttons, text="确定", command=confirm, width=10).pack(side=tk.LEFT, padx=6)
    ttk.Button(buttons, text="取消", command=cancel, width=10).pack(side=tk.LEFT, padx=6)
    dialog.bind("<Return>", lambda _event: confirm())
    dialog.bind("<Escape>", lambda _event: cancel())

    dialog.grab_set()
    parent.wait_window(dialog)
    return result or None
