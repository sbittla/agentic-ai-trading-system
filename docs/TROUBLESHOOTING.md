# Troubleshooting

**Version:** 1.0 (October 2, 2026) · Companion to [RUNBOOK.md](RUNBOOK.md) and [ACCOUNT_SETUP.md](ACCOUNT_SETUP.md)

Known problems found while setting the system up on the owner's Windows machine (RTX 4060 Laptop GPU, 8 GB; Docker Desktop with WSL2), with the symptom to look for, the cause and the fix. Add a row whenever a new problem is solved.

Trading safety does not depend on any of these: broker-side bracket stops protect open positions, and entries stop automatically whenever Docker, Postgres, Ollama or the data stream is unhealthy (ARCHITECTURE §6).

---

## 1. Recovery sequence: Docker Desktop crashes or the model will not load

Run these in order; each step depends on the one before.

### 1.1 Repair Docker Desktop and WSL

0. **Root cause on this machine (found Oct 2, 2026): no page file.** With the page file disabled, Windows can commit only as much memory as there is RAM (31.7 GB), and commit use sat at about 31.2 GB (about 9 GB of it the WSL VM). Every large allocation then fails: `docker.exe` dies with `fatal error: out of memory allocating heap arena map`, OpenBLAS fails, and Docker Desktop's engine crashes. Check:
   ```powershell
   $os = Get-CimInstance Win32_OperatingSystem
   "Commit: {0:N1} of {1:N1} GB" -f (($os.TotalVirtualMemorySize-$os.FreeVirtualMemory)/1MB), ($os.TotalVirtualMemorySize/1MB)
   (Get-CimInstance Win32_ComputerSystem).AutomaticManagedPagefile   # False and no Win32_PageFileUsage = no page file
   ```
   Fix (PowerShell **as administrator**, then restart Windows):
   ```powershell
   $cs = Get-CimInstance Win32_ComputerSystem
   Set-CimInstance -InputObject $cs -Property @{AutomaticManagedPagefile = $true}
   ```
   Or: **System Properties → Advanced → Performance → Settings → Advanced → Virtual memory → Change → "Automatically manage paging file size for all drives"**.
1. **Update Docker Desktop:** gear icon → **Software updates** → install. If it will not open, reinstall from docker.com over the existing install (images and volumes are kept).
2. **Update and reset WSL** (PowerShell as administrator):
   ```powershell
   wsl --update
   wsl --shutdown
   ```
3. **Remove stale PATH entries.** Docker's error log showed `wsl: Failed to translate 'E:\Program Files\gradle-8.8\bin'` (a folder on a missing drive). Find it:
   ```powershell
   [Environment]::GetEnvironmentVariable("Path","User")    -split ';' | Select-String gradle
   [Environment]::GetEnvironmentVariable("Path","Machine") -split ';' | Select-String gradle
   ```
   Then **Start → "Edit the system environment variables" → Environment Variables…** and delete (or correct) that entry in the list where it appeared. Check for other entries pointing at drives that no longer exist. For the system PATH, an administrator PowerShell can do it in one line:
   ```powershell
   $p = [Environment]::GetEnvironmentVariable("Path","Machine"); [Environment]::SetEnvironmentVariable("Path", ((($p -split ';') | Where-Object { $_ -and $_ -notmatch 'gradle-8.8' }) -join ';'), "Machine")
   ```
4. **Give the WSL VM headroom** (memory was nearly exhausted during the crashes). Create `C:\Users\<you>\.wslconfig`:
   ```ini
   [wsl2]
   memory=12GB
   swap=8GB
   ```
   then `wsl --shutdown` again.
5. **Restart Docker Desktop** (`docker desktop restart`) and confirm GPU access:
   ```powershell
   docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi
   ```

### 1.2 Load the model from the host copy

The model is downloaded on Windows (outside Docker) and copied in, so the runtime `ollama` container never needs the internet (SEC-9).

```powershell
cd D:\Learning\AIAgentsTrading
# only if data\ollama_models is missing or incomplete (resumes, verifies sha256):
.venv\Scripts\python tools/fetch_ollama_model.py qwen3:8b data/ollama_models
docker compose -f docker/docker-compose.yml up -d ollama
powershell -ExecutionPolicy Bypass -File tools/import_ollama_model.ps1   # last line must list qwen3:8b
```

### 1.3 Verify GPU and every connection

```powershell
docker compose -f docker/docker-compose.yml up -d postgres
docker compose -f docker/docker-compose.yml build trader-core
docker compose -f docker/docker-compose.yml run --rm --no-deps trader-core trader check-connections --service core --service db --service llm
```

Every line must say `PASS`, including `llm gpu 100% on GPU`. Then `data\ollama_models` can be deleted (5 GB).

### 1.4 Telegram chat id (first setup, or after a new bot token)

Open the bot in Telegram, press **Start**, send `STATUS`, then:
```powershell
.venv\Scripts\trader telegram-chats --env-file .env.notifier
```
Put the printed id in `TELEGRAM_CHAT_ID=` in `.env.notifier`.

### 1.5 Start everything

```powershell
docker compose -f docker/docker-compose.yml up -d
docker compose -f docker/docker-compose.yml ps        # every service healthy
```

If step 1.2 still crashes Docker after 1.1, copy the model in small pieces (split the large blob into about 250 MB parts, `docker cp` each, join them inside the container, check the sha256). This is not scripted yet.

---

## 2. Known issues

### Docker and the model

| Symptom | Cause | Fix |
| --- | --- | --- |
| `docker cp` or other `docker` commands: `fatal error: out of memory allocating heap arena map` | Windows commit limit exhausted: no page file (§1.1 step 0) | Enable the page file and restart Windows |
| `Error response from daemon: Docker Desktop is unable to start`; log shows `wsl-bootstrap ... exit status 0xc00000fd` | Docker Desktop's WSL engine crashed during a large transfer into the VM (a model download, a 5 GB copy, a big image build). Happened 8 times on Oct 1–2, 2026; most likely the missing page file (§1.1 step 0) | §1.1, then `docker desktop restart`. Avoid large transfers into Docker: use §1.2 for models |
| `docker compose ... model-pull` dies part-way; the next attempt keeps only part of the download | Same crash, triggered by the download itself | Use §1.2 (`tools/fetch_ollama_model.py` + `tools/import_ollama_model.ps1`). `model-pull` already uses 2 download streams instead of 4 |
| `cp: error reading '/src/blobs/...': Input/output error` while importing | Large files read through a Windows bind mount (`-v D:\...:/src`) fail on this Docker Desktop | The import script uses `docker cp` instead (already done) |
| `docker cp ... 502 Bad Gateway` during import | Docker crashed mid-copy | §1.1, then rerun the import script (it re-copies and verifies) |
| `ollama pull` inside the `ollama` container: `dial tcp: lookup registry.ollama.ai ... server misbehaving` | By design the runtime `ollama` is on the internal network with no internet (SEC-9) | Use the `model-pull` setup service or §1.2. Do not attach `ollama` to the internet |
| `Error: pull model manifest: ssh: no key found` | `/root/.ollama/id_ed25519` was left empty (0 bytes) by a crash during Ollama's first start | `docker exec <ollama> rm -f /root/.ollama/id_ed25519 /root/.ollama/id_ed25519.pub`, then `docker restart <ollama>`; Ollama regenerates them |
| `check-connections --service llm`: `only NN% of X GB in GPU memory` | Model does not fit in VRAM; part runs on the CPU and calls take minutes | Close other GPU programs; lower `llm.max_context_tokens`; or use a smaller model. With 8 GB use `qwen3:8b`, not 14B; `trader select-model` shows what fits |
| Ollama log lacks `library=CUDA` | GPU not passed through | Check the NVIDIA driver and `docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi` (§1.1 step 5) |
| http://localhost:3000 does not load; `./run.sh status` lists no `grafana` / `grafana-proxy` | Grafana is in the `monitoring` profile: a plain `./run.sh` / `docker compose up -d` does not start it | `./run.sh monitoring` (or `./run.sh start --monitoring`). If it still fails, the command prints Grafana's log; log in as `admin` with `secrets/grafana_admin_password.txt` |
| Your other containers (for example `llm_qdrant`, `llm_redis`) are running again after a Docker restart | Their restart policy relaunches them with Docker | `docker stop <name>`, or `docker update --restart=no <name>` |

### Host and Python

| Symptom | Cause | Fix |
| --- | --- | --- |
| `OpenBLAS error: Memory allocation still failed` when running `trader backtest` | Windows commit memory exhausted: no page file (§1.1 step 0) | Enable the page file; meanwhile close programs or `$env:OPENBLAS_NUM_THREADS=1` |
| A folder `D:\app\data\bars` appeared | A host run used the container path `/app/data/bars` (fixed: host runs now map it to `<repo>\data\bars`) | Delete `D:\app` if it still exists |
| Edit scripts corrupt `§`, `—` or other non-ASCII text in source files | Windows Python defaults to the cp1252 encoding | Always read and write files with `encoding="utf-8"` (or set `PYTHONUTF8=1`) |
| `init.sh` fails inside Postgres with `\r` errors | Windows line endings | `.gitattributes` forces LF for `*.sh` and `docker/postgres/*.sql`; re-checkout if needed |

### Owner Console (http://localhost:8080)

| Symptom | Cause | Fix |
| --- | --- | --- |
| Browser cannot connect | Console not running, or skipped with `--no-console` | `./run.sh console` (shows the last log lines if it fails); `./run.sh status` |
| `console password file … missing` or `at least 12 characters` in its log | `secrets/console_password.txt` absent or too short | `./run.sh setup` creates it (never overwrites); or write 12+ characters into it, then `./run.sh console` |
| `port is already allocated` for 8080 | Another program uses port 8080 | Stop it, or change the left side of `127.0.0.1:8080:8080` in `docker/docker-compose.yml` (keep `127.0.0.1`) |
| "too many attempts: wait 5 minutes" | Five wrong passwords in 5 minutes (UI-21) | Wait 5 minutes; the password is in `secrets/console_password.txt` |
| Logged out while working | 30 minutes without a request (`CONSOLE_IDLE_MIN`) | Log in again; raise `CONSOLE_IDLE_MIN` in `.env.console` if needed |
| Home says "trader-core API unavailable: showing its last snapshot" | trader-core is down or restarting (its API on port 8081 is internal only) | `./run.sh logs trader-core`; the console keeps working read-only from the journal (UI-27) |
| `permission denied` for the console in `./run.sh logs console` | Console role or grants missing (an old database) | `./run.sh` reruns `upgrade.sql` and `trader migrate`, both idempotent |
| A console command says "sent; trader-core has not answered yet" | trader-core is not consuming `inbound_cmd` (down or busy) | Check `./run.sh status`; the command is kept in the outbox and runs when trader-core is back, so do not resend destructive ones |
| Ask always answers "table answer" | Entry window (by design, UI-8b), model unloaded, or `CONSOLE_ASK_MODEL=0` | Wait for the window to end; the Model page shows whether Qwen is loaded |
| `trader: error: argument cmd: invalid choice: 'backfill-news'` (or `'recap'`) | Images and containers were built from older code (after a `git pull`) | Update `run.sh` too (`git pull`); `./run.sh backfill` now rebuilds the images, runs `trader migrate` and uses one-off containers from the new images, then restarts the running services on the new code (trader-core only outside 9:25 AM–4:05 PM ET). `./run.sh` also rebuilds everything |
| Analyst ratings, News or Market recap say "No … yet" | Fresh install: the news service polls from 7:15 AM ET on market days and the recap is built on request | `./run.sh backfill 30` once (or `RECAP 21` on Telegram / the Market recap buttons for the recap only) |
| Backfill finds news but no rating actions | Finnhub's `stock/upgrade-downgrade` is not in the free plan (the feed logs `finnhub_ratings_not_in_plan` and continues with RSS), or the network blocks `feeds.finance.yahoo.com` / `news.google.com` | `./run.sh logs news-sentiment` for `ratings_rss_failed`; allow those hosts; the RSS rules only count headlines whose subject is the ticker, so quiet weeks can have none |
| `RECAP needs the market-data history client` | trader-core started without Alpaca data keys | Fill in `.env.core`, restart trader-core |
| Options tab: "options module is not running" | `options.mode: off`, or no options data on the account | Set `options.mode: advisory` (restart applies next session, SR-5) |

### Database

| Symptom | Cause | Fix |
| --- | --- | --- |
| `trader migrate`: `permission denied for database` on `CREATE SCHEMA` | The least-privilege role has no database-level CREATE (fixed: the migration now checks for the schema first) | Use the current code; the schema is created by `init.sql` |
| Changing `docker/.env` names or `secrets/` passwords has no effect | Postgres reads them only when its volume is first created | Change passwords with `ALTER ROLE <role> PASSWORD '...'` as the admin user and update the env file; to start over: `docker compose -f docker/docker-compose.yml down` then `docker volume rm agentic-ai-trading-system_pg-data` (**deletes the journal**) |
| `trader migrate` prints `FAILED grants` | `DB_ROLE_PREFIX` does not match the roles that exist | Make `DB_ROLE_PREFIX` in `docker/.env` match the roles (`srb_` for `srb_core_rw` …) |
| `BACKUP FAILED` in `./run.sh logs backup`, or `permission denied for sequence` | The read-only backup role could not read sequences (databases created before October 10, 2026) | `./run.sh` (reruns `upgrade.sql` and `trader migrate`, which grant it); then `./run.sh restore-test` to prove the backup restores |
| `./run.sh restore-test`: `no backup found` | No nightly dump yet in `BACKUP_PATH` | Wait for 18:00 or run `docker compose -f docker/docker-compose.yml exec backup trader backup --once` |

### Owner messages

| Symptom | Cause | Fix |
| --- | --- | --- |
| Twilio: `21654 ContentSid Required` even inside the 24-hour window | Twilio **trial** WhatsApp numbers ("join twilio-trial") send templates only, and the console's sample `HX…` template is a fixed appointment reminder that ignores our text | Use Telegram (default, ADR-014), or upgrade Twilio and create a template with one `{{1}}` variable |
| Twilio replies with "Standard auto-reply…" | Your message reached Twilio's trial number (inbound works) | Nothing to fix; see the row above for sending |
| `./run.sh chats`: `no messages yet` | You have not messaged the bot yet | Open the bot, press **Start**, send `STATUS`, rerun |
| Telegram bot stops receiving messages | A webhook was set for the bot, which disables `getUpdates` | Never set a webhook; to clear one: `https://api.telegram.org/bot<token>/deleteWebhook` |
| Approvals never arrive in approve mode | Notifier unhealthy or chat id missing; approve mode then blocks entries (by design, NF-1: incident `NOTIFIER_DOWN`, e-mail if SMTP is set) | `./run.sh connections`; the Owner Console still shows approvals (type the code from Telegram when it is back) and its Controls page sends KILL; `docker compose -f docker/docker-compose.yml exec trader-core trader kill` also works |

### Market data and broker

| Symptom | Cause | Fix |
| --- | --- | --- |
| A ticker is blocked with `UR-3` | Fewer than 60 trading days of history (SPCX listed 2026-06-12 and passed with 76 days) | Wait until it has 60 days, or remove it from `universe.tickers` |
| Pre-flight RED: `macro calendar has no CPI date on or after today` | `config/macro_calendar_2026_2027.yaml` has no future CPI date (2027 dates still to add) | Add the BLS dates (RUNBOOK §5 maintenance) |
| Start refused: `clock drift … > 2s` (trader-core keeps restarting) | The Windows clock is off, or Docker's VM clock is | Settings → Time & language → **Sync now**, or the `w32time` fix in the next row. Still refused although `w32tm /stripchart` shows the host within ±0.1 s? The Docker VM is behind on its own: `docker run --rm --privileged alpine ntpd -q -n -p time.windows.com`, then `docker compose -f docker/docker-compose.yml restart trader-core` |
| Health `clock` AMBER (`drift -0.8s`, 0.5–2 s) | The Windows Time service (`w32time`) is stopped, so the host drifts; Docker Desktop's WSL2 VM copies the host clock and drifts again after sleep (docker/for-win#4526, microsoft/WSL#10006) | In an **admin** PowerShell: `Set-Service w32time -StartupType Automatic; Start-Service w32time; w32tm /resync /force`, then `docker run --rm --privileged alpine hwclock -s` (if the VM stays behind, its hardware clock is off too: `docker run --rm --privileged alpine ntpd -q -n -p time.windows.com` instead). Check with `w32tm /stripchart /computer:time.windows.com /samples:3 /dataonly` (should be under ±0.1 s). Health turns green within 30 s, no restart needed |

---

## 3. Useful checks

```powershell
trader check-connections --service core --service news --service notifier --env-file .env.core --env-file .env.news --env-file .env.notifier
docker compose -f docker/docker-compose.yml run --rm --no-deps trader-core trader check-connections --service db --service llm
docker compose -f docker/docker-compose.yml ps
docker compose -f docker/docker-compose.yml logs --tail 200 <service>
docker logs agentic-ai-trading-system-ollama-1 2>&1 | Select-String "inference compute"
docker desktop status
trader init-env        # lists any setup value still missing
```
