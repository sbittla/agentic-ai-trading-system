-- Idempotent schema/role upgrades (REQUIREMENTS 1.3). Safe to run on every start:
--   fresh volumes: init.sh runs it right after init.sql
--   existing volumes: run.sh runs it as the admin user (upgrade.sh) before migrations
-- psql variables: prefix, console_pw

\set core_role :prefix 'core_rw'
\set console_role :prefix 'console_rw'

-- Owner console (UI-C2): reads everything, may publish only owner commands, writes only its
-- own notes table (grants on tables are applied by `trader migrate`).
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'console_role', :'console_pw')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'console_role') \gexec
SELECT format('ALTER ROLE %I PASSWORD %L', :'console_role', :'console_pw') \gexec
GRANT USAGE ON SCHEMA journal TO :"console_role";
GRANT INSERT, SELECT ON journal.outbox TO :"console_role";
GRANT USAGE ON SEQUENCE journal.outbox_id_seq TO :"console_role";
ALTER DEFAULT PRIVILEGES FOR ROLE :"core_role" IN SCHEMA journal GRANT SELECT ON TABLES TO :"console_role";

-- Outbox guard: console may publish only owner commands, tagged source=console (SEC-17).
CREATE OR REPLACE FUNCTION journal.outbox_guard() RETURNS trigger AS $$
BEGIN
  IF (current_user LIKE '%news\_rw'     AND NEW.channel NOT IN ('news_flags','heartbeat'))
  OR (current_user LIKE '%notifier\_rw' AND NEW.channel NOT IN ('inbound_cmd','heartbeat'))
  OR (current_user LIKE '%console\_rw'  AND NEW.channel NOT IN ('inbound_cmd')) THEN
    RAISE EXCEPTION 'role % may not publish to channel %', current_user, NEW.channel;
  END IF;
  NEW.source := CASE WHEN current_user LIKE '%news\_rw' THEN 'news'
                     WHEN current_user LIKE '%notifier\_rw' THEN 'notifier'
                     WHEN current_user LIKE '%console\_rw' THEN 'console'
                     ELSE 'core' END;
  PERFORM pg_notify(NEW.channel, NEW.id::text);
  RETURN NEW;
END $$ LANGUAGE plpgsql;
