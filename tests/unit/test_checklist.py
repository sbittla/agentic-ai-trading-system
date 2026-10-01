"""Entry checklist CK-1..CK-10 (A7-1): each item failing alone rejects with its reason."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.conftest import AGREE, DISAGREE, PLAIN_DAY, et, make_ctx, make_setup
from trader.agents.a07_risk import RiskManager
from trader.models.messages import ComplianceResult

OK_COMP = ComplianceResult(proposal_id="p1", ok=True)


def first_failed(d) -> str:  # type: ignore[no-untyped-def]
    return next(i.id for i in d.checklist if not i.passed)


def test_all_items_pass(risk: RiskManager) -> None:
    d = risk.evaluate(make_setup(), make_ctx(), AGREE, OK_COMP, "p1")
    assert d.result == "APPROVE", d.detail
    assert [i.id for i in d.checklist] == [f"CK-{i}" for i in range(1, 11)]
    assert all(i.passed for i in d.checklist)


@pytest.mark.rule("CK-1")
@pytest.mark.rule("A7-1")
def test_ck1_locked_ticker(risk: RiskManager) -> None:
    comp = ComplianceResult(proposal_id="p1", ok=False, locks_found=("WASH:2026-11-12",),
                            reason="NT_WASH_LOCK")
    d = risk.evaluate(make_setup(), make_ctx(), AGREE, comp, "p1")
    assert first_failed(d) == "CK-1" and d.reason_code == "NT_WASH_LOCK"


@pytest.mark.rule("CK-1")
def test_ck1_kill_switch(risk: RiskManager) -> None:
    risk.state.kill_switch = True
    d = risk.evaluate(make_setup(), make_ctx(), AGREE, OK_COMP, "p1")
    assert first_failed(d) == "CK-1" and d.reason_code == "NT_KILL_SWITCH"


@pytest.mark.rule("CK-2")
def test_ck2_outside_window(risk: RiskManager) -> None:
    d = risk.evaluate(make_setup(), make_ctx(et(PLAIN_DAY, "10:15", 1)), AGREE, OK_COMP, "p1")
    assert first_failed(d) == "CK-2" and d.reason_code == "NT_WINDOW"


@pytest.mark.rule("CK-3")
@pytest.mark.parametrize("mf", ["MF-1", "MF-2", "MF-3", "MF-4", "MF-5"])
def test_ck3_each_filter(risk: RiskManager, mf: str) -> None:
    f = {k: k != mf for k in ("MF-1", "MF-2", "MF-3", "MF-4", "MF-5")}
    d = risk.evaluate(make_setup(), make_ctx(filters=f), AGREE, OK_COMP, "p1")
    assert first_failed(d) == "CK-3" and d.reason_code == f"NT_FILTER_{mf.replace('-', '')}"


@pytest.mark.rule("CK-4")
def test_ck4_not_a_playbook_setup(risk: RiskManager) -> None:
    d = risk.evaluate(make_setup(setup="HOT_TIP"), make_ctx(), AGREE, OK_COMP, "p1")
    assert first_failed(d) == "CK-4"


@pytest.mark.rule("CK-4")
def test_ck4_setup_in_wrong_window(risk: RiskManager) -> None:
    d = risk.evaluate(make_setup(setup="POWER_HOUR_BREAKOUT"), make_ctx(), AGREE, OK_COMP, "p1")
    assert first_failed(d) == "CK-4"


@pytest.mark.rule("CK-4")
@pytest.mark.rule("DR-7")
def test_ck4_unplanned_is_allowed_at_half_size(risk: RiskManager) -> None:
    planned = risk.evaluate(make_setup(), make_ctx(planned=True, equity="100000",
                                                   bp="100000"), AGREE, OK_COMP, "p1")
    unplanned = risk.evaluate(make_setup(), make_ctx(planned=False, equity="100000",
                                                     bp="100000"), AGREE, OK_COMP, "p2")
    assert unplanned.result == "APPROVE"
    assert unplanned.risk_pct == planned.risk_pct / 2


@pytest.mark.rule("CK-5")
def test_ck5_bar_not_closed(risk: RiskManager) -> None:
    d = risk.evaluate(make_setup(bar_closed=False), make_ctx(), AGREE, OK_COMP, "p1")
    assert first_failed(d) == "CK-5"


@pytest.mark.rule("CK-5")
def test_ck5_trigger_in_the_future(risk: RiskManager) -> None:
    d = risk.evaluate(make_setup(ts=et(PLAIN_DAY, "09:50")), make_ctx(et(PLAIN_DAY, "09:45")),
                      AGREE, OK_COMP, "p1")
    assert first_failed(d) == "CK-5"


@pytest.mark.rule("CK-6")
def test_ck6_entry_026_atr_above_trigger_rejected(risk: RiskManager) -> None:
    # ATR5 1.00: 0.26 above the trigger is a chase
    s = make_setup(trigger="100.00", entry="100.26", stop="99.40", atr5=1.0, resistance=None)
    d = risk.evaluate(s, make_ctx(last="100.20"), AGREE, OK_COMP, "p1")
    assert first_failed(d) == "CK-6" and d.reason_code == "NT_CK6"
    ok = make_setup(trigger="100.00", entry="100.25", stop="99.40", atr5=1.0, resistance=None)
    assert risk.evaluate(ok, make_ctx(last="100.20"), AGREE, OK_COMP, "p2").result == "APPROVE"


@pytest.mark.rule("CK-6")
def test_ck6_price_already_ran_away(risk: RiskManager) -> None:
    s = make_setup(trigger="100.00", entry="100.10", stop="99.40", atr5=1.0, resistance=None)
    d = risk.evaluate(s, make_ctx(last="100.50"), AGREE, OK_COMP, "p1")
    assert first_failed(d) == "CK-6"


@pytest.mark.rule("CK-7")
def test_ck7_stop_101_atr_wide_rejected(risk: RiskManager) -> None:
    s = make_setup(trigger="100.00", entry="100.10", stop="99.09", atr5=1.0, resistance=None)
    d = risk.evaluate(s, make_ctx(last="100.05"), AGREE, OK_COMP, "p1")
    assert first_failed(d) == "CK-7" and d.reason_code == "NT_CK7"
    s = make_setup(trigger="100.00", entry="100.10", stop="99.10", atr5=1.0, resistance=None)
    assert risk.evaluate(s, make_ctx(last="100.05"), AGREE, OK_COMP, "p2").result == "APPROVE"


@pytest.mark.rule("CK-7")
def test_ck7_swing_uses_atr14d(risk: RiskManager) -> None:
    s = make_setup(setup="SWING_BASE_BREAKOUT", window="afternoon", style="swing",
                   ts=et(PLAIN_DAY, "15:45"), trigger="100.00", entry="100.05", stop="93.00",
                   atr5=1.0, resistance=None, context={"atr14d": 4.0})
    d = risk.evaluate(s, make_ctx(et(PLAIN_DAY, "15:45", 5), last="100.0"), AGREE, OK_COMP,
                      "p1")
    assert first_failed(d) == "CK-7"   # R 7.05 > 1.5 x 4.0


@pytest.mark.rule("CK-8")
def test_ck8_resistance_too_close(risk: RiskManager) -> None:
    s = make_setup(resistance=191.00)   # entry 190.49, R 0.88 -> needs >= 191.81
    d = risk.evaluate(s, make_ctx(), AGREE, OK_COMP, "p1")
    assert first_failed(d) == "CK-8" and d.reason_code == "NT_CK8"


@pytest.mark.rule("CK-8")
def test_ck8_target_below_2r(risk: RiskManager) -> None:
    s = make_setup(t2="191.50")
    d = risk.evaluate(s, make_ctx(), AGREE, OK_COMP, "p1")
    assert first_failed(d) == "CK-8"


@pytest.mark.rule("CK-9")
@pytest.mark.rule("A6-3")
def test_ck9_review_disagrees_auto_mode(risk: RiskManager) -> None:
    d = risk.evaluate(make_setup(), make_ctx(), DISAGREE, OK_COMP, "p1")
    assert first_failed(d) == "CK-9" and d.reason_code == "NT_REVIEW_DISAGREE"


@pytest.mark.rule("CK-9")
def test_ck9_no_review_no_trade(risk: RiskManager) -> None:
    d = risk.evaluate(make_setup(), make_ctx(), None, OK_COMP, "p1")
    assert first_failed(d) == "CK-9" and d.reason_code == "NT_LLM_INVALID"


@pytest.mark.rule("A6-3")
def test_disagreement_goes_to_owner_in_approve_mode(cfg, windows) -> None:  # type: ignore[no-untyped-def]
    c = cfg.model_copy(update={"mode": cfg.mode.model_copy(update={"day_trades": "approve"})})
    r = RiskManager(c, windows)
    r.start_day(PLAIN_DAY, Decimal("25000"))
    d = r.evaluate(make_setup(), make_ctx(), DISAGREE, OK_COMP, "p1")
    assert d.result == "NEEDS_APPROVAL" and d.override


@pytest.mark.rule("CK-10")
@pytest.mark.rule("A7-4")
def test_ck10_limits(risk: RiskManager) -> None:
    risk.state.halted = "HALT_LL3"
    d = risk.evaluate(make_setup(), make_ctx(), AGREE, OK_COMP, "p1")
    assert first_failed(d) == "CK-10" and d.reason_code == "HALT_LL3"


@pytest.mark.rule("CK-10")
def test_ck10_size_too_small(risk: RiskManager) -> None:
    d = risk.evaluate(make_setup(), make_ctx(equity="500", bp="500"), AGREE, OK_COMP, "p1")
    assert first_failed(d) == "CK-10" and d.reason_code == "SIZE_TOO_SMALL"
