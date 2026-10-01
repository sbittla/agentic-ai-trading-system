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

StyleMode = Literal["paper", "approve", "auto"]


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


class WashSaleCfg(_Frozen):
    policy: Literal["strict", "balanced", "year_end_only"] = "strict"
    lock_days: int = 31
    balanced_min_loss_r: float = 0.5
    year_end_strict_from: str = "12-01"


class DataCfg(_Frozen):
    alpaca_feed: Literal["iex", "sip"] = "iex"
    stale_after_sec: int = 60
    news_poll_min: int = 15
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
