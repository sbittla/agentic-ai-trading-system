"""FC-1..FC-5 as pure functions of daily bars (no look-ahead: index ``i`` uses bars <= i)."""

from __future__ import annotations

import math
from collections.abc import Collection, Mapping, Sequence
from datetime import date
from typing import Any

import numpy as np

from trader.scanner import ta
from trader.scanner.techniques import Series

Z80 = 1.2816
ESTIMATORS = ("ewma", "parkinson", "har")


# --- FC-1 / FC-2 volatility ------------------------------------------------------------------

def _logret(s: Series) -> np.ndarray:
    return np.diff(np.log(np.asarray(s.c, dtype=np.float64)))


def parkinson_var(s: Series) -> np.ndarray:
    """Daily Parkinson variance from the high-low range: (ln H/L)^2 / (4 ln 2)."""
    hl = np.log(np.asarray(s.h, dtype=np.float64) / np.asarray(s.l, dtype=np.float64))
    return hl * hl / (4 * math.log(2))


def ewma_var(r: np.ndarray, end: int, lam: float = 0.94) -> float | None:
    """Variance forecast for return ``end`` from returns before it."""
    if end < 21:
        return None
    var = float(np.var(r[max(0, end - 250):max(0, end - 250) + 20]))
    for x in r[max(0, end - 250) + 20:end]:
        var = lam * var + (1 - lam) * float(x) * float(x)
    return var


def _har_x(pv: np.ndarray, t: int) -> list[float] | None:
    if t < 22:
        return None
    return [1.0, float(pv[t]), float(pv[t - 4:t + 1].mean()), float(pv[t - 21:t + 1].mean())]


def har_fit(pv: np.ndarray, end: int, window: int = 500) -> np.ndarray | None:
    """HAR-RV (Corsi 2009) on Parkinson variance: next day's variance on today's, the week's
    and the month's averages. Least squares on bars before ``end`` only."""
    rows, ys = [], []
    for t in range(max(22, end - window), end - 1):
        x = _har_x(pv, t)
        if x is not None:
            rows.append(x)
            ys.append(float(pv[t + 1]))
    if len(rows) < 60:
        return None
    beta, *_ = np.linalg.lstsq(np.asarray(rows), np.asarray(ys), rcond=None)
    return np.asarray(beta, dtype=np.float64)


def forecast_var(s: Series, i: int, method: str, beta: np.ndarray | None = None
                 ) -> float | None:
    """One-session-ahead variance forecast made at the close of bar ``i``."""
    if method == "ewma":
        return ewma_var(_logret(s), i)        # returns index i-1 is the move into bar i
    pv = parkinson_var(s)
    if method == "parkinson":
        return float(pv[max(0, i - 19):i + 1].mean()) if i >= 20 else None
    if method == "har":
        b = beta if beta is not None else har_fit(pv, i + 1)
        x = _har_x(pv, i)
        if b is None or x is None:
            return None
        return max(float(np.dot(b, x)), 1e-8)
    raise ValueError(method)


def qlike(var_hat: float, r: float) -> float:
    """QLIKE loss (Patton 2011): robust for comparing variance forecasts with noisy proxies."""
    v = max(var_hat, 1e-10)
    return math.log(v) + r * r / v


def grade_vol(s: Series, start: int = 260, step: int = 1, refit: int = 21) -> dict[str, Any]:
    """FC-2: walk-forward QLIKE and 80%-range coverage per estimator."""
    r = _logret(s)
    pv = parkinson_var(s)
    loss: dict[str, list[float]] = {m: [] for m in ESTIMATORS}
    hit: dict[str, list[bool]] = {m: [] for m in ESTIMATORS}
    beta = None
    for i in range(start, len(s.c) - 1, step):
        if beta is None or (i - start) % refit == 0:
            beta = har_fit(pv, i + 1)
        nxt = float(r[i])                          # the move from close i to close i+1
        for m in ESTIMATORS:
            v = forecast_var(s, i, m, beta if m == "har" else None)
            if v is None:
                continue
            loss[m].append(qlike(v, nxt))
            hit[m].append(abs(nxt) <= Z80 * math.sqrt(v))
    out = {m: {"n": len(loss[m]),
               "qlike": round(float(np.mean(loss[m])), 4) if loss[m] else None,
               "coverage_80": round(float(np.mean(hit[m])), 3) if hit[m] else None}
           for m in ESTIMATORS}
    losses = {m: float(np.mean(loss[m])) for m in ESTIMATORS if loss[m]}
    best = min(losses, key=lambda m: losses[m]) if losses else "ewma"
    return {"estimators": out, "best": best}


def vol_view(s: Series, method: str = "ewma") -> dict[str, Any] | None:
    """FC-1 at the latest close: sigma and the 80% move for 1 and 5 sessions."""
    i = len(s.c) - 1
    v = forecast_var(s, i, method)
    if v is None and method != "ewma":
        method, v = "ewma", forecast_var(s, i, "ewma")
    if v is None:
        return None
    sd = math.sqrt(v)
    last = float(s.c[i])
    return {"method": method, "sigma_1d_pct": round(sd * 100, 2),
            "range_1d_pct": round(Z80 * sd * 100, 2),
            "range_5d_pct": round(Z80 * sd * math.sqrt(5) * 100, 2),
            "range_1d": [round(last * (1 - Z80 * sd), 2), round(last * (1 + Z80 * sd), 2)],
            "range_5d": [round(last * (1 - Z80 * sd * math.sqrt(5)), 2),
                         round(last * (1 + Z80 * sd * math.sqrt(5)), 2)]}


# --- FC-3 gaps -------------------------------------------------------------------------------

def gap_stats(series: Mapping[str, Series], buckets: Sequence[float] = (0.25, 0.5, 1.0, 2.0)
              ) -> dict[str, Any]:
    """FC-3: fill rate and gap-and-go rate by gap size in ATR and direction, per ticker and
    pooled. A gap up fills when the day's low reaches the prior close; it 'goes' when the
    close is above the open."""
    edges = list(buckets) + [math.inf]
    def label(k: int) -> str:
        hi = edges[k + 1]
        return f"{edges[k]:g}-{hi:g}" if hi != math.inf else f">{edges[k]:g}"
    acc: dict[str, dict[str, list[tuple[bool, bool]]]] = {}
    for t, s in series.items():
        atr = ta.atr(s.h, s.l, s.c, 14)
        for i in range(15, len(s.c)):
            a, pc = float(atr[i - 1]), float(s.c[i - 1])
            if not a or math.isnan(a):
                continue
            g = (float(s.o[i]) - pc) / a
            size = abs(g)
            k = next((j for j in range(len(edges) - 1) if edges[j] <= size < edges[j + 1]),
                     None)
            if k is None:
                continue
            up = g > 0
            filled = float(s.l[i]) <= pc if up else float(s.h[i]) >= pc
            went = float(s.c[i]) > float(s.o[i]) if up else float(s.c[i]) < float(s.o[i])
            key = f"{'up' if up else 'down'} {label(k)} ATR"
            for who in (t, "ALL"):
                acc.setdefault(who, {}).setdefault(key, []).append((filled, went))
    out: dict[str, Any] = {}
    for who, d in acc.items():
        out[who] = {k: {"n": len(v), "fill_rate": round(sum(f for f, _ in v) / len(v), 3),
                        "go_rate": round(sum(g for _, g in v) / len(v), 3)}
                    for k, v in sorted(d.items())}
    return out


# --- FC-4 macro-event reactions -------------------------------------------------------------

def event_reactions(s: Series, spy: Series | None, events: Mapping[str, Collection[date]],
                    min_events: int = 5) -> dict[str, Any]:
    """FC-4: average |return| and |excess return| on event days vs other days."""
    pos = {d: k for k, d in enumerate(spy.dates)} if spy is not None else {}
    ret = {s.dates[k]: float(s.c[k] / s.c[k - 1] - 1) for k in range(1, len(s.c))}
    ex = {}
    for d, x in ret.items():
        j = pos.get(d)
        ex[d] = x - (float(spy.c[j] / spy.c[j - 1] - 1) if spy is not None and j else 0.0)
    all_days = set(ret)
    ev_days = set().union(*[set(v) for v in events.values()]) if events else set()
    normal = [abs(ret[d]) for d in all_days - ev_days]
    base = float(np.mean(normal)) if normal else 0.0
    out: dict[str, Any] = {"normal_abs_pct": round(base * 100, 3)}
    for name, days in events.items():
        hit = [d for d in days if d in ret]
        if not hit:
            out[name] = {"n": 0, "multiplier": 1.0}
            continue
        m_abs = float(np.mean([abs(ret[d]) for d in hit]))
        mult = m_abs / base if base > 0 and len(hit) >= min_events else 1.0
        out[name] = {"n": len(hit), "abs_pct": round(m_abs * 100, 3),
                     "abs_excess_pct": round(float(np.mean([abs(ex[d]) for d in hit])) * 100,
                                             3),
                     "multiplier": round(mult, 2),
                     "used": len(hit) >= min_events}
    return out


# --- FC-5 sector rotation -------------------------------------------------------------------

def momentum(s: Series, i: int) -> float | None:
    """6-month return excluding the last month (126 to 21 sessions back)."""
    if i < 126:
        return None
    return float(s.c[i - 21] / s.c[i - 126] - 1) * 100


def rs3m(s: Series, spy: Series, i: int) -> float | None:
    d = s.dates[i]
    if i < 63 or d not in spy.dates:
        return None
    j = spy.dates.index(d)
    if j < 63:
        return None
    return (float(s.c[i] / s.c[i - 63]) - float(spy.c[j] / spy.c[j - 63])) * 100


def rank_sectors(etfs: Mapping[str, Series], spy: Series | None, asof: date | None = None
                 ) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for t, s in etfs.items():
        n = len(s.dates) if asof is None else sum(1 for d in s.dates if d <= asof)
        i = n - 1
        mo = momentum(s, i) if i >= 0 else None
        rs = rs3m(s, spy, i) if spy is not None and i >= 0 else None
        if mo is None:
            continue
        rows.append({"etf": t, "momentum_6_1_pct": round(mo, 2),
                     "rs_3m_pts": None if rs is None else round(rs, 2)})
    rows.sort(key=lambda r: -(float(r["momentum_6_1_pct"]) + float(r["rs_3m_pts"] or 0.0)))
    for k, r in enumerate(rows, 1):
        r["rank"] = k
    return rows


def _fwd(s: Series, d: date, d2: date) -> float | None:
    if d not in s.dates or d2 not in s.dates:
        return None
    return float(s.c[s.dates.index(d2)] / s.c[s.dates.index(d)] - 1) * 100


def grade_rotation(etfs: Mapping[str, Series], spy: Series | None, horizon: int = 21,
                   top: int = 3) -> dict[str, Any]:
    """FC-5: month by month, the next-``horizon`` return of the top ``top`` minus the bottom."""
    if not etfs:
        return {"months": 0}
    ref = max(etfs.values(), key=lambda s: len(s.dates))
    spreads = []
    for i in range(130, len(ref.dates) - horizon, horizon):
        d, d2 = ref.dates[i], ref.dates[i + horizon]
        ranked = rank_sectors(etfs, spy, d)
        if len(ranked) < 2 * top:
            continue
        hi = [x for x in (_fwd(etfs[r["etf"]], d, d2) for r in ranked[:top]) if x is not None]
        lo = [x for x in (_fwd(etfs[r["etf"]], d, d2) for r in ranked[-top:]) if x is not None]
        if hi and lo:
            spreads.append(float(np.mean(hi)) - float(np.mean(lo)))
    if not spreads:
        return {"months": 0}
    a = np.asarray(spreads)
    return {"months": len(spreads), "avg_top_minus_bottom_pct": round(float(a.mean()), 2),
            "hit_rate": round(float((a > 0).mean()), 3),
            "t_stat": round(float(a.mean() / (a.std(ddof=1) / math.sqrt(len(a)))), 2)
            if len(a) > 2 and a.std(ddof=1) > 0 else None}
