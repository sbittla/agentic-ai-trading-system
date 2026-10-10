"""Typed messages for the event bus and the external interfaces (DATA_SPEC.md §4, INTERFACES.md).

Money that reaches orders, decisions or P&L is ``Decimal`` (IF-1). Bar prices are floats:
they feed indicators only and are converted to cents before any decision uses them.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Timeframe = Literal["1Min", "5Min", "1Day"]
Style = Literal["day", "swing"]
Window = Literal["morning", "afternoon"]
SetupName = Literal["ORB", "GAP_AND_GO", "GAP_AND_GO_NO_PM", "VWAP_RECLAIM", "FIRST_PULLBACK",
                    "POWER_HOUR_BREAKOUT", "VWAP_TREND", "SWING_CLOSE_NEAR_HIGH",
                    "SWING_BASE_BREAKOUT"]
SETUP_NAMES: tuple[str, ...] = ("ORB", "GAP_AND_GO", "GAP_AND_GO_NO_PM", "VWAP_RECLAIM",
                                "FIRST_PULLBACK", "POWER_HOUR_BREAKOUT", "VWAP_TREND",
                                "SWING_CLOSE_NEAR_HIGH", "SWING_BASE_BREAKOUT")
MORNING_SETUPS = ("ORB", "GAP_AND_GO", "GAP_AND_GO_NO_PM", "VWAP_RECLAIM", "FIRST_PULLBACK")
AFTERNOON_SETUPS = ("POWER_HOUR_BREAKOUT", "VWAP_TREND", "SWING_CLOSE_NEAR_HIGH",
                    "SWING_BASE_BREAKOUT")
SWING_SETUPS = ("SWING_CLOSE_NEAR_HIGH", "SWING_BASE_BREAKOUT")
NEWS_SETUPS = ("GAP_AND_GO", "GAP_AND_GO_NO_PM")   # need A3 (ARCHITECTURE §6)


def _aware(v: datetime) -> datetime:
    if v.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware (IF-2)")
    return v


class Msg(BaseModel):
    model_config = ConfigDict(frozen=True)


# --- market data ---------------------------------------------------------------------------

class Bar(Msg):
    symbol: str
    ts: datetime                  # bar start
    timeframe: Timeframe = "1Min"
    o: float
    h: float
    l: float   # noqa: E741
    c: float
    v: int
    vwap: float | None = None
    source: str = "iex"

    _check_ts = field_validator("ts")(_aware)

    @property
    def end(self) -> datetime:
        if self.timeframe == "1Min":
            return self.ts + timedelta(minutes=1)
        if self.timeframe == "5Min":
            return self.ts + timedelta(minutes=5)
        return self.ts + timedelta(days=1)


class Quote(Msg):
    symbol: str
    ts: datetime
    bid: float
    ask: float
    bid_size: int = 0
    ask_size: int = 0

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread_pct(self) -> float:
        mid = self.mid
        return (self.ask - self.bid) / mid * 100 if mid > 0 else 100.0


class TradingStatus(Msg):
    """Halt / resume / LULD band events (MF-5)."""

    symbol: str
    ts: datetime
    kind: Literal["halt", "resume", "luld"]
    detail: str = ""


class NewsItem(Msg):
    id: str
    ticker: str
    source: Literal["finnhub", "edgar", "reddit", "recorded"]
    published_at: datetime
    headline: str
    text: str = ""
    url: str = ""
    kind: Literal["news", "8-K", "post", "earnings", "rating"] = "news"
    meta: dict[str, Any] = Field(default_factory=dict)


# --- broker --------------------------------------------------------------------------------

class Account(Msg):
    equity: Decimal
    cash: Decimal
    buying_power: Decimal
    status: str = "ACTIVE"
    currency: str = "USD"


class BrokerPosition(Msg):
    symbol: str
    qty: int
    avg_entry_price: Decimal
    market_value: Decimal = Decimal(0)
    unrealized_pl: Decimal = Decimal(0)


OrderStatus = Literal["new", "accepted", "partially_filled", "filled", "canceled", "expired",
                      "rejected", "replaced", "held", "pending_new"]


class Order(Msg):
    id: str
    client_order_id: str
    symbol: str
    side: Literal["buy", "sell"]
    qty: int
    filled_qty: int = 0
    type: Literal["limit", "market", "stop", "stop_limit"] = "limit"
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    status: OrderStatus = "new"
    leg: Literal["entry", "take_profit", "stop_loss", "exit"] = "entry"
    parent_id: str | None = None
    filled_avg_price: Decimal | None = None
    time_in_force: Literal["day", "gtc"] = "day"
    submitted_at: datetime | None = None


class OrderUpdate(Msg):
    event: Literal["new", "fill", "partial_fill", "canceled", "expired", "rejected", "replaced"]
    order: Order
    ts: datetime
    fill_qty: int = 0
    fill_price: Decimal | None = None


class BracketRequest(Msg):
    client_order_id: str = Field(max_length=48)
    symbol: str
    qty: int = Field(ge=1)          # whole shares (Alpaca brackets)
    limit_price: Decimal
    take_profit: Decimal
    stop_price: Decimal
    time_in_force: Literal["day", "gtc"]


# --- pipeline ------------------------------------------------------------------------------

class Levels(Msg):
    """A4-1 key levels (8:00 AM)."""

    day: date
    ticker: str
    prior_high: float
    prior_low: float
    prior_close: float
    pm_high: float | None = None
    pm_low: float | None = None
    ema20d: float | None = None
    ema50d: float | None = None
    ema10d: float | None = None
    atr14d: float | None = None
    high20d: float | None = None          # highest high, prior 20 sessions (resistance)
    close_high20d: float | None = None    # highest close, prior 20 sessions (base breakout)
    avg_vol50d: float | None = None
    low5d: float | None = None
    pullback_to_ema: bool = False         # a low within 0.5% of EMA20d/EMA50d, last 5 sessions
    history_days: int = 0


class EventFlags(Msg):
    """A3 output per ticker (news_flags outbox channel)."""

    ticker: str
    updated_at: datetime
    flags: tuple[str, ...] = ()
    earnings_date: date | None = None
    summary: str = ""
    catalyst: str = "none"
    tone: Literal["positive", "negative", "mixed", "neutral"] = "neutral"
    sentiment: float = 0.0
    risk_events: tuple[str, ...] = ()
    suspicious_items: int = 0


class SetupTriggered(Msg):
    setup_id: str
    ts: datetime                    # decision time = end of the closed bar that fired (CK-5)
    bar_closed: bool = True
    ticker: str
    style: Style
    window: Window
    setup: str
    trigger: Decimal
    entry: Decimal                  # limit price (trigger + 0.10 ATR5 + offset, capped)
    stop: Decimal
    t1: Decimal
    t2: Decimal
    score: int
    atr5: float
    vwap: float
    rvol: float
    last_price: float
    resistance: float | None
    filters: dict[str, bool]
    context: dict[str, Any] = Field(default_factory=dict)

    @property
    def r(self) -> Decimal:
        return self.entry - self.stop


class GamePlanItem(Msg):
    ticker: str
    setup: str
    trigger: float
    stop: float
    target_1: float
    why: str = ""


class GamePlan(Msg):
    day: date
    window: Window
    market_bias: Literal["with_us", "against_us", "mixed"] = "mixed"
    watch: tuple[GamePlanItem, ...] = ()
    off_limits: tuple[tuple[str, str], ...] = ()
    source: Literal["llm", "preview", "fallback"] = "llm"   # preview: built after the close


class TradeProposal(Msg):
    """REQUIREMENTS §10.1 / AGENT_PROMPTS §5. Code sets the ids, times and model fields."""

    proposal_id: str
    setup_id: str
    action: Literal["take", "skip"]
    ticker: str
    style: Style
    window: Window
    side: Literal["buy"] = "buy"
    setup: str
    planned: bool
    trigger: float
    entry: float
    stop: float
    target_1: float
    target_2: float
    confidence: float = Field(ge=0, le=1)
    thesis: str = Field(max_length=300)
    weakest_point: str = Field(default="", max_length=160)
    expires_at: datetime
    created_at: datetime
    model: str
    prompt_version: str


class ReviewChecks(Msg):
    chasing: bool = False
    resistance_before_t1: bool = False
    weak_market: bool = False
    late_in_window: bool = False
    event_risk: bool = False
    bad_stop: bool = False


class Review(Msg):
    proposal_id: str
    verdict: Literal["agree", "disagree"]
    main_risk: str = Field(default="", max_length=160)
    rules_broken: tuple[str, ...] = ()
    checks: ReviewChecks = ReviewChecks()


class ComplianceResult(Msg):
    proposal_id: str
    ok: bool
    locks_found: tuple[str, ...] = ()
    reason: str | None = None


class ChecklistItem(Msg):
    id: str
    passed: bool
    detail: str = ""


class Decision(Msg):
    proposal_id: str
    result: Literal["APPROVE", "REJECT", "NEEDS_APPROVAL"]
    reason_code: str
    qty: int = 0
    risk_usd: Decimal = Decimal(0)
    risk_pct: Decimal = Decimal(0)
    entry: Decimal = Decimal(0)
    stop: Decimal = Decimal(0)
    t1: Decimal = Decimal(0)
    t2: Decimal = Decimal(0)
    checklist: tuple[ChecklistItem, ...] = ()
    detail: str = ""
    route: Literal["broker", "paper"] = "broker"
    override: bool = False


class ApprovalRequest(Msg):
    proposal_id: str
    code: str
    text: str
    expires_at: datetime


class ApprovalResponse(Msg):
    proposal_id: str
    answer: Literal["yes", "no", "timeout"]
    answered_at: datetime | None = None


class Command(Msg):
    """An inbound owner command after sender checks (SEC-1..3)."""

    name: str                       # STATUS KILL RESUME FLATTEN YES NO ACK PAUSE CONFIRM UNPAUSE
    arg: str = ""
    sender: str = "local"
    received_at: datetime
    source: Literal["whatsapp", "telegram", "local"] = "local"


class HealthStatus(Msg):
    component: str
    level: Literal["green", "amber", "red"]
    detail: str = ""
    ts: datetime


class LimitHit(Msg):
    rule: str
    reason_code: str
    detail: str
    ts: datetime


class KillSwitch(Msg):
    on: bool
    by: str
    ts: datetime
    reason: str = ""


class PositionOpened(Msg):
    position_id: str
    ticker: str
    style: Style
    qty: int
    avg_entry: Decimal
    stop: Decimal
    ts: datetime


class StopMoved(Msg):
    position_id: str
    ticker: str
    old_stop: Decimal
    new_stop: Decimal
    reason: str
    ts: datetime


class PositionClosed(Msg):
    position_id: str
    ticker: str
    style: Style
    qty: int
    avg_entry: Decimal
    avg_exit: Decimal
    pnl: Decimal
    r_multiple: float
    exit_reason: str
    ts: datetime


class LockCreated(Msg):
    ticker: str
    lock_type: Literal["WASH", "EARNINGS", "LOSS_STREAK", "REVENGE"]
    start: datetime
    until: datetime
    reason: str


class ReportPublished(Msg):
    kind: str
    path: str
    ts: datetime


class NoTrade(Msg):
    """NT-1: a window or a setup ended without an order; why, and the closest candidate."""

    ts: datetime
    ticker: str | None
    setup: str | None
    reason_code: str
    detail: str = ""
    stage: str = ""
