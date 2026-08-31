# DeskClaw IDE-mode one-click startup (no Docker required).
# Target audience: people receiving this codebase for the first time.
# Boots: portable PostgreSQL -> llm-proxy -> backend -> portal, then opens
# the browser with seeded admin credentials.
#
# Usage:  double-click start-ide.bat   (or)
#         powershell -ExecutionPolicy Bypass -File scripts\start-ide.ps1
#
# First run needs internet (downloads portable PostgreSQL ~300MB) and takes
# a few minutes; subsequent runs start in well under a minute.
#
# Requirements: uv (https://docs.astral.sh/uv/), Node.js >= 18.
# Optional env overrides: BACKEND_PORT / PG_PORT / DEMO_PASSWORD / TOOLS_DIR

$ErrorActionPreference = "Stop"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

$RepoRoot = Split-Path -Parent $PSScriptRoot
$ToolsDir = if ($env:TOOLS_DIR) { $env:TOOLS_DIR } else { Join-Path $RepoRoot ".tools" }
$PGBin    = Join-Path $ToolsDir "pgsql\bin"
$PGData   = Join-Path $ToolsDir "pgdata"
$PGZip    = Join-Path $ToolsDir "pg16-binaries.zip"
$BackendDir  = Join-Path $RepoRoot "nodeskclaw-backend"
$LlmProxyDir = Join-Path $RepoRoot "nodeskclaw-llm-proxy"
$PortalDir   = Join-Path $RepoRoot "nodeskclaw-portal"

$PgPort      = if ($env:PG_PORT)      { [int]$env:PG_PORT }      else { 15432 }
$DemoPassword = if ($env:DEMO_PASSWORD) { $env:DEMO_PASSWORD }    else { "DeskClaw@2026" }
$PortalPort   = 4517
$LlmProxyPort = 4511

function Log($msg)  { Write-Host "[ide] $msg" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "[ide] $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "[ide] $msg" -ForegroundColor Yellow }
function Fail($msg) { Write-Host "[ide] $msg" -ForegroundColor Red; exit 1 }

# 本机探测一律绕过系统代理（Invoke-WebRequest 会吃代理导致 localhost 健康检查误报）
$env:NO_PROXY = "localhost,127.0.0.1"
$env:no_proxy = "localhost,127.0.0.1"
function Test-HttpOk([string]$url) {
    try {
        $out = & curl.exe -s --noproxy "*" -o NUL -w "%{http_code}" --max-time 3 $url 2>$null
        return ($out -eq "200")
    } catch { return $false }
}

function Test-PortFree([int]$port) {
    $c = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    return ($null -eq $c)
}

# Pick the first free port from a starting point (backend port is flexible;
# Windows services sometimes squat on 4510 - that is why we start at 24510).
function Find-FreePort([int]$from) {
    for ($p = $from; $p -lt $from + 50; $p++) {
        if (Test-PortFree $p) { return $p }
    }
    Fail "no free port found starting at $from"
}

# ── 1. prerequisites ─────────────────────────────────────────────────────
foreach ($cmd in @("uv", "node", "npm")) {
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) {
        Fail "missing prerequisite: $cmd. Install uv (https://docs.astral.sh/uv/) and Node.js >= 18, then re-run."
    }
}
Ok "prerequisites ok (node=$(node --version))"

if (-not (Test-PortFree $PortalPort))   { Fail "port $PortalPort (portal) is occupied - close the process using it and re-run" }
if (-not (Test-PortFree $LlmProxyPort)) { Fail "port $LlmProxyPort (llm-proxy) is occupied - close the process using it and re-run" }
$BackendPort = if ($env:BACKEND_PORT) { [int]$env:BACKEND_PORT } else { Find-FreePort 24510 }

# ── 2. portable PostgreSQL (no Docker, no Windows service, user-space) ───
# 完整性以 postgres.bki 为准（pg_ctl.exe 在但 share/ 缺失 = 上次解压被中断）
function Test-PgComplete {
    return (Test-Path (Join-Path $PGBin "pg_ctl.exe")) -and
           (Test-Path (Join-Path $ToolsDir "pgsql\share\postgres.bki"))
}

if (-not (Test-PgComplete)) {
    New-Item -ItemType Directory -Force -Path $ToolsDir | Out-Null
    if (Test-Path (Join-Path $ToolsDir "pgsql")) {
        Warn "previous extraction incomplete, cleaning up ..."
        Remove-Item -Recurse -Force (Join-Path $ToolsDir "pgsql")
    }
    if (-not (Test-Path $PGZip)) {
        Warn "downloading portable PostgreSQL 16 (~300MB, first run only - please wait)..."
        $pgUrl = "https://get.enterprisedb.com/postgresql/postgresql-16.6-1-windows-x64-binaries.zip"
        # curl.exe ships with Windows 10 1803+; fall back to Invoke-WebRequest
        if (Get-Command curl.exe -ErrorAction SilentlyContinue) {
            curl.exe -sL --retry 3 -o $PGZip $pgUrl
            if ($LASTEXITCODE -ne 0) { Fail "download failed - check internet connection" }
        } else {
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
            Invoke-WebRequest -Uri $pgUrl -OutFile $PGZip -UseBasicParsing
        }
    }
    Warn "extracting PostgreSQL (a few minutes on first run)..."
    # tar.exe (bsdtar, built into Win10+) unzips several times faster than Expand-Archive
    if (Get-Command tar.exe -ErrorAction SilentlyContinue) {
        & tar.exe -xf $PGZip -C $ToolsDir
        if ($LASTEXITCODE -ne 0) { Fail "tar extraction failed" }
    } else {
        Expand-Archive -Path $PGZip -DestinationPath $ToolsDir -Force
    }
    if (-not (Test-PgComplete)) { Fail "extraction finished but PostgreSQL incomplete - delete $ToolsDir\pg16-binaries.zip and re-run" }
}
if (-not (Test-Path $PGData)) {
    Log "initializing database (initdb)..."
    & (Join-Path $PGBin "initdb.exe") -D $PGData -U nodeskclaw -E UTF8 -A trust --no-locale | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail "initdb failed - delete the $PGData directory and retry" }
}

if (Test-PortFree $PgPort) {
    Log "starting PostgreSQL on 127.0.0.1:$PgPort ..."
    & (Join-Path $PGBin "pg_ctl.exe") -D $PGData -l (Join-Path $ToolsDir "pg.log") `
        -o "-p $PgPort -h 127.0.0.1" start | Out-Null
    Start-Sleep -Seconds 2
    if (Test-PortFree $PgPort) { Fail "PostgreSQL did not start - see $ToolsDir\pg.log" }
} else {
    Log "PostgreSQL already listening on $PgPort, reusing"
}
$dbExists = & (Join-Path $PGBin "psql.exe") -h 127.0.0.1 -p $PgPort -U nodeskclaw -d postgres -tAc `
    "SELECT 1 FROM pg_database WHERE datname='nodeskclaw_rbac_test'"
if (-not $dbExists) {
    & (Join-Path $PGBin "psql.exe") -h 127.0.0.1 -p $PgPort -U nodeskclaw -d postgres -c `
        "CREATE DATABASE nodeskclaw_rbac_test;" | Out-Null
}
Ok "PostgreSQL ready (127.0.0.1:$PgPort)"

$env:DATABASE_URL = "postgresql+asyncpg://nodeskclaw@127.0.0.1:$PgPort/nodeskclaw_rbac_test"
$env:LLM_PROXY_URL = "http://localhost:$LlmProxyPort"
$env:LLM_PROXY_INTERNAL_URL = "http://localhost:$LlmProxyPort"
$env:API_PROXY_TARGET = "http://localhost:$BackendPort"

# ── 3. dependencies (installed once, skipped afterwards) ─────────────────
if (-not (Test-Path (Join-Path $BackendDir ".venv"))) {
    Log "installing backend dependencies (uv sync, first run only)..."
    Push-Location $BackendDir; uv sync; Pop-Location
}
if (-not (Test-Path (Join-Path $LlmProxyDir ".venv"))) {
    Log "installing llm-proxy dependencies (uv sync, first run only)..."
    Push-Location $LlmProxyDir; uv sync; Pop-Location
}
if (-not (Test-Path (Join-Path $PortalDir "node_modules"))) {
    Log "installing portal dependencies (npm install, first run only)..."
    Push-Location $PortalDir; npm install; Pop-Location
}

# ── 4. launch services (each in its own window; close window = stop service) ──
Warn "starting services: llm-proxy($LlmProxyPort) backend($BackendPort) portal($PortalPort)"
# chcp 65001 + PYTHONIOENCODING keep the log windows readable (UTF-8 Chinese)
Start-Process -FilePath "cmd.exe" -ArgumentList "/k", "chcp 65001 >nul && title deskclaw-llm-proxy && cd /d `"$LlmProxyDir`" && set PYTHONIOENCODING=utf-8&& set DATABASE_URL=$env:DATABASE_URL&& uv run uvicorn app.main:app --host 127.0.0.1 --port $LlmProxyPort"
Start-Sleep -Seconds 1
Start-Process -FilePath "cmd.exe" -ArgumentList "/k", "chcp 65001 >nul && title deskclaw-backend && cd /d `"$BackendDir`" && set PYTHONIOENCODING=utf-8&& set DATABASE_URL=$env:DATABASE_URL&& set LLM_PROXY_URL=$env:LLM_PROXY_URL&& set LLM_PROXY_INTERNAL_URL=$env:LLM_PROXY_INTERNAL_URL&& uv run uvicorn app.main:app --host 127.0.0.1 --port $BackendPort"
Start-Sleep -Seconds 1
Start-Process -FilePath "cmd.exe" -ArgumentList "/k", "title deskclaw-portal && cd /d `"$PortalDir`" && set API_PROXY_TARGET=$env:API_PROXY_TARGET&& npm run dev -- --host 127.0.0.1 --port $PortalPort"

# ── 5. wait for backend (first boot runs all migrations, ~1 min) ─────────
Log "waiting for backend to be ready (first run applies DB migrations, ~1 min)..."
$ready = $false
for ($i = 0; $i -lt 120; $i++) {
    if (Test-PortFree $BackendPort) { Start-Sleep -Seconds 2; continue }
    if (Test-HttpOk "http://localhost:$BackendPort/api/v1/health") { $ready = $true; break }
    Start-Sleep -Seconds 2
}
if (-not $ready) { Fail "backend not ready after 4 min - check the deskclaw-backend window for errors" }
Ok "backend ready"

# set a known demo password on the seeded admin account
$resetPy = @'
import asyncio
from sqlalchemy import select
from app.core.deps import async_session_factory
from app.models.user import User
from app.services.auth_service import hash_password

async def main():
    async with async_session_factory() as db:
        user = (await db.execute(select(User).where(User.username == "admin"))).scalar_one_or_none()
        if user is None:
            print("WARN: admin user not found (first boot may still be seeding)")
            return
        user.password_hash = hash_password("%%PASSWORD%%")
        user.must_change_password = False
        await db.commit()
        print("admin password reset OK")

asyncio.run(main())
'@ -replace "%%PASSWORD%%", $DemoPassword
$resetPyFile = Join-Path $env:TEMP "deskclaw_reset_admin_pw.py"
[IO.File]::WriteAllText($resetPyFile, $resetPy)
Push-Location $BackendDir
uv run python $resetPyFile
Pop-Location

# ── 6. wait for portal, then summary + open browser ──────────────────────
Log "waiting for portal (vite dev server, ~10-30s first time)..."
for ($i = 0; $i -lt 60; $i++) {
    if (Test-HttpOk "http://localhost:$PortalPort") { break }
    Start-Sleep -Seconds 2
}

$loginFile = Join-Path $RepoRoot "IDE-登录信息.txt"
@"
DeskClaw IDE mode (no Docker)
Portal : http://localhost:$PortalPort
Backend: http://localhost:$BackendPort
Login  : admin@deskclaw.com / $DemoPassword
Stop   : close the three deskclaw-* command windows
(Database: portable PostgreSQL at 127.0.0.1:$PgPort, data in .tools\pgdata)
"@ | Out-File -Encoding utf8 $loginFile

Ok "ALL UP."
Ok "Portal: http://localhost:$PortalPort   login: admin@deskclaw.com / $DemoPassword"
Ok "credentials saved to IDE-登录信息.txt"
Start-Process "http://localhost:$PortalPort"
