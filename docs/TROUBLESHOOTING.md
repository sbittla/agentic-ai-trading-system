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
| `check-connections --service llm`: `only NN% of X GB in GPU memory` | Model does not fit in VRAM; part runs on the CPU and calls take minutes | Close other GPU programs; lower `llm.max_context_tokens`; or use a smaller model. With 8 GB use `qwen3:8b`, not 14B |
| Ollama log lacks `library=CUDA` | GPU not passed through | Check the NVIDIA driver and `docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi` (§1.1 step 5) |
| Your other containers (for example `llm_qdrant`, `llm_redis`) are running again after a Docker restart | Their restart policy relaunches them with Docker | `docker stop <name>`, or `docker update --restart=no <name>` |

### Host and Python

| Symptom | Cause | Fix |
| --- | --- | --- |
| `OpenBLAS error: Memory allocation still failed` when running `trader backtest` | Windows commit memory exhausted: no page file (§1.1 step 0) | Enable the page file; meanwhile close programs or `$env:OPENBLAS_NUM_THREADS=1` |
| A folder `D:\app\data\bars` appeared | A host run used the container path `/app/data/bars` (fixed: host runs now map it to `<repo>\data\bars`) | Delete `D:\app` if it still exists |
| Edit scripts corrupt `§`, `—` or other non-ASCII text in source files | Windows Python defaults to the cp1252 encoding | Always read and write files with `encoding="utf-8"` (or set `PYTHONUTF8=1`) |
| `init.sh` fails inside Postgres with `\r` errors | Windows line endings | `.gitattributes` forces LF for `*.sh` and `docker/postgres/*.sql`; re-checkout if needed |

### Database

| Symptom | Cause | Fix |
| --- | --- | --- |
| `trader migrate`: `permission denied for database` on `CREATE SCHEMA` | The least-privilege role has no database-level CREATE (fixed: the migration now checks for the schema first) | Use the current code; the schema is created by `init.sql` |
| Changing `docker/.env` names or `secrets/` passwords has no effect | Postgres reads them only when its volume is first created | Change passwords with `ALTER ROLE <role> PASSWORD '...'` as the admin user and update the env file; to start over: `docker compose -f docker/docker-compose.yml down` then `docker volume rm agentic-ai-trading-system_pg-data` (**deletes the journal**) |
| `trader migrate` prints `FAILED grants` | `DB_ROLE_PREFIX` does not match the roles that exist | Make `DB_ROLE_PREFIX` in `docker/.env` match the roles (`srb_` for `srb_core_rw` …) |

### Owner messages

| Symptom | Cause | Fix |
| --- | --- | --- |
| Twilio: `21654 ContentSid Required` even inside the 24-hour window | Twilio **trial** WhatsApp numbers ("join twilio-trial") send templates only, and the console's sample `HX…` template is a fixed appointment reminder that ignores our text | Use Telegram (default, ADR-014), or upgrade Twilio and create a template with one `{{1}}` variable |
| Twilio replies with "Standard auto-reply…" | Your message reached Twilio's trial number (inbound works) | Nothing to fix; see the row above for sending |
| `trader telegram-chats`: `no messages yet` | You have not messaged the bot yet | Open the bot, press **Start**, send `STATUS`, rerun |
| Telegram bot stops receiving messages | A webhook was set for the bot, which disables `getUpdates` | Never set a webhook; to clear one: `https://api.telegram.org/bot<token>/deleteWebhook` |
| Approvals never arrive in approve mode | Notifier unhealthy or chat id missing; approve mode then blocks entries (by design) | `trader check-connections --service notifier --env-file .env.notifier`; local commands (`trader kill`, `trader status`) still work |

### Market data and broker

| Symptom | Cause | Fix |
| --- | --- | --- |
| A ticker is blocked with `UR-3` | Fewer than 60 trading days of history (SPCX listed 2026-06-12 and passed with 76 days) | Wait until it has 60 days, or remove it from `universe.tickers` |
| Pre-flight RED: `macro calendar has no CPI date on or after today` | `config/macro_calendar_2026_2027.yaml` has no future CPI date (2027 dates still to add) | Add the BLS dates (RUNBOOK §5 maintenance) |
| Start refused: `clock drift … > 2s` | The Windows clock is off | Settings → Time & language → **Sync now** |

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
