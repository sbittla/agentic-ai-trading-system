#!/bin/sh
# OPS-1: restore the newest nightly dump into a throwaway Postgres (tmpfs) and print one JSON
# line: {"file", "counts": {table: rows}, "last_event"}. run.sh pipes it to
# `trader restore-test`, which compares it with production and records the result.
set -e
f=""
for x in /backups/trader_*.dump; do [ -e "$x" ] && f=$x; done   # names sort by date
if [ -z "$f" ]; then echo '{"error": "no backup found in /backups"}'; exit 1; fi
export PGDATA=/tmp/pgdata
initdb -U restore --auth=trust >/dev/null
pg_ctl -o "-c listen_addresses='' -k /tmp" -w start >/dev/null
createdb -h /tmp -U restore scratch
pg_restore -h /tmp -U restore -d scratch --no-owner --no-privileges "$f" >/tmp/restore.log 2>&1 || true
psql -h /tmp -U restore -d scratch -At -v file="$(basename "$f")" <<'SQL'
SELECT json_build_object(
  'file', :'file',
  'counts', COALESCE((SELECT json_object_agg(table_name,
      (xpath('/row/c/text()', query_to_xml(format('SELECT count(*) AS c FROM %I.%I',
                                                  table_schema, table_name),
                                           false, true, '')))[1]::text::int)
    FROM information_schema.tables WHERE table_schema = 'journal'), '{}'::json),
  'last_event', (SELECT to_char(max(ts) AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"+00:00"')
                 FROM journal.events));
SQL
pg_ctl -w stop >/dev/null
