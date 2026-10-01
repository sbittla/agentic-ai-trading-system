"""Replay tests: no-trade days (NT-1, NT-2), determinism (RP-2, RP-3) and the REQUIREMENTS §11
acceptance checks evaluated on a multi-day synthetic replay."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from tests.replay.helpers import PRICES, Harness, flat
from trader.backtest.engine import diff_runs, run_replay
from trader.backtest.synthetic import generate
from trader.config import default_config
from trader.journal import schema as s
from trader.journal.store import SqlJournal
from trader.llm.fakes import RulesLLM
from trader.timeutil import to_et

DAY = date(2026, 10, 16)


@pytest.fixture(scope="module")
def synth():  # type: ignore[no-untyped-def]
    return generate(date(2026, 10, 5), 12, seed=7)


@pytest.fixture(scope="module")
def replay(synth, tmp_path_factory):  # type: ignore[no-untyped-def]
    import asyncio

    out = tmp_path_factory.mktemp("run1")
    return asyncio.run(run_replay(default_config(), minutes=synth.minutes, daily=synth.daily,
                                  llm=RulesLLM(), out_dir=out, news=synth.news,
                                  seed_minutes=synth.seed_minutes, keep_engine=False))


@pytest.mark.rule("NT-1")
@pytest.mark.rule("NT-2")
async def test_quiet_day_no_trade_and_logged(tmp_path: Path) -> None:
    h = Harness(default_config(), DAY, tmp_path)
    await h.start()
    await h.premarket()
    bars = sorted([b for sym, px in PRICES.items() for b in flat(sym, DAY, px)],
                  key=lambda b: (b.ts, b.symbol))
    await h.run_bars(bars)
    await h.at(16, 0)
    assert h.broker.submissions == 0
    rows = [r for r in h.journal.rows(s.decisions) if r["reason_code"] == "NT_NO_SETUP"]
    assert {r["window"] for r in rows} == {"morning", "afternoon"}
    assert "no qualifying setup" in "\n".join(t for _, t in h.notifier.sent)


@pytest.mark.rule("NT-1")
def test_closest_candidate_logged(replay) -> None:  # type: ignore[no-untyped-def]
    rows = [d for d in replay.decisions if d["reason_code"] == "NT_NO_SETUP"]
    assert rows, "expected at least one window without a trade"
    assert any("closest:" in (d["detail"] or "") for d in rows)


def test_replay_outputs(replay) -> None:  # type: ignore[no-untyped-def]
    out = replay.out_dir
    for f in ("run.yaml", "journal.db", "trades.csv", "metrics.json", "report.html",
              "decisions.log"):
        assert (out / f).exists(), f
    assert replay.metrics["pipeline"]["setups"] > 0


def _journal(replay) -> SqlJournal:  # type: ignore[no-untyped-def]
    return SqlJournal(f"sqlite:///{(replay.out_dir / 'journal.db').as_posix()}", create=False)


def test_acceptance_zero_orders_outside_windows(replay) -> None:  # type: ignore[no-untyped-def]
    j = _journal(replay)
    for o in j.rows(s.orders):
        if o["leg"] in ("A", "B"):
            t = to_et(o["ts"]).time()
            assert (t.hour == 9 and t.minute >= 35) or (t.hour == 10 and t.minute <= 15) or \
                (t.hour == 15 and t.minute <= 50), o


def test_acceptance_zero_off_list_orders(replay) -> None:  # type: ignore[no-untyped-def]
    j = _journal(replay)
    tickers = set(default_config().universe.tickers)
    assert {o["ticker"] for o in j.rows(s.orders)} <= tickers


@pytest.mark.rule("A7-1")
def test_acceptance_every_order_has_full_checklist_approval(replay) -> None:  # type: ignore[no-untyped-def]
    j = _journal(replay)
    approved = {d["proposal_id"]: d for d in j.rows(s.decisions) if d["result"] == "APPROVE"}
    for o in j.rows(s.orders):
        if o["leg"] in ("A", "B"):
            d = approved[o["proposal_id"]]
            assert len(d["checklist_json"]) == 10 and all(i["passed"] for i in d["checklist_json"])


@pytest.mark.rule("A9-2")
def test_acceptance_no_day_trade_past_flatten(replay) -> None:  # type: ignore[no-untyped-def]
    j = _journal(replay)
    for p in j.closed_positions():
        if p["style"] != "day":
            continue
        closed = to_et(p["closed_at"]).time()
        limit = (10, 32) if p["window"] == "morning" else (15, 57)
        assert (closed.hour, closed.minute) <= limit, p
        assert to_et(p["closed_at"]).date() == to_et(p["opened_at"]).date()


def test_acceptance_no_stop_moved_down(replay) -> None:  # type: ignore[no-untyped-def]
    j = _journal(replay)
    for r in j.rows(s.events):
        if r["type"] == "StopMoved":
            assert Decimal(r["payload"]["new_stop"]) >= Decimal(r["payload"]["old_stop"])


def test_acceptance_zero_wash_sales_strict(replay) -> None:  # type: ignore[no-untyped-def]
    j = _journal(replay)
    assert all(not r["disallowed_loss"] for r in j.rows(s.tax_lots))


def test_acceptance_reports_match_journal(replay) -> None:  # type: ignore[no-untyped-def]
    import json

    j = _journal(replay)
    for f in (replay.out_dir / "reports").glob("*_daily.json"):
        data = json.loads(f.read_text(encoding="utf-8"))
        d = date.fromisoformat(data["date"])
        trades = [t for t in j.closed_positions() if to_et(t["closed_at"]).date() == d]
        assert data["metrics"]["trades"] == len(trades)
        assert data["metrics"]["pnl"] == pytest.approx(sum(float(t["pnl"]) for t in trades),
                                                       abs=0.01)


async def test_replay_is_deterministic(synth, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """RP-2: the same days replayed twice give identical decisions; RP-3 diff file."""
    days = dict(list(synth.minutes.items())[:4])
    a = await run_replay(default_config(), minutes=days, daily=synth.daily, llm=RulesLLM(),
                         out_dir=tmp_path / "a", news=synth.news,
                         seed_minutes=synth.seed_minutes)
    b = await run_replay(default_config(), minutes=days, daily=synth.daily, llm=RulesLLM(),
                         out_dir=tmp_path / "b", news=synth.news,
                         seed_minutes=synth.seed_minutes)
    la = (a.out_dir / "decisions.log").read_bytes()
    assert la == (b.out_dir / "decisions.log").read_bytes()
    text = diff_runs(a.out_dir, b.out_dir)
    assert "- removed: 0" in text and "- added: 0" in text
