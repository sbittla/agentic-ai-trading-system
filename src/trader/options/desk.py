"""Options desk: A13-A16 wired into trader-core (OPTIONS_REQUIREMENTS §6, §9).

Advisory only: suggestions, follow-ups and grading; this module never places an order.

- OP-10 triggers: A4 setups (bull) and BR-1 bearish setups in a window; alerts-engine
  signals when ``options.from_alerts`` (signal-only, 50% size); the 4:15 PM next-day ideas;
  ``OPT`` on request.
- OP-13 commands: OPT, CHAIN, IV, OPTS, OPTIONS ON|OFF (read-only, SEC-1 allow-list).
- OP-15 quiet rules: one per ticker/direction/window, a per-window cap (A15), follow-ups
  grouped into one message per minute.
- OP-20 prefetch: when a stock setup is pre-armed, the chain snapshot is fetched early.
- OP-8: during market hours the rationale is a code template; after the close Qwen may add
  one or two sentences (no digits allowed, A11-3), else the template stays.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from typing import Any

import structlog
from pydantic import BaseModel, Field

from trader.journal import schema as s
from trader.options import monitor
from trader.options.data import OptionQuote, OptionsData, events_in, expiries, vol_context
from trader.options.risk import OptionsRisk
from trader.options.strategist import (
    OptionSuggestion,
    View,
    format_suggestion,
    suggest,
)
from trader.timeutil import to_et

log = structlog.get_logger(__name__)


class OptionRationaleOut(BaseModel):
    rationale: str = Field(max_length=400)


class OptionsDesk:
    def __init__(self, cfg: Any, engine: Any, data: OptionsData) -> None:
        self.cfg = cfg
        self.ocfg = cfg.options
        self.engine = engine
        self.data = data
        self.book = monitor.OptionsBook(engine.journal)
        self.risk = OptionsRisk(cfg, engine.risk, engine.compliance, engine.macro,
                                self._stock_entries)
        self.enabled = self.ocfg.mode != "off"
        self.pending_followups: list[str] = []
        self.last_flush: datetime | None = None
        self.prefetched: set[tuple[date, str]] = set()
        self.dropped: list[tuple[str, str]] = []      # (ticker, why) for the console/tests

    # -- helpers -------------------------------------------------------------------------------
    def now(self) -> datetime:
        n: datetime = self.engine.now()
        return n

    def _stock_entries(self, ticker: str, window: str) -> set[str]:
        st = self.engine.risk.state
        t = self.cfg.canonical(ticker)
        return {"long"} if (window, t) in st.window_entries else set()

    def spot(self, ticker: str) -> float | None:
        st = self.engine.md.state.get(ticker)
        if st is not None and st.last_price:
            return float(st.last_price)
        lv = self.engine.ta.levels.get(ticker)
        return float(lv.prior_close) if lv is not None else None

    def earnings(self, ticker: str) -> date | None:
        nf = self.engine.ta.news.get(ticker)
        return nf.earnings_date if nf is not None else None

    def macro_days(self, start: date, end: date) -> list[tuple[date, str]]:
        out: list[tuple[date, str]] = []
        m = self.engine.macro
        d = start
        while d <= end:
            if m.is_fomc(d):
                out.append((d, "FOMC"))
            elif m.is_cpi_or_jobs(d):
                out.append((d, "CPI/jobs"))
            d += timedelta(days=1)
        return out

    def events_for(self, ticker: str, today: date) -> Any:
        earn = self.earnings(ticker)

        def fn(expiry: date) -> list[str]:
            return events_in(expiry, today, earn, None, self.macro_days(today, expiry))
        return fn

    def risk_pct(self, planned: bool = True) -> float:
        return float(self.engine.risk.risk_pct(planned))

    def equity(self) -> float:
        return float(self.engine.risk.state.equity or 0)

    # -- OP-10 ---------------------------------------------------------------------------------
    async def on_view(self, view: View) -> list[OptionSuggestion]:
        """Build, gate (A15) and send suggestions for one view."""
        if not self.enabled:
            return []
        now = self.now()
        today = to_et(now).date()
        dte = (0 if self.ocfg.allow_0dte else self.ocfg.day_dte[0], self.ocfg.day_dte[1]) \
            if view.style == "day" else self.ocfg.swing_dte
        try:
            ctx, _ = await self.data.context(view.ticker, view.spot, today)
            quotes = await self.data.quotes(view.ticker, view.spot, today, dte)
        except Exception as exc:   # noqa: BLE001 - no data is a valid "no suggestion"
            log.warning("options_data_failed", ticker=view.ticker, error=str(exc))
            self.dropped.append((view.ticker, f"data: {exc}"))
            return []
        moves = {e: m for e in expiries(quotes)
                 if (m := _em(quotes, view.spot, e)) is not None} | ctx.expected_moves
        sugs, notes = suggest(view, quotes, cfg=self.ocfg, now=now, equity=self.equity(),
                              risk_pct=self.risk_pct(not view.signal_only),
                              iv_rank=ctx.iv_rank, expected_moves=moves,
                              events_for=self.events_for(view.ticker, today),
                              earnings=self.earnings(view.ticker),
                              indicative=self.data.client is None
                              or getattr(self.data.client, "feed", "indicative") != "opra")
        for n in notes:
            self.dropped.append((view.ticker, n))
            log.info("option_dropped", ticker=view.ticker, why=n)
        sent: list[OptionSuggestion] = []
        for sg in sugs:
            ok, why = self.risk.check(sg, now)
            if not ok:
                self.dropped.append((view.ticker, why))
                log.info("option_blocked", ticker=view.ticker, why=why)
                continue
            if view.source == "idea" and to_et(now).time() >= time(16, 0):
                sg.rationale = await self.explain(sg) or sg.rationale
            text = format_suggestion(sg, self.ocfg.mode)
            self.book.add(sg, text)
            self.risk.record(sg, now)
            sent.append(sg)
            if view.source != "idea":
                await self.engine.say(text, "options")
        return sent

    async def on_setup(self, s_: Any, checklist_ok: bool, stock_taken: bool) -> None:
        """A4 long setup -> bullish view (after the stock pipeline decided)."""
        if stock_taken:
            self.dropped.append((s_.ticker, "A15-2 stock entry taken on the same view"))
            return
        await self.on_view(View(s_.ticker, "bull", float(s_.last_price or s_.entry),
                                float(s_.stop), float(s_.t1), float(s_.t2), s_.style,
                                int(s_.score), "setup", s_.setup, False, checklist_ok,
                                window=s_.window))

    async def on_bear(self, sig: Any) -> None:
        """BR-1 bearish setup -> bearish view (options only)."""
        await self.on_view(View(sig.ticker, "bear", float(sig.last_price), sig.invalidation,
                                sig.t1, sig.t2, sig.style, sig.score, "bear_setup", sig.setup,
                                False, True, window=sig.window))

    async def on_alert(self, ticker: str, direction: str, plan: Any, kind: str,
                       passed: int) -> None:
        """OP-10 (2): alerts-engine signal, lower conviction, 50% size."""
        if not self.ocfg.from_alerts or plan is None:
            return
        sp = self.spot(ticker)
        if sp is None:
            return
        await self.on_view(View(ticker, "bull" if direction == "long" else "bear", sp,
                                float(plan.stop), float(plan.t1), float(plan.t2), "day",
                                passed * 10, "alert", kind, True, True))

    async def prefetch(self, ticker: str, style: str = "day") -> None:
        """OP-20: warm the snapshot cache when a stock setup is pre-armed."""
        now = self.now()
        key = (to_et(now).date(), ticker)
        sp = self.spot(ticker)
        if not self.enabled or key in self.prefetched or sp is None:
            return
        self.prefetched.add(key)
        dte = self.ocfg.day_dte if style == "day" else self.ocfg.swing_dte
        try:
            await self.data.quotes(ticker, sp, key[0], dte)
        except Exception as exc:   # noqa: BLE001 - an optimisation only
            log.warning("options_prefetch_failed", ticker=ticker, error=str(exc))

    # -- A16 every minute ------------------------------------------------------------------------
    async def on_minute(self, now: datetime) -> None:
        if not self.book.open:
            return
        by_ticker: dict[str, list[monitor.Tracked]] = {}
        for tr in list(self.book.open.values()):
            by_ticker.setdefault(tr.sug.view.ticker, []).append(tr)
        today = to_et(now).date()
        for t, trs in by_ticker.items():
            sp = self.spot(t)
            if sp is None:
                continue
            quotes: dict[str, OptionQuote] = {}
            try:
                lo = min(tr.sug.expiry for tr in trs)
                hi = max(tr.sug.expiry for tr in trs)
                qs = await self.data.quotes(t, sp, today, ((lo - today).days, (hi - today).days))
                quotes = {q.symbol: q for q in qs}
            except Exception as exc:   # noqa: BLE001 - marks fall back to Black-Scholes
                log.info("options_mark_model", ticker=t, error=str(exc))
            for tr in trs:
                events = self.events_for(t, today)(tr.sug.expiry)
                msgs, reason = self.book.mark(tr, sp, quotes, now, events)
                self.pending_followups += msgs
                if reason is not None:
                    res = self.book.close(tr, reason, now)
                    self.risk.closed(tr.sug, float(res["pnl"]), now)
                    self.pending_followups.append(
                        f"OPT {t}: paper result {float(res['pnl']):+,.0f} "
                        f"({res['r_multiple']:+.2f}R, {reason})")
                else:
                    self.book.snapshot(tr, now)
        await self.flush(now)

    async def flush(self, now: datetime, force: bool = False) -> None:
        """OP-15: follow-ups grouped into one message per minute."""
        if not self.pending_followups:
            return
        if not force and self.last_flush is not None and now - self.last_flush < timedelta(
                seconds=60):
            return
        text, self.pending_followups = "\n".join(self.pending_followups), []
        self.last_flush = now
        await self.engine.say(text, "options")

    # -- jobs -------------------------------------------------------------------------------
    async def job_chain_load(self, now: datetime) -> None:
        """A13-1 (8:30): chains for the universe plus SPY and QQQ."""
        if not self.enabled:
            return
        today = to_et(now).date()
        for t in list(self.cfg.universe.tickers) + ["SPY", "QQQ"]:
            sp = self.spot(t)
            if sp is None:
                continue
            try:
                await self.data.load_chain(t, sp, today)
            except Exception as exc:   # noqa: BLE001
                log.warning("option_chain_failed", ticker=t, error=str(exc))

    async def job_close(self, now: datetime) -> None:
        """16:05: mark at the close (A16-1) and store today's ATM IV (A13-6)."""
        if not self.enabled:
            return
        await self.on_minute(now)
        await self.flush(now, force=True)
        today = to_et(now).date()
        for t in self.cfg.universe.tickers:
            sp = self.spot(t)
            if sp is None:
                continue
            try:
                ctx, _ = await self.data.context(t, sp, today)
                self.data.store_daily(ctx, today)
            except Exception as exc:   # noqa: BLE001
                log.warning("iv_daily_failed", ticker=t, error=str(exc))

    async def next_day_section(self, picks: list[View]) -> str:
        """OP-10 (3) / BR-3: swing structures for the 4:15 PM ideas."""
        if not self.enabled or not picks:
            return ""
        lines: list[str] = []
        for v in picks:
            for sg in await self.on_view(v):
                lines.append(format_suggestion(sg, self.ocfg.mode))
        bears = [v for v in picks if v.direction == "bear"]
        head = "\nOptions ideas (swing structures, advisory):"
        if bears:
            head += "\nBearish (options only): " + ", ".join(
                f"{v.ticker} trigger {v.spot:.2f} invalidation {v.invalidation:.2f} "
                f"T1 {v.t1:.2f}" for v in bears)
        return head + ("\n\n" + "\n\n".join(lines) if lines else "\n(no suggestion passed)")

    async def explain(self, sg: OptionSuggestion) -> str | None:
        """OP-8: after the close only; digits are not allowed (A11-3)."""
        llm = getattr(self.engine, "llm", None)
        if llm is None or not hasattr(llm, "structured"):
            return None
        try:
            from trader.llm.base import load_prompt

            version = self.cfg.llm.prompt_versions.get("a14", "v1")
            v = sg.view
            res = await llm.structured(
                agent="a14", prompt_version=version, system=load_prompt("a14", version),
                data={"ticker": v.ticker, "direction": v.direction, "setup": v.setup,
                      "style": v.style, "structure": sg.structure, "iv_rank": sg.iv_rank,
                      "t1_vs_expected_move": "inside" if sg.expected_move and abs(
                          v.t1 - v.spot) <= sg.expected_move else "beyond"},
                schema=OptionRationaleOut, timeout_s=self.cfg.llm.timeout_sec)
        except Exception as exc:   # noqa: BLE001 - the template stays
            log.info("option_explain_failed", error=str(exc))
            return None
        if not res.ok or not isinstance(res.value, OptionRationaleOut):
            return None
        text = res.value.rationale.strip()
        return None if re.search(r"\d", text) or not text else text

    def report_text(self, since: datetime | None = None) -> str:
        rows = self.engine.journal.rows(s.option_suggestions)
        if since is not None:
            rows = [r for r in rows if r.get("ts") is not None and r["ts"] >= since]
        if not rows:
            return ""
        return monitor.report(rows)

    def promotion_text(self) -> str:
        st = monitor.promotion_status(self.engine.journal.rows(s.option_suggestions))
        if not st:
            return "Options promotion gate (A16-4): no graded suggestions yet"
        return "Options promotion gate (A16-4):\n" + "\n".join(
            f"- {k[0]} {k[1]}: {'MET' if ok else 'not met'} ({why})"
            for k, (ok, why) in sorted(st.items()))

    # -- OP-13 commands --------------------------------------------------------------------------
    async def command(self, action: str, arg: str) -> str:
        parts = arg.upper().split()
        if action == "OPTIONS":
            if parts[:1] == ["ON"]:
                self.enabled = True
            elif parts[:1] == ["OFF"]:
                self.enabled = False
            return f"Options suggestions {'on' if self.enabled else 'off'} " \
                   f"(mode {self.ocfg.mode})"
        if action == "OPTS":
            return self.book.status_text() + "\n" + self.promotion_text()
        if not parts:
            return f"usage: {action} <TICKER>"
        t = self.cfg.canonical(parts[0])
        sp = self.spot(t)
        if sp is None:
            return f"{t}: no price yet"
        today = to_et(self.now()).date()
        try:
            if action == "IV":
                ctx, _ = await self.data.context(t, sp, today)
                return ctx.text()
            if action == "CHAIN":
                return await self.chain_text(t, sp, today, parts[1] if len(parts) > 1 else None)
        except Exception as exc:   # noqa: BLE001
            return f"{action} {t}: options data unavailable ({exc})"
        if action == "OPT":
            return await self.opt_command(t, sp, parts[1:])
        return f"unknown options command {action}"

    async def opt_command(self, t: str, sp: float, opts: list[str]) -> str:
        st = self.engine.md.state.get(t)
        direction = "bear" if "BEAR" in opts else "bull" if "BULL" in opts else (
            "bear" if st is not None and st.above_vwap() is False else "bull")
        style = "swing" if "SWING" in opts else "day"
        lv = self.engine.ta.levels.get(t)
        if style == "day":
            r = float(st.atr5.value) if st is not None and st.atr5.value else sp * 0.005
        else:
            r = float(lv.atr14d) if lv is not None and lv.atr14d else sp * 0.02
        sign = 1 if direction == "bull" else -1
        view = View(t, direction, sp, sp - sign * r, sp + sign * 1.5 * r, sp + sign * 3 * r,
                    style, 0, "owner", "owner view", True, True, "EARNINGS" in opts)
        before = len(self.dropped)
        sent = await self.on_view(view)
        if sent:
            return f"{len(sent)} options idea(s) sent for {t}"
        why = "; ".join(w for _, w in self.dropped[before:]) or "no structure passed"
        return f"No options suggestion for {t} {direction} {style}: {why}"

    async def chain_text(self, t: str, sp: float, today: date, expiry: str | None) -> str:
        qs = await self.data.quotes(t, sp, today, (0, 60), band=0.08)
        exps = expiries(qs)
        if not exps:
            return f"{t}: no chain"
        if expiry:
            try:
                want = date.fromisoformat(expiry)
            except ValueError:
                return "usage: CHAIN <TICKER> [YYYY-MM-DD]"
            exp = min(exps, key=lambda e: abs((e - want).days))
        else:
            exp = next((e for e in exps if (e - today).days >= self.ocfg.day_dte[0]), exps[-1])
        rows = [q for q in qs if q.expiry == exp]
        strikes = sorted({q.strike for q in rows}, key=lambda k: abs(k - sp))[:9]
        lines = [f"CHAIN {t} {exp:%b %d} ({(exp - today).days} DTE) · stock {sp:.2f}",
                 "strike | call bid/ask IV Δ OI | put bid/ask IV Δ OI"]
        for k in sorted(strikes):
            c = next((q for q in rows if q.strike == k and q.kind == "C"), None)
            p = next((q for q in rows if q.strike == k and q.kind == "P"), None)
            lines.append(f"{k:g} | {_cell(c)} | {_cell(p)}")
        ctx = vol_context(t, qs, sp, today, self.data.iv_history(t))
        if ctx.expected_moves.get(exp):
            lines.append(f"expected move ±{ctx.expected_moves[exp]:.2f}")
        lines.append("Prices indicative (free feed).")
        return "\n".join(lines)


def _em(quotes: list[OptionQuote], spot: float, e: date) -> float | None:
    from trader.options.data import expected_move

    return expected_move(quotes, spot, e)


def _cell(q: OptionQuote | None) -> str:
    if q is None:
        return "-"
    iv = f"{q.iv * 100:.0f}%" if q.iv else "-"
    d = f"{q.delta:+.2f}" if q.delta is not None else "-"
    return f"{q.bid:.2f}/{q.ask:.2f} {iv} {d} {q.open_interest}"
