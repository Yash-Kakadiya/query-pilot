"""Backend Offline Orchestration Contract Tests for Milestone B10.

Verifies the decoupled backend handoff between the Hosted API and Local Agent boundaries:
1. Candidate generation produces unexecuted SQL (is_executed=False) with zero database execution calls.
2. Unsafe or mutated SQL is rejected at the local execution boundary before reaching the database.
3. Local execution returns structured rows and row counts to local callers.
4. Hosted metadata synthesis receives only metadata and never raw result rows or cell values.
5. All tests run offline using mocks and in-memory constructs without live network or database access.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

from fastapi import status
from fastapi.testclient import TestClient
import pytest
from sqlalchemy.engine import Connection, Engine

from query_pilot.agent.app import create_agent_app
from query_pilot.agent.auth import BootstrapTokenManager, SessionManager
from query_pilot.agent.config import AgentConfig
from query_pilot.agent.engine_manager import AgentEngineManager
from query_pilot.api.app import create_app
from query_pilot.api.models import (
    ColumnSchemaContext,
    GenerateSQLRequest,
    GenerateSQLResponse,
    GenerationStatus,
    ModelFacingSchemaContext,
    SynthesizeAnswerRequest,
    SynthesizeAnswerResponse,
    TableSchemaContext,
)
from query_pilot.config import Settings
from query_pilot.db.factory import ProfileConnectionFactory
from query_pilot.db.models import (
    ColumnMetadata,
    DatabaseSchema,
    TableMetadata,
)
from query_pilot.db.profile import ConnectionProfile, ProfileStore
from query_pilot.db.profile_executor import ProfileExecutionBoundary
from query_pilot.presentation.synthesizer import (
    AnswerGroundingStatus,
    AnswerSynthesisService,
)
from query_pilot.sql.generation import (
    SQLGenerationResponse,
    SQLGenerationStatus,
    SQLGenerator,
)


# ==============================================================================
# Fixtures for Offline Backend Orchestration
# ==============================================================================

@pytest.fixture
def mock_profile() -> ConnectionProfile:
    """Connection profile for B10 backend orchestration."""
    return ConnectionProfile(
        profile_id="b10_test_profile",
        display_name="B10 Test Profile",
        server="localhost",
        database_name="B10_TestDB",
        schema_name="dbo",
        timeout_seconds=5,
        max_row_limit=50,
    )


@pytest.fixture
def mock_db_connection() -> MagicMock:
    """Mock connection satisfying DB_NAME() check and statement execution."""
    conn = MagicMock(spec=Connection)
    raw_dbapi = MagicMock()
    raw_dbapi.timeout = 5
    conn.connection.dbapi_connection = raw_dbapi
    conn.execute.return_value.scalar.return_value = "B10_TestDB"

    mock_cursor = MagicMock()
    mock_cursor.keys.return_value = ["total_records"]
    mock_cursor.fetchmany.return_value = [(2,)]
    conn.execution_options.return_value.execute.return_value = mock_cursor
    return conn


@pytest.fixture
def mock_db_engine(mock_db_connection: MagicMock) -> MagicMock:
    """Mock engine returning mock_db_connection."""
    engine = MagicMock(spec=Engine)
    engine.connect.return_value = mock_db_connection
    return engine


@pytest.fixture
def model_facing_schema() -> DatabaseSchema:
    """Model-facing schema context with sensitive columns redacted."""
    return DatabaseSchema(
        database_name="B10_TestDB",
        tables=[
            TableMetadata(
                schema_name="dbo",
                table_name="qp_test_sample",
                columns=[
                    ColumnMetadata(name="id", data_type="INTEGER", primary_key=True),
                    ColumnMetadata(name="category", data_type="TEXT"),
                ],
                primary_keys=["id"],
            )
        ],
        is_model_facing=True,
    )


# ==============================================================================
# B10 Backend Orchestration Tests
# ==============================================================================

class TestB10BackendOrchestrationContracts:
    """Verifies backend orchestration contracts between hosted API and local agent."""

    def test_decoupled_generation_execution_synthesis_handoff(
        self,
        mock_db_engine: MagicMock,
        mock_db_connection: MagicMock,
        mock_profile: ConnectionProfile,
        model_facing_schema: DatabaseSchema,
        tmp_path: Any,
    ):
        """Full handoff: Generation produces unexecuted SQL (0 DB calls) -> Local execution -> Metadata synthesis."""
        # 1. Setup Local Agent App
        profile_store = ProfileStore(profiles={"b10_test_profile": mock_profile})
        agent_config = AgentConfig(bind_host="127.0.0.1", token_dir=tmp_path)
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
            caller_engine=mock_db_engine,
            caller_schema=model_facing_schema,
        )
        agent_client = TestClient(agent_app, base_url="http://127.0.0.1:8001")

        # Authenticate local agent session
        bootstrap_token = token_mgr.initialize_token()
        auth_resp = agent_client.post(
            "/agent/v1/auth/bootstrap",
            headers={"X-QueryPilot-Request": "1"},
            json={"bootstrap_token": bootstrap_token},
        )
        assert auth_resp.status_code == status.HTTP_200_OK

        # 2. Local Agent Schema Introspection (using mocked inspector)
        mock_inspector = MagicMock()
        mock_inspector.get_table_names.return_value = ["qp_test_sample"]
        mock_inspector.get_pk_constraint.return_value = {"constrained_columns": ["id"]}
        mock_inspector.get_columns.return_value = [
            {"name": "id", "type": "INTEGER", "nullable": False, "primary_key": True, "default": None, "identity": True, "comment": None},
            {"name": "category", "type": "TEXT", "nullable": False, "primary_key": False, "default": None, "identity": False, "comment": None},
            {"name": "password_hash", "type": "TEXT", "nullable": True, "primary_key": False, "default": None, "identity": False, "comment": None},
        ]
        mock_inspector.get_foreign_keys.return_value = []
        mock_inspector.get_indexes.return_value = []
        mock_inspector.get_unique_constraints.return_value = []

        with patch("query_pilot.db.profile_introspector.inspect", return_value=mock_inspector):
            schema_resp = agent_client.get(
                "/agent/v1/profiles/b10_test_profile/schema",
                headers={"X-QueryPilot-Request": "1"},
            )
            assert schema_resp.status_code == status.HTTP_200_OK
            introspected_schema = schema_resp.json()
            assert introspected_schema["database_name"] == "B10_TestDB"
            introspected_cols = [c["name"].lower() for c in introspected_schema["tables"][0]["columns"]]
            assert "id" in introspected_cols
            assert "category" in introspected_cols
            assert "password_hash" not in introspected_cols

        # 3. Hosted Generation API (Mocked Generator)
        mock_generator = MagicMock(spec=SQLGenerator)
        candidate_sql_text = "SELECT COUNT(*) AS total_records FROM dbo.qp_test_sample;"
        mock_generator.generate.return_value = SQLGenerationResponse(
            status=SQLGenerationStatus.ANSWERABLE,
            sql=candidate_sql_text,
            explanation="Count records in dbo.qp_test_sample.",
        )

        hosted_app = create_app(settings=Settings(allowed_origins=["*"]))
        hosted_app.state.sql_generator = mock_generator
        hosted_client = TestClient(hosted_app)

        client_schema_ctx = ModelFacingSchemaContext(
            database_name="B10_TestDB",
            tables=[
                TableSchemaContext(
                    table_name="qp_test_sample",
                    schema_name="dbo",
                    columns=[
                        ColumnSchemaContext(name="id", data_type="INTEGER", nullable=False),
                        ColumnSchemaContext(name="category", data_type="TEXT", nullable=False),
                    ],
                )
            ],
        )

        # Invariant Check: Generation causes ZERO DB calls
        with patch.object(mock_db_engine, "connect", wraps=mock_db_engine.connect) as spy_connect, \
             patch.object(ProfileExecutionBoundary, "execute") as spy_exec:
            gen_resp = hosted_client.post(
                "/api/v1/generate-sql",
                json={
                    "question": "How many records are in the sample table?",
                    "schema_context": client_schema_ctx.model_dump(),
                    "request_id": "b10-req-gen-001",
                },
            )
            assert gen_resp.status_code == status.HTTP_200_OK
            gen_data = gen_resp.json()
            assert gen_data["candidate_sql"] == candidate_sql_text
            assert gen_data["is_executed"] is False
            assert spy_connect.call_count == 0
            assert spy_exec.call_count == 0

        # 4. Explicit Execution Request to Local Agent
        exec_resp = agent_client.post(
            "/agent/v1/profiles/b10_test_profile/execute",
            headers={"X-QueryPilot-Request": "1"},
            json={
                "sql": candidate_sql_text,
                "request_id": "b10-req-exec-001",
            },
        )
        assert exec_resp.status_code == status.HTTP_200_OK
        exec_data = exec_resp.json()
        assert exec_data["columns"] == ["total_records"]
        assert len(exec_data["rows"]) == 1
        assert exec_data["rows"][0]["total_records"] == 2
        assert exec_data["row_count"] == 1
        assert exec_data["truncated"] is False

        # 5. Hosted Metadata Synthesis (Metadata Only, Zero Rows Passed)
        mock_gemini_client = MagicMock()
        mock_gemini_response = MagicMock()
        mock_gemini_response.text = "The query executed successfully and returned 1 record."
        mock_gemini_client.models.generate_content.return_value = mock_gemini_response

        synth_service = AnswerSynthesisService(gemini_client=mock_gemini_client)
        hosted_app.state.synthesizer = synth_service

        synth_resp = hosted_client.post(
            "/api/v1/synthesize-answer",
            json={
                "question": "How many records are in the sample table?",
                "candidate_sql": candidate_sql_text,
                "columns": ["total_records"],
                "row_count": 1,
                "truncated": False,
                "execution_status": "succeeded",
                "execution_time_ms": 11.2,
                "request_id": "b10-req-synth-001",
            },
        )
        assert synth_resp.status_code == status.HTTP_200_OK
        synth_data = synth_resp.json()
        assert synth_data["status"] == AnswerGroundingStatus.GROUNDED_FROM_METADATA
        assert synth_data["request_id"] == "b10-req-synth-001"

    def test_unsafe_modified_sql_rejected_before_db_dispatch(
        self,
        mock_db_engine: MagicMock,
        mock_db_connection: MagicMock,
        mock_profile: ConnectionProfile,
        model_facing_schema: DatabaseSchema,
        tmp_path: Any,
    ):
        """Mutated or dangerous SQL is rejected by AST validation before execution."""
        profile_store = ProfileStore(profiles={"b10_test_profile": mock_profile})
        agent_config = AgentConfig(bind_host="127.0.0.1", token_dir=tmp_path)
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
            caller_engine=mock_db_engine,
            caller_schema=model_facing_schema,
        )
        agent_client = TestClient(agent_app, base_url="http://127.0.0.1:8001")

        btoken = token_mgr.initialize_token()
        agent_client.post(
            "/agent/v1/auth/bootstrap",
            headers={"X-QueryPilot-Request": "1"},
            json={"bootstrap_token": btoken},
        )

        dangerous_queries = [
            "DROP TABLE qp_test_sample;",
            "INSERT INTO qp_test_sample (id, category) VALUES (3, 'Gamma');",
            "UPDATE qp_test_sample SET category = 'Hacked';",
            "SELECT 1; DROP TABLE qp_test_sample;",
        ]

        for bad_sql in dangerous_queries:
            resp = agent_client.post(
                "/agent/v1/profiles/b10_test_profile/execute",
                headers={"X-QueryPilot-Request": "1"},
                json={"sql": bad_sql, "request_id": "b10-bad-exec"},
            )
            # Must fail closed with HTTP 400 Bad Request
            assert resp.status_code == status.HTTP_400_BAD_REQUEST, (
                f"Expected HTTP 400 for unsafe query '{bad_sql}', got {resp.status_code}: {resp.text}"
            )

        # Invariant: mock execution on the connection was NEVER called for the bad queries
        assert mock_db_connection.execution_options.return_value.execute.call_count == 0
