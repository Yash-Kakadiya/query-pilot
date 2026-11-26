# QueryPilot Application Build & Asset Deployment Script
# Run this script whenever you update frontend code, constants (developerInfo.ts), or public assets (developer.gif).
#
# Examples:
#   .\build-app.ps1          # Standard: Builds frontend, deploys assets to all targets & clears cache
#   .\build-app.ps1 -Run     # Builds, deploys assets, and immediately launches QueryPilot Desktop
#   .\build-app.ps1 -Full    # Full: Builds frontend, regenerates PyInstaller sidecar + Tauri binary & deploys

param (
    [switch]$Run,    # Automatically launch QueryPilot Desktop after building
    [switch]$Full    # Perform full PyInstaller sidecar + Tauri binary release packaging
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Resolve-Path $PSScriptRoot
Set-Location $ProjectRoot

Write-Host "==================================================" -ForegroundColor Cyan
Write-Host "       QueryPilot Desktop Build & Deploy          " -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan

# 1. Close running QueryPilot processes so static files and WebView2 cache can be updated
Write-Host "`n[1/4] Stopping existing QueryPilot processes..." -ForegroundColor Yellow
Stop-Process -Name "querypilot", "querypilot-agent" -Force -ErrorAction SilentlyContinue
Start-Sleep -Milliseconds 600

# 2. ALWAYS compile React frontend static bundle first
Write-Host "`n[2/4] Compiling React frontend bundle (Vite + TypeScript)..." -ForegroundColor Green
npm run build --prefix frontend
if ($LASTEXITCODE -ne 0) {
    Write-Error "Frontend compilation failed with code $LASTEXITCODE"
    exit $LASTEXITCODE
}

# 3. If -Full is requested, package the complete desktop release (rebuilding sidecar and Tauri binary)
if ($Full) {
    Write-Host "`n[3/4] Running Full Desktop Packaging (Python Sidecar + Tauri)..." -ForegroundColor Green
    & .venv\Scripts\python scripts/package_desktop_release.py --rebuild
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Packaging failed with code $LASTEXITCODE"
        exit $LASTEXITCODE
    }
}

# 4. Deploy static assets to all desktop sidecars (Installed AppData, Release dirs, Repo) & clear WebView2 cache
Write-Host "`n[4/4] Deploying static assets to Desktop Sidecar & clearing cache..." -ForegroundColor Green
& .venv\Scripts\python scripts/deploy_assets.py
if ($LASTEXITCODE -ne 0) {
    Write-Error "Asset deployment failed with code $LASTEXITCODE"
    exit $LASTEXITCODE
}

Write-Host "`nBuild & deployment completed successfully! ✨" -ForegroundColor Green

# 5. Launch application if -Run was specified
if ($Run) {
    $exePath = Join-Path $env:LOCALAPPDATA "QueryPilot\querypilot.exe"
    if (-not (Test-Path $exePath)) {
        $exePath = Join-Path $ProjectRoot "release\QueryPilot.exe"
    }
    if (-not (Test-Path $exePath)) {
        $exePath = Join-Path $ProjectRoot "src-tauri\target\release\querypilot.exe"
    }

    if (Test-Path $exePath) {
        Write-Host "Launching QueryPilot: $exePath..." -ForegroundColor Cyan
        $workingDir = Split-Path $exePath -Parent
        Start-Process -FilePath $exePath -WorkingDirectory $workingDir
    } else {
        Write-Warning "QueryPilot.exe not found. Run '.\build-app.ps1 -Full' to compile."
    }
} else {
    Write-Host "`nYou can now launch QueryPilot Desktop to view your changes." -ForegroundColor Cyan
    Write-Host "Tip: To build and immediately launch in one step, run:" -ForegroundColor Gray
    Write-Host "  .\build-app.ps1 -Run" -ForegroundColor Yellow
}
