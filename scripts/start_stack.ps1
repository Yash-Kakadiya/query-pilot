# QueryPilot Stack Launcher
# Starts QueryPilot services in dedicated PowerShell windows to prevent blocking.

param (
    [switch]$Dev,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $ProjectRoot

$StateDir = Join-Path $HOME ".query_pilot"
if (-not (Test-Path $StateDir)) {
    New-Item -ItemType Directory -Path $StateDir -Force | Out-Null
}
$PidFile = Join-Path $StateDir "stack_pids.json"

Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "       QueryPilot Service Launcher        " -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan

# 1. Check Python virtual environment
$PythonExe = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $PythonExe)) {
    Write-Error "Python executable not found at '$PythonExe'. Please run 'python -m venv .venv' and install requirements."
    exit 1
}

# 2. Check GEMINI_API_KEY without exposing secret value
$envFile = Join-Path $ProjectRoot ".env"
$hasApiKey = $false
if ($env:GEMINI_API_KEY) {
    $hasApiKey = $true
} elseif (Test-Path $envFile) {
    $envContent = Get-Content $envFile -Raw
    if ($envContent -match 'GEMINI_API_KEY\s*=\s*([^\r\n]+)') {
        $hasApiKey = $true
    }
}

if (-not $hasApiKey) {
    Write-Warning "GEMINI_API_KEY is not set in environment or .env file. SQL generation will fail without an API key."
}

# 3. Check port availability
function Test-PortAvailable([int]$port) {
    $conn = Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue
    return ($null -eq $conn)
}

if (-not (Test-PortAvailable 8000)) {
    Write-Error "Port 8000 is already in use. Please run 'scripts\stop_stack.ps1' or terminate the existing service on port 8000."
    exit 1
}
if (-not (Test-PortAvailable 8001)) {
    Write-Error "Port 8001 is already in use. Please run 'scripts\stop_stack.ps1' or terminate the existing service on port 8001."
    exit 1
}
if ($Dev -and (-not (Test-PortAvailable 5173))) {
    Write-Error "Port 5173 is already in use. Please run 'scripts\stop_stack.ps1' or terminate the existing service on port 5173."
    exit 1
}

# 4. Check frontend production assets if running in standard (non-Dev) mode
$distIndex = Join-Path $ProjectRoot "frontend\dist\index.html"
if ((-not $Dev) -and (-not (Test-Path $distIndex))) {
    Write-Host "Frontend production assets not found in frontend/dist/. Building assets..." -ForegroundColor Yellow
    npm run build --prefix frontend
    if (-not (Test-Path $distIndex)) {
        Write-Error "Failed to build frontend static assets. Please run 'npm install' and 'npm run build' inside 'frontend/'."
        exit 1
    }
}

$trackedPids = @{}

# 5. Launch Service 1: Hosted Generation API (Port 8000, Loopback only)
Write-Host "[1/3] Starting Hosted Generation API (port 8000)..." -ForegroundColor Green
$apiArgs = "-NoExit -Command `"cd '$ProjectRoot'; & '$PythonExe' -m query_pilot.api.cli --port 8000`""
$apiProc = Start-Process -FilePath "powershell.exe" -ArgumentList $apiArgs -PassThru
$trackedPids["api_window_pid"] = $apiProc.Id

# 5. Launch Service 2: Local Agent (Port 8001, Loopback only)
Write-Host "[2/3] Starting Local Agent Daemon (port 8001)..." -ForegroundColor Green
$agentFlags = "--port 8001"
if ($Dev) {
    $agentFlags += " --allow-dev-origin"
}
$agentArgs = "-NoExit -Command `"cd '$ProjectRoot'; & '$PythonExe' -m query_pilot.agent.cli $agentFlags`""
$agentProc = Start-Process -FilePath "powershell.exe" -ArgumentList $agentArgs -PassThru
$trackedPids["agent_window_pid"] = $agentProc.Id

# 6. Launch Service 3: Frontend
if ($Dev) {
    Write-Host "[3/3] Starting Vite Frontend Dev Server (port 5173)..." -ForegroundColor Green
    $devArgs = "-NoExit -Command `"cd '$ProjectRoot\frontend'; npm run dev`""
    $devProc = Start-Process -FilePath "powershell.exe" -ArgumentList $devArgs -PassThru
    $trackedPids["frontend_window_pid"] = $devProc.Id
    $targetUrl = "http://localhost:5173/app/"
} else {
    Write-Host "[3/3] Frontend served directly by Local Agent at http://127.0.0.1:8001/app/" -ForegroundColor Green
    $targetUrl = "http://127.0.0.1:8001/app/"
}

# Record tracked PIDs for safe shutdown
$trackedPids["created_at"] = (Get-Date).ToString("o")
$trackedPids | ConvertTo-Json | Set-Content -Path $PidFile -Encoding utf8

# 7. Wait briefly and open browser if requested
Start-Sleep -Seconds 3

Write-Host ""
Write-Host "Services successfully launched in dedicated windows:" -ForegroundColor Cyan
Write-Host "  * Generation API: http://127.0.0.1:8000/health (Window PID: $($trackedPids['api_window_pid']))" -ForegroundColor Gray
Write-Host "  * Local Agent:    http://127.0.0.1:8001/api/health (Window PID: $($trackedPids['agent_window_pid']))" -ForegroundColor Gray
Write-Host "  * Web UI:         $targetUrl" -ForegroundColor Gray
Write-Host ""
Write-Host "PID tracking saved to: $PidFile" -ForegroundColor DarkGray
Write-Host "Note: For authentication, copy the single-use bootstrap token from the Local Agent terminal window." -ForegroundColor Yellow

if (-not $NoBrowser) {
    Start-Process $targetUrl
}
