# -*- coding: utf-8 -*-
"""
localtonet.gui.server_viewmodel —— 管理台侧的唯一状态所有者
============================================================
与客户端侧 :class:`~localtonet.gui.viewmodel.GuiViewModel` 同一套分工：
邮筒消息该怎样变成界面状态，全部逻辑都在这里，**一行 tkinter 都没有**。

消息三种（kind 见 :class:`~localtonet.gui.bridge.UiBridge`）：

``event``           服务端事件总线里的一个事件（上线 / 下线 / 转发失败……），只用来写日志
``snapshot``        :meth:`TunnelServer.snapshot` 的定时快照，负责表格与所有计数器
``mapping_result``  管理台提交映射的本地结果（**不是** socket 回执，见 server_controller）
``kick_result``     管理台踢人的本地结果（同上，也没有回执报文）
``local``           界面自己产生的提示（比如"服务端启动失败"）

这里有一处与客户端侧**必须不同**的地方，值得单独说：

客户端靠 ``MAPPING_CHANGED`` 事件刷新映射表；**服务端根本不发这个事件**，
映射表是要靠周期快照（``snapshot()["mapping"]``）带过来的。
于是"远端映射变了"这件事变成了"两次采样之间指纹不同"——既然采样每 0.5 秒一次，
就不能每次采样都喊一句"服务端映射表已更新"，那会把提示栏刷成噪声。
所以这里记住上一份指纹，只在**真的变了**并且用户正在编辑时才提示。
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from localtonet.gui.bridge import UiMessage
from localtonet.gui.model import MappingTableModel
from localtonet.gui.server_model import (
    ServerState,
    adopt_server_snapshot,
    append_server_log,
    apply_server_event,
    mapping_signature,
    note_kick_result,
    note_server_mapping_result,
)
from logging_setup import get_logger

__all__ = ["REMOTE_STALE_NOTICE", "ServerViewModel"]

REMOTE_STALE_NOTICE = "服务端映射表已更新，但你有未提交的改动——界面保留了你的输入，未覆盖"


class ServerViewModel:
    """把邮筒消息折叠成"在线客户端表 + 映射表 + 状态栏 + 日志"四个可渲染对象。"""

    def __init__(
        self,
        *,
        table: Optional[MappingTableModel] = None,
        state: Optional[ServerState] = None,
        default_local_port: int = 8000,
        default_local_host: str = "127.0.0.1",
        idle_timeout: float = 0.0,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._log = logger or get_logger("gui.server_viewmodel")
        self._table = table or MappingTableModel(
            default_local_port=default_local_port,
            default_local_host=default_local_host,
        )
        self._state = state or ServerState()
        self._state = replace(self._state, idle_timeout=idle_timeout)
        # 上一份远端映射的指纹。初值 None 表示"还没见过任何一份"，
        # 于是第一次采样一定算"变了"——但如果此刻用户没有未提交改动，
        # load_remote 会直接刷新、不会走提示分支，所以不会误报。
        self._remote_signature: Optional[Tuple[Any, ...]] = None

    # ------------------------------------------------------------------ #
    # 只读
    # ------------------------------------------------------------------ #

    @property
    def table(self) -> MappingTableModel:
        return self._table

    @property
    def state(self) -> ServerState:
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
                return self._apply_snapshot(dict(snapshot))
            return False
        if kind == "mapping_result":
            result = payload.get("result")
            if isinstance(result, Mapping):
                self._apply_mapping_result(dict(result))
                return True
            return False
        if kind == "kick_result":
            result = payload.get("result")
            if isinstance(result, Mapping):
                self._apply_kick_result(dict(result))
                return True
            return False
        if kind == "local":
            level = str(payload.get("level") or "info")
            text = str(payload.get("text") or "")
            if not text:
                return False
            self._state = append_server_log(
                self._state, level, text, notice=text if level in ("warn", "error") else None
            )
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

        self._state = apply_server_event(self._state, name, **event_payload)
        return True

    def _apply_snapshot(self, snapshot: Dict[str, Any]) -> bool:
        self._state = adopt_server_snapshot(self._state, snapshot)

        mapping = snapshot.get("mapping")
        if not isinstance(mapping, (list, tuple)):
            return True

        signature = mapping_signature(mapping)
        changed = signature != self._remote_signature
        self._remote_signature = signature

        adopted = self._table.load_remote(list(mapping))
        if not adopted and changed:
            # 只有"远端真的变了"才提示：采样是周期性的，每次采样都提示等于没提示
            self._state = replace(self._state, notice=REMOTE_STALE_NOTICE)
        return True

    def _apply_mapping_result(self, result: Dict[str, Any]) -> None:
        ok = bool(result.get("ok"))
        msg = str(result.get("msg") or "")
        self._state = note_server_mapping_result(self._state, ok, msg)
        if ok:
            # 界面上这份工作副本已经是服务端认下的版本，可以当作新的"已保存"基准。
            # 同步刷新指纹：否则下一份快照会被判成"远端变了"，
            # 而它其实只是把刚提交的内容原样送回来。
            self._table.mark_submitted()
            self._remote_signature = mapping_signature(
                [row.to_dict() for row in self._table.snapshot]
            )

    def _apply_kick_result(self, result: Dict[str, Any]) -> None:
        """踢人的结果只写日志与提示。

        刻意**不碰映射表工作副本**——踢人与映射表无关。（对照
        :meth:`_apply_mapping_result`：那个必须 ``mark_submitted`` + 刷指纹，
        因为提交成功后界面上那份工作副本就是服务端认下的版本；踢人不改变任何映射，
        去刷指纹只会把用户正在编辑的内容误判成"已同步"。）
        """
        self._state = note_kick_result(
            self._state, bool(result.get("ok")), str(result.get("msg") or "")
        )

    def __repr__(self) -> str:
        return f"ServerViewModel(clients={self._state.client_count}, dirty={self._table.is_dirty})"
