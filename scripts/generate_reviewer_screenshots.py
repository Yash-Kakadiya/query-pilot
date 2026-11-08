"""
Script to capture all visual evaluation screenshots for QueryPilot 0.2.0 Internal Reviewer Report.
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
import time
import urllib.request
import psutil
import websockets

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = REPO_ROOT / "docs"
LOCAL_APPDATA = Path(os.environ["LOCALAPPDATA"])
INSTALLED_EXE = LOCAL_APPDATA / "QueryPilot" / "querypilot.exe"
DEMO_DB = (REPO_ROOT / "release" / "QueryPilot-0.2.0-reviewer" / "university_demo.db").resolve()
CDP_PORT = 9228

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
WM_COMMAND = 0x0111
IDCANCEL = 2


def kill_existing():
    for p in psutil.process_iter(["name"]):
        try:
            if p.info["name"] in ("querypilot.exe", "querypilot-agent.exe"):
                p.kill()
        except Exception:
            pass
    time.sleep(1)


async def find_native_dialog(timeout: float = 8.0) -> int | None:
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
        print(f"Captured native dialog screenshot: {output_path} ({output_path.stat().st_size} bytes)")
    except Exception as e:
        print(f"Failed to capture native dialog screenshot: {e}")


async def run_capture():
    kill_existing()
    env = os.environ.copy()
    env["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = f"--remote-debugging-port={CDP_PORT}"
    env["QUERYPILOT_NO_POPUP"] = "1"
    env.pop("QUERYPILOT_UPSTREAM_API_URL", None)

    proc = subprocess.Popen([str(INSTALLED_EXE)], env=env)
    print(f"Spawned QueryPilot PID={proc.pid}")
    time.sleep(3)

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json", timeout=5) as r:
            targets = json.loads(r.read().decode())
        ws_url = next(t["webSocketDebuggerUrl"] for t in targets if t.get("type") == "page")
        print(f"CDP URL: {ws_url}")

        async with websockets.connect(ws_url) as ws:
            msg_id = 0

            async def eval_js(expr):
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
                        print(f"Saved: {path.name} ({len(raw)} bytes)")
                        return

            for _ in range(30):
                href = await eval_js("window.location.href")
                if href and "/app/" in href:
                    break
                await asyncio.sleep(0.5)

            await asyncio.sleep(1.5)

            # 1. First launch
            await screenshot(DOCS_DIR / "eval_01_first_launch.png")

            # 2. Open Add Profile Modal
            await eval_js("document.querySelector('button.profile-add-header-btn')?.click()")
            await asyncio.sleep(1.0)
            await screenshot(DOCS_DIR / "eval_02_onboarding_modal.png")

            # 3. Toggle SQLite
            await eval_js("""
                const btns = Array.from(document.querySelectorAll('button.engine-toggle-btn'));
                btns.find(b => b.textContent.includes('SQLite'))?.click();
            """)
            await asyncio.sleep(0.5)
            await screenshot(DOCS_DIR / "eval_03_sqlite_form.png")

            # 4. Click Browse and capture native dialog
            await eval_js("document.querySelector('button.profile-browse-btn')?.click()")
            await asyncio.sleep(1.0)
            hwnd = await find_native_dialog()
            if hwnd:
                capture_window_screenshot(hwnd, DOCS_DIR / "eval_04_native_picker.png")
                # Cancel dialog
                user32.PostMessageW(hwnd, WM_COMMAND, IDCANCEL, 0)
                await asyncio.sleep(1.0)

            # Close modal by clicking cancel
            await eval_js("document.querySelector('.profile-modal-close-btn, .profile-modal-cancel-btn')?.click()")
            await asyncio.sleep(1.0)

            # 5. Select 'university-review-demo'
            await eval_js("""
                const select = document.querySelector('select');
                if (select) {
                    select.value = 'university-review-demo';
                    select.dispatchEvent(new Event('change', { bubbles: true }));
                }
            """)
            await asyncio.sleep(2.0)
            await screenshot(DOCS_DIR / "eval_05_schema_introspected.png")

            # 6. Attempt offline AI generation
            await eval_js("""
                const ta = document.getElementById('candidate-question-input');
                if (ta) {
                    const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
                    setter.call(ta, 'List all computer science courses with at least 3 credits');
                    ta.dispatchEvent(new Event('input', { bubbles: true }));
                }
            """)
            await eval_js("document.querySelector('button.candidate-submit-btn')?.click()")
            await asyncio.sleep(3.5)
            await screenshot(DOCS_DIR / "eval_06_offline_ai_attempt.png")
            await screenshot(DOCS_DIR / "eval_07_offline_sql_editor_gating.png")

    finally:
        kill_existing()


if __name__ == "__main__":
    asyncio.run(run_capture())

