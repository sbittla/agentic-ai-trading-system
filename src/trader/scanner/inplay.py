"""Next-session day-trading watchlist (SCANNER_REQUIREMENTS TS-5).

Measured on the stored history (docs/SCANNER_REQUIREMENTS.md §6): directional day trades
taken from daily patterns (NR7, inside day, range expansion) lost money, and NR7/inside days
did not predict a wider next session on these stocks. What did predict tomorrow's range,
across tickers, was the stock's own ATR% and, to a lesser degree, today's relative volume
and range. So the day list ranks tickers by expected range and hands the trader the levels;
the direction comes intraday from the playbook (ORB, VWAP reclaim, ...) and the alerts bot.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from trader.scanner import ta
from trader.scanner.techniques import MIN_HISTORY, Series

RVOL_WEIGHT = 0.15
RANGE_WEIGHT = 0.15


@dataclass
class InPlay:
    symbol: str
    asof: date
    close: float
    atr_pct: float              # ATR14 / close, %
    rvol: float                 # today's volume / prior 50-session average
    range_atr: float            # today's range / ATR14
    expected_range_pct: float   # TS-5 ranking value
    bias: str                   # long | short | neutral (trend and EMA20)
    levels: dict[str, float] = field(default_factory=dict)
    plan: str = ""
    rank: int = 0
    index: int = -1

    def as_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in self.__dict__.items() if k != "index"}
        d["asof"] = self.asof.isoformat()
        return d


def expected_range(atr_pct: float, rvol: float, range_atr: float) -> float:
    """ATR% scaled by how much busier than usual today was (clamped 0.7x..1.6x)."""
    k = 1 + RVOL_WEIGHT * (min(rvol, 4.0) - 1) + RANGE_WEIGHT * (min(range_atr, 3.0) - 1)
    return atr_pct * max(0.7, min(1.6, k))


class _Ind:
    def __init__(self, s: Series) -> None:
        self.atr = ta.atr(s.h, s.l, s.c, 14)
        self.ema20 = ta.ema(s.c, 20)
        self.sma50, self.sma200 = ta.sma(s.c, 50), ta.sma(s.c, 200)
        self.vol50 = ta.shift(ta.sma(s.v, 50))
        self.hi20, self.lo20 = ta.rolling_max(s.h, 20), ta.rolling_min(s.l, 20)


def in_play_at(s: Series, ind: _Ind, i: int) -> InPlay | None:
    a, v50 = float(ind.atr[i]), float(ind.vol50[i])
    if i < MIN_HISTORY - 1 or math.isnan(a) or math.isnan(v50) or v50 <= 0 or a <= 0:
        return None
    c = float(s.c[i])
    atr_pct, rvol, range_atr = a / c * 100, float(s.v[i]) / v50, float(s.h[i] - s.l[i]) / a
    up = c > ind.ema20[i] and c > ind.sma50[i] > ind.sma200[i]
    down = c < ind.ema20[i] and c < ind.sma50[i] < ind.sma200[i]
    bias = "long" if up else "short" if down else "neutral"
    pdh, pdl = round(float(s.h[i]), 2), round(float(s.l[i]), 2)
    levels = {"prior_high": pdh, "prior_low": pdl, "prior_close": round(c, 2),
              "high_20d": round(float(ind.hi20[i]), 2), "low_20d": round(float(ind.lo20[i]), 2),
              "ema20": round(float(ind.ema20[i]), 2)}
    plan = {"long": f"favour longs: ORB or VWAP reclaim, strength above {pdh:.2f}",
            "short": f"favour shorts: ORB breakdown or VWAP loss, weakness below {pdl:.2f}",
            "neutral": f"no trend bias: trade the first clean break of {pdl:.2f}-{pdh:.2f}"}[bias]
    return InPlay(s.symbol, s.dates[i], round(c, 2), round(atr_pct, 2), round(rvol, 2),
                  round(range_atr, 2), round(expected_range(atr_pct, rvol, range_atr), 2),
                  bias, levels, plan, index=i)


def in_play(series: dict[str, Series], asof: date | None = None) -> list[InPlay]:
    """Watchlist for the session after ``asof`` (default: each symbol's last bar), ranked by
    expected range."""
    out: list[InPlay] = []
    for s in series.values():
        n = len(s.c) if asof is None else sum(1 for d in s.dates if d <= asof)
        if n < MIN_HISTORY or (asof is not None and s.dates[n - 1] != asof):
            continue
        s = s.upto(n)
        row = in_play_at(s, _Ind(s), n - 1)
        if row:
            out.append(row)
    out.sort(key=lambda r: (-r.expected_range_pct, r.symbol))
    for k, r in enumerate(out, 1):
        r.rank = k
    return out


def evaluate_in_play(series: dict[str, Series], top_n: int = 3, start: date | None = None,
                     end: date | None = None) -> dict[str, Any]:
    """TS-12 for the day list: each session, did the top ``top_n`` tickers by expected range
    move more the next day than the average ticker? Also the calibration (realised/expected)."""
    by_day: dict[date, list[tuple[float, float]]] = {}
    for s in series.values():
        if len(s.c) < MIN_HISTORY + 1:
            continue
        ind = _Ind(s)
        for i in range(MIN_HISTORY - 1, len(s.c) - 1):
            d = s.dates[i]
            if (start and d < start) or (end and d > end):
                continue
            row = in_play_at(s, ind, i)
            if row is None:
                continue
            realised = float(s.h[i + 1] - s.l[i + 1]) / float(s.c[i]) * 100
            by_day.setdefault(d, []).append((row.expected_range_pct, realised))
    top: list[float] = []
    every: list[float] = []
    ratio: list[float] = []
    hits = days = 0
    for rows in by_day.values():
        if len(rows) <= top_n:
            continue
        rows.sort(key=lambda r: -r[0])
        picked = [r for _, r in rows[:top_n]]
        avg_all = sum(r for _, r in rows) / len(rows)
        top += picked
        every += [r for _, r in rows]
        ratio += [r / e for e, r in rows if e > 0]
        days += 1
        hits += sum(picked) / len(picked) > avg_all
    if not days:
        return {"days": 0}
    return {"days": days, "top_n": top_n,
            "top_avg_range_pct": round(sum(top) / len(top), 2),
            "all_avg_range_pct": round(sum(every) / len(every), 2),
            "lift": round((sum(top) / len(top)) / (sum(every) / len(every)), 2),
            "days_top_beat_avg": round(hits / days, 3),
            "calibration": round(sum(ratio) / len(ratio), 2)}
