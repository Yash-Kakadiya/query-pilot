"""Browser-driven acceptance tests for the single-turn QueryPilot journey (Milestone B12-F11C).

Tests the full, real user journey inside headless Chrome against the built production SPA
and the local agent daemon:
1. Authenticate using isolated test token.
2. Select database profile and verify schema introspection.
3. Submit natural-language question and verify candidate SQL generation.
4. Verify candidate SQL and 'NOT YET EXECUTED' safety disclaimer.
5. Verify generation alone has NOT executed the query (audit log + DOM).
6. Click explicit 'Execute Query on Database' button.
7. Verify execution request follows generation request in the audit log.
8. Verify tabular results, column headers, deterministic rows, row count, and local privacy notice.
9. Verify grounded deterministic answer-synthesis card.
10. Verify question modification triggers stale drift notice and blocks execution.
11. Verify unsafe SQL (DROP TABLE) is rejected by production AST validator.
12. Reliable cleanup of browser and server.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, List, Optional
import urllib.request
import urllib.error

import pytest
from websockets.sync.client import ClientConnection, connect

# Ensure repository root is on sys.path for importing scripts/
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from query_pilot.agent.models import ExecuteQueryResponse
from query_pilot.api.models import GenerateSQLResponse, GenerationStatus
from query_pilot.db.profile import ConnectionProfile
from scripts.browser_test_server import BrowserTestServer, find_free_port


def find_chrome_executable() -> str:
    """Discover a suitable Chrome/Chromium executable across platforms.

    Search hierarchy:
    1. CHROME_BIN environment variable if valid and executable.
    2. PATH lookup via shutil.which for standard browser binary names.
    3. Platform-specific fallback locations (Windows, Linux, macOS).
    """
    env_bin = os.environ.get("CHROME_BIN")
    if env_bin:
        env_path = Path(env_bin)
        if env_path.is_file() and os.access(str(env_path), os.X_OK if sys.platform != "win32" else os.F_OK):
            return str(env_path.resolve())
        which_env = shutil.which(env_bin)
        if which_env and os.access(which_env, os.X_OK if sys.platform != "win32" else os.F_OK):
            return which_env

    # Common binary names on PATH
    candidate_names = [
        "google-chrome",
        "google-chrome-stable",
        "chromium-browser",
        "chromium",
        "chrome",
        "msedge",
    ]
    for name in candidate_names:
        found = shutil.which(name)
        if found and os.access(found, os.X_OK if sys.platform != "win32" else os.F_OK):
            return found

    # Well-known platform paths
    if sys.platform == "win32":
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ]
    elif sys.platform == "darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ]
    else:  # Linux / POSIX
        candidates = [
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium-browser",
            "/usr/bin/chromium",
            "/snap/bin/chromium",
        ]

    for cand in candidates:
        p = Path(cand)
        if p.is_file() and os.access(str(p), os.X_OK if sys.platform != "win32" else os.F_OK):
            return str(p.resolve())

    raise RuntimeError(
        "No supported Chrome or Chromium executable found on system. "
        "Please install Google Chrome or set the CHROME_BIN environment variable."
    )


class HeadlessChromeClient:
    """Minimal, synchronous CDP client for headless Chrome acceptance testing."""

    def __init__(self, cdp_port: Optional[int] = None, headless: bool = True):
        self.cdp_port = cdp_port if cdp_port and cdp_port > 0 else find_free_port("127.0.0.1")
        self.headless = headless
        self._temp_dir: Optional[tempfile.TemporaryDirectory] = None
        self._proc: Optional[subprocess.Popen] = None
        self._ws: Optional[ClientConnection] = None
        self._msg_id = 0

        try:
            self._temp_dir = tempfile.TemporaryDirectory(prefix="qp_chrome_test_")
            self.chrome_path = find_chrome_executable()

            args = [
                self.chrome_path,
                "--headless=new" if self.headless else "",
                f"--remote-debugging-port={self.cdp_port}",
                "--disable-gpu",
                "--no-first-run",
                "--no-default-browser-check",
                f"--user-data-dir={self._temp_dir.name}",
            ]
            if sys.platform != "win32":
                args.extend([
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-setuid-sandbox",
                ])
            self._proc = subprocess.Popen(
                [a for a in args if a],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._wait_for_cdp_port()
        except Exception:
            try:
                self.close()
            except Exception:
                pass
            raise

    def _wait_for_cdp_port(self, timeout: float = 5.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                req = urllib.request.Request(f"http://127.0.0.1:{self.cdp_port}/json/version", method="GET")
                with urllib.request.urlopen(req, timeout=0.5) as resp:
                    if resp.status == 200:
                        return
            except Exception:
                time.sleep(0.05)
        raise RuntimeError(f"Chrome CDP on port {self.cdp_port} failed to start within {timeout}s.")

    def navigate(self, url: str) -> None:
        req = urllib.request.Request(f"http://127.0.0.1:{self.cdp_port}/json/new?{url}", method="PUT")
        with urllib.request.urlopen(req) as resp:
            target = json.loads(resp.read().decode())
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
        self._ws = connect(target["webSocketDebuggerUrl"])
        # Wait for page readyState complete
        self.wait_for_condition("document.readyState === 'complete'", timeout=5.0)

    def send_cdp(self, method: str, params: Optional[dict] = None) -> Any:
        if not self._ws:
            raise RuntimeError("WebSocket not connected to a target page.")
        self._msg_id += 1
        msg = {"id": self._msg_id, "method": method, "params": params or {}}
        self._ws.send(json.dumps(msg))
        while True:
            raw = self._ws.recv()
            data = json.loads(raw)
            if data.get("id") == self._msg_id:
                if "error" in data:
                    raise RuntimeError(f"CDP Error in {method}: {data['error']}")
                return data.get("result", {})

    def evaluate(self, expr: str) -> Any:
        res = self.send_cdp(
            "Runtime.evaluate",
            {
                "expression": expr,
                "returnByValue": True,
                "awaitPromise": True,
            },
        )
        result_obj = res.get("result", {})
        if result_obj.get("subtype") == "error":
            raise RuntimeError(f"JS evaluation error: {result_obj.get('description')}")
        return result_obj.get("value")

    def wait_for_condition(self, condition_js: str, timeout: float = 10.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.evaluate(f"Boolean({condition_js})"):
                    return True
            except Exception:
                pass
            time.sleep(0.05)
        raise TimeoutError(f"Condition '{condition_js}' not met within {timeout}s.")

    def wait_for_selector(self, selector: str, timeout: float = 10.0) -> bool:
        js = f"document.querySelector({json.dumps(selector)}) !== null"
        return self.wait_for_condition(js, timeout=timeout)

    def click(self, selector: str) -> None:
        self.wait_for_selector(selector)
        js = f"""(() => {{
            const el = document.querySelector({json.dumps(selector)});
            if (!el) throw new Error("Element not found: " + {json.dumps(selector)});
            el.click();
        }})()"""
        self.evaluate(js)

    def set_input_value(self, selector: str, value: str) -> None:
        self.wait_for_selector(selector)
        js = f"""(() => {{
            const el = document.querySelector({json.dumps(selector)});
            if (!el) throw new Error("Element not found: " + {json.dumps(selector)});
            el.focus();
            const proto = el instanceof HTMLTextAreaElement ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
            const nativeSetter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
            if (nativeSetter) {{
                nativeSetter.call(el, {json.dumps(value)});
            }} else {{
                el.value = {json.dumps(value)};
            }}
            el.dispatchEvent(new Event('input', {{ bubbles: true }}));
            el.dispatchEvent(new Event('change', {{ bubbles: true }}));
        }})()"""
        self.evaluate(js)

    def select_option(self, selector: str, value: str) -> None:
        self.wait_for_selector(selector)
        js = f"""(() => {{
            const el = document.querySelector({json.dumps(selector)});
            if (!el) throw new Error("Element not found: " + {json.dumps(selector)});
            el.value = {json.dumps(value)};
            el.dispatchEvent(new Event('change', {{ bubbles: true }}));
        }})()"""
        self.evaluate(js)

    def get_text(self, selector: str) -> str:
        self.wait_for_selector(selector)
        js = f"""(() => {{
            const el = document.querySelector({json.dumps(selector)});
            return el ? el.innerText || el.textContent || "" : "";
        }})()"""
        return self.evaluate(js) or ""

    def element_exists(self, selector: str) -> bool:
        js = f"document.querySelector({json.dumps(selector)}) !== null"
        return bool(self.evaluate(js))

    def is_disabled(self, selector: str) -> bool:
        self.wait_for_selector(selector)
        js = f"""(() => {{
            const el = document.querySelector({json.dumps(selector)});
            return el ? Boolean(el.disabled) : true;
        }})()"""
        return bool(self.evaluate(js))

    def __enter__(self) -> HeadlessChromeClient:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def close(self) -> None:
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None
        if hasattr(self, "_proc") and self._proc:
            proc = self._proc
            self._proc = None
            if proc.poll() is None:
                try:
                    if sys.platform == "win32":
                        subprocess.run(
                            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            check=False,
                        )
                    else:
                        proc.terminate()
                        try:
                            proc.wait(timeout=3.0)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                            proc.wait(timeout=2.0)
                except Exception:
                    try:
                        proc.kill()
                        proc.wait(timeout=2.0)
                    except Exception:
                        pass
        if hasattr(self, "_temp_dir") and self._temp_dir:
            temp_dir = self._temp_dir
            self._temp_dir = None
            try:
                temp_dir.cleanup()
            except Exception:
                pass


def test_single_turn_browser_query_journey():
    """Execute complete single-turn QueryPilot journey in headless Chrome."""
    server = BrowserTestServer()

    # Pre-seed approved test profile into server store
    test_profile = ConnectionProfile(
        profile_id="test_inv_db",
        display_name="Inventory Production DB",
        server="localhost",
        database_name="InventoryDB",
        schema_name="dbo",
        driver="ODBC Driver 17 for SQL Server",
        engine_type="mssql",
        use_trusted_connection=True,
        timeout_seconds=15,
        max_row_limit=100,
    )
    server.store.register_profile(test_profile)

    browser = None
    try:
        server.start(timeout_seconds=5.0)
        browser = HeadlessChromeClient(headless=True)
        # Step 1: Navigate to SPA
        browser.navigate(server.app_url)

        # Wait for either the auth modal input or the authenticate button
        browser.wait_for_condition(
            "document.querySelector('#bootstrap-token-input') !== null || document.querySelector('.agent-auth-btn') !== null",
            timeout=10.0,
        )
        if not browser.element_exists("#bootstrap-token-input"):
            browser.click(".agent-auth-btn")

        browser.wait_for_selector("#bootstrap-token-input", timeout=10.0)

        # Authenticate using server bootstrap token
        browser.set_input_value("#bootstrap-token-input", server.bootstrap_token)
        browser.evaluate("document.querySelector('form.agent-auth-form').requestSubmit()")

        # Wait for modal to dismiss and profile selector to become visible
        browser.wait_for_selector("#database-profile-select", timeout=10.0)
        assert "Connected" in browser.get_text(".agent-session-text")

        # Step 2: Select database profile and confirm schema becomes available
        browser.select_option("#database-profile-select", "test_inv_db")
        browser.wait_for_selector(".schema-status-loaded", timeout=10.0)
        schema_text = browser.get_text(".schema-status-loaded")
        assert "InventoryDB" in schema_text
        assert "2 tables available" in schema_text

        # Step 3: Candidate SQL Generation workspace appears
        browser.wait_for_selector("#candidate-question-input", timeout=10.0)
        assert browser.element_exists("button.candidate-submit-btn")

        # Submit natural language question
        question_text = "Show all customers in Seattle"
        browser.set_input_value("#candidate-question-input", question_text)
        browser.click("button.candidate-submit-btn")

        # Wait for candidate review panel to render
        browser.wait_for_selector(".candidate-review-panel", timeout=10.0)
        assert "candidate generated" in browser.get_text(".candidate-status-badge").lower()

        # Step 4: Assert candidate SQL and NOT YET EXECUTED disclaimer
        candidate_sql = browser.get_text(".candidate-code-block code")
        assert "SELECT CustomerID, CompanyName, City FROM dbo.Customers" in candidate_sql
        assert "Seattle" in candidate_sql

        disclaimer_text = browser.get_text(".candidate-disclaimer-box")
        assert "STRICT EXECUTION BOUNDARY" in disclaimer_text
        assert "NOT YET EXECUTED" in disclaimer_text

        # Step 5: Assert generation alone has NOT executed the query
        audit_paths_after_gen = [r.path for r in server.audit_log]
        assert "/api/v1/generate-sql" in audit_paths_after_gen
        assert "/agent/v1/profiles/test_inv_db/execute" not in audit_paths_after_gen
        assert not browser.element_exists(".local-execution-card")
        assert not browser.element_exists("table.local-data-table")

        # Step 6: Explicit user execution action
        assert not browser.is_disabled("button.candidate-execute-btn")
        browser.click("button.candidate-execute-btn")

        # Step 7: Wait for execution and verify audit request ordering
        browser.wait_for_selector(".local-execution-card", timeout=5.0)
        browser.wait_for_selector("table.local-data-table", timeout=5.0)

        # Audit log must prove execution occurred after generation
        gen_indices = [i for i, r in enumerate(server.audit_log) if r.path == "/api/v1/generate-sql"]
        exec_indices = [i for i, r in enumerate(server.audit_log) if r.path == "/agent/v1/profiles/test_inv_db/execute"]
        assert len(gen_indices) >= 1
        assert len(exec_indices) >= 1
        assert max(gen_indices) < min(exec_indices)

        # Step 8: Verify tabular results, row count, truncation, and privacy notice
        table_headers = browser.get_text("table.local-data-table thead")
        assert "CustomerID" in table_headers
        assert "CompanyName" in table_headers
        assert "City" in table_headers

        table_body = browser.get_text("table.local-data-table tbody")
        assert "Contoso Ltd" in table_body
        assert "Fabrikam Inc" in table_body
        assert "Northwind Traders" in table_body

        row_stat = browser.get_text(".local-chip-rows")
        assert "3" in row_stat and "rows" in row_stat
        assert not browser.element_exists(".local-chip-truncated")

        privacy_notice = browser.get_text(".local-privacy-notice")
        assert "LOCAL RESULTS ONLY" in privacy_notice

        # Step 9: Verify grounded deterministic answer synthesis card
        synth_badge = browser.get_text(".local-synthesized-badge").lower()
        assert "deterministic answer" in synth_badge and "local results" in synth_badge
        synth_text = browser.get_text(".local-synthesized-text")
        assert len(synth_text) > 10

        # Step 10: Question edit drift disables execution until regenerated
        browser.set_input_value("#candidate-question-input", "Show all customers in Seattle sorted by name")
        browser.wait_for_selector(".candidate-stale-banner", timeout=5.0)
        assert "question was modified" in browser.get_text(".candidate-stale-banner").lower()
        assert browser.is_disabled("button.candidate-execute-btn")

        # Step 11: Verify unsafe SQL is rejected by production AST validator
        # Regenerate candidate to clear stale state
        browser.click("button.candidate-submit-btn")
        browser.wait_for_condition("!document.querySelector('.candidate-stale-banner')", timeout=5.0)

        # Enter SQL edit mode
        browser.click("button.candidate-edit-toggle-btn")
        browser.wait_for_selector("textarea.candidate-edit-textarea", timeout=5.0)
        browser.set_input_value("textarea.candidate-edit-textarea", "DROP TABLE dbo.Customers;")

        # Execute unsafe SQL
        browser.click("button.candidate-execute-btn")

        # Expect local error card displaying production validation rejection
        browser.wait_for_selector(".local-error-card", timeout=5.0)
        error_msg = browser.get_text(".local-error-card").lower()
        assert "query execution failed" in error_msg
        assert "sql validation rejected query" in error_msg
        assert not browser.element_exists("table.local-data-table")

    finally:
        if browser:
            browser.close()
        server.stop()
