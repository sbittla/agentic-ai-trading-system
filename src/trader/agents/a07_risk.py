"""A7 Risk manager (A7-1..A7-4): checklist, sizing, loss limits and behaviour rules.

Pure code (ADR-006). Limits come from the frozen config and are read-only to every agent
(A7-3, DV-3). Every rejection names the checklist item and a reason code (DV-5).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from typing import Literal

import structlog

from trader.calendar import TradingWindows
from trader.config import Config
from trader.models.messages import (
    AFTERNOON_SETUPS,
    MORNING_SETUPS,
    NEWS_SETUPS,
    SETUP_NAMES,
    SWING_SETUPS,
    ChecklistItem,
    ComplianceResult,
    Decision,
    LimitHit,
    Review,
    SetupTriggered,
)
from trader.models.reasons import Reason
from trader.portfolio import PortfolioStats
from trader.timeutil import et_date

log = structlog.get_logger(__name__)
D0 = Decimal(0)
TOL = Decimal("0.0099")      # stops are rounded down to the cent: R can grow by < 1 cent
TOL_ENTRY = Decimal("0.005")  # entries are rounded half-up: at most half a cent


@dataclass
class OpenPositionView:
    position_id: str
    ticker: str
    group: str
    style: str
    window: str
    qty: int
    entry: Decimal
    stop: Decimal
    last: Decimal
    t1_hit: bool = False
    route: str = "broker"

    @property
    def exposure(self) -> Decimal:
        return self.last * self.qty

    @property
    def unrealized(self) -> Decimal:
        return (self.last - self.entry) * self.qty


@dataclass
class ClosedTrade:
    position_id: str
    ticker: str
    setup: str
    style: str
    window: str
    closed_at: datetime
    pnl: Decimal
    r_multiple: float
    exit_reason: str
    route: str = "broker"
    reduced_size: bool = False


@dataclass
class RiskState:
    day: date | None = None
    equity: Decimal = D0
    day_start_equity: Decimal = D0
    hwm: Decimal = D0
    week_start_equity: Decimal = D0
    week_start: date | None = None
    month_peak: Decimal = D0
    month: tuple[int, int] | None = None
    realized_today: Decimal = D0
    open_pnl: Decimal = D0
    peak_day_pnl: Decimal = D0
    day_r_usd: Decimal = D0          # 1R in dollars at the day's base risk (LL-7)
    trades_today: dict[str, int] = field(default_factory=lambda: {"morning": 0,
                                                                  "afternoon": 0})
    consecutive_losses_today: int = 0
    last_stopout_at: datetime | None = None
    window_entries: set[tuple[str, str]] = field(default_factory=set)
    window_losses: set[tuple[str, str]] = field(default_factory=set)
    halted: str | None = None        # reason code of today's halt
    paper_only_until: date | None = None   # LL-5
    live_disabled: bool = False      # LL-6
    kill_switch: bool = False
    paused_styles: set[str] = field(default_factory=set)
    regime: str = "normal"                 # RG-1 (set by the engine)
    slippage_paper: set[str] = field(default_factory=set)   # EXQ-1: setups back to paper
    news_blocked: set[str] = field(default_factory=set)     # NW-2: tickers blocked today
    reduced_since: Decimal | None = None   # RC-1: HWM at the time size was cut
    reduced_trades: list[float] = field(default_factory=list)
    history: list[ClosedTrade] = field(default_factory=list)
    breaches: int = 0
    portfolio: PortfolioStats = field(default_factory=PortfolioStats)   # PF-1..PF-4

    @property
    def day_pnl(self) -> Decimal:
        return self.realized_today + self.open_pnl

    @property
    def trades_total_today(self) -> int:
        return sum(self.trades_today.values())


@dataclass
class DecisionContext:
    now: datetime
    equity: Decimal
    buying_power: Decimal
    last_price: Decimal
    positions: list[OpenPositionView]
    filters: dict[str, bool]
    planned: bool
    health_ok: bool = True
    health_detail: str = ""
    data_stale: bool = False
    news_available: bool = True
    approvals_available: bool = True
    settled_cash: Decimal | None = None      # AC-1: cash accounts size from settled cash only


class RiskManager:
    def __init__(self, cfg: Config, windows: TradingWindows, state: RiskState | None = None
                 ) -> None:
        self.cfg = cfg
        self.windows = windows
        self.state = state or RiskState()

    # -- day / mark updates ------------------------------------------------------------------
    def start_day(self, d: date, equity: Decimal) -> None:
        st = self.state
        st.day = d
        st.equity = equity
        st.day_start_equity = equity
        st.hwm = max(st.hwm, equity)
        monday = d - timedelta(days=d.weekday())
        if st.week_start != monday:
            st.week_start = monday
            st.week_start_equity = equity
        if st.month != (d.year, d.month):
            st.month = (d.year, d.month)
            st.month_peak = equity
        if st.paper_only_until is not None and d >= st.paper_only_until:
            st.paper_only_until = None
        st.realized_today = st.open_pnl = st.peak_day_pnl = D0
        st.trades_today = {"morning": 0, "afternoon": 0}
        st.consecutive_losses_today = 0
        st.last_stopout_at = None
        st.window_entries.clear()
        st.window_losses.clear()
        st.halted = None
        st.news_blocked = set()
        st.day_r_usd = equity * Decimal(str(self.cfg.risk.risk_per_trade_pct)) / 100
        self._update_reduced()

    def mark(self, now: datetime, equity: Decimal, open_pnl: Decimal) -> list[LimitHit]:
        """Update equity and open P&L; returns any limits newly hit (LL-3, LL-5, LL-6, LL-7)."""
        st = self.state
        st.equity = equity
        st.open_pnl = open_pnl
        st.month_peak = max(st.month_peak, equity)
        if equity > st.hwm:
            st.hwm = equity
        self._update_reduced()
        st.peak_day_pnl = max(st.peak_day_pnl, st.day_pnl)
        return self._check_limits(now)

    def _check_limits(self, now: datetime) -> list[LimitHit]:
        st, r = self.state, self.cfg.risk
        hits: list[LimitHit] = []
        base = st.day_start_equity or st.equity
        if st.halted is None and base > 0 and st.day_pnl <= -base * Decimal(
                str(r.daily_loss_limit_pct)) / 100:
            st.halted = Reason.HALT_LL3
            hits.append(LimitHit(rule="LL-3", reason_code=Reason.HALT_LL3, ts=now,
                                 detail=f"daily loss {st.day_pnl:.2f} <= -{r.daily_loss_limit_pct}%"
                                 " of equity: flatten day trades, no more entries today"))
        if (st.halted is None and st.day_r_usd > 0 and st.peak_day_pnl >= st.day_r_usd
                and st.day_pnl <= st.peak_day_pnl * (1 - Decimal(str(r.profit_giveback_pct))
                                                     / 100)):
            st.halted = Reason.HALT_LL7
            hits.append(LimitHit(rule="LL-7", reason_code=Reason.HALT_LL7, ts=now,
                                 detail=f"P&L {st.day_pnl:.2f} gave back {r.profit_giveback_pct}%"
                                 f" of peak {st.peak_day_pnl:.2f}"))
        if (st.paper_only_until is None and st.week_start_equity > 0
                and st.equity - st.week_start_equity <= -st.week_start_equity * Decimal(
                    str(r.weekly_loss_limit_pct)) / 100):
            d = et_date(now)
            st.paper_only_until = d + timedelta(days=7 - d.weekday())   # next Monday
            hits.append(LimitHit(rule="LL-5", reason_code=Reason.HALT_LL5, ts=now,
                                 detail=f"weekly loss limit {r.weekly_loss_limit_pct}%: paper "
                                 f"only until {st.paper_only_until}"))
        if (not st.live_disabled and st.month_peak > 0
                and st.equity <= st.month_peak * (1 - Decimal(str(r.monthly_drawdown_limit_pct))
                                                  / 100)):
            st.live_disabled = True
            hits.append(LimitHit(rule="LL-6", reason_code=Reason.HALT_LL6, ts=now,
                                 detail=f"monthly drawdown {r.monthly_drawdown_limit_pct}% from "
                                 "the month's peak: live trading off until the owner re-enables"))
        return hits

    def on_trade_closed(self, t: ClosedTrade, now: datetime) -> list[LimitHit]:
        st = self.state
        st.history.append(t)
        st.realized_today += t.pnl
        hits: list[LimitHit] = []
        if t.reduced_size:
            st.reduced_trades.append(t.r_multiple)
        if t.pnl < 0:
            st.consecutive_losses_today += 1
            st.window_losses.add((t.window, self.cfg.canonical(t.ticker)))   # DR-3
            if t.exit_reason in (Reason.EXIT_STOP, Reason.EXIT_BREAKEVEN):
                st.last_stopout_at = now
            if (st.halted is None and st.consecutive_losses_today
                    >= self.cfg.risk.stop_after_consecutive_losses):
                st.halted = Reason.HALT_LL4
                hits.append(LimitHit(rule="LL-4", reason_code=Reason.HALT_LL4, ts=now,
                                     detail=f"{st.consecutive_losses_today} losses in a row "
                                     "today: no more entries"))
        else:
            st.consecutive_losses_today = 0
        st.peak_day_pnl = max(st.peak_day_pnl, st.day_pnl)
        self._update_reduced()
        return hits + self._check_limits(now)

    def _update_reduced(self) -> None:
        """RC-1 / RC-2: halve risk 3% below HWM; full size after a new high or 10 trades with
        positive expectancy at reduced size."""
        st, r = self.state, self.cfg.risk
        if st.hwm <= 0:
            return
        if st.reduced_since is None:
            if st.equity <= st.hwm * (1 - Decimal(str(r.drawdown_trigger_pct)) / 100):
                st.reduced_since = st.hwm
                st.reduced_trades = []
        else:
            n = r.recovery_trades
            recovered = len(st.reduced_trades) >= n and sum(st.reduced_trades[-n:]) / n > 0
            if st.equity > st.reduced_since or recovered:
                st.reduced_since = None
                st.reduced_trades = []

    # -- sizing (A7-2, STRATEGY_SPEC §6) -------------------------------------------------------
    def risk_pct(self, planned: bool) -> Decimal:
        r = self.cfg.risk
        pct = Decimal(str(r.drawdown_risk_per_trade_pct if self.state.reduced_since is not None
                          else r.risk_per_trade_pct))
        if self.state.regime == "high-vol":   # RG-2: halve, never stacked below the RC-1 floor
            hv = max(Decimal(str(r.risk_per_trade_pct)) * Decimal(
                str(self.cfg.regime.risk_factor_high_vol)),
                Decimal(str(r.drawdown_risk_per_trade_pct)))
            pct = min(pct, hv)
        if not planned:   # DR-7
            pct *= Decimal(str(self.cfg.entries.unplanned_size_factor))
        return pct

    def size(self, entry: Decimal, stop: Decimal, equity: Decimal, buying_power: Decimal,
             open_exposure: Decimal, planned: bool, *, ticker: str | None = None,
             style: str | None = None, positions: list[OpenPositionView] | None = None,
             settled_cash: Decimal | None = None) -> tuple[int, Decimal, Decimal, str]:
        """Returns (qty, risk_usd, risk_pct, binding constraint). With ``ticker`` and
        ``positions`` the portfolio caps apply too (PF-1, PF-3, PF-4); with ``settled_cash``
        (a cash account) the settled-funds cap (AC-1)."""
        r = self.cfg.risk
        pct = self.risk_pct(planned)
        risk_usd = equity * pct / 100
        per_share = entry - stop
        if per_share <= 0 or entry <= 0:
            return 0, D0, pct, "invalid"
        qty_risk = int((risk_usd / per_share).to_integral_value(rounding=ROUND_FLOOR))
        qty_cap = int((equity * Decimal(str(r.max_position_pct)) / 100 / entry)
                      .to_integral_value(rounding=ROUND_FLOOR))                     # LL-9
        room = min(buying_power, equity * Decimal(str(r.max_leverage)) - open_exposure)  # LL-11
        qty_bp = max(0, math.floor(room / entry)) if room > 0 else 0
        caps = {"risk": qty_risk, "LL-9 position cap": qty_cap, "LL-11 leverage": qty_bp}
        if settled_cash is not None:                                                   # AC-1
            caps["AC-1 settled cash"] = max(0, math.floor(settled_cash / entry))
        if ticker is not None and positions is not None:
            caps.update(self.portfolio_caps(entry, stop, equity, ticker, style, positions))
        qty = min(caps.values())
        binding = [k for k, v in caps.items() if v == qty][-1]   # ties: the last cap, as before
        return max(qty, 0), per_share * max(qty, 0), pct, binding

    def portfolio_caps(self, entry: Decimal, stop: Decimal, equity: Decimal, ticker: str,
                       style: str | None, positions: list[OpenPositionView]) -> dict[str, int]:
        """Share caps from the whole book, not the single trade (PF-1, PF-3, PF-4)."""
        r, pf = self.cfg.risk, self.state.portfolio
        per_share = entry - stop
        out: dict[str, int] = {}
        # PF-1 open risk ("heat"): what every open position loses from its last price to its
        # stop, plus the new trade, stays under the cap. Locked-in stops add nothing.
        heat = sum((max(D0, p.last - p.stop) * p.qty for p in positions), D0)
        room = equity * Decimal(str(r.max_open_risk_pct)) / 100 - heat
        out["PF-1 open risk"] = int((room / per_share).to_integral_value(
            rounding=ROUND_FLOOR)) if room > 0 else 0
        # PF-3 beta-weighted exposure: a 1.8-beta stock counts as 1.8x its market value
        used = sum((Decimal(str(pf.beta_of(p.group))) * p.exposure for p in positions), D0)
        b = Decimal(str(max(pf.beta_of(ticker), 0.1)))
        room_b = equity * Decimal(str(r.max_beta_exposure_pct)) / 100 - used
        out["PF-3 beta exposure"] = int((room_b / (b * entry)).to_integral_value(
            rounding=ROUND_FLOOR)) if room_b > 0 else 0
        # PF-4 overnight gap: a swing held overnight can open through its stop. The stressed
        # loss per share is the stop distance plus the stock's 99th-percentile adverse gap.
        g = pf.gap.get(ticker)
        if style == "swing" and g is not None:
            stressed = per_share + entry * Decimal(str(g))
            out["PF-4 gap risk"] = int((equity * Decimal(str(r.max_gap_loss_pct)) / 100
                                        / stressed).to_integral_value(rounding=ROUND_FLOOR))
        return out

    # -- checklist (A7-1) --------------------------------------------------------------------
    def setup_status(self, setup: str) -> Literal["live", "paper"]:
        """RC-3: negative expectancy over the last 20 trades sends a setup back to paper."""
        n = self.cfg.risk.setup_disable_after_trades
        trades = [t for t in self.state.history if t.setup == setup][-n:]
        if len(trades) >= n and sum(t.r_multiple for t in trades) / n < 0:
            return "paper"
        return "live"

    def route_for(self, setup: str, style: str) -> Literal["broker", "paper"]:
        if self.cfg.mode.environment == "paper":
            return "broker"   # the broker *is* the paper account
        st = self.state
        if (self.cfg.style_mode(style) == "paper" or st.live_disabled
                or st.paper_only_until is not None or self.setup_status(setup) == "paper"
                or setup in st.slippage_paper):
            return "paper"
        return "broker"

    def _items(self, s: SetupTriggered, ctx: DecisionContext, review: Review | None,
               comp: ComplianceResult | None) -> list[ChecklistItem]:
        cfg = self.cfg
        items: list[ChecklistItem] = []
        st = self.state
        # CK-1 ticker on the list, not locked, kill switch off
        if s.ticker not in cfg.universe.tickers:
            items.append(ChecklistItem(id="CK-1", passed=False,
                                       detail=f"{Reason.NT_UNIVERSE}: {s.ticker} not on list"))
        elif st.kill_switch:
            items.append(ChecklistItem(id="CK-1", passed=False,
                                       detail=f"{Reason.NT_KILL_SWITCH}: kill switch on"))
        elif comp is not None and not comp.ok:
            items.append(ChecklistItem(id="CK-1", passed=False,
                                       detail=f"{comp.reason}: {', '.join(comp.locks_found)}"))
        else:
            items.append(ChecklistItem(id="CK-1", passed=True))
        # CK-2 inside an entry window (TW-1..TW-5)
        win, why = self.windows.entry_window(ctx.now, s.style)
        ok = win is not None and win == s.window
        code2 = Reason.NT_MACRO_RELEASE if why.startswith("TW-6") else Reason.NT_WINDOW
        items.append(ChecklistItem(id="CK-2", passed=ok,
                                   detail="" if ok else f"{code2}: {why or 'window'}"))
        # CK-3 filters MF-1..MF-5
        failed = [k for k in ("MF-1", "MF-2", "MF-3", "MF-4", "MF-5", "MF-6")
                  if not ctx.filters.get(k, k == "MF-6")]   # MF-6 absent = no quote rule
        if ctx.data_stale and "MF-5" not in failed:
            failed.append("MF-5")
        code3 = (Reason.NT_NO_RELIABLE_QUOTE if "MF-6" in failed   # DQ-8
                 else f"NT_FILTER_{failed[0].replace('-', '')}" if failed else "")
        items.append(ChecklistItem(
            id="CK-3", passed=not failed,
            detail="" if not failed else f"{code3}: failed {', '.join(failed)}"))
        # CK-4 named playbook setup, right window/style, planned or flagged unplanned
        ok4, d4 = True, ""
        if s.setup not in SETUP_NAMES:
            ok4, d4 = False, f"{Reason.NT_CK4}: {s.setup} is not a playbook setup"
        elif (s.window == "morning") != (s.setup in MORNING_SETUPS) or (
                s.window == "afternoon" and s.setup not in AFTERNOON_SETUPS):
            ok4, d4 = False, f"{Reason.NT_CK4}: {s.setup} not allowed in the {s.window} window"
        elif (s.style == "swing") != (s.setup in SWING_SETUPS):
            ok4, d4 = False, f"{Reason.NT_CK4}: {s.setup} has the wrong style {s.style}"
        elif s.setup in NEWS_SETUPS and not ctx.news_available:
            ok4, d4 = False, f"{Reason.NT_NEWS_UNAVAILABLE}: news-sentiment down"
        items.append(ChecklistItem(id="CK-4", passed=ok4,
                                   detail=d4 or ("planned" if ctx.planned else "unplanned")))
        # CK-5 trigger fired on a closed candle, no anticipation
        ok5 = s.bar_closed and s.ts <= ctx.now
        items.append(ChecklistItem(id="CK-5", passed=ok5,
                                   detail="" if ok5 else f"{Reason.NT_CK5}: bar not closed"))
        # CK-6 never chase
        atr5 = Decimal(str(s.atr5))
        chase = s.entry - s.trigger
        max_chase = Decimal(str(cfg.entries.max_chase_atr5)) * atr5
        runaway = s.trigger + Decimal(str(cfg.entries.cancel_if_runs_atr5)) * atr5
        ok6 = chase <= max_chase + TOL_ENTRY and ctx.last_price < runaway
        items.append(ChecklistItem(
            id="CK-6", passed=ok6,
            detail="" if ok6 else f"{Reason.NT_CK6}: entry {s.entry} vs trigger {s.trigger} "
            f"(max {max_chase:.2f}), last {ctx.last_price} (runaway {runaway:.2f})"))
        # CK-7 structural stop, no wider than 1 ATR5 (day) / 1.5 ATR14d (swing)
        r = s.entry - s.stop
        if s.style == "day":
            limit = Decimal(str(cfg.entries.max_day_stop_atr5)) * atr5
        else:
            atr14 = s.context.get("atr14d") or 0
            limit = Decimal("1.5") * Decimal(str(atr14))
        ok7 = r > 0 and r <= limit + TOL
        items.append(ChecklistItem(id="CK-7", passed=ok7,
                                   detail="" if ok7 else f"{Reason.NT_CK7}: R {r} > {limit:.2f}"))
        # CK-8 resistance >= 1.5R above entry, T2 >= 2R
        ok8, d8 = True, ""
        if r > 0:
            if s.resistance is not None and Decimal(str(s.resistance)) < s.entry + Decimal(
                    str(cfg.entries.min_t1_r)) * r - TOL:
                ok8, d8 = False, f"{Reason.NT_CK8}: resistance {s.resistance:.2f} < entry + " \
                                 f"{cfg.entries.min_t1_r}R"
            elif s.t2 < s.entry + Decimal(str(cfg.entries.min_reward_risk)) * r - TOL:
                ok8, d8 = False, f"{Reason.NT_CK8}: T2 {s.t2} < {cfg.entries.min_reward_risk}R"
        else:
            ok8, d8 = False, f"{Reason.NT_CK8}: no risk unit"
        items.append(ChecklistItem(id="CK-8", passed=ok8, detail=d8))
        # CK-9 review agrees
        if review is None:
            items.append(ChecklistItem(id="CK-9", passed=False,
                                       detail=f"{Reason.NT_LLM_INVALID}: no review (QW-3)"))
        else:
            ok9 = review.verdict == "agree"
            items.append(ChecklistItem(id="CK-9", passed=ok9,
                                       detail="" if ok9 else
                                       f"{Reason.NT_REVIEW_DISAGREE}: {review.main_risk}"))
        return items

    def _limits(self, s: SetupTriggered, ctx: DecisionContext) -> tuple[bool, str]:
        """CK-10 limits and behaviour rules (A7-3, A7-4)."""
        st, r, cfg = self.state, self.cfg.risk, self.cfg
        g = cfg.canonical(s.ticker)
        if st.kill_switch:
            return False, f"{Reason.NT_KILL_SWITCH}: kill switch on"
        if not ctx.health_ok:
            return False, f"{Reason.NT_HEALTH}: {ctx.health_detail}"
        if ctx.data_stale:
            return False, f"{Reason.NT_STALE_DATA}: data stale (A2-4)"
        if s.style in st.paused_styles:
            return False, f"{Reason.NT_STYLE_PAUSED}: {s.style} paused"
        if g in st.news_blocked:
            return False, f"{Reason.NT_NEWS_SHOCK}: negative news, 8-K or halt on {g} today"
        if st.halted is not None:
            return False, f"{st.halted}: trading halted for today"
        if st.trades_total_today >= r.max_trades_per_day:
            return False, f"{Reason.HALT_LL2}: {r.max_trades_per_day} trades today"
        per_window = r.max_trades_morning if s.window == "morning" else r.max_trades_afternoon
        if st.trades_today.get(s.window, 0) >= per_window:
            return False, f"{Reason.HALT_LL2}: {per_window} trades in the {s.window} window"
        open_pos = ctx.positions
        if len(open_pos) >= r.max_open_positions:
            return False, f"{Reason.HALT_LL8}: {len(open_pos)} positions open"
        if s.style == "day" and sum(1 for p in open_pos if p.style == "day"
                                    ) >= r.max_open_day_trades:
            return False, f"{Reason.HALT_LL8}: {r.max_open_day_trades} day trades open"
        for name, grp in cfg.universe.correlated_groups.items():
            if g in grp.tickers and sum(1 for p in open_pos if p.group in grp.tickers
                                        and p.group != g) >= grp.max_open:
                return False, f"{Reason.HALT_LL10}: correlated group {name} already open"
            if g in grp.tickers and any(p.group == g for p in open_pos) and grp.max_open <= 1:
                pass   # same ticker handled by DR-1 / DR-5 below
        pf = st.portfolio
        for p in open_pos:   # PF-2: measured correlation, on top of the configured groups
            c = pf.correlation(g, p.group) if p.group != g else None
            if c is not None and c >= r.max_pair_correlation:
                return False, (f"{Reason.NT_PF_CORRELATION}: {g} moves with open {p.group} "
                               f"(60-day correlation {c:.2f} >= {r.max_pair_correlation})")
        same = [p for p in open_pos if p.group == g]
        for p in same:
            if p.unrealized < 0 or ctx.last_price < p.entry:
                return False, f"{Reason.NT_DR1}: never average down ({p.ticker} losing)"
            if not (p.t1_hit and p.stop >= p.entry):
                return False, (f"{Reason.NT_DR5}: add only after T1 with the stop at "
                               "break-even")
        if (s.window, g) in st.window_entries:
            return False, f"{Reason.NT_DR4}: {g} already traded in the {s.window} window"
        if (s.window, g) in st.window_losses:
            return False, f"{Reason.NT_DR3}: {g} lost earlier in this window"
        if st.last_stopout_at is not None and ctx.now - st.last_stopout_at < timedelta(
                minutes=r.revenge_cooldown_min):
            return False, f"{Reason.NT_DR3}: {r.revenge_cooldown_min}-minute cooldown after a stop"
        return True, ""

    def evaluate(self, s: SetupTriggered, ctx: DecisionContext, review: Review | None,
                 comp: ComplianceResult | None, proposal_id: str) -> Decision:
        """Full checklist CK-1..CK-10 and size (A7-1, A7-2)."""
        if s.context.get("direction") == "bear" or s.context.get("route") == "options_only":
            return Decision(proposal_id=proposal_id, result="REJECT",   # BR-4, ADR-008
                            reason_code=Reason.NT_OPTIONS_ONLY, checklist=(),
                            detail="bearish signal: options only, never a stock order",
                            entry=s.entry, stop=s.stop, t1=s.t1, t2=s.t2)
        items = self._items(s, ctx, review, comp)
        mode = self.cfg.style_mode(s.style)
        route = self.route_for(s.setup, s.style)
        needs_owner_for_disagree = False
        for it in items:
            if it.passed:
                continue
            if it.id == "CK-9" and review is not None and mode == "approve" and route == "broker":
                needs_owner_for_disagree = True   # A6-3: disagreement goes to the owner
                continue
            return self._reject(proposal_id, it, items, s)
        ok, why = self._limits(s, ctx)
        exposure = sum((p.exposure for p in ctx.positions if p.route == route), D0)
        book = [p for p in ctx.positions if p.route == route]
        qty, risk_usd, pct, binding = self.size(s.entry, s.stop, ctx.equity, ctx.buying_power,
                                                exposure, ctx.planned,
                                                ticker=self.cfg.canonical(s.ticker),
                                                style=s.style, positions=book,
                                                settled_cash=ctx.settled_cash)
        if ok and qty < 1:
            code = Reason.NT_UNSETTLED_FUNDS if binding == "AC-1 settled cash" \
                else Reason.SIZE_TOO_SMALL
            ok, why = False, f"{code}: qty {qty} ({binding})"
        if ok and mode in ("approve", "copilot") and route == "broker" \
                and not ctx.approvals_available:   # NF-1: no silent auto-reject
            ok, why = False, f"{Reason.NT_NOTIFIER_DOWN}: notifier down, {mode} mode blocked"
        items.append(ChecklistItem(id="CK-10", passed=ok,
                                   detail=why or f"qty {qty}, risk ${risk_usd:.2f} ({pct}%), "
                                   f"binding {binding}"))
        if not ok:
            return self._reject(proposal_id, items[-1], items, s)
        needs = (mode == "approve" and route == "broker") or needs_owner_for_disagree
        reason = Reason.NEEDS_APPROVAL if needs else Reason.APPROVED
        if self.state.reduced_since is not None:
            reason = Reason.NEEDS_APPROVAL if needs else Reason.SIZE_REDUCED
        if mode == "copilot" and route == "broker":   # CP-1: plan only, the owner executes
            needs, reason = False, Reason.COPILOT_PLAN
        return Decision(proposal_id=proposal_id,
                        result="NEEDS_APPROVAL" if needs else "APPROVE",
                        reason_code=reason, qty=qty, risk_usd=risk_usd, risk_pct=pct,
                        entry=s.entry, stop=s.stop, t1=s.t1, t2=s.t2, checklist=tuple(items),
                        detail=f"binding {binding}", route=route,
                        override=needs_owner_for_disagree)

    def _reject(self, proposal_id: str, item: ChecklistItem, items: list[ChecklistItem],
                s: SetupTriggered) -> Decision:
        code = item.detail.split(":")[0] if item.detail and item.detail.split(":")[0].isupper(
        ) and "_" in item.detail.split(":")[0] else f"NT_{item.id.replace('-', '')}"
        return Decision(proposal_id=proposal_id, result="REJECT", reason_code=code,
                        checklist=tuple(items), detail=f"{item.id} {item.detail}",
                        entry=s.entry, stop=s.stop, t1=s.t1, t2=s.t2)

    # -- bookkeeping when an entry is submitted ------------------------------------------------
    def on_entry_submitted(self, s: SetupTriggered) -> None:
        st = self.state
        st.trades_today[s.window] = st.trades_today.get(s.window, 0) + 1
        st.window_entries.add((s.window, self.cfg.canonical(s.ticker)))
