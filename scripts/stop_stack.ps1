# QueryPilot Stack Stopper
# Gracefully and safely terminates only QueryPilot processes on ports 8000, 8001, and 5173.

param (
    [int[]]$Ports = @(8000, 8001, 5173),
    [switch]$ForceUnsafe
)

$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$PidFile = Join-Path $HOME ".query_pilot\stack_pids.json"

Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "       QueryPilot Safe Stack Stopper       " -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan

# 1. Terminate tracked launcher windows if PID tracking file exists
if (Test-Path $PidFile) {
    try {
        $trackingData = Get-Content $PidFile -Raw | ConvertFrom-Json
        Write-Host "Found active launcher PID registry ($PidFile)..." -ForegroundColor Gray
        
        $windowPids = @($trackingData.api_window_pid, $trackingData.agent_window_pid, $trackingData.frontend_window_pid)
        foreach ($wPid in $windowPids) {
            if ($wPid -and ($wPid -gt 0)) {
                $proc = Get-Process -Id $wPid -ErrorAction SilentlyContinue
                if ($proc) {
                    Write-Host "Stopping QueryPilot window process $($proc.ProcessName) (PID: $wPid)..." -ForegroundColor Cyan
                    Stop-Process -Id $wPid -Force -ErrorAction SilentlyContinue
                }
            }
        }
        Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    } catch {
        Write-Warning "Could not process PID file: $_"
    }
}

# 2. Check listening ports and terminate ONLY confirmed QueryPilot processes
foreach ($port in $Ports) {
    $connections = Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue
    if ($connections) {
        $pids = $connections | Select-Object -ExpandProperty OwningProcess -Unique
        foreach ($procId in $pids) {
            if ($procId -gt 0) {
                try {
                    $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
                    if (-not $proc) {
                        continue
                    }

                    # Inspect process command line / path to verify it belongs to QueryPilot
                    $isQueryPilot = $false
                    $procPath = $proc.Path
                    $procCmd = ""
                    try {
                        $cim = Get-CimInstance Win32_Process -Filter "ProcessId = $procId" -ErrorAction SilentlyContinue
                        if ($cim -and $cim.CommandLine) {
                            $procCmd = $cim.CommandLine
                        }
                    } catch {
                        # Ignore CIM inspection errors
                    }

                    if ($procCmd -match "query_pilot" -or 
                        $procCmd -match "vite" -or 
                        $procCmd -match "university_demo" -or
                        ($procPath -and $procPath.StartsWith($ProjectRoot))) {
                        $isQueryPilot = $true
                    }

                    if ($isQueryPilot -or $ForceUnsafe) {
                        Write-Host "Stopping confirmed QueryPilot process $($proc.ProcessName) (PID: $procId) on port $port..." -ForegroundColor Green
                        Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
                    } else {
                        Write-Warning "Process '$($proc.ProcessName)' (PID: $procId) on port $port does not match QueryPilot signatures. Refusing to stop unrelated process. Use -ForceUnsafe to override."
                    }
                } catch {
                    Write-Warning ("Could not stop PID " + $procId + ": " + $_)
                }
            }
        }
    } else {
        Write-Host "Port $port is free." -ForegroundColor Gray
    }
}

Write-Host "QueryPilot stack shutdown check completed." -ForegroundColor Green
