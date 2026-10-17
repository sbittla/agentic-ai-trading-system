"""Momentum factors, RS rating, trend template, stage and regime (MO-2..MO-6).

Every array value at index i uses bars up to i only (MO-19). Returns are fractions, not %.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date

import numpy as np

from trader.scanner import ta
from trader.scanner.ta import Arr
from trader.scanner.techniques import Series

MIN_HISTORY = 260
JUMP_UP = 0.5            # MO-22: a one-day gain above 50% is a probable corporate action
FACTORS = ("r12_1", "r6_1", "r3", "clenow", "high52", "vadj", "resid")
RS_WEIGHTS = ((63, 0.4), (126, 0.2), (189, 0.2), (252, 0.2))


def _nan(n: int) -> Arr:
    return np.full(n, np.nan, dtype=np.float64)


def ret(c: Arr, n: int, skip: int = 0) -> Arr:
    """Return from n sessions ago to ``skip`` sessions ago."""
    out = _nan(len(c))
    if len(c) > n:
        out[n:] = c[n - skip:len(c) - skip] / c[:len(c) - n] - 1.0
    return out


def clenow(c: Arr, n: int = 90) -> Arr:
    """Annualised slope of the n-session log-price regression x R-squared (Clenow)."""
    out = _nan(len(c))
    if len(c) < n:
        return out
    y = np.log(c)
    x = np.arange(n, dtype=np.float64)
    sx, sxx = x.sum(), (x * x).sum()
    sy = np.convolve(y, np.ones(n), "valid")
    syy = np.convolve(y * y, np.ones(n), "valid")
    sxy = np.correlate(y, x, "valid")
    cov = sxy - sx * sy / n
    vx = sxx - sx * sx / n
    vy = syy - sy * sy / n
    b = cov / vx
    r2 = np.where(vy > 0, cov * cov / (vx * np.where(vy > 0, vy, 1.0)), 0.0)
    out[n - 1:] = (np.exp(b * 252) - 1.0) * r2
    return out


def _roll_sum(x: Arr, n: int) -> Arr:
    out = _nan(len(x))
    if len(x) >= n:
        cs = np.cumsum(np.insert(x, 0, 0.0))
        out[n - 1:] = cs[n:] - cs[:-n]
    return out


def residual_momentum(c: Arr, m: Arr, n: int = 252, skip: int = 21) -> Arr:
    """12-1 log return net of an n-session market beta, / residual volatility (Blitz, Huij &
    Martens). The intercept is not removed: with the beta window equal to the formation
    window it would absorb exactly the drift the factor is meant to find.
    ``m`` is the market close aligned to ``c`` (NaN where missing)."""
    out = _nan(len(c))
    if len(c) <= n:
        return out
    r = np.diff(np.log(c), prepend=np.nan)
    rm = np.diff(np.log(m), prepend=np.nan)
    ok = ~(np.isnan(r) | np.isnan(rm))
    r0, rm0 = np.where(ok, r, 0.0), np.where(ok, rm, 0.0)
    cnt = _roll_sum(ok.astype(np.float64), n)
    sr, sm = _roll_sum(r0, n), _roll_sum(rm0, n)
    srr, smm, srm = _roll_sum(r0 * r0, n), _roll_sum(rm0 * rm0, n), _roll_sum(r0 * rm0, n)
    with np.errstate(invalid="ignore", divide="ignore"):
        cov = srm - sr * sm / cnt
        vm = smm - sm * sm / cnt
        vr = srr - sr * sr / cnt
        beta = cov / vm
        res_var = np.maximum(vr - beta * cov, 0.0) / np.maximum(cnt - 2, 1)
        # the sub-window [i-n+1, i-skip]
        sub_r = sr - _roll_sum_shift(r0, skip)
        sub_m = sm - _roll_sum_shift(rm0, skip)
        sub_n = cnt - _roll_sum_shift(ok.astype(np.float64), skip)
        resid = sub_r - beta * sub_m           # the alpha stays in: it is the signal
        val = resid / np.sqrt(res_var * np.maximum(sub_n, 1))
    good = (cnt >= 0.9 * n) & np.isfinite(val)
    out[good] = val[good]
    return out


def _roll_sum_shift(x: Arr, k: int) -> Arr:
    """Sum of the last k values (the part of the window excluded by ``skip``)."""
    if k <= 0:
        return np.zeros(len(x))
    s = _roll_sum(x, k)
    return np.where(np.isnan(s), 0.0, s)


def info_discreteness(c: Arr, n: int = 252, skip: int = 21) -> Arr:
    """Frog-in-the-pan ID (Da, Gurun & Warachka): sign(PRET) x (%neg - %pos), lower is a
    smoother trend."""
    out = _nan(len(c))
    if len(c) <= n:
        return out
    r = np.diff(c, prepend=np.nan)
    pos = _roll_sum(np.where(r > 0, 1.0, 0.0), n - skip)
    neg = _roll_sum(np.where(r < 0, 1.0, 0.0), n - skip)
    pos, neg = ta.shift(pos, skip), ta.shift(neg, skip)
    pret = ret(c, n, skip)
    tot = n - skip
    out[:] = np.sign(pret) * (neg - pos) / tot
    return out


def adr_pct(h: Arr, lo: Arr, n: int = 20) -> Arr:
    return ta.sma(h / lo - 1.0, n) * 100.0


def max_gap(o: Arr, c: Arr, n: int = 90) -> Arr:
    g = np.abs(o / ta.shift(c) - 1.0)
    g[0] = 0.0
    return ta.rolling_max(np.nan_to_num(g), n)


def align(s: Series, other: Series | None) -> Arr:
    """``other``'s close on each of ``s``'s dates (NaN where missing)."""
    out = _nan(len(s.c))
    if other is None:
        return out
    idx = {d: j for j, d in enumerate(other.dates)}
    for i, d in enumerate(s.dates):
        j = idx.get(d)
        if j is not None:
            out[i] = other.c[j]
    return out


class MomFrame:
    """Per-symbol factor and indicator arrays (long side only: ADR-008)."""

    def __init__(self, s: Series, spy: Series | None) -> None:
        self.s = s
        self.symbol, self.dates = s.symbol, s.dates
        o, h, lo, c, v = s.o, s.h, s.l, s.c, s.v
        self.o, self.h, self.l, self.c, self.v = o, h, lo, c, v
        self.pos = {d: i for i, d in enumerate(s.dates)}
        self.atr = ta.atr(h, lo, c, 14)
        self.atr20 = ta.atr(h, lo, c, 20)
        self.atr22 = ta.atr(h, lo, c, 22)
        self.ema10, self.ema20 = ta.ema(c, 10), ta.ema(c, 20)
        self.sma10, self.sma50 = ta.sma(c, 10), ta.sma(c, 50)
        self.sma100, self.sma150, self.sma200 = ta.sma(c, 100), ta.sma(c, 150), ta.sma(c, 200)
        self.vol50 = ta.shift(ta.sma(v, 50))
        self.dollar20 = ta.sma(c * v, 20)
        self.hi252 = ta.rolling_max(h, 252)
        self.lo252 = ta.rolling_min(lo, 252)
        self.ll20 = ta.shift(ta.rolling_min(lo, 20))
        lr = np.diff(np.log(c), prepend=np.nan)
        self.vol60 = _roll_std(lr, 60) * math.sqrt(252)
        vol252 = _roll_std(lr, 252) * math.sqrt(252)
        mkt = align(s, spy)
        self.f: dict[str, Arr] = {
            "r12_1": ret(c, 252, 21), "r6_1": ret(c, 126, 21), "r3": ret(c, 63),
            "clenow": clenow(c, 90), "high52": c / self.hi252,
            "vadj": ret(c, 252, 21) / vol252, "resid": residual_momentum(c, mkt),
        }
        self.rs_raw: Arr = np.zeros(len(c))
        for n, w in RS_WEIGHTS:
            self.rs_raw = self.rs_raw + w * ret(c, n)
        self.fip = info_discreteness(c)
        self.adr = adr_pct(h, lo)
        self.max_gap = max_gap(o, c)
        up = np.zeros(len(c))
        up[1:] = (c[1:] / c[:-1] - 1.0) > JUMP_UP
        roll = ta.rolling_max(up, max(1, min(252, len(c))))
        self.suspect = np.where(np.isnan(roll), np.maximum.accumulate(up) > 0, roll > 0)

    def liquid(self, i: int, min_price: float, min_dollar: float) -> bool:
        return bool(i >= MIN_HISTORY - 1 and self.c[i] >= min_price
                    and self.dollar20[i] >= min_dollar and not self.suspect[i])

    def template(self, i: int, rs: float | None) -> tuple[int, list[str]]:
        """MO-4: Minervini trend template score and the failed items."""
        c, s50, s150, s200 = self.c[i], self.sma50[i], self.sma150[i], self.sma200[i]
        items = {
            "close>SMA150&SMA200": c > s150 and c > s200,
            "SMA150>SMA200": s150 > s200,
            "SMA200 rising": i >= 20 and s200 > self.sma200[i - 20],
            "SMA50>SMA150>SMA200": s50 > s150 > s200,
            "close>SMA50": c > s50,
            "close>=1.3x52w low": c >= 1.30 * self.lo252[i],
            "close>=0.75x52w high": c >= 0.75 * self.hi252[i],
            "RS>=70": rs is not None and rs >= 70,
        }
        failed = [k for k, ok in items.items() if not bool(ok)]
        return 8 - len(failed), failed

    def stage(self, i: int) -> int:
        """Weinstein stage from the 150-session (30-week) average."""
        if i < 170 or math.isnan(self.sma150[i - 20]):
            return 0
        rising = self.sma150[i] > self.sma150[i - 20]
        above = self.c[i] > self.sma150[i]
        if above and rising:
            return 2
        if not above and not rising:
            return 4
        return 3 if rising else 1      # above a falling average: basing; under a rising: topping


def corr(a: MomFrame, b: MomFrame, d: date, n: int = 60) -> float | None:
    """Correlation of daily returns over the n sessions up to d on common dates (MO-17)."""
    ia, ib = a.pos.get(d), b.pos.get(d)
    if ia is None or ib is None or ia < n or ib < n:
        return None
    da = {a.dates[k]: a.c[k] / a.c[k - 1] - 1 for k in range(ia - n + 1, ia + 1)}
    pairs = [(da[b.dates[k]], b.c[k] / b.c[k - 1] - 1) for k in range(ib - n + 1, ib + 1)
             if b.dates[k] in da]
    if len(pairs) < n // 2:
        return None
    x = np.asarray(pairs, dtype=np.float64)
    if x[:, 0].std() == 0 or x[:, 1].std() == 0:
        return None
    return float(np.corrcoef(x[:, 0], x[:, 1])[0, 1])


def crowded(frames: Mapping[str, MomFrame], sym: str, held: Sequence[str], d: date,
            max_correlated: int, threshold: float = 0.7) -> str | None:
    """MO-17: the held names correlated above ``threshold`` with ``sym`` when there are
    already ``max_correlated`` of them, else None."""
    x = frames.get(sym)
    if x is None:
        return None
    close = [h for h in held if h != sym and h in frames and
             (corr(x, frames[h], d) or 0.0) > threshold]
    return ", ".join(close) if len(close) >= max_correlated else None


def _roll_std(x: Arr, n: int) -> Arr:
    out = _nan(len(x))
    if len(x) > n:
        w = np.lib.stride_tricks.sliding_window_view(x, n)
        out[n - 1:] = np.std(w, axis=1, ddof=1)
    return out


def percentile_ranks(values: Mapping[str, float]) -> dict[str, float]:
    """0..1 percentile of each value among the finite ones (ties share the average rank)."""
    items = [(k, v) for k, v in values.items() if v is not None and math.isfinite(v)]
    if not items:
        return {}
    arr = np.asarray([v for _, v in items])
    order = arr.argsort(kind="stable")
    ranks = np.empty(len(arr))
    ranks[order] = np.arange(len(arr))
    for v in np.unique(arr):                       # average rank for ties
        m = arr == v
        if m.sum() > 1:
            ranks[m] = ranks[m].mean()
    denom = max(len(arr) - 1, 1)
    return {k: float(ranks[j] / denom) for j, (k, _) in enumerate(items)}


@dataclass
class Row:
    """One leaderboard row (MO-5)."""

    symbol: str
    asof: date
    close: float
    composite: float
    rank: int
    rs: int
    template: int
    failed: list[str]
    stage: int
    factors: dict[str, float]
    fip: float | None
    adr: float | None
    gap_flag: bool
    above_sma100: bool
    vol60: float | None
    extra: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        d = dict(self.__dict__)
        d["asof"] = self.asof.isoformat()
        return d


def _fin(x: float) -> float | None:
    return None if x is None or not math.isfinite(x) else float(x)


class Panel:
    """All symbols' frames plus the cross-sectional RS rating and composite per date."""

    def __init__(self, frames: Mapping[str, MomFrame], *, min_price: float = 10.0,
                 min_dollar: float = 50e6, rank_factors: Sequence[str] = ("r12_1", "clenow",
                                                                         "high52", "resid")
                 ) -> None:
        self.frames = dict(frames)
        self.min_price, self.min_dollar = min_price, min_dollar
        self.rank_factors = tuple(rank_factors)
        self._rs: dict[date, dict[str, int]] = {}
        self._comp: dict[date, dict[str, float]] = {}

    def eligible(self, d: date) -> dict[str, int]:
        out = {}
        for sym, fr in self.frames.items():
            i = fr.pos.get(d)
            if i is not None and fr.liquid(i, self.min_price, self.min_dollar):
                out[sym] = i
        return out

    def rs(self, d: date) -> dict[str, int]:
        """MO-3: RS rating 1-99 on date d."""
        if d not in self._rs:
            el = self.eligible(d)
            raw = {k: float(self.frames[k].rs_raw[i]) for k, i in el.items()}
            self._rs[d] = {k: max(1, min(99, int(round(p * 98 + 1))))
                           for k, p in percentile_ranks(raw).items()}
        return self._rs[d]

    def composite(self, d: date, factors: Sequence[str] | None = None) -> dict[str, float]:
        """MO-5: average percentile of the rank factors among eligible symbols."""
        fs = tuple(factors or self.rank_factors)
        key = d if factors is None else None
        if key is not None and key in self._comp:
            return self._comp[key]
        el = self.eligible(d)
        acc: dict[str, list[float]] = {k: [] for k in el}
        for f in fs:
            vals = {k: float(self.frames[k].f[f][i]) for k, i in el.items()}
            for k, p in percentile_ranks(vals).items():
                acc[k].append(p)
        out = {k: sum(v) / len(v) for k, v in acc.items() if len(v) == len(fs)}
        if key is not None:
            self._comp[key] = out
        return out

    def leaderboard(self, d: date, n: int | None = None) -> list[Row]:
        comp = self.composite(d)
        rs = self.rs(d)
        ordered = sorted(comp.items(), key=lambda kv: (-kv[1], kv[0]))
        rows = []
        for rank, (sym, val) in enumerate(ordered[:n] if n else ordered, 1):
            fr = self.frames[sym]
            i = fr.pos[d]
            score, failed = fr.template(i, rs.get(sym))
            rows.append(Row(
                symbol=sym, asof=d, close=round(float(fr.c[i]), 2), composite=round(val, 3),
                rank=rank, rs=rs.get(sym, 0), template=score, failed=failed, stage=fr.stage(i),
                factors={f: round(float(fr.f[f][i]), 4) for f in FACTORS
                         if math.isfinite(fr.f[f][i])},
                fip=_fin(round(float(fr.fip[i]), 3)), adr=_fin(round(float(fr.adr[i]), 2)),
                gap_flag=bool(fr.max_gap[i] > 0.15),
                above_sma100=bool(fr.c[i] > fr.sma100[i]),
                vol60=_fin(round(float(fr.vol60[i]), 3))))
        return rows


@dataclass(frozen=True)
class Regime:
    """MO-6."""

    state: str            # ON | CAUTION | OFF | UNKNOWN
    size_mult: float
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {"state": self.state, "size_mult": self.size_mult, "reasons": list(self.reasons)}


def regime(spy: Series | None, d: date, macro_label: str | None = None) -> Regime:
    if spy is None:
        return Regime("UNKNOWN", 0.5, ("no SPY bars",))
    n = sum(1 for x in spy.dates if x <= d)
    if n < 253:
        return Regime("UNKNOWN", 0.5, ("under 253 SPY sessions",))
    c = spy.c[:n]
    s200 = float(np.mean(c[-200:]))
    last = float(c[-1])
    if last < s200:
        return Regime("OFF", 0.0, (f"SPY {last:.2f} under its 200-day average {s200:.2f}",))
    r252 = last / float(c[-253]) - 1
    r21 = last / float(c[-22]) - 1
    why = [f"SPY {last:.2f} above its 200-day average {s200:.2f}"]
    caution = False
    if r252 < 0 and r21 > 0.05:
        why.append(f"crash guard: 12-month return {r252:+.1%} with a {r21:+.1%} one-month "
                   "rebound (Daniel & Moskowitz)")
        caution = True
    if macro_label == "RISK_OFF":
        why.append("macro regime RISK_OFF (A18)")
        caution = True
    return Regime("CAUTION", 0.5, tuple(why)) if caution else Regime("ON", 1.0, tuple(why))
