# -*- coding: utf-8 -*-
"""make_icon.py —— 生成 assets/icon.ico（256×256，纯标准库，无第三方依赖）。

为什么手写而不引 Pillow：为一张占位图标引入第三方依赖不划算，运行时/构建期
都不想多背一个包。ICO 容器本身极简：

    6 字节文件头（保留=0、类型=1=图标、条目数=1）
  + 16 字节目录条目（宽/高各 1 字节——256 记作 0；色数/保留；位面=1；位深=32；
    内嵌数据字节数；数据偏移=22）
  + 一段 zlib 压缩的 PNG（Vista+ 的 ICO 支持内嵌 PNG，宽度 256 只能这么存）

图案（占位，允许后续人工替换 assets/icon.ico）：
深色圆角方块（#0F172A）+ 浅色圆环（#38BDF8）+ 圆环右上方一颗浅色"数据包"
圆点（#F8FAFC）。用 3× 超采样统计每个像素的形状覆盖率，边缘做廉价抗锯齿。

用法::

    python packaging/make_icon.py [输出.ico]     # 默认 <repo>/assets/icon.ico
"""

from __future__ import annotations

import math
import struct
import sys
import zlib
from pathlib import Path

SIZE = 256  # 图标边长（像素）
SS = 3      # 超采样倍率：每像素 SS×SS 个采样点统计覆盖率

# 图案几何（256 空间坐标）与配色
_RECT = {"cx": 128.0, "cy": 128.0, "hw": 114.0, "hh": 114.0, "r": 54.0, "rgb": (15, 23, 42)}
_RING = {"cx": 128.0, "cy": 128.0, "r_out": 64.0, "r_in": 44.0, "rgb": (56, 189, 248)}
_DOT = {
    "cx": 128.0 + 54.0 * math.cos(math.radians(-40.0)),
    "cy": 128.0 + 54.0 * math.sin(math.radians(-40.0)),
    "r": 11.0,
    "rgb": (248, 250, 252),
}


def sd_round_rect(px: float, py: float, cx: float, cy: float, hw: float, hh: float, r: float) -> float:
    """圆角矩形有符号距离：内部为负，外部为正。"""
    qx = abs(px - cx) - (hw - r)
    qy = abs(py - cy) - (hh - r)
    return math.hypot(max(qx, 0.0), max(qy, 0.0)) + min(max(qx, qy), 0.0) - r


def sd_circle(px: float, py: float, cx: float, cy: float, r: float) -> float:
    """圆有符号距离：内部为负，外部为正。"""
    return math.hypot(px - cx, py - cy) - r


def _coverage(fn, x0: float, y0: float, ss: int = SS) -> float:
    """像素 [x0, x0+1) × [y0, y0+1) 内 ss×ss 网格采样，返回 [0, 1] 覆盖率。"""
    inside = 0
    for sy in range(ss):
        py = y0 + (sy + 0.5) / ss
        for sx in range(ss):
            if fn(x0 + (sx + 0.5) / ss, py) <= 0.0:
                inside += 1
    return inside / (ss * ss)


def _blend(dst: tuple, rgb: tuple, alpha: float) -> tuple:
    """straight-alpha 的 source-over 合成。dst=(r, g, b, a)，a∈[0,1]。"""
    if alpha <= 0.0:
        return dst
    if alpha >= 1.0:
        return (rgb[0], rgb[1], rgb[2], 1.0)
    a_out = alpha + dst[3] * (1.0 - alpha)
    if a_out <= 0.0:
        return (0, 0, 0, 0.0)
    mixed = tuple(
        int(round((rgb[i] * alpha + dst[i] * dst[3] * (1.0 - alpha)) / a_out))
        for i in range(3)
    )
    return (mixed[0], mixed[1], mixed[2], a_out)


def _bbox(cx: float, cy: float, extent: float) -> tuple:
    """形状外接矩形（整数像素范围，外扩 1 像素容纳亚像素边缘）。"""
    return (
        max(0, int(math.floor(cx - extent)) - 1),
        max(0, int(math.floor(cy - extent)) - 1),
        min(SIZE, int(math.ceil(cx + extent)) + 1),
        min(SIZE, int(math.ceil(cy + extent)) + 1),
    )


def render(size: int = SIZE) -> bytes:
    """渲染 RGBA8888 像素缓冲（先行后列，每像素 4 字节）。"""
    bbs = (
        _bbox(_RECT["cx"], _RECT["cy"], max(_RECT["hw"], _RECT["hh"])),
        _bbox(_RING["cx"], _RING["cy"], _RING["r_out"]),
        _bbox(_DOT["cx"], _DOT["cy"], _DOT["r"]),
    )
    fns = (
        lambda x, y: sd_round_rect(x, y, _RECT["cx"], _RECT["cy"], _RECT["hw"], _RECT["hh"], _RECT["r"]),
        lambda x, y: sd_circle(x, y, _RING["cx"], _RING["cy"], _RING["r_out"]),
        lambda x, y: sd_circle(x, y, _RING["cx"], _RING["cy"], _RING["r_in"]),
        lambda x, y: sd_circle(x, y, _DOT["cx"], _DOT["cy"], _DOT["r"]),
    )
    buf = bytearray()
    for y in range(size):
        for x in range(size):
            dst = (0, 0, 0, 0.0)
            for idx, (bb, fn) in enumerate(zip(bbs, fns)):
                if not (bb[0] <= x < bb[2] and bb[1] <= y < bb[3]):
                    continue
                c = _coverage(fn, float(x), float(y))
                if idx == 0:  # 深色圆角方块
                    dst = _blend(dst, _RECT["rgb"], c)
                elif idx == 1:  # 浅色圆环 = 外圆覆盖 − 内圆覆盖（同心，外圆包含内圆）
                    c_in = _coverage(fns[2], float(x), float(y))
                    dst = _blend(dst, _RING["rgb"], max(0.0, c - c_in))
                else:  # 圆环上的浅色圆点
                    dst = _blend(dst, _DOT["rgb"], c)
            buf += bytes(
                (
                    int(round(dst[0])),
                    int(round(dst[1])),
                    int(round(dst[2])),
                    int(round(dst[3] * 255.0)),
                )
            )
    return bytes(buf)


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def encode_png(width: int, height: int, rgba: bytes) -> bytes:
    """最小 PNG 编码器：IHDR + 单块 IDAT（filter 0）+ IEND。"""
    stride = width * 4
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter type: None
        raw += rgba[y * stride : (y + 1) * stride]
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)  # 8bit / RGBA
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + _png_chunk(b"IEND", b"")
    )


def encode_ico(png: bytes) -> bytes:
    """最小 ICO 容器：6 字节头 + 16 字节条目 + 内嵌 PNG。"""
    header = struct.pack("<HHH", 0, 1, 1)  # 保留=0、类型=1（图标）、条目数=1
    entry = struct.pack(
        "<BBBBHHII",
        0,           # 宽：0 表示 256
        0,           # 高：0 表示 256
        0,           # 色数：0（真彩色）
        0,           # 保留
        1,           # 位面
        32,          # 位深
        len(png),    # 内嵌数据字节数
        22,          # 数据偏移 = 6 + 16
    )
    return header + entry + png


def main(argv: list) -> int:
    if len(argv) > 2:
        print("用法：python packaging/make_icon.py [输出.ico]", file=sys.stderr)
        return 2
    out = Path(argv[1]) if len(argv) == 2 else Path(__file__).resolve().parent.parent / "assets" / "icon.ico"
    out.parent.mkdir(parents=True, exist_ok=True)
    png = encode_png(SIZE, SIZE, render(SIZE))
    ico = encode_ico(png)
    out.write_bytes(ico)
    print(f"已生成 {out}（{len(ico)} 字节，PNG 负载 {len(png)} 字节，文件头 {ico[:4].hex(' ')}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
