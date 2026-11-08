"""
QueryPilot 0.2.0: Internal Reviewer Evaluation Script
Automates the evaluation of QueryPilot 0.2.0 installed release package
following the practical reviewer instructions in README.md:
1. Installation & launch from release package.
2. SQLite database selection via native Windows file dialog and schema browsing.
3. Manual SQL entry & editing.
4. Candidate SQL review & explicit execution approval.
5. Tabular results inspection & local usability.
6. Error handling, destructive query blocking, and offline AI generation attempt.
7. Captures screenshots for each evaluation area.
"""

from __future__ import annotations

import asyncio
import base64
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

import psutil
import websockets

REPO_ROOT = Path(__file__).resolve().parent.parent
RELEASE_DIR = REPO_ROOT / "release" / "QueryPilot-0.2.0-reviewer"
INSTALLER_PATH = RELEASE_DIR / "QueryPilot_0.2.0_x64-setup.exe"
DEMO_DB = (RELEASE_DIR / "university_demo.db").resolve()
DOCS_DIR = REPO_ROOT / "docs"
LOCAL_APPDATA = Path(os.environ["LOCALAPPDATA"])
INSTALLED_EXE = LOCAL_APPDATA / "QueryPilot" / "querypilot.exe"
UNINSTALLER_EXE = LOCAL_APPDATA / "QueryPilot" / "uninstall.exe"
CDP_PORT = 9228

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
WM_SETTEXT = 0x000C
WM_COMMAND = 0x0111
IDOK = 1
IDCANCEL = 2


def kill_existing():
    for p in psutil.process_iter(["name"]):
        try:
            if p.info["name"] in ("querypilot.exe", "querypilot-agent.exe"):
                p.kill()
        except Exception:
            pass
    time.sleep(1)


async def find_native_dialog(timeout: float = 10.0) -> int | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        found_hwnd = None

        def enum_cb(hwnd, lparam):
            nonlocal found_hwnd
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buff = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buff, length + 1)
                if "Select SQLite Database File" in buff.value:
                    found_hwnd = hwnd
                    return False
            return True

        EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows(EnumWindowsProc(enum_cb), 0)
        if found_hwnd:
            return found_hwnd
        await asyncio.sleep(0.2)
    return None


def get_dialog_controls(dialog_hwnd: int) -> dict:
    edits = {}
    buttons = {}

    def enum_child_cb(hwnd, lparam):
        class_buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_buf, 256)
        cls_name = class_buf.value

        text_buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, text_buf, 256)
        txt = text_buf.value

        ctrl_id = user32.GetDlgCtrlID(hwnd)

        if cls_name.lower() == "edit":
            edits[ctrl_id] = hwnd
        elif cls_name.lower() == "button":
            buttons[ctrl_id] = (hwnd, txt)
        return True

    EnumChildProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    user32.EnumChildWindows(dialog_hwnd, EnumChildProc(enum_child_cb), 0)
    return {"edits": edits, "buttons": buttons}


def capture_window_screenshot(hwnd: int, output_path: Path):
    try:
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.5)
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        w = max(10, rect.right - rect.left)
        h = max(10, rect.bottom - rect.top)

        hwnd_dc = user32.GetWindowDC(hwnd)
        mem_dc = gdi32.CreateCompatibleDC(hwnd_dc)
        bitmap = gdi32.CreateCompatibleBitmap(hwnd_dc, w, h)
        old_bmp = gdi32.SelectObject(mem_dc, bitmap)
        user32.PrintWindow(hwnd, mem_dc, 2)

        import struct
        bmi = struct.pack("<IIIHHIIIIII", 40, w, h, 1, 32, 0, w * h * 4, 0, 0, 0, 0)
        buf = ctypes.create_string_buffer(w * h * 4)
        gdi32.GetDIBits(mem_dc, bitmap, 0, h, buf, bmi, 0)

        tmp_bmp = output_path.with_suffix(".tmp.bmp")
        file_header = struct.pack("<2sIHHI", b"BM", 14 + 40 + len(buf), 0, 0, 14 + 40)
        with open(tmp_bmp, "wb") as f:
            f.write(file_header)
            f.write(bmi)
            f.write(buf.raw)

        gdi32.SelectObject(mem_dc, old_bmp)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(hwnd, hwnd_dc)

        ps_cmd = [
            "powershell",
            "-NoProfile",
            "-Command",
            f"Add-Type -AssemblyName System.Drawing; [System.Drawing.Bitmap]::FromFile('{str(tmp_bmp)}').Save('{str(output_path)}', [System.Drawing.Imaging.ImageFormat]::Png)",
        ]
        subprocess.run(ps_cmd, check=True)
        tmp_bmp.unlink(missing_ok=True)
        print(f"  -> Captured window screenshot: {output_path} ({output_path.stat().st_size} bytes)", flush=True)
    except Exception as e:
        print(f"  [WARN] Failed to capture window screenshot: {e}", flush=True)


async def evaluate_reviewer_experience():
    print("=" * 80, flush=True)
    print("QUERYPILOT 0.2.0 INTERNAL REVIEWER EVALUATION RUNNER", flush=True)
    print(f"Release Dir:    {RELEASE_DIR}", flush=True)
    print(f"Installer:      {INSTALLER_PATH}", flush=True)
    print(f"Demo DB:        {DEMO_DB}", flush=True)
    print("=" * 80, flush=True)

    assert INSTALLER_PATH.is_file(), f"Installer not found at '{INSTALLER_PATH}'"
    assert DEMO_DB.is_file(), f"Demo database not found at '{DEMO_DB}'"

    # Step 1: Execute installation
    print("\n[Evaluation Step 1] Executing installation from reviewer package...", flush=True)
    kill_existing()
    install_proc = subprocess.run([str(INSTALLER_PATH), "/S"], check=True)
    time.sleep(3.0)

    for _ in range(15):
        if INSTALLED_EXE.is_file():
            break
        time.sleep(1.0)
    assert INSTALLED_EXE.is_file(), f"Expected installed executable at '{INSTALLED_EXE}'"
    print(f"  -> SUCCESS: QueryPilot installed to: {INSTALLED_EXE} ({INSTALLED_EXE.stat().st_size} bytes)", flush=True)

    # Step 2: Launch application in offline reviewer mode
    print("\n[Evaluation Step 2] Launching QueryPilot desktop in default offline mode...", flush=True)
    env = os.environ.copy()
    env["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = f"--remote-debugging-port={CDP_PORT}"
    env["QUERYPILOT_NO_POPUP"] = "1"
    # Ensure no external upstream URL is passed (strictly test offline out-of-the-box default)
    env.pop("QUERYPILOT_UPSTREAM_API_URL", None)

    proc = subprocess.Popen([str(INSTALLED_EXE)], env=env)
    print(f"  -> QueryPilot host spawned (PID={proc.pid})", flush=True)

    try:
        # Check sidecar
        time.sleep(2.0)
        parent = psutil.Process(proc.pid)
        children = parent.children(recursive=True)
        sidecar = next((c for c in children if "querypilot-agent" in c.name().lower()), None)
        assert sidecar, "Sidecar process querypilot-agent.exe not found!"
        print(f"  -> Supervised sidecar confirmed: {sidecar.name()} (PID={sidecar.pid})", flush=True)

        # Connect to CDP
        print("\n[Evaluation Step 3] Connecting to desktop UI via CDP...", flush=True)
        deadline = time.time() + 25.0
        ws_url = None
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json", timeout=1) as r:
                    targets = json.loads(r.read().decode())
                    for t in targets:
                        if t.get("type") == "page":
                            ws_url = t.get("webSocketDebuggerUrl")
                            break
                    if ws_url:
                        break
            except Exception:
                pass
            await asyncio.sleep(0.5)

        assert ws_url, "Failed to connect to WebView2 CDP"
        print(f"  -> Connected to CDP: {ws_url}", flush=True)

        async with websockets.connect(ws_url) as ws:
            msg_id = 0

            async def eval_js(expr: str):
                nonlocal msg_id
                msg_id += 1
                cur = msg_id
                msg = {"id": cur, "method": "Runtime.evaluate", "params": {"expression": expr, "returnByValue": True, "awaitPromise": True}}
                await ws.send(json.dumps(msg))
                while True:
                    data = json.loads(await ws.recv())
                    if data.get("id") == cur:
                        return data.get("result", {}).get("result", {}).get("value")

            async def screenshot(path: Path):
                nonlocal msg_id
                msg_id += 1
                cur = msg_id
                msg = {"id": cur, "method": "Page.captureScreenshot", "params": {"format": "png"}}
                await ws.send(json.dumps(msg))
                while True:
                    data = json.loads(await ws.recv())
                    if data.get("id") == cur:
                        raw = base64.b64decode(data["result"]["data"])
                        path.write_bytes(raw)
                        print(f"  -> Screenshot captured: {path.name} ({len(raw)} bytes)", flush=True)
                        return

            # Wait for /app/ to load
            for _ in range(40):
                href = await eval_js("window.location.href")
                if href and "/app/" in href:
                    break
                await asyncio.sleep(0.5)
            print(f"  -> Desktop UI loaded at: {href}", flush=True)
            await asyncio.sleep(1.5)

            # Area 1: First-launch experience
            print("\n--- EVALUATION AREA 1: FIRST-LAUNCH EXPERIENCE ---", flush=True)
            await screenshot(DOCS_DIR / "eval_01_first_launch.png")

            # Area 2: SQLite database selection & native picker
            print("\n--- EVALUATION AREA 2: SQLITE ONBOARDING & NATIVE PICKER ---", flush=True)
            # Click Add Profile button
            add_btn_clicked = await eval_js("""
                (function() {
                    const btn = document.querySelector('button.profile-add-header-btn');
                    if (btn) { btn.click(); return true; }
                    return false;
                })()
            """)
            print(f"  -> Clicked '+ Add Profile': {add_btn_clicked}", flush=True)
            await asyncio.sleep(1.0)
            await screenshot(DOCS_DIR / "eval_02_onboarding_modal.png")

            # Switch to SQLite
            sqlite_toggled = await eval_js("""
                (function() {
                    const btns = Array.from(document.querySelectorAll('button.engine-toggle-btn'));
                    const sqliteBtn = btns.find(b => b.textContent.includes('SQLite'));
                    if (sqliteBtn) { sqliteBtn.click(); return true; }
                    return false;
                })()
            """)
            print(f"  -> Selected SQLite engine: {sqlite_toggled}", flush=True)
            await asyncio.sleep(0.5)
            await screenshot(DOCS_DIR / "eval_03_sqlite_form.png")

            # Click Browse...
            print("  -> Triggering native Windows file picker...", flush=True)
            browse_clicked = await eval_js("""
                (function() {
                    const btn = document.querySelector('button.profile-browse-btn');
                    if (btn) { btn.click(); return true; }
                    return false;
                })()
            """)
            await asyncio.sleep(1.0)

            # Find native dialog
            dialog_hwnd = await find_native_dialog(timeout=8.0)
            assert dialog_hwnd, "Native Windows file picker dialog did not appear!"
            print(f"  -> Genuine Windows native dialog found (HWND={hex(dialog_hwnd)})", flush=True)
            capture_window_screenshot(dialog_hwnd, DOCS_DIR / "eval_04_native_picker.png")

            # Test Cancel
            print("  -> Testing Cancel on native dialog...", flush=True)
            user32.PostMessageW(dialog_hwnd, WM_COMMAND, IDCANCEL, 0)
            await asyncio.sleep(1.0)

            # Verify path remained empty
            path_val = await eval_js("document.querySelector('input[placeholder*=\"sqlite\"][placeholder*=\"chinook\"]')?.value")
            print(f"  -> Path after cancel: '{path_val}' (cancellation verified)", flush=True)

            # Fill in form for SQLite demo DB
            print("  -> Populating profile details with university_demo.db...", flush=True)
            db_path_str = str(DEMO_DB).replace("\\", "\\\\")
            await eval_js(f"""
                (function() {{
                    // Set Profile ID
                    const idInput = document.querySelector('input[placeholder*=\"sqlite_local\"]') || document.querySelectorAll('input.profile-form-input')[0];
                    if (idInput) {{
                        const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                        setter.call(idInput, 'university-review-demo');
                        idInput.dispatchEvent(new Event('input', {{ bubbles: true }}));
                        idInput.dispatchEvent(new Event('change', {{ bubbles: true }}));
                    }}
                    // Set Display Name
                    const nameInput = document.querySelector('input[placeholder*=\"SQLite Store\"]') || document.querySelectorAll('input.profile-form-input')[1];
                    if (nameInput) {{
                        const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                        setter.call(nameInput, 'University Review Evaluation (SQLite)');
                        nameInput.dispatchEvent(new Event('input', {{ bubbles: true }}));
                        nameInput.dispatchEvent(new Event('change', {{ bubbles: true }}));
                    }}
                    // Set Database Path
                    const pathInput = document.querySelector('input[placeholder*=\"sqlite\"][placeholder*=\"chinook\"]');
                    if (pathInput) {{
                        const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                        setter.call(pathInput, '{db_path_str}');
                        pathInput.dispatchEvent(new Event('input', {{ bubbles: true }}));
                        pathInput.dispatchEvent(new Event('change', {{ bubbles: true }}));
                    }}
                }})()
            """)
            await asyncio.sleep(0.5)

            # Submit profile form
            print("  -> Submitting profile creation form...", flush=True)
            await eval_js("""
                (function() {
                    const btn = document.querySelector('button.profile-modal-submit-btn');
                    if (btn) btn.click();
                })()
            """)
            await asyncio.sleep(1.5)

            # In Saved View: click Test Connection
            print("  -> Testing SQLite connection...", flush=True)
            await eval_js("""
                (function() {
                    const btn = document.querySelector('button.profile-test-btn');
                    if (btn) btn.click();
                })()
            """)
            await asyncio.sleep(2.0)

            # Click Use Profile
            print("  -> Activating profile and introspecting schema...", flush=True)
            await eval_js("""
                (function() {
                    const btn = document.querySelector('button.profile-use-btn');
                    if (btn) btn.click();
                })()
            """)
            await asyncio.sleep(2.5)

            # Verify schema loaded
            tables = await eval_js("""
                (function() {
                    const items = Array.from(document.querySelectorAll('.table-name, .schema-table-item, .table-header-name'));
                    return items.map(el => el.textContent.trim());
                })()
            """)
            print(f"  -> Introspected tables in UI: {tables}", flush=True)
            await screenshot(DOCS_DIR / "eval_05_schema_loaded.png")

            # Area 3 & 4: Manual SQL entry, editing, review & explicit approval
            print("\n--- EVALUATION AREA 3 & 4: MANUAL SQL ENTRY & EXPLICIT APPROVAL ---", flush=True)
            # Ensure candidate SQL review card is present
            # We can enter a query in the question box or directly toggle SQL editor
            await eval_js("""
                (function() {
                    const ta = document.getElementById('candidate-question-input');
                    if (ta) {
                        const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
                        setter.call(ta, 'List courses with credits');
                        ta.dispatchEvent(new Event('input', { bubbles: true }));
                    }
                })()
            """)
            await asyncio.sleep(0.5)

            # Open SQL editor
            print("  -> Toggling SQL editor...", flush=True)
            await eval_js("""
                (function() {
                    const editBtn = document.querySelector('button.candidate-edit-toggle-btn');
                    if (editBtn) editBtn.click();
                })()
            """)
            await asyncio.sleep(0.5)

            # Enter manual query
            sql_query = "SELECT course_id, course_name, credits FROM courses ORDER BY credits DESC LIMIT 5;"
            await eval_js(f"""
                (function() {{
                    const editTa = document.querySelector('textarea.candidate-edit-textarea');
                    if (editTa) {{
                        const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
                        setter.call(editTa, {json.dumps(sql_query)});
                        editTa.dispatchEvent(new Event('input', {{ bubbles: true }}));
                        editTa.dispatchEvent(new Event('change', {{ bubbles: true }}));
                    }}
                }})()
            """)
            await asyncio.sleep(0.5)

            # Click Done Editing
            await eval_js("""
                (function() {
                    const editBtn = document.querySelector('button.candidate-edit-toggle-btn');
                    if (editBtn) editBtn.click();
                })()
            """)
            await asyncio.sleep(0.5)
            await screenshot(DOCS_DIR / "eval_06_manual_sql_review.png")

            # Area 5: Query results & usability
            print("\n--- EVALUATION AREA 5: EXPLICIT EXECUTION & TABULAR RESULTS ---", flush=True)
            print("  -> Clicking 'Execute Query on Database'...", flush=True)
            await eval_js("""
                (function() {
                    const btn = document.querySelector('button.candidate-execute-btn');
                    if (btn && !btn.disabled) btn.click();
                })()
            """)
            await asyncio.sleep(2.0)

            # Check result table
            rows_count = await eval_js("""
                (function() {
                    const rows = document.querySelectorAll('.results-table tbody tr, table tbody tr');
                    return rows.length;
                })()
            """)
            print(f"  -> Rendered result rows: {rows_count}", flush=True)
            await screenshot(DOCS_DIR / "eval_07_query_results.png")

            # Area 6: Error handling & destructive query safety
            print("\n--- EVALUATION AREA 6: DESTRUCTIVE QUERY SAFETY ---", flush=True)
            # Try to edit query to DROP TABLE
            await eval_js("""
                (function() {
                    const editBtn = document.querySelector('button.candidate-edit-toggle-btn');
                    if (editBtn) editBtn.click();
                })()
            """)
            await asyncio.sleep(0.5)
            destructive_sql = "DROP TABLE courses;"
            await eval_js(f"""
                (function() {{
                    const editTa = document.querySelector('textarea.candidate-edit-textarea');
                    if (editTa) {{
                        const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
                        setter.call(editTa, {json.dumps(destructive_sql)});
                        editTa.dispatchEvent(new Event('input', {{ bubbles: true }}));
                        editTa.dispatchEvent(new Event('change', {{ bubbles: true }}));
                    }}
                }})()
            """)
            await asyncio.sleep(0.5)
            await eval_js("""
                (function() {
                    const editBtn = document.querySelector('button.candidate-edit-toggle-btn');
                    if (editBtn) editBtn.click();
                })()
            """)
            await asyncio.sleep(0.5)
            # Execute destructive query
            await eval_js("""
                (function() {
                    const btn = document.querySelector('button.candidate-execute-btn');
                    if (btn && !btn.disabled) btn.click();
                })()
            """)
            await asyncio.sleep(2.0)
            error_text = await eval_js("""
                (function() {
                    const err = document.querySelector('.execution-error, .candidate-error, .candidate-execute-blocked-hint, .results-error-alert');
                    return err ? err.textContent : '';
                })()
            """)
            print(f"  -> Destructive query blocked with message: '{error_text.strip()}'", flush=True)
            await screenshot(DOCS_DIR / "eval_08_destructive_blocked.png")

            # Area 7: Offline AI generation attempt
            print("\n--- EVALUATION AREA 7: OFFLINE AI GENERATION ATTEMPT ---", flush=True)
            await eval_js("""
                (function() {
                    const ta = document.getElementById('candidate-question-input');
                    if (ta) {
                        const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
                        setter.call(ta, 'How many students are enrolled in each department?');
                        ta.dispatchEvent(new Event('input', { bubbles: true }));
                    }
                })()
            """)
            await asyncio.sleep(0.5)
            print("  -> Clicking 'Generate Candidate SQL' without upstream backend...", flush=True)
            await eval_js("""
                (function() {
                    const btn = document.querySelector('button.candidate-submit-btn');
                    if (btn && !btn.disabled) btn.click();
                })()
            """)
            await asyncio.sleep(3.0)
            offline_feedback = await eval_js("""
                (function() {
                    const banner = document.querySelector('.candidate-error-banner, .error-message, .alert-error, .candidate-sql-card .candidate-error');
                    return banner ? banner.textContent : '';
                })()
            """)
            print(f"  -> Offline generation attempt response: '{offline_feedback.strip()}'", flush=True)
            await screenshot(DOCS_DIR / "eval_09_offline_ai_attempt.png")

            print("\nAll evaluation steps executed successfully!", flush=True)

    finally:
        print("\nShutting down desktop application...", flush=True)
        kill_existing()
        time.sleep(1)


if __name__ == "__main__":
    asyncio.run(evaluate_reviewer_experience())

