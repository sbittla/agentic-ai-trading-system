# One script to check, set up and run the whole trading platform from Windows (repo root).
# The Windows twin of run.sh: run.cmd calls this file, and the two take the same commands.
# Everything runs in Docker containers; nothing in this repo runs on the host.
#
#   run.cmd                 start every component (same as run.cmd start)
#   run.cmd start [--no-monitoring] [--no-console] [--alerts]
#   run.cmd setup           first time: env files + passwords, pick the model (in a container)
#   run.cmd check           pre-flight only (Docker, GPU, env files, secrets not in git)
#   run.cmd connections     read-only credential checks (Alpaca, Finnhub, Telegram, DB, Qwen)
#   run.cmd status | logs [service] | stop | restart
#   run.cmd model [--apply] which Qwen model fits this GPU (docs/HARDWARE.md)
#   run.cmd chats           Telegram chat ids that messaged your bot
#   run.cmd import-trades FILE [--broker NAME]   other-broker trades for wash sales (WS-9)
#   run.cmd restore-test    restore the latest backup into a scratch database (OPS-1)
#   run.cmd drill           record the monthly emergency drill (OPS-2)
#   run.cmd monitoring      start/repair Grafana on http://localhost:3000
#   run.cmd console         start/repair the Owner Console on http://localhost:8080
#   run.cmd alerts          only the standalone Telegram price-alert bot (docs/ALERTS.md)
#   run.cmd replay [days]   offline trial on synthetic days (no broker keys, no GPU)
#   run.cmd backtest [days] playbook on real minute bars from the journal store (BT-6)
#   run.cmd test            lint, type check, tests and traceability in the dev container
#   run.cmd backfill [days] last N days (default 30) of news + analyst ratings, a recap and
#                           watchlist to Telegram, then next-session ideas (PLAN)
#   run.cmd scan [--telegram] [--date D] [--all]   technical scanner: swing ideas + day
#                           watchlist from stored daily bars (docs/SCANNER_REQUIREMENTS.md)
#   run.cmd scan-eval       grade every technique walk-forward; writes reports/scanner_eval.json
#   run.cmd catalyst [TICKER] [--telegram]   news catalysts: strength, 1d-1m outlook, view
#   run.cmd catalyst-eval   grade past outlooks walk-forward; writes reports/catalyst_eval.json
#   run.cmd backfill-catalysts [days]   once: a year of news + bars, then catalyst-eval
#   run.cmd macro [--telegram]   macro regime, releases, Fed/economy headlines (macro-backfill once)
#   run.cmd forecast [TICKER] | forecast-eval   volatility, gaps, macro days, sector ranks
#   run.cmd momentum [--telegram] [--equity N] | momentum-eval | momentum-history   momentum
#                            leaders, buy-stops, exits, rotation (docs/MOMENTUM_REQUIREMENTS.md)
#   run.cmd events [TICKER] [--telegram]   product launches, developer days: past price
#                            pattern and dated ideas (docs/EVENT_REQUIREMENTS.md)
#
# Secrets stay local: env files and secrets/ are git-ignored and this script refuses to start
# if git tracks any of them. Docs: docs/RUNBOOK.md (operations), docs/ACCOUNT_SETUP.md (keys).
# Works in Windows PowerShell 5.1 and PowerShell 7; keep this file ASCII.

$ErrorActionPreference = 'Continue'   # native commands are checked through $LASTEXITCODE
Set-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)
$Compose = @('compose', '-f', 'docker/docker-compose.yml')
$AllProfiles = @('--profile', 'monitoring', '--profile', 'alerts')
$EnvFiles = @('.env.core', '.env.news', '.env.notifier', '.env.backup', '.env.replay', '.env.console')
$SecretFiles = @('pg_admin', 'pg_core', 'pg_news', 'pg_notifier', 'pg_readonly', 'pg_console', 'grafana_admin', 'console')
$WaitS = if ($env:WAIT_S) { $env:WAIT_S } else { '900' }   # first start builds images and loads the model
# Docker Desktop maps bind-mounted files to you on Windows; the ids only matter inside the container
$HostUser = '{0}:{1}' -f $(if ($env:HOST_UID) { $env:HOST_UID } else { '1000' }), $(if ($env:HOST_GID) { $env:HOST_GID } else { '1000' })

try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }
function ok($m)   { Write-Host ([char]0x2714) -ForegroundColor Green -NoNewline; Write-Host " $m" }
function warn($m) { Write-Host '!' -ForegroundColor Yellow -NoNewline; Write-Host " $m" }
function die($m)  { Write-Host "$([char]0x2716) $m" -ForegroundColor Red; exit 1 }
function step($m) { Write-Host ''; Write-Host "== $m ==" -ForegroundColor White }

function Get-EnvValue([string]$File, [string]$Key) {   # value of KEY in FILE (empty if unset)
    if (-not (Test-Path -LiteralPath $File)) { return '' }
    $line = Get-Content -LiteralPath $File | Where-Object { $_ -match ('^' + [regex]::Escape($Key) + '=') } |
        Select-Object -Last 1
    if ($null -eq $line) { return '' }
    return $line.Substring($Key.Length + 1).TrimEnd("`r")
}

function Get-YamlValue([string]$Section, [string]$Key) {   # first KEY under top-level SECTION: in config.yaml
    if (-not (Test-Path -LiteralPath config/config.yaml)) { return '' }
    $inside = $false
    foreach ($l in Get-Content -LiteralPath config/config.yaml) {
        if ($l -match '^[^\s#]') { $inside = $l -match ('^' + $Section + ':') }
        if ($inside -and $l -match ('^\s+' + $Key + ':\s*(\S+)')) { return $Matches[1] }
    }
    return ''
}

function Assert-Native([string]$Message) {   # die with MESSAGE if the last native command failed
    if ($LASTEXITCODE -ne 0) { die $Message }
}

function Format-NativeArg([string]$Text) {   # Windows PowerShell 5.1 drops embedded quotes
    if ($PSVersionTable.PSVersion -lt [version]'7.3' -or $PSNativeCommandArgumentPassing -eq 'Legacy') {
        return $Text.Replace('"', '\"')
    }
    return $Text
}

# --- the toolbox container runs every helper command (no host Python) --------------------
function Invoke-Toolbox([object[]]$A) {   # `trader A...` inside the toolbox image, repo at /work
    & docker @Compose --profile tools run --rm toolbox @A
}
function Invoke-ToolboxGpu([object[]]$A) {   # same with the NVIDIA GPU visible; falls back without one
    $err = Join-Path $env:TEMP 'run-cmd-gpu.err'
    & docker @Compose --profile tools run --rm toolbox-gpu @A 2> $err
    if ($LASTEXITCODE -eq 0) { return }
    $text = (Get-Content -LiteralPath $err -Raw -ErrorAction SilentlyContinue) -as [string]
    if ($text -match '(?i)gpu|nvidia|device driver') {
        warn 'no GPU passthrough for containers: detecting without the GPU'
        Invoke-Toolbox $A
    } else {
        Write-Host $text
    }
}
function Build-Toolbox {
    & docker @Compose --profile tools build -q toolbox | Out-Null
    Assert-Native 'toolbox image build failed'
}

# --- checks -------------------------------------------------------------------------------
function Test-Docker {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        die 'docker not found: install Docker Desktop (WSL2 backend) and open a new terminal'
    }
    & docker info *> $null
    Assert-Native 'Docker is not running: start Docker Desktop (docs/TROUBLESHOOTING.md section 1)'
    & docker compose version *> $null
    Assert-Native 'docker compose v2 is required (update Docker Desktop)'
    $version = & docker version --format '{{.Server.Version}}' 2> $null
    ok "Docker $version with compose"
}

function Test-SecretsNotInGit {
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) { return }
    & git rev-parse --git-dir *> $null
    if ($LASTEXITCODE -ne 0) { return }
    $tracked = @(& git ls-files -- '.env' '.env.*' 'docker/.env' 'secrets/*' 'config/config.yaml' 'config/alerts.yaml' |
        Where-Object { $_ -notmatch '\.example$|^secrets/README\.md$' })
    if ($tracked.Count -gt 0) { die "git is tracking secret files (remove with 'git rm --cached'): $($tracked -join ' ')" }
    foreach ($f in ($EnvFiles + @('.env.alerts', '.env.live', 'docker/.env', 'config/config.yaml'))) {
        if (Test-Path -LiteralPath $f) {
            & git check-ignore -q $f
            if ($LASTEXITCODE -ne 0) { die "$f is not git-ignored" }
        }
    }
    ok 'secrets are git-ignored and not tracked (they never leave this machine)'
}

function Test-Files {
    $missing = @()
    foreach ($f in (@('docker/.env', 'config/config.yaml') + $EnvFiles)) {
        if (-not (Test-Path -LiteralPath $f)) { $missing += $f }
    }
    foreach ($f in $SecretFiles) {
        if (-not (Test-Path -LiteralPath "secrets/${f}_password.txt")) { $missing += "secrets/${f}_password.txt" }
    }
    if ($missing.Count -gt 0) { die "missing: $($missing -join ' ') -> run 'run.cmd setup' (creates only what is missing)" }
    $empty = @()
    foreach ($pair in @('.env.core:ALPACA_PAPER_KEY', '.env.core:ALPACA_PAPER_SECRET', '.env.news:FINNHUB_TOKEN',
                        '.env.news:SEC_USER_AGENT', '.env.notifier:TELEGRAM_BOT_TOKEN', '.env.notifier:TELEGRAM_CHAT_ID')) {
        $file, $key = $pair.Split(':', 2)
        $v = Get-EnvValue $file $key
        if (-not $v -or $v.Contains('<')) { $empty += $pair }
    }
    if ((Get-EnvValue .env.notifier NOTIFIER_CHANNEL) -eq 'whatsapp') {
        $empty = @($empty | Where-Object { $_ -notlike '.env.notifier:TELEGRAM_*' })
    }
    foreach ($f in @('.env.core', '.env.news', '.env.notifier', '.env.backup', '.env.console')) {
        if ((Get-EnvValue $f DATABASE_URL).Contains('<')) { $empty += "${f}:DATABASE_URL" }
    }
    if ($empty.Count -gt 0) { die "fill in: $($empty -join ' ') (docs/ACCOUNT_SETUP.md)" }
    $crlf = @(foreach ($f in (@('docker/.env', 'config/config.yaml') + $EnvFiles)) {
        if ([IO.File]::ReadAllText((Resolve-Path -LiteralPath $f).Path).Contains("`r")) { $f }
    })
    if ($crlf.Count -gt 0) {
        warn "Windows line endings (CRLF) in: $($crlf -join ' ')"
        warn '  fix: save them with LF line endings (VS Code: click CRLF in the status bar, pick LF)'
        warn '  (values would otherwise end in a hidden \r)'
    }
    ok 'env files, config and passwords present'
}

function Test-ModelMatch {
    $envModel = Get-EnvValue docker/.env OLLAMA_MODEL
    $cfgModel = Get-YamlValue llm model
    if (-not $envModel) { die 'OLLAMA_MODEL is not set in docker/.env (run.cmd model --apply)' }
    if ($envModel -ne $cfgModel) {
        die "model mismatch: docker/.env OLLAMA_MODEL=$envModel but config.yaml llm.model=$cfgModel -> 'run.cmd model --apply'"
    }
    ok "model $envModel (docker/.env and config.yaml agree)"
}

function Invoke-Preflight {
    step 'Pre-flight'
    Test-Docker
    Test-SecretsNotInGit
    Test-Files
    Test-ModelMatch
    $mode = Get-YamlValue mode environment
    if ($mode -eq 'live') { warn 'config mode is LIVE: real money (RUNBOOK section 6)' }
    else { ok "mode: $(if ($mode) { $mode } else { 'paper' })" }
}

# --- actions ------------------------------------------------------------------------------
function Confirm-Model {
    $model = Get-EnvValue docker/.env OLLAMA_MODEL
    $list = & docker @Compose exec -T ollama ollama list 2> $null
    $names = @($list | Select-Object -Skip 1 | ForEach-Object { ($_ -split '\s+')[0] })
    if ($LASTEXITCODE -eq 0 -and $names -contains $model) { ok "model $model is in the ollama volume"; return }
    warn "model $model is not downloaded yet (~5 GB for qwen3:8b)"
    warn '  if Docker Desktop crashes during the download, use tools\fetch_ollama_model.py +'
    warn '  tools\import_ollama_model.ps1 instead (docs/ACCOUNT_SETUP.md section 0)'
    if ([Console]::IsInputRedirected) { die 'model missing (run interactively to download, or import it first)' }
    $answer = Read-Host 'Download it now with the model-pull service? [y/N]'
    if ($answer -notmatch '^[Yy]') { die 'start stopped: model missing' }
    $env:OLLAMA_MODEL = $model
    & docker @Compose --profile setup run --rm model-pull
    $pulled = $LASTEXITCODE
    Remove-Item Env:OLLAMA_MODEL
    if ($pulled -ne 0) { die "model download failed: $model" }
    & docker @Compose restart ollama | Out-Null
}

function Test-Gpu {
    $gpu = @(& docker @Compose exec -T ollama nvidia-smi '--query-gpu=name,memory.total,memory.used' '--format=csv,noheader' 2> $null)
    if ($LASTEXITCODE -eq 0 -and $gpu.Count -gt 0 -and $gpu[0]) {
        foreach ($line in $gpu) { Write-Host "  GPU: $line" }
        ok 'GPU visible inside the ollama container'
    } else {
        warn 'no NVIDIA GPU inside ollama: Qwen would run on the CPU (slow). Check the Windows NVIDIA driver and'
        warn '  docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi   (docs/TROUBLESHOOTING.md section 1.1)'
    }
}

function Update-Database {   # idempotent roles + outbox guard for REQUIREMENTS 1.3 (console role)
    & docker @Compose exec -T postgres sh /init/upgrade.sh
    Assert-Native 'database upgrade failed (docker/postgres/upgrade.sql); see run.cmd logs postgres'
    ok 'database roles and outbox guard up to date'
}

function Test-AlertsWanted {   # the standalone bot needs its own Alpaca account (one WebSocket per account)
    if (-not (Test-Path -LiteralPath .env.alerts)) { return $false }
    $ak = Get-EnvValue .env.alerts ALPACA_KEY
    return [bool]($ak -and $ak -ne (Get-EnvValue .env.core ALPACA_PAPER_KEY) -and $ak -ne (Get-EnvValue .env.live ALPACA_LIVE_KEY))
}

function Start-All([object[]]$Options) {
    $profiles = @('--profile', 'monitoring'); $console = $true; $alerts = 'auto'
    foreach ($arg in $Options) {
        switch ($arg) {
            '--monitoring' { }                      # default now; kept for old habits
            '--no-monitoring' { $profiles = @() }
            '--no-console' { $console = $false }
            '--alerts' { $alerts = 'yes' }
            '--no-alerts' { $alerts = 'no' }
            default { die "unknown option $arg (run.cmd help)" }
        }
    }
    Invoke-Preflight
    step 'Database and model server'
    & docker @Compose up -d --build --wait --wait-timeout $WaitS postgres ollama
    Assert-Native 'postgres or ollama did not become healthy: run.cmd logs <service>'
    Update-Database
    Test-Gpu
    Confirm-Model
    step 'Journal tables and grants (idempotent)'
    & docker @Compose run --rm trader-core trader migrate
    Assert-Native 'trader migrate failed: run.cmd logs postgres'
    if ($alerts -eq 'yes' -or ($alerts -eq 'auto' -and (Test-AlertsWanted))) {
        if (-not (Test-AlertsWanted)) {
            die "--alerts needs .env.alerts with an Alpaca key different from trader-core's (one WebSocket per account; docs/ALERTS.md)"
        }
        $profiles += @('--profile', 'alerts')
    } elseif ((Test-Path -LiteralPath .env.alerts) -and $alerts -eq 'auto') {
        warn "standalone alerts bot not started: it shares trader-core's Alpaca key; trader-core sends the alerts itself (AL-1)"
    }
    $skip = @()
    if (-not $console) { $skip = @('--scale', 'console=0') }
    step 'All services'
    & docker @Compose @profiles up -d --build --wait --wait-timeout $WaitS @skip
    if ($LASTEXITCODE -ne 0) {
        & docker @Compose @AllProfiles ps
        die 'a service did not become healthy: run.cmd logs <service>'
    }
    & docker @Compose @profiles ps
    step 'Model check (must say 100% on GPU)'
    & docker @Compose exec -T trader-core trader check-connections --service llm
    if ($LASTEXITCODE -ne 0) { warn 'LLM check failed: docs/TROUBLESHOOTING.md (model, GPU memory)' }
    step 'Running'
    Write-Host '  Telegram: send STATUS to your bot - daily routine: docs/RUNBOOK.md section 2'
    if ($console) { Write-Host '  console:    http://localhost:8080 (password in secrets\console_password.txt)' }
    if ($profiles.Count -gt 0) { Write-Host '  dashboards: http://localhost:3000 (admin / secrets\grafana_admin_password.txt)' }
    Write-Host '  logs: run.cmd logs trader-core - stop: run.cmd stop'
}

function Invoke-Setup {
    Test-Docker
    Build-Toolbox
    step 'Create missing local files (never overwrites; all git-ignored)'
    if (Test-Path -LiteralPath docker/.env) {
        Invoke-Toolbox @('init-env')
    } else {
        $prefix = Read-Host 'Database role prefix (e.g. your initials, blank for none)'
        if ($prefix) { Invoke-Toolbox @('init-env', '--prefix', $prefix) } else { Invoke-Toolbox @('init-env') }
    }
    Assert-Native 'init-env failed (output above)'
    step "Model for this machine's GPU"
    Invoke-ToolboxGpu @('select-model', '--apply')
    if ($LASTEXITCODE -ne 0) { warn 'model selection failed: set OLLAMA_MODEL and llm.model by hand' }
    Write-Host ''
    Write-Host 'Next: fill in the keys listed above (docs/ACCOUNT_SETUP.md), then run.cmd connections and run.cmd'
}

function Invoke-Connections([object[]]$Rest) {
    Test-Docker; Build-Toolbox
    & docker @Compose up -d --wait postgres | Out-Null
    Invoke-Toolbox (@('check-connections', '--service', 'core', '--service', 'news', '--service', 'notifier',
        '--service', 'db', '--env-file', '.env.core', '--env-file', '.env.news', '--env-file', '.env.notifier') + $Rest)
}

function Start-Ui([string[]]$Services) {   # start one UI service and print where it is
    Test-Docker
    & docker @Compose --profile monitoring up -d --build --wait --wait-timeout 300 @Services
    if ($LASTEXITCODE -ne 0) {
        & docker @Compose --profile monitoring ps @Services
        & docker @Compose --profile monitoring logs --tail 40 @Services
        die "$($Services -join ' ') did not become healthy (log above; docs/TROUBLESHOOTING.md)"
    }
}

function Get-StaleServices([string[]]$Services) {   # running services older than the image just built
    foreach ($svc in $Services) {
        $cid = @(& docker @Compose ps -q --status running $svc 2> $null)[0]
        if ($LASTEXITCODE -ne 0 -or -not $cid) { continue }
        $img = & docker inspect -f '{{.Config.Image}}' $cid
        if ($LASTEXITCODE -ne 0) { continue }
        $running = & docker inspect -f '{{.Image}}' $cid
        $latest = & docker image inspect -f '{{.Id}}' $img 2> $null
        if ($running -ne $latest) { $svc }
    }
}

function Test-InSession {   # true on weekdays 9:25-16:05 ET (do not restart trader-core then)
    $et = [TimeZoneInfo]::ConvertTimeFromUtc([DateTime]::UtcNow,
        [TimeZoneInfo]::FindSystemTimeZoneById('Eastern Standard Time'))
    $day = [int]$et.DayOfWeek; $hm = $et.Hour * 100 + $et.Minute
    return ($day -ge 1 -and $day -le 5 -and $hm -ge 925 -and $hm -le 1605)
}

function Invoke-BackfillCatalysts([object[]]$Rest) {   # NC-30: event-study history, then grading
    $days = if ($Rest.Count -gt 0) { [string]$Rest[0] } else { '365' }
    if ($days -notmatch '^\d+$' -or [int]$days -lt 30 -or [int]$days -gt 730) { die 'usage: run.cmd backfill-catalysts [days 30-730]' }
    Test-Docker
    step 'Images for the current code'
    & docker @Compose build -q trader-core news-sentiment console
    Assert-Native 'image build failed (docker/Dockerfile); see the output above'
    & docker @Compose up -d --wait postgres | Out-Null
    Update-Database
    step 'Journal tables and grants (idempotent)'
    & docker @Compose run --rm -T --no-deps trader-core trader migrate
    Assert-Native 'trader migrate failed: run.cmd logs postgres'
    step 'Daily bars for the history tickers (trader-core)'
    & docker @Compose run --rm -T --no-deps trader-core trader fetch-history --days ([int]$days + 420) --daily-only
    if ($LASTEXITCODE -ne 0) { warn 'bar download failed (Alpaca keys in .env.core?); grading uses what is stored' }
    step "News, filings and fundamentals: last $days days (news-sentiment; can take an hour)"
    & docker @Compose run --rm -T --no-deps news-sentiment trader backfill-catalysts --days $days
    if ($LASTEXITCODE -ne 0) { warn 'catalyst news backfill failed (FINNHUB_TOKEN in .env.news?)' }
    step 'Walk-forward grading (trader-core)'
    & docker @Compose run --rm -T --no-deps trader-core trader catalyst-eval --days $days
    Assert-Native 'catalyst-eval failed: see the output above'
    ok 'done: run.cmd catalyst, or the console Catalysts page'
}

function Invoke-Backfill([object[]]$Rest) {   # data for the past week/month, then ideas (UI-33/34)
    $days = if ($Rest.Count -gt 0) { [string]$Rest[0] } else { '30' }
    if ($days -notmatch '^\d+$' -or [int]$days -lt 1 -or [int]$days -gt 365) { die 'usage: run.cmd backfill [days 1-365]' }
    $sessions = [math]::Max(1, [math]::Floor([int]$days * 5 / 7))
    Test-Docker
    step 'Images for the current code'
    & docker @Compose build -q trader-core news-sentiment console
    Assert-Native 'image build failed (docker/Dockerfile); see the output above'
    & docker @Compose up -d --wait postgres | Out-Null
    Update-Database
    step 'Journal tables and grants (idempotent)'
    & docker @Compose run --rm -T --no-deps trader-core trader migrate
    Assert-Native 'trader migrate failed: run.cmd logs postgres'
    # one-off containers from the fresh images, so a running older container cannot answer
    step "News, filings and analyst ratings: last $days days (news-sentiment)"
    & docker @Compose run --rm -T --no-deps news-sentiment trader backfill-news --days $days
    if ($LASTEXITCODE -ne 0) { warn 'news backfill failed (FINNHUB_TOKEN in .env.news? network access to the feeds?)' }
    step "Recap of the last $sessions sessions and the next-session watchlist (Telegram)"
    & docker @Compose run --rm -T --no-deps trader-core trader recap --days $sessions
    Assert-Native 'recap failed (Alpaca keys in .env.core? run.cmd connections)'
    $stale = @(Get-StaleServices @('trader-core', 'news-sentiment', 'console'))
    if ($stale.Count -gt 0) {
        if ((Test-InSession) -and $stale -contains 'trader-core') {
            warn "running on older code: $($stale -join ' '). Market is open, so not restarting trader-core now; run run.cmd after 4:05 PM ET"
        } else {
            step "Restart on the new code: $($stale -join ' ')"
            & docker @Compose up -d --no-deps --wait --wait-timeout $WaitS @stale
            Assert-Native 'restart failed: run.cmd logs <service>'
        }
    }
    step 'Next-session ideas'
    $running = @(& docker @Compose ps --status running --services 2> $null)
    if ($running -contains 'trader-core') {
        & docker @Compose exec -T trader-core trader plan
        ok 'ideas and options ideas arrive on Telegram in a minute or two; console: http://localhost:8080/ideas'
    } else {
        warn 'trader-core is not running: run.cmd, then send PLAN on Telegram for the ideas'
    }
}

function Show-Help {
    foreach ($line in Get-Content -LiteralPath $PSCommandPath) {
        if ($line -notmatch '^#') { break }
        Write-Host ($line -replace '^# ?', '')
    }
}

$cmd = if ($args.Count -gt 0) { [string]$args[0] } else { 'start' }
$rest = @(if ($args.Count -gt 1) { $args[1..($args.Count - 1)] })

switch ($cmd) {
    'start' { Start-All $rest; exit 0 }
    'setup' { Invoke-Setup; exit 0 }
    'check' { Invoke-Preflight; ok 'pre-flight passed'; exit 0 }
    'connections' { Invoke-Connections $rest; exit $LASTEXITCODE }
    'stop' { & docker @Compose @AllProfiles stop; exit $LASTEXITCODE }
    'restart' { & docker @Compose @AllProfiles stop; Start-All $rest; exit 0 }
    'status' {
        & docker @Compose @AllProfiles ps
        $running = @(& docker @Compose @AllProfiles ps --status running --services 2> $null)
        if ($running -notcontains 'grafana-proxy') { warn 'Grafana is not running (http://localhost:3000): run.cmd monitoring' }
        if ($running -notcontains 'console') { warn 'Owner Console is not running (http://localhost:8080): run.cmd console' }
        & docker @Compose exec -T trader-core trader status 2> $null | Select-Object -First 40
        exit 0
    }
    'logs' { & docker @Compose @AllProfiles logs -f --tail 200 @rest; exit $LASTEXITCODE }
    'model' { Test-Docker; Build-Toolbox; Invoke-ToolboxGpu (@('select-model') + $rest); exit $LASTEXITCODE }
    'chats' { Test-Docker; Build-Toolbox; Invoke-Toolbox @('telegram-chats', '--env-file', '.env.notifier'); exit $LASTEXITCODE }
    'import-trades' {
        Test-Docker; Build-Toolbox
        if ($rest.Count -lt 1) { die 'usage: run.cmd import-trades FILE [--broker robinhood|webull|fidelity|schwab|generic]' }
        & docker @Compose up -d --wait postgres | Out-Null
        Invoke-Toolbox (@('import-trades', '--env-file', '.env.core') + $rest)
        exit $LASTEXITCODE
    }
    'restore-test' {
        Test-Docker; Build-Toolbox
        & docker @Compose up -d --wait postgres | Out-Null
        $report = & docker @Compose --profile tools run --rm restore-test
        if ($LASTEXITCODE -ne 0) { die "restore failed: $($report -join "`n")" }
        $report | & docker @Compose --profile tools run --rm -T toolbox restore-test --env-file .env.core --report -
        exit $LASTEXITCODE
    }
    'drill' {
        Test-Docker; Build-Toolbox
        & docker @Compose up -d --wait postgres | Out-Null
        Invoke-Toolbox (@('drill', '--env-file', '.env.core') + $rest)
        exit $LASTEXITCODE
    }
    'monitoring' {
        if (-not (Test-Path -LiteralPath secrets/grafana_admin_password.txt)) { die 'secrets/grafana_admin_password.txt missing: run.cmd setup' }
        Start-Ui @('grafana', 'grafana-proxy')
        ok 'dashboards: http://localhost:3000  user admin, password in secrets\grafana_admin_password.txt'
        exit 0
    }
    'console' {
        if (-not (Test-Path -LiteralPath secrets/console_password.txt)) { die 'secrets/console_password.txt missing: run.cmd setup' }
        Start-Ui @('console')
        ok 'Owner Console: http://localhost:8080  (password in secrets\console_password.txt)'
        exit 0
    }
    'alerts' {
        Test-Docker; Test-SecretsNotInGit
        if (-not (Test-Path -LiteralPath .env.alerts)) { die '.env.alerts missing: copy .env.alerts.example to .env.alerts and fill it in (docs/ALERTS.md)' }
        & docker @Compose --profile alerts up -d --build --wait --wait-timeout $WaitS alerts
        Assert-Native 'alerts bot did not become healthy: run.cmd logs alerts'
        ok 'alerts bot running: send /help to your Telegram bot - logs: run.cmd logs alerts'
        exit 0
    }
    'replay' {
        Test-Docker
        if (-not (Test-Path -LiteralPath .env.replay)) { Copy-Item .env.replay.example .env.replay }
        $days = if ($rest.Count -gt 0) { $rest[0] } else { '10' }
        & docker @Compose --profile replay run --rm replay replay --synthetic $days --start 2026-10-05
        exit $LASTEXITCODE
    }
    'backtest' {
        Test-Docker
        if (-not (Test-Path -LiteralPath .env.replay)) { Copy-Item .env.replay.example .env.replay }
        $days = if ($rest.Count -gt 0) { $rest[0] } else { '500' }
        & docker @Compose --profile replay run --rm replay backtest --history --days $days
        exit $LASTEXITCODE
    }
    'backfill' { Invoke-Backfill $rest; exit 0 }
    'backfill-catalysts' { Invoke-BackfillCatalysts $rest; exit 0 }
    'macro-backfill' {
        Test-Docker   # MA-2: full FRED history + headlines (news container)
        & docker @Compose run --rm -T --no-deps news-sentiment trader macro-collect --history
        Assert-Native 'macro collection failed: see the output above'
        & docker @Compose run --rm -T --no-deps trader-core trader macro
        exit $LASTEXITCODE
    }
    'forecast-eval' {
        Test-Docker
        & docker @Compose run --rm -T --no-deps trader-core trader forecast --eval @rest
        exit $LASTEXITCODE
    }
    { $_ -in 'scan', 'scan-eval', 'catalyst', 'catalyst-eval', 'macro', 'forecast',
              'momentum', 'momentum-eval', 'momentum-history', 'events' } {
        Test-Docker   # trader-core has the bar store; --telegram goes via the notifier
        & docker @Compose run --rm -T --no-deps trader-core trader $cmd @rest
        exit $LASTEXITCODE
    }
    'test' {
        Test-Docker   # developers too: nothing runs from a host venv
        & docker @Compose --profile dev build -q dev | Out-Null
        Assert-Native 'dev image build failed'
        $script = Format-NativeArg ('ruff check --no-cache src tests && mypy --cache-dir /tmp/mypy && ' +
            'pytest -q -p no:cacheprovider "$@" && ' +
            'python tools/check_traceability.py docs/TRACEABILITY.md tests')
        & docker @Compose --profile dev run --rm -T --user $HostUser -e HOME=/tmp -e PYTHONPATH=/workspace/src `
            -w /workspace dev sh -c $script sh @rest
        exit $LASTEXITCODE
    }
    { $_ -in 'help', '-h', '--help' } { Show-Help; exit 0 }
    default { die "unknown command '$cmd' (run.cmd help)" }
}
