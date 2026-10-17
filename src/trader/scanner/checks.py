"""Decision checks for scanner ideas (SCANNER_REQUIREMENTS TS-17..TS-19).

The live playbook runs every trade through the entry checklist (REQUIREMENTS §5.4) and the
loss limits; scanner ideas are advisory, but they must respect the same decisions before an
owner acts on them: event risk, locks, positions and correlation, size, and the long-only
rule (ADR-008). Each idea gets a list of checks and a verdict:

* ``go``       every hard check passes and at most one soft check fails;
* ``caution``  hard checks pass, two or more soft checks fail;
* ``blocked``  a hard check fails (earnings, lock, already held, correlation, size);
* ``watch``    not on the trading list (``scanner.symbols``): research only (UR-1);
* ``options``  a bearish view: stock is long-only, so it is a puts/put-spread idea (ADR-008).

A check whose data is missing (for example earnings in an offline CLI run) is ``None``:
shown as unknown, never counted as a pass.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from trader.scanner import ta
from trader.scanner.techniques import Idea, Series

Sizer = Callable[[float, float], tuple[int, float, str]]   # (entry, stop) -> qty, risk $, why
EARNINGS_BLOCK_SESSIONS = 3        # swing entries never hold into earnings (CK-1, XR-6)
ROOM_MIN_R = 1.5                   # nearest resistance >= 1.5R (CK-8)



@dataclass(frozen=True)
class Check:
    key: str
    label: str
    ok: bool | None          # None = no data
    detail: str
    hard: bool = False
    rule: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "label": self.label, "ok": self.ok, "detail": self.detail,
                "hard": self.hard, "rule": self.rule}


@dataclass
class ScanContext:
    """What the caller knows beyond the bars. Empty fields mean "unknown"."""

    tradable: frozenset[str] = frozenset()
    next_session: date | None = None
    sessions_until: Callable[[date], int] | None = None      # trading sessions from today
    earnings: dict[str, date | None] | None = None            # None = earnings data unknown
    macro_next: str | None = None                             # e.g. "FOMC", "CPI/jobs"
    macro_known: bool = False
    locks: dict[str, list[str]] | None = None                 # ticker -> active lock names
    held: frozenset[str] | None = None                        # open positions (canonical)
    groups: dict[str, tuple[frozenset[str], int]] = field(default_factory=dict)
    sector_up: dict[str, bool | None] = field(default_factory=dict)   # MF-2 analogue
    sizer: Sizer | None = None
    edges: dict[str, float] | None = None


def sector_trend(sector: Series | None) -> bool | None:
    """Sector ETF (SMH) above its 50-day average and the average rising over 10 sessions."""
    if sector is None or len(sector.c) < 60:
        return None
    m = ta.sma(sector.c, 50)
    return bool(sector.c[-1] > m[-1] and m[-1] > m[-11])


def _group_of(t: str, ctx: ScanContext) -> tuple[str, frozenset[str], int] | None:
    for name, (members, cap) in ctx.groups.items():
        if t in members:
            return name, members, cap
    return None


def _checks(idea: Idea, ctx: ScanContext, taken: dict[str, int]) -> tuple[list[Check], int | None,
                                                                         float | None, str]:
    t, long = idea.symbol, idea.side == "long"
    cx = idea.context
    out: list[Check] = []

    def add(key: str, label: str, ok: bool | None, detail: str, hard: bool = False,
            rule: str = "") -> None:
        out.append(Check(key, label, ok, detail, hard, rule))

    # --- direction and list (hard) --------------------------------------------------------
    add("direction", "Long-only stock (bearish views via options)", long,
        "long: stock entry allowed" if long else
        "bearish view: no stock shorting (ADR-008); use puts or a put spread on the "
        "Options page", hard=True, rule="ADR-008")
    add("tradable", "On the trading list", t in ctx.tradable,
        "on universe.tickers" if t in ctx.tradable else
        "research only: not on universe.tickers", hard=True, rule="UR-1")
    # --- event risk (hard for earnings, soft for macro) ------------------------------------
    if ctx.earnings is None:
        add("earnings", f"No earnings within {EARNINGS_BLOCK_SESSIONS} sessions", None,
            "earnings dates unknown here (trader-core knows them)", True, "CK-1, XR-6")
    else:
        ed = ctx.earnings.get(t)
        if ed is None:
            add("earnings", f"No earnings within {EARNINGS_BLOCK_SESSIONS} sessions", True,
                "no earnings date on file", True, "CK-1, XR-6")
        else:
            n = ctx.sessions_until(ed) if ctx.sessions_until else (ed - idea.asof).days
            add("earnings", f"No earnings within {EARNINGS_BLOCK_SESSIONS} sessions",
                n > EARNINGS_BLOCK_SESSIONS, f"earnings {ed.isoformat()} ({n} session(s))",
                True, "CK-1, XR-6")
    if ctx.macro_known:
        add("macro", "No FOMC/CPI/jobs day next session", ctx.macro_next is None,
            f"{ctx.macro_next} on {ctx.next_session}" if ctx.macro_next else
            "no scheduled macro release", rule="TW-5")
    else:
        add("macro", "No FOMC/CPI/jobs day next session", None, "macro calendar not loaded",
            rule="TW-5")
    # --- locks, positions, correlation (hard) ----------------------------------------------
    if ctx.locks is None:
        add("locks", "No wash-sale or loss-streak lock", None,
            "locks unknown here (trader-core knows them)", True, "WS-1, RC-4")
    else:
        lk = ctx.locks.get(t, [])
        add("locks", "No wash-sale or loss-streak lock", not lk,
            ", ".join(lk) if lk else "no active lock", True, "WS-1, RC-4")
    if ctx.held is None:
        add("held", "Not already in a position", None, "positions unknown here", True, "DR-4")
    else:
        add("held", "Not already in a position", t not in ctx.held,
            "already held" if t in ctx.held else "no open position", True, "DR-4")
    grp = _group_of(t, ctx)
    if grp is not None and long:
        name, members, cap = grp
        used = taken.get(name, 0) + (len(members & ctx.held) if ctx.held else 0)
        add("correlation", f"Room in the correlated group '{name}'", used < cap,
            f"{used} of {cap} already used (open or a higher-ranked idea)", True, "LL-10")
    # --- size (hard) --------------------------------------------------------------------
    shares: int | None = None
    risk_usd: float | None = None
    if long and ctx.sizer is not None:
        shares, risk_usd, why = ctx.sizer(idea.trigger, idea.stop)
        add("size", "At least one share within the risk rules", shares >= 1,
            f"{shares} shares, ${risk_usd:,.0f} at risk ({why})", True, "LL-1, LL-9, LL-11")
    elif long:
        add("size", "At least one share within the risk rules", None,
            "equity unknown (trader-core sizes it; CLI: --equity)", True, "LL-1, LL-9")
    # --- quality (soft) -------------------------------------------------------------------
    if cx.get("reversion"):
        add("trend", "Daily trend aligned", True, "mean reversion inside the 200-day trend",
            rule="TS-4")
    else:
        add("trend", "Daily trend aligned", cx.get("uptrend"),
            "close vs SMA50 vs SMA200", rule="TS-6")
    add("weekly", "Weekly trend aligned", cx.get("weekly_up"),
        "week close vs a rising 10-week average", rule="TS-17")
    mk = cx.get("market_up")
    add("market", "Market (SPY) trend aligned", mk, "SPY vs its 50-day average", rule="MF-1")
    if t in ctx.sector_up:
        add("sector", "Sector (SMH) trend aligned",
            None if ctx.sector_up[t] is None else ctx.sector_up[t] == long,
            "SMH vs its rising 50-day average", rule="MF-2")
    rs = cx.get("rs63")
    add("rs", "Relative strength vs SPY", None if rs is None else rs > 0,
        "—" if rs is None else f"{rs:+.1f} pts over 63 sessions", rule="TS-6")
    room = idea.room_r
    add("room", f"Room to the next level >= {ROOM_MIN_R}R", room is None or room >= ROOM_MIN_R,
        "open sky" if room is None else f"{room:.1f}R", rule="CK-8")
    adx = cx.get("adx")
    add("overheated", "Trend not overheated (ADX < 35)", None if adx is None else adx < 35,
        "—" if adx is None else f"ADX {adx:.0f}", rule="TS-8")
    key = f"{idea.technique}:{idea.side}"
    if ctx.edges is None:
        add("edge", "Technique has a positive measured edge", None,
            "run scan-eval to measure it", rule="TS-12")
    else:
        e = ctx.edges.get(key)
        add("edge", "Technique has a positive measured edge", None if e is None else e > 0,
            "too few graded trades" if e is None else f"{e:+.2f}R (shrunk)", rule="TS-12")
    # --- verdict ------------------------------------------------------------------------
    hard_fail = [c for c in out if c.hard and c.ok is False]
    soft_fail = [c for c in out if not c.hard and c.ok is False]
    if not long:
        verdict = "options"
    elif any(c.key == "tradable" for c in hard_fail):
        verdict = "watch"
    elif hard_fail:
        verdict = "blocked"
    elif len(soft_fail) >= 2:
        verdict = "caution"
    else:
        verdict = "go"
    return out, shares, risk_usd, verdict


def apply_checks(ideas: Sequence[Idea], ctx: ScanContext) -> list[Idea]:
    """Checks, size and verdict on every idea (in score order, so a higher-ranked idea takes
    the correlated group's slot first). Returns the ideas ranked: go, caution, options,
    watch, blocked; by score inside each."""
    taken: dict[str, int] = {}
    for idea in sorted(ideas, key=lambda d: (-d.score, d.symbol)):
        checks, shares, risk_usd, verdict = _checks(idea, ctx, taken)
        idea.checks = [c.as_dict() for c in checks]
        idea.shares, idea.risk_usd, idea.verdict = shares, risk_usd, verdict
        if verdict in ("go", "caution"):
            grp = _group_of(idea.symbol, ctx)
            if grp is not None:
                taken[grp[0]] = taken.get(grp[0], 0) + 1
        if idea.side == "short":
            idea.plan = ("bearish view below " + f"{idea.trigger:.2f}"
                         + ": express with puts or a put spread (Options page), never stock")
    order = {v: k for k, v in enumerate(("go", "caution", "options", "watch", "blocked"))}
    return sorted(ideas, key=lambda d: (order.get(d.verdict or "", 9), -d.score, d.symbol))


def check_summary(idea: Idea) -> str:
    """'9/12 checks; failed: earnings, room' (unknown checks are not counted)."""
    known = [c for c in idea.checks if c["ok"] is not None]
    passed = sum(1 for c in known if c["ok"])
    failed = [c["key"] for c in known if not c["ok"] and c["key"] != "direction"]
    tail = f"; failed: {', '.join(failed)}" if failed else ""
    unknown = len(idea.checks) - len(known)
    return (f"{passed}/{len(known)} checks{tail}"
            + (f"; {unknown} unknown" if unknown else ""))


def simple_sizer(equity: float, risk_pct: float, max_position_pct: float,
                 max_leverage: float = 1.0) -> Sizer:
    """STRATEGY_SPEC §6 without the drawdown and regime state (CLI use; trader-core passes
    RiskManager.size instead)."""
    def size(entry: float, stop: float) -> tuple[int, float, str]:
        per = entry - stop
        if per <= 0 or entry <= 0:
            return 0, 0.0, "invalid"
        risk = equity * risk_pct / 100
        q_risk = math.floor(risk / per)
        q_cap = math.floor(equity * max_position_pct / 100 / entry)
        q_lev = math.floor(equity * max_leverage / entry)
        qty = max(0, min(q_risk, q_cap, q_lev))
        why = "risk" if qty == q_risk else "LL-9 position cap" if qty == q_cap else "LL-11"
        return qty, round(per * qty, 2), why
    return size


def base_context(cfg: Any, *, asof: date, edges: dict[str, float] | None,
                 macro: Any = None, sector: Series | None = None,
                 calendar: Any = None) -> ScanContext:
    """The part of the context that config, calendars and bars answer (CLI and trader-core).
    ``cfg`` is a trader Config; ``macro`` a MacroCalendar; ``calendar`` a MarketCalendar."""
    nxt = calendar.next_trading_day(asof) if calendar is not None else None
    macro_next: str | None = None
    if macro is not None and nxt is not None:
        macro_next = ("FOMC" if macro.is_fomc(nxt) else
                      "CPI/jobs" if macro.is_cpi_or_jobs(nxt) else None)
    up = sector_trend(sector)
    groups = {name: (frozenset(cfg.canonical(t) for t in g.tickers), int(g.max_open))
              for name, g in cfg.universe.correlated_groups.items()}

    def sessions_until(d: date) -> int:
        if calendar is None:
            return (d - asof).days
        n, cur = 0, asof
        while cur < d and n < 30:
            cur = calendar.next_trading_day(cur)
            n += 1
        return n

    return ScanContext(
        tradable=frozenset(cfg.universe.tickers), next_session=nxt,
        sessions_until=sessions_until, macro_next=macro_next,
        macro_known=macro is not None and nxt is not None, groups=groups,
        sector_up={t: up for t in cfg.filters.smh_check_for}, edges=edges)


# Every decision check, for the console legend and the docs (key, label, hard, rule, why).
CATALOG: tuple[tuple[str, str, bool, str, str], ...] = (
    ("direction", "Long-only stock", True, "ADR-008",
     "Bearish ideas become puts or put spreads, never a short sale"),
    ("tradable", "On the trading list", True, "UR-1",
     "scanner.symbols are research only; only universe.tickers can be traded"),
    ("earnings", "No earnings within 3 sessions", True, "CK-1, XR-6",
     "A swing trade never holds through an earnings release"),
    ("locks", "No wash-sale or loss-streak lock", True, "WS-1, RC-4",
     "Locked tickers cannot be bought (tax and revenge-trading rules)"),
    ("held", "Not already in a position", True, "DR-4",
     "No second entry and no averaging (DR-1)"),
    ("correlation", "Room in the correlated group", True, "LL-10",
     "At most one of NVDA/AVGO/TSM at once; the higher-ranked idea takes the slot"),
    ("size", "At least one share within the risk rules", True, "LL-1, LL-9, LL-11",
     "Size from the stop at the current risk %, capped at 25% of equity and 1x leverage"),
    ("macro", "No FOMC/CPI/jobs day next session", False, "TW-5",
     "Scheduled releases move the whole market; wait or size down"),
    ("trend", "Daily trend aligned", False, "TS-6", "Trade with the 50/200-day trend"),
    ("weekly", "Weekly trend aligned", False, "TS-17",
     "The higher timeframe agrees (rising 10-week average)"),
    ("market", "Market (SPY) trend aligned", False, "MF-1",
     "Most stocks follow the index; longs work best above SPY's 50-day average"),
    ("sector", "Sector (SMH) trend aligned", False, "MF-2", "Semis need their sector too"),
    ("rs", "Relative strength vs SPY", False, "TS-6", "Leaders outperform the index"),
    ("room", "Room to the next level >= 1.5R", False, "CK-8",
     "Overhead resistance closer than 1.5R caps the trade"),
    ("overheated", "Trend not overheated (ADX < 35)", False, "TS-8",
     "Very strong trends at the signal lost money on the history"),
    ("edge", "Technique has a positive measured edge", False, "TS-12",
     "Walk-forward average R of the technique (scan-eval)"),
)
