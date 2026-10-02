"""Container boundaries (SR-6, SEC-17, SEC-19): compose wiring, roles and the outbox trigger.

The trigger test runs against a real Postgres when ``TEST_PG_URL`` points at a database
initialised with ``docker/postgres/init.sql`` (admin role); otherwise it is skipped.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = yaml.safe_load((ROOT / "docker" / "docker-compose.yml").read_text(encoding="utf-8"))
SERVICES = COMPOSE["services"]


@pytest.mark.rule("SR-6")
def test_six_always_on_services() -> None:
    always = {k for k, v in SERVICES.items() if "profiles" not in v}
    assert always == {"postgres", "ollama", "trader-core", "news-sentiment", "notifier",
                      "backup"}


@pytest.mark.rule("SR-6")
@pytest.mark.rule("SEC-17")
def test_only_trader_core_has_broker_keys() -> None:
    env_files = {k: v.get("env_file", []) for k, v in SERVICES.items()}
    assert env_files["trader-core"] == ["../.env.core"]
    assert env_files["news-sentiment"] == ["../.env.news"]
    assert env_files["notifier"] == ["../.env.notifier"]
    blocks = {}
    for name in (".env.core", ".env.news", ".env.notifier", ".env.backup"):
        ex = (ROOT / f"{name}.example").read_text(encoding="utf-8")
        blocks[name] = [ln.split("=")[0] for ln in ex.splitlines()
                        if "=" in ln and not ln.startswith("#")]
    assert any(k.startswith("ALPACA") for k in blocks[".env.core"])
    for other in (".env.news", ".env.notifier", ".env.backup"):
        assert not any(k.startswith("ALPACA") for k in blocks[other]), other


@pytest.mark.rule("SEC-19")
def test_no_inbound_ports_and_hardened_containers() -> None:
    for name, svc in SERVICES.items():
        if name == "grafana":
            assert all(p.startswith("127.0.0.1:") for p in svc["ports"])
            continue
        assert "ports" not in svc, name
    app = COMPOSE["x-app"]
    assert app["read_only"] is True and "no-new-privileges:true" in app["security_opt"]
    assert "webhook" not in (ROOT / "src" / "trader" / "services" / "notifier_main.py"
                             ).read_text(encoding="utf-8").lower().replace("no webhook", "")


@pytest.mark.rule("SEC-17")
def test_init_sql_roles_and_trigger() -> None:
    sql = (ROOT / "docker" / "postgres" / "init.sql").read_text(encoding="utf-8")
    for role in ("core_rw", "news_rw", "notifier_rw", "readonly"):
        assert f":prefix '{role}'" in sql
    assert "outbox_guard" in sql and "RAISE EXCEPTION" in sql
    assert "PASSWORD '" not in sql   # passwords come from Docker secrets, never from git
    sh = (ROOT / "docker" / "postgres" / "init.sh").read_bytes()
    assert b"/run/secrets/" in sh and bytes([13, 10]) not in sh   # LF only (Linux)


@pytest.mark.integration
@pytest.mark.rule("SEC-17")
@pytest.mark.skipif(not os.environ.get("TEST_PG_NEWS_URL"), reason="needs Postgres")
def test_news_role_cannot_publish_commands() -> None:
    from sqlalchemy import create_engine, text

    eng = create_engine(os.environ["TEST_PG_NEWS_URL"])
    with eng.begin() as c:
        c.execute(text("INSERT INTO journal.outbox (source, channel, payload) "
                       "VALUES ('x', 'news_flags', '{}')"))
    with pytest.raises(Exception, match="may not publish"), eng.begin() as c:
        c.execute(text("INSERT INTO journal.outbox (source, channel, payload) "
                       "VALUES ('x', 'inbound_cmd', '{\"name\": \"FLATTEN\"}')"))
