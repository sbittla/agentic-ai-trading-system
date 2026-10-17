"""A20: the corporate-event report (EV-6, EV-7). Reads the calendar and the SIP daily bars,
studies every group, and turns patterns into dated ideas for the events ahead. Advisory: no
orders, long stock only (ADR-006, ADR-008)."""

from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from trader.calendar import MarketCalendar
from trader.config import Config
from trader.events.study import (
    WINDOWS,
    Group,
    baseline,
    ideas_for,
    load_groups,
    observe,
    reaction_day,
    study,
)
from trader.scanner import ta
from trader.scanner.techniques import Series
from trader.timeutil import to_et

SeriesLoader = Callable[[Sequence[str], date], Awaitable[dict[str, Series]]]


class EventsAgent:
    def __init__(self, cfg: Config, load: SeriesLoader, now: Callable[[], datetime], *,
                 cal: MarketCalendar | None = None, base: Path | None = None) -> None:
        self.cfg = cfg
        self.e = cfg.events
        self.load = load
        self.now = now
        self.cal = cal or MarketCalendar()
        self.base = base or Path.cwd()

    def gate(self) -> dict[str, Any]:
        e = self.e
        return {"min_years": e.min_years, "min_hit": e.min_hit, "max_p": e.max_p,
                "min_move_pct": e.min_move_pct}

    async def run(self, *, write: bool = True) -> dict[str, Any]:
        e = self.e
        groups = load_groups(self.base / e.file)
        today = to_et(self.now()).date()
        tickers = sorted({g.ticker for g in groups})
        series = await self.load([*tickers, "SPY"], today)
        spy = series.pop("SPY", None)
        if spy is None:
            return {"error": "no SPY bars: run momentum-history"}
        obs: dict[str, list[dict[str, Any]]] = {}
        lengths = (e.pre_sessions, 1, e.post_sessions)
        bases: dict[str, dict[int, float]] = {}
        for g in groups:
            s = series.get(g.ticker)
            if s is None:
                obs[g.id] = []
                continue
            if g.ticker not in bases:       # EV-4: over the span of all the ticker's events
                days = [d.day for x in groups if x.ticker == g.ticker for d in x.dates]
                bases[g.ticker] = baseline(s, spy, lengths, min(days, default=None),
                                           max(days, default=None))
            rows = [observe(s, spy, d, e.pre_sessions, e.post_sessions, bases[g.ticker])
                    for d in g.dates]
            obs[g.id] = [r for r in rows if r is not None]
        fam: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for g in groups:
            fam[g.family] += [{**r, "group": g.id} for r in obs[g.id]]
        fam_study = {f: study(sorted(v, key=lambda r: r["date"]), **self.gate())
                     for f, v in fam.items() if len({r["group"] for r in v}) > 1}
        out_groups: list[dict[str, Any]] = []
        for g in groups:
            st = study(obs[g.id], **self.gate())
            out_groups.append({"id": g.id, "ticker": g.ticker, "name": g.name,
                               "family": g.family, "listed": len(g.dates),
                               "study": st, "observations": obs[g.id],
                               "missing_bars": g.ticker not in series})
        by_id = {x["id"]: x for x in out_groups}
        upcoming: list[dict[str, Any]] = []
        ideas: list[dict[str, Any]] = []
        horizon = today + timedelta(days=e.lookahead_days)
        for g in groups:
            for ev in g.upcoming:
                r = reaction_day(self.cal, ev)
                if not today - timedelta(days=e.post_sessions * 2) <= ev.day <= horizon:
                    continue
                bst, basis = self._basis(g, by_id[g.id]["study"], fam_study)
                found = ideas_for(g, ev, bst, basis, today, self.cal, e.pre_sessions,
                                  e.post_sessions) if bst else []
                ctx = context(series.get(g.ticker))
                for i in found:
                    if i["side"] == "buy" and ctx.get("atr"):
                        i["stop_hint"] = round(ctx["close"] - 2 * ctx["atr"], 2)
                ideas += found
                upcoming.append({
                    "group": g.id, "ticker": g.ticker, "event": g.name,
                    "event_date": ev.day.isoformat(), "estimated": ev.estimated,
                    "reaction_day": r.isoformat(),
                    "sessions_away": self.cal.trading_days_between(today, r) if r > today
                    else -self.cal.trading_days_between(r, today),
                    "basis": basis, "context": ctx, "ideas": [i["kind"] for i in found],
                    "summary": {w: _brief(bst.get(w)) for w in WINDOWS} if bst else None})
        upcoming.sort(key=lambda u: u["event_date"])
        ideas.sort(key=lambda i: (i["status"] == "passed", i["enter"], i["ticker"]))
        tested = sum(1 for x in out_groups for w in WINDOWS
                     if x["study"][w].get("n", 0) >= e.min_years)
        found_n = sum(1 for x in out_groups for w in WINDOWS if x["study"][w].get("pattern"))
        payload = {
            "generated": self.now().isoformat(), "asof": today.isoformat(),
            "windows": {"pre_sessions": e.pre_sessions, "post_sessions": e.post_sessions},
            "gate": self.gate(), "tests": tested, "patterns": found_n,
            "expected_false": round(tested * e.max_p, 1),
            "baselines": {k: {str(n): round(v, 2) for n, v in b.items()}
                          for k, b in bases.items()},
            "upcoming": upcoming, "ideas": ideas, "groups": out_groups,
            "families": fam_study}
        if write:
            p = Path(e.latest_file)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")
        return payload

    def _basis(self, g: Group, st: Mapping[str, Any], fam: Mapping[str, Any]
               ) -> tuple[dict[str, Any] | None, str]:
        """EV-5: the group's own history, else its family's, else none."""
        n = min((st[w].get("n", 0) for w in WINDOWS), default=0)
        if n >= self.e.min_years:
            return dict(st), "group"
        f = fam.get(g.family)
        if f is not None and min(f[w].get("n", 0) for w in WINDOWS) >= self.e.min_years:
            return dict(f), f"family {g.family}"
        return None, f"too few past events ({n} < {self.e.min_years})"


def context(s: Series | None) -> dict[str, Any]:
    """Trend context: the last close, 200-day average and ATR14."""
    if s is None or len(s.c) < 200:
        return {}
    atr = float(ta.atr(s.h, s.l, s.c, 14)[-1])
    sma = float(np.mean(s.c[-200:]))
    c = float(s.c[-1])
    return {"asof": s.dates[-1].isoformat(), "close": round(c, 2), "sma200": round(sma, 2),
            "above_sma200": c > sma, "atr": round(atr, 2) if math.isfinite(atr) else None}


def _brief(w: Mapping[str, Any] | None) -> str:
    if not w or not w.get("n"):
        return "no data"
    tag = f" PATTERN {w['pattern'].upper()}" if w.get("pattern") else ""
    return (f"{w['mean']:+.1f}%, {w['up_rate']:.0%} up of {w['n']}, "
            f"p {w['p']:.2f}{tag}")


WINDOW_LABEL = {"run_up": "into", "reaction": "event", "drift": "after"}


def render(payload: Mapping[str, Any], *, ticker: str | None = None) -> str:
    if payload.get("error"):
        return f"Events: {payload['error']}"
    w = payload["windows"]
    lines = [f"Company events · {payload['asof']} · windows: {w['pre_sessions']} sessions "
             f"into, the event session, {w['post_sessions']} after (moves net of SPY and of "
             "the stock's usual move over as many sessions)"]
    ideas = [i for i in payload["ideas"] if i["status"] != "passed"
             and (ticker is None or i["ticker"] == ticker)]
    if ideas:
        lines.append("IDEAS:")
        for i in ideas:
            est = " (date estimated)" if i["estimated"] else ""
            stop = f" Stop hint {i['stop_hint']} (2 ATR)." if i.get("stop_hint") else ""
            lines.append(
                f"  {i['ticker']} {i['kind']} · {i['event']} {i['event_date']}{est} · "
                f"{i['action']}.{stop} History ({i['basis']}): {i['mean_pct']:+.1f}% beyond "
                f"the usual, same way in {i['same_sign_rate']:.0%} of {i['n']} events, "
                f"p {i['p']:.2f}"
                f"{' · window open' if i['status'] == 'open' else ''}")
    else:
        lines.append("IDEAS: none (no upcoming event has a consistent past pattern)")
    ups = [u for u in payload["upcoming"] if ticker is None or u["ticker"] == ticker]
    if ups:
        lines.append("UPCOMING:")
        for u in ups:
            est = " est." if u["estimated"] else ""
            ctx = u["context"]
            trend = (f" · {'above' if ctx['above_sma200'] else 'BELOW'} its 200-day "
                     f"{ctx['sma200']}" if ctx else "")
            lines.append(f"  {u['event_date']}{est} {u['ticker']} {u['event']} "
                         f"(reacts {u['reaction_day']}, {u['sessions_away']} sessions){trend}")
            if u["summary"]:
                lines.append("    " + " | ".join(f"{WINDOW_LABEL[k]}: {v}"
                                                 for k, v in u["summary"].items()))
            else:
                lines.append(f"    {u['basis']}")
    lines.append(f"{payload['patterns']} patterns in {payload['tests']} tests; about "
                 f"{payload['expected_false']} could be chance at p <= "
                 f"{payload['gate']['max_p']}. A pattern is a tendency over a handful of events, "
                 "not a proven edge: size small.")
    return "\n".join(lines)
