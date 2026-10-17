"""Plain-text summary of ``momentum-eval`` (MO-11..MO-13, MO-24)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _s(st: Mapping[str, Any] | None) -> str:
    if not st or not st.get("trades"):
        return "no trades"
    ci = st.get("ci95")
    return (f"{st['trades']} tr, win {st['win_rate']:.0%}, avg {st['avg_r']:+.2f}R, "
            f"PF {st.get('profit_factor')}, hold {st['avg_sessions']}"
            + (f", CI [{ci[0]:+.2f}, {ci[1]:+.2f}]" if ci else ""))


def _c(st: Mapping[str, Any] | None) -> str:
    if not st:
        return "-"
    return (f"CAGR {st.get('cagr_pct')}%, Sharpe {st.get('sharpe')}, max DD "
            f"{st.get('max_dd_pct')}%, total {st.get('total_return_pct')}%")


def eval_text(res: Mapping[str, Any]) -> str:
    if res.get("error"):
        return f"momentum-eval: {res['error']}"
    per = res["period"]
    lines = [f"Momentum walk-forward {per['start']} .. {per['end']} ({per['sessions']} sessions,"
             f" {per['symbols']} symbols; out-of-sample from {per['split']}); "
             f"{res['signals']} armed setups"]
    lines.append("Setups (default exit policy; IS | OOS):")
    for name, r in res["setups"].items():
        pol = r["default_policy"]
        h = r["policies"][pol]
        best = max(r["policies"].items(), key=lambda kv: (kv[1]["all"].get("avg_r") or -9))
        lines.append(f"  {name} [{pol}] {'GATE PASS' if r['gate']['pass'] else 'unproven'}")
        lines.append(f"    IS  {_s(h['is'])}")
        lines.append(f"    OOS {_s(h['oos'])}")
        lines.append(f"    best policy over all: {best[0]} {_s(best[1]['all'])}")
        ab = r["ablation"]
        if ab.get("no_leader_filter"):
            lines.append(f"    without the leader filter: {_s(ab['no_leader_filter']['all'])}")
        lines.append(f"    without the regime filter: {_s(ab['no_regime_filter']['all'])}")
        lines.append(f"    pessimistic entry day: {_s(ab['pessimistic_entry_day']['all'])}")
    lines.append("Alert stream as a portfolio (slots, risk sizing, heat cap, no leverage):")
    for k, st in res["alert_stream"].items():
        g = st.get("gate") or {}
        lines.append(f"  {k}: {_c(st)}, exposure {st.get('avg_exposure')}, trades "
                     f"{st.get('taken')} (skipped {st.get('skipped')}); SV-3 "
                     f"{'pass' if g.get('pass') else 'fail'}")
        for half in ("is", "oos"):
            if st.get(half):
                lines.append(f"    {half.upper()}: {_c(st[half])}")
    rot = res["rotation"]
    lines.append("Rotation model (top 10, inverse-vol, rebalanced every 2nd week; IS | OOS):")
    for k, st in sorted(rot["runs"].items(), key=lambda kv: -(kv[1].get("is") or {}).get(
            "cagr_pct", -999)):
        lines.append(f"  {k}: IS {_c(st.get('is'))} | OOS {_c(st.get('oos'))}")
    sel = rot["selected"]
    lines.append(f"  selected on the in-sample half: {sel['name']} (factors "
                 f"{', '.join(sel['factors'])}, regime filter {sel['regime_filter']}); its "
                 f"out-of-sample CAGR ranks {sel['oos_rank']}. Gate (MO-25): "
                 f"{'PASS' if rot['gate']['pass'] else 'fail'} "
                 + ", ".join(k for k, ok in rot["gate"]["checks"].items() if not ok))
    lines.append("Benchmarks, same sessions:")
    for k, st in res["benchmarks"].items():
        lines.append(f"  {k}: {_c(st)}" + (f" | OOS {_c(st['oos'])}" if st.get("oos") else ""))
    lines.append("Setups passing the alert gate (MO-14): "
                 + (", ".join(res["gate_pass"]) or "none"))
    lines.append(res.get("survivorship_note", ""))
    return "\n".join(lines)
