"""REQUIREMENTS 1.3 core trading rules: TW-6, MF-6/DQ-8, RG, EX-5, LAT, SH, AC, EXQ, NW."""

from __future__ import annotations

import ast
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.conftest import ALL_PASS, PLAIN_DAY, ROOT, et
from tests.replay.helpers import Harness, orb_day
from trader.agents.a02_market_data import MarketDataAgent
from trader.agents.a03_news import LexiconScorer, NewsAgent
from trader.agents.a04_technicals import TechnicalAgent, classify_regime
from trader.agents.a07_risk import DecisionContext, RiskManager
from trader.agents.a09_positions import ExitAction, ManagedPosition, PositionMonitor, StopMove
from trader.agents.execution_quality import SlippageMonitor
from trader.agents.shadow import ShadowBook, shadow_report
from trader.agents.speed import LatencyTracker, PreArmCache, PreArmed, expected_setup, rebind
from trader.calendar import MacroCalendar, MarketCalendar, TradingWindows
from trader.config import Config, default_config
from trader.journal import schema as s
from trader.journal.store import SqlJournal
from trader.llm.fakes import RulesLLM
from trader.models.messages import (
    Bar,
    EventFlags,
    GamePlanItem,
    NewsItem,
    Quote,
    Review,
    SetupTriggered,
    TradeProposal,
)
from trader.models.reasons import Reason
from trader.news.sources import RecordedNews

RELEASE_DAY = date(2026, 10, 23)    # a 10:00 release day in the shipped calendar


def setup_at(ts: datetime, ticker: str = "NVDA", trigger: str = "100.00", entry: str = "100.05",
             stop: str = "99.50", t1: str = "100.60", t2: str = "101.15", atr5: float = 0.5,
             window: str = "morning", setup: str = "ORB") -> SetupTriggered:
    return SetupTriggered(setup_id=f"s-{ticker}-{ts.isoformat()}", ts=ts, ticker=ticker,
                          style="day", window=window, setup=setup,  # type: ignore[arg-type]
                          trigger=Decimal(trigger), entry=Decimal(entry), stop=Decimal(stop),
                          t1=Decimal(t1), t2=Decimal(t2), score=80, atr5=atr5, vwap=99.8,
                          rvol=2.0, last_price=100.02, resistance=None, filters=dict(ALL_PASS))


def bar(sym: str, ts: datetime, o: float, h: float, lo: float, c: float, v: int = 1000) -> Bar:
    return Bar(symbol=sym, ts=ts.astimezone(UTC), o=o, h=h, l=lo, c=c, v=v)


def position(**kw: object) -> ManagedPosition:
    base = dict(position_id="p1", ticker="NVDA", style="day", window="morning", setup="ORB",
                planned=True, route="broker", override=False, trigger=Decimal("100"),
                breakout_level=100.0, atr5=Decimal("0.5"), initial_stop=Decimal("99.50"),
                stop=Decimal("99.50"), t1=Decimal("100.60"), t2=Decimal("101.15"),
                opened_at=et(PLAIN_DAY, "09:40"), opened_day=PLAIN_DAY, qty_filled=100,
                cost=Decimal("10005"))
    base.update(kw)
    return ManagedPosition(**base)  # type: ignore[arg-type]


# --- TW-6 ---------------------------------------------------------------------------------

@pytest.mark.rule("TW-6")
def test_release_block_speech_block_and_stale_calendar(cfg: Config, cal: MarketCalendar,
                                                       macro: MacroCalendar) -> None:
    w = TradingWindows(cfg, cal, macro)
    assert macro.is_release_1000(RELEASE_DAY)
    assert w.entry_window(et(RELEASE_DAY, "09:56"), "day")[0] == "morning"
    win, why = w.entry_window(et(RELEASE_DAY, "10:00"), "day")
    assert win is None and why.startswith("TW-6")
    assert w.entry_window(et(RELEASE_DAY, "10:06"), "day")[0] == "morning"
    # a Fed speech at 15:20 blocks 15:17-15:25
    sp = replace(macro, speeches=(et(PLAIN_DAY, "15:20"),))
    w2 = TradingWindows(cfg, cal, sp)
    assert w2.entry_window(et(PLAIN_DAY, "15:16"), "day")[0] == "afternoon"
    assert w2.entry_window(et(PLAIN_DAY, "15:18"), "day")[0] is None
    assert et(PLAIN_DAY, "15:17") in w2.macro_block_starts(PLAIN_DAY)
    # an out-of-date release list blocks the morning window (not the afternoon)
    stale = replace(macro, release_1000=frozenset({date(2026, 1, 5)}))
    w3 = TradingWindows(cfg, cal, stale)
    assert w3.entry_window(et(PLAIN_DAY, "09:40"), "day")[0] is None
    assert w3.entry_window(et(PLAIN_DAY, "15:10"), "day")[0] == "afternoon"
    # the in-memory empty calendar (tests, synthetic replays) never blocks
    assert MacroCalendar.empty().releases_cover(PLAIN_DAY)


@pytest.mark.rule("TW-6")
def test_release_block_reason_code_and_breakeven(cfg: Config, cal: MarketCalendar,
                                                 macro: MacroCalendar) -> None:
    w = TradingWindows(cfg, cal, macro)
    rm = RiskManager(cfg, w)
    rm.start_day(RELEASE_DAY, Decimal("25000"))
    now = et(RELEASE_DAY, "10:01")
    ctx = DecisionContext(now=now, equity=Decimal("25000"), buying_power=Decimal("25000"),
                          last_price=Decimal("100.02"), positions=[], filters=dict(ALL_PASS),
                          planned=True)
    items = rm._items(setup_at(now), ctx, None, None)
    ck2 = next(i for i in items if i.id == "CK-2")
    assert not ck2.passed and ck2.detail.startswith(Reason.NT_MACRO_RELEASE)
    pm = PositionMonitor(cfg, w, cal)
    pm.positions["p1"] = position()
    pm.positions["p2"] = position(position_id="p2", ticker="AAPL")
    acts = pm.macro_breakeven({"NVDA": 100.40, "AAPL": 100.10}, 0.5)   # NVDA +0.7R, AAPL +0.1R
    assert [a.position_id for a in acts] == ["p1"]
    assert isinstance(acts[0], StopMove) and acts[0].new_stop == Decimal("100.05")


# --- MF-6 / DQ-8 --------------------------------------------------------------------------

@pytest.mark.rule("MF-6")
@pytest.mark.rule("DQ-8")
def test_unreliable_quotes_fail_mf4_with_their_own_code(cfg: Config, cal: MarketCalendar,
                                                        macro: MacroCalendar,
                                                        windows: TradingWindows) -> None:
    md = MarketDataAgent(["NVDA", "QQQ", "SMH"])
    ta = TechnicalAgent(cfg, md, cal, macro, require_quotes=True)
    now = et(PLAIN_DAY, "09:45")
    st = md.state["NVDA"]
    st.quote = Quote(symbol="NVDA", ts=now - timedelta(seconds=1), bid=100.0, ask=100.02)
    assert ta.quote_reliable("NVDA", now)
    for q in (Quote(symbol="NVDA", ts=now - timedelta(seconds=5), bid=100.0, ask=100.02),
              Quote(symbol="NVDA", ts=now, bid=0.0, ask=100.02),
              Quote(symbol="NVDA", ts=now, bid=100.05, ask=100.0)):
        st.quote = q
        f = ta.filters("NVDA", now, "ORB", 80)
        assert not f["MF-6"] and not f["MF-4"]   # never passes by default
    rm = RiskManager(cfg, windows)
    rm.start_day(PLAIN_DAY, Decimal("25000"))
    ctx = DecisionContext(now=now, equity=Decimal("25000"), buying_power=Decimal("25000"),
                          last_price=Decimal("100.02"), positions=[],
                          filters={**ALL_PASS, "MF-4": False, "MF-6": False}, planned=True)
    ck3 = next(i for i in rm._items(setup_at(now), ctx, None, None) if i.id == "CK-3")
    assert ck3.detail.startswith(Reason.NT_NO_RELIABLE_QUOTE)


# --- RG -----------------------------------------------------------------------------------

def spy_days(n: int, last_range: float) -> list[Bar]:
    out = []
    d = date(2026, 5, 1)
    for i in range(n):
        d += timedelta(days=1)
        rng = 6.0 if i < n - 5 else last_range
        out.append(Bar(symbol="SPY", ts=et(d, "09:30").astimezone(UTC), timeframe="1Day",
                       o=660, h=660 + rng / 2, l=660 - rng / 2, c=660, v=1))
    return out


@pytest.mark.rule("RG-1")
def test_regime_classification() -> None:
    day = date(2026, 10, 1)
    assert classify_regime(spy_days(100, 6.0), day, 0.2, 1.5, 1.5)[0] == "normal"
    hv, ratio, detail = classify_regime(spy_days(100, 30.0), day, 0.2, 1.5, 1.5)
    assert hv == "high-vol" and ratio is not None and ratio > 1.5 and "ATR14" in detail
    assert classify_regime(spy_days(100, 6.0), day, -2.0, 1.5, 1.5)[0] == "high-vol"


@pytest.mark.rule("RG-2")
def test_high_vol_halves_risk_not_below_drawdown_floor(risk: RiskManager) -> None:
    assert risk.risk_pct(True) == Decimal("0.5")
    risk.state.regime = "high-vol"
    assert risk.risk_pct(True) == Decimal("0.25")
    risk.state.reduced_since = Decimal("26000")   # RC-1 drawdown already at 0.25
    assert risk.risk_pct(True) == Decimal("0.25")


@pytest.mark.rule("RG-3")
def test_trend_day_definition() -> None:
    md = MarketDataAgent(["NVDA"])
    st = md.state["NVDA"]
    d = PLAIN_DAY
    for i in range(10):
        st.bars5.append(bar("NVDA", et(d, "09:30") + timedelta(minutes=5 * i), 100, 101, 99,
                            100.5))
        st.vwap_at_5m.append(100.0 if i != 0 else 101.0)   # 9 of 10 above VWAP = 90%
    st.hod_time_before_1500 = et(d, "14:10")
    assert st.trend_day(80, et(d, "13:00").time())
    st.hod_time_before_1500 = et(d, "11:00")              # high made before 1 PM
    assert not st.trend_day(80, et(d, "13:00").time())
    st.hod_time_before_1500 = et(d, "14:10")
    assert not st.trend_day(95, et(d, "13:00").time())    # 90% < 95%


# --- EX-5 ---------------------------------------------------------------------------------

@pytest.mark.rule("EX-5")
def test_protective_stops_are_stop_market_and_swing_gap_exits(cfg: Config,
                                                             cal: MarketCalendar,
                                                             windows: TradingWindows) -> None:
    src = (ROOT / "src/trader/brokers/alpaca_adapter.py").read_text(encoding="utf-8")
    calls = [n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Call)
             and getattr(n.func, "id", "") == "StopLossRequest"]
    assert calls and all({k.arg for k in c.keywords} == {"stop_price"} for c in calls)
    pm = PositionMonitor(cfg, windows, cal)
    pm.positions["p1"] = position(style="swing", opened_day=date(2026, 10, 15))
    acts = pm.on_1m(bar("NVDA", et(PLAIN_DAY, "09:30"), 99.0, 99.2, 98.8, 99.1), et(
        PLAIN_DAY, "09:31"))
    assert len(acts) == 1 and isinstance(acts[0], ExitAction)
    assert acts[0].reason == Reason.EXIT_GAP_BELOW_STOP


# --- LAT ----------------------------------------------------------------------------------

@pytest.mark.rule("LAT-1")
@pytest.mark.rule("LAT-4")
def test_latency_tracker_orders_missed_and_health(journal: SqlJournal) -> None:
    lt = LatencyTracker(journal, budget_s=10)
    t = et(PLAIN_DAY, "09:40")
    for sec in (2, 3, 4):
        lt.order(setup_at(t), "pid", t + timedelta(seconds=sec), "auto", 1.0, None, False)
    assert lt.health()[0] == "green" and lt.p95_today() == 4
    lt.order(setup_at(t), "pid", t + timedelta(seconds=60), "approve", 1.0, 45.0, True)
    assert lt.p95_today() == 15   # owner time is not counted against the budget
    assert lt.health()[0] == "amber"
    assert lt.missed_trade(setup_at(t), "p2", t, "approve", Reason.NT_APPROVAL_TIMEOUT) \
        == "NT_OWNER_NO"
    assert lt.missed_trade(setup_at(t), "p3", t, "auto", Reason.NT_LLM_INVALID) \
        == "NT_MODEL_TIMEOUT"
    rows = journal.rows(s.latency)
    assert len(rows) == 6 and {r["outcome"] for r in rows} == {"ORDER", "MISSED"}
    assert "missed" in lt.summary()


@pytest.mark.rule("LAT-2")
@pytest.mark.rule("A5-5")
def test_prearm_near_match_and_rebind() -> None:
    pc = PreArmCache(0.3, 0.1)
    item = GamePlanItem(ticker="NVDA", setup="ORB", trigger=100.0, stop=99.5, target_1=100.6)
    assert pc.near(item, 99.9, 0.5) and not pc.near(item, 99.7, 0.5)
    assert not pc.near(item, 100.1, 0.5)     # already through the trigger
    now = et(PLAIN_DAY, "09:38")
    exp = expected_setup(item, "morning", 0.5, Decimal("100.05"), now, "prearm-x")
    prop = TradeProposal(proposal_id="prearm-pid", setup_id="prearm-x", action="take",
                         ticker="NVDA", style="day", window="morning", setup="ORB",
                         planned=True, trigger=100.0, entry=100.05, stop=99.5, target_1=100.6,
                         target_2=101.15, confidence=0.7, thesis="t", expires_at=now,
                         created_at=now, model="m", prompt_version="v1")
    rev = Review(proposal_id="prearm-pid", verdict="agree")
    k = pc.key(PLAIN_DAY, "morning", "NVDA", "ORB")
    pc.entries[k] = PreArmed(item, exp, prop, rev, 1.2, now)
    actual = setup_at(et(PLAIN_DAY, "09:40"), trigger="100.02", entry="100.07", stop="99.52",
                      t1=str(exp.t1 + Decimal("0.02")), t2=str(exp.t2 + Decimal("0.02")))
    pa = pc.match(actual)
    assert pa is not None
    p2, r2 = rebind(pa, actual, "real-pid", et(PLAIN_DAY, "09:40"), et(PLAIN_DAY, "09:42"))
    assert p2.proposal_id == r2.proposal_id == "real-pid" and p2.entry == 100.07
    far = setup_at(et(PLAIN_DAY, "09:40"), trigger="100.30", entry="100.35")
    assert pc.match(far) is None             # more than 0.1 ATR away: redo


@pytest.mark.rule("LAT-3")
async def test_chase_check_at_send_time(tmp_path: Path) -> None:
    h = Harness(default_config(), PLAIN_DAY, tmp_path)
    s_ = setup_at(et(PLAIN_DAY, "09:40"))
    h.engine.md.state["NVDA"].last_price = 100.10
    assert h.engine._chase_at_send(s_, et(PLAIN_DAY, "09:41")) == ""
    h.engine.md.state["NVDA"].last_price = 100.30   # >= trigger + 0.5 ATR5
    assert "CK-6 at send" in h.engine._chase_at_send(s_, et(PLAIN_DAY, "09:41"))


@pytest.mark.rule("LAT-1")
@pytest.mark.rule("SH-1")
async def test_engine_journals_latency_and_shadow_for_a_trade(tmp_path: Path) -> None:
    h = Harness(default_config(), PLAIN_DAY, tmp_path)
    await h.start()
    await h.premarket()
    await h.run_bars(orb_day(PLAIN_DAY))
    await h.at(16, 0)
    lat = h.journal.rows(s.latency)
    assert any(r["outcome"] == "ORDER" and r["ticker"] == "NVDA" for r in lat)
    sh = h.journal.rows(s.shadow_trades)
    taken = [r for r in sh if r["taken"]]
    assert taken and taken[0]["ticker"] == "NVDA" and taken[0]["status"] in ("closed",
                                                                              "nofill")


# --- SH -----------------------------------------------------------------------------------

@pytest.mark.rule("SH-1")
def test_shadow_fill_model(journal: SqlJournal) -> None:
    book = ShadowBook(journal)
    t = et(PLAIN_DAY, "09:40")
    flat = {"morning": et(PLAIN_DAY, "10:30"), "afternoon": et(PLAIN_DAY, "15:55")}
    win = book.add(setup_at(t), "p1", False, "NT_REVIEW_DISAGREE")
    book.on_bar(bar("NVDA", t, 100.1, 100.2, 100.0, 100.1), flat)          # fills at 100.05
    book.on_bar(bar("NVDA", t + timedelta(minutes=1), 100.2, 100.7, 100.2, 100.6), flat)  # T1
    book.on_bar(bar("NVDA", t + timedelta(minutes=2), 100.6, 101.3, 100.6, 101.2), flat)  # T2
    assert win.status == "closed" and win.realized_r == pytest.approx(1.5, abs=0.01)
    lose = book.add(setup_at(t, ticker="AAPL"), "p2", False, "NT_FILTER_MF1")
    book.on_bar(bar("AAPL", t, 100.1, 100.1, 100.0, 100.05), flat)
    book.on_bar(bar("AAPL", t + timedelta(minutes=1), 100.0, 100.0, 99.4, 99.5), flat)
    assert lose.status == "closed" and lose.realized_r == pytest.approx(-1.0, abs=0.01)
    run = book.add(setup_at(t, ticker="MSFT"), "p3", False, "NT_EXPIRED")
    book.on_bar(bar("MSFT", t, 100.3, 100.5, 100.3, 100.4), flat)          # ran away: no fill
    assert run.status == "nofill"
    rows = journal.rows(s.shadow_trades)
    assert {r["status"] for r in rows} == {"closed", "nofill"}


@pytest.mark.rule("SH-2")
def test_shadow_report_flags_rules_that_cost_money() -> None:
    rows = [{"status": "closed", "taken": True, "r_multiple": 0.5, "reason_code": "TAKEN"}]
    rows += [{"status": "closed", "taken": False, "r_multiple": 0.8,
              "reason_code": "NT_REVIEW_DISAGREE"} for _ in range(40)]
    rows += [{"status": "closed", "taken": False, "r_multiple": -0.6,
              "reason_code": "NT_FILTER_MF1"} for _ in range(45)]
    text, flags = shadow_report(rows, 40)
    assert "NT_REVIEW_DISAGREE" in text and "cost money" in text and "saved money" in text
    assert len(flags) == 1 and flags[0].startswith("NT_REVIEW_DISAGREE")


# --- AC -----------------------------------------------------------------------------------

@pytest.mark.rule("AC-1")
@pytest.mark.rule("AC-2")
async def test_cash_account_sizes_from_settled_cash(tmp_path: Path) -> None:
    cfg = default_config()
    h = Harness(cfg, PLAIN_DAY, tmp_path, equity="20000")
    h.broker.multiplier = 1                     # a cash account
    await h.start()
    await h.at(7, 0)
    assert h.engine.account_type == "cash"
    h.journal.insert(s.fills, {"broker_id": "x", "client_order_id": "c", "ticker": "AAPL",
                               "side": "sell", "ts": et(PLAIN_DAY, "07:01"), "qty": 20,
                               "price": Decimal("250")})
    await h.engine._refresh_account()
    assert h.engine.settled_cash == Decimal("15000.00")   # 5,000 of proceeds settle tomorrow
    assert h.engine.buying_power == Decimal("15000.00")
    # AC-2: a config that says margin while Alpaca reports cash blocks the day
    cfg2 = cfg.model_copy(update={"mode": cfg.mode.model_copy(update={"account_type":
                                                                       "margin"})})
    (tmp_path / "b").mkdir(exist_ok=True)
    h2 = Harness(cfg2, PLAIN_DAY, tmp_path / "b", equity="20000")
    h2.broker.multiplier = 1
    await h2.start()
    await h2.at(7, 0)
    assert not h2.engine.entries_enabled
    assert any(Reason.NT_ACCOUNT_MISMATCH in m for _, m in h2.notifier.sent)


# --- EXQ-1 --------------------------------------------------------------------------------

@pytest.mark.rule("EXQ-1")
def test_slippage_switches_to_passive_then_paper(cfg: Config, cal: MarketCalendar,
                                                 macro: MacroCalendar) -> None:
    m = SlippageMonitor(0.1, 3)
    assert SlippageMonitor.slip_r(Decimal("100.10"), Decimal("100"), Decimal("0.5")) == \
        pytest.approx(0.2)
    assert [m.record("ORB", 0.2) for _ in range(3)] == [None, None, "passive"]
    assert [m.record("ORB", 0.05) for _ in range(3)] == [None, None, None]   # recovered
    m2 = SlippageMonitor(0.1, 2)
    assert [m2.record("GAP", 0.3) for _ in range(4)] == [None, "passive", None, "paper"]
    ta = TechnicalAgent(cfg, MarketDataAgent(["NVDA"]), cal, macro, require_quotes=False)
    assert ta.entry_price(100.0, 0.5, "ORB") > Decimal("100")
    ta.passive_setups.add("ORB")
    assert ta.entry_price(100.0, 0.5, "ORB") == Decimal("100.00")


# --- NW -----------------------------------------------------------------------------------

@pytest.mark.rule("NW-1")
async def test_hot_cycle_fetches_only_hot_tickers_and_keeps_summary(
        cfg: Config, cal: MarketCalendar, macro: MacroCalendar, journal: SqlJournal) -> None:
    clock = {"now": et(PLAIN_DAY, "09:40")}
    now = clock["now"]
    src = RecordedNews([], lambda: clock["now"])
    items = src.items
    agent = NewsAgent(cfg, [src], LexiconScorer(), RulesLLM(), journal, cal, macro,
                      lambda: clock["now"])
    agent.last_flags["NVDA"] = EventFlags(ticker="NVDA", updated_at=now, summary="full",
                                          catalyst="earnings beat", tone="positive")
    assert await agent.hot_cycle(["NVDA"]) == []          # nothing new
    clock["now"] = now + timedelta(minutes=1)              # the next 60-second poll
    items.append(NewsItem(id="h1", ticker="NVDA", source="recorded", kind="8-K",
                          published_at=now + timedelta(seconds=30),
                          headline="Nvidia plunges after weak guidance, shares crash"))
    items.append(NewsItem(id="h2", ticker="AAPL", source="recorded",
                          published_at=now + timedelta(seconds=30), headline="Apple news"))
    out = await agent.hot_cycle(["NVDA"])
    assert [f.ticker for f in out] == ["NVDA"]
    nv = out[0]
    assert "NEW_8K" in nv.flags and nv.tone == "negative"
    assert nv.summary == "full" and nv.catalyst == "earnings beat"   # full-cycle summary kept


@pytest.mark.rule("NW-2")
def test_shock_moves_stop_up_never_down(cfg: Config, cal: MarketCalendar,
                                        windows: TradingWindows) -> None:
    pm = PositionMonitor(cfg, windows, cal)
    pm.positions["p1"] = position()
    acts, alerts = pm.shock(bar("NVDA", et(PLAIN_DAY, "09:50"), 101.0, 101.0, 100.10, 100.2),
                            0.5)
    assert alerts and isinstance(acts[0], StopMove) and acts[0].new_stop == Decimal("100.10")
    acts, alerts = pm.shock(bar("NVDA", et(PLAIN_DAY, "09:51"), 100.3, 100.3, 99.0, 99.2), 0.5)
    assert alerts and not acts                   # bar low below the stop: kept (DR-2)
    acts, alerts = pm.shock(bar("NVDA", et(PLAIN_DAY, "09:52"), 100.3, 100.4, 100.0, 100.1),
                            0.5)
    assert not acts and not alerts               # 0.4 < 1.5 x 0.5


@pytest.mark.rule("NW-2")
async def test_negative_8k_on_held_ticker_blocks_new_entries(tmp_path: Path) -> None:
    h = Harness(default_config(), PLAIN_DAY, tmp_path)
    eng = h.engine
    eng.pm.positions["p1"] = position()
    now = et(PLAIN_DAY, "09:50")
    eng.on_news(EventFlags(ticker="NVDA", updated_at=now, flags=("NEW_8K",), tone="negative",
                           summary="restatement"))
    await eng.settle()
    assert "NVDA" in eng.risk.state.news_blocked
    ctx = DecisionContext(now=now, equity=Decimal("25000"), buying_power=Decimal("25000"),
                          last_price=Decimal("100.02"), positions=[], filters=dict(ALL_PASS),
                          planned=True)
    ok, why = eng.risk._limits(setup_at(now), ctx)
    assert not ok and why.startswith(Reason.NT_NEWS_SHOCK)
    assert any("NEWS SHOCK NVDA" in m for _, m in h.notifier.sent)
