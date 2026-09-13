# -*- coding: utf-8 -*-
"""
localtonet.gui.viewmodel —— 界面侧的唯一状态所有者
====================================================
邮筒里的消息该怎样变成界面状态，全部逻辑都在这里，**一行 tkinter 都没有**。
界面层只做两件事：定时把邮筒倒进 :meth:`GuiViewModel.apply_all`，
再把 :attr:`GuiViewModel.state` 与 :attr:`GuiViewModel.table` 画出来。

这样切分的收益很实在：映射表编辑、脏标记、服务端推送覆盖策略、错误提示
这些真正会出错的地方，全都可以在无显示环境里被测试覆盖；
界面代码退化成"把字符串填进控件"，改版式不会碰到业务判断。

消息三种（kind 见 :class:`localtonet.gui.bridge.UiBridge`）：

``event``           事件总线里的一个事件（连接状态、映射变更、转发失败……）
``snapshot``        :meth:`TunnelClient.snapshot` 的定时快照，负责所有计数器
``mapping_result``  ``set_mapping`` 的回执，成功与否由它决定

**服务端推送与用户编辑的冲突策略**（本模块最需要小心的一处）：
服务端在任意客户端改映射后会广播给所有人。如果用广播直接刷新表格，
正在编辑的用户会被静默清空输入——他可能刚敲完十条。因此只要本地有未提交改动，
就**保留用户的输入**，只更新"已保存"快照并给出提示。
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any, Dict, Mapping, Optional, Sequence

from localtonet.core.events import EventType
from localtonet.gui.bridge import UiMessage
from localtonet.gui.model import (
    GuiState,
    MappingTableModel,
    adopt_snapshot,
    append_log,
    apply_event,
    note_mapping_result,
)
from logging_setup import get_logger

__all__ = ["GuiViewModel"]

REMOTE_STALE_NOTICE = "服务端映射表已更新，但你有未提交的改动——界面保留了你的输入，未覆盖"


class GuiViewModel:
    """把邮筒消息折叠成"表格 + 状态栏"两个可渲染对象。"""

    def __init__(
        self,
        *,
        table: Optional[MappingTableModel] = None,
        state: Optional[GuiState] = None,
        default_local_port: int = 8000,
        default_local_host: str = "127.0.0.1",
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._log = logger or get_logger("gui.viewmodel")
        self._table = table or MappingTableModel(
            default_local_port=default_local_port,
            default_local_host=default_local_host,
        )
        self._state = state or GuiState()

    # ------------------------------------------------------------------ #
    # 只读
    # ------------------------------------------------------------------ #

    @property
    def table(self) -> MappingTableModel:
        return self._table

    @property
    def state(self) -> GuiState:
        return self._state

    @property
    def notice(self) -> str:
        return self._state.notice

    def status_line(self) -> str:
        return self._state.status_line()

    # ------------------------------------------------------------------ #
    # 消息处理
    # ------------------------------------------------------------------ #

    def apply_all(self, messages: Sequence[UiMessage]) -> bool:
        """批量处理消息，返回"是否有任何变化"（界面据此决定要不要重绘）。"""
        changed = False
        for message in messages:
            changed = self.apply(message) or changed
        return changed

    def apply(self, message: UiMessage) -> bool:
        """处理一条消息。未知 kind 记一条日志并忽略——新增消息类型不会让界面崩掉。"""
        kind, payload = message
        if kind == "event":
            return self._apply_event(payload)
        if kind == "snapshot":
            snapshot = payload.get("snapshot")
            if isinstance(snapshot, Mapping):
                self._state = adopt_snapshot(self._state, snapshot)
                return True
            return False
        if kind == "mapping_result":
            result = payload.get("result")
            if isinstance(result, Mapping):
                self._apply_mapping_result(dict(result))
                return True
            return False
        if kind == "local":
            level = str(payload.get("level") or "info")
            text = str(payload.get("text") or "")
            if not text:
                return False
            self._state = append_log(self._state, level, text, notice=text if level in ("warn", "error") else None)
            return True
        self._log.warning("忽略未知的界面消息类型：%r", kind)
        return False

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    def _apply_event(self, payload: Mapping[str, Any]) -> bool:
        name = payload.get("name")
        if not isinstance(name, str) or not name:
            return False
        data = payload.get("payload")
        event_payload: Dict[str, Any] = dict(data) if isinstance(data, Mapping) else {}

        self._state = apply_event(self._state, name, **event_payload)

        if name == EventType.MAPPING_CHANGED:
            mapping = event_payload.get("mapping")
            if isinstance(mapping, (list, tuple)):
                adopted = self._table.load_remote(list(mapping))
                if not adopted:
                    # 有未提交改动：保住用户输入，只提示
                    self._state = replace(self._state, notice=REMOTE_STALE_NOTICE)
        return True

    def _apply_mapping_result(self, result: Dict[str, Any]) -> None:
        self._state = note_mapping_result(self._state, result)
        if result.get("ok"):
            # 界面上这份工作副本已经是服务端认下的版本，可以当作新的"已保存"基准
            self._table.mark_submitted()
