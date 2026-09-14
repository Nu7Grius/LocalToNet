# -*- coding: utf-8 -*-
"""
protocol.py —— 帧协议层（长度头 + JSON）
=========================================
解决 TCP 粘包 / 半包问题：发送端先写 4 字节大端长度头，接收端先精确读 4 字节
拿到消息体长度，再精确读 length 字节拿到**完整的一条**消息。

    消息帧 = ┌ 4 字节长度头 (struct.pack("!I", n)) ┬ JSON 消息体 (n 字节 UTF-8) ┐

对外提供三层 API：

1. 模块级快捷函数 ``send_msg`` / ``recv_msg``
   —— 与教程用法完全一致，内部使用默认编解码器。
2. ``Codec`` 抽象 + ``LengthPrefixedJSONCodec``
   —— **扩展点**：将来要换 msgpack / protobuf / 压缩，只需新增一个 Codec 子类，
      服务端与客户端的业务代码零改动。
3. ``MsgType`` 指令常量
   —— 避免指令名字符串散落各处，拼错时静默失效。
"""

from __future__ import annotations

import asyncio
import json
import struct
from abc import ABC, abstractmethod
from typing import Any, Dict

__all__ = [
    "HEADER_FMT",
    "HEADER_SIZE",
    "MAX_MSG_LEN",
    "MsgType",
    "ProtocolError",
    "FrameLengthError",
    "Codec",
    "LengthPrefixedJSONCodec",
    "encode_frame",
    "send_msg",
    "recv_msg",
    "make_msg",
]

HEADER_FMT = "!I"
"""!I = 大端序 4 字节无符号整数。"""

HEADER_SIZE = struct.calcsize(HEADER_FMT)
"""长度头固定 4 字节。"""

MAX_MSG_LEN = 10 * 1024 * 1024
"""单条消息上限 10MB。

控制通道只传小 JSON，实际用量远低于此值；这个上限是**防御性**的——
防止对端声称一个超大长度导致本端按该长度分配内存。
"""


class ProtocolError(Exception):
    """帧编解码异常：长度越界、非法 JSON、连接提前关闭。"""


class FrameLengthError(ProtocolError):
    """长度头声明的长度超出上限。

    单独立一个子类，是因为它有一个高度特征化的成因：**对端把非本协议的数据
    当成了帧**。最典型的就是 TLS 记录头被解析成帧长度——
    TLS 1.3 的 ClientHello 前 4 字节是 ``16 03 01 …``，大端解析出来约 3.7 亿字节，
    必然越界。服务端据此能在日志里直接点破"你可能连错加密方式了"，
    而不是丢一句"长度非法"让人去猜。
    """


class MsgType:
    """指令集常量。与教程协议表一一对应。"""

    REGISTER_CLIENT = "register_client"
    REGISTER_ACK = "register_ack"
    MAPPING_LIST = "mapping_list"
    NEW_CONN = "new_conn"
    REGISTER = "register"
    PING = "ping"
    PONG = "pong"
    CONN_ERROR = "conn_error"
    SET_MAPPING = "set_mapping"
    MAPPING_RESULT = "mapping_result"


def make_msg(msg_type: str, **fields: Any) -> Dict[str, Any]:
    """构造一条消息字典，统一把 ``type`` 放在首位，便于日志阅读。"""
    return {"type": msg_type, **fields}


def encode_frame(obj: Dict[str, Any], max_msg_len: int = MAX_MSG_LEN) -> bytes:
    """把消息字典编码成 ``长度头 + JSON 体`` 的完整帧字节。

    抽成纯函数是为了让单测可以直接断言字节内容，不必先搭一条 TCP 连接。
    """
    payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    if len(payload) > max_msg_len:
        raise ProtocolError(f"消息体 {len(payload)} 字节，超过上限 {max_msg_len}")
    return struct.pack(HEADER_FMT, len(payload)) + payload


class Codec(ABC):
    """编解码器抽象。实现这套接口即可整体替换线上帧格式。"""

    @abstractmethod
    async def send(self, writer: asyncio.StreamWriter, obj: Dict[str, Any]) -> None:
        """发送一条消息。"""

    @abstractmethod
    async def recv(self, reader: asyncio.StreamReader) -> Dict[str, Any]:
        """接收一条消息，连接断开时抛 :class:`ProtocolError`。"""


class LengthPrefixedJSONCodec(Codec):
    """默认实现：4 字节大端长度头 + UTF-8 JSON。"""

    def __init__(
        self,
        max_msg_len: int = MAX_MSG_LEN,
        encoding: str = "utf-8",
    ) -> None:
        self._max_msg_len = max_msg_len
        self._encoding = encoding

    @property
    def max_msg_len(self) -> int:
        return self._max_msg_len

    async def send(self, writer: asyncio.StreamWriter, obj: Dict[str, Any]) -> None:
        writer.write(encode_frame(obj, self._max_msg_len))
        await writer.drain()

    async def recv(self, reader: asyncio.StreamReader) -> Dict[str, Any]:
        try:
            header = await reader.readexactly(HEADER_SIZE)
        except asyncio.IncompleteReadError as exc:
            raise ProtocolError("对端在读取长度头时关闭了连接") from exc

        (length,) = struct.unpack(HEADER_FMT, header)
        if length > self._max_msg_len:
            raise FrameLengthError(f"对端声明的消息体长度 {length} 超过上限 {self._max_msg_len}")

        try:
            body = await reader.readexactly(length)
        except asyncio.IncompleteReadError as exc:
            raise ProtocolError(
                f"对端在读取消息体时关闭了连接（声明 {length} 字节，实到 {len(exc.partial)} 字节）"
            ) from exc

        try:
            msg = json.loads(body.decode(self._encoding))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtocolError(f"消息体不是合法 JSON：{exc}") from exc

        if not isinstance(msg, dict):
            raise ProtocolError(f"消息体必须是 JSON 对象，实际为 {type(msg).__name__}")
        return msg


_default_codec = LengthPrefixedJSONCodec()


async def send_msg(writer: asyncio.StreamWriter, obj: Dict[str, Any]) -> None:
    """用默认编解码器发送一条消息（教程同名 API）。"""
    await _default_codec.send(writer, obj)


async def recv_msg(reader: asyncio.StreamReader) -> Dict[str, Any]:
    """用默认编解码器接收一条消息（教程同名 API）。"""
    return await _default_codec.recv(reader)
