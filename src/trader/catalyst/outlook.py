"""Horizon outlook: P(up), typical move and a 10-90% range per horizon (NC-17..NC-23).

Method (explainable, gradeable, honest about thin data):

- **Width:** daily volatility from the options' implied move (A13) blended with EWMA realized
  volatility, times sqrt(h); earnings inside the horizon add their own variance (NC-18).
- **Tilt:** a pooled event study. Every past story gives forward returns at each horizon in
  volatility units, x = return / (sigma x sqrt(h)), as excess over beta x SPY and raw. The
  mean x is shrunk level by level (all -> direction -> type -> strength bucket) with weight
  n / (n + k), so a thin bucket shows almost no tilt instead of a confident guess (NC-19).
- **Shape:** percentiles of the past residuals at the finest level with >= 30 events, so fat
  tails from real events show (NC-21); a normal curve only when history is too short.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time
from statistics import NormalDist
from typing import Any

import numpy as np

from trader.catalyst.strength import Reaction
from trader.scanner.techniques import Series
from trader.timeutil import ET

Z = NormalDist()
BUCKETS = ((80, "major"), (60, "strong"), (40, "moderate"), (0, "weak"))
ADJUSTMENTS = ("pead", "priced_in", "crowding", "trend")


def bucket(strength: float) -> str:
    return next(name for floor, name in BUCKETS if strength >= floor)


# --- prices -------------------------------------------------------------------------------

def day0_index(series: Series, published_at: datetime) -> int | None:
    """The session a story belongs to: the same day if it came out before the 4 PM close on a
    trading day, else the next session (after-hours news trades the next day)."""
    local = published_at.astimezone(ET)
    d = local.date()
    after_close = local.time() >= time(16, 0)
    for i, sd in enumerate(series.dates):
        if sd > d or (sd == d and not after_close):
            return i
    return None


def ewma_sigma(c: Sequence[float] | np.ndarray, end: int, lam: float = 0.94,
               min_n: int = 20) -> float | None:
    """EWMA (RiskMetrics, lambda 0.94) daily volatility of log returns up to ``end``
    (exclusive), so the event day never inflates its own yardstick."""
    if end < min_n + 1:
        return None
    r = np.diff(np.log(np.asarray(c[max(0, end - 250):end], dtype=np.float64)))
    var = float(np.var(r[:20])) if len(r) >= 20 else float(np.var(r))
    for x in r[20:]:
        var = lam * var + (1 - lam) * float(x) * float(x)
    return math.sqrt(var) if var > 0 else None


def _spy_ret(spy: Series | None, d0: date, d1: date) -> float:
    if spy is None:
        return 0.0
    try:
        i, j = spy.dates.index(d0), spy.dates.index(d1)
    except ValueError:
        return 0.0
    return float(spy.c[j] / spy.c[i] - 1)


def beta_at(series: Series, spy: Series | None, end: int, n: int = 120) -> float:
    if spy is None or end < 30:
        return 1.0
    pos = {d: k for k, d in enumerate(spy.dates)}
    pairs = [(float(series.c[k] / series.c[k - 1] - 1),
              float(spy.c[pos[series.dates[k]]] / spy.c[pos[series.dates[k]] - 1] - 1))
             for k in range(max(1, end - n), end)
             if series.dates[k] in pos and pos[series.dates[k]] >= 1]
    if len(pairs) < 30:
        return 1.0
    a = np.asarray(pairs)
    var = float(np.var(a[:, 1]))
    return float(np.cov(a[:, 0], a[:, 1], bias=True)[0, 1] / var) if var > 0 else 1.0


def reaction(series: Series, spy: Series | None, i: int, beta: float) -> Reaction | None:
    """Day-0 reaction (NC-8 confirmation, NC-10 priced-in)."""
    if i < 1:
        return None
    sig = ewma_sigma(series.c, i)
    if sig is None:
        return None
    ret = float(series.c[i] / series.c[i - 1] - 1)
    ab = ret - beta * _spy_ret(spy, series.dates[i - 1], series.dates[i])
    v50 = float(np.mean(series.v[max(0, i - 50):i])) if i >= 10 else 0.0
    tr = [max(series.h[k] - series.l[k], abs(series.h[k] - series.c[k - 1]),
              abs(series.l[k] - series.c[k - 1])) for k in range(max(1, i - 14), i)]
    atr = float(np.mean(tr)) if tr else 0.0
    rng = float(series.h[i] - series.l[i])
    return Reaction(ret=ret, abnormal=ab, sigma=sig,
                    rvol=float(series.v[i] / v50) if v50 > 0 else None,
                    atr_move=abs(float(series.c[i] - series.c[i - 1])) / atr if atr > 0 else None,
                    close_loc=float((series.c[i] - series.l[i]) / rng) if rng > 0 else None)


# --- event observations ------------------------------------------------------------------

@dataclass
class EventObs:
    """One past story and what followed, in volatility units (no look-ahead: ``end[h]`` is
    the session the h-day outcome was known)."""

    ticker: str
    d0: date
    ctype: str
    direction: str
    strength: float
    sigma: float
    x_abn: dict[int, float] = field(default_factory=dict)
    x_raw: dict[int, float] = field(default_factory=dict)
    end: dict[int, date] = field(default_factory=dict)
    flags: dict[str, bool] = field(default_factory=dict)   # pead, priced_in, crowding, trend

    def key(self, level: int) -> tuple[str, ...]:
        return (("*",), (self.direction,), (self.direction, self.ctype),
                (self.direction, self.ctype, bucket(self.strength)))[level]

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["d0"] = self.d0.isoformat()
        d["end"] = {str(h): v.isoformat() for h, v in self.end.items()}
        d["x_abn"] = {str(h): v for h, v in self.x_abn.items()}
        d["x_raw"] = {str(h): v for h, v in self.x_raw.items()}
        return d

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> EventObs:
        return cls(ticker=d["ticker"], d0=date.fromisoformat(d["d0"]), ctype=d["ctype"],
                   direction=d["direction"], strength=float(d["strength"]),
                   sigma=float(d["sigma"]),
                   x_abn={int(h): float(v) for h, v in d["x_abn"].items()},
                   x_raw={int(h): float(v) for h, v in d["x_raw"].items()},
                   end={int(h): date.fromisoformat(v) for h, v in d["end"].items()},
                   flags={k: bool(v) for k, v in d.get("flags", {}).items()})


def observe(series: Series, spy: Series | None, i0: int, beta: float, horizons: Sequence[int]
            ) -> tuple[dict[int, float], dict[int, float], dict[int, date], float] | None:
    """Forward outcomes from the close of day 0 (when the outlook is made)."""
    sig = ewma_sigma(series.c, i0)
    if sig is None or sig <= 0:
        return None
    xa: dict[int, float] = {}
    xr: dict[int, float] = {}
    end: dict[int, date] = {}
    for h in horizons:
        j = i0 + h
        if j >= len(series.c):
            continue
        ret = float(series.c[j] / series.c[i0] - 1)
        mk = _spy_ret(spy, series.dates[i0], series.dates[j])
        scale = sig * math.sqrt(h)
        xa[h], xr[h], end[h] = (ret - beta * mk) / scale, ret / scale, series.dates[j]
    return xa, xr, end, sig


# --- the view for one horizon ---------------------------------------------------------------

@dataclass
class HorizonView:
    h: int
    p_up: float
    median_pct: float
    p10_pct: float
    p90_pct: float
    ex_median_pct: float
    ex_p10_pct: float
    ex_p90_pct: float
    sigma_pct: float
    n: int
    confidence: str
    basis: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class Pool:
    """Past events indexed by horizon, level and key (the walk-forward adds to it as time
    passes, so lookups must stay cheap)."""

    def __init__(self, obs: Sequence[EventObs] = ()) -> None:
        self.n = 0
        self._idx: dict[tuple[Any, ...], list[float]] = {}
        self._arr: dict[tuple[Any, ...], np.ndarray] = {}
        for o in obs:
            self.add(o)

    def add(self, o: EventObs, horizons: Sequence[int] | None = None) -> None:
        """Add ``o``'s outcomes for ``horizons`` (default: every horizon it has)."""
        self.n += 1
        for h in (horizons if horizons is not None else list(o.x_abn)):
            if h not in o.x_abn:
                continue
            for raw, x in ((True, o.x_raw[h]), (False, o.x_abn[h])):
                for level in range(4):
                    self._push((h, raw, level, o.key(level)), x)
                for name, val in o.flags.items():
                    self._push((h, raw, 1, o.key(1), name, val), x)

    def _push(self, k: tuple[Any, ...], x: float) -> None:
        self._idx.setdefault(k, []).append(x)
        self._arr.pop(k, None)

    def samples(self, h: int, key: tuple[str, ...], level: int, raw: bool,
                flag: tuple[str, bool] | None = None) -> np.ndarray:
        k: tuple[Any, ...] = (h, raw, level, key) if flag is None else (h, raw, level, key,
                                                                         *flag)
        arr = self._arr.get(k)
        if arr is None:
            arr = np.asarray(self._idx.get(k, ()), dtype=np.float64)
            self._arr[k] = arr
        return arr


def _shrunk_mean(pool: Pool, h: int, keys: Sequence[tuple[str, ...]], k: float, raw: bool
                 ) -> tuple[float, int]:
    """Empirical-Bayes cascade: start from 0, move toward each finer level's mean by
    n / (n + k). Returns (mean in vol units, n at the finest level used)."""
    m, n_used = 0.0, 0
    for level, key in enumerate(keys):
        x = pool.samples(h, key, level, raw)
        n = len(x)
        if n == 0:
            break
        m = m + (float(np.mean(x)) - m) * n / (n + k)
        n_used = n
    return m, n_used


def _residuals(pool: Pool, h: int, keys: Sequence[tuple[str, ...]], raw: bool
               ) -> tuple[np.ndarray | None, str]:
    names = ("all events", "same direction", "same type", "same type and strength")
    for level in range(len(keys) - 1, -1, -1):
        x = pool.samples(h, keys[level], level, raw)
        if len(x) >= 30:
            return x - float(np.mean(x)), names[level]
    return None, "a normal curve (too few events)"


def _adjust(pool: Pool, h: int, o: EventObs, names: Sequence[str], k: float, raw: bool
            ) -> float:
    """NC-20: each enabled adjustment shifts the mean by the shrunk difference between past
    events that shared this flag and all events of the same direction."""
    delta = 0.0
    key = o.key(1)
    base = pool.samples(h, key, 1, raw)
    if len(base) == 0:
        return 0.0
    for a in names:
        if a not in o.flags:
            continue
        sub = pool.samples(h, key, 1, raw, (a, o.flags[a]))
        if len(sub):
            delta += (float(np.mean(sub)) - float(np.mean(base))) * len(sub) / (len(sub) + k)
    return delta


def horizon_view(pool: Pool, o: EventObs, h: int, sigma_daily: float, *, k: float = 30,
                 adjustments: Sequence[str] = (), event_var: float = 0.0,
                 calibrated: bool = False) -> HorizonView:
    """NC-17: the view for horizon ``h`` for event ``o`` (its flags and keys), with daily
    volatility ``sigma_daily`` (fraction). ``event_var`` adds variance (fraction^2) for an
    earnings report or macro event inside the horizon (NC-18)."""
    keys = [o.key(lv) for lv in range(4)]
    scale = math.sqrt(sigma_daily ** 2 * h + event_var)
    out: dict[str, Any] = {}
    n_used = 0
    basis = "normal"
    for raw in (True, False):
        m, n = _shrunk_mean(pool, h, keys, k, raw)
        m += _adjust(pool, h, o, adjustments, k, raw)
        res, where = _residuals(pool, h, keys, raw)
        for a in adjustments:   # an adjustment that applies also brings its own shape
            if a in o.flags:
                sub = pool.samples(h, keys[1], 1, raw, (a, o.flags[a]))
                if len(sub) >= 30:
                    res, where = sub - float(np.mean(sub)), f"events with {a}={o.flags[a]}"
                    break
        if res is not None:
            dist = m + res
            p_up = float(np.mean(dist > 0))
            q10, q50, q90 = (float(np.quantile(dist, q)) for q in (0.1, 0.5, 0.9))
        else:
            p_up = 1 - Z.cdf(-m)
            q10, q50, q90 = m + Z.inv_cdf(0.1), m, m + Z.inv_cdf(0.9)
        tag = "" if raw else "ex_"
        out[f"{tag}median_pct"] = round(q50 * scale * 100, 2)
        out[f"{tag}p10_pct"] = round(q10 * scale * 100, 2)
        out[f"{tag}p90_pct"] = round(q90 * scale * 100, 2)
        if raw:
            out["p_up"] = round(p_up, 3)
            n_used, basis = n, where
    conf = "HIGH" if n_used >= 100 and calibrated else "MEDIUM" if n_used >= 30 else "LOW"
    return HorizonView(h=h, p_up=out["p_up"], median_pct=out["median_pct"],
                       p10_pct=out["p10_pct"], p90_pct=out["p90_pct"],
                       ex_median_pct=out["ex_median_pct"], ex_p10_pct=out["ex_p10_pct"],
                       ex_p90_pct=out["ex_p90_pct"], sigma_pct=round(scale * 100, 2),
                       n=n_used, confidence=conf,
                       basis=f"{n_used} similar events, shape from {basis}")


def blended_sigma(ewma: float | None, iv_30d: float | None, iv_weight: float) -> float | None:
    """NC-18: daily sigma from implied vol (annualised, fraction) and EWMA realized vol."""
    iv_d = iv_30d / math.sqrt(252) if iv_30d and iv_30d > 0 else None
    if ewma is None:
        return iv_d
    if iv_d is None:
        return ewma
    return iv_weight * iv_d + (1 - iv_weight) * ewma


def scenarios(ticker: str, direction: str, day0_high: float, day0_low: float,
              views: Sequence[HorizonView]) -> list[str]:
    """NC-22: base, bull and bear cases in words, each with the level that confirms it."""
    if not views:
        return []
    v = max(views, key=lambda x: abs(x.median_pct) / max(x.sigma_pct, 1e-9))
    lead = (f"Base case: {ticker} {v.median_pct:+.1f}% over {v.h} session"
            f"{'s' if v.h > 1 else ''} (likely {v.p10_pct:+.1f}% to {v.p90_pct:+.1f}%).")
    up = f"Bull case: a close above the day-0 high {day0_high:.2f} keeps buyers in control."
    down = f"Bear case: a close below the day-0 low {day0_low:.2f} says the news has faded."
    if direction == "negative":
        up, down = (f"Bear case holds while price stays below the day-0 high {day0_high:.2f}.",
                    f"A close back above {day0_high:.2f} would say the selling is done.")
    return [lead, up, down]
