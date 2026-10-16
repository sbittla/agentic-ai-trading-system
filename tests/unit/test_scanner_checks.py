"""Decision checks on scanner ideas and the Technical scan console page
(SCANNER_REQUIREMENTS TS-17..TS-19, UI_REQUIREMENTS UI-37)."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from tests.unit.test_scanner import mk, pullback_series, reflect, uptrend
from trader.calendar import MacroCalendar, MarketCalendar
from trader.config import default_config
from trader.scanner import techniques as T
from trader.scanner.checks import (
    CATALOG,
    ScanContext,
    apply_checks,
    base_context,
    check_summary,
    sector_trend,
    simple_sizer,
)
from trader.scanner.report import as_json, render
from trader.scanner.techniques import Frame, Idea, ideas_at

ASOF = date(2026, 10, 15)


def idea(sym: str = "AAPL", score: int = 60, side: str = "long", **ctx: Any) -> Idea:
    base = {"uptrend": True, "weekly_up": True, "market_up": True, "rs63": 4.0, "adx": 22.0,
            "rvol": 1.2, "reversion": False}
    base.update(ctx)
    trig, stop = (100.0, 96.0) if side == "long" else (100.0, 104.0)
    return Idea(symbol=sym, asof=ASOF, technique="TREND_PULLBACK", style="swing",
                side=side, entry="stop", trigger=trig, stop=stop,  # type: ignore[arg-type]
                t1=trig + (6 if side == "long" else -6), t2=trig + (12 if side == "long"
                                                                     else -12),
                risk=4.0, room_r=3.0, score=score, why="test", context=base)


def full_ctx(**kw: Any) -> ScanContext:
    c = ScanContext(tradable=frozenset({"AAPL", "NVDA", "AVGO", "TSM", "MSFT"}),
                    next_session=ASOF + timedelta(days=1), sessions_until=lambda d: (
                        d - ASOF).days, earnings={}, macro_known=True, locks={},
                    held=frozenset(), groups={"semis": (frozenset({"NVDA", "AVGO", "TSM"}), 1)},
                    sizer=simple_sizer(25_000, 0.5, 25), edges={"TREND_PULLBACK:long": 0.1})
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def check(d: Idea, key: str) -> dict[str, Any]:
    return next(c for c in d.checks if c["key"] == key)


# --- TS-17: the checks ------------------------------------------------------------------------


@pytest.mark.rule("TS-17")
def test_clean_idea_is_go_with_every_check_passing() -> None:
    [d] = apply_checks([idea()], full_ctx())
    assert d.verdict == "go"
    assert all(c["ok"] for c in d.checks), [c for c in d.checks if not c["ok"]]
    keys = {c["key"] for c in d.checks}
    assert {"direction", "tradable", "earnings", "macro", "locks", "held", "size", "trend",
            "weekly", "market", "rs", "room", "overheated", "edge"} <= keys
    assert {k for k, *_ in CATALOG} >= keys
    assert check_summary(d).startswith(f"{len(d.checks)}/{len(d.checks)} checks")


@pytest.mark.rule("TS-17")
@pytest.mark.parametrize(("kw", "key"), [
    ({"earnings": {"AAPL": ASOF + timedelta(days=2)}}, "earnings"),
    ({"locks": {"AAPL": ["WASH:2026-11-14 00:00"]}}, "locks"),
    ({"held": frozenset({"AAPL"})}, "held"),
    ({"sizer": simple_sizer(100, 0.5, 25)}, "size"),
])
def test_hard_check_failure_blocks_the_idea(kw: dict[str, Any], key: str) -> None:
    [d] = apply_checks([idea()], full_ctx(**kw))
    assert d.verdict == "blocked" and check(d, key)["ok"] is False and check(d, key)["hard"]
    text = render(ASOF, [d], [], {}, top_n=5, in_play_n=5, min_score=0)
    blocked = text.split("BLOCKED BY A RULE")[1]
    assert check(d, key)["label"] in blocked and "SWING IDEAS" in text
    assert "none qualify today" in text                          # not shown as actionable


@pytest.mark.rule("TS-17")
def test_earnings_far_enough_away_passes() -> None:
    [d] = apply_checks([idea()], full_ctx(earnings={"AAPL": ASOF + timedelta(days=10)}))
    assert check(d, "earnings")["ok"] and d.verdict == "go"


@pytest.mark.rule("TS-17")
def test_soft_failures_make_a_caution_not_a_block() -> None:
    [one] = apply_checks([idea(weekly_up=False)], full_ctx())
    assert one.verdict == "go"                                  # one soft failure is fine
    [two] = apply_checks([idea(weekly_up=False, market_up=False, adx=40.0)], full_ctx())
    assert two.verdict == "caution"
    assert {c["key"] for c in two.checks if c["ok"] is False} == {"weekly", "market",
                                                                  "overheated"}


@pytest.mark.rule("TS-17")
def test_correlated_group_gives_the_slot_to_the_higher_score() -> None:
    nvda, avgo = idea("NVDA", 70), idea("AVGO", 60)
    out = apply_checks([avgo, nvda], full_ctx())
    assert [(d.symbol, d.verdict) for d in out] == [("NVDA", "go"), ("AVGO", "blocked")]
    assert "1 of 1" in check(out[1], "correlation")["detail"]
    # an open NVDA position already fills the group
    [solo] = apply_checks([idea("AVGO", 60)], full_ctx(held=frozenset({"NVDA"})))
    assert solo.verdict == "blocked"


@pytest.mark.rule("TS-17")
def test_unknown_data_is_shown_never_counted_as_a_pass() -> None:
    ctx = ScanContext(tradable=frozenset({"AAPL"}))            # bars only (offline CLI)
    [d] = apply_checks([idea()], ctx)
    unknown = {c["key"] for c in d.checks if c["ok"] is None}
    assert {"earnings", "locks", "held", "size", "macro", "edge"} <= unknown
    assert d.verdict == "go" and "unknown" in check_summary(d)


@pytest.mark.rule("TS-17")
def test_weekly_and_sector_trend() -> None:
    up = mk(uptrend(260))
    down = reflect(up)
    i = len(up.c) - 1
    assert Frame(up, None, +1).weekly_up(i) is True
    assert Frame(up, None, -1).weekly_up(i) is False
    assert Frame(down, None, -1).weekly_up(i) is True           # the short side's trend
    assert Frame(mk(uptrend(40)), None, +1).weekly_up(39) is None
    assert sector_trend(up) is True and sector_trend(down) is False
    assert sector_trend(mk(uptrend(30))) is None
    d = [x for x in ideas_at(T.frames(pullback_series(), None), 263, dedup=False)][0]
    assert d.context["weekly_up"] is True and d.context["reversion"] is False


@pytest.mark.rule("TS-17")
def test_macro_day_and_base_context_from_config() -> None:
    cal = MarketCalendar()
    nxt = cal.next_trading_day(ASOF)
    fomc = MacroCalendar(fomc=frozenset({nxt}), cpi=frozenset(), jobs=frozenset(),
                         extra_caution=frozenset())
    cfg = default_config()
    ctx = base_context(cfg, asof=ASOF, edges=None, macro=fomc, calendar=cal,
                       sector=mk(uptrend(260)))
    assert ctx.macro_next == "FOMC" and ctx.tradable == frozenset(cfg.universe.tickers)
    assert "semis" in ctx.groups and ctx.sector_up["NVDA"] is True
    [d] = apply_checks([idea("NVDA")], ctx)
    assert check(d, "macro")["ok"] is False and "FOMC" in check(d, "macro")["detail"]
    assert check(d, "sector")["ok"] is True
    assert ctx.sessions_until is not None and ctx.sessions_until(nxt) == 1


# --- TS-18: size, verdicts, long-only ----------------------------------------------------------


@pytest.mark.rule("TS-18")
def test_size_follows_the_risk_rules() -> None:
    size = simple_sizer(25_000, 0.5, 25)
    assert size(100.0, 96.0) == (31, 124.0, "risk")              # $125 / $4 = 31
    assert size(500.0, 499.0)[0] == 12 and size(500.0, 499.0)[2] == "LL-9 position cap"
    assert size(100.0, 101.0)[0] == 0
    [d] = apply_checks([idea()], full_ctx())
    assert d.shares == 31 and d.risk_usd == 124.0
    assert "size 31 sh, $124 at risk" in render(ASOF, [d], [], {}, top_n=5, in_play_n=5,
                                                min_score=0)


@pytest.mark.rule("TS-18")
def test_bearish_ideas_are_options_only_and_not_sized() -> None:
    [d] = apply_checks([idea(side="short")], full_ctx())
    assert d.verdict == "options" and d.shares is None
    assert check(d, "direction")["ok"] is False and "ADR-008" in check(d, "direction")["rule"]
    assert "puts" in d.plan and "never stock" in d.plan
    text = render(ASOF, [d], [], {}, top_n=5, in_play_n=5, min_score=0)
    assert "BEARISH VIEWS (options only" in text and "sell-stop" not in text


@pytest.mark.rule("TS-18")
def test_research_symbols_are_watch_only_and_ranking_by_verdict() -> None:
    ideas = [idea("PLTR", 90), idea("AAPL", 50, weekly_up=False, market_up=False),
             idea("MSFT", 55), idea("NVDA", 80, side="short")]
    out = apply_checks(ideas, full_ctx())
    assert [(d.symbol, d.verdict) for d in out] == [
        ("MSFT", "go"), ("AAPL", "caution"), ("NVDA", "options"), ("PLTR", "watch")]
    text = render(ASOF, out, [], {}, top_n=5, in_play_n=5, min_score=0)
    assert text.index("MSFT") < text.index("WATCH ONLY") < text.index("PLTR")


# --- TS-19, UI-37: the console --------------------------------------------------------------


def _scan_file(tmp: Path) -> dict[str, Any]:
    ideas = apply_checks([idea("MSFT", 72), idea("AAPL", 58, weekly_up=False, adx=41.0,
                                                   market_up=False),
                          idea("NVDA", 66, side="short"),
                          idea("TSM", 61), idea("AVGO", 57)],
                         full_ctx(earnings={"TSM": ASOF + timedelta(days=1)}))
    payload = as_json(ASOF, ideas, [], {"SPCX": "only 74 sessions"}, generated="2026-10-15T20:"
                      "20:00+00:00", source="trader-core", edges_from="scanner_eval.json",
                      min_score=50, top_n=5, context_note="live state")
    (tmp / "reports").mkdir(exist_ok=True)
    (tmp / "reports" / "technical_scan.json").write_text(json.dumps(payload, default=str))
    return payload


@pytest.mark.rule("TS-19")
@pytest.mark.rule("UI-37")
def test_console_technical_page(tmp_path: Path) -> None:
    pytest.importorskip("starlette")
    from starlette.testclient import TestClient

    from trader.console.app import Auth, create_app
    from trader.console.data import ConsoleData, CoreClient, Paths
    from trader.journal.store import SqlJournal

    j = SqlJournal(f"sqlite:///{(tmp_path / 'j.db').as_posix()}")
    (tmp_path / "reports").mkdir()
    data = ConsoleData(j, CoreClient("http://127.0.0.1:9"), default_config(),
                       Paths(tmp_path / "reports", tmp_path / "runs", tmp_path / "config"))
    c = TestClient(create_app(data, Auth("a long test password")))
    c.post("/login", data={"password": "a long test password"})

    empty = c.get("/technical").text                              # UI-35 empty state
    assert "No technical scan yet" in empty and 'value="SCAN"' in empty
    assert "Decisions checked on every idea" in empty and "No earnings within 3" in empty

    _scan_file(tmp_path)
    (tmp_path / "reports" / "scanner_eval.json").write_text(json.dumps({
        "generated": "2026-10-15T21:00", "edges": {}, "in_play": {
            "days": 100, "top_n": 3, "top_avg_range_pct": 3.2, "all_avg_range_pct": 2.7,
            "lift": 1.19, "days_top_beat_avg": 0.8, "calibration": 0.9},
        "report": {"min_samples": 30, "score_buckets": [], "techniques": [
            {"technique": "BEAR_FLAG", "side": "short", "ideas": 80, "filled": 18,
             "hit_1r": 0.2, "avg_r": -0.5, "total_r": -9.0, "profit_factor": 0.2,
             "weak": False},
            {"technique": "RSI2_REVERSION", "side": "long", "ideas": 160, "filled": 160,
             "hit_1r": 0.35, "avg_r": 0.18, "total_r": 29.0, "profit_factor": 1.6,
             "weak": False}]}}))
    t = c.get("/technical").text
    assert ' style="' not in t and "<script>" not in t           # CSP (UI-21)
    for want in ("GO", "CAUTION", "Bearish views (options only)", "Puts preview",
                 "/options?preview=NVDA&amp;dir=bear&amp;style=swing", "Blocked by a rule",
                 "earnings 2026-10-16", "31 sh", "Technique scorecard", "disabled",
                 "lift 1.19x", "Skipped: SPCX", "Day watchlist"):
        assert want in t, want
    assert t.index("MSFT") < t.index("Bearish views")            # actionable first
    home = c.get("/").text
    assert "All ideas and checks" in home and "MSFT" in home
    nav = c.get("/ideas").text
    assert 'href="/technical"' in nav


@pytest.mark.rule("TS-19")
def test_scan_command_is_allowed_everywhere() -> None:
    from trader.console.data import ALLOWED
    from trader.notify.commands import SAFE

    assert "SCAN" in SAFE and "SCAN" in ALLOWED


@pytest.mark.rule("TS-19")
def test_cli_writes_the_latest_scan_for_the_console(tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch,
                                                    capsys: pytest.CaptureFixture[str]) -> None:
    from tests.unit.test_scanner import _write_store, random_walk
    from trader.cli import main

    _write_store(tmp_path / "bars", {"AAA": random_walk(420, seed=51, sym="AAA"),
                                     "SPY": random_walk(420, seed=52, sym="SPY")})
    macro = Path(__file__).resolve().parents[2] / "config" / "macro_calendar_2026_2027.yaml"
    latest = tmp_path / "rep" / "technical_scan.json"
    cfg = tmp_path / "config.yaml"
    cfg.write_text("universe:\n  tickers: [AAA]\n"
                   f"windows:\n  macro_calendar_file: {macro.as_posix()}\n"
                   f"scanner:\n  latest_file: {latest.as_posix()}\n"
                   f"  eval_file: {(tmp_path / 'none.json').as_posix()}\n", encoding="utf-8")
    monkeypatch.setenv("TRADER_BARS_PATH", str(tmp_path / "bars"))
    assert main(["--config", str(cfg), "scan", "--equity", "25000"]) == 0
    data = json.loads(latest.read_text(encoding="utf-8"))
    assert data["source"] == "cli" and "unknown" in data["context_note"]
    for d in data["swing"]:
        assert d["verdict"] in ("go", "caution", "blocked", "options") and d["checks"]
        if d["side"] == "long":
            assert isinstance(d["shares"], int)
    latest.unlink()
    assert main(["--config", str(cfg), "scan", "--date",
                 data["asof"], "--no-write"]) == 0
    assert not latest.exists()                                   # dated runs never overwrite
    capsys.readouterr()


def test_idea_context_survives_json() -> None:
    d = replace(idea(), checks=[{"key": "x"}])
    js = json.loads(json.dumps(d.as_dict(), default=str))
    assert js["context"]["weekly_up"] is True and js["checks"] == [{"key": "x"}]
    assert np.isfinite(js["score"])


@pytest.mark.rule("TS-17")
def test_room_to_resistance_and_options_tag() -> None:
    [tight] = apply_checks([replace(idea(), room_r=0.8)], full_ctx())
    assert check(tight, "room")["ok"] is False and check(tight, "room")["rule"] == "CK-8"
    [sky] = apply_checks([replace(idea(), room_r=None)], full_ctx())
    assert check(sky, "room")["ok"] is True and check(sky, "room")["detail"] == "open sky"
    [bear] = apply_checks([idea(side="short")], full_ctx())
    line = render(ASOF, [bear], [], {}, top_n=5, in_play_n=5, min_score=0)
    assert " OPTIONS " in line and "invalidation 104.00" in line
