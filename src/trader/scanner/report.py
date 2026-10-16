"""Text for the console and Telegram (TS-14)."""

from __future__ import annotations

from datetime import date
from typing import Any

from trader.scanner.inplay import InPlay
from trader.scanner.techniques import Idea

VERDICT = {"go": "GO", "caution": "CAUTION", "blocked": "BLOCKED", "watch": "WATCH",
           "options": "OPTIONS"}


def idea_line(d: Idea) -> str:
    from trader.scanner.checks import check_summary

    conf = f" +{', '.join(d.confluence)}" if d.confluence else ""
    room = "open sky" if d.room_r is None else f"{d.room_r:.1f}R to next level"
    tag = VERDICT.get(d.verdict or "", "LONG" if d.side == "long" else "BEARISH")
    head = f"{d.score:3d}  {tag:<8}{d.symbol:<6} {d.technique}{conf}"
    if d.side == "short":
        plan = (f"      {d.plan or 'bearish view: puts or a put spread, never stock (ADR-008)'}"
                f"\n      level {d.trigger:.2f} | invalidation {d.stop:.2f} | "
                f"T1 {d.t1:.2f} | T2 {d.t2:.2f}")
    else:
        entry = "at the open, ref" if d.entry == "open" else "buy-stop"
        plan = (f"      {entry} {d.trigger:.2f} | stop {d.stop:.2f} | T1 {d.t1:.2f} | "
                f"T2 {d.t2:.2f} | R {d.risk:.2f} | {room}")
        if d.shares is not None:
            plan += f"\n      size {d.shares} sh, ${d.risk_usd or 0:,.0f} at risk"
    lines = [head, plan]
    if d.checks:
        lines.append(f"      {check_summary(d)}")
    lines.append(f"      {d.why}")
    return "\n".join(lines)


def blocked_line(d: Idea) -> str:
    why = "; ".join(f"{c['label']}: {c['detail']}" for c in d.checks
                    if c["hard"] and c["ok"] is False)
    return f"  {d.symbol:<6} {d.technique} ({d.score}) - {why}"


def play_line(p: InPlay) -> str:
    lv = p.levels
    return (f"{p.rank:2d}. {p.symbol:<6} exp. range {p.expected_range_pct:.1f}% "
            f"(ATR {p.atr_pct:.1f}%, RVOL {p.rvol:.1f}) bias {p.bias}\n"
            f"      PDH {lv['prior_high']:.2f} | PDL {lv['prior_low']:.2f} | "
            f"PDC {lv['prior_close']:.2f} | 20d {lv['low_20d']:.2f}-{lv['high_20d']:.2f}\n"
            f"      {p.plan}")


def render(asof: date | None, ideas: list[Idea], plays: list[InPlay], skipped: dict[str, str],
           *, top_n: int, in_play_n: int, min_score: int, summary: str | None = None,
           edges_from: str | None = None) -> str:
    """Ideas are grouped by verdict when decision checks ran (TS-18): actionable longs first,
    then bearish views (options only), research-only names, and blocked ideas with the rule."""
    ok = [d for d in ideas if d.score >= min_score]
    day = asof.isoformat() if asof else "latest session"
    out = [f"Technical scan | {day}"]
    if summary:
        out += ["", summary]
    act = [d for d in ok if d.verdict in (None, "go", "caution")][:top_n]
    out += ["", f"SWING IDEAS (score >= {min_score}, next-session entries)"]
    out += [idea_line(d) for d in act] or ["  none qualify today: no trade is a position"]
    for title, verdict in (("BEARISH VIEWS (options only: the stock side is long-only)",
                            "options"), ("WATCH ONLY (not on the trading list)", "watch")):
        rows = [d for d in ok if d.verdict == verdict][:top_n]
        if rows:
            out += ["", title] + [idea_line(d) for d in rows]
    blocked = [d for d in ok if d.verdict == "blocked"]
    if blocked:
        out += ["", "BLOCKED BY A RULE"] + [blocked_line(d) for d in blocked]
    hidden = len(ideas) - len(ok)
    if hidden > 0:
        out.append(f"  ({hidden} lower-scoring idea(s) not shown; --all lists them)")
    out += ["", "DAY WATCHLIST (in play next session; take entries from the intraday playbook)"]
    out += [play_line(p) for p in plays[:in_play_n]] or ["  no data"]
    if skipped:
        out += ["", "Skipped: " + "; ".join(f"{k} ({v})" for k, v in sorted(skipped.items()))]
    out += ["", "Score: measured edge "
            + (f"from {edges_from}" if edges_from else "not loaded (run scan-eval)")
            + " + trend, market, relative strength, volume, room; ideas only, not advice."]
    return "\n".join(out)


def as_json(asof: date | None, ideas: list[Idea], plays: list[InPlay], skipped: dict[str, str],
            **meta: Any) -> dict[str, Any]:
    """The scan as data (``--json``, and ``reports/technical_scan.json`` for the console).
    ``meta``: generated, source, edges_from, min_score, top_n, context_note."""
    return {"asof": asof.isoformat() if asof else None, **meta,
            "swing": [d.as_dict() for d in ideas], "in_play": [p.as_dict() for p in plays],
            "skipped": skipped}


def write_latest(path: str, payload: dict[str, Any]) -> None:
    """TS-19: the newest scan for the Owner Console (reports/ is read-only there)."""
    import json
    from pathlib import Path

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")
    tmp.replace(out)


def eval_text(rep: dict[str, Any], in_play: dict[str, Any]) -> str:
    rows = [f"{'technique':<16} {'side':<5} {'ideas':>5} {'filled':>6} {'hit1R':>6} "
            f"{'avgR':>6} {'totalR':>7} {'PF':>5}"]
    for t in rep["techniques"]:
        pf = t["profit_factor"]
        rows.append(f"{t['technique']:<16} {t['side']:<5} {t['ideas']:>5} {t['filled']:>6} "
                    f"{(t['hit_1r'] or 0):>6.2f} {(t['avg_r'] or 0):>+6.2f} "
                    f"{t['total_r']:>+7.1f} {'-' if pf is None else f'{pf:.2f}':>5}"
                    + ("  WEAK" if t["weak"] else ""))
    sw = rep["styles"].get("swing", {})
    rows += ["", f"All swing ideas: {sw.get('filled', 0)} filled of {sw.get('ideas', 0)}, "
                 f"hit +1R {sw.get('hit_1r')}, avg R {sw.get('avg_r')}, PF "
                 f"{sw.get('profit_factor')}"]
    rows += ["By score:"] + [f"  {b['bucket']:>7}: {b['filled']:>4} filled, avg R {b['avg_r']}, "
                             f"hit +1R {b['hit_1r']}" for b in rep["score_buckets"]]
    if in_play.get("days"):
        rows += ["", f"Day watchlist: top {in_play['top_n']} moved "
                     f"{in_play['top_avg_range_pct']}% vs {in_play['all_avg_range_pct']}% for "
                     f"the average ticker (lift {in_play['lift']}x, beat the average on "
                     f"{in_play['days_top_beat_avg']:.0%} of {in_play['days']} days, "
                     f"calibration {in_play['calibration']})"]
    else:
        rows += ["", "Day watchlist: not graded (needs more tickers with 210+ sessions than "
                     "the watchlist size; lower --top or add scanner.symbols)"]
    if rep["weak"]:
        rows += ["", f"Weak (negative over >= {rep['min_samples']} filled ideas): "
                 + ", ".join(rep["weak"]) + ". Consider scanner.disabled."]
    return "\n".join(rows)
