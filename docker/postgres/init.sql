-- Least-privilege roles, one per service (docs/SECURITY.md SEC-17).
-- Run once at database initialisation by init.sh, which passes psql variables:
--   prefix   role-name prefix (DB_ROLE_PREFIX, e.g. "srb_"; empty by default)
--   *_pw     role passwords, read from git-ignored files in secrets/ (never stored here)
-- Tables are created by `trader migrate` (role core_rw); it also applies the per-table grants
-- (docs/DATA_SPEC.md §3 "Writers").

\set core_role :prefix 'core_rw'
\set news_role :prefix 'news_rw'
\set notifier_role :prefix 'notifier_rw'
\set ro_role :prefix 'readonly'

CREATE ROLE :"core_role"     LOGIN PASSWORD :'core_pw';
CREATE ROLE :"news_role"     LOGIN PASSWORD :'news_pw';
CREATE ROLE :"notifier_role" LOGIN PASSWORD :'notifier_pw';
CREATE ROLE :"ro_role"       LOGIN PASSWORD :'readonly_pw';   -- backup, Grafana, reports

CREATE SCHEMA IF NOT EXISTS journal AUTHORIZATION :"core_role";
GRANT USAGE ON SCHEMA journal TO :"news_role", :"notifier_role", :"ro_role";
ALTER DEFAULT PRIVILEGES FOR ROLE :"core_role" IN SCHEMA journal GRANT SELECT ON TABLES TO :"ro_role";

-- Outbox bus table (shared). Each service may insert only its own message types (checked by trigger).
CREATE TABLE IF NOT EXISTS journal.outbox (
    id           BIGSERIAL PRIMARY KEY,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    source       TEXT NOT NULL,            -- core | news | notifier
    channel      TEXT NOT NULL,            -- news_flags | outbound_msg | inbound_cmd | heartbeat
    payload      JSONB NOT NULL,
    consumed_at  TIMESTAMPTZ
);
ALTER TABLE journal.outbox OWNER TO :"core_role";
GRANT INSERT, SELECT ON journal.outbox TO :"news_role", :"notifier_role";
GRANT USAGE ON SEQUENCE journal.outbox_id_seq TO :"news_role", :"notifier_role";
GRANT UPDATE (consumed_at) ON journal.outbox TO :"notifier_role";

-- Roles are matched by suffix so the guard works with any DB_ROLE_PREFIX.
CREATE OR REPLACE FUNCTION journal.outbox_guard() RETURNS trigger AS $$
BEGIN
  IF (current_user LIKE '%news\_rw'     AND NEW.channel NOT IN ('news_flags','heartbeat'))
  OR (current_user LIKE '%notifier\_rw' AND NEW.channel NOT IN ('inbound_cmd','heartbeat')) THEN
    RAISE EXCEPTION 'role % may not publish to channel %', current_user, NEW.channel;
  END IF;
  NEW.source := CASE WHEN current_user LIKE '%news\_rw' THEN 'news'
                     WHEN current_user LIKE '%notifier\_rw' THEN 'notifier'
                     ELSE 'core' END;
  PERFORM pg_notify(NEW.channel, NEW.id::text);
  RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER outbox_guard BEFORE INSERT ON journal.outbox
FOR EACH ROW EXECUTE FUNCTION journal.outbox_guard();
