#!/usr/bin/env bash
# One script to check, set up and run the whole trading platform from WSL (repo root).
# Everything runs in Docker containers; nothing in this repo runs on the host.
#
#   ./run.sh                 start every component (same as ./run.sh start)
#   ./run.sh start [--no-monitoring] [--no-console] [--alerts]
#   ./run.sh setup           first time: env files + passwords, pick the model (in a container)
#   ./run.sh check           pre-flight only (Docker, GPU, env files, secrets not in git)
#   ./run.sh connections     read-only credential checks (Alpaca, Finnhub, Telegram, DB, Qwen)
#   ./run.sh status | logs [service] | stop | restart
#   ./run.sh model [--apply] which Qwen model fits this GPU (docs/HARDWARE.md)
#   ./run.sh chats           Telegram chat ids that messaged your bot
#   ./run.sh import-trades FILE [--broker NAME]   other-broker trades for wash sales (WS-9)
#   ./run.sh restore-test    restore the latest backup into a scratch database (OPS-1)
#   ./run.sh drill           record the monthly emergency drill (OPS-2)
#   ./run.sh monitoring      start/repair Grafana on http://localhost:3000
#   ./run.sh console         start/repair the Owner Console on http://localhost:8080
#   ./run.sh alerts          only the standalone Telegram price-alert bot (docs/ALERTS.md)
#   ./run.sh replay [days]   offline trial on synthetic days (no broker keys, no GPU)
#   ./run.sh backtest [days] playbook on real minute bars from the journal store (BT-6)
#   ./run.sh test            lint, type check, tests and traceability in the dev container
#   ./run.sh backfill [days] last N days (default 30) of news + analyst ratings, a recap and
#                            watchlist to Telegram, then next-session ideas (PLAN)
#   ./run.sh scan [--telegram] [--date D] [--all]   technical scanner: swing ideas + day
#                            watchlist from stored daily bars (docs/SCANNER_REQUIREMENTS.md)
#   ./run.sh scan-eval       grade every technique walk-forward; writes reports/scanner_eval.json
#
# Secrets stay local: env files and secrets/ are git-ignored and this script refuses to start
# if git tracks any of them. Docs: docs/RUNBOOK.md (operations), docs/ACCOUNT_SETUP.md (keys).
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"
COMPOSE=(docker compose -f docker/docker-compose.yml)
ALL_PROFILES=(--profile monitoring --profile alerts)
ENV_FILES=(.env.core .env.news .env.notifier .env.backup .env.replay .env.console)
SECRET_FILES=(pg_admin pg_core pg_news pg_notifier pg_readonly pg_console grafana_admin console)
WAIT_S=${WAIT_S:-900}     # first start builds images and loads the model
export HOST_UID HOST_GID
HOST_UID=$(id -u); HOST_GID=$(id -g)   # files written by the toolbox belong to you

if [ -t 1 ]; then R=$'\e[31m'; G=$'\e[32m'; Y=$'\e[33m'; B=$'\e[1m'; N=$'\e[0m'
else R=; G=; Y=; B=; N=; fi
ok()   { echo "${G}✔${N} $*"; }
warn() { echo "${Y}!${N} $*"; }
die()  { echo "${R}✖ $*${N}" >&2; exit 1; }
step() { echo; echo "${B}== $* ==${N}"; }

env_value() {   # env_value FILE KEY -> value (empty if unset)
  [ -f "$1" ] || return 0
  grep -E "^$2=" "$1" | tail -n1 | cut -d= -f2- | tr -d '\r' || true
}

config_model() {   # llm.model from config/config.yaml
  [ -f config/config.yaml ] || return 0
  awk '/^[^[:space:]#]/{inllm=($0 ~ /^llm:/)} inllm && /^[[:space:]]+model:/{print $2; exit}' \
    config/config.yaml | tr -d '\r'
}

# --- the toolbox container runs every helper command (no host Python) --------------------
toolbox() {   # toolbox ARGS... -> `trader ARGS...` inside the toolbox image, repo at /work
  "${COMPOSE[@]}" --profile tools run --rm toolbox "$@"
}
toolbox_gpu() {   # same with the NVIDIA GPU visible (select-model); falls back without a GPU
  if ! "${COMPOSE[@]}" --profile tools run --rm toolbox-gpu "$@" 2>/tmp/run-sh-gpu.err; then
    if grep -qiE "gpu|nvidia|device driver" /tmp/run-sh-gpu.err; then
      warn "no GPU passthrough for containers: detecting without the GPU"
      toolbox "$@"
    else
      cat /tmp/run-sh-gpu.err >&2; return 1
    fi
  fi
}
build_toolbox() {
  "${COMPOSE[@]}" --profile tools build -q toolbox >/dev/null || die "toolbox image build failed"
}

# --- checks -------------------------------------------------------------------------------
check_docker() {
  command -v docker >/dev/null || die "docker not found in WSL: Docker Desktop -> Settings -> Resources -> WSL integration -> enable this distro"
  docker info >/dev/null 2>&1 || die "Docker is not running: start Docker Desktop (docs/TROUBLESHOOTING.md §1)"
  docker compose version >/dev/null 2>&1 || die "docker compose v2 is required (update Docker Desktop)"
  ok "Docker $(docker version --format '{{.Server.Version}}' 2>/dev/null) with compose"
}

check_secrets_not_in_git() {
  command -v git >/dev/null && git rev-parse --git-dir >/dev/null 2>&1 || return 0
  local tracked
  tracked=$(git ls-files -- '.env' '.env.*' 'docker/.env' 'secrets/*' 'config/config.yaml' \
              'config/alerts.yaml' | grep -vE '\.example$|^secrets/README\.md$' || true)
  [ -z "$tracked" ] || die "git is tracking secret files (remove with 'git rm --cached'): $tracked"
  local f
  for f in "${ENV_FILES[@]}" .env.alerts .env.live docker/.env config/config.yaml; do
    if [ -f "$f" ] && ! git check-ignore -q "$f"; then die "$f is not git-ignored"; fi
  done
  ok "secrets are git-ignored and not tracked (they never leave this machine)"
}

check_files() {
  local missing=() f
  for f in docker/.env config/config.yaml "${ENV_FILES[@]}"; do [ -f "$f" ] || missing+=("$f"); done
  for f in "${SECRET_FILES[@]}"; do [ -f "secrets/${f}_password.txt" ] || missing+=("secrets/${f}_password.txt"); done
  [ ${#missing[@]} -eq 0 ] || die "missing: ${missing[*]} -> run './run.sh setup' (creates only what is missing)"
  local empty=() pair
  for pair in .env.core:ALPACA_PAPER_KEY .env.core:ALPACA_PAPER_SECRET .env.news:FINNHUB_TOKEN \
              .env.news:SEC_USER_AGENT .env.notifier:TELEGRAM_BOT_TOKEN .env.notifier:TELEGRAM_CHAT_ID; do
    local v; v=$(env_value "${pair%%:*}" "${pair#*:}")
    if [ -z "$v" ] || [[ "$v" == *"<"* ]]; then empty+=("$pair"); fi
  done
  if [ "$(env_value .env.notifier NOTIFIER_CHANNEL)" = "whatsapp" ]; then
    empty=("${empty[@]/.env.notifier:TELEGRAM_*/}")
  fi
  for f in .env.core .env.news .env.notifier .env.backup .env.console; do
    if [[ "$(env_value "$f" DATABASE_URL)" == *"<"* ]]; then empty+=("$f:DATABASE_URL"); fi
  done
  local real=(); for f in "${empty[@]}"; do [ -n "$f" ] && real+=("$f"); done
  [ ${#real[@]} -eq 0 ] || die "fill in: ${real[*]} (docs/ACCOUNT_SETUP.md)"
  if grep -lq $'\r' docker/.env config/config.yaml "${ENV_FILES[@]}" 2>/dev/null; then
    warn "Windows line endings (CRLF) in: $(grep -l $'\r' docker/.env config/config.yaml "${ENV_FILES[@]}" | tr '\n' ' ')"
    warn "  fix: sed -i 's/\\r\$//' <file>   (values would otherwise end in a hidden \\r)"
  fi
  ok "env files, config and passwords present"
}

check_model_match() {
  local env_m cfg_m
  env_m=$(env_value docker/.env OLLAMA_MODEL); cfg_m=$(config_model)
  [ -n "$env_m" ] || die "OLLAMA_MODEL is not set in docker/.env (./run.sh model --apply)"
  [ "$env_m" = "$cfg_m" ] || die "model mismatch: docker/.env OLLAMA_MODEL=$env_m but config.yaml llm.model=$cfg_m -> './run.sh model --apply'"
  ok "model $env_m (docker/.env and config.yaml agree)"
}

preflight() {
  step "Pre-flight"
  check_docker
  check_secrets_not_in_git
  check_files
  check_model_match
  local mode; mode=$(awk '/^mode:/{m=1} m && /environment:/{print $2; exit}' config/config.yaml | tr -d '\r')
  if [ "$mode" = "live" ]; then warn "config mode is LIVE: real money (RUNBOOK §6)"
  else ok "mode: ${mode:-paper}"; fi
}

# --- actions ------------------------------------------------------------------------------
ensure_model() {
  local model; model=$(env_value docker/.env OLLAMA_MODEL)
  if "${COMPOSE[@]}" exec -T ollama ollama list 2>/dev/null | awk 'NR>1{print $1}' | grep -qx "$model"; then
    ok "model $model is in the ollama volume"; return
  fi
  warn "model $model is not downloaded yet (~5 GB for qwen3:8b)"
  warn "  if Docker Desktop crashes during the download, use tools/fetch_ollama_model.py +"
  warn "  tools/import_ollama_model.ps1 on Windows instead (docs/ACCOUNT_SETUP.md §0)"
  if [ -t 0 ]; then
    read -r -p "Download it now with the model-pull service? [y/N] " a
    [[ "$a" =~ ^[Yy] ]] || die "start stopped: model missing"
  else
    die "model missing (run interactively to download, or import it first)"
  fi
  OLLAMA_MODEL="$model" "${COMPOSE[@]}" --profile setup run --rm model-pull
  "${COMPOSE[@]}" restart ollama >/dev/null
}

check_gpu() {
  local gpu
  gpu=$("${COMPOSE[@]}" exec -T ollama nvidia-smi --query-gpu=name,memory.total,memory.used \
          --format=csv,noheader 2>/dev/null || true)
  if [ -n "$gpu" ]; then
    while IFS= read -r line; do echo "  GPU: $line"; done <<< "$gpu"
    ok "GPU visible inside the ollama container"
  else
    warn "no NVIDIA GPU inside ollama: Qwen would run on the CPU (slow). Check the Windows NVIDIA driver and"
    warn "  docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi   (docs/TROUBLESHOOTING.md §1.1)"
  fi
}

db_upgrade() {   # idempotent roles + outbox guard for REQUIREMENTS 1.3 (console role)
  "${COMPOSE[@]}" exec -T postgres sh /init/upgrade.sh \
    || die "database upgrade failed (docker/postgres/upgrade.sql); see ./run.sh logs postgres"
  ok "database roles and outbox guard up to date"
}

alerts_wanted() {   # the standalone bot needs its own Alpaca account (one WebSocket per account)
  [ -f .env.alerts ] || return 1
  local ak ck lk
  ak=$(env_value .env.alerts ALPACA_KEY); ck=$(env_value .env.core ALPACA_PAPER_KEY)
  lk=$(env_value .env.live ALPACA_LIVE_KEY)
  [ -n "$ak" ] && [ "$ak" != "$ck" ] && [ "$ak" != "$lk" ]
}

cmd_start() {
  local profiles=(--profile monitoring) console=1 alerts=auto
  for arg in "$@"; do
    case "$arg" in
      --monitoring) ;;                      # default now; kept for old habits
      --no-monitoring) profiles=() ;;
      --no-console) console=0 ;;
      --alerts) alerts=yes ;;
      --no-alerts) alerts=no ;;
      *) die "unknown option $arg (./run.sh help)" ;;
    esac
  done
  preflight
  step "Database and model server"
  "${COMPOSE[@]}" up -d --build --wait --wait-timeout "$WAIT_S" postgres ollama
  db_upgrade
  check_gpu
  ensure_model
  step "Journal tables and grants (idempotent)"
  "${COMPOSE[@]}" run --rm trader-core trader migrate
  if [ "$alerts" = yes ] || { [ "$alerts" = auto ] && alerts_wanted; }; then
    alerts_wanted || die "--alerts needs .env.alerts with an Alpaca key different from trader-core's (one WebSocket per account; docs/ALERTS.md)"
    profiles+=(--profile alerts)
  elif [ -f .env.alerts ] && [ "$alerts" = auto ]; then
    warn "standalone alerts bot not started: it shares trader-core's Alpaca key; trader-core sends the alerts itself (AL-1)"
  fi
  local skip=()
  [ "$console" = 0 ] && skip=(--scale console=0)
  step "All services"
  "${COMPOSE[@]}" "${profiles[@]}" up -d --build --wait --wait-timeout "$WAIT_S" "${skip[@]}" \
    || { "${COMPOSE[@]}" "${ALL_PROFILES[@]}" ps; die "a service did not become healthy: ./run.sh logs <service>"; }
  "${COMPOSE[@]}" "${profiles[@]}" ps
  step "Model check (must say 100% on GPU)"
  "${COMPOSE[@]}" exec -T trader-core trader check-connections --service llm \
    || warn "LLM check failed: docs/TROUBLESHOOTING.md (model, GPU memory)"
  step "Running"
  echo "  Telegram: send STATUS to your bot · daily routine: docs/RUNBOOK.md §2"
  [ "$console" = 1 ] && echo "  console:    http://localhost:8080 (password in secrets/console_password.txt)"
  [ ${#profiles[@]} -gt 0 ] && echo "  dashboards: http://localhost:3000 (admin / secrets/grafana_admin_password.txt)"
  echo "  logs: ./run.sh logs trader-core · stop: ./run.sh stop"
  return 0
}

cmd_setup() {
  check_docker
  build_toolbox
  step "Create missing local files (never overwrites; all git-ignored)"
  if [ -f docker/.env ]; then toolbox init-env
  else
    read -r -p "Database role prefix (e.g. your initials, blank for none): " prefix
    toolbox init-env ${prefix:+--prefix "$prefix"}
  fi
  step "Model for this machine's GPU"
  toolbox_gpu select-model --apply || warn "model selection failed: set OLLAMA_MODEL and llm.model by hand"
  echo
  echo "Next: fill in the keys listed above (docs/ACCOUNT_SETUP.md), then ./run.sh connections and ./run.sh"
}

cmd_connections() {
  check_docker; build_toolbox
  "${COMPOSE[@]}" up -d --wait postgres >/dev/null
  toolbox check-connections --service core --service news --service notifier --service db \
    --env-file .env.core --env-file .env.news --env-file .env.notifier "$@"
}

cmd_ui() {   # cmd_ui SERVICE... : start one UI service and print where it is
  check_docker
  if ! "${COMPOSE[@]}" --profile monitoring up -d --build --wait --wait-timeout 300 "$@"; then
    "${COMPOSE[@]}" --profile monitoring ps "$@"
    "${COMPOSE[@]}" --profile monitoring logs --tail 40 "$@" || true
    die "$* did not become healthy (log above; docs/TROUBLESHOOTING.md)"
  fi
}

stale_services() {   # running services whose container is older than the image just built
  local svc cid img
  for svc in "$@"; do
    cid=$("${COMPOSE[@]}" ps -q --status running "$svc" 2>/dev/null) || continue
    [ -n "$cid" ] || continue
    img=$(docker inspect -f '{{.Config.Image}}' "$cid") || continue
    [ "$(docker inspect -f '{{.Image}}' "$cid")" = "$(docker image inspect -f '{{.Id}}' "$img" 2>/dev/null)" ] \
      || echo "$svc"
  done
}

in_session() {   # true on weekdays 9:25-16:05 ET (do not restart trader-core then)
  local now; now=$(TZ=America/New_York date +%u%H%M)
  [ "${now:0:1}" -le 5 ] && [ "${now:1}" -ge 925 ] && [ "${now:1}" -le 1605 ]
}

cmd_backfill() {   # cmd_backfill [days]: data for the past week/month, then ideas (UI-33/34)
  local days="${1:-30}"
  [[ "$days" =~ ^[0-9]+$ ]] && [ "$days" -ge 1 ] && [ "$days" -le 365 ] \
    || die "usage: ./run.sh backfill [days 1-365]"
  local sessions=$(( days * 5 / 7 )); [ "$sessions" -lt 1 ] && sessions=1
  check_docker
  step "Images for the current code"
  "${COMPOSE[@]}" build -q trader-core news-sentiment console \
    || die "image build failed (docker/Dockerfile); see the output above"
  "${COMPOSE[@]}" up -d --wait postgres >/dev/null
  db_upgrade
  step "Journal tables and grants (idempotent)"
  "${COMPOSE[@]}" run --rm -T --no-deps trader-core trader migrate \
    || die "trader migrate failed: ./run.sh logs postgres"
  # one-off containers from the fresh images, so a running older container cannot answer
  step "News, filings and analyst ratings: last $days days (news-sentiment)"
  "${COMPOSE[@]}" run --rm -T --no-deps news-sentiment trader backfill-news --days "$days" \
    || warn "news backfill failed (FINNHUB_TOKEN in .env.news? network access to the feeds?)"
  step "Recap of the last $sessions sessions and the next-session watchlist (Telegram)"
  "${COMPOSE[@]}" run --rm -T --no-deps trader-core trader recap --days "$sessions" \
    || die "recap failed (Alpaca keys in .env.core? ./run.sh connections)"
  local stale; stale=$(stale_services trader-core news-sentiment console | xargs)
  if [ -n "$stale" ]; then
    if in_session && [[ " $stale " == *" trader-core "* ]]; then
      warn "running on older code: $stale. Market is open, so not restarting trader-core now; run ./run.sh after 4:05 PM ET"
    else
      step "Restart on the new code: $stale"
      # shellcheck disable=SC2086 # word splitting intended: one service per word
      "${COMPOSE[@]}" up -d --no-deps --wait --wait-timeout "$WAIT_S" $stale \
        || die "restart failed: ./run.sh logs <service>"
    fi
  fi
  step "Next-session ideas"
  if "${COMPOSE[@]}" ps --status running --services 2>/dev/null | grep -qx trader-core; then
    "${COMPOSE[@]}" exec -T trader-core trader plan
    ok "ideas and options ideas arrive on Telegram in a minute or two; console: http://localhost:8080/ideas"
  else
    warn "trader-core is not running: ./run.sh, then send PLAN on Telegram for the ideas"
  fi
}

cmd="${1:-start}"; [ $# -gt 0 ] && shift
case "$cmd" in
  start)   cmd_start "$@" ;;
  setup)   cmd_setup ;;
  check)   preflight; ok "pre-flight passed" ;;
  connections) cmd_connections "$@" ;;
  stop)    "${COMPOSE[@]}" "${ALL_PROFILES[@]}" stop ;;
  restart) "${COMPOSE[@]}" "${ALL_PROFILES[@]}" stop; cmd_start "$@" ;;
  status)  "${COMPOSE[@]}" "${ALL_PROFILES[@]}" ps
           running=$("${COMPOSE[@]}" "${ALL_PROFILES[@]}" ps --status running --services 2>/dev/null || true)
           grep -qx grafana-proxy <<< "$running" || warn "Grafana is not running (http://localhost:3000): ./run.sh monitoring"
           grep -qx console <<< "$running" || warn "Owner Console is not running (http://localhost:8080): ./run.sh console"
           "${COMPOSE[@]}" exec -T trader-core trader status 2>/dev/null | head -40 || true ;;
  logs)    "${COMPOSE[@]}" "${ALL_PROFILES[@]}" logs -f --tail 200 "$@" ;;
  model)   check_docker; build_toolbox; toolbox_gpu select-model "$@" ;;
  chats)   check_docker; build_toolbox; toolbox telegram-chats --env-file .env.notifier ;;
  import-trades) check_docker; build_toolbox
           [ $# -ge 1 ] || die "usage: ./run.sh import-trades FILE [--broker robinhood|webull|fidelity|schwab|generic]"
           "${COMPOSE[@]}" up -d --wait postgres >/dev/null
           toolbox import-trades --env-file .env.core "$@" ;;
  restore-test) check_docker; build_toolbox
           "${COMPOSE[@]}" up -d --wait postgres >/dev/null
           report=$("${COMPOSE[@]}" --profile tools run --rm restore-test) \
             || die "restore failed: $report"
           printf '%s\n' "$report" | "${COMPOSE[@]}" --profile tools run --rm -T toolbox \
             restore-test --env-file .env.core --report - ;;
  drill)   check_docker; build_toolbox
           "${COMPOSE[@]}" up -d --wait postgres >/dev/null
           toolbox drill --env-file .env.core "$@" ;;
  monitoring) [ -f secrets/grafana_admin_password.txt ] || die "secrets/grafana_admin_password.txt missing: ./run.sh setup"
           cmd_ui grafana grafana-proxy
           ok "dashboards: http://localhost:3000  user admin, password in secrets/grafana_admin_password.txt" ;;
  console) [ -f secrets/console_password.txt ] || die "secrets/console_password.txt missing: ./run.sh setup"
           cmd_ui console
           ok "Owner Console: http://localhost:8080  (password in secrets/console_password.txt)" ;;
  alerts)  check_docker; check_secrets_not_in_git
           [ -f .env.alerts ] || die ".env.alerts missing: cp .env.alerts.example .env.alerts and fill it in (docs/ALERTS.md)"
           "${COMPOSE[@]}" --profile alerts up -d --build --wait --wait-timeout "$WAIT_S" alerts
           ok "alerts bot running: send /help to your Telegram bot · logs: ./run.sh logs alerts" ;;
  replay)  check_docker
           [ -f .env.replay ] || cp .env.replay.example .env.replay
           "${COMPOSE[@]}" --profile replay run --rm replay replay --synthetic "${1:-10}" --start 2026-10-05 ;;
  backtest) check_docker
           [ -f .env.replay ] || cp .env.replay.example .env.replay
           "${COMPOSE[@]}" --profile replay run --rm replay backtest --history --days "${1:-500}" ;;
  backfill) cmd_backfill "$@" ;;
  scan|scan-eval) check_docker   # trader-core has the bar store; --telegram goes via the notifier
           "${COMPOSE[@]}" run --rm -T --no-deps trader-core trader "$cmd" "$@" ;;
  test)    check_docker   # developers too: nothing runs from a host venv
           "${COMPOSE[@]}" --profile dev build -q dev >/dev/null || die "dev image build failed"
           "${COMPOSE[@]}" --profile dev run --rm -T --user "$HOST_UID:$HOST_GID" \
             -e HOME=/tmp -e PYTHONPATH=/workspace/src -w /workspace dev sh -c \
             'ruff check --no-cache src tests && mypy --cache-dir /tmp/mypy &&
              pytest -q -p no:cacheprovider "$@" &&
              python tools/check_traceability.py docs/TRACEABILITY.md tests' sh "$@" ;;
  help|-h|--help) sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//' ;;
  *) die "unknown command '$cmd' (./run.sh help)" ;;
esac
