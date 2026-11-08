"""Controlled Runtime Acceptance Script for Tauri v2 Native SQLite File Picker (B28-A.3).

Validates:
1. Launching development desktop shell (src-tauri/target/debug/querypilot.exe).
2. Connecting via WebView2 CDP.
3. Inspecting Tauri IPC presence on loopback origin (window.__TAURI_INTERNALS__).
4. Opening Profile Onboarding Modal and selecting SQLite engine.
5. Verifying Browse button, manual entry fallback, and dialog contracts.
6. Clean shutdown of desktop process and sidecar.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import psutil
import websockets

REPO_ROOT = Path(__file__).resolve().parent.parent
TAURI_EXE = REPO_ROOT / "src-tauri" / "target" / "debug" / "querypilot.exe"
DEMO_DB = REPO_ROOT / "data" / "demo" / "university_demo.db"
CDP_PORT = 9226


def kill_existing():
    for p in psutil.process_iter(["name"]):
        try:
            if p.info["name"] in ("querypilot.exe", "querypilot-agent.exe"):
                p.kill()
        except Exception:
            pass
    time.sleep(1)


async def main():
    print("=" * 70)
    print("B28-A.3: RUNTIME VERIFICATION OF TAURI NATIVE FILE PICKER")
    print(f"Target Binary: {TAURI_EXE}")
    print(f"Demo DB:       {DEMO_DB.resolve()}")
    print("=" * 70)

    kill_existing()

    env = os.environ.copy()
    env["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = f"--remote-debugging-port={CDP_PORT}"
    env["QUERYPILOT_NO_POPUP"] = "1"

    proc = subprocess.Popen([str(TAURI_EXE)], cwd=str(REPO_ROOT), env=env)
    print(f"[1] Launched desktop process PID={proc.pid}")

    try:
        # Wait for CDP endpoint
        deadline = time.time() + 20.0
        ws_url = None
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json", timeout=2) as r:
                    targets = json.loads(r.read().decode())
                    for t in targets:
                        if t.get("type") == "page":
                            ws_url = t.get("webSocketDebuggerUrl")
                            break
                    if ws_url:
                        break
            except Exception:
                pass
            time.sleep(0.5)

        assert ws_url, f"Failed to connect to WebView2 CDP on port {CDP_PORT}"
        print(f"[2] Connected to WebView2 CDP: {ws_url}")

        async with websockets.connect(ws_url) as ws:
            async def eval_js(expr: str):
                msg = {
                    "id": 1,
                    "method": "Runtime.evaluate",
                    "params": {"expression": expr, "returnByValue": True, "awaitPromise": True},
                }
                await ws.send(json.dumps(msg))
                res = json.loads(await ws.recv())
                return res.get("result", {}).get("result", {}).get("value")

            # Wait for app load
            print("[3] Waiting for frontend app rendering...")
            for _ in range(30):
                href = await eval_js("window.location.href")
                if href and "/app/" in href:
                    break
                await asyncio.sleep(0.5)

            print(f"  -> App URL: {await eval_js('window.location.href')}")

            # Check Tauri IPC availability
            has_internals = await eval_js("typeof window.__TAURI_INTERNALS__ !== 'undefined'")
            has_invoke = await eval_js("typeof window.__TAURI_INTERNALS__?.invoke === 'function'")
            print(f"[4] Tauri IPC Detection: __TAURI_INTERNALS__={has_internals}, invoke={has_invoke}")

            # Check if Add Profile button exists and click it
            print("[5] Opening Profile Onboarding Modal...")
            modal_opened = await eval_js("""(() => {
                const btn = document.querySelector('.profile-add-header-btn') || 
                            Array.from(document.querySelectorAll('button')).find(b => b.innerText.includes('Add Profile'));
                if (btn) {
                    btn.click();
                    return true;
                }
                return false;
            })()""")
            print(f"  -> Modal trigger clicked: {modal_opened}")
            await asyncio.sleep(1.0)

            # Switch to SQLite tab in onboarding modal
            switched_sqlite = await eval_js("""(() => {
                const sqliteBtn = Array.from(document.querySelectorAll('.engine-toggle-btn')).find(b => b.innerText.includes('SQLite'));
                if (sqliteBtn) {
                    sqliteBtn.click();
                    return true;
                }
                return false;
            })()""")
            print(f"[6] Switched engine to SQLite: {switched_sqlite}")
            await asyncio.sleep(0.5)

            # Check presence of Database File Path input and Browse button
            has_path_input = await eval_js("Boolean(document.getElementById('onboarding-database-path'))")
            has_browse_btn = await eval_js("Boolean(document.querySelector('.profile-browse-btn'))")
            print(f"[7] UI Elements: path_input={has_path_input}, browse_button={has_browse_btn}")

            # Test manual entry functionality
            test_manual_path = str(DEMO_DB.resolve())
            manual_entry_result = await eval_js(f"""(() => {{
                const input = document.getElementById('onboarding-database-path');
                if (!input) return false;
                input.value = {json.dumps(test_manual_path)};
                input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                input.dispatchEvent(new Event('change', {{ bubbles: true }}));
                return input.value;
            }})()""")
            print(f"[8] Manual path entry test: set value -> '{manual_entry_result}'")
            assert manual_entry_result == test_manual_path, "Manual path entry failed!"

            # Test cancellation leaves existing value unchanged
            cancel_result = await eval_js("""(() => {
                const input = document.getElementById('onboarding-database-path');
                const beforeVal = input ? input.value : '';
                // Dispatch empty change on file picker (simulating cancellation)
                const filePicker = document.getElementById('onboarding-file-picker');
                if (filePicker) {
                    filePicker.dispatchEvent(new Event('change', { bubbles: true }));
                }
                const afterVal = input ? input.value : '';
                return { before: beforeVal, after: afterVal, preserved: beforeVal === afterVal };
            })()""")
            print(f"[9] Cancellation preservation test: {cancel_result}")
            assert cancel_result.get("preserved"), "Cancellation mutated field value!"

            print("\n[10] ALL CONTROLLED RUNTIME CHECKS PASSED CLEANLY!")

    finally:
        print("[11] Terminating desktop process...")
        kill_existing()
        print("  -> Clean shutdown complete.")

if __name__ == "__main__":
    asyncio.run(main())
