"""Momentum agent A19: leaderboard, armed buy-stops, the live signal tracker and its alerts
(MO-5, MO-6, MO-14..MO-18, MO-20). Advisory: nothing is ordered (ADR-006)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import select

from trader.calendar import MarketCalendar
from trader.config import Config
from trader.journal import schema as s
from trader.momentum import rotation, sma200
from trader.momentum.evaluate import build_panel, evaluate
from trader.momentum.exits import ExitParams, Position, enter, step
from trader.momentum.factors import MomFrame, Panel, crowded, regime
from trader.momentum.setups import SETUPS, setups_at
from trader.scanner.techniques import Series
from trader.timeutil import et_date, et_datetime, to_et

log = structlog.get_logger(__name__)
SeriesLoader = Callable[[Sequence[str], date], Awaitable[dict[str, Series]]]
Sizer = Callable[[float, float], tuple[int, float, str]]
LIVE_SESSIONS = 700          # bars kept for the live job (SMA200 + 252 + margin)
SETUP_BY_NAME = {st.name: st for st in SETUPS}


def _write(path: str, payload: Any) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")
    tmp.replace(p)


def load_json(path: str) -> dict[str, Any] | None:
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


# --- data (MO-22) --------------------------------------------------------------------------

def store_loader(root: Path, kind: str = "daily_sip") -> SeriesLoader:
    async def load(symbols: Sequence[str], until: date) -> dict[str, Series]:
        from trader.scanner.data import load_daily

        got = await asyncio.to_thread(load_daily, root, list(symbols), kind)
        out = {}
        for k, v in got.items():
            n = sum(1 for d in v.dates if d <= until)
            if n:
                out[k] = v.upto(n)
        return out
    return load


def refresh_loader(root: Path, hist: Any, now: Callable[[], datetime], kind: str = "daily_sip",
                   new_days: int = 420) -> SeriesLoader:
    """The store, topped up from Alpaca for symbols missing the newest session; the fetched
    bars are written back so the store stays current. SIP requests end 16 minutes ago."""
    from trader.data.store import BarStore

    base = store_loader(root, kind)
    store = BarStore(root)

    async def load(symbols: Sequence[str], until: date) -> dict[str, Series]:
        got = await base(symbols, until)
        due = until
        while due.weekday() >= 5:
            due -= timedelta(days=1)
        stale = [t for t in symbols if t not in got or got[t].dates[-1] < due]
        end = min(now() - timedelta(minutes=16), et_datetime(until, dtime(23, 0)))
        for t in stale:
            start = (got[t].dates[-1] - timedelta(days=7)) if t in got else \
                until - timedelta(days=new_days)
            try:
                bars = await hist.daily_bars_until(t, start, end)
            except Exception as exc:   # noqa: BLE001
                log.warning("momentum_history_failed", ticker=t, error=str(exc))
                continue
            if bars:
                await asyncio.to_thread(store.write, kind, t, bars)
        if stale:
            got.update(await base(stale, until))
        return got
    return load


# --- helpers -------------------------------------------------------------------------------

def exit_params(cfg: Config, policy: str) -> ExitParams:
    m = cfg.momentum
    return ExitParams(policy=policy, partial_frac=m.partial_frac, partial_r=m.partial_r,
                      partial_days=m.partial_days, climax_atr=m.climax_atr,
                      climax_frac=m.climax_frac, time_stop_days=m.time_stop_days,
                      max_hold_days=m.max_hold_days, max_loss_pct=m.max_loss_pct / 100,
                      cost_bps=m.cost_bps)


def policy_for(cfg: Config, setup: str) -> str:
    return cfg.momentum.setup_policy.get(setup, SETUP_BY_NAME[setup].policy)


def setup_record(ev: Mapping[str, Any] | None, setup: str) -> dict[str, Any]:
    """The measured record of a setup's default policy, and whether it passed MO-14."""
    if not ev:
        return {"proven": False, "why": "not graded yet: run momentum-eval"}
    res = (ev.get("setups") or {}).get(setup) or {}
    pol = res.get("default_policy")
    st = ((res.get("policies") or {}).get(pol) or {}).get("all") or {}
    g = res.get("gate") or {}
    failed = [k for k, ok in (g.get("checks") or {}).items() if not ok]
    return {"proven": bool(g.get("pass")), "policy": pol, "trades": st.get("trades", 0),
            "win_rate": st.get("win_rate"), "avg_r": st.get("avg_r"),
            "profit_factor": st.get("profit_factor"),
            "why": "passed the walk-forward gate" if g.get("pass") else
            ("failed: " + ", ".join(failed) if failed else "not graded")}


class MomentumAgent:
    def __init__(self, cfg: Config, journal: Any, load: SeriesLoader,
                 now: Callable[[], datetime], *, cal: MarketCalendar | None = None,
                 sizer: Sizer | None = None, equity: float | None = None,
                 macro_label: str | None = None) -> None:
        self.cfg = cfg
        self.m = cfg.momentum
        self.journal = journal
        self.load = load
        self.now = now
        self.cal = cal or MarketCalendar()
        self.sizer = sizer
        self.equity = equity
        self.macro_label = macro_label

    def symbols(self) -> list[str]:
        return list(dict.fromkeys([*self.m.universe, *self.cfg.universe.tickers]))

    async def series(self, until: date, full: bool = False
                     ) -> tuple[dict[str, Series], Series | None]:
        got = await self.load([*self.symbols(), "SPY"], until)
        spy = got.pop("SPY", None)
        if not full:
            got = {k: v.upto(len(v.c)) if len(v.c) <= LIVE_SESSIONS else
                   _tail(v, LIVE_SESSIONS) for k, v in got.items()}
            spy = _tail(spy, LIVE_SESSIONS) if spy is not None and len(spy.c) > \
                LIVE_SESSIONS else spy
        return got, spy

    def earnings_in(self, tickers: Sequence[str], asof: date) -> dict[str, int]:
        """Sessions to the next earnings date from the stored Finnhub calendar (MO-18)."""
        if self.journal is None:
            return {}
        f = s.fundamentals_daily.c
        try:
            rows = self.journal.query(select(f.ticker, f.date, f.json).where(
                f.kind == "earnings_calendar", f.ticker.in_(list(tickers)),
                f.date >= asof - timedelta(days=10), f.date <= asof).order_by(f.date))
        except Exception:   # noqa: BLE001 - no table yet: earnings unknown
            return {}
        out: dict[str, int] = {}
        for r in rows:
            d = (r["json"] or {}).get("date")
            if d:
                nd = date.fromisoformat(str(d))
                if nd > asof:
                    out[r["ticker"]] = self.cal.trading_days_between(asof, nd)
        return out

    def earnings_status(self, asof: date) -> str:
        """MO-18 fails closed: ``fresh`` when the Finnhub calendar was collected within
        ``earnings_max_age_days``, ``stale`` when it is missing or older (entries are then held
        back), ``unchecked`` without a journal (a host CLI run: the report says so)."""
        if self.journal is None:
            return "unchecked"
        f = s.fundamentals_daily.c
        try:
            rows = self.journal.query(select(f.date).where(
                f.kind == "earnings_calendar", f.date <= asof,
                f.date >= asof - timedelta(days=self.m.earnings_max_age_days)).limit(1))
        except Exception:   # noqa: BLE001 - no table: unknown, so fail closed
            return "stale"
        return "fresh" if rows else "stale"

    def final_session(self) -> date:
        """The newest session whose daily bar is final: today after 16:15 ET, else the one
        before (a mid-session run must not treat a partial bar as a closed session)."""
        local = to_et(self.now())
        d = local.date()
        if self.cal.is_trading_day(d) and local.time() >= dtime(16, 15):
            return d
        return self.cal.prev_trading_day(d)

    # -- the close job -------------------------------------------------------------------
    async def run(self, asof: date | None = None, *, write: bool = True, track: bool = True,
                  expect: date | None = None) -> dict[str, Any]:
        """The close job. ``expect``: the session that must be in the bars (else
        ``{"pending": True}``, so trader-core retries instead of tracking a stale day)."""
        until = asof or self.final_session()
        series, spy = await self.series(until)
        if spy is None or not series:
            return {"error": "no bars: run momentum-history"}
        day = asof or max(v.dates[-1] for v in series.values())
        if expect is not None and day < expect:
            return {"pending": True, "error": f"no bar for {expect} yet (last {day})"}
        skipped = {k: f"no bar for {day} (last {v.dates[-1]})" for k, v in series.items()
                   if v.dates[-1] != day}
        skipped.update({k: f"only {len(v.c)} sessions" for k, v in series.items()
                        if len(v.c) < self.m.min_history and k not in skipped})
        live = {k: v for k, v in series.items() if k not in skipped}
        ev = load_json(self.m.eval_file)
        factors, _ = self.rank_choice(ev)
        panel = await asyncio.to_thread(build_panel, live, spy, min_price=self.m.min_price,
                                        min_dollar=self.m.min_dollar_volume,
                                        rank_factors=factors)
        reg = regime(spy, day, self.macro_label)
        board = panel.leaderboard(day, self.m.leaderboard_n)
        earn = self.earnings_in(list(live), day)
        earn_status = self.earnings_status(day)
        cands = self._candidates(panel, day, reg.state, reg.size_mult, ev, earn, earn_status)
        tracked: dict[str, Any] = {"events": [], "open": [], "closed": [], "armed": []}
        if track and self.journal is not None:
            tracked = self._track(panel, day, cands)
        else:
            self.select(panel, day, cands, [])
        rot = None
        if self.m.rotation:
            rot = await asyncio.to_thread(self._rotation, panel, spy, day, ev)
        watch = sma200.watch(panel, day, max_dist=self.m.sma200_watch_pct / 100,
                             always=tuple(self.cfg.universe.tickers))   # MO-27
        payload = {
            "generated": self.now().isoformat(), "asof": day.isoformat(),
            "regime": reg.as_dict(), "leaderboard": [r.as_dict() for r in board],
            "earnings_status": earn_status,
            "rank_factors": list(factors),
            "candidates": cands, "tracked": tracked, "skipped": skipped, "rotation": rot,
            "sma200_watch": watch,
            "eval": {"generated": (ev or {}).get("generated"),
                     "gate_pass": (ev or {}).get("gate_pass", []),
                     "period": (ev or {}).get("period")},
            "records": {st.name: setup_record(ev, st.name) for st in SETUPS},
        }
        if write:
            _write(self.m.latest_file, payload)
        return payload

    def _candidates(self, panel: Panel, day: date, state: str, size_mult: float,
                    ev: Mapping[str, Any] | None, earn: Mapping[str, int],
                    earn_status: str = "fresh") -> list[dict[str, Any]]:
        rs = panel.rs(day)
        out = []
        for sym, x in sorted(panel.frames.items()):
            i = x.pos.get(day)
            if i is None or not x.liquid(i, panel.min_price, panel.min_dollar):
                continue
            score, failed = x.template(i, rs.get(sym))
            leader = rs.get(sym, 0) >= self.m.min_rs and score >= self.m.min_template
            for st, cand in setups_at(x, i, leader, frozenset(self.m.disabled)):
                rec = setup_record(ev, st.name)
                pol = policy_for(self.cfg, st.name)
                r = cand.trigger - cand.stop
                stop = max(cand.stop, cand.trigger * (1 - self.m.max_loss_pct / 100))
                verdict, why = "alert", rec["why"]
                if state == "OFF":
                    verdict, why = "regime_off", "SPY under its 200-day average: no new entries"
                elif not rec["proven"]:
                    verdict = "unproven"
                elif st.name in self.m.watch_only:
                    verdict, why = "watch_only", (
                        "graded and tracked, not alerted: it lowered the alert stream (MO-26)")
                n_earn = earn.get(sym)
                if n_earn is not None and n_earn <= self.m.earnings_block_sessions and \
                        st.name != "EPISODIC_PIVOT" and verdict == "alert":
                    verdict, why = "earnings", f"earnings in {n_earn} sessions (CK-1)"
                elif earn_status == "stale" and st.name != "EPISODIC_PIVOT" and \
                        verdict == "alert":
                    verdict, why = "earnings_unknown", (
                        "no fresh earnings calendar: held back until it is (MO-18)")
                shares = risk_usd = None
                if self.sizer is not None:
                    q, ru, _ = self.sizer(cand.trigger, stop)
                    shares, risk_usd = int(q * size_mult), round(ru * size_mult, 2)
                session = self.cal.next_trading_day(day)
                out.append({
                    "ticker": sym, "setup": st.name, "policy": pol, "asof": day.isoformat(),
                    "session": session.isoformat(), "entry": cand.entry,
                    "trigger": round(cand.trigger, 2), "stop": round(stop, 2),
                    "risk_pct": round((cand.trigger - stop) / cand.trigger * 100, 2),
                    "r": round(r, 2), "partial_at": round(cand.trigger + self.m.partial_r *
                                                          (cand.trigger - stop), 2),
                    "time_stop": self.cal.add_trading_days(session, self.m.time_stop_days
                                                           ).isoformat(),
                    "rs": rs.get(sym, 0), "template": score, "failed": failed,
                    "stage": x.stage(i), "adr": round(float(x.adr[i]), 2),
                    "earnings_in": n_earn, "shares": shares, "risk_usd": risk_usd,
                    "size_mult": size_mult, "why": cand.why, "verdict": verdict,
                    "partial_days": self.m.partial_days,
                    "verdict_why": why, "record": rec})
        out.sort(key=lambda c: (c["verdict"] != "alert", -c["rs"], c["ticker"]))
        return out

    def rank_choice(self, ev: Mapping[str, Any] | None) -> tuple[tuple[str, ...], bool]:
        """Factors and regime filter: the config's, else the eval's in-sample choice
        (MO-13), else ``rank_factors`` with the filter on."""
        sel = ((ev or {}).get("rotation") or {}).get("selected") or {}
        factors = tuple(self.m.rotation_factors) or tuple(sel.get("factors") or
                                                          self.m.rank_factors)
        rf = self.m.rotation_regime_filter
        if rf is None:
            rf = bool(sel.get("regime_filter", True))
        return factors, rf

    # -- rotation (MO-25) -----------------------------------------------------------------
    def _rotation(self, panel: Panel, spy: Series, day: date, ev: Mapping[str, Any] | None
                  ) -> dict[str, Any]:
        """The model portfolio, replayed over the loaded sessions with the backtest's code;
        on a rebalance day, the buys and sells against the previous rebalance."""
        m = self.m
        rot_ev = (ev or {}).get("rotation") or {}
        sel = rot_ev.get("selected") or {}
        factors, rf = self.rank_choice(ev)
        dates = [d for d in spy.dates if d <= day and len(panel.eligible(d)) >= 10]
        if not dates:
            return {"error": "not enough history for the rotation model"}
        nxt = self.cal.next_trading_day(day)
        r = rotation.run(panel, spy, dates, factors=factors, top_n=m.top_n,
                         hold_rank=m.hold_rank, weeks=m.rebalance_weeks, cost_bps=m.cost_bps,
                         regime_filter=rf, last_next=nxt, max_weight=m.rotation_max_weight)
        hist = r.rebalances
        cur = hist[-1][1] if hist else {}
        due = bool(hist) and hist[-1][0] == day
        prev = hist[-2][1] if len(hist) >= 2 else {}
        base = prev if due else cur
        target = cur
        nd, k = day, 0
        while k < 30:
            k += 1
            nd = self.cal.next_trading_day(nd)
            if rotation.rebalance_due(nd, self.cal.next_trading_day(nd), m.rebalance_weeks):
                break
        rows: list[dict[str, Any]] = []
        for sym in sorted(set(base) | set(target), key=lambda s_: -target.get(s_, 0.0)):
            x = panel.frames[sym]
            px = float(x.c[x.pos[day]]) if day in x.pos else float(x.c[-1])
            w = target.get(sym, 0.0)
            act = "HOLD" if sym in base and sym in target else (
                "BUY" if sym in target else "SELL")
            shares = int(w * self.equity // px) if self.equity and w else None
            rows.append({"ticker": sym, "action": act if due else "HOLD" if w else "SELL",
                         "weight": round(w, 4), "prev_weight": round(base.get(sym, 0.0), 4),
                         "close": round(px, 2), "shares": shares})
        if not due:
            rows = [x for x in rows if x["weight"] > 0]
        return {"factors": list(factors), "regime_filter": rf, "due": due,
                "next_rebalance": nd.isoformat(), "last_rebalance":
                hist[-1][0].isoformat() if hist else None, "holdings": rows,
                "cash_weight": round(max(0.0, 1 - sum(target.values())), 4),
                "proven": bool((rot_ev.get("gate") or {}).get("pass")),
                "selected": sel.get("name"), "record": {
                    k: ((rot_ev.get("runs") or {}).get(sel.get("name") or "") or {}).get(k)
                    for k in ("is", "oos")}}

    # -- tracker (MO-15) ------------------------------------------------------------------
    def _rows(self, states: Sequence[str]) -> list[dict[str, Any]]:
        t = s.momentum_signals.c
        rows: list[dict[str, Any]] = self.journal.query(
            select(s.momentum_signals).where(t.state.in_(list(states))).order_by(t.asof))
        return rows

    def select(self, panel: Panel, day: date, cands: list[dict[str, Any]],
               active: Sequence[Mapping[str, Any]]) -> list[tuple[dict[str, Any], bool]]:
        """MO-17: which candidates are alerted. One signal per ticker; slots, the heat cap and
        the correlation cap count the alerted signals still armed or open. Sets each
        candidate's verdict; returns (candidate, alerted) for the ones to arm."""
        busy = {r["ticker"] for r in active}
        alerted_open = [r for r in active if r["alerted"]]
        heat = sum(float(r["risk_usd"] or 0) for r in alerted_open
                   if r["state"] == "ARMED" or not (r["position"] or {}).get("partial_done"))
        slots = len(alerted_open)
        held = [str(r["ticker"]) for r in alerted_open]
        out = []
        for c in cands:
            if c["verdict"] == "regime_off":
                continue
            if c["ticker"] in busy:
                c["verdict"], c["verdict_why"] = "busy", "a signal on this ticker is open"
                continue
            send = c["verdict"] == "alert" or (self.m.alert_unproven and
                                               c["verdict"] == "unproven")
            if send and slots >= self.m.max_positions:
                c["verdict"], c["verdict_why"], send = "no_slot", (
                    f"{self.m.max_positions} alerted signals open (MO-17)"), False
            elif send and (near := crowded(panel.frames, c["ticker"], held, day,
                                           self.m.max_correlated)) is not None:
                c["verdict"], c["verdict_why"], send = "correlated", (
                    f"moves with {near} (60-day correlation > 0.7, MO-17)"), False
            elif send and self.equity and c["risk_usd"] is not None and \
                    heat + c["risk_usd"] > self.equity * self.m.max_heat_pct / 100:
                c["verdict"], c["verdict_why"], send = "no_slot", (
                    f"open risk would pass {self.m.max_heat_pct}% of equity (MO-17)"), False
            busy.add(c["ticker"])
            if send:
                slots += 1
                heat += c["risk_usd"] or 0.0
                held.append(c["ticker"])
            out.append((c, send))
        return out

    def _track(self, panel: Panel, day: date, cands: list[dict[str, Any]]) -> dict[str, Any]:
        events: list[dict[str, Any]] = []
        for row in self._rows(("ARMED", "OPEN")):
            x = panel.frames.get(row["ticker"])
            if x is None:
                continue
            events += self._advance(row, x, day)
        for c, send in self.select(panel, day, cands, self._rows(("ARMED", "OPEN"))):
            sid = f"{c['ticker']}:{c['setup']}:{c['asof']}"
            self.journal.insert(s.momentum_signals, {
                "signal_id": sid, "ticker": c["ticker"], "setup": c["setup"],
                "policy": c["policy"], "asof": day, "state": "ARMED",
                "entry_type": c["entry"], "trigger": c["trigger"], "stop": c["stop"],
                "rs": c["rs"], "template": c["template"], "proven": c["record"]["proven"],
                "alerted": send, "shares": c["shares"], "risk_usd": c["risk_usd"],
                "sent": [], "events": [], "why": c["why"], "updated": self.now()})
        out = {"events": events, "armed": [], "open": [], "closed": []}
        for r in self._rows(("ARMED", "OPEN")):
            out["armed" if r["state"] == "ARMED" else "open"].append(_public(r, panel))
        t = s.momentum_signals.c
        for r in self.journal.query(select(s.momentum_signals).where(
                t.state.in_(["CLOSED", "SKIPPED", "EXPIRED"]),
                t.updated >= self.now() - timedelta(days=30)).order_by(t.updated.desc())):
            out["closed"].append(_public(r, panel))
        return out

    def _advance(self, row: dict[str, Any], x: MomFrame, day: date) -> list[dict[str, Any]]:
        """Apply every stored session after the signal's last one (MO-9, MO-15)."""
        prm = exit_params(self.cfg, row["policy"])
        pos_d = row["position"]
        sent = list(row["sent"] or [])
        hist = list(row["events"] or [])
        new: list[Any] = []
        state: str = row["state"]
        values: dict[str, Any] = {}
        if pos_d is None:                                  # armed (or seen live intraday)
            j = next((k for k, d in enumerate(x.dates) if d > row["asof"]), None)
            if j is None or x.dates[j] > day:
                return []
            p, evs = enter(x, j, row["trigger"], row["stop"], row["entry_type"] or "stop", prm)
            new += [(x.dates[e.index], e) for e in evs]
            if p is None:
                kind = evs[0].kind if evs else "EXPIRE"
                state = "SKIPPED" if kind == "SKIP" else "EXPIRED"
                values.update(exit_reason=kind)
            else:
                state = "OPEN"
                values.update(entry_date=x.dates[j], entry_price=round(p.entry, 2))
                pos_d = p.as_dict() | {"entry_date": x.dates[j].isoformat(),
                                       "last_date": x.dates[j].isoformat()}
        if pos_d is not None and state == "OPEN":
            p = Position.from_dict(pos_d)
            ei = x.pos.get(date.fromisoformat(pos_d["entry_date"]))
            li = x.pos.get(date.fromisoformat(pos_d["last_date"]))
            if ei is None or li is None:
                log.warning("momentum_signal_bars_missing", signal=row["signal_id"])
                return []
            p.entry_index, p.last_index = ei, li
            k = li + 1
            while not p.closed and k < len(x.c) and x.dates[k] <= day:
                for e in step(x, k, p, replace(prm, policy=p.policy)):
                    new.append((x.dates[k], e))
                k += 1
            pos_d = p.as_dict() | {"entry_date": pos_d["entry_date"],
                                   "last_date": x.dates[min(k - 1, len(x.c) - 1)].isoformat()}
            values.update(stop=round(p.stop, 4))
            if p.closed:
                state = "CLOSED"
                values.update(exit_date=x.dates[p.exit_index], exit_reason=p.exit_reason,
                              r=round(p.realized_r, 3))
            else:
                values.update(r=round(p.realized_r + p.open_frac * (float(x.c[k - 1]) - p.entry)
                                      / p.risk, 3))
        alerts = []
        for d, e in new:
            key = alert_key(e.kind, d, e.price)
            hist.append({"date": d.isoformat(), **e.as_dict()})
            if row["alerted"] and key not in sent and _alertable(e.kind) and not e.executed:
                sent.append(key)
                alerts.append({"ticker": row["ticker"], "setup": row["setup"],
                               "date": d.isoformat(), "kind": e.kind, "price": e.price,
                               "frac": e.frac, "r": e.r, "note": e.note,
                               "at_open_next": e.at_open_next, "signal_id": row["signal_id"]})
        values.update(state=state, position=pos_d, sent=sent, events=hist[-60:],
                      updated=self.now())
        self.journal.update(s.momentum_signals, {"signal_id": row["signal_id"]}, values)
        return alerts

    # -- intraday (MO-15) -----------------------------------------------------------------
    async def intraday(self, rest: Any) -> list[dict[str, Any]]:
        """Trigger, stop and +2R checks from Alpaca snapshots. Alerts only: the close job
        replays the session on the daily bar and its events reuse the same keys."""
        if self.journal is None:
            return []
        rows = [r for r in self._rows(("ARMED", "OPEN")) if r["alerted"]]
        if not rows:
            return []
        snaps = await rest.snapshots(sorted({r["ticker"] for r in rows}))
        today = et_date(self.now())
        alerts = []
        for r in rows:
            snap = snaps.get(r["ticker"]) or {}
            daily = snap.get("dailyBar") or {}
            last = (snap.get("latestTrade") or {}).get("p") or daily.get("c")
            if not last or not daily.get("t"):
                continue
            from trader.alerts.feed import parse_ts

            if to_et(parse_ts(daily["t"])).date() != today:
                continue
            o, h, lo = float(daily["o"]), float(daily["h"]), float(daily["l"])
            sent = list(r["sent"] or [])
            out: list[tuple[str, float, str]] = []
            if r["state"] == "ARMED" and r["asof"] < today:
                trig, stop = float(r["trigger"]), float(r["stop"])
                if r["entry_type"] == "open":
                    out.append(("ENTER", round(o, 2), "buy at the open"))
                elif o > trig + 0.5 * (trig - stop):
                    out.append(("SKIP", round(o, 2), "opened more than 0.5R above the "
                                                     "trigger: not chased"))
                elif h >= trig:
                    out.append(("ENTER", round(max(o, trig), 2), f"stop {stop:.2f}"))
            elif r["state"] == "OPEN" and r["position"]:
                p = Position.from_dict(r["position"])
                if lo <= p.stop:
                    out.append(("STOP", round(min(p.stop, o), 2), "stop hit: sell all"))
                elif not p.partial_done and h >= p.entry + self.m.partial_r * p.risk:
                    out.append(("PARTIAL", round(p.entry + self.m.partial_r * p.risk, 2),
                                f"+{self.m.partial_r:g}R: sell {self.m.partial_frac:.0%}, "
                                "stop to break-even"))
            for kind, px, note in out:
                key = alert_key(kind, today, px)
                if key in sent:
                    continue
                sent.append(key)
                alerts.append({"ticker": r["ticker"], "setup": r["setup"],
                               "date": today.isoformat(), "kind": kind, "price": px,
                               "note": note, "live": True, "signal_id": r["signal_id"],
                               "shares": r["shares"], "last": float(last)})
            if len(sent) != len(r["sent"] or []):
                self.journal.update(s.momentum_signals, {"signal_id": r["signal_id"]},
                                    {"sent": sent, "updated": self.now()})
        return alerts

    # -- evaluation (MO-11..MO-13) --------------------------------------------------------
    async def evaluate(self, start: date | None = None, end: date | None = None,
                       write: bool = True) -> dict[str, Any]:
        until = end or et_date(self.now())
        series, spy = await self.series(until, full=True)
        m = self.m
        res = await asyncio.to_thread(
            evaluate, series, spy, params=exit_params(self.cfg, "ema20"),
            setup_policy={st.name: policy_for(self.cfg, st.name) for st in SETUPS},
            min_rs=m.min_rs, min_template=m.min_template, min_price=m.min_price,
            min_dollar=m.min_dollar_volume, rank_factors=m.rank_factors, start=start, end=end,
            min_trades=m.min_trades, min_pf=m.min_pf,
            risk_pct=m.risk_pct or self.cfg.risk.risk_per_trade_pct,
            max_positions=m.max_positions, max_heat_pct=m.max_heat_pct,
            max_position_pct=self.cfg.risk.max_position_pct, top_n=m.top_n,
            hold_rank=m.hold_rank, rebalance_weeks=m.rebalance_weeks,
            max_correlated=m.max_correlated, max_weight=m.rotation_max_weight,
            watch_only=m.watch_only)
        res["generated"] = self.now().isoformat()
        if write:
            _write(m.eval_file, res)
        return res


def _tail(v: Series, n: int) -> Series:
    k = len(v.c) - n
    return Series(v.symbol, v.dates[k:], v.o[k:], v.h[k:], v.l[k:], v.c[k:], v.v[k:])


def alert_key(kind: str, d: date, price: float) -> str:
    """One alert per signal, event kind and session (a stop raised twice keeps both)."""
    return f"{kind}:{d.isoformat()}" + (f":{price}" if kind == "RAISE_STOP" else "")


def _alertable(kind: str) -> bool:
    return kind in ("ENTER", "SKIP", "STOP", "PARTIAL", "RAISE_STOP", "TRAIL", "CLIMAX",
                    "TIME", "MAX_HOLD")


def _public(r: Mapping[str, Any], panel: Panel) -> dict[str, Any]:
    pos = r.get("position") or {}
    x = panel.frames.get(r["ticker"])
    last = float(x.c[-1]) if x is not None else None
    return {"ticker": r["ticker"], "setup": r["setup"], "policy": pos.get("policy") or
            r["policy"], "state": r["state"], "asof": str(r["asof"]),
            "trigger": r["trigger"], "stop": r["stop"], "entry_date": str(r["entry_date"] or ""),
            "entry_price": r["entry_price"], "exit_date": str(r["exit_date"] or ""),
            "exit_reason": r["exit_reason"], "r": r["r"], "alerted": r["alerted"],
            "proven": r["proven"], "shares": r["shares"], "risk_usd": r["risk_usd"],
            "open_frac": pos.get("open_frac"), "partial_done": pos.get("partial_done"),
            "last": None if last is None else round(last, 2), "rs": r["rs"]}


# --- text (MO-16) --------------------------------------------------------------------------

ACTION = {"ENTER": "BOUGHT (trigger hit)", "SKIP": "SKIPPED", "STOP": "SELL ALL (stop)",
          "PARTIAL": "SELL PART", "RAISE_STOP": "RAISE STOP", "TRAIL": "SELL ALL (trend broke)",
          "CLIMAX": "SELL HALF (climax)", "TIME": "SELL ALL (time stop)",
          "MAX_HOLD": "SELL ALL (max hold)"}


def render_alert(a: Mapping[str, Any]) -> str:
    act = ACTION.get(str(a["kind"]), str(a["kind"]))
    when = " at the next open" if a.get("at_open_next") else ""
    live = " (live)" if a.get("live") else ""
    size = f" · {a['shares']} sh" if a.get("shares") and a["kind"] == "ENTER" else ""
    r = f" · {a['r']:+.2f}R" if a.get("r") else ""
    return (f"{a['ticker']} {act}{when} @ {a['price']:.2f}{live}{size}{r} · {a['setup']} · "
            f"{a.get('note', '')}")


def render(payload: Mapping[str, Any], *, show_unproven: bool = False, n_board: int = 10
           ) -> str:
    if payload.get("error"):
        return f"Momentum: {payload['error']}"
    reg = payload["regime"]
    lines = [f"Momentum · {payload['asof']} · regime {reg['state']} "
             f"(size x{reg['size_mult']:g}) · {'; '.join(reg['reasons'])}"]
    if payload.get("earnings_status") == "unchecked":
        lines.append("Earnings dates NOT checked (no journal): verify before buying (MO-18).")
    elif payload.get("earnings_status") == "stale":
        lines.append("Earnings calendar missing or stale: entry alerts held back (MO-18).")
    tr = payload.get("tracked") or {}
    ev = tr.get("events") or []
    if ev:
        lines.append("EXITS and fills:")
        lines += ["  " + render_alert(a) for a in ev]
    buys = [c for c in payload["candidates"] if c["verdict"] == "alert" or
            (show_unproven and c["verdict"] in ("unproven", "watch_only"))]
    if buys:
        lines.append(f"BUY-STOPS for {buys[0]['session']}:")
        for c in buys:
            rec = c["record"]
            size = f"{c['shares']} sh, ${c['risk_usd']:,.0f} risk" if c["shares"] else \
                "size: run with --equity or in trader-core"
            stats = (f"{rec['trades']} trades, {rec['win_rate']:.0%} win, avg {rec['avg_r']:+.2f}R"
                     f", PF {rec['profit_factor']}") if rec.get("trades") else rec["why"]
            entry = "buy at the open" if c["entry"] == "open" else f"buy-stop {c['trigger']}"
            earn = f" · earnings in {c['earnings_in']} sessions" if c["earnings_in"] else ""
            lines.append(
                f"  {c['ticker']} {c['setup']}: {entry}, stop {c['stop']} ({c['risk_pct']}%), "
                f"{size}. Sell 1/3 at {c['partial_at']} or after {c['partial_days']} sessions in "
                f"profit, trail "
                f"{c['policy']}, time stop {c['time_stop']}. RS {c['rs']}, template "
                f"{c['template']}/8, stage {c['stage']}{earn}. {c['why']}. Record: {stats}")
    held = [c for c in payload["candidates"] if c["verdict"] not in ("alert",)]
    if held:
        by: dict[str, list[str]] = {}
        for c in held:
            by.setdefault(c["verdict"], []).append(f"{c['ticker']} {c['setup']}")
        lines.append("Not alerted: " + "; ".join(f"{k} ({len(v)}): {', '.join(v[:6])}"
                                                 for k, v in by.items()))
    op = tr.get("open") or []
    if op:
        lines.append("OPEN: " + ", ".join(
            f"{o['ticker']} {o['r']:+.2f}R stop {o['stop']:.2f}" for o in op if o["alerted"]
            and o["r"] is not None) or "OPEN: none alerted")
    rot = payload.get("rotation") or {}
    if rot.get("holdings"):
        state = "proven" if rot.get("proven") else "unproven: not alerted"
        head = "REBALANCE today" if rot.get("due") else f"next rebalance {rot['next_rebalance']}"
        lines.append(f"Rotation model ({', '.join(rot['factors'])}; {state}) · {head}:")
        acts = [h for h in rot["holdings"] if h["action"] in ("BUY", "SELL")] if rot.get(
            "due") else []
        if acts:
            lines.append("  " + "; ".join(
                (f"BUY {h['ticker']} {h['weight']:.0%}" + (f" ({h['shares']} sh)" if
                                                          h.get("shares") else ""))
                if h["action"] == "BUY" else f"SELL {h['ticker']} (was {h['prev_weight']:.0%})"
                for h in acts))
        lines.append("  holds: " + ", ".join(
            f"{h['ticker']} {h['weight']:.0%}" for h in rot["holdings"] if h["weight"] > 0))
    watch = payload.get("sma200_watch") or []
    if watch:
        rec = (payload.get("records") or {}).get("SMA200_PULLBACK") or {}
        state = "graded" if rec.get("proven") else "unproven"
        lines.append(f"Near the 200-day average (SMA200_PULLBACK arms on a touch; {state}, "
                     "watch-only):")
        for w in watch[:n_board]:
            h = w["history"]
            past = (f"{h['tests']} past test{'s' if h['tests'] != 1 else ''}, "
                    f"{h['held_rate']:.0%} held, median "
                    f"{h['median_r20_pct']:+.1f}% in 20 sessions" if h.get("tests") else
                    "no past first test")
            lines.append(f"  {w['ticker']} {w['close']} is {w['dist_pct']:.1f}% "
                         f"({w['atr_away']} ATR) above its 200-day {w['sma200']} · "
                         f"{w['from_high_pct']:+.1f}% from the 52w high · RS {w['rs']} · {past}")
    board = payload.get("leaderboard") or []
    if board:
        lines.append(f"Leaders (ranked on {', '.join(payload.get('rank_factors') or [])}):")
        lines.append("  " + ", ".join(
            f"{r['rank']}. {r['symbol']} RS{r['rs']} T{r['template']}" for r in board[:n_board]))
    if not (payload.get("eval") or {}).get("generated"):
        lines.append("Setups not graded yet: run `./run.sh momentum-eval`; until then no "
                     "entry alert is sent (MO-14).")
    return "\n".join(lines)
