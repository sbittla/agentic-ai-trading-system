"""Configuration loader and validator (CONFIG_REFERENCE.md, CF-4, SR-5, DV-3).

The config is loaded once at start, validated, hashed and frozen. No agent can change it.
Every failed check names the rule it broke, for example ``risk.risk_per_trade_pct <= 1.0``.
"""

from __future__ import annotations

import hashlib
import os
import re
from datetime import time
from importlib import resources
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from trader.timeutil import parse_hhmm

StyleMode = Literal["paper", "approve", "auto", "copilot"]   # copilot: CP-1


class ConfigError(Exception):
    """Raised when the config breaks one or more validation rules (CF-4)."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("invalid config:\n  - " + "\n  - ".join(problems))


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModeCfg(_Frozen):
    environment: Literal["paper", "live"] = "paper"
    day_trades: StyleMode = "approve"
    swing_trades: StyleMode = "approve"
    account_type: Literal["auto", "cash", "margin"] = "auto"   # AC-1/AC-2; auto = from Alpaca


class CorrelatedGroup(_Frozen):
    tickers: tuple[str, ...]
    max_open: int = 1


class UniverseCfg(_Frozen):
    tickers: tuple[str, ...]
    context_symbols: tuple[str, ...] = ("SPY", "QQQ", "SMH")
    substantially_identical: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    correlated_groups: dict[str, CorrelatedGroup] = Field(default_factory=dict)
    min_history_days: int = 60
    effective_date: str | None = None   # UR-5; defaults to the config snapshot date

    @field_validator("tickers", "context_symbols", mode="before")
    @classmethod
    def _upper(cls, v: Any) -> Any:
        return tuple(str(t).upper().strip() for t in v)


class MorningWindow(_Frozen):
    observe_from: str = "09:30"
    entries_from: str = "09:35"
    entries_until: str = "10:15"
    flatten_at: str = "10:30"


class AfternoonWindow(_Frozen):
    entries_from: str = "15:00"
    day_entries_until: str = "15:40"
    swing_entries_from: str = "15:30"
    swing_entries_until: str = "15:50"
    flatten_at: str = "15:55"


class MacroDays(_Frozen):
    cpi_jobs_morning_entries_from: str = "09:45"
    skip_afternoon_on_fomc: bool = True
    release_block_from: str = "09:57"      # TW-6: 10:00 AM releases
    release_block_until: str = "10:05"
    speech_block_before_min: int = 3       # TW-6: scheduled Fed speeches inside a window
    speech_block_after_min: int = 5
    breakeven_at_r: float = 0.5            # TW-6: open day trades at +0.5R move to break-even


class WindowsCfg(_Frozen):
    morning: MorningWindow = MorningWindow()
    afternoon: AfternoonWindow = AfternoonWindow()
    macro_days: MacroDays = MacroDays()
    macro_calendar_file: str = "config/macro_calendar_2026_2027.yaml"


class FiltersCfg(_Frozen):
    qqq_above_vwap_required: bool = True
    qqq_max_day_drop_pct: float = 1.0
    smh_check_for: tuple[str, ...] = ("NVDA", "AVGO", "TSM")
    min_rvol: float = 1.5
    max_spread_pct: float = 0.05
    halt_lookback_min: int = 10


class EntriesCfg(_Frozen):
    max_chase_atr5: float = 0.25
    max_day_stop_atr5: float = 1.0
    min_t1_r: float = 1.5
    min_reward_risk: float = 2.0
    min_confidence: float = 0.6
    unplanned_size_factor: float = 0.5
    cancel_unfilled_after_min: int = 3
    cancel_if_runs_atr5: float = 0.5
    limit_offset_pct: float = 0.03


class DayExits(_Frozen):
    t1_r: float = 1.0
    t1_fraction: float = 0.5
    trail: Literal["ema9_5m", "last_higher_low"] = "ema9_5m"
    time_stop_min: int = 15
    time_stop_min_progress_r: float = 0.5


class SwingExits(_Frozen):
    t1_r: float = 1.5
    t1_fraction: float = 0.5
    trail: Literal["ema10_1d"] = "ema10_1d"
    max_hold_days: int = 10
    exit_before_earnings: bool = True


class ExitsCfg(_Frozen):
    day: DayExits = DayExits()
    swing: SwingExits = SwingExits()


class LossStreakLock(_Frozen):
    losses: int = 3
    days: int = 5


class RiskCfg(_Frozen):
    risk_per_trade_pct: float = 0.5
    drawdown_risk_per_trade_pct: float = 0.25
    drawdown_trigger_pct: float = 3.0
    max_trades_per_day: int = 3
    max_trades_morning: int = 2
    max_trades_afternoon: int = 1
    daily_loss_limit_pct: float = 1.5
    stop_after_consecutive_losses: int = 2
    weekly_loss_limit_pct: float = 3.0
    monthly_drawdown_limit_pct: float = 6.0
    profit_giveback_pct: float = 50
    max_open_positions: int = 3
    max_open_day_trades: int = 2
    max_position_pct: float = 25
    max_leverage: float = 1.0
    revenge_cooldown_min: int = 10
    ticker_loss_streak_lock: LossStreakLock = LossStreakLock()
    setup_disable_after_trades: int = 20
    recovery_trades: int = 10          # RC-2
    # portfolio risk (PF-1..PF-4): limits across positions, not just per trade
    max_open_risk_pct: float = 1.5     # PF-1: open risk to stops + the new trade, % of equity
    max_pair_correlation: float = 0.8  # PF-2: 60-day return correlation counted as one bet
    correlation_lookback: int = 60
    max_beta_exposure_pct: float = 150  # PF-3: sum(beta x market value), % of equity
    max_gap_loss_pct: float = 1.5      # PF-4: swing loss if the stop gaps by the 99th pct gap
    gap_percentile: float = 99.0


class WashSaleCfg(_Frozen):
    policy: Literal["strict", "balanced", "year_end_only"] = "strict"
    lock_days: int = 31
    balanced_min_loss_r: float = 0.5
    year_end_strict_from: str = "12-01"


class DataCfg(_Frozen):
    alpaca_feed: Literal["iex", "sip"] = "iex"
    stale_after_sec: int = 60
    news_poll_min: int = 15
    news_poll_open_s: int = 60             # NW-1: tickers with a position or a plan setup
    quote_max_age_s: float = 2.0           # DQ-8 / MF-6
    sip_divergence_pct: float = 0.05       # DQ-6 / DQ-7
    sip_divergence_share_pct: float = 95.0  # DQ-7: share of setups within the tolerance
    sip_gate_days: int = 20                # DQ-7
    reddit_subreddits: tuple[str, ...] = ("stocks", "investing", "wallstreetbets", "StockMarket")
    sec_user_agent: str = "agentic-ai-trading-system owner owner@example.com"


class LlmCfg(_Frozen):
    provider: Literal["ollama"] = "ollama"
    base_url: str = "http://ollama:11434"
    model: str = "qwen3:8b"
    temperature: float = 0
    seed: int = 42
    timeout_sec: int = 60
    retries: int = 1
    max_context_tokens: int = 8192
    keep_alive: str = "8h"
    prompt_versions: dict[str, str] = Field(
        default_factory=lambda: {"a03": "v1", "a05_plan": "v1", "a05_proposal": "v1",
                                 "a06": "v1", "a11": "v1"})


class NotificationsCfg(_Frozen):
    whatsapp_enabled: bool = True
    approval_timeout_sec: int = 90
    approval_chart: bool = True            # AP-1
    approval_live_update_s: int = 5        # AP-3
    notifier_down_after_s: int = 120       # NF-1


class LatencyCfg(_Frozen):
    """LAT-1..LAT-4."""

    budget_s: float = 10.0
    prearm: bool = True
    prearm_atr: float = 0.3
    prearm_tolerance_atr: float = 0.1
    approval_grace_s: int = 30


class RegimeCfg(_Frozen):
    """RG-1..RG-3."""

    high_vol_atr_ratio: float = 1.5
    high_vol_gap_pct: float = 1.5          # SPY opening gap that alone marks a high-vol day
    risk_factor_high_vol: float = 0.5
    gap_and_go_max_normal_x: float = 2.0
    trend_day_vwap_pct: float = 80.0
    trend_day_high_after: str = "13:00"


class AlertsCfg(_Frozen):
    """Owner alerts produced by trader-core (AL-1..AL-7)."""

    enabled: bool = True
    schedule: Literal["windows", "all_day"] = "windows"
    per_symbol_cap: int = 3
    per_window_cap: int = 12
    top_n: int = 5
    digest_min: int = 10
    grade_window_min: int = 30
    min_hit_rate: float = 0.40
    min_graded: int = 50
    disabled_kinds: tuple[str, ...] = ()


class ExecutionCfg(_Frozen):
    """EX-5 / EXQ-1 (stops are always stop-market; not configurable)."""

    slippage_switch_r: float = 0.1
    slippage_window: int = 10
    # pre-trade order sanity, the last check before an order leaves (OC-1..OC-4)
    max_price_deviation_pct: float = 1.0   # OC-1: limit price vs last trade
    max_adv_pct: float = 1.0               # OC-2: order shares vs 20-day average volume
    max_order_notional: float = 0.0        # OC-4: absolute $ cap per order; 0 = off


class ShadowCfg(_Frozen):
    """SH-1 / SH-2."""

    enabled: bool = True
    flag_after: int = 40


class InPlayCfg(_Frozen):
    """UN-1 / UN-2: the 50 largest S&P 500 names (watch only; edit as rankings change)."""

    enabled: bool = True
    top_n: int = 5
    rotation_days: int = 3
    rotation_lookback: int = 5
    symbols: tuple[str, ...] = (
        "NVDA", "AAPL", "GOOGL", "MSFT", "AMZN", "META", "AVGO", "TSLA", "MU", "LLY",
        "BRK.B", "AMD", "JPM", "WMT", "V", "XOM", "JNJ", "INTC", "MA", "ABBV", "ORCL", "COST",
        "NFLX", "PLTR", "HD", "PG", "BAC", "UNH", "KO", "CVX", "GE", "CSCO", "PM", "WFC",
        "CRM", "IBM", "MS", "ABT", "AXP", "GS", "MCD", "LIN", "CAT", "MRK", "TMO", "PEP",
        "DIS", "ISRG", "UBER", "QCOM")


class TaxCfg(_Frozen):
    set_aside_rate_pct: float = 30.0       # TX-1 (an estimate, not tax advice)


class OptionsCfg(_Frozen):
    """Options suggestions (OPTIONS_REQUIREMENTS.md §10). Advisory first."""

    mode: Literal["off", "advisory", "paper", "live"] = "advisory"
    account_level: int = 3
    from_alerts: bool = False
    structures: tuple[str, ...] = ("long_call", "long_put", "call_debit", "put_debit",
                                   "put_credit", "call_credit")
    allow_0dte: bool = False
    day_dte: tuple[int, int] = (5, 14)
    swing_dte: tuple[int, int] = (21, 45)
    min_open_interest: int = 500
    min_volume: int = 100
    min_volume_early: int = 20
    max_spread_pct: float = 5.0
    iv_rank_cheap: float = 30
    iv_rank_rich: float = 60
    max_premium_pct: float = 1.0
    max_contracts: int = 10
    avoid_earnings: bool = True
    max_alerts_per_window: int = 6
    min_rr_at_t1: float = 1.5
    max_target_expected_move_x: float = 1.5


class StorageCfg(_Frozen):
    journal_url_env: str = "DATABASE_URL"
    bars_path: str = "data/bars"
    backup_daily_at: str = "18:00"


class PollCfg(_Frozen):
    approval: int = 3
    market_hours: int = 10
    off_hours: int = 60


class ServicesCfg(_Frozen):
    heartbeat_sec: int = 30
    news_stale_after_min: int = 5
    notifier_stale_after_min: int = 2
    notifier_poll_sec: PollCfg = PollCfg()


class ScannerCfg(_Frozen):
    """Technical scanner (SCANNER_REQUIREMENTS.md TS-15). ``disabled`` holds technique names,
    optionally with ":long" or ":short"; the defaults lost money in both halves of the
    2025-2026 history (SCANNER_REQUIREMENTS.md §6). Re-check with ``trader scan-eval``."""

    symbols: tuple[str, ...] = ()        # scanned on top of universe.tickers (fetch-history too)
    min_score: int = 50
    top_n: int = 5
    in_play_top_n: int = 5
    min_samples: int = 30
    max_hold_days: int = 10
    disabled: tuple[str, ...] = ("REVERSAL_CANDLE:long", "RSI2_REVERSION:short",
                                 "TREND_PULLBACK:short", "BEAR_FLAG")
    eval_file: str = "reports/scanner_eval.json"
    alert_after_close: bool = True       # trader-core sends the scan at 16:20 ET
    latest_file: str = "reports/technical_scan.json"   # newest scan, read by the console

    @field_validator("symbols", mode="before")
    @classmethod
    def _upper(cls, v: Any) -> Any:
        return tuple(str(t).upper().strip() for t in v)


class CatalystCfg(_Frozen):
    """News catalysts (NEWS_CATALYST_REQUIREMENTS.md §10). Advisory only: nothing here can
    place an order (ADR-006)."""

    enabled: bool = True
    horizons: tuple[int, ...] = (1, 5, 10, 21)          # sessions
    story_join: float = 0.75            # NC-1: cosine similarity, sentence embeddings
    story_join_hash: float = 0.45       # NC-1: same, word-hash vectors (no model loaded)
    story_window_h: int = 72
    novelty_days: int = 30              # NC-2
    min_strength: float = 40            # NC-4
    alert_strength: float = 60          # NC-28
    half_life_h: float = 24             # NC-7
    iv_weight: float = 0.6              # NC-18
    shrink_k: float = 30                # NC-19
    stance_p: float = 0.56              # NC-24
    stance_min_edge: float = 0.25       # NC-24: median move >= this x the horizon's sigma
    max_posts_per_author: int = 3       # NC-12
    social_sources: tuple[str, ...] = ("reddit",)   # + stocktwits, apewisdom once verified
    telegram: bool = False              # NC-28; the gate (NC-34) must pass as well
    history_universe: Literal["universe", "extended"] = "extended"
    adjustments: tuple[str, ...] = ()   # NC-20/NC-33: set by catalyst-eval, not by hand
    gate_min_graded: int = 100          # NC-34
    gate_coverage: tuple[float, float] = (0.75, 0.85)
    embed_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    social_model: str = "StephanAkkerman/FinTwitBERT-sentiment"
    source_weights: dict[str, float] = Field(default_factory=lambda: {
        "sec": 1.0, "business wire": 1.0, "pr newswire": 1.0, "globenewswire": 1.0,
        "reuters": 0.8, "bloomberg": 0.8, "dow jones": 0.8, "wsj": 0.8, "cnbc": 0.8,
        "marketwatch": 0.8, "financial times": 0.8, "barron": 0.8, "associated press": 0.8,
        "benzinga": 0.6, "rss": 0.6, "yahoo": 0.4, "seekingalpha": 0.4, "motley fool": 0.4,
        "zacks": 0.4, "tipranks": 0.4, "investorplace": 0.4, "insidermonkey": 0.4,
        "reddit": 0.0, "stocktwits": 0.0})
    default_source_weight: float = 0.6
    eval_file: str = "reports/catalyst_eval.json"
    latest_file: str = "reports/catalysts.json"

    @field_validator("horizons")
    @classmethod
    def _horizons(cls, v: tuple[int, ...]) -> tuple[int, ...]:
        if not v or any(h < 1 or h > 63 for h in v):
            raise ValueError("catalyst.horizons: 1-63 sessions each")
        return tuple(sorted(set(v)))


class Config(_Frozen):
    mode: ModeCfg = ModeCfg()
    universe: UniverseCfg
    windows: WindowsCfg = WindowsCfg()
    filters: FiltersCfg = FiltersCfg()
    entries: EntriesCfg = EntriesCfg()
    exits: ExitsCfg = ExitsCfg()
    risk: RiskCfg = RiskCfg()
    wash_sale: WashSaleCfg = WashSaleCfg()
    data: DataCfg = DataCfg()
    llm: LlmCfg = LlmCfg()
    notifications: NotificationsCfg = NotificationsCfg()
    storage: StorageCfg = StorageCfg()
    services: ServicesCfg = ServicesCfg()
    latency: LatencyCfg = LatencyCfg()
    regime: RegimeCfg = RegimeCfg()
    alerts: AlertsCfg = AlertsCfg()
    execution: ExecutionCfg = ExecutionCfg()
    shadow: ShadowCfg = ShadowCfg()
    in_play: InPlayCfg = InPlayCfg()
    tax: TaxCfg = TaxCfg()
    options: OptionsCfg = OptionsCfg()
    scanner: ScannerCfg = ScannerCfg()
    catalyst: CatalystCfg = CatalystCfg()

    def style_mode(self, style: str) -> StyleMode:
        return self.mode.day_trades if style == "day" else self.mode.swing_trades

    def identical_group(self, ticker: str) -> frozenset[str]:
        """All symbols treated as the same stock as ``ticker`` (UR-2, WS-4)."""
        t = ticker.upper()
        for primary, others in self.universe.substantially_identical.items():
            group = {primary.upper(), *(o.upper() for o in others)}
            if t in group:
                return frozenset(group)
        return frozenset({t})

    def canonical(self, ticker: str) -> str:
        """The configured share class for a symbol (GOOG -> GOOGL)."""
        t = ticker.upper()
        for primary, others in self.universe.substantially_identical.items():
            if t == primary.upper() or t in {o.upper() for o in others}:
                return primary.upper()
        return t


class LoadedConfig(BaseModel):
    """A validated config plus the text it came from (hash logged daily, SEC-8)."""

    model_config = ConfigDict(frozen=True)

    config: Config
    yaml_text: str
    sha256: str
    path: str


EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def _t(s: str) -> time:
    return parse_hhmm(s)


def validate(cfg: Config, *, base_dir: Path | None = None, check_env: bool = False,
             gates_passed: set[str] | None = None) -> list[str]:
    """Return every broken rule (empty list = valid). CF-4."""
    p: list[str] = []
    u = cfg.universe
    # universe
    if not 1 <= len(u.tickers) <= 10:
        p.append("universe.tickers: 1-10 tickers (UR-1)")
    if len(set(u.tickers)) != len(u.tickers):
        p.append("universe.tickers: duplicate ticker")
    if len(u.tickers) + len(u.context_symbols) > 30:
        p.append("universe: tickers + context_symbols <= 30 (free stream limit)")
    if set(u.tickers) & set(u.context_symbols):
        p.append("universe: context symbols are never traded and cannot be on the ticker list")
    for primary, others in u.substantially_identical.items():
        group = {primary.upper(), *(o.upper() for o in others)}
        if len(group & set(u.tickers)) > 1:
            p.append(f"universe.tickers: one share class per company (UR-2): {sorted(group)}")
    if u.min_history_days < 60:
        p.append("universe.min_history_days >= 60 (UR-3)")
    for name, g in u.correlated_groups.items():
        if g.max_open < 1:
            p.append(f"universe.correlated_groups.{name}.max_open >= 1")
    # technical scanner (TS-15)
    sc = cfg.scanner
    if not 0 <= sc.min_score <= 100:
        p.append("scanner.min_score: 0-100")
    if sc.top_n < 1 or sc.in_play_top_n < 1 or sc.max_hold_days < 1 or sc.min_samples < 1:
        p.append("scanner: top_n, in_play_top_n, max_hold_days and min_samples >= 1")
    ca = cfg.catalyst
    if not (0 < ca.story_join <= 1 and 0 < ca.story_join_hash <= 1):
        p.append("catalyst.story_join and story_join_hash: 0-1 (NC-1)")
    if not 0 <= ca.min_strength <= ca.alert_strength <= 100:
        p.append("catalyst: 0 <= min_strength <= alert_strength <= 100 (NC-4, NC-28)")
    if not (0 <= ca.iv_weight <= 1 and ca.shrink_k >= 0 and 0.5 < ca.stance_p < 1):
        p.append("catalyst: iv_weight 0-1, shrink_k >= 0, stance_p 0.5-1 (NC-18..NC-24)")
    lo_cov, hi_cov = ca.gate_coverage
    if not 0 < lo_cov < hi_cov <= 1 or ca.gate_min_graded < 30:
        p.append("catalyst: gate_coverage low < high within 0-1, gate_min_graded >= 30 (NC-34)")
    bad_src = set(ca.social_sources) - {"reddit", "stocktwits", "apewisdom"}
    if bad_src:
        p.append(f"catalyst.social_sources: unknown {sorted(bad_src)}")
    bad_adj = set(ca.adjustments) - {"pead", "priced_in", "crowding", "trend"}
    if bad_adj:
        p.append(f"catalyst.adjustments: unknown {sorted(bad_adj)} (NC-20)")
    from trader.scanner.techniques import technique_names
    known = set(technique_names())
    for name in sc.disabled:
        tech, _, side = name.partition(":")
        if tech not in known or side not in ("", "long", "short"):
            p.append(f"scanner.disabled: unknown technique {name!r} "
                     f"(known: {', '.join(sorted(known))}; optional :long or :short)")
    # windows (TW-1)
    w = cfg.windows
    try:
        m, a = w.morning, w.afternoon
        if _t(m.entries_from) < time(9, 35):
            p.append("windows.morning.entries_from >= 09:35 (TW-1)")
        if _t(w.macro_days.cpi_jobs_morning_entries_from) < time(9, 35):
            p.append("windows.macro_days.cpi_jobs_morning_entries_from >= 09:35 (TW-1)")
        if not _t(m.entries_from) <= _t(m.entries_until) < _t(m.flatten_at):
            p.append("windows.morning: entries_from <= entries_until < flatten_at")
        if _t(m.flatten_at) > time(10, 30):
            p.append("windows.morning.flatten_at <= 10:30 (TW-2)")
        for field in ("entries_from", "day_entries_until", "swing_entries_from",
                      "swing_entries_until"):
            if _t(getattr(a, field)) > time(15, 50):
                p.append(f"windows.afternoon.{field} <= 15:50 (TW-1)")
        if _t(a.entries_from) < time(15, 0):
            p.append("windows.afternoon.entries_from >= 15:00 (afternoon window)")
        if not _t(a.entries_from) <= _t(a.day_entries_until) < _t(a.flatten_at):
            p.append("windows.afternoon: entries_from <= day_entries_until < flatten_at")
        if not _t(a.swing_entries_from) <= _t(a.swing_entries_until):
            p.append("windows.afternoon: swing_entries_from <= swing_entries_until")
        if _t(a.flatten_at) > time(15, 55):
            p.append("windows.afternoon.flatten_at <= 15:55")
    except (ValueError, AttributeError) as exc:
        p.append(f"windows: times must be HH:MM ({exc})")
    if base_dir is not None and not (base_dir / w.macro_calendar_file).exists():
        p.append(f"windows.macro_calendar_file not found: {w.macro_calendar_file}")
    # filters / entries / exits
    if cfg.filters.min_rvol < 1.0:
        p.append("filters.min_rvol >= 1.0")
    e = cfg.entries
    if e.min_reward_risk < 1.5:
        p.append("entries.min_reward_risk >= 1.5")
    if e.min_confidence < 0.5:
        p.append("entries.min_confidence >= 0.5")
    if not 0 < e.unplanned_size_factor <= 1:
        p.append("entries.unplanned_size_factor in (0, 1] (DR-7)")
    if e.max_chase_atr5 > 0.5:
        p.append("entries.max_chase_atr5 <= 0.5 (CK-6)")
    for style in ("day", "swing"):
        f = getattr(cfg.exits, style).t1_fraction
        if not 0 < f < 1:
            p.append(f"exits.{style}.t1_fraction between 0 and 1")
    # risk
    r = cfg.risk
    if not 0 < r.risk_per_trade_pct <= 1.0:
        p.append("risk.risk_per_trade_pct <= 1.0")
    if not 0 < r.drawdown_risk_per_trade_pct <= r.risk_per_trade_pct:
        p.append("risk.drawdown_risk_per_trade_pct <= risk.risk_per_trade_pct (RC-1)")
    if not r.risk_per_trade_pct <= r.max_open_risk_pct <= 5.0:
        p.append("risk.max_open_risk_pct between risk_per_trade_pct and 5.0 (PF-1)")
    if not 0.3 <= r.max_pair_correlation <= 1.0 or r.correlation_lookback < 20:
        p.append("risk.max_pair_correlation 0.3-1.0, correlation_lookback >= 20 (PF-2)")
    if not 25 <= r.max_beta_exposure_pct <= 300:
        p.append("risk.max_beta_exposure_pct 25-300 (PF-3)")
    if not 0 < r.max_gap_loss_pct <= 5.0 or not 90 <= r.gap_percentile < 100:
        p.append("risk.max_gap_loss_pct 0-5, gap_percentile 90-99.9 (PF-4)")
    ex = cfg.execution
    if not 0.1 <= ex.max_price_deviation_pct <= 5.0 or not 0 < ex.max_adv_pct <= 10.0 \
            or ex.max_order_notional < 0:
        p.append("execution: max_price_deviation_pct 0.1-5, max_adv_pct 0-10, "
                 "max_order_notional >= 0 (OC-1..OC-4)")
    if not 0 < r.daily_loss_limit_pct <= 3.0:
        p.append("risk.daily_loss_limit_pct <= 3.0")
    if not 0 < r.max_leverage <= 1.0:
        p.append("risk.max_leverage <= 1.0 (SR-2, LL-11)")
    if r.max_position_pct > 100 or r.max_position_pct <= 0:
        p.append("risk.max_position_pct in (0, 100]")
    if r.max_trades_morning + r.max_trades_afternoon < 1:
        p.append("risk: at least one trade per day must be possible")
    # wash sale
    if cfg.wash_sale.policy == "strict" and cfg.wash_sale.lock_days < 31:
        p.append("wash_sale.lock_days >= 31 for strict")
    # data
    if not EMAIL_RE.search(cfg.data.sec_user_agent):
        p.append("data.sec_user_agent must contain an email (SEC fair access)")
    # llm
    if cfg.llm.temperature != 0:
        p.append("llm.temperature = 0 (QW-2)")
    for agent, version in cfg.llm.prompt_versions.items():
        if not prompt_exists(agent, version):
            p.append(f"llm.prompt_versions: prompt file missing for {agent}/{version} (PR-1)")
    # notifications / services
    if cfg.notifications.approval_timeout_sec > 120:
        p.append("notifications.approval_timeout_sec <= 120")
    if cfg.services.notifier_poll_sec.approval > 5:
        p.append("services.notifier_poll_sec.approval <= 5")
    # REQUIREMENTS 1.3
    try:
        mb = w.macro_days
        if not _t(mb.release_block_from) < _t(mb.release_block_until):
            p.append("windows.macro_days: release_block_from < release_block_until (TW-6)")
        _t(cfg.regime.trend_day_high_after)
    except (ValueError, AttributeError) as exc:
        p.append(f"windows.macro_days / regime: times must be HH:MM ({exc})")
    if not 0 < cfg.latency.budget_s <= 30:
        p.append("latency.budget_s in (0, 30] (LAT-1)")
    if not 0 < cfg.latency.prearm_tolerance_atr <= cfg.latency.prearm_atr:
        p.append("latency.prearm_tolerance_atr in (0, prearm_atr] (LAT-2)")
    if cfg.regime.high_vol_atr_ratio < 1.0:
        p.append("regime.high_vol_atr_ratio >= 1.0 (RG-1)")
    if not 0 < cfg.regime.risk_factor_high_vol <= 1:
        p.append("regime.risk_factor_high_vol in (0, 1] (RG-2)")
    if cfg.data.news_poll_open_s < 30:
        p.append("data.news_poll_open_s >= 30 (Finnhub free limit, NW-1)")
    if not 0 < cfg.data.quote_max_age_s <= 10:
        p.append("data.quote_max_age_s in (0, 10] (DQ-8)")
    a_ = cfg.alerts
    if min(a_.per_symbol_cap, a_.per_window_cap, a_.top_n, a_.digest_min) < 1:
        p.append("alerts: caps, top_n and digest_min >= 1 (AL-5, AL-6)")
    if cfg.execution.slippage_switch_r <= 0:
        p.append("execution.slippage_switch_r > 0 (EXQ-1)")
    if not 0 <= cfg.tax.set_aside_rate_pct <= 60:
        p.append("tax.set_aside_rate_pct in [0, 60] (TX-1)")
    o = cfg.options
    if not o.day_dte[0] <= o.day_dte[1] or not o.swing_dte[0] <= o.swing_dte[1]:
        p.append("options: DTE ranges must be ordered (low <= high)")
    if o.account_level < 3 and set(o.structures) & {"call_debit", "put_debit", "put_credit",
                                                     "call_credit"}:
        p.append("options.structures: spreads need options.account_level >= 3 (A15-4)")
    if o.account_level < 2:
        p.append("options.account_level >= 2 (long options) for any suggestion (A15-4)")
    unknown = set(o.structures) - {"long_call", "long_put", "call_debit", "put_debit",
                                   "put_credit", "call_credit"}
    if unknown:
        p.append(f"options.structures: only defined-risk structures allowed (OP-1): "
                 f"{sorted(unknown)}")
    # storage
    if check_env and not os.environ.get(cfg.storage.journal_url_env):
        p.append(f"storage: env var {cfg.storage.journal_url_env} must be set")
    # modes (go-live gates, TEST_PLAN.md §7)
    if gates_passed is not None:
        for style in ("day", "swing"):
            if cfg.style_mode(style) == "auto" and f"auto_{style}" not in gates_passed:
                p.append(f"mode.{style}_trades: auto not allowed until gate auto_{style} is "
                         "recorded as passed")
        if cfg.mode.environment == "live" and "live" not in gates_passed:
            p.append("mode.environment: live not allowed until gate 'live' is recorded")
        if cfg.mode.environment == "live" and cfg.data.alpaca_feed == "iex" \
                and "data_sip" not in gates_passed:
            p.append("data.alpaca_feed: live on IEX needs the DQ-7 data gate (gate 'data_sip') "
                     "or the SIP feed")
        if o.mode == "paper" and "options_paper" not in gates_passed:
            p.append("options.mode: paper needs the A16-4 promotion gate 'options_paper'")
        if o.mode == "live" and "options_live" not in gates_passed:
            p.append("options.mode: live needs gate 'options_live' (OPTIONS_REQUIREMENTS §9)")
    return p


def prompt_exists(agent: str, version: str) -> bool:
    try:
        return resources.files("trader.prompts").joinpath(agent, f"{version}.md").is_file()
    except (ModuleNotFoundError, FileNotFoundError):
        return False


def parse(yaml_text: str) -> Config:
    raw = yaml.safe_load(yaml_text) or {}
    try:
        return Config.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError([f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}"
                           for err in exc.errors()]) from exc


def load(path: str | os.PathLike[str] | None = None, *, check_env: bool = False,
         gates_passed: set[str] | None = None) -> LoadedConfig:
    """Load, validate and freeze the config. Raises ConfigError naming each broken rule."""
    path = Path(path or os.environ.get("TRADER_CONFIG", "config/config.yaml"))
    text = path.read_text(encoding="utf-8")
    cfg = parse(text)
    base_dir = _base_dir(path)
    problems = validate(cfg, base_dir=base_dir, check_env=check_env, gates_passed=gates_passed)
    if problems:
        raise ConfigError(problems)
    return LoadedConfig(config=cfg, yaml_text=text, sha256=sha256_text(text), path=str(path))


def from_text(text: str, path: str = "<memory>") -> LoadedConfig:
    cfg = parse(text)
    problems = validate(cfg)
    if problems:
        raise ConfigError(problems)
    return LoadedConfig(config=cfg, yaml_text=text, sha256=sha256_text(text), path=path)


def _base_dir(config_path: Path) -> Path:
    """The repo/app root: the config lives in <root>/config/."""
    parent = config_path.resolve().parent
    return parent.parent if parent.name == "config" else parent


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def default_config(**overrides: Any) -> Config:
    """v1 defaults with the owner's list; used by tests and synthetic replays."""
    data: dict[str, Any] = {
        "universe": {
            "tickers": ["NVDA", "AAPL", "GOOGL", "MSFT", "AMZN", "TSM", "SPCX", "META", "AVGO",
                        "TSLA"],
            "context_symbols": ["SPY", "QQQ", "SMH"],
            "substantially_identical": {"GOOGL": ["GOOG"]},
            "correlated_groups": {"semis": {"tickers": ["NVDA", "AVGO", "TSM"], "max_open": 1}},
        },
        "mode": {"environment": "paper", "day_trades": "auto", "swing_trades": "auto"},
    }
    for key, value in overrides.items():
        section, _, field = key.partition("__")
        if field:
            data.setdefault(section, {})
            target = data[section]
            parts = field.split("__")
            for part in parts[:-1]:
                target = target.setdefault(part, {})
            target[parts[-1]] = value
        else:
            data[section] = value
    return Config.model_validate(data)
