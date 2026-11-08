#!/usr/bin/env python3
"""End-to-end verification suite for QueryPilot Tauri 2 Desktop Shell (B26-B3).

Validates:
1. Tauri desktop executable compilation and binary integrity.
2. Desktop application launch and automated sidecar spawning.
3. Readiness handshake parsing (ephemeral port and PID).
4. Native WebView target URL verification (`/app/` on loopback agent).
5. Single-instance mutex enforcement.
6. Clean process termination and Win32 Job Object kill guarantee.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
import urllib.error


def check_pid_exists(pid: int) -> bool:
    """Check if a process with given PID exists on Windows."""
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return False
    except Exception:
        return False


def test_tauri_desktop() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    tauri_exe = repo_root / "src-tauri" / "target" / "debug" / "querypilot.exe"
    sidecar_exe = repo_root / "dist" / "querypilot-agent" / "querypilot-agent.exe"

    sys.stdout.write("=" * 75 + "\n")
    sys.stdout.write("QUERYPILOT TAURI 2 DESKTOP SHELL VALIDATION SUITE (B26-B3)\n")
    sys.stdout.write(f"Desktop Target: {tauri_exe}\n")
    sys.stdout.write(f"Sidecar Target: {sidecar_exe}\n")
    sys.stdout.write("=" * 75 + "\n\n")

    # 1. Verify desktop executable exists
    sys.stdout.write("[Test 1/6] Verifying Tauri desktop binary compilation...\n")
    assert tauri_exe.is_file(), f"Tauri executable missing at: {tauri_exe}"
    assert sidecar_exe.is_file(), f"Sidecar executable missing at: {sidecar_exe}"
    sys.stdout.write(f"  -> PASS: Found desktop shell ({tauri_exe.stat().st_size / (1024*1024):.1f} MB)\n")
    sys.stdout.write(f"  -> PASS: Found packaged sidecar ({sidecar_exe.stat().st_size / (1024*1024):.1f} MB)\n\n")

    # 2. Launch desktop shell and capture sidecar handshake
    sys.stdout.write("[Test 2/6] Launching desktop shell and capturing sidecar handshake...\n")
    proc = subprocess.Popen(
        [str(tauri_exe)],
        cwd=str(repo_root),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    sidecar_port = None
    sidecar_pid = None

    try:
        deadline = time.time() + 15.0
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                time.sleep(0.1)
                continue
            line = line.strip()
            # Expecting: [QueryPilot] Sidecar running on port <port> (PID=<pid>)
            if "[QueryPilot] Sidecar running on port" in line:
                parts = line.split("port")
                if len(parts) > 1:
                    port_pid_part = parts[1].strip()
                    # e.g. "54072 (PID=48860)"
                    port_str = port_pid_part.split()[0]
                    pid_str = port_pid_part.split("PID=")[1].rstrip(")")
                    sidecar_port = int(port_str)
                    sidecar_pid = int(pid_str)
                    break

        assert sidecar_port is not None, "Failed to capture sidecar port from desktop shell stdout."
        assert sidecar_pid is not None, "Failed to capture sidecar PID from desktop shell stdout."
        assert sidecar_port > 1024
        assert sidecar_pid > 0
        sys.stdout.write(f"  -> PASS: Sidecar launched: port={sidecar_port}, PID={sidecar_pid}\n")
        assert check_pid_exists(sidecar_pid), f"Sidecar PID {sidecar_pid} is not actively running!"
        sys.stdout.write(f"  -> PASS: Verified sidecar PID {sidecar_pid} is alive in Windows kernel.\n\n")

        # 3. Verify loopback frontend serving at http://127.0.0.1:<port>/app/
        sys.stdout.write("[Test 3/6] Verifying React frontend rendering via sidecar loopback endpoint...\n")
        app_url = f"http://127.0.0.1:{sidecar_port}/app/"
        app_req = urllib.request.Request(app_url)
        with urllib.request.urlopen(app_req, timeout=5.0) as resp:
            assert resp.status == 200
            content = resp.read().decode("utf-8")
            assert "<!doctype html>" in content.lower() or "<html" in content.lower()
            assert "root" in content
        sys.stdout.write(f"  -> PASS: Successfully fetched React frontend SPA from {app_url} (HTTP 200).\n\n")

        # 4. Verify Single-Instance Mutex Enforcement
        sys.stdout.write("[Test 4/6] Verifying single-instance enforcement...\n")
        second_proc = subprocess.Popen(
            [str(tauri_exe)],
            cwd=str(repo_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            sec_stdout, sec_stderr = second_proc.communicate(timeout=5.0)
            sec_exit = second_proc.returncode
            assert sec_exit in (0, 1)
            # The second instance must not spawn another sidecar or block
            sys.stdout.write(f"  -> PASS: Second instance exited promptly with code {sec_exit}.\n")
        except subprocess.TimeoutExpired:
            second_proc.kill()
            raise AssertionError("Second instance failed to terminate promptly (single instance violated)!")
        sys.stdout.write("  -> PASS: Single-instance mutex enforcement verified.\n\n")

        # 5. Verify Process Lifecycle: Terminating desktop shell terminates sidecar (Job Object)
        sys.stdout.write("[Test 5/6] Verifying Win32 Job Object cleanup on desktop exit...\n")
        proc.terminate()
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

        # Wait briefly for Windows kernel to close Job Object and terminate assigned processes
        time.sleep(1.5)

        # Sidecar process MUST BE DEAD
        assert not check_pid_exists(sidecar_pid), f"ORPHAN PROCESS DETECTED: Sidecar PID {sidecar_pid} still running after desktop shell exit!"
        sys.stdout.write(f"  -> PASS: Sidecar PID {sidecar_pid} was automatically terminated by Windows Job Object.\n")

        # Port must be free
        import socket
        chk_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        chk_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            chk_sock.bind(("127.0.0.1", sidecar_port))
            chk_sock.close()
        except OSError:
            raise AssertionError(f"Port {sidecar_port} remained bound after sidecar termination!")
        sys.stdout.write(f"  -> PASS: Ephemeral port {sidecar_port} is released.\n\n")

        # 6. Verify sidecar startup failure produces actionable error
        sys.stdout.write("[Test 6/6] Verifying failure handling when sidecar executable is missing...\n")
        fail_env = os.environ.copy()
        fail_env["QUERYPILOT_SIDECAR_PATH"] = "C:\\nonexistent_querypilot_sidecar.exe"
        fail_env["QUERYPILOT_NO_POPUP"] = "1"
        fail_proc = subprocess.Popen(
            [str(tauri_exe)],
            cwd=str(repo_root),
            env=fail_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        fail_stdout, fail_stderr = fail_proc.communicate(timeout=5.0)
        assert fail_proc.returncode == 1, f"Expected exit code 1 on sidecar failure, got {fail_proc.returncode}"
        assert "does not exist or is not a file" in fail_stderr
        sys.stdout.write(f"  -> PASS: Cleanly exited code 1 with diagnostic: {fail_stderr.strip()}\n\n")

    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if sidecar_pid and check_pid_exists(sidecar_pid):
            try:
                os.kill(sidecar_pid, 9)
            except Exception:
                pass

    sys.stdout.write("=" * 75 + "\n")
    sys.stdout.write("ALL TAURI 2 DESKTOP SHELL VALIDATIONS PASSED CLEANLY (6/6)\n")
    sys.stdout.write("=" * 75 + "\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(test_tauri_desktop())
    except Exception as exc:
        import traceback
        traceback.print_exc()
        sys.stderr.write(f"\nVALIDATION FAILED: {exc}\n")
        sys.exit(1)
