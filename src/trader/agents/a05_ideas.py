"""After-close ideas for the next session (A5-1 preview): journal rows, grading, owner alert.

Pure functions; ``TradingEngine.job_next_plan`` fetches the data, runs the preview plans and
writes what these return. One row per ticker per run in ``next_day_ideas``: the code screen
(session_screen), the plan picks per window, and, after the planned session, the outcome on
that session's daily bar so the owner can see whether the ideas worked.
"""

from __future__ import annotations

from datetime import date, datetime
from html import escape
from typing import Any

from trader.models.messages import Bar, GamePlan, GamePlanItem

PICK, WATCH, SKIP, OFF = "pick", "watch", "skip", "off"


def idea_rows(run_at: datetime, plan_for: date, screens: dict[str, dict[str, Any]],
              news: dict[str, dict[str, Any] | None], plans: dict[str, GamePlan],
              off_limits: dict[str, str], passed: dict[str, str] | None = None
              ) -> list[dict[str, Any]]:
    """Rows for ``next_day_ideas``, ranked: picks first, then by readiness score.
    ``off_limits``: hard exclusions (locks, blocked); ``passed``: tickers Qwen vetoed."""
    passed = passed or {}
    picks: dict[str, dict[str, GamePlanItem]] = {}
    for window, plan in plans.items():
        for item in plan.watch:
            picks.setdefault(item.ticker, {})[window] = item
    rows: list[dict[str, Any]] = []
    for t, scr in screens.items():
        nf = news.get(t) or {}
        mine = picks.get(t, {})
        status = PICK if mine else OFF if t in off_limits else SKIP if t in passed else WATCH
        row: dict[str, Any] = {
            "run_at": run_at, "plan_for": plan_for, "session_date": date.fromisoformat(
                scr["session_date"]), "ticker": t, "status": status,
            "score": int(scr["readiness_score"]), "close": scr["close"],
            "change_pct": scr["change_pct"], "range_pos": scr["range_pos"],
            "volume_ratio": scr["volume_vs_50d_avg"], "trend_up": scr["trend_up"],
            "atr_to_prior_high": scr["atr_to_prior_high"],
            "atr_to_20d_closing_high": scr["atr_to_20d_closing_high"],
            "atr_to_ema20d": scr["atr_to_ema20d"],
            "news_tone": nf.get("tone"), "news_catalyst": (nf.get("catalyst") or None),
            "note": off_limits.get(t) if status == OFF else passed.get(t) if status == SKIP
            else next((i.why for i in mine.values()), None)}
        for window in ("morning", "afternoon"):
            pick = mine.get(window)
            row[f"{window}_setup"] = pick.setup if pick else None
            row[f"{window}_trigger"] = pick.trigger if pick else None
            row[f"{window}_stop"] = pick.stop if pick else None
            row[f"{window}_target"] = pick.target_1 if pick else None
        rows.append(row)
    order = {PICK: 0, WATCH: 1, SKIP: 2, OFF: 3}
    rows.sort(key=lambda r: (order[r["status"]], -r["score"], r["ticker"]))
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return rows


def grade(row: dict[str, Any], bar: Bar) -> dict[str, Any]:
    """Outcome of one idea on the planned session's daily bar. Daily bars cannot order the
    high and the low, so a day that crossed both T1 and the stop is ``both``."""
    out: dict[str, Any] = {"day_high": bar.h, "day_low": bar.l, "day_close": bar.c}
    window = "morning" if row.get("morning_setup") else "afternoon" \
        if row.get("afternoon_setup") else None
    if window is None:
        out["outcome"] = "no_pick"
        return out
    trig, stop, t1 = (row[f"{window}_{k}"] for k in ("trigger", "stop", "target"))
    triggered = bar.h >= trig
    reached, stopped = triggered and bar.h >= t1, triggered and bar.l <= stop
    out.update(triggered=triggered, reached_t1=reached, hit_stop=stopped,
               outcome="not_triggered" if not triggered else "both" if reached and stopped
               else "t1" if reached else "stopped" if stopped else "open")
    return out


def grade_summary(graded: list[dict[str, Any]]) -> str | None:
    picks = [g for g in graded if g.get("outcome") not in (None, "no_pick")]
    if not picks:
        return None
    n = len(picks)
    trig = sum(1 for g in picks if g.get("triggered"))
    t1 = sum(1 for g in picks if g["outcome"] == "t1")
    st = sum(1 for g in picks if g["outcome"] == "stopped")
    both = sum(1 for g in picks if g["outcome"] == "both")
    text = f"{trig}/{n} triggered · {t1} reached T1 · {st} stopped"
    return text + (f" · {both} hit both" if both else "")


def _fmt(v: float) -> str:
    return f"{v:,.2f}"


def alert_text(plan_for: date, session_date: date, market: dict[str, dict[str, Any]],
               plans: dict[str, GamePlan], rows: list[dict[str, Any]],
               off_limits: dict[str, str], last_result: str | None,
               passed: dict[str, str] | None = None) -> str:
    """Telegram HTML (``parse_mode=HTML``); WhatsApp strips the tags. Fits one message."""
    by_t = {r["ticker"]: r for r in rows}
    out = [f"<b>📋 Next-day ideas · {plan_for:%a %b %d}</b>",
           f"<i>from the {session_date:%b %d} close</i>"]
    mkt = " · ".join(f"{s} {m['change_pct']:+.2f}%" for s, m in market.items())
    bias = next((p.market_bias for p in plans.values()), "mixed")
    if mkt:
        out.append(f"Market: {escape(mkt)} · bias {bias.replace('_', ' ')}")
    for window, title in (("morning", "🌅 Morning 9:35–10:15"),
                          ("afternoon", "🌇 Afternoon 3:05–3:40")):
        plan = plans.get(window)
        if plan is None:
            continue
        out.append("")
        out.append(f"<b>{title}</b>")
        if not plan.watch:
            out.append("  no setup worth watching")
        for i, w in enumerate(plan.watch, 1):
            r = by_t.get(w.ticker, {})
            vol = r.get("volume_ratio")
            out.append(f"{i}. <b>{escape(w.ticker)}</b> {escape(w.setup)} · score "
                       f"{r.get('score', '?')}")
            out.append(f"   buy &gt; {_fmt(w.trigger)} · stop {_fmt(w.stop)} · T1 "
                       f"{_fmt(w.target_1)}")
            facts = [f"{r['change_pct']:+.1f}% day" if r.get("change_pct") is not None
                     else "", f"vol {vol:.1f}x" if vol else "",
                     f"news {r['news_tone']}" if r.get("news_tone") else ""]
            out.append("   " + escape(" · ".join(f for f in facts if f)))
    if off_limits:
        out.append("")
        out.append("<b>⛔ Off-limits</b> " + escape("; ".join(
            f"{t}: {why[:60]}" for t, why in off_limits.items())))
    if passed:
        out.append("<b>Passed</b> " + escape("; ".join(
            f"{t}: {why[:60]}" for t, why in passed.items())))
    if last_result:
        out.append("")
        out.append(f"<b>Last ideas:</b> {escape(last_result)}")
    out.append("")
    out.append("<i>Ideas, not orders: entries still need the live trigger, checklist, review "
               "and risk. Refreshed 9:00 / 2:45. Reply PLAN to rebuild.</i>")
    return "\n".join(out)
