"""Event study of recurring company events (EV-1..EV-6). Pure functions of the calendar and
daily bars, so the backtest and the live report cannot disagree.

For each past event the **reaction session** R is the first session that can price it: the
event day when it starts during or before market hours, else the next session (``amc``).
Three windows, each as the stock's return minus SPY's over the same closes (abnormal), less
the stock's **baseline**: its mean abnormal return over every window of the same length in
the study period. Without the baseline a stock that beat SPY all decade (NVDA) shows a
"pattern" in any window:

- ``run_up``:   close of R - 1 - pre  ->  close of R - 1   (into the event)
- ``reaction``: close of R - 1        ->  close of R       (the event itself)
- ``drift``:    close of R            ->  close of R + post (after it)

A window has a **pattern** when, over at least ``min_years`` events, the mean abnormal move
is at least ``min_move_pct``, at least ``min_hit`` of the years share its sign, the two-sided
sign test gives p <= ``max_p``, and the newer half of the years agrees in sign. With about
ten events a group, that bar still lets some chance patterns through: the report counts the
tests and the false patterns expected from them (EV-5).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from trader.calendar import MarketCalendar
from trader.scanner.techniques import Series

WINDOWS = ("run_up", "reaction", "drift")


@dataclass(frozen=True)
class EventDate:
    day: date
    amc: bool = False
    estimated: bool = False


@dataclass(frozen=True)
class Group:
    id: str
    ticker: str
    name: str
    family: str
    dates: tuple[EventDate, ...]
    upcoming: tuple[EventDate, ...]


def parse_date(v: Any, estimated: bool = False) -> EventDate:
    """``2025-06-09`` (a YAML date) or ``"2025-09-17 amc"``."""
    if isinstance(v, date):
        return EventDate(v, False, estimated)
    parts = str(v).split()
    if not parts or len(parts) > 2 or (len(parts) == 2 and parts[1] != "amc"):
        raise ValueError(f"bad event date {v!r}: use YYYY-MM-DD or 'YYYY-MM-DD amc'")
    return EventDate(date.fromisoformat(parts[0]), len(parts) == 2, estimated)


def load_groups(path: str | Path) -> list[Group]:
    """EV-1: the hand-kept calendar; malformed entries fail loudly."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    out: list[Group] = []
    seen: set[str] = set()
    for g in raw.get("groups") or []:
        gid = str(g["id"])
        if gid in seen:
            raise ValueError(f"duplicate event group {gid}")
        seen.add(gid)
        dates = tuple(sorted((parse_date(d) for d in g.get("dates") or []),
                             key=lambda e: e.day))
        if len({e.day for e in dates}) != len(dates):
            raise ValueError(f"{gid}: a date is listed twice")
        up = tuple(sorted((parse_date(u["date"] if isinstance(u, dict) else u,
                                      bool(u.get("estimated")) if isinstance(u, dict) else False)
                           for u in g.get("upcoming") or []), key=lambda e: e.day))
        ticker = str(g["ticker"]).upper()
        out.append(Group(gid, ticker, str(g.get("name") or gid),
                         str(g.get("family") or gid), dates, up))
    return out


def reaction_index(s: Series, ev: EventDate) -> int | None:
    """Index of the first session that can price the event (None when after the data)."""
    for i, d in enumerate(s.dates):
        if d > ev.day or (d == ev.day and not ev.amc):
            return i
    return None


def baseline(s: Series, spy: Series, lengths: Sequence[int], start: date | None = None,
             end: date | None = None) -> dict[int, float]:
    """EV-4: the stock's mean abnormal return (vs SPY, %) over every window of each length
    between ``start`` and ``end``."""
    spy_c = dict(zip(spy.dates, spy.c, strict=True))
    idx = [i for i, d in enumerate(s.dates) if d in spy_c and (start is None or d >= start)
           and (end is None or d <= end)]
    out: dict[int, float] = {}
    for n in lengths:
        vals = [float(s.c[b] / s.c[a] - spy_c[s.dates[b]] / spy_c[s.dates[a]])
                for a, b in zip(idx, idx[n:], strict=False)]
        out[n] = sum(vals) / len(vals) * 100 if vals else 0.0
    return out


def observe(s: Series, spy: Series, ev: EventDate, pre: int, post: int,
            base: Mapping[int, float] | None = None) -> dict[str, Any] | None:
    """EV-3: the three windows of one past event: ``<window>`` is the move net of SPY and
    of the baseline, ``_spy`` net of SPY only, ``_raw`` the stock's own (None when the bars
    do not cover all of them)."""
    r = reaction_index(s, ev)
    if r is None or r - 1 - pre < 0 or r + post >= len(s.c):
        return None
    spy_c = dict(zip(spy.dates, spy.c, strict=True))
    marks = {"start": r - 1 - pre, "pre": r - 1, "react": r, "end": r + post}
    if any(s.dates[i] not in spy_c for i in marks.values()):
        return None

    def ret(a: int, b: int) -> tuple[float, float]:
        raw = float(s.c[b] / s.c[a] - 1)
        mkt = float(spy_c[s.dates[b]] / spy_c[s.dates[a]] - 1)
        return raw, raw - mkt
    out: dict[str, Any] = {"date": ev.day.isoformat(), "reaction_day": s.dates[r].isoformat(),
                           "amc": ev.amc}
    for name, (a, b) in (("run_up", ("start", "pre")), ("reaction", ("pre", "react")),
                         ("drift", ("react", "end"))):
        raw, ab = ret(marks[a], marks[b])
        n = marks[b] - marks[a]
        out[name] = round(ab * 100 - (base or {}).get(n, 0.0), 2)
        out[name + "_spy"] = round(ab * 100, 2)
        out[name + "_raw"] = round(raw * 100, 2)
    return out


def sign_test_p(k: int, n: int) -> float:
    """Two-sided binomial sign test of k positives in n against p = 0.5."""
    if n == 0:
        return 1.0
    lo = float(sum(math.comb(n, j) for j in range(0, min(k, n - k) + 1))) / float(2 ** n)
    return min(1.0, 2 * lo)


def summarize(values: Sequence[float]) -> dict[str, Any]:
    n = len(values)
    if n == 0:
        return {"n": 0}
    mean = sum(values) / n
    srt = sorted(values)
    med = srt[n // 2] if n % 2 else (srt[n // 2 - 1] + srt[n // 2]) / 2
    up = sum(v > 0 for v in values)
    sd = math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1)) if n > 1 else 0.0
    half = values[n // 2:]                      # the newer half (values are in date order)
    return {"n": n, "mean": round(mean, 2), "median": round(med, 2),
            "up_rate": round(up / n, 2), "p": round(sign_test_p(up, n), 3),
            "t": round(mean / (sd / math.sqrt(n)), 2) if sd > 0 else None,
            "recent_mean": round(sum(half) / len(half), 2)}


def pattern(st: Mapping[str, Any], *, min_years: int, min_hit: float, max_p: float,
            min_move_pct: float) -> str | None:
    """EV-5: "up", "down" or None."""
    if st.get("n", 0) < min_years:
        return None
    mean = float(st["mean"])
    if abs(mean) < min_move_pct or float(st["p"]) > max_p:
        return None
    hit = float(st["up_rate"]) if mean > 0 else 1 - float(st["up_rate"])
    if hit < min_hit or (float(st["recent_mean"]) > 0) != (mean > 0):
        return None
    return "up" if mean > 0 else "down"


def study(obs: Sequence[Mapping[str, Any]], **gate: Any) -> dict[str, Any]:
    """Per window: the summary and its pattern."""
    out: dict[str, Any] = {}
    for w in WINDOWS:
        st = summarize([float(o[w]) for o in obs])
        out[w] = {**st, "pattern": pattern(st, **gate)}
    return out


# --- ideas ---------------------------------------------------------------------------------

def _back(cal: MarketCalendar, d: date, n: int) -> date:
    for _ in range(n):
        d = cal.prev_trading_day(d)
    return d


def reaction_day(cal: MarketCalendar, ev: EventDate) -> date:
    if not ev.amc and cal.is_trading_day(ev.day):
        return ev.day
    return cal.next_trading_day(ev.day)


ACTIONS = {
    ("run_up", "up"): ("BUY_RUN_UP", "buy", "Buy at the close of {start}, sell at the close of "
                       "{pre} (before the event)"),
    ("run_up", "down"): ("AVOID_BEFORE", "avoid", "Weak into the event: no new buys from {start} "
                         "to {pre}"),
    ("reaction", "up"): ("HOLD_THROUGH", "buy", "Buy at the close of {pre}, sell at the close of "
                         "{react} (holds the event's gap risk)"),
    ("reaction", "down"): ("TRIM_BEFORE", "avoid", "Sell-the-news: holders trim by the close of "
                           "{pre}"),
    ("drift", "up"): ("BUY_AFTER", "buy", "Buy at the close of {react}, sell at the close of "
                      "{end}"),
    ("drift", "down"): ("WAIT_AFTER", "avoid", "Weak after the event: wait until {end} before "
                        "buying"),
}


def ideas_for(g: Group, ev: EventDate, st: Mapping[str, Any], basis: str, today: date,
              cal: MarketCalendar, pre: int, post: int) -> list[dict[str, Any]]:
    """EV-6: one idea per window with a pattern, dated from the reaction session."""
    r = reaction_day(cal, ev)
    marks = {"start": _back(cal, r, pre + 1), "pre": cal.prev_trading_day(r), "react": r,
             "end": cal.add_trading_days(r, post)}
    span = {"run_up": ("start", "pre"), "reaction": ("pre", "react"), "drift": ("react", "end")}
    out = []
    for w in WINDOWS:
        p = (st.get(w) or {}).get("pattern")
        if p is None:
            continue
        kind, side, text = ACTIONS[(w, p)]
        a, b = (marks[k] for k in span[w])
        status = "upcoming" if today < a else "open" if today < b else "passed"
        s = st[w]
        out.append({
            "group": g.id, "ticker": g.ticker, "event": g.name,
            "event_date": ev.day.isoformat(), "estimated": ev.estimated, "amc": ev.amc,
            "reaction_day": r.isoformat(), "window": w, "kind": kind, "side": side,
            "enter": a.isoformat(), "exit": b.isoformat(), "status": status,
            "action": text.format(**{k: v.isoformat() for k, v in marks.items()}),
            "basis": basis, "n": s["n"], "mean_pct": s["mean"], "median_pct": s["median"],
            "same_sign_rate": s["up_rate"] if p == "up" else round(1 - s["up_rate"], 2),
            "p": s["p"], "recent_mean_pct": s["recent_mean"]})
    return out
