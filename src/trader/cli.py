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
    from trader.data.store import BarStore, resolve_bars_path

    cfg = _cfg(a.config)
    key, secret = os.environ["ALPACA_PAPER_KEY"], os.environ["ALPACA_PAPER_SECRET"]
    hist = AlpacaHistory(key, secret, feed=cfg.data.alpaca_feed)
    store = BarStore(resolve_bars_path(cfg.storage.bars_path))
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
             ".env.notifier": ("notifier", "notifier_rw"), ".env.backup": ("readonly", "readonly")}
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
    for name in (*(f"pg_{r}" for r in ("admin", "core", "news", "notifier", "readonly")),
                 "grafana_admin"):
        write_new(root / "secrets" / f"{name}_password.txt",
                  "".join(pysecrets.choice(alphabet) for _ in range(28)))

    # env files with the database URL filled in
    for name in (".env.core", ".env.news", ".env.notifier", ".env.backup", ".env.replay",
                 ".env.alerts"):
        if name == ".env.alerts" and not (root / f"{name}.example").exists():
            continue   # optional: standalone price alerts
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
    print("then: trader check-connections --service core --service news --service notifier "
          "--env-file .env.core --env-file .env.news --env-file .env.notifier")
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
    r.add_argument("--service", choices=["core", "news", "notifier", "alerts"], required=True)
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
