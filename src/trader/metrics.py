"""Trading metrics computed from journal rows (MONITORING.md §2, REQUIREMENTS §6.4).

Identical in replay, paper and live because every figure is a journal query (A11-3).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal
from typing import Any


def _f(x: Any) -> float:
    return float(x) if x is not None else 0.0


def summarize(trades: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    ts = list(trades)
    pnls = [_f(t.get("pnl")) for t in ts]
    rs = [_f(t.get("r_multiple")) for t in ts]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_win, gross_loss = sum(wins), -sum(losses)
    avg_win = gross_win / len(wins) if wins else 0.0
    avg_loss = gross_loss / len(losses) if losses else 0.0
    return {
        "trades": len(ts),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(ts), 4) if ts else 0.0,
        "pnl": round(sum(pnls), 2),
        "total_r": round(sum(rs), 2),
        "expectancy_r": round(sum(rs) / len(rs), 3) if rs else 0.0,
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss > 0 else (
            float("inf") if gross_win > 0 else 0.0),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "win_loss_ratio": round(avg_win / avg_loss, 3) if avg_loss > 0 else (
            float("inf") if avg_win > 0 else 0.0),
    }


def max_drawdown_pct(equity_curve: Iterable[float]) -> float:
    peak, worst = None, 0.0
    for e in equity_curve:
        peak = e if peak is None else max(peak, e)
        if peak and peak > 0:
            worst = min(worst, (e - peak) / peak * 100)
    return abs(round(-worst, 3))


def by(trades: Iterable[Mapping[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for t in trades:
        groups[str(t.get(key))].append(t)
    return {k: summarize(v) for k, v in sorted(groups.items())}


def standards(trades: Sequence[Mapping[str, Any]], breaches: int, max_dd_pct: float,
              monthly_limit_pct: float = 6.0) -> dict[str, Any]:
    """REQUIREMENTS §6.4 performance standards over the last 30 trades."""
    s = summarize(trades[-30:])
    checks = {
        "expectancy_positive": s["expectancy_r"] > 0,
        "profit_factor_ge_1_3": s["profit_factor"] >= 1.3,
        "win_loss_ge_1_5": s["win_loss_ratio"] >= 1.5,
        "zero_breaches": breaches == 0,
        "drawdown_inside_limit": max_dd_pct < monthly_limit_pct,
    }
    return {"summary": s, "checks": checks, "pass": all(checks.values())}


def dec_sum(values: Iterable[Any]) -> Decimal:
    return sum((Decimal(str(v)) for v in values if v is not None), Decimal(0))
