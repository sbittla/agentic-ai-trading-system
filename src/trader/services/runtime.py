"""Shared service plumbing: logging, heartbeats, health files, secrets (DV-6, SEC-6)."""

from __future__ import annotations

import logging
import os
import re
import sys
import time
from collections.abc import MutableMapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

HEALTH_DIR = Path(os.environ.get("TRADER_HEALTH_DIR", "/tmp" if os.name != "nt" else  # noqa: S108
                                 os.environ.get("TEMP", ".")))
KEY_RE = re.compile(r"(?i)(key|secret|token|password|sid)")


def _redact(_: Any, __: str, event: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    """SEC-6: never log anything that looks like a secret."""
    secrets = [v for k, v in os.environ.items() if KEY_RE.search(k) and v and len(v) >= 6]
    for k, v in list(event.items()):
        if KEY_RE.search(k) and k not in ("event",):
            event[k] = "[REDACTED]"
        elif isinstance(v, str):
            for sv in secrets:
                if sv in v:
                    event[k] = v.replace(sv, "[REDACTED]")
                    v = event[k]
    return event


def configure_logging(level: str = "INFO", json_logs: bool | None = None) -> None:
    json_logs = (not sys.stderr.isatty()) if json_logs is None else json_logs
    logging.basicConfig(level=level, format="%(message)s", stream=sys.stderr)
    # httpx/httpcore log every request URL at INFO; Telegram's URL carries the bot token
    # (SEC-6), and the per-poll lines are noise. Warnings and errors still show.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    structlog.configure(
        processors=[structlog.contextvars.merge_contextvars, structlog.processors.add_log_level,
                    structlog.processors.TimeStamper(fmt="iso", utc=True), _redact,
                    structlog.processors.JSONRenderer() if json_logs
                    else structlog.dev.ConsoleRenderer()],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level)))


def health_file(service: str) -> Path:
    return HEALTH_DIR / f"trader-{service}.health"


def touch_health(service: str) -> None:
    p = health_file(service)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(datetime.now(UTC).isoformat(), encoding="utf-8")


def health_ok(service: str, max_age_s: float = 90) -> bool:
    p = health_file(service)
    return p.exists() and time.time() - p.stat().st_mtime < max_age_s


def require_env(*names: str) -> dict[str, str]:
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        raise SystemExit(f"missing environment variables: {', '.join(missing)}")
    return {n: os.environ[n] for n in names}
