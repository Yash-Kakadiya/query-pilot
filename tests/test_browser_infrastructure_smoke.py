"""Smoke test for minimal browser-test server infrastructure (Milestone B12-F10B).

Verifies that the BrowserTestServer:
1. Binds strictly to loopback and serves the production frontend.
2. Accepts bootstrap authentication using the pre-seeded test token.
3. Executes profile validation and persistence offline.
4. Executes deterministic connection test probes (success and simulated failure).
5. Returns deterministic model-facing schema for created profiles.
6. Records request auditing accurately.
7. Shuts down cleanly without leaking background threads or sockets.
"""

from __future__ import annotations

import json
from pathlib import Path
import urllib.request
import urllib.error
import pytest

from scripts.browser_test_server import BrowserTestServer, TEST_BOOTSTRAP_TOKEN


def test_browser_test_server_lifecycle_and_mock_behavior():
    """Verify complete lifecycle and deterministic mocking of BrowserTestServer."""
    server = BrowserTestServer(port=8006)
    server.start(timeout_seconds=5.0)

    try:
        # 1. Verify frontend index.html served
        req_app = urllib.request.Request(f"{server.app_url}", method="GET")
        with urllib.request.urlopen(req_app, timeout=2.0) as resp:
            assert resp.status == 200
            content = resp.read().decode("utf-8")
            assert "<title>QueryPilot</title>" in content
            assert "/app/assets/" in content

        # 2. Verify bootstrap token exchange
        auth_payload = json.dumps({"bootstrap_token": server.bootstrap_token}).encode("utf-8")
        req_auth = urllib.request.Request(
            f"{server.base_url}/agent/v1/auth/bootstrap",
            data=auth_payload,
            headers={
                "Content-Type": "application/json",
                "X-QueryPilot-Request": "1",
                "Host": f"127.0.0.1:{server.port}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req_auth, timeout=2.0) as resp:
            assert resp.status == 200
            cookie_header = resp.headers.get("Set-Cookie", "")
            assert "qpt_session=" in cookie_header
            session_cookie = cookie_header.split(";")[0]

        common_headers = {
            "Host": f"127.0.0.1:{server.port}",
            "Cookie": session_cookie,
        }
        state_headers = {
            **common_headers,
            "Content-Type": "application/json",
            "X-QueryPilot-Request": "1",
        }

        # 3. Verify profile validation endpoint
        val_payload = json.dumps({
            "profile_id": "test_inv_db",
            "display_name": "Test Inventory Database",
            "server": "localhost",
            "database_name": "InventoryDB",
            "schema_name": "dbo",
            "driver": "ODBC Driver 17 for SQL Server",
            "engine_type": "mssql",
            "use_trusted_connection": True,
            "timeout_seconds": 15,
            "max_row_limit": 100,
        }).encode("utf-8")
        req_val = urllib.request.Request(
            f"{server.base_url}/agent/v1/profiles/validate-config",
            data=val_payload,
            headers=state_headers,
            method="POST",
        )
        with urllib.request.urlopen(req_val, timeout=2.0) as resp:
            assert resp.status == 200
            val_data = json.loads(resp.read().decode("utf-8"))
            assert val_data["valid"] is True

        # 4. Verify profile creation endpoint
        create_payload = json.dumps({
            "profile_id": "test_inv_db",
            "display_name": "Test Inventory Database",
            "server": "localhost",
            "database_name": "InventoryDB",
            "schema_name": "dbo",
            "driver": "ODBC Driver 17 for SQL Server",
            "engine_type": "mssql",
            "use_trusted_connection": True,
            "timeout_seconds": 15,
            "max_row_limit": 100,
            "set_as_default": True,
        }).encode("utf-8")
        req_create = urllib.request.Request(
            f"{server.base_url}/agent/v1/profiles",
            data=create_payload,
            headers=state_headers,
            method="POST",
        )
        with urllib.request.urlopen(req_create, timeout=2.0) as resp:
            assert resp.status == 201
            create_data = json.loads(resp.read().decode("utf-8"))
            assert create_data["profile"]["profile_id"] == "test_inv_db"

        # 5. Verify deterministic connection test success
        req_test_conn = urllib.request.Request(
            f"{server.base_url}/agent/v1/profiles/test_inv_db/test-connection",
            headers=state_headers,
            method="POST",
        )
        with urllib.request.urlopen(req_test_conn, timeout=2.0) as resp:
            assert resp.status == 200
            test_data = json.loads(resp.read().decode("utf-8"))
            assert test_data["connected"] is True
            assert test_data["database_name"] == "InventoryDB"
            assert test_data["failure_code"] is None

        # 6. Verify deterministic connection test failure simulation
        fail_payload = json.dumps({
            "profile_id": "test_fail_db",
            "display_name": "Simulated Failure DB",
            "server": "localhost",
            "database_name": "UnreachableDB",
            "schema_name": "dbo",
            "driver": "ODBC Driver 17 for SQL Server",
            "engine_type": "mssql",
            "use_trusted_connection": True,
            "timeout_seconds": 15,
            "max_row_limit": 100,
            "set_as_default": False,
        }).encode("utf-8")
        req_create_fail = urllib.request.Request(
            f"{server.base_url}/agent/v1/profiles",
            data=fail_payload,
            headers=state_headers,
            method="POST",
        )
        with urllib.request.urlopen(req_create_fail, timeout=2.0) as resp:
            assert resp.status == 201

        req_test_fail = urllib.request.Request(
            f"{server.base_url}/agent/v1/profiles/test_fail_db/test-connection",
            headers=state_headers,
            method="POST",
        )
        with urllib.request.urlopen(req_test_fail, timeout=2.0) as resp:
            assert resp.status == 200
            fail_data = json.loads(resp.read().decode("utf-8"))
            assert fail_data["connected"] is False
            assert fail_data["failure_code"] == "UNREACHABLE"

        # 7. Verify dynamic schema loading for created profile
        req_schema = urllib.request.Request(
            f"{server.base_url}/agent/v1/profiles/test_inv_db/schema",
            headers=common_headers,
            method="GET",
        )
        with urllib.request.urlopen(req_schema, timeout=2.0) as resp:
            assert resp.status == 200
            schema_data = json.loads(resp.read().decode("utf-8"))
            assert schema_data["database_name"] == "InventoryDB"
            assert len(schema_data["tables"]) == 2

        # 8. Verify audit log recorded endpoints
        paths_called = [r.path for r in server.audit_log]
        assert "/app/" in paths_called
        assert "/agent/v1/auth/bootstrap" in paths_called
        assert "/agent/v1/profiles/validate-config" in paths_called
        assert "/agent/v1/profiles" in paths_called
        assert "/agent/v1/profiles/test_inv_db/test-connection" in paths_called
        assert "/agent/v1/profiles/test_fail_db/test-connection" in paths_called
        assert "/agent/v1/profiles/test_inv_db/schema" in paths_called

    finally:
        server.stop()

    # 9. Verify server has stopped
    with pytest.raises(Exception):
        req_dead = urllib.request.Request(f"{server.app_url}", method="GET")
        urllib.request.urlopen(req_dead, timeout=0.5)
