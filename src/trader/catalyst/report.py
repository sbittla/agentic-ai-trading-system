"""Text and JSON for catalyst outlooks (NC-27..NC-29): CLI, Telegram and the console."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

HORIZON_NAMES = {1: "1 day", 5: "1 week", 10: "2 weeks", 21: "1 month"}
VEHICLES = {"stock": "stock (long)", "call_debit_spread": "call debit spread",
            "put_debit_spread": "put debit spread (no stock short)",
            "wait": "wait for confirmation", "none": "no position"}
DISCLAIMER = "Not advice: ranges are probabilities from past events, not promises."


def hname(h: int) -> str:
    return HORIZON_NAMES.get(h, f"{h} sessions")


def render_one(o: Mapping[str, Any], track: Mapping[str, Any] | None = None) -> str:
    """The message format at the top of NEWS_CATALYST_REQUIREMENTS.md."""
    t = o["ticker"]
    lines = [f"{t} · catalyst {o['strength']:.0f}/100 {o['label']} · {o['ctype'].lower()} "
             f"({o['direction']}) · {o['items']} items, {o['sources']} sources, novelty "
             f"{o['novelty']:.2f}",
             f"  {o['headline'][:160]}"]
    bits = [f"News {o['sentiment']:+.2f}"]
    soc = o.get("social")
    if soc:
        z = soc.get("mention_z")
        bits.append(f"Social {soc['net_sentiment']:+.2f}, mentions "
                    + (f"z {z:+.1f}" if z is not None else f"{soc['mentions']}")
                    + f" ({o['agreement'].lower()})")
    else:
        bits.append("Social: no data")
    rx = o.get("reaction")
    if rx:
        bits.append(f"Day-0 {rx['ret_pct']:+.1f}% (excess {rx['abnormal_pct']:+.1f}%)"
                    + (f" on {rx['rvol']:.1f}x volume" if rx.get("rvol") else ""))
    lines.append("  " + " · ".join(bits))
    if o.get("priced_in"):
        lines.append("  Largely priced in: the day-0 move already exceeds the usual range.")
    if o.get("crowding"):
        lines.append(f"  Crowding warning: very loud, one-sided {o['crowding']} chatter while "
                     "price is stretched.")
    lines.append(f"  {'Horizon':<9} {'P(up)':>6} {'Typical':>8}  Likely range (10-90%)   Basis")
    for v in o["horizons"]:
        lines.append(f"  {hname(v['h']):<9} {v['p_up']:>6.0%} {v['median_pct']:>+7.1f}%  "
                     f"{v['p10_pct']:+6.1f}% … {v['p90_pct']:+6.1f}%   "
                     f"{v['confidence'].lower()}, n={v['n']}")
    tl = o.get("tally") or {}
    lines.append(f"  Confluence: {tl.get('supports', 0)} support, {tl.get('against', 0)} "
                 f"against, {tl.get('neutral', 0)} neutral")
    for c in o.get("confluence", []):
        if c["verdict"] != "neutral":
            lines.append(f"    {'+' if c['verdict'] == 'supports' else '-'} {c['group']}: "
                         f"{c['name']} {c['detail']}")
    view = o["view"]
    vh = f", {hname(view['horizon'])}" if view.get("horizon") else ""
    lines.append(f"  View: {view['stance']}{vh} · {VEHICLES.get(view['vehicle'], view['vehicle'])}"
                 f" · {view['why']}")
    pl = view.get("plan") or {}
    if pl:
        size = f" · {pl['qty']} sh, risk ${pl['risk_usd']:.0f} ({pl['size_binding']})" \
            if pl.get("qty") else ""
        lines.append(f"  Plan: trigger {pl['trigger']:.2f} · invalidation "
                     f"{pl['invalidation']:.2f} · T1 {pl['t1']:.2f} · T2 {pl['t2']:.2f}{size}")
    for sc in o.get("scenarios", []):
        lines.append(f"  {sc}")
    cal = "calibrated" if o.get("calibrated") else "UNCALIBRATED (track record still building)"
    rec = ""
    if track:
        bits2 = [f"{hname(int(h))} {m['graded']} graded, Brier {m['brier']:.3f}, range hit "
                 f"{m['coverage']:.0%}" for h, m in track.items() if m.get("graded")]
        rec = (" · " + "; ".join(bits2[:2])) if bits2 else ""
    lines.append(f"  {cal}{rec}. {DISCLAIMER}")
    return "\n".join(lines)


def render(outlooks: Sequence[Mapping[str, Any]], *, as_of: str, track: Mapping[str, Any] | None
           = None, top_n: int = 5) -> str:
    if not outlooks:
        return (f"News catalysts {as_of}: no live catalyst on the watchlist "
                "(strength below the threshold and no hard event in the last 72 hours).")
    head = f"News catalysts {as_of}: {len(outlooks)} live, strongest first"
    return "\n\n".join([head] + [render_one(o, track) for o in outlooks[:top_n]])


def write_latest(path: str, payload: Mapping[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")
    tmp.replace(p)


def as_json(outlooks: Sequence[Mapping[str, Any]], *, as_of: str, generated: datetime,
            source: str, gate: Mapping[str, Any] | None, track: Mapping[str, Any] | None,
            notes: Sequence[str] = (), live: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {"as_of": as_of, "generated": generated.isoformat(), "source": source,
            "outlooks": list(outlooks), "gate": gate, "track": track, "live": live,
            "notes": list(notes)}


def load_json(path: str) -> dict[str, Any] | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None
