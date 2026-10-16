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


# --- risk-adjusted performance and statistical validation (PA-1..PA-3, SV-1..SV-3) ----------

def _mean(x: Sequence[float]) -> float:
    return sum(x) / len(x) if x else 0.0


def _std(x: Sequence[float]) -> float:
    if len(x) < 2:
        return 0.0
    m = _mean(x)
    return float((sum((v - m) ** 2 for v in x) / (len(x) - 1)) ** 0.5)


def daily_returns(equity: Sequence[float]) -> list[float]:
    """Simple returns between consecutive end-of-day equity values."""
    return [b / a - 1 for a, b in zip(equity, equity[1:], strict=False) if a > 0]


def risk_adjusted(returns: Sequence[float], rs: Sequence[float], periods: int = 252
                  ) -> dict[str, Any]:
    """PA-1: Sharpe, Sortino and Calmar from daily returns (risk-free 0, annualised), SQN and
    the longest losing streak from per-trade R. ``None`` when there is too little data."""
    sd = _std(returns)
    downside = [min(0.0, r) for r in returns]
    dd_sd = (sum(v * v for v in downside) / len(downside)) ** 0.5 if downside else 0.0
    curve = [1.0]
    for r in returns:
        curve.append(curve[-1] * (1 + r))
    mdd = max_drawdown_pct(curve)
    years = len(returns) / periods if returns else 0.0
    cagr = (curve[-1] ** (1 / years) - 1) * 100 if years > 0 and curve[-1] > 0 else 0.0
    streak = longest = 0
    for r in rs:
        streak = streak + 1 if r <= 0 else 0
        longest = max(longest, streak)
    sr = _std(rs)
    enough = len(returns) >= 20
    return {
        "days": len(returns),
        "sharpe": round(_mean(returns) / sd * periods ** 0.5, 2) if enough and sd > 0 else None,
        "sortino": round(_mean(returns) / dd_sd * periods ** 0.5, 2)
        if enough and dd_sd > 0 else None,
        "calmar": round(cagr / mdd, 2) if enough and mdd > 0 else None,
        "cagr_pct": round(cagr, 2) if enough else None,
        "max_dd_pct": mdd,
        "sqn": round(len(rs) ** 0.5 * _mean(rs) / sr, 2) if len(rs) >= 10 and sr > 0 else None,
        "max_consecutive_losses": longest,
    }


def _rng(seed: int) -> Any:
    import random

    return random.Random(seed)   # noqa: S311 - resampling, not security


def bootstrap_ci(rs: Sequence[float], n_boot: int = 2000, alpha: float = 0.05, seed: int = 7
                 ) -> tuple[float, float] | None:
    """SV-1: percentile-bootstrap confidence interval of the mean R. Seeded: same trades, same
    interval."""
    if len(rs) < 10:
        return None
    rng, n = _rng(seed), len(rs)
    means = sorted(_mean([rs[rng.randrange(n)] for _ in range(n)]) for _ in range(n_boot))
    lo = means[int(alpha / 2 * n_boot)]
    hi = means[min(n_boot - 1, int((1 - alpha / 2) * n_boot))]
    return round(lo, 3), round(hi, 3)


def monte_carlo_drawdown(rs: Sequence[float], risk_pct: float, n_trades: int | None = None,
                         n_paths: int = 2000, pct: float = 95, seed: int = 11) -> float | None:
    """SV-2: the ``pct`` percentile of the max drawdown (% of equity) over ``n_trades`` trades
    drawn with replacement from the observed R, at ``risk_pct`` risk per trade. Shows the
    drawdown to expect from bad luck alone, before it happens."""
    if len(rs) < 10:
        return None
    rng, n = _rng(seed), len(rs)
    k = n_trades or n
    dds = []
    for _ in range(n_paths):
        eq, peak, worst = 1.0, 1.0, 0.0
        for _ in range(k):
            eq *= 1 + rs[rng.randrange(n)] * risk_pct / 100
            peak = max(peak, eq)
            worst = max(worst, (peak - eq) / peak)
        dds.append(worst * 100)
    dds.sort()
    return round(dds[min(n_paths - 1, int(pct / 100 * n_paths))], 2)


def statistical_gate(rs: Sequence[float], risk_pct: float, dd_limit_pct: float,
                     min_trades: int = 100) -> dict[str, Any]:
    """SV-3: the go-live evidence a desk asks for, on top of §6.4 averages: enough trades, the
    lower end of the 95% interval of expectancy above zero, and a Monte Carlo 95th-percentile
    drawdown (over 100 trades) inside the monthly limit."""
    ci = bootstrap_ci(rs)
    mc = monte_carlo_drawdown(rs, risk_pct, n_trades=100)
    checks = {
        f"trades_ge_{min_trades}": len(rs) >= min_trades,
        "expectancy_ci_low_gt_0": ci is not None and ci[0] > 0,
        "mc_dd95_inside_limit": mc is not None and mc <= dd_limit_pct,
    }
    return {"trades": len(rs), "expectancy_ci95": ci, "mc_dd95_pct_100_trades": mc,
            "dd_limit_pct": dd_limit_pct, "checks": checks, "pass": all(checks.values())}


def benchmark(start_close: float | None, end_close: float | None, strategy_return_pct: float
              ) -> dict[str, Any]:
    """PA-2: the same period's SPY return and the strategy's excess over it."""
    if not start_close or not end_close:
        return {"spy_return_pct": None, "excess_pct": None}
    spy = (end_close / start_close - 1) * 100
    return {"spy_return_pct": round(spy, 2), "excess_pct": round(strategy_return_pct - spy, 2)}
