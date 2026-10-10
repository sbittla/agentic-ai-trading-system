#!/usr/bin/env bash
# One script to check, set up and run the trading platform from WSL (repo root).
#
#   ./run.sh                 start everything (same as ./run.sh start)
#   ./run.sh start [--monitoring] [--alerts]   + Grafana dashboards / Telegram price-alert bot
#   ./run.sh setup           first time: create env files + passwords, pick the model
#   ./run.sh check           pre-flight only (Docker, GPU, env files, secrets not in git)
#   ./run.sh status | logs [service] | stop | restart
#   ./run.sh model           show which Qwen model fits this GPU (docs/HARDWARE.md)
#   ./run.sh monitoring      start/repair Grafana dashboards on http://localhost:3000
#   ./run.sh alerts          only the lightweight Telegram price-alert bot (docs/ALERTS.md)
#   ./run.sh replay [days]   offline trial on synthetic days (no broker keys, no GPU)
#
# Secrets stay local: env files and secrets/ are git-ignored and this script refuses to start
# if git tracks any of them. Docs: docs/RUNBOOK.md (operations), docs/ACCOUNT_SETUP.md (keys).
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"
COMPOSE=(docker compose -f docker/docker-compose.yml)
ENV_FILES=(.env.core .env.news .env.notifier .env.backup .env.replay)
SECRET_FILES=(pg_admin pg_core pg_news pg_notifier pg_readonly grafana_admin)
WAIT_S=${WAIT_S:-900}     # first start builds images and loads the model

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

# --- host Python (only for `setup` and `model`; the services run in Docker) ---------------
host_py() {
  if [ -x .venv/bin/python ]; then echo .venv/bin/python; return; fi       # Linux venv
  if [ -x .venv-wsl/bin/python ]; then echo .venv-wsl/bin/python; return; fi
  # The repo's .venv was made on Windows (.venv\Scripts): WSL needs its own Linux venv.
  echo "creating .venv-wsl (Linux venv for the host-side helper commands)..." >&2
  if command -v uv >/dev/null; then
    uv venv -q -p 3.12 .venv-wsl >&2
    VIRTUAL_ENV=.venv-wsl uv pip install -q --no-deps -e . >&2
    VIRTUAL_ENV=.venv-wsl uv pip install -q pydantic pyyaml structlog >&2
  elif command -v python3.12 >/dev/null; then
    python3.12 -m venv .venv-wsl >&2
    .venv-wsl/bin/pip install -q --no-deps -e . >&2
    .venv-wsl/bin/pip install -q pydantic pyyaml structlog >&2
  elif [ -x .venv/Scripts/python.exe ]; then
    echo .venv/Scripts/python.exe; return                                  # Windows venv via interop
  else
    die "need Python 3.12 in WSL: 'sudo apt install python3.12-venv' or install uv (https://docs.astral.sh/uv/)"
  fi
  echo .venv-wsl/bin/python
}
trader() { "$(host_py)" -m trader.cli "$@"; }

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
  [ ${#missing[@]} -eq 0 ] || die "missing: ${missing[*]} -> run './run.sh setup' (docs/ACCOUNT_SETUP.md)"
  local empty=() pair
  for pair in .env.core:ALPACA_PAPER_KEY .env.core:ALPACA_PAPER_SECRET .env.news:FINNHUB_TOKEN \
              .env.news:SEC_USER_AGENT .env.notifier:TELEGRAM_BOT_TOKEN .env.notifier:TELEGRAM_CHAT_ID; do
    local v; v=$(env_value "${pair%%:*}" "${pair#*:}")
    if [ -z "$v" ] || [[ "$v" == *"<"* ]]; then empty+=("$pair"); fi
  done
  if [ "$(env_value .env.notifier NOTIFIER_CHANNEL)" = "whatsapp" ]; then
    empty=("${empty[@]/.env.notifier:TELEGRAM_*/}")
  fi
  for f in .env.core .env.news .env.notifier .env.backup; do
    if [[ "$(env_value "$f" DATABASE_URL)" == *"<"* ]]; then empty+=("$f:DATABASE_URL"); fi
  done
  local real=(); for f in "${empty[@]}"; do [ -n "$f" ] && real+=("$f"); done
  [ ${#real[@]} -eq 0 ] || die "fill in: ${real[*]} (docs/ACCOUNT_SETUP.md)"
  if grep -lq $'\r' docker/.env config/config.yaml "${ENV_FILES[@]}" 2>/dev/null; then
    warn "Windows line endings (CRLF) in: $(grep -l $'\r' docker/.env config/config.yaml "${ENV_FILES[@]}" | tr '\n' ' ')"
    warn "  fix: sed -i 's/\\r\$//' <file>   (values would otherwise end in a hidden \\r)"
  fi
  ok "env files, config and database passwords present"
}

check_model_match() {
  local env_m cfg_m
  env_m=$(env_value docker/.env OLLAMA_MODEL); cfg_m=$(config_model)
  [ -n "$env_m" ] || die "OLLAMA_MODEL is not set in docker/.env (./run.sh model)"
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

cmd_start() {
  local profiles=()
  for arg in "$@"; do
    case "$arg" in
      --monitoring) profiles+=(--profile monitoring) ;;
      --alerts) [ -f .env.alerts ] || die ".env.alerts missing (cp .env.alerts.example .env.alerts; docs/ALERTS.md)"
                profiles+=(--profile alerts) ;;
      *) die "unknown option $arg" ;;
    esac
  done
  preflight
  step "Database and model server"
  "${COMPOSE[@]}" up -d --wait --wait-timeout "$WAIT_S" postgres ollama
  check_gpu
  ensure_model
  step "Journal tables (idempotent)"
  "${COMPOSE[@]}" run --rm trader-core trader migrate
  step "All services (agents A1-A12: trader-core + news-sentiment; notifier; backup)"
  "${COMPOSE[@]}" "${profiles[@]}" up -d --build --wait --wait-timeout "$WAIT_S" \
    || { "${COMPOSE[@]}" ps; die "a service did not become healthy: ./run.sh logs <service>"; }
  "${COMPOSE[@]}" "${profiles[@]}" ps
  step "Model check (must say 100% on GPU)"
  "${COMPOSE[@]}" exec -T trader-core trader check-connections --service llm \
    || warn "LLM check failed: docs/TROUBLESHOOTING.md (model, GPU memory)"
  step "Running"
  echo "  Telegram: send STATUS to your bot · daily routine: docs/RUNBOOK.md §2"
  echo "  logs: ./run.sh logs trader-core · stop: ./run.sh stop"
  [[ " ${profiles[*]} " == *" monitoring "* ]] && echo "  dashboards: http://localhost:3000 (admin / secrets/grafana_admin_password.txt)"
  return 0
}

cmd_setup() {
  step "Create missing local files (never overwrites; all git-ignored)"
  if [ -f docker/.env ]; then trader init-env
  else
    read -r -p "Database role prefix (e.g. your initials, blank for none): " prefix
    trader init-env ${prefix:+--prefix "$prefix"}
  fi
  step "Model for this machine's GPU"
  trader select-model --apply || warn "model selection failed: set OLLAMA_MODEL and llm.model by hand"
  echo
  echo "Next: fill in the keys listed above (docs/ACCOUNT_SETUP.md), then ./run.sh"
}

cmd_monitoring() {
  check_docker
  [ -f secrets/grafana_admin_password.txt ] || die "secrets/grafana_admin_password.txt missing: ./run.sh setup"
  step "Grafana dashboards (docs/MONITORING.md)"
  if ! "${COMPOSE[@]}" --profile monitoring up -d --wait --wait-timeout 300 grafana grafana-proxy; then
    "${COMPOSE[@]}" --profile monitoring ps grafana grafana-proxy
    echo "--- last grafana log lines:"
    "${COMPOSE[@]}" --profile monitoring logs --tail 40 grafana || true
    die "Grafana did not become healthy (log above; docs/TROUBLESHOOTING.md)"
  fi
  ok "dashboards: http://localhost:3000  user admin, password in secrets/grafana_admin_password.txt"
  echo "  (Windows browser works too: Docker Desktop forwards localhost from WSL)"
}

cmd_alerts() {
  check_docker; check_secrets_not_in_git
  [ -f .env.alerts ] || die ".env.alerts missing: cp .env.alerts.example .env.alerts and fill it in (docs/ALERTS.md)"
  "${COMPOSE[@]}" --profile alerts up -d --build --wait --wait-timeout "$WAIT_S" alerts
  ok "alerts bot running: send /help to your Telegram bot · logs: ./run.sh logs alerts"
}

cmd="${1:-start}"; [ $# -gt 0 ] && shift
case "$cmd" in
  start)   cmd_start "$@" ;;
  setup)   cmd_setup ;;
  check)   preflight; ok "pre-flight passed" ;;
  stop)    "${COMPOSE[@]}" --profile monitoring --profile alerts stop ;;
  restart) "${COMPOSE[@]}" --profile monitoring --profile alerts stop; cmd_start "$@" ;;
  status)  "${COMPOSE[@]}" --profile monitoring --profile alerts ps
           if ! "${COMPOSE[@]}" --profile monitoring ps --status running --services 2>/dev/null \
                | grep -qx grafana-proxy; then
             warn "Grafana is not running (http://localhost:3000): ./run.sh monitoring"
           fi
           "${COMPOSE[@]}" exec -T trader-core trader status 2>/dev/null | head -40 || true ;;
  logs)    "${COMPOSE[@]}" --profile monitoring --profile alerts logs -f --tail 200 "$@" ;;
  model)   trader select-model "$@" ;;
  monitoring) cmd_monitoring ;;
  alerts)  cmd_alerts ;;
  replay)  check_docker
           [ -f .env.replay ] || cp .env.replay.example .env.replay
           "${COMPOSE[@]}" --profile replay run --rm replay replay --synthetic "${1:-10}" --start 2026-10-05 ;;
  help|-h|--help) sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//' ;;
  *) die "unknown command '$cmd' (./run.sh help)" ;;
esac
