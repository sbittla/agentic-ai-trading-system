"""Console pages built from the components in ``render`` (UI_REQUIREMENTS).

Each page leads with what matters (KPI tiles or a chart), then detail tables; a section with
no data says why and what fills it (UI-35).
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from decimal import Decimal
from typing import Any
from urllib.parse import quote

from trader.console.render import (
    ACTION_LABEL,
    backfill_hint,
    badge,
    card,
    checklist_table,
    chip,
    cmd_button,
    code,
    csrf_field,
    delta,
    e,
    empty,
    ext_link,
    hbars,
    health_list,
    heatmap,
    link,
    meter,
    money,
    note_form,
    notes_list,
    num,
    page_head,
    passfail,
    rating_list_html,
    rating_rows_html,
    recap_hint,
    services,
    stock_link,
    table,
    tag_chips,
    tile,
    tiles,
    ts,
)
from trader.timeutil import to_et

# --- Today -------------------------------------------------------------------------------------


def home(st: dict[str, Any], *, plans: list[dict[str, Any]], ideas: list[dict[str, Any]],
         incidents: list[dict[str, Any]], beats: dict[str, datetime], llm: dict[str, Any],
         pending: int, scanner: list[dict[str, Any]], recap_rows: list[dict[str, Any]],
         recap_at: datetime | None, ratings: list[dict[str, Any]], news: list[dict[str, Any]],
         events: list[dict[str, Any]], quotes: dict[str, dict[str, Any]], csrf: str,
         now: datetime, technical: dict[str, Any] | None = None,
         catalysts: dict[str, Any] | None = None) -> str:
    from trader.console.catalysts import home_card as catalyst_card
    from trader.console.technical import home_card
    src = st.get("source")
    notice = "" if src == "live" else (
        f'<p class="flash">trader-core API unavailable: showing its last snapshot '
        f'({ts(st.get("snapshot_ts"), "%H:%M")}). Start it with {code("./run.sh")}.</p>'
        if src == "snapshot" else
        f'<p class="flash">trader-core has not reported yet: {e(st.get("error"))}. '
        f'Start everything with {code("./run.sh")}.</p>')
    start = st.get("day_start_equity")
    limit = float(st.get("loss_limit_pct") or 1.5)
    lim_usd = Decimal(str(start)) * Decimal(str(limit)) / 100 if start else None
    pnl = Decimal(str(st.get("day_pnl") or 0))
    used = float(-pnl / lim_usd) if lim_usd and pnl < 0 else 0.0
    trades = int(st.get("trades_today") or 0)
    max_tr = int(st.get("max_trades") or 3)
    flags = []
    if st.get("kill_switch"):
        flags.append(chip("Kill switch on", "bad"))
    if st.get("halted"):
        flags.append(chip(f"Halted {st['halted']}", "bad"))
    for p in st.get("paused") or []:
        flags.append(chip(f"Paused {p}", "warn"))
    state = " ".join(flags) or chip("Trading allowed", "ok")
    pos = st.get("positions") or []
    k = tiles([
        tile("Equity", money(st.get("equity")), state, "equity"),
        tile("Day P&L", money(st.get("day_pnl")),
             meter(used, "bad" if used > 0.66 else "warn" if used > 0.33 else "")
             + f'<span class="muted small">of the -{money(lim_usd)} daily limit ({limit:g}%)'
               '</span>', "day_pnl"),
        tile("Trades today", f"{trades} <small>of {max_tr}</small>",
             meter(trades / max_tr if max_tr else 0), "trades_today"),
        tile("Open positions", str(len(pos)),
             f'<span class="muted small">window {e(st.get("window") or "closed")} · regime '
             f'{e(st.get("regime", "—"))}</span>'),
        tile("Approvals waiting", str(pending),
             link("/approvals", "open inbox") if pending else
             '<span class="muted small">none</span>', tone="attn" if pending else ""),
    ])
    # market strip + heatmap
    strip = []
    for sym in ("SPY", "QQQ", "SMH"):
        q = quotes.get(sym) or {}
        rec = next((r for r in recap_rows if r.get("ticker") == sym), None)
        last = q.get("last") or (rec or {}).get("close")
        chg = q.get("change_pct") if q.get("change_pct") is not None else (rec or {}).get("ret_1d")
        strip.append(tile(sym, num(last) if last else "—",
                          delta(chg) + (f' <span class="muted small">5d '
                                        f'{num((rec or {}).get("ret_5d"), 1, True)}%</span>'
                                        if rec else "")))
    heat_items = [{"ticker": r["ticker"], "value": r.get("change_pct"),
                   "sub": f"RVOL {num(r.get('rvol'), 1)}" if r.get("rvol") else "",
                   "href": f"/stock?symbol={quote(r['ticker'])}"}
                  for r in scanner if r.get("last") is not None]
    heat_src = "live"
    if not heat_items and recap_rows:
        heat_src = f"last session ({ts(recap_at, '%a %b %d')})"
        heat_items = [{"ticker": r["ticker"], "value": r.get("ret_1d"),
                       "sub": f"5d {num(r.get('ret_5d'), 1, True)}%",
                       "href": f"/stock?symbol={quote(r['ticker'])}"}
                      for r in recap_rows if r.get("kind") == "stock"]
    heat = heatmap(heat_items) if heat_items else empty(
        "No prices yet", "The heatmap fills from trader-core's live stream in market hours, or "
        "from the last recap outside them.", recap_hint(csrf), "▦")
    market = card("Market", tiles(strip) + heat, sub=f"Universe, {heat_src}",
                  actions=link("/market", "Full recap →"))
    # positions
    pos_html = table(pos, [
        ("ticker", "Ticker"), ("style", "Style"), ("qty", "Qty"), ("entry", "Entry"),
        ("stop", "Stop"), ("t1", "T1"), ("last", "Last"), ("unrealized", "Unrealized"),
        ("r", "R"), ("opened_at", "Since"), ("position_id", "")], "",
        {"unrealized": lambda v, r: money(v), "r": lambda v, r: num(v, 2, True),
         "opened_at": lambda v, r: ts(v, "%H:%M"),
         "position_id": lambda v, r: link(f"/story/{v}", "story"),
         "ticker": lambda v, r: stock_link(v)}) if pos else empty(
        "No open positions", "Positions appear here as soon as an entry fills. The system "
        "trades only 9:35-10:30 and 15:00-15:50 ET, and only setups that pass every check.")
    # plan + ideas
    plan_html = table(plans, [("window", "Window"), ("ticker", "Ticker"), ("setup", "Setup"),
                              ("trigger", "Trigger"), ("stop", "Stop"), ("target", "T1")], "",
                      {"ticker": lambda v, r: stock_link(v)}) if plans else empty(
        "No game plan for today yet", "The morning plan is built at 9:00 and the afternoon "
        "plan at 2:45 ET from the next-day ideas.", cmd_button(csrf, "PLAN", "", "Build ideas now"))
    picks = [r for r in ideas if r.get("status") == "pick"]
    ideas_html = table(picks, [("plan_for", "For"), ("ticker", "Ticker"), ("score", "Score"),
                               ("morning_setup", "Setup"), ("morning_trigger", "Trigger"),
                               ("morning_stop", "Stop"), ("morning_target", "T1")], "",
                       {"ticker": lambda v, r: stock_link(v)}, compact=True) if picks else empty(
        "No next-day ideas yet", "Built after the close at 4:15 PM ET from the day just ended.",
        cmd_button(csrf, "PLAN", "", "Build ideas now", primary=True))
    # ratings, news, events
    rat_html = rating_list_html(ratings, 8) if ratings else empty(
        "No analyst actions collected yet", "The news service collects upgrades, downgrades "
        "and price-target changes every 30 minutes. " + backfill_hint(), icon="★")
    news_html = _news_list(news[:8]) if news else empty(
        "No news yet", "The news service polls Finnhub and SEC filings from 7:15 AM ET. "
        + backfill_hint(), icon="✉")
    ev_html = table(events, [("date", "Date"), ("event", "Event"), ("ticker", "Ticker"),
                             ("detail", "Note")], "", {"date": lambda v, r: ts(v, "%a %b %d"),
                                                       "ticker": lambda v, r: stock_link(v)
                                                       if v else ""},
                    compact=True) if events else empty(
        "No events in the next 7 days", "Earnings come from the news service; CPI, jobs, FOMC "
        "and 10:00 releases from the macro calendar.", icon="◷")
    def _sev(t: str | None) -> str:
        return "red" if "HALT" in (t or "") or "DOWN" in (t or "") else "amber"
    inc = "".join(f'<li>{badge(_sev(i["type"]), i["type"])} '
                  f'<span class="muted small">{ts(i["ts"], "%m-%d %H:%M")}</span> '
                  f'{e((i["detail"] or "")[:160])}</li>' for i in incidents)
    inc_html = (f'<ul class="plain">{inc}</ul>' if incidents else
                f'<p class="muted small">{chip("No open incidents", "ok")}</p>')
    return (f'<div data-live="home">{notice}'
            + page_head("Today", f'{to_et(now):%A %B %d, %H:%M} ET',
                        cmd_button(csrf, "STATUS", "", "Refresh status") + " "
                        + link("/controls", "Controls", "btn"))
            + k + market
            + card("Open positions", pos_html)
            + home_card(technical) + catalyst_card(catalysts)
            + '<div class="grid2">' + card("Today's plan", plan_html)
            + card("Next-day ideas", ideas_html, actions=link("/ideas", "All ideas →")) + "</div>"
            + '<div class="grid2">' + card("Latest analyst actions", rat_html,
                                           actions=link("/ratings", "All ratings →"))
            + card("Latest news", news_html, actions=link("/news", "All news →")) + "</div>"
            + '<div class="grid3">' + card("Upcoming events", ev_html, sub="Next 7 days")
            + card("Health", health_list(st))
            + card("Services and model", services(beats, llm, now),
                   actions=link("http://localhost:3000", "Grafana ↗")) + "</div>"
            + (card("Incidents", inc_html, actions=cmd_button(csrf, "ACK", "", "ACK alerts"),
                    cls="attn") if incidents else "") + "</div>")


def _news_list(rows: list[dict[str, Any]]) -> str:
    items = []
    for r in rows:
        sc = r.get("finbert_score")
        tone = "ok" if sc is not None and sc >= 0.3 else "bad" if sc is not None and sc <= -0.3 \
            else "neutral"
        word = "positive" if tone == "ok" else "negative" if tone == "bad" else "neutral"
        items.append(f'<li><span class="muted small">{ts(r["published_at"], "%b %d %H:%M")}'
                     f'</span> {stock_link(r["ticker"])} {chip(word, tone)} '
                     f'{e((r.get("headline") or "")[:160])}</li>')
    return f'<ul class="plain news">{"".join(items)}</ul>'


# --- Market recap (UI-34) ------------------------------------------------------------------------


def market(run_at: datetime | None, rows: list[dict[str, Any]], csrf: str) -> str:
    built = f"built {ts(run_at, '%a %b %d %H:%M')} ET" if run_at else ""
    head = page_head("Market recap", (f"Past sessions per ticker, {built}"
                                       if run_at else "Past week or month per ticker"),
                     recap_hint(csrf))
    if not rows:
        return head + card("No recap yet", empty(
            "Build the recap", "A recap pulls daily bars for the universe and the indexes and "
            "shows returns, strength against QQQ, levels and analyst actions, then sends the "
            "next-session watchlist to Telegram. Press a button above, send "
            + code("RECAP 21") + " on Telegram, or " + backfill_hint(), recap_hint(csrf), "▤"))
    days = next((int(k.split("_")[1][:-1]) for k in rows[0] if k.startswith("ret_")
                 and k not in ("ret_1d", "ret_5d")), 5)
    idx = [r for r in rows if r.get("kind") == "index"]
    stocks = [r for r in rows if r.get("kind") == "stock"]
    itiles = tiles(tile(r["ticker"], num(r["close"]), f"1d {delta(r.get('ret_1d'))} · 5d "
                        f"{delta(r.get('ret_5d'))} · {days}d {delta(r.get(f'ret_{days}d'))}")
                   for r in idx)
    rs = sorted(((r["ticker"], r.get("rs_qqq"), f"/stock?symbol={quote(r['ticker'])}")
                 for r in stocks), key=lambda x: -(x[1] or -999))
    chart = hbars(rs, " pts", f"{days}-session return minus QQQ") or ""
    rets = sorted(((r["ticker"], r.get(f"ret_{days}d"), f"/stock?symbol={quote(r['ticker'])}")
                   for r in stocks), key=lambda x: -(x[1] or -999))
    chart2 = hbars(rets, "%", f"{days}-session return") or ""
    watch = [r for r in stocks if r.get("tags")]
    wcards = "".join(
        f'<div class="watch"><div class="whead">{stock_link(r["ticker"])} '
        f'<b>{num(r["close"])}</b> {delta(r.get("ret_1d"))}</div><div>{tag_chips(r["tags"])}'
        f'</div><dl><dt>Above</dt><dd>{num(r["levels"]["prior_high"])} prior high · '
        f'{num(r["levels"]["high20"])} 20d high</dd><dt>Below</dt><dd>'
        f'{num(r["levels"]["prior_low"])} prior low · {num(r["levels"]["low20"])} 20d low</dd>'
        f'<dt>Pivot</dt><dd>{num(r["levels"]["pivot"])} (R1 {num(r["levels"]["r1"])}, S1 '
        f'{num(r["levels"]["s1"])})</dd><dt>ATR</dt><dd>{num(r.get("atr14"))} '
        f'({num(r.get("atr_pct"), 1)}%)</dd></dl>'
        + (f'<p class="small">{e(r["latest_rating"])}</p>' if r.get("latest_rating") else "")
        + "</div>" for r in sorted(watch, key=lambda r: -len(r["tags"])))
    tbl = table(stocks, [
        ("ticker", "Ticker"), ("close", "Close"), ("ret_1d", "1d %"), ("ret_5d", "5d %"),
        (f"ret_{days}d", f"{days}d %"), ("rs_qqq", "vs QQQ"), ("atr_pct", "ATR %"),
        ("atr_to_high20", "ATR to 20d high"), ("volume_ratio", "Vol vs 20d"),
        ("ratings", "Analyst actions"), ("tags", "Tags")], "", {
        "ticker": lambda v, r: stock_link(v), "ret_1d": lambda v, r: delta(v),
        "ret_5d": lambda v, r: delta(v), f"ret_{days}d": lambda v, r: delta(v),
        "rs_qqq": lambda v, r: delta(v, " pts", 1),
        "ratings": lambda v, r: ", ".join(f"{ACTION_LABEL.get(k, k)} {n}"
                                          for k, n in (v or {}).items()) or "—",
        "news_tone": lambda v, r: num(v, 2, True), "tags": lambda v, r: tag_chips(v or [])})
    return (head + itiles + '<div class="grid2">'
            + card("Strength against QQQ", chart or "<p class=muted>no data</p>",
                   sub=f"{days}-session return minus QQQ's, in points")
            + card("Return", chart2 or "<p class=muted>no data</p>",
                   sub=f"{days}-session return, %") + "</div>"
            + card("Watchlist for the next session", f'<div class="watchgrid">{wcards}</div>'
                   if wcards else empty("Nothing stands out", "No ticker is a leader, laggard, "
                                        "near its 20-day high, oversold, re-rated or near "
                                        "earnings."),
                   sub="Levels to watch, not orders; the 9:00 game plan decides")
            + card("All tickers", tbl))


# --- Analyst ratings (UI-33) ----------------------------------------------------------------------


def ratings(rows: list[dict[str, Any]], *, days: int, ticker: str, action: str,
            universe: tuple[str, ...]) -> str:
    opts_t = "".join(f'<option value="{e(t)}"{" selected" if t == ticker else ""}>'
                     f'{e(t or "All tickers")}</option>' for t in ("",) + universe)
    opts_a = "".join(f'<option value="{e(a)}"{" selected" if a == action else ""}>'
                     f'{e(ACTION_LABEL.get(a, "Any action"))}</option>'
                     for a in ("",) + tuple(ACTION_LABEL))
    opts_d = "".join(f'<option value="{d}"{" selected" if d == days else ""}>last {d} days'
                     f'</option>' for d in (7, 30, 90))
    filters = (f'<form method="get" action="/ratings" class="filters"><select name="days">'
               f'{opts_d}</select><select name="ticker">{opts_t}</select>'
               f'<select name="action">{opts_a}</select><button>Apply</button></form>')
    head = page_head("Analyst ratings", "Upgrades, downgrades, initiations and price-target "
                     "changes from Finnhub and news headlines (Yahoo Finance, Google News)")
    if not rows:
        return head + filters + card("No analyst actions", empty(
            "Nothing collected for this filter", "The news service collects rating actions "
            "every 30 minutes during market days. " + backfill_hint(), icon="★"))
    c = Counter(r["action"] for r in rows)
    kt = tiles([tile("Upgrades", str(c["upgrade"])), tile("Downgrades", str(c["downgrade"])),
                tile("Initiations", str(c["initiate"])),
                tile("Targets raised", str(c["pt_raise"])),
                tile("Targets cut", str(c["pt_cut"])),
                tile("Firms", str(len({r["firm"] for r in rows})))])
    net: Counter[str] = Counter()
    for r in rows:
        net[r["ticker"]] += {"upgrade": 1, "pt_raise": 1, "initiate": 0, "downgrade": -1,
                             "pt_cut": -1}.get(r["action"], 0)
    bars = hbars(sorted(((t, float(n), f"/ratings?ticker={quote(t)}&days={days}")
                         for t, n in net.items()), key=lambda x: -(x[1] or 0)), "",
                 "net rating actions")
    return (head + filters + kt
            + card("Net actions per ticker", bars or "<p class=muted>none</p>",
                   sub="upgrades and target raises minus downgrades and target cuts")
            + card(f"{len(rows)} actions", rating_rows_html(rows, 300, show_ticker=not ticker)))


def news_page(rows: list[dict[str, Any]], ticker: str, universe: tuple[str, ...], days: int
              ) -> str:
    opts = "".join(f'<option value="{e(t)}"{" selected" if t == ticker else ""}>'
                   f'{e(t or "All tickers")}</option>' for t in ("",) + universe)
    filters = (f'<form method="get" action="/news" class="filters"><select name="ticker">'
               f'{opts}</select><select name="days">'
               + "".join(f'<option value="{d}"{" selected" if d == days else ""}>last {d} days'
                         f'</option>' for d in (1, 7, 30)) + '</select><button>Apply</button>'
               '</form>')
    head = page_head("News", "Headlines from Finnhub, SEC filings and rating feeds, scored by "
                     "FinBERT (−1 negative to +1 positive)")
    if not rows:
        return head + filters + card("No news", empty(
            "Nothing for this filter yet", "The news service polls from 7:15 AM ET on market "
            "days. " + backfill_hint(), icon="✉"))
    by_t: dict[str, list[float]] = {}
    for r in rows:
        if r.get("finbert_score") is not None:
            by_t.setdefault(r["ticker"], []).append(float(r["finbert_score"]))
    tone = hbars(sorted(((t, sum(v) / len(v), f"/news?ticker={quote(t)}&days={days}")
                         for t, v in by_t.items()), key=lambda x: -(x[1] or 0)), "", "average tone")
    return (head + filters + (card("Average tone per ticker", tone,
                                   sub="FinBERT, −1 negative to +1 positive") if tone and not
                              ticker else "")
            + card(f"{len(rows)} headlines", _news_list(rows)))


# --- Options (OP-16..19, UI-36) ------------------------------------------------------------------


def options(sugs: list[dict[str, Any]], board: dict[str, Any] | None, preview: dict[str, Any] |
            None, report: str, gate: dict[tuple[str, str], tuple[bool, str]], csrf: str,
            error: str = "") -> str:
    closed = [r for r in sugs if r.get("status") == "closed"]
    rs = [float(r.get("r_multiple") or 0) for r in closed]
    wins = [x for x in rs if x > 0]
    losses = [-x for x in rs if x < 0]
    status = (board or {}).get("status") or {}
    k = tiles([
        tile("Suggestions (30 days)", str(len(sugs))),
        tile("Open, paper-tracked", str(sum(1 for r in sugs if r.get("status") == "open"))),
        tile("Win rate", f"{len(wins) / len(rs) * 100:.0f}%" if rs else "—",
             f'<span class="muted small">{len(rs)} graded</span>'),
        tile("Expectancy", f"{sum(rs) / len(rs):+.2f}R" if rs else "—",
             '<span class="muted small">gate: ≥ +0.15R</span>'),
        tile("Profit factor", f"{sum(wins) / sum(losses):.2f}" if losses else "—",
             '<span class="muted small">gate: ≥ 1.3</span>'),
        tile("Mode", e(status.get("mode", "—")),
             chip("on", "ok") if status.get("enabled") else chip("off", "neutral")),
    ])
    head = page_head("Options", "Advisory defined-risk ideas on the system's bullish and bearish "
                     "views; nothing is ordered")
    # board
    rows = (board or {}).get("rows") or []
    if rows:
        brd = table(rows, [("ticker", "Ticker"), ("spot", "Stock"), ("atm_iv_30d", "ATM IV 30d"),
                           ("iv_rank", "IV rank"), ("em", "Expected move"),
                           ("bull", "If bullish"), ("bear", "If bearish"),
                           ("earnings", "Earnings"), ("preview", "Preview")], "", {
            "ticker": lambda v, r: stock_link(v), "spot": lambda v, r: num(v),
            "atm_iv_30d": lambda v, r: f"{v * 100:.1f}%" if v else e(r.get("error", "—")),
            "iv_rank": lambda v, r: (f"{v:.0f}" if v is not None else "n/a")
            + (f' <span class="muted small">{e(r["history"])}</span>'
               if r.get("history") not in (None, "full") else ""),
            "em": lambda v, r: f"±{v:.2f} ({r['em_pct']:.1f}%) to {e(r['em_expiry'][5:])}"
            if v else "—",
            "bull": lambda v, r: e(", ".join(x.replace("_", " ") for x in v or []) or "none"),
            "bear": lambda v, r: e(", ".join(x.replace("_", " ") for x in v or []) or "none"),
            "earnings": lambda v, r: chip(v, "warn") if v else "",
            "preview": lambda v, r: link(f"/options?preview={quote(r['ticker'])}&dir=bull",
                                         "bull", "btn small") + " "
            + link(f"/options?preview={quote(r['ticker'])}&dir=bear", "bear", "btn small")})
        board_html = card("Options board", brd, sub="Live IV, IV rank, expected move to the "
                          "nearest weekly, and the structure the rules would use (OP-1)")
    else:
        board_html = card("Options board", empty(
            "Options data unavailable", (e(error) + ". " if error else "")
            + "The board needs trader-core running with options.mode advisory and the Alpaca "
            "options feed (free indicative data on paper accounts).", icon="◇"))
    prev_html = ""
    if preview is not None:
        v = preview.get("view") or {}
        cards = []
        for s_ in preview.get("suggestions") or []:
            legs = " / ".join(f"{'+' if lg['side'] > 0 else '−'}{lg['strike']:g}{lg['kind']}"
                              for lg in s_["legs"])
            gain = money(s_["max_gain"]) if s_.get("max_gain") is not None else "unlimited"
            cards.append(
                f'<div class="watch"><div class="whead"><b>{e(s_["label"])}</b> '
                f'{e(s_["expiry"])} · {e(legs)}</div>'
                f'<dl><dt>Price</dt><dd>{"credit" if s_["net_mid"] < 0 else "debit"} '
                f'~{abs(s_["net_mid"]):.2f} (fill est. {abs(s_["est_fill"]):.2f})</dd>'
                f'<dt>Max loss</dt><dd>{money(s_["max_loss"])} per contract</dd>'
                f'<dt>Max gain</dt><dd>{gain}</dd><dt>Breakeven</dt><dd>{num(s_["breakeven"])}</dd>'
                f'<dt>At T1</dt><dd>{money(s_["pnl_t1"])} · at invalidation '
                f'{money(s_["pnl_inv"])}</dd><dt>Size</dt><dd>{e(s_["contracts"])} = '
                f'{money(s_["risk_usd"])} risk</dd><dt>P(profit)</dt><dd>~{s_["pop"] * 100:.0f}% '
                f'(estimate)</dd></dl><p>'
                + (chip("would be sent", "ok") if s_["allowed"] else
                   chip(f"blocked: {s_['blocked_by']}", "warn")) + "</p></div>")
        notes = "".join(f"<li>{e(n)}</li>" for n in preview.get("notes") or [])
        prev_html = card(
            f"Preview: {preview.get('symbol')} {v.get('direction', '')}",
            (f'<p class="small muted">Stock {num(v.get("spot"))} · invalidation '
             f'{num(v.get("invalidation"))} · T1 {num(v.get("t1"))} ({e(v.get("style"))} style, '
             f'1 ATR stop, 1.5R target). Nothing is sent or journaled.</p>')
            + (f'<div class="watchgrid">{"".join(cards)}</div>' if cards else
               empty("No structure passed", "The rules dropped every structure for this view:"))
            + (f'<ul class="plain small">{notes}</ul>' if notes else "")
            + "<p>" + cmd_button(csrf, "OPT",
                                 f'{preview.get("symbol")} {v.get("direction", "").upper()}',
                                 "Send this idea to Telegram") + "</p>")
    # suggestions + why none
    dropped = status.get("dropped") or []
    why = table(dropped[:12], [("ticker", "Ticker"), ("why", "Why it was dropped")], "",
                {"ticker": lambda v, r: stock_link(v)}, compact=True) if dropped else ""
    if sugs:
        sug_html = table(sugs, [("ts", "Time"), ("ticker", "Ticker"), ("direction", "View"),
                                ("structure", "Structure"), ("expiry", "Expiry"),
                                ("contracts", "Qty"), ("max_loss", "Max loss"),
                                ("status", "Status"), ("mark", "P/L per contract"),
                                ("pnl", "Result"), ("r_multiple", "R"),
                                ("suggestion_id", "")], "", {
            "suggestion_id": lambda v, r: link(f"/options/{v}", "payoff"),
            "max_loss": lambda v, r: money(v), "mark": lambda v, r: money(v),
            "pnl": lambda v, r: money(v), "structure": lambda v, r: e((v or "").replace("_", " ")),
            "ticker": lambda v, r: stock_link(v)})
    else:
        sug_html = empty(
            "No suggestions in the last 30 days",
            "Ideas fire only when a stock setup or a bearish setup triggers inside a trading "
            "window (9:35-10:30, 15:00-15:50 ET), with the 4:15 PM next-day ideas, or when you "
            "ask (" + code("OPT NVDA bull") + " on Telegram, or the preview buttons above). "
            "Use the board's preview to see what the rules would suggest right now.", icon="◇")
    gate_html = "".join(f"<li>{passfail(ok)} {e(key[0].replace('_', ' '))} {e(key[1])}: "
                        f'<span class="muted small">{e(w)}</span></li>'
                        for key, (ok, w) in sorted(gate.items()))
    return (head + k + prev_html + board_html
            + card("Suggestions", sug_html + (f"<h4>Recently dropped</h4>{why}" if why else ""))
            + '<div class="grid2">'
            + card("Track record", f"<pre>{e(report)}</pre>" if report else empty(
                "Nothing graded yet", "Every suggestion is paper-tracked from its estimated fill "
                "and graded when its plan exits (A16)."))
            + card("Promotion gate", f'<ul class="plain">{gate_html}</ul>' if gate_html else
                   empty("Not reached", "A structure and direction may move to paper orders only "
                         "after ≥ 40 graded suggestions over ≥ 20 sessions with ≥ +0.15R and "
                         "profit factor ≥ 1.3 (A16-4)."))
            + "</div>")


# --- Stock (UI-4..UI-6a, OP-16) -----------------------------------------------------------------


def stock(sym: str, *, q: dict[str, Any] | None, chart_html: str, daily_html: str,
          ind: dict[str, Any], news: list[dict[str, Any]], flags: list[dict[str, Any]],
          status: dict[str, Any], checklist: dict[str, Any] | None, iv: dict[str, Any] | None,
          chain: dict[str, Any] | None, ratings: list[dict[str, Any]],
          recap: dict[str, Any] | None, csrf: str, error: str = "") -> str:
    if not sym:
        return page_head("Stock lookup", "Any US symbol: quote, charts, indicators, analyst "
                         "actions, news and options") + card("Search", empty(
            "Type a symbol", "Use the search box at the top (press " + code("/") + "). Universe "
            "tickers also show the live entry checklist; other symbols are informational only "
            "(UR-1).", icon="⌕"))
    q = q or {}
    head = page_head(sym, (f'{num(q.get("last"))} {delta(q.get("change_pct"))} · '
                           f'{"in the universe" if status.get("universe") else "informational"}'
                           if q else f'quote unavailable: {e(error)}'),
                     cmd_button(csrf, "OPT", f"{sym} BULL", "Options idea: bull") + " "
                     + cmd_button(csrf, "OPT", f"{sym} BEAR", "Options idea: bear") + " "
                     + link(f"/ask?q={quote('What is the latest on ' + sym + '?')}", "Ask",
                            "btn") + " "
                     + link("/alerts", "Price alert", "btn") + " "
                     + ext_link("http://localhost:3000", "Grafana", "btn"))
    rk = recap or {}
    kt = tiles([
        tile("Last", num(q.get("last")), delta(q.get("change_pct"))),
        tile("Bid / ask", f'<span data-q="bid">{num(q.get("bid"))}</span> / '
             f'<span data-q="ask">{num(q.get("ask"))}</span>',
             f'<span class="muted small">spread {num(q.get("spread_pct"), 3)}%</span>'),
        tile("VWAP", num(q.get("vwap")), f'<span class="muted small">RVOL '
             f'{num(q.get("rvol"), 2)}</span>'),
        tile("5 / 21 sessions", f"{num(rk.get('ret_5d'), 1, True)}%" if rk else "—",
             (delta(rk.get("rs_qqq"), " pts vs QQQ", 1) if rk else
              '<span class="muted small">build a recap</span>')),
        tile("ATR (14d)", num(rk.get("atr14") or ind.get("atr14d")),
             f'<span class="muted small">5-min {num(ind.get("atr5m"))}</span>'),
    ])
    quote_live = f'<div data-symbol="{e(sym)}" class="hidden"><span data-q="last"></span></div>'
    macd = ind.get("macd")
    bb = ind.get("bollinger")
    ind_html = table([
        {"name": "RSI 14 (1-min / daily)", "value": f"{num(ind.get('rsi14'), 1)} / "
                                                     f"{num(ind.get('rsi14d'), 1)}"},
        {"name": "MACD / signal / hist", "value": " / ".join(num(x, 3) for x in macd)
         if macd else "—"},
        {"name": "Gap / range position", "value": f"{num(ind.get('gap_pct'), 2, True)}% / "
         f"{num(None if (rp := ind.get('range_pos')) is None else rp * 100, 0)}%"},
        {"name": "Bollinger 20,2", "value": f"{num(bb[0])} – {num(bb[2])}" if bb else "—"},
        {"name": "EMA 20d / 50d", "value": f"{num(ind.get('ema20d'))} / {num(ind.get('ema50d'))}"},
    ], [("name", "Indicator"), ("value", "Value")], compact=True)
    sys_html = ('<ul class="plain">'
                f'<li>Universe {chip("yes", "ok") if status.get("universe") else chip("no")}</li>'
                f'<li>Correlated group: {e(status.get("group") or "none")}</li>'
                f'<li>Locks: {e(", ".join(status.get("locks") or []) or "none")}</li>'
                f'<li>Ideas: {e(", ".join(status.get("ideas") or []) or "none")}</li>'
                f'<li>Past trades: {e(status.get("trades_summary") or "none")}</li></ul>')
    if checklist and checklist.get("tradable"):
        ck = table(checklist["items"], [("id", "Item"), ("passed", "Result"),
                                        ("value", "Measured"), ("threshold", "Threshold")],
                   fmt={"passed": lambda v, r: passfail(v)}, compact=True)
        ck_html = card("Entry checklist now", ck, sub="Why the system is or is not buying")
    elif checklist:
        ck_html = card("Entry checklist", f'<p class="muted">{e(checklist.get("note"))}</p>')
    else:
        ck_html = card("Entry checklist", empty("Unavailable", "trader-core's API did not "
                                                "answer; the checklist needs it running."))
    rat = rating_list_html(ratings, 12, show_ticker=False) if ratings else empty(
        "No analyst actions in 90 days", "Collected every 30 minutes by the news service. "
        + backfill_hint(), icon="★")
    nw = _news_list(news) if news else empty("No news in the journal", backfill_hint(), icon="✉")
    fl = " ".join(chip(f"{f['flag'].replace('_', ' ').lower()} {f['date']}", "warn")
                  for f in flags) or '<span class="muted small">none</span>'
    levels = ""
    if rk.get("levels"):
        lv = rk["levels"]
        levels = card("Levels for the next session", table([
            {"level": "Prior high / low / close", "value": f"{num(lv['prior_high'])} / "
             f"{num(lv['prior_low'])} / {num(lv['prior_close'])}"},
            {"level": "Pivot (R1 / S1)", "value": f"{num(lv['pivot'])} ({num(lv['r1'])} / "
             f"{num(lv['s1'])})"},
            {"level": "20-session high / low", "value": f"{num(lv['high20'])} / "
             f"{num(lv['low20'])}"}], [("level", "Level"), ("value", "Price")], compact=True)
            + (f"<p>{tag_chips(rk.get('tags') or [])}</p>" if rk.get("tags") else ""),
            sub="From the last recap")
    return (head + quote_live + kt
            + card("Intraday", chart_html, sub="1-minute candles, VWAP (violet), opening range "
                   "(shaded), prior-day levels, this system's fills")
            + '<div class="grid2">' + ck_html + levels
            + card("In this system", sys_html) + card("Indicators", ind_html) + "</div>"
            + card("Daily", daily_html, sub="Last 90 sessions")
            + '<div class="grid2">' + card("Analyst actions", rat)
            + card("News", f"<p>Event flags: {fl}</p>" + nw) + "</div>"
            + card("Options", options_block(iv, chain), sub="IV, expected move and the chain "
                   "around the money (indicative prices on the free feed)")
            + '<p class="muted small">Data limits: the free Alpaca IEX feed covers about 2–3% of '
              'volume; Finnhub free tier 60 calls/min (cached 60 s); symbols outside the '
              'universe get no FinBERT run.</p>')


def options_block(iv: dict[str, Any] | None, chain: dict[str, Any] | None) -> str:
    if iv is None:
        return empty("Options data unavailable", "Needs trader-core with options.mode advisory "
                     "and the Alpaca options feed.", icon="◇")
    moves = ", ".join(f"{k[5:]}: ±{v:.2f}" for k, v in list((iv.get("expected_moves")
                                                             or {}).items())[:5])
    head = tiles([
        tile("ATM IV 30d", f"{(iv.get('atm_iv_30d') or 0) * 100:.1f}%"),
        tile("IV rank", num(iv.get("iv_rank"), 0), f'<span class="muted small">'
             f'{e(iv.get("history"))}</span>'),
        tile("Term front / 30d", f"{(iv.get('term_front') or 0) * 100:.1f}% / "
             f"{(iv.get('term_30d') or 0) * 100:.1f}%"),
        tile("Skew 25Δ", f"{(iv.get('skew') or 0) * 100:+.1f} pts"),
    ]) + f'<p class="small">Expected move: {e(moves or "n/a")}</p>'
    if not chain:
        return head
    spot = chain.get("spot") or 0
    rows = sorted(chain["rows"], key=lambda r: abs(r["strike"] - spot))[:18]
    calls = {r["strike"]: r for r in rows if r["kind"] == "C"}
    puts = {r["strike"]: r for r in rows if r["kind"] == "P"}

    def cell(r: dict[str, Any] | None) -> str:
        if r is None:
            return "<td colspan=5>—</td>"
        liq = chip("liquid", "ok") if r["liquid"] else chip(r["why"], "warn")
        return (f'<td class="num">{num(r["bid"])}/{num(r["ask"])}</td>'
                f'<td class="num">{num((r["iv"] or 0) * 100, 0)}%</td>'
                f'<td class="num">{num(r["delta"], 2, True)}</td>'
                f'<td class="num">{e(r["oi"])}/{e(r["volume"])}</td><td>{liq}</td>')
    body = "".join(f'<tr>{cell(calls.get(k))}<th class="strike">{k:g}</th>{cell(puts.get(k))}'
                   f'</tr>' for k in sorted(set(calls) | set(puts)))
    hdr = '<th>bid/ask</th><th>IV</th><th>Δ</th><th>OI/vol</th><th>OP-4</th>'
    return (f'{head}<p class="small">Chain {e(chain.get("expiry"))} (stock {num(spot)})</p>'
            f'<div class="tbl"><table class="compact"><thead><tr><th colspan=5>Calls</th>'
            f'<th>Strike</th><th colspan=5>Puts</th></tr><tr>{hdr}<th></th>{hdr}</tr></thead>'
            f'<tbody>{body}</tbody></table></div>')


# --- Ideas -----------------------------------------------------------------------------------


def ideas(cur: list[dict[str, Any]], hist: list[dict[str, Any]], watch: list[dict[str, Any]],
          csrf: str) -> str:
    head = page_head("Next-day ideas", "Built after the close (4:15 PM ET) from the session just "
                     "ended; refreshed at 9:00 and 2:45 with gap and pre-market data",
                     cmd_button(csrf, "PLAN", "", "Build ideas now", primary=True) + " "
                     + cmd_button(csrf, "RECAP", "5", "Build week recap"))
    picks = [r for r in cur if r.get("status") == "pick"]
    if picks:
        cards = "".join(
            f'<div class="watch"><div class="whead">{stock_link(r["ticker"])} '
            f'{chip(f"score {r.get("score")}", "info")}</div><dl>'
            + (f'<dt>Morning</dt><dd>{e(r.get("morning_setup"))} · trigger '
               f'{num(r.get("morning_trigger"))} · stop {num(r.get("morning_stop"))} · T1 '
               f'{num(r.get("morning_target"))}</dd>' if r.get("morning_setup") else "")
            + (f'<dt>Afternoon</dt><dd>{e(r.get("afternoon_setup"))} · trigger '
               f'{num(r.get("afternoon_trigger"))} · stop {num(r.get("afternoon_stop"))} · T1 '
               f'{num(r.get("afternoon_target"))}</dd>' if r.get("afternoon_setup") else "")
            + f'<dt>Close</dt><dd>{num(r.get("close"))} ({num(r.get("change_pct"), 2, True)}%)'
              f'</dd><dt>News</dt><dd>{e(r.get("news_tone") or "—")}</dd></dl>'
            + (f'<p class="small">{e(r.get("note"))}</p>' if r.get("note") else "") + "</div>"
            for r in picks)
        picks_html = card(f"Picks for {ts(picks[0]['plan_for'], '%a %b %d')}",
                          f'<div class="watchgrid">{cards}</div>')
    else:
        picks_html = card("Picks", empty(
            "No ideas yet", "Ideas are built at 4:15 PM ET on trading days. Press " +
            "<b>Build ideas now</b> (outside market hours) or " + backfill_hint(),
            cmd_button(csrf, "PLAN", "", "Build ideas now", primary=True), "✦"))
    watch_html = ""
    if watch:
        watch_html = card("Recap watchlist", table(watch, [
            ("ticker", "Ticker"), ("close", "Close"), ("tags", "Why"),
            ("levels", "Levels (prior high / low · 20d high)")], "", {
            "ticker": lambda v, r: stock_link(v), "tags": lambda v, r: tag_chips(v or []),
            "levels": lambda v, r: f"{num(v['prior_high'])} / {num(v['prior_low'])} · "
                                   f"{num(v['high20'])}"}, compact=True),
            sub="From the last recap", actions=link("/market", "Market recap →"))
    screen = [r for r in cur if r.get("status") != "pick"]
    screen_html = card("The screen", table(screen, [
        ("ticker", "Ticker"), ("status", "Status"), ("score", "Score"), ("close", "Close"),
        ("change_pct", "Day %"), ("range_pos", "Close in range"), ("volume_ratio", "Vol vs 50d"),
        ("trend_up", "Uptrend"), ("note", "Why not picked")], "", {
        "ticker": lambda v, r: stock_link(v), "change_pct": lambda v, r: delta(v)},
        compact=True), sub="Every ticker the after-close screen looked at") if screen else ""
    hist_html = card("Track record (20 sessions)", table(hist, [
        ("plan_for", "Session"), ("ticker", "Ticker"), ("score", "Score"),
        ("triggered", "Triggered"), ("outcome", "Outcome")], "", {
        "ticker": lambda v, r: stock_link(v),
        "outcome": lambda v, r: chip(v, "ok" if v == "t1" else "bad" if v == "stopped"
                                     else "neutral") if v else ""}, compact=True)
        if hist else empty("No graded ideas yet", "Each pick is graded on its planned session's "
                           "daily bar (triggered, reached T1, stopped)."))
    return head + picks_html + watch_html + '<div class="grid2">' + hist_html + "</div>" + \
        screen_html


# --- simple pages ---------------------------------------------------------------------------------


def ask_page(csrf: str, answer: Any, history: list[dict[str, Any]], q: str,
             search: str = "") -> str:
    ans = ""
    if answer is not None:
        sug = "".join(link(f"/controls?suggest={quote(c)}",
                           f"Suggested command: {c} (opens Controls, you confirm)", "btn")
                      for c in answer.suggested)
        note = f'<p class="muted small">{e(answer.note)}</p>' if answer.note else ""
        ans = card(answer.question, f'<p class="answer">{e(answer.text)}</p>{note}'
                   f'<p class="muted small">Source: {e(answer.source)} · tool {e(answer.tool)} '
                   f'{e(answer.args)} · {"Qwen wording" if answer.model_used else "table only"}'
                   f'</p>{table(answer.rows[:50], compact=True)}{sug}')
    examples = ("Why were there no trades this morning?", "How did the ORB setup do in the last "
                "30 days?", "Which tickers are wash-sale locked?", "What does NVDA's checklist "
                "say?", "How did put debit spreads do on high-IV days?")
    ex = " ".join(link(f"/ask?q={quote(x)}", x, "chip-link") for x in examples)
    hist = "".join(f'<li><span class="muted small">{ts(h["ts"], "%H:%M")}</span> '
                   f'<b>{e(h["question"])}</b> — {e((h["answer"] or "")[:200])}</li>'
                   for h in reversed(history))
    return (page_head("Ask", "Plain-language questions answered from journal tools; every number "
                      "comes from a tool's rows")
            + card("Question", f'<form method="post" action="/ask" class="askform">'
                   f'{csrf_field(csrf)}<input name="q" value="{e(q)}" maxlength="500" '
                   f'placeholder="Ask about trades, setups, locks, a stock…" autofocus>'
                   f'<button class="primary">Ask</button></form><p class="small">Try: {ex}</p>'
                   '<p class="muted small">During entry windows (09:35–10:30, 15:00–15:55 ET) '
                   'the model waits and the table answers (UI-8b).</p>')
            + ans
            + card("Today's questions", f'<form method="get" action="/ask" class="inline">'
                   f'<input name="search" value="{e(search)}" placeholder="search"><button>'
                   f'Search</button></form> <form method="post" action="/ask/clear" '
                   f'class="inline" data-confirm="Clear today\'s history?">{csrf_field(csrf)}'
                   f'<button>Clear</button></form><ul class="plain">'
                   f'{hist or "<li class=muted>none yet</li>"}</ul>'))


def approvals_page(csrf: str, pending: list[dict[str, Any]], today: list[dict[str, Any]]) -> str:
    cards = []
    for p in pending:
        cards.append(card(
            f"{p['ticker']} {p['setup']}",
            f'<p class="countdown"><span data-expires="{e(p["expires"].isoformat())}">…</span> '
            f'left</p><dl class="kv"><dt>Entry</dt><dd>{num(p["entry"])}</dd><dt>Stop</dt>'
            f'<dd>{num(p["stop"])}</dd><dt>T1</dt><dd>{num(p["t1"])}</dd><dt>Size</dt>'
            f'<dd>{e(p["qty"])} sh, risk {money(p["risk_usd"])}</dd></dl>'
            f'<p>{e(p["reasoning"][:400])}</p>'
            f'<p>{link("/story/" + p["proposal_id"], "trade story")}</p>'
            f'<form method="post" action="/cmd" class="inline">{csrf_field(csrf)}'
            f'<input type="hidden" name="name" value="YES"><label>Code from your phone '
            f'<input name="arg" maxlength="8" autocomplete="off" required></label>'
            f'<button class="primary">Approve</button></form> '
            f'<form method="post" action="/cmd" class="inline">{csrf_field(csrf)}'
            f'<input type="hidden" name="name" value="NO"><label>Code '
            f'<input name="arg" maxlength="8" autocomplete="off" required></label>'
            f'<button class="danger">Reject</button></form>', cls="attn"))
    body = "".join(cards) or card("Inbox", empty(
        "No trade waiting for approval", "In approve mode each qualified setup appears here and "
        "on Telegram at the same moment, with a 90-second countdown. Approve by typing the "
        "one-time code from your phone; a click alone never approves (UI-C3).", icon="✓"))
    hist = table(today, [("sent_at", "Sent"), ("ticker", "Ticker"), ("setup", "Setup"),
                         ("answer", "Outcome"), ("answered_at", "Answered"),
                         ("proposal_id", "")], "No approvals requested today",
                 {"proposal_id": lambda v, r: link(f"/story/{v}", "story"),
                  "ticker": lambda v, r: stock_link(v)})
    return page_head("Approvals", "The first answer wins, from either channel") + body + \
        card("Today", hist)


def controls_page(csrf: str, st: dict[str, Any], audit: list[dict[str, Any]],
                  suggest: str = "", result: dict[str, Any] | None = None) -> str:
    pos = st.get("positions") or []
    flat = "close " + (", ".join(f"{p['ticker']} {p['qty']} sh" for p in pos) or
                       "nothing (no open positions)") + " at market"
    res = ""
    if result is not None:
        ok = bool(result.get("ok"))
        res = card("Result", f'<p>{chip("SENT" if ok else "REFUSED", "ok" if ok else "bad")} '
                   f'{e(result.get("reason"))}</p>'
                   + (f'<form method="post" action="/cmd" class="inline">{csrf_field(csrf)}'
                      f'<input type="hidden" name="name" value="CONFIRM"><label>Confirmation '
                      f'code <input name="arg" maxlength="8" autocomplete="off" required>'
                      f'</label><button class="danger">Confirm</button></form>'
                      if result.get("need_confirm") else ""))
    sug = (f'<p class="flash">Suggested by Ask: {e(suggest)}. Review it, then press the button '
           f'below.</p>') if suggest else ""
    buttons = " ".join([
        cmd_button(csrf, "STATUS"), cmd_button(csrf, "PLAN", "", "PLAN (build ideas)"),
        cmd_button(csrf, "RECAP", "5", "RECAP week"), cmd_button(csrf, "RECAP", "21",
                                                                 "RECAP month"),
        cmd_button(csrf, "PAUSE", "DAY", "PAUSE DAY"),
        cmd_button(csrf, "PAUSE", "SWING", "PAUSE SWING"), cmd_button(csrf, "ACK"),
        cmd_button(csrf, "ALERTS", "OFF", "Signal alerts OFF"),
        cmd_button(csrf, "ALERTS", "ON", "Signal alerts ON")])
    destructive = " ".join([
        cmd_button(csrf, "FLATTEN", label="FLATTEN", danger=True, confirm=f"FLATTEN: {flat}?"),
        cmd_button(csrf, "RESUME", label="RESUME", confirm="Resume trading after a kill?"),
        cmd_button(csrf, "UNPAUSE", "DAY", "UNPAUSE DAY", confirm="Unpause day trades?"),
        cmd_button(csrf, "UNPAUSE", "SWING", "UNPAUSE SWING", confirm="Unpause swing?")])
    aud = table(audit, [("ts", "Time"), ("text", "Command"), ("accepted", "Accepted"),
                        ("reason", "Result")], "No console commands yet",
                {"accepted": lambda v, r: passfail(v),
                 "reason": lambda v, r: e("confirmation code issued (sent to phone)"
                                          if "confirmation code issued" in (v or "") else v)})
    return (page_head("Controls", "Commands go through the same checks as Telegram")
            + sug + res
            + card("Commands", f'<p>{buttons}</p>')
            + card("Kill switch", "<p>" + cmd_button(
                csrf, "KILL", label="KILL (no confirmation: the safe direction)",
                danger=True) + "</p>"
                   '<p class="muted small">Cancels entry orders and blocks new ones; broker-side '
                   'stops stay.</p>', cls="attn")
            + card("Need the confirmation code from your phone (SEC-3)",
                   f'<p>{destructive}</p><p class="muted small">FLATTEN would {e(flat)}.</p>')
            + card("Audit (source = console)", aud))


def story_page(d: dict[str, Any], chart_html: str, reasons: dict[str, str], csrf: str) -> str:
    def sect(title: str, rows: list[dict[str, Any]], empty_text: str = "none") -> str:
        return card(title, table(rows, empty=empty_text, compact=True))
    decs = d["decisions"]
    expl = "".join(f'<li>{chip(x["reason_code"], "info")} '
                   f'{e(reasons.get(x["reason_code"] or "", "—"))}</li>'
                   for x in decs if x.get("reason_code"))
    checklist = [it for x in decs for it in x.get("checklist_json") or []]
    prop = d["proposal"][0]["json"] if d["proposal"] else {}
    rev = d["review"][0] if d["review"] else {}
    return (page_head(f"Trade story {d['proposal_id'][:12]}", "From trigger to result")
            + (card("Chart", chart_html) if chart_html else "")
            + '<div class="grid2">'
            + card("A5 proposal", f"<pre>{e(prop)}</pre>")
            + card("A6 review", "<p>" + badge("green" if rev.get("verdict") == "agree" else "amber",
                                              rev.get("verdict") or "none") + " "
                   f'{e(rev.get("main_risk"))}</p><pre>{e(rev.get("rules_broken"))}</pre>')
            + "</div>"
            + sect("Setup and trigger", d["setup"])
            + card("Checklist", checklist_table(checklist))
            + card("A7 decision and sizing", table(decs, compact=True)
                   + f'<ul class="plain">{expl}</ul>')
            + sect("A12 compliance", d["compliance"]) + sect("Approval", d["approvals"])
            + sect("Orders", d["orders"]) + sect("Fills", d["fills"])
            + sect("Position and result", d["positions"]) + sect("Latency", d["latency"])
            + card("Owner notes", notes_list(d["notes"]) + note_form(csrf, "proposal",
                                                                     d["proposal_id"])))
