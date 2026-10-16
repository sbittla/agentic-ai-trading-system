"""Risk-adjusted analytics PA-1..PA-3 and statistical validation SV-1..SV-3 (GAP_REVIEW.md)."""

from __future__ import annotations

import pytest

from trader.metrics import (
    benchmark,
    bootstrap_ci,
    daily_returns,
    monte_carlo_drawdown,
    risk_adjusted,
    statistical_gate,
)


@pytest.mark.rule("PA-1")
def test_sharpe_sortino_calmar_sqn() -> None:
    rets = [0.002, -0.001, 0.003, 0.0, -0.002] * 10
    rs = [1.5, -1.0, 2.0, -1.0, 0.5, -1.0, 1.0, -1.0, 2.5, -1.0, -1.0, -1.0]
    m = risk_adjusted(rets, rs)
    assert m["sharpe"] is not None and m["sharpe"] > 0
    assert m["sortino"] is not None and m["sortino"] > m["sharpe"]   # few down days
    assert m["calmar"] is not None and m["max_dd_pct"] > 0
    assert m["sqn"] is not None
    assert m["max_consecutive_losses"] == 3
    short = risk_adjusted(rets[:5], rs[:3])
    assert short["sharpe"] is None and short["sqn"] is None    # too little data: no number


@pytest.mark.rule("PA-1")
def test_daily_returns() -> None:
    assert daily_returns([100, 110, 99]) == pytest.approx([0.1, -0.1])


@pytest.mark.rule("PA-2")
def test_benchmark_excess_over_spy() -> None:
    b = benchmark(500.0, 510.0, 3.0)
    assert b == {"spy_return_pct": 2.0, "excess_pct": 1.0}
    assert benchmark(None, 510.0, 3.0)["excess_pct"] is None


@pytest.mark.rule("SV-1")
def test_bootstrap_interval_is_seeded_and_brackets_the_mean() -> None:
    rs = [1.0, -1.0, 2.0, -1.0, 0.5, -0.5, 1.5, -1.0, 0.8, -1.0] * 3
    lo, hi = bootstrap_ci(rs) or (0.0, 0.0)
    mean = sum(rs) / len(rs)
    assert lo < mean < hi
    assert bootstrap_ci(rs) == (lo, hi)          # deterministic
    assert bootstrap_ci(rs[:5]) is None


@pytest.mark.rule("SV-2")
def test_monte_carlo_drawdown_grows_with_risk() -> None:
    rs = [1.5, -1.0, -1.0, 2.0, -1.0, 0.5, -1.0, 1.0, -1.0, 1.2]
    low = monte_carlo_drawdown(rs, 0.25, n_trades=100)
    high = monte_carlo_drawdown(rs, 1.0, n_trades=100)
    assert low is not None and high is not None and high > 3 * low


@pytest.mark.rule("SV-3")
def test_statistical_gate_needs_evidence_not_just_a_good_average() -> None:
    lucky = [3.0, -1.0, -1.0, -1.0, 4.0, -1.0, -1.0, -1.0, -1.0, 2.0] * 3   # 30 trades
    g = statistical_gate(lucky, 0.5, 6.0)
    assert sum(lucky) / len(lucky) > 0 and g["pass"] is False      # too few, interval spans 0
    solid = [1.2, -1.0, 1.5, 0.8, -1.0, 1.0, 1.4, -0.8, 1.1, 0.9] * 12   # 120 trades
    ok = statistical_gate(solid, 0.5, 6.0)
    assert ok["checks"]["expectancy_ci_low_gt_0"] and ok["pass"]
