"""Journal tables (DATA_SPEC.md §3). Same schema on Postgres (live/paper) and SQLite (replay).

Postgres uses schema ``journal``; SQLite maps it away with ``schema_translate_map``.
Times are stored in UTC (IF-2); money is exact (IF-1).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    TypeDecorator,
)
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeEngine

SCHEMA = "journal"
metadata = MetaData(schema=SCHEMA)


class UtcDateTime(TypeDecorator[datetime]):
    """Timezone-aware UTC timestamps on every backend."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime written to the journal (IF-2)")
        value = value.astimezone(UTC)
        return value.replace(tzinfo=None) if dialect.name == "sqlite" else value

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class Money(TypeDecorator[Decimal]):
    """Exact decimal: NUMERIC on Postgres, text on SQLite."""

    impl = String(40)
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(Numeric(20, 6))
        return dialect.type_descriptor(String(40))

    def process_bind_param(self, value: Any, dialect: Dialect) -> Any:
        if value is None:
            return None
        d = value if isinstance(value, Decimal) else Decimal(str(value))
        return d if dialect.name == "postgresql" else str(d)

    def process_result_value(self, value: Any, dialect: Dialect) -> Decimal | None:
        if value is None:
            return None
        return value if isinstance(value, Decimal) else Decimal(str(value))


def _id() -> Column[int]:
    return Column("id", Integer().with_variant(BigInteger(), "postgresql"), primary_key=True,
                  autoincrement=True)


events = Table("events", metadata, _id(),
               Column("ts", UtcDateTime, nullable=False, index=True),
               Column("type", String(48), nullable=False, index=True),
               Column("payload", JSON, nullable=False))

config_snapshots = Table("config_snapshots", metadata, _id(),
                         Column("effective_date", Date, nullable=False),
                         Column("created_at", UtcDateTime, nullable=False),
                         Column("yaml_hash", String(64), nullable=False),
                         Column("yaml_text", Text, nullable=False))

universe = Table("universe", metadata, _id(),
                 Column("effective_date", Date, nullable=False, index=True),
                 Column("ticker", String(12), nullable=False),
                 Column("rank", Integer),
                 Column("market_cap", Float),
                 Column("on_owner_list", Boolean, nullable=False))

bars_1m = Table("bars_1m", metadata, _id(),
                Column("ticker", String(12), nullable=False, index=True),
                Column("ts", UtcDateTime, nullable=False, index=True),
                Column("o", Float), Column("h", Float), Column("l", Float), Column("c", Float),
                Column("v", BigInteger), Column("vwap", Float), Column("source", String(12)))

bars_1d = Table("bars_1d", metadata, _id(),
                Column("ticker", String(12), nullable=False, index=True),
                Column("ts", UtcDateTime, nullable=False, index=True),
                Column("o", Float), Column("h", Float), Column("l", Float), Column("c", Float),
                Column("v", BigInteger), Column("vwap", Float), Column("source", String(12)))

levels = Table("levels", metadata, _id(),
               Column("date", Date, nullable=False, index=True),
               Column("ticker", String(12), nullable=False),
               Column("prior_high", Float), Column("prior_low", Float),
               Column("prior_close", Float), Column("pm_high", Float), Column("pm_low", Float),
               Column("ema20d", Float), Column("ema50d", Float), Column("atr14d", Float),
               Column("json", JSON))

news_items = Table("news_items", metadata, _id(),
                   Column("item_id", String(128), nullable=False, index=True),
                   Column("ticker", String(12), nullable=False, index=True),
                   Column("source", String(16), nullable=False),
                   Column("published_at", UtcDateTime, nullable=False),
                   Column("received_at", UtcDateTime, nullable=False),
                   Column("headline", Text), Column("cleaned_text", Text),
                   Column("raw_text", Text),
                   Column("finbert_score", Float),
                   Column("suspicious", Boolean, default=False))

event_flags = Table("event_flags", metadata, _id(),
                    Column("date", Date, nullable=False, index=True),
                    Column("ticker", String(12), nullable=False),
                    Column("flag", String(24), nullable=False),
                    Column("detail", Text))

game_plans = Table("game_plans", metadata, _id(),
                   Column("date", Date, nullable=False, index=True),
                   Column("window", String(10), nullable=False),
                   Column("ticker", String(12)),
                   Column("setup", String(32)),
                   Column("trigger", Float), Column("stop", Float), Column("target", Float),
                   Column("notes", Text),
                   Column("off_limits", JSON),
                   Column("source", String(10)))

# A5-1 preview: after-close screen + picks for the next session, graded after that session
next_day_ideas = Table(
    "next_day_ideas", metadata, _id(),
    Column("run_at", UtcDateTime, nullable=False),
    Column("plan_for", Date, nullable=False, index=True),
    Column("session_date", Date, nullable=False),
    Column("ticker", String(12), nullable=False),
    Column("rank", Integer), Column("status", String(8)),   # pick / watch / off
    Column("score", Integer), Column("close", Float), Column("change_pct", Float),
    Column("range_pos", Float), Column("volume_ratio", Float), Column("trend_up", Boolean),
    Column("atr_to_prior_high", Float), Column("atr_to_20d_closing_high", Float),
    Column("atr_to_ema20d", Float),
    Column("news_tone", String(12)), Column("news_catalyst", Text),
    Column("morning_setup", String(32)), Column("morning_trigger", Float),
    Column("morning_stop", Float), Column("morning_target", Float),
    Column("afternoon_setup", String(32)), Column("afternoon_trigger", Float),
    Column("afternoon_stop", Float), Column("afternoon_target", Float),
    Column("note", Text),
    Column("day_high", Float), Column("day_low", Float), Column("day_close", Float),
    Column("triggered", Boolean), Column("reached_t1", Boolean), Column("hit_stop", Boolean),
    Column("outcome", String(16)))

setups = Table("setups", metadata,
               Column("id", String(64), primary_key=True),
               Column("ts", UtcDateTime, nullable=False, index=True),
               Column("ticker", String(12), nullable=False),
               Column("style", String(6)), Column("window", String(10)),
               Column("setup_name", String(32)),
               Column("trigger", Money), Column("entry", Money), Column("stop", Money),
               Column("t1", Money), Column("t2", Money),
               Column("score", Integer), Column("filters_json", JSON),
               Column("context_json", JSON))

proposals = Table("proposals", metadata,
                  Column("proposal_id", String(64), primary_key=True),
                  Column("setup_id", String(64), index=True),
                  Column("ts", UtcDateTime),
                  Column("ticker", String(12)),
                  Column("json", JSON), Column("valid", Boolean),
                  Column("error", Text),
                  Column("model", String(64)), Column("prompt_version", String(16)),
                  Column("latency_ms", Integer), Column("tokens", Integer))

reviews = Table("reviews", metadata,
                Column("proposal_id", String(64), primary_key=True),
                Column("ts", UtcDateTime),
                Column("verdict", String(10)), Column("main_risk", Text),
                Column("rules_broken", JSON), Column("json", JSON))

compliance_checks = Table("compliance_checks", metadata, _id(),
                          Column("proposal_id", String(64), index=True),
                          Column("ts", UtcDateTime),
                          Column("kind", String(16)),       # entry | loss_exit
                          Column("result", String(16)),
                          Column("locks_found", JSON),
                          Column("detail", Text))

decisions = Table("decisions", metadata, _id(),
                  Column("proposal_id", String(64), index=True),
                  Column("ts", UtcDateTime, index=True),
                  Column("ticker", String(12)), Column("setup", String(32)),
                  Column("window", String(10)), Column("style", String(6)),
                  Column("result", String(16)), Column("reason_code", String(32), index=True),
                  Column("qty", Integer), Column("risk_usd", Money),
                  Column("checklist_json", JSON), Column("detail", Text),
                  Column("override", Boolean, default=False))

approvals = Table("approvals", metadata, _id(),
                  Column("proposal_id", String(64), index=True),
                  Column("code", String(8)),
                  Column("sent_at", UtcDateTime), Column("answered_at", UtcDateTime),
                  Column("answer", String(10)))

orders = Table("orders", metadata, _id(),
               Column("client_order_id", String(64), index=True),
               Column("broker_id", String(64), index=True),
               Column("proposal_id", String(64), index=True),
               Column("leg", String(16)), Column("type", String(16)),
               Column("side", String(4)), Column("ticker", String(12)),
               Column("qty", Integer), Column("limit", Money), Column("stop", Money),
               Column("take_profit", Money),
               Column("status", String(20)), Column("ts", UtcDateTime),
               Column("route", String(8)))

fills = Table("fills", metadata, _id(),
              Column("broker_id", String(64), index=True),
              Column("client_order_id", String(64)),
              Column("ticker", String(12)), Column("side", String(4)),
              Column("ts", UtcDateTime, index=True), Column("qty", Integer),
              Column("price", Money))

positions = Table("positions", metadata,
                  Column("position_id", String(64), primary_key=True),
                  Column("ticker", String(12), index=True),
                  Column("style", String(6)), Column("window", String(10)),
                  Column("setup", String(32)), Column("planned", Boolean),
                  Column("opened_at", UtcDateTime), Column("closed_at", UtcDateTime),
                  Column("qty", Integer), Column("avg_entry", Money), Column("avg_exit", Money),
                  Column("initial_stop", Money), Column("risk_usd", Money),
                  Column("pnl", Money), Column("r_multiple", Float),
                  Column("exit_reason", String(32)), Column("route", String(8)),
                  Column("override", Boolean, default=False),
                  Column("state_json", JSON))

tax_lots = Table("tax_lots", metadata,
                 Column("lot_id", String(64), primary_key=True),
                 Column("ticker", String(12), index=True),
                 Column("position_id", String(64)),
                 Column("acquired_at", UtcDateTime), Column("qty", Integer),
                 Column("remaining_qty", Integer),
                 Column("cost", Money), Column("adjusted_basis", Money),
                 Column("disposed_at", UtcDateTime), Column("proceeds", Money),
                 Column("realized_pnl", Money),
                 Column("disallowed_loss", Money), Column("wash_link_lot_id", String(64)),
                 Column("route", String(8)))

locks = Table("locks", metadata, _id(),
              Column("ticker", String(12), index=True),
              Column("lock_type", String(16)),
              Column("start", UtcDateTime), Column("until", UtcDateTime),
              Column("reason", Text))

daily_metrics = Table("daily_metrics", metadata,
                      Column("date", Date, primary_key=True),
                      Column("trades", Integer), Column("wins", Integer),
                      Column("losses", Integer), Column("pnl", Money),
                      Column("peak_pnl", Money), Column("max_dd", Money),
                      Column("expectancy_r", Float), Column("rule_breaches", Integer),
                      Column("equity", Money))

equity = Table("equity", metadata, _id(),
               Column("ts", UtcDateTime, index=True),
               Column("equity", Money), Column("hwm", Money))

llm_calls = Table("llm_calls", metadata, _id(),
                  Column("ts", UtcDateTime, index=True),
                  Column("agent", String(16)), Column("prompt_version", String(16)),
                  Column("model", String(64)),
                  Column("input_hash", String(64), index=True),
                  Column("prompt", Text),
                  Column("output", Text), Column("valid", Boolean), Column("error", Text),
                  Column("latency_ms", Integer), Column("input_tokens", Integer),
                  Column("output_tokens", Integer), Column("cached", Boolean))

incidents = Table("incidents", metadata, _id(),
                  Column("ts", UtcDateTime, index=True),
                  Column("type", String(32), index=True),
                  Column("detail", Text),
                  Column("resolved_at", UtcDateTime))

commands = Table("commands", metadata, _id(),
                 Column("ts", UtcDateTime, index=True),
                 Column("sender", String(64)), Column("text", Text),
                 Column("accepted", Boolean), Column("reason", Text),
                 Column("source", String(16)))

status_snapshots = Table("status_snapshots", metadata, _id(),
                         Column("ts", UtcDateTime, index=True),
                         Column("json", JSON))

outbox = Table("outbox", metadata,
               _id(),
               Column("created_at", UtcDateTime, nullable=False),
               Column("source", Text, nullable=False),
               Column("channel", Text, nullable=False, index=True),
               Column("payload", JSON, nullable=False),
               Column("consumed_at", UtcDateTime))

# Writers by role (DATA_SPEC §3, SEC-17): applied by `trader migrate` on Postgres.
NEWS_WRITES = ("news_items", "event_flags", "llm_calls")
NOTIFIER_WRITES = ("commands",)
