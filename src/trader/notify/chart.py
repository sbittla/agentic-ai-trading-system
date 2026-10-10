"""Approval chart image (REQUIREMENTS AP-1) with no imaging dependency.

A tiny RGB raster with candles and horizontal levels, encoded as PNG with ``zlib``. The text
(prices, labels) goes in the message caption; the image shows the shape: 5-minute candles,
session VWAP (orange), the opening range (grey), entry (blue), stop (red) and T1 (green).
About 10-40 KB, rendered in a few milliseconds.
"""

from __future__ import annotations

import struct
import zlib
from collections.abc import Sequence

from trader.models.messages import Bar

RGB = tuple[int, int, int]
BG: RGB = (255, 255, 255)
GRID: RGB = (235, 235, 235)
UP: RGB = (38, 166, 91)
DOWN: RGB = (214, 69, 65)
VWAP: RGB = (240, 140, 0)
OR_C: RGB = (150, 150, 150)
ENTRY: RGB = (33, 102, 204)
STOP: RGB = (214, 40, 40)
T1: RGB = (20, 150, 60)


class Canvas:
    def __init__(self, w: int, h: int, bg: RGB = BG) -> None:
        self.w, self.h = w, h
        self.px = bytearray(bytes(bg) * (w * h))

    def set(self, x: int, y: int, c: RGB) -> None:
        if 0 <= x < self.w and 0 <= y < self.h:
            i = (y * self.w + x) * 3
            self.px[i:i + 3] = bytes(c)

    def rect(self, x0: int, y0: int, x1: int, y1: int, c: RGB) -> None:
        for y in range(max(0, min(y0, y1)), min(self.h, max(y0, y1) + 1)):
            for x in range(max(0, min(x0, x1)), min(self.w, max(x0, x1) + 1)):
                self.set(x, y, c)

    def hline(self, y: int, c: RGB, dash: int = 0, x0: int = 0, x1: int | None = None,
              thick: int = 1) -> None:
        x1 = self.w - 1 if x1 is None else x1
        for t in range(thick):
            for x in range(x0, x1 + 1):
                if not dash or (x // dash) % 2 == 0:
                    self.set(x, y + t, c)

    def line(self, x0: int, y0: int, x1: int, y1: int, c: RGB) -> None:
        dx, dy = abs(x1 - x0), -abs(y1 - y0)
        sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
        err = dx + dy
        while True:
            self.set(x0, y0, c)
            self.set(x0, y0 + 1, c)
            if x0 == x1 and y0 == y1:
                return
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                x0 += sx
            if e2 <= dx:
                err += dx
                y0 += sy

    def png(self) -> bytes:
        raw = b"".join(b"\x00" + bytes(self.px[y * self.w * 3:(y + 1) * self.w * 3])
                       for y in range(self.h))

        def chunk(kind: bytes, data: bytes) -> bytes:
            return (struct.pack(">I", len(data)) + kind + data
                    + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

        return (b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", struct.pack(">IIBBBBB", self.w, self.h, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def approval_chart(bars: Sequence[Bar], vwap: Sequence[float | None], *, entry: float,
                   stop: float, t1: float, or_high: float | None = None,
                   or_low: float | None = None, width: int = 480, height: int = 280) -> bytes:
    """PNG of the last 5-minute candles with the trade's levels."""
    bars = list(bars)[-48:]
    vw = list(vwap)[-len(bars):] if vwap else []
    prices = [entry, stop, t1] + [b.h for b in bars] + [b.l for b in bars] + [
        x for x in (or_high, or_low, *vw) if x is not None]
    lo, hi = min(prices), max(prices)
    pad = (hi - lo) * 0.08 or 0.5
    lo, hi = lo - pad, hi + pad
    cv = Canvas(width, height)
    left, right, top, bottom = 6, width - 6, 6, height - 6

    def y(p: float) -> int:
        return int(bottom - (p - lo) / (hi - lo) * (bottom - top))

    for i in range(1, 5):                                   # light grid
        cv.hline(top + i * (bottom - top) // 5, GRID)
    if or_high is not None:
        cv.hline(y(or_high), OR_C, dash=6)
    if or_low is not None:
        cv.hline(y(or_low), OR_C, dash=6)
    n = max(len(bars), 1)
    step = (right - left) / n
    body = max(2, int(step * 0.6))
    prev = None
    for i, b in enumerate(bars):
        cx = int(left + step * i + step / 2)
        c = UP if b.c >= b.o else DOWN
        cv.rect(cx, y(b.h), cx, y(b.l), c)                  # wick
        cv.rect(cx - body // 2, y(max(b.o, b.c)), cx + body // 2, y(min(b.o, b.c)), c)
        v = vw[i] if i < len(vw) else None
        if v is not None:
            pt = (cx, y(v))
            if prev is not None:
                cv.line(prev[0], prev[1], pt[0], pt[1], VWAP)
            prev = pt
    cv.hline(y(entry), ENTRY, thick=2)
    cv.hline(y(stop), STOP, thick=2)
    cv.hline(y(t1), T1, dash=8, thick=2)
    return cv.png()
