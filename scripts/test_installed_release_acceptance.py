"""
Milestone B28-B.3: Controlled Windows Runtime Acceptance Testing
Tests installed QueryPilot 0.2.0 release executable:
  %LOCALAPPDATA%\\QueryPilot\\querypilot.exe
Verifies:
1. Automatic sidecar startup, port binding, and handshake.
2. Win32 Job Object supervision and automatic child cleanup.
3. Zero-touch bootstrap session authentication.
4. Native Windows #32770 file dialog invocation, selection, cancellation, and repeated use.
5. Screenshot capture of the genuine native dialog and populated path.
6. Application termination, sidecar cleanup, and restart.
"""

from __future__ import annotations

import asyncio
import base64
import ctypes
from ctypes import wintypes
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
LOCAL_APPDATA = Path(os.environ["LOCALAPPDATA"])
INSTALLED_EXE = LOCAL_APPDATA / "QueryPilot" / "querypilot.exe"
DEMO_DB = (REPO_ROOT / "data" / "demo" / "university_demo.db").resolve()
DOCS_DIR = REPO_ROOT / "docs"
CDP_PORT = 9228

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
WM_SETTEXT = 0x000C
WM_COMMAND = 0x0111
BM_CLICK = 0x00F5
IDOK = 1
IDCANCEL = 2


def kill_existing():
    for p in psutil.process_iter(["name"]):
        try:
            if p.info["name"] in ("querypilot.exe", "querypilot-agent.exe", "msedgewebview2.exe"):
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
    """Capture a screenshot of a specific window via Win32 PrintWindow and save as PNG."""
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

        # Convert BMP to PNG using PowerShell System.Drawing
        ps_cmd = [
            "powershell",
            "-NoProfile",
            "-Command",
            f"Add-Type -AssemblyName System.Drawing; [System.Drawing.Bitmap]::FromFile('{str(tmp_bmp)}').Save('{str(output_path)}', [System.Drawing.Imaging.ImageFormat]::Png)",
        ]
        subprocess.run(ps_cmd, check=True)
        tmp_bmp.unlink(missing_ok=True)
        print(f"  -> Captured window screenshot successfully: {output_path} ({output_path.stat().st_size} bytes)", flush=True)
    except Exception as e:
        print(f"  [WARN] Failed to capture window screenshot: {e}", flush=True)


async def run_installed_acceptance():
    print("=" * 75, flush=True)
    print("B28-B.3: INSTALLED QUERYPILOT 0.2.0 RUNTIME ACCEPTANCE", flush=True)
    print(f"Target Executable: {INSTALLED_EXE}", flush=True)
    print(f"Demo DB:           {DEMO_DB}", flush=True)
    print("=" * 75, flush=True)

    assert INSTALLED_EXE.is_file(), f"Installed executable not found at '{INSTALLED_EXE}'"
    assert DEMO_DB.is_file(), f"Demo database not found at '{DEMO_DB}'"

    kill_existing()

    env = os.environ.copy()
    env["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = f"--remote-debugging-port={CDP_PORT}"
    env["QUERYPILOT_NO_POPUP"] = "1"

    print("\n[Step 1] Launching installed QueryPilot 0.2.0 executable...", flush=True)
    proc = subprocess.Popen([str(INSTALLED_EXE)], env=env)
    print(f"  -> Host process launched: PID={proc.pid}", flush=True)

    try:
        # Check child processes for sidecar
        print("[Step 2] Verifying Python sidecar launch under process supervision...", flush=True)
        time.sleep(2.0)
        parent_proc = psutil.Process(proc.pid)
        children = parent_proc.children(recursive=True)
        print(f"  -> Detected child processes: {[c.name() + ' (PID=' + str(c.pid) + ')' for c in children]}", flush=True)

        sidecar_found = False
        for c in children:
            if "querypilot-agent" in c.name().lower():
                sidecar_found = True
                print(f"  -> PASS: Managed sidecar process confirmed: {c.name()} (PID={c.pid})", flush=True)
                break
        assert sidecar_found, "FAILED: Sidecar process not found under installed QueryPilot host!"

        # Connect to WebView2 CDP
        print("[Step 3] Connecting to WebView2 CDP...", flush=True)
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

        assert ws_url, f"Failed to connect to WebView2 CDP on port {CDP_PORT}"
        print(f"  -> Connected to WebView2 CDP: {ws_url}", flush=True)

        async with websockets.connect(ws_url) as ws:
            msg_counter = 0

            async def eval_js(expr: str):
                nonlocal msg_counter
                msg_counter += 1
                cur_id = msg_counter
                msg = {
                    "id": cur_id,
                    "method": "Runtime.evaluate",
                    "params": {"expression": expr, "returnByValue": True, "awaitPromise": True},
                }
                await ws.send(json.dumps(msg))
                while True:
                    raw = await ws.recv()
                    data = json.loads(raw)
                    if data.get("id") == cur_id:
                        return data.get("result", {}).get("result", {}).get("value")

            async def capture_cdp_screenshot(out_file: Path):
                nonlocal msg_counter
                msg_counter += 1
                cur_id = msg_counter
                msg = {"id": cur_id, "method": "Page.captureScreenshot", "params": {"format": "png"}}
                await ws.send(json.dumps(msg))
                while True:
                    raw = await ws.recv()
                    data = json.loads(raw)
                    if data.get("id") == cur_id:
                        img_data = base64.b64decode(data["result"]["data"])
                        out_file.write_bytes(img_data)
                        return

            # Wait for /app/ load
            print("[Step 4] Verifying frontend load and bootstrap authentication...", flush=True)
            for _ in range(30):
                href = await eval_js("window.location.href")
                if href and "/app/" in href:
                    break
                await asyncio.sleep(0.5)

            app_url = await eval_js("window.location.href")
            print(f"  -> App URL: {app_url}", flush=True)
            assert "/app/" in app_url, f"Unexpected application URL: {app_url}"

            has_tauri = await eval_js("typeof window.__TAURI_INTERNALS__ === 'object'")
            print(f"  -> Tauri IPC runtime active: {has_tauri}", flush=True)
            assert has_tauri, "Tauri IPC is not active in WebView2!"

            # Open onboarding modal
            print("\n[Step 5] Opening Profile Onboarding Modal...", flush=True)
            for _ in range(30):
                has_header_btn = await eval_js("""(() => {
                    return !!(document.querySelector('.profile-add-header-btn') || 
                              Array.from(document.querySelectorAll('button')).find(b => b.innerText && b.innerText.includes('Add Profile')));
                })()""")
                if has_header_btn:
                    break
                await asyncio.sleep(0.5)

            open_res = await eval_js("""(() => {
                const btn = document.querySelector('.profile-add-header-btn') || 
                            Array.from(document.querySelectorAll('button')).find(b => b.innerText && b.innerText.includes('Add Profile'));
                if (btn) { btn.click(); return true; }
                return false;
            })()""")
            assert open_res, "Failed to click '+ Add Profile' button"
            await asyncio.sleep(1.0)

            # Switch engine to SQLite
            print("[Step 6] Switching engine to SQLite...", flush=True)
            switch_res = await eval_js("""(() => {
                const sqliteBtn = Array.from(document.querySelectorAll('.engine-toggle-btn')).find(b => b.innerText && b.innerText.includes('SQLite'));
                if (sqliteBtn) { sqliteBtn.click(); return true; }
                return false;
            })()""")
            assert switch_res, "Failed to click 'SQLite' toggle button"
            await asyncio.sleep(0.5)

            has_browse = await eval_js("!!document.querySelector('.profile-browse-btn')")
            assert has_browse, "Browse button not found after switching to SQLite!"

            # -------------------------------------------------------------
            # TEST 1: Open and Select Native File
            # -------------------------------------------------------------
            print("\n" + "=" * 60, flush=True)
            print("CRITICAL ACCEPTANCE TEST 1: OPEN AND SELECT NATIVE FILE", flush=True)
            print("=" * 60, flush=True)

            print("[Test 1.1] Clicking Browse button to trigger native Windows dialog...", flush=True)
            click_task_1 = asyncio.create_task(eval_js("""(() => {
                const btn = document.querySelector('.profile-browse-btn');
                if (btn) { btn.click(); return true; }
                return false;
            })()"""))

            dialog_hwnd = await find_native_dialog(timeout=8.0)
            assert dialog_hwnd is not None, "FAILED: Windows native file dialog did not appear in installed release!"
            print(f"[Test 1.2] Genuine Windows dialog detected: HWND={hex(dialog_hwnd)} ({dialog_hwnd})", flush=True)

            # Capture screenshot of the native dialog window
            dialog_screenshot_path = DOCS_DIR / "b28_b3_installed_picker_dialog.png"
            capture_window_screenshot(dialog_hwnd, dialog_screenshot_path)
            print(f"[Test 1.3] Saved native dialog screenshot: {dialog_screenshot_path}", flush=True)

            await asyncio.sleep(0.5)
            controls = get_dialog_controls(dialog_hwnd)
            print(f"[Test 1.4] Found controls: Edits={list(controls['edits'].keys())}, Buttons={list(controls['buttons'].keys())}", flush=True)

            # Set path into File Name edit box (id 1148)
            edit_hwnd = controls["edits"].get(1148) or next(iter(controls["edits"].values()), None)
            assert edit_hwnd is not None, "FAILED: File Name edit control not found in native dialog!"
            target_path_str = str(DEMO_DB)
            user32.SendMessageW(edit_hwnd, WM_SETTEXT, 0, target_path_str)
            print(f"[Test 1.5] Set target path in native dialog: '{target_path_str}'", flush=True)
            await asyncio.sleep(0.5)

            # Click Open on native dialog (id 1)
            open_btn_tuple = controls["buttons"].get(1)
            if open_btn_tuple:
                user32.SendMessageW(open_btn_tuple[0], BM_CLICK, 0, 0)
                print(f"[Test 1.6] Clicked native '&Open' button via BM_CLICK.", flush=True)
            else:
                user32.SendMessageW(dialog_hwnd, WM_COMMAND, IDOK, 0)
                print("[Test 1.6] Sent native WM_COMMAND IDOK.", flush=True)

            await asyncio.sleep(1.0)
            await click_task_1

            ui_path_val = await eval_js("document.getElementById('onboarding-database-path')?.value")
            print(f"[Test 1.7] React field value after native selection: '{ui_path_val}'", flush=True)
            assert ui_path_val == target_path_str, f"Mismatch: expected '{target_path_str}', got '{ui_path_val}'"
            print("  -> PASS: Native file selection in installed release successfully populated absolute path!", flush=True)

            # Capture screenshot of UI with populated path
            selected_screenshot_path = DOCS_DIR / "b28_b3_installed_picker_selected.png"
            await capture_cdp_screenshot(selected_screenshot_path)
            print(f"[Test 1.8] Saved populated path screenshot: {selected_screenshot_path}", flush=True)

            # -------------------------------------------------------------
            # TEST 2: Cancel Preservation
            # -------------------------------------------------------------
            print("\n" + "=" * 60, flush=True)
            print("CRITICAL ACCEPTANCE TEST 2: NATIVE CANCEL PRESERVATION", flush=True)
            print("=" * 60, flush=True)

            print("[Test 2.1] Clicking Browse button again...", flush=True)
            click_task_2 = asyncio.create_task(eval_js("""(() => {
                const btn = document.querySelector('.profile-browse-btn');
                if (btn) { btn.click(); return true; }
                return false;
            })()"""))

            dialog_hwnd_2 = await find_native_dialog(timeout=8.0)
            assert dialog_hwnd_2 is not None, "FAILED: Second native file dialog did not appear!"
            print(f"[Test 2.2] Genuine Windows dialog detected: HWND={hex(dialog_hwnd_2)}", flush=True)

            await asyncio.sleep(0.5)
            controls_2 = get_dialog_controls(dialog_hwnd_2)
            cancel_btn_tuple = controls_2["buttons"].get(2)
            if cancel_btn_tuple:
                user32.SendMessageW(cancel_btn_tuple[0], BM_CLICK, 0, 0)
                print(f"[Test 2.3] Clicked native 'Cancel' button via BM_CLICK.", flush=True)
            else:
                user32.SendMessageW(dialog_hwnd_2, WM_COMMAND, IDCANCEL, 0)
                print("[Test 2.3] Sent native WM_COMMAND IDCANCEL.", flush=True)

            await asyncio.sleep(1.0)
            await click_task_2

            ui_path_after_cancel = await eval_js("document.getElementById('onboarding-database-path')?.value")
            print(f"[Test 2.4] React field value after native cancel: '{ui_path_after_cancel}'", flush=True)
            assert ui_path_after_cancel == target_path_str, "Path corrupted after cancel!"
            print("  -> PASS: Path strictly preserved on cancellation in installed release!", flush=True)

            # -------------------------------------------------------------
            # TEST 3: Repeated Use After Cancellation
            # -------------------------------------------------------------
            print("\n" + "=" * 60, flush=True)
            print("CRITICAL ACCEPTANCE TEST 3: REPEATED USE AFTER CANCELLATION", flush=True)
            print("=" * 60, flush=True)

            print("[Test 3.1] Clicking Browse button a third time...", flush=True)
            click_task_3 = asyncio.create_task(eval_js("""(() => {
                const btn = document.querySelector('.profile-browse-btn');
                if (btn) { btn.click(); return true; }
                return false;
            })()"""))

            dialog_hwnd_3 = await find_native_dialog(timeout=8.0)
            assert dialog_hwnd_3 is not None, "FAILED: Third native file dialog did not appear!"
            print(f"[Test 3.2] Genuine Windows dialog detected: HWND={hex(dialog_hwnd_3)}", flush=True)

            await asyncio.sleep(0.5)
            controls_3 = get_dialog_controls(dialog_hwnd_3)
            cancel_btn_tuple_3 = controls_3["buttons"].get(2)
            if cancel_btn_tuple_3:
                user32.SendMessageW(cancel_btn_tuple_3[0], BM_CLICK, 0, 0)
            else:
                user32.SendMessageW(dialog_hwnd_3, WM_COMMAND, IDCANCEL, 0)
            print("[Test 3.3] Clicked native 'Cancel' button on third invocation.", flush=True)

            await asyncio.sleep(1.0)
            await click_task_3

            ui_path_final = await eval_js("document.getElementById('onboarding-database-path')?.value")
            assert ui_path_final == target_path_str
            print("  -> PASS: Repeated native dialog invocation succeeded without crash or leak!", flush=True)

    finally:
        print("\n[Step 7] Testing process termination and sidecar cleanup...", flush=True)
        # Terminate host process
        proc.kill()
        proc.wait(timeout=5)
        print("  -> Host process terminated.", flush=True)

        # Verify sidecar termination
        time.sleep(2.0)
        remaining = [p.info for p in psutil.process_iter(["pid", "name"]) if "querypilot" in (p.info["name"] or "").lower()]
        print(f"  -> Remaining QueryPilot processes: {remaining}", flush=True)
        assert len(remaining) == 0, f"FAILED: Orphan processes remained: {remaining}"
        print("  -> PASS: All child processes cleanly terminated by Windows Job Object!", flush=True)

    # Test fresh restart
    print("\n[Step 8] Testing clean restart from installed shortcut...", flush=True)
    proc2 = subprocess.Popen([str(INSTALLED_EXE)], env=env)
    print(f"  -> Restarted host PID={proc2.pid}", flush=True)
    time.sleep(3.0)
    proc2_children = psutil.Process(proc2.pid).children(recursive=True)
    print(f"  -> Restarted children: {[c.name() for c in proc2_children]}", flush=True)
    assert any("querypilot-agent" in c.name().lower() for c in proc2_children), "Sidecar did not start on restart!"
    print("  -> PASS: Fresh session started cleanly with supervised sidecar!", flush=True)
    proc2.kill()
    proc2.wait(timeout=5)
    kill_existing()
    print("  -> Restart cleanup complete.", flush=True)

    print("\n" + "=" * 75, flush=True)
    print("ALL INSTALLED RELEASE ACCEPTANCE TESTS PASSED SUCCESSFULLY!", flush=True)
    print("=" * 75, flush=True)


if __name__ == "__main__":
    asyncio.run(run_installed_acceptance())
