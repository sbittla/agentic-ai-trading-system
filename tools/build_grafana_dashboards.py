"""Generate the Grafana dashboards in docker/grafana/dashboards/ (docs/MONITORING.md §3).

Every panel is a read-only SQL query on the journal (ADR-012), run as the readonly role.
Edit the queries here, then regenerate:

    python tools/build_grafana_dashboards.py

Grafana reloads the files within 30 s; no restart needed.
"""
# ruff: noqa: S608  -- SQL is built from constant table names only; no user input reaches it
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

OUT = Path(__file__).resolve().parents[1] / "docker" / "grafana" / "dashboards"
DS = {"type": "grafana-postgresql-datasource", "uid": "journal"}
J = "journal"

# Latest status snapshot written by trader-core every 30 s.
SNAP = f"(SELECT json, ts FROM {J}.status_snapshots ORDER BY id DESC LIMIT 1)"
# Start of today's session in US Eastern time.
TODAY = "date_trunc('day', now() AT TIME ZONE 'America/New_York') AT TIME ZONE 'America/New_York'"

HEALTH_COLORS = {"type": "value", "options": {
    "green": {"color": "green", "index": 0},
    "amber": {"color": "orange", "index": 1},
    "red": {"color": "red", "index": 2}}}


class Board:
    def __init__(self, uid: str, title: str, time_from: str = "now-24h", refresh: str = "30s"):
        self.uid, self.title, self.time_from, self.refresh = uid, title, time_from, refresh
        self.panels: list[dict[str, Any]] = []
        self.x = self.y = self.row_h = 0

    def add(self, title: str, sql: str, *, kind: str = "table", w: int = 12, h: int = 8,
            fmt: str = "table", unit: str | None = None, desc: str = "",
            thresholds: list[tuple[str, float | None]] | None = None,
            mappings: list[dict[str, Any]] | None = None, color_cells: bool = False,
            options: dict[str, Any] | None = None, decimals: int | None = None,
            min_max: tuple[float, float] | None = None,
            overrides: list[dict[str, Any]] | None = None) -> None:
        if self.x + w > 24:
            self.x, self.y, self.row_h = 0, self.y + self.row_h, 0
        defaults: dict[str, Any] = {}
        if unit:
            defaults["unit"] = unit
        if decimals is not None:
            defaults["decimals"] = decimals
        if min_max:
            defaults["min"], defaults["max"] = min_max
        if thresholds:
            defaults["thresholds"] = {"mode": "absolute", "steps": [
                {"color": c, "value": v} for c, v in thresholds]}
        if mappings:
            defaults["mappings"] = mappings
        if color_cells:
            defaults["custom"] = {"cellOptions": {"type": "color-background"}}
        panel: dict[str, Any] = {
            "id": len(self.panels) + 1, "type": kind, "title": title, "description": desc,
            "datasource": DS, "gridPos": {"x": self.x, "y": self.y, "w": w, "h": h},
            "targets": [{"refId": "A", "datasource": DS, "editorMode": "code",
                         "format": fmt, "rawQuery": True, "rawSql": sql.strip()}],
            "fieldConfig": {"defaults": defaults, "overrides": overrides or []},
            "options": options or {},
        }
        if kind == "stat":
            panel["options"] = {"colorMode": "background", "graphMode": "none",
                                "reduceOptions": {"calcs": ["lastNotNull"], "fields": "",
                                                  "values": False}, **(options or {})}
        self.panels.append(panel)
        self.x += w
        self.row_h = max(self.row_h, h)

    def write(self) -> Path:
        board = {"uid": self.uid, "title": self.title, "tags": ["trader"],
                 "timezone": "America/New_York", "schemaVersion": 39, "version": 1,
                 "editable": False, "refresh": self.refresh,
                 "time": {"from": self.time_from, "to": "now"}, "panels": self.panels}
        path = OUT / f"{self.uid}.json"
        path.write_text(json.dumps(board, indent=2) + "\n", encoding="utf-8")
        return path


def today() -> Board:
    b = Board("trader-today", "1. Today", time_from="now-12h", refresh="10s")
    b.add("System alive (last status, s ago)",
          f"SELECT extract(epoch FROM now() - ts)::int AS seconds FROM {SNAP} s",
          kind="stat", w=4, h=4, unit="s",
          thresholds=[("green", None), ("orange", 60), ("red", 120)],
          desc="trader-core writes a status every 30 s. Red means it has stopped.")
    b.add("Kill switch",
          f"SELECT CASE WHEN (json->>'kill_switch')::boolean THEN 'ON' ELSE 'off' END AS kill "
          f"FROM {SNAP} s",
          kind="stat", w=4, h=4, mappings=[{"type": "value", "options": {
              "ON": {"color": "red", "index": 0}, "off": {"color": "green", "index": 1}}}])
    b.add("Equity", f"SELECT (json->>'equity')::numeric AS equity FROM {SNAP} s",
          kind="stat", w=4, h=4, unit="currencyUSD",
          thresholds=[("blue", None)])
    b.add("Day P&L", f"SELECT (json->>'day_pnl')::numeric AS pnl FROM {SNAP} s",
          kind="stat", w=4, h=4, unit="currencyUSD",
          thresholds=[("red", None), ("green", 0)])
    b.add("Day P&L vs 1.5% loss limit",
          f"SELECT 100 * (json->>'day_pnl')::numeric / NULLIF((json->>'equity')::numeric, 0) "
          f"AS pct FROM {SNAP} s",
          kind="gauge", w=4, h=4, unit="percent", decimals=2,
          thresholds=[("red", None), ("orange", -1.5), ("yellow", -0.75), ("green", 0)],
          min_max=(-1.5, 1.5), options={"reduceOptions": {"calcs": ["lastNotNull"]}},
          desc="risk.daily_loss_limit_pct = 1.5; trading stops at the left edge.")
    b.add("Trades today (max 3)",
          f"SELECT (json->>'trades_today')::int AS trades FROM {SNAP} s",
          kind="stat", w=4, h=4, thresholds=[("green", None), ("orange", 2), ("red", 3)])
    b.add("Health checks",
          f"SELECT key AS check, value AS status "
          f"FROM {SNAP} s, json_each_text(s.json->'health') ORDER BY key",
          w=6, h=10, mappings=[HEALTH_COLORS], color_cells=True)
    b.add("Open positions",
          f"SELECT ticker, style, setup, qty, avg_entry, initial_stop, risk_usd, opened_at "
          f"FROM {J}.positions WHERE closed_at IS NULL ORDER BY opened_at", w=10, h=10)
    b.add("Active locks (no trading these tickers)",
          f"SELECT ticker, lock_type, until, reason FROM {J}.locks "
          f"WHERE until IS NULL OR until > now() ORDER BY until", w=8, h=10)
    b.add("Today's decisions",
          f"SELECT ts, ticker, setup, \"window\", result, reason_code, qty, risk_usd, detail "
          f"FROM {J}.decisions WHERE ts >= {TODAY} ORDER BY ts DESC", w=24, h=10)
    b.add("Game plans (today and next session)", f"""
WITH k AS (SELECT date, "window", max(id) AS mx FROM {J}.game_plans
           WHERE date >= (now() AT TIME ZONE 'America/New_York')::date GROUP BY 1, 2),
-- one plan run = one contiguous block of ids; keep only the latest run per date and window
b AS (SELECT k.*, (SELECT coalesce(max(o.id), 0) FROM {J}.game_plans o WHERE o.id < k.mx
                   AND (o.date, o."window") <> (k.date, k."window")) AS lo FROM k)
SELECT g.date, g."window", g.ticker, g.setup, g.trigger, g.stop, g.target, g.notes, g.source
FROM {J}.game_plans g JOIN b ON g.date = b.date AND g."window" = b."window" AND g.id > b.lo
ORDER BY g.date, g."window" DESC, g.id""", w=12, h=8,
          desc="preview = built after the previous close (trader plan / 4:15 PM ET); "
               "llm = the 9:00 AM or 2:45 PM refresh with gap and pre-market data.")
    b.add("Open incidents",
          f"SELECT ts, type, detail FROM {J}.incidents WHERE resolved_at IS NULL ORDER BY ts DESC",
          w=12, h=8)
    return b


def pipeline() -> Board:
    b = Board("trader-pipeline", "2. Pipeline", time_from="now-7d", refresh="30s")
    b.add("Funnel (selected time range)", f"""
SELECT * FROM (
  SELECT 1 AS n, '1 setups' AS stage, count(*) AS count FROM {J}.setups WHERE $__timeFilter(ts)
  UNION ALL SELECT 2, '2 proposals', count(*) FROM {J}.proposals WHERE $__timeFilter(ts)
  UNION ALL SELECT 3, '3 LLM agreed', count(*) FROM {J}.reviews
            WHERE $__timeFilter(ts) AND verdict = 'agree'
  UNION ALL SELECT 4, '4 risk approved', count(*) FROM {J}.decisions
            WHERE $__timeFilter(ts) AND result IN ('APPROVE', 'NEEDS_APPROVAL')
  UNION ALL SELECT 5, '5 owner said yes', count(*) FROM {J}.approvals
            WHERE $__timeFilter(sent_at) AND answer = 'yes'
  UNION ALL SELECT 6, '6 entry orders', count(*) FROM {J}.orders
            WHERE $__timeFilter(ts) AND leg = 'entry'
  UNION ALL SELECT 7, '7 entry fills', count(*) FROM {J}.fills f
            JOIN {J}.orders o USING (client_order_id) WHERE $__timeFilter(f.ts) AND o.leg = 'entry'
) t ORDER BY n""", kind="bargauge", w=12, h=10,
          options={"orientation": "horizontal", "displayMode": "basic",
                   "reduceOptions": {"calcs": ["lastNotNull"], "values": True,
                                     "fields": "/count/"}},
          thresholds=[("blue", None)])
    b.add("No-trade reasons", f"""
SELECT coalesce(reason_code, result) AS reason, count(*) AS count FROM {J}.decisions
WHERE $__timeFilter(ts) AND result NOT IN ('APPROVE', 'NEEDS_APPROVAL')
GROUP BY 1 ORDER BY 2 DESC""", kind="barchart", w=12, h=10,
          options={"orientation": "horizontal", "xField": "reason"},
          desc="MONITORING §2: one code above 70% for 5 days may mean a miscalibrated filter.")
    b.add("Decisions over time", f"""
SELECT $__timeGroupAlias(ts, '1h'), result AS metric, count(*) AS value FROM {J}.decisions
WHERE $__timeFilter(ts) GROUP BY 1, 2 ORDER BY 1""",
          kind="timeseries", fmt="time_series", w=24, h=8,
          options={"legend": {"displayMode": "list", "placement": "bottom"}})
    b.add("Approvals (response time, answer)", f"""
SELECT sent_at, proposal_id, answer,
       extract(epoch FROM answered_at - sent_at)::int AS seconds_to_answer
FROM {J}.approvals WHERE $__timeFilter(sent_at) ORDER BY sent_at DESC""", w=12, h=9)
    b.add("LLM reviews", f"""
SELECT r.ts, p.ticker, r.verdict, r.main_risk, r.rules_broken
FROM {J}.reviews r LEFT JOIN {J}.proposals p USING (proposal_id)
WHERE $__timeFilter(r.ts) ORDER BY r.ts DESC""", w=12, h=9)
    b.add("All decisions", f"""
SELECT ts, ticker, setup, style, "window", result, reason_code, qty, risk_usd, override, detail
FROM {J}.decisions WHERE $__timeFilter(ts) ORDER BY ts DESC LIMIT 500""", w=24, h=12)
    return b


def performance() -> Board:
    b = Board("trader-performance", "3. Performance", time_from="now-90d", refresh="1m")
    closed = f"FROM {J}.positions WHERE closed_at IS NOT NULL AND $__timeFilter(closed_at)"
    b.add("Closed trades", f"SELECT count(*) AS trades {closed}", kind="stat", w=4, h=4,
          thresholds=[("blue", None)])
    b.add("Win rate", f"SELECT 100.0 * avg((pnl > 0)::int) AS win_rate {closed}",
          kind="stat", w=4, h=4, unit="percent", decimals=0,
          thresholds=[("red", None), ("orange", 35), ("green", 45)])
    b.add("Expectancy (R per trade)", f"SELECT avg(r_multiple) AS expectancy {closed}",
          kind="stat", w=4, h=4, decimals=2, thresholds=[("red", None), ("green", 0)],
          desc="Alert when below 0 over 20 trades (RC-3).")
    b.add("Profit factor",
          f"SELECT sum(pnl) FILTER (WHERE pnl > 0) / NULLIF(-sum(pnl) FILTER (WHERE pnl < 0), 0) "
          f"AS profit_factor {closed}",
          kind="stat", w=4, h=4, decimals=2,
          thresholds=[("red", None), ("orange", 1), ("green", 1.3)],
          desc="Alert when below 1.3 over 30 trades.")
    b.add("Total P&L", f"SELECT coalesce(sum(pnl), 0) AS pnl {closed}",
          kind="stat", w=4, h=4, unit="currencyUSD", thresholds=[("red", None), ("green", 0)])
    b.add("Current drawdown",
          f"SELECT 100 * (equity - hwm) / NULLIF(hwm, 0) AS dd FROM {J}.equity "
          f"ORDER BY ts DESC LIMIT 1",
          kind="stat", w=4, h=4, unit="percent", decimals=2,
          thresholds=[("red", None), ("orange", -6), ("yellow", -3), ("green", -0.0001)],
          desc="-3% cuts size (RC-1); -6% in a month stops trading (LL-6).")
    b.add("Equity and high-water mark", f"""
SELECT ts AS time, equity, hwm AS high_water_mark FROM {J}.equity
WHERE $__timeFilter(ts) ORDER BY ts""", kind="timeseries", fmt="time_series", w=16, h=9,
          unit="currencyUSD")
    b.add("Drawdown %", f"""
SELECT ts AS time, 100 * (equity - hwm) / NULLIF(hwm, 0) AS drawdown FROM {J}.equity
WHERE $__timeFilter(ts) ORDER BY ts""", kind="timeseries", fmt="time_series", w=8, h=9,
          unit="percent")
    for by, col in (("setup", "setup"), ("ticker", "ticker"), ("window", '"window"'),
                    ("weekday", "to_char(closed_at AT TIME ZONE 'America/New_York', 'ID Dy')")):
        b.add(f"Expectancy by {by}", f"""
SELECT {col} AS {by.replace('window', 'trade_window')}, round(avg(r_multiple)::numeric, 2) AS avg_r,
       count(*) AS trades, round(sum(pnl), 2) AS pnl {closed}
GROUP BY 1 ORDER BY 1""", w=6, h=8)
    b.add("Daily metrics", f"""
SELECT date, trades, wins, losses, pnl, peak_pnl, max_dd, expectancy_r, rule_breaches, equity
FROM {J}.daily_metrics WHERE $__timeFilter(date::timestamptz) ORDER BY date DESC""", w=24, h=8)
    b.add("Closed trades", f"""
SELECT closed_at, ticker, style, setup, "window", qty, avg_entry, avg_exit, pnl,
       round(r_multiple::numeric, 2) AS r, exit_reason, override
{closed} ORDER BY closed_at DESC LIMIT 500""", w=24, h=12)
    return b


def llm() -> Board:
    b = Board("trader-llm", "4. LLM (Qwen)", time_from="now-7d", refresh="30s")
    b.add("Calls", f"SELECT count(*) FROM {J}.llm_calls WHERE $__timeFilter(ts)",
          kind="stat", w=4, h=4, thresholds=[("blue", None)])
    b.add("Valid JSON first try",
          f"SELECT 100.0 * avg(valid::int) AS valid FROM {J}.llm_calls WHERE $__timeFilter(ts)",
          kind="stat", w=4, h=4, unit="percent", decimals=1,
          thresholds=[("red", None), ("green", 95)], desc="Alert below 95%.")
    b.add("Latency p95",
          f"SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95 "
          f"FROM {J}.llm_calls WHERE $__timeFilter(ts) AND NOT cached",
          kind="stat", w=4, h=4, unit="ms",
          thresholds=[("green", None), ("orange", 20000), ("red", 45000)],
          desc="Amber above 20 s, red (entries blocked) above 45 s.")
    b.add("Skip / disagree rate", f"""
SELECT 100.0 * count(*) FILTER (WHERE verdict = 'disagree') / NULLIF(count(*), 0) AS rate
FROM {J}.reviews WHERE $__timeFilter(ts)""",
          kind="stat", w=4, h=4, unit="percent", decimals=0,
          thresholds=[("red", None), ("green", 10), ("red", 90)],
          desc="Below 10% suggests rubber-stamping; above 90% blocks everything.")
    b.add("Tokens per call (avg)",
          f"SELECT avg(input_tokens + output_tokens) AS tokens FROM {J}.llm_calls "
          f"WHERE $__timeFilter(ts)", kind="stat", w=4, h=4, decimals=0,
          thresholds=[("blue", None)])
    b.add("Errors",
          f"SELECT count(*) FROM {J}.llm_calls WHERE $__timeFilter(ts) AND error IS NOT NULL",
          kind="stat", w=4, h=4, thresholds=[("green", None), ("red", 1)])
    b.add("Latency p50 / p95 by agent", f"""
SELECT $__timeGroupAlias(ts, '1h'), agent || ' p50' AS metric,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) AS value
FROM {J}.llm_calls WHERE $__timeFilter(ts) AND NOT cached GROUP BY 1, 2
UNION ALL
SELECT $__timeGroupAlias(ts, '1h'), agent || ' p95',
       percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms)
FROM {J}.llm_calls WHERE $__timeFilter(ts) AND NOT cached GROUP BY 1, 2
ORDER BY 1""", kind="timeseries", fmt="time_series", w=24, h=9, unit="ms")
    b.add("By agent and prompt version", f"""
SELECT agent, prompt_version, model, count(*) AS calls,
       round(100.0 * avg(valid::int), 1) AS valid_pct,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms)::int AS p50_ms,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms)::int AS p95_ms,
       avg(input_tokens)::int AS avg_in, avg(output_tokens)::int AS avg_out
FROM {J}.llm_calls WHERE $__timeFilter(ts) GROUP BY 1, 2, 3 ORDER BY 1, 2""", w=24, h=8)
    b.add("Recent calls", f"""
SELECT ts, agent, prompt_version, valid, latency_ms, input_tokens, output_tokens, cached,
       error, left(output, 300) AS output
FROM {J}.llm_calls WHERE $__timeFilter(ts) ORDER BY ts DESC LIMIT 200""", w=24, h=12)
    return b


def data_quality() -> Board:
    b = Board("trader-data", "5. Data quality", time_from="now-3d", refresh="30s")
    b.add("Latest bar per ticker", f"""
SELECT ticker, max(ts) AS last_bar, extract(epoch FROM now() - max(ts))::int AS age_s,
       count(*) FILTER (WHERE ts >= {TODAY}) AS bars_today
FROM {J}.bars_1m GROUP BY 1 ORDER BY 1""", w=10, h=10,
          desc="During a window the stream must be < 10 s old; > 60 s blocks entries.")
    b.add("Stream gaps (> 1 min between bars, market hours)", f"""
SELECT ticker, count(*) AS gaps, max(gap) AS longest FROM (
  SELECT ticker, ts - lag(ts) OVER (PARTITION BY ticker, (ts AT TIME ZONE 'America/New_York')::date
                                    ORDER BY ts) AS gap
  FROM {J}.bars_1m WHERE $__timeFilter(ts)) g
WHERE gap > interval '1 minute' GROUP BY 1 ORDER BY 2 DESC""", w=7, h=10)
    b.add("Health problems (non-green checks)", f"""
SELECT s.ts, h.key AS check, h.value AS status
FROM {J}.status_snapshots s, json_each_text(s.json->'health') h
WHERE $__timeFilter(s.ts) AND h.value <> 'green' ORDER BY s.ts DESC LIMIT 300""",
          w=7, h=10, mappings=[HEALTH_COLORS])
    b.add("Bars per hour", f"""
SELECT $__timeGroupAlias(ts, '1h'), ticker AS metric, count(*) AS value FROM {J}.bars_1m
WHERE $__timeFilter(ts) GROUP BY 1, 2 ORDER BY 1""",
          kind="timeseries", fmt="time_series", w=12, h=9)
    b.add("News items per hour by source", f"""
SELECT $__timeGroupAlias(received_at, '1h'), source AS metric, count(*) AS value
FROM {J}.news_items WHERE $__timeFilter(received_at) GROUP BY 1, 2 ORDER BY 1""",
          kind="timeseries", fmt="time_series", w=12, h=9)
    b.add("Service heartbeats (last seen)", f"""
SELECT source AS service, max(created_at) AS last_heartbeat,
       extract(epoch FROM now() - max(created_at))::int AS age_s
FROM {J}.outbox WHERE channel = 'heartbeat' GROUP BY 1 ORDER BY 1""", w=8, h=8,
          desc="news-sentiment: red after 5 min; notifier: red after 2 min.")
    b.add("Recent news", f"""
SELECT received_at, ticker, source, round(finbert_score::numeric, 2) AS sentiment, suspicious,
       headline
FROM {J}.news_items WHERE $__timeFilter(received_at) ORDER BY received_at DESC LIMIT 200""",
          w=16, h=8)
    b.add("Incidents", f"""
SELECT ts, type, detail, resolved_at FROM {J}.incidents
WHERE $__timeFilter(ts) ORDER BY ts DESC""", w=24, h=8)
    return b


# Latest after-close ideas run for the latest planned session (A5-1 preview).
IDEAS = (f"{J}.next_day_ideas WHERE run_at = (SELECT max(run_at) FROM {J}.next_day_ideas "
         f"WHERE plan_for = (SELECT max(plan_for) FROM {J}.next_day_ideas))")
STATUS_COLORS = {"type": "value", "options": {
    "pick": {"color": "green", "index": 0, "text": "PICK"},
    "watch": {"color": "blue", "index": 1, "text": "watch"},
    "skip": {"color": "orange", "index": 2, "text": "passed (Qwen)"},
    "off": {"color": "red", "index": 3, "text": "OFF-LIMITS"}}}
OUTCOME_COLORS = {"type": "value", "options": {
    "t1": {"color": "green", "index": 0, "text": "reached T1"},
    "open": {"color": "blue", "index": 1, "text": "triggered, open"},
    "both": {"color": "orange", "index": 2, "text": "T1 and stop"},
    "stopped": {"color": "red", "index": 3, "text": "stopped"},
    "not_triggered": {"color": "text", "index": 4, "text": "not triggered"}}}


def _cell(name: str, cell: dict[str, Any], **extra: Any) -> dict[str, Any]:
    props = [{"id": "custom.cellOptions", "value": cell}]
    props += [{"id": k, "value": v} for k, v in extra.items()]
    return {"matcher": {"id": "byName", "options": name}, "properties": props}


def ideas() -> Board:
    b = Board("trader-ideas", "6. Next-day ideas", time_from="now-30d", refresh="1m")
    b.add("Ideas for", f"SELECT to_char(max(plan_for), 'Dy Mon DD') AS session FROM {IDEAS}",
          kind="stat", w=5, h=4, thresholds=[("blue", None)],
          options={"reduceOptions": {"calcs": ["lastNotNull"], "fields": "/session/",
                                     "values": False}},
          desc="Built after the close (4:15 PM ET) or by `trader plan` / PLAN; the 9:00 and "
               "2:45 plans refresh it with gap and pre-market data.")
    b.add("Picks", f"SELECT count(*) FILTER (WHERE status = 'pick') AS picks FROM {IDEAS}",
          kind="stat", w=3, h=4, thresholds=[("text", None), ("green", 1)])
    b.add("Built", f"SELECT max(run_at) AS built FROM {IDEAS}", kind="stat", w=4, h=4,
          unit="dateTimeFromNow", thresholds=[("blue", None)])
    graded = (f"FROM {J}.next_day_ideas WHERE outcome IS NOT NULL AND outcome <> 'no_pick' "
              f"AND $__timeFilter(run_at)")
    b.add("Ideas triggered", f"SELECT 100.0 * avg(triggered::int) AS pct {graded}",
          kind="stat", w=4, h=4, unit="percent", decimals=0, thresholds=[("blue", None)],
          desc="Picks whose trigger traded on the planned session (daily high).")
    b.add("Reached T1 (of triggered)",
          f"SELECT 100.0 * avg((outcome = 't1')::int) FILTER (WHERE triggered) AS pct {graded}",
          kind="stat", w=4, h=4, unit="percent", decimals=0,
          thresholds=[("red", None), ("orange", 35), ("green", 50)])
    b.add("Stopped (of triggered)",
          f"SELECT 100.0 * avg((outcome = 'stopped')::int) FILTER (WHERE triggered) AS pct "
          f"{graded}", kind="stat", w=4, h=4, unit="percent", decimals=0,
          thresholds=[("green", None), ("orange", 35), ("red", 50)])
    b.add("Next-day ideas (latest run)", f"""
SELECT rank AS "#", ticker, status, score, close, change_pct AS "day %",
       range_pos AS "close in range", volume_ratio AS "vol vs 50d", trend_up AS uptrend,
       atr_to_prior_high AS "ATR to prior high", atr_to_20d_closing_high AS "ATR to 20d close high",
       morning_setup || ' ' || morning_trigger || ' / ' || morning_stop || ' / ' || morning_target
         AS "morning: trigger / stop / T1",
       afternoon_setup || ' ' || afternoon_trigger || ' / ' || afternoon_stop || ' / '
         || afternoon_target AS "afternoon: trigger / stop / T1",
       news_tone AS news, note
FROM {IDEAS} ORDER BY rank""", w=24, h=11,
          desc="score: 30 uptrend + 20 strong close + 15 volume >= avg + 15 pullback to EMA "
               "+ 20 has a candidate setup. ATR distances: negative = already above.",
          overrides=[_cell("status", {"type": "color-background"},
                           mappings=[STATUS_COLORS]),
                     _cell("score", {"type": "gauge", "mode": "basic"}, min=0, max=100,
                           thresholds={"mode": "absolute", "steps": [
                               {"color": "red", "value": None}, {"color": "orange", "value": 50},
                               {"color": "green", "value": 70}]}),
                     _cell("day %", {"type": "color-text"}, decimals=2, thresholds={
                         "mode": "absolute", "steps": [{"color": "red", "value": None},
                                                       {"color": "green", "value": 0}]})])
    b.add("Readiness score", f"SELECT ticker, score FROM {IDEAS} ORDER BY score DESC",
          kind="bargauge", w=10, h=10, min_max=(0, 100),
          thresholds=[("red", None), ("orange", 50), ("green", 70)],
          options={"orientation": "horizontal", "displayMode": "gradient",
                   "reduceOptions": {"calcs": ["lastNotNull"], "values": True,
                                     "fields": "/score/"}})
    b.add("Track record (graded on the planned session)", f"""
SELECT plan_for AS session, ticker,
       coalesce(morning_setup, afternoon_setup) AS setup,
       coalesce(morning_trigger, afternoon_trigger) AS trigger,
       coalesce(morning_stop, afternoon_stop) AS stop,
       coalesce(morning_target, afternoon_target) AS t1,
       day_high, day_low, day_close, outcome
FROM {J}.next_day_ideas
WHERE status = 'pick' AND outcome IS NOT NULL AND $__timeFilter(run_at)
ORDER BY plan_for DESC, rank""", w=14, h=10,
          desc="Daily bars cannot tell whether T1 or the stop came first: 'T1 and stop'.",
          overrides=[_cell("outcome", {"type": "color-background"},
                           mappings=[OUTCOME_COLORS])])
    return b


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for build in (today, pipeline, performance, llm, data_quality, ideas):
        print(build().write().relative_to(OUT.parents[2]))


if __name__ == "__main__":
    main()
