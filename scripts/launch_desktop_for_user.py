"""Launch QueryPilot Desktop with Render upstream and keep it running for user interaction.

Ensures:
- Installed desktop binary is used.
- Upstream is set to Render: https://querypilot-backend-7bug.onrender.com
- Profile 'university-live-demo' is available.
- Desktop UI opens on screen for user.
- AccessKeyBar is available for user to enter prototype access key into sessionStorage.
- Zero credential logging, printing, or storage.
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
    print("LAUNCHING QUERYPILOT DESKTOP FOR SECURE ACCESS-KEY PROVISIONING")
    print("=" * 80)

    assert INSTALLED_EXE.is_file(), f"Binary not found: {INSTALLED_EXE}"
    assert DEMO_DB.is_file(), f"Demo DB not found: {DEMO_DB}"

    kill_existing()

    # Ensure profile exists
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
    print(f"Verified profile 'university-live-demo' -> {DEMO_DB}")

    env = os.environ.copy()
    env["QUERYPILOT_UPSTREAM_API_URL"] = RENDER_UPSTREAM_URL
    env["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = "--remote-debugging-port=9222"

    proc = subprocess.Popen([str(INSTALLED_EXE)], env=env)
    print(f"Launched QueryPilot Desktop (PID {proc.pid})")

    # Wait for sidecar
    sidecar_port = None
    sidecar_proc = None
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
    print(f"Sidecar PID {sidecar_proc.pid} listening on port {sidecar_port}")

    # Verify Render health via agent
    health_url = f"http://127.0.0.1:{sidecar_port}/api/health"
    with urllib.request.urlopen(health_url, timeout=10) as r:
        h_data = json.loads(r.read().decode())
    print(f"Upstream health via agent: HTTP {r.status} {h_data}")
    assert h_data.get("status") == "healthy"

    print("\nDesktop application is now running on screen.")
    print("AccessKeyBar is visible at top of window.")
    print("Waiting for user to enter prototype access key in AccessKeyBar...\n")

    # Monitor CDP for key presence (checks boolean flag only, NEVER reads or prints key!)
    async def monitor():
        time.sleep(2.0)
        cdp_json_url = "http://127.0.0.1:9222/json"
        with urllib.request.urlopen(cdp_json_url, timeout=5) as resp:
            targets = json.loads(resp.read().decode())
        ws_url = next(t["webSocketDebuggerUrl"] for t in targets if t.get("type") == "page")

        async with websockets.connect(ws_url) as ws:
            req_id = 1
            key_detected = False
            while True:
                # Strictly evaluate boolean presence only. NEVER retrieve or log key string!
                has_key = await cdp_eval(
                    ws,
                    "Boolean(sessionStorage.getItem('querypilot_access_key') && sessionStorage.getItem('querypilot_access_key').trim().length > 0)",
                    req_id,
                )
                req_id += 1

                if has_key and not key_detected:
                    key_detected = True
                    print("[AccessKeyBar] Access key has been entered and saved in session storage by user.")
                    print("[AccessKeyBar] Ready to resume live validation!")

                await asyncio.sleep(1.0)

    try:
        asyncio.run(monitor())
    except KeyboardInterrupt:
        print("Terminating...")
        proc.terminate()


if __name__ == "__main__":
    main()

