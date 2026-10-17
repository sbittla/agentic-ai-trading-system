"""Momentum setups (MO-7, §6). Long only (ADR-008); each armed at the close of bar i for the
next session. Uses bars up to i only (MO-19)."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

import numpy as np

from trader.momentum.factors import MomFrame

R_MIN_ATR = 0.25
R_MAX_ATR = 2.5


@dataclass(frozen=True)
class Cand:
    trigger: float
    stop: float
    why: str
    entry: Literal["stop", "open"] = "stop"


SetupFn = Callable[[MomFrame, int], Cand | None]


@dataclass(frozen=True)
class Setup:
    name: str
    fn: SetupFn
    needs_leader: bool
    policy: str
    doc: str
    r_max_atr: float = R_MAX_ATR


def _b(x: MomFrame, i: int) -> float:
    return 0.05 * float(x.atr[i])


def _f(v: float) -> str:
    return f"{v:.2f}"


def momentum_breakout(x: MomFrame, i: int) -> Cand | None:
    """Qullamaggie: a 30%+ advance into a high, then a tight base (5-40 sessions, <= 15%
    below that high) above SMA50 that is tightening, closing near the high."""
    if i < 110:
        return None
    w = x.h[i - 39:i + 1]
    p = i - 39 + int(np.argmax(w))                   # the advance's top
    n = i - p + 1
    if n < 5:
        return None
    hi = float(x.h[p])
    lo = float(x.l[p:i + 1].min())
    if (hi - lo) / hi > 0.15 or lo <= x.sma50[i] or x.c[i] < 0.97 * hi:
        return None
    if n >= 10:                                       # ranges contracting into the pivot
        half = n // 2
        first = float(x.h[p:p + half].max() - x.l[p:p + half].min())
        last = float(x.h[i - 4:i + 1].max() - x.l[i - 4:i + 1].min())
        if not last < first:
            return None
    bottom = float(x.l[p - 60:p + 1].min())
    if hi < 1.30 * bottom:
        return None
    a = float(x.atr[i])
    trig = hi + _b(x, i)
    stop = max(lo, trig - 1.5 * a) - _b(x, i)
    return Cand(trig, stop, f"{n}-session base {(hi - lo) / hi:.0%} deep after a "
                f"{hi / bottom - 1:.0%} advance, close {_f(x.c[i])} near the high {_f(hi)}")


def vcp(x: MomFrame, i: int) -> Cand | None:
    """Minervini volatility contraction: a 20-60 session base <= 25% deep, the last 10
    sessions within half its range on dry volume, close within 5% of the base high."""
    if i < 70 or not x.vol50[i] > 0:
        return None
    for n in (60, 50, 40, 30, 20):
        hi = float(x.h[i - n + 1:i + 1].max())
        lo = float(x.l[i - n + 1:i + 1].min())
        if (hi - lo) / hi > 0.25:
            continue
        tight = float(x.h[i - 9:i + 1].max() - x.l[i - 9:i + 1].min())
        if tight > 0.5 * (hi - lo) or float(x.v[i - 9:i + 1].mean()) > 0.8 * x.vol50[i]:
            return None
        if x.c[i] < 0.95 * hi:
            return None
        lo10 = float(x.l[i - 9:i + 1].min())
        return Cand(hi + _b(x, i), lo10 - _b(x, i),
                    f"{n}-session base {(hi - lo) / hi:.0%} deep, last 10 sessions "
                    f"{tight / (hi - lo):.0%} of it on {x.v[i - 9:i + 1].mean() / x.vol50[i]:.0%}"
                    f" volume; pivot {_f(hi)}")
    return None


def high_tight_flag(x: MomFrame, i: int) -> Cand | None:
    """O'Neil: +90% in at most 40 sessions, then a 10-25 session flag down at most 25%."""
    if i < 110:
        return None
    w = x.h[i - 59:i + 1]
    p = i - 59 + int(np.argmax(w))
    k = i - p
    if not 10 <= k <= 25:
        return None
    top = float(x.h[p])
    base = float(x.l[p - 40:p + 1].min())
    if top < 1.90 * base:
        return None
    fl_lo = float(x.l[p + 1:i + 1].min())
    fl_hi = float(x.h[p + 1:i + 1].max())
    if fl_lo < 0.75 * top or x.c[i] < 0.90 * top:
        return None
    return Cand(fl_hi + _b(x, i), fl_lo - _b(x, i),
                f"+{top / base - 1:.0%} in 40 sessions to {_f(top)}, then a {k}-session flag "
                f"{1 - fl_lo / top:.0%} deep")


def episodic_pivot(x: MomFrame, i: int) -> Cand | None:
    """Qullamaggie EP: a gap of 8%+ on 3x volume that holds (close above the open, top half)."""
    if i < 60 or not x.vol50[i] > 0:
        return None
    gap = x.o[i] / x.c[i - 1] - 1
    rng = x.h[i] - x.l[i]
    if gap < 0.08 or x.v[i] < 3 * x.vol50[i] or rng <= 0:
        return None
    if x.c[i] < x.o[i] or (x.c[i] - x.l[i]) / rng < 0.5:
        return None
    return Cand(float(x.h[i]) + _b(x, i), float(x.l[i]) - _b(x, i),
                f"gap {gap:+.0%} on {x.v[i] / x.vol50[i]:.1f}x volume, held into the close")


def pocket_pivot(x: MomFrame, i: int) -> Cand | None:
    """Kacher & Morales: an up day out-volumes every down day of the prior 10 sessions, at
    the 10-day average in an uptrend."""
    if i < 60 or not x.c[i] > x.c[i - 1]:
        return None
    downs = [j for j in range(i - 10, i) if x.c[j] < x.c[j - 1]]
    if not downs or x.v[i] <= max(float(x.v[j]) for j in downs):
        return None
    a = float(x.atr[i])
    if not (x.c[i] > x.sma50[i] and abs(x.c[i] - x.sma10[i]) <= a):
        return None
    return Cand(float(x.c[i]), float(x.l[i - 2:i + 1].min()) - _b(x, i),
                f"pocket pivot: volume {x.v[i]:,.0f} above every down day of 10 sessions, at "
                f"the 10-day average {_f(x.sma10[i])}", entry="open")


def donchian_55(x: MomFrame, i: int) -> Cand | None:
    """Turtle System 2: buy a new 55-session high, stop 2N."""
    if i < 60:
        return None
    lvl = float(x.h[i - 54:i + 1].max())
    n = float(x.atr20[i])
    if math.isnan(n) or x.c[i] < lvl - n:
        return None
    trig = lvl + _b(x, i)
    return Cand(trig, trig - 2 * n, f"close {_f(x.c[i])} within 1N of the 55-session high "
                f"{_f(lvl)} (Turtle System 2)")


def high_52w(x: MomFrame, i: int) -> Cand | None:
    """George & Hwang / the scanner's HIGH_52W: a tight 10-session range under the high."""
    if i < 252 or not x.c[i] > x.sma50[i] > x.sma200[i]:
        return None
    top, a = float(x.hi252[i]), float(x.atr[i])
    if not (top - a <= x.c[i] < top) or x.h[i] >= top:
        return None
    lo10 = float(x.l[i - 9:i + 1].min())
    if float(x.h[i - 9:i + 1].max()) - lo10 > 2.5 * a:
        return None
    return Cand(top + _b(x, i), lo10 - _b(x, i),
                f"tight 10-session range just under the 52-week high {_f(top)}")


SETUPS: tuple[Setup, ...] = (
    Setup("MOMENTUM_BREAKOUT", momentum_breakout, True, "ema10",
          "tight base after a 30%+ advance (Qullamaggie)"),
    Setup("VCP", vcp, True, "ema20", "volatility contraction pattern (Minervini)"),
    Setup("HIGH_TIGHT_FLAG", high_tight_flag, True, "ema20", "high tight flag (O'Neil)"),
    Setup("EPISODIC_PIVOT", episodic_pivot, False, "ema10",
          "gap on volume that holds (Qullamaggie EP)", r_max_atr=4.0),
    Setup("POCKET_PIVOT", pocket_pivot, True, "ema20", "pocket pivot (Kacher & Morales)"),
    Setup("DONCHIAN_55", donchian_55, True, "donchian20", "55-session breakout (Turtle S2)"),
    Setup("HIGH_52W", high_52w, True, "chandelier", "tight range under the 52-week high"),
)
SETUP_NAMES = tuple(s.name for s in SETUPS)


def setups_at(x: MomFrame, i: int, leader: bool, disabled: frozenset[str] = frozenset()
              ) -> list[tuple[Setup, Cand]]:
    """Every setup armed on bar i, with R between 0.25 ATR and the setup's maximum."""
    out: list[tuple[Setup, Cand]] = []
    a = float(x.atr[i])
    if math.isnan(a) or a <= 0:
        return out
    for st in SETUPS:
        if st.name in disabled or (st.needs_leader and not leader):
            continue
        cand = st.fn(x, i)
        if cand is None:
            continue
        r = cand.trigger - cand.stop
        if not (R_MIN_ATR * a <= r <= st.r_max_atr * a):
            continue
        out.append((st, cand))
    return out
