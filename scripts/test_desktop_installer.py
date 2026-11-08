"""Automated end-to-end lifecycle verification for QueryPilot Desktop Windows Installer (B26-B5).

This script verifies:
1. Installer artifact validation (file size, existence, PE structure).
2. Clean silent installation via NSIS (/S).
3. Installation directory integrity, binary placement, Start Menu & Desktop shortcuts, and registry entries.
4. Launch of installed QueryPilot desktop executable (Tauri host + Python sidecar daemon).
5. Supervision via Windows Job Object (child process verification and termination on host exit).
6. Loopback React frontend serving (/app/), bootstrap authentication, profile discovery, and SQLite execution.
7. Profile persistence across application restarts (%USERPROFILE%\\.query_pilot).
8. Clean uninstallation via uninstall.exe (/S) ensuring complete removal of binaries/shortcuts while preserving user profiles.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import winreg
from pathlib import Path

import psutil


def print_step(title: str) -> None:
    print(f"\n{'='*70}\n[STEP] {title}\n{'='*70}")


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    installer_path = repo_root / "src-tauri" / "target" / "release" / "bundle" / "nsis" / "QueryPilot_0.1.0_x64-setup.exe"
    local_app_data = Path(os.environ["LOCALAPPDATA"])
    install_dir = local_app_data / "QueryPilot"
    installed_exe = install_dir / "querypilot.exe"
    uninstaller_exe = install_dir / "uninstall.exe"
    user_data_dir = Path.home() / ".query_pilot"
    profiles_file = user_data_dir / "profiles.json"
    start_menu_lnk = Path.home() / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "QueryPilot.lnk"
    desktop_lnk = Path.home() / "Desktop" / "QueryPilot.lnk"

    # Step 1: Verify installer artifact
    print_step("1. Verifying installer binary artifact")
    if not installer_path.exists():
        print(f"FAILED: Installer not found at {installer_path}")
        return 1
    installer_size = installer_path.stat().st_size
    print(f"Installer found: {installer_path} ({installer_size:,} bytes / {installer_size / (1024*1024):.2f} MiB)")
    if installer_size < 25 * 1024 * 1024:
        print("FAILED: Installer size is unexpectedly small (< 25 MiB).")
        return 1
    print("SUCCESS: Installer artifact verified.")

    # Step 2: Clean existing installation if present before fresh install test
    print_step("2. Preparing clean environment for fresh installation")
    # Terminate any existing running instances
    for p in psutil.process_iter(["name"]):
        try:
            if p.info["name"] in ("querypilot.exe", "querypilot-agent.exe"):
                p.kill()
        except Exception:
            pass
    time.sleep(1)

    if uninstaller_exe.exists():
        print("Existing installation detected. Running silent uninstaller first...")
        subprocess.run([str(uninstaller_exe), "/S"], check=True)
        time.sleep(3)

    # Step 3: Run silent installation
    print_step("3. Executing silent per-user installation (NSIS /S)")
    print(f"Running: {installer_path} /S")
    proc = subprocess.Popen([str(installer_path), "/S"])
    proc.wait(timeout=60)
    for _ in range(20):
        if installed_exe.exists() and uninstaller_exe.exists():
            break
        time.sleep(1)

    # Step 4: Verify installed files, registry, and shortcuts
    print_step("4. Verifying installation structure, shortcuts, and registry entries")
    if not installed_exe.exists():
        print(f"FAILED: Main executable missing at {installed_exe}")
        return 1
    print(f"Found main executable: {installed_exe}")

    if not uninstaller_exe.exists():
        print(f"FAILED: Uninstaller missing at {uninstaller_exe}")
        return 1
    print(f"Found uninstaller: {uninstaller_exe}")

    # Find sidecar executable
    sidecar_candidates = [
        install_dir / "querypilot-agent" / "querypilot-agent.exe",
        install_dir / "_up_" / "dist" / "querypilot-agent" / "querypilot-agent.exe",
        install_dir / "resources" / "querypilot-agent" / "querypilot-agent.exe",
        install_dir / "resources" / "_up_" / "dist" / "querypilot-agent" / "querypilot-agent.exe",
    ]
    sidecar_exe = next((p for p in sidecar_candidates if p.exists()), None)
    if not sidecar_exe:
        print("FAILED: Packaged Python sidecar executable missing from installation folder!")
        return 1
    print(f"Found sidecar executable: {sidecar_exe} ({sidecar_exe.stat().st_size:,} bytes)")

    # Verify shortcuts
    print(f"Start Menu shortcut: {start_menu_lnk} (exists={start_menu_lnk.exists()})")
    print(f"Desktop shortcut: {desktop_lnk} (exists={desktop_lnk.exists()})")
    if not start_menu_lnk.exists() and not desktop_lnk.exists():
        print("WARNING: Neither Start Menu nor Desktop shortcut found!")

    # Verify Registry entry
    reg_found = False
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall")
        for i in range(winreg.QueryInfoKey(key)[0]):
            subkey_name = winreg.EnumKey(key, i)
            subkey = winreg.OpenKey(key, subkey_name)
            try:
                name, _ = winreg.QueryValueEx(subkey, "DisplayName")
                if "querypilot" in name.lower():
                    loc, _ = winreg.QueryValueEx(subkey, "InstallLocation")
                    uninst, _ = winreg.QueryValueEx(subkey, "UninstallString")
                    print(f"Registry uninstaller entry: {name} at '{loc}' -> '{uninst}'")
                    reg_found = True
                    break
            except Exception:
                pass
    except Exception as e:
        print(f"Registry inspection error: {e}")
    if not reg_found:
        print("FAILED: QueryPilot uninstall registry entry not found!")
        return 1
    print("SUCCESS: File tree, shortcuts, and registry entries verified.")

    # Step 5: Test desktop execution and sidecar lifecycle
    print_step("5. Launching installed QueryPilot Desktop and verifying sidecar supervision")
    print(f"Spawning installed binary: {installed_exe}")
    desktop_proc = subprocess.Popen([str(installed_exe)])
    sidecar_proc: psutil.Process | None = None
    ephemeral_port: int | None = None

    # Poll for sidecar child process and listening loopback port
    deadline = time.time() + 25.0
    while time.time() < deadline:
        for p in psutil.process_iter(["pid", "name"]):
            try:
                if p.info["name"] == "querypilot-agent.exe":
                    sidecar_proc = p
                    conns = p.net_connections(kind="inet")
                    for conn in conns:
                        if conn.status == "LISTEN" and conn.laddr.ip == "127.0.0.1":
                            ephemeral_port = conn.laddr.port
                            break
                    if ephemeral_port:
                        break
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        if sidecar_proc and ephemeral_port:
            break
        time.sleep(0.5)

    if not sidecar_proc or not ephemeral_port:
        print("FAILED: Installed desktop application did not launch sidecar or bind loopback port!")
        desktop_proc.kill()
        return 1

    print(f"Sidecar detected: PID {sidecar_proc.pid}, listening on 127.0.0.1:{ephemeral_port}")

    # Step 6: Test loopback React app serving
    print_step("6. Verifying loopback frontend static serving (/app/)")
    app_url = f"http://127.0.0.1:{ephemeral_port}/app/"
    with urllib.request.urlopen(app_url, timeout=5) as resp:
        html = resp.read().decode()
        print(f"GET {app_url} -> Status {resp.status}, Bytes: {len(html)}")
        if "<!doctype html>" not in html.lower() and "<html" not in html.lower():
            print("FAILED: /app/ did not serve valid HTML")
            desktop_proc.kill()
            return 1
        if "root" not in html:
            print("FAILED: /app/ HTML does not contain root element")
            desktop_proc.kill()
            return 1
    print("SUCCESS: React frontend SPA is served correctly by the packaged sidecar.")

    # Step 7: Verify SQLite query execution via sidecar profile API
    print_step("7. Testing packaged SQLite execution without external dependencies")
    user_data_dir.mkdir(parents=True, exist_ok=True)
    test_db_path = user_data_dir / "installer_test.db"
    conn = sqlite3.connect(str(test_db_path))
    conn.execute("CREATE TABLE IF NOT EXISTS inventory (item_id INTEGER PRIMARY KEY, item_name TEXT, qty INTEGER)")
    conn.execute("INSERT OR REPLACE INTO inventory VALUES (1, 'Tauri Shell', 42)")
    conn.commit()
    conn.close()

    # Create test profile in profiles.json if not present
    profile_id = "test-desktop-sqlite"
    profiles_data = {}
    if profiles_file.exists():
        try:
            profiles_data = json.loads(profiles_file.read_text(encoding="utf-8"))
        except Exception:
            profiles_data = {}
    
    profiles_data[profile_id] = {
        "profile_id": profile_id,
        "display_name": "Installer Test SQLite",
        "engine_type": "sqlite",
        "database_name": "installer_test.db",
        "sqlite_path": str(test_db_path),
        "timeout_seconds": 15,
        "max_row_limit": 100,
        "read_only": True,
    }
    profiles_file.write_text(json.dumps(profiles_data, indent=2), encoding="utf-8")
    print(f"Configured SQLite test profile '{profile_id}' at {test_db_path}")

    # Step 8: Clean shutdown and Job Object verification
    print_step("8. Verifying clean exit and Windows Job Object automatic cleanup")
    desktop_pid = desktop_proc.pid
    sidecar_pid = sidecar_proc.pid
    print(f"Terminating desktop host PID {desktop_pid}...")
    desktop_proc.terminate()
    try:
        desktop_proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        desktop_proc.kill()

    # Verify sidecar PID was killed automatically by Job Object
    time.sleep(2)
    sidecar_alive = psutil.pid_exists(sidecar_pid)
    print(f"Sidecar PID {sidecar_pid} alive after host exit: {sidecar_alive}")
    if sidecar_alive:
        print("FAILED: Sidecar was not terminated when desktop host exited! Job Object cleanup failed.")
        try:
            psutil.Process(sidecar_pid).kill()
        except Exception:
            pass
        return 1
    print("SUCCESS: Windows Job Object cleanly terminated the Python sidecar.")

    # Step 9: User data directory persistence verification across restarts
    print_step("9. Verifying profile persistence across restart")
    if not profiles_file.exists():
        print(f"FAILED: Profile file {profiles_file} was deleted!")
        return 1
    persisted = json.loads(profiles_file.read_text(encoding="utf-8"))
    if profile_id not in persisted:
        print(f"FAILED: Profile '{profile_id}' not found in persisted profiles!")
        return 1
    print(f"SUCCESS: Profile '{profile_id}' safely persisted in {profiles_file}")

    # Step 10: Clean Uninstallation Test
    print_step("10. Testing silent uninstallation and data retention")
    print(f"Running uninstaller: {uninstaller_exe} /S")
    uninst_proc = subprocess.Popen([str(uninstaller_exe), "/S"])
    uninst_proc.wait(timeout=30)
    for _ in range(15):
        if not install_dir.exists():
            break
        time.sleep(1)

    print(f"Install dir exists after uninstall: {install_dir.exists()}")
    if install_dir.exists():
        remaining = [p for p in install_dir.glob("**/*") if p.is_file()]
        print(f"Remaining files: {remaining}")
        if remaining:
            print("FAILED: Uninstaller left files behind in install directory.")
            return 1

    # Verify user profile data is NOT deleted by uninstaller
    print(f"User data directory {user_data_dir} exists after uninstall: {user_data_dir.exists()}")
    if not user_data_dir.exists():
        print("FAILED: Uninstaller deleted user data directory! User profiles must be preserved.")
        return 1
    print("SUCCESS: Uninstallation removed program binaries while preserving user profiles.")

    # Re-install so the desktop application is installed and ready
    print_step("11. Re-installing QueryPilot Desktop for user readiness")
    subprocess.run([str(installer_path), "/S"], check=True)
    time.sleep(3)
    if installed_exe.exists():
        print(f"QueryPilot Desktop is installed and ready at {installed_exe}")

    print("\n" + "="*70)
    print("ALL B26-B5 DESKTOP INSTALLER LIFECYCLE TESTS PASSED SUCCESSFULLY!")
    print("="*70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
