"""Server-rendered SVG charts (no third-party scripts, UI-21): candles with VWAP, the
opening range, prior-day levels and trade markers (UI-4, UI-9), and the options payoff view
(OP-17). SVG presentation attributes are used instead of inline styles (CSP style-src
'self')."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from html import escape
from typing import Any

W, H, PAD_L, PAD_R, PAD_T, PAD_B = 760, 320, 8, 64, 10, 22


def _scale(lo: float, hi: float, top: float, bottom: float) -> Callable[[float], float]:
    span = hi - lo or 1.0
    return lambda v: bottom - (v - lo) / span * (bottom - top)


def candles(
    bars: Sequence[dict[str, Any]],
    *,
    vwap: Sequence[float | None] = (),
    levels: dict[str, float | None] | None = None,
    opening_range: Sequence[float] | None = None,
    markers: Sequence[dict[str, Any]] = (),
    title: str = "",
) -> str:
    """``markers``: {"i": bar index, "price", "kind": buy|sell|stop, "label"}."""
    if not bars:
        return '<p class="muted">no bars</p>'
    lv = {k: v for k, v in (levels or {}).items() if v}
    vals = (
        [b["h"] for b in bars]
        + [b["l"] for b in bars]
        + list(lv.values())
        + [v for v in vwap if v]
        + list(opening_range or [])
    )
    lo, hi = min(vals), max(vals)
    pad = (hi - lo) * 0.04 or 0.5
    y = _scale(lo - pad, hi + pad, PAD_T, H - PAD_B)
    n = len(bars)
    step = (W - PAD_L - PAD_R) / max(n, 1)
    bw = max(1.0, step * 0.6)

    def x(i: int) -> float:
        return PAD_L + step * i + step / 2

    parts = [
        f'<svg class="chart" viewBox="0 0 {W} {H}" role="img" '
        f'aria-label="{escape(title or "price chart")}">',
        f'<rect x="0" y="0" width="{W}" height="{H}" class="chart-bg"/>',
    ]
    for name, v in lv.items():
        yy = y(v)
        parts.append(
            f'<line x1="{PAD_L}" x2="{W - PAD_R}" y1="{yy:.1f}" y2="{yy:.1f}" '
            f'class="level" stroke-dasharray="2 4"/>'
            f'<text x="{W - PAD_R + 4}" y="{yy + 4:.1f}" class="lbl">'
            f"{escape(name)} {v:.2f}</text>"
        )
    if opening_range and len(opening_range) == 2:
        y1, y2 = y(max(opening_range)), y(min(opening_range))
        parts.append(
            f'<rect x="{PAD_L}" y="{y1:.1f}" width="{W - PAD_L - PAD_R}" '
            f'height="{max(y2 - y1, 1):.1f}" class="orange"/>'
        )
    for i, b in enumerate(bars):
        up = b["c"] >= b["o"]
        cls = "up" if up else "dn"
        xx = x(i)
        top, bot = y(max(b["o"], b["c"])), y(min(b["o"], b["c"]))
        parts.append(
            f'<line x1="{xx:.1f}" x2="{xx:.1f}" y1="{y(b["h"]):.1f}" '
            f'y2="{y(b["l"]):.1f}" class="wick-{cls}" stroke-width="1"/>'
            f'<rect x="{xx - bw / 2:.1f}" y="{top:.1f}" width="{bw:.1f}" '
            f'height="{max(bot - top, 1):.1f}" class="c-{cls}"/>'
        )
    pts = [f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(vwap) if v]
    if len(pts) > 1:
        parts.append(
            f'<polyline points="{" ".join(pts)}" class="vwap" stroke-width="1.5"/>'
        )
    for m in markers:
        xx, yy = x(int(m["i"])), y(float(m["price"]))
        kind = m.get("kind", "buy")
        mcls = "m-buy" if kind == "buy" else "m-sell"
        tri = (
            f"{xx:.1f},{yy + 2:.1f} {xx - 5:.1f},{yy + 10:.1f} {xx + 5:.1f},{yy + 10:.1f}"
            if kind == "buy"
            else f"{xx:.1f},{yy - 2:.1f} {xx - 5:.1f},{yy - 10:.1f} {xx + 5:.1f},{yy - 10:.1f}"
        )
        parts.append(
            f'<polygon points="{tri}" class="{mcls}"><title>'
            f"{escape(str(m.get('label', kind)))}</title></polygon>"
        )
    parts.append(
        f'<text x="{PAD_L}" y="{H - 6}" class="lbl">{escape(bars[0]["t"][11:16])}'
        f'</text><text x="{W - PAD_R - 40}" y="{H - 6}" class="lbl">'
        f"{escape(bars[-1]['t'][11:16])}</text></svg>"
    )
    return "".join(parts)


def payoff(
    prices: Sequence[float],
    at_expiry: Sequence[float],
    at_exit: Sequence[float],
    marks: dict[str, float],
    band: tuple[float, float] | None = None,
) -> str:
    """OP-17: P/L per contract at expiry and at the planned exit across stock prices."""
    if not prices:
        return '<p class="muted">no payoff</p>'
    vals = list(at_expiry) + list(at_exit) + [0.0]
    lo, hi = min(vals), max(vals)
    pad = (hi - lo) * 0.08 or 10
    y = _scale(lo - pad, hi + pad, PAD_T, H - PAD_B)
    p0, span = prices[0], (prices[-1] - prices[0]) or 1.0

    def x(p: float) -> float:
        return PAD_L + (p - p0) / span * (W - PAD_L - PAD_R)

    parts = [
        f'<svg class="chart" viewBox="0 0 {W} {H}" role="img" aria-label="payoff">',
        f'<rect x="0" y="0" width="{W}" height="{H}" class="chart-bg"/>',
    ]
    if band:
        parts.append(
            f'<rect x="{x(band[0]):.1f}" y="{PAD_T}" '
            f'width="{max(x(band[1]) - x(band[0]), 1):.1f}" height="{H - PAD_T - PAD_B}" '
            'class="band"><title>1x expected move</title></rect>'
        )
    parts.append(
        f'<line x1="{PAD_L}" x2="{W - PAD_R}" y1="{y(0):.1f}" y2="{y(0):.1f}" '
        'class="zero"/>'
    )
    for series, col, name in (
        (at_expiry, "pay-exp", "at expiry"),
        (at_exit, "pay-exit", "at planned exit"),
    ):
        pts = " ".join(f"{x(p):.1f},{y(v):.1f}" for p, v in zip(prices, series, strict=True))
        parts.append(
            f'<polyline points="{pts}" class="{col}">'
            f"<title>{name}</title></polyline>"
        )
    for name, p in marks.items():
        xx = x(p)
        parts.append(
            f'<line x1="{xx:.1f}" x2="{xx:.1f}" y1="{PAD_T}" y2="{H - PAD_B}" '
            f'class="mark"/><text x="{xx + 3:.1f}" '
            f'y="{PAD_T + 12}" class="lbl">{escape(name)} {p:.2f}</text>'
        )
    parts.append(
        f'<text x="{W - PAD_R + 4}" y="{y(hi) + 4:.1f}" class="lbl">{hi:+,.0f}</text>'
        f'<text x="{W - PAD_R + 4}" y="{y(lo) + 4:.1f}" class="lbl">{lo:+,.0f}</text>'
        "</svg>"
        '<div class="legend"><span><i class="k-exp"></i>at expiry</span>'
        '<span><i class="k-exit"></i>at the planned exit</span></div>'
    )
    return "".join(parts)
