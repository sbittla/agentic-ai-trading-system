"""OPTIONS_REQUIREMENTS: A13 data, A14 strategist, A15 risk, A16 monitor, desk, BR, OP-14."""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import PLAIN_DAY, et
from tests.replay.helpers import Harness
from trader.config import OptionsCfg, default_config
from trader.journal import schema as s
from trader.models.reasons import Reason
from trader.options import monitor, pricing
from trader.options.data import (
    OptionQuote,
    OptionsData,
    events_in,
    expected_move,
    iv_rank,
    parse_occ,
    quote_from_snapshot,
    vol_context,
    years_to,
)
from trader.options.desk import OptionsDesk
from trader.options.strategist import (
    View,
    format_suggestion,
    liquid,
    pick_expiry,
    planned_exit,
    size,
    structures_for,
    suggest,
)

OCFG = OptionsCfg()


# --- synthetic chain -----------------------------------------------------------------------------

def fridays(today: date, n: int = 9) -> list[date]:
    d = today + timedelta(days=(4 - today.weekday()) % 7 or 7)
    return [d + timedelta(days=7 * i) for i in range(n)]


def chain(spot: float, now: datetime, iv: float = 0.35, step: float = 1.0,
          spread: float = 0.02, oi: int = 2000, vol: int = 500, skew: float = 0.0,
          exps: list[date] | None = None) -> list[OptionQuote]:
    """Black-Scholes mids with a ``spread`` bid/ask around them; puts get +skew IV."""
    today = now.astimezone(UTC).date()
    out = []
    for e in exps or fridays(today):
        t = years_to(e, now)
        k = math.floor(spot * 0.85)
        while k <= spot * 1.15:
            for kind in ("C", "P"):
                v = iv + (skew if kind == "P" else 0.0)
                mid = pricing.price(kind, spot, k, t, v)
                if mid < 0.05:
                    continue
                half = max(mid * spread / 2, 0.01)
                g = pricing.greeks(kind, spot, k, t, v)
                out.append(OptionQuote(occ_symbol("NVDA", e, kind, k), "NVDA", e, kind, k,
                                       round(mid - half, 2), round(mid + half, 2), mid, vol,
                                       oi, v, g.delta, g.gamma, g.theta, g.vega, now))
            k += step
    return out


def occ_symbol(underlying: str, expiry: date, kind: str, strike: float) -> str:
    """Build an OCC option symbol for fixtures (production only parses them)."""
    return f"{underlying}{expiry:%y%m%d}{kind}{round(strike * 1000):08d}"


class FakeClient:
    """Alpaca-shaped JSON from a synthetic chain (exercises the parsers)."""

    feed = "indicative"

    def __init__(self, quotes: list[OptionQuote]) -> None:
        self.quotes = quotes
        self.calls: list[str] = []

    async def contracts(self, underlying: str, a: date, b: date, lo: float, hi: float
                        ) -> list[dict[str, Any]]:
        self.calls.append("contracts")
        return [{"symbol": q.symbol, "open_interest": str(q.open_interest)}
                for q in self.quotes if a <= q.expiry <= b and lo <= q.strike <= hi]

    async def snapshots(self, underlying: str, a: date, b: date, lo: float, hi: float
                        ) -> dict[str, dict[str, Any]]:
        self.calls.append("snapshots")
        return {q.symbol: {"latestQuote": {"bp": q.bid, "ap": q.ask,
                                           "t": q.quote_ts.isoformat() if q.quote_ts else None},
                           "latestTrade": {"p": q.last}, "dailyBar": {"v": q.volume},
                           "impliedVolatility": q.iv,
                           "greeks": {"delta": q.delta, "gamma": q.gamma, "theta": q.theta,
                                      "vega": q.vega}}
                for q in self.quotes if a <= q.expiry <= b and lo <= q.strike <= hi}


NOW = et(PLAIN_DAY, "09:50")


def bull_view(**kw: Any) -> View:
    base: dict[str, Any] = dict(ticker="NVDA", direction="bull", spot=100.0, invalidation=99.0,
                                t1=101.5, t2=103.0, style="day", score=75, setup="ORB",
                                window="morning")
    base.update(kw)
    return View(**base)


def bear_view(**kw: Any) -> View:
    base: dict[str, Any] = dict(ticker="NVDA", direction="bear", spot=100.0, invalidation=101.0,
                                t1=98.5, t2=97.0, style="day", score=70, source="bear_setup",
                                setup="ORB_BREAKDOWN", window="morning")
    base.update(kw)
    return View(**base)


# --- pricing / A13 ------------------------------------------------------------------------------

@pytest.mark.rule("OP-7")
def test_black_scholes_parity_greeks_and_implied_vol() -> None:
    s_, k, t, v = 100.0, 100.0, 30 / 365, 0.3
    c, p = pricing.price("C", s_, k, t, v), pricing.price("P", s_, k, t, v)
    assert c - p == pytest.approx(s_ - k * math.exp(-pricing.RATE * t), abs=1e-6)
    g = pricing.greeks("C", s_, k, t, v)
    assert 0.5 < g.delta < 0.6 and g.theta < 0 and g.vega > 0
    assert pricing.greeks("P", s_, k, t, v).delta == pytest.approx(g.delta - 1)
    assert pricing.implied_vol("C", c, s_, k, t) == pytest.approx(v, abs=1e-3)
    assert pricing.price("P", 90, 100, 0, 0.3) == 10.0


@pytest.mark.rule("A13-1")
@pytest.mark.rule("A13-2")
async def test_chain_and_snapshots_parse_with_oi_and_cache() -> None:
    assert parse_occ("NVDA261024C00130000") == ("NVDA", date(2026, 10, 24), "C", 130.0)
    assert parse_occ(occ_symbol("NVDA", date(2026, 10, 24), "P", 127.5)) == (
        "NVDA", date(2026, 10, 24), "P", 127.5)
    assert parse_occ("garbage") is None
    clock = [NOW]
    fake = FakeClient(chain(100, NOW))
    data = OptionsData(fake, None, now=lambda: clock[0])
    qs = await data.quotes("NVDA", 100, PLAIN_DAY, (5, 14))
    assert qs and all(q.open_interest == 2000 for q in qs)
    assert all(5 <= q.dte(PLAIN_DAY) <= 14 for q in qs)
    assert fake.calls == ["contracts", "snapshots"]
    await data.quotes("NVDA", 100, PLAIN_DAY, (5, 14))
    assert fake.calls.count("snapshots") == 1                # 15-s cache
    clock[0] = NOW + timedelta(seconds=16)
    await data.quotes("NVDA", 100, PLAIN_DAY, (5, 14))
    assert fake.calls.count("snapshots") == 2


@pytest.mark.rule("A13-3")
def test_vol_context_iv_rank_expected_move_and_skew() -> None:
    qs = chain(100, NOW, iv=0.30, skew=0.04)
    e = sorted({q.expiry for q in qs})[1]
    em = expected_move(qs, 100, e)
    assert em is not None and 0.03 * 100 < em < 0.08 * 100
    rank, pct, n = iv_rank([0.2, 0.25, 0.4, 0.5, 0.3, 0.35], 0.45)
    assert rank == pytest.approx(83.3, abs=0.1) and pct == pytest.approx(83.3, abs=0.1) \
        and n == 6
    assert iv_rank([0.3, 0.3], 0.3) == (None, None, 2)
    ctx = vol_context("NVDA", qs, 100, PLAIN_DAY, [0.25] * 30)
    assert ctx.atm_iv_30d == pytest.approx(0.32, abs=0.03)
    assert ctx.history_label == "short history: 30 days"
    assert ctx.skew is not None and ctx.skew > 0.03                       # puts richer
    assert "IV rank" in ctx.text() and "Expected move" in ctx.text()


@pytest.mark.rule("A13-4")
def test_events_inside_expiry() -> None:
    ev = events_in(date(2026, 10, 30), PLAIN_DAY, date(2026, 10, 28), date(2026, 11, 5),
                   [(date(2026, 10, 28), "FOMC")])
    assert ev == ["earnings Oct 28", "FOMC Oct 28"]


@pytest.mark.rule("A13-5")
def test_stale_or_zero_bid_quotes_are_unusable() -> None:
    q = chain(100, NOW)[0]
    assert q.usable(NOW)
    assert not q.usable(NOW + timedelta(seconds=61))
    snap = {"latestQuote": {"bp": 0, "ap": 1.2, "t": NOW.isoformat()}}
    zero = quote_from_snapshot(q.symbol, snap)
    assert zero is not None and not zero.usable(NOW)
    sug, _ = suggest(bull_view(), chain(100, NOW - timedelta(minutes=5)), cfg=OCFG, now=NOW,
                     equity=25000, risk_pct=0.5, iv_rank=45)
    assert sug == []                                                      # all stale


@pytest.mark.rule("A13-6")
async def test_iv_daily_history_is_stored_once(tmp_path: Path) -> None:
    from trader.journal.store import SqlJournal

    j = SqlJournal(f"sqlite:///{(tmp_path / 'j.db').as_posix()}")
    data = OptionsData(FakeClient(chain(100, NOW)), j, now=lambda: NOW)
    ctx, _ = await data.context("NVDA", 100, PLAIN_DAY)
    data.store_daily(ctx, PLAIN_DAY)
    data.store_daily(ctx, PLAIN_DAY)
    rows = j.rows(s.options_iv_daily)
    assert len(rows) == 1 and rows[0]["atm_iv_30d"] == pytest.approx(ctx.atm_iv_30d)
    assert data.iv_history("NVDA") == [pytest.approx(ctx.atm_iv_30d)]


# --- A14 ----------------------------------------------------------------------------------------

@pytest.mark.rule("OP-1")
@pytest.mark.rule("A15-4")
def test_structure_matrix_and_account_level() -> None:
    assert structures_for("bull", 20, OCFG) == ["long_call", "call_debit"]
    assert structures_for("bull", 45, OCFG) == ["call_debit"]
    assert structures_for("bull", 75, OCFG) == ["put_credit"]
    assert structures_for("bear", 20, OCFG) == ["long_put", "put_debit"]
    assert structures_for("bear", 75, OCFG) == ["call_credit"]
    assert structures_for("bear", None, OCFG) == ["put_debit"]           # short history
    assert structures_for("bull", 20, OCFG, account_level=2) == ["long_call"]
    assert structures_for("bull", 75, OCFG, account_level=2) == []


@pytest.mark.rule("OP-2")
def test_expiry_by_style_and_earnings() -> None:
    exps = fridays(PLAIN_DAY)       # Oct 23, 30, ...
    assert pick_expiry(exps, PLAIN_DAY, "day", OCFG) == date(2026, 10, 23)
    sw = pick_expiry(exps, PLAIN_DAY, "swing", OCFG)
    assert sw is not None and 21 <= (sw - PLAIN_DAY).days <= 45
    assert pick_expiry(exps, PLAIN_DAY, "day", OCFG, {date(2026, 10, 23)}) == date(2026, 10, 30)
    qs = chain(100, NOW)
    sug, notes = suggest(bull_view(t1=102.0), qs, cfg=OCFG, now=NOW, equity=100000,
                         risk_pct=0.5, iv_rank=45, earnings=date(2026, 10, 20))
    assert sug == [] and "earnings" in notes[0]
    sug, _ = suggest(bull_view(t1=102.0, allow_earnings=True), qs, cfg=OCFG, now=NOW,
                     equity=100000, risk_pct=0.5, iv_rank=45, earnings=date(2026, 10, 20))
    assert sug


@pytest.mark.rule("OP-3")
@pytest.mark.rule("OP-4")
def test_debit_spread_strikes_tied_to_levels_and_liquidity() -> None:
    qs = chain(100, NOW)
    sug, _ = suggest(bull_view(t1=102.0), qs, cfg=OCFG, now=NOW, equity=100000, risk_pct=0.5,
                     iv_rank=45)
    assert len(sug) == 1 and sug[0].structure == "call_debit"
    long_leg, short_leg = sug[0].legs
    assert 0.45 <= (long_leg.quote.delta or 0) <= 0.75 and long_leg.side == 1
    assert short_leg.quote.strike == 102.0 and short_leg.side == -1      # at T1
    # the T1 strike is illiquid -> the next strike beyond it
    thin = [q if not (q.strike == 102.0 and q.kind == "C") else
            OptionQuote(**{**q.__dict__, "open_interest": 10}) for q in qs]
    sug, _ = suggest(bull_view(t1=102.0), thin, cfg=OCFG, now=NOW, equity=100000,
                     risk_pct=0.5, iv_rank=45)
    assert sug and sug[0].legs[1].quote.strike == 103.0
    wide = OptionQuote(**{**qs[0].__dict__, "bid": 1.0, "ask": 1.2})
    assert liquid(wide, OCFG, NOW) == (False, "spread 18.2%")
    early = OptionQuote(**{**qs[0].__dict__, "volume": 50})
    assert liquid(early, OCFG, et(PLAIN_DAY, "09:40"))[0]                 # >= 20 before 10:00
    assert not liquid(early, OCFG, et(PLAIN_DAY, "10:30"))[0]


@pytest.mark.rule("OP-3")
def test_credit_spread_short_strike_beyond_invalidation() -> None:
    qs = chain(100, NOW, iv=0.55)
    sug, notes = suggest(bear_view(invalidation=101.0, t1=97.5), qs, cfg=OCFG, now=NOW,
                         equity=25000, risk_pct=0.5, iv_rank=75)
    if not sug:
        pytest.fail(f"no call credit spread: {notes}")
    sg = sug[0]
    short_leg, long_leg = sg.legs
    assert sg.structure == "call_credit" and short_leg.side == -1
    assert short_leg.quote.strike > 101.0 and abs(short_leg.quote.delta or 1) <= 0.30
    assert long_leg.quote.strike > short_leg.quote.strike
    assert sg.max_loss == pytest.approx((sg.width + sg.est_fill) * 100)
    assert sg.breakeven == pytest.approx(short_leg.quote.strike - sg.est_fill)


@pytest.mark.rule("OP-5")
def test_sizing_from_risk_rules() -> None:
    assert size(185, 25000, 0.5, OCFG, 1.85) == 0         # 125 budget < 185
    assert size(185, 100000, 0.5, OCFG, 1.85) == 2        # 500 / 185
    assert size(185, 100000, 0.5, OCFG, 1.85, signal_only=True) == 1
    assert size(50, 1_000_000, 0.5, OCFG, 0.5) == 10      # max_contracts
    assert size(100, 20000, 5.0, OCFG, 1.0) == 2          # premium cap 1% = 200 / 100


@pytest.mark.rule("OP-6")
@pytest.mark.rule("OP-7")
@pytest.mark.rule("OP-11")
def test_numbers_exits_and_telegram_format() -> None:
    sug, _ = suggest(bear_view(t1=98.0), chain(100, NOW), cfg=OCFG, now=NOW, equity=100000,
                     risk_pct=0.5, iv_rank=45)
    assert sug, "put debit spread expected"
    sg = sug[0]
    assert sg.structure == "put_debit"
    assert sg.breakeven == pytest.approx(sg.legs[0].quote.strike - sg.est_fill)
    assert sg.max_loss == pytest.approx(sg.est_fill * 100)
    assert sg.max_gain == pytest.approx((sg.width - sg.est_fill) * 100)
    assert sg.pnl_t1 > 0 > sg.pnl_inv and sg.rr_t1 >= OCFG.min_rr_at_t1
    assert sg.exit_at == et(PLAIN_DAY, "15:45")
    assert 0 < sg.pop < 1 and sg.risk_usd == sg.contracts * sg.max_loss
    text = format_suggestion(sg)
    for part in ("📉 OPTIONS IDEA · NVDA bearish (ORB_BREAKDOWN", "PAPER/ADVISORY",
                 "Put debit spread", "max loss $", "breakeven", "at T1: ~+",
                 "at invalidation: ~-", "IV rank 45", "OI 2.0k / 2.0k",
                 "Exit: stock > 101.00, or at T1, or by 15:45 today",
                 "Prices indicative (free feed). Not advice."):
        assert part in text, part
    swing_exit = planned_exit(bull_view(style="swing"), date(2026, 11, 27), NOW)
    assert swing_exit.date() == date(2026, 10, 30)                       # 10 sessions first
    near = planned_exit(bull_view(), PLAIN_DAY, NOW)
    assert near == et(PLAIN_DAY, "15:00")                                 # not in the last hour


@pytest.mark.rule("OP-9")
def test_gate_before_sending() -> None:
    qs = chain(100, NOW)
    assert suggest(bull_view(checklist_ok=False), qs, cfg=OCFG, now=NOW, equity=25000,
                   risk_pct=0.5, iv_rank=45)[0] == []
    e = sorted({q.expiry for q in qs})[0]
    _, notes = suggest(bull_view(t1=102.0), qs, cfg=OCFG, now=NOW, equity=100000,
                       risk_pct=0.5, iv_rank=45, expected_moves={e: 1.0})
    assert notes == ["call_debit: T1 beyond 1.5x the expected move (1.00)"]
    _, notes = suggest(bull_view(t1=101.0), qs, cfg=OCFG, now=NOW, equity=100000,
                       risk_pct=0.5, iv_rank=45)
    assert notes == ["call_debit: reward/risk at T1 0.80 < 1.5"]


# --- A15 ----------------------------------------------------------------------------------------

def desk_for(tmp_path: Path, quotes: list[OptionQuote] | None = None,
             cfg: Any = None) -> tuple[Harness, OptionsDesk, FakeClient]:
    h = Harness(cfg or default_config(), PLAIN_DAY, tmp_path)
    eng = h.engine
    eng.risk.start_day(PLAIN_DAY, Decimal("100000"))
    eng.md.state["NVDA"].last_price = 100.0
    fake = FakeClient(quotes if quotes is not None else chain(100, NOW))
    desk = OptionsDesk(eng.cfg, eng, OptionsData(fake, eng.journal, now=eng.now))
    eng.options = desk
    h.clock.advance_to(NOW.astimezone(UTC))
    return h, desk, fake


@pytest.mark.rule("A15-1")
@pytest.mark.rule("A15-2")
@pytest.mark.rule("A15-3")
@pytest.mark.rule("A15-5")
async def test_options_risk_checks(tmp_path: Path) -> None:
    h, desk, _ = desk_for(tmp_path)
    sug, _ = suggest(bear_view(t1=98.0), chain(100, NOW), cfg=OCFG, now=NOW, equity=100000,
                     risk_pct=0.5, iv_rank=45)
    sg = sug[0]
    rk = desk.risk
    assert rk.check(sg, NOW) == (True, "")
    st = h.engine.risk.state
    st.window_entries.add(("morning", "NVDA"))
    assert rk.check(sg, NOW)[0]                       # a long stock entry, bearish idea: ok
    bull, _ = suggest(bull_view(t1=102.0), chain(100, NOW), cfg=OCFG, now=NOW,
                      equity=100000, risk_pct=0.5, iv_rank=45)
    assert rk.check(bull[0], NOW)[1].startswith("A15-2 stock entry")
    st.window_entries.clear()
    st.trades_today["morning"] = 3
    assert "trade budget" in rk.check(sg, NOW)[1]
    st.trades_today["morning"] = 0
    assert rk.check(sg, et(PLAIN_DAY, "15:46"))[1].startswith("A15-3 no new")
    assert "ex-dividend" in rk.check(bull[0].__class__(**{**bull[0].__dict__,
                                                          "structure": "call_credit"}),
                                     NOW, ex_div=PLAIN_DAY + timedelta(days=2))[1]
    rk.state.open_tickers["x"] = "AVGO"                                   # semis group
    assert rk.check(sg, NOW)[1].startswith("A15-5")
    rk.state.open_tickers.clear()
    # an option loss locks the stock (A15-1) and then the options too
    rk.closed(sg, -120.0, NOW)
    assert h.engine.compliance.active_locks(NOW, "NVDA")
    assert rk.check(sg, NOW)[1].startswith("A15-1")


@pytest.mark.rule("A15-3")
async def test_fomc_day_ideas_only_before_1330(tmp_path: Path) -> None:
    h, desk, _ = desk_for(tmp_path)
    sug, _ = suggest(bear_view(t1=98.0), chain(100, NOW), cfg=OCFG, now=NOW, equity=100000,
                     risk_pct=0.5, iv_rank=45)

    class Fomc:
        def is_fomc(self, d: date) -> bool:
            return True

    desk.risk.macro = Fomc()
    assert desk.risk.check(sug[0], et(PLAIN_DAY, "13:31"))[1].startswith("A15-3 FOMC")
    assert desk.risk.check(sug[0], et(PLAIN_DAY, "13:00"))[0]


# --- desk, OP-10/12/13/15/20 ----------------------------------------------------------------

@pytest.mark.rule("OP-10")
@pytest.mark.rule("OP-15")
@pytest.mark.rule("A16-1")
async def test_view_sends_one_idea_per_ticker_direction_window(tmp_path: Path) -> None:
    h, desk, _ = desk_for(tmp_path)
    sent = await desk.on_view(bear_view(t1=98.0))
    assert len(sent) == 1
    assert any("OPTIONS IDEA" in m for _, m in h.notifier.sent)
    row = h.journal.rows(s.option_suggestions)[0]
    assert row["status"] == "open" and row["direction"] == "bear" and row["legs_json"]
    assert row["detail_json"]["entry_quotes"][0]["ts"]                    # A13-6
    assert await desk.on_view(bear_view(t1=98.0)) == []                    # OP-15
    assert any("OP-15" in why for _, why in desk.dropped)
    assert h.engine.risk.state.trades_today["morning"] == 1               # A15-2: one trade


@pytest.mark.rule("OP-12")
@pytest.mark.rule("A16-1")
@pytest.mark.rule("A16-2")
async def test_follow_ups_exit_and_grade(tmp_path: Path) -> None:
    h, desk, _ = desk_for(tmp_path)
    [sg] = await desk.on_view(bear_view(t1=98.0))
    h.engine.md.state["NVDA"].last_price = 97.9                           # T1 hit
    later = NOW + timedelta(minutes=10)
    h.clock.advance_to(later.astimezone(UTC))
    desk.data.client.quotes = chain(97.9, later)                          # type: ignore[attr-defined]
    await desk.on_minute(later)
    assert not desk.book.open
    row = h.journal.rows(s.option_suggestions)[0]
    assert row["status"] == "closed" and row["exit_reason"] == "T1"
    assert row["stock_view_right"] is True and float(row["pnl"]) > 0 and row["r_multiple"] > 0
    msgs = "\n".join(m for _, m in h.notifier.sent)
    assert "T1 reached: CLOSE the spread (plan)" in msgs and "paper result +" in msgs


@pytest.mark.rule("OP-12")
def test_follow_up_alerts_each_once() -> None:
    sug, _ = suggest(bull_view(t1=101.5, style="day"), chain(100, NOW, iv=0.25), cfg=OCFG,
                     now=NOW, equity=100000, risk_pct=0.5, iv_rank=20)
    single = next(x for x in sug if x.structure == "long_call")
    book = monitor.OptionsBook(None)
    tr = book.add(single, "")
    msgs, reason = book.mark(tr, 101.6, None, NOW + timedelta(minutes=5))
    assert reason is None and tr.half_pnl is not None and "TAKE 50%" in msgs[0]
    q = single.legs[0].quote
    crushed = {q.symbol: OptionQuote(**{**q.__dict__, "iv": q.iv * 0.7,      # type: ignore[operator]
                                        "bid": 0.2, "ask": 0.22})}
    msgs, _ = book.mark(tr, 100.0, crushed, NOW + timedelta(minutes=6), ["earnings Oct 21"])
    text = "\n".join(msgs)
    assert "down 50% of premium" in text and "crush" in text and "earnings Oct 21" in text
    assert "3 DTE" not in text
    msgs2, _ = book.mark(tr, 100.0, crushed, NOW + timedelta(minutes=7), ["earnings Oct 21"])
    assert msgs2 == []                                                    # each once
    msgs, reason = book.mark(tr, 100.5, None, et(PLAIN_DAY, "15:45"))
    assert reason == "TIME"
    res = book.close(tr, reason, et(PLAIN_DAY, "15:45"))
    assert res["per_contract"] == pytest.approx(0.5 * tr.half_pnl + 0.5 * tr.last_pnl)


@pytest.mark.rule("OP-6")
def test_credit_spread_exits() -> None:
    sug, _ = suggest(bear_view(invalidation=101.0, t1=97.5), chain(100, NOW, iv=0.55),
                     cfg=OCFG, now=NOW, equity=25000, risk_pct=0.5, iv_rank=75)
    book = monitor.OptionsBook(None)
    tr = book.add(sug[0], "")
    k = sug[0].legs[0].quote.strike
    assert book.mark(tr, k + 0.01, None, NOW)[1] == "INVALIDATION"        # above 101 first
    tr2 = book.add(sug[0], "")
    far = NOW + timedelta(days=4)
    assert book.mark(tr2, 95.0, None, far)[1] in ("PROFIT_50", "TIME")


@pytest.mark.rule("OP-15")
async def test_follow_ups_grouped_per_minute(tmp_path: Path) -> None:
    h, desk, _ = desk_for(tmp_path)
    desk.pending_followups = ["a", "b"]
    await desk.flush(NOW)
    desk.pending_followups = ["c"]
    await desk.flush(NOW + timedelta(seconds=30))
    assert [m.split("] ")[-1] for _, m in h.notifier.sent] == ["a\nb"]
    await desk.flush(NOW + timedelta(seconds=61))
    assert h.notifier.sent[-1][1].endswith("c")


@pytest.mark.rule("OP-13")
async def test_commands(tmp_path: Path) -> None:
    h, desk, _ = desk_for(tmp_path)
    iv = await desk.command("IV", "NVDA")
    assert iv.startswith("IV NVDA") and "IV rank" in iv
    ch = await desk.command("CHAIN", "NVDA")
    assert ch.startswith("CHAIN NVDA Oct 23") and "strike | call" in ch and "100 |" in ch
    reply = await desk.command("OPT", "NVDA BEAR")
    assert "options idea" in reply or reply.startswith("No options suggestion")
    opts = await desk.command("OPTS", "")
    assert "promotion gate" in opts
    assert (await desk.command("OPTIONS", "OFF")).startswith("Options suggestions off")
    assert await desk.on_view(bear_view()) == []
    assert (await desk.command("OPT", "")).startswith("usage")
    # through the engine's command path (SEC-1 allow-list)
    from trader.models.messages import Command

    out = await h.engine.handle_command(Command(name="IV", arg="NVDA", sender="telegram:42",
                                                received_at=NOW, source="telegram"))
    assert out.startswith("IV NVDA")


@pytest.mark.rule("OP-20")
async def test_prefetch_warms_the_cache(tmp_path: Path) -> None:
    h, desk, fake = desk_for(tmp_path)
    await desk.prefetch("NVDA")
    n = fake.calls.count("snapshots")
    await desk.prefetch("NVDA")                       # once per ticker per day
    assert fake.calls.count("snapshots") == n == 1
    await desk.data.quotes("NVDA", 100, PLAIN_DAY, OCFG.day_dte)
    assert fake.calls.count("snapshots") == 1         # the setup's call hits the cache


@pytest.mark.rule("OP-8")
async def test_after_close_rationale_rejects_numbers(tmp_path: Path) -> None:
    from trader.interfaces import LLMResult
    from trader.options.desk import OptionRationaleOut

    h, desk, _ = desk_for(tmp_path)
    sug, _ = suggest(bear_view(t1=98.0), chain(100, NOW), cfg=OCFG, now=NOW, equity=100000,
                     risk_pct=0.5, iv_rank=45)

    class Llm:
        def __init__(self, text: str) -> None:
            self.text = text

        async def structured(self, **kw: Any) -> LLMResult:
            return LLMResult(ok=True, value=OptionRationaleOut(rationale=self.text),
                             model="m")

    h.engine.llm = Llm("Cheap defined risk aimed at the target.")
    assert await desk.explain(sug[0]) == "Cheap defined risk aimed at the target."
    h.engine.llm = Llm("It makes 300 dollars.")
    assert await desk.explain(sug[0]) is None
    assert "fits" not in sug[0].rationale and sug[0].rationale.startswith("Put debit spread")


@pytest.mark.rule("A16-3")
@pytest.mark.rule("A16-4")
def test_report_and_promotion_gate() -> None:
    t0 = datetime(2026, 9, 1, 14, tzinfo=UTC)
    rows = [{"status": "closed", "structure": "put_debit", "direction": "bear",
             "r_multiple": 0.6 if i % 2 else -0.3, "pnl": 60 if i % 2 else -30,
             "iv_rank": 45, "ts": t0 + timedelta(days=i), "expiry": (t0 + timedelta(
                 days=i + 10)).date(), "stock_view_right": i % 3 == 0} for i in range(40)]
    text = monitor.report(rows)
    assert "40 graded" in text and "win 50%" in text and "+0.15R" in text
    assert "by structure: Put debit spread 40" in text and "by IV regime: IV30-60" in text
    assert "8-14 DTE" in text and "view right but option lost" in text
    st = monitor.promotion_status(rows)
    ok, why = st[("put_debit", "bear")]
    assert ok and "PF 2.00" in why
    assert not monitor.promotion_status(rows[:39])[("put_debit", "bear")][0]


@pytest.mark.rule("A16-1")
async def test_open_suggestions_survive_restart(tmp_path: Path) -> None:
    h, desk, _ = desk_for(tmp_path)
    [sg] = await desk.on_view(bear_view(t1=98.0))
    book2 = monitor.OptionsBook(h.journal)
    assert book2.restore() == 1
    again = book2.open[sg.suggestion_id].sug
    assert again.structure == sg.structure and again.legs[0].quote.symbol == \
        sg.legs[0].quote.symbol and again.exit_at == sg.exit_at


# --- BR-1..4 -----------------------------------------------------------------------------------

@pytest.mark.rule("BR-1")
@pytest.mark.rule("BR-2")
@pytest.mark.rule("BR-4")
async def test_orb_breakdown_signal_options_only(tmp_path: Path) -> None:
    from tests.unit.test_req13_workflow import bar

    h, desk, _ = desk_for(tmp_path)
    eng = h.engine
    eng.bear.start_day(PLAIN_DAY)
    st = eng.md.state["NVDA"]
    for _ in range(15):
        st.atr5.update(100.6, 99.4, 100.0)
    st.or5, st.or5_avg_vol = (100.5, 99.8), 1000.0
    st.vwap.update(100.2, 100.2, 100.2, 10_000)
    qqq = eng.md.state["QQQ"]
    qqq.vwap.update(600.0, 600.0, 600.0, 10_000)
    qqq.last_price = 598.0                                               # QQQ below VWAP
    st.rvol = lambda: 2.0                                                 # type: ignore[method-assign]
    b5 = bar("NVDA", et(PLAIN_DAY, "09:40"), 99.9, 99.95, 99.4, 99.5, 2000)
    b5 = b5.model_copy(update={"timeframe": "5Min"})
    sigs = eng.bear.on_5m_close(b5, et(PLAIN_DAY, "09:45"))
    assert [x.setup for x in sigs] == ["ORB_BREAKDOWN"]
    sig = sigs[0]
    assert sig.trigger == 99.8 and sig.invalidation == 100.5 and sig.t1 < sig.trigger
    row = sig.journal_row()
    assert row["context_json"] == {"or_high": 100.5, "or_low": 99.8, "direction": "bear",
                                   "route": "options_only"}
    # BR-2: QQQ above VWAP and no relative weakness -> filtered
    eng.bear.start_day(PLAIN_DAY)
    qqq.last_price = 601.0
    st.change_from_open_pct = lambda: 0.5                                 # type: ignore[method-assign]
    qqq.change_from_open_pct = lambda: 0.1                                # type: ignore[method-assign]
    assert eng.bear.on_5m_close(b5, et(PLAIN_DAY, "09:45")) == []
    # engine path: journaled as a setup and handed to the options desk only
    await eng._on_bears([sig])
    assert eng.journal.rows(s.setups, {"id": sig.setup_id})[0]["context_json"]["route"] == \
        "options_only"
    assert h.broker.submissions == 0


@pytest.mark.rule("BR-4")
def test_stock_side_rejects_bearish_setups(tmp_path: Path) -> None:
    from tests.conftest import AGREE, make_ctx, make_setup
    from trader.models.messages import ComplianceResult

    h = Harness(default_config(), PLAIN_DAY, tmp_path)
    s_ = make_setup().model_copy(update={"context": {"direction": "bear",
                                                     "route": "options_only"}})
    d = h.engine.risk.evaluate(s_, make_ctx(), AGREE, ComplianceResult(proposal_id="p1",
                                                                       ok=True), "p1")
    assert d.result == "REJECT" and d.reason_code == Reason.NT_OPTIONS_ONLY


@pytest.mark.rule("BR-1")
def test_failed_breakout_and_vwap_rejection(tmp_path: Path) -> None:
    from tests.unit.test_req13_workflow import bar

    h = Harness(default_config(), PLAIN_DAY, tmp_path)
    eng = h.engine
    eng.bear.start_day(PLAIN_DAY)
    st = eng.md.state["NVDA"]
    for _ in range(15):
        st.atr5.update(100.6, 99.4, 100.0)
    st.or5, st.hod = (100.5, 99.8), 101.0
    up = bar("NVDA", et(PLAIN_DAY, "09:50"), 100.4, 101.0, 100.4, 100.9).model_copy(
        update={"timeframe": "5Min"})
    down = bar("NVDA", et(PLAIN_DAY, "09:55"), 100.9, 100.9, 100.2, 100.3).model_copy(
        update={"timeframe": "5Min"})
    bd = eng.bear.days["NVDA"]
    assert eng.bear._failed_breakout("NVDA", st, bd, up, et(PLAIN_DAY, "09:55")) is None
    sig = eng.bear._failed_breakout("NVDA", st, bd, down, et(PLAIN_DAY, "10:00"))
    assert sig is not None and sig.setup == "FAILED_BREAKOUT" and sig.invalidation == 101.0
    st.vwap.update(100.5, 100.5, 100.5, 10_000)
    st.bars5 = [bar("NVDA", et(PLAIN_DAY, "09:40"), 100, 100.6, 100.1, 100.2),
                bar("NVDA", et(PLAIN_DAY, "09:45"), 100.2, 100.4, 99.9, 100.0)]
    fail = bar("NVDA", et(PLAIN_DAY, "09:40"), 100.3, 100.7, 100.1, 100.2)
    assert eng.bear._vwap_rejection("NVDA", st, bd, fail, NOW, et(PLAIN_DAY, "09:45").time()) \
        is None and bd.failed_reclaim_high == 100.7
    lower = bar("NVDA", et(PLAIN_DAY, "09:45"), 100.0, 100.3, 99.7, 99.8)
    sig = eng.bear._vwap_rejection("NVDA", st, bd, lower, NOW, et(PLAIN_DAY, "09:50").time())
    assert sig is not None and sig.setup == "VWAP_REJECTION" and sig.invalidation == 100.7


@pytest.mark.rule("BR-3")
def test_next_day_views_include_bearish_candidates() -> None:
    from trader.engine import idea_views
    from trader.models.messages import GamePlan, GamePlanItem

    plans = {"morning": GamePlan(day=PLAIN_DAY, window="morning", watch=(GamePlanItem(
        ticker="AMD", setup="ORB", trigger=151, stop=148, target_1=155),))}
    screens = {"AMD": {"close": 150.0, "range_pos": 0.8, "trend_up": True},
               "KO": {"close": 60.0, "range_pos": 0.05, "trend_up": False},
               "NVDA": {"close": 190.0, "range_pos": 0.5, "trend_up": False}}
    compact = {"KO": {"levels": {"atr14d": 1.2}}}
    views = idea_views(plans, screens, compact, {})
    assert [(v.ticker, v.direction, v.style) for v in views] == [
        ("AMD", "bull", "swing"), ("KO", "bear", "swing")]
    assert views[1].invalidation == pytest.approx(61.2) and views[1].source == "idea"


# --- engine integration, OP-14 -------------------------------------------------------------

@pytest.mark.rule("A15-2")
@pytest.mark.rule("OP-10")
async def test_engine_hands_setups_to_options_after_the_stock_decision(tmp_path: Path) -> None:
    from tests.replay.helpers import orb_day

    h = Harness(default_config(), PLAIN_DAY, tmp_path)
    await h.start()
    fake = FakeClient([])
    desk = OptionsDesk(h.engine.cfg, h.engine, OptionsData(fake, h.journal, now=h.engine.now))
    h.engine.options = desk
    await h.premarket()
    await h.run_bars(orb_day(PLAIN_DAY), until=et(PLAIN_DAY, "09:50"))
    assert h.broker.submissions >= 1                                      # stock taken
    assert any("A15-2 stock entry taken" in why for _, why in desk.dropped)


@pytest.mark.rule("OP-14")
async def test_alerts_bot_options_line() -> None:
    from trader.alerts.options_line import OptionsLine, OptionsLineSettings, view_for

    class St:
        or_low, or_high, vwap = 99.0, 100.5, 99.5

    v = view_for("orb_up", "NVDA", 100.8, St())
    assert v is not None and v.invalidation == 99.0 and v.t1 == pytest.approx(103.5)
    assert view_for("volume", "NVDA", 100.0, St()) is None
    fake = FakeClient(chain(100.8, NOW))
    line = OptionsLine(OptionsLineSettings(enabled=True, fixed_risk_usd=500), fake, lambda: NOW)
    text = await line.line("orb_down", "NVDA", 100.0, St())
    assert text is not None and text.startswith("Options: ") and "indicative" in text
    assert await line.line("orb_down", "NVDA", 100.0, St()) is None       # 10-s rate limit
    off = OptionsLine(OptionsLineSettings(), fake, lambda: NOW)
    assert await off.line("orb_up", "NVDA", 100.8, St()) is None


@pytest.mark.rule("UI-36")
async def test_board_preview_and_status_have_no_side_effects(tmp_path: Path) -> None:
    from trader.services.core_api import CoreApi

    h, desk, _ = desk_for(tmp_path)
    view = desk.owner_view("NVDA", "bear")
    assert view is not None and view.direction == "bear" and view.invalidation > 100
    assert desk.owner_view("ZZZZ", "bull") is None                       # no price
    owner = await desk.preview(view)                    # a 1-ATR view: dropped, with the reason
    assert owner["suggestions"] == [] and "reward/risk" in owner["notes"][0]
    res = await desk.preview(bear_view(t1=98.0))
    assert [ok for _, ok, _ in res["suggestions"]] == [True]
    assert h.journal.rows(s.option_suggestions) == []                     # nothing journaled
    assert not any("OPTIONS IDEA" in m for _, m in h.notifier.sent)       # nothing sent
    board = {r["ticker"]: r for r in await desk.board()}
    nv = board["NVDA"]
    assert nv["spot"] == 100.0 and nv["atm_iv_30d"] and nv["bull"] and nv["bear"]
    assert desk.status()["mode"] == desk.ocfg.mode
    api = CoreApi(h.engine)
    code, p = await api.handle("/options/preview", {"symbol": "NVDA", "direction": "bear"})
    assert code == 200 and p["view"]["direction"] == "bear" and p["notes"]
    assert (await api.handle("/options/preview", {"symbol": "ZZZZ"}))[0] == 404
    code, b = await api.handle("/options/board", {})
    assert code == 200 and b["rows"] and "dropped" in b["status"]
    assert (await api.handle("/options/status", {}))[1]["enabled"] == desk.enabled
