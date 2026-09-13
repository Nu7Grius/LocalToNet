# -*- coding: utf-8 -*-
"""
tests/test_protocol.py —— 帧编解码单测
========================================
覆盖教程里强调的三类问题：**粘包**、**半包**、**越界**。

两个约定：

1. 不引入 pytest-asyncio，异步场景统一用 ``asyncio.run`` 包一层，
   让测试依赖只剩 pytest 本身。
2. ``asyncio.StreamReader`` **必须在事件循环内构造**（它构造时会去取当前事件循环），
   因此所有 helper 都是"接收协程 + asyncio.run"的形态，不在同步上下文里提前造对象。
"""

from __future__ import annotations

import asyncio
import json
import struct
from typing import Any, Dict, List

import pytest

from protocol import (
    HEADER_FMT,
    HEADER_SIZE,
    MAX_MSG_LEN,
    LengthPrefixedJSONCodec,
    ProtocolError,
    encode_frame,
    recv_msg,
)


def recv_many(*chunks: bytes, count: int = 1) -> List[Dict[str, Any]]:
    """让接收端处理这些字节，读出 count 条消息。"""

    async def scenario() -> List[Dict[str, Any]]:
        reader = asyncio.StreamReader()
        for chunk in chunks:
            reader.feed_data(chunk)
        reader.feed_eof()
        return [await recv_msg(reader) for _ in range(count)]

    return asyncio.run(scenario())


def recv_one(*chunks: bytes) -> Dict[str, Any]:
    (message,) = recv_many(*chunks, count=1)
    return message


def recv_should_fail(*chunks: bytes) -> None:
    """执行一次注定失败的接收，供 pytest.raises 包裹。"""
    recv_one(*chunks)


class _FakeWriter:
    """只实现 send 需要的 write/drain 两个方法，用于验证超长消息被拒绝。"""

    def __init__(self) -> None:
        self.buffer = bytearray()

    def write(self, data: bytes) -> None:
        self.buffer.extend(data)

    async def drain(self) -> None:
        return None


# --------------------------------------------------------------------------- #
# 编码
# --------------------------------------------------------------------------- #


def test_header_size_is_four_bytes() -> None:
    assert HEADER_SIZE == 4
    assert struct.calcsize(HEADER_FMT) == 4


def test_encode_frame_layout() -> None:
    obj = {"type": "ping"}
    frame = encode_frame(obj)

    payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    assert len(frame) == HEADER_SIZE + len(payload)
    assert struct.unpack(HEADER_FMT, frame[:HEADER_SIZE])[0] == len(payload)
    assert json.loads(frame[HEADER_SIZE:].decode("utf-8")) == obj


def test_header_is_big_endian() -> None:
    """16 字节的消息体，长度头必须是 00 00 00 10；小端会写成 10 00 00 00。"""
    frame = encode_frame({"type": "ping"})
    assert len(frame) - HEADER_SIZE == 16
    assert frame[:4] == b"\x00\x00\x00\x10"


def test_chinese_payload_roundtrip() -> None:
    obj = {"type": "reply", "data": "服务端已收到：你好，世界"}
    assert recv_one(encode_frame(obj)) == obj


def test_empty_object_roundtrip() -> None:
    assert recv_one(encode_frame({})) == {}


# --------------------------------------------------------------------------- #
# 解码：粘包与半包
# --------------------------------------------------------------------------- #


def test_three_frames_glued_together_are_split_correctly() -> None:
    """连发 3 条消息（粘包），接收端必须精确拆成 3 条。"""
    expected = [{"type": "hello", "data": f"第{i + 1}条打招呼"} for i in range(3)]
    glued = b"".join(encode_frame(item) for item in expected)

    assert recv_many(glued, count=3) == expected


def test_frame_split_into_single_bytes() -> None:
    """一帧被拆成一个字节一个字节到达（半包），仍必须完整还原。"""
    obj = {"type": "new_conn", "conn_id": "abc123", "local_port": 8000}
    frame = encode_frame(obj)

    async def scenario() -> Dict[str, Any]:
        reader = asyncio.StreamReader()

        async def slow_feed() -> None:
            for index in range(len(frame)):
                reader.feed_data(frame[index : index + 1])
                await asyncio.sleep(0)
            reader.feed_eof()

        feeder = asyncio.create_task(slow_feed())
        try:
            return await recv_msg(reader)
        finally:
            await feeder

    assert asyncio.run(scenario()) == obj


def test_mixed_glue_and_split() -> None:
    """真实网络的常态：首帧完整到达，次帧被切成两半，末帧与前一半的尾巴粘在一起。"""
    first = encode_frame({"seq": 1})
    second = encode_frame({"seq": 2, "data": "x" * 40})
    third = encode_frame({"seq": 3})

    cut = len(second) // 2
    actual = recv_many(first + second[:cut], second[cut:] + third, count=3)

    assert actual == [{"seq": 1}, {"seq": 2, "data": "x" * 40}, {"seq": 3}]


# --------------------------------------------------------------------------- #
# 解码：异常路径
# --------------------------------------------------------------------------- #


def test_declared_length_over_limit_is_rejected_before_allocating() -> None:
    """对端谎报超大长度时必须立刻拒绝，而不是傻等或按该长度分配内存。"""
    bogus_header = struct.pack(HEADER_FMT, MAX_MSG_LEN + 1)
    with pytest.raises(ProtocolError, match="超过上限"):
        recv_should_fail(bogus_header)


def test_invalid_json_body_is_rejected() -> None:
    body = b"not-a-json"
    with pytest.raises(ProtocolError, match="合法 JSON"):
        recv_should_fail(struct.pack(HEADER_FMT, len(body)) + body)


def test_non_object_json_is_rejected() -> None:
    body = b"[1, 2, 3]"
    with pytest.raises(ProtocolError, match="必须是 JSON 对象"):
        recv_should_fail(struct.pack(HEADER_FMT, len(body)) + body)


def test_truncated_header_is_rejected() -> None:
    with pytest.raises(ProtocolError, match="长度头"):
        recv_should_fail(b"\x00\x00")


def test_truncated_body_is_rejected() -> None:
    with pytest.raises(ProtocolError, match="消息体"):
        recv_should_fail(struct.pack(HEADER_FMT, 100) + b"abc")


def test_no_data_at_all_is_rejected() -> None:
    with pytest.raises(ProtocolError):
        recv_should_fail(b"")


# --------------------------------------------------------------------------- #
# 发送 / 接收：上限保护
# --------------------------------------------------------------------------- #


def test_encode_frame_rejects_oversized_payload() -> None:
    with pytest.raises(ProtocolError, match="超过上限"):
        encode_frame({"data": "x" * 100}, max_msg_len=10)


def test_codec_send_rejects_oversized_payload() -> None:
    codec = LengthPrefixedJSONCodec(max_msg_len=10)

    async def scenario() -> None:
        await codec.send(_FakeWriter(), {"type": "ping"})

    with pytest.raises(ProtocolError, match="超过上限"):
        asyncio.run(scenario())


def test_codec_custom_limit_is_honoured_on_receive() -> None:
    """``{"a": 1}`` 编码后正是 8 字节，因此上限 8 收得下、上限 4 必须拒绝。"""
    codec = LengthPrefixedJSONCodec(max_msg_len=8)
    frame = encode_frame({"a": 1})
    assert len(frame) - HEADER_SIZE == 8

    async def receives() -> Dict[str, Any]:
        reader = asyncio.StreamReader()
        reader.feed_data(frame)
        reader.feed_eof()
        return await codec.recv(reader)

    assert asyncio.run(receives()) == {"a": 1}

    strict = LengthPrefixedJSONCodec(max_msg_len=4)

    async def rejects() -> Dict[str, Any]:
        reader = asyncio.StreamReader()
        reader.feed_data(frame)
        reader.feed_eof()
        return await strict.recv(reader)

    with pytest.raises(ProtocolError, match="超过上限"):
        asyncio.run(rejects())
