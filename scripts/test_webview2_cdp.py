"""Automated WebView2 integration & automatic authentication verification for QueryPilot Desktop (B26-B4).

Tests:
1. Spawns querypilot.exe with WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS remote debugging enabled.
2. Handshake verified (Tauri bound port and child PID detected).
3. Connects to WebView2 Chrome DevTools Protocol (CDP) endpoint over WebSocket.
4. Verifies session cookie `qpt_session` is injected into WebView2 with strict security attributes:
   - name: qpt_session
   - domain: 127.0.0.1
   - path: /
   - httpOnly: True
   - secure: False
   - sameSite: Strict
   - session: True
5. Verifies bootstrap token file on disk was consumed and wiped (single-use guarantee).
6. Verifies document title is 'QueryPilot' and URL is active loopback origin.
7. Verifies React root DOM element exists and contains mounted component tree.
8. Verifies initial session probe succeeded automatically:
   - Manual bootstrap modal (#bootstrap-token-input) is NOT displayed.
   - UI shows authenticated state or database profiles.
9. Verifies external navigation behavior (intercepted safely).
10. Verifies clean shutdown and sidecar termination.
11. Relaunch test: Launches application a second time, verifying a fresh session is established automatically.
"""

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
import urllib.error

try:
    import websockets
except ImportError:
    print("Error: websockets library not installed. Run 'pip install websockets'")
    sys.exit(1)

TAURI_EXE = os.path.abspath("src-tauri/target/debug/querypilot.exe")
CDP_PORT = 9225

def check_pid_alive(pid: int) -> bool:
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            exit_code = ctypes.c_ulong()
            kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            kernel32.CloseHandle(handle)
            return exit_code.value == STILL_ACTIVE
        return False
    except Exception:
        return False

async def cdp_call(ws, method, params=None, req_id=1):
    msg = {"id": req_id, "method": method, "params": params or {}}
    await ws.send(json.dumps(msg))
    while True:
        resp = await ws.recv()
        data = json.loads(resp)
        if data.get("id") == req_id:
            return data.get("result", {})

async def cdp_eval(ws, expression, req_id):
    result = await cdp_call(ws, "Runtime.evaluate", {
        "expression": expression,
        "returnByValue": True,
        "awaitPromise": True,
    }, req_id=req_id)
    return result.get("result", {}).get("value")

async def run_desktop_test(is_relaunch: bool = False):
    phase = "RELAUNCH" if is_relaunch else "INITIAL LAUNCH"
    print(f"\n=======================================================")
    print(f"STARTING {phase} TEST (B26-B4 Automatic Authentication)")
    print(f"=======================================================")

    env = os.environ.copy()
    env["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = f"--remote-debugging-port={CDP_PORT}"
    env["QUERYPILOT_NO_POPUP"] = "1"

    print(f"Spawning QueryPilot desktop: {TAURI_EXE}")
    proc = subprocess.Popen(
        [TAURI_EXE],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        bufsize=1,
    )

    sidecar_port = None
    sidecar_pid = None

    try:
        # Step 1: Wait for handshake
        start_time = time.time()
        while time.time() - start_time < 20:
            line = proc.stdout.readline()
            if line:
                line_str = line.strip()
                print(f"[Desktop Stdout] {line_str}")
                if "[QueryPilot] Sidecar running on port" in line_str:
                    parts = line_str.split("port ")[1].split(" ")
                    sidecar_port = int(parts[0])
                    pid_str = parts[1].replace("(PID=", "").replace(")", "").strip()
                    sidecar_pid = int(pid_str)
                    break
            elif proc.poll() is not None:
                err = proc.stderr.read()
                raise RuntimeError(f"Tauri exited prematurely (code {proc.returncode}): {err}")
            time.sleep(0.05)

        if not sidecar_port or not sidecar_pid:
            raise RuntimeError("Timed out waiting for sidecar startup handshake")

        print(f"[PASS] Sidecar handshake verified on port {sidecar_port}, PID {sidecar_pid}")

        # Step 2: Poll CDP endpoint
        cdp_json_url = f"http://127.0.0.1:{CDP_PORT}/json"
        ws_url = None
        cdp_start = time.time()
        while time.time() - cdp_start < 15:
            try:
                with urllib.request.urlopen(cdp_json_url, timeout=2) as response:
                    targets = json.loads(response.read().decode())
                    for t in targets:
                        if t.get("type") == "page":
                            ws_url = t.get("webSocketDebuggerUrl")
                            print(f"Found WebView2 page target: {t.get('title')} - {t.get('url')}")
                            break
                    if ws_url:
                        break
            except Exception:
                pass
            time.sleep(0.2)

        if not ws_url:
            raise RuntimeError(f"Could not connect to WebView2 CDP on {cdp_json_url}")

        print(f"[PASS] Connected to WebView2 CDP: {ws_url}")

        # Step 3: Connect WebSocket and inspect cookie jar and DOM
        async with websockets.connect(ws_url) as ws:
            req_id = 1

            # 3a. Wait for navigation to complete away from about:blank to local app
            expected_prefix = f"http://127.0.0.1:{sidecar_port}/app"
            navigated = False
            for _ in range(50):
                current_url = await cdp_eval(ws, "window.location.href", req_id)
                req_id += 1
                if current_url and current_url.startswith(expected_prefix):
                    navigated = True
                    break
                await asyncio.sleep(0.2)

            assert navigated, f"WebView did not navigate to {expected_prefix}, remained at {current_url}"
            print(f"[PASS] WebView navigated to expected origin: {current_url}")

            # 3b. Verify session cookie injected via CDP Network domain
            print("Inspecting WebView2 cookie manager via CDP...")
            cookies_res = await cdp_call(ws, "Network.getCookies", {
                "urls": [f"http://127.0.0.1:{sidecar_port}/"]
            }, req_id=req_id)
            req_id += 1

            cookies = cookies_res.get("cookies", [])
            session_cookie = None
            for c in cookies:
                if c.get("name") == "qpt_session":
                    session_cookie = c
                    break

            assert session_cookie is not None, f"Expected 'qpt_session' cookie in WebView2 cookie jar, found: {[c.get('name') for c in cookies]}"
            print(f"[PASS] Verified 'qpt_session' cookie present in WebView2 cookie jar")
            print(f"       Domain: {session_cookie.get('domain')}")
            print(f"       Path: {session_cookie.get('path')}")
            print(f"       HttpOnly: {session_cookie.get('httpOnly')}")
            print(f"       Secure: {session_cookie.get('secure')}")
            print(f"       SameSite: {session_cookie.get('sameSite')}")
            print(f"       Session: {session_cookie.get('session')}")

            assert session_cookie.get("domain") in ("127.0.0.1", ".127.0.0.1"), f"Unexpected cookie domain: {session_cookie.get('domain')}"
            assert session_cookie.get("path") == "/", f"Unexpected cookie path: {session_cookie.get('path')}"
            assert session_cookie.get("httpOnly") is True, "Expected httpOnly to be True"
            assert session_cookie.get("secure") is False, "Expected secure to be False for loopback HTTP"
            assert session_cookie.get("sameSite") == "Strict", f"Expected SameSite Strict, got {session_cookie.get('sameSite')}"
            assert session_cookie.get("session") is True, "Expected session-only lifetime"

            # 3c. Verify token file was consumed from disk (single-use invariant)
            token_file = Path.home() / ".query_pilot" / "agent.token"
            assert not token_file.exists(), f"Bootstrap token file '{token_file}' was not deleted upon authentication!"
            print(f"[PASS] Bootstrap token file '{token_file}' was successfully consumed and removed from disk")

            # 3d. Verify document.title
            title = None
            for _ in range(30):
                title = await cdp_eval(ws, "document.title", req_id)
                req_id += 1
                if title == "QueryPilot":
                    break
                await asyncio.sleep(0.2)

            print(f"Document title: '{title}'")
            assert title == "QueryPilot", f"Expected title 'QueryPilot', got '{title}'"
            print("[PASS] Document title is 'QueryPilot'")

            # 3e. Wait for React to mount into #root
            root_mounted = False
            for _ in range(30):
                child_count = await cdp_eval(ws, "document.getElementById('root') ? document.getElementById('root').children.length : 0", req_id)
                req_id += 1
                if child_count and child_count > 0:
                    root_mounted = True
                    break
                await asyncio.sleep(0.3)

            assert root_mounted, "React did not mount any children into #root"
            print(f"[PASS] React successfully mounted into #root (child elements: {child_count})")

            # 3f. VERIFY AUTOMATIC AUTHENTICATION: Ensure manual bootstrap modal is NOT displayed
            # Give React a moment to complete initial session probe
            await asyncio.sleep(1.0)
            token_input = await cdp_eval(ws, "document.getElementById('bootstrap-token-input') !== null", req_id)
            req_id += 1
            auth_modal = await cdp_eval(ws, "document.querySelector('.agent-auth-modal') !== null", req_id)
            req_id += 1

            assert not token_input, "Manual bootstrap token input was displayed! Automatic authentication failed."
            assert not auth_modal, "Manual bootstrap modal was displayed! Automatic authentication failed."
            print("[PASS] ZERO MANUAL INTERVENTION: Bootstrap token input modal was NOT shown!")

            # 3g. Inspect rendered UI text
            body_text = await cdp_eval(ws, "document.body.innerText", req_id)
            req_id += 1
            first_few_lines = [l.strip() for l in body_text.splitlines() if l.strip()][:5]
            safe_lines = [l.encode("ascii", errors="replace").decode() for l in first_few_lines]
            print(f"Rendered UI content sample: {safe_lines}")
            assert len(body_text.strip()) > 0, "Rendered body text is empty"
            print(f"[PASS] {phase} UI screen rendered successfully in authenticated state")

            # 3h. External link navigation safety check
            print("Testing external navigation interception...")
            await cdp_call(ws, "Runtime.evaluate", {
                "expression": "window.location.href = 'https://example.com/'",
            }, req_id=req_id)
            req_id += 1
            await asyncio.sleep(0.5)

            after_url = await cdp_eval(ws, "window.location.href", req_id)
            req_id += 1
            assert "example.com" not in after_url
            assert after_url.startswith(f"http://127.0.0.1:{sidecar_port}/")
            print("[PASS] External navigation was blocked inside the WebView and preserved local origin")

    finally:
        print("\nShutting down desktop shell...")
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

        # Step 4: Verify sidecar terminated via Job Object
        if sidecar_pid:
            time.sleep(1.0)
            assert not check_pid_alive(sidecar_pid), f"Sidecar PID {sidecar_pid} is still running after shell termination!"
            print(f"[PASS] Sidecar PID {sidecar_pid} cleanly terminated on desktop shell exit")

async def test_cdp_isolation():
    print(f"\n=======================================================")
    print("STARTING CDP TEST ISOLATION VERIFICATION (B26-B4.1)")
    print("=======================================================")

    env = os.environ.copy()
    # Explicitly ensure NO remote debugging arguments are passed
    env.pop("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", None)
    env["QUERYPILOT_NO_POPUP"] = "1"

    print(f"Spawning QueryPilot desktop without debugging env: {TAURI_EXE}")
    proc = subprocess.Popen(
        [TAURI_EXE],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        bufsize=1,
    )

    sidecar_pid = None
    try:
        start_time = time.time()
        while time.time() - start_time < 15:
            line = proc.stdout.readline()
            if line:
                line_str = line.strip()
                if "[QueryPilot] Sidecar running on port" in line_str:
                    parts = line_str.split("PID=")[1].replace(")", "").strip()
                    sidecar_pid = int(parts)
                    break
            elif proc.poll() is not None:
                raise RuntimeError(f"Tauri exited prematurely: {proc.stderr.read()}")
            time.sleep(0.05)

        assert sidecar_pid is not None, "Failed to capture sidecar PID"

        # Give WebView2 time to initialize
        await asyncio.sleep(2.0)

        # Verify CDP endpoints are CLOSED
        for port in [CDP_PORT, 9222]:
            cdp_url = f"http://127.0.0.1:{port}/json"
            try:
                with urllib.request.urlopen(cdp_url, timeout=1.0) as _:
                    raise AssertionError(f"SECURITY VIOLATION: Remote debugging port {port} was reachable in default launch mode!")
            except (urllib.error.URLError, ConnectionRefusedError, TimeoutError, OSError):
                # Expected: port is not listening
                pass

        print("[PASS] Verified remote debugging endpoints (9222, 9225) are completely CLOSED by default.")
        print("[PASS] Remote debugging is strictly isolated to explicit test harness executions.")

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        if sidecar_pid:
            time.sleep(1.0)
            assert not check_pid_alive(sidecar_pid)

async def main():
    # 1. Initial launch and automatic authentication test
    await run_desktop_test(is_relaunch=False)

    # Brief delay between launches
    await asyncio.sleep(1.5)

    # 2. Relaunch test (proves fresh session established, no persisted secrets)
    await run_desktop_test(is_relaunch=True)

    # Brief delay
    await asyncio.sleep(1.5)

    # 3. CDP isolation test (verifies default release behavior does NOT expose debugging endpoint)
    await test_cdp_isolation()

    print("\n=======================================================")
    print("ALL B26-B4 & B26-B4.1 CHECKS PASSED CLEANLY!")
    print("=======================================================\n")

if __name__ == "__main__":
    asyncio.run(main())
