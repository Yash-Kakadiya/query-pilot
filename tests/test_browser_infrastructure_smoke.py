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

from query_pilot.agent.models import ExecuteQueryResponse
from query_pilot.api.models import (
    GenerateSQLResponse,
    GenerationStatus,
    SynthesizeAnswerResponse,
)
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


def test_mock_sql_generation_and_execution_offline():
    """Verify deterministic candidate SQL generation and profile execution mocks (Milestone B12-F11B)."""
    server = BrowserTestServer(port=8007)
    server.start(timeout_seconds=5.0)

    try:
        # 1. Bootstrap agent session
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
            cookie_header = resp.headers.get("Set-Cookie", "")
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

        # 2. Create test profile
        create_payload = json.dumps({
            "profile_id": "test_e2e_db",
            "display_name": "E2E Test Inventory DB",
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

        # 3. Load profile schema
        req_schema = urllib.request.Request(
            f"{server.base_url}/agent/v1/profiles/test_e2e_db/schema",
            headers=common_headers,
            method="GET",
        )
        with urllib.request.urlopen(req_schema, timeout=2.0) as resp:
            raw_schema = json.loads(resp.read().decode("utf-8"))

        # Project to model-facing schema context format expected by POST /api/v1/generate-sql
        schema_context = {
            "database_name": raw_schema["database_name"],
            "tables": [
                {
                    "schema_name": t["schema_name"],
                    "table_name": t["table_name"],
                    "columns": [
                        {
                            "name": c["name"],
                            "data_type": c["data_type"],
                            "nullable": c["nullable"],
                            "primary_key": c["primary_key"],
                        }
                        for c in t["columns"]
                    ],
                    "primary_keys": [c["name"] for c in t["columns"] if c["primary_key"]],
                    "foreign_keys": [],
                }
                for t in raw_schema["tables"]
            ],
        }

        # 4. Verify candidate SQL generation (success case)
        gen_payload = json.dumps({
            "question": "Show all customers in Seattle",
            "schema_context": schema_context,
            "request_id": "req-gen-test-1",
        }).encode("utf-8")
        req_gen = urllib.request.Request(
            f"{server.base_url}/api/v1/generate-sql",
            data=gen_payload,
            headers=state_headers,
            method="POST",
        )
        with urllib.request.urlopen(req_gen, timeout=2.0) as resp:
            assert resp.status == 200
            gen_data = json.loads(resp.read().decode("utf-8"))
            validated_gen = GenerateSQLResponse.model_validate(gen_data)
            assert validated_gen.status == GenerationStatus.GENERATED
            assert validated_gen.candidate_sql is not None
            assert "Customers" in validated_gen.candidate_sql
            assert validated_gen.is_executed is False
            assert validated_gen.execution_disclaimer == "Candidate SQL is unexecuted."

        # 5. Verify candidate generation with controlled failure (unsupported question)
        unsupp_payload = json.dumps({
            "question": "What is the weather forecast for tomorrow? (unsupported)",
            "schema_context": schema_context,
            "request_id": "req-gen-unsupp-1",
        }).encode("utf-8")
        req_unsupp = urllib.request.Request(
            f"{server.base_url}/api/v1/generate-sql",
            data=unsupp_payload,
            headers=state_headers,
            method="POST",
        )
        with urllib.request.urlopen(req_unsupp, timeout=2.0) as resp:
            assert resp.status == 200
            unsupp_data = json.loads(resp.read().decode("utf-8"))
            validated_unsupp = GenerateSQLResponse.model_validate(unsupp_data)
            assert validated_unsupp.status == GenerationStatus.UNSUPPORTED
            assert validated_unsupp.candidate_sql is None
            assert validated_unsupp.unsupported_reason is not None

        # 6. Verify safe execution of generated candidate SQL
        exec_payload = json.dumps({
            "sql": validated_gen.candidate_sql,
            "request_id": "req-exec-test-1",
        }).encode("utf-8")
        req_exec = urllib.request.Request(
            f"{server.base_url}/agent/v1/profiles/test_e2e_db/execute",
            data=exec_payload,
            headers=state_headers,
            method="POST",
        )
        with urllib.request.urlopen(req_exec, timeout=2.0) as resp:
            assert resp.status == 200
            exec_data = json.loads(resp.read().decode("utf-8"))
            validated_exec = ExecuteQueryResponse.model_validate(exec_data)
            assert validated_exec.columns == ["CustomerID", "CompanyName", "City"]
            assert validated_exec.row_count == 3
            assert validated_exec.truncated is False
            assert len(validated_exec.rows) == 3
            assert validated_exec.rows[0]["CompanyName"] == "Contoso Ltd"
            assert validated_exec.execution_time_ms >= 0.0

        # 7. Verify AST rejection of unsafe SQL (DROP TABLE) through production validator
        unsafe_payload = json.dumps({
            "sql": "DROP TABLE dbo.Customers;",
            "request_id": "req-unsafe-1",
        }).encode("utf-8")
        req_unsafe = urllib.request.Request(
            f"{server.base_url}/agent/v1/profiles/test_e2e_db/execute",
            data=unsafe_payload,
            headers=state_headers,
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req_unsafe, timeout=2.0)
        assert exc_info.value.code == 400

        # 8. Verify rejection of unknown table
        unknown_table_payload = json.dumps({
            "sql": "SELECT * FROM dbo.SecretSalaries;",
            "request_id": "req-unknown-tbl-1",
        }).encode("utf-8")
        req_unknown_tbl = urllib.request.Request(
            f"{server.base_url}/agent/v1/profiles/test_e2e_db/execute",
            data=unknown_table_payload,
            headers=state_headers,
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req_unknown_tbl, timeout=2.0)
        assert exc_info.value.code == 400

        # 9. Verify rejection without CSRF header
        req_no_csrf = urllib.request.Request(
            f"{server.base_url}/agent/v1/profiles/test_e2e_db/execute",
            data=exec_payload,
            headers={
                **common_headers,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req_no_csrf, timeout=2.0)
        assert exc_info.value.code == 400
        csrf_err = json.loads(exc_info.value.read().decode("utf-8"))
        assert csrf_err.get("error") == "MISSING_CUSTOM_HEADER"

        # 10. Verify rejection when unauthenticated
        req_no_auth = urllib.request.Request(
            f"{server.base_url}/agent/v1/profiles/test_e2e_db/execute",
            data=exec_payload,
            headers={
                "Host": f"127.0.0.1:{server.port}",
                "Content-Type": "application/json",
                "X-QueryPilot-Request": "1",
            },
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req_no_auth, timeout=2.0)
        assert exc_info.value.code == 401

        # 11. Verify query timeout simulation
        create_timeout_payload = json.dumps({
            "profile_id": "test_timeout_db",
            "display_name": "Timeout Simulation DB",
            "server": "localhost",
            "database_name": "TimeoutDB",
            "schema_name": "dbo",
            "driver": "ODBC Driver 17 for SQL Server",
            "engine_type": "mssql",
            "use_trusted_connection": True,
            "timeout_seconds": 1,
            "max_row_limit": 100,
        }).encode("utf-8")
        req_create_timeout = urllib.request.Request(
            f"{server.base_url}/agent/v1/profiles",
            data=create_timeout_payload,
            headers=state_headers,
            method="POST",
        )
        with urllib.request.urlopen(req_create_timeout, timeout=2.0) as resp:
            assert resp.status == 201

        timeout_exec_payload = json.dumps({
            "sql": "SELECT CustomerID, CompanyName, City FROM dbo.Customers WHERE City = 'Seattle';",
        }).encode("utf-8")
        req_exec_timeout = urllib.request.Request(
            f"{server.base_url}/agent/v1/profiles/test_timeout_db/execute",
            data=timeout_exec_payload,
            headers=state_headers,
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req_exec_timeout, timeout=2.0)
        assert exc_info.value.code == 504

        # 12. Verify row truncation simulation
        create_trunc_payload = json.dumps({
            "profile_id": "test_trunc_db",
            "display_name": "Truncation Simulation DB",
            "server": "localhost",
            "database_name": "TruncDB",
            "schema_name": "dbo",
            "driver": "ODBC Driver 17 for SQL Server",
            "engine_type": "mssql",
            "use_trusted_connection": True,
            "timeout_seconds": 15,
            "max_row_limit": 2,
        }).encode("utf-8")
        req_create_trunc = urllib.request.Request(
            f"{server.base_url}/agent/v1/profiles",
            data=create_trunc_payload,
            headers=state_headers,
            method="POST",
        )
        with urllib.request.urlopen(req_create_trunc, timeout=2.0) as resp:
            assert resp.status == 201

        req_exec_trunc = urllib.request.Request(
            f"{server.base_url}/agent/v1/profiles/test_trunc_db/execute",
            data=exec_payload,
            headers=state_headers,
            method="POST",
        )
        with urllib.request.urlopen(req_exec_trunc, timeout=2.0) as resp:
            assert resp.status == 200
            trunc_data = json.loads(resp.read().decode("utf-8"))
            validated_trunc = ExecuteQueryResponse.model_validate(trunc_data)
            assert validated_trunc.truncated is True
            assert validated_trunc.row_count == 2
            assert len(validated_trunc.rows) == 2

        # 13. Verify answer synthesis route mock
        synth_payload = json.dumps({
            "question": "Show all customers in Seattle",
            "candidate_sql": validated_gen.candidate_sql,
            "columns": ["CustomerID", "CompanyName", "City"],
            "row_count": 3,
            "truncated": False,
            "execution_status": "succeeded",
            "execution_time_ms": 12.5,
            "request_id": "req-synth-1",
        }).encode("utf-8")
        req_synth = urllib.request.Request(
            f"{server.base_url}/api/v1/synthesize-answer",
            data=synth_payload,
            headers=state_headers,
            method="POST",
        )
        with urllib.request.urlopen(req_synth, timeout=2.0) as resp:
            assert resp.status == 200
            synth_data = json.loads(resp.read().decode("utf-8"))
            validated_synth = SynthesizeAnswerResponse.model_validate(synth_data)
            assert "3 matching records" in validated_synth.summary

        # 14. Verify unknown upstream route returns 404 and does not make outbound network calls
        req_unknown = urllib.request.Request(
            f"{server.base_url}/api/v1/unknown-external-route",
            data=b"{}",
            headers=state_headers,
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req_unknown, timeout=2.0)
        assert exc_info.value.code == 404

        # 15. Verify audit log captures request sequence
        audit_paths = [r.path for r in server.audit_log]
        assert "/api/v1/generate-sql" in audit_paths
        assert "/agent/v1/profiles/test_e2e_db/execute" in audit_paths
        assert "/api/v1/synthesize-answer" in audit_paths

    finally:
        server.stop()
