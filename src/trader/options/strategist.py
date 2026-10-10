"""A14 Options Strategist (code picks; Qwen only explains, after the close).

Pure functions: the same code serves trader-core and the lightweight alerts bot (OP-14).
Input: a ``View`` (direction, price, invalidation, T1/T2, style) and option quotes (A13).
Output: zero to two ``OptionSuggestion``s plus notes on what was dropped. No suggestion is a
valid answer.

The OP-9 "risk/reward at T1" is the position's planned P/L at T1 divided by its planned loss
at the invalidation level (both repriced with Black-Scholes at the planned exit time), the
same definition the stock checklist uses (reward to the target over loss to the stop).
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from trader.config import OptionsCfg
from trader.options import pricing
from trader.options.data import OptionQuote, years_to
from trader.timeutil import ET, to_et

STRUCTURES = ("long_call", "long_put", "call_debit", "put_debit", "put_credit", "call_credit")
LEVEL_NEEDED = {"long_call": 2, "long_put": 2, "call_debit": 3, "put_debit": 3,
                "put_credit": 3, "call_credit": 3}                             # A15-4
IDEA_QUOTE_AGE_S = 20 * 3600     # next-day ideas use the session's last quotes
LABEL = {"long_call": "Long call", "long_put": "Long put", "call_debit": "Call debit spread",
         "put_debit": "Put debit spread", "put_credit": "Put credit spread",
         "call_credit": "Call credit spread"}


@dataclass(frozen=True)
class View:
    ticker: str
    direction: str             # bull | bear
    spot: float
    invalidation: float        # the stock idea's stop
    t1: float
    t2: float | None = None
    style: str = "day"         # day | swing
    score: int = 0
    source: str = "setup"      # setup | bear_setup | alert | idea | owner
    setup: str = ""
    signal_only: bool = False  # OP-10 (2): no full checklist -> 50% size, lower conviction
    checklist_ok: bool = True
    allow_earnings: bool = False   # `OPT NVDA earnings`
    window: str = ""

    @property
    def target(self) -> float:
        """Short leg anchor (OP-3): T1 for day setups, T2 (if any) for swing."""
        return self.t2 if self.style == "swing" and self.t2 else self.t1


@dataclass(frozen=True)
class Leg:
    quote: OptionQuote
    side: int                  # +1 long, -1 short

    def as_dict(self) -> dict[str, Any]:
        q = self.quote
        return {"symbol": q.symbol, "kind": q.kind, "strike": q.strike, "side": self.side,
                "bid": q.bid, "ask": q.ask, "iv": q.iv, "delta": q.delta, "oi": q.open_interest,
                "volume": q.volume}


@dataclass
class OptionSuggestion:
    suggestion_id: str
    ts: datetime
    view: View
    structure: str
    expiry: date
    legs: list[Leg]
    net_mid: float             # + debit / - credit, per share
    est_fill: float            # same sign convention, OP-4 estimate
    width: float               # spreads: strike width; singles 0
    max_loss: float            # $ per contract
    max_gain: float | None     # $ per contract; None = unlimited
    breakeven: float
    pnl_t1: float              # $ per contract at the planned exit time
    pnl_inv: float
    pop: float                 # estimate, from delta at the breakeven
    contracts: int
    risk_usd: float
    risk_pct_equity: float
    greeks: dict[str, float]
    iv_rank: float | None
    expected_move: float | None
    events: list[str]
    exit_at: datetime
    indicative: bool = True
    rationale: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def is_credit(self) -> bool:
        return self.structure.endswith("_credit")

    @property
    def rr_t1(self) -> float:
        return self.pnl_t1 / abs(self.pnl_inv) if self.pnl_inv < 0 else math.inf

    def value(self, quotes: dict[str, OptionQuote] | None, spot: float, now: datetime,
              iv_shift: float = 0.0) -> float:
        """Position value per share (long legs positive), from live mids when available,
        else Black-Scholes at each leg's entry IV."""
        tot = 0.0
        for lg in self.legs:
            q = (quotes or {}).get(lg.quote.symbol)
            if q is not None and q.mid > 0:
                v = q.mid
            else:
                iv = (lg.quote.iv or 0.3) + iv_shift
                v = pricing.price(lg.quote.kind, spot, lg.quote.strike,
                                  years_to(self.expiry, now), max(iv, 0.01))
            tot += lg.side * v
        return tot

    def pnl(self, value_now: float) -> float:
        """$ per contract: entry at the estimated fill."""
        return (value_now - self.est_fill) * 100


# --- OP-1 structures ----------------------------------------------------------------------------

def structures_for(direction: str, iv_rank: float | None, cfg: OptionsCfg,
                   account_level: int | None = None) -> list[str]:
    rank = 45.0 if iv_rank is None else iv_rank   # short history: the middle column
    if direction == "bull":
        cands = (["long_call", "call_debit"] if rank < cfg.iv_rank_cheap else
                 ["call_debit"] if rank <= cfg.iv_rank_rich else ["put_credit"])
    else:
        cands = (["long_put", "put_debit"] if rank < cfg.iv_rank_cheap else
                 ["put_debit"] if rank <= cfg.iv_rank_rich else ["call_credit"])
    lvl = cfg.account_level if account_level is None else account_level
    return [c for c in cands if c in cfg.structures and LEVEL_NEEDED[c] <= lvl]


# --- OP-2 expiry --------------------------------------------------------------------------------

def pick_expiry(exps: list[date], today: date, style: str, cfg: OptionsCfg,
                blocked: set[date] | None = None) -> date | None:
    lo, hi = cfg.day_dte if style == "day" else cfg.swing_dte
    if style == "day" and cfg.allow_0dte:
        lo = 0
    ok = [e for e in exps if lo <= (e - today).days <= hi and e not in (blocked or set())]
    if not ok:
        return None
    if style == "day":
        return min(ok)                                       # nearest weekly >= 5 DTE
    return min(ok, key=lambda e: abs((e - today).days - 30))


# --- OP-4 liquidity -----------------------------------------------------------------------------

def liquid(q: OptionQuote, cfg: OptionsCfg, now: datetime, max_age_s: float = 60
           ) -> tuple[bool, str]:
    min_vol = cfg.min_volume_early if to_et(now).time() < time(10, 0) else cfg.min_volume
    if not q.usable(now, max_age_s):
        return False, "stale or no bid"
    if q.open_interest < cfg.min_open_interest:
        return False, f"OI {q.open_interest} < {cfg.min_open_interest}"
    if q.volume < min_vol:
        return False, f"volume {q.volume} < {min_vol}"
    m = q.mid
    if (m < 1 and q.ask - q.bid > 0.05 + 1e-9) or (m >= 1 and q.spread_pct > cfg.max_spread_pct):
        return False, f"spread {q.spread_pct:.1f}%"
    return True, ""


def fill_estimate(legs: list[Leg]) -> tuple[float, float]:
    """(net mid, estimated fill): mid +- 25% of the combined bid-ask width, against us."""
    mid = sum(lg.side * lg.quote.mid for lg in legs)
    width = sum(lg.quote.ask - lg.quote.bid for lg in legs)
    return mid, mid + 0.25 * width


# --- OP-3 strikes -------------------------------------------------------------------------------

def _by_delta(qs: list[OptionQuote], target: float, lo: float, hi: float) -> OptionQuote | None:
    ok = [q for q in qs if q.delta is not None and lo <= abs(q.delta) <= hi]
    return min(ok, key=lambda q: abs(abs(q.delta or 0) - target)) if ok else None


def _beyond(qs: list[OptionQuote], level: float, kind: str, start: float) -> list[OptionQuote]:
    """Strikes at or beyond ``level`` in the direction of profit, nearest first, and beyond
    the long leg's strike ``start``."""
    if kind == "C":
        return sorted((q for q in qs if q.strike >= level - 1e-9 and q.strike > start),
                      key=lambda q: q.strike)
    return sorted((q for q in qs if q.strike <= level + 1e-9 and q.strike < start),
                  key=lambda q: -q.strike)


def build_legs(structure: str, view: View, qs: list[OptionQuote], cfg: OptionsCfg,
               now: datetime, risk_budget: float, max_age_s: float = 60
               ) -> tuple[list[Leg] | None, str]:
    kind = "C" if structure in ("long_call", "call_debit", "call_credit") else "P"
    side_q = [q for q in qs if q.kind == kind]
    good = [q for q in side_q if liquid(q, cfg, now, max_age_s)[0]]
    if not good:
        return None, f"no liquid {kind} contracts"
    if structure in ("long_call", "long_put"):
        q = _by_delta(good, 0.62, 0.55, 0.70)
        return ([Leg(q, 1)], "") if q else (None, "no liquid strike at delta 0.55-0.70")
    if structure in ("call_debit", "put_debit"):
        long_q = _by_delta(good, 0.60, 0.45, 0.75)
        if long_q is None:
            return None, "no liquid long leg near delta 0.60"
        for short_q in _beyond(side_q, view.target, kind, long_q.strike)[:4]:
            if liquid(short_q, cfg, now, max_age_s)[0]:
                return [Leg(long_q, 1), Leg(short_q, -1)], ""
        return None, "no liquid short leg at or beyond the target"
    # credit spreads: short strike beyond the invalidation level, |delta| <= 0.30
    if structure == "put_credit":
        shorts = sorted((q for q in good if q.strike < view.invalidation and q.delta is not None
                         and abs(q.delta) <= 0.30), key=lambda q: -q.strike)
    else:
        shorts = sorted((q for q in good if q.strike > view.invalidation and q.delta is not None
                         and abs(q.delta) <= 0.30), key=lambda q: q.strike)
    if not shorts:
        return None, "no liquid short strike beyond the invalidation level with delta <= 0.30"
    short_q = shorts[0]
    wings = sorted((q for q in good if (q.strike < short_q.strike if kind == "P"
                                        else q.strike > short_q.strike)),
                   key=lambda q: abs(q.strike - short_q.strike))
    for long_q in wings:   # narrowest width that the risk budget allows with a fair credit
        legs = [Leg(short_q, -1), Leg(long_q, 1)]
        width = abs(short_q.strike - long_q.strike)
        _mid, fill = fill_estimate(legs)
        credit = -fill
        if credit <= 0:
            continue
        if credit / width >= 0.20 and (width - credit) * 100 <= risk_budget:
            return legs, ""
    return None, "no credit spread width with credit >= 20% of width inside the risk budget"


# --- OP-5 size, OP-7 numbers --------------------------------------------------------------------

def planned_exit(view: View, expiry: date, now: datetime) -> datetime:
    """OP-6 time exit: day -> 15:45 today; swing -> 21 DTE or 10 sessions, whichever first;
    never into the last hour of expiry day."""
    d = to_et(now).date()
    if view.style == "day":
        t = datetime(d.year, d.month, d.day, 15, 45, tzinfo=ET)
    else:
        by_dte = expiry - timedelta(days=21)
        by_sessions = d + timedelta(days=14)
        x = max(min(by_dte, by_sessions), d + timedelta(days=1))
        t = datetime(x.year, x.month, x.day, 15, 0, tzinfo=ET)
    last_hour = datetime(expiry.year, expiry.month, expiry.day, 15, 0, tzinfo=ET)
    return min(t, last_hour)


def _payoff_numbers(structure: str, legs: list[Leg], fill: float) -> tuple[float, float | None,
                                                                          float, float]:
    """(max loss $, max gain $ or None, breakeven, width) per contract."""
    k = [lg.quote.strike for lg in legs]
    if structure == "long_call":
        return fill * 100, None, k[0] + fill, 0.0
    if structure == "long_put":
        return fill * 100, (k[0] - fill) * 100, k[0] - fill, 0.0
    width = abs(k[0] - k[1])
    if structure == "call_debit":
        return fill * 100, (width - fill) * 100, legs[0].quote.strike + fill, width
    if structure == "put_debit":
        return fill * 100, (width - fill) * 100, legs[0].quote.strike - fill, width
    credit = -fill
    short_k = legs[0].quote.strike
    be = short_k - credit if structure == "put_credit" else short_k + credit
    return (width - credit) * 100, credit * 100, be, width


def size(max_loss: float, equity: float, risk_pct: float, cfg: OptionsCfg, debit: float,
         signal_only: bool = False) -> int:
    """OP-5: floor(risk% x equity / max loss), capped by premium % and max contracts."""
    if max_loss <= 0 or equity <= 0:
        return 0
    budget = equity * risk_pct / 100 * (0.5 if signal_only else 1.0)
    n = math.floor(budget / max_loss)
    cap_basis = debit * 100 if debit > 0 else max_loss
    n = min(n, math.floor(equity * cfg.max_premium_pct / 100 / cap_basis), cfg.max_contracts)
    return max(0, n)


def suggestion_id(view: View, structure: str, expiry: date, now: datetime) -> str:
    key = f"{view.ticker}|{view.direction}|{structure}|{expiry}|{now.isoformat()}"
    return "op-" + hashlib.sha256(key.encode()).hexdigest()[:20]


def suggest(view: View, quotes: list[OptionQuote], *, cfg: OptionsCfg, now: datetime,
            equity: float, risk_pct: float, iv_rank: float | None,
            expected_moves: dict[date, float] | None = None,
            events_for: Any = None, earnings: date | None = None,
            account_level: int | None = None, indicative: bool = True
            ) -> tuple[list[OptionSuggestion], list[str]]:
    """Build 0-2 suggestions. ``events_for(expiry) -> list[str]`` (A13-4)."""
    notes: list[str] = []
    if not view.checklist_ok:
        return [], ["view's checklist failed (OP-9)"]
    today = to_et(now).date()
    blocked: set[date] = set()
    if cfg.avoid_earnings and earnings is not None and not view.allow_earnings:
        blocked = {e for e in {q.expiry for q in quotes} if today <= earnings <= e}
    exp = pick_expiry(sorted({q.expiry for q in quotes}), today, view.style, cfg, blocked)
    if exp is None:
        return [], [f"no expiry in the {view.style} DTE range"
                    + (" without earnings (OP-2)" if blocked else "")]
    qs = [q for q in quotes if q.expiry == exp]
    em = (expected_moves or {}).get(exp)
    budget = equity * risk_pct / 100 * (0.5 if view.signal_only else 1.0)
    out: list[OptionSuggestion] = []
    for st in structures_for(view.direction, iv_rank, cfg, account_level):
        legs, why = build_legs(st, view, qs, cfg, now, budget,
                               IDEA_QUOTE_AGE_S if view.source == "idea" else 60)
        if legs is None:
            notes.append(f"{st}: {why}")
            continue
        mid, fill = fill_estimate(legs)
        if not st.endswith("_credit") and fill <= 0:
            notes.append(f"{st}: no debit")
            continue
        max_loss, max_gain, be, width = _payoff_numbers(st, legs, fill)
        exit_at = planned_exit(view, exp, now)
        t_exit = years_to(exp, exit_at)

        def value_at(spot: float, _legs: list[Leg] = legs, _t: float = t_exit) -> float:
            return sum(lg.side * pricing.price(lg.quote.kind, spot, lg.quote.strike, _t,
                                               lg.quote.iv or 0.3) for lg in _legs)
        pnl_t1 = (value_at(view.t1) - fill) * 100
        pnl_inv = (value_at(view.invalidation) - fill) * 100
        if max_gain is not None:   # a repricing never beats the payoff bounds
            pnl_t1 = min(pnl_t1, max_gain)
        pnl_inv = max(pnl_inv, -max_loss)
        lead = legs[0].quote
        g_be = pricing.greeks(lead.kind, view.spot, be, years_to(exp, now), lead.iv or 0.3)
        pop = abs(g_be.delta) if not st.endswith("_credit") else 1 - abs(g_be.delta)
        n = size(max_loss, equity, risk_pct, cfg, fill if fill > 0 else 0.0, view.signal_only)
        sug = OptionSuggestion(
            suggestion_id(view, st, exp, now), now, view, st, exp, legs, mid, fill, width,
            max_loss, max_gain, be, pnl_t1, pnl_inv, pop, n, n * max_loss,
            n * max_loss / equity * 100 if equity else 0.0,
            {k: sum(lg.side * (getattr(lg.quote, k) or 0) for lg in legs) * 100
             for k in ("delta", "gamma", "theta", "vega")},
            iv_rank, em, events_for(exp) if events_for else [], exit_at, indicative)
        # OP-9 gate
        if n == 0:
            notes.append(f"{st}: size 0 at the risk budget (OP-5)")
            continue
        if sug.rr_t1 < cfg.min_rr_at_t1:
            notes.append(f"{st}: reward/risk at T1 {sug.rr_t1:.2f} < {cfg.min_rr_at_t1}")
            continue
        if em and abs(view.t1 - view.spot) > cfg.max_target_expected_move_x * em:
            notes.append(f"{st}: T1 beyond {cfg.max_target_expected_move_x}x the expected "
                         f"move ({em:.2f})")
            continue
        sug.rationale = template_rationale(sug)
        out.append(sug)
    return out[:2], notes


# --- OP-8 / OP-11 text ----------------------------------------------------------------------------

def template_rationale(s_: OptionSuggestion) -> str:
    """Market-hours rationale: a code template, no model call (OP-8)."""
    v = s_.view
    why = {"long_call": "options are cheap (low IV rank), so a single call keeps the upside",
           "long_put": "options are cheap (low IV rank), so a single put keeps the downside",
           "call_debit": "the short call at the target sells the move beyond T1 to cut cost",
           "put_debit": "the short put at the target sells the move beyond T1 to cut cost",
           "put_credit": "options are expensive (high IV rank): sell premium below the "
                         "invalidation level",
           "call_credit": "options are expensive (high IV rank): sell premium above the "
                          "invalidation level"}[s_.structure]
    return f"{LABEL[s_.structure]} for a {v.direction}ish {v.setup or v.source}: {why}."


def _strikes(s_: OptionSuggestion) -> str:
    return " / ".join(f"{lg.quote.strike:g}{lg.quote.kind}" for lg in s_.legs)


def format_suggestion(s_: OptionSuggestion, mode: str = "advisory") -> str:
    v = s_.view
    icon = "📈" if v.direction == "bull" else "📉"
    conv = " · signal-only, lower conviction" if v.signal_only else ""
    tag = "PAPER/ADVISORY"   # option order placement (§9 paper/live) is not built yet
    del mode
    em = f" · expected move to {s_.expiry:%b %d}: ±{s_.expected_move:.2f}" \
        if s_.expected_move else ""
    dte = (s_.expiry - to_et(s_.ts).date()).days
    q0 = s_.legs[0].quote
    if s_.is_credit:
        price = f"credit ~{-s_.net_mid:.2f} (est. fill {-s_.est_fill:.2f})"
    else:
        bidask = (f" (bid/ask {q0.bid:.2f}/{q0.ask:.2f})" if len(s_.legs) == 1 else "")
        price = f"debit ~{s_.net_mid:.2f}{bidask} (est. fill {s_.est_fill:.2f})"
    gain = f"${s_.max_gain:,.0f}" if s_.max_gain is not None else "unlimited"
    unit = "spread" if len(s_.legs) > 1 else "contract"
    deltas = " / ".join(f"{'long' if lg.side > 0 else 'short'} Δ{(lg.quote.delta or 0):+.2f}"
                        for lg in s_.legs)
    oi = " / ".join(f"{lg.quote.open_interest / 1000:.1f}k" for lg in s_.legs)
    spr = max(lg.quote.spread_pct for lg in s_.legs)
    rank = f"{s_.iv_rank:.0f}" if s_.iv_rank is not None else "n/a"
    stock_exit = (f"stock {'<' if v.direction == 'bull' else '>'} {v.invalidation:.2f}")
    when = (f"by {to_et(s_.exit_at):%H:%M} today" if v.style == "day"
            else f"by {to_et(s_.exit_at):%b %d}")
    target_vs_em = ""
    if s_.expected_move:
        inside = abs(v.t1 - v.spot) <= s_.expected_move
        target_vs_em = f" · T1 {'inside' if inside else 'beyond'} 1x expected move"
    events = ("Events before expiry: " + ", ".join(s_.events) + ".") if s_.events else \
        "No earnings before expiry."
    lines = [
        f"{icon} OPTIONS IDEA · {v.ticker} {'bullish' if v.direction == 'bull' else 'bearish'} "
        f"({v.setup or v.source}, score {v.score}){conv} · {tag}",
        f"Stock {v.spot:.2f} · invalidation {v.invalidation:.2f} · T1 {v.t1:.2f}{em}",
        f"{LABEL[s_.structure]}  {s_.expiry:%b %d} ({dte} DTE)  {_strikes(s_)}",
        f"  {price} · max loss ${s_.max_loss:,.0f} · max gain {gain} · breakeven "
        f"{s_.breakeven:.2f}",
        f"  at T1: ~{s_.pnl_t1:+,.0f} · at invalidation: ~{s_.pnl_inv:+,.0f}   (per {unit})",
        f"  size {s_.contracts} {unit}{'s' if s_.contracts != 1 else ''} = ${s_.risk_usd:,.0f} "
        f"risk ({s_.risk_pct_equity:.2f}% of equity) · P(profit) ~{s_.pop * 100:.0f}% "
        f"(estimate){target_vs_em}",
        f"IV rank {rank} · {deltas} · OI {oi} · spread {spr:.1f}%",
        f"Exit: {stock_exit}, or at T1, or {when}. {events}",
        s_.rationale,
        ("Prices indicative (free feed). " if s_.indicative else "") + "Not advice.",
    ]
    return "\n".join(x for x in lines if x)


def money(x: float) -> Decimal:
    return Decimal(str(round(x, 2)))
