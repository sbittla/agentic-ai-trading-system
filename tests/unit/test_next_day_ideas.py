"""After-close next-day ideas (A5-1 preview): journal rows, grading, owner alert, LLM economy."""

from __future__ import annotations

import logging
from datetime import timedelta

import pytest

from tests.conftest import PLAIN_DAY, bar, et
from tests.unit.test_telegram import TOKEN, FakeBot
from trader.agents.a05_ideas import alert_text, grade, grade_summary, idea_rows
from trader.agents.a05_strategy import StrategyAgent
from trader.config import Config
from trader.llm.fakes import ScriptedLLM
from trader.models.messages import GamePlan, GamePlanItem
from trader.notify.telegram import TelegramNotifier
from trader.services.runtime import configure_logging

NEXT = PLAIN_DAY + timedelta(days=3)
NOW = et(PLAIN_DAY, "16:15")


def screen(score: int, close: float = 100.0) -> dict:  # type: ignore[type-arg]
    return {"session_date": PLAIN_DAY.isoformat(), "close": close, "change_pct": 1.25,
            "range_pos": 0.8, "volume_vs_50d_avg": 1.3, "trend_up": True,
            "atr_to_prior_high": 0.2, "atr_to_20d_closing_high": -0.1, "atr_to_ema20d": -1.5,
            "readiness_score": score, "candidates": []}


def plans() -> dict[str, GamePlan]:
    item = GamePlanItem(ticker="NVDA", setup="ORB", trigger=101.0, stop=99.0, target_1=103.0,
                        why="uptrend & strong close")
    return {"morning": GamePlan(day=NEXT, window="morning", market_bias="with_us",
                                watch=(item,), off_limits=(("TSLA", "earnings <3 days"),),
                                source="preview"),
            "afternoon": GamePlan(day=NEXT, window="afternoon", source="preview")}


@pytest.mark.rule("A5-1")
def test_idea_rows_rank_picks_then_score_and_mark_off_limits() -> None:
    rows = idea_rows(NOW, NEXT, {"AAPL": screen(85), "NVDA": screen(60), "TSLA": screen(95),
                                 "META": screen(70)},
                     {"NVDA": {"tone": "positive", "catalyst": "AI deal"}}, plans(),
                     {"TSLA": "earnings <3 days"}, {"META": "weak catalyst"})
    assert [(r["ticker"], r["status"], r["rank"]) for r in rows] == [
        ("NVDA", "pick", 1), ("AAPL", "watch", 2), ("META", "skip", 3), ("TSLA", "off", 4)]
    assert rows[2]["note"] == "weak catalyst"
    nv = rows[0]
    assert nv["morning_setup"] == "ORB" and nv["morning_trigger"] == 101.0
    assert nv["afternoon_setup"] is None and nv["news_tone"] == "positive"
    assert nv["note"] == "uptrend & strong close" and rows[3]["note"] == "earnings <3 days"
    assert nv["session_date"] == PLAIN_DAY and nv["plan_for"] == NEXT


@pytest.mark.rule("A5-1")
@pytest.mark.parametrize(("h", "low", "outcome"), [
    (100.5, 99.5, "not_triggered"), (103.5, 100.0, "t1"), (101.5, 98.5, "stopped"),
    (104.0, 98.0, "both"), (102.0, 99.5, "open")])
def test_grade_on_the_planned_session_bar(h: float, low: float, outcome: str) -> None:
    row = {"morning_setup": "ORB", "morning_trigger": 101.0, "morning_stop": 99.0,
           "morning_target": 103.0}
    g = grade(row, bar("NVDA", et(NEXT, "09:30"), 100, h, low, 100.2, tf="1Day"))
    assert g["outcome"] == outcome and g["day_high"] == h
    assert grade({}, bar("X", et(NEXT, "09:30"), 1, 2, 0.5, 1, tf="1Day"))["outcome"] == "no_pick"
    summary = grade_summary([{**row, **g}])
    assert summary is not None and summary.startswith(("0/1", "1/1"))


@pytest.mark.rule("A5-1")
def test_alert_is_escaped_html_and_fits_one_message() -> None:
    p = plans()
    rows = idea_rows(NOW, NEXT, {"NVDA": screen(85)}, {"NVDA": {"tone": "positive"}}, p,
                     {"TSLA": "earnings <3 days"})
    text = alert_text(NEXT, PLAIN_DAY, {"QQQ": {"change_pct": 1.01}}, p, rows,
                      {"TSLA": "earnings <3 days"}, "1/2 triggered · 1 reached T1 · 0 stopped",
                      {"MSFT": "weak & late"})
    assert text.startswith("<b>📋 Next-day ideas · Mon Oct 19</b>")
    assert "<b>NVDA</b> ORB · score 85" in text and "buy &gt; 101.00" in text
    assert "earnings &lt;3 days" in text and "<3" not in text   # escaped for parse_mode=HTML
    assert "no setup worth watching" in text and "Last ideas:" in text
    assert "<b>Passed</b> MSFT: weak &amp; late" in text
    assert len(text) < 4000


@pytest.mark.rule("A5-1")
async def test_preview_window_without_candidates_skips_the_llm(cfg: Config) -> None:
    llm = ScriptedLLM({"a05_preview": []})
    a5 = StrategyAgent(cfg, llm, None, lambda: NOW)
    gp = await a5.game_plan(NEXT, "afternoon", {"tickers": {}}, [], preview=True)
    assert gp.source == "preview" and gp.watch == () and llm.calls == []


@pytest.mark.rule("A5-1")
async def test_preview_with_llm_down_still_uses_the_screen(cfg: Config) -> None:
    a5 = StrategyAgent(cfg, ScriptedLLM({"a05_preview": ["bad", "bad"]}), None, lambda: NOW)
    data = {"tickers": {"NVDA": {"session": {"readiness_score": 90, "candidates": [
        {"setup": "ORB", "trigger": 101.0, "stop": 99.0, "target_1": 103.0, "basis": "b"}]}}}}
    gp = await a5.game_plan(NEXT, "morning", data, [], preview=True)
    assert gp.source == "preview" and [w.ticker for w in gp.watch] == ["NVDA"]


@pytest.mark.rule("SEC-19")
async def test_ideas_alert_uses_html_other_kinds_stay_plain() -> None:
    bot = FakeBot()
    tg = TelegramNotifier(TOKEN, "42", transport=bot.transport())
    await tg.send("<b>ideas</b>", kind="next_day_ideas")
    await tg.send("a < b", kind="alert")
    assert bot.sent[0]["parse_mode"] == "HTML" and "parse_mode" not in bot.sent[1]


@pytest.mark.rule("SEC-6")
def test_http_request_lines_with_the_token_are_not_logged() -> None:
    configure_logging("INFO", json_logs=True)
    assert not logging.getLogger("httpx").isEnabledFor(logging.INFO)
    assert logging.getLogger("httpx").isEnabledFor(logging.WARNING)

