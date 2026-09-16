# -*- coding: utf-8 -*-
"""
localtonet.gui.server_model —— 服务端管理台的纯逻辑层
=====================================================
与客户端侧 :mod:`localtonet.gui.model` 同一套分工：只做"数据 → 表格行 / 状态栏文本"
的纯计算，既不 import tkinter 也不碰网络，因此可以无头测试。

数据源刻意分成两路，缺一不可：

* :meth:`~localtonet.server.core.TunnelServer.snapshot` —— **此刻是什么样**（在线客户端、
  映射表、监听端口、统计）。界面周期性取它，是唯一的权威状态。
* :class:`~localtonet.core.events.EventBus` —— **刚才发生了什么**（谁上线、谁掉了、
  转发为什么失败）。只用来写日志面板。

快照说不出"三分钟前掉了个人"，事件也说不出"现在还剩几个人在线"。
把两者混成一个数据源，要么日志面板刷不出历史，要么表格显示的是一份过期名单。

审核注意：这里**不复制**端口/字节的格式化逻辑，直接复用
:func:`localtonet.gui.model.fmt_ports` / :func:`~localtonet.gui.model.fmt_bytes`——
服务端表格与客户端状态栏的数字长得不一样会让人怀疑哪边在说谎。
"""

from __future__ import annotations

import time
from collections.abc import MutableMapping
from dataclasses import dataclass, field, replace
from typing import Any, ClassVar, Dict, List, Mapping, Optional, Sequence, Tuple

from config import ConfigError
from localtonet.core.events import EventType
from localtonet.gui.model import MAX_LOG_ENTRIES, LogEntry, MappingRow, fmt_bytes, fmt_ports

__all__ = [
    "ANONYMOUS_IDENTITY",
    "ClientRow",
    "ServerState",
    "adopt_server_snapshot",
    "append_server_log",
    "apply_server_event",
    "mapping_signature",
    "note_server_mapping_result",
]

ANONYMOUS_IDENTITY = "anonymous"
"""不配鉴权时服务端给的身份标签（与 ``server.auth.ANONYMOUS`` 同名，界面只做展示）。"""


# --------------------------------------------------------------------------- #
# 在线客户端：表格行
# --------------------------------------------------------------------------- #


def _as_float(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) else 0.0


def _fmt_duration(seconds: float) -> str:
    """把秒数写成人类看得懂的短形式（表格列宽有限，不写 1234.5s 这种）。"""
    if seconds < 60:
        return f"{seconds:.0f}秒"
    if seconds < 3600:
        return f"{seconds / 60:.1f}分"
    return f"{seconds / 3600:.1f}小时"


@dataclass(frozen=True)
class ClientRow:
    """在线客户端表格的一行，字段与 :meth:`ClientSession.snapshot` 一一对应。

    刻意**只读**：管理台不提供"踢人"按钮（本轮范围），所以这一层没有任何写操作，
    也就不存在"表格里的值和服务端不一致"的可能——每次刷新都整份重建。
    """

    client_id: str = ""
    identity: str = ""
    peer: str = ""
    ports: Tuple[int, ...] = ()
    online_seconds: float = 0.0
    idle_seconds: float = 0.0

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ClientRow":
        ports = tuple(port for port in data.get("local_ports") or [] if isinstance(port, int))
        return cls(
            client_id=str(data.get("client_id") or ""),
            identity=str(data.get("identity") or ""),
            peer=str(data.get("peer") or ""),
            ports=ports,
            online_seconds=_as_float(data.get("online_seconds")),
            idle_seconds=_as_float(data.get("idle_seconds")),
        )

    def is_idle(self, threshold: float) -> bool:
        """空闲是否已达看门狗阈值（阈值来自 ``timeouts.client_idle_timeout``）。

        ``threshold <= 0`` 表示界面没拿到配置，一律不算空闲——
        宁可少标一个黄标，也不要因为一个没填的默认值把所有人标成"要掉线了"。
        """
        return threshold > 0 and self.idle_seconds >= threshold

    def as_cells(self, *, idle_timeout: float = 0.0) -> Tuple[str, ...]:
        """表格一行各列文本。列序与 :data:`ClientRow.COLUMNS` 一致。"""
        idle = _fmt_duration(self.idle_seconds)
        if self.is_idle(idle_timeout):
            idle += "（将失联）"
        return (
            self.identity or ANONYMOUS_IDENTITY,
            self.client_id or "-",
            self.peer or "-",
            fmt_ports(self.ports),
            _fmt_duration(self.online_seconds),
            idle,
        )

    COLUMNS: ClassVar[Tuple[str, ...]] = ("身份", "客户端", "对端地址", "认领端口", "在线", "空闲")
    """表格列标题。放 :class:`typing.ClassVar` 里，否则会被 dataclass 当成一个字段。"""

    def describe(self) -> str:
        return (
            f"{self.identity or ANONYMOUS_IDENTITY}（{self.client_id or '-'}）"
            f" 来自 {self.peer or '-'}，认领 {fmt_ports(self.ports)}"
        )


# --------------------------------------------------------------------------- #
# 界面状态
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ServerState:
    """管理台状态。``snapshot`` 就是 :meth:`TunnelServer.snapshot` 的返回值。"""

    snapshot: Dict[str, Any] = field(default_factory=dict)
    log: Tuple[LogEntry, ...] = ()
    log_total: int = 0
    """**累计**产生过多少条日志（不随环形截断回退），理由同客户端侧 GuiState。"""
    notice: str = ""
    last_diff: str = ""
    idle_timeout: float = 0.0
    """看门狗空闲阈值，由界面层从配置注入；``0`` 表示未知，不标"将失联"。"""

    # ---- 便捷读取：只从 snapshot 里取，不做二次统计 ---- #

    @property
    def name(self) -> str:
        return str(self.snapshot.get("name") or "-")

    @property
    def auth_mode(self) -> str:
        return str(self.snapshot.get("auth") or "-")

    @property
    def clients(self) -> List[ClientRow]:
        raw = self.snapshot.get("clients")
        if not isinstance(raw, Sequence):
            return []
        return [ClientRow.from_dict(item) for item in raw if isinstance(item, Mapping)]

    @property
    def client_count(self) -> int:
        return len(self.clients)

    @property
    def listening(self) -> List[int]:
        return [port for port in self.snapshot.get("listening") or [] if isinstance(port, int)]

    @property
    def pending(self) -> int:
        value = self.snapshot.get("pending")
        return value if isinstance(value, int) else 0

    def stat(self, name: str) -> int:
        stats = self.snapshot.get("stats")
        if not isinstance(stats, Mapping):
            return 0
        value = stats.get(name)
        return value if isinstance(value, int) else 0

    @property
    def throttled_seconds(self) -> float:
        stats = self.snapshot.get("stats")
        if not isinstance(stats, Mapping):
            return 0.0
        return _as_float(stats.get("throttled_seconds"))

    def status_line(self) -> str:
        """状态栏文本。按"运维先想知道什么"排序，计数放后面。"""
        parts = [
            f"服务端 {self.name}",
            f"鉴权 {self.auth_mode}",
            f"在线 {self.client_count}",
            f"监听 {fmt_ports(self.listening)}",
            f"挂起通道 {self.pending}",
            f"注册 {self.stat('clients_registered')}（被拒 {self.stat('registrations_rejected')}）",
            f"请求 {self.stat('requests_total')}"
            f"（失败 {self.stat('requests_failed')} / 拒绝 {self.stat('requests_rejected')}）",
            f"上行 {fmt_bytes(self.stat('bytes_upload'))} / 下行 {fmt_bytes(self.stat('bytes_download'))}",
        ]
        if self.throttled_seconds:
            parts.append(f"限速等待 {self.throttled_seconds:.1f}秒")
        return " ｜ ".join(parts)


# --------------------------------------------------------------------------- #
# 事件 → 状态（纯函数）
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Outcome:
    logs: Tuple[Tuple[str, str], ...] = ()
    notice: Optional[str] = None


def _on_server_started(payload: Mapping[str, Any]) -> _Outcome:
    name = payload.get("name") or "-"
    mapping = [item for item in payload.get("mapping") or [] if isinstance(item, Mapping)]
    return _Outcome(
        logs=(
            (
                "ok",
                f"服务端 {name} 已启动：控制 {payload.get('control_port')} ｜ "
                f"数据 {payload.get('data_port')} ｜ 映射 {len(mapping)} 条",
            ),
        ),
        notice=f"服务端已启动（{len(mapping)} 条映射）",
    )


def _on_server_stopped(payload: Mapping[str, Any]) -> _Outcome:
    return _Outcome(logs=(("warn", "服务端已停止，访客端口全部关闭"),), notice="服务端已停止")


def _on_client_connected(payload: Mapping[str, Any]) -> _Outcome:
    identity = payload.get("identity") or ANONYMOUS_IDENTITY
    client_id = payload.get("client_id") or "-"
    peer = payload.get("peer") or "-"
    claimed = [port for port in payload.get("claimed") or [] if isinstance(port, int)]
    conflicts = [port for port in payload.get("conflicts") or [] if isinstance(port, int)]

    logs = [("ok", f"客户端上线：{identity}（{client_id}）来自 {peer}，认领 {fmt_ports(claimed)}")]
    if conflicts:
        logs.append(("warn", f"{identity} 有端口未认领（被其他客户端占用）：{fmt_ports(conflicts)}"))
    return _Outcome(logs=tuple(logs), notice=f"{identity} 已上线")


def _on_client_disconnected(payload: Mapping[str, Any]) -> _Outcome:
    client_id = payload.get("client_id") or "-"
    reason = payload.get("reason") or "未知原因"
    ports = [port for port in payload.get("ports") or [] if isinstance(port, int)]
    return _Outcome(
        logs=(("warn", f"客户端下线：{client_id}（{reason}），释放 {fmt_ports(ports)}"),),
        notice=f"{client_id} 已下线：{reason}",
    )


def _on_conn_error(payload: Mapping[str, Any]) -> _Outcome:
    reason = payload.get("reason") or "未知原因"
    client_id = payload.get("client_id") or "-"
    local_port = payload.get("local_port")
    return _Outcome(logs=(("warn", f"转发失败（客户端 {client_id}，内网端口 {local_port}）：{reason}"),))


def _on_mapping_rejected(payload: Mapping[str, Any]) -> _Outcome:
    """有身份试图改映射表但没有写权限。

    这条必须在面板上留下痕迹：它是**安全事件**，而且运维看到它的第一反应
    应该是"去令牌表里给该条目加 ``can_manage_mapping: true`` 并让客户端重连"，
    所以日志行要把身份和客户端 id 一起写清楚。
    """
    identity = payload.get("identity") or ANONYMOUS_IDENTITY
    client_id = payload.get("client_id") or "-"
    reason = payload.get("reason") or "无映射表写权限"
    return _Outcome(
        logs=(("warn", f"映射表改动被拒：{identity}（{client_id}）—— {reason}"),),
        notice=f"{identity} 没有映射表写权限",
    )


def _on_request_end(payload: Mapping[str, Any]) -> _Outcome:
    """服务端的 ``REQUEST_END`` 载荷里**没有**成功与否的字段。

    隧道跑起来后每次访问都会触发它，把"某次请求耗时 3ms"记进面板只会淹没真正要看的信息
    ——那些信息已经由状态栏的请求数、失败数、字节数覆盖。所以这里刻意什么都不做，
    而不是"看起来漏了"。
    """
    return _Outcome()


_SERVER_EVENT_HANDLERS = {
    EventType.SERVER_STARTED: _on_server_started,
    EventType.SERVER_STOPPED: _on_server_stopped,
    EventType.CLIENT_CONNECTED: _on_client_connected,
    EventType.CLIENT_DISCONNECTED: _on_client_disconnected,
    EventType.CONN_ERROR: _on_conn_error,
    EventType.MAPPING_REJECTED: _on_mapping_rejected,
    EventType.REQUEST_END: _on_request_end,
}
"""只登记**需要在面板上留下痕迹**的服务端事件。

``REQUEST_START`` / ``DATA_CHANNEL_OPENED`` 每次请求都触发，价值已被 ``snapshot()``
的计数覆盖。没有登记的事件不报错，直接忽略——脚本里新增事件不会把管理台搞崩。"""


def _append(state: ServerState, entries: Sequence[LogEntry]) -> ServerState:
    """追加日志并做环形截断，同时推进累计计数。"""
    if not entries:
        return state
    return replace(
        state,
        log=(state.log + tuple(entries))[-MAX_LOG_ENTRIES:],
        log_total=state.log_total + len(entries),
    )


def apply_server_event(state: ServerState, event: str, **payload: Any) -> ServerState:
    """把一个服务端事件折叠进管理台状态。未知事件原样返回。"""
    handler = _SERVER_EVENT_HANDLERS.get(event)
    if handler is None:
        return state

    outcome = handler(payload)
    result = state
    if outcome.logs:
        result = _append(
            result,
            [LogEntry(ts=_now(), level=level, text=text) for level, text in outcome.logs],
        )
    if outcome.notice is not None:
        result = replace(result, notice=outcome.notice)
    return result


def adopt_server_snapshot(state: ServerState, snapshot: Mapping[str, Any]) -> ServerState:
    """把 :meth:`TunnelServer.snapshot` 的结果并入管理台状态。"""
    return replace(state, snapshot=dict(snapshot))


def mapping_signature(mapping: Sequence[Any]) -> Tuple[Any, ...]:
    """一份映射表的内容指纹，用来回答"远端到底变没变"。

    为什么不直接比较两个 dict 列表：JSON 里的键序不保证稳定，脏数据还可能缺字段。
    这里复用 :meth:`MappingRow.from_dict` 归一化，再取 :meth:`MappingRow.signature`——
    与映射表编辑缓冲区判断"脏不脏"用的是**同一套字段**。若这里另立一套，
    就会出现"表格认为变了、刷新逻辑认为没变"这种只在特定字段上复现的分裂。

    解析不了的条目跳过（服务端下发的数据本应合法，真遇到脏数据也不该让刷新逻辑崩掉）。
    """
    signatures = []
    for index, item in enumerate(mapping):
        if not isinstance(item, Mapping):
            continue
        try:
            signatures.append(MappingRow.from_dict(dict(item), f"mapping[{index}]").signature())
        except ConfigError:
            continue
    signatures.sort()
    return tuple(signatures)


def append_server_log(
    state: ServerState, level: str, text: str, *, notice: Optional[str] = None
) -> ServerState:
    """记一条**界面自己产生**的日志（按钮点错了、提交被本地校验拦下……）。

    管理台与命令行服务端并存时，"我点了提交但什么也没发生"必须有迹可循。
    """
    result = _append(state, [LogEntry(ts=_now(), level=level, text=text)])
    return replace(result, notice=text) if notice is not None else result


def note_server_mapping_result(state: ServerState, ok: bool, msg: str) -> ServerState:
    """把管理台提交映射的结果写进日志与提示。

    与客户端侧的 ``note_mapping_result`` 分开，是因为服务端这里**没有回执报文**——
    结果直接来自 ``submit_mapping`` 的返回值（成功给 ``MappingDiff.describe()``，
    失败给异常消息），不是从 socket 上读来的 JSON。
    """
    text = msg or ("已提交" if ok else "提交失败")
    level = "ok" if ok else "error"
    updated = _append(
        state,
        [LogEntry(ts=_now(), level=level, text=f"映射提交{'成功' if ok else '失败'}：{text}")],
    )
    return replace(
        updated,
        notice=f"映射{'已生效' if ok else '提交失败'}：{text}",
        last_diff=text,
    )


def _now() -> str:
    return time.strftime("%H:%M:%S")
