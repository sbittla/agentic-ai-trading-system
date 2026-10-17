"""SqlJournal: the single write path for the audit trail (SR-4).

Live and paper use Postgres (role per container); replay and backtest runs use a per-run
SQLite file with the same schema, so every run is self-contained (BACKTEST_ENGINE §6).
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import (
    JSON,
    Engine,
    Table,
    and_,
    create_engine,
    desc,
    event,
    select,
    text,
    update,
)
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from trader.journal import schema as s

log = structlog.get_logger(__name__)


class JournalUnavailable(Exception):
    """Postgres down or unreachable: no journal, no trade (ARCHITECTURE §6)."""


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_jsonable(v) for v in value]
    return value


class SqlJournal:
    def __init__(self, url: str, *, create: bool = True, echo: bool = False) -> None:
        self.url = url
        self.is_sqlite = url.startswith("sqlite")
        kwargs: dict[str, Any] = {"echo": echo, "future": True}
        if self.is_sqlite:
            kwargs["execution_options"] = {"schema_translate_map": {s.SCHEMA: None}}
        else:
            kwargs["pool_pre_ping"] = True
            kwargs["connect_args"] = {"connect_timeout": 5}
        self.engine: Engine = create_engine(url, **kwargs)
        if self.is_sqlite:
            event.listen(self.engine, "connect", _sqlite_pragmas)
        if create:
            self.create_all()

    # -- lifecycle ------------------------------------------------------------------------
    def create_all(self) -> None:
        try:
            if not self.is_sqlite:
                # init.sql creates the schema owned by core_rw; the role has no database-level
                # CREATE (least privilege), and Postgres checks that even for IF NOT EXISTS.
                with self.engine.begin() as c:
                    exists = c.execute(text("SELECT 1 FROM information_schema.schemata "
                                            "WHERE schema_name = :n"), {"n": s.SCHEMA}).first()
                    if exists is None:
                        c.execute(text(f"CREATE SCHEMA {s.SCHEMA}"))
            s.metadata.create_all(self.engine, checkfirst=True)
            self._add_missing_columns()
        except OperationalError as exc:
            raise JournalUnavailable(str(exc)) from exc

    def _add_missing_columns(self) -> None:
        """New nullable columns on existing tables (``create_all`` only creates tables).
        Additive only: a column is never dropped, renamed or retyped here."""
        from sqlalchemy import inspect

        insp = inspect(self.engine)
        schema = None if self.is_sqlite else s.SCHEMA
        have_tables = set(insp.get_table_names(schema=schema))
        for table in s.metadata.sorted_tables:
            if table.name not in have_tables:
                continue
            have = {c["name"] for c in insp.get_columns(table.name, schema=schema)}
            for col in table.columns:
                if col.name in have or not col.nullable or col.primary_key:
                    continue
                ddl = col.type.compile(dialect=self.engine.dialect)
                name = table.name if self.is_sqlite else f"{s.SCHEMA}.{table.name}"
                with self.engine.begin() as c:
                    c.execute(text(f'ALTER TABLE {name} ADD COLUMN "{col.name}" {ddl}'))
                log.info("journal_column_added", table=table.name, column=col.name)

    def apply_grants(self, prefix: str | None = None) -> list[str]:
        """Least-privilege grants per role (SEC-17); Postgres only, run by `trader migrate`.
        Role names carry ``DB_ROLE_PREFIX`` (e.g. ``srb_news_rw``). Returns failed statements."""
        if self.is_sqlite:
            return []
        p = os.environ.get("DB_ROLE_PREFIX", "") if prefix is None else prefix
        if not re.fullmatch(r"[a-z0-9_]*", p):
            raise ValueError(f"invalid DB_ROLE_PREFIX {p!r}")
        news, notifier, ro = f'"{p}news_rw"', f'"{p}notifier_rw"', f'"{p}readonly"'
        console = f'"{p}console_rw"'
        stmts = [f"GRANT SELECT ON ALL TABLES IN SCHEMA {s.SCHEMA} TO {ro}",
                 # pg_dump (nightly backup, read-only role) also reads every sequence
                 f"GRANT SELECT ON ALL SEQUENCES IN SCHEMA {s.SCHEMA} TO {ro}",
                 f"GRANT SELECT ON {s.SCHEMA}.config_snapshots TO {news}, {notifier}",
                 # UI-C2: the console reads everything and writes only its own notes
                 f"GRANT SELECT ON ALL TABLES IN SCHEMA {s.SCHEMA} TO {console}"]
        for t in s.NEWS_READS:   # NW-1: news polls fast for open positions and plan tickers
            stmts.append(f"GRANT SELECT ON {s.SCHEMA}.{t} TO {news}")
        for t in s.CONSOLE_WRITES:
            stmts.append(f"GRANT INSERT, SELECT ON {s.SCHEMA}.{t} TO {console}")
            stmts.append(f"GRANT USAGE ON SEQUENCE {s.SCHEMA}.{t}_id_seq TO {console}")
        stmts.append(f"GRANT DELETE ON {s.SCHEMA}.console_chats TO {console}")   # UI-8d clear
        for t in s.NEWS_WRITES:
            stmts.append(f"GRANT INSERT, SELECT ON {s.SCHEMA}.{t} TO {news}")
            stmts.append(f"GRANT USAGE ON SEQUENCE {s.SCHEMA}.{t}_id_seq TO {news}")
        for t in s.NEWS_UPDATES:
            stmts.append(f"GRANT UPDATE ON {s.SCHEMA}.{t} TO {news}")
        for t in s.NOTIFIER_WRITES:
            stmts.append(f"GRANT INSERT, SELECT ON {s.SCHEMA}.{t} TO {notifier}")
            stmts.append(f"GRANT USAGE ON SEQUENCE {s.SCHEMA}.{t}_id_seq TO {notifier}")
        failed = []
        for st in stmts:   # one transaction each: a failure must not abort the others
            try:
                with self.engine.begin() as c:
                    c.execute(text(st))
            except SQLAlchemyError as exc:
                log.error("grant_failed", stmt=st, error=str(exc))
                failed.append(st)
        return failed

    def ping(self) -> bool:
        try:
            with self.engine.connect() as c:
                c.execute(text("SELECT 1"))
            return True
        except SQLAlchemyError:
            return False

    def dispose(self) -> None:
        self.engine.dispose()

    # -- generic ---------------------------------------------------------------------------
    def insert(self, table: Table, row: Mapping[str, Any]) -> int | None:
        values = {k: (_jsonable(v) if isinstance(table.c[k].type, JSON) else v)
                  for k, v in row.items()}
        try:
            with self.engine.begin() as c:
                res = c.execute(table.insert().values(**values))
                pk = res.inserted_primary_key
                return int(pk[0]) if pk and isinstance(pk[0], int) else None
        except OperationalError as exc:
            raise JournalUnavailable(str(exc)) from exc

    def insert_many(self, table: Table, rows: Sequence[Mapping[str, Any]]) -> None:
        if not rows:
            return
        with self.engine.begin() as c:
            c.execute(table.insert(), [
                {k: (_jsonable(v) if isinstance(table.c[k].type, JSON) else v)
                 for k, v in r.items()} for r in rows])

    def update(self, table: Table, where: Mapping[str, Any], values: Mapping[str, Any]) -> int:
        cond = and_(*[table.c[k] == v for k, v in where.items()])
        vals = {k: (_jsonable(v) if isinstance(table.c[k].type, JSON) else v)
                for k, v in values.items()}
        with self.engine.begin() as c:
            return int(c.execute(update(table).where(cond).values(**vals)).rowcount)

    def upsert(self, table: Table, key: Mapping[str, Any], values: Mapping[str, Any]) -> None:
        if not self.update(table, key, values):
            self.insert(table, {**key, **values})

    def rows(self, table: Table, where: Mapping[str, Any] | None = None, *,
             order_by: str | None = None, descending: bool = False,
             limit: int | None = None) -> list[dict[str, Any]]:
        q = select(table)
        if where:
            q = q.where(and_(*[table.c[k] == v for k, v in where.items()]))
        if order_by:
            col = table.c[order_by]
            q = q.order_by(desc(col) if descending else col)
        if limit:
            q = q.limit(limit)
        with self.engine.connect() as c:
            return [dict(r._mapping) for r in c.execute(q)]

    def query(self, stmt: Any) -> list[dict[str, Any]]:
        with self.engine.connect() as c:
            return [dict(r._mapping) for r in c.execute(stmt)]

    # -- events (bus) ----------------------------------------------------------------------
    def record_event(self, ts: datetime, type_: str, payload: Mapping[str, Any]) -> int:
        eid = self.insert(s.events, {"ts": ts, "type": type_, "payload": payload})
        return eid or 0

    # -- helpers used by several agents ----------------------------------------------------
    def incident(self, ts: datetime, type_: str, detail: str) -> None:
        self.insert(s.incidents, {"ts": ts, "type": type_, "detail": detail})

    def gates_passed(self) -> set[str]:
        rows = self.rows(s.incidents, {"type": "GATE_PASSED"})
        return {str(r["detail"]).split(":")[0].strip() for r in rows if r["detail"]}

    def snapshot_config(self, effective: date, now: datetime, sha: str, yaml_text: str) -> None:
        self.insert(s.config_snapshots, {"effective_date": effective, "created_at": now,
                                         "yaml_hash": sha, "yaml_text": yaml_text})

    def config_for_day(self, d: date) -> dict[str, Any] | None:
        rows = self.query(select(s.config_snapshots)
                          .where(s.config_snapshots.c.effective_date == d)
                          .order_by(s.config_snapshots.c.id).limit(1))
        return rows[0] if rows else None

    def closed_positions(self, since: datetime | None = None, until: datetime | None = None,
                         route: str | None = None) -> list[dict[str, Any]]:
        q = select(s.positions).where(s.positions.c.closed_at.is_not(None))
        if since is not None:
            q = q.where(s.positions.c.closed_at >= since)
        if until is not None:
            q = q.where(s.positions.c.closed_at < until)
        if route is not None:
            q = q.where(s.positions.c.route == route)
        return self.query(q.order_by(s.positions.c.closed_at))

    def open_positions(self) -> list[dict[str, Any]]:
        return self.query(select(s.positions).where(s.positions.c.closed_at.is_(None))
                          .order_by(s.positions.c.opened_at))

    def active_locks(self, now: datetime) -> list[dict[str, Any]]:
        return self.query(select(s.locks).where(and_(s.locks.c.start <= now,
                                                     s.locks.c.until > now)))

    def last_equity(self) -> dict[str, Any] | None:
        rows = self.rows(s.equity, order_by="id", descending=True, limit=1)
        return rows[0] if rows else None

    def decisions_between(self, since: datetime, until: datetime) -> list[dict[str, Any]]:
        return self.query(select(s.decisions).where(and_(s.decisions.c.ts >= since,
                                                         s.decisions.c.ts < until))
                          .order_by(s.decisions.c.id))


def _sqlite_pragmas(dbapi_conn: Any, _rec: Any) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.close()
