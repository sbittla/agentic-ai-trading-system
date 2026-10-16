"""Portfolio risk statistics from daily bars (PF-1..PF-4, OC-2).

Computed once a day by trader-core from the daily bars it already loads for the level map
(A4-1), using bars before today only. A7 reads them to size and limit entries; A8 reads the
average daily volume for the order sanity check. Pure functions: same bars, same numbers.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date

from trader.models.messages import Bar
from trader.timeutil import et_date


@dataclass(frozen=True)
class PortfolioStats:
    asof: date | None = None
    corr: Mapping[frozenset[str], float] = field(default_factory=dict)   # 60d return corr
    beta: Mapping[str, float] = field(default_factory=dict)              # vs SPY, 60d
    gap: Mapping[str, float] = field(default_factory=dict)       # adverse overnight gap pctile
    adv: Mapping[str, float] = field(default_factory=dict)       # 20-day average volume

    def correlation(self, a: str, b: str) -> float | None:
        return self.corr.get(frozenset((a, b))) if a != b else 1.0

    def beta_of(self, t: str) -> float:
        """Unknown beta counts as 1.0 (market-like) rather than 0, so it is never ignored."""
        return self.beta.get(t, 1.0)


def _closes(bars: Sequence[Bar], before: date | None) -> dict[date, Bar]:
    return {et_date(b.ts): b for b in bars if before is None or et_date(b.ts) < before}


def _returns(by_day: Mapping[date, Bar], days: Sequence[date]) -> list[float]:
    out = []
    for prev, cur in zip(days, days[1:], strict=False):
        p, c = by_day[prev].c, by_day[cur].c
        out.append(math.log(c / p) if p > 0 and c > 0 else 0.0)
    return out


def _pearson(x: Sequence[float], y: Sequence[float]) -> float | None:
    n = len(x)
    if n < 20:
        return None
    mx, my = sum(x) / n, sum(y) / n
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y, strict=True))
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / math.sqrt(sxx * syy)


def percentile(values: Sequence[float], pct: float) -> float:
    """Linear-interpolated percentile (numpy's default method), no numpy needed."""
    v = sorted(values)
    if not v:
        return 0.0
    k = (len(v) - 1) * pct / 100
    lo, hi = math.floor(k), math.ceil(k)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def compute(daily: Mapping[str, Sequence[Bar]], spy: Sequence[Bar] | None, asof: date | None,
            lookback: int = 60, gap_lookback: int = 252, gap_pct: float = 99.0,
            adv_days: int = 20) -> PortfolioStats:
    """PF-2 correlations and PF-3 betas over the last ``lookback`` common sessions; PF-4 the
    ``gap_pct`` percentile of adverse overnight gaps (open vs prior close, as a positive
    fraction) over ``gap_lookback`` sessions; OC-2 the average daily volume."""
    by = {t: _closes(b, asof) for t, b in daily.items() if b}
    spy_by = _closes(spy, asof) if spy else {}
    corr: dict[frozenset[str], float] = {}
    beta: dict[str, float] = {}
    gap: dict[str, float] = {}
    adv: dict[str, float] = {}
    tickers = sorted(by)
    for t in tickers:
        days = sorted(by[t])
        gaps = [max(0.0, 1 - by[t][d].o / by[t][p].c)
                for p, d in zip(days, days[1:], strict=False)][-gap_lookback:]
        if len(gaps) >= 20:
            gap[t] = round(percentile(gaps, gap_pct), 5)
        vols = [by[t][d].v for d in days[-adv_days:]]
        if vols:
            adv[t] = sum(vols) / len(vols)
        if spy_by:
            common = sorted(set(days) & set(spy_by))[-(lookback + 1):]
            rs, rm = _returns(by[t], common), _returns(spy_by, common)
            n = len(rm)
            var = sum((x - sum(rm) / n) ** 2 for x in rm) if n else 0.0
            if n >= 20 and var > 0:
                cov = sum((a - sum(rs) / n) * (b - sum(rm) / n) for a, b in zip(rs, rm,
                                                                                strict=True))
                beta[t] = round(cov / var, 3)
    for i, a in enumerate(tickers):
        for b in tickers[i + 1:]:
            common = sorted(set(by[a]) & set(by[b]))[-(lookback + 1):]
            c = _pearson(_returns(by[a], common), _returns(by[b], common))
            if c is not None:
                corr[frozenset((a, b))] = round(c, 3)
    return PortfolioStats(asof=asof, corr=corr, beta=beta, gap=gap, adv=adv)
