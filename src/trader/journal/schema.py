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
    UniqueConstraint,
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
                   Column("suspicious", Boolean, default=False),
                   Column("kind", String(16)),          # news | 8-K | post | rating | form4
                   Column("publisher", String(64)))     # NC-3: the outlet behind the feed

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

# --- REQUIREMENTS 1.3 ------------------------------------------------------------------------

latency = Table("latency", metadata, _id(),                       # LAT-1, LAT-4
                Column("proposal_id", String(64), index=True),
                Column("setup_id", String(64)),
                Column("ticker", String(8)), Column("setup", String(32)),
                Column("mode", String(12)),
                Column("trigger_ts", UtcDateTime, index=True),
                Column("order_ts", UtcDateTime),
                Column("latency_s", Float), Column("model_s", Float),
                Column("owner_s", Float),
                Column("prearmed", Boolean),
                Column("outcome", String(16)),        # ORDER | MISSED
                Column("reason_code", String(32)))

shadow_trades = Table("shadow_trades", metadata, _id(),           # SH-1, SH-2
                      Column("setup_id", String(64), index=True),
                      Column("proposal_id", String(64)),
                      Column("ts", UtcDateTime, index=True),
                      Column("ticker", String(8)), Column("setup", String(32)),
                      Column("window", String(10)), Column("style", String(6)),
                      Column("direction", String(5)),
                      Column("taken", Boolean),
                      Column("reason_code", String(32)),
                      Column("entry", Money), Column("stop", Money),
                      Column("t1", Money), Column("t2", Money),
                      Column("filled_at", UtcDateTime), Column("closed_at", UtcDateTime),
                      Column("exit_price", Money), Column("exit_reason", String(24)),
                      Column("r_multiple", Float), Column("status", String(10)))

alerts_log = Table("alerts_log", metadata, _id(),                 # AL-1..AL-7
                   Column("ts", UtcDateTime, index=True),
                   Column("ticker", String(8), index=True), Column("kind", String(24)),
                   Column("direction", String(5)),
                   Column("label", String(10)),        # raw | qualified
                   Column("checklist_score", Integer), Column("first_failed", String(48)),
                   Column("quality", Float),
                   Column("delivery", String(10)),     # sent | digest | capped | off | muted
                   Column("price", Money), Column("stop", Money), Column("t1", Money),
                   Column("text", Text), Column("plan_json", JSON),
                   Column("hit", Boolean), Column("graded_at", UtcDateTime))

regime_daily = Table("regime_daily", metadata, _id(),             # RG-1
                     Column("date", Date, index=True),
                     Column("regime", String(10)),
                     Column("spy_atr_ratio", Float), Column("spy_gap_pct", Float),
                     Column("detail", Text))

data_divergence = Table("data_divergence", metadata, _id(),       # DQ-6
                        Column("date", Date, index=True),
                        Column("ticker", String(8)),
                        Column("iex_or_high", Float), Column("sip_or_high", Float),
                        Column("iex_or_low", Float), Column("sip_or_low", Float),
                        Column("or_diff_pct", Float), Column("iex_volume_share_pct", Float),
                        Column("setups_checked", Integer), Column("setups_within", Integer))

external_trades = Table("external_trades", metadata, _id(),       # WS-9
                        Column("imported_at", UtcDateTime),
                        Column("broker", String(16)), Column("account", String(32)),
                        Column("ts", UtcDateTime, index=True),
                        Column("ticker", String(8), index=True),
                        Column("side", String(4)), Column("qty", Float),
                        Column("price", Money), Column("fees", Money),
                        Column("source_file", Text),
                        Column("row_hash", String(64), unique=True))

owner_notes = Table("owner_notes", metadata, _id(),               # UI-29 (console writes)
                    Column("ts", UtcDateTime, index=True),
                    Column("ref_type", String(16)), Column("ref_id", String(64), index=True),
                    Column("tags", JSON), Column("note", Text),
                    Column("screenshot_b64", Text))

analyst_actions = Table("analyst_actions", metadata, _id(),       # UI-33 (news writes)
                        Column("ts", UtcDateTime, index=True),
                        Column("ticker", String(12), index=True),
                        Column("firm", String(64)), Column("action", String(12)),
                        Column("from_rating", String(32)), Column("to_rating", String(32)),
                        Column("pt_old", Float), Column("pt_new", Float),
                        Column("headline", Text), Column("source", String(16)),
                        Column("url", Text), Column("action_hash", String(40), unique=True))

market_recap = Table("market_recap", metadata, _id(),             # UI-34 (core writes)
                     Column("run_at", UtcDateTime, index=True),
                     Column("as_of", Date, index=True),
                     Column("ticker", String(12)),
                     Column("metrics_json", JSON))

console_chats = Table("console_chats", metadata, _id(),           # UI-8d (console writes)
                      Column("ts", UtcDateTime, index=True),
                      Column("day", Date, index=True),
                      Column("question", Text), Column("answer", Text),
                      Column("tool", String(32)), Column("rows_json", JSON),
                      Column("model_used", Boolean))

in_play = Table("in_play", metadata, _id(),                       # UN-1, UN-2
                Column("date", Date, index=True),
                Column("ticker", String(8)),
                Column("gap_pct", Float), Column("pm_volume_ratio", Float),
                Column("news", Boolean), Column("rank", Integer),
                Column("on_list", Boolean))

ops_checks = Table("ops_checks", metadata, _id(),                 # OPS-1, OPS-2
                   Column("ts", UtcDateTime, index=True),
                   Column("kind", String(16)),          # restore_test | drill
                   Column("passed", Boolean),
                   Column("detail", JSON))

options_iv_daily = Table("options_iv_daily", metadata, _id(),     # A13-3, A13-6
                         Column("date", Date, index=True),
                         Column("ticker", String(8), index=True),
                         Column("atm_iv_30d", Float), Column("iv_rank", Float),
                         Column("iv_pct", Float), Column("expected_move_30d", Float),
                         Column("history_days", Integer))

option_suggestions = Table("option_suggestions", metadata, _id(),  # A14, A16
                           Column("suggestion_id", String(64), unique=True),
                           Column("ts", UtcDateTime, index=True),
                           Column("ticker", String(8), index=True),
                           Column("direction", String(5)), Column("source", String(24)),
                           Column("style", String(6)), Column("structure", String(16)),
                           Column("expiry", Date), Column("legs_json", JSON),
                           Column("contracts", Integer),
                           Column("entry_price", Money),        # net debit (+) / credit (-)
                           Column("max_loss", Money), Column("max_gain", Money),
                           Column("breakeven", Float),
                           Column("underlying", Float), Column("invalidation", Float),
                           Column("t1", Float), Column("iv_rank", Float),
                           Column("detail_json", JSON), Column("text", Text),
                           Column("status", String(10)),        # open | closed
                           Column("mark", Money), Column("marked_at", UtcDateTime),
                           Column("exit_price", Money), Column("exit_reason", String(24)),
                           Column("closed_at", UtcDateTime),
                           Column("pnl", Money), Column("r_multiple", Float),
                           Column("stock_view_right", Boolean))

# --- news catalysts (NEWS_CATALYST_REQUIREMENTS.md §8) ---------------------------------------
news_embeddings = Table("news_embeddings", metadata, _id(),       # NC-1 (news writes)
                        Column("item_id", String(128), unique=True),
                        Column("ticker", String(12), index=True),
                        Column("model", String(64)),
                        Column("vec", Text))                     # base64 float16

social_posts = Table("social_posts", metadata, _id(),             # NC-11 (news writes)
                     Column("item_id", String(128), unique=True),
                     Column("ticker", String(12), index=True),
                     Column("ts", UtcDateTime, index=True),
                     Column("source", String(16)),
                     Column("author", String(64)),               # salted hash, never the name
                     Column("score", Float),                     # -1..+1 from the social model
                     Column("tag", String(8)),                   # user's own Bullish/Bearish
                     Column("length", Integer),
                     Column("text_hash", String(16)),
                     Column("model", String(64)))

fundamentals_daily = Table("fundamentals_daily", metadata, _id(),  # NC-16 (news writes)
                           Column("date", Date, index=True),
                           Column("ticker", String(12), index=True),
                           Column("kind", String(24)),   # earnings|recommendation|insider|...
                           Column("json", JSON))

news_stories = Table("news_stories", metadata, _id(),             # NC-1..NC-6 (core writes)
                     Column("story_id", String(64), unique=True),
                     Column("ticker", String(12), index=True),
                     Column("first_seen", UtcDateTime, index=True),
                     Column("last_seen", UtcDateTime),
                     Column("ctype", String(20)), Column("direction", String(8)),
                     Column("type_source", String(8)), Column("prompt_version", String(16)),
                     Column("novelty", Float), Column("items", Integer),
                     Column("sources", Integer), Column("sentiment", Float),
                     Column("strength", Float), Column("headline", Text),
                     Column("item_ids", JSON))

catalyst_outlooks = Table("catalyst_outlooks", metadata, _id(),   # NC-17..NC-26 (core)
                          Column("outlook_id", String(64), unique=True),
                          Column("ts", UtcDateTime, index=True),
                          Column("as_of", Date, index=True),
                          Column("ticker", String(12), index=True),
                          Column("story_id", String(64)),
                          Column("strength", Float), Column("label", String(10)),
                          Column("direction", String(8)), Column("ctype", String(20)),
                          Column("factors", JSON), Column("confluence", JSON),
                          Column("social", JSON), Column("horizons", JSON),
                          Column("stance", String(10)), Column("vehicle", String(24)),
                          Column("plan", JSON), Column("calibrated", Boolean),
                          Column("text", Text))

catalyst_grades = Table("catalyst_grades", metadata, _id(),       # NC-35 (core writes)
                        Column("outlook_id", String(64), index=True),
                        Column("as_of", Date, index=True),
                        Column("ticker", String(12)),
                        Column("horizon", Integer),
                        Column("p_up", Float), Column("p10", Float), Column("p90", Float),
                        Column("median", Float), Column("stance", String(10)),
                        Column("realized", Float), Column("realized_excess", Float),
                        Column("in_range", Boolean), Column("up", Boolean),
                        Column("graded_at", UtcDateTime))

# --- macro (MACRO_FORECAST_REQUIREMENTS.md §5) ---------------------------------------------
macro_series = Table("macro_series", metadata, _id(),             # MA-1 (news writes)
                     Column("series_id", String(24), index=True),
                     Column("date", Date, index=True),
                     Column("value", Float),
                     UniqueConstraint("series_id", "date", name="uq_macro_series_date"))

macro_news = Table("macro_news", metadata, _id(),                 # MA-4 (news writes)
                   Column("item_id", String(64), unique=True),
                   Column("ts", UtcDateTime, index=True),
                   Column("source", String(16)),
                   Column("headline", Text), Column("cleaned_text", Text),
                   Column("theme", String(16)), Column("tone", String(10)),
                   Column("sentiment", Float), Column("suspicious", Boolean))

macro_daily = Table("macro_daily", metadata, _id(),               # MA-6 (core writes)
                    Column("date", Date, unique=True),
                    Column("score", Float), Column("label", String(10)),
                    Column("indicators", JSON), Column("releases", JSON))

momentum_signals = Table("momentum_signals", metadata, _id(),     # MO-15 (core writes)
                         Column("signal_id", String(64), unique=True),
                         Column("ticker", String(10), index=True),
                         Column("setup", String(24)), Column("policy", String(12)),
                         Column("asof", Date, index=True),       # the close it was armed on
                         Column("state", String(10), index=True),  # ARMED OPEN CLOSED ...
                         Column("entry_type", String(6)),
                         Column("trigger", Float), Column("stop", Float),
                         Column("rs", Integer), Column("template", Integer),
                         Column("proven", Boolean), Column("alerted", Boolean),
                         Column("shares", Integer), Column("risk_usd", Float),
                         Column("entry_date", Date), Column("entry_price", Float),
                         Column("exit_date", Date), Column("r", Float),
                         Column("exit_reason", String(16)),
                         Column("position", JSON),     # exits.Position state
                         Column("sent", JSON),         # alert keys already sent (MO-15)
                         Column("events", JSON),
                         Column("why", Text),
                         Column("updated", UtcDateTime))

event_ideas = Table("event_ideas", metadata, _id(),               # EV-9 (core writes)
                    Column("idea_id", String(96), unique=True),  # group:event_date:window
                    Column("group_id", String(32), index=True),
                    Column("ticker", String(10), index=True),
                    Column("kind", String(16)), Column("side", String(6)),
                    Column("window", String(10)),
                    Column("event_date", Date), Column("estimated", Boolean),
                    Column("enter", Date), Column("exit", Date, index=True),
                    Column("verdict", String(20)),   # ok | earnings_in_window | ...unknown
                    Column("earnings_date", Date),
                    Column("basis", String(48)), Column("n", Integer),
                    Column("mean_pct", Float), Column("same_sign_rate", Float),
                    Column("p", Float),
                    Column("first_seen", Date), Column("updated", UtcDateTime),
                    Column("realized_pct", Float),       # net of SPY, close enter -> exit
                    Column("realized_raw_pct", Float),
                    Column("graded", Date))

# Writers by role (DATA_SPEC §3, SEC-17): applied by `trader migrate` on Postgres.
NEWS_WRITES = ("news_items", "event_flags", "llm_calls", "analyst_actions", "news_embeddings",
               "social_posts", "fundamentals_daily", "macro_series", "macro_news")
NEWS_UPDATES = ("fundamentals_daily",)   # NC-16: same-day refresh of a snapshot
NOTIFIER_WRITES = ("commands",)
CONSOLE_WRITES = ("owner_notes", "console_chats", "llm_calls")   # llm_calls: UI-8c log
NEWS_READS = ("positions", "game_plans")   # NW-1: tickers with a position or a plan setup
