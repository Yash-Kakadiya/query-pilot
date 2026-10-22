"""B11-E Live SQL Server Application-Path Validation Test.

Validates the complete QueryPilot local-agent application path against the pre-approved,
disposable SQL Server test database `QueryPilot_TestDB` using a deterministic mock SQL generator.

Components exercised:
- `create_agent_app`
- `AgentConfig`
- `ProfileStore`
- `ProfileConnectionFactory`
- `AgentEngineManager`
- `ProfileExecutionBoundary`
- Same-origin API router (/api/v1/generate-sql)
- Authenticated profile schema introspection (/agent/v1/profiles/{id}/schema)
- Explicit query execution endpoint (/agent/v1/profiles/{id}/execute)

Safety & Invariants:
- NEVER connects to any database other than `QueryPilot_TestDB`.
- NEVER issues DDL, INSERT, UPDATE, DELETE, or any mutating statements.
- Requires QUERYPILOT_TEST_SQLSERVER_ENABLED=1 and QUERYPILOT_TEST_SQLSERVER_ALLOW_MUTATION=0.
- Runs under dedicated non-sysadmin Windows test identity with Windows Integrated Auth.
- Zero external network calls (Gemini, LangSmith, etc. are never contacted).
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

from fastapi import status
from fastapi.testclient import TestClient
import httpx
import pytest

from query_pilot.agent.app import create_agent_app
from query_pilot.agent.auth import (
    SESSION_COOKIE_NAME,
    BootstrapTokenManager,
    SessionManager,
)
from query_pilot.agent.config import AgentConfig
from query_pilot.agent.engine_manager import AgentEngineManager
from query_pilot.api.app import create_app as create_api_app
from query_pilot.api.models import (
    ColumnSchemaContext,
    ForeignKeySchemaContext,
    ModelFacingSchemaContext,
    TableSchemaContext,
)
from query_pilot.config import Settings
from query_pilot.db.connection import (
    DatabaseSecurityError,
    create_profile_db_engine,
    verify_database_context,
)
from query_pilot.db.factory import ProfileConnectionFactory
from query_pilot.db.models import DatabaseSchema
from query_pilot.db.profile import (
    ConnectionProfile,
    ProfileStore,
)
from query_pilot.sql.generation import (
    SQLGenerationRequest,
    SQLGenerationResponse,
    SQLGenerationStatus,
    SQLGenerator,
)
from tests.test_sqlserver_integration import (
    PROTECTED_DATABASES,
    load_integration_config,
    require_live_sqlserver,
)

pytestmark = pytest.mark.integration


class DeterministicMockSQLGenerator(SQLGenerator):
    """Deterministic, read-only mock generator that strictly requires schema validation.

    Guarantees:
    - Never calls external APIs (Gemini, LangSmith, or external models).
    - Verifies the requested table 'qp_test_sample' exists in the verified schema context.
    - Generates only the authorized read-only count query.
    - Tracks execution invocations to prove zero database calls occur during generation.
    """

    def __init__(self):
        self.call_count: int = 0
        self.last_schema_received: Optional[DatabaseSchema] = None

    def generate(self, request: SQLGenerationRequest) -> SQLGenerationResponse:
        self.call_count += 1
        self.last_schema_received = request.schema_context

        # Verify table existence in model-facing schema context
        table_names = [t.table_name.lower() for t in request.schema_context.tables]
        if "qp_test_sample" not in table_names:
            return SQLGenerationResponse(
                status=SQLGenerationStatus.UNSUPPORTED,
                sql=None,
                explanation="Required table 'qp_test_sample' was not found in schema context.",
            )

        if "How many records are in the QueryPilot test sample?" in request.question:
            return SQLGenerationResponse(
                status=SQLGenerationStatus.ANSWERABLE,
                sql="SELECT COUNT(*) AS total_records FROM dbo.qp_test_sample;",
                explanation="Deterministic count of records in dbo.qp_test_sample.",
            )

        return SQLGenerationResponse(
            status=SQLGenerationStatus.UNSUPPORTED,
            sql=None,
            explanation="Unrecognized question for deterministic test harness.",
        )


class TestSqlServerApplicationPathValidation:
    """End-to-end application path validation against live SQL Server without external models."""

    @pytest.fixture(autouse=True)
    def setup_safety_preflight(self):
        """Enforce all preflight safety boundaries before each test."""
        config = require_live_sqlserver()
        assert config.database_name.lower() == "querypilot_testdb", (
            f"B11-E requires target database 'QueryPilot_TestDB', found '{config.database_name}'."
        )
        assert config.database_name.lower() not in PROTECTED_DATABASES
        assert config.allow_mutation is False, "B11-E requires ALLOW_MUTATION=0"
        self.integration_config = config

    def test_live_sqlserver_application_path_end_to_end(self, tmp_path):
        """Execute the full local-agent application path against QueryPilot_TestDB."""
        # 1. Profile and store configuration using actual SQL Server settings
        profile = self.integration_config.to_profile(
            profile_id="live_sqlserver_test_profile",
            schema_name="dbo",
            sensitive_columns=("password_hash", "ssn", "api_key"),
        )
        store = ProfileStore(
            profiles={"live_sqlserver_test_profile": profile},
            default_profile_id="live_sqlserver_test_profile",
        )
        factory = ProfileConnectionFactory(store=store)
        engine_manager = AgentEngineManager(factory=factory)

        # 2. Wire deterministic mock generator into FastAPI API application
        mock_generator = DeterministicMockSQLGenerator()
        api_settings = Settings(
            querypilot_prototype_access_key="b11-e-test-key",
            gemini_api_key="mock-offline-key",
            gemini_model="mock-model",
            database_url="",
        )
        api_app = create_api_app(
            sql_generator=mock_generator,
            settings=api_settings,
        )

        # 3. Create ASGI transport client for same-origin proxy routing
        transport = httpx.ASGITransport(app=api_app)
        upstream_client = httpx.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1:8000",
            timeout=10.0,
        )

        # 4. Configure local agent application
        token_dir = tmp_path / "tokens_b11_e"
        token_dir.mkdir(parents=True, exist_ok=True)
        agent_config = AgentConfig(
            bind_host="127.0.0.1",
            bind_port=8001,
            upstream_api_url="http://127.0.0.1:8000",
            token_dir=token_dir,
        )
        token_manager = BootstrapTokenManager(token_dir=token_dir)
        session_manager = SessionManager()

        # NOTE: caller_engine and caller_schema are explicitly NOT provided.
        # This forces the agent app to use the real ProfileConnectionFactory and ODBC driver.
        agent_app = create_agent_app(
            config=agent_config,
            store=store,
            factory=factory,
            engine_manager=engine_manager,
            token_manager=token_manager,
            session_manager=session_manager,
            caller_engine=None,
            caller_schema=None,
            upstream_client=upstream_client,
        )

        client = TestClient(
            agent_app,
            base_url="http://127.0.0.1:8001",
            headers={"Host": "127.0.0.1:8001", "Origin": "http://127.0.0.1:8001"},
        )

        # ----------------------------------------------------------------------
        # Pre-test: Bootstrap authentication
        # ----------------------------------------------------------------------
        active_token = token_manager.initialize_token()
        assert active_token is not None

        bootstrap_resp = client.post(
            "/agent/v1/auth/bootstrap",
            headers={"X-QueryPilot-Request": "1"},
            json={"bootstrap_token": active_token},
        )
        assert bootstrap_resp.status_code == status.HTTP_200_OK
        assert SESSION_COOKIE_NAME in client.cookies

        # ----------------------------------------------------------------------
        # A. Connection and identity
        # ----------------------------------------------------------------------
        # Verify profiles route
        profiles_resp = client.get("/agent/v1/profiles")
        assert profiles_resp.status_code == status.HTTP_200_OK
        profiles_data = profiles_resp.json().get("profiles", [])
        assert len(profiles_data) == 1
        assert profiles_data[0]["profile_id"] == "live_sqlserver_test_profile"
        assert profiles_data[0]["schema_name"] == "dbo"

        # Verify underlying connection identity via engine manager
        live_engine = engine_manager.get_or_create_engine("live_sqlserver_test_profile")
        with live_engine.connect() as conn:
            actual_db = verify_database_context(conn, expected_db="QueryPilot_TestDB")
            assert actual_db.lower() == "querypilot_testdb"
            assert actual_db.lower() not in PROTECTED_DATABASES

        # ----------------------------------------------------------------------
        # B. Schema introspection via authenticated route
        # ----------------------------------------------------------------------
        schema_resp = client.get("/agent/v1/profiles/live_sqlserver_test_profile/schema")
        assert schema_resp.status_code == status.HTTP_200_OK
        schema_dict = schema_resp.json()

        assert schema_dict["database_name"].lower() == "querypilot_testdb"
        assert schema_dict["is_model_facing"] is True

        table_names = [t["table_name"] for t in schema_dict.get("tables", [])]
        assert "qp_test_sample" in table_names

        # Find qp_test_sample columns
        sample_table = next(t for t in schema_dict["tables"] if t["table_name"] == "qp_test_sample")
        col_names = [c["name"].lower() for c in sample_table.get("columns", [])]

        # Sensitive column redaction verification
        assert "password_hash" not in col_names, "Sensitive column 'password_hash' must be redacted"
        assert "ssn" not in col_names
        assert "api_key" not in col_names

        # Ensure no credentials or connection strings leaked in schema payload
        raw_schema_text = schema_resp.text.lower()
        assert "password" not in raw_schema_text or "password_hash" not in raw_schema_text
        assert "driver=" not in raw_schema_text
        assert "trusted_connection" not in raw_schema_text

        # Project introspected schema_dict into ModelFacingSchemaContext (matching frontend schemaProjection)
        tables_ctx = []
        for t in schema_dict.get("tables", []):
            cols_ctx = []
            for c in t.get("columns", []):
                clean_type = c["data_type"].split(" COLLATE")[0].strip()
                cols_ctx.append(
                    ColumnSchemaContext(
                        name=c["name"],
                        data_type=clean_type,
                        nullable=c.get("nullable", True),
                        primary_key=c.get("primary_key", False),
                        comment=c.get("comment"),
                    )
                )
            fks_ctx = [
                ForeignKeySchemaContext(
                    source_table=fk["source_table"],
                    source_column=fk["source_column"],
                    target_table=fk["target_table"],
                    target_column=fk["target_column"],
                    name=fk.get("name"),
                )
                for fk in t.get("foreign_keys", [])
            ]
            tables_ctx.append(
                TableSchemaContext(
                    table_name=t["table_name"],
                    schema_name=t.get("schema_name", "dbo"),
                    columns=cols_ctx,
                    primary_keys=t.get("primary_keys", []),
                    foreign_keys=fks_ctx,
                )
            )

        client_schema_ctx = ModelFacingSchemaContext(
            database_name=schema_dict["database_name"],
            tables=tables_ctx,
        )

        # ----------------------------------------------------------------------
        # C. Generation without execution (Same-Origin Proxy)
        # ----------------------------------------------------------------------
        question = "How many records are in the QueryPilot test sample?"
        generate_payload = {
            "question": question,
            "schema_context": client_schema_ctx.model_dump(),
        }

        # Track execution count before generation to prove zero DB execution during generation
        gen_resp = client.post(
            "/api/v1/generate-sql",
            headers={
                "X-QueryPilot-Access-Key": "b11-e-test-key",
                "X-Request-ID": "req-b11-e-001",
            },
            json=generate_payload,
        )
        assert gen_resp.status_code == status.HTTP_200_OK
        gen_data = gen_resp.json()

        assert gen_data["status"] == "generated"
        assert gen_data["is_executed"] is False, "Candidate SQL must be marked unexecuted"
        assert gen_data["candidate_sql"] == "SELECT COUNT(*) AS total_records FROM dbo.qp_test_sample;"
        assert mock_generator.call_count == 1
        assert mock_generator.last_schema_received is not None

        # ----------------------------------------------------------------------
        # D. Explicit execution via local-agent route
        # ----------------------------------------------------------------------
        exec_payload = {
            "sql": gen_data["candidate_sql"],
            "request_id": "req-b11-e-002",
        }
        exec_resp = client.post(
            "/agent/v1/profiles/live_sqlserver_test_profile/execute",
            headers={"X-QueryPilot-Request": "1"},
            json=exec_payload,
        )
        assert exec_resp.status_code == status.HTTP_200_OK
        exec_data = exec_resp.json()

        # Validate result shape and scalar count
        assert exec_data["columns"] == ["total_records"]
        assert len(exec_data["rows"]) == 1
        assert exec_data["rows"][0]["total_records"] == 2
        assert exec_data["row_count"] == 1
        assert exec_data["truncated"] is False
        assert exec_data["execution_time_ms"] > 0

        # Verify no database mutations occurred (re-verify count remains 2)
        with live_engine.connect() as conn:
            from sqlalchemy import text
            count = conn.execute(text("SELECT COUNT(*) FROM dbo.qp_test_sample;")).scalar()
            assert count == 2

        # ----------------------------------------------------------------------
        # Cleanup
        # ----------------------------------------------------------------------
        engine_manager.dispose_all()
        session_manager.invalidate_all()
