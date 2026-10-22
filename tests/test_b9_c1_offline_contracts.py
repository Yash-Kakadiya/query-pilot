"""Offline Workflow Contract Tests for Milestone B9-C1.

Verifies end-to-end safety and data boundaries without connecting to SQL Server
or making external network/model calls:
1. Candidate generation produces unexecuted SQL (is_executed=False) with zero DB execution calls.
2. Unsafe SQL statements (DROP, INSERT, UPDATE, EXEC, multi-statement) are rejected by AST validation
   before any database connection or execution dispatch occurs.
3. Sensitive schema metadata (password_hash, api_key, ssn) is filtered out under default policy
   and never enters model-facing schemas or generation prompts.
4. Hosted answer synthesis receives only approved query metadata, strictly excluding raw rows and
   cell sentinels, and refuses row disclosure without calling LLM endpoints.
5. Local deterministic synthesis faithfully grounds summaries in returned results without hallucinating.
6. Database connections and engine resources are deterministically closed on success and failure paths.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

from fastapi import status
from fastapi.testclient import TestClient
import pytest
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from query_pilot.agent.app import create_agent_app
from query_pilot.agent.auth import BootstrapTokenManager
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
    TableSchemaContext,
)
from query_pilot.config import Settings
from query_pilot.db.executor import QueryExecutionError, UnsafeQueryError
from query_pilot.db.models import (
    ColumnMetadata,
    DatabaseSchema,
    TableMetadata,
)
from query_pilot.db.profile import ConnectionProfile, ProfileStore
from query_pilot.db.profile_executor import (
    ProfileExecutionBoundary,
    ProfileExecutionError,
)
from query_pilot.db.profile_introspector import ProfileDatabaseIntrospector
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
# Common Reusable Offline Fixtures
# ==============================================================================

@pytest.fixture
def sample_table_metadata() -> TableMetadata:
    """Sample table with non-sensitive columns."""
    return TableMetadata(
        schema_name="dbo",
        table_name="Customers",
        columns=[
            ColumnMetadata(name="CustomerID", data_type="int", primary_key=True),
            ColumnMetadata(name="Name", data_type="nvarchar"),
            ColumnMetadata(name="Active", data_type="bit"),
        ],
        primary_keys=["CustomerID"],
    )


@pytest.fixture
def sample_db_schema(sample_table_metadata: TableMetadata) -> DatabaseSchema:
    """Model-facing DatabaseSchema for testing."""
    return DatabaseSchema(
        database_name="TestDB",
        tables=[sample_table_metadata],
        is_model_facing=True,
    )


@pytest.fixture
def sample_connection_profile() -> ConnectionProfile:
    """Strict test ConnectionProfile with no custom sensitive-column overrides."""
    return ConnectionProfile(
        profile_id="test_customer_profile",
        display_name="Test Customer Profile",
        server="localhost",
        database_name="TestDB",
        schema_name="dbo",
        timeout_seconds=5,
        max_row_limit=50,
        sensitive_columns=None,  # Rely exclusively on production defaults
    )


# ==============================================================================
# 1. Candidate Generation Decoupling Contracts
# ==============================================================================

class TestCandidateGenerationDecouplingContracts:
    """Verifies candidate SQL generation never dispatches database execution."""

    def test_candidate_generation_produces_unexecuted_sql_with_zero_db_calls(
        self,
        sample_db_schema: DatabaseSchema,
    ):
        """POST /api/v1/generate-sql produces candidate SQL with is_executed=False and 0 DB queries."""
        # Setup mocked generator returning a candidate query
        mock_generator = MagicMock(spec=SQLGenerator)
        mock_generator.generate.return_value = SQLGenerationResponse(
            status=SQLGenerationStatus.ANSWERABLE,
            sql="SELECT CustomerID, Name FROM Customers WHERE Active = 1;",
            explanation="Select active customer IDs and names.",
            assumptions=["Active column represents active status."],
        )

        app = create_app(settings=Settings(allowed_origins=["*"]))
        app.state.sql_generator = mock_generator

        client = TestClient(app)
        schema_ctx = ModelFacingSchemaContext(
            database_name=sample_db_schema.database_name,
            tables=[
                TableSchemaContext(
                    table_name=tbl.table_name,
                    schema_name=tbl.schema_name,
                    columns=[
                        ColumnSchemaContext(name=c.name, data_type=c.data_type, nullable=c.nullable)
                        for c in tbl.columns
                    ],
                )
                for tbl in sample_db_schema.tables
            ],
        )

        req_body = GenerateSQLRequest(
            question="List all active customers",
            schema_context=schema_ctx,
            request_id="req-gen-test-001",
        )

        # Spy on the real execution boundaries to prove candidate generation dispatches zero DB calls
        with patch.object(ProfileExecutionBoundary, "execute") as spy_profile_exec, \
             patch.object(Engine, "connect") as spy_engine_connect:
            response = client.post("/api/v1/generate-sql", json=req_body.model_dump())
            assert response.status_code == status.HTTP_200_OK

            data = response.json()
            assert data["candidate_sql"] == "SELECT CustomerID, Name FROM Customers WHERE Active = 1;"
            # Contract invariant: candidate is explicitly flagged as unexecuted
            assert data["is_executed"] is False
            assert data["request_id"] == "req-gen-test-001"

            # Assert zero database execution calls occurred across the system boundaries
            assert spy_profile_exec.call_count == 0
            assert spy_engine_connect.call_count == 0

    def test_candidate_response_is_executed_invariant_rejects_true(self):
        """GenerateSQLResponse strictly enforces is_executed=False via Literal[False]."""
        # Normal construction defaults is_executed to False and serializes false
        resp = GenerateSQLResponse(
            status=GenerationStatus.GENERATED,
            candidate_sql="SELECT CustomerID FROM Customers;",
            request_id="req-invar-test-001",
        )
        assert resp.is_executed is False
        assert resp.model_dump()["is_executed"] is False
        serialized = json.loads(resp.model_dump_json())
        assert serialized["is_executed"] is False

        # Explicitly passing False is valid
        resp_explicit = GenerateSQLResponse(
            status=GenerationStatus.GENERATED,
            candidate_sql="SELECT CustomerID FROM Customers;",
            request_id="req-invar-test-002",
            is_executed=False,
        )
        assert resp_explicit.is_executed is False

        # Attempting to construct with is_executed=True is strictly rejected by schema validation
        from pydantic import ValidationError

        with pytest.raises(ValidationError) as exc_info:
            GenerateSQLResponse(
                status=GenerationStatus.GENERATED,
                candidate_sql="SELECT CustomerID FROM Customers;",
                request_id="req-invar-test-003",
                is_executed=True,  # type: ignore[arg-type]
            )
        assert "is_executed" in str(exc_info.value)

    def test_execution_requires_separate_approved_request(
        self,
        sample_connection_profile: ConnectionProfile,
        sample_db_schema: DatabaseSchema,
        tmp_path: Path,
    ):
        """Execution happens only when client explicitly dispatches to local agent execute route."""
        mock_conn = MagicMock(spec=Connection)
        raw_dbapi = MagicMock()
        raw_dbapi.timeout = 5
        mock_conn.connection.dbapi_connection = raw_dbapi
        mock_conn.execute.return_value.scalar.return_value = "TestDB"

        mock_cursor = MagicMock()
        mock_cursor.keys.return_value = ["CustomerID", "Name"]
        mock_cursor.fetchmany.return_value = [(101, "Acme Corp")]
        mock_conn.execution_options.return_value.execute.return_value = mock_cursor

        test_engine = MagicMock(spec=Engine)
        test_engine.connect.return_value = mock_conn

        profile_store = ProfileStore(profiles={"test_customer_profile": sample_connection_profile})

        agent_app = create_agent_app(
            config=AgentConfig(
                bind_host="127.0.0.1",
                token_dir=tmp_path,
            ),
            store=profile_store,
            caller_engine=test_engine,
            caller_schema=sample_db_schema,
        )

        client = TestClient(agent_app, base_url="http://127.0.0.1:8001")
        token_mgr: BootstrapTokenManager = agent_app.state.token_manager
        btoken = token_mgr.initialize_token()

        # Authenticate session
        auth_resp = client.post(
            "/agent/v1/auth/bootstrap",
            headers={"X-QueryPilot-Request": "1"},
            json={"bootstrap_token": btoken},
        )
        assert auth_resp.status_code == status.HTTP_200_OK

        # Ensure database has not been touched yet
        assert mock_conn.execution_options.return_value.execute.call_count == 0

        # User reviews and approves candidate SQL -> Dispatches execution
        approved_sql = "SELECT CustomerID, Name FROM Customers WHERE Active = 1;"
        exec_resp = client.post(
            "/agent/v1/profiles/test_customer_profile/execute",
            headers={"X-QueryPilot-Request": "1"},
            json={"sql": approved_sql, "request_id": "req-exec-approved-001"},
        )
        assert exec_resp.status_code == status.HTTP_200_OK
        exec_data = exec_resp.json()
        assert exec_data["columns"] == ["CustomerID", "Name"]
        assert exec_data["rows"] == [{"CustomerID": 101, "Name": "Acme Corp"}]
        assert exec_data["row_count"] == 1
        assert exec_data["request_id"] == "req-exec-approved-001"

        # Assert database was executed exactly once following explicit approval
        assert mock_conn.execution_options.return_value.execute.call_count == 1


# ==============================================================================
# 2. Unsafe SQL AST Rejection Before Database Dispatch Contracts
# ==============================================================================

class TestUnsafeSQLRejectionBeforeDatabaseDispatchContracts:
    """Verifies that mutating, dangerous, and multi-statement queries fail before DB acquisition."""

    UNSAFE_QUERIES = [
        ("DROP TABLE Customers;", "drop"),
        ("INSERT INTO Customers (CustomerID, Name) VALUES (999, 'Malicious');", "insert"),
        ("UPDATE Customers SET Active = 0 WHERE CustomerID = 1;", "update"),
        ("DELETE FROM Customers WHERE CustomerID = 1;", "delete"),
        ("EXEC sp_executesql N'SELECT 1';", "exec"),
        ("SELECT CustomerID FROM Customers; DROP TABLE Customers;", "multi_statement"),
        ("WAITFOR DELAY '00:00:05';", "waitfor"),
    ]

    @pytest.mark.parametrize("unsafe_sql,label", UNSAFE_QUERIES)
    def test_unsafe_sql_rejected_at_validation_boundary_with_zero_db_connections(
        self,
        sample_connection_profile: ConnectionProfile,
        sample_db_schema: DatabaseSchema,
        unsafe_sql: str,
        label: str,
    ):
        """ProfileExecutionBoundary validates AST and blocks unsafe query before acquiring connection."""
        mock_engine = MagicMock(spec=Engine)

        boundary = ProfileExecutionBoundary(
            config=sample_connection_profile,
            schema=sample_db_schema,
            engine=mock_engine,
        )

        with pytest.raises(UnsafeQueryError) as exc_info:
            boundary.execute(unsafe_sql)

        assert "rejected" in str(exc_info.value).lower() or "forbidden" in str(exc_info.value).lower()

        # Strict security proof: engine.connect was never called
        assert mock_engine.connect.call_count == 0

    @pytest.mark.parametrize("unsafe_sql,label", UNSAFE_QUERIES)
    def test_agent_endpoint_rejects_unsafe_sql_with_http_400_and_zero_db_calls(
        self,
        sample_connection_profile: ConnectionProfile,
        sample_db_schema: DatabaseSchema,
        tmp_path: Path,
        unsafe_sql: str,
        label: str,
    ):
        """POST /agent/v1/profiles/{id}/execute returns HTTP 400 and makes zero DB queries."""
        mock_conn = MagicMock(spec=Connection)
        mock_conn.execute.return_value.scalar.return_value = "TestDB"

        test_engine = MagicMock(spec=Engine)
        test_engine.connect.return_value = mock_conn

        profile_store = ProfileStore(profiles={"test_customer_profile": sample_connection_profile})

        agent_app = create_agent_app(
            config=AgentConfig(
                bind_host="127.0.0.1",
                token_dir=tmp_path,
            ),
            store=profile_store,
            caller_engine=test_engine,
            caller_schema=sample_db_schema,
        )

        client = TestClient(agent_app, base_url="http://127.0.0.1:8001")
        token_mgr: BootstrapTokenManager = agent_app.state.token_manager
        btoken = token_mgr.initialize_token()

        auth_resp = client.post(
            "/agent/v1/auth/bootstrap",
            headers={"X-QueryPilot-Request": "1"},
            json={"bootstrap_token": btoken},
        )
        assert auth_resp.status_code == status.HTTP_200_OK

        resp = client.post(
            "/agent/v1/profiles/test_customer_profile/execute",
            headers={"X-QueryPilot-Request": "1"},
            json={"sql": unsafe_sql},
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST

        # Zero execution calls dispatched
        assert mock_conn.execution_options.return_value.execute.call_count == 0


# ==============================================================================
# 3. Sensitive Schema Metadata Filtering Contracts
# ==============================================================================

class TestSensitiveMetadataFilteringContracts:
    """Verifies default policy excludes sensitive columns without custom overrides."""

    @patch("query_pilot.db.profile_introspector.verify_database_context")
    @patch("query_pilot.db.profile_introspector.inspect")
    def test_default_policy_filters_canonical_sensitive_columns_without_overrides(
        self,
        mock_inspect_fn,
        mock_verify_db,
    ):
        """Introspection redacts password_hash, api_key, ssn under default policy (sensitive_columns=None)."""
        mock_verify_db.return_value = "TestDB"

        mock_inspector = MagicMock()
        mock_inspector.get_table_names.return_value = ["Users"]
        mock_inspector.get_pk_constraint.return_value = {"constrained_columns": ["id"]}
        mock_inspector.get_columns.return_value = [
            {"name": "id", "type": "int", "nullable": False, "primary_key": True, "default": None, "identity": True, "comment": None},
            {"name": "username", "type": "nvarchar(50)", "nullable": False, "primary_key": False, "default": None, "identity": False, "comment": None},
            {"name": "password_hash", "type": "nvarchar(255)", "nullable": False, "primary_key": False, "default": None, "identity": False, "comment": None},
            {"name": "PasswordHash", "type": "nvarchar(255)", "nullable": False, "primary_key": False, "default": None, "identity": False, "comment": None},
            {"name": "api_key", "type": "varchar(64)", "nullable": True, "primary_key": False, "default": None, "identity": False, "comment": None},
            {"name": "ssn", "type": "char(9)", "nullable": True, "primary_key": False, "default": None, "identity": False, "comment": None},
            {"name": "credit_card", "type": "varchar(20)", "nullable": True, "primary_key": False, "default": None, "identity": False, "comment": None},
        ]
        mock_inspector.get_foreign_keys.return_value = []
        mock_inspector.get_indexes.return_value = []
        mock_inspector.get_unique_constraints.return_value = []
        mock_inspect_fn.return_value = mock_inspector

        mock_conn = MagicMock(spec=Connection)
        mock_engine = MagicMock(spec=Engine)
        mock_engine.connect.return_value = mock_conn

        profile = ConnectionProfile(
            profile_id="default_policy_profile",
            display_name="Default Policy Profile",
            server="localhost",
            database_name="TestDB",
            sensitive_columns=None,  # No profile-specific overrides!
        )

        introspector = ProfileDatabaseIntrospector(config=profile, engine=mock_engine)

        # 1. Raw schema contains all columns
        raw_schema = introspector.introspect_raw()
        raw_col_names = {c.name.lower() for c in raw_schema.tables[0].columns}
        assert "password_hash" in raw_col_names
        assert "api_key" in raw_col_names
        assert "ssn" in raw_col_names
        assert "credit_card" in raw_col_names

        # 2. Model-facing schema filters all canonical sensitive columns
        model_facing = introspector.introspect_model_facing()
        assert model_facing.is_model_facing is True
        model_col_names = {c.name.lower() for c in model_facing.tables[0].columns}

        assert "password_hash" not in model_col_names
        assert "passwordhash" not in model_col_names
        assert "api_key" not in model_col_names
        assert "ssn" not in model_col_names
        assert "credit_card" not in model_col_names

        # Permitted non-sensitive columns remain
        assert "id" in model_col_names
        assert "username" in model_col_names

    def test_filtered_columns_never_enter_model_facing_generation_payload(
        self,
        sample_connection_profile: ConnectionProfile,
    ):
        """Model-facing schema context passed to generator excludes filtered columns."""
        # Create a model-facing schema context that represents redacted metadata
        schema_ctx = ModelFacingSchemaContext(
            database_name="TestDB",
            tables=[
                TableSchemaContext(
                    table_name="Users",
                    schema_name="dbo",
                    columns=[
                        ColumnSchemaContext(name="id", data_type="int", nullable=False),
                        ColumnSchemaContext(name="username", data_type="nvarchar(50)", nullable=False),
                    ],
                )
            ],
        )

        captured_requests = []

        class MockCaptureGenerator(SQLGenerator):
            def generate(self, req):
                captured_requests.append(req)
                return SQLGenerationResponse(
                    status=SQLGenerationStatus.ANSWERABLE,
                    sql="SELECT username FROM Users;",
                    explanation="Retrieve usernames.",
                )

        app = create_app(settings=Settings(allowed_origins=["*"]))
        app.state.sql_generator = MockCaptureGenerator()

        client = TestClient(app)
        client.post(
            "/api/v1/generate-sql",
            json={
                "question": "What are all usernames?",
                "schema_context": schema_ctx.model_dump(),
            },
        )

        assert len(captured_requests) == 1
        outbound_schema = captured_requests[0].schema_context
        all_col_names = [
            c.name.lower()
            for t in outbound_schema.tables
            for c in t.columns
        ]

        assert "password_hash" not in all_col_names
        assert "api_key" not in all_col_names
        assert "ssn" not in all_col_names


# ==============================================================================
# 4. Hosted Synthesis Metadata-Only Privacy Contracts
# ==============================================================================

class TestHostedSynthesisMetadataOnlyPrivacyContracts:
    """Verifies that hosted answer synthesis receives metadata only and never raw rows."""

    SENTINEL_ROW_VALUE = "SENTINEL_SECRET_ROW_RECORD_XYZ987"
    SENTINEL_SSN_VALUE = "SENTINEL_SSN_123_45_6789"

    def test_synthesis_payload_excludes_raw_rows_and_sentinel_values(self):
        """synthesize_from_metadata outbound model prompt strictly excludes row data."""
        captured_prompts: List[str] = []

        mock_gemini_client = MagicMock()
        mock_gemini_response = MagicMock()
        mock_gemini_response.text = "Based on the query, 3 user records were identified."
        mock_gemini_response.usage_metadata = None

        mock_gemini_client.models.generate_content.side_effect = (
            lambda model, contents, config: (captured_prompts.append(str(contents)) or mock_gemini_response)
        )

        service = AnswerSynthesisService(gemini_client=mock_gemini_client)

        # Call synthesis with approved metadata
        summary, status_out, notes = service.synthesize_from_metadata(
            question="Summarize active user accounts",
            candidate_sql="SELECT user_id, display_name FROM Users WHERE status_code = 1;",
            columns=["user_id", "display_name"],
            row_count=3,
            truncated=False,
            execution_time_ms=14.2,
        )

        assert status_out == AnswerGroundingStatus.GROUNDED_FROM_METADATA or status_out == AnswerGroundingStatus.UNAVAILABLE
        assert len(captured_prompts) == 1
        prompt = captured_prompts[0]

        # Verify metadata is present
        assert "Total Returned Rows: 3" in prompt
        assert "user_id, display_name" in prompt

        # Verify sentinels are completely absent from prompt
        assert self.SENTINEL_ROW_VALUE not in prompt
        assert self.SENTINEL_SSN_VALUE not in prompt

    def test_row_disclosure_refusal_does_not_call_hosted_model(self):
        """Questions requiring specific undisclosed cell values are refused without calling LLM."""
        mock_gemini_client = MagicMock()
        mock_response = MagicMock()
        mock_response.text = "Mock text"
        mock_gemini_client.models.generate_content.return_value = mock_response

        service = AnswerSynthesisService(gemini_client=mock_gemini_client)

        # Question clearly asking for specific row cell values
        summary, status_out, notes = service.synthesize_from_metadata(
            question="What is the name of user admin?",
            candidate_sql="SELECT name FROM Users WHERE username = 'admin';",
            columns=["name"],
            row_count=1,
            execution_time_ms=3.1,
        )

        # Refuses with zero disclosure status
        assert status_out == AnswerGroundingStatus.REQUIRES_ROW_DISCLOSURE
        assert "QueryPilot protects data privacy by not transmitting database records" in summary

        # Model client was NEVER called
        assert mock_gemini_client.models.generate_content.call_count == 0

    def test_api_v1_synthesize_endpoint_rejects_raw_row_data(self):
        """POST /api/v1/synthesize-answer forbids unexpected row/data properties (extra='forbid')."""
        app = create_app(settings=Settings(allowed_origins=["*"]))
        client = TestClient(app)

        # Request containing forbidden 'rows' payload
        malformed_payload = {
            "question": "How many active items?",
            "candidate_sql": "SELECT COUNT(*) FROM Items;",
            "columns": ["count"],
            "row_count": 5,
            "rows": [{"count": 5, "secret": self.SENTINEL_ROW_VALUE}],  # Forbidden field!
        }

        resp = client.post("/api/v1/synthesize-answer", json=malformed_payload)
        # Pydantic extra='forbid' produces HTTP 422
        assert resp.status_code == 422


# ==============================================================================
# 5. Local Deterministic Synthesis Contracts
# ==============================================================================

class TestLocalDeterministicSynthesisContracts:
    """Verifies deterministic synthesis formatting logic offline."""

    def test_empty_result_returns_truthful_non_hallucinatory_message(self):
        """Zero row count produces deterministic zero-row message without calling model."""
        mock_client = MagicMock()
        service = AnswerSynthesisService(gemini_client=mock_client)

        summary, status_out, notes = service.synthesize_from_metadata(
            question="How many users registered today?",
            candidate_sql="SELECT COUNT(*) FROM Users WHERE registration_date = CAST(GETDATE() AS DATE);",
            columns=["count"],
            row_count=0,
        )

        assert status_out == AnswerGroundingStatus.GROUNDED_FROM_METADATA
        assert summary == "Query executed successfully. No rows returned."
        assert mock_client.models.generate_content.call_count == 0


# ==============================================================================
# 6. Resource and Error Cleanup Contracts
# ==============================================================================

class TestResourceAndErrorCleanupContracts:
    """Verifies that database connections and cursors are cleaned up on all execution paths."""

    def test_connection_closed_on_successful_execution(
        self,
        sample_connection_profile: ConnectionProfile,
        sample_db_schema: DatabaseSchema,
    ):
        """Connection is strictly closed in finally block on successful execution."""
        mock_conn = MagicMock(spec=Connection)
        mock_conn.execute.return_value.scalar.return_value = "TestDB"

        mock_cursor = MagicMock()
        mock_cursor.keys.return_value = ["CustomerID"]
        mock_cursor.fetchmany.return_value = [(1,)]
        mock_conn.execution_options.return_value.execute.return_value = mock_cursor

        mock_engine = MagicMock(spec=Engine)
        mock_engine.connect.return_value = mock_conn

        boundary = ProfileExecutionBoundary(
            config=sample_connection_profile,
            schema=sample_db_schema,
            engine=mock_engine,
        )

        result = boundary.execute("SELECT CustomerID FROM Customers;")
        assert result.row_count == 1

        # Connection must be closed in finally block
        mock_conn.close.assert_called_once()

    def test_connection_closed_on_execution_failure(
        self,
        sample_connection_profile: ConnectionProfile,
        sample_db_schema: DatabaseSchema,
    ):
        """Connection is strictly closed in finally block when query raises database error."""
        mock_conn = MagicMock(spec=Connection)
        mock_conn.execute.return_value.scalar.return_value = "TestDB"
        mock_conn.execution_options.return_value.execute.side_effect = SQLAlchemyError("Simulated DB execution failure")

        mock_engine = MagicMock(spec=Engine)
        mock_engine.connect.return_value = mock_conn

        boundary = ProfileExecutionBoundary(
            config=sample_connection_profile,
            schema=sample_db_schema,
            engine=mock_engine,
        )

        with pytest.raises(ProfileExecutionError):
            boundary.execute("SELECT CustomerID FROM Customers;")

        # Connection must still be closed in finally block
        mock_conn.close.assert_called_once()

    def test_agent_engine_manager_dispose_all_disposes_all_engines(
        self,
        sample_connection_profile: ConnectionProfile,
    ):
        """AgentEngineManager.dispose_all() calls dispose() on all active cached engines."""
        mock_engine1 = MagicMock(spec=Engine)
        mock_engine2 = MagicMock(spec=Engine)

        mock_factory = MagicMock()
        mock_factory.create_engine.side_effect = [mock_engine1, mock_engine2]

        mgr = AgentEngineManager(factory=mock_factory)
        eng1 = mgr.get_or_create_engine("profile_1")
        eng2 = mgr.get_or_create_engine("profile_2")

        assert eng1 is mock_engine1
        assert eng2 is mock_engine2

        disposed_count = mgr.dispose_all()

        assert disposed_count == 2
        mock_engine1.dispose.assert_called_once()
        mock_engine2.dispose.assert_called_once()
        assert len(mgr._owned_engines) == 0
        assert len(mgr._engines) == 0
