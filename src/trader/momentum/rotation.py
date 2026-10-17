"""The momentum rotation model (MO-13, MO-25): hold the top names by a momentum ranking,
inverse-volatility weighted, rebalanced on a fixed calendar. The same functions run the
backtest and the live rebalance alerts."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from trader.momentum.factors import Panel, regime
from trader.scanner.techniques import Series


def rebalance_due(d: date, next_d: date | None, weeks: int) -> bool:
    """MO-13: the last session of an ISO week whose number is a multiple of ``weeks``."""
    wk = d.isocalendar()
    if next_d is not None and next_d.isocalendar()[:2] == wk[:2]:
        return False
    return wk[1] % weeks == 0


def targets(panel: Panel, spy: Series | None, d: date, current: Mapping[str, float],
            factors: Sequence[str] | None, top_n: int, hold_rank: int, regime_filter: bool,
            max_weight: float = 0.2) -> dict[str, float]:
    """Target weights on session d. Holdings stay while ranked within ``hold_rank`` and above
    their SMA100; free slots take the best-ranked names above their SMA100 (none in regime
    OFF when the filter is on). Weights are inverse 60-session volatility; with fewer than
    ``top_n`` names the rest stays in cash. No name above ``max_weight``: a stock pinned by a
    takeover bid has almost no volatility and would otherwise take a third of the book."""
    comp = panel.composite(d, factors)
    ranked = [s for s, _ in sorted(comp.items(), key=lambda kv: (-kv[1], kv[0]))]
    pos = {s: n for n, s in enumerate(ranked, 1)}

    def ok(sym: str) -> bool:
        x = panel.frames[sym]
        i = x.pos[d]
        return bool(x.c[i] > x.sma100[i])
    picks = [s for s in current if s in pos and pos[s] <= hold_rank and ok(s)]
    if not (regime_filter and regime(spy, d).state == "OFF"):
        for s in ranked:
            if len(picks) >= top_n:
                break
            if s not in picks and ok(s):
                picks.append(s)
    inv = {}
    for s in picks:
        x = panel.frames[s]
        v = float(x.vol60[x.pos[d]])
        inv[s] = 1 / v if v > 0 and math.isfinite(v) else 0.0
    tot = sum(inv.values())
    if tot <= 0:
        return {}
    scale = min(1.0, len(picks) / top_n)
    return cap_weights({s: v / tot * scale for s, v in inv.items()}, max_weight)


def cap_weights(w: Mapping[str, float], cap: float) -> dict[str, float]:
    """Cap each weight, handing the excess to the uncapped names pro rata (water-filling);
    what no name can take stays in cash."""
    out = dict(w)
    for _ in range(len(out) + 1):
        over = {s: v for s, v in out.items() if v > cap + 1e-12}
        if not over:
            break
        excess = sum(v - cap for v in over.values())
        for s in over:
            out[s] = cap
        free = {s: v for s, v in out.items() if v < cap - 1e-12}
        tot = sum(free.values())
        if tot <= 0:
            break
        for s, v in free.items():
            out[s] = v + excess * v / tot
    return out


@dataclass
class Run:
    equity: list[float]
    rebalances: list[tuple[date, dict[str, float]]]
    turnover_per_year: float
    invested_share: float


def run(panel: Panel, spy: Series | None, dates: Sequence[date], *,
        factors: Sequence[str] | None = None, top_n: int = 10, hold_rank: int = 20,
        weeks: int = 2, cost_bps: float = 10.0, regime_filter: bool = True,
        last_next: date | None = None, max_weight: float = 0.2) -> Run:
    """Decided on a rebalance close, held from the next session's close (one-session lag, no
    look-ahead). ``last_next``: the session after the last date (live), for the calendar."""
    weights: dict[str, float] = {}
    pending: dict[str, float] | None = None
    eq = [1.0]
    turnover = 0.0
    held = 0
    hist: list[tuple[date, dict[str, float]]] = []
    for k in range(len(dates)):
        if k > 0:
            a, b = dates[k - 1], dates[k]
            r = 0.0
            for sym, w in weights.items():
                x = panel.frames[sym]
                i, j = x.pos.get(a), x.pos.get(b)
                if i is not None and j is not None:
                    r += w * float(x.c[j] / x.c[i] - 1)
            if pending is not None:
                keys = set(weights) | set(pending)
                tw = sum(abs(pending.get(s, 0.0) - weights.get(s, 0.0)) for s in keys)
                turnover += tw
                r -= tw * cost_bps / 1e4
                weights, pending = pending, None
            eq.append(eq[-1] * (1 + r))
            held += bool(weights)
        nxt = dates[k + 1] if k + 1 < len(dates) else last_next
        if rebalance_due(dates[k], nxt, weeks):
            base = pending if pending is not None else weights
            pending = targets(panel, spy, dates[k], base, factors, top_n, hold_rank,
                              regime_filter, max_weight)
            hist.append((dates[k], dict(pending)))
    years = max(len(dates) / 252, 1e-9)
    return Run(eq, hist, round(turnover / years, 2), round(held / max(len(dates) - 1, 1), 3))
