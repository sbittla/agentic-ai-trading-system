"""Operations checks and small daily analyses (REQUIREMENTS 1.3).

Pure functions (no I/O except the journal passed in), so each is unit tested:

- OPS-1 ``compare_restore``: a restored backup against production row counts.
- OPS-2 ``DRILL_STEPS`` / ``record_check``: the monthly emergency drill.
- PT-4 ``strict_paper``: Alpaca paper fills re-checked against the replay fill model.
- DQ-6 ``divergence_row`` / DQ-7 ``data_gate``: IEX against consolidated (SIP) bars.
- TX-1 ``tax_set_aside``: realized short-term gains and an estimated set-aside.
- UN-1 ``rank_in_play`` / UN-2 ``rotation_candidates``.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from trader.journal import schema as s
from trader.journal.store import SqlJournal
from trader.models.messages import Bar
from trader.timeutil import to_et

# --- OPS-1 ------------------------------------------------------------------------------------


def compare_restore(restored: dict[str, Any], live_counts: dict[str, int],
                    live_last_event: datetime | None, max_age_h: float = 48
                    ) -> tuple[bool, list[str]]:
    """``restored`` = {"counts": {table: n}, "last_event": iso | None} from the scratch
    restore. Passes when every live table is present, no restored table has more rows than
    production, and the restored journal's last event is at most ``max_age_h`` hours older
    than production's."""
    problems: list[str] = []
    counts: dict[str, int] = restored.get("counts") or {}
    for t, n in live_counts.items():
        if t not in counts:
            problems.append(f"{t}: missing from the backup")
        elif counts[t] > n:
            problems.append(f"{t}: backup has {counts[t]} rows > production {n}")
    last = restored.get("last_event")
    if live_last_event is not None:
        if not last:
            problems.append("backup has no events")
        else:
            age = live_last_event - datetime.fromisoformat(str(last))
            if age > timedelta(hours=max_age_h):
                problems.append(f"backup is {age.total_seconds() / 3600:.0f} h behind "
                                f"production (> {max_age_h:.0f} h)")
    return not problems, problems


# --- OPS-2 ------------------------------------------------------------------------------------

DRILL_STEPS = (
    ("kill_switch", "Send KILL (Telegram or ./run.sh -> trader kill): open entry orders "
                    "cancelled within 10 s, broker-side stops kept?"),
    ("flatten", "Send FLATTEN, then the confirmation code: the paper position closed?"),
    ("network", "Pull the network cable for 2 minutes with a position open: the stop is "
                "still at Alpaca (check the Alpaca app), and after reconnect the reconcile "
                "health is green?"),
)


def record_check(journal: SqlJournal, kind: str, passed: bool, detail: dict[str, Any],
                 now: datetime) -> None:
    journal.insert(s.ops_checks, {"ts": now, "kind": kind, "passed": passed, "detail": detail})


def last_check(journal: SqlJournal, kind: str) -> dict[str, Any] | None:
    rows = [r for r in journal.rows(s.ops_checks, {"kind": kind}) if r.get("passed")]
    return max(rows, key=lambda r: r["ts"]) if rows else None


def ops_overdue(journal: SqlJournal, now: datetime, days: int = 35) -> list[str]:
    out = []
    for kind, label in (("restore_test", "restore test (OPS-1): ./run.sh restore-test"),
                        ("drill", "emergency drill (OPS-2): ./run.sh drill")):
        last = last_check(journal, kind)
        if last is None or now - last["ts"] > timedelta(days=days):
            out.append(f"{label} overdue"
                       + (f" (last {to_et(last['ts']):%Y-%m-%d})" if last else " (never)"))
    return out


# --- PT-4 -------------------------------------------------------------------------------------


def strict_paper(positions: list[dict[str, Any]], entry_limits: dict[str, Decimal],
                 bars: dict[str, list[Bar]]) -> dict[str, Any]:
    """Re-check paper results with the replay fill model:
    an entry counts only if price traded *through* the limit (a bar low below it within the
    fill minute); a stop fills at the worse of the reported exit and the next bar's open.
    ``positions``: closed positions (journal rows); ``entry_limits``: position id -> limit;
    ``bars``: ticker -> 1-min bars of the day."""
    n = unfilled = 0
    reported = strict = Decimal(0)
    for p in positions:
        pid, t = p["position_id"], p["ticker"]
        qty = int(p.get("qty") or 0)
        entry, exit_ = Decimal(str(p.get("avg_entry") or 0)), Decimal(str(p.get("avg_exit")
                                                                          or 0))
        pnl = Decimal(str(p.get("pnl") or 0))
        reported += pnl
        n += 1
        bb = bars.get(t, [])
        limit = entry_limits.get(pid, entry)
        opened = p.get("opened_at")
        window = [b for b in bb if opened is not None and opened - timedelta(minutes=1)
                  <= b.ts <= opened]
        if window and not any(Decimal(str(b.l)) < limit for b in window):
            unfilled += 1          # touched but never traded through: no fill in replay
            continue
        if p.get("exit_reason") in ("EXIT_STOP", "EXIT_BREAKEVEN", "EXIT_TRAIL"):
            closed = p.get("closed_at")
            nxt = next((b for b in bb if closed is not None and b.ts >= closed), None)
            if nxt is not None:
                exit_ = min(exit_, Decimal(str(nxt.o)))
        strict += (exit_ - entry) * qty
    return {"entries": n, "would_not_fill": unfilled, "reported_pnl": reported,
            "strict_pnl": strict.quantize(Decimal("0.01")),
            "text": f"Paper fills (PT-4): {n} closed, {unfilled} would not fill under the "
                    f"strict model; P&L strict ${strict:+.2f} vs reported ${reported:+.2f}"}


# --- DQ-6 / DQ-7 ------------------------------------------------------------------------------


def opening_range(bars: Sequence[Bar], minutes: int = 15) -> tuple[float, float] | None:
    rng = [b for b in bars if 0 <= (to_et(b.ts).hour * 60 + to_et(b.ts).minute) - 570
           < minutes]
    if not rng:
        return None
    return max(b.h for b in rng), min(b.l for b in rng)


def divergence_row(d: date, ticker: str, iex: Sequence[Bar], sip: Sequence[Bar],
                   triggers: Sequence[tuple[datetime, float]], tol_pct: float
                   ) -> dict[str, Any]:
    """One DQ-6 row: opening-range difference, IEX volume share, and how many of the day's
    trigger levels the consolidated tape also traded (within ``tol_pct``)."""
    io_, so = opening_range(iex), opening_range(sip)
    diff = None
    if io_ and so and so[0] and so[1]:
        diff = max(abs(io_[0] - so[0]) / so[0], abs(io_[1] - so[1]) / so[1]) * 100
    iv, sv = sum(b.v for b in iex), sum(b.v for b in sip)
    within = 0
    for ts, level in triggers:
        near = [b.h for b in sip if ts - timedelta(minutes=5) <= b.ts <= ts]
        if near and max(near) >= level * (1 - tol_pct / 100):
            within += 1
    return {"date": d, "ticker": ticker,
            "iex_or_high": io_[0] if io_ else None, "sip_or_high": so[0] if so else None,
            "iex_or_low": io_[1] if io_ else None, "sip_or_low": so[1] if so else None,
            "or_diff_pct": round(diff, 4) if diff is not None else None,
            "iex_volume_share_pct": round(iv / sv * 100, 2) if sv else None,
            "setups_checked": len(triggers), "setups_within": within}


def data_gate(rows: list[dict[str, Any]], days: int, tol_pct: float, share_pct: float
              ) -> tuple[bool, str]:
    """DQ-7: at least ``days`` paper days in which opening-range and trigger levels agree
    with SIP within ``tol_pct`` on at least ``share_pct``% of setups (or of ORs when a day
    had no setups)."""
    dates = sorted({r["date"] for r in rows})
    if len(dates) < days:
        return False, f"{len(dates)} of {days} days measured"
    recent = [r for r in rows if r["date"] in set(dates[-days:])]
    checked = sum(int(r.get("setups_checked") or 0) for r in recent)
    within = sum(int(r.get("setups_within") or 0) for r in recent)
    ors = [r for r in recent if r.get("or_diff_pct") is not None]
    or_ok = sum(1 for r in ors if r["or_diff_pct"] <= tol_pct)
    share_setups = within / checked * 100 if checked else 100.0
    share_or = or_ok / len(ors) * 100 if ors else 0.0
    ok = share_setups >= share_pct and share_or >= share_pct
    return ok, (f"setups within {tol_pct}%: {share_setups:.1f}% of {checked}; opening ranges "
                f"within: {share_or:.1f}% of {len(ors)} (need {share_pct}%)")


# --- TX-1 -------------------------------------------------------------------------------------


def tax_set_aside(closed: list[dict[str, Any]], year: int, rate_pct: float) -> str:
    ytd = sum((Decimal(str(r.get("pnl") or 0)) for r in closed
               if r.get("closed_at") is not None and r["closed_at"].year == year
               and r.get("route") != "paper"), Decimal(0))
    aside = max(Decimal(0), ytd) * Decimal(str(rate_pct)) / 100
    return (f"Tax (TX-1, estimate, not tax advice): realized short-term gains {year} YTD "
            f"${ytd:+,.2f}; set aside ~${aside:,.2f} at {rate_pct:g}%")


# --- UN-1 / UN-2 ------------------------------------------------------------------------------


def rank_in_play(snaps: dict[str, dict[str, Any]], news: set[str], top_n: int,
                 on_list: set[str]) -> list[dict[str, Any]]:
    """Pre-market gap and volume ranking from Alpaca snapshots (watch only, UN-1)."""
    out: list[dict[str, Any]] = []
    for sym, sn in snaps.items():
        prev = (sn.get("prevDailyBar") or {}).get("c")
        last = ((sn.get("latestTrade") or {}).get("p") or (sn.get("minuteBar") or {}).get("c"))
        if not prev or not last:
            continue
        gap = (float(last) / float(prev) - 1) * 100
        today_v = float((sn.get("dailyBar") or {}).get("v") or 0)
        prev_v = float((sn.get("prevDailyBar") or {}).get("v") or 0)
        ratio = today_v / prev_v if prev_v else 0.0
        score = abs(gap) * (1 + math.log1p(ratio * 10)) + (1.0 if sym in news else 0.0)
        out.append({"ticker": sym, "gap_pct": round(gap, 2), "pm_volume_ratio": round(ratio,
                                                                                     3),
                    "news": sym in news, "score": score, "on_list": sym in on_list})
    out.sort(key=lambda r: -float(r["score"]))
    for i, r in enumerate(out[:top_n], start=1):
        r["rank"] = i
    return out[:top_n]


def rotation_candidates(rows: list[dict[str, Any]], on_list: set[str], days: int,
                        lookback_dates: Sequence[date]) -> list[str]:
    """UN-2: names in play on at least ``days`` of the last sessions and not on the list."""
    recent = set(lookback_dates)
    c = Counter(r["ticker"] for r in rows if r.get("date") in recent
                and r["ticker"] not in on_list)
    return sorted(t for t, n in c.items() if n >= days)
