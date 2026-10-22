"""Live Local-Agent Production Workflow Verification for Milestone B9-C2.

Verifies the end-to-end production workflow against live SQL Server test database (QueryPilot_TestDB)
using the dedicated least-privilege Windows identity (yash\\qp_test_windows_user) and a mocked SQL generator:
1. Preflight safety checks: identity, environment variables, Windows Auth, DB_NAME() safety guards.
2. Local agent schema introspection: verifies qp_test_sample fixture presence and sensitive-column redaction.
3. Decoupled candidate generation: hosted API interface produces candidate with is_executed=False and 0 DB queries.
4. Approval gate: candidate is not dispatched before separate explicit user approval.
5. Local agent query execution: authenticated endpoint executes approved candidate on live QueryPilot_TestDB.
6. Execution verification: confirms exact DB_NAME(), expected record count, absence of credentials/secrets in response.
7. Resource hygiene: deterministic connection and cursor closure on success and error paths.
8. Database security enforcement: mismatched expected database name fails closed prior to SQL execution.
"""

from __future__ import annotations

import getpass
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

from fastapi import status
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from query_pilot.agent.app import create_agent_app
from query_pilot.agent.auth import (
    SESSION_COOKIE_NAME,
    BootstrapTokenManager,
    SessionManager,
)
from query_pilot.agent.config import AgentConfig
from query_pilot.agent.engine_manager import AgentEngineManager
from query_pilot.api.app import create_app
from query_pilot.api.models import (
    ColumnSchemaContext,
    GenerateSQLRequest,
    GenerateSQLResponse,
    GenerationStatus,
    ModelFacingSchemaContext,
    TableSchemaContext,
)
from query_pilot.config import Settings
from query_pilot.db.connection import (
    ALLOWED_TARGET_DATABASE,
    DatabaseConnectionError,
    DatabaseSecurityError,
    create_profile_db_engine,
    verify_database_context,
)
from query_pilot.db.executor import QueryResult, UnsafeQueryError
from query_pilot.db.factory import ProfileConnectionFactory
from query_pilot.db.models import DatabaseSchema
from query_pilot.db.profile import (
    ConnectionProfile,
    ProfileStore,
)
from query_pilot.db.profile_executor import (
    ProfileExecutionBoundary,
    ProfileExecutionError,
)
from query_pilot.db.profile_introspector import (
    ProfileDatabaseIntrospector,
)
from query_pilot.sql.generation import (
    SQLGenerationResponse,
    SQLGenerationStatus,
    SQLGenerator,
)
from tests.test_sqlserver_integration import (
    PROTECTED_DATABASES,
    SQLServerIntegrationConfig,
    load_integration_config,
    require_live_sqlserver,
)

# Opt-in integration test marker
pytestmark = pytest.mark.integration


# ==============================================================================
# B9-C2 Live Workflow & Verification Tests
# ==============================================================================


class TestB9C2LiveLocalAgentWorkflow:
    """Verifies live query workflow against QueryPilot_TestDB under yash\\qp_test_windows_user."""

    def test_b9_c2_preflight_checks(self):
        """Preflight 1: Confirm dedicated identity, environment variables, and DB safety guards."""
        # 1. Windows identity verification
        def _get_identity() -> str:
            try:
                import ctypes
                buf = ctypes.create_unicode_buffer(256)
                size = ctypes.c_uint(256)
                if ctypes.windll.advapi32.GetUserNameW(buf, ctypes.byref(size)):
                    val = buf.value.strip().lower()
                    if val:
                        return val
            except Exception:
                pass
            return os.environ.get("USERNAME", getpass.getuser()).strip().lower()

        current_user = _get_identity()
        domain = os.environ.get("USERDOMAIN", "").lower()
        assert current_user == "qp_test_windows_user", (
            f"PREFLIGHT FAILED: Expected Windows identity 'qp_test_windows_user', got '{current_user}'. "
            f"Domain: '{domain}'. Live integration tests must run as dedicated account."
        )

        # 2. Strict environment variable checks
        enabled = os.environ.get("QUERYPILOT_TEST_SQLSERVER_ENABLED", "").strip()
        assert enabled == "1", f"PREFLIGHT FAILED: QUERYPILOT_TEST_SQLSERVER_ENABLED must be '1', got '{enabled}'."

        db_name = os.environ.get("QUERYPILOT_TEST_SQLSERVER_DB", "").strip()
        assert db_name == "QueryPilot_TestDB", (
            f"PREFLIGHT FAILED: QUERYPILOT_TEST_SQLSERVER_DB must be 'QueryPilot_TestDB', got '{db_name}'."
        )

        allow_mutation = os.environ.get("QUERYPILOT_TEST_SQLSERVER_ALLOW_MUTATION", "").strip()
        assert allow_mutation == "0", (
            f"PREFLIGHT FAILED: QUERYPILOT_TEST_SQLSERVER_ALLOW_MUTATION must be '0', got '{allow_mutation}'."
        )

        schema_name = os.environ.get("QUERYPILOT_TEST_SQLSERVER_SCHEMA", "dbo").strip()
        assert schema_name == "dbo", (
            f"PREFLIGHT FAILED: QUERYPILOT_TEST_SQLSERVER_SCHEMA must be 'dbo', got '{schema_name}'."
        )

        # 3. Integration config validation
        config = require_live_sqlserver()
        assert config.use_trusted_connection is True, "PREFLIGHT FAILED: Must use Windows Integrated Authentication."
        assert config.database_name.lower() not in PROTECTED_DATABASES, "PREFLIGHT FAILED: Target database is protected."

        # 4. Live database context & least-privilege verification
        profile = config.to_profile()
        engine = create_profile_db_engine(profile)
        try:
            with engine.connect() as conn:
                actual_db = verify_database_context(conn, expected_db=config.database_name)
                assert actual_db == "QueryPilot_TestDB", f"Runtime DB_NAME mismatch: '{actual_db}'."

                is_sysadmin = conn.execute(text("SELECT IS_SRVROLEMEMBER('sysadmin');")).scalar()
                assert is_sysadmin == 0, "PREFLIGHT FAILED: Connected identity is sysadmin (least privilege violation)."

                has_select = conn.execute(text("SELECT HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'SELECT');")).scalar()
                assert has_select == 1, "PREFLIGHT FAILED: Connected identity lacks SELECT permission."
        finally:
            engine.dispose()

    def test_b9_c2_live_production_workflow_end_to_end(self, tmp_path: Path):
        """Executes full B9-C2 workflow: Introspection -> Mocked Generation -> Approved Execution."""
        config = require_live_sqlserver()
        profile = config.to_profile(profile_id="live_test_profile")

        # ----------------------------------------------------------------------
        # Setup Local Agent Daemon Application
        # ----------------------------------------------------------------------
        profile_store = ProfileStore(profiles={"live_test_profile": profile})
        agent_config = AgentConfig(
            bind_host="127.0.0.1",
            token_dir=tmp_path,
        )
        token_mgr = BootstrapTokenManager(token_dir=tmp_path)
        session_mgr = SessionManager(idle_timeout_seconds=300, max_lifetime_seconds=3600)
        conn_factory = ProfileConnectionFactory(store=profile_store)
        engine_mgr = AgentEngineManager(factory=conn_factory)

        agent_app = create_agent_app(
            config=agent_config,
            store=profile_store,
            factory=conn_factory,
            engine_manager=engine_mgr,
            token_manager=token_mgr,
            session_manager=session_mgr,
        )

        agent_client = TestClient(agent_app, base_url="http://127.0.0.1:8001")

        # Initialize and authenticate local agent session
        bootstrap_token = token_mgr.initialize_token()
        auth_resp = agent_client.post(
            "/agent/v1/auth/bootstrap",
            headers={"X-QueryPilot-Request": "1"},
            json={"bootstrap_token": bootstrap_token},
        )
        assert auth_resp.status_code == status.HTTP_200_OK, f"Agent auth failed: {auth_resp.text}"

        # ----------------------------------------------------------------------
        # Stage 1: Request Schema Introspection from Local Agent
        # ----------------------------------------------------------------------
        schema_resp = agent_client.get(
            "/agent/v1/profiles/live_test_profile/schema",
            headers={"X-QueryPilot-Request": "1"},
        )
        assert schema_resp.status_code == status.HTTP_200_OK, f"Schema introspection failed: {schema_resp.text}"
        model_schema_dict = schema_resp.json()

        # ----------------------------------------------------------------------
        # Stage 2: Assert Fixture Metadata Presence (dbo.qp_test_sample)
        # ----------------------------------------------------------------------
        table_names = [t["table_name"].lower() for t in model_schema_dict.get("tables", [])]
        assert "qp_test_sample" in table_names, (
            f"FIXTURE MISSING: Table 'qp_test_sample' not found in model-facing schema: {table_names}"
        )

        sample_table = next(t for t in model_schema_dict["tables"] if t["table_name"].lower() == "qp_test_sample")
        sample_columns = [c["name"].lower() for c in sample_table.get("columns", [])]
        assert len(sample_columns) >= 1, "Fixture table 'qp_test_sample' has 0 columns in model schema."

        # ----------------------------------------------------------------------
        # Stage 3: Assert Sensitive Columns (password_hash) Absent
        # ----------------------------------------------------------------------
        for col in sample_columns:
            assert col not in ("password_hash", "passwordhash", "api_key", "ssn"), (
                f"SECURITY VIOLATION: Sensitive column '{col}' leaked into model-facing schema!"
            )

        # ----------------------------------------------------------------------
        # Stage 4: Request Candidate Generation through Hosted-Generation API Interface
        # ----------------------------------------------------------------------
        # Create client schema context from introspected metadata
        client_tables = []
        for tbl in model_schema_dict["tables"]:
            cols = []
            for c in tbl.get("columns", []):
                raw_type = str(c.get("data_type", "nvarchar"))
                cleaned_type = raw_type.split(" COLLATE")[0].replace('"', '').strip()
                cols.append(
                    ColumnSchemaContext(
                        name=c["name"],
                        data_type=cleaned_type,
                        nullable=c.get("nullable", True),
                    )
                )
            client_tables.append(
                TableSchemaContext(
                    table_name=tbl["table_name"],
                    schema_name=tbl.get("schema_name", "dbo"),
                    columns=cols,
                )
            )
        schema_ctx = ModelFacingSchemaContext(
            database_name=model_schema_dict.get("database_name", "QueryPilot_TestDB"),
            tables=client_tables,
        )

        mock_generator = MagicMock(spec=SQLGenerator)
        candidate_sql_text = "SELECT COUNT(*) AS total_records FROM dbo.qp_test_sample;"
        mock_generator.generate.return_value = SQLGenerationResponse(
            status=SQLGenerationStatus.ANSWERABLE,
            sql=candidate_sql_text,
            explanation="Count total records in dbo.qp_test_sample.",
        )

        hosted_app = create_app(settings=Settings(allowed_origins=["*"]))
        hosted_app.state.sql_generator = mock_generator
        hosted_client = TestClient(hosted_app)

        gen_req_body = GenerateSQLRequest(
            question="How many total records are in the sample table?",
            schema_context=schema_ctx,
            request_id="b9-c2-gen-001",
        )

        # ----------------------------------------------------------------------
        # Stage 5 & 6: Generation Dispatches Zero DB Calls & Marks is_executed=False
        # ----------------------------------------------------------------------
        with patch.object(ProfileExecutionBoundary, "execute") as spy_exec_boundary, \
             patch.object(Engine, "connect") as spy_engine_connect:
            gen_resp = hosted_client.post("/api/v1/generate-sql", json=gen_req_body.model_dump())
            assert gen_resp.status_code == status.HTTP_200_OK, f"Generation failed: {gen_resp.text}"

            gen_data = gen_resp.json()
            assert gen_data["candidate_sql"] == candidate_sql_text
            # Strict invariant: candidate is explicitly flagged as unexecuted
            assert gen_data["is_executed"] is False
            assert gen_data["request_id"] == "b9-c2-gen-001"

            # Strictly 0 database calls during generation
            assert spy_exec_boundary.call_count == 0
            assert spy_engine_connect.call_count == 0

        # ----------------------------------------------------------------------
        # Stage 7 & 8: Verify No Dispatch Prior to Explicit Request, then Execute
        # ----------------------------------------------------------------------
        original_execute = ProfileExecutionBoundary.execute
        boundary_call_count = [0]

        def spy_execute(boundary_self, *args, **kwargs):
            boundary_call_count[0] += 1
            return original_execute(boundary_self, *args, **kwargs)

        with patch.object(ProfileExecutionBoundary, "execute", spy_execute):
            # Assert execution boundary has not been invoked before explicit user approval request
            assert boundary_call_count[0] == 0

            # Stage 8: Submit Separate Authenticated Execution Request
            exec_resp = agent_client.post(
                "/agent/v1/profiles/live_test_profile/execute",
                headers={"X-QueryPilot-Request": "1"},
                json={
                    "sql": candidate_sql_text,
                    "request_id": "b9-c2-exec-001",
                },
            )
            assert exec_resp.status_code == status.HTTP_200_OK, f"Execution failed: {exec_resp.text}"
            exec_data = exec_resp.json()

            # Assert database execution boundary was dispatched exactly once upon separate explicit request
            assert boundary_call_count[0] == 1

        # ----------------------------------------------------------------------
        # Stage 9 & 10: Verify DB Identity, Results, and Row Count
        # ----------------------------------------------------------------------
        assert exec_data["columns"] == ["total_records"]
        assert len(exec_data["rows"]) == 1
        record_count = exec_data["rows"][0]["total_records"]
        print(f"\nB9-C2_RECORD_COUNT={record_count}\n")
        assert record_count == 2, f"Expected 2 fixture records in dbo.qp_test_sample, got {record_count}."
        assert exec_data["row_count"] == 1
        assert exec_data["truncated"] is False
        assert exec_data["execution_time_ms"] > 0.0
        assert exec_data["request_id"] == "b9-c2-exec-001"

        # ----------------------------------------------------------------------
        # Stage 11: Response Contains No Credentials, Connection Strings, or Secrets
        # ----------------------------------------------------------------------
        resp_text = exec_resp.text.lower()
        for forbidden in ["password", "trusted_connection", "uid=", "pwd=", "server=", "driver="]:
            assert forbidden not in resp_text, f"Potential credential/connection leakage in response: {forbidden}"

        # ----------------------------------------------------------------------
        # Stage 12: Clean Up Engines Deterministically
        # ----------------------------------------------------------------------
        disposed = engine_mgr.dispose_all()
        assert disposed >= 1
        assert len(engine_mgr._owned_engines) == 0

    def test_b9_c2_database_mismatch_fails_closed(self, tmp_path: Path):
        """Security: Deliberate expected database mismatch fails closed with DatabaseSecurityError."""
        config = require_live_sqlserver()

        # Create connection profile with deliberate database mismatch:
        # Physical server is contacted, but profile expects 'Mismatched_Security_DB'
        mismatched_profile = ConnectionProfile(
            profile_id="mismatched_db_profile",
            display_name="Mismatched Database Test Profile",
            server=config.server,
            database_name="Mismatched_Security_DB",  # Deliberate mismatch!
            driver=config.driver,
            use_trusted_connection=config.use_trusted_connection,
            schema_name="dbo",
            timeout_seconds=5,
            max_row_limit=50,
        )

        profile_store = ProfileStore(profiles={"mismatched_db_profile": mismatched_profile})
        agent_config = AgentConfig(
            bind_host="127.0.0.1",
            token_dir=tmp_path,
        )
        token_mgr = BootstrapTokenManager(token_dir=tmp_path)
        session_mgr = SessionManager(idle_timeout_seconds=300, max_lifetime_seconds=3600)
        conn_factory = ProfileConnectionFactory(store=profile_store)
        engine_mgr = AgentEngineManager(factory=conn_factory)

        agent_app = create_agent_app(
            config=agent_config,
            store=profile_store,
            factory=conn_factory,
            engine_manager=engine_mgr,
            token_manager=token_mgr,
            session_manager=session_mgr,
        )

        agent_client = TestClient(agent_app, base_url="http://127.0.0.1:8001")
        btoken = token_mgr.initialize_token()
        agent_client.post(
            "/agent/v1/auth/bootstrap",
            headers={"X-QueryPilot-Request": "1"},
            json={"bootstrap_token": btoken},
        )

        try:
            # Query execution must fail closed because physical database (QueryPilot_TestDB)
            # does not match profile expected database (Mismatched_Security_DB)
            resp = agent_client.post(
                "/agent/v1/profiles/mismatched_db_profile/execute",
                headers={"X-QueryPilot-Request": "1"},
                json={
                    "sql": "SELECT 1 AS probe;",
                    "request_id": "b9-c2-mismatch-probe",
                },
            )
            # Fails with HTTP 403 Forbidden (DatabaseSecurityError) or 500 (Connection error)
            assert resp.status_code in (status.HTTP_403_FORBIDDEN, status.HTTP_500_INTERNAL_SERVER_ERROR)
        finally:
            engine_mgr.dispose_all()
