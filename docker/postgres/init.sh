#!/bin/sh
# Database initialisation (runs once, when the Postgres volume is first created).
# Role passwords come from Docker secrets (git-ignored files in secrets/), never from git.
set -e

secret() {
  if [ -f "/run/secrets/$1" ]; then
    tr -d '\r\n' < "/run/secrets/$1"
  else
    echo "missing secret $1" >&2
    exit 1
  fi
}

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v prefix="${DB_ROLE_PREFIX:-}" \
  -v core_pw="$(secret pg_core_password)" \
  -v news_pw="$(secret pg_news_password)" \
  -v notifier_pw="$(secret pg_notifier_password)" \
  -v readonly_pw="$(secret pg_readonly_password)" \
  -f /init/init.sql

# REQUIREMENTS 1.3 roles and guard (same script run.sh applies to existing volumes)
sh /init/upgrade.sh
