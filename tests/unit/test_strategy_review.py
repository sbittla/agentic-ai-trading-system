"""A5 strategy and A6 review (A5-1..A5-4, A6-1..A6-3, PR-1..PR-4) with scripted Qwen output."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from tests.conftest import PLAIN_DAY, et, make_setup
from trader.agents.a05_strategy import StrategyAgent, proposal_id_for
from trader.agents.a06_review import ReviewAgent
from trader.config import Config
from trader.journal import schema as s
from trader.journal.store import SqlJournal
from trader.llm.fakes import RulesLLM, ScriptedLLM

NOW = et(PLAIN_DAY, "09:45", 3)


def proposal_json(setup, **over) -> str:  # type: ignore[no-untyped-def]
    d = {"action": "take", "ticker": setup.ticker, "style": setup.style,
         "window": setup.window, "side": "buy", "setup": setup.setup,
         "trigger": float(setup.trigger), "entry": float(setup.entry),
         "stop": float(setup.stop), "target_1": float(setup.t1),
         "target_2": float(setup.t2), "confidence": 0.72,
         "thesis": "Clean ORB with volume.", "weakest_point": "late"}
    d.update(over)
    return json.dumps(d)


def data_for(setup):  # type: ignore[no-untyped-def]
    return {"setup": {"ticker": setup.ticker, "setup": setup.setup, "style": setup.style,
                      "window": setup.window, "trigger": float(setup.trigger),
                      "entry": float(setup.entry), "stop": float(setup.stop),
                      "t1": float(setup.t1), "t2": float(setup.t2)}}


@pytest.mark.rule("A5-2")
@pytest.mark.rule("A5-4")
async def test_valid_proposal(cfg: Config, journal: SqlJournal) -> None:
    st = make_setup()
    a5 = StrategyAgent(cfg, ScriptedLLM({"a05_proposal": [proposal_json(st)]}), journal,
                       lambda: NOW)
    out = await a5.propose(st, data_for(st), True, 190.45)
    p = out.proposal
    assert p is not None and p.action == "take"
    assert p.proposal_id == proposal_id_for(st.setup_id)   # set by code
    assert p.expires_at == NOW + timedelta(minutes=2)
    assert p.planned is True and p.model == "scripted" and p.prompt_version == "v1"
    assert journal.rows(s.proposals)[0]["valid"] is True


@pytest.mark.rule("A5-4")
async def test_swing_expires_at_1550(cfg: Config) -> None:
    st = make_setup(setup="SWING_BASE_BREAKOUT", style="swing", window="afternoon",
                    ts=et(PLAIN_DAY, "15:45"), resistance=None)
    a5 = StrategyAgent(cfg, ScriptedLLM({"a05_proposal": [proposal_json(st)]}), None,
                       lambda: et(PLAIN_DAY, "15:45", 4))
    out = await a5.propose(st, data_for(st), True, float(st.entry))
    assert out.proposal is not None and out.proposal.expires_at == et(PLAIN_DAY, "15:50")


@pytest.mark.rule("A5-2")
@pytest.mark.rule("QW-3")
async def test_invalid_json_retried_once_then_dropped(cfg: Config) -> None:
    st = make_setup()
    llm = ScriptedLLM({"a05_proposal": ["not json", proposal_json(st)]})
    out = await StrategyAgent(cfg, llm, None, lambda: NOW).propose(st, data_for(st), True,
                                                                     190.45)
    assert out.proposal is not None   # second attempt valid
    llm = ScriptedLLM({"a05_proposal": ["{bad", "{\"action\": \"maybe\"}"]})
    out = await StrategyAgent(cfg, llm, None, lambda: NOW).propose(st, data_for(st), True,
                                                                     190.45)
    assert out.proposal is None and "LLMInvalidOutput" in (out.error or "")


@pytest.mark.rule("A5-2")
async def test_code_validation_rejects_bad_numbers(cfg: Config) -> None:
    st = make_setup()
    cases = {
        "far": proposal_json(st, trigger=195.0, entry=195.1),               # > 1% from quote
        "hallucinated": proposal_json(st, target_2=float(st.t2) + 0.5),     # not in DATA
        "order": proposal_json(st, stop=float(st.entry) + 1),               # stop above entry
        "lowconf": proposal_json(st, confidence=0.5),
        "ticker": proposal_json(st, ticker="AAPL"),
    }
    for name, raw in cases.items():
        llm = ScriptedLLM({"a05_proposal": [raw]})
        out = await StrategyAgent(cfg, llm, None, lambda: NOW).propose(st, data_for(st), True,
                                                                         190.45)
        assert out.proposal is None, name


@pytest.mark.rule("A5-3")
async def test_skip_names_weakest_point(cfg: Config) -> None:
    st = make_setup()
    raw = proposal_json(st, action="skip", weakest_point="CK-8 prior high just above",
                        confidence=0.3)
    out = await StrategyAgent(cfg, ScriptedLLM({"a05_proposal": [raw]}), None,
                              lambda: NOW).propose(st, data_for(st), True, 190.45)
    assert out.proposal is not None and out.proposal.action == "skip"
    assert "CK-8" in out.proposal.weakest_point


@pytest.mark.rule("A5-1")
async def test_game_plan_validated_and_journaled(cfg: Config, journal: SqlJournal) -> None:
    plan = {"window": "morning", "market_bias": "with_us", "watch": [
        {"ticker": "NVDA", "setup": "ORB", "trigger": 190.4, "stop": 189.61, "target_1": 191.37,
         "why": "gap on news"},
        {"ticker": "AMD", "setup": "ORB", "trigger": 1, "stop": 0.5, "target_1": 2, "why": "x"},
        {"ticker": "AAPL", "setup": "ORB", "trigger": 777.0, "stop": 1, "target_1": 2,
         "why": "invented levels"},
        {"ticker": "META", "setup": "POWER_HOUR_BREAKOUT", "trigger": 760, "stop": 755,
         "target_1": 770, "why": "wrong window"}],
        "off_limits": [{"ticker": "TSLA", "reason": "earnings"}]}
    a5 = StrategyAgent(cfg, ScriptedLLM({"a05_plan": [json.dumps(plan)]}), journal,
                       lambda: NOW)
    data = {"tickers": {"NVDA": {"levels": {"or_high": 190.4, "stop": 189.61, "t1": 191.37}},
                        "META": {"levels": {"x": 760, "y": 755, "z": 770}}}}
    gp = await a5.game_plan(PLAIN_DAY, "morning", data, [{"ticker": "META", "reason": "WASH"}])
    assert [w.ticker for w in gp.watch] == ["NVDA"]
    assert ("TSLA", "earnings") in gp.off_limits and ("META", "WASH") in gp.off_limits
    assert a5.planned("NVDA", "ORB", PLAIN_DAY, "morning")
    assert not a5.planned("NVDA", "VWAP_RECLAIM", PLAIN_DAY, "morning")
    assert len(journal.rows(s.game_plans)) == 1


@pytest.mark.rule("A5-1")
async def test_zero_picks_is_fine_and_fallback_on_failure(cfg: Config) -> None:
    a5 = StrategyAgent(cfg, ScriptedLLM({"a05_plan": ["garbage", "garbage"]}), None,
                       lambda: NOW)
    gp = await a5.game_plan(PLAIN_DAY, "morning", {}, [{"ticker": "AAPL", "reason": "lock"}])
    assert gp.source == "fallback" and gp.watch == () and gp.off_limits == (("AAPL", "lock"),)


@pytest.mark.rule("A6-1")
@pytest.mark.rule("A6-2")
async def test_review_agree_and_disagree(cfg: Config, journal: SqlJournal) -> None:
    st = make_setup()
    a5 = StrategyAgent(cfg, ScriptedLLM({"a05_proposal": [proposal_json(st)]}), None,
                       lambda: NOW)
    p = (await a5.propose(st, data_for(st), True, 190.45)).proposal
    assert p is not None
    rv = {"proposal_id": p.proposal_id, "verdict": "disagree",
          "main_risk": "Prior day high sits just above target 1.", "rules_broken": ["CK-8"],
          "checks": {"chasing": False, "resistance_before_t1": True, "weak_market": False,
                     "late_in_window": False, "event_risk": False, "bad_stop": False}}
    a6 = ReviewAgent(cfg, ScriptedLLM({"a06": [json.dumps(rv)]}), journal, lambda: NOW)
    review, err = await a6.review(p, data_for(st))
    assert review is not None and review.verdict == "disagree"
    assert review.rules_broken == ("CK-8",) and review.checks.resistance_before_t1
    assert journal.rows(s.reviews)[0]["verdict"] == "disagree"
    llm = ScriptedLLM({"a06": [json.dumps({**rv, "proposal_id": "someone-else"})]})
    review, err = await ReviewAgent(cfg, llm, None, lambda: NOW).review(p, data_for(st))
    assert review is None and "does not match" in (err or "")


@pytest.mark.rule("A6-1")
async def test_review_uses_a_separate_prompt(cfg: Config) -> None:
    st = make_setup()
    llm = RulesLLM()
    p = (await StrategyAgent(cfg, llm, None, lambda: NOW).propose(st, data_for(st), True,
                                                                    190.45)).proposal
    assert p is not None
    await ReviewAgent(cfg, llm, None, lambda: NOW).review(p, data_for(st))
    assert llm.calls == ["a05_proposal", "a06"]
