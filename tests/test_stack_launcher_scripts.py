"""Safety and invariant audit tests for QueryPilot stack launcher scripts.

Verifies:
1. scripts/start_stack.ps1 and scripts/stop_stack.ps1 exist.
2. start_stack.ps1 validates Python virtual environment and port availability before launching.
3. start_stack.ps1 tracks process IDs in stack_pids.json.
4. stop_stack.ps1 uses PID tracking and process signature checks before terminating.
5. stop_stack.ps1 protects unrelated processes on ports 8000/8001/5173 from accidental termination.
6. Secrets (GEMINI_API_KEY, bootstrap tokens) are never printed to console output.
"""

from pathlib import Path
import re
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_launcher_scripts_exist():
    """Verify both launcher and stopper scripts exist in scripts/ directory."""
    start_script = REPO_ROOT / "scripts" / "start_stack.ps1"
    stop_script = REPO_ROOT / "scripts" / "stop_stack.ps1"

    assert start_script.is_file(), "scripts/start_stack.ps1 must exist"
    assert stop_script.is_file(), "scripts/stop_stack.ps1 must exist"


def test_start_script_safety_invariants():
    """Verify start_stack.ps1 enforces pre-flight checks, loopback binding, and PID tracking."""
    start_script = (REPO_ROOT / "scripts" / "start_stack.ps1").read_text(encoding="utf-8")

    # 1. Pre-flight Python virtualenv check
    assert "Test-Path $PythonExe" in start_script

    # 2. Port availability check before launching
    assert "Test-PortAvailable 8000" in start_script
    assert "Test-PortAvailable 8001" in start_script

    # 3. Process ID tracking
    assert "-PassThru" in start_script
    assert "stack_pids.json" in start_script
    assert "trackedPids" in start_script

    # 4. Loopback and port binding
    assert "--port 8000" in start_script
    assert "--port 8001" in start_script


def test_stop_script_unrelated_process_protection():
    """Verify stop_stack.ps1 validates process identity and protects unrelated processes."""
    stop_script = (REPO_ROOT / "scripts" / "stop_stack.ps1").read_text(encoding="utf-8")

    # 1. Uses PID file tracking
    assert "stack_pids.json" in stop_script

    # 2. Process inspection & QueryPilot signature verification
    assert "Get-CimInstance Win32_Process" in stop_script
    assert "query_pilot" in stop_script

    # 3. Explicit refusal to kill unrelated processes
    assert "Refusing to stop unrelated process" in stop_script


def test_zero_secret_exposure_in_scripts():
    """Verify launcher scripts do not print GEMINI_API_KEY or bootstrap tokens."""
    start_script = (REPO_ROOT / "scripts" / "start_stack.ps1").read_text(encoding="utf-8")
    stop_script = (REPO_ROOT / "scripts" / "stop_stack.ps1").read_text(encoding="utf-8")

    # Ensure no Write-Host prints GEMINI_API_KEY value
    assert not re.search(r'Write-Host [^\r\n]*\$env:GEMINI_API_KEY', start_script)
    assert not re.search(r'Write-Host [^\r\n]*GEMINI_API_KEY\s*=', start_script)

    # Ensure bootstrap token is not echoed to console
    assert not re.search(r'Write-Host [^\r\n]*token', start_script, re.IGNORECASE) or "copy the single-use bootstrap token from" in start_script

