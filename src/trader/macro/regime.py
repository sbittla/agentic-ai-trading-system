"""Macro regime and release surprises (MA-3, MA-5, MA-6), plus macro-news themes (MA-4).

Fixed, published rules: each indicator says risk-on (+1), neutral (0) or risk-off (-1) with
its value and threshold. Everything is computed from observations dated before the as-of day.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Any

import numpy as np

from trader.scanner.techniques import Series

Obs = Sequence[tuple[date, float]]

THEMES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("FED", re.compile(r"\b(fed|fomc|powell|federal reserve|rate decision|monetary policy|"
                       r"dot plot|minutes)\b", re.I)),
    ("INFLATION", re.compile(r"\b(inflation|cpi|pce|prices? index|core prices|disinflation)\b",
                             re.I)),
    ("JOBS", re.compile(r"\b(jobs|payrolls?|unemployment|jobless|labor market|hiring|layoffs)\b",
                        re.I)),
    ("RATES", re.compile(r"\b(treasur(y|ies)|yields?|bond market|10-year|2-year|curve)\b",
                         re.I)),
    ("CREDIT", re.compile(r"\b(credit|spreads?|default|junk|high-yield|bank(ing)? (stress|"
                          r"failure))\b", re.I)),
    ("TRADE", re.compile(r"\b(tariffs?|trade (war|deal|talks)|export (controls|ban)|imports?)\b",
                         re.I)),
    ("GEOPOLITICS", re.compile(r"\b(war|sanctions|missiles?|invasion|conflict|ceasefire|taiwan|"
                               r"ukraine|russia|middle east|israel\w*|iran|gaza|west bank|"
                               r"houthis?|attacks?|air ?strikes?|coalition|troops)\b", re.I)),
    ("FISCAL", re.compile(r"\b(shutdown|debt ceiling|deficit|budget|stimulus|tax (bill|cut))\b",
                          re.I)),
    ("ENERGY", re.compile(r"\b(oil|opec|crude|natural gas|gasoline)\b", re.I)),
    ("GROWTH", re.compile(r"\b(gdp|recession|growth|retail sales|manufacturing|ism|pmi|"
                          r"consumer (spending|sentiment|confidence))\b", re.I)),
)
RISK_OFF_RE = re.compile(r"\b(fear gauge|volatility (jumps|spikes|surges)|vix (jumps|spikes|"
                         r"surges)|missiles?|attacks?|air ?strikes?|crushed|"
                         r"hawkish|hike|hot(ter)?|surge in (prices|inflation)|recession|"
                         r"slump|plunge|selloff|sell-off|crisis|default|war|escalat\w*|tariffs?|"
                         r"weak(er|ens)?|layoffs|contraction|stress)\b", re.I)
RISK_ON_RE = re.compile(r"\b(dovish|cut(s)? rates|rate cut|cool(s|ing|er)|eas(es|ing)|soft "
                        r"landing|rally|record high|strong(er)? (jobs|growth)|ceasefire|deal "
                        r"reached|beat)\b", re.I)


def theme(headline: str, text: str = "") -> str:
    t = f"{headline} {text[:300]}"
    return next((name for name, rx in THEMES if rx.search(t)), "OTHER")


def tone(headline: str, sentiment: float) -> str:
    """MA-4: words first (macro FinBERT polarity is not risk direction), then FinBERT."""
    off, on = len(RISK_OFF_RE.findall(headline)), len(RISK_ON_RE.findall(headline))
    if off > on:
        return "risk-off"
    if on > off:
        return "risk-on"
    return "risk-on" if sentiment > 0.4 else "risk-off" if sentiment < -0.4 else "neutral"


# --- series helpers ------------------------------------------------------------------------

def upto(obs: Obs, asof: date) -> list[tuple[date, float]]:
    return [(d, v) for d, v in obs if d < asof]


def last(obs: Obs) -> float | None:
    return obs[-1][1] if obs else None


def value_ago(obs: Obs, days: int) -> float | None:
    if not obs:
        return None
    cut = obs[-1][0] - timedelta(days=days)
    prior = [v for d, v in obs if d <= cut]
    return prior[-1] if prior else None


def yoy(obs: Obs) -> float | None:
    a, b = last(obs), value_ago(obs, 365)
    return (a / b - 1) * 100 if a is not None and b else None


def annualised_3m(obs: Obs) -> float | None:
    a, b = last(obs), value_ago(obs, 91)
    return ((a / b) ** 4 - 1) * 100 if a is not None and b else None


def sahm(unrate: Obs) -> float | None:
    """Sahm rule: 3-month average unemployment minus its low of the prior 12 months."""
    if len(unrate) < 15:
        return None
    v = [x for _, x in unrate]
    ma3 = [sum(v[i - 2:i + 1]) / 3 for i in range(2, len(v))]
    return round(ma3[-1] - min(ma3[-13:-1]), 2)


def release_surprise(obs: Obs, n: int = 6) -> dict[str, Any] | None:
    """MA-3: the latest change vs the mean change of the prior ``n`` (vs trend, not
    consensus), in standard deviations."""
    if len(obs) < n + 2:
        return None
    ch = [b[1] - a[1] for a, b in zip(obs, obs[1:], strict=False)]
    latest, prior = ch[-1], ch[-n - 1:-1]
    m = sum(prior) / n
    sd = (sum((x - m) ** 2 for x in prior) / n) ** 0.5
    return {"date": obs[-1][0].isoformat(), "value": obs[-1][1], "change": round(latest, 4),
            "trend_change": round(m, 4),
            "surprise_sd": round((latest - m) / sd, 2) if sd > 0 else None,
            "basis": "vs trend of the prior 6 releases, not consensus"}


def distribution_days(spy: Series | None, i: int | None = None, window: int = 25) -> int | None:
    """TS-22: sessions in the last ``window`` that closed down >= 0.2% on higher volume than
    the session before."""
    if spy is None:
        return None
    end = len(spy.c) - 1 if i is None else i
    if end < window + 1:
        return None
    n = 0
    for k in range(end - window + 1, end + 1):
        if spy.c[k] / spy.c[k - 1] - 1 <= -0.002 and spy.v[k] > spy.v[k - 1]:
            n += 1
    return n


# --- regime --------------------------------------------------------------------------------

@dataclass
class Indicator:
    key: str
    name: str
    value: float | None
    signal: int            # +1 risk-on, 0 neutral, -1 risk-off
    detail: str


@dataclass
class Regime:
    asof: date
    score: float
    label: str
    indicators: list[Indicator] = field(default_factory=list)
    releases: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["asof"] = self.asof.isoformat()
        return d


def _ind(key: str, name: str, value: float | None, signal: int, detail: str) -> Indicator:
    return Indicator(key, name, None if value is None else round(value, 3), signal, detail)


def indicators(series: Mapping[str, Obs], spy: Series | None, asof: date) -> list[Indicator]:
    """MA-5."""
    s = {k: upto(v, asof) for k, v in series.items()}
    out: list[Indicator] = []
    v = last(s.get("T10Y3M", []))
    if v is not None:
        out.append(_ind("curve", "Yield curve 10y-3m", v, -1 if v < 0 else 0,
                        "inverted: a classic recession warning" if v < 0 else "positive slope"))
    hy = s.get("BAMLH0A0HYM2", [])
    if len(hy) > 200:
        cur = hy[-1][1]
        med = float(np.median([x for d, x in hy if d >= hy[-1][0] - timedelta(days=365)]))
        sig = -1 if cur > med + 0.5 else 1 if cur < med - 0.3 else 0
        out.append(_ind("credit", "High-yield spread", cur, sig,
                        f"{cur:.2f} vs 1-year median {med:.2f}"))
    vix = last(s.get("VIXCLS", []))
    if vix is not None:
        out.append(_ind("vix", "VIX", vix, -1 if vix > 25 else 1 if vix < 15 else 0,
                        "stress" if vix > 25 else "calm" if vix < 15 else "normal"))
        v3 = last(s.get("VXVCLS", []))
        if v3:
            ratio = vix / v3
            out.append(_ind("vix_term", "VIX / VIX3M", ratio, -1 if ratio > 1 else 0,
                            "inverted term structure: near-term fear" if ratio > 1
                            else "normal term structure"))
    sm = sahm(s.get("UNRATE", []))
    if sm is not None:
        out.append(_ind("sahm", "Sahm rule", sm, -1 if sm >= 0.5 else 0,
                        "recession signal" if sm >= 0.5 else "labour market not triggered"))
    core = s.get("CPILFESL", [])
    a3, y = annualised_3m(core), yoy(core)
    if a3 is not None and y is not None:
        hot = a3 > y + 0.5
        out.append(_ind("inflation", "Core CPI trend", a3, -1 if hot else 0,
                        f"3-month annualised {a3:.1f}% vs 12-month {y:.1f}%"
                        + (": re-accelerating" if hot else "")))
    for key, sid, name, cut in (("dollar", "DTWEXBGS", "Dollar 63-day change", 4.0),
                                ("oil", "DCOILWTICO", "Oil 63-day change", 25.0)):
        obs = s.get(sid, [])
        a, b = last(obs), value_ago(obs, 91)
        if a is not None and b:
            ch = (a / b - 1) * 100
            out.append(_ind(key, name, ch, -1 if ch > cut else 0, f"{ch:+.1f}%"))
    if spy is not None:
        idx = [k for k, d in enumerate(spy.dates) if d < asof]
        if len(idx) >= 200:
            i = idx[-1]
            sma = float(np.mean(spy.c[i - 199:i + 1]))
            up = float(spy.c[i]) > sma
            out.append(_ind("trend", "SPY vs 200-day", float(spy.c[i] / sma - 1) * 100,
                            1 if up else -1, "above" if up else "below"))
            dd = distribution_days(spy, i)
            if dd is not None:
                out.append(_ind("distribution", "Distribution days (25)", float(dd),
                                -1 if dd >= 5 else 0,
                                f"{dd} down-on-volume sessions" + (": institutions selling"
                                                                    if dd >= 5 else "")))
    return out


def regime(series: Mapping[str, Obs], spy: Series | None, asof: date,
           risk_on_at: float = 25, risk_off_at: float = -25) -> Regime:
    """MA-6."""
    ind = indicators(series, spy, asof)
    score = round(100 * sum(x.signal for x in ind) / len(ind), 1) if ind else 0.0
    label = "RISK_ON" if score >= risk_on_at else "RISK_OFF" if score <= risk_off_at \
        else "NEUTRAL"
    rel = {}
    for sid in ("CPILFESL", "CPIAUCSL", "PCEPILFE", "UNRATE", "PAYEMS", "ICSA", "INDPRO",
                "RSAFS", "UMCSENT"):
        r = release_surprise(upto(series.get(sid, []), asof))
        if r is not None:
            rel[sid] = r
    return Regime(asof, score, label, ind, rel)


def evaluate(series: Mapping[str, Obs], spy: Series, start: date | None = None,
             step: int = 5, risk_on_at: float = 25, risk_off_at: float = -25,
             horizon: int = 21) -> dict[str, Any]:
    """MA-9: what followed each regime label: SPY's next-``horizon`` return and realized vol."""
    by: dict[str, list[tuple[float, float]]] = {}
    for i in range(250, len(spy.c) - horizon, step):
        d = spy.dates[i]
        if start and d < start:
            continue
        lab = regime(series, spy, d + timedelta(days=1), risk_on_at, risk_off_at).label
        ret = float(spy.c[i + horizon] / spy.c[i] - 1) * 100
        r = np.diff(np.log(spy.c[i:i + horizon + 1]))
        by.setdefault(lab, []).append((ret, float(np.std(r)) * math.sqrt(252) * 100))
    out = {}
    for lab, xs in sorted(by.items()):
        a = np.asarray(xs)
        out[lab] = {"n": len(xs), "avg_fwd_return_pct": round(float(a[:, 0].mean()), 2),
                    "pct_up": round(float((a[:, 0] > 0).mean()), 3),
                    "avg_realized_vol_pct": round(float(a[:, 1].mean()), 1)}
    useful = len(out) >= 2 and max(v["avg_realized_vol_pct"] for v in out.values()) - min(
        v["avg_realized_vol_pct"] for v in out.values()) >= 3
    return {"by_label": out, "separates_volatility": useful, "horizon": horizon,
            "note": "Samples every 5 sessions overlap, so counts overstate independence."}
