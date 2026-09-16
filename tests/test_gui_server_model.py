# -*- coding: utf-8 -*-
"""
tests/test_gui_server_model.py —— 服务端管理台纯逻辑层的单元测试
=================================================================
这里**一个 tkinter 都不需要**，也不起任何网络：管理台真正会出错的地方是
"快照怎么变成表格行""事件怎么变成日志""远端变了要不要覆盖用户输入"，
这些全是纯计算，能在无显示环境里被穷举。

守四条线：

1. **脏数据不崩**：服务端下发的快照本应合法，但界面不该因为一条畸形数据整片白屏；
2. **阈值语义**：空闲阈值拿不到（0）时不许把所有人标成"将失联"——
   宁可少标一个黄标，也不要制造假警报；
3. **日志环形截断后仍持续刷新**：靠累计计数而不是 ``len(log)``；
4. **映射指纹与编辑缓冲区同一套字段**：否则会出现"表格认为变了、刷新逻辑认为没变"。
"""

from __future__ import annotations

import sys

from localtonet.core.events import EventType
from localtonet.gui.model import MAX_LOG_ENTRIES, MappingRow
from localtonet.gui.server_model import (
    ANONYMOUS_IDENTITY,
    ClientRow,
    ServerState,
    adopt_server_snapshot,
    append_server_log,
    apply_server_event,
    mapping_signature,
    note_kick_result,
    note_server_mapping_result,
)

# --------------------------------------------------------------------------- #
# ClientRow
# --------------------------------------------------------------------------- #


def test_client_row_from_dict_reads_every_field() -> None:
    row = ClientRow.from_dict(
        {
            "client_id": "c1",
            "identity": "alice",
            "peer": "10.0.0.9:5000",
            "local_ports": [8081, 8080],
            "online_seconds": 12.5,
            "idle_seconds": 0.5,
        }
    )
    assert row.client_id == "c1"
    assert row.identity == "alice"
    assert row.peer == "10.0.0.9:5000"
    assert row.ports == (8081, 8080)
    assert row.online_seconds == 12.5
    assert row.idle_seconds == 0.5


def test_client_row_survives_malformed_snapshot() -> None:
    """畸形数据只该丢字段，不该抛异常把整张表搞崩。"""
    row = ClientRow.from_dict({"client_id": 7, "local_ports": ["8080", None, 8081], "online_seconds": "x"})
    assert row.client_id == "7"
    assert row.ports == (8081,)  # 非整数一律丢掉
    assert row.online_seconds == 0.0
    assert row.identity == ""


def test_client_row_cells_match_column_count() -> None:
    row = ClientRow.from_dict({"client_id": "c1", "identity": "alice", "local_ports": [8080]})
    assert len(row.as_cells()) == len(ClientRow.COLUMNS)


def test_client_row_anonymous_identity_is_shown() -> None:
    """不配鉴权时身份是 `anonymous`，但快照里可能是空串——表格要显示得出来。"""
    row = ClientRow.from_dict({"client_id": "c1"})
    assert row.as_cells()[0] == ANONYMOUS_IDENTITY
    assert ANONYMOUS_IDENTITY in row.describe()


def test_client_row_is_never_marked_idle_when_threshold_unknown() -> None:
    """阈值 0 表示"界面没拿到配置"，不是"立刻失联"。"""
    row = ClientRow.from_dict({"idle_seconds": 9999.0})
    assert row.is_idle(0.0) is False
    assert "将失联" not in row.as_cells(idle_timeout=0.0)[-1]


def test_client_row_marks_idle_at_threshold() -> None:
    row = ClientRow.from_dict({"idle_seconds": 30.0})
    assert row.is_idle(30.0) is True
    assert "将失联" in row.as_cells(idle_timeout=30.0)[-1]
    assert row.is_idle(31.0) is False


def test_client_row_formats_durations_in_three_ranges() -> None:
    short = ClientRow.from_dict({"online_seconds": 42.0, "idle_seconds": 5.0})
    medium = ClientRow.from_dict({"online_seconds": 125.0})
    long = ClientRow.from_dict({"online_seconds": 7200.0})
    assert short.as_cells()[4] == "42秒"
    assert medium.as_cells()[4] == "2.1分"
    assert long.as_cells()[4] == "2.0小时"


# --------------------------------------------------------------------------- #
# ServerState
# --------------------------------------------------------------------------- #


def _snapshot(**overrides: object) -> dict:
    base = {
        "name": "demo",
        "auth": "token-file(2 条)",
        "clients": [
            {
                "client_id": "c1",
                "identity": "alice",
                "peer": "1.2.3.4:5",
                "local_ports": [8000],
                "online_seconds": 10.0,
                "idle_seconds": 1.0,
            }
        ],
        "port_owner": {"8000": "c1"},
        "mapping": [{"public_port": 9028, "local_host": "127.0.0.1", "local_port": 8000}],
        "listening": [9028],
        "pending": 2,
        "stats": {
            "clients_registered": 3,
            "registrations_rejected": 1,
            "clients_online": 1,
            "requests_total": 9,
            "requests_failed": 2,
            "requests_rejected": 1,
            "bytes_upload": 2048,
            "bytes_download": 1048576,
            "throttled_seconds": 0.0,
        },
    }
    base.update(overrides)
    return base


def test_server_state_reads_snapshot() -> None:
    state = adopt_server_snapshot(ServerState(), _snapshot())
    assert state.name == "demo"
    assert state.auth_mode == "token-file(2 条)"
    assert state.client_count == 1
    assert state.clients[0].identity == "alice"
    assert state.listening == [9028]
    assert state.pending == 2
    assert state.stat("requests_total") == 9
    assert state.throttled_seconds == 0.0


def test_server_state_tolerates_empty_and_missing_snapshot() -> None:
    state = ServerState()
    assert state.clients == []
    assert state.listening == []
    assert state.pending == 0
    assert state.stat("requests_total") == 0
    assert state.name == "-"
    assert state.auth_mode == "-"


def test_server_state_ignores_non_dict_clients() -> None:
    state = adopt_server_snapshot(ServerState(), _snapshot(clients=["oops", None, {"client_id": "c2"}]))
    assert state.client_count == 1
    assert state.clients[0].client_id == "c2"


def test_status_line_carries_the_fields_ops_asks_for() -> None:
    state = adopt_server_snapshot(ServerState(), _snapshot())
    line = state.status_line()
    for token in ("demo", "token-file(2 条)", "在线 1", "9028", "被拒 1", "请求 9", "上行 2.0KB", "下行 1.0MB"):
        assert token in line, token


def test_status_line_hides_throttle_when_never_throttled() -> None:
    """没限速过就不要在状态栏占一格——空字段比缺字段更让人分心。"""
    state = adopt_server_snapshot(ServerState(), _snapshot())
    assert "限速等待" not in state.status_line()


def test_status_line_shows_throttle_when_used() -> None:
    stats = dict(_snapshot()["stats"])
    stats["throttled_seconds"] = 1.25
    state = adopt_server_snapshot(ServerState(), _snapshot(stats=stats))
    assert "限速等待 1.2秒" in state.status_line()


def test_status_line_hides_the_default_rate_limit_scope() -> None:
    """默认口径天天写在状态栏上是噪声——只在"从默认改开去"时才提醒。"""
    state = adopt_server_snapshot(ServerState(), _snapshot(rate_limit={"scope": "client", "evicted": 0}))
    assert state.rate_limit_scope == "client"
    assert "限速口径" not in state.status_line()


def test_status_line_shows_the_non_default_rate_limit_scope() -> None:
    """换成按端口/按访客后，per_client_*_bps 的含义被改了，必须写在脸上。"""
    state = adopt_server_snapshot(ServerState(), _snapshot(rate_limit={"scope": "visitor", "evicted": 0}))
    assert "限速口径 按访客IP" in state.status_line()

    by_port = adopt_server_snapshot(ServerState(), _snapshot(rate_limit={"scope": "port", "evicted": 0}))
    assert "限速口径 按端口" in by_port.status_line()


def test_status_line_reports_evicted_buckets() -> None:
    """桶表开始淘汰说明上限该调大了——这是"按访客分桶"最可能踩到的坑。"""
    state = adopt_server_snapshot(ServerState(), _snapshot(rate_limit={"scope": "visitor", "evicted": 7}))
    assert state.rate_limit_evicted == 7
    assert "限速桶表淘汰 7" in state.status_line()


def test_rate_limit_block_tolerates_missing_and_malformed_snapshot() -> None:
    """老服务端没有这一块、或字段类型不对时，一律退回默认口径，不许抛异常。"""
    for snapshot in (
        _snapshot(),  # 压根没有 rate_limit 键（老服务端）
        _snapshot(rate_limit="nonsense"),
        _snapshot(rate_limit={"scope": 1, "evicted": "x"}),
    ):
        state = adopt_server_snapshot(ServerState(), snapshot)
        assert state.rate_limit_scope == "client"
        assert state.rate_limit_evicted == 0
        assert "限速口径" not in state.status_line()


# --------------------------------------------------------------------------- #
# 踢人冷却（快照里的 kick_cooldowns）
# --------------------------------------------------------------------------- #


def test_kick_cooldowns_tolerate_missing_and_malformed_snapshot() -> None:
    """老服务端没有这一块时按"没人在冷却期"处理，不许抛异常。"""
    for snapshot in (
        _snapshot(),  # 压根没有 kick_cooldowns 键（老服务端）
        _snapshot(kick_cooldowns="nonsense"),
        _snapshot(kick_cooldowns={"c1": "soon", "c2": True}),
    ):
        state = adopt_server_snapshot(ServerState(), snapshot)
        assert state.kick_cooldowns == {}
        assert "踢出冷却中" not in state.status_line()


def test_status_line_reports_who_is_cooling_down() -> None:
    """冷却期里的机器**不在**在线表里，状态栏是界面上唯一能看见它的地方。

    没有这一格，界面只能表现为"那台机器一直没上来"，看起来像配置坏了。
    """
    state = adopt_server_snapshot(
        ServerState(), _snapshot(kick_cooldowns={"c1": 12.6, "c2": 3.0})
    )
    assert state.kick_cooldowns == {"c1": 12.6, "c2": 3.0}
    assert "踢出冷却中 2 台（剩 13秒）" in state.status_line()


# --------------------------------------------------------------------------- #
# 事件 → 状态
# --------------------------------------------------------------------------- #


def test_unknown_event_is_ignored() -> None:
    state = ServerState()
    assert apply_server_event(state, "no_such_event", x=1) is state


def test_server_started_and_stopped_are_logged() -> None:
    state = apply_server_event(
        ServerState(),
        EventType.SERVER_STARTED,
        name="demo",
        control_port=7000,
        data_port=7001,
        mapping=[{"public_port": 9028, "local_port": 8000}],
    )
    assert "已启动" in state.log[-1].text
    assert state.log[-1].level == "ok"
    assert "已启动" in state.notice

    state = apply_server_event(state, EventType.SERVER_STOPPED, stats={})
    assert state.log[-1].level == "warn"
    assert "已停止" in state.notice


def test_client_connected_carries_identity_into_the_log() -> None:
    """身份标签是运维排查的第一信息，缺了它这条日志几乎没用。"""
    state = apply_server_event(
        ServerState(),
        EventType.CLIENT_CONNECTED,
        client_id="c1",
        peer="1.2.3.4:5",
        identity="alice",
        claimed=[8000],
        conflicts=[],
    )
    text = state.log[-1].text
    assert "alice" in text and "c1" in text and "8000" in text
    assert state.notice == "alice 已上线"


def test_client_connected_reports_conflicts_separately() -> None:
    state = apply_server_event(
        ServerState(),
        EventType.CLIENT_CONNECTED,
        client_id="c1",
        identity="alice",
        claimed=[8000],
        conflicts=[8080],
    )
    assert len(state.log) == 2
    assert state.log[-1].level == "warn"
    assert "8080" in state.log[-1].text


def test_client_disconnected_logs_reason_and_released_ports() -> None:
    state = apply_server_event(
        ServerState(),
        EventType.CLIENT_DISCONNECTED,
        client_id="c1",
        reason="心跳超时",
        ports=[8000, 8001],
    )
    assert "心跳超时" in state.log[-1].text
    assert "释放 8000,8001" in state.log[-1].text


def test_conn_error_logs_failure_reason() -> None:
    state = apply_server_event(
        ServerState(),
        EventType.CONN_ERROR,
        client_id="c1",
        local_port=8000,
        reason="Connection refused",
    )
    assert state.log[-1].level == "warn"
    assert "Connection refused" in state.log[-1].text


def test_mapping_rejected_event_lands_in_panel() -> None:
    """映射表写权限被拒是**安全事件**，必须在面板上留下"谁被拒了"。

    运维看到它的第一反应应该是"去令牌表给该条目加 can_manage_mapping"，
    所以身份与 client_id 都要出现在日志行里，光有一句"被拒"等于没说。
    """
    state = apply_server_event(
        ServerState(),
        EventType.MAPPING_REJECTED,
        client_id="alice-1",
        identity="alice",
        reason="无映射表写权限",
    )
    assert len(state.log) == 1
    assert state.log[-1].level == "warn"
    assert "alice" in state.log[-1].text and "alice-1" in state.log[-1].text
    assert "无映射表写权限" in state.log[-1].text
    assert "alice" in (state.notice or "")


def test_mapping_rejected_event_survives_missing_fields() -> None:
    """缺字段（老版本服务端或日志被裁剪）只该退化成默认值，不该把界面搞崩。"""
    state = apply_server_event(ServerState(), EventType.MAPPING_REJECTED)
    assert state.log[-1].level == "warn"
    assert ANONYMOUS_IDENTITY in state.log[-1].text


# --------------------------------------------------------------------------- #
# REGISTRATION_REJECTED：把状态栏那个"被拒 N"变成可读的一行
# --------------------------------------------------------------------------- #
#
# 状态栏只回答"拒了几次"，回答不了"谁被拒、为什么"。日志行必须能**独立读懂**：
# 谁（有身份就带身份）、从哪来、什么码、什么原因、以及客户端接下来会不会继续重试。


def test_registration_rejected_renders_identity_client_and_reason() -> None:
    state = apply_server_event(
        ServerState(),
        EventType.REGISTRATION_REJECTED,
        code=403,
        retryable=True,
        msg="端口未授权：[8000]（身份 alice 允许的内网端口：[9000]）",
        client_id="alice-1",
        peer="10.0.0.9:5001",
        identity="alice",
    )
    assert len(state.log) == 1
    text = state.log[-1].text
    assert "alice" in text and "alice-1" in text and "10.0.0.9:5001" in text
    assert "403" in text and "端口未授权" in text
    # 可重试＝客户端自己会退避重试：不该用 warn 吓人（严重度区分是有意义的）
    assert state.log[-1].level == "info"
    assert "继续重试" in text
    assert "alice" in (state.notice or "")


def test_registration_rejected_permanent_failure_is_a_warning() -> None:
    """永久失败（令牌不对／冒充）要人工介入，必须是 warn 级，且写明客户端已停手。"""
    state = apply_server_event(
        ServerState(),
        EventType.REGISTRATION_REJECTED,
        code=403,
        retryable=False,
        msg="客户端 127.0.0.1:5 提供的 token 不合法",
        client_id="c-1",
        peer="127.0.0.1:5",
        identity="",
    )
    assert state.log[-1].level == "warn"
    assert "已停止重试" in state.log[-1].text


def test_registration_rejected_without_identity_does_not_say_anonymous() -> None:
    """鉴权失败时身份**从未确立**，界面不许把它渲染成"匿名用户"。

    这一条盯的是最容易犯的错：照抄 ``_on_mapping_rejected`` 的
    ``payload.get("identity") or ANONYMOUS_IDENTITY`` —— 那会把
    "令牌失效 / 冒充" 显示成 "anonymous 没有权限"，指错排查方向。
    """
    state = apply_server_event(
        ServerState(),
        EventType.REGISTRATION_REJECTED,
        code=403,
        retryable=False,
        msg="令牌不在令牌表中",
        client_id="c-2",
        peer="127.0.0.1:6",
        identity="",
    )
    text = state.log[-1].text
    assert "c-2" in text
    assert ANONYMOUS_IDENTITY not in text
    assert ANONYMOUS_IDENTITY not in (state.notice or "")


def test_registration_rejected_event_survives_missing_fields() -> None:
    """老版本服务端 / 载荷被裁剪时只该退化，不该把界面搞崩。

    缺 ``retryable`` 按"不可重试"渲染（warn）：宁可多标一次黄，
    也不要让"客户端已停止重试"这种要人动手的状态被静默成 info。
    """
    state = apply_server_event(ServerState(), EventType.REGISTRATION_REJECTED)
    assert len(state.log) == 1
    assert state.log[-1].level == "warn"
    text = state.log[-1].text
    assert "注册被拒" in text and "[?]" in text and "未知原因" in text
    assert ANONYMOUS_IDENTITY not in text


def test_request_end_produces_no_log_on_purpose() -> None:
    """服务端的 REQUEST_END **没有**成功标志字段，写不出有信息量的日志。

    这里刻意断言"什么都不记"：否则以后有人顺手加一条"请求完成"，
    面板会在每个请求上刷一行，把真正要看的失败信息冲走。
    """
    state = apply_server_event(ServerState(), EventType.REQUEST_END, conn_id="x", public_port=9028, duration=0.01)
    assert state.log == ()
    assert state.log_total == 0


def test_log_ring_truncation_still_advances_the_total() -> None:
    """环形截断后 ``log_total`` 必须继续增长，否则面板超过上限就再也不刷新。"""
    state = ServerState()
    produced = MAX_LOG_ENTRIES + 25
    for index in range(produced):
        state = append_server_log(state, "info", f"第 {index} 条")
    assert len(state.log) == MAX_LOG_ENTRIES
    assert state.log_total == produced
    # 留下的应该是**最后** MAX_LOG_ENTRIES 条，最新那条仍在
    assert state.log[-1].text == f"第 {produced - 1} 条"


def test_append_server_log_sets_notice_only_when_asked() -> None:
    quiet = append_server_log(ServerState(), "info", "普通信息")
    assert quiet.notice == ""
    loud = append_server_log(ServerState(), "error", "出错了", notice="出错了")
    assert loud.notice == "出错了"


def test_mapping_result_success_and_failure() -> None:
    ok = note_server_mapping_result(ServerState(), True, "新增 1 个端口")
    assert ok.log[-1].level == "ok"
    assert ok.last_diff == "新增 1 个端口"
    assert "已生效" in ok.notice

    bad = note_server_mapping_result(ServerState(), False, "端口 1 非法")
    assert bad.log[-1].level == "error"
    assert "提交失败" in bad.notice
    assert "端口 1 非法" in bad.notice


def test_mapping_result_falls_back_when_message_missing() -> None:
    assert "已提交" in note_server_mapping_result(ServerState(), True, "").notice
    assert "提交失败" in note_server_mapping_result(ServerState(), False, "").notice


def test_kick_result_success_and_failure_both_leave_a_trace() -> None:
    """踢人是这个窗口唯一的"直接断人"动作，成败都要留痕。

    失败那一路（大多是采样窗口里的竞态：界面显示在线，点下去时它已经掉线）
    必须是 **warn** 而不是 error——它不是错误，只是"什么也没发生"，
    但"我点了按钮却毫无反应"必须能在面板上读到。
    """
    ok = note_kick_result(ServerState(), True, "已踢出 admin-c1")
    assert ok.log[-1].level == "ok"
    assert "管理台踢出" in ok.log[-1].text
    assert "已踢出 admin-c1" in ok.notice

    raced = note_kick_result(ServerState(), False, "客户端 admin-c1 已经不在线，无需踢出")
    assert raced.log[-1].level == "warn"
    assert "不在线" in raced.log[-1].text
    assert "不在线" in raced.notice


def test_kick_result_falls_back_when_message_missing() -> None:
    assert "已踢出" in note_kick_result(ServerState(), True, "").notice
    assert "踢出失败" in note_kick_result(ServerState(), False, "").notice


# --------------------------------------------------------------------------- #
# 映射指纹
# --------------------------------------------------------------------------- #


def test_mapping_signature_is_order_insensitive() -> None:
    first = [
        {"public_port": 9028, "local_host": "127.0.0.1", "local_port": 8000},
        {"public_port": 9029, "local_host": "127.0.0.1", "local_port": 8001},
    ]
    second = list(reversed(first))
    assert mapping_signature(first) == mapping_signature(second)


def test_mapping_signature_tracks_tls_and_ports() -> None:
    base = [{"public_port": 9028, "local_host": "127.0.0.1", "local_port": 8000}]
    plain = mapping_signature(base)
    assert plain != mapping_signature([dict(base[0], tls=False)])
    assert plain != mapping_signature([dict(base[0], local_port=8001)])
    assert plain != mapping_signature([])


def test_mapping_signature_uses_the_same_fields_as_the_row() -> None:
    """指纹必须与编辑缓冲区判断"脏不脏"用的是同一套字段。

    否则会出现"表格认为变了、刷新逻辑认为没变"这种只在特定字段上复现的分裂。
    """
    row = MappingRow(public_port=9028, local_port=8000, host="0.0.0.0", local_host="127.0.0.1", remark="x", tls=True)
    assert mapping_signature([row.to_dict()]) == (row.signature(),)


def test_mapping_signature_skips_malformed_entries() -> None:
    """服务端下发的数据本应合法；真遇到脏数据也不该让刷新逻辑崩掉。"""
    assert mapping_signature([{"public_port": "不是端口"}, None, "x"]) == ()


def test_headless_server_modules_never_import_tkinter() -> None:
    """管理台的无头层不许把 tkinter 拉进来——否则无显示环境连测试都跑不了。"""
    assert "localtonet.gui.server_app" not in sys.modules
    assert "localtonet.gui.widgets" not in sys.modules
    assert "tkinter" not in sys.modules
