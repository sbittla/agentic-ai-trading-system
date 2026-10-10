# Setup: Machine, Accounts and Secrets

**Version:** 1.1 (October 2, 2026) · Companion to [RUNBOOK.md](RUNBOOK.md) §1 and [SECURITY.md](SECURITY.md)

How to create every external account, key and password the system needs before paper trading (milestone M8), and where each one goes. Do this once. Keys go only into the git-ignored env files in the repo root, never into `config/config.yaml`, code, chat or tickets (SEC-6, SEC-18).

## Quick path

```powershell
trader init-env --prefix srb          # creates every missing local file from the *.example templates
# fill in the keys it lists (sections 1-5), then:
trader check-connections --service core --service news --service notifier `
  --env-file .env.core --env-file .env.news --env-file .env.notifier
```

`init-env` never overwrites an existing file. It creates `docker/.env` (database and role names), five random passwords in `secrets/`, the five `.env.*` files with their `DATABASE_URL` already filled in, and `config/config.yaml`; then it lists the values still empty.

## Template files (tracked in git, no values)

| Template | Real file (git-ignored) | Container |
| --- | --- | --- |
| [`.env.core.example`](../.env.core.example) | `.env.core` | trader-core |
| [`.env.news.example`](../.env.news.example) | `.env.news` | news-sentiment |
| [`.env.notifier.example`](../.env.notifier.example) | `.env.notifier` | notifier |
| [`.env.backup.example`](../.env.backup.example) | `.env.backup` | backup |
| [`.env.replay.example`](../.env.replay.example) | `.env.replay` | replay, dev |
| [`.env.live.example`](../.env.live.example) | `.env.live` | trader-core, go-live only |
| [`docker/.env.example`](../docker/.env.example) | `docker/.env` | Compose (names, model) |
| [`config/config.example.yaml`](../config/config.example.yaml) | `config/config.yaml` | every container (read-only) |
| [`secrets/README.md`](../secrets/README.md) | `secrets/pg_<role>_password.txt` | postgres |

## 0. Machine: Docker Desktop and the NVIDIA GPU

Qwen runs in the `ollama` container on the local NVIDIA GPU (QW-1).

1. Install the latest **NVIDIA driver** for Windows (it includes WSL GPU support; do not install a Linux driver inside WSL).
2. Update WSL: `wsl --update`, then `wsl --shutdown`.
3. Install **Docker Desktop** with the WSL2 backend. Check GPU passthrough:
   `docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi` must show your GPU.
4. Pick the model for your GPU (QW-6): run `trader select-model` on the Windows host (it reads `nvidia-smi`). It lists which models fit fully in GPU memory and recommends one; `trader select-model --apply` writes it to both `docker/.env` (`OLLAMA_MODEL`) and `config/config.yaml` (`llm.model`). On the reference machine (RTX 4060 Laptop, 8 GB) that is `qwen3:8b`; 12–16 GB cards get `qwen3:14b`. Details and the full table: [HARDWARE.md](HARDWARE.md).
5. After the first start (section 8), verify it really runs on the GPU:
   `docker compose -f docker/docker-compose.yml run --rm --no-deps trader-core trader check-connections --service llm`
   must print `PASS llm gpu 100% on GPU`. The Ollama log shows `library=CUDA` with your GPU name.

Compose already passes the GPU through (`gpus: all`) and sizes Ollama for an 8 GB card: one model loaded, one request at a time, flash attention and an 8-bit KV cache, kept loaded for 8 hours. If `llm gpu` reports less than 100%, part of the model runs on the CPU and calls take minutes: close other GPU programs, lower `llm.max_context_tokens`, or use a smaller model.

**Docker Desktop stability (seen on this machine; full recovery sequence in [TROUBLESHOOTING.md](TROUBLESHOOTING.md) §1):** large downloads inside Docker crashed Docker Desktop's WSL engine several times (`wsl-bootstrap` exit status 0xc00000fd), even with the `model-pull` service limited to 2 download streams. The reliable path is to download the model on Windows and copy it into the volume (resumable, sha256-verified, and the runtime `ollama` still never touches the internet):

```powershell
.venv\Scripts\python tools/fetch_ollama_model.py qwen3:8b data/ollama_models   # rerun to resume
powershell -File tools/import_ollama_model.ps1                                  # copy into the volume, restart ollama
```

`data/` is git-ignored; delete `data/ollama_models` after the import to free 5 GB. Keeping Docker Desktop and WSL updated helps; a PATH entry for a missing drive (`Failed to translate 'E:\...'`) also appears in those logs and is worth removing.

## What is needed

| Item | Required? | Used by | Goes into |
| --- | --- | --- | --- |
| Alpaca **paper** API key and secret | Yes | `trader-core` (orders, market data, history) | `.env.core` |
| Finnhub API key | Yes | `news-sentiment` (news, earnings calendar, market caps) | `.env.news` |
| SEC EDGAR User-Agent (name + email) | Yes (no account) | `news-sentiment` (8-K filings) | `.env.news` and `config/config.yaml` |
| Reddit "script" app | Optional | `news-sentiment` (social-volume flag) | `.env.news` |
| Telegram bot (default) **or** Twilio WhatsApp | Yes for approve mode | `notifier` | `.env.notifier` |
| Postgres role passwords (4) | Yes | every container | `secrets/pg_<role>_password.txt` and the matching `.env.*` file |
| Postgres admin password | Yes | `postgres` (initialisation only) | `secrets/pg_admin_password.txt` |
| Database and role names | Optional | `postgres`, `trader migrate` | `docker/.env` |
| Alpaca **live** keys | Not yet | go-live only (RUNBOOK §6) | `.env.live` |

Without Finnhub there is no earnings calendar, so earnings locks (A12-1) and the pre-earnings exit (XR-6) cannot work, GAP_AND_GO has no catalyst, and the Sunday universe check (A1-2) does not run. Without Reddit only the `SOCIAL_SPIKE` flag is lost. Without the notifier, approve mode blocks every entry (ARCHITECTURE §6).

---

## 1. Alpaca (paper)

1. Log in at **alpaca.markets** and switch to the **Paper Trading** account (top-left account selector).
2. On the paper dashboard, generate an API key. Paper keys usually start with `PK`. Copy the secret now: it is shown only once.
3. Nothing else is needed: the paper account includes the free IEX real-time feed the system uses (ADR-003).
4. Do **not** generate live keys until every stage of [TEST_PLAN.md](TEST_PLAN.md) has passed (SEC-7).

## 2. Finnhub

1. Go to **finnhub.io** and choose **Get free API key**; sign up with email or Google.
2. The API key is shown on the dashboard.
3. The free tier (about 60 calls a minute, personal use) covers the system's budget (DATA_SPEC §1).

## 3. SEC EDGAR (no account)

EDGAR's fair-access policy requires a User-Agent with your name and email; there is no sign-up. Example:

```
agentic-ai-trading-system Your Name you@example.com
```

Put the same string in two places: `SEC_USER_AGENT` in `.env.news`, and `data.sec_user_agent` in `config/config.yaml`. `trader validate-config` fails if the config value has no email.

## 4. Reddit (optional)

1. Log in at **reddit.com/prefs/apps** and choose **create another app**.
2. Choose type **script**, any name, redirect URI `http://localhost:8080` (unused).
3. After creating it: the **client ID** is the short string under the app name; the **secret** is labelled "secret".
4. User agent: `agentic-ai-trading-system/0.1 by <your-reddit-username>`.

Reddit has recently required approval for new API apps. If it asks for a request, skip Reddit and leave the three `REDDIT_*` values empty: the news service runs without it.

## 5. Owner messages: Telegram bot (default)

Telegram is the default channel (ADR-014): free, no templates and no 24-hour window.

1. In Telegram, open **@BotFather**, send `/newbot`, and choose a display name and a username ending in `bot`.
2. Copy the **bot token** (looks like `123456789:AA…`) into `TELEGRAM_BOT_TOKEN` in `.env.notifier`, and set `NOTIFIER_CHANNEL=telegram`.
3. Open your new bot in Telegram, press **Start**, and send `STATUS`.
4. Find your chat id: `trader telegram-chats --env-file .env.notifier`. Put it in `TELEGRAM_CHAT_ID` (and, if more than one person may send commands, a comma-separated list in `TELEGRAM_ALLOWED_CHAT_IDS`; otherwise only `TELEGRAM_CHAT_ID` can).
5. Check: `trader check-connections --service notifier --env-file .env.notifier` must print `PASS`.

Never add the bot to a group, and never set a webhook for it: the notifier long-polls `getUpdates`, which stops working while a webhook is set. If the token leaks, send `/revoke` to @BotFather and update the env file.

## 5b. Owner messages: Twilio WhatsApp (alternative)

Use this only with `NOTIFIER_CHANNEL=whatsapp`.

1. Sign up at **twilio.com**; the trial credit is enough to start.
2. In the console open **Messaging → Try it out → Send a WhatsApp message** (the WhatsApp Sandbox).
3. From your phone, send the shown `join <two-words>` message to the sandbox number (usually `+1 415 523 8886`).
4. Copy the **Account SID** and **Auth Token** from the console home page.
5. The values for `.env.notifier`:

| Variable | Value |
| --- | --- |
| `TWILIO_SID` | Account SID |
| `TWILIO_TOKEN` | Auth Token |
| `WHATSAPP_FROM` | `whatsapp:+14155238886` (the sandbox number) |
| `WHATSAPP_TO` | `whatsapp:+1XXXXXXXXXX` (your phone, with country code) |
| `WHATSAPP_ALLOWED_SENDERS` | `whatsapp:+1XXXXXXXXXX` (your phone again; the only number allowed to send commands, SEC-1; comma-separate several) |

**Using your own WhatsApp sender instead of the sandbox:** set `WHATSAPP_FROM` to your approved Twilio WhatsApp number and, to reach you outside the 24-hour window, `WHATSAPP_CONTENT_SID` to an approved content template whose body has one variable `{{1}}`; the notifier sends free-form text while you have messaged the bot in the last 24 hours and otherwise puts the text into `{{1}}` of that template (`WHATSAPP_CONTENT_VARIABLE`, default `1`). A template such as "Trading update: {{1}}" works; a template with a different fixed wording or several variables does not.

**Twilio trial accounts with a trial WhatsApp number** (the "join twilio-trial" flow, tested on October 1, 2026): free-form messages are rejected with "ContentSid Required" even inside the 24-hour window, and the `HX…` ContentSid in the console's sample `curl` is Twilio's fixed sample (an appointment reminder) that ignores `{{1}}`, so do not use it as `WHATSAPP_CONTENT_SID`. Inbound replies do reach the account. The notifier cannot send its own text through such a sender. Use one of:

1. The classic **WhatsApp Sandbox** (`WHATSAPP_FROM=whatsapp:+14155238886`, join with its `join <two-words>` code), which accepts free-form text within 24 hours of your last message; or
2. **Upgrade** the Twilio account, register your own WhatsApp sender, and create a template with one `{{1}}` body variable for `WHATSAPP_CONTENT_SID`.

In either case, keep the 24-hour window open by replying `STATUS` once a day (after the 4:30 PM review covers the next day's 7:00 pre-flight and both windows). At start-up the notifier looks back 24 hours for your last message, so a restart does not lose an open window; those old messages are never re-executed as commands.

**Sandbox limits:**

- WhatsApp allows free-form business messages only within 24 hours of your last message to the number. Reply `STATUS` once a day (for example after the 4:30 PM review) so the 7:00 pre-flight and the approval requests keep arriving.
- The sandbox join can lapse after a few days. If messages stop, send the `join …` text again.
- A permanent WhatsApp sender needs Meta business verification and approved message templates. The sandbox is fine for paper trading; revisit before go-live. Alternative if WhatsApp proves unreliable: a Telegram bot (ADR-013).

## 6. Database names, passwords and env files

No password is stored in a tracked file: Postgres reads every password from git-ignored files in `secrets/` (Docker secrets), and `docker/postgres/init.sh` passes them to `init.sql` at initialisation.

### Database and role names (optional)

Copy [`docker/.env.example`](../docker/.env.example) to `docker/.env` (git-ignored). Example with the prefix `srb`:

```
PG_DATABASE=srb_trader
PG_ADMIN_USER=srb_admin
DB_ROLE_PREFIX=srb_
OLLAMA_MODEL=qwen3:8b
```

The four service roles become `srb_core_rw`, `srb_news_rw`, `srb_notifier_rw` and `srb_readonly`. Without `docker/.env` the defaults are `trader`, `trader_admin` and no prefix. Names are applied only when the Postgres volume is first created.

### Passwords

Create five files in `secrets/`, each holding one password on one line (no newline needed). In PowerShell:

```powershell
foreach ($r in "admin","core","news","notifier","readonly") {
  $pw = -join ((48..57)+(65..90)+(97..122) | Get-Random -Count 28 | % {[char]$_})
  Set-Content -NoNewline -Path "secrets\pg_$($r)_password.txt" -Value $pw
}
```

- Letters and digits only, so the passwords can go into URLs unescaped.
- They are read only when the Postgres volume is first created. To change one later: `ALTER ROLE <role> PASSWORD '...'` as the admin user, then update the secret file and the matching env file.

### Create the env files

Run `trader init-env` (or copy each `.env.<name>.example` to `.env.<name>` in the repo root) and fill in the keys. Each container reads only its own file (SEC-18). The user and database in each `DATABASE_URL` must match `docker/.env` and the password must match the secret file:

| File | Contents |
| --- | --- |
| `.env.core` | `ALPACA_PAPER_KEY`, `ALPACA_PAPER_SECRET`, `DATABASE_URL=postgresql+psycopg://srb_core_rw:<core password>@postgres:5432/srb_trader` |
| `.env.news` | `FINNHUB_TOKEN`, `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_USER_AGENT` (Reddit values may stay empty), `SEC_USER_AGENT`, `DATABASE_URL` with `srb_news_rw` |
| `.env.notifier` | the Twilio/WhatsApp values from step 5, `DATABASE_URL` with `srb_notifier_rw` |
| `.env.backup` | `DATABASE_URL` with `srb_readonly` |
| `.env.replay` | `REPLAY=1` (no keys) |

Rules for these files:

- One `KEY=value` per line; no quotes and no inline comments after a value.
- The host in every `DATABASE_URL` is `postgres` (the Compose service name).
- Confirm nothing is tracked: `git status --ignored` must show every `.env.*` file, `docker/.env`, `secrets/` and `config/config.yaml` as ignored (`!!`), and `git status` must not list them.

## 7. Config

In `config/config.yaml` (git-ignored copy of `config/config.example.yaml`):

- `data.sec_user_agent`: your name and email (step 3).
- `llm.model`: set by `trader select-model --apply` together with `OLLAMA_MODEL` (QW-6); `qwen3:8b` on the 8 GB reference GPU, `qwen3:14b` on 12–16 GB.
- Keep `mode.environment: paper`, `day_trades: approve`, `swing_trades: approve` for the first paper days (TEST_PLAN PT-1).
- Run `trader validate-config --config config/config.yaml`; it must print `OK`.

## 8. First start

Check the credentials first (read-only: no orders, no messages):

```powershell
trader --config config/config.yaml check-connections --service core --service news --service notifier `
  --env-file .env.core --env-file .env.news --env-file .env.notifier
```

Every line must say `PASS`. Then:

```powershell
docker compose -f docker/docker-compose.yml up -d postgres ollama
docker compose -f docker/docker-compose.yml --profile setup run --rm model-pull   # model from docker/.env
docker compose -f docker/docker-compose.yml run --rm --no-deps trader-core trader migrate
docker compose -f docker/docker-compose.yml run --rm --no-deps trader-core trader check-connections --service core --service db --service llm
trader fetch-history                                                              # optional: 2 years of bars into data/bars (host)
docker compose -f docker/docker-compose.yml up -d
docker compose -f docker/docker-compose.yml ps                                    # every service healthy
```

Then:

1. Send `STATUS` to your bot (or on WhatsApp); a status message must come back (inbound polling works).
2. Optionally check the model against the evaluation set: `trader eval-prompts --llm ollama` (AGENT_PROMPTS §8).
3. The next trading day, the 7:00 pre-flight message must arrive (RUNBOOK §1 step 9).

## 9. Key hygiene

| Key | Rotate | If leaked |
| --- | --- | --- |
| Alpaca paper | Every 90 days | Revoke in the Alpaca dashboard, send `KILL`, create a new key, update `.env.core`, restart `trader-core` |
| Finnhub, Reddit | Every 180 days | Regenerate, update `.env.news`, restart `news-sentiment` |
| Telegram bot | Every 180 days | `/revoke` in @BotFather, put the new token in `.env.notifier`, restart `notifier` |
| Twilio | Every 180 days | Rotate the Auth Token in the console, update `.env.notifier`, restart `notifier` |
| Postgres roles | Every 180 days | `ALTER ROLE`, update `secrets/pg_<role>_password.txt` and the matching env file, restart that container |

Full leak response and key table: [SECURITY.md](SECURITY.md) §3.
