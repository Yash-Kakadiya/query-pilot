"""
B28-A.4: Automated End-to-End Verification of Native Windows File Picker
Exercises genuine Win32 file dialog opened by Tauri v2 plugin-dialog in QueryPilot.
"""

import asyncio
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
TAURI_EXE = REPO_ROOT / "src-tauri" / "target" / "debug" / "querypilot.exe"
DEMO_DB = (REPO_ROOT / "data" / "demo" / "university_demo.db").resolve()
CDP_PORT = 9228

user32 = ctypes.windll.user32
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
    """Locate the genuine Windows File Open dialog window by title asynchronously."""
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
    """Enumerate child controls of the native Windows file dialog."""
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


async def main():
    print("=" * 75, flush=True)
    print("B28-A.4: ACTUAL WINDOWS NATIVE FILE DIALOG RUNTIME VERIFICATION", flush=True)
    print(f"Target Binary: {TAURI_EXE}", flush=True)
    print(f"Demo DB:       {DEMO_DB}", flush=True)
    print("=" * 75, flush=True)

    assert TAURI_EXE.is_file(), f"Target binary not found: {TAURI_EXE}"
    assert DEMO_DB.is_file(), f"Demo database not found: {DEMO_DB}"

    kill_existing()

    env = os.environ.copy()
    env["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = f"--remote-debugging-port={CDP_PORT}"
    env["QUERYPILOT_NO_POPUP"] = "1"

    proc = subprocess.Popen([str(TAURI_EXE)], cwd=str(REPO_ROOT), env=env)
    print(f"[1] Launched desktop process PID={proc.pid}", flush=True)

    try:
        # Connect to CDP
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
            time.sleep(0.5)

        assert ws_url, f"Failed to connect to WebView2 CDP on port {CDP_PORT}"
        print(f"[2] Connected to WebView2 CDP: {ws_url}", flush=True)

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

            # Wait for app load
            print("[3] Waiting for frontend app rendering...", flush=True)
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
            print("[4] Opening Profile Onboarding Modal...", flush=True)
            open_res = await eval_js("""(() => {
                const btn = document.querySelector('.profile-add-header-btn') || 
                            Array.from(document.querySelectorAll('button')).find(b => b.innerText && b.innerText.includes('Add Profile'));
                if (btn) { btn.click(); return true; }
                return false;
            })()""")
            assert open_res, "Failed to click '+ Add Profile' button"
            await asyncio.sleep(1.0)

            # Switch engine to SQLite
            print("[5] Switching engine to SQLite...", flush=True)
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
            # TEST 1: Open and Select
            # -------------------------------------------------------------
            print("\n" + "=" * 60, flush=True)
            print("TEST 1: OPEN AND SELECT NATIVE FILE", flush=True)
            print("=" * 60, flush=True)

            print("[Test 1.1] Clicking Browse button to trigger native Windows dialog...", flush=True)
            click_task_1 = asyncio.create_task(eval_js("""(() => {
                const btn = document.querySelector('.profile-browse-btn');
                if (btn) { btn.click(); return true; }
                return false;
            })()"""))

            # Detect genuine Windows dialog
            dialog_hwnd = await find_native_dialog(timeout=8.0)
            assert dialog_hwnd is not None, "FAILED: Windows native file dialog did not appear!"
            print(f"[Test 1.2] Genuine Windows dialog detected: HWND={hex(dialog_hwnd)} ({dialog_hwnd})", flush=True)

            # Wait for child controls to initialize
            await asyncio.sleep(0.5)
            controls = get_dialog_controls(dialog_hwnd)
            print(f"[Test 1.3] Found controls: Edits={list(controls['edits'].keys())}, Buttons={list(controls['buttons'].keys())}", flush=True)
            
            # Locate file name edit box (id 1148 in Win32 File Open dialog) or first edit
            edit_hwnd = controls["edits"].get(1148) or next(iter(controls["edits"].values()), None)
            assert edit_hwnd is not None, "FAILED: File Name edit control not found in native dialog!"

            # Set the exact path into the native File Name edit box
            target_path_str = str(DEMO_DB)
            user32.SendMessageW(edit_hwnd, WM_SETTEXT, 0, target_path_str)
            print(f"[Test 1.4] Set path in native dialog: '{target_path_str}'", flush=True)
            await asyncio.sleep(0.5)

            # Click Open on the native dialog (id 1 = IDOK)
            open_btn_tuple = controls["buttons"].get(1)
            if open_btn_tuple:
                open_btn_hwnd = open_btn_tuple[0]
                user32.SendMessageW(open_btn_hwnd, BM_CLICK, 0, 0)
                print(f"[Test 1.5] Clicked native '&Open' button (HWND={hex(open_btn_hwnd)}) via BM_CLICK.", flush=True)
            else:
                user32.SendMessageW(dialog_hwnd, WM_COMMAND, IDOK, 0)
                print("[Test 1.5] Sent native WM_COMMAND IDOK.", flush=True)

            # Wait for dialog to close and JS promise to resolve
            await asyncio.sleep(1.0)
            await click_task_1

            # Verify the path field in React UI
            ui_path_val = await eval_js("document.getElementById('onboarding-database-path')?.value")
            print(f"[Test 1.6] React field value after native selection: '{ui_path_val}'", flush=True)
            assert ui_path_val == target_path_str, f"Mismatch: expected '{target_path_str}', got '{ui_path_val}'"
            print("  -> PASS: Native file selection populated genuine absolute path!", flush=True)

            # -------------------------------------------------------------
            # TEST 2: Native Cancel Preservation
            # -------------------------------------------------------------
            print("\n" + "=" * 60, flush=True)
            print("TEST 2: NATIVE CANCEL PRESERVATION", flush=True)
            print("=" * 60, flush=True)

            print("[Test 2.1] Clicking Browse button again...", flush=True)
            click_task_2 = asyncio.create_task(eval_js("""(() => {
                const btn = document.querySelector('.profile-browse-btn');
                if (btn) { btn.click(); return true; }
                return false;
            })()"""))

            dialog_hwnd_2 = await find_native_dialog(timeout=8.0)
            assert dialog_hwnd_2 is not None, "FAILED: Second native file dialog did not appear!"
            print(f"[Test 2.2] Genuine Windows dialog detected: HWND={hex(dialog_hwnd_2)} ({dialog_hwnd_2})", flush=True)

            await asyncio.sleep(0.5)
            controls_2 = get_dialog_controls(dialog_hwnd_2)
            
            # Click Cancel on the native dialog (id 2 = IDCANCEL)
            cancel_btn_tuple = controls_2["buttons"].get(2)
            if cancel_btn_tuple:
                cancel_btn_hwnd = cancel_btn_tuple[0]
                user32.SendMessageW(cancel_btn_hwnd, BM_CLICK, 0, 0)
                print(f"[Test 2.3] Clicked native 'Cancel' button (HWND={hex(cancel_btn_hwnd)}) via BM_CLICK.", flush=True)
            else:
                user32.SendMessageW(dialog_hwnd_2, WM_COMMAND, IDCANCEL, 0)
                print("[Test 2.3] Sent native WM_COMMAND IDCANCEL.", flush=True)

            await asyncio.sleep(1.0)
            await click_task_2

            # Verify path value is preserved unchanged
            ui_path_after_cancel = await eval_js("document.getElementById('onboarding-database-path')?.value")
            print(f"[Test 2.4] React field value after native cancel: '{ui_path_after_cancel}'", flush=True)
            assert ui_path_after_cancel == target_path_str, (
                f"Path corrupted after cancel! Expected '{target_path_str}', got '{ui_path_after_cancel}'"
            )
            print("  -> PASS: Previous path was strictly preserved upon native cancellation!", flush=True)

            # -------------------------------------------------------------
            # TEST 3: Repeated Use After Cancellation
            # -------------------------------------------------------------
            print("\n" + "=" * 60, flush=True)
            print("TEST 3: REPEATED USE AFTER CANCELLATION", flush=True)
            print("=" * 60, flush=True)

            print("[Test 3.1] Clicking Browse button a third time...", flush=True)
            click_task_3 = asyncio.create_task(eval_js("""(() => {
                const btn = document.querySelector('.profile-browse-btn');
                if (btn) { btn.click(); return true; }
                return false;
            })()"""))

            dialog_hwnd_3 = await find_native_dialog(timeout=8.0)
            assert dialog_hwnd_3 is not None, "FAILED: Third native file dialog did not appear!"
            print(f"[Test 3.2] Genuine Windows dialog detected: HWND={hex(dialog_hwnd_3)} ({dialog_hwnd_3})", flush=True)

            await asyncio.sleep(0.5)
            controls_3 = get_dialog_controls(dialog_hwnd_3)
            
            # Click Cancel again to dismiss cleanly
            cancel_btn_tuple_3 = controls_3["buttons"].get(2)
            if cancel_btn_tuple_3:
                user32.SendMessageW(cancel_btn_tuple_3[0], BM_CLICK, 0, 0)
                print("[Test 3.3] Clicked native 'Cancel' button on third invocation.", flush=True)
            else:
                user32.SendMessageW(dialog_hwnd_3, WM_COMMAND, IDCANCEL, 0)
                print("[Test 3.3] Sent native WM_COMMAND IDCANCEL.", flush=True)

            await asyncio.sleep(1.0)
            await click_task_3

            ui_path_final = await eval_js("document.getElementById('onboarding-database-path')?.value")
            print(f"[Test 3.4] React field value after third invocation: '{ui_path_final}'", flush=True)
            assert ui_path_final == target_path_str, (
                f"Path changed unexpectedly! Expected '{target_path_str}', got '{ui_path_final}'"
            )
            print("  -> PASS: Repeated invocation works normally without regression!", flush=True)

            print("\n" + "=" * 75, flush=True)
            print("ALL 3 NATIVE WINDOWS FILE DIALOG TESTS PASSED SUCCESSFULLY!", flush=True)
            print("=" * 75, flush=True)

    finally:
        print("\nTerminating desktop process...", flush=True)
        proc.kill()
        kill_existing()
        print("Done.", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
