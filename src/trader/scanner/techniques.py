"""Technique catalog, scoring and ranking (SCANNER_REQUIREMENTS TS-4..TS-9, §3, §4).

Every technique is written once, for longs. Shorts run the same code on the mirrored series
(prices x -1): highs become lows, an RSI of 90 becomes 10, "above SMA200" becomes "below".
Candlestick patterns are not mirror-symmetric, so the frame carries the bullish set for
longs and the bearish set for shorts. Values at index i use bars up to i only (TS-10).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

import numpy as np

from trader.scanner import patterns as pat
from trader.scanner import ta
from trader.scanner.ta import Arr

Side = Literal["long", "short"]
Style = Literal["swing"]

MIN_HISTORY = 210            # SMA200 plus a little slope (TS-16)
VOLUME_MULT = 1.5
SWING_TARGETS = (1.5, 3.0)
R_MIN_ATR = 0.25
R_MAX_ATR = 2.5
ADX_OVERHEATED = 35

SHORT_NAMES = {"BREAKOUT_20D": "BREAKDOWN_20D", "HIGH_52W": "LOW_52W", "BULL_FLAG": "BEAR_FLAG",
               "BASE_BREAKOUT": "BASE_BREAKDOWN"}


@dataclass(frozen=True)
class Series:
    """Daily bars for one symbol, oldest first."""

    symbol: str
    dates: list[date]
    o: Arr
    h: Arr
    l: Arr  # noqa: E741
    c: Arr
    v: Arr

    @classmethod
    def from_rows(cls, symbol: str, rows: list[tuple[date, float, float, float, float, float]]
                  ) -> Series:
        rows = sorted(rows)

        def col(k: int) -> Arr:
            return np.asarray([r[k] for r in rows], dtype=np.float64)
        return cls(symbol, [r[0] for r in rows], col(1), col(2), col(3), col(4), col(5))

    def upto(self, n: int) -> Series:
        return Series(self.symbol, self.dates[:n], self.o[:n], self.h[:n], self.l[:n],
                      self.c[:n], self.v[:n])


class Frame:
    """All indicators for one symbol in one direction (side=+1 long, -1 mirrored short)."""

    def __init__(self, s: Series, spy: Series | None, side: int) -> None:
        self.symbol, self.dates, self.side = s.symbol, s.dates, side
        k = float(side)
        o, c = s.o * k, s.c * k
        h, l = (s.h * k, s.l * k) if side > 0 else (s.l * k, s.h * k)  # noqa: E741
        self.o, self.h, self.l, self.c, self.v = o, h, l, c, s.v
        self.atr = ta.atr(h, l, c, 14)
        self.ema20, self.ema50 = ta.ema(c, 20), ta.ema(c, 50)
        self.sma50, self.sma200 = ta.sma(c, 50), ta.sma(c, 200)
        self.rsi2 = ta.rsi(c, 2)
        self.macd, self.macd_sig, self.macd_hist = ta.macd(c)
        self.bb_up, _, self.bb_lo = ta.bbands(c, 20, 2.0)
        self.kc_up, _, self.kc_lo = ta.keltner(h, l, c, 20, 1.5)
        self.adx = ta.adx(s.h, s.l, s.c, 14)[0]               # direction-free
        self.vol50 = ta.shift(ta.sma(s.v, 50))                # prior 50 sessions
        self.hh20 = ta.shift(ta.rolling_max(h, 20))           # prior 20 sessions
        self.ll20 = ta.shift(ta.rolling_min(l, 20))
        self.high252 = ta.rolling_max(h, max(1, min(252, len(h))))   # 52-week extreme
        self.rng = h - l
        # TS-20 accumulation / distribution (on the mirrored series for shorts)
        self.obv_slope = ta.slope(ta.obv(c, s.v) / max(float(np.mean(s.v)) or 1.0, 1.0), 20)
        self.cmf20 = ta.cmf(h, l, c, s.v, 20)
        self.inside = pat.inside_bar(s.h, s.l)
        self.nr7 = pat.nr(s.h, s.l, 7)
        self.reversal = (pat.bullish_reversal(s.o, s.h, s.l, s.c) if side > 0
                         else pat.bearish_reversal(s.o, s.h, s.l, s.c))
        self.rs63 = np.full(len(c), np.nan)
        self.spy_up = np.full(len(c), np.nan)                 # +1 aligned, -1 against
        self.spy_dist = np.full(len(c), np.nan)               # TS-22 distribution days
        if spy is not None and len(spy.c):
            idx = {d: j for j, d in enumerate(spy.dates)}
            spy_roc, spy_sma = ta.roc(spy.c, 63), ta.sma(spy.c, 50)
            my_roc = ta.roc(s.c, 63)
            for i, d in enumerate(s.dates):
                j = idx.get(d)
                if j is None:
                    continue
                self.rs63[i] = k * (my_roc[i] - spy_roc[j])
                if j >= 26:
                    self.spy_dist[i] = sum(
                        1 for q in range(j - 24, j + 1)
                        if spy.c[q] / spy.c[q - 1] - 1 <= -0.002 and spy.v[q] > spy.v[q - 1])
                if not math.isnan(spy_sma[j]):
                    self.spy_up[i] = 1.0 if k * (spy.c[j] - spy_sma[j]) > 0 else -1.0

    def px(self, v: float) -> float:
        """Mirrored value back to a real price."""
        return round(v * self.side, 2)

    def weekly_up(self, i: int) -> bool | None:
        """Higher timeframe (TS-17): the week-to-date close above a rising 10-week average
        of weekly closes (mirrored for shorts). None with fewer than 15 weeks."""
        closes: list[float] = []
        last: tuple[int, int] | None = None
        for d, c in zip(self.dates[:i + 1], self.c[:i + 1], strict=True):
            key = (d.isocalendar()[0], d.isocalendar()[1])
            if key != last:
                closes.append(float(c))
                last = key
            else:
                closes[-1] = float(c)
        if len(closes) < 15:
            return None
        w = ta.sma(np.asarray(closes, dtype=np.float64), 10)
        return bool(closes[-1] > w[-1] and w[-1] > w[-5])

    def accumulation(self, i: int) -> int:
        """TS-20: +1 money flowing in (CMF > 0.05 and OBV rising), -1 out, 0 otherwise."""
        cm, ob = self.cmf20[i], self.obv_slope[i]
        if math.isnan(cm) or math.isnan(ob):
            return 0
        if cm > 0.05 and ob > 0:
            return 1
        if cm < -0.05 and ob < 0:
            return -1
        return 0

    def b(self, i: int) -> float:
        return 0.05 * float(self.atr[i])

    def uptrend(self, i: int) -> bool:
        return bool(self.c[i] > self.sma50[i] > self.sma200[i])

    def pivot_above(self, i: int, level: float, lookback: int = 120) -> float | None:
        """Nearest confirmed swing high (highest of 5 bars each side, so k <= i-5) above
        ``level``, or the 252-session high, whichever is closer."""
        best: float | None = None
        for kk in range(max(5, i - lookback), i - 4):
            hk = self.h[kk]
            if hk > level and hk == self.h[kk - 5:kk + 6].max() and (best is None or hk < best):
                best = float(hk)
        top = float(self.high252[i])
        if top > level and (best is None or top < best):
            best = top
        return best


@dataclass(frozen=True)
class Cand:
    trigger: float
    stop: float
    why: str
    entry: Literal["stop", "open"] = "stop"


TechFn = Callable[[Frame, int], Cand | None]


@dataclass(frozen=True)
class Technique:
    name: str
    style: Style
    kind: Literal["trend", "reversion"]
    fn: TechFn
    doc: str


@dataclass
class Idea:
    symbol: str
    asof: date
    technique: str
    style: Style
    side: Side
    entry: Literal["stop", "open"]
    trigger: float
    stop: float
    t1: float
    t2: float
    risk: float
    room_r: float | None
    score: int
    why: str
    confluence: list[str] = field(default_factory=list)
    factors: dict[str, int] = field(default_factory=dict)
    index: int = -1          # bar index in the series (evaluation)
    context: dict[str, Any] = field(default_factory=dict)   # inputs of the decision checks
    checks: list[dict[str, Any]] = field(default_factory=list)   # TS-17 (scanner.checks)
    verdict: str | None = None       # go | caution | blocked | watch | options (TS-18)
    shares: int | None = None        # TS-18 size from the risk rules
    risk_usd: float | None = None
    plan: str = ""

    def as_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in self.__dict__.items() if k != "index"}
        d["asof"] = self.asof.isoformat()
        return d


# --- techniques (long frame) ---------------------------------------------------------------

def _f(x: float) -> str:
    return f"{abs(x):.2f}"


def trend_pullback(x: Frame, i: int) -> Cand | None:
    if i < 12 or not (x.uptrend(i) and x.ema20[i] > x.ema50[i]):
        return None
    lo3 = float(x.l[i - 2:i + 1].min())
    a = float(x.atr[i])
    if not (lo3 - x.ema20[i] <= 0.5 * a and lo3 >= x.ema50[i] - 0.25 * a and x.c[i] > x.ema20[i]):
        return None
    if x.h[i - 2:i + 1].max() >= x.h[i - 9:i - 2].max():      # still at highs: not a pullback
        return None
    return Cand(float(x.h[i]) + x.b(i), lo3 - x.b(i),
                f"pullback to EMA20 {_f(x.ema20[i])} in an uptrend, closed back above it")


def breakout_20d(x: Frame, i: int) -> Cand | None:
    lvl = float(x.hh20[i])
    if math.isnan(lvl) or not x.c[i] > lvl or not x.v[i] >= VOLUME_MULT * x.vol50[i]:
        return None
    if x.rng[i] <= 0 or (x.c[i] - x.l[i]) / x.rng[i] < 0.7:
        return None
    return Cand(float(x.h[i]) + x.b(i), lvl - 0.5 * float(x.atr[i]),
                f"closed beyond the 20-day level {_f(lvl)} on {x.v[i] / x.vol50[i]:.1f}x volume")


def high_52w(x: Frame, i: int) -> Cand | None:
    if i < 252 or not x.uptrend(i):
        return None
    top, a = float(x.high252[i]), float(x.atr[i])
    if not (top - a <= x.c[i] < top) or x.h[i] >= top:
        return None
    lo10 = float(x.l[i - 9:i + 1].min())
    if float(x.h[i - 9:i + 1].max()) - lo10 > 2.5 * a:
        return None
    return Cand(top + x.b(i), lo10 - x.b(i),
                f"tight 10-day range just under the 52-week extreme {_f(top)}")


def bull_flag(x: Frame, i: int) -> Cand | None:
    if i < 30 or not x.c[i] > x.sma50[i]:
        return None
    for p in range(i - 3, i - 11, -1):                       # most recent pole top first
        top = float(x.h[p])
        if top < x.h[p - 8:i + 1].max():
            continue
        pole_lo = float(x.l[p - 8:p + 1].min())
        pole = top - pole_lo
        if pole < 3 * float(x.atr[p]):
            continue
        fl_lo, fl_hi = float(x.l[p + 1:i + 1].min()), float(x.h[p + 1:i + 1].max())
        if fl_lo < top - 0.5 * pole or fl_hi > top:
            continue
        lo_idx = p - 8 + int(np.argmin(x.l[p - 8:p + 1]))
        if x.v[p + 1:i + 1].mean() >= x.v[lo_idx:p + 1].mean():
            continue
        return Cand(fl_hi + x.b(i), fl_lo - x.b(i),
                    f"{i - p}-day flag after a {pole / float(x.atr[p]):.1f} ATR pole to "
                    f"{_f(top)}, volume drying up")
    return None


def squeeze(x: Frame, i: int) -> Cand | None:
    if i < 30 or not x.c[i] > x.sma50[i]:
        return None
    sq = (x.bb_up[i - 6:i] < x.kc_up[i - 6:i]) & (x.bb_lo[i - 6:i] > x.kc_lo[i - 6:i])
    if sq.sum() < 5:
        return None
    released = x.c[i] > x.bb_up[i] or (x.macd_hist[i] > 0 and x.macd_hist[i] > x.macd_hist[i - 1])
    if not released:
        return None
    return Cand(float(x.h[i]) + x.b(i), float(x.l[i - 4:i + 1].min()) - x.b(i),
                "Bollinger bands were inside Keltner for 5 of 6 sessions; momentum turning")


def inside_nr7(x: Frame, i: int) -> Cand | None:
    if not (x.uptrend(i) and x.inside[i] and x.nr7[i]):
        return None
    return Cand(float(x.h[i]) + x.b(i), float(x.l[i]) - x.b(i),
                "inside day that is also the narrowest range of 7 sessions, in trend")


def rsi2_reversion(x: Frame, i: int) -> Cand | None:
    if not (x.c[i] > x.sma200[i] and x.rsi2[i] < 10):
        return None
    rsi_real = x.rsi2[i] if x.side > 0 else 100 - x.rsi2[i]
    return Cand(float(x.c[i]), float(x.c[i]) - 2 * float(x.atr[i]),
                f"RSI(2) {rsi_real:.0f} stretched against the 200-day trend (Connors)",
                entry="open")


def macd_cross(x: Frame, i: int) -> Cand | None:
    if i < 40 or not x.uptrend(i):
        return None
    crossed = x.macd[i] > x.macd_sig[i] and x.macd[i - 1] <= x.macd_sig[i - 1]
    if not crossed or x.macd[i] > float(x.atr[i]):
        return None
    return Cand(float(x.h[i]) + x.b(i), float(x.l[i - 4:i + 1].min()) - x.b(i),
                "MACD crossed its signal line early in the move, trend intact")


def base_breakout(x: Frame, i: int) -> Cand | None:
    """TS-21: a 20-60 session base no deeper than 25%, ranges contracting, volume drying up,
    then a close above the base high on volume (a volatility contraction pattern)."""
    if i < 70 or not x.vol50[i] > 0:
        return None
    for n in (60, 50, 40, 30, 20):
        hi = float(x.h[i - n:i].max())
        lo = float(x.l[i - n:i].min())
        if hi <= 0 or (hi - lo) / abs(hi) > 0.25:
            continue
        tight = float(x.h[i - 10:i].max() - x.l[i - 10:i].min())
        if tight > 0.6 * (hi - lo):
            return None
        if float(x.v[i - 10:i].mean()) > 0.8 * float(x.vol50[i]):
            return None
        if not (x.c[i] > hi and x.v[i] >= VOLUME_MULT * x.vol50[i]):
            return None
        return Cand(float(x.h[i]) + x.b(i), float(x.l[i - 10:i + 1].min()) - x.b(i),
                    f"{n}-session base {(hi - lo) / abs(hi):.0%} deep, ranges contracting and "
                    f"volume drying up; closed above {_f(hi)} on "
                    f"{x.v[i] / x.vol50[i]:.1f}x volume")
    return None


def reversal_candle(x: Frame, i: int) -> Cand | None:
    if i < 25 or not x.c[i] > x.sma200[i]:
        return None
    names = [n for n, arr in x.reversal.items() if arr[i]]
    if not names:
        return None
    a = float(x.atr[i])
    supports = {"EMA20": x.ema20[i], "SMA50": x.sma50[i], "20-day extreme": x.ll20[i]}
    at = [n for n, lv in supports.items() if not math.isnan(lv) and abs(x.l[i] - lv) <= 0.5 * a]
    if not at:
        return None
    return Cand(float(x.h[i]) + x.b(i), float(x.l[i - 2:i + 1].min()) - x.b(i),
                f"{names[0]} at the {at[0]}")


TECHNIQUES: tuple[Technique, ...] = (
    Technique("TREND_PULLBACK", "swing", "trend", trend_pullback, "pullback to EMA20 in trend"),
    Technique("BREAKOUT_20D", "swing", "trend", breakout_20d, "20-day Donchian break on volume"),
    Technique("HIGH_52W", "swing", "trend", high_52w, "tight range under the 52-week high"),
    Technique("BULL_FLAG", "swing", "trend", bull_flag, "flag after a strong pole"),
    Technique("SQUEEZE", "swing", "trend", squeeze, "Bollinger-in-Keltner squeeze release"),
    Technique("INSIDE_NR7", "swing", "trend", inside_nr7, "inside day + NR7 in trend"),
    Technique("RSI2_REVERSION", "swing", "reversion", rsi2_reversion, "Connors RSI(2)"),
    Technique("MACD_CROSS", "swing", "trend", macd_cross, "early MACD cross in trend"),
    Technique("REVERSAL_CANDLE", "swing", "trend", reversal_candle, "reversal candle at support"),
    Technique("BASE_BREAKOUT", "swing", "trend", base_breakout,
              "volatility-contraction base breakout on volume"),
)


def technique_names() -> list[str]:
    names: list[str] = []
    for t in TECHNIQUES:
        names += [t.name] + ([SHORT_NAMES[t.name]] if t.name in SHORT_NAMES else [])
    return names


# --- scoring and ranking ---------------------------------------------------------------------

def _score(x: Frame, i: int, tech: Technique, cand: Cand, others: int, edge: float | None
           ) -> tuple[int, dict[str, int], float | None]:
    """TS-8, §4. The measured edge carries the most weight; the classic context factors are
    kept small because they did not predict outcomes on the stored history (§6)."""
    f: dict[str, int] = {}
    if edge is not None:                                  # shrunk avg R from scan-eval
        f["edge"] = int(max(-20, min(20, round(edge * 100))))
    if tech.kind == "trend":
        if x.uptrend(i):
            f["trend"] = 5
        elif x.c[i] < x.sma50[i] < x.sma200[i]:
            f["trend"] = -10
    if not math.isnan(x.spy_up[i]):
        f["market"] = int(5 * x.spy_up[i])
    if not math.isnan(x.rs63[i]) and x.rs63[i] > 0:
        f["rel_strength"] = int(min(5, x.rs63[i] / 2))
    if x.vol50[i] > 0 and x.v[i] >= VOLUME_MULT * x.vol50[i]:
        f["volume"] = 3
    if x.adx[i] >= ADX_OVERHEATED:
        f["overheated"] = -10
    if others:
        f["confluence"] = min(8, 4 * others)
    r = cand.trigger - cand.stop
    level = x.pivot_above(i, cand.trigger)
    room = None if level is None else (level - cand.trigger) / r
    if room is None or room >= 2:
        f["room"] = 5
    elif room < 1:
        f["room"] = -10
    return max(0, min(100, 50 + sum(f.values()))), f, room


def _context(x: Frame, i: int, t: Technique) -> dict[str, Any]:
    """Inputs of the decision checks, in the trade's direction."""
    mk = x.spy_up[i]
    rs = x.rs63[i]
    return {"uptrend": x.uptrend(i), "weekly_up": x.weekly_up(i),
            "market_up": None if math.isnan(mk) else bool(mk > 0),
            "rs63": None if math.isnan(rs) else round(float(rs), 2),
            "adx": None if math.isnan(x.adx[i]) else round(float(x.adx[i]), 1),
            "rvol": round(float(x.v[i] / x.vol50[i]), 2) if x.vol50[i] > 0 else None,
            "reversion": t.kind == "reversion",
            "accumulation": x.accumulation(i),                                  # TS-20
            "cmf20": None if math.isnan(x.cmf20[i]) else round(float(x.cmf20[i]), 3),
            "market_distribution_days": None if math.isnan(x.spy_dist[i])        # TS-22
            else int(x.spy_dist[i])}


def frames(s: Series, spy: Series | None) -> tuple[Frame, Frame]:
    return Frame(s, spy, +1), Frame(s, spy, -1)


def ideas_at(fr: tuple[Frame, Frame], i: int, disabled: frozenset[str] = frozenset(),
             dedup: bool = True, edges: dict[str, float] | None = None) -> list[Idea]:
    """All ideas for bar i of one symbol (TS-7..TS-9). ``disabled`` names are skipped;
    ``edges`` maps "TECHNIQUE:side" to its measured average R (TS-8)."""
    out: list[Idea] = []
    for x in fr:
        if i < MIN_HISTORY - 1 or i >= len(x.c) or math.isnan(x.atr[i]) or math.isnan(x.sma200[i]):
            continue
        side: Side = "long" if x.side > 0 else "short"
        hits: list[tuple[Technique, Cand, str]] = []
        for t in TECHNIQUES:
            name = SHORT_NAMES.get(t.name, t.name) if x.side < 0 else t.name
            if {name, t.name, f"{name}:{side}", f"{t.name}:{side}"} & disabled:
                continue
            cand = t.fn(x, i)
            if cand is None:
                continue
            r, a = cand.trigger - cand.stop, float(x.atr[i])
            if not (R_MIN_ATR * a <= r <= R_MAX_ATR * a):
                continue
            hits.append((t, cand, name))
        for t, cand, name in hits:
            same = [n for tt, _, n in hits if tt.style == t.style and n != name]
            edge = (edges or {}).get(f"{name}:{side}")
            score, factors, room = _score(x, i, t, cand, len(same), edge)
            m1, m2 = SWING_TARGETS
            trig, stop = x.px(cand.trigger), x.px(cand.stop)     # prices the owner sees
            r = round(abs(trig - stop), 2)
            out.append(Idea(
                symbol=x.symbol, asof=x.dates[i], technique=name, style=t.style, side=side,
                entry=cand.entry, trigger=trig, stop=stop,
                t1=round(trig + x.side * m1 * r, 2), t2=round(trig + x.side * m2 * r, 2),
                risk=r, room_r=None if room is None else round(room, 1), score=score,
                why=cand.why, confluence=same, factors=factors, index=i,
                context=_context(x, i, t)))
    return _dedup(out) if dedup else out


def _dedup(ideas: list[Idea]) -> list[Idea]:
    """TS-9: best technique per symbol/style/side, then the better side per symbol/style."""
    best: dict[tuple[str, str], Idea] = {}
    for idea in sorted(ideas, key=lambda d: (-d.score, d.technique)):
        key = (idea.symbol, idea.style)
        if key not in best:
            best[key] = idea
    return sorted(best.values(), key=lambda d: (-d.score, d.symbol))


def scan(series: dict[str, Series], spy: Series | None, asof: date | None = None,
         disabled: frozenset[str] = frozenset(), edges: dict[str, float] | None = None
         ) -> tuple[list[Idea], dict[str, str]]:
    """Ideas for every symbol on ``asof`` (default: each symbol's last bar). Returns the ideas
    ranked by score and a note per skipped symbol (TS-16)."""
    ideas: list[Idea] = []
    skipped: dict[str, str] = {}
    for sym, s in sorted(series.items()):
        if asof is not None:
            n = sum(1 for d in s.dates if d <= asof)
            s = s.upto(n)
        if len(s.c) < MIN_HISTORY:
            skipped[sym] = f"only {len(s.c)} sessions of history (need {MIN_HISTORY})"
            continue
        if asof is not None and s.dates[-1] != asof:
            skipped[sym] = f"no bar for {asof} (last {s.dates[-1]})"
            continue
        ideas += ideas_at(frames(s, spy), len(s.c) - 1, disabled, edges=edges)
    ideas.sort(key=lambda d: (-d.score, d.symbol))
    return ideas, skipped
