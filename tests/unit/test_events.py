"""Company events A20, EV-1..EV-8 (EVENT_REQUIREMENTS.md), on synthetic bars."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from trader.calendar import MarketCalendar
from trader.config import Config, validate
from trader.events.agent import EventsAgent, render
from trader.events.study import (
    EventDate,
    Group,
    baseline,
    ideas_for,
    load_groups,
    observe,
    parse_date,
    pattern,
    reaction_day,
    reaction_index,
    sign_test_p,
    study,
    summarize,
)
from trader.scanner.techniques import Series

ROOT = Path(__file__).resolve().parents[2]
CAL = MarketCalendar()


def sessions(start: date, end: date) -> list[date]:
    out, d = [], start
    while d <= end:
        if CAL.is_trading_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def series(sym: str, days: Sequence[date], closes: Sequence[float]) -> Series:
    return Series.from_rows(sym, [(d, c, c * 1.01, c * 0.99, c, 1e6)
                                  for d, c in zip(days, closes, strict=True)])


def cfg(**events: object) -> Config:
    raw = json.loads(Config(universe={"tickers": ["NVDA"]}).model_dump_json())   # type: ignore[arg-type]
    raw["events"].update(events)
    return Config.model_validate(raw)


@pytest.mark.rule("EV-1")
def test_calendar_file_parses_and_rejects_bad_entries(tmp_path: Path) -> None:
    groups = load_groups(ROOT / "config/corporate_events.yaml")
    ids = [g.id for g in groups]
    assert len(ids) == len(set(ids)) and "NVDA_GTC" in ids
    for g in groups:
        assert g.ticker.isupper() and [d.day for d in g.dates] == sorted(d.day for d in g.dates)
    assert parse_date("2025-09-17 amc") == EventDate(date(2025, 9, 17), True)
    assert parse_date(date(2025, 6, 9)) == EventDate(date(2025, 6, 9), False)
    with pytest.raises(ValueError):
        parse_date("2025-09-17 pm")
    bad = tmp_path / "e.yaml"
    bad.write_text("groups:\n  - {id: A, ticker: x, dates: [2020-01-02]}\n"
                   "  - {id: A, ticker: y, dates: [2020-01-03]}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        load_groups(bad)
    bad.write_text("groups:\n  - {id: A, ticker: x, dates: [2020-01-02, 2020-01-02]}\n",
                   encoding="utf-8")
    with pytest.raises(ValueError, match="twice"):
        load_groups(bad)
    bad.write_text("groups:\n  - id: A\n    ticker: x\n    dates: [2020-01-02]\n"
                   "    upcoming: [{date: 2027-01-05, estimated: true}]\n", encoding="utf-8")
    (g,) = load_groups(bad)
    assert g.ticker == "X" and g.family == "A" and g.upcoming[0].estimated


@pytest.mark.rule("EV-2")
def test_reaction_session_during_amc_and_weekend() -> None:
    days = sessions(date(2025, 6, 2), date(2025, 6, 20))
    s = series("X", days, [100.0] * len(days))
    assert s.dates[reaction_index(s, EventDate(date(2025, 6, 9)))] == date(2025, 6, 9)
    assert s.dates[reaction_index(s, EventDate(date(2025, 6, 9), amc=True))] == date(2025, 6, 10)
    assert s.dates[reaction_index(s, EventDate(date(2025, 6, 7)))] == date(2025, 6, 9)  # Saturday
    assert reaction_index(s, EventDate(date(2025, 7, 1))) is None
    assert reaction_day(CAL, EventDate(date(2026, 11, 26))) == date(2026, 11, 27)  # Thanksgiving
    assert reaction_day(CAL, EventDate(date(2026, 12, 1), amc=True)) == date(2026, 12, 2)


@pytest.mark.rule("EV-3")
@pytest.mark.rule("EV-4")
def test_windows_net_of_spy_and_of_the_baseline() -> None:
    days = sessions(date(2025, 1, 2), date(2025, 3, 31))
    n = len(days)
    r = days.index(date(2025, 2, 18))
    closes = [100.0] * n
    for k in range(r - 11, r):                   # +1% a session into the event
        closes[k + 1] = closes[k] * 1.01
    for k in range(r, n - 1):
        closes[k + 1] = closes[k]
    closes[r] = closes[r - 1] * 0.95             # -5% on the event
    for k in range(r + 1, n):
        closes[k] = closes[r]
    s = series("X", days, closes)
    spy = series("SPY", days, [400.0] * n)
    o = observe(s, spy, EventDate(date(2025, 2, 18)), 10, 10)
    assert o is not None
    assert o["run_up_raw"] == pytest.approx((1.01 ** 10 - 1) * 100, abs=0.01)
    assert o["reaction"] == pytest.approx(-5.0, abs=0.01) and o["drift"] == pytest.approx(0.0)
    up_spy = series("SPY", days, [400.0 * 1.001 ** k for k in range(n)])
    o2 = observe(s, up_spy, EventDate(date(2025, 2, 18)), 10, 10)
    assert o2 is not None and o2["run_up_spy"] < o["run_up_spy"]
    base = baseline(s, spy, (10, 1))
    o3 = observe(s, spy, EventDate(date(2025, 2, 18)), 10, 10, base)
    assert o3 is not None and o3["run_up"] == pytest.approx(o3["run_up_spy"] - base[10], abs=0.01)
    assert observe(s, spy, EventDate(date(2025, 1, 6)), 10, 10) is None   # not enough before
    assert observe(s, spy, EventDate(date(2025, 3, 28)), 10, 10) is None  # not enough after


@pytest.mark.rule("EV-5")
def test_sign_test_and_pattern_gate() -> None:
    assert sign_test_p(9, 10) == pytest.approx(2 * 11 / 1024)
    assert sign_test_p(5, 10) == 1.0 and sign_test_p(0, 0) == 1.0
    gate = {"min_years": 6, "min_hit": 0.7, "max_p": 0.1, "min_move_pct": 1.0}
    st = summarize([3.0, 2.0, 4.0, -1.0, 5.0, 2.5, 3.5, 1.5, 2.0, 3.0])
    assert st["up_rate"] == 0.9 and pattern(st, **gate) == "up"
    assert pattern(summarize([-v for v in [3.0, 2.0, 4.0, -1.0, 5.0, 2.5, 3.5, 1.5, 2.0, 3.0]]),
                   **gate) == "down"
    assert pattern(summarize([3.0, 2.0, 4.0, 5.0, 2.5]), **gate) is None          # too few
    assert pattern(summarize([0.3, 0.2, 0.4, 0.5, 0.2, 0.3, 0.1, 0.2]), **gate) is None   # small
    assert pattern(summarize([2.0, -2.0] * 5), **gate) is None                     # no sign
    # strong early years and weak recent years: the newer half must agree
    assert pattern(summarize([9.0, 8.0, 9.0, 8.0, 9.0, 0.5, -0.5, 0.4, -0.6, -0.4]),
                   **gate) is None
    res = study([{"run_up": v, "reaction": 0.0, "drift": -v} for v in
                 [3.0, 2.0, 4.0, -1.0, 5.0, 2.5, 3.5, 1.5, 2.0, 3.0]], **gate)
    assert res["run_up"]["pattern"] == "up" and res["drift"]["pattern"] == "down"
    assert res["reaction"]["pattern"] is None


@pytest.mark.rule("EV-6")
def test_ideas_are_dated_from_the_reaction_session() -> None:
    g = Group("G", "NVDA", "Keynote", "G", (), ())
    ev = EventDate(date(2026, 12, 1))
    st = study([{"run_up": v, "reaction": -v, "drift": 0.1} for v in
                [3.0, 2.0, 4.0, -1.0, 5.0, 2.5, 3.5, 1.5, 2.0, 3.0]],
               min_years=6, min_hit=0.7, max_p=0.1, min_move_pct=1.0)
    ideas = ideas_for(g, ev, st, "group", date(2026, 10, 17), CAL, 10, 10)
    kinds = {i["kind"]: i for i in ideas}
    assert set(kinds) == {"BUY_RUN_UP", "TRIM_BEFORE"}
    buy = kinds["BUY_RUN_UP"]
    assert buy["exit"] == "2026-11-30" and buy["status"] == "upcoming"
    start = date.fromisoformat(buy["enter"])
    assert CAL.is_trading_day(start) and CAL.trading_days_between(start, date(2026, 12, 1)) == 11
    assert kinds["TRIM_BEFORE"]["side"] == "avoid"
    late = ideas_for(g, ev, st, "group", date(2026, 11, 20), CAL, 10, 10)
    assert {i["kind"]: i["status"] for i in late}["BUY_RUN_UP"] == "open"


def _loader(data: dict[str, Series]):   # noqa: ANN202
    async def load(symbols: Sequence[str], until: date) -> dict[str, Series]:
        return {k: v for k, v in data.items() if k in symbols}
    return load


@pytest.mark.rule("EV-6")
@pytest.mark.rule("EV-7")
def test_agent_end_to_end(tmp_path: Path) -> None:
    days = sessions(date(2014, 1, 2), date(2026, 10, 16))
    past = [date(y, 3, 18) for y in range(2015, 2026)]
    closes = [100.0] * len(days)
    bump = set()
    for d in past:                              # a steady run-up into every event
        r = next(i for i in range(len(days)) if days[i] >= d)
        bump.update(range(r - 10, r))
    for i in range(1, len(days)):
        closes[i] = closes[i - 1] * (1.006 if i in bump else 0.9997)
    data = {"NVDA": series("NVDA", days, closes),
            "SPY": series("SPY", days, [400.0] * len(days))}
    (tmp_path / "config").mkdir()
    (tmp_path / "config/ev.yaml").write_text(
        "groups:\n  - id: KEY\n    ticker: NVDA\n    name: Keynote\n    dates: ["
        + ", ".join(d.isoformat() for d in past)
        + "]\n    upcoming: [{date: 2026-12-01}, {date: 2027-12-01, estimated: true}]\n"
          "  - id: LONELY\n    ticker: NVDA\n    name: One-off\n    dates: [2025-10-28]\n"
          "    upcoming: [2026-11-10]\n", encoding="utf-8")
    out = tmp_path / "reports/events.json"
    c = cfg(file="config/ev.yaml", latest_file=str(out))
    now = datetime(2026, 10, 17, 21, 0, tzinfo=UTC)
    agent = EventsAgent(c, _loader(data), lambda: now, base=tmp_path)
    p = asyncio.run(agent.run())
    assert json.loads(out.read_text(encoding="utf-8"))["asof"] == "2026-10-17"
    key = next(g for g in p["groups"] if g["id"] == "KEY")
    assert key["study"]["run_up"]["pattern"] == "up" and len(key["observations"]) == 11
    ideas = [i for i in p["ideas"] if i["kind"] == "BUY_RUN_UP"]
    assert [i["event_date"] for i in ideas] == ["2026-12-01"]   # 2027 is past the horizon
    assert ideas[0]["basis"] == "group" and ideas[0]["stop_hint"] < data["NVDA"].c[-1]
    lonely = next(u for u in p["upcoming"] if u["group"] == "LONELY")
    assert lonely["ideas"] == [] and "too few" in lonely["basis"]
    assert p["tests"] == 3 and p["expected_false"] == pytest.approx(0.3)
    text = render(p)
    assert "BUY_RUN_UP" in text and "One-off" in text and "could be chance" in text
    assert "NVDA" not in render(p, ticker="AAPL").split("IDEAS")[1].split("UPCOMING")[0]
    no_spy = EventsAgent(c, _loader({"NVDA": data["NVDA"]}), lambda: now, base=tmp_path)
    assert "error" in asyncio.run(no_spy.run(write=False))


@pytest.mark.rule("EV-8")
def test_config_validation() -> None:
    assert not [x for x in validate(cfg()) if x.startswith("events")]
    assert any(x.startswith("events:") for x in validate(cfg(min_hit=0.4)))
    assert any(x.startswith("events:") for x in validate(cfg(min_years=2)))
    # a missing calendar fails the events job (logged), never the platform's start
    assert not [x for x in validate(cfg(file="nope.yaml"), base_dir=ROOT) if "events" in x]


@pytest.mark.rule("EV-4")
def test_trailing_baseline_uses_no_later_data() -> None:
    days = sessions(date(2018, 1, 2), date(2020, 12, 31))
    n = len(days)
    closes = [100.0 * 1.001 ** k for k in range(n)]
    s = series("X", days, closes)
    spy = series("SPY", days, [400.0] * n)
    ev = EventDate(date(2020, 3, 2))
    a = observe(s, spy, ev, 10, 10, base_sessions=504)
    later = closes[:]
    for k in range(days.index(date(2020, 4, 1)), n):     # change only data after the event
        later[k] = later[k] * 3
    b = observe(series("X", days, later), spy, ev, 10, 10, base_sessions=504)
    assert a is not None and b is not None and a["run_up_base"] == b["run_up_base"]
    assert a["run_up_base"] == pytest.approx((1.001 ** 10 - 1) * 100, abs=0.01)
    assert a["run_up"] == pytest.approx(0.0, abs=0.01)    # a steady trend is no pattern
    assert observe(s, spy, EventDate(date(2018, 6, 1)), 10, 10, base_sessions=504) is None


def _idea(enter: str, exit_: str, side: str = "buy") -> dict:
    return {"ticker": "NVDA", "enter": enter, "exit": exit_, "side": side}


@pytest.mark.rule("EV-10")
def test_earnings_inside_a_window_block_buy_ideas(tmp_path: Path, journal) -> None:   # noqa: ANN001
    from trader.events.agent import EventsAgent as EA
    from trader.events.study import load_earnings
    from trader.journal import schema as js

    chk = EA._earnings_check
    i = _idea("2026-11-13", "2026-11-30")
    chk(i, {"NVDA": [date(2026, 11, 17)]}, None)
    assert i["verdict"] == "earnings_in_window" and i["earnings_date"] == "2026-11-17"
    i = _idea("2026-12-17", "2027-01-04")
    chk(i, {"NVDA": [date(2026, 11, 17)]}, None)
    assert i["verdict"] == "earnings_unknown"              # nothing covers the window
    chk(i, {"NVDA": [date(2026, 11, 17)]}, date(2027, 1, 10))
    assert i["verdict"] == "ok"                            # the calendar covers it: none due
    chk(i, {"NVDA": [date(2027, 2, 24)]}, None)
    assert i["verdict"] == "ok"                            # the next report is after the window
    i = _idea("2026-11-13", "2026-11-30", side="avoid")
    chk(i, {"NVDA": [date(2026, 11, 17)]}, None)
    assert i["verdict"] == "ok" and i["earnings_date"] == "2026-11-17"
    (tmp_path / "config").mkdir()
    f = tmp_path / "config/ev.yaml"
    f.write_text("earnings:\n  nvda: [2026-11-17]\n  AAPL: 2027-01-28\ngroups: []\n",
                 encoding="utf-8")
    assert load_earnings(f) == {"NVDA": [date(2026, 11, 17)], "AAPL": [date(2027, 1, 28)]}
    c = cfg(file="config/ev.yaml")
    now = datetime(2026, 10, 17, 21, 0, tzinfo=UTC)
    ag = EA(c, _loader({}), lambda: now, base=tmp_path, journal=journal)
    assert ag.earnings(date(2026, 10, 17))[1:] == (None, "file")
    journal.insert(js.fundamentals_daily, {"date": date(2026, 10, 16), "ticker": "MSFT",
                                           "kind": "earnings_calendar",
                                           "json": {"date": "2026-10-28"}})
    dates, until, src = ag.earnings(date(2026, 10, 17))
    assert src == "finnhub" and until == date(2026, 12, 15)
    assert dates["MSFT"] == [date(2026, 10, 28)] and dates["NVDA"] == [date(2026, 11, 17)]
    assert ag.earnings(date(2026, 10, 25))[2] == "file"    # the Finnhub rows are stale


@pytest.mark.rule("EV-9")
def test_every_idea_is_kept_and_graded_once(tmp_path: Path, journal) -> None:   # noqa: ANN001
    from trader.journal import schema as js

    days = sessions(date(2026, 9, 1), date(2026, 10, 30))
    s = series("NVDA", days, [100.0 + k for k in range(len(days))])
    spy = series("SPY", days, [400.0] * len(days))
    (tmp_path / "config").mkdir()
    (tmp_path / "config/ev.yaml").write_text("groups: []\n", encoding="utf-8")
    now = [datetime(2026, 10, 1, 21, 0, tzinfo=UTC)]
    ag = EventsAgent(cfg(file="config/ev.yaml"), _loader({}), lambda: now[0], base=tmp_path,
                     journal=journal)
    idea = {"group": "G", "ticker": "NVDA", "kind": "BUY_RUN_UP", "side": "buy",
            "window": "run_up", "event_date": "2026-10-20", "estimated": False,
            "enter": "2026-10-05", "exit": "2026-10-19", "verdict": "ok", "basis": "group",
            "n": 9, "mean_pct": 5.0, "same_sign_rate": 0.9, "p": 0.04}
    led = ag.record([idea], {"NVDA": s}, spy, date(2026, 10, 1))
    assert led["ideas"] == 1 and led["graded"] == 0
    ag.record([{**idea, "mean_pct": 4.0}], {"NVDA": s}, spy, date(2026, 10, 2))
    (row,) = journal.rows(js.event_ideas)
    assert row["first_seen"] == date(2026, 10, 1) and row["mean_pct"] == 4.0
    led = ag.record([], {"NVDA": s}, spy, date(2026, 10, 20))
    (row,) = journal.rows(js.event_ideas)
    c = dict(zip(s.dates, s.c, strict=True))
    want = (c[date(2026, 10, 19)] / c[date(2026, 10, 5)] - 1) * 100
    assert row["graded"] == date(2026, 10, 20) and row["realized_pct"] == pytest.approx(want, 0.01)
    assert led["graded_buys"] == 1 and led["buy_hit_rate"] == 1.0
    ag.record([{**idea, "mean_pct": 1.0}], {"NVDA": s}, spy, date(2026, 10, 21))
    (row,) = journal.rows(js.event_ideas)
    assert row["mean_pct"] == 4.0 and row["graded"] == date(2026, 10, 20)   # frozen once graded
    assert "Past buy ideas: 1 graded" in render({
        "asof": "2026-10-21", "windows": {"pre_sessions": 10, "post_sessions": 10},
        "ideas": [], "upcoming": [], "patterns": 0, "tests": 0, "expected_false": 0,
        "gate": {"max_p": 0.1}, "ledger": led})
