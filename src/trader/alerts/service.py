"""Alerts service: wires the Alpaca feed, the alert engine, SQLite and Telegram.

Lifecycle (keeps CPU, memory and network near zero outside market hours):

- Telegram long polling runs all the time (one idle HTTPS request per 50 s).
- A few minutes before the open the Alpaca WebSocket connects: 1-minute bars for the
  watchlist, trades only for symbols with a price rule. It disconnects after the close and a
  daily summary is sent. The Alpaca clock decides open/close, so holidays and half days are
  handled without loading a market calendar.
- Started mid-session, it back-fills today's bars quietly so VWAP, opening range and
  high/low of day are right before the first alert.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from datetime import time as dtime
from typing import Any

import structlog

from trader.alerts.bot import CommandHandler
from trader.alerts.config import MAX_IEX_SYMBOLS, AlertsConfig
from trader.alerts.engine import Alert, AlertEngine, BarIn
from trader.alerts.feed import (
    AlpacaRest,
    AlpacaStream,
    AuthError,
    last_price_from_snapshot,
    parse_ts,
    prev_close_from_snapshot,
)
from trader.alerts.store import AlertStore
from trader.services.runtime import touch_health
from trader.timeutil import et_datetime, to_et

log = structlog.get_logger(__name__)
SIGNAL_LOG_DAYS = 30


class AlertService:
    def __init__(self, cfg: AlertsConfig, store: AlertStore, rest: AlpacaRest,
                 stream: AlpacaStream, telegram: Any, allowed_chats: set[str],
                 now: Callable[[], datetime] = lambda: datetime.now(UTC),
                 options_line: Any = None) -> None:
        self.cfg = cfg
        self.options_line = options_line          # OP-14 (None when options.enabled is off)
        self.store = store
        self.rest = rest
        self.stream = stream
        self.tg = telegram
        self.allowed = allowed_chats
        self.now = now
        self.engine = AlertEngine(cfg.signals, cfg.bad_print_pct)
        self.commands = CommandHandler(self)
        self.session_close: datetime | None = None
        self.started = time.monotonic()
        self.reload_rules()

    # --- state used by the bot ---------------------------------------------------------
    def watchlist(self) -> list[str]:
        saved = self.store.watchlist()
        return saved if saved is not None else self.cfg.symbols()

    def subscribed(self) -> set[str]:
        return set(self.watchlist()) | self.engine.rule_symbols()

    @property
    def in_session(self) -> bool:
        return self.session_close is not None

    def reload_rules(self) -> None:
        self.engine.set_rules(self.store.rules())

    async def rules_changed(self) -> None:
        self.reload_rules()
        await self._resubscribe()

    async def set_watchlist(self, symbols: list[str]) -> None:
        self.store.set_watchlist(symbols[:MAX_IEX_SYMBOLS])
        await self._resubscribe()

    async def _resubscribe(self) -> None:
        bars = sorted(self.subscribed())[:MAX_IEX_SYMBOLS]
        trades = sorted(self.engine.rule_symbols())
        if self.in_session:
            missing = [s for s in bars if s not in self.engine.state]
            if missing:
                await self._seed(missing)
            for s in list(self.engine.state):
                if s not in bars:
                    self.engine.drop_symbol(s)
        await self.stream.set_symbols(bars, trades)

    async def _seed(self, symbols: list[str]) -> None:
        """Prior close and last price for symbols added during the session."""
        try:
            snaps = await self.rest.snapshots(symbols)
        except Exception as exc:   # noqa: BLE001
            log.warning("alerts_snapshot_failed", error=str(exc))
            snaps = {}
        today = to_et(self.now()).date().isoformat()
        for s in symbols:
            snap = snaps.get(s) or {}
            self.engine.ensure_symbol(s, prev_close_from_snapshot(snap, today) if snap else None,
                                      last_price_from_snapshot(snap) if snap else None)

    async def last_price(self, symbol: str) -> float | None:
        st = self.engine.state.get(symbol)
        if st is not None and st.last is not None and self.in_session:
            return st.last
        with contextlib.suppress(Exception):
            snap = (await self.rest.snapshots([symbol])).get(symbol)
            if snap:
                return last_price_from_snapshot(snap)
        return None

    async def quote(self, symbols: list[str]) -> str:
        if not symbols:
            return "Watchlist is empty: /watch NVDA"
        symbols = symbols[:MAX_IEX_SYMBOLS]
        # a fresh snapshot is one REST call for all symbols and always current
        try:
            snaps = await self.rest.snapshots(symbols)
        except Exception as exc:   # noqa: BLE001
            log.warning("alerts_snapshot_failed", error=str(exc))
            snaps = {}
        today = to_et(self.now()).date().isoformat()
        lines = []
        for s in symbols:
            snap = snaps.get(s)
            st = self.engine.state.get(s)
            if self.in_session and st is not None and snap:
                last = last_price_from_snapshot(snap)
                if last is not None:
                    st.last = last
                lines.append(self.engine.quote_line(s))
            elif snap:
                last = last_price_from_snapshot(snap)
                prev = prev_close_from_snapshot(snap, today)
                chg = f" {(last / prev - 1) * 100:+.2f}%" if last and prev else ""
                lines.append(f"{s} {last:,.2f}{chg}" if last else f"{s}: no data")
            else:
                lines.append(self.engine.quote_line(s) if st else f"{s}: no data")
        return "\n".join(lines)

    def movers_text(self) -> str:
        movers = [(s, c) for s, c in self.engine.movers() if s in set(self.watchlist())]
        if not movers:
            return "No session data yet (movers are available while the market is open)"
        return "Movers:\n" + "\n".join(f"{c:+6.2f}%  {s}" for s, c in movers)

    def muted(self) -> bool:
        until = self.store.get("mute_until") or ""
        return bool(until) and datetime.fromisoformat(until) > self.now()

    def status_text(self) -> str:
        now = self.now()
        since = et_datetime(to_et(now).date(), dtime(0, 0))
        n_today = len(self.store.alerts_since(since))
        if self.stream.connected and self.stream.last_message is not None:
            feed = f"connected, last data {time.monotonic() - self.stream.last_message:.0f}s ago"
        elif self.in_session:
            feed = "reconnecting"
        else:
            feed = "idle (market closed)"
        up = timedelta(seconds=int(time.monotonic() - self.started))
        return (f"📟 alerts up {up}\nfeed: {feed}\nwatching {len(self.watchlist())} · "
                f"rules {len(self.store.rules())} · alerts today {n_today}\n"
                f"signals {'off' if self.store.get('signals') == 'off' else 'on'} · "
                f"{'🔕 muted' if self.muted() else '🔔 not muted'}")

    # --- market data -------------------------------------------------------------------
    async def on_messages(self, msgs: list[dict[str, Any]]) -> None:
        """One WebSocket frame. Trades only update prices and price rules; bars drive the
        scanner. Alerts from one frame go out as one Telegram message."""
        alerts: list[Alert] = []
        now: datetime | None = None
        for m in msgs:
            t = m.get("T")
            if t == "t":
                now = now or self.now()   # one clock read per frame, not per trade
                alerts += self.engine.on_trade(m["S"], float(m["p"]), now)
            elif t == "b":
                alerts += self.engine.on_bar(_bar(m))
        if alerts:
            await self.dispatch(alerts)

    async def dispatch(self, alerts: list[Alert]) -> None:
        signals_on = self.store.get("signals") != "off"
        watch = set(self.watchlist())
        out = []
        for a in alerts:
            if a.consumed and a.rule_id is not None:
                self.store.delete_rule(a.rule_id)
            if a.rule_id is None and (not signals_on or a.symbol not in watch):
                continue
            text = a.text
            if a.rule_id is None and self.options_line is not None:
                line = await self.options_line.line(a.kind, a.symbol, a.price or 0.0,
                                                    self.engine.state.get(a.symbol))
                if line:
                    text += "\n" + line
            self.store.log_alert(a.ts, a.symbol, a.kind, a.price, text)
            out.append(text)
        if any(a.consumed for a in alerts):
            await self._resubscribe()   # drop trade streams no rule needs any more
        if out and not self.muted():
            await self.tg.send("\n\n".join(out), kind="alert")

    async def warm_up(self, session_open: datetime) -> None:
        """Mid-session start: replay today's bars quietly so the session state is right."""
        now = self.now()
        if now - session_open < timedelta(minutes=2):
            return
        try:
            data = await self.rest.minute_bars(self.watchlist(), session_open, now)
        except Exception as exc:   # noqa: BLE001
            log.warning("alerts_warmup_failed", error=str(exc))
            return
        rows = sorted((_bar({**b, "S": sym}) for sym, bars in data.items() for b in bars),
                      key=lambda b: b.ts)
        for b in rows:
            self.engine.on_bar(b, quiet=True)
        log.info("alerts_warmed_up", bars=len(rows))

    async def run_session(self, session_open: datetime, session_close: datetime) -> None:
        syms = sorted(self.subscribed())[:MAX_IEX_SYMBOLS]
        today = to_et(session_open).date().isoformat()
        try:
            snaps = await self.rest.snapshots(syms)
        except Exception as exc:   # noqa: BLE001
            log.warning("alerts_snapshot_failed", error=str(exc))
            snaps = {}
        self.engine.start_session(session_open, {
            s: prev_close_from_snapshot(snaps[s], today) if s in snaps else None for s in syms})
        for s, snap in snaps.items():
            self.engine.state[s].last = last_price_from_snapshot(snap)
        self.session_close = session_close
        await self.warm_up(session_open)
        await self.stream.set_symbols(syms, sorted(self.engine.rule_symbols()))
        end = session_close + timedelta(minutes=self.cfg.disconnect_after_close_min)
        log.info("alerts_session_start", symbols=len(syms), close=session_close.isoformat())
        task = asyncio.create_task(self.stream.run(self.on_messages))
        try:
            remaining = (end - self.now()).total_seconds()
            done, _ = await asyncio.wait({task}, timeout=max(0.0, remaining))
            if task in done:
                task.result()   # AuthError surfaces here
        finally:
            self.stream.stop()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(task, 5)
            self.session_close = None
        if self.cfg.daily_summary:
            await self.tg.send(self.summary_text(session_open), kind="report")
        self.store.prune_log(self.now() - timedelta(days=SIGNAL_LOG_DAYS))

    def summary_text(self, session_open: datetime) -> str:
        n = len(self.store.alerts_since(session_open - timedelta(hours=6)))
        movers = [(s, c) for s, c in self.engine.movers() if s in set(self.watchlist())]
        lines = [f"🏁 Close {to_et(session_open):%a %b %d}: {n} alert(s) today"]
        if movers:
            top = movers[:3]
            bottom = [m for m in movers[-3:] if m not in top]
            lines.append("Top: " + ", ".join(f"{s} {c:+.2f}%" for s, c in top))
            if bottom:
                lines.append("Bottom: " + ", ".join(f"{s} {c:+.2f}%" for s, c in bottom))
        rules = self.store.rules()
        if rules:
            lines.append(f"{len(rules)} price rule(s) stay active for tomorrow (/list)")
        return "\n".join(lines)

    async def market_loop(self) -> None:
        lead = timedelta(minutes=self.cfg.connect_before_open_min)
        while True:
            try:
                clock = await self.rest.clock()
            except AuthError as exc:
                await self.tg.send(f"⚠️ Alpaca rejected the API keys ({exc}). Fix "
                                   "ALPACA_KEY/ALPACA_SECRET and restart.", kind="alert")
                await asyncio.sleep(3600)
                continue
            except Exception as exc:   # noqa: BLE001
                log.warning("alerts_clock_failed", error=str(exc))
                await asyncio.sleep(60)
                continue
            now = self.now()
            next_open = parse_ts(clock["next_open"])
            next_close = parse_ts(clock["next_close"])
            if clock.get("is_open") or next_open - now <= lead:
                if clock.get("is_open"):
                    session_open = et_datetime(to_et(now).date(), dtime(9, 30))
                else:
                    session_open = next_open
                try:
                    await self.run_session(session_open, next_close)
                except AuthError as exc:
                    await self.tg.send(f"⚠️ Alpaca stream refused: {exc}", kind="alert")
                    await asyncio.sleep(600)
                continue
            # closed: sleep until shortly before the next open (re-check at least hourly)
            wait = (next_open - lead - now).total_seconds()
            log.info("alerts_market_closed", next_open=next_open.isoformat())
            await asyncio.sleep(min(max(wait, 5.0), 3600.0))

    # --- telegram ----------------------------------------------------------------------
    async def telegram_loop(self) -> None:
        with contextlib.suppress(Exception):
            await self.tg.prime()   # never act on commands sent while we were down
        while True:
            try:
                msgs = await self.tg.poll(wait_s=50)
            except Exception as exc:   # noqa: BLE001
                log.warning("alerts_poll_failed", error=str(exc))
                await asyncio.sleep(5)
                continue
            for sender, text, _ts in msgs:
                await self.handle_message(sender, text)

    async def handle_message(self, sender: str, text: str) -> None:
        chat = sender.split(":", 1)[-1]
        if chat not in self.allowed:
            log.warning("alerts_sender_rejected", sender=sender)
            return
        try:
            reply = await self.commands.handle(text)
        except Exception as exc:   # noqa: BLE001
            log.exception("alerts_command_failed", error=str(exc))
            reply = "⚠️ command failed; see the service log"
        await self.tg.send(reply, kind="reply", chat_id=chat)

    async def heartbeat(self) -> None:
        while True:
            touch_health("alerts")
            await asyncio.sleep(30)

    async def run(self) -> None:
        await self.tg.send(f"📟 Alerts online: watching {len(self.watchlist())}, "
                           f"{len(self.store.rules())} price rule(s). /help", kind="info")
        await asyncio.gather(self.telegram_loop(), self.market_loop(), self.heartbeat())


def _bar(m: dict[str, Any]) -> BarIn:
    return BarIn(symbol=m["S"], ts=parse_ts(m["t"]), o=float(m["o"]), h=float(m["h"]),
                 l=float(m["l"]), c=float(m["c"]), v=float(m["v"]))


def _env(*names: str) -> str:
    for n in names:
        if os.environ.get(n):
            return os.environ[n]
    raise SystemExit(f"missing environment variable: one of {', '.join(names)}")


def main(config_path: str | None = None) -> None:
    from trader.alerts import config as alerts_config
    from trader.notify.telegram import TelegramNotifier

    cfg = alerts_config.load(config_path)
    key = _env("ALPACA_KEY", "ALPACA_PAPER_KEY", "ALPACA_LIVE_KEY")
    secret = _env("ALPACA_SECRET", "ALPACA_PAPER_SECRET", "ALPACA_LIVE_SECRET")
    token = _env("TELEGRAM_BOT_TOKEN")
    chat = _env("TELEGRAM_CHAT_ID")
    allowed = {c.strip() for c in (os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS") or chat)
               .split(",") if c.strip()}
    line = None
    if cfg.options.enabled:   # OP-14
        from trader.alerts.options_line import OptionsLine
        from trader.options.data import AlpacaOptionsClient

        line = OptionsLine(cfg.options, AlpacaOptionsClient(key, secret, paper=True),
                           lambda: datetime.now(UTC))
    svc = AlertService(cfg, AlertStore(cfg.db_path), AlpacaRest(key, secret, cfg.feed),
                       AlpacaStream(key, secret, cfg.feed), TelegramNotifier(token, chat),
                       allowed, options_line=line)
    asyncio.run(svc.run())
