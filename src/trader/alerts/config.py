"""Alerts settings: ``config/alerts.yaml`` (optional; every field has a default)."""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

MAX_IEX_SYMBOLS = 30   # Alpaca free plan: 30 symbols on the IEX stream


class SignalSettings(BaseModel):
    """Automatic scanner signals on every watched symbol (evaluated on 1-minute bar closes)."""

    model_config = ConfigDict(extra="forbid")

    opening_range_min: int = Field(default=15, ge=1, le=60)  # ORB: high/low of the first N minutes
    orb: bool = True
    vwap_cross: bool = True
    new_high_low: bool = True          # new high/low of day, after the opening range
    volume_spike: bool = True
    volume_spike_x: float = Field(default=4.0, gt=1)  # 1-min volume vs the last 20 bars' average
    volume_spike_min_avg: float = 500           # ignore illiquid minutes (IEX shares)
    gap: bool = True
    gap_pct: float = Field(default=2.0, gt=0)
    move_step_pct: float = Field(default=2.0, ge=0)  # alert at every ±step% from prior close; 0=off
    cooldown_min: int = Field(default=15, ge=0)  # per symbol and signal (VWAP, HOD/LOD, volume)


class AlertsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    watchlist: list[str] = Field(default_factory=lambda: [
        "NVDA", "AAPL", "GOOGL", "MSFT", "AMZN", "TSM", "META", "AVGO", "TSLA", "SPY", "QQQ"])
    feed: str = "iex"                           # iex (free) | sip (paid plan)
    connect_before_open_min: int = Field(default=5, ge=0, le=120)
    disconnect_after_close_min: int = Field(default=1, ge=0, le=120)
    bad_print_pct: float = Field(default=5.0, gt=0)  # a lone trade this far from the last price
    signals: SignalSettings = Field(default_factory=lambda: SignalSettings())
    daily_summary: bool = True
    db_path: str = "data/alerts.sqlite"

    def symbols(self) -> list[str]:
        return normalize_symbols(self.watchlist)[:MAX_IEX_SYMBOLS]


def normalize_symbols(items: list[str]) -> list[str]:
    out: list[str] = []
    for raw in items:
        sym = raw.strip().upper().lstrip("$")
        if sym and sym not in out and sym.replace(".", "").isalnum() and len(sym) <= 6:
            out.append(sym)
    return out


def load(path: str | None = None) -> AlertsConfig:
    """``path``, else ``$ALERTS_CONFIG``, else ``config/alerts.yaml`` if it exists."""
    p = Path(path or os.environ.get("ALERTS_CONFIG") or "config/alerts.yaml")
    data: dict[str, object] = {}
    if p.exists():
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    cfg = AlertsConfig.model_validate(data)
    if os.environ.get("ALERTS_DB"):
        cfg = cfg.model_copy(update={"db_path": os.environ["ALERTS_DB"]})
    return cfg
