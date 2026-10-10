"""Black-Scholes (European, no dividends) for repricing and greeks (OP-7).

American single-stock options are priced as European: adequate for the P/L estimates in a
suggestion, which are labelled estimates. Time is in years, volatility annualised.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

RATE = 0.04          # risk-free rate used for estimates
MIN_T = 1 / (365 * 24 * 4)   # 15 minutes


def ncdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def npdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def _d1d2(s: float, k: float, t: float, vol: float, r: float) -> tuple[float, float]:
    t = max(t, MIN_T)
    vol = max(vol, 1e-4)
    d1 = (math.log(s / k) + (r + 0.5 * vol * vol) * t) / (vol * math.sqrt(t))
    return d1, d1 - vol * math.sqrt(t)


def price(kind: str, s: float, k: float, t: float, vol: float, r: float = RATE) -> float:
    """``kind`` "C" or "P". At or past expiry: intrinsic value."""
    if t <= 0:
        return max(0.0, s - k) if kind == "C" else max(0.0, k - s)
    d1, d2 = _d1d2(s, k, t, vol, r)
    if kind == "C":
        return s * ncdf(d1) - k * math.exp(-r * t) * ncdf(d2)
    return k * math.exp(-r * t) * ncdf(-d2) - s * ncdf(-d1)


@dataclass(frozen=True)
class Greeks:
    delta: float
    gamma: float
    theta: float      # per calendar day
    vega: float       # per 1 vol point


def greeks(kind: str, s: float, k: float, t: float, vol: float, r: float = RATE) -> Greeks:
    d1, d2 = _d1d2(s, k, t, vol, r)
    t = max(t, MIN_T)
    sq = math.sqrt(t)
    gamma = npdf(d1) / (s * max(vol, 1e-4) * sq)
    vega = s * npdf(d1) * sq / 100
    if kind == "C":
        delta = ncdf(d1)
        theta = -s * npdf(d1) * vol / (2 * sq) - r * k * math.exp(-r * t) * ncdf(d2)
    else:
        delta = ncdf(d1) - 1
        theta = -s * npdf(d1) * vol / (2 * sq) + r * k * math.exp(-r * t) * ncdf(-d2)
    return Greeks(delta, gamma, theta / 365, vega)


def implied_vol(kind: str, premium: float, s: float, k: float, t: float, r: float = RATE
                ) -> float | None:
    """Bisection on 1%..500%; None when the premium is below intrinsic or above the bound."""
    if premium <= 0 or t <= 0:
        return None
    lo, hi = 0.01, 5.0
    if premium < price(kind, s, k, t, lo, r) - 1e-6 or premium > price(kind, s, k, t, hi, r):
        return None
    for _ in range(80):
        mid = (lo + hi) / 2
        if price(kind, s, k, t, mid, r) > premium:
            hi = mid
        else:
            lo = mid
        if hi - lo < 1e-5:
            break
    return (lo + hi) / 2
