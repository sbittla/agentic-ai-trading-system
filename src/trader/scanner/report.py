"""Text for the console and Telegram (TS-14)."""

from __future__ import annotations

from datetime import date
from typing import Any

from trader.scanner.inplay import InPlay
from trader.scanner.techniques import Idea

ARROW = {"long": "LONG ", "short": "SHORT"}


def idea_line(d: Idea) -> str:
    entry = "buy-stop" if d.side == "long" else "sell-stop"
    if d.entry == "open":
        entry = "at the open, ref"
    room = "open sky" if d.room_r is None else f"{d.room_r:.1f}R to next level"
    conf = f" +{', '.join(d.confluence)}" if d.confluence else ""
    return (f"{d.score:3d}  {ARROW[d.side]} {d.symbol:<6} {d.technique}{conf}\n"
            f"      {entry} {d.trigger:.2f} | stop {d.stop:.2f} | T1 {d.t1:.2f} | "
            f"T2 {d.t2:.2f} | R {d.risk:.2f} | {room}\n"
            f"      {d.why}")


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
    shown = [d for d in ideas if d.score >= min_score][:top_n]
    day = asof.isoformat() if asof else "latest session"
    out = [f"Technical scan | {day}"]
    if summary:
        out += ["", summary]
    out += ["", f"SWING IDEAS (score >= {min_score}, next-session entries)"]
    out += [idea_line(d) for d in shown] or ["  none qualify today: no trade is a position"]
    hidden = len(ideas) - len(shown)
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


def as_json(asof: date | None, ideas: list[Idea], plays: list[InPlay], skipped: dict[str, str]
            ) -> dict[str, Any]:
    return {"asof": asof.isoformat() if asof else None,
            "swing": [d.as_dict() for d in ideas], "in_play": [p.as_dict() for p in plays],
            "skipped": skipped}


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
