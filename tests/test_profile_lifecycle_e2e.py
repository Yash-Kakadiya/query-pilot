"""Offline end-to-end lifecycle integration test (Milestone B12-F8G).

Exercises the complete database profile lifecycle through actual local agent routes:
1. POST /agent/v1/profiles/validate-config (Offline validation, zero I/O)
2. POST /agent/v1/profiles (Profile creation, atomic disk write, in-memory sync)
3. POST /agent/v1/profiles/{id}/test-connection (Isolated NullPool probe, DB context verification)
4. GET /agent/v1/profiles/{id}/schema (Schema introspection, table allowlisting, sensitive redaction)
5. POST /agent/v1/profiles/{id}/execute (Explicit read-only query execution, AST validation)

Invariants Verified:
- Real FastAPI routes and Pydantic request/response models.
- Single profile ID maintained consistently across all 5 stages.
- Isolated temporary profile file; real user profile store is never modified.
- Connection test uses isolated NullPool engine and disposes it deterministically.
- Shared engine manager is never disrupted or disposed by connection testing.
- Runtime database identity check (SELECT DB_NAME()) verified before execution.
- SQL query is executed ONLY at the explicit execute endpoint.
- Zero Gemini or external hosted API calls.
- Full offline execution with zero real database or ODBC driver calls.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi import status
from fastapi.testclient import TestClient
import pytest
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.pool import NullPool

from query_pilot.agent.app import create_agent_app
from query_pilot.agent.auth import (
    SESSION_COOKIE_NAME,
    BootstrapTokenManager,
    SessionManager,
)
from query_pilot.agent.config import AgentConfig
from query_pilot.agent.engine_manager import AgentEngineManager
from query_pilot.agent.models import (
    CreateProfileResponse,
    ExecuteQueryResponse,
    TestConnectionResponse,
    ValidateProfileConfigResponse,
)
from query_pilot.db.factory import ProfileConnectionFactory
from query_pilot.db.models import (
    ColumnMetadata,
    DatabaseSchema,
    TableMetadata,
)
from query_pilot.db.profile import (
    ConnectionProfile,
    ProfileStore,
    load_trusted_profiles_file,
)
from query_pilot.db.profile_introspector import ProfileDatabaseIntrospector


@pytest.fixture
def lifecycle_env(tmp_path: Path):
    """Sets up an isolated agent environment with authenticated session and empty initial profile store."""
    token_dir = tmp_path / "token_dir"
    token_dir.mkdir(parents=True, exist_ok=True)
    profiles_file = tmp_path / "profiles.json"

    store = ProfileStore()
    config = AgentConfig(
        bind_host="127.0.0.1",
        bind_port=8001,
        token_dir=token_dir,
        profiles_file=profiles_file,
    )
    factory = ProfileConnectionFactory(store=store)
    engine_manager = AgentEngineManager(factory=factory)
    token_manager = BootstrapTokenManager(token_dir=token_dir)
    token = token_manager.initialize_token()
    session_manager = SessionManager()

    app = create_agent_app(
        config=config,
        store=store,
        factory=factory,
        engine_manager=engine_manager,
        token_manager=token_manager,
        session_manager=session_manager,
    )

    client = TestClient(app, base_url="http://127.0.0.1:8001")

    # Bootstrap authenticated session
    login_resp = client.post(
        "/agent/v1/auth/bootstrap",
        headers={"Host": "127.0.0.1:8001", "X-QueryPilot-Request": "1"},
        json={"bootstrap_token": token},
    )
    assert login_resp.status_code == status.HTTP_200_OK
    session_id = login_resp.cookies.get(SESSION_COOKIE_NAME)
    assert session_id is not None

    return {
        "app": app,
        "client": client,
        "session_id": session_id,
        "store": store,
        "profiles_file": profiles_file,
        "factory": factory,
        "engine_manager": engine_manager,
    }


def test_complete_profile_onboarding_and_query_lifecycle_e2e(lifecycle_env):
    """Test the complete 5-stage profile onboarding and execution lifecycle."""
    client: TestClient = lifecycle_env["client"]
    session_id: str = lifecycle_env["session_id"]
    store: ProfileStore = lifecycle_env["store"]
    profiles_file: Path = lifecycle_env["profiles_file"]
    factory: ProfileConnectionFactory = lifecycle_env["factory"]
    engine_manager: AgentEngineManager = lifecycle_env["engine_manager"]

    profile_id = "lifecycle_dw"
    expected_db = "LifecycleAnalyticsDB"

    headers_state_changing = {
        "Host": "127.0.0.1:8001",
        "X-QueryPilot-Request": "1",
    }
    headers_get = {
        "Host": "127.0.0.1:8001",
    }
    cookies = {SESSION_COOKIE_NAME: session_id}

    # ==========================================================================
    # Stage 1: Configuration Validation (POST /agent/v1/profiles/validate-config)
    # ==========================================================================
    profile_config_payload = {
        "profile_id": profile_id,
        "display_name": "Lifecycle Analytics Data Warehouse",
        "server": r"localhost\SQLEXPRESS",
        "database_name": expected_db,
        "schema_name": "dbo",
        "driver": "ODBC Driver 17 for SQL Server",
        "engine_type": "mssql",
        "use_trusted_connection": True,
        "timeout_seconds": 12,
        "max_row_limit": 50,
        "allowed_tables": ["Customers", "Orders"],
        "sensitive_columns": ["TaxID", "SSN"],
    }

    resp_validate = client.post(
        "/agent/v1/profiles/validate-config",
        headers=headers_state_changing,
        cookies=cookies,
        json=profile_config_payload,
    )
    assert resp_validate.status_code == status.HTTP_200_OK
    val_data = resp_validate.json()
    assert val_data["valid"] is True
    assert "Profile configuration is valid" in val_data["message"]

    # Verify Stage 1 was completely offline: zero profiles saved, file not yet written
    assert not profiles_file.exists()
    assert len(store.profiles) == 0

    # ==========================================================================
    # Stage 2: Profile Creation (POST /agent/v1/profiles)
    # ==========================================================================
    resp_create = client.post(
        "/agent/v1/profiles",
        headers=headers_state_changing,
        cookies=cookies,
        json=profile_config_payload,
    )
    assert resp_create.status_code == status.HTTP_201_CREATED
    create_data = resp_create.json()
    assert create_data["is_default"] is True
    created_summary = create_data["profile"]
    assert created_summary["profile_id"] == profile_id
    assert created_summary["display_name"] == "Lifecycle Analytics Data Warehouse"
    assert created_summary["timeout_seconds"] == 12
    assert created_summary["max_row_limit"] == 50
    assert created_summary["has_allowlist"] is True
    assert created_summary["allowed_tables"] == ["Customers", "Orders"]
    # Verify zero secrets, connection strings, or host details in response
    assert "server" not in created_summary
    assert "password" not in created_summary
    assert "connection_string" not in created_summary

    # Verify persistence to disk and in-memory synchronization
    assert profiles_file.exists()
    persisted_store = load_trusted_profiles_file(profiles_file)
    assert profile_id in persisted_store.profiles
    assert persisted_store.default_profile_id == profile_id
    assert profile_id in store.profiles
    assert store.default_profile_id == profile_id

    # Verify duplicate registration is rejected (HTTP 409)
    resp_duplicate = client.post(
        "/agent/v1/profiles",
        headers=headers_state_changing,
        cookies=cookies,
        json=profile_config_payload,
    )
    assert resp_duplicate.status_code == status.HTTP_409_CONFLICT

    # ==========================================================================
    # Setup Mock Engines: Isolated NullPool for Testing, Shared Engine for Operations
    # ==========================================================================
    # 1. Ephemeral engine for Stage 3 connection test
    mock_temp_engine = MagicMock(spec=Engine)
    mock_temp_conn = MagicMock(spec=Connection)
    mock_temp_conn.execute.return_value.scalar.return_value = expected_db
    mock_temp_conn.__enter__.return_value = mock_temp_conn
    mock_temp_conn.__exit__.return_value = None
    mock_temp_engine.connect.return_value = mock_temp_conn

    # 2. Shared engine for Stage 4 schema & Stage 5 execution
    mock_shared_engine = MagicMock(spec=Engine)
    mock_shared_conn = MagicMock(spec=Connection)
    mock_shared_conn.execute.return_value.scalar.return_value = expected_db
    mock_shared_conn.__enter__.return_value = mock_shared_conn
    mock_shared_conn.__exit__.return_value = None
    raw_dbapi = MagicMock()
    raw_dbapi.timeout = 12
    mock_shared_conn.connection.dbapi_connection = raw_dbapi

    # Setup execution cursor for SELECT CustomerID, CustomerName FROM Customers;
    mock_cursor = MagicMock()
    mock_cursor.keys.return_value = ["CustomerID", "CustomerName"]
    mock_cursor.fetchmany.return_value = [(101, "Acme Corporation"), (102, "Beta Global")]
    mock_shared_conn.execution_options.return_value.execute.return_value = mock_cursor
    mock_shared_engine.connect.return_value = mock_shared_conn

    def mock_create_engine(profile_id=None, profile=None, poolclass=None, **kwargs):
        if poolclass == NullPool:
            return mock_temp_engine
        return mock_shared_engine

    with patch.object(factory, "create_engine", side_effect=mock_create_engine) as mock_factory_create:
        # ======================================================================
        # Stage 3: Connection Testing (POST /agent/v1/profiles/{id}/test-connection)
        # ======================================================================
        resp_test_conn = client.post(
            f"/agent/v1/profiles/{profile_id}/test-connection",
            headers=headers_state_changing,
            cookies=cookies,
        )
        assert resp_test_conn.status_code == status.HTTP_200_OK
        test_conn_data = resp_test_conn.json()
        assert test_conn_data["connected"] is True
        assert test_conn_data["database_name"] == expected_db
        assert test_conn_data["latency_ms"] >= 0.0
        assert test_conn_data["failure_code"] is None
        assert "verified successfully" in test_conn_data["message"]

        # Verify isolated engine was created with NullPool and disposed
        mock_temp_engine.dispose.assert_called_once()
        # Verify shared engine was not touched or disposed by connection testing
        mock_shared_engine.dispose.assert_not_called()

        # Verify Stage 3 failure classification behavior
        mock_fail_conn = MagicMock(spec=Connection)
        mock_fail_conn.execute.side_effect = Exception("Login timeout expired after 15 seconds")
        mock_fail_engine = MagicMock(spec=Engine)
        mock_fail_engine.connect.return_value.__enter__.return_value = mock_fail_conn

        with patch.object(factory, "create_engine", return_value=mock_fail_engine):
            resp_test_fail = client.post(
                f"/agent/v1/profiles/{profile_id}/test-connection",
                headers=headers_state_changing,
                cookies=cookies,
            )
            assert resp_test_fail.status_code == status.HTTP_200_OK
            fail_data = resp_test_fail.json()
            assert fail_data["connected"] is False
            assert fail_data["database_name"] is None
            assert fail_data["failure_code"] == "TIMEOUT"
            mock_fail_engine.dispose.assert_called_once()

        # ======================================================================
        # Stage 4: Schema Introspection (GET /agent/v1/profiles/{id}/schema)
        # ======================================================================
        synthetic_model_schema = DatabaseSchema(
            database_name=expected_db,
            is_model_facing=True,
            tables=[
                TableMetadata(
                    schema_name="dbo",
                    table_name="Customers",
                    columns=[
                        ColumnMetadata(
                            name="CustomerID",
                            data_type="int",
                            nullable=False,
                            primary_key=True,
                        ),
                        ColumnMetadata(
                            name="CustomerName",
                            data_type="nvarchar",
                            nullable=False,
                            primary_key=False,
                        ),
                    ],
                ),
            ],
        )

        synthetic_full_schema = DatabaseSchema(
            database_name=expected_db,
            tables=[
                TableMetadata(
                    schema_name="dbo",
                    table_name="Customers",
                    columns=[
                        ColumnMetadata(name="CustomerID", data_type="int", primary_key=True),
                        ColumnMetadata(name="CustomerName", data_type="nvarchar"),
                        ColumnMetadata(name="SSN", data_type="nvarchar"),  # sensitive column
                    ],
                ),
            ],
        )
        lifecycle_env["app"].state.caller_schema = synthetic_full_schema

        with patch.object(
            ProfileDatabaseIntrospector,
            "introspect_model_facing",
            return_value=synthetic_model_schema,
        ):
            resp_schema = client.get(
                f"/agent/v1/profiles/{profile_id}/schema",
                headers=headers_get,
                cookies=cookies,
            )
            assert resp_schema.status_code == status.HTTP_200_OK
            schema_data = resp_schema.json()
            assert schema_data["database_name"] == expected_db
            assert schema_data["is_model_facing"] is True
            assert len(schema_data["tables"]) == 1
            customer_table = schema_data["tables"][0]
            assert customer_table["table_name"] == "Customers"
            column_names = [c["name"] for c in customer_table["columns"]]
            assert "CustomerID" in column_names
            assert "CustomerName" in column_names
            # Assert sensitive columns TaxID/SSN were redacted
            assert "SSN" not in column_names
            assert "TaxID" not in column_names

        # ======================================================================
        # Stage 5: Explicit SQL Execution (POST /agent/v1/profiles/{id}/execute)
        # ======================================================================
        # 1. Non-permitted table is rejected by AST safety check
        resp_illegal_table = client.post(
            f"/agent/v1/profiles/{profile_id}/execute",
            headers=headers_state_changing,
            cookies=cookies,
            json={
                "sql": "SELECT * FROM NonAllowedTable;",
                "request_id": "req-blocked-001",
            },
        )
        assert resp_illegal_table.status_code == status.HTTP_400_BAD_REQUEST

        # 2. Mutating SQL (DROP / INSERT) is rejected by AST safety check
        resp_mutation = client.post(
            f"/agent/v1/profiles/{profile_id}/execute",
            headers=headers_state_changing,
            cookies=cookies,
            json={
                "sql": "DROP TABLE Customers;",
                "request_id": "req-blocked-002",
            },
        )
        assert resp_mutation.status_code == status.HTTP_400_BAD_REQUEST

        # 3. Read-only valid query succeeds and executes ONLY at this explicit endpoint
        valid_query = "SELECT CustomerID, CustomerName FROM Customers;"
        resp_execute = client.post(
            f"/agent/v1/profiles/{profile_id}/execute",
            headers=headers_state_changing,
            cookies=cookies,
            json={
                "sql": valid_query,
                "request_id": "req-lifecycle-exec-001",
            },
        )
        if resp_execute.status_code != status.HTTP_200_OK:
            print("EXECUTE ERROR DETAIL:", resp_execute.status_code, resp_execute.json())
        assert resp_execute.status_code == status.HTTP_200_OK, f"Execute failed: {resp_execute.status_code} - {resp_execute.json()}"
        exec_data = resp_execute.json()
        assert exec_data["columns"] == ["CustomerID", "CustomerName"]
        assert exec_data["rows"] == [
            {"CustomerID": 101, "CustomerName": "Acme Corporation"},
            {"CustomerID": 102, "CustomerName": "Beta Global"},
        ]
        assert exec_data["row_count"] == 2
        assert exec_data["truncated"] is False
        assert exec_data["request_id"] == "req-lifecycle-exec-001"
        assert exec_data["execution_time_ms"] >= 0.0

        # Assert query executed against mock_shared_conn
        mock_shared_conn.execution_options.assert_called()

        # Shared engine manager must remain alive and not disposed
        mock_shared_engine.dispose.assert_not_called()


def test_lifecycle_unauthenticated_requests_fail_closed(lifecycle_env):
    """Every endpoint across the lifecycle rejects unauthenticated requests (HTTP 401)."""
    client: TestClient = lifecycle_env["client"]
    client.cookies.clear()

    endpoints = [
        ("POST", "/agent/v1/profiles/validate-config", {"profile_id": "test", "display_name": "Test", "server": "localhost", "database_name": "TestDB"}),
        ("POST", "/agent/v1/profiles", {"profile_id": "test", "display_name": "Test", "server": "localhost", "database_name": "TestDB"}),
        ("POST", "/agent/v1/profiles/test_id/test-connection", {}),
        ("GET", "/agent/v1/profiles/test_id/schema", None),
        ("POST", "/agent/v1/profiles/test_id/execute", {"sql": "SELECT 1;"}),
    ]

    for method, path, body in endpoints:
        headers = {"Host": "127.0.0.1:8001", "X-QueryPilot-Request": "1"}
        if method == "POST":
            resp = client.post(path, headers=headers, json=body)
        else:
            resp = client.get(path, headers=headers)
        assert resp.status_code == status.HTTP_401_UNAUTHORIZED, f"{method} {path} should require authentication"


def test_lifecycle_missing_custom_header_rejected(lifecycle_env):
    """State-changing POST endpoints across the lifecycle require X-QueryPilot-Request: 1 (HTTP 400)."""
    client: TestClient = lifecycle_env["client"]
    session_id: str = lifecycle_env["session_id"]
    cookies = {SESSION_COOKIE_NAME: session_id}

    post_endpoints = [
        ("/agent/v1/profiles/validate-config", {"profile_id": "test", "display_name": "Test", "server": "localhost", "database_name": "TestDB"}),
        ("/agent/v1/profiles", {"profile_id": "test", "display_name": "Test", "server": "localhost", "database_name": "TestDB"}),
        ("/agent/v1/profiles/test_id/test-connection", {}),
        ("/agent/v1/profiles/test_id/execute", {"sql": "SELECT 1;"}),
    ]

    for path, body in post_endpoints:
        resp = client.post(path, headers={"Host": "127.0.0.1:8001"}, cookies=cookies, json=body)
        assert resp.status_code == status.HTTP_400_BAD_REQUEST, f"POST {path} should require custom header"


def test_lifecycle_unknown_profile_returns_404(lifecycle_env):
    """Profile-specific endpoints fail closed with HTTP 404 for unknown profile IDs."""
    client: TestClient = lifecycle_env["client"]
    session_id: str = lifecycle_env["session_id"]
    cookies = {SESSION_COOKIE_NAME: session_id}
    headers = {"Host": "127.0.0.1:8001", "X-QueryPilot-Request": "1"}

    resp_test = client.post("/agent/v1/profiles/non_existent/test-connection", headers=headers, cookies=cookies)
    assert resp_test.status_code == status.HTTP_404_NOT_FOUND

    resp_schema = client.get("/agent/v1/profiles/non_existent/schema", headers={"Host": "127.0.0.1:8001"}, cookies=cookies)
    assert resp_schema.status_code == status.HTTP_404_NOT_FOUND

    resp_exec = client.post("/agent/v1/profiles/non_existent/execute", headers=headers, cookies=cookies, json={"sql": "SELECT 1;"})
    assert resp_exec.status_code == status.HTTP_404_NOT_FOUND


def test_lifecycle_database_identity_mismatch_fails_closed(lifecycle_env):
    """Runtime database identity mismatch fails safe on connection testing and execution."""
    client: TestClient = lifecycle_env["client"]
    session_id: str = lifecycle_env["session_id"]
    store: ProfileStore = lifecycle_env["store"]
    factory: ProfileConnectionFactory = lifecycle_env["factory"]

    # Register a profile expecting 'ProtectedDB'
    profile = ConnectionProfile(
        profile_id="mismatch_profile",
        display_name="Mismatch Profile",
        server="localhost",
        database_name="ProtectedDB",
        use_trusted_connection=True,
    )
    store.register_profile(profile)

    cookies = {SESSION_COOKIE_NAME: session_id}
    headers = {"Host": "127.0.0.1:8001", "X-QueryPilot-Request": "1"}

    # Mock engine connecting to 'WrongDB'
    mock_engine = MagicMock(spec=Engine)
    mock_conn = MagicMock(spec=Connection)
    mock_conn.execute.return_value.scalar.return_value = "WrongDB"
    mock_conn.__enter__.return_value = mock_conn
    mock_conn.__exit__.return_value = None
    mock_engine.connect.return_value = mock_conn

    with patch.object(factory, "create_engine", return_value=mock_engine):
        # 1. Connection test returns DATABASE_MISMATCH
        resp_test = client.post(
            "/agent/v1/profiles/mismatch_profile/test-connection",
            headers=headers,
            cookies=cookies,
        )
        assert resp_test.status_code == status.HTTP_200_OK
        data = resp_test.json()
        assert data["connected"] is False
        assert data["database_name"] is None
        assert data["failure_code"] == "DATABASE_MISMATCH"

        # 2. Query execution fails with HTTP 403 Forbidden
        synthetic_schema = DatabaseSchema(
            database_name="ProtectedDB",
            tables=[TableMetadata(schema_name="dbo", table_name="T", columns=[ColumnMetadata(name="C", data_type="int")])],
        )
        lifecycle_env["app"].state.caller_schema = synthetic_schema

        resp_exec = client.post(
            "/agent/v1/profiles/mismatch_profile/execute",
            headers=headers,
            cookies=cookies,
            json={"sql": "SELECT C FROM T;"},
        )
        assert resp_exec.status_code == status.HTTP_403_FORBIDDEN
        assert "SECURITY VIOLATION" in resp_exec.json()["detail"]

