"""`trader` command line (ARCHITECTURE §2, RUNBOOK §3).

    trader run --service core|news|notifier
    trader replay --day 2026-10-15 | --synthetic 20 --start 2026-10-05 [--llm rules|ollama]
    trader backtest --synthetic 120 [--walk-forward]
    trader status | kill | resume | flatten --confirm | pause day|swing | unpause day|swing
    trader validate-config [--config path]
    trader health --service core
    trader migrate | backup --daily-at 18:00 | fetch-history --days 730
    trader eval-prompts [--llm ollama|rules] | gate --record NAME --note TEXT
    trader validate-journal
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
    else:
        from trader.services.notifier_main import main
    main(a.config)
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
    j.apply_grants()
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
        from trader.data.store import BarStore, load_recorded_day

        days = [date.fromisoformat(d) for d in a.day]
        minutes, news = {}, {}
        for d in days:
            rec = load_recorded_day(Path(a.recordings) / d.isoformat())
            minutes[d] = rec["bars"]
            news[d] = rec["news"]
        store = BarStore(cfg.storage.bars_path)
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


def cmd_backtest(a: argparse.Namespace) -> int:
    from trader.backtest.synthetic import generate
    from trader.backtest.walkforward import walk_forward

    configure_logging("WARNING", json_logs=False)
    cfg = _cfg(a.config)
    data = generate(date.fromisoformat(a.start), a.synthetic, seed=a.seed)
    out = Path(a.out or f"runs/{date.today().isoformat()}_backtest")
    if a.walk_forward:
        res = asyncio.run(walk_forward(cfg, data.minutes, data.daily, out,
                                       in_sample=a.in_sample, out_sample=a.out_sample,
                                       news=data.news, seed_minutes=data.seed_minutes))
        (out / "walk_forward.json").write_text(json.dumps(res, indent=1, default=str),
                                               encoding="utf-8")
        print(json.dumps({k: res[k] for k in ("oos", "unstable_params", "pass_bar")}, indent=1,
                         default=str))
        return 0
    a.synthetic, a.llm, a.approvals, a.compare = a.synthetic, "rules", "yes", None
    a.mode, a.cache, a.day = None, "llm_cache", []
    return cmd_replay(a)


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


def cmd_fetch_history(a: argparse.Namespace) -> int:
    from trader.data.alpaca_data import AlpacaHistory
    from trader.data.store import BarStore

    cfg = _cfg(a.config)
    key, secret = os.environ["ALPACA_PAPER_KEY"], os.environ["ALPACA_PAPER_SECRET"]
    hist = AlpacaHistory(key, secret, feed=cfg.data.alpaca_feed)
    store = BarStore(cfg.storage.bars_path)
    end = date.today()

    async def go() -> None:
        for sym in list(cfg.universe.tickers) + list(cfg.universe.context_symbols):
            daily = await hist.daily_bars(sym, end - timedelta(days=a.days), end)
            store.write("daily", sym, daily)
            mins = await hist.minute_bars(sym, datetime.now(UTC) - timedelta(days=a.minute_days),
                                          datetime.now(UTC))
            store.write("minute", sym, mins, keep_days=a.minute_days + 2)
            print(f"{sym}: {len(daily)} daily, {len(mins)} minute bars")
    asyncio.run(go())
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
            subprocess.run([pg_dump, "--format=custom", f"--file={out}", url], check=False)  # noqa: S603
            bars = Path("/app/data/bars")
            if bars.exists():
                shutil.copytree(bars, dest / f"bars_{now:%Y%m%d}", dirs_exist_ok=True)
            Path("/tmp/trader-backup.health").write_text(now.isoformat())  # noqa: S108
        if a.once:
            return 0
        time.sleep(60)


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
    r.add_argument("--service", choices=["core", "news", "notifier"], required=True)
    r.set_defaults(fn=cmd_run)
    v = sub.add_parser("validate-config")
    v.add_argument("--config", dest="config")
    v.set_defaults(fn=cmd_validate_config)
    h = sub.add_parser("health")
    h.add_argument("--service", required=True)
    h.add_argument("--max-age", type=float, default=90)
    h.set_defaults(fn=cmd_health)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("kill").set_defaults(fn=lambda a: _local_command("KILL"))
    sub.add_parser("resume").set_defaults(fn=lambda a: _local_command("RESUME"))
    f = sub.add_parser("flatten")
    f.add_argument("--confirm", action="store_true")
    f.set_defaults(fn=cmd_flatten)
    for name in ("pause", "unpause"):
        pp = sub.add_parser(name)
        pp.add_argument("style", choices=["day", "swing"])
        pp.set_defaults(fn=lambda a, n=name: _local_command(n.upper(), a.style.upper()))
    sub.add_parser("migrate").set_defaults(fn=cmd_migrate)
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
    bt.set_defaults(fn=cmd_backtest)
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
    fh.set_defaults(fn=cmd_fetch_history)
    bk = sub.add_parser("backup")
    bk.add_argument("--daily-at", default="18:00")
    bk.add_argument("--once", action="store_true")
    bk.set_defaults(fn=cmd_backup)
    sub.add_parser("validate-journal").set_defaults(fn=cmd_validate_journal)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    rc = args.fn(args)
    return int(rc or 0)


if __name__ == "__main__":
    sys.exit(main())
