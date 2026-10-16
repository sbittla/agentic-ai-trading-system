"""Candlestick and range patterns (SCANNER_REQUIREMENTS TS-3).

Each function returns an int array: +100 bullish, -100 bearish, 0 none (the TA-Lib
convention). ``engulfing`` follows TA-Lib's CDLENGULFING rule exactly (±80 when an open or
close only equals the other body's edge). The others use the geometric rules written next
to them; whether a pattern matters (at support, in a trend) is decided by the techniques.
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

from trader.scanner.ta import Arr

Sig = NDArray[np.int64]


def _prev(x: Arr, k: int = 1) -> Arr:
    out = np.full(len(x), np.nan)
    if k < len(x):
        out[k:] = x[:-k]
    return out


def _color(o: Arr, c: Arr) -> NDArray[np.int64]:
    """TA-Lib candle colour: +1 white (close >= open), -1 black."""
    return np.where(c >= o, 1, -1).astype(np.int64)


def engulfing(o: Arr, h: Arr, l: Arr, c: Arr) -> Sig:  # noqa: E741
    out = np.zeros(len(c), dtype=np.int64)
    if len(c) < 2:
        return out
    col, pcol = _color(o, c), np.roll(_color(o, c), 1)
    po, pc = _prev(o), _prev(c)
    with np.errstate(invalid="ignore"):
        bull = (col == 1) & (pcol == -1) & (((c >= po) & (o < pc)) | ((c > po) & (o <= pc)))
        bear = (col == -1) & (pcol == 1) & (((o >= pc) & (c < po)) | ((o > pc) & (c <= po)))
        strength = np.where((o != pc) & (c != po), 100, 80)
    hit = bull | bear
    hit[0] = False
    out[hit] = (col * strength)[hit]
    return out


def _parts(o: Arr, h: Arr, l: Arr, c: Arr) -> tuple[Arr, Arr, Arr, Arr]:  # noqa: E741
    body = np.abs(c - o)
    rng = h - l
    upper = h - np.maximum(o, c)
    lower = np.minimum(o, c) - l
    return body, rng, upper, lower


def _avg(x: Arr, period: int, factor: float, back: int = 0) -> Arr:
    """TA-Lib TA_CANDLEAVERAGE: factor x mean of x over the ``period`` bars before bar
    i - back (or x itself at i - back when period is 0)."""
    if period == 0:
        return factor * _prev(x, back) if back else factor * x
    m = np.full(len(x), np.nan)
    if len(x) > period:
        cs = np.cumsum(np.insert(x, 0, 0.0))
        m[period:] = (cs[period:-1] - cs[:-period - 1]) / period
    return factor * (_prev(m, back) if back else m)


class _Candles:
    """TA-Lib default candle settings: averages of body, range or shadows over prior bars."""

    def __init__(self, o: Arr, h: Arr, l: Arr, c: Arr) -> None:  # noqa: E741
        self.body, self.rng, self.upper, self.lower = _parts(o, h, l, c)
        self.shadows = self.upper + self.lower

    def body_long(self, back: int = 0) -> Arr:
        return _avg(self.body, 10, 1.0, back)

    def body_short(self, back: int = 0) -> Arr:
        return _avg(self.body, 10, 1.0, back)

    def body_doji(self) -> Arr:
        return _avg(self.rng, 10, 0.1)

    def shadow_long(self) -> Arr:
        return self.body * 1.0

    def shadow_very_short(self) -> Arr:
        return _avg(self.rng, 10, 0.1)

    def near(self, back: int = 0) -> Arr:
        return _avg(self.rng, 5, 0.2, back)


def _done(ok: NDArray[np.bool_], value: int, lookback: int) -> Sig:
    ok = np.where(np.isnan(ok.astype(float)), False, ok)
    ok[:lookback] = False
    return np.where(ok, value, 0).astype(np.int64)


def doji(o: Arr, h: Arr, l: Arr, c: Arr) -> Sig:  # noqa: E741
    """TA-Lib CDLDOJI: body <= 10% of the average range of the prior 10 bars (+100)."""
    k = _Candles(o, h, l, c)
    with np.errstate(invalid="ignore"):
        return _done(k.body <= k.body_doji(), 100, 10)


def hammer(o: Arr, h: Arr, l: Arr, c: Arr) -> Sig:  # noqa: E741
    """TA-Lib CDLHAMMER: short body, lower shadow longer than the body, very short upper
    shadow, body near or below the prior bar's low (+100)."""
    k = _Candles(o, h, l, c)
    with np.errstate(invalid="ignore"):
        ok = (
            (k.body < k.body_short())
            & (k.lower > k.shadow_long())
            & (k.upper < k.shadow_very_short())
            & (np.minimum(o, c) <= _prev(l) + k.near(1))
        )
    return _done(ok, 100, 11)


def shooting_star(o: Arr, h: Arr, l: Arr, c: Arr) -> Sig:  # noqa: E741
    """TA-Lib CDLSHOOTINGSTAR: short body, upper shadow longer than the body, very short
    lower shadow, body gapping above the prior body (-100)."""
    k = _Candles(o, h, l, c)
    with np.errstate(invalid="ignore"):
        ok = (
            (k.body < k.body_short())
            & (k.upper > k.shadow_long())
            & (k.lower < k.shadow_very_short())
            & (np.minimum(o, c) > np.maximum(_prev(o), _prev(c)))
        )
    return _done(ok, -100, 11)


def morning_evening_star(o: Arr, h: Arr, l: Arr, c: Arr, penetration: float = 0.3  # noqa: E741
                         ) -> Sig:
    """TA-Lib CDLMORNINGSTAR / CDLEVENINGSTAR: long bar, short bar whose body gaps away,
    then an opposite bar closing at least 30% into the first body. +100 / -100."""
    k = _Candles(o, h, l, c)
    o1, c1, b1 = _prev(o, 2), _prev(c, 2), _prev(k.body, 2)
    o2, c2, b2 = _prev(o), _prev(c), _prev(k.body)
    with np.errstate(invalid="ignore"):
        first_long = b1 > k.body_long(2)
        second_short = b2 <= k.body_short(1)
        third = k.body > k.body_short()
        morning = (
            first_long
            & (c1 < o1)
            & second_short
            & (np.maximum(o2, c2) < np.minimum(o1, c1))
            & third
            & (c >= o)
            & (c > c1 + b1 * penetration)
        )
        evening = (
            first_long
            & (c1 >= o1)
            & second_short
            & (np.minimum(o2, c2) > np.maximum(o1, c1))
            & third
            & (c < o)
            & (c < c1 - b1 * penetration)
        )
    return _done(morning, 100, 12) + _done(evening, -100, 12)


def piercing_darkcloud(o: Arr, h: Arr, l: Arr, c: Arr) -> Sig:  # noqa: E741
    """TA-Lib CDLPIERCING (+100): long black bar, long white bar opening below its low and
    closing above its body midpoint but below its open. CDLDARKCLOUDCOVER (-100): long
    white bar, black bar opening above its high and closing below its midpoint (50%)."""
    k = _Candles(o, h, l, c)
    po, ph, pl, pc, pb = _prev(o), _prev(h), _prev(l), _prev(c), _prev(k.body)
    with np.errstate(invalid="ignore"):
        prior_long = pb > k.body_long(1)
        pierce = (
            (pc < po)
            & prior_long
            & (c >= o)
            & (k.body > k.body_long())
            & (o < pl)
            & (c < po)
            & (c > pc + pb * 0.5)
        )
        cloud = (
            (pc >= po)
            & prior_long
            & (c < o)
            & (o > ph)
            & (c > po)
            & (c < pc - pb * 0.5)
        )
    return _done(pierce, 100, 11) + _done(cloud, -100, 11)


def inside_bar(h: Arr, l: Arr) -> NDArray[np.bool_]:  # noqa: E741
    """High <= prior high, low >= prior low, and a narrower range."""
    ph, pl = _prev(h), _prev(l)
    with np.errstate(invalid="ignore"):
        ok = (h <= ph) & (l >= pl) & ((h - l) < (ph - pl))
    ok[:1] = False
    return ok


def nr(h: Arr, l: Arr, n: int = 7) -> NDArray[np.bool_]:  # noqa: E741
    """Today's range is strictly the narrowest of the last n sessions (NR7 for n=7)."""
    rng = h - l
    ok = np.zeros(len(rng), dtype=bool)
    if len(rng) >= n:
        prior_min = np.lib.stride_tricks.sliding_window_view(rng[:-1], n - 1).min(axis=1)
        ok[n - 1:] = rng[n - 1:] < prior_min
    return ok


def bullish_reversal(o: Arr, h: Arr, l: Arr, c: Arr) -> dict[str, NDArray[np.bool_]]:  # noqa: E741
    """Named bullish reversal candles (for REVERSAL_CANDLE)."""
    return {"bullish engulfing": engulfing(o, h, l, c) == 100,
            "hammer": hammer(o, h, l, c) > 0,
            "morning star": morning_evening_star(o, h, l, c) > 0,
            "piercing line": piercing_darkcloud(o, h, l, c) > 0}


def bearish_reversal(o: Arr, h: Arr, l: Arr, c: Arr) -> dict[str, NDArray[np.bool_]]:  # noqa: E741
    return {"bearish engulfing": engulfing(o, h, l, c) == -100,
            "shooting star": shooting_star(o, h, l, c) < 0,
            "evening star": morning_evening_star(o, h, l, c) < 0,
            "dark cloud cover": piercing_darkcloud(o, h, l, c) < 0}
