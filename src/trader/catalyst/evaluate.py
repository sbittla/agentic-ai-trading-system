"""Walk-forward grading of catalyst outlooks (NC-30..NC-35).

Every past story becomes an outlook made at the close of its day 0, using only events whose
outcome was already known that day. The outlook is then graded against what happened:

- Brier score of P(up) (lower is better; always saying 50% scores 0.25),
- coverage of the 10-90% range (it should contain the outcome about 80% of the time),
- a reliability table (predicted vs actual frequency, by decile),
- the average excess return after BULLISH and BEARISH stances.

The feature gate (NC-33) keeps an adjustment only if it improves the walk-forward result;
the alert gate (NC-34) unlocks the "calibrated" label and Telegram alerts.
"""

from __future__ import annotations

import heapq
import math
from collections import defaultdict
from collections.abc import Sequence
from datetime import date
from typing import Any

import numpy as np

from trader.catalyst.outlook import ADJUSTMENTS, EventObs, Pool, horizon_view

WARMUP = 30   # events with known outcomes before the first graded outlook


def walk_forward(obs: Sequence[EventObs], horizons: Sequence[int], *, k: float = 30,
                 adjustments: Sequence[str] = (), stance_p: float = 0.56
                 ) -> dict[int, list[dict[str, Any]]]:
    """Graded outlooks per horizon. No look-ahead: an event joins the pool for horizon h only
    once its h-session outcome date is before the outlook's day 0 (NC-31)."""
    events = sorted(obs, key=lambda o: (o.d0, o.ticker))
    pools = {h: Pool() for h in horizons}
    pending: dict[int, list[tuple[date, int, EventObs]]] = {h: [] for h in horizons}
    out: dict[int, list[dict[str, Any]]] = {h: [] for h in horizons}
    seq = 0
    for o in events:
        for h in horizons:
            q = pending[h]
            while q and q[0][0] < o.d0:
                _, _, done = heapq.heappop(q)
                pools[h].add(done, [h])
            if h in o.x_raw and pools[h].n >= WARMUP:
                v = horizon_view(pools[h], o, h, o.sigma, k=k, adjustments=adjustments)
                real = o.x_raw[h] * o.sigma * math.sqrt(h) * 100
                real_ex = o.x_abn[h] * o.sigma * math.sqrt(h) * 100
                st = "BULLISH" if v.p_up >= stance_p and v.ex_median_pct > 0 else \
                    "BEARISH" if v.p_up <= 1 - stance_p and v.ex_median_pct < 0 else "NEUTRAL"
                out[h].append({"d0": o.d0.isoformat(), "ticker": o.ticker, "ctype": o.ctype,
                               "direction": o.direction, "p_up": v.p_up,
                               "p10": v.p10_pct, "p90": v.p90_pct, "median": v.median_pct,
                               "realized": round(real, 3), "realized_ex": round(real_ex, 3),
                               "up": real > 0, "in_range": v.p10_pct <= real <= v.p90_pct,
                               "stance": st, "n": v.n})
            if h in o.end:
                seq += 1
                heapq.heappush(pending[h], (o.end[h], seq, o))
    return out


def metrics(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """NC-32 for one horizon."""
    if not rows:
        return {"graded": 0}
    p = np.asarray([r["p_up"] for r in rows])
    y = np.asarray([1.0 if r["up"] else 0.0 for r in rows])
    brier = float(np.mean((p - y) ** 2))
    clim = float(np.mean(y))
    rel = []
    for lo in np.arange(0.0, 1.0, 0.1):
        m = (p >= lo) & (p < lo + 0.1 if lo < 0.9 else p <= 1.0)
        if m.sum() >= 5:
            rel.append({"bin": f"{lo:.1f}-{lo + 0.1:.1f}", "n": int(m.sum()),
                        "predicted": round(float(p[m].mean()), 3),
                        "actual": round(float(y[m].mean()), 3)})
    stance: dict[str, Any] = {}
    for st in ("BULLISH", "BEARISH", "NEUTRAL"):
        xs = [r["realized_ex"] for r in rows if r["stance"] == st]
        stance[st] = {"n": len(xs), "avg_excess_pct": round(float(np.mean(xs)), 3)
                      if xs else None}
    by_type: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        by_type[r["ctype"]].append((r["p_up"] - (1.0 if r["up"] else 0.0)) ** 2)
    return {"graded": len(rows), "brier": round(brier, 4), "brier_coin": 0.25,
            "brier_climatology": round(clim * (1 - clim), 4),
            "coverage": round(float(np.mean([r["in_range"] for r in rows])), 3),
            "up_rate": round(clim, 3), "reliability": rel, "stance": stance,
            "brier_by_type": {k: {"n": len(v), "brier": round(float(np.mean(v)), 4)}
                              for k, v in sorted(by_type.items())}}


def _score(ms: dict[int, dict[str, Any]]) -> tuple[float, float]:
    """(mean Brier, mean |coverage - 0.8|) over horizons with graded outlooks."""
    graded = [m for m in ms.values() if m.get("graded")]
    if not graded:
        return 1.0, 1.0
    return (float(np.mean([m["brier"] for m in graded])),
            float(np.mean([abs(m["coverage"] - 0.8) for m in graded])))


def feature_gate(obs: Sequence[EventObs], horizons: Sequence[int], k: float, stance_p: float
                 ) -> tuple[list[str], dict[str, Any]]:
    """NC-33: an adjustment stays only if it lowers the walk-forward Brier score without
    making the range coverage worse."""
    base = {h: metrics(v) for h, v in walk_forward(obs, horizons, k=k,
                                                   stance_p=stance_p).items()}
    b_brier, b_cov = _score(base)
    kept: list[str] = []
    detail: dict[str, Any] = {"none": {"brier": round(b_brier, 4), "coverage_err": round(b_cov,
                                                                                         4)}}
    for a in ADJUSTMENTS:
        if not any(a in o.flags for o in obs):
            detail[a] = {"skipped": "no history carries this flag yet"}
            continue
        ms = {h: metrics(v) for h, v in walk_forward(obs, horizons, k=k, adjustments=(a,),
                                                     stance_p=stance_p).items()}
        br, cov = _score(ms)
        keep = br < b_brier - 0.0005 and cov <= b_cov + 0.01
        detail[a] = {"brier": round(br, 4), "coverage_err": round(cov, 4), "kept": keep}
        if keep:
            kept.append(a)
    return kept, detail


def alert_gate(ms: dict[int, dict[str, Any]], min_graded: int,
               coverage: tuple[float, float]) -> dict[str, Any]:
    """NC-34: per horizon, enough graded outlooks, Brier better than a coin, and the 10-90%
    range holding the outcome 75-85% of the time."""
    per = {}
    for h, m in ms.items():
        ok = (m.get("graded", 0) >= min_graded and m["brier"] < 0.25
              and coverage[0] <= m["coverage"] <= coverage[1])
        per[str(h)] = {"pass": bool(ok), "graded": m.get("graded", 0),
                       "brier": m.get("brier"), "coverage": m.get("coverage")}
    return {"pass": bool(per) and all(v["pass"] for v in per.values()), "horizons": per}


NO_TILT_K = 1e12    # shrinkage so strong the mean is zero: the volatility range alone


def evaluate(obs: Sequence[EventObs], horizons: Sequence[int], *, k: float = 30,
             stance_p: float = 0.56, min_graded: int = 100,
             coverage: tuple[float, float] = (0.75, 0.85)) -> dict[str, Any]:
    kept, detail = feature_gate(obs, horizons, k, stance_p)
    graded = walk_forward(obs, horizons, k=k, adjustments=kept, stance_p=stance_p)
    ms = {h: metrics(v) for h, v in graded.items()}
    # NC-49 tilt gate: the measured tilt must beat no tilt at all, or it is switched off
    flat = {h: metrics(v) for h, v in walk_forward(obs, horizons, k=NO_TILT_K,
                                                   stance_p=stance_p).items()}
    tilt_brier, flat_brier = _score(ms)[0], _score(flat)[0]
    tilt = tilt_brier < flat_brier
    if not tilt:
        kept, ms = [], flat
    detail["tilt"] = {"with_tilt_brier": round(tilt_brier, 4),
                      "no_tilt_brier": round(flat_brier, 4), "kept": tilt}
    gate = alert_gate(ms, min_graded, coverage)
    tickers = sorted({o.ticker for o in obs})
    return {"events": len(obs), "tickers": len(tickers),
            "from": min((o.d0 for o in obs), default=None),
            "to": max((o.d0 for o in obs), default=None),
            "horizons": {str(h): m for h, m in ms.items()},
            "tilt": tilt, "adjustments": kept, "feature_gate": detail, "gate": gate,
            "note": ("Outlooks are graded walk-forward; overlapping horizons on the same "
                     "ticker are not independent, so treat the counts as optimistic.")}
