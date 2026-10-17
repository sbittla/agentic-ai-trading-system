"""Leaders falling toward their 200-day average (MO-26, MO-27).

``watch`` lists the strong stocks (top half of the composite, rising 200-day average, first
test) whose close is within ``max_dist`` above the average: the price point where the
``SMA200_PULLBACK`` setup would arm. ``history`` is each stock's own record at that line:
how often a past first test held and what followed. Bars up to the session only (MO-19).
"""

from __future__ import annotations

import math
from datetime import date
from typing import Any

import numpy as np

from trader.momentum.factors import MomFrame, Panel
from trader.momentum.setups import sma200_trend

HOLD_SESSIONS = 20         # a test "held" when no close fell 3% under the average in 20 sessions
GAP = 20                   # touches closer together than this count once


def touches(x: MomFrame, upto: int) -> list[int]:
    """Sessions up to ``upto`` where a first test of the rising 200-day average happened
    (the setup's touch rule, without its R limits)."""
    out: list[int] = []
    for i in range(260, upto + 1):
        if out and i - out[-1] < GAP:
            continue
        s = x.sma200[i]
        if x.l[i] <= 1.01 * s and x.c[i] >= 0.98 * s and sma200_trend(x, i):
            out.append(i)
    return out


def history(x: MomFrame, upto: int) -> dict[str, Any]:
    """The stock's past first tests with 60 sessions of follow-through before ``upto``."""
    rows: list[dict[str, Any]] = []
    for i in touches(x, upto - 60):
        c0 = float(x.c[i])
        later = x.c[i + 1:i + 1 + HOLD_SESSIONS] / x.sma200[i + 1:i + 1 + HOLD_SESSIONS]
        rows.append({"date": x.dates[i].isoformat(),
                     "held": bool(np.all(later >= 0.97)),
                     "r20": float(x.c[i + 20] / c0 - 1), "r60": float(x.c[i + 60] / c0 - 1)})
    if not rows:
        return {"tests": 0}
    r20 = [r["r20"] for r in rows]
    r60 = [r["r60"] for r in rows]
    return {"tests": len(rows), "held_rate": round(sum(r["held"] for r in rows) / len(rows), 2),
            "up20_rate": round(sum(v > 0 for v in r20) / len(r20), 2),
            "median_r20_pct": round(float(np.median(r20)) * 100, 1),
            "median_r60_pct": round(float(np.median(r60)) * 100, 1),
            "last": rows[-1]}


def watch(panel: Panel, day: date, *, max_dist: float = 0.06, min_composite: float = 0.5,
          always: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """Strong stocks within ``max_dist`` above a rising 200-day average, nearest first.
    ``always``: tickers listed whatever their composite (the tradable universe)."""
    comp = panel.composite(day)
    rs = panel.rs(day)
    out = []
    for sym, i in panel.eligible(day).items():
        x = panel.frames[sym]
        if comp.get(sym, 0.0) < min_composite and sym not in always:
            continue
        s = float(x.sma200[i])
        if math.isnan(s) or not sma200_trend(x, i):
            continue
        dist = float(x.c[i]) / s - 1
        if not 0 <= dist <= max_dist:
            continue
        a = float(x.atr[i])
        out.append({"ticker": sym, "close": round(float(x.c[i]), 2), "sma200": round(s, 2),
                    "dist_pct": round(dist * 100, 2),
                    "atr_away": round((float(x.c[i]) - s) / a, 2) if a > 0 else None,
                    "from_high_pct": round((float(x.c[i]) / float(x.hi252[i]) - 1) * 100, 1),
                    "composite": round(comp.get(sym, 0.0), 3), "rs": rs.get(sym, 0),
                    "history": history(x, i)})
    out.sort(key=lambda r: (r["dist_pct"], r["ticker"]))
    return out
