"""Opt-in SQL Server Integration Test Harness (Task 1.34-B1-F28-A9-B9-B-Implementation).

Provides a guarded, opt-in test harness for validating QueryPilot against an
explicitly authorized Microsoft SQL Server test environment.

Safety Boundaries:
1. Marked with `@pytest.mark.integration` and excluded from default pytest runs
   via `addopts = "-m 'not integration'"` in pyproject.toml.
2. Requires explicit environment configuration:
   - `QUERYPILOT_TEST_SQLSERVER_ENABLED=1`
   - `QUERYPILOT_TEST_SQLSERVER_DB=<disposable_test_db>`
   Missing configuration results in a clean skip; NEVER defaults to GradeSense_Local
   or any system database.
3. Target database identity verification:
   Every connection, query, and fixture validates `SELECT DB_NAME() == expected_db`.
4. Fixture lifecycle and table name safety:
   - All fixture-created tables MUST start with the prefix `qp_test_`.
   - DDL mutations are gated behind `QUERYPILOT_TEST_SQLSERVER_ALLOW_MUTATION=1`.
   - Teardown only drops tracked `qp_test_*` tables created by that fixture.
   - Never drops databases, schemas, or untracked objects.
5. Resource bounds and semantics:
   - Row ceiling is application-level bounded fetching (fetchmany(effective_rows + 1)).
   - Statement timeout is measured via monotonic clock (time.monotonic()).
   - Errors map strictly to confirmed HTTP status codes:
     * UnsafeQueryError -> 400 Bad Request
     * DatabaseSecurityError -> 403 Forbidden
     * QueryTimeoutError -> 504 Gateway Timeout
     * DatabaseConnectionError / ProfileExecutionError -> 500 Internal Server Error
     * UnknownProfileError -> 404 Not Found
   - Canonical session cookie is `qpt_session`.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import time
from typing import Any, Dict, List, Optional, Set
from unittest.mock import MagicMock, patch

from fastapi import status
from fastapi.testclient import TestClient
from pydantic import SecretStr
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from query_pilot.agent.app import create_agent_app
from query_pilot.agent.auth import (
    SESSION_COOKIE_NAME,
    BootstrapTokenManager,
    SessionManager,
)
from query_pilot.agent.config import AgentConfig
from query_pilot.db.connection import (
    ALLOWED_TARGET_DATABASE,
    DatabaseConnectionError,
    DatabaseSecurityError,
    create_profile_db_engine,
    verify_database_context,
)
from query_pilot.db.executor import (
    QueryResult,
    QueryTimeoutError,
    UnsafeQueryError,
)
from query_pilot.db.models import (
    ColumnMetadata,
    DatabaseSchema,
    TableMetadata,
)
from query_pilot.db.profile import (
    GLOBAL_MAX_ROW_LIMIT,
    GLOBAL_MAX_TIMEOUT_SECONDS,
    ConnectionProfile,
    ConnectionTargetOverrideError,
    ProfileStore,
    UnknownProfileError,
)
from query_pilot.db.profile_executor import (
    ProfileExecutionBoundary,
    ProfileExecutionError,
)
from query_pilot.db.profile_introspector import (
    ProfileDatabaseIntrospector,
    ProfileIntrospectionError,
)
from query_pilot.sql.validator import validate_sql

# Apply integration marker to all tests in this module.
# Excluded by default in pyproject.toml via addopts = "-m 'not integration'".
pytestmark = pytest.mark.integration

# Protected databases that must NEVER be used as the integration test target
PROTECTED_DATABASES: Set[str] = {
    "master",
    "msdb",
    "tempdb",
    "model",
    "gradesense",
    "gradesense_local",
    "gradesensecodefirst",
}

# Mandatory prefix for any fixture-created table
FIXTURE_TABLE_PREFIX = "qp_test_"

# Bounded read-only SELECT workload designed to test driver statement timeouts.
# Generates 1,000 numbers on the fly via a 3-way CTE Cartesian product and evaluates
# chained HASHBYTES across a 1,000,000-combination Cartesian join in a streaming aggregate.
# NOTE: Does not claim guaranteed execution duration, zero tempdb usage, bounded memory consumption,
# or guaranteed immediate cancellation. Runtime behavior depends on CPU clock rate, execution plan
# selection, and engine scheduler yield points.
TIMEOUT_WORKLOAD_SQL = (
    "WITH Digits AS ("
    " SELECT 0 AS d UNION ALL SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3 UNION ALL SELECT 4"
    " UNION ALL SELECT 5 UNION ALL SELECT 6 UNION ALL SELECT 7 UNION ALL SELECT 8 UNION ALL SELECT 9"
    "), "
    "Numbers AS ("
    " SELECT d1.d * 100 + d2.d * 10 + d3.d AS n"
    " FROM Digits d1"
    " CROSS JOIN Digits d2"
    " CROSS JOIN Digits d3"
    ") "
    "SELECT COUNT(HASHBYTES('SHA2_256', HASHBYTES('SHA2_256', CAST(a.n * 1000 + b.n AS VARCHAR(30))))) AS hash_count "
    "FROM Numbers a "
    "CROSS JOIN Numbers b;"
)


# ==============================================================================
# Configuration Loader & Safety Validators
# ==============================================================================


@dataclass(frozen=True)
class SQLServerIntegrationConfig:
    """Explicit configuration for live SQL Server integration testing."""

    server: str
    database_name: str
    driver: str = "ODBC Driver 17 for SQL Server"
    use_trusted_connection: bool = True
    user: Optional[str] = None
    password: Optional[SecretStr] = None
    allow_mutation: bool = False
    timeout_seconds: int = 15
    max_row_limit: int = 100
    schema_name: str = "dbo"
    sensitive_columns: Optional[Tuple[str, ...]] = None

    def to_profile(
        self,
        profile_id: str = "integration_test_profile",
        schema_name: Optional[str] = None,
        sensitive_columns: Optional[Tuple[str, ...]] = None,
    ) -> ConnectionProfile:
        """Derive a validated, immutable ConnectionProfile."""
        target_schema = schema_name or self.schema_name
        cols = sensitive_columns if sensitive_columns is not None else self.sensitive_columns
        return ConnectionProfile(
            profile_id=profile_id,
            display_name=f"Integration Test ({self.database_name})",
            server=self.server,
            database_name=self.database_name,
            driver=self.driver,
            use_trusted_connection=self.use_trusted_connection,
            user=self.user,
            password=self.password,
            timeout_seconds=min(self.timeout_seconds, GLOBAL_MAX_TIMEOUT_SECONDS),
            max_row_limit=min(self.max_row_limit, GLOBAL_MAX_ROW_LIMIT),
            schema_name=target_schema,
            sensitive_columns=cols,
        )


def load_integration_config(
    environ: Optional[Dict[str, str]] = None,
) -> Optional[SQLServerIntegrationConfig]:
    """Load integration configuration from environment with strict validation.

    Returns:
        SQLServerIntegrationConfig if explicitly enabled and configured.
        None if integration testing is disabled or unconfigured.

    Raises:
        ValueError: If target database is a protected database or legacy default.
    """
    env = environ if environ is not None else os.environ

    enabled = env.get("QUERYPILOT_TEST_SQLSERVER_ENABLED", "").strip().lower()
    if enabled not in ("1", "true", "yes"):
        return None

    db_name = env.get("QUERYPILOT_TEST_SQLSERVER_DB", "").strip()
    if not db_name:
        return None

    # Strict check: NEVER default to or allow GradeSense_Local or system databases
    if db_name.lower() in PROTECTED_DATABASES:
        raise ValueError(
            f"SECURITY VIOLATION: Database '{db_name}' is a protected/production/legacy database. "
            f"Live integration tests require an explicitly authorized disposable test database."
        )

    server = env.get("QUERYPILOT_TEST_SQLSERVER_SERVER", r"localhost\SQLEXPRESS").strip()
    driver = env.get("QUERYPILOT_TEST_SQLSERVER_DRIVER", "ODBC Driver 17 for SQL Server").strip()
    use_trusted = env.get("QUERYPILOT_TEST_SQLSERVER_USE_TRUSTED", "true").strip().lower() in ("1", "true", "yes")

    user = env.get("QUERYPILOT_TEST_SQLSERVER_USER")
    pwd_raw = env.get("QUERYPILOT_TEST_SQLSERVER_PASSWORD")
    password = SecretStr(pwd_raw) if pwd_raw else None

    allow_mutation = env.get("QUERYPILOT_TEST_SQLSERVER_ALLOW_MUTATION", "").strip().lower() in ("1", "true", "yes")
    schema_name = env.get("QUERYPILOT_TEST_SQLSERVER_SCHEMA", "dbo").strip() or "dbo"

    raw_sensitive = env.get("QUERYPILOT_TEST_SQLSERVER_SENSITIVE_COLUMNS")
    if raw_sensitive:
        sensitive_cols: Optional[Tuple[str, ...]] = tuple(c.strip() for c in raw_sensitive.split(",") if c.strip())
    else:
        sensitive_cols = None

    return SQLServerIntegrationConfig(
        server=server,
        database_name=db_name,
        driver=driver,
        use_trusted_connection=use_trusted,
        user=user,
        password=password,
        allow_mutation=allow_mutation,
        schema_name=schema_name,
        sensitive_columns=sensitive_cols,
    )


def require_live_sqlserver() -> SQLServerIntegrationConfig:
    """Fixture helper that skips cleanly if SQL Server integration is unconfigured."""
    config = load_integration_config()
    if config is None:
        pytest.skip(
            "SQL Server integration tests are opt-in. Set QUERYPILOT_TEST_SQLSERVER_ENABLED=1 "
            "and QUERYPILOT_TEST_SQLSERVER_DB=<disposable_db> to execute against an authorized database."
        )
    return config


# ==============================================================================
# Fixture Safety Guards
# ==============================================================================


class GuardedFixtureTableManager:
    """Manages fixture-created tables with strict identity and prefix enforcement.

    Guarantees:
    - Tables must have the prefix `qp_test_`.
    - DDL requires `allow_mutation=True`.
    - Every DDL operation verifies `SELECT DB_NAME() == expected_db`.
    - Teardown only drops tables explicitly recorded by this instance.
    """

    def __init__(self, config: SQLServerIntegrationConfig):
        self._config = config
        self._created_tables: List[str] = []

    @property
    def created_tables(self) -> List[str]:
        return list(self._created_tables)

    def verify_database_safety(self, conn: Connection) -> str:
        """Verify the active connection is strictly on the expected disposable database."""
        actual_db = verify_database_context(conn, expected_db=self._config.database_name)
        if actual_db.lower() in PROTECTED_DATABASES:
            raise DatabaseSecurityError(
                f"Refusing to execute DDL against protected database '{actual_db}'."
            )
        return actual_db

    def create_table(self, conn: Connection, table_name: str, create_sql: str) -> None:
        """Create a guarded table strictly verifying name prefix, mutation flag, and DB context."""
        if not self._config.allow_mutation:
            raise RuntimeError(
                f"Fixture mutation blocked: QUERYPILOT_TEST_SQLSERVER_ALLOW_MUTATION is not enabled. "
                f"Cannot create table '{table_name}'."
            )

        clean_name = table_name.strip()
        if not clean_name.lower().startswith(FIXTURE_TABLE_PREFIX):
            raise ValueError(
                f"Fixture security violation: Table name '{clean_name}' does not start with "
                f"mandatory prefix '{FIXTURE_TABLE_PREFIX}'."
            )

        # Verify target database identity before DDL execution
        self.verify_database_safety(conn)

        # Execute table creation
        conn.execute(text(create_sql))
        self._created_tables.append(clean_name)

    def teardown(self, conn: Connection) -> None:
        """Clean up only tables recorded as created by this fixture instance."""
        if not self._created_tables:
            return

        # Verify target database identity before DDL teardown
        self.verify_database_safety(conn)

        for tbl in reversed(self._created_tables):
            if not tbl.lower().startswith(FIXTURE_TABLE_PREFIX):
                continue
            try:
                conn.execute(text(f"DROP TABLE IF EXISTS dbo.[{tbl}];"))
            except SQLAlchemyError:
                pass
        self._created_tables.clear()


# ==============================================================================
# 1. Configuration & Safety Guard Verification (Offline-Safe / Mockable)
# ==============================================================================


class TestSQLServerHarnessSafetyGuards:
    """Verifies that the integration test harness enforces all safety boundaries."""

    def test_config_loader_returns_none_when_disabled(self):
        """When QUERYPILOT_TEST_SQLSERVER_ENABLED is not set, loader returns None."""
        config = load_integration_config(environ={})
        assert config is None

    def test_config_loader_returns_none_when_db_missing(self):
        """When enabled=1 but target DB is omitted, loader returns None (does not default)."""
        config = load_integration_config(environ={"QUERYPILOT_TEST_SQLSERVER_ENABLED": "1"})
        assert config is None

    def test_config_loader_rejects_gradesense_local(self):
        """Explicitly specifying GradeSense_Local raises ValueError and fails closed."""
        with pytest.raises(ValueError) as exc_info:
            load_integration_config(
                environ={
                    "QUERYPILOT_TEST_SQLSERVER_ENABLED": "1",
                    "QUERYPILOT_TEST_SQLSERVER_DB": "GradeSense_Local",
                }
            )
        assert "protected/production/legacy database" in str(exc_info.value)

    @pytest.mark.parametrize("system_db", ["master", "msdb", "tempdb", "model"])
    def test_config_loader_rejects_system_databases(self, system_db: str):
        """Specifying any system database raises ValueError and fails closed."""
        with pytest.raises(ValueError) as exc_info:
            load_integration_config(
                environ={
                    "QUERYPILOT_TEST_SQLSERVER_ENABLED": "1",
                    "QUERYPILOT_TEST_SQLSERVER_DB": system_db,
                }
            )
        assert "protected/production/legacy database" in str(exc_info.value)

    def test_config_loader_valid_custom_disposable_db(self):
        """Valid disposable database configuration produces a typed SQLServerIntegrationConfig."""
        config = load_integration_config(
            environ={
                "QUERYPILOT_TEST_SQLSERVER_ENABLED": "1",
                "QUERYPILOT_TEST_SQLSERVER_DB": "QueryPilot_Disposable_TestDB",
                "QUERYPILOT_TEST_SQLSERVER_SERVER": r"localhost\SQLEXPRESS",
                "QUERYPILOT_TEST_SQLSERVER_ALLOW_MUTATION": "1",
            }
        )
        assert config is not None
        assert config.database_name == "QueryPilot_Disposable_TestDB"
        assert config.allow_mutation is True
        profile = config.to_profile()
        assert profile.database_name == "QueryPilot_Disposable_TestDB"
        assert profile.expected_db == "QueryPilot_Disposable_TestDB"

    def test_require_live_sqlserver_skips_when_unconfigured(self):
        """require_live_sqlserver raises pytest.skip when unconfigured."""
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(pytest.skip.Exception) as exc_info:
                require_live_sqlserver()
            assert "opt-in" in str(exc_info.value)

    def test_fixture_guard_rejects_unprefixed_table_name(self):
        """Creating a table without 'qp_test_' prefix raises ValueError."""
        config = SQLServerIntegrationConfig(
            server=r"localhost\SQLEXPRESS",
            database_name="QueryPilot_TestDB",
            allow_mutation=True,
        )
        manager = GuardedFixtureTableManager(config)
        mock_conn = MagicMock(spec=Connection)

        with pytest.raises(ValueError) as exc_info:
            manager.create_table(mock_conn, "unprefixed_table", "CREATE TABLE unprefixed_table (id INT);")

        assert "mandatory prefix 'qp_test_'" in str(exc_info.value)
        assert mock_conn.execute.call_count == 0

    def test_fixture_guard_rejects_mutation_when_disallowed(self):
        """Creating a table when allow_mutation=False raises RuntimeError."""
        config = SQLServerIntegrationConfig(
            server=r"localhost\SQLEXPRESS",
            database_name="QueryPilot_TestDB",
            allow_mutation=False,
        )
        manager = GuardedFixtureTableManager(config)
        mock_conn = MagicMock(spec=Connection)

        with pytest.raises(RuntimeError) as exc_info:
            manager.create_table(mock_conn, "qp_test_sample", "CREATE TABLE qp_test_sample (id INT);")

        assert "QUERYPILOT_TEST_SQLSERVER_ALLOW_MUTATION is not enabled" in str(exc_info.value)
        assert mock_conn.execute.call_count == 0

    def test_fixture_guard_verifies_target_database_before_ddl(self):
        """If connected database does not match expected_db, DDL is aborted with DatabaseSecurityError."""
        config = SQLServerIntegrationConfig(
            server=r"localhost\SQLEXPRESS",
            database_name="QueryPilot_TestDB",
            allow_mutation=True,
        )
        manager = GuardedFixtureTableManager(config)
        mock_conn = MagicMock(spec=Connection)
        mock_conn.execute.return_value.scalar.return_value = "Wrong_Database"

        with pytest.raises(DatabaseSecurityError):
            manager.create_table(mock_conn, "qp_test_sample", "CREATE TABLE qp_test_sample (id INT);")

    def test_fixture_guard_teardown_verifies_target_database_before_drop(self):
        """Teardown refuses to execute DROP statements if DB_NAME() does not match expected_db."""
        config = SQLServerIntegrationConfig(
            server=r"localhost\SQLEXPRESS",
            database_name="QueryPilot_TestDB",
            allow_mutation=True,
        )
        manager = GuardedFixtureTableManager(config)
        manager._created_tables.append("qp_test_sample")

        mock_conn = MagicMock(spec=Connection)
        mock_conn.execute.return_value.scalar.return_value = "Wrong_Database"

        with pytest.raises(DatabaseSecurityError) as exc_info:
            manager.teardown(mock_conn)

        assert "SECURITY VIOLATION" in str(exc_info.value) or "unauthorized database" in str(exc_info.value)
        # Verify no DROP statement was executed
        for call_args in mock_conn.execute.call_args_list:
            sql_text = str(call_args[0][0])
            assert "DROP" not in sql_text.upper()
        # Ensure the tracked table list was preserved and not cleared
        assert "qp_test_sample" in manager.created_tables


# ==============================================================================
# 2. HTTP Status Code Mapping Contracts (Offline-Safe)
# ==============================================================================


class TestHTTPStatusCodeMappingContracts:
    """Verifies that all query and database exceptions map strictly to documented HTTP statuses."""

    def test_canonical_session_cookie_name(self):
        """Canonical session cookie is qpt_session, resolving the cookie-name discrepancy."""
        assert SESSION_COOKIE_NAME == "qpt_session"

    def test_unsafe_query_error_maps_to_http_400(self, tmp_path):
        """UnsafeQueryError (AST rejection) maps to HTTP 400 Bad Request."""
        profile = ConnectionProfile(
            profile_id="test_p",
            display_name="Test",
            server="localhost",
            database_name="TestDB",
        )
        t1 = TableMetadata(
            schema_name="dbo",
            table_name="t1",
            columns=[
                ColumnMetadata(name="id", data_type="int", primary_key=True),
                ColumnMetadata(name="val", data_type="nvarchar"),
            ],
        )
        schema = DatabaseSchema(
            database_name="TestDB",
            tables=[t1],
        )
        boundary = ProfileExecutionBoundary(config=profile, schema=schema)

        # Direct execution check: AST rejection raises UnsafeQueryError
        with pytest.raises(UnsafeQueryError):
            boundary.execute("DROP TABLE t1;")

        # Agent app route verification
        token_dir = tmp_path / "tokens_unsafe"
        token_dir.mkdir(parents=True, exist_ok=True)
        config = AgentConfig(bind_host="127.0.0.1", bind_port=8001, token_dir=token_dir)
        store = ProfileStore({"test_p": profile}, default_profile_id="test_p")
        token_mgr = BootstrapTokenManager(token_dir=token_dir)
        session_mgr = SessionManager()
        session = session_mgr.create_session()

        app = create_agent_app(
            config=config,
            store=store,
            token_manager=token_mgr,
            session_manager=session_mgr,
            caller_schema=schema,
        )
        client = TestClient(app, base_url="http://127.0.0.1:8001")
        client.cookies.set(SESSION_COOKIE_NAME, session.session_id)

        response = client.post(
            "/agent/v1/profiles/test_p/execute",
            headers={"X-QueryPilot-Request": "1"},
            json={"sql": "DROP TABLE t1;"},
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_database_security_error_maps_to_http_403(self, tmp_path):
        """DatabaseSecurityError (database identity mismatch) maps to HTTP 403 Forbidden."""
        profile = ConnectionProfile(
            profile_id="test_p",
            display_name="Test",
            server="localhost",
            database_name="ExpectedDB",
        )
        t1 = TableMetadata(
            schema_name="dbo",
            table_name="t1",
            columns=[ColumnMetadata(name="id", data_type="int")],
        )
        schema = DatabaseSchema(database_name="ExpectedDB", tables=[t1])

        # Mock engine returning wrong DB_NAME
        mock_conn = MagicMock(spec=Connection)
        mock_conn.execute.return_value.scalar.return_value = "DifferentDB"
        mock_engine = MagicMock(spec=Engine)
        mock_engine.connect.return_value = mock_conn

        token_dir = tmp_path / "tokens_sec"
        token_dir.mkdir(parents=True, exist_ok=True)
        config = AgentConfig(bind_host="127.0.0.1", bind_port=8001, token_dir=token_dir)
        store = ProfileStore({"test_p": profile}, default_profile_id="test_p")
        token_mgr = BootstrapTokenManager(token_dir=token_dir)
        session_mgr = SessionManager()
        session = session_mgr.create_session()

        app = create_agent_app(
            config=config,
            store=store,
            token_manager=token_mgr,
            session_manager=session_mgr,
            caller_engine=mock_engine,
            caller_schema=schema,
        )
        client = TestClient(app, base_url="http://127.0.0.1:8001")
        client.cookies.set(SESSION_COOKIE_NAME, session.session_id)

        response = client.post(
            "/agent/v1/profiles/test_p/execute",
            headers={"X-QueryPilot-Request": "1"},
            json={"sql": "SELECT 1;"},
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_query_timeout_error_maps_to_http_504(self, tmp_path):
        """QueryTimeoutError maps to HTTP 504 Gateway Timeout."""
        profile = ConnectionProfile(
            profile_id="test_p",
            display_name="Test",
            server="localhost",
            database_name="ExpectedDB",
        )
        t1 = TableMetadata(
            schema_name="dbo",
            table_name="t1",
            columns=[ColumnMetadata(name="id", data_type="int")],
        )
        schema = DatabaseSchema(database_name="ExpectedDB", tables=[t1])

        mock_conn = MagicMock(spec=Connection)
        mock_conn.execute.return_value.scalar.return_value = "ExpectedDB"
        # Simulate timeout on cursor execution
        mock_exec_options = MagicMock()
        mock_exec_options.execute.side_effect = OperationalError("SELECT ...", {}, Exception("HYT00: Query timeout"))
        mock_conn.execution_options.return_value = mock_exec_options

        # Provide dbapi_connection with timeout attribute
        raw_dbapi = MagicMock()
        raw_dbapi.timeout = 15
        mock_conn.connection.dbapi_connection = raw_dbapi

        mock_engine = MagicMock(spec=Engine)
        mock_engine.connect.return_value = mock_conn

        token_dir = tmp_path / "tokens_timeout"
        token_dir.mkdir(parents=True, exist_ok=True)
        config = AgentConfig(bind_host="127.0.0.1", bind_port=8001, token_dir=token_dir)
        store = ProfileStore({"test_p": profile}, default_profile_id="test_p")
        token_mgr = BootstrapTokenManager(token_dir=token_dir)
        session_mgr = SessionManager()
        session = session_mgr.create_session()

        app = create_agent_app(
            config=config,
            store=store,
            token_manager=token_mgr,
            session_manager=session_mgr,
            caller_engine=mock_engine,
            caller_schema=schema,
        )
        client = TestClient(app, base_url="http://127.0.0.1:8001")
        client.cookies.set(SESSION_COOKIE_NAME, session.session_id)

        response = client.post(
            "/agent/v1/profiles/test_p/execute",
            headers={"X-QueryPilot-Request": "1"},
            json={"sql": "SELECT 1;"},
        )
        assert response.status_code == status.HTTP_504_GATEWAY_TIMEOUT


# ==============================================================================
# 3. Monotonic Timeout & Bounded Fetching Measurement Helpers (Offline-Safe)
# ==============================================================================


def _run_post_timeout_recovery_protocol(
    boundary: ProfileExecutionBoundary,
    workload_sql: str = TIMEOUT_WORKLOAD_SQL,
    timeout_seconds: int = 1,
    recovery_sql: str = "SELECT 1 AS recovery_check;",
) -> QueryResult:
    """Execute the post-timeout recovery protocol against a ProfileExecutionBoundary.

    Requires the initial workload to trigger a statement timeout (QueryTimeoutError).
    If the workload finishes normally without timing out (e.g. on fast hardware),
    fails immediately via pytest.fail without executing the recovery query.

    Returns:
        QueryResult from the subsequent recovery query if timeout occurred and
        connection pool recovered successfully.

    Raises:
        pytest.fail.Exception: If the initial workload completes without timing out.
        QueryExecutionError: If the subsequent recovery query fails.
    """
    t0 = time.monotonic()
    try:
        result = boundary.execute(workload_sql, timeout_seconds=timeout_seconds)
    except QueryTimeoutError:
        # Expected statement timeout occurred; proceed to recovery query
        pass
    else:
        elapsed = time.monotonic() - t0
        pytest.fail(
            f"Workload completed normally in {elapsed:.2f}s ({result.row_count} rows) "
            f"without exercising timeout cancellation. Post-timeout recovery cannot be tested."
        )

    # Execute recovery query only after QueryTimeoutError was verified
    return boundary.execute(recovery_sql)


class TestMeasurementAndCeilingContracts:
    """Verifies timing measurement and ceiling properties."""

    def test_monotonic_clock_duration_calculation(self):
        """Monotonic clock time.monotonic() calculates elapsed duration accurately."""
        t0 = time.monotonic()
        time.sleep(0.01)  # 10ms
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        assert elapsed_ms >= 5.0
        assert elapsed_ms <= 500.0

    def test_bounded_fetching_semantics_contract(self):
        """Row limits operate at application cursor level (fetchmany(limit + 1)), not TOP rewrite."""
        profile = ConnectionProfile(
            profile_id="test_p",
            display_name="Test",
            server="localhost",
            database_name="TestDB",
            max_row_limit=5,
        )
        t1 = TableMetadata(
            schema_name="dbo",
            table_name="t1",
            columns=[
                ColumnMetadata(name="id", data_type="int", primary_key=True),
                ColumnMetadata(name="val", data_type="nvarchar"),
            ],
        )
        schema = DatabaseSchema(database_name="TestDB", tables=[t1])

        # Mock connection returning 6 rows when max_rows is 5
        mock_conn = MagicMock(spec=Connection)
        mock_conn.execute.return_value.scalar.return_value = "TestDB"

        mock_cursor = MagicMock()
        mock_cursor.keys.return_value = ["id", "val"]
        mock_cursor.fetchmany.return_value = [
            (1, "a"), (2, "b"), (3, "c"), (4, "d"), (5, "e"), (6, "f")
        ]

        mock_exec_options = MagicMock()
        mock_exec_options.execute.return_value = mock_cursor
        mock_conn.execution_options.return_value = mock_exec_options

        raw_dbapi = MagicMock()
        raw_dbapi.timeout = 15
        mock_conn.connection.dbapi_connection = raw_dbapi

        boundary = ProfileExecutionBoundary(config=profile, schema=schema, engine=MagicMock())
        boundary._acquire_connection = MagicMock(return_value=mock_conn)

        result = boundary.execute("SELECT id, val FROM t1;", max_rows=5)

        # Application-level bounded fetching truncates to 5 rows and sets truncated=True
        assert result.row_count == 5
        assert len(result.rows) == 5
        assert result.truncated is True
        # Verify fetchmany was called with effective_rows + 1 (5 + 1 = 6)
        mock_cursor.fetchmany.assert_called_once_with(6)

    def test_waitfor_delay_rejected_by_ast_validator(self):
        """WAITFOR DELAY is rejected by AST validation as unsafe/unsupported and cannot be used as timeout workload."""
        schema = DatabaseSchema(database_name="TestDB", tables=[])
        result = validate_sql("WAITFOR DELAY '00:00:05';", schema=schema)
        assert result.allowed is False
        assert any(r.code.value in ("PARSE_ERROR", "NOT_READ_ONLY") for r in result.reasons)

    def test_timeout_workload_accepted_by_ast_validator(self):
        """TIMEOUT_WORKLOAD_SQL passes AST validation as a valid read-only SELECT query."""
        schema = DatabaseSchema(database_name="TestDB", tables=[])
        result = validate_sql(TIMEOUT_WORKLOAD_SQL, schema=schema)
        assert result.allowed is True
        assert len(result.reasons) == 0

    def test_timeout_workload_operational_error_classification(self):
        """Simulated operational timeout on TIMEOUT_WORKLOAD_SQL raises QueryTimeoutError."""
        profile = ConnectionProfile(
            profile_id="test_p",
            display_name="Test",
            server="localhost",
            database_name="TestDB",
        )
        schema = DatabaseSchema(database_name="TestDB", tables=[])

        mock_conn = MagicMock(spec=Connection)
        mock_conn.execute.return_value.scalar.return_value = "TestDB"

        mock_exec_options = MagicMock()
        mock_exec_options.execute.side_effect = OperationalError(
            TIMEOUT_WORKLOAD_SQL, {}, Exception("HYT00: [Microsoft][ODBC Driver 17]Query timeout expired")
        )
        mock_conn.execution_options.return_value = mock_exec_options

        raw_dbapi = MagicMock()
        raw_dbapi.timeout = 1
        mock_conn.connection.dbapi_connection = raw_dbapi

        boundary = ProfileExecutionBoundary(config=profile, schema=schema, engine=MagicMock())
        boundary._acquire_connection = MagicMock(return_value=mock_conn)

        with pytest.raises(QueryTimeoutError) as exc_info:
            boundary.execute(TIMEOUT_WORKLOAD_SQL, timeout_seconds=1)

        assert "timed out after 1 seconds" in str(exc_info.value)

    def test_post_timeout_recovery_success_when_timeout_occurs(self):
        """When initial query times out, recovery query is executed and succeeds."""
        profile = ConnectionProfile(
            profile_id="test_p",
            display_name="Test",
            server="localhost",
            database_name="TestDB",
        )
        schema = DatabaseSchema(database_name="TestDB", tables=[])

        mock_conn = MagicMock(spec=Connection)
        mock_conn.execute.return_value.scalar.return_value = "TestDB"

        raw_dbapi = MagicMock()
        raw_dbapi.timeout = 1
        mock_conn.connection.dbapi_connection = raw_dbapi

        # First call (timeout workload) raises OperationalError(HYT00) -> QueryTimeoutError
        # Second call (recovery query) returns 1 row with recovery_check = 1
        mock_cursor_recovery = MagicMock()
        mock_cursor_recovery.keys.return_value = ["recovery_check"]
        mock_cursor_recovery.fetchmany.return_value = [(1,)]

        mock_exec_options = MagicMock()
        mock_exec_options.execute.side_effect = [
            OperationalError(
                TIMEOUT_WORKLOAD_SQL, {}, Exception("HYT00: [Microsoft][ODBC Driver 17]Query timeout expired")
            ),
            mock_cursor_recovery,
        ]
        mock_conn.execution_options.return_value = mock_exec_options

        boundary = ProfileExecutionBoundary(config=profile, schema=schema, engine=MagicMock())
        boundary._acquire_connection = MagicMock(return_value=mock_conn)

        # Execute recovery workflow via shared protocol helper
        recovery_result = _run_post_timeout_recovery_protocol(
            boundary=boundary,
            workload_sql=TIMEOUT_WORKLOAD_SQL,
            timeout_seconds=1,
            recovery_sql="SELECT 1 AS recovery_check;",
        )

        assert recovery_result.row_count == 1
        assert recovery_result.rows[0].get("recovery_check") == 1
        assert mock_exec_options.execute.call_count == 2

    def test_post_timeout_recovery_fails_and_aborts_if_workload_completes_without_timeout(self):
        """When initial query finishes normally without timeout, helper fails and aborts before running recovery."""
        profile = ConnectionProfile(
            profile_id="test_p",
            display_name="Test",
            server="localhost",
            database_name="TestDB",
        )
        schema = DatabaseSchema(database_name="TestDB", tables=[])

        mock_conn = MagicMock(spec=Connection)
        mock_conn.execute.return_value.scalar.return_value = "TestDB"

        raw_dbapi = MagicMock()
        raw_dbapi.timeout = 1
        mock_conn.connection.dbapi_connection = raw_dbapi

        # Initial query completes normally (returns 1 row with count)
        mock_cursor_normal = MagicMock()
        mock_cursor_normal.keys.return_value = ["hash_count"]
        mock_cursor_normal.fetchmany.return_value = [(1000000,)]

        mock_exec_options = MagicMock()
        mock_exec_options.execute.return_value = mock_cursor_normal
        mock_conn.execution_options.return_value = mock_exec_options

        boundary = ProfileExecutionBoundary(config=profile, schema=schema, engine=MagicMock())
        boundary._acquire_connection = MagicMock(return_value=mock_conn)

        # Calling shared helper must raise pytest.fail.Exception and never execute recovery
        with pytest.raises(pytest.fail.Exception) as exc_info:
            _run_post_timeout_recovery_protocol(
                boundary=boundary,
                workload_sql=TIMEOUT_WORKLOAD_SQL,
                timeout_seconds=1,
                recovery_sql="SELECT 1 AS recovery_check;",
            )

        assert "Workload completed normally" in str(exc_info.value)
        assert "without exercising timeout cancellation" in str(exc_info.value)
        assert mock_exec_options.execute.call_count == 1, (
            "Only the initial query should have been executed; recovery query was erroneously dispatched."
        )


# ==============================================================================
# 4. Schema Introspection & Multi-Schema Contract Tests (Offline-Safe)
# ==============================================================================


class TestSchemaIntrospectionContracts:
    """Verifies schema selection, multi-schema routing, and override security offline.

    NOTE: These unit tests verify QueryPilot's internal routing contract and security
    boundary using mock objects. They do NOT prove actual SQL Server multi-schema
    catalog behavior, which requires a live database connection with multi-schema tables.
    """

    def test_introspector_routes_non_default_schema_to_inspector(self):
        """Profile with custom schema_name correctly routes that schema to the inspector."""
        profile = ConnectionProfile(
            profile_id="custom_schema_profile",
            display_name="Custom Schema Test",
            server="localhost",
            database_name="TestDB",
            schema_name="sales",
        )

        mock_conn = MagicMock(spec=Connection)
        mock_conn.execute.return_value.scalar.return_value = "TestDB"

        mock_inspector = MagicMock()
        mock_inspector.get_table_names.return_value = ["orders"]
        mock_inspector.get_columns.return_value = [
            {"name": "order_id", "type": MagicMock(__str__=lambda s: "INTEGER"), "nullable": False, "default": None}
        ]
        mock_inspector.get_pk_constraint.return_value = {"constrained_columns": ["order_id"]}
        mock_inspector.get_foreign_keys.return_value = []
        mock_inspector.get_indexes.return_value = []

        with patch("query_pilot.db.profile_introspector.inspect", return_value=mock_inspector):
            introspector = ProfileDatabaseIntrospector(profile)
            introspector._acquire_connection = MagicMock(return_value=mock_conn)

            schema = introspector.introspect_raw()

            # Verify the non-default schema was passed to get_table_names and get_columns
            mock_inspector.get_table_names.assert_called_once_with(schema="sales")
            mock_inspector.get_columns.assert_called_once_with("orders", schema="sales")
            assert schema.database_name == "TestDB"
            assert "orders" in schema.table_names
            assert schema.get_table("orders").schema_name == "sales"

    def test_introspector_blocks_unauthorized_schema_override(self):
        """Caller cannot override a profile's configured schema at runtime."""
        profile = ConnectionProfile(
            profile_id="default_schema_profile",
            display_name="Default Schema Test",
            server="localhost",
            database_name="TestDB",
            schema_name="dbo",
        )
        introspector = ProfileDatabaseIntrospector(profile)
        with pytest.raises(ConnectionTargetOverrideError) as exc_info:
            introspector.introspect_raw(schema_name="unauthorized_schema")
        assert "SECURITY VIOLATION" in str(exc_info.value)
        assert "Caller is not permitted to override the profile's configured schema" in str(exc_info.value)

    def test_sqlserver_integration_config_supports_custom_schema(self):
        """SQLServerIntegrationConfig accurately forwards custom schema_name to profile."""
        config = SQLServerIntegrationConfig(
            server="localhost",
            database_name="TestDB",
            schema_name="analytics",
        )
        profile = config.to_profile()
        assert profile.schema_name == "analytics"

        profile_override = config.to_profile(schema_name="reporting")
        assert profile_override.schema_name == "reporting"


# ==============================================================================
# 5. Opt-in Live SQL Server Integration Tests (Require Explicit Authorization)
# ==============================================================================


class TestLiveSQLServerIntegration:
    """Guarded live integration tests executed exclusively against an authorized disposable DB."""

    def test_live_connection_identity_and_least_privilege(self):
        """Verify DB_NAME() matches authorized disposable DB and assess least privilege.

        NOTE ON WINDOWS INTEGRATED AUTHENTICATION:
        When QUERYPILOT_TEST_SQLSERVER_USE_TRUSTED=true (the default), the connection
        inherits the Windows privileges of the current process. On developer workstations,
        the local Windows user is often a SQL Server sysadmin. To prove true least privilege,
        configure a dedicated SQL Server login (e.g., qp_test_user) with USE_TRUSTED=0
        and db_datareader role membership.
        """
        config = require_live_sqlserver()
        profile = config.to_profile()
        engine = create_profile_db_engine(profile)

        with engine.connect() as conn:
            # 1. Connected database identity must match authorized disposable database
            actual_db = verify_database_context(conn, expected_db=config.database_name)
            assert actual_db.lower() == config.database_name.lower()
            assert actual_db.lower() not in PROTECTED_DATABASES

            # 2. Least privilege assessment
            is_sysadmin = conn.execute(text("SELECT IS_SRVROLEMEMBER('sysadmin');")).scalar()
            is_dbowner = conn.execute(text("SELECT IS_MEMBER('db_owner');")).scalar()
            has_select = conn.execute(text("SELECT HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'SELECT');")).scalar()

            # Handle NULL or indeterminate role-membership results explicitly
            assert is_sysadmin is not None, (
                "IS_SRVROLEMEMBER('sysadmin') returned NULL (indeterminate). "
                "Unable to evaluate server role membership. Fail closed to prevent accidental sysadmin execution."
            )

            # Strict least-privilege enforcement: fail if connected as sysadmin
            assert is_sysadmin == 0, (
                "LEAST PRIVILEGE VIOLATION: Connected session possesses the 'sysadmin' fixed server role. "
                "Live tests must be executed with a dedicated least-privilege identity (e.g. db_datareader). "
                "If using Windows Integrated Auth (USE_TRUSTED=true), the local Windows account is a sysadmin; "
                "switch to a dedicated SQL login via QUERYPILOT_TEST_SQLSERVER_USER / PASSWORD with USE_TRUSTED=0."
            )

            # Assert active SELECT permission on the test database
            assert has_select is not None and has_select == 1, (
                "Connected user must have active SELECT permission on the test database (e.g., via db_datareader)."
            )

    def test_live_schema_introspection_multischema_and_types(self):
        """Introspect tables in authorized disposable DB, verifying data types and redaction.

        Requires pre-seeded fixture tables (e.g. dbo.qp_test_sample) with representative types
        and at least one canonical sensitive column (e.g. password_hash, ssn, api_key).
        Fails non-vacuously if fixtures are missing from the test database.

        NOTE ON MULTI-SCHEMA ARCHITECTURE:
        Each QueryPilot ConnectionProfile binds to a single configured schema (profile.schema_name).
        Multi-schema discovery across distinct schemas requires configuring distinct profiles or
        extending the application profile schema model.
        """
        config = require_live_sqlserver()
        profile = config.to_profile()
        introspector = ProfileDatabaseIntrospector(profile)

        # 1. Raw Introspection: Discover schema metadata in configured schema
        schema = introspector.introspect_raw()
        assert schema.database_name.lower() == config.database_name.lower()

        # Non-vacuous fixture presence checks
        assert len(schema.tables) >= 1, (
            f"FIXTURE MISSING: Database '{config.database_name}' contains 0 tables in schema '{profile.schema_name}'. "
            f"Introspection test requires pre-seeded fixture tables (e.g., 'qp_test_sample'). "
            f"Cannot evaluate schema introspection against an empty database."
        )
        total_columns = sum(len(tbl.columns) for tbl in schema.tables)
        assert total_columns >= 1, (
            f"FIXTURE INCOMPLETE: Discovered {len(schema.tables)} table(s) in '{config.database_name}', "
            f"but total column count is 0. Fixture tables must contain column definitions."
        )

        # Verify that the expected sensitive-column fixture is actually present in raw schema
        raw_sensitive_columns = [
            f"{tbl.table_name}.{col.name}"
            for tbl in schema.tables
            for col in tbl.columns
            if col.name.lower() in ("password_hash", "ssn", "api_key")
        ]
        assert len(raw_sensitive_columns) >= 1, (
            f"FIXTURE MISSING SENSITIVE COLUMN: Discovered tables {schema.table_names}, but none contain "
            f"a canonical sensitive column ('password_hash', 'ssn', 'api_key'). "
            f"Provision at least one sensitive column fixture to verify model-facing redaction."
        )

        # 2. Model-facing schema must redact canonical sensitive columns
        model_facing = introspector.introspect_model_facing()
        assert model_facing.is_model_facing is True, "model_facing schema must have is_model_facing=True"
        assert len(model_facing.tables) >= 1, (
            f"Model-facing schema has 0 tables after filtering profile '{profile.profile_id}'."
        )

        model_facing_column_names = {
            f"{tbl.table_name}.{col.name}".lower()
            for tbl in model_facing.tables
            for col in tbl.columns
        }
        for sensitive_col in raw_sensitive_columns:
            assert sensitive_col.lower() not in model_facing_column_names, (
                f"Sensitive column '{sensitive_col}' from raw schema was not redacted in model-facing schema."
            )

        for tbl in model_facing.tables:
            for col in tbl.columns:
                assert col.name.lower() not in ("password_hash", "ssn", "api_key"), (
                    f"Sensitive column '{tbl.table_name}.{col.name}' leaked into model-facing schema."
                )

    def test_live_ast_rejection_prior_to_db_execution(self):
        """AST validation rejects mutating or dangerous statements before touching the database."""
        config = require_live_sqlserver()
        profile = config.to_profile()
        introspector = ProfileDatabaseIntrospector(profile)
        schema = introspector.introspect_model_facing()
        boundary = ProfileExecutionBoundary(config=profile, schema=schema)

        # Mutating or non-SELECT queries must be rejected by validator before DB execution
        dangerous_queries = [
            "DROP TABLE qp_test_sample;",
            "INSERT INTO qp_test_sample (id) VALUES (1);",
            "UPDATE qp_test_sample SET id = 2;",
            "EXEC sp_executesql N'SELECT 1';",
            "WAITFOR DELAY '00:00:05';",
            "SELECT 1; DROP TABLE qp_test_sample;",
        ]
        for query in dangerous_queries:
            with pytest.raises(UnsafeQueryError):
                boundary.execute(query)

    def test_live_statement_timeout_and_classification(self):
        """Verify hard driver-level statement timeout cancels execution and raises QueryTimeoutError."""
        config = require_live_sqlserver()
        profile = config.to_profile()
        introspector = ProfileDatabaseIntrospector(profile)
        schema = introspector.introspect_model_facing()
        boundary = ProfileExecutionBoundary(config=profile, schema=schema)

        # Execute bounded compute-intensive SELECT with a 1-second timeout
        timeout_sec = 1
        t0 = time.monotonic()
        try:
            result = boundary.execute(
                TIMEOUT_WORKLOAD_SQL,
                timeout_seconds=timeout_sec,
            )
            pytest.fail(
                f"Workload completed normally in {time.monotonic() - t0:.2f}s ({result.row_count} rows) "
                f"without exercising timeout cancellation. Timeout behavior was not tested."
            )
        except QueryTimeoutError:
            pass

        elapsed = time.monotonic() - t0

        # Monotonic verification: elapsed time should reflect timeout expiration (>= 0.9s)
        # with reasonable driver attention-packet round-trip tolerance (<= timeout + 4.0s).
        # Does not assume instantaneous cancellation or fixed duration.
        assert elapsed >= timeout_sec * 0.9, (
            f"Timeout cancellation reported too quickly ({elapsed:.2f}s < {timeout_sec * 0.9:.2f}s)."
        )
        assert elapsed <= timeout_sec + 4.0, (
            f"Driver timeout cancellation exceeded expected latency window ({elapsed:.2f}s > {timeout_sec + 4.0:.2f}s)."
        )

    def test_live_post_timeout_session_recovery(self):
        """Subsequent queries on the pool succeed cleanly after a statement timeout.

        Requires the initial workload to trigger a hard statement timeout (QueryTimeoutError).
        If the query completes normally (e.g., due to high-performance hardware finishing
        within 1 second), fails explicitly without executing the recovery check.
        """
        config = require_live_sqlserver()
        profile = config.to_profile()
        introspector = ProfileDatabaseIntrospector(profile)
        schema = introspector.introspect_model_facing()
        boundary = ProfileExecutionBoundary(config=profile, schema=schema)

        # Execute shared recovery protocol helper
        recovery_result = _run_post_timeout_recovery_protocol(
            boundary=boundary,
            workload_sql=TIMEOUT_WORKLOAD_SQL,
            timeout_seconds=1,
            recovery_sql="SELECT 1 AS recovery_check;",
        )
        assert recovery_result.row_count == 1
        assert recovery_result.rows[0].get("recovery_check") == 1
