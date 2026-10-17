"""`trader` command line (ARCHITECTURE §2, RUNBOOK §3).

    trader run --service core|news|notifier|alerts
    trader alerts [--alerts-config config/alerts.yaml]   (lightweight Telegram price alerts)
    trader replay --day 2026-10-15 | --synthetic 20 --start 2026-10-05 [--llm rules|ollama]
    trader backtest --synthetic 120 [--walk-forward]
    trader status | kill | resume | flatten --confirm | pause day|swing | unpause day|swing
    trader validate-config [--config path]
    trader select-model [--apply] [--model qwen3:14b]   (Qwen model for this GPU/RAM)
    trader health --service core
    trader migrate | backup --daily-at 18:00 | fetch-history --days 730
    trader eval-prompts [--llm ollama|rules] | gate --record NAME --note TEXT
    trader validate-journal
    trader scan [--date D] [--all] [--json] [--telegram] [--explain]   (technical scanner)
    trader scan-eval [--from D] [--to D] [--minutes]   (walk-forward grading, TS-11)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from trader import config as config_mod
from trader.services.runtime import configure_logging, health_ok


def _journal(create: bool = True) -> Any:
    from trader.journal.store import SqlJournal

    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL is not set")
    return SqlJournal(url, create=create)


def _local_command(name: str, arg: str = "") -> int:
    """Local commands go through the outbox as source=local (instant via NOTIFY)."""
    from trader.bus import SqlOutbox

    j = _journal(create=False)
    asyncio.run(SqlOutbox(j, "core").publish("inbound_cmd", {
        "name": name, "arg": arg, "sender": "local", "source": "local",
        "received_at": datetime.now(UTC).isoformat()}))
    print(f"{name} {arg}".strip() + " sent to trader-core")
    return 0


def cmd_flatten(a: argparse.Namespace) -> int:
    if not a.confirm:
        print("refused: add --confirm (SEC-3)")
        return 1
    return _local_command("FLATTEN")


def cmd_validate_config(a: argparse.Namespace) -> int:
    try:
        loaded = config_mod.load(a.config)
    except config_mod.ConfigError as exc:
        print(f"INVALID: {a.config or os.environ.get('TRADER_CONFIG', 'config/config.yaml')}")
        for p in exc.problems:
            print(f"  - {p}")
        return 1
    except FileNotFoundError as exc:
        print(f"config not found: {exc}")
        return 1
    c = loaded.config
    print(f"OK {loaded.path} sha256={loaded.sha256[:12]} env={c.mode.environment} "
          f"day={c.mode.day_trades} swing={c.mode.swing_trades} model={c.llm.model} "
          f"tickers={','.join(c.universe.tickers)}")
    return 0


def cmd_run(a: argparse.Namespace) -> int:
    configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
    if a.service == "core":
        from trader.services.core_main import main
    elif a.service == "news":
        from trader.services.news_main import main
    elif a.service == "alerts":
        return cmd_alerts(a)
    elif a.service == "console":
        from trader.console.main import main
    else:
        from trader.services.notifier_main import main
    main(a.config)
    return 0


def cmd_alerts(a: argparse.Namespace) -> int:
    """Standalone price-alert bot: Alpaca + Telegram only (no Postgres, Ollama or torch)."""
    configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
    from trader.alerts.service import main

    main(getattr(a, "alerts_config", None))
    return 0


def cmd_select_model(a: argparse.Namespace) -> int:
    """Show which Qwen models fit this machine, recommend one, optionally write it (QW-6)."""
    from trader import hardware as hw_mod

    hw = hw_mod.detect()
    if a.vram is not None:   # plan for another machine, or override detection
        hw = hw_mod.Hardware((hw_mod.Gpu("(given)", a.vram, a.vram_used),), a.ram or hw.ram_gb,
                             hw.cpu_threads)
    elif a.ram is not None:
        hw = hw_mod.Hardware(hw.gpus, a.ram, hw.cpu_threads)
    root = Path(a.root)
    for g in hw.gpus:
        print(f"GPU   {g.name}: {g.total_gb:.1f} GB VRAM, {g.used_gb:.1f} GB in use")
    if not hw.gpus:
        print("GPU   no NVIDIA GPU found (Intel/AMD integrated GPUs cannot run Ollama in Docker)")
    print(f"RAM   {hw.ram_gb or '?'} GB · CPU threads {hw.cpu_threads or '?'}")
    print(f"\n{'model':<15}{'needs':>7}  {'tier':<10}{'on this machine':<13} note")
    for f in hw_mod.assess(hw, a.ctx):
        print(f"{f.profile.tag:<15}{f.needed_gb:>5.1f}GB  {f.profile.tier:<10}{f.status:<13} "
              f"{f.note}")
    best, why = hw_mod.recommend(hw, a.ctx)
    print(f"\nrecommended: {best.tag} ({why})")
    env_model, cfg_model = hw_mod.configured_models(root)
    print(f"configured:  docker/.env OLLAMA_MODEL={env_model or '(unset)'} · "
          f"config/config.yaml llm.model={cfg_model or '(unset)'}")
    if env_model and cfg_model and env_model != cfg_model:
        print("WARNING: the two differ; trader-core will ask for a model that was not "
              "downloaded. Run with --apply to align them.")
    choice = a.model or best.tag
    if a.model and hw_mod.profile(a.model) is None:
        print(f"note: {a.model} is not a known profile; it is used as given")
    fit = next((f for f in hw_mod.assess(hw, a.ctx) if f.profile.tag == choice), None)
    if fit is not None and fit.status not in ("fits", "tight", "cpu-only"):
        print(f"WARNING: {choice} does not fit fully in GPU memory here ({fit.note})")
        if a.apply and not a.force:
            print("refused: add --force to write it anyway")
            return 1
    if not a.apply:
        print(f"\nto use {choice}: trader select-model --apply"
              + (f" --model {choice}" if a.model else ""))
        return 0
    env_path, cfg_path = root / "docker" / ".env", root / "config" / "config.yaml"
    missing = [p for p in (env_path, cfg_path) if not p.exists()]
    if missing:
        print(f"missing {', '.join(map(str, missing))}: run `trader init-env` first")
        return 1
    env_path.write_text(hw_mod.set_env_value(env_path.read_text(encoding="utf-8"),
                                             "OLLAMA_MODEL", choice), encoding="utf-8")
    cfg_path.write_text(hw_mod.set_yaml_llm_model(cfg_path.read_text(encoding="utf-8"), choice),
                        encoding="utf-8")
    print(f"wrote {choice} to {env_path} and {cfg_path}")
    print("next: download it (RUNBOOK §1 step 6 or tools/fetch_ollama_model.py "
          f"{choice}), restart ollama and trader-core, then "
          "`trader check-connections --service llm` must show 100% on GPU")
    return 0


def cmd_health(a: argparse.Namespace) -> int:
    ok = health_ok(a.service, max_age_s=a.max_age)
    print("healthy" if ok else "unhealthy")
    return 0 if ok else 1


def cmd_status(a: argparse.Namespace) -> int:
    from trader.journal import schema as s

    rows = _journal(create=False).rows(s.status_snapshots, order_by="id", descending=True,
                                       limit=1)
    print(json.dumps(rows[0]["json"], indent=1) if rows else "no status yet")
    return 0


def cmd_migrate(a: argparse.Namespace) -> int:
    j = _journal()
    failed = j.apply_grants()
    if failed:
        print("journal tables created; FAILED grants (check DB_ROLE_PREFIX and the roles):")
        for st in failed:
            print(f"  - {st}")
        return 1
    print("journal tables created; grants applied")
    return 0


def cmd_gate(a: argparse.Namespace) -> int:
    _journal().incident(datetime.now(UTC), "GATE_PASSED", f"{a.record}: {a.note}")
    print(f"gate {a.record} recorded")
    return 0


def _llm(kind: str, cfg: config_mod.Config, journal: Any = None, cache: str = "llm_cache"
         ) -> Any:
    from trader.llm.cached import CachedLLM
    from trader.llm.fakes import RulesLLM

    if kind == "rules":
        return RulesLLM()
    from trader.llm.ollama_client import OllamaClient

    url = os.environ.get("OLLAMA_URL", cfg.llm.base_url)
    inner = OllamaClient(url, cfg.llm.model, journal=journal, seed=cfg.llm.seed,
                         num_ctx=cfg.llm.max_context_tokens)
    return CachedLLM(inner, cache, cfg.llm.model, journal) if kind == "cached" else inner


def _cfg(path: str | None) -> config_mod.Config:
    p = path or os.environ.get("TRADER_CONFIG")
    if p and Path(p).exists():
        return config_mod.load(p).config
    if Path("config/config.yaml").exists():
        return config_mod.load("config/config.yaml").config
    return config_mod.load("config/config.example.yaml").config


def cmd_replay(a: argparse.Namespace) -> int:
    from trader.backtest.engine import diff_runs, run_replay
    from trader.backtest.synthetic import generate

    configure_logging("WARNING", json_logs=False)
    cfg = _cfg(a.config)
    if a.mode:
        cfg = cfg.model_copy(update={"mode": cfg.mode.model_copy(update={
            "day_trades": a.mode, "swing_trades": a.mode})})
    out = Path(a.out or f"runs/{date.today().isoformat()}_replay_{a.llm}")
    if a.synthetic:
        data = generate(date.fromisoformat(a.start), a.synthetic, seed=a.seed)
        minutes, daily, news, seed_min = data.minutes, data.daily, data.news, data.seed_minutes
    else:
        from trader.data.store import BarStore, load_recorded_day, resolve_bars_path

        days = [date.fromisoformat(d) for d in a.day]
        minutes, news = {}, {}
        for d in days:
            rec = load_recorded_day(Path(a.recordings) / d.isoformat())
            minutes[d] = rec["bars"]
            news[d] = rec["news"]
        store = BarStore(resolve_bars_path(cfg.storage.bars_path))
        daily = {sym: store.read("daily", sym) for sym in store.symbols("daily")}
        seed_min = [b for sym in store.symbols("minute") for b in store.read("minute", sym)
                    if b.ts < min(minutes[d][0].ts for d in days)]
    t0 = time.time()
    res = asyncio.run(run_replay(cfg, minutes=minutes, daily=daily,
                                 llm=_llm(a.llm, cfg, cache=a.cache), out_dir=out, news=news,
                                 approvals=a.approvals, seed_minutes=seed_min,
                                 name=out.name))
    if a.compare:
        print(diff_runs(Path(a.compare), out))
    m = res.metrics
    print(json.dumps({k: m[k] for k in ("trades", "win_rate", "expectancy_r", "profit_factor",
                                        "pnl", "max_drawdown_pct", "final_equity", "pipeline",
                                        "rejections")}, indent=1, default=str))
    print(f"run folder: {out} ({time.time() - t0:.1f}s)")
    return 0


def _history_data(cfg: config_mod.Config, days: int) -> tuple[dict[date, list[Any]],
                                                             dict[str, list[Any]], list[Any]]:
    """BT-6: real minute bars from the bar store (``trader fetch-history``), by ET day."""
    from trader.data.store import BarStore, resolve_bars_path
    from trader.timeutil import et_date

    store = BarStore(resolve_bars_path(cfg.storage.bars_path))
    daily = {sym: store.read("daily", sym) for sym in store.symbols("daily")}
    by_day: dict[date, list[Any]] = {}
    for sym in store.symbols("minute"):
        for b in store.read("minute", sym):
            by_day.setdefault(et_date(b.ts), []).append(b)
    days_sorted = sorted(by_day)
    if len(days_sorted) < 21:
        raise SystemExit(f"bar store has {len(days_sorted)} days of minute bars; run "
                         "`trader fetch-history --minute-days 500` first (needs >= 21)")
    seed_days, run_days = days_sorted[:20], days_sorted[20:][-days:]
    seed = [b for d in seed_days for b in by_day[d]]
    minutes = {d: sorted(by_day[d], key=lambda b: (b.ts, b.symbol)) for d in run_days}
    return minutes, daily, seed


def _bt6_breakdown(journal_path: Path) -> dict[str, Any]:
    """BT-6: expectancy by setup, window and regime from a replay journal."""
    from trader.journal import schema as s
    from trader.journal.store import SqlJournal
    from trader.timeutil import et_date

    j = SqlJournal(f"sqlite:///{journal_path.as_posix()}", create=False)
    regime = {r["date"]: r["regime"] for r in j.rows(s.regime_daily, order_by="id")}
    groups: dict[str, dict[str, list[float]]] = {"setup": {}, "window": {}, "regime": {}}
    for p in j.closed_positions():
        r = float(p.get("r_multiple") or 0)
        groups["setup"].setdefault(p.get("setup") or "?", []).append(r)
        groups["window"].setdefault(p.get("window") or "?", []).append(r)
        groups["regime"].setdefault(regime.get(et_date(p["closed_at"]), "normal"), []).append(r)
    return {g: {k: {"trades": len(v), "expectancy_r": round(sum(v) / len(v), 3)}
                for k, v in d.items()} for g, d in groups.items()}


WF_KEYS = ("oos", "unstable_params", "statistical", "pass_bar")   # SV-3 in the summary


def cmd_backtest(a: argparse.Namespace) -> int:
    from trader.backtest.synthetic import generate
    from trader.backtest.walkforward import walk_forward

    configure_logging("WARNING", json_logs=False)
    cfg = _cfg(a.config)
    if a.history:   # BT-6: the playbook on real minute bars before paper trading
        from trader.backtest.engine import run_replay

        minutes, daily, seed = _history_data(cfg, a.days)
        out = Path(a.out or f"runs/{date.today().isoformat()}_backtest_history")
        if a.walk_forward:
            res = asyncio.run(walk_forward(cfg, minutes, daily, out, in_sample=a.in_sample,
                                           out_sample=a.out_sample, news={},
                                           seed_minutes=seed))
            (out / "walk_forward.json").write_text(json.dumps(res, indent=1, default=str),
                                                   encoding="utf-8")
            print(json.dumps({k: res[k] for k in WF_KEYS},
                             indent=1, default=str))
            return 0
        rr = asyncio.run(run_replay(cfg, minutes=minutes, daily=daily, llm=_llm("rules", cfg),
                                    out_dir=out, news={}, approvals="yes", seed_minutes=seed,
                                    name=out.name))
        breakdown = {}
        jp = next(out.glob("*.db"), None)
        if jp is not None:
            breakdown = _bt6_breakdown(jp)
            (out / "bt6_breakdown.json").write_text(json.dumps(breakdown, indent=1),
                                                    encoding="utf-8")
        m = rr.metrics
        print(json.dumps({"days": len(minutes), **{k: m[k] for k in (
            "trades", "win_rate", "expectancy_r", "profit_factor", "max_drawdown_pct")},
            "by": breakdown}, indent=1, default=str))
        print(f"run folder: {out}")
        return 0
    data = generate(date.fromisoformat(a.start), a.synthetic, seed=a.seed)
    out = Path(a.out or f"runs/{date.today().isoformat()}_backtest")
    if a.walk_forward:
        res = asyncio.run(walk_forward(cfg, data.minutes, data.daily, out,
                                       in_sample=a.in_sample, out_sample=a.out_sample,
                                       news=data.news, seed_minutes=data.seed_minutes))
        (out / "walk_forward.json").write_text(json.dumps(res, indent=1, default=str),
                                               encoding="utf-8")
        print(json.dumps({k: res[k] for k in WF_KEYS}, indent=1,
                         default=str))
        return 0
    a.synthetic, a.llm, a.approvals, a.compare = a.synthetic, "rules", "yes", None
    a.mode, a.cache, a.day = None, "llm_cache", []
    return cmd_replay(a)


def _journal_from_env(a: argparse.Namespace) -> Any:
    for f in getattr(a, "env_file", None) or []:
        load_env_file(f)
    return _journal(create=False)


def cmd_import_trades(a: argparse.Namespace) -> int:
    """WS-9: other-broker trades -> journal; losses become wash-sale locks."""
    from trader.agents import external_trades as ext
    from trader.agents.a12_compliance import ComplianceGuard
    from trader.calendar import MarketCalendar

    cfg = _cfg(a.config)
    j = _journal_from_env(a)
    trades, skipped = ext.parse_csv(Path(a.file).read_text(encoding="utf-8-sig"), a.broker)
    now = datetime.now(UTC)
    new = ext.store(j, trades, a.broker, a.file, now)
    guard = ComplianceGuard(cfg, MarketCalendar(), j)
    guard.load_from_journal(now)
    created, warnings = ext.apply_locks(guard, ext.load(j), now, cfg.wash_sale.lock_days,
                                        cfg.canonical)
    print(f"{len(trades)} trades read ({new} new, {len(skipped)} rows skipped); "
          f"{created} wash-sale lock(s) created (WS-9)")
    for w in warnings:
        print(f"WARNING: {w}")
    for sk in skipped[:10]:
        print(f"  skipped {sk}")
    return 0


def cmd_restore_test(a: argparse.Namespace) -> int:
    """OPS-1: compare a scratch restore (JSON from the restore-test container) with
    production and record the result."""
    from sqlalchemy import func, select

    from trader.agents import ops
    from trader.journal import schema as s

    j = _journal_from_env(a)
    raw = sys.stdin.read() if a.report == "-" else Path(a.report).read_text(encoding="utf-8")
    line = next((ln for ln in reversed(raw.strip().splitlines()) if ln.startswith("{")), "{}")
    restored = json.loads(line)
    live: dict[str, int] = {}
    with j.engine.connect() as c:
        for t in s.metadata.sorted_tables:
            live[t.name] = int(c.execute(select(func.count()).select_from(t)).scalar() or 0)
        last = c.execute(select(func.max(s.events.c.ts))).scalar()
    from trader.journal.schema import UtcDateTime

    last_dt = UtcDateTime().process_result_value(last, j.engine.dialect) if last else None
    ok, problems = ops.compare_restore(restored, live, last_dt)
    ops.record_check(j, "restore_test", ok, {"problems": problems,
                                             "file": restored.get("file")}, datetime.now(UTC))
    print(("PASS" if ok else "FAIL") + f" restore test of {restored.get('file', '?')}")
    for p in problems:
        print(f"  - {p}")
    return 0 if ok else 1


def cmd_drill(a: argparse.Namespace) -> int:
    """OPS-2: walk through the monthly emergency drill on the paper account and record it."""
    from trader.agents import ops

    j = _journal_from_env(a)
    results: dict[str, Any] = {}
    print("Monthly emergency drill (OPS-2): run it on the PAPER account with one position "
          "open.\n")
    for key, question in ops.DRILL_STEPS:
        ans = input(f"{question} [y/n] ").strip().lower()
        note = input("  note (optional): ").strip()
        results[key] = {"ok": ans.startswith("y"), "note": note}
    passed = all(v["ok"] for v in results.values())
    ops.record_check(j, "drill", passed, results, datetime.now(UTC))
    print("drill recorded: " + ("PASS" if passed else "FAIL (see notes)"))
    return 0 if passed else 1


def cmd_data_gate(a: argparse.Namespace) -> int:
    """DQ-7: evaluate IEX vs SIP divergence; record gate 'data_sip' when it passes."""
    from trader.agents import ops
    from trader.journal import schema as s

    cfg = _cfg(a.config)
    j = _journal_from_env(a)
    ok, detail = ops.data_gate(j.rows(s.data_divergence), cfg.data.sip_gate_days,
                               cfg.data.sip_divergence_pct, cfg.data.sip_divergence_share_pct)
    print(("PASS " if ok else "NOT YET ") + detail)
    if ok and a.record:
        j.incident(datetime.now(UTC), "GATE_PASSED", f"data_sip: {detail}")
        print("gate data_sip recorded")
    return 0 if ok else 1


def cmd_eval(a: argparse.Namespace) -> int:
    from trader.backtest.evaluation import build_cases, evaluate
    from trader.journal.store import SqlJournal

    configure_logging("WARNING", json_logs=False)
    cfg = _cfg(a.config)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    journal = SqlJournal(f"sqlite:///{(out / 'eval_journal.db').as_posix()}")
    cases = build_cases(a.good, a.bad, a.inject)
    res = asyncio.run(evaluate(cfg, _llm(a.llm, cfg, journal), cases, journal))
    (out / "eval.json").write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")
    print(json.dumps({k: v for k, v in res.items() if k != "details"}, indent=1))
    return 0 if res["pass"] else 1


def _data_keys() -> tuple[str, str]:
    for k, s_ in (("ALPACA_PAPER_KEY", "ALPACA_PAPER_SECRET"),
                  ("ALPACA_LIVE_KEY", "ALPACA_LIVE_SECRET"), ("ALPACA_KEY", "ALPACA_SECRET")):
        if os.environ.get(k) and os.environ.get(s_):
            return os.environ[k], os.environ[s_]
    raise SystemExit("no Alpaca data keys in the environment (.env.core)")


def cmd_recap(a: argparse.Namespace) -> int:
    """UI-34: the last N sessions per ticker into ``market_recap``, and the next-session
    watchlist to Telegram (``--no-send`` prints only). Read-only on the broker."""
    from trader.agents.recap import run_recap
    from trader.bus import SqlOutbox
    from trader.calendar import MacroCalendar, MarketCalendar
    from trader.data.alpaca_data import AlpacaHistory
    from trader.notify.whatsapp import OutboxNotifier

    configure_logging(os.environ.get("LOG_LEVEL", "WARNING"))
    cfg = _cfg(a.config)
    j = _journal(create=False)
    now = datetime.now(UTC)
    base = Path(a.config or os.environ.get("TRADER_CONFIG") or "config/config.yaml"
                ).resolve().parent.parent
    try:
        macro = MacroCalendar.load(base / cfg.windows.macro_calendar_file)
    except Exception as exc:   # noqa: BLE001
        print(f"  macro calendar unavailable: {exc}")
        macro = None
    rows, text = asyncio.run(run_recap(cfg, j, AlpacaHistory(*_data_keys(),
                                                             feed=cfg.data.alpaca_feed),
                                       MarketCalendar(), macro, a.days, now))
    if not rows:
        print("no daily bars returned: check the Alpaca keys (./run.sh connections)")
        return 1
    print(text)
    if not a.no_send:
        asyncio.run(OutboxNotifier(SqlOutbox(j, "core"), lambda: now).send(text, kind="recap"))
        print("\nsent to Telegram via the notifier")
    return 0


def cmd_backfill_news(a: argparse.Namespace) -> int:
    """Last N days of news, filings and analyst actions into the journal (news container)."""
    from trader.agents.a03_news import NewsAgent, make_scorer
    from trader.calendar import MacroCalendar, MarketCalendar
    from trader.news.ratings import RatingsFeed
    from trader.news.sources import EdgarFilings, FinnhubNews
    from trader.services.news_main import cfg_company_names

    configure_logging(os.environ.get("LOG_LEVEL", "WARNING"))
    cfg = _cfg(a.config)
    j = _journal(create=False)
    sources: list[Any] = []
    if os.environ.get("FINNHUB_TOKEN"):
        sources.append(FinnhubNews(os.environ["FINNHUB_TOKEN"]))
    else:
        print("  FINNHUB_TOKEN not set: company news skipped (RSS ratings still run)")
    sources.append(EdgarFilings(os.environ.get("SEC_USER_AGENT") or cfg.data.sec_user_agent))
    sources.append(RatingsFeed(j, os.environ.get("FINNHUB_TOKEN"),
                               names=cfg_company_names(cfg)))
    base = Path(a.config or os.environ.get("TRADER_CONFIG") or "config/config.yaml"
                ).resolve().parent.parent
    agent = NewsAgent(cfg, sources, make_scorer(), None, j, MarketCalendar(),
                      MacroCalendar.load(base / cfg.windows.macro_calendar_file),
                      lambda: datetime.now(UTC))
    counts = asyncio.run(agent.backfill(a.days))
    print(f"backfilled {a.days} days: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
    for name, err in agent.source_errors.items():
        print(f"  {name} failed: {err[:200]}")
    return 0


def cmd_fetch_history(a: argparse.Namespace) -> int:
    from trader.data.alpaca_data import AlpacaHistory
    from trader.data.store import BarStore, resolve_bars_path

    cfg = _cfg(a.config)
    key, secret = os.environ["ALPACA_PAPER_KEY"], os.environ["ALPACA_PAPER_SECRET"]
    hist = AlpacaHistory(key, secret, feed=cfg.data.alpaca_feed)
    store = BarStore(resolve_bars_path(cfg.storage.bars_path))
    end = date.today()

    async def go() -> None:
        from trader.catalyst.agent import SECTOR, extended_tickers

        syms = dict.fromkeys([*cfg.universe.tickers, *cfg.universe.context_symbols,
                              *cfg.scanner.symbols, *extended_tickers(cfg),
                              *set(SECTOR.values()),    # NC-30: event-study history
                              *cfg.forecast.sector_etfs])   # FC-5
        for sym in syms:
            daily = await hist.daily_bars(sym, end - timedelta(days=a.days), end)
            store.write("daily", sym, daily)
            if a.daily_only:      # NC-30 history: leave the stored minute bars alone
                print(f"{sym}: {len(daily)} daily bars")
                continue
            mins = await hist.minute_bars(sym, datetime.now(UTC) - timedelta(days=a.minute_days),
                                          datetime.now(UTC))
            store.write("minute", sym, mins, keep_days=a.minute_days + 2)
            print(f"{sym}: {len(daily)} daily, {len(mins)} minute bars")
    asyncio.run(go())
    return 0


def _scan_inputs(cfg: config_mod.Config) -> tuple[Path, dict[str, Any], Any]:
    from trader.data.store import resolve_bars_path
    from trader.scanner.data import load_daily

    root = resolve_bars_path(cfg.storage.bars_path)
    syms = list(dict.fromkeys([*cfg.universe.tickers, *cfg.scanner.symbols, "SPY"]))
    series = load_daily(root, syms)
    spy = series.pop("SPY", None)
    return root, series, spy


def _sector_series(cfg: config_mod.Config) -> Any:
    from trader.data.store import resolve_bars_path
    from trader.scanner.data import load_daily

    return load_daily(resolve_bars_path(cfg.storage.bars_path), ["SMH"]).get("SMH")


def cmd_scan(a: argparse.Namespace) -> int:
    """TS-14: swing ideas and the next-session day watchlist from stored daily bars."""
    from trader.calendar import MacroCalendar, MarketCalendar
    from trader.notify.telegram import TelegramNotifier
    from trader.scanner import report
    from trader.scanner.checks import apply_checks, base_context, simple_sizer
    from trader.scanner.evaluate import load_edges
    from trader.scanner.inplay import in_play
    from trader.scanner.techniques import scan

    for f in a.env_file or []:
        load_env_file(f)
    cfg = _cfg(a.config)
    sc = cfg.scanner
    root, series, spy = _scan_inputs(cfg)
    if not series:
        print(f"no daily bars under {root}: run `trader fetch-history` (./run.sh fetch-history)")
        return 1
    asof = date.fromisoformat(a.date) if a.date else None
    edges, edges_src = load_edges(sc.eval_file)
    ideas, skipped = scan(series, spy, asof, frozenset(sc.disabled), edges)
    plays = in_play(series, asof)
    day = asof or max(s.dates[-1] for s in series.values())
    sector = _sector_series(cfg)
    if sector is not None:
        sector = sector.upto(sum(1 for d in sector.dates if d <= day))
    try:
        base = Path(a.config or os.environ.get("TRADER_CONFIG") or "config/config.yaml"
                    ).resolve().parent.parent
        macro: Any = MacroCalendar.load(base / cfg.windows.macro_calendar_file)
    except Exception:   # noqa: BLE001 - the macro check then shows "unknown"
        macro = None
    ctx = base_context(cfg, asof=day, edges=edges, macro=macro, sector=sector,
                       calendar=MarketCalendar())
    if a.equity:
        ctx.sizer = simple_sizer(a.equity, cfg.risk.risk_per_trade_pct,
                                 cfg.risk.max_position_pct, cfg.risk.max_leverage)
    ideas = apply_checks(ideas, ctx)
    payload = report.as_json(day, ideas, plays, skipped, generated=datetime.now(UTC),
                             source="cli", edges_from=edges_src, min_score=sc.min_score,
                             top_n=sc.top_n, context_note=(
                                 "CLI run: earnings, locks and positions are known only to "
                                 "trader-core; those checks show as unknown."))
    if a.date is None and not a.no_write:
        report.write_latest(sc.latest_file, payload)
    if a.json:
        print(json.dumps(payload, indent=1, default=str))
        return 0
    summary = None
    if a.explain:
        from trader.scanner.explain import explain

        try:
            shown = [d for d in ideas if d.score >= sc.min_score][:sc.top_n]
            summary = asyncio.run(explain(_llm("ollama", cfg), shown,
                                          plays[:sc.in_play_top_n]))
        except Exception as exc:   # noqa: BLE001 - the scan never depends on the model
            print(f"  Qwen summary skipped: {exc}")
        if summary is None:
            print("  Qwen summary unavailable or rejected (numbers not in the data)")
    if asof is None and series:
        asof = max(s.dates[-1] for s in series.values())
    text = report.render(asof, ideas, plays, skipped,
                         top_n=len(ideas) if a.all else sc.top_n, in_play_n=sc.in_play_top_n,
                         min_score=0 if a.all else sc.min_score, summary=summary,
                         edges_from=edges_src)
    print(text)
    if a.telegram:
        token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
        if token and chat:
            ok = asyncio.run(TelegramNotifier(token, chat).send(text, kind="scan"))
        elif os.environ.get("DATABASE_URL"):     # trader-core: the notifier service delivers
            from trader.bus import SqlOutbox
            from trader.notify.whatsapp import OutboxNotifier

            now = datetime.now(UTC)
            asyncio.run(OutboxNotifier(SqlOutbox(_journal(create=False), "core"),
                                       lambda: now).send(text, kind="scan"))
            ok = True
        else:
            print("no TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID and no DATABASE_URL: use "
                  "--env-file .env.alerts, or ./run.sh scan --telegram (via the notifier)")
            return 1
        print("sent to Telegram" if ok else "Telegram send failed")
        return 0 if ok else 1
    return 0


def cmd_scan_eval(a: argparse.Namespace) -> int:
    """TS-11/TS-12: grade every past idea walk-forward; write the edges file the scan uses."""
    from trader.scanner import report
    from trader.scanner.data import MinuteBars
    from trader.scanner.evaluate import edges_from, walk_forward
    from trader.scanner.evaluate import report as eval_report
    from trader.scanner.inplay import evaluate_in_play

    cfg = _cfg(a.config)
    sc = cfg.scanner
    root, series, spy = _scan_inputs(cfg)
    if not series:
        print(f"no daily bars under {root}: run `trader fetch-history` (./run.sh fetch-history)")
        return 1
    start = date.fromisoformat(a.start) if a.start else None
    end = date.fromisoformat(a.end) if a.end else None
    graded = walk_forward(series, spy, start=start, end=end, max_hold=sc.max_hold_days,
                          minutes=MinuteBars(root) if a.minutes else None)
    rep = eval_report(graded, sc.min_samples)
    ip = evaluate_in_play(series, sc.in_play_top_n if a.top is None else a.top, start, end)
    print(report.eval_text(rep, ip))
    print("\nGraded without scanner.disabled, so turned-off techniques keep being measured.")
    if not a.no_write:
        out = Path(sc.eval_file)
        out.parent.mkdir(parents=True, exist_ok=True)
        from trader.timeutil import ET

        payload = {"generated": datetime.now(ET).isoformat(), "from": a.start, "to": a.end,
                   "edges": edges_from(graded), "report": rep, "in_play": ip}
        out.write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")
        print(f"wrote {out} (the scan's measured-edge input)")
    return 0


def _send_text(text: str, kind: str) -> bool:
    """Telegram directly with bot env vars, else through the notifier via the outbox."""
    from trader.notify.telegram import TelegramNotifier

    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat:
        return bool(asyncio.run(TelegramNotifier(token, chat).send(text, kind=kind)))
    if os.environ.get("DATABASE_URL"):
        from trader.bus import SqlOutbox
        from trader.notify.whatsapp import OutboxNotifier

        now = datetime.now(UTC)
        asyncio.run(OutboxNotifier(SqlOutbox(_journal(create=False), "core"),
                                   lambda: now).send(text, kind=kind))
        return True
    print("no TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID and no DATABASE_URL")
    return False


def _catalyst_agent(a: argparse.Namespace, cfg: config_mod.Config, llm: bool = False) -> Any:
    from trader.calendar import MacroCalendar, MarketCalendar
    from trader.catalyst.agent import CatalystAgent, parquet_loader
    from trader.data.store import resolve_bars_path

    base = Path(a.config or os.environ.get("TRADER_CONFIG") or "config/config.yaml"
                ).resolve().parent.parent
    try:
        macro: Any = MacroCalendar.load(base / cfg.windows.macro_calendar_file)
    except Exception:   # noqa: BLE001 - macro events then show as unknown
        macro = None
    from trader.catalyst.agent import combined_loader, history_loader

    fallback = None
    key, secret = os.environ.get("ALPACA_PAPER_KEY"), os.environ.get("ALPACA_PAPER_SECRET")
    if key and secret:     # bars the store lacks come from Alpaca, as in trader-core
        from trader.data.alpaca_data import AlpacaHistory

        fallback = history_loader(AlpacaHistory(key, secret, feed=cfg.data.alpaca_feed))
    loader = combined_loader(parquet_loader(resolve_bars_path(cfg.storage.bars_path)),
                             fallback)
    return CatalystAgent(cfg, _journal(create=False), loader,
                         _llm("ollama", cfg) if llm else None, lambda: datetime.now(UTC),
                         MarketCalendar(), macro)


def cmd_catalyst(a: argparse.Namespace) -> int:
    """NC-29: live catalysts with strength, outlook and position view (advisory)."""
    from trader.catalyst import report
    from trader.scanner.checks import simple_sizer

    configure_logging(os.environ.get("LOG_LEVEL", "WARNING"), json_logs=False)
    cfg = _cfg(a.config)
    agent = _catalyst_agent(a, cfg, llm=a.model)
    sizer = simple_sizer(a.equity, cfg.risk.risk_per_trade_pct, cfg.risk.max_position_pct,
                         cfg.risk.max_leverage) if a.equity else None
    tickers = [a.ticker.upper()] if a.ticker else None
    asof = date.fromisoformat(a.date) if a.date else None
    outs = asyncio.run(agent.run(tickers, asof, sizer=sizer,
                                 write=not a.no_write and asof is None,
                                 llm_budget=20 if a.model else 0))
    data = [o.as_dict() for o in outs]
    ev = agent.load_eval() or {}
    if a.json:
        print(json.dumps(data, indent=1, default=str))
        return 0
    day = (asof or date.today()).isoformat()
    text = report.render(data, as_of=day, track=ev.get("horizons"), top_n=len(data) or 1)
    if not ev:
        text += ("\n\nNo graded history yet: run `./run.sh backfill-catalysts 365`, then "
                 "the ranges carry measured tilts instead of only the volatility range.")
    print(text)
    if a.telegram:
        ok = _send_text(text, "catalyst")
        print("sent to Telegram" if ok else "Telegram send failed")
        return 0 if ok else 1
    return 0


def cmd_catalyst_eval(a: argparse.Namespace) -> int:
    """NC-30..NC-34: walk-forward grading, feature and alert gates; writes the eval file."""
    configure_logging(os.environ.get("LOG_LEVEL", "WARNING"), json_logs=False)
    cfg = _cfg(a.config)
    agent = _catalyst_agent(a, cfg)
    res = asyncio.run(agent.evaluate(a.days))
    print(f"{res['events']} events on {res['tickers']} tickers, {res['from']} to {res['to']}")
    for h, m in res["horizons"].items():
        if m.get("graded"):
            print(f"  {h:>2} sessions: {m['graded']} graded, Brier {m['brier']:.4f} (coin "
                  f"0.25, climatology {m['brier_climatology']:.4f}), range hit "
                  f"{m['coverage']:.0%}")
        else:
            print(f"  {h:>2} sessions: not enough history to grade yet")
    t = res["feature_gate"]["tilt"]
    print(f"news tilt (NC-49): {'kept' if t['kept'] else 'OFF'} (Brier with tilt "
          f"{t['with_tilt_brier']}, without {t['no_tilt_brier']})")
    print(f"adjustments kept (NC-33): {', '.join(res['adjustments']) or 'none'}")
    print(f"alert gate (NC-34): {'PASS' if res['gate']['pass'] else 'not passed'}")
    print(f"wrote {cfg.catalyst.eval_file}")
    return 0


def cmd_backfill_catalysts(a: argparse.Namespace) -> int:
    """NC-30: news history for the event study plus today's fundamentals (news container)."""
    from trader.agents.a03_news import make_scorer
    from trader.catalyst.agent import extended_tickers
    from trader.catalyst.collect import (
        Enricher,
        FundamentalsFeed,
        backfill_news,
        embed_backlog,
    )
    from trader.catalyst.text import make_embedder
    from trader.news.sources import EdgarFilings, FinnhubNews

    configure_logging(os.environ.get("LOG_LEVEL", "WARNING"), json_logs=False)
    cfg = _cfg(a.config)
    j = _journal(create=False)
    tickers = extended_tickers(cfg)
    ua = os.environ.get("SEC_USER_AGENT") or cfg.data.sec_user_agent
    sources: list[Any] = [EdgarFilings(ua)]
    token = os.environ.get("FINNHUB_TOKEN")
    if token:
        sources.insert(0, FinnhubNews(token))
    else:
        print("  FINNHUB_TOKEN not set: company news skipped (SEC filings still run)")
    embedder = make_embedder(cfg.catalyst.embed_model)
    print(f"{len(tickers)} tickers, {a.days} days; embeddings: {embedder.name}")
    counts = asyncio.run(backfill_news(j, sources, tickers, a.days, make_scorer(),
                                       Enricher(j, embedder, None)))
    print("news: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
    n = asyncio.run(embed_backlog(j, embedder, datetime.now(UTC) - timedelta(days=a.days)))
    print(f"embedded {n} older items")
    if not a.skip_fundamentals:
        feed = FundamentalsFeed(j, token, ua)
        today = date.today()

        async def fund() -> dict[str, int]:
            ciks = await EdgarFilings(ua).cik_map()
            return {"finnhub": await feed.finnhub(tickers, today),
                    "insiders": await feed.insiders(tickers, today, ciks),
                    "short_volume": await feed.short_volume(tickers, today)}
        got = asyncio.run(fund())
        print("fundamentals: " + ", ".join(f"{k} {v}" for k, v in got.items()))
        for name, err in feed.errors.items():
            print(f"  {name}: {err}")
        if feed.off:
            print(f"  not on this plan (skipped): {', '.join(sorted(feed.off))}")
    return 0


def _bar_loader(cfg: config_mod.Config) -> Any:
    """Bar store first, Alpaca for what it lacks (catalyst, macro and forecast commands)."""
    from trader.catalyst.agent import combined_loader, history_loader, parquet_loader
    from trader.data.store import resolve_bars_path

    fallback = None
    key, secret = os.environ.get("ALPACA_PAPER_KEY"), os.environ.get("ALPACA_PAPER_SECRET")
    if key and secret:
        from trader.data.alpaca_data import AlpacaHistory

        fallback = history_loader(AlpacaHistory(key, secret, feed=cfg.data.alpaca_feed))
    return combined_loader(parquet_loader(resolve_bars_path(cfg.storage.bars_path)), fallback)


def _macro_calendar(a: argparse.Namespace, cfg: config_mod.Config) -> Any:
    from trader.calendar import MacroCalendar

    base = Path(a.config or os.environ.get("TRADER_CONFIG") or "config/config.yaml"
                ).resolve().parent.parent
    try:
        return MacroCalendar.load(base / cfg.windows.macro_calendar_file)
    except Exception:   # noqa: BLE001
        return None


def cmd_macro(a: argparse.Namespace) -> int:
    """MA-8 / MA-9: today's macro regime, releases and headlines (trader-core container)."""
    from trader.macro.agent import MacroAgent, render

    configure_logging(os.environ.get("LOG_LEVEL", "WARNING"), json_logs=False)
    cfg = _cfg(a.config)
    agent = MacroAgent(cfg, _journal(create=False), _bar_loader(cfg), lambda: datetime.now(UTC))
    if a.eval:
        print(json.dumps(asyncio.run(agent.evaluate()), indent=1, default=str))
        return 0
    payload = asyncio.run(agent.run(date.fromisoformat(a.date) if a.date else None,
                                    write=not a.no_write and not a.date))
    if a.json:
        print(json.dumps(payload, indent=1, default=str))
        return 0
    text = render(payload)
    print(text)
    if a.telegram:
        ok = _send_text(text, "macro")
        print("sent to Telegram" if ok else "Telegram send failed")
        return 0 if ok else 1
    return 0


def cmd_macro_collect(a: argparse.Namespace) -> int:
    """MA-2 / MA-4: FRED series and macro headlines into the journal (news container)."""
    from trader.agents.a03_news import make_scorer
    from trader.macro.agent import collect
    from trader.macro.sources import MacroFeeds

    configure_logging(os.environ.get("LOG_LEVEL", "WARNING"), json_logs=False)
    cfg = _cfg(a.config)
    feeds = MacroFeeds(os.environ.get("FINNHUB_TOKEN"),
                       os.environ.get("SEC_USER_AGENT") or cfg.data.sec_user_agent)
    got = asyncio.run(collect(_journal(create=False), feeds, cfg.macro.series, make_scorer(),
                              history=a.history, categories=cfg.macro.news_categories))
    print("stored: " + ", ".join(f"{k} {v}" for k, v in got.items()))
    for k, v in feeds.errors.items():
        print(f"  {k}: {v}")
    return 0


def cmd_forecast(a: argparse.Namespace) -> int:
    """FC-6: volatility range, gap odds, macro-day reactions and sector ranks."""
    from trader.forecast.agent import ForecastAgent, render

    configure_logging(os.environ.get("LOG_LEVEL", "WARNING"), json_logs=False)
    cfg = _cfg(a.config)
    agent = ForecastAgent(cfg, _bar_loader(cfg), _macro_calendar(a, cfg),
                          lambda: datetime.now(UTC))
    tks = [a.ticker.upper()] if a.ticker else None
    if a.eval:
        res = asyncio.run(agent.evaluate(tks))
        for t, v in res["volatility"].items():
            est = v["estimators"]
            print(f"{t}: best {v['best']} · " + ", ".join(
                f"{m} QLIKE {e['qlike']} cover {e['coverage_80']}" for m, e in est.items()
                if e["qlike"] is not None))
        rot = res["rotation"]
        if rot.get("months"):
            print(f"sector rotation: top 3 minus bottom 3 {rot['avg_top_minus_bottom_pct']:+.2f}%"
                  f" a month over {rot['months']} months, hit rate {rot['hit_rate']:.0%}, "
                  f"t {rot['t_stat']}")
        print(f"wrote {cfg.forecast.eval_file}")
        return 0
    payload = asyncio.run(agent.run(tks, write=not a.no_write and tks is None))
    if a.json:
        print(json.dumps(payload, indent=1, default=str))
        return 0
    print(render(payload, tks[0] if tks else None))
    return 0


def _momentum_agent(a: argparse.Namespace, cfg: config_mod.Config, *, fetch: bool = True
                    ) -> Any:
    """A19 with the SIP store (topped up from Alpaca when keys are set) and, in trader-core's
    container, the journal for the tracker."""
    from trader.data.store import resolve_bars_path
    from trader.momentum.agent import MomentumAgent, refresh_loader, store_loader
    from trader.scanner.checks import simple_sizer

    root = resolve_bars_path(cfg.storage.bars_path)
    load = store_loader(root)
    key, secret = os.environ.get("ALPACA_PAPER_KEY"), os.environ.get("ALPACA_PAPER_SECRET")
    if fetch and key and secret:
        from trader.data.alpaca_data import AlpacaHistory

        load = refresh_loader(root, AlpacaHistory(key, secret, feed=cfg.momentum.feed),
                              lambda: datetime.now(UTC))
    equity = getattr(a, "equity", None)
    sizer = simple_sizer(equity, cfg.momentum.risk_pct or cfg.risk.risk_per_trade_pct,
                         cfg.risk.max_position_pct, cfg.risk.max_leverage) if equity else None
    db = _journal(create=False) if os.environ.get("DATABASE_URL") else None
    journal = db
    label = None
    if db is not None:
        from trader.macro.agent import latest_label

        try:
            label = (latest_label(db, date.today()) or {}).get("label")
        except Exception:   # noqa: BLE001 - the regime then ignores the macro view
            label = None
    return MomentumAgent(cfg, journal, load, lambda: datetime.now(UTC), sizer=sizer,
                         equity=equity, macro_label=label)


def cmd_momentum_history(a: argparse.Namespace) -> int:
    """MO-22: SIP daily bars for momentum.universe + SPY into the bar store (kind daily_sip)."""
    from trader.data.alpaca_data import AlpacaHistory
    from trader.data.store import BarStore, resolve_bars_path

    cfg = _cfg(a.config)
    hist = AlpacaHistory(os.environ["ALPACA_PAPER_KEY"], os.environ["ALPACA_PAPER_SECRET"],
                         feed=cfg.momentum.feed)
    store = BarStore(resolve_bars_path(cfg.storage.bars_path))
    end = datetime.now(UTC) - timedelta(minutes=16)
    start = date.today() - timedelta(days=a.days or cfg.momentum.history_days)
    syms = list(dict.fromkeys([*cfg.momentum.universe, *cfg.universe.tickers, "SPY"]))

    async def go() -> int:
        bad = 0
        for sym in syms:
            try:
                bars = await hist.daily_bars_until(sym, start, end)
            except Exception as exc:   # noqa: BLE001
                print(f"{sym}: failed ({exc})")
                bad += 1
                continue
            store.write("daily_sip", sym, bars)
            print(f"{sym}: {len(bars)} daily bars"
                  + (f" from {bars[0].ts.date()}" if bars else ""))
        return bad
    bad = asyncio.run(go())
    print(f"{len(syms) - bad} of {len(syms)} symbols stored (kind daily_sip)")
    return 0 if bad < len(syms) else 1


def cmd_momentum(a: argparse.Namespace) -> int:
    """MO-20: leaderboard, buy-stops and (with the journal) the tracked signals."""
    from trader.momentum.agent import render

    configure_logging(os.environ.get("LOG_LEVEL", "WARNING"), json_logs=False)
    cfg = _cfg(a.config)
    agent = _momentum_agent(a, cfg)
    asof = date.fromisoformat(a.date) if a.date else None
    payload = asyncio.run(agent.run(asof, write=not a.no_write and asof is None,
                                    track=a.track and asof is None))
    if a.json:
        print(json.dumps(payload, indent=1, default=str))
        return 0
    text = render(payload, show_unproven=a.all or cfg.momentum.alert_unproven)
    print(text)
    if a.telegram:
        ok = _send_text(text, "momentum")
        print("sent to Telegram" if ok else "Telegram send failed")
        return 0 if ok else 1
    return 0


def cmd_momentum_eval(a: argparse.Namespace) -> int:
    """MO-11..MO-13: walk-forward grading; writes momentum.eval_file (the alert gate)."""
    from trader.momentum.report import eval_text

    configure_logging(os.environ.get("LOG_LEVEL", "WARNING"), json_logs=False)
    cfg = _cfg(a.config)
    agent = _momentum_agent(a, cfg, fetch=False)
    res = asyncio.run(agent.evaluate(date.fromisoformat(a.start) if a.start else None,
                                     date.fromisoformat(a.end) if a.end else None,
                                     write=not a.no_write))
    print(eval_text(res))
    if not a.no_write:
        print(f"wrote {cfg.momentum.eval_file}")
    return 0


def cmd_events(a: argparse.Namespace) -> int:
    """EV-7: recurring company events, their past price pattern and dated trade ideas."""
    from trader.data.store import resolve_bars_path
    from trader.events.agent import EventsAgent, render
    from trader.momentum.agent import refresh_loader, store_loader

    configure_logging(os.environ.get("LOG_LEVEL", "WARNING"), json_logs=False)
    cfg = _cfg(a.config)
    root = resolve_bars_path(cfg.storage.bars_path)
    load = store_loader(root)
    key, secret = os.environ.get("ALPACA_PAPER_KEY"), os.environ.get("ALPACA_PAPER_SECRET")
    if key and secret:
        from trader.data.alpaca_data import AlpacaHistory

        load = refresh_loader(root, AlpacaHistory(key, secret, feed=cfg.momentum.feed),
                              lambda: datetime.now(UTC))
    db = _journal(create=False) if os.environ.get("DATABASE_URL") else None
    agent = EventsAgent(cfg, load, lambda: datetime.now(UTC), journal=db)
    payload = asyncio.run(agent.run(write=not a.no_write))
    if a.json:
        print(json.dumps(payload, indent=1, default=str))
        return 0
    text = render(payload, ticker=a.ticker.upper() if a.ticker else None)
    print(text)
    if a.telegram:
        ok = _send_text(text, "events")
        print("sent to Telegram" if ok else "Telegram send failed")
        return 0 if ok else 1
    return 0


def cmd_backup(a: argparse.Namespace) -> int:
    """Nightly pg_dump + Parquet copy (backup container)."""
    configure_logging()
    dest = Path(os.environ.get("BACKUP_DIR", "/backups"))
    hh, mm = (int(x) for x in a.daily_at.split(":"))
    last = None
    while True:
        from trader.timeutil import to_et

        now = to_et(datetime.now(UTC))
        if (now.hour, now.minute) >= (hh, mm) and last != now.date():
            last = now.date()
            dest.mkdir(parents=True, exist_ok=True)
            url = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")
            out = dest / f"trader_{now:%Y%m%d}.dump"
            pg_dump = shutil.which("pg_dump") or "pg_dump"
            res = subprocess.run([pg_dump, "--format=custom", f"--file={out}", url],  # noqa: S603
                                 check=False, capture_output=True, text=True)
            if res.returncode != 0:
                # never report a failed backup as healthy (OPS-1 found this silently failing)
                print(f"BACKUP FAILED: pg_dump exit {res.returncode}: {res.stderr.strip()[:500]}",
                      file=sys.stderr, flush=True)
                out.unlink(missing_ok=True)
                last = None   # retry in a minute
                time.sleep(60)
                continue
            bars = Path("/app/data/bars")
            if bars.exists():
                shutil.copytree(bars, dest / f"bars_{now:%Y%m%d}", dirs_exist_ok=True)
            Path("/tmp/trader-backup.health").write_text(now.isoformat())  # noqa: S108
        if a.once:
            return 0
        time.sleep(60)


def load_env_file(path: str) -> None:
    """Load KEY=value lines (no quotes, no inline comments) into os.environ."""
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def cmd_check(a: argparse.Namespace) -> int:
    """Read-only checks of external credentials: no orders, no messages (ACCOUNT_SETUP §8)."""
    for f in a.env_file or []:
        load_env_file(f)
    results: list[tuple[str, bool, str]] = []

    async def check(name: str, coro: Any) -> None:
        try:
            detail = await asyncio.wait_for(coro, 30)
            results.append((name, True, str(detail)))
        except Exception as exc:   # noqa: BLE001 - report every failure
            results.append((name, False, f"{type(exc).__name__}: {str(exc)[:200]}"))

    async def alpaca() -> None:
        from trader.brokers.alpaca_adapter import AlpacaBroker
        from trader.data.alpaca_data import AlpacaHistory

        key, secret = os.environ["ALPACA_PAPER_KEY"], os.environ["ALPACA_PAPER_SECRET"]
        b = AlpacaBroker(key, secret, paper=True)

        async def account() -> str:
            acct = await b.account()
            return f"paper account {acct.status}, equity ${acct.equity:,.2f}"

        async def drift() -> str:
            d = (datetime.now(UTC) - await b.clock_time()).total_seconds()
            if abs(d) > 2:
                raise RuntimeError(f"clock drift {d:+.2f}s > 2s (A10 would refuse to start)")
            return f"clock drift {d:+.2f}s"

        async def assets() -> str:
            cfg = _cfg(a.config)
            bad = [t for t in cfg.universe.tickers if not await b.is_tradable(t)]
            if bad:
                raise RuntimeError(f"not tradable on Alpaca: {', '.join(bad)} (UR-3)")
            return f"all {len(cfg.universe.tickers)} tickers tradable"

        async def history() -> str:
            h = AlpacaHistory(key, secret)
            end = date.today()
            daily = await h.daily_bars("SPY", end - timedelta(days=120), end)
            mins = await h.minute_bars("SPY", datetime.now(UTC) - timedelta(days=5),
                                       datetime.now(UTC) - timedelta(minutes=20))
            if len(daily) < 60:
                raise RuntimeError(f"only {len(daily)} daily bars")
            return f"IEX history ok: {len(daily)} daily, {len(mins)} 1-min bars (SPY)"

        await check("alpaca account", account())
        await check("alpaca clock", drift())
        await check("alpaca assets", assets())
        await check("alpaca history", history())

    async def news() -> None:
        from trader.news.sources import EdgarFilings, FinnhubNews

        async def finnhub() -> str:
            fh = FinnhubNews(os.environ["FINNHUB_TOKEN"])
            items = await fh.fetch(["NVDA"], datetime.now(UTC) - timedelta(days=2))
            caps = await fh.market_caps(["NVDA"])
            return f"{len(items)} NVDA items; market cap ${caps.get('NVDA', 0) / 1e12:.2f}T"

        async def edgar() -> str:
            ua = os.environ.get("SEC_USER_AGENT") or _cfg(a.config).data.sec_user_agent
            filings = await EdgarFilings(ua).fetch(["NVDA"], datetime.now(UTC) - timedelta(
                days=120))
            return f"{len(filings)} NVDA 8-K/6-K filings in 120 days"

        await check("finnhub", finnhub())
        await check("sec edgar", edgar())

    async def notifier() -> None:
        from trader.services.notifier_main import channel_from_env

        async def telegram() -> str:
            from trader.notify.telegram import TelegramNotifier

            token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
            chat = os.environ.get("TELEGRAM_CHAT_ID", "")
            if not token:
                raise RuntimeError("TELEGRAM_BOT_TOKEN is empty")
            me = await TelegramNotifier(token, chat or "0").get_me()
            if not chat:
                raise RuntimeError(f"bot @{me['username']} ok, but TELEGRAM_CHAT_ID is empty: "
                                   "message the bot, then run `trader telegram-chats`")
            return f"bot @{me['username']} ok, chat {chat} (no message sent)"

        async def twilio() -> str:
            sid, token = os.environ.get("TWILIO_SID", ""), os.environ.get("TWILIO_TOKEN", "")
            if not token:
                raise RuntimeError("TWILIO_TOKEN is empty")
            from twilio.rest import Client

            acct = await asyncio.to_thread(lambda: Client(sid, token).api.v2010.accounts(
                sid).fetch())
            return f"account '{acct.friendly_name}' {acct.status} (no message sent)"

        if channel_from_env() == "telegram":
            await check("telegram", telegram())
        else:
            await check("twilio", twilio())

    async def llm() -> None:
        from trader.llm.ollama_client import OllamaClient
        from trader.models.llm_schemas import NewsSummaryOut

        cfg = _cfg(a.config)
        url = os.environ.get("OLLAMA_URL", cfg.llm.base_url)
        client = OllamaClient(url, cfg.llm.model, num_ctx=cfg.llm.max_context_tokens,
                              keep_alive=cfg.llm.keep_alive)

        async def model() -> str:
            if not await client.healthy():
                raise RuntimeError(f"Ollama at {url} is down or model {cfg.llm.model} is not "
                                   "pulled (run the model-pull service)")
            return f"{cfg.llm.model} present at {url}"

        async def generate() -> str:
            res = await client.structured(
                agent="check", prompt_version="v1",
                system="Summarize the items for a trader. Return JSON matching the schema.",
                data={"ticker": "NVDA", "items": [{"headline": "Nvidia signs a supply deal",
                                                   "score": 0.6}]},
                schema=NewsSummaryOut, timeout_s=180)
            if not res.ok:
                raise RuntimeError(res.error or "invalid output")
            return f"valid JSON in {res.latency_ms / 1000:.1f}s ({res.output_tokens} tokens)"

        async def gpu() -> str:
            st = await client.gpu_status()
            m = st.get(cfg.llm.model) or next(iter(st.values()), None)
            if m is None:
                raise RuntimeError("model not loaded")
            if m["gpu_pct"] < 100:
                raise RuntimeError(f"only {m['gpu_pct']}% of {m['size_gb']} GB in GPU memory: "
                                   "part runs on the CPU (slow); use a smaller model or context")
            return f"100% on GPU ({m['vram_gb']} GB VRAM)"

        await check("llm model", model())
        await check("llm generate", generate())
        await check("llm gpu", gpu())

    async def database() -> None:
        async def db() -> str:
            from trader.journal.store import SqlJournal

            url = os.environ["DATABASE_URL"]
            if a.db_host:
                url = url.replace("@postgres:", f"@{a.db_host}:")
            ok = await asyncio.to_thread(SqlJournal(url, create=False).ping)
            if not ok:
                raise RuntimeError("cannot connect")
            return "connected"

        await check("database", db())

    async def go() -> None:
        for svc in a.service:
            await {"core": alpaca, "news": news, "notifier": notifier, "llm": llm,
                   "db": database}[svc]()

    configure_logging("ERROR", json_logs=False)
    asyncio.run(go())
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name:<16} {detail}")
    return 0 if all(ok for _, ok, _ in results) else 1


ENV_ROLES = {".env.core": ("core", "core_rw"), ".env.news": ("news", "news_rw"),
             ".env.notifier": ("notifier", "notifier_rw"), ".env.backup": ("readonly", "readonly"),
             ".env.console": ("console", "console_rw")}
OPTIONAL_ENV = (".env.alerts", ".env.console")   # created only when their template exists
REQUIRED_KEYS = {".env.core": ("ALPACA_PAPER_KEY", "ALPACA_PAPER_SECRET"),
                 ".env.news": ("FINNHUB_TOKEN", "SEC_USER_AGENT"),
                 ".env.notifier": ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")}


def read_kv(path: Path) -> dict[str, str]:
    out = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip()
    return out


def cmd_init_env(a: argparse.Namespace) -> int:
    """Create missing local setup files from the *.example templates (never overwrites)."""
    import secrets as pysecrets
    import string

    root = Path(a.root).resolve()
    created: list[str] = []
    kept: list[str] = []

    def write_new(path: Path, text: str) -> None:
        if path.exists():
            kept.append(str(path.relative_to(root)))
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        created.append(str(path.relative_to(root)))

    # docker/.env: database and role names
    denv = root / "docker" / ".env"
    if not denv.exists():
        t = (root / "docker" / ".env.example").read_text(encoding="utf-8")
        prefix = f"{a.prefix.rstrip('_')}_" if a.prefix else ""
        db = a.database or (f"{prefix}trader" if prefix else "trader")
        admin = f"{prefix}admin" if prefix else "trader_admin"
        t = (t.replace("PG_DATABASE=trader", f"PG_DATABASE={db}")
             .replace("PG_ADMIN_USER=trader_admin", f"PG_ADMIN_USER={admin}")
             .replace("DB_ROLE_PREFIX=\n", f"DB_ROLE_PREFIX={prefix}\n"))
        write_new(denv, t)
    else:
        kept.append("docker/.env")
    names = read_kv(denv)
    prefix, db = names.get("DB_ROLE_PREFIX", ""), names.get("PG_DATABASE", "trader")

    # secrets/: one random password per role, plus the Grafana admin login
    alphabet = string.ascii_letters + string.digits
    for name in (*(f"pg_{r}" for r in ("admin", "core", "news", "notifier", "readonly",
                                       "console")),
                 "grafana_admin", "console"):
        write_new(root / "secrets" / f"{name}_password.txt",
                  "".join(pysecrets.choice(alphabet) for _ in range(28)))

    # env files with the database URL filled in
    for name in (".env.core", ".env.news", ".env.notifier", ".env.backup", ".env.replay",
                 *OPTIONAL_ENV):
        if name in OPTIONAL_ENV and not (root / f"{name}.example").exists():
            continue   # optional: standalone price alerts, owner console
        t = (root / f"{name}.example").read_text(encoding="utf-8")
        if name in ENV_ROLES:
            role, user = ENV_ROLES[name]
            pw = (root / "secrets" / f"pg_{role}_password.txt").read_text(encoding="utf-8").strip()
            t = "\n".join(
                f"DATABASE_URL=postgresql+psycopg://{prefix}{user}:{pw}@postgres:5432/{db}"
                if ln.startswith("DATABASE_URL=") else ln for ln in t.splitlines()) + "\n"
        write_new(root / name, t)

    # config.yaml
    write_new(root / "config" / "config.yaml",
              (root / "config" / "config.example.yaml").read_text(encoding="utf-8"))
    if (root / "config" / "alerts.example.yaml").exists():
        write_new(root / "config" / "alerts.yaml",
                  (root / "config" / "alerts.example.yaml").read_text(encoding="utf-8"))

    print("created: " + (", ".join(created) or "nothing"))
    if kept:
        print("kept existing (not changed): " + ", ".join(kept))
    missing = [f"{name}: {k}" for name, keys in REQUIRED_KEYS.items()
               for k in keys if not read_kv(root / name).get(k)
               or "<" in read_kv(root / name).get(k, "")]
    cfg_text = (root / "config" / "config.yaml").read_text(encoding="utf-8")
    if "your-email@example.com" in cfg_text or "owner@example.com" in cfg_text:
        missing.append("config/config.yaml: data.sec_user_agent (name + email)")
    if missing:
        print("still to fill in (docs/ACCOUNT_SETUP.md):")
        for m in missing:
            print(f"  - {m}")
    print("then: ./run.sh connections   (read-only checks of every key, in a container)")
    return 0


def cmd_telegram_chats(a: argparse.Namespace) -> int:
    """List chats that messaged the bot, to find TELEGRAM_CHAT_ID (setup helper)."""
    from trader.notify.telegram import TelegramNotifier

    for f in a.env_file or []:
        load_env_file(f)
    tg = TelegramNotifier(os.environ["TELEGRAM_BOT_TOKEN"], "0")
    me = asyncio.run(tg.get_me())
    chats = asyncio.run(tg.recent_chats())
    print(f"bot @{me['username']}")
    if not chats:
        print("no messages yet: open the bot in Telegram, press Start or send STATUS, rerun")
        return 1
    for cid, name in chats.items():
        print(f"chat id {cid}  ({name})")
    return 0


def cmd_validate_journal(a: argparse.Namespace) -> int:
    from trader.journal import schema as s

    j = _journal(create=False)
    counts = {t.name: len(j.rows(t, limit=1_000_000)) for t in s.metadata.sorted_tables}
    print(json.dumps(counts, indent=1))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="trader", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--service", choices=["core", "news", "notifier", "alerts", "console"],
                   required=True)
    r.add_argument("--alerts-config", default=None)
    r.set_defaults(fn=cmd_run)
    al = sub.add_parser("alerts", help="lightweight Telegram price alerts (no LLM/Postgres)")
    al.add_argument("--alerts-config", default=None, help="default: $ALERTS_CONFIG or "
                    "config/alerts.yaml")
    al.set_defaults(fn=cmd_alerts)
    v = sub.add_parser("validate-config")
    v.add_argument("--config", dest="config")
    v.set_defaults(fn=cmd_validate_config)
    sm = sub.add_parser("select-model", help="pick the Qwen model for this machine's GPU/RAM")
    sm.add_argument("--model", default=None, help="choose this tag instead of the recommended")
    sm.add_argument("--apply", action="store_true",
                    help="write it to docker/.env and config/config.yaml")
    sm.add_argument("--force", action="store_true", help="allow a model that does not fit")
    sm.add_argument("--vram", type=float, default=None, help="GPU memory in GB (skip detection)")
    sm.add_argument("--vram-used", type=float, default=0.5, help="GB already in use")
    sm.add_argument("--ram", type=float, default=None, help="system RAM in GB")
    sm.add_argument("--ctx", type=int, default=8192, help="context tokens (llm.max_context_tokens)")
    sm.add_argument("--root", default=".")
    sm.set_defaults(fn=cmd_select_model)
    h = sub.add_parser("health")
    h.add_argument("--service", required=True)
    h.add_argument("--max-age", type=float, default=90)
    h.set_defaults(fn=cmd_health)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("kill").set_defaults(fn=lambda a: _local_command("KILL"))
    sub.add_parser("resume").set_defaults(fn=lambda a: _local_command("RESUME"))
    sub.add_parser("plan", help="build the next session's preview game plans now "
                   "(outside market hours)").set_defaults(fn=lambda a: _local_command("PLAN"))
    f = sub.add_parser("flatten")
    f.add_argument("--confirm", action="store_true")
    f.set_defaults(fn=cmd_flatten)
    for name in ("pause", "unpause"):
        pp = sub.add_parser(name)
        pp.add_argument("style", choices=["day", "swing"])
        pp.set_defaults(fn=lambda a, n=name: _local_command(n.upper(), a.style.upper()))
    sub.add_parser("migrate").set_defaults(fn=cmd_migrate)
    rc = sub.add_parser("recap", help="last N sessions per ticker + next-session watchlist")
    rc.add_argument("--days", type=int, default=5)
    rc.add_argument("--no-send", action="store_true", help="print only, no Telegram")
    rc.set_defaults(fn=cmd_recap)
    bn = sub.add_parser("backfill-news", help="last N days of news and analyst actions")
    bn.add_argument("--days", type=int, default=30)
    bn.set_defaults(fn=cmd_backfill_news)
    g = sub.add_parser("gate")
    g.add_argument("--record", required=True,
                   help="e.g. replay, backtest, paper, live, auto_day, auto_swing")
    g.add_argument("--note", default="")
    g.set_defaults(fn=cmd_gate)
    rp = sub.add_parser("replay")
    rp.add_argument("--day", action="append", default=[])
    rp.add_argument("--recordings", default="recordings")
    rp.add_argument("--synthetic", type=int, default=0)
    rp.add_argument("--start", default="2026-10-05")
    rp.add_argument("--seed", type=int, default=7)
    rp.add_argument("--llm", choices=["rules", "ollama", "cached"], default="rules")
    rp.add_argument("--cache", default="llm_cache")
    rp.add_argument("--approvals", choices=["yes", "no", "timeout"], default="yes")
    rp.add_argument("--mode", choices=["auto", "approve", "paper"], default=None)
    rp.add_argument("--out", default=None)
    rp.add_argument("--compare", default=None)
    rp.add_argument("--speed", type=float, default=0, help="ignored: replays run as fast as "
                    "possible")
    rp.set_defaults(fn=cmd_replay)
    bt = sub.add_parser("backtest")
    bt.add_argument("--synthetic", type=int, default=60)
    bt.add_argument("--start", default="2026-01-05")
    bt.add_argument("--seed", type=int, default=7)
    bt.add_argument("--walk-forward", action="store_true")
    bt.add_argument("--in-sample", type=int, default=40)
    bt.add_argument("--out-sample", type=int, default=10)
    bt.add_argument("--out", default=None)
    bt.add_argument("--history", action="store_true",
                    help="BT-6: real minute bars from the bar store instead of synthetic")
    bt.add_argument("--days", type=int, default=500)
    bt.set_defaults(fn=cmd_backtest)
    it = sub.add_parser("import-trades", help="WS-9: other-broker trades for wash sales")
    it.add_argument("file")
    it.add_argument("--broker", default="generic",
                    choices=["generic", "robinhood", "webull", "fidelity", "schwab"])
    it.add_argument("--env-file", action="append")
    it.set_defaults(fn=cmd_import_trades)
    rt = sub.add_parser("restore-test", help="OPS-1: compare a scratch restore with production")
    rt.add_argument("--report", default="-", help="JSON from the restore-test container")
    rt.add_argument("--env-file", action="append")
    rt.set_defaults(fn=cmd_restore_test)
    dr = sub.add_parser("drill", help="OPS-2: record the monthly emergency drill")
    dr.add_argument("--env-file", action="append")
    dr.set_defaults(fn=cmd_drill)
    dg = sub.add_parser("data-gate", help="DQ-7: IEX vs SIP divergence gate")
    dg.add_argument("--record", action="store_true")
    dg.add_argument("--env-file", action="append")
    dg.set_defaults(fn=cmd_data_gate)
    ev = sub.add_parser("eval-prompts")
    ev.add_argument("--llm", choices=["rules", "ollama", "cached"], default="ollama")
    ev.add_argument("--good", type=int, default=50)
    ev.add_argument("--bad", type=int, default=50)
    ev.add_argument("--inject", type=int, default=20)
    ev.add_argument("--out", default="runs/eval")
    ev.set_defaults(fn=cmd_eval)
    fh = sub.add_parser("fetch-history")
    fh.add_argument("--days", type=int, default=730)
    fh.add_argument("--minute-days", type=int, default=60)
    fh.add_argument("--daily-only", action="store_true", help="skip minute bars")
    fh.set_defaults(fn=cmd_fetch_history)
    bk = sub.add_parser("backup")
    bk.add_argument("--daily-at", default="18:00")
    bk.add_argument("--once", action="store_true")
    bk.set_defaults(fn=cmd_backup)
    sub.add_parser("validate-journal").set_defaults(fn=cmd_validate_journal)
    ck = sub.add_parser("check-connections", help="read-only credential checks")
    ck.add_argument("--service", action="append",
                    choices=["core", "news", "notifier", "llm", "db"], required=True)
    ck.add_argument("--env-file", action="append", help="load KEY=value file(s) first")
    ck.add_argument("--db-host", default=None, help="replace host 'postgres' (host runs)")
    ck.set_defaults(fn=cmd_check)
    ie = sub.add_parser("init-env", help="create missing setup files from the examples")
    ie.add_argument("--root", default=".")
    ie.add_argument("--prefix", default="", help="database role prefix, e.g. srb")
    ie.add_argument("--database", default="", help="database name (default <prefix>_trader)")
    ie.set_defaults(fn=cmd_init_env)
    sc = sub.add_parser("scan", help="technical scanner: swing ideas + day watchlist (TS-14)")
    sc.add_argument("--date", help="scan as of this session (default: latest stored bar)")
    sc.add_argument("--all", action="store_true", help="every idea, ignoring min_score/top_n")
    sc.add_argument("--json", action="store_true")
    sc.add_argument("--telegram", action="store_true", help="also send the text to Telegram")
    sc.add_argument("--explain", action="store_true", help="add a Qwen summary (TS-13)")
    sc.add_argument("--equity", type=float, help="size ideas for this account equity ($)")
    sc.add_argument("--no-write", action="store_true",
                    help="do not update scanner.latest_file (the console's copy)")
    sc.add_argument("--env-file", action="append", help="load KEY=value file(s) first")
    sc.set_defaults(fn=cmd_scan)
    se = sub.add_parser("scan-eval", help="walk-forward grading of the scanner (TS-11/12)")
    se.add_argument("--from", dest="start")
    se.add_argument("--to", dest="end")
    se.add_argument("--minutes", action="store_true", help="use stored minute bars")
    se.add_argument("--top", type=int, default=None, help="day watchlist size to grade")
    se.add_argument("--no-write", action="store_true", help="do not write scanner.eval_file")
    se.set_defaults(fn=cmd_scan_eval)
    ca = sub.add_parser("catalyst", help="news catalysts: strength, outlook, view (NC-29)")
    ca.add_argument("ticker", nargs="?", help="one ticker (default: the universe)")
    ca.add_argument("--date", help="as of this session (replays stored data)")
    ca.add_argument("--json", action="store_true")
    ca.add_argument("--telegram", action="store_true", help="also send the text to Telegram")
    ca.add_argument("--equity", type=float, help="size the plan for this account equity ($)")
    ca.add_argument("--model", action="store_true", help="let Qwen type stories rules miss")
    ca.add_argument("--no-write", action="store_true",
                    help="do not store stories/outlooks or update catalyst.latest_file")
    ca.set_defaults(fn=cmd_catalyst)
    ce = sub.add_parser("catalyst-eval", help="walk-forward grading of catalyst outlooks")
    ce.add_argument("--days", type=int, default=365)
    ce.set_defaults(fn=cmd_catalyst_eval)
    cb = sub.add_parser("backfill-catalysts", help="news history + fundamentals (NC-30)")
    cb.add_argument("--days", type=int, default=365)
    cb.add_argument("--skip-fundamentals", action="store_true")
    cb.set_defaults(fn=cmd_backfill_catalysts)
    mc = sub.add_parser("macro", help="macro regime, releases and headlines (MA-8)")
    mc.add_argument("--date", help="regime as of this day (no write)")
    mc.add_argument("--json", action="store_true")
    mc.add_argument("--telegram", action="store_true")
    mc.add_argument("--eval", action="store_true", help="what followed each label (MA-9)")
    mc.add_argument("--no-write", action="store_true")
    mc.set_defaults(fn=cmd_macro)
    mk = sub.add_parser("macro-collect", help="FRED series + macro news (news container)")
    mk.add_argument("--history", action="store_true", help="full history of every series")
    mk.set_defaults(fn=cmd_macro_collect)
    fc = sub.add_parser("forecast", help="volatility, gaps, macro days, sectors (FC-6)")
    fc.add_argument("ticker", nargs="?")
    fc.add_argument("--json", action="store_true")
    fc.add_argument("--eval", action="store_true", help="walk-forward grading (FC-2, FC-5)")
    fc.add_argument("--no-write", action="store_true")
    fc.set_defaults(fn=cmd_forecast)
    mo = sub.add_parser("momentum", help="momentum leaders, buy-stops, signals (MO-20)")
    mo.add_argument("--date", help="as of this session (stored bars, no tracking)")
    mo.add_argument("--json", action="store_true")
    mo.add_argument("--telegram", action="store_true")
    mo.add_argument("--equity", type=float, help="size the buy-stops for this equity ($)")
    mo.add_argument("--all", action="store_true", help="also list unproven setups")
    mo.add_argument("--track", action="store_true",
                    help="update the journal's tracked signals (trader-core does this daily)")
    mo.add_argument("--no-write", action="store_true")
    mo.set_defaults(fn=cmd_momentum)
    me = sub.add_parser("momentum-eval", help="walk-forward grading of momentum (MO-11..13)")
    me.add_argument("--start")
    me.add_argument("--end")
    me.add_argument("--no-write", action="store_true")
    me.set_defaults(fn=cmd_momentum_eval)
    mh = sub.add_parser("momentum-history", help="SIP daily bars for momentum.universe")
    mh.add_argument("--days", type=int, help="calendar days (default momentum.history_days)")
    mh.set_defaults(fn=cmd_momentum_history)
    evp = sub.add_parser("events", help="company events: past pattern, dated ideas (EV-7)")
    evp.add_argument("ticker", nargs="?")
    evp.add_argument("--json", action="store_true")
    evp.add_argument("--telegram", action="store_true")
    evp.add_argument("--no-write", action="store_true")
    evp.set_defaults(fn=cmd_events)
    tc = sub.add_parser("telegram-chats", help="show chat ids that messaged the bot")
    tc.add_argument("--env-file", action="append")
    tc.set_defaults(fn=cmd_telegram_chats)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    rc = args.fn(args)
    return int(rc or 0)


if __name__ == "__main__":
    sys.exit(main())
