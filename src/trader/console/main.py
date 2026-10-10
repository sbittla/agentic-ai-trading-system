"""``trader console``: run the Owner Console (container ``console``, 127.0.0.1:8080).

Environment (``.env.console``): ``DATABASE_URL`` (console_rw role), ``CORE_API_URL``,
``OLLAMA_URL``, ``CONSOLE_IDLE_MIN``; the password comes from the ``console_password``
Docker secret (``CONSOLE_PASSWORD_FILE``).
"""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

import structlog

from trader import config as config_mod
from trader.console.app import Auth, create_app
from trader.console.ask import Asker
from trader.console.data import ConsoleData, CoreClient, Paths
from trader.journal.store import SqlJournal

log = structlog.get_logger(__name__)


def read_password() -> str:
    p = Path(os.environ.get("CONSOLE_PASSWORD_FILE", "/run/secrets/console_password"))
    if not p.exists():
        raise SystemExit(f"console password file {p} missing: ./run.sh setup")
    pw = p.read_text(encoding="utf-8").strip()
    if len(pw) < 12:
        raise SystemExit("console password must be at least 12 characters (UI-21)")
    return pw


def main(config_path: str | None = None) -> None:
    import uvicorn

    loaded = config_mod.load(config_path)
    cfg = loaded.config
    base = Path(loaded.path).resolve().parent.parent
    journal = SqlJournal(os.environ["DATABASE_URL"], create=False)
    data = ConsoleData(
        journal,
        CoreClient(os.environ.get("CORE_API_URL", "http://trader-core:8081")),
        cfg,
        Paths(
            base / "reports",
            base / "runs",
            base / "config",
            Path(os.environ["ALERTS_DB"]) if os.environ.get("ALERTS_DB") else None,
        ),
        ollama_url=os.environ.get("OLLAMA_URL", cfg.llm.base_url),
    )
    llm = None
    if os.environ.get("CONSOLE_ASK_MODEL", "1") != "0":
        from trader.llm.ollama_client import OllamaClient

        llm = OllamaClient(
            cfg.llm.base_url,
            cfg.llm.model,
            journal=journal,
            temperature=0.0,
            seed=cfg.llm.seed,
            retries=0,
            num_ctx=cfg.llm.max_context_tokens,
            keep_alive=cfg.llm.keep_alive,
        )
    auth = Auth(
        read_password(), idle=timedelta(minutes=int(os.environ.get("CONSOLE_IDLE_MIN", "30")))
    )
    app = create_app(data, auth, Asker(data, llm))
    log.info("console_start", port=8080)
    # 0.0.0.0 inside the container; compose publishes it on 127.0.0.1 only (UI-C1)
    uvicorn.run(
        app,
        host="0.0.0.0",  # noqa: S104
        port=int(os.environ.get("CONSOLE_PORT", "8080")),
        log_level="warning",
        proxy_headers=False,
        server_header=False,
    )
