"""Stock-page indicators computed on demand (UI-4 item 3). Pure functions over OHLC dicts
(``{"o","h","l","c","v"}``), newest last."""

from __future__ import annotations

import math
from typing import Any


def ema(values: list[float], n: int) -> list[float | None]:
    out: list[float | None] = []
    k = 2 / (n + 1)
    e: float | None = None
    for i, v in enumerate(values):
        if i + 1 < n:
            out.append(None)
            continue
        e = sum(values[i + 1 - n : i + 1]) / n if e is None else v * k + e * (1 - k)
        out.append(e)
    return out


def rsi(closes: list[float], n: int = 14) -> float | None:
    if len(closes) <= n:
        return None
    gains = losses = 0.0
    for a, b in zip(closes[:n], closes[1 : n + 1], strict=True):
        d = b - a
        gains += max(d, 0)
        losses += max(-d, 0)
    ag, al = gains / n, losses / n
    for a, b in zip(closes[n:-1], closes[n + 1 :], strict=True):
        d = b - a
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)


def macd(closes: list[float]) -> tuple[float, float, float] | None:
    if len(closes) < 35:
        return None
    fast, slow = ema(closes, 12), ema(closes, 26)
    line = [f - s for f, s in zip(fast, slow, strict=True) if f is not None and s is not None]
    sig = ema(line, 9)
    if not line or sig[-1] is None:
        return None
    return line[-1], sig[-1], line[-1] - sig[-1]


def atr(bars: list[dict[str, Any]], n: int = 14) -> float | None:
    if len(bars) <= n:
        return None
    trs = [
        max(b["h"] - b["l"], abs(b["h"] - p["c"]), abs(b["l"] - p["c"]))
        for p, b in zip(bars[:-1], bars[1:], strict=True)
    ]
    a = sum(trs[:n]) / n
    for tr in trs[n:]:
        a = (a * (n - 1) + tr) / n
    return float(a)


def bollinger(
    closes: list[float], n: int = 20, k: float = 2.0
) -> tuple[float, float, float] | None:
    if len(closes) < n:
        return None
    w = closes[-n:]
    m = sum(w) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in w) / n)
    return m - k * sd, m, m + k * sd


def keltner(
    bars: list[dict[str, Any]], n: int = 20, k: float = 2.0
) -> tuple[float, float, float] | None:
    closes = [b["c"] for b in bars]
    e = ema(closes, n)
    a = atr(bars, n)
    if not e or e[-1] is None or a is None:
        return None
    return e[-1] - k * a, e[-1], e[-1] + k * a


def summary(
    intraday: list[dict[str, Any]], daily: list[dict[str, Any]], prior_close: float | None
) -> dict[str, Any]:
    """RSI, MACD, ATR(5m), gap %, position in the day's range, Bollinger/Keltner."""
    closes = [b["c"] for b in intraday]
    five = [
        {
            "o": g[0]["o"],
            "h": max(b["h"] for b in g),
            "l": min(b["l"] for b in g),
            "c": g[-1]["c"],
            "v": sum(b["v"] for b in g),
        }
        for g in (intraday[i : i + 5] for i in range(0, len(intraday), 5))
        if g
    ]
    out: dict[str, Any] = {
        "rsi14": rsi(closes),
        "macd": macd(closes),
        "atr5m": atr(five),
        "bollinger": bollinger(closes),
        "keltner": keltner(intraday) if len(intraday) > 21 else None,
    }
    if intraday and prior_close:
        out["gap_pct"] = (intraday[0]["o"] / prior_close - 1) * 100
    if intraday:
        hi, lo = max(b["h"] for b in intraday), min(b["l"] for b in intraday)
        out["range_pos"] = (closes[-1] - lo) / (hi - lo) if hi > lo else None
    dcl = [b["c"] for b in daily]
    out["ema20d"] = ema(dcl, 20)[-1] if len(dcl) >= 20 else None
    out["ema50d"] = ema(dcl, 50)[-1] if len(dcl) >= 50 else None
    out["atr14d"] = atr(daily, 14)
    out["rsi14d"] = rsi(dcl)
    return out
