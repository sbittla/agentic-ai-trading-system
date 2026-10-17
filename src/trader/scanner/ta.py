"""Array indicators for the technical scanner (SCANNER_REQUIREMENTS TS-2).

Each function takes NumPy arrays and returns arrays of the same length, with NaN during
warm-up, following TA-Lib's default definitions so tests can check them against TA-Lib.
Every value at index i depends only on inputs up to i (TS-10).
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

Arr = NDArray[np.float64]


def _nan(n: int) -> Arr:
    return np.full(n, np.nan, dtype=np.float64)


def sma(x: Arr, n: int) -> Arr:
    out = _nan(len(x))
    if len(x) >= n:
        c = np.cumsum(np.insert(x, 0, 0.0))
        out[n - 1:] = (c[n:] - c[:-n]) / n
    return out


def ema(x: Arr, n: int) -> Arr:
    """EMA seeded with the SMA of the first n values (TA-Lib default). NaNs at the start of
    ``x`` (an input still warming up) are skipped."""
    out = _nan(len(x))
    valid = np.flatnonzero(~np.isnan(x))
    if len(valid) < n:
        return out
    start = int(valid[0])
    k = 2.0 / (n + 1)
    prev = float(np.mean(x[start:start + n]))
    out[start + n - 1] = prev
    for i in range(start + n, len(x)):
        prev = (float(x[i]) - prev) * k + prev
        out[i] = prev
    return out


def _wilder(x: Arr, n: int, start: int) -> Arr:
    """Wilder smoothing: mean of x[start:start+n], then (prev*(n-1)+x)/n."""
    out = _nan(len(x))
    if len(x) < start + n:
        return out
    prev = float(np.mean(x[start:start + n]))
    out[start + n - 1] = prev
    for i in range(start + n, len(x)):
        prev = (prev * (n - 1) + float(x[i])) / n
        out[i] = prev
    return out


def rsi(close: Arr, n: int = 14) -> Arr:
    out = _nan(len(close))
    if len(close) <= n:
        return out
    d = np.diff(close)
    gain, loss = np.clip(d, 0, None), np.clip(-d, 0, None)
    ag, al = _wilder(gain, n, 0), _wilder(loss, n, 0)
    with np.errstate(divide="ignore", invalid="ignore"):
        r = np.where(al == 0, 100.0, 100.0 - 100.0 / (1.0 + ag / al))
    r = np.where((ag == 0) & (al == 0), 0.0, r)
    out[1:] = np.where(np.isnan(ag), np.nan, r)
    return out


def true_range(high: Arr, low: Arr, close: Arr) -> Arr:
    tr = _nan(len(close))
    if len(close) > 1:
        pc = close[:-1]
        tr[1:] = np.maximum(high[1:] - low[1:],
                            np.maximum(np.abs(high[1:] - pc), np.abs(low[1:] - pc)))
    return tr


def atr(high: Arr, low: Arr, close: Arr, n: int = 14) -> Arr:
    """Wilder ATR as TA-Lib: TR from bar 1, first value = mean of TR[1..n]."""
    return _wilder(true_range(high, low, close), n, 1)


def macd(close: Arr, fast: int = 12, slow: int = 26, signal: int = 9
         ) -> tuple[Arr, Arr, Arr]:
    """TA-Lib MACD: the fast EMA is seeded on the window that ends where the slow EMA starts,
    so both start on the same bar; the signal line is an EMA of the MACD line."""
    n = len(close)
    line, sig, hist = _nan(n), _nan(n), _nan(n)
    first = slow - 1
    if n <= first:
        return line, sig, hist
    es = ema(close, slow)
    ef = _nan(n)
    kf = 2.0 / (fast + 1)
    prev = float(np.mean(close[first - fast + 1:first + 1]))
    ef[first] = prev
    for i in range(first + 1, n):
        prev = (float(close[i]) - prev) * kf + prev
        ef[i] = prev
    line = ef - es
    sig = ema(line, signal)
    line = np.where(np.isnan(sig), np.nan, line)
    return line, sig, line - sig


def bbands(close: Arr, n: int = 20, k: float = 2.0) -> tuple[Arr, Arr, Arr]:
    mid = sma(close, n)
    sd = _nan(len(close))
    if len(close) >= n:
        w = np.lib.stride_tricks.sliding_window_view(close, n)
        sd[n - 1:] = w.std(axis=1)
    return mid + k * sd, mid, mid - k * sd


def keltner(high: Arr, low: Arr, close: Arr, n: int = 20, k: float = 1.5
            ) -> tuple[Arr, Arr, Arr]:
    mid, a = ema(close, n), atr(high, low, close, n)
    return mid + k * a, mid, mid - k * a


def adx(high: Arr, low: Arr, close: Arr, n: int = 14) -> tuple[Arr, Arr, Arr]:
    """Wilder ADX, +DI, −DI as TA-Lib (DI from bar n, ADX from bar 2n−1)."""
    size = len(close)
    pdi, mdi, adx_ = _nan(size), _nan(size), _nan(size)
    if size < 2 * n:
        return adx_, pdi, mdi
    up = high[1:] - high[:-1]
    dn = low[:-1] - low[1:]
    pdm = np.where((up > dn) & (up > 0), up, 0.0)
    mdm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = true_range(high, low, close)[1:]
    # TA-Lib: first smoothed value = sum of the first n-1 values, then s - s/n + x
    sp, sm, st = float(pdm[:n - 1].sum()), float(mdm[:n - 1].sum()), float(tr[:n - 1].sum())
    dx = _nan(size)
    for j in range(n - 1, size - 1):
        sp, sm, st = sp - sp / n + pdm[j], sm - sm / n + mdm[j], st - st / n + tr[j]
        i = j + 1
        if st > 0:
            pdi[i], mdi[i] = 100 * sp / st, 100 * sm / st
            s = pdi[i] + mdi[i]
            dx[i] = 100 * abs(pdi[i] - mdi[i]) / s if s > 0 else 0.0
    adx_ = _wilder(dx, n, n)
    return adx_, pdi, mdi


def stoch(high: Arr, low: Arr, close: Arr, k_n: int = 14, slow_k: int = 3, slow_d: int = 3
          ) -> tuple[Arr, Arr]:
    """Slow stochastic as TA-Lib STOCH defaults (SMA smoothing)."""
    size = len(close)
    fk = _nan(size)
    if size >= k_n:
        hh = np.lib.stride_tricks.sliding_window_view(high, k_n).max(axis=1)
        ll = np.lib.stride_tricks.sliding_window_view(low, k_n).min(axis=1)
        rng = hh - ll
        with np.errstate(divide="ignore", invalid="ignore"):
            fk[k_n - 1:] = np.where(rng > 0, 100 * (close[k_n - 1:] - ll) / rng, 0.0)
    k = _sma_skip(fk, slow_k)
    d = _sma_skip(k, slow_d)
    return np.where(np.isnan(d), np.nan, k), d


def _sma_skip(x: Arr, n: int) -> Arr:
    out = _nan(len(x))
    valid = np.flatnonzero(~np.isnan(x))
    if len(valid):
        s = int(valid[0])
        out[s:] = sma(x[s:], n)
    return out


def obv(close: Arr, volume: Arr) -> Arr:
    """TA-Lib OBV: starts at volume[0]."""
    sign = np.sign(np.diff(close, prepend=close[0]))
    sign[0] = 1.0
    return np.cumsum(sign * volume)


def ad_line(high: Arr, low: Arr, close: Arr, volume: Arr) -> Arr:
    """TA-Lib AD (Chaikin Accumulation/Distribution line); flat bars add nothing."""
    rng = high - low
    mfm = np.where(rng > 0, ((close - low) - (high - close)) / np.where(rng > 0, rng, 1.0), 0.0)
    return np.cumsum(mfm * volume)


def cmf(high: Arr, low: Arr, close: Arr, volume: Arr, n: int = 20) -> Arr:
    """Chaikin Money Flow: sum of money-flow volume / sum of volume over ``n`` bars (TS-20)."""
    rng = high - low
    mfm = np.where(rng > 0, ((close - low) - (high - close)) / np.where(rng > 0, rng, 1.0), 0.0)
    out = _nan(len(close))
    mfv = mfm * volume
    for i in range(n - 1, len(close)):
        vs = float(volume[i - n + 1:i + 1].sum())
        out[i] = float(mfv[i - n + 1:i + 1].sum()) / vs if vs > 0 else 0.0
    return out


def slope(x: Arr, n: int) -> Arr:
    """Least-squares slope of the last ``n`` values, per bar (NaN until n values)."""
    out = _nan(len(x))
    t = np.arange(n, dtype=np.float64)
    tc = t - t.mean()
    den = float((tc * tc).sum())
    for i in range(n - 1, len(x)):
        w = x[i - n + 1:i + 1]
        if not np.isnan(w).any():
            out[i] = float((tc * (w - w.mean())).sum()) / den
    return out


def roc(x: Arr, n: int) -> Arr:
    out = _nan(len(x))
    if len(x) > n:
        out[n:] = (x[n:] / x[:-n] - 1.0) * 100.0
    return out


def rolling_max(x: Arr, n: int) -> Arr:
    """Max of x[i-n+1..i] (includes today). Shift by one for 'prior n sessions'."""
    out = _nan(len(x))
    if len(x) >= n:
        out[n - 1:] = np.lib.stride_tricks.sliding_window_view(x, n).max(axis=1)
    return out


def rolling_min(x: Arr, n: int) -> Arr:
    out = _nan(len(x))
    if len(x) >= n:
        out[n - 1:] = np.lib.stride_tricks.sliding_window_view(x, n).min(axis=1)
    return out


def shift(x: Arr, k: int = 1) -> Arr:
    out = _nan(len(x))
    if k < len(x):
        out[k:] = x[:-k]
    return out
