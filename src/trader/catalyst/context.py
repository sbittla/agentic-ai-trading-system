"""Confluence panel: independent checks that support or argue against the news (NC-16).

Each check is a plain number with a verdict relative to the catalyst's direction:
``supports``, ``against`` or ``neutral``. Nothing here is weighted into the outlook unless
catalyst-eval shows it helps (NC-33); the panel is for the trader's judgement.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

from trader.scanner import ta
from trader.scanner.techniques import Series

Check = dict[str, Any]


def _v(sign: float, direction: str, dead: float = 0.0) -> str:
    """Verdict for a signed reading: positive supports a positive catalyst."""
    if direction == "mixed" or abs(sign) <= dead:
        return "neutral"
    return "supports" if (sign > 0) == (direction == "positive") else "against"


def _c(group: str, name: str, value: Any, verdict: str, detail: str) -> Check:
    return {"group": group, "name": name, "value": value, "verdict": verdict, "detail": detail}


def technical(s: Series, spy: Series | None, i: int, direction: str) -> list[Check]:
    out: list[Check] = []
    c = s.c[: i + 1]
    if len(c) < 60:
        return out
    sma50, sma200 = ta.sma(c, 50), ta.sma(c, 200)
    last = float(c[-1])
    up50 = last > sma50[-1]
    trend = 1.0 if up50 and (np.isnan(sma200[-1]) or last > sma200[-1]) else \
        -1.0 if not up50 else 0.0
    out.append(_c("Technical", "Trend", "up" if trend > 0 else "down" if trend < 0 else "mixed",
                  _v(trend, direction), f"close {last:.2f} vs SMA50 {sma50[-1]:.2f}"
                  + ("" if np.isnan(sma200[-1]) else f", SMA200 {sma200[-1]:.2f}")))
    if spy is not None and i >= 63:
        pos = {d: k for k, d in enumerate(spy.dates)}
        d0, d1 = s.dates[i - 63], s.dates[i]
        if d0 in pos and d1 in pos:
            rs = (float(c[-1] / c[-64]) - float(spy.c[pos[d1]] / spy.c[pos[d0]])) * 100
            out.append(_c("Technical", "Relative strength 63d", round(rs, 1), _v(rs, direction, 2),
                          f"{rs:+.1f} pts vs SPY over 3 months"))
    v50 = float(np.mean(s.v[max(0, i - 50):i])) if i >= 10 else 0.0
    if v50 > 0:
        rvol = float(s.v[i] / v50)
        out.append(_c("Technical", "Relative volume", round(rvol, 2),
                      "supports" if rvol >= 1.5 and direction != "mixed" else "neutral",
                      f"{rvol:.1f}x the 50-day average"))
    atr = ta.atr(s.h[: i + 1], s.l[: i + 1], c, 14)
    ema20 = ta.ema(c, 20)
    if not np.isnan(atr[-1]) and atr[-1] > 0:
        ext = (last - float(ema20[-1])) / float(atr[-1])
        out.append(_c("Technical", "Stretch vs EMA20", round(ext, 2),
                      "against" if abs(ext) >= 3 and _v(ext, direction) == "supports"
                      else "neutral", f"{ext:+.1f} ATR from EMA20"))
    adx, _, _ = ta.adx(s.h[: i + 1], s.l[: i + 1], c, 14)
    if not np.isnan(adx[-1]):
        out.append(_c("Technical", "ADX", round(float(adx[-1]), 1), "neutral",
                      "trending" if adx[-1] >= 25 else "no strong trend"))
    return out


def day0(s: Series, i: int, direction: str) -> list[Check]:
    if i < 1:
        return []
    gap = float(s.o[i] / s.c[i - 1] - 1) * 100
    rng = float(s.h[i] - s.l[i])
    loc = float((s.c[i] - s.l[i]) / rng) if rng > 0 else 0.5
    body = float(s.c[i] - s.o[i])
    out = [_c("Day-0 reaction", "Gap", round(gap, 2), _v(gap, direction, 0.3),
              f"opened {gap:+.2f}% vs the prior close"),
           _c("Day-0 reaction", "Close location", round(loc, 2),
              _v(loc - 0.5, direction, 0.2),
              "closed near the high" if loc >= 0.7 else "closed near the low" if loc <= 0.3
              else "closed mid-range")]
    if abs(gap) >= 1 and np.sign(body) != np.sign(gap):
        out.append(_c("Day-0 reaction", "Gap faded", True, "against",
                      "the opening gap was sold into (or bought back) during the day"))
    return out


def options(iv: Mapping[str, Any] | None) -> list[Check]:
    if not iv:
        return []
    out = []
    rank = iv.get("iv_rank")
    if rank is not None:
        out.append(_c("Options", "IV rank", round(float(rank), 0), "neutral",
                      "options cheap" if rank < 30 else "options expensive" if rank > 60
                      else "options fairly priced"))
    em = iv.get("expected_move_30d")
    if em:
        out.append(_c("Options", "Expected move 30d", round(float(em), 2), "neutral",
                      f"the options price a ±{float(em):.2f} move over 30 days"))
    return out


def analysts(actions: Sequence[Mapping[str, Any]], recs: Sequence[Mapping[str, Any]] | None,
             last: float, direction: str) -> list[Check]:
    out: list[Check] = []
    ups = sum(1 for a in actions if str(a.get("action", "")).lower() in ("up", "upgrade"))
    downs = sum(1 for a in actions if str(a.get("action", "")).lower() in ("down", "downgrade"))
    pts = [float(a["pt_new"]) for a in actions if a.get("pt_new")]
    if ups or downs:
        out.append(_c("Analysts", "Rating changes 30d", f"{ups} up / {downs} down",
                      _v(ups - downs, direction), "upgrades minus downgrades"))
    if pts and last > 0:
        gap = (float(np.mean(pts)) / last - 1) * 100
        out.append(_c("Analysts", "Target vs price", round(gap, 1), _v(gap, direction, 5),
                      f"recent targets average {np.mean(pts):.2f} ({gap:+.1f}%)"))
    if recs and len(recs) >= 2:
        def bull(r: Mapping[str, Any]) -> float:
            tot = sum(float(r.get(k) or 0) for k in ("strongBuy", "buy", "hold", "sell",
                                                     "strongSell"))
            return (float(r.get("strongBuy") or 0) + float(r.get("buy") or 0)) / tot \
                if tot else 0.0
        delta = (bull(recs[0]) - bull(recs[1])) * 100
        out.append(_c("Analysts", "Consensus trend", round(delta, 1), _v(delta, direction, 1),
                      f"buy share {bull(recs[0]):.0%} ({delta:+.1f} pts vs last month)"))
    return out


def earnings(history: Sequence[Mapping[str, Any]] | None, sessions_to_next: int | None,
             direction: str) -> list[Check]:
    out: list[Check] = []
    if history:
        sur = [float(h["surprisePercent"]) for h in history[:4]
               if h.get("surprisePercent") is not None]
        if sur:
            beats = sum(1 for x in sur if x > 0)
            out.append(_c("Earnings", "Last surprises", f"{beats}/{len(sur)} beats",
                          _v(sur[0], direction, 1), f"latest {sur[0]:+.1f}% vs estimate"))
    if sessions_to_next is not None:
        out.append(_c("Earnings", "Next report", sessions_to_next,
                      "against" if sessions_to_next <= 10 else "neutral",
                      f"in {sessions_to_next} sessions" + (" (binary risk inside 2 weeks)"
                                                           if sessions_to_next <= 10 else "")))
    return out


def insiders(ins: Mapping[str, Any] | None, direction: str) -> list[Check]:
    if not ins:
        return []
    buys, sells = int(ins.get("buys", 0)), int(ins.get("sells", 0))
    net = float(ins.get("net_value", 0.0))
    v = "supports" if buys >= 2 and direction == "positive" else \
        "against" if buys >= 2 and direction == "negative" else "neutral"
    return [_c("Insiders", "Open-market trades 90d", f"{buys} buys / {sells} sells", v,
               f"net ${net:,.0f}; routine selling is common, cluster buying is the signal")]


def short_interest(sv: Mapping[str, Any] | None, direction: str) -> list[Check]:
    if not sv or sv.get("ratio_5d") is None:
        return []
    r5, r20 = float(sv["ratio_5d"]), float(sv.get("ratio_20d") or sv["ratio_5d"])
    delta = (r5 - r20) * 100
    return [_c("Short activity", "Short volume share 5d", round(r5 * 100, 1),
               _v(-delta, direction, 2),
               f"{r5:.0%} of volume sold short (20-day {r20:.0%}); FINRA Reg SHO daily")]


def regime(spy: Series | None, sector: Series | None, d_index_spy: int | None,
           macro_events: Sequence[str], direction: str) -> list[Check]:
    out: list[Check] = []
    if spy is not None and d_index_spy is not None and d_index_spy >= 50:
        sm = ta.sma(spy.c[: d_index_spy + 1], 50)
        up = float(spy.c[d_index_spy]) > float(sm[-1])
        out.append(_c("Regime", "Market (SPY vs SMA50)", "up" if up else "down",
                      _v(1 if up else -1, direction), "good stock news fights a falling market"
                      if not up else "market trend up"))
    if sector is not None and len(sector.c) >= 50:
        sm = ta.sma(sector.c, 50)
        up = float(sector.c[-1]) > float(sm[-1])
        out.append(_c("Regime", f"Sector ({sector.symbol})", "up" if up else "down",
                      _v(1 if up else -1, direction), "sector ETF vs its SMA50"))
    if macro_events:
        out.append(_c("Regime", "Macro events in horizon", ", ".join(macro_events), "neutral",
                      "scheduled releases can swamp single-stock news"))
    return out


def tally(checks: Sequence[Check]) -> dict[str, int]:
    return {k: sum(1 for c in checks if c["verdict"] == k)
            for k in ("supports", "against", "neutral")}
