# secrets/

Git-ignored folder (only this README is tracked). Docker reads these files as secrets.
`trader init-env` creates them with random 28-character passwords; never commit them.

| File | Used by |
| --- | --- |
| `pg_admin_password.txt` | Postgres admin, at database initialisation only |
| `pg_core_password.txt` | role `<prefix>core_rw` (trader-core); also in `.env.core` `DATABASE_URL` |
| `pg_news_password.txt` | role `<prefix>news_rw` (news-sentiment); also in `.env.news` |
| `pg_notifier_password.txt` | role `<prefix>notifier_rw` (notifier); also in `.env.notifier` |
| `pg_readonly_password.txt` | role `<prefix>readonly` (backup, Grafana); also in `.env.backup` |

One password per file, letters and digits only, no trailing newline. Passwords are read only
when the Postgres volume is first created; to change one later see docs/ACCOUNT_SETUP.md §6.
