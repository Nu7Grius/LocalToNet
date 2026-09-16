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
    "note_kick_result",
]

ANONYMOUS_IDENTITY = "anonymous"
"""不配鉴权时服务端给的身份标签（与 ``server.auth.ANONYMOUS`` 同名，界面只做展示）。"""

RATE_LIMIT_SCOPE_LABELS: Dict[str, str] = {
    "client": "按客户端",
    "port": "按端口",
    "visitor": "按访客IP",
}
"""限速汇总口径的中文标签。只在**非默认口径**时才写进状态栏——
默认值天天显示是噪声，而"从默认改开去"的那一刻恰恰是运维唯一需要被提醒的时刻。"""


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

    @property
    def rate_limit_scope(self) -> str:
        """限速汇总口径。老服务端/老快照没有这一块时按默认口径 ``client`` 处理。"""
        block = self.snapshot.get("rate_limit")
        if not isinstance(block, Mapping):
            return "client"
        scope = block.get("scope")
        return scope if isinstance(scope, str) and scope else "client"

    @property
    def rate_limit_evicted(self) -> int:
        """被桶表上限淘汰掉的 key 数。非 0 说明 ``limits.rate_limit_max_keys`` 该调大。"""
        block = self.snapshot.get("rate_limit")
        if not isinstance(block, Mapping):
            return 0
        value = block.get("evicted")
        return value if isinstance(value, int) else 0

    @property
    def kick_cooldowns(self) -> Dict[str, float]:
        """``client_id -> 剩余冷却秒数``：被管理台踢出、还没到重连时间的机器。

        老服务端 / 老快照没有 ``kick_cooldowns`` 这一块，按"没有人在冷却期"处理
        （同 ``rate_limit`` 的缺省口径处理）。这一屏信息必须看得见：冷却期里的机器
        **不在**在线表里，没有它，界面上只表现为"那台机器一直没上来"，看起来像配置坏了。
        """
        block = self.snapshot.get("kick_cooldowns")
        if not isinstance(block, Mapping):
            return {}
        return {
            str(key): value
            for key, value in block.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        }

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
        scope = self.rate_limit_scope
        if scope != "client":
            # 换成非默认口径后，per_client_*_bps 的含义就从"客户端总额度"变成了
            # "每个汇总单位各自的额度"——总带宽上限被放大了，必须写在脸上
            parts.append(f"限速口径 {RATE_LIMIT_SCOPE_LABELS.get(scope, scope)}")
        if self.throttled_seconds:
            parts.append(f"限速等待 {self.throttled_seconds:.1f}秒")
        evicted = self.rate_limit_evicted
        if evicted:
            parts.append(f"限速桶表淘汰 {evicted}")
        cooling = self.kick_cooldowns
        if cooling:
            # 桶里的机器不在在线表里，只在冷却期结束后自己回来。剩余时间给最长的那个：
            # 运维要知道的是"还要多久这一切恢复正常"，而不是每一台的精确读数
            parts.append(f"踢出冷却中 {len(cooling)} 台（剩 {max(cooling.values()):.0f}秒）")
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


def _on_registration_rejected(payload: Mapping[str, Any]) -> _Outcome:
    """有客户端注册被拒。

    状态栏那个"被拒 N"只回答"拒了几次"；运维真正要看的是**这一次是谁、为什么**——
    客户端侧的表现往往只是"一直连不上、日志里反复重连"，原因全在这里。
    所以日志行必须能独立读懂：谁（有身份就带上身份）、从哪来、什么码、什么原因、
    以及**客户端接下来会继续重试还是会停手**（``retryable``）。

    ``identity`` 可能为空串：鉴权失败时身份从未确立（服务端不会把它写成 anonymous，
    否则"令牌失效/冒充"会被误读成"匿名用户"）。缺身份就只显示 client_id。
    """
    code = payload.get("code")
    retryable = payload.get("retryable") is True
    reason = payload.get("msg") or "未知原因"
    client_id = payload.get("client_id") or "-"
    peer = payload.get("peer") or "-"
    identity = payload.get("identity") or ""
    who = f"{identity}（{client_id}）" if identity else str(client_id)
    code_text = str(code) if code is not None else "?"
    # 可重试＝客户端自己会退避重试，运维通常只需修配置；不可重试＝这一端要动手
    # （换令牌 / 放行 client_id），严重度更高。
    # 缺 ``retryable`` 时按"不可重试"处理：宁可多标一次黄，也不要让"停止重试"被静默成 info。
    level = "info" if retryable else "warn"
    fate = "客户端将继续重试" if retryable else "客户端已停止重试"
    return _Outcome(
        logs=((level, f"注册被拒：{who} 来自 {peer} → [{code_text}] {reason}（{fate}）"),),
        notice=f"注册被拒（{code_text}）：{who}",
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
    EventType.REGISTRATION_REJECTED: _on_registration_rejected,
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


def note_kick_result(state: ServerState, ok: bool, msg: str) -> ServerState:
    """把管理台"踢出客户端"的结果写进日志与提示。

    踢人是本窗口唯一的**直接断人**动作（映射表改动只影响之后的新请求，不会掐断已有隧道），
    所以无论成败都要留痕：成功＝一次干预的审计线索（并提示冷却期会让它等一会儿才能回来），
    失败＝"我点了按钮却什么也没发生"的解释（多半是采样窗口里的竞态：界面显示在线，
    点下去时那台机器已经自己掉线了）。
    """
    text = msg or ("已踢出" if ok else "踢出失败")
    updated = _append(
        state,
        [LogEntry(ts=_now(), level="ok" if ok else "warn", text=f"管理台踢出：{text}")],
    )
    return replace(updated, notice=f"管理台踢出：{text}")


def _now() -> str:
    return time.strftime("%H:%M:%S")
