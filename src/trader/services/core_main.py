"""trader-core service: A1, A2, A4-A12 in one process (ARCHITECTURE §1, ADR-010).

Wires the real interfaces (Alpaca, Ollama, Postgres outbox) into the TradingEngine and runs
the loops: market data, order updates, the 1-second clock tick, 30-second health checks,
1-minute reconcile, outbox consumers (news flags, owner commands) and heartbeats.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import desc, select

from trader import config as config_mod
from trader.brokers.alpaca_adapter import AlpacaBroker
from trader.bus import SqlOutbox
from trader.calendar import MacroCalendar, MarketCalendar
from trader.data.alpaca_data import AlpacaHistory, AlpacaIEXFeed
from trader.data.store import BarStore, DayRecorder, resolve_bars_path
from trader.engine import TradingEngine
from trader.journal import schema as s
from trader.journal.store import SqlJournal
from trader.llm.ollama_client import OllamaClient
from trader.models.messages import Command, EventFlags
from trader.notify.commands import command_source
from trader.notify.whatsapp import OutboxNotifier
from trader.services.runtime import require_env, touch_health
from trader.timeutil import et_date, et_datetime, to_et

log = structlog.get_logger(__name__)


def effective_config(loaded: config_mod.LoadedConfig, journal: SqlJournal,
                     now: datetime) -> config_mod.LoadedConfig:
    """SR-5 / SEC-8: config changes take effect the next trading day. If today already has a
    snapshot with a different hash, keep today's snapshot until tomorrow."""
    today = et_date(now)
    snap = journal.config_for_day(today)
    if snap is None:
        journal.snapshot_config(today, now, loaded.sha256, loaded.yaml_text)
        return loaded
    if snap["yaml_hash"] != loaded.sha256 and snap["yaml_text"]:
        log.warning("config_changed_mid_day_using_morning_snapshot", today=str(today))
        journal.incident(now, "CONFIG_CHANGED", "config edited during the day; applies "
                         "next trading day (SR-5)")
        return config_mod.from_text(snap["yaml_text"], loaded.path)
    return loaded


class CoreService:
    def __init__(self, config_path: str | None = None) -> None:
        self.config_path = config_path

    async def run(self) -> None:
        now = datetime.now(UTC)
        loaded = config_mod.load(self.config_path, check_env=True)
        url = os.environ[loaded.config.storage.journal_url_env]
        journal = SqlJournal(url)
        loaded = effective_config(loaded, journal, now)
        cfg = loaded.config
        problems = config_mod.validate(cfg, gates_passed=journal.gates_passed())
        if problems:
            raise config_mod.ConfigError(problems)
        env = cfg.mode.environment
        if env == "live":   # SEC-7: live keys only when environment is live
            keys = require_env("ALPACA_LIVE_KEY", "ALPACA_LIVE_SECRET")
            broker = AlpacaBroker(keys["ALPACA_LIVE_KEY"], keys["ALPACA_LIVE_SECRET"],
                                  paper=False)
            paper_keys = {k: os.environ.get(k, "") for k in ("ALPACA_PAPER_KEY",
                                                              "ALPACA_PAPER_SECRET")}
            paper_broker = AlpacaBroker(paper_keys["ALPACA_PAPER_KEY"],
                                        paper_keys["ALPACA_PAPER_SECRET"], paper=True) \
                if all(paper_keys.values()) else None
            data_keys = keys["ALPACA_LIVE_KEY"], keys["ALPACA_LIVE_SECRET"]
        else:
            keys = require_env("ALPACA_PAPER_KEY", "ALPACA_PAPER_SECRET")
            broker = AlpacaBroker(keys["ALPACA_PAPER_KEY"], keys["ALPACA_PAPER_SECRET"],
                                  paper=True)
            paper_broker = None
            data_keys = keys["ALPACA_PAPER_KEY"], keys["ALPACA_PAPER_SECRET"]
        cal = MarketCalendar()
        base = Path(loaded.path).resolve().parent.parent
        macro = MacroCalendar.load(base / cfg.windows.macro_calendar_file)
        # A10: clock drift vs Alpaca > 2 s refuses start
        server = await broker.clock_time()
        drift = abs((datetime.now(UTC) - server).total_seconds())
        if drift > 2:
            journal.incident(now, "CLOCK_DRIFT", f"{drift:.2f}s vs Alpaca: start refused")
            raise SystemExit(f"clock drift {drift:.2f}s > 2s vs Alpaca (A10)")
        outbox = SqlOutbox(journal, "core")
        notifier = OutboxNotifier(outbox, lambda: datetime.now(UTC))
        llm = OllamaClient(cfg.llm.base_url, cfg.llm.model, journal=journal,
                           temperature=cfg.llm.temperature, seed=cfg.llm.seed,
                           retries=cfg.llm.retries, num_ctx=cfg.llm.max_context_tokens,
                           keep_alive=cfg.llm.keep_alive)
        history = AlpacaHistory(*data_keys, feed=cfg.data.alpaca_feed)
        engine = TradingEngine(cfg, now=lambda: datetime.now(UTC), calendar=cal, macro=macro,
                               broker=broker, journal=journal, llm=llm, notifier=notifier,
                               history=history, paper_broker=paper_broker,
                               require_quotes=True, reports_dir=base / "reports")
        self.engine, self.journal, self.llm, self.broker = engine, journal, llm, broker
        self.outbox, self.notifier = outbox, notifier
        self.cfg = cfg
        self.store = BarStore(resolve_bars_path(cfg.storage.bars_path, base))
        self.recorder = DayRecorder(base / "recordings")
        await self._warm_history(history)
        await engine.start()
        await self._restore_news()
        self.feed = AlpacaIEXFeed(*data_keys, feed=cfg.data.alpaca_feed)
        symbols = list(cfg.universe.tickers) + list(cfg.universe.context_symbols)
        await self.feed.subscribe(symbols)
        rec = engine.sup.health.get("reconcile")
        await engine.say(f"trader-core started ({env}); reconcile "
                         f"{rec.level if rec else '?'}", "alert")
        loops: list[Callable[[], Awaitable[None]]] = [
            self._bars, self._quotes, self._statuses, self._ticks, self._health,
            self._reconcile, self._news_flags, self._commands, self._heartbeat,
            lambda: self._order_updates(broker)]
        if paper_broker is not None:
            loops.append(lambda: self._order_updates(paper_broker))
        await asyncio.gather(*(self._guard(fn) for fn in loops))

    async def _guard(self, fn: Callable[[], Awaitable[None]]) -> None:
        """Restart a loop that crashes; never let one loop kill the service."""
        while True:
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception as exc:   # noqa: BLE001
                log.exception("loop_crashed", loop=getattr(fn, "__name__", "?"), error=str(exc))
                with contextlib.suppress(Exception):
                    self.journal.incident(datetime.now(UTC), "LOOP_CRASH", str(exc)[:500])
                await asyncio.sleep(5)

    async def _warm_history(self, history: AlpacaHistory) -> None:
        """Seed RVOL / ATR5 from the last 20 sessions of IEX minute bars (A2-5)."""
        end = datetime.now(UTC)
        for sym in self.engine.md.symbols:
            try:
                bars = self.store.read("minute", sym)
                if not bars or end - bars[-1].ts > timedelta(days=3):
                    bars = await history.minute_bars(sym, end - timedelta(days=32), end)
                    self.store.write("minute", sym, bars, keep_days=62)
                by_day: dict[Any, list[Any]] = {}
                for b in bars:
                    by_day.setdefault(et_date(b.ts), []).append(b)
                days = sorted(by_day)[-20:]
                self.engine.md.seed_history(sym, [by_day[d] for d in days
                                                  if d != et_date(end)])
            except Exception as exc:   # noqa: BLE001
                log.warning("warm_history_failed", symbol=sym, error=str(exc))

    async def _bars(self) -> None:
        async for bar in self.feed.bars():
            self.recorder.add_bar(bar)
            await self.engine.on_bar(bar)
            # A2: journal the live 1-min bar (DATA_SPEC bars_1m; Grafana "Data" dashboard).
            # After on_bar so a slow or down journal never delays trading.
            try:
                await asyncio.to_thread(self.journal.insert, s.bars_1m, {
                    "ticker": bar.symbol, "ts": bar.ts, "o": bar.o, "h": bar.h, "l": bar.l,
                    "c": bar.c, "v": bar.v, "vwap": bar.vwap, "source": bar.source})
            except Exception as exc:   # noqa: BLE001
                log.warning("bar_journal_failed", symbol=bar.symbol, error=str(exc))

    async def _quotes(self) -> None:
        async for q in self.feed.quotes():
            self.engine.on_quote(q)

    async def _statuses(self) -> None:
        async for st in self.feed.statuses():
            self.recorder.add_status(st)
            self.engine.on_status(st)

    async def _order_updates(self, broker: AlpacaBroker) -> None:
        async for u in broker.order_updates():
            await self.engine.on_order_update(u)

    async def _ticks(self) -> None:
        flushed: Any = None
        while True:
            now = datetime.now(UTC)
            await self.engine.tick(now)
            local = to_et(now)
            if local.hour == 8 and local.minute == 55 and local.second < 2:
                await self.llm.ensure_loaded()   # QW-4
            if local.hour == 16 and local.minute == 5 and flushed != local.date():
                flushed = local.date()
                path = self.recorder.flush()
                log.info("day_recorded", path=str(path))
            await asyncio.sleep(1)

    async def _health(self) -> None:
        """A10-2: every 30 s; red on a required component blocks entries (MONITORING §1)."""
        from trader.agents import a10_supervisor as sv

        sup, eng = self.engine.sup, self.engine
        while True:
            now = datetime.now(UTC)
            changes = []
            t0 = time.monotonic()
            try:
                await self.broker.account()
                lat: float | None = time.monotonic() - t0
            except Exception:   # noqa: BLE001
                lat = None
            changes.append(sup.set_health("alpaca", sv.grade_alpaca_latency_s(
                lat, eng.exec.failures), f"latency {lat}", now))
            last = self.feed.last_message_at()
            age = (now - last).total_seconds() if last else None
            in_window = eng.windows.window_at(now) is not None
            changes.append(sup.set_health("data", sv.grade_data_age_s(age, in_window),
                                          f"last message {age}s ago", now))
            up = await self.llm.healthy()
            p95 = self.llm.p95_ms() / 1000
            changes.append(sup.set_health("ollama", sv.grade_llm(up, p95),
                                          f"up={up} p95={p95:.1f}s", now))
            t0 = time.monotonic()
            ok = await asyncio.to_thread(self.journal.ping)
            ms = (time.monotonic() - t0) * 1000 if ok else None
            changes.append(sup.set_health("postgres", sv.grade_pg_ms(ms), f"{ms} ms", now))
            for svc, amber, red in (("news", 2, self.cfg.services.news_stale_after_min),
                                    ("notifier", 1, self.cfg.services.notifier_stale_after_min)):
                age_min = await asyncio.to_thread(self._heartbeat_age_min, svc)
                changes.append(sup.set_health(svc, sv.grade_heartbeat_min(age_min, amber, red),
                                              f"heartbeat {age_min} min", now))
            eng.ta.news_available = sup.news_ok()
            try:
                server = await self.broker.clock_time()
                drift: float | None = (datetime.now(UTC) - server).total_seconds()
            except Exception:   # noqa: BLE001
                drift = None
            changes.append(sup.set_health("clock", sv.grade_clock_drift_s(drift),
                                          f"drift {drift}s", now))
            for ch in changes:
                if ch is not None and ch.level != "green":
                    await eng.say(f"HEALTH {ch.component} {ch.level.upper()}: {ch.detail}",
                                  "alert")
            await asyncio.sleep(self.cfg.services.heartbeat_sec)

    def _heartbeat_age_min(self, source: str) -> float | None:
        rows = self.journal.query(select(s.outbox.c.created_at).where(
            s.outbox.c.channel == "heartbeat", s.outbox.c.source == source)
            .order_by(desc(s.outbox.c.id)).limit(1))
        if not rows:
            return None
        age: float = (datetime.now(UTC) - rows[0]["created_at"]).total_seconds() / 60
        return age

    async def _reconcile(self) -> None:
        last_reconnects = 0
        while True:
            await asyncio.sleep(60 if self.feed.reconnects == last_reconnects else 1)
            if self.feed.reconnects != last_reconnects:   # DQ-5 back-fill after a reconnect
                last_reconnects = self.feed.reconnects
                await self._backfill()
            problems = await self.engine.reconcile()
            if problems:
                await self.engine.say("RECONCILE MISMATCH (entries blocked): "
                                      + "; ".join(problems), "alert")

    async def _backfill(self) -> None:
        eng = self.engine
        now = datetime.now(UTC)
        for sym, st in eng.md.state.items():
            start = st.last_bar_end or now - timedelta(minutes=30)
            try:
                bars = await AlpacaHistory(*self._data_keys()).minute_bars(sym, start, now)
            except Exception as exc:   # noqa: BLE001
                log.warning("backfill_failed", symbol=sym, error=str(exc))
                continue
            for b5 in eng.md.backfill([b for b in bars if b.end <= now]):
                eng._buffer += eng.ta.on_5m_close(b5, now) if sym in eng.cfg.universe.tickers \
                    else []

    def _data_keys(self) -> tuple[str, str]:
        if self.cfg.mode.environment == "live":
            return os.environ["ALPACA_LIVE_KEY"], os.environ["ALPACA_LIVE_SECRET"]
        return os.environ["ALPACA_PAPER_KEY"], os.environ["ALPACA_PAPER_SECRET"]

    async def _restore_news(self) -> None:
        """News state is in memory: after a restart, reload the latest flags per ticker since
        7:00 of the previous trading day, so a plan run after the restart (the morning plan,
        or the after-close / weekend ideas run) still sees the catalysts (A5-1)."""
        now = datetime.now(UTC)
        since = et_datetime(self.engine.cal.prev_trading_day(et_date(now)),
                            dtime(7, 0)).astimezone(UTC)
        try:
            msgs = await asyncio.to_thread(self.outbox.recent, "news_flags", since)
        except Exception as exc:   # noqa: BLE001
            log.warning("news_restore_failed", error=str(exc))
            return
        latest: dict[str, EventFlags] = {}
        for m in msgs:
            with contextlib.suppress(Exception):
                if m.payload.get("type") != "universe_report":
                    flags = EventFlags.model_validate(m.payload)
                    latest[flags.ticker] = flags
        for flags in latest.values():
            self.engine.on_news(flags)
        log.info("news_restored", tickers=len(latest))

    async def _news_flags(self) -> None:
        async for m in self.outbox.subscribe("news_flags"):
            p = m.payload
            if p.get("type") == "universe_report":
                await self.engine.say("Universe check: top 10 " + ", ".join(p.get("top10", []))
                                      + " · not in top 10: " + ", ".join(
                                          p.get("not_in_top10", [])) + " (list unchanged, UR-4)",
                                      "report_universe")
            else:
                with contextlib.suppress(Exception):
                    flags = EventFlags.model_validate(p)
                    self.engine.on_news(flags)
                    self.recorder.add_news(flags)
            await self.outbox.ack(m.id)

    async def _commands(self) -> None:
        async for m in self.outbox.subscribe("inbound_cmd", poll_s=0.5):
            p = m.payload
            cmd = Command(name=p["name"], arg=p.get("arg", ""), sender=p.get("sender", "?"),
                          received_at=datetime.fromisoformat(p["received_at"]),
                          source=command_source(p.get("source")))
            await self.engine.handle_command(cmd)   # resolves approvals via notifier.resolve
            await self.outbox.ack(m.id)

    async def _heartbeat(self) -> None:
        while True:
            touch_health("core")
            with contextlib.suppress(Exception):
                await self.outbox.publish("heartbeat", {"service": "core"})
                await asyncio.to_thread(self.journal.insert, s.status_snapshots, {
                    "ts": datetime.now(UTC), "json": self.engine.status_dict()})
            await asyncio.sleep(self.cfg.services.heartbeat_sec)


def main(config_path: str | None = None) -> None:
    asyncio.run(CoreService(config_path).run())
