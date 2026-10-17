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
from sqlalchemy import select

from trader.calendar import MarketCalendar
from trader.config import Config
from trader.events.study import (
    BASE_SESSIONS,
    WINDOWS,
    Group,
    ideas_for,
    load_earnings,
    load_groups,
    observe,
    reaction_day,
    study,
)
from trader.journal import schema as js
from trader.scanner import ta
from trader.scanner.techniques import Series
from trader.timeutil import to_et

SeriesLoader = Callable[[Sequence[str], date], Awaitable[dict[str, Series]]]
FINNHUB_HORIZON = 60        # days the Finnhub earnings calendar covers (NC-16)
CALENDAR_MAX_AGE = 4        # days: an older calendar is stale (as MO-18)


class EventsAgent:
    def __init__(self, cfg: Config, load: SeriesLoader, now: Callable[[], datetime], *,
                 cal: MarketCalendar | None = None, base: Path | None = None,
                 journal: Any = None) -> None:
        self.cfg = cfg
        self.journal = journal
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
        for g in groups:                    # EV-4: a trailing baseline per event
            s = series.get(g.ticker)
            rows = [observe(s, spy, d, e.pre_sessions, e.post_sessions,
                            base_sessions=BASE_SESSIONS) for d in g.dates] if s is not None else []
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
        earn, known_until, earn_source = self.earnings(today)
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
                for i in found:
                    self._earnings_check(i, earn, known_until)
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
            "earnings": {"source": earn_source,
                         "known_until": known_until.isoformat() if known_until else None},
            "upcoming": upcoming, "ideas": ideas, "groups": out_groups,
            "families": fam_study}
        if self.journal is not None:                                    # EV-9
            payload["ledger"] = self.record(ideas, series, spy, today)
        if write:
            p = Path(e.latest_file)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")
        return payload

    def earnings(self, today: date) -> tuple[dict[str, list[date]], date | None, str]:
        """EV-10: earnings dates per ticker and the date up to which the calendar is complete.
        The journal's Finnhub calendar (fresh within 4 days, covering 60) wins; the calendar
        file's hand-entered ``earnings:`` dates fill in for runs without it."""
        manual = load_earnings(self.base / self.e.file)
        out = {k: list(v) for k, v in manual.items()}
        if self.journal is not None:
            f = js.fundamentals_daily.c
            try:
                rows = self.journal.query(select(f.ticker, f.date, f.json).where(
                    f.kind == "earnings_calendar", f.date <= today,
                    f.date >= today - timedelta(days=CALENDAR_MAX_AGE)).order_by(f.date))
            except Exception:   # noqa: BLE001 - no table: fall back to the file
                rows = []
            if rows:
                for r in rows:
                    d = (r["json"] or {}).get("date")
                    if d:
                        out.setdefault(r["ticker"], []).append(date.fromisoformat(str(d)))
                last = max(r["date"] for r in rows)
                return out, last + timedelta(days=FINNHUB_HORIZON), "finnhub"
        return out, None, "file" if manual else "none"

    @staticmethod
    def _earnings_check(i: dict[str, Any], earn: Mapping[str, list[date]],
                        known_until: date | None) -> None:
        """EV-10: a buy idea whose window holds an earnings report is blocked (no past window
        held one, so the history does not apply); with no calendar covering the window it is
        unknown. Neither is sent."""
        a, b = date.fromisoformat(i["enter"]), date.fromisoformat(i["exit"])
        dates = earn.get(i["ticker"], [])
        hit = next((d for d in dates if a <= d <= b), None)
        i["earnings_date"] = hit.isoformat() if hit else None
        if i["side"] != "buy":
            i["verdict"] = "ok"
        elif hit is not None:
            i["verdict"] = "earnings_in_window"
        elif (known_until is not None and b <= known_until) or any(d > b for d in dates):
            i["verdict"] = "ok"
        else:
            i["verdict"] = "earnings_unknown"

    def record(self, ideas: Sequence[Mapping[str, Any]], series: Mapping[str, Series],
               spy: Series, today: date) -> dict[str, Any]:
        """EV-9: keep every idea (first sighting kept, fields refreshed until graded) and grade
        the ones whose window has closed: the return from the close of ``enter`` to the close
        of ``exit``, raw and net of SPY."""
        t = js.event_ideas
        now = self.now()
        for i in ideas:
            iid = f"{i['group']}:{i['event_date']}:{i['window']}"
            vals = {"group_id": i["group"], "ticker": i["ticker"], "kind": i["kind"],
                    "side": i["side"], "window": i["window"],
                    "event_date": date.fromisoformat(i["event_date"]),
                    "estimated": i["estimated"], "enter": date.fromisoformat(i["enter"]),
                    "exit": date.fromisoformat(i["exit"]), "verdict": i.get("verdict"),
                    "earnings_date": date.fromisoformat(i["earnings_date"])
                    if i.get("earnings_date") else None, "basis": i["basis"][:48],
                    "n": i["n"], "mean_pct": i["mean_pct"],
                    "same_sign_rate": i["same_sign_rate"], "p": i["p"], "updated": now}
            have = self.journal.query(select(t.c.graded).where(t.c.idea_id == iid))
            if not have:
                self.journal.insert(t, {"idea_id": iid, "first_seen": today, **vals})
            elif have[0]["graded"] is None:
                self.journal.update(t, {"idea_id": iid}, vals)
        spy_c = dict(zip(spy.dates, spy.c, strict=True))
        for r in self.journal.query(select(t).where(t.c.graded.is_(None), t.c.exit < today)):
            x = series.get(r["ticker"])
            if x is None:
                continue
            c = dict(zip(x.dates, x.c, strict=True))
            a, b = r["enter"], r["exit"]
            if a in c and b in c and a in spy_c and b in spy_c:
                raw = c[b] / c[a] - 1
                self.journal.update(t, {"idea_id": r["idea_id"]}, {
                    "realized_raw_pct": round(raw * 100, 2),
                    "realized_pct": round((raw - (spy_c[b] / spy_c[a] - 1)) * 100, 2),
                    "graded": today})
        done = self.journal.query(select(t).where(t.c.graded.is_not(None)))
        buys = [r for r in done if r["side"] == "buy" and r["verdict"] == "ok"]
        return {"ideas": len(self.journal.query(select(t.c.idea_id))), "graded": len(done),
                "graded_buys": len(buys),
                "buy_hit_rate": round(sum(r["realized_pct"] > 0 for r in buys) / len(buys), 2)
                if buys else None,
                "buy_mean_pct": round(sum(r["realized_pct"] for r in buys) / len(buys), 2)
                if buys else None}

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
            block = {"earnings_in_window": f"BLOCKED: earnings {i.get('earnings_date')} fall "
                                           "inside the window, which no past window held. ",
                     "earnings_unknown": "HELD: no earnings calendar covers the window; "
                                         "check the date first. "}.get(i.get("verdict", ""), "")
            lines.append(
                f"  {i['ticker']} {i['kind']} · {i['event']} {i['event_date']}{est} · "
                f"{block}{i['action']}.{stop} History ({i['basis']}): {i['mean_pct']:+.1f}% beyond "
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
    led = payload.get("ledger") or {}
    if led.get("graded_buys"):
        lines.append(f"Past buy ideas: {led['graded_buys']} graded, {led['buy_hit_rate']:.0%} "
                     f"beat SPY, mean {led['buy_mean_pct']:+.1f}% (EV-9).")
    lines.append(f"{payload['patterns']} patterns in {payload['tests']} tests; about "
                 f"{payload['expected_false']} could be chance at p <= "
                 f"{payload['gate']['max_p']}. A pattern is a tendency over a handful of events, "
                 "not a proven edge: size small.")
    return "\n".join(lines)
