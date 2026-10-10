"""A16 Options Monitor & Grader (code).

- A16-1 every suggestion is paper-tracked: entry at the estimated fill, marks every minute
  in a window and at the close, exits by the plan (OP-6) on the first condition met.
- OP-12 follow-up alerts (each once): T1 / invalidation, 50% / 70% of max profit, -50% of
  premium, time exit due, IV down >= 20%, an event now inside the expiry, 3 DTE left.
- A16-2 result: P/L in $ and R (R = max loss), exit reason, holding time, IV change, and
  whether the stock view was right vs whether the option made money.
- A16-3 report sections and A16-4 promotion gate (pure functions over journal rows).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from trader.journal import schema as s
from trader.options.data import OptionQuote, parse_occ
from trader.options.strategist import LABEL, Leg, OptionSuggestion, View, money
from trader.timeutil import to_et


@dataclass
class Tracked:
    sug: OptionSuggestion
    entry_iv: float | None
    flags: set[str] = field(default_factory=set)
    half_pnl: float | None = None      # singles: 50% taken at T1 ($ per contract)
    t1_hit: bool = False
    inv_hit: bool = False
    last_pnl: float = 0.0
    last_spot: float | None = None


def _iv(quotes: dict[str, OptionQuote] | None, sug: OptionSuggestion) -> float | None:
    q = (quotes or {}).get(sug.legs[0].quote.symbol)
    return q.iv if q is not None and q.iv else None


class OptionsBook:
    def __init__(self, journal: Any) -> None:
        self.journal = journal
        self.open: dict[str, Tracked] = {}

    # -- lifecycle ---------------------------------------------------------------------------
    def add(self, sug: OptionSuggestion, text: str) -> Tracked:
        tr = Tracked(sug, sug.legs[0].quote.iv)
        self.open[sug.suggestion_id] = tr
        if self.journal is not None:
            self.journal.insert(s.option_suggestions, {
                "suggestion_id": sug.suggestion_id, "ts": sug.ts, "ticker": sug.view.ticker,
                "direction": sug.view.direction, "source": sug.view.source,
                "style": sug.view.style, "structure": sug.structure, "expiry": sug.expiry,
                "legs_json": [lg.as_dict() for lg in sug.legs], "contracts": sug.contracts,
                "entry_price": money(sug.est_fill), "max_loss": money(sug.max_loss),
                "max_gain": money(sug.max_gain) if sug.max_gain is not None else None,
                "breakeven": sug.breakeven, "underlying": sug.view.spot,
                "invalidation": sug.view.invalidation, "t1": sug.view.t1,
                "iv_rank": sug.iv_rank, "text": text, "status": "open",
                "detail_json": detail_of(sug)})
        return tr

    def restore(self) -> int:
        """Re-open tracking after a restart (open rows in the journal)."""
        if self.journal is None:
            return 0
        n = 0
        for r in self.journal.rows(s.option_suggestions, {"status": "open"}):
            sug = from_row(r)
            if sug is not None:
                self.open[sug.suggestion_id] = Tracked(sug, sug.legs[0].quote.iv)
                n += 1
        return n

    # -- A16-1 marks, OP-6 exits, OP-12 follow-ups ---------------------------------------------
    def mark(self, tr: Tracked, spot: float, quotes: dict[str, OptionQuote] | None,
             now: datetime, events: list[str] | None = None
             ) -> tuple[list[str], str | None]:
        """Returns (follow-up texts, exit reason or None)."""
        sug, v = tr.sug, tr.sug.view
        value = sug.value(quotes, spot, now)
        pnl = sug.pnl(value)
        tr.last_pnl, tr.last_spot = pnl, spot
        msgs: list[str] = []
        head = f"OPT {v.ticker} {LABEL[sug.structure]} {sug.expiry:%b %d}"
        bull = v.direction == "bull"
        through_inv = spot <= v.invalidation if bull else spot >= v.invalidation
        at_t1 = spot >= v.t1 if bull else spot <= v.t1

        def once(flag: str, text: str) -> None:
            if flag not in tr.flags:
                tr.flags.add(flag)
                msgs.append(f"{head}: {text} (P/L ~{pnl:+,.0f}/contract)")

        if through_inv:
            tr.inv_hit = True
            once("inv", f"stock {spot:.2f} through invalidation {v.invalidation:.2f}: EXIT now")
            return msgs, "INVALIDATION"
        if sug.is_credit:
            credit = -sug.est_fill * 100
            short_k = sug.legs[0].quote.strike
            if (spot < short_k) if bull else (spot > short_k):
                once("breach", f"short strike {short_k:g} breached: CLOSE the spread")
                return msgs, "SHORT_BREACHED"
            if pnl >= 0.5 * credit:
                once("p50", "50% of max profit reached: CLOSE (plan)")
                return msgs, "PROFIT_50"
        elif len(sug.legs) == 2:
            max_value = sug.width * 100
            if at_t1 or value * 100 >= 0.7 * max_value:
                tr.t1_hit = tr.t1_hit or at_t1
                once("p70", ("T1 reached" if at_t1 else "70% of max value reached")
                     + ": CLOSE the spread (plan)")
                return msgs, "T1" if at_t1 else "PROFIT_70"
            if pnl >= 0.5 * (sug.max_gain or 0):
                once("p50", "50% of max profit: plan says hold to T1 or 70%")
        else:
            if at_t1 and tr.half_pnl is None:
                tr.t1_hit = True
                tr.half_pnl = pnl
                once("t1", f"T1 {v.t1:.2f} reached: TAKE 50% (plan), hold the rest to the time "
                           "exit or invalidation")
        if not sug.is_credit and pnl <= -0.5 * sug.max_loss:
            once("m50", "down 50% of premium: plan exit is the invalidation level")
        if now >= sug.exit_at:
            once("time", f"time exit due ({to_et(sug.exit_at):%H:%M}): CLOSE (plan)")
            return msgs, "TIME"
        iv_now = _iv(quotes, sug)
        if tr.entry_iv and iv_now and iv_now <= tr.entry_iv * 0.8:
            once("crush", f"IV fell {(1 - iv_now / tr.entry_iv) * 100:.0f}% since entry "
                          "(crush): long premium loses value; plan exits unchanged")
        for ev in events or []:
            if ev not in sug.events:
                once(f"ev:{ev}", f"{ev} is now inside the expiry: review; the plan avoids "
                                 "holding through it")
        if (sug.expiry - to_et(now).date()).days <= 3:
            once("dte3", "3 DTE left: plan never holds into the last hour of expiry")
        return msgs, None

    def close(self, tr: Tracked, reason: str, now: datetime) -> dict[str, Any]:
        """A16-2: realize and journal the result."""
        sug, v = tr.sug, tr.sug.view
        per = tr.last_pnl if tr.half_pnl is None else 0.5 * tr.half_pnl + 0.5 * tr.last_pnl
        total = per * sug.contracts
        r = per / sug.max_loss if sug.max_loss else 0.0
        spot = tr.last_spot if tr.last_spot is not None else v.spot
        view_right = True if tr.t1_hit else False if tr.inv_hit else (
            spot > v.spot if v.direction == "bull" else spot < v.spot)
        self.open.pop(sug.suggestion_id, None)
        res = {"status": "closed", "exit_price": money(sug.est_fill + per / 100),
               "exit_reason": reason, "closed_at": now, "pnl": money(total),
               "r_multiple": round(r, 3), "stock_view_right": view_right,
               "mark": money(per), "marked_at": now}
        if self.journal is not None:
            self.journal.update(s.option_suggestions, {"suggestion_id": sug.suggestion_id}, res)
        return {**res, "suggestion_id": sug.suggestion_id, "per_contract": per,
                "option_made_money": total > 0}

    def snapshot(self, tr: Tracked, now: datetime) -> None:
        if self.journal is not None:
            self.journal.update(s.option_suggestions, {"suggestion_id": tr.sug.suggestion_id},
                                {"mark": money(tr.last_pnl), "marked_at": now})

    def status_text(self) -> str:
        if not self.open:
            return "No open options suggestions"
        lines = ["Open options suggestions (paper-tracked):"]
        for tr in self.open.values():
            sg = tr.sug
            lines.append(f"- {sg.view.ticker} {LABEL[sg.structure]} {sg.expiry:%b %d} "
                         f"x{sg.contracts}: {tr.last_pnl * sg.contracts:+,.0f} "
                         f"({tr.last_pnl / sg.max_loss:+.2f}R)" if sg.max_loss else "")
        return "\n".join(lines)


# --- persistence -----------------------------------------------------------------------------

def detail_of(sug: OptionSuggestion) -> dict[str, Any]:
    v = sug.view
    return {"view": {k: getattr(v, k) for k in View.__dataclass_fields__},
            "net_mid": sug.net_mid, "est_fill": sug.est_fill, "width": sug.width,
            "pnl_t1": sug.pnl_t1, "pnl_inv": sug.pnl_inv, "pop": sug.pop,
            "risk_usd": sug.risk_usd, "risk_pct_equity": sug.risk_pct_equity,
            "greeks": sug.greeks, "expected_move": sug.expected_move, "events": sug.events,
            "exit_at": sug.exit_at.isoformat(), "indicative": sug.indicative,
            "rationale": sug.rationale,
            "entry_quotes": [{**lg.as_dict(), "ts": lg.quote.quote_ts.isoformat()
                              if lg.quote.quote_ts else None} for lg in sug.legs]}  # A13-6


def from_row(r: dict[str, Any]) -> OptionSuggestion | None:
    d = r.get("detail_json") or {}
    if "view" not in d:
        return None
    v = View(**d["view"])
    legs = []
    for lg in r.get("legs_json") or []:
        p = parse_occ(lg["symbol"])
        if p is None:
            return None
        legs.append(Leg(OptionQuote(lg["symbol"], p[0], p[1], p[2], p[3], lg.get("bid") or 0,
                                    lg.get("ask") or 0, None, lg.get("volume") or 0,
                                    lg.get("oi") or 0, lg.get("iv"), lg.get("delta")),
                        int(lg["side"])))
    return OptionSuggestion(
        r["suggestion_id"], r["ts"], v, r["structure"], r["expiry"], legs, d["net_mid"],
        d["est_fill"], d["width"], float(r["max_loss"]),
        float(r["max_gain"]) if r.get("max_gain") is not None else None,
        float(r["breakeven"]), d["pnl_t1"], d["pnl_inv"], d["pop"], int(r["contracts"]),
        d["risk_usd"], d["risk_pct_equity"], d["greeks"], r.get("iv_rank"),
        d.get("expected_move"), d.get("events") or [], datetime.fromisoformat(d["exit_at"]),
        d.get("indicative", True), d.get("rationale", ""))


# --- A16-3 report, A16-4 promotion gate ------------------------------------------------------

def _iv_bucket(rank: float | None) -> str:
    if rank is None:
        return "IV n/a"
    return "IV<30" if rank < 30 else "IV30-60" if rank <= 60 else "IV>60"


def _dte_bucket(r: dict[str, Any]) -> str:
    ts, exp = r.get("ts"), r.get("expiry")
    if ts is None or exp is None:
        return "DTE n/a"
    n = (exp - to_et(ts).date()).days
    return "0-7 DTE" if n <= 7 else "8-14 DTE" if n <= 14 else "15-30 DTE" if n <= 30 \
        else "31+ DTE"


def _stats(rows: list[dict[str, Any]]) -> tuple[int, float, float, float]:
    """(n, win rate %, expectancy R, profit factor)."""
    rs = [float(r.get("r_multiple") or 0) for r in rows]
    if not rs:
        return 0, 0.0, 0.0, 0.0
    wins = [x for x in rs if x > 0]
    losses = [-x for x in rs if x < 0]
    pf = sum(wins) / sum(losses) if losses else (float("inf") if wins else 0.0)
    return len(rs), len(wins) / len(rs) * 100, sum(rs) / len(rs), pf


def report(rows: list[dict[str, Any]], title: str = "Options (A16-3)") -> str:
    closed = [r for r in rows if r.get("status") == "closed"]
    opened = len(rows)
    n, wr, exp, pf = _stats(closed)
    lines = [f"{title}: {opened} suggestions, {n} graded · win {wr:.0f}% · "
             f"expectancy {exp:+.2f}R · PF {pf:.2f}" if n else
             f"{title}: {opened} suggestions, none graded yet"]
    for name, key in (("structure", lambda r: LABEL.get(r["structure"], r["structure"])),
                      ("direction", lambda r: r["direction"]),
                      ("IV regime", lambda r: _iv_bucket(r.get("iv_rank"))),
                      ("DTE", _dte_bucket)):
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in closed:
            groups[key(r)].append(r)
        if groups:
            parts = []
            for g, rr in sorted(groups.items()):
                gn, gwr, gexp, _ = _stats(rr)
                parts.append(f"{g} {gn} ({gwr:.0f}%, {gexp:+.2f}R)")
            lines.append(f"  by {name}: " + "; ".join(parts))
    wrong_structure = sum(1 for r in closed if r.get("stock_view_right")
                          and float(r.get("pnl") or 0) <= 0)
    if closed:
        lines.append(f"  view right but option lost (structure, not view): {wrong_structure}")
    return "\n".join(lines)


def promotion_status(rows: list[dict[str, Any]], min_n: int = 40, min_exp: float = 0.15,
                     min_pf: float = 1.3, min_sessions: int = 20
                     ) -> dict[tuple[str, str], tuple[bool, str]]:
    """A16-4 per (structure, direction): may move from advisory to paper execution."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        if r.get("status") == "closed":
            groups[(r["structure"], r["direction"])].append(r)
    out = {}
    for k, rr in groups.items():
        n, _wr, exp, pf = _stats(rr)
        sessions = len({to_et(r["ts"]).date() for r in rr if r.get("ts") is not None})
        ok = n >= min_n and exp >= min_exp and pf >= min_pf and sessions >= min_sessions
        out[k] = (ok, f"{n}/{min_n} graded, {exp:+.2f}R (>= {min_exp}), PF {pf:.2f} "
                      f"(>= {min_pf}), {sessions}/{min_sessions} sessions")
    return out


def session_of(r: dict[str, Any]) -> date | None:
    return to_et(r["ts"]).date() if r.get("ts") is not None else None
