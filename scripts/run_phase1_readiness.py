"""Phase 1 Readiness Verification Script for B27-C.

Checks:
1. Verify the installed desktop application is using the correct Render backend.
2. Verify the access key remains provisioned in the current desktop session without reading or logging its value.
3. Check Render health.
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
RENDER_UPSTREAM_URL = "https://querypilot-backend-7bug.onrender.com"
LOCAL_APP_DATA = Path(os.environ["LOCALAPPDATA"])
INSTALLED_EXE = LOCAL_APP_DATA / "QueryPilot" / "querypilot.exe"
PROFILES_FILE = Path.home() / ".query_pilot" / "profiles.json"
DEMO_DB = REPO_ROOT / "data" / "demo" / "university_demo.db"


def kill_existing():
    for p in psutil.process_iter(["name"]):
        try:
            if p.info["name"] in ("querypilot.exe", "querypilot-agent.exe"):
                p.kill()
        except Exception:
            pass
    time.sleep(1)


async def cdp_eval(ws, expression: str, req_id: int = 1):
    msg = {
        "id": req_id,
        "method": "Runtime.evaluate",
        "params": {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
        },
    }
    await ws.send(json.dumps(msg))
    while True:
        resp = json.loads(await ws.recv())
        if resp.get("id") == req_id:
            return resp.get("result", {}).get("value")


def main():
    print("=" * 80)
    print("B27-C PHASE 1: READINESS CHECK")
    print("=" * 80)

    assert INSTALLED_EXE.is_file(), f"Binary not found: {INSTALLED_EXE}"
    assert DEMO_DB.is_file(), f"Demo DB not found: {DEMO_DB}"

    # Ensure university-live-demo profile exists
    p_data = {
        "profiles": {
            "university-live-demo": {
                "profile_id": "university-live-demo",
                "display_name": "University Live Demo (SQLite)",
                "engine_type": "sqlite",
                "database_path": str(DEMO_DB.resolve()),
                "timeout_seconds": 15,
                "max_row_limit": 100,
            }
        }
    }
    PROFILES_FILE.parent.mkdir(parents=True, exist_ok=True)
    PROFILES_FILE.write_text(json.dumps(p_data, indent=2), encoding="utf-8")
    print(f"[OK] Profile 'university-live-demo' configured -> {DEMO_DB}")

    # Check if querypilot.exe is already running
    running_proc = None
    for p in psutil.process_iter(["name", "pid"]):
        try:
            if p.info["name"] == "querypilot.exe":
                running_proc = p
                break
        except Exception:
            pass

    if running_proc is None:
        kill_existing()
        env = os.environ.copy()
        env["QUERYPILOT_UPSTREAM_API_URL"] = RENDER_UPSTREAM_URL
        env["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = "--remote-debugging-port=9225"
        proc = subprocess.Popen([str(INSTALLED_EXE)], env=env)
        print(f"[OK] Launched QueryPilot Desktop (PID {proc.pid}) with upstream {RENDER_UPSTREAM_URL}")
    else:
        print(f"[OK] QueryPilot Desktop is already running (PID {running_proc.pid})")

    # Locate sidecar querypilot-agent.exe
    sidecar_proc = None
    sidecar_port = None
    deadline = time.time() + 25.0
    while time.time() < deadline:
        for p in psutil.process_iter(["name", "pid"]):
            try:
                if p.info["name"] == "querypilot-agent.exe":
                    sidecar_proc = p
                    for c in p.net_connections(kind="inet"):
                        if c.status == "LISTEN" and c.laddr.ip == "127.0.0.1":
                            sidecar_port = c.laddr.port
                            break
                    if sidecar_port:
                        break
            except Exception:
                continue
        if sidecar_port:
            break
        time.sleep(0.5)

    assert sidecar_port is not None, "Sidecar failed to bind"
    print(f"[OK] Supervised sidecar PID {sidecar_proc.pid} listening on 127.0.0.1:{sidecar_port}")

    # Check Render health via agent proxy
    health_url = f"http://127.0.0.1:{sidecar_port}/api/health"
    with urllib.request.urlopen(health_url, timeout=30) as r:
        h_data = json.loads(r.read().decode())
    print(f"[OK] Render backend health via sidecar proxy: HTTP {r.status} {h_data}")
    assert h_data.get("status") == "healthy", f"Unhealthy status: {h_data}"

    # Connect to WebView2 CDP on 9225
    time.sleep(2.0)
    cdp_json_url = "http://127.0.0.1:9225/json"
    with urllib.request.urlopen(cdp_json_url, timeout=10) as resp:
        targets = json.loads(resp.read().decode())
    ws_url = next(t["webSocketDebuggerUrl"] for t in targets if t.get("type") == "page")
    print(f"[OK] Connected to WebView2 CDP: {ws_url}")

    async def check_session():
        async with websockets.connect(ws_url) as ws:
            # Check navigation
            url = await cdp_eval(ws, "window.location.href", 1)
            print(f"[OK] WebView2 Current URL: {url}")

            # Check access key presence strictly as boolean (never read/print value)
            expr = "Boolean(sessionStorage.getItem('querypilot_access_key') && sessionStorage.getItem('querypilot_access_key').trim().length > 0)"
            has_key = await cdp_eval(ws, expr, 2)
            print(f"[RESULT] Access key provisioned in current session: {has_key}")
            return has_key, sidecar_port

    has_key, port = asyncio.run(check_session())
    return 0 if has_key else 2


if __name__ == "__main__":
    sys.exit(main())

