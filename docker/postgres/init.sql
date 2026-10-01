-- Least-privilege roles, one per service (docs/SECURITY.md SEC-17).
-- Passwords are set from secret files by the M0 setup script; placeholders here are replaced at init.
-- Tables are created by Alembic migrations run by trader-core; grants below are applied by the
-- migration that creates each table (see docs/DATA_SPEC.md §3 "Writers").

CREATE ROLE core_rw     LOGIN PASSWORD 'change-me-core';
CREATE ROLE news_rw     LOGIN PASSWORD 'change-me-news';
CREATE ROLE notifier_rw LOGIN PASSWORD 'change-me-notifier';
CREATE ROLE readonly    LOGIN PASSWORD 'change-me-readonly';   -- backup, Grafana, reports

CREATE SCHEMA IF NOT EXISTS journal AUTHORIZATION core_rw;
GRANT USAGE ON SCHEMA journal TO news_rw, notifier_rw, readonly;
ALTER DEFAULT PRIVILEGES FOR ROLE core_rw IN SCHEMA journal GRANT SELECT ON TABLES TO readonly;

-- Outbox bus table (shared). Each service may insert only its own message types (checked by trigger).
CREATE TABLE IF NOT EXISTS journal.outbox (
    id           BIGSERIAL PRIMARY KEY,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    source       TEXT NOT NULL,            -- core | news | notifier
    channel      TEXT NOT NULL,            -- news_flags | outbound_msg | inbound_cmd | heartbeat
    payload      JSONB NOT NULL,
    consumed_at  TIMESTAMPTZ
);
ALTER TABLE journal.outbox OWNER TO core_rw;
GRANT INSERT, SELECT ON journal.outbox TO news_rw, notifier_rw;
GRANT USAGE ON SEQUENCE journal.outbox_id_seq TO news_rw, notifier_rw;
GRANT UPDATE (consumed_at) ON journal.outbox TO notifier_rw;

CREATE OR REPLACE FUNCTION journal.outbox_guard() RETURNS trigger AS $$
BEGIN
  IF (current_user = 'news_rw'     AND NEW.channel NOT IN ('news_flags','heartbeat'))
  OR (current_user = 'notifier_rw' AND NEW.channel NOT IN ('inbound_cmd','heartbeat')) THEN
    RAISE EXCEPTION 'role % may not publish to channel %', current_user, NEW.channel;
  END IF;
  NEW.source := CASE current_user WHEN 'news_rw' THEN 'news' WHEN 'notifier_rw' THEN 'notifier' ELSE 'core' END;
  PERFORM pg_notify(NEW.channel, NEW.id::text);
  RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER outbox_guard BEFORE INSERT ON journal.outbox
FOR EACH ROW EXECUTE FUNCTION journal.outbox_guard();
