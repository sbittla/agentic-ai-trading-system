"""Indicators (DATA_SPEC.md §2, A4-2). Pure functions and small incremental classes (DV-7).

Intraday indicators reset at 9:30 ET; ATR(5-min) and the intraday EMAs carry across days.
The alert-bot indicators (RSI, MACD, Bollinger, Keltner, OBV, CMF) are computed for the
strategy and review agents to read; no v1 trigger depends on them.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field


class Ema:
    """EMA seeded with the SMA of the first ``n`` values."""

    def __init__(self, n: int) -> None:
        self.n = n
        self.alpha = 2.0 / (n + 1)
        self._seed: list[float] = []
        self.value: float | None = None

    def update(self, x: float) -> float | None:
        if self.value is None:
            self._seed.append(x)
            if len(self._seed) == self.n:
                self.value = sum(self._seed) / self.n
            return self.value
        self.value = self.alpha * x + (1 - self.alpha) * self.value
        return self.value


class WilderAtr:
    """14-period Wilder ATR: first value = mean TR of the first n bars, then smoothed."""

    def __init__(self, n: int = 14) -> None:
        self.n = n
        self.prev_close: float | None = None
        self._trs: list[float] = []
        self.value: float | None = None

    def update(self, h: float, l: float, c: float) -> float | None:   # noqa: E741
        tr = h - l if self.prev_close is None else max(h - l, abs(h - self.prev_close),
                                                       abs(l - self.prev_close))
        self.prev_close = c
        if self.value is None:
            self._trs.append(tr)
            if len(self._trs) == self.n:
                self.value = sum(self._trs) / self.n
            return self.value
        self.value = (self.value * (self.n - 1) + tr) / self.n
        return self.value


@dataclass
class SessionVwap:
    """Session VWAP from 9:30 on 1-min bars, typical price (h+l+c)/3."""

    pv: float = 0.0
    vol: float = 0.0

    def update(self, h: float, l: float, c: float, v: float) -> float | None:   # noqa: E741
        self.pv += (h + l + c) / 3 * v
        self.vol += v
        return self.value

    @property
    def value(self) -> float | None:
        return self.pv / self.vol if self.vol > 0 else None


@dataclass
class RvolProfile:
    """Time-of-day RVOL (MF-3, A2-5): cumulative volume since 9:30 divided by the average
    cumulative volume at the same minute over the last 20 sessions, same (IEX) feed."""

    lookback: int = 20
    sessions: list[list[float]] = field(default_factory=list)   # cumulative per minute 0..389

    def add_session(self, minute_volumes: Sequence[tuple[int, float]]) -> None:
        cum = [0.0] * 390
        by_min = {m: v for m, v in minute_volumes if 0 <= m < 390}
        total = 0.0
        for m in range(390):
            total += by_min.get(m, 0.0)
            cum[m] = total
        self.sessions.append(cum)
        if len(self.sessions) > self.lookback:
            self.sessions.pop(0)

    def avg_cum(self, minute: int) -> float | None:
        if not self.sessions or minute < 0:
            return None
        m = min(minute, 389)
        return sum(sess[m] for sess in self.sessions) / len(self.sessions)

    def rvol(self, minute: int, cum_volume: float) -> float | None:
        avg = self.avg_cum(minute)
        if avg is None or avg <= 0:
            return None
        return cum_volume / avg

    def fraction_done(self, minute: int) -> float | None:
        """Average fraction of the day's volume done by ``minute`` (projected volume)."""
        if not self.sessions:
            return None
        fracs = [sess[min(minute, 389)] / sess[389] for sess in self.sessions if sess[389] > 0]
        return sum(fracs) / len(fracs) if fracs else None


# --- batch (pure) versions ---------------------------------------------------------------

def ema_series(values: Sequence[float], n: int) -> list[float | None]:
    e = Ema(n)
    return [e.update(v) for v in values]


def atr_series(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
               n: int = 14) -> list[float | None]:
    a = WilderAtr(n)
    return [a.update(h, lo, c) for h, lo, c in zip(highs, lows, closes, strict=True)]


def vwap(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
         volumes: Sequence[float]) -> float | None:
    sv = SessionVwap()
    for h, lo, c, v in zip(highs, lows, closes, volumes, strict=True):
        sv.update(h, lo, c, v)
    return sv.value


def rsi(closes: Sequence[float], n: int = 14) -> float | None:
    if len(closes) <= n:
        return None
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    ag, al = gains / n, losses / n
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)


def macd(closes: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9
         ) -> tuple[float, float, float] | None:
    ef, es = ema_series(closes, fast), ema_series(closes, slow)
    line = [f - s for f, s in zip(ef, es, strict=True) if f is not None and s is not None]
    if len(line) < signal:
        return None
    sig = ema_series(line, signal)[-1]
    if sig is None:
        return None
    return line[-1], sig, line[-1] - sig


def bollinger(closes: Sequence[float], n: int = 20, k: float = 2.0
              ) -> tuple[float, float, float] | None:
    if len(closes) < n:
        return None
    window = closes[-n:]
    mid = sum(window) / n
    sd = math.sqrt(sum((x - mid) ** 2 for x in window) / n)
    return mid - k * sd, mid, mid + k * sd


def keltner(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
            n: int = 20, atr_n: int = 10, k: float = 2.0) -> tuple[float, float, float] | None:
    mid = ema_series(closes, n)[-1] if closes else None
    atr = atr_series(highs, lows, closes, atr_n)[-1] if closes else None
    if mid is None or atr is None:
        return None
    return mid - k * atr, mid, mid + k * atr


def obv(closes: Sequence[float], volumes: Sequence[float]) -> float:
    total = 0.0
    for i in range(1, len(closes)):
        if closes[i] > closes[i - 1]:
            total += volumes[i]
        elif closes[i] < closes[i - 1]:
            total -= volumes[i]
    return total


def cmf(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
        volumes: Sequence[float], n: int = 20) -> float | None:
    if len(closes) < n:
        return None
    mfv = vol = 0.0
    for h, lo, c, v in zip(highs[-n:], lows[-n:], closes[-n:], volumes[-n:], strict=True):
        rng = h - lo
        mfm = ((c - lo) - (h - c)) / rng if rng > 0 else 0.0
        mfv += mfm * v
        vol += v
    return mfv / vol if vol > 0 else None


def day_range_position(close: float, low: float, high: float) -> float:
    return (close - low) / (high - low) if high > low else 1.0


def gap_pct(today_open: float, prior_close: float) -> float:
    return (today_open - prior_close) / prior_close * 100 if prior_close else 0.0
