#!/bin/sh
# Apply upgrade.sql (idempotent). Called by init.sh on a fresh volume and by run.sh on start.
set -e
secret() {
  if [ -f "/run/secrets/$1" ]; then tr -d '\r\n' < "/run/secrets/$1"
  else echo "missing secret $1 (run ./run.sh setup)" >&2; exit 1; fi
}
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" -q \
  -v prefix="${DB_ROLE_PREFIX:-}" \
  -v console_pw="$(secret pg_console_password)" \
  -f /init/upgrade.sql
