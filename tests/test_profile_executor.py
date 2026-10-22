"""Offline tests for ProfileExecutionBoundary (Task 1.34-B1-F28-A3).

Verifies the secure execution-boundary contract:
1. Trusted profile configuration & database identity derivation.
2. Rejection of client connection-target and security parameter overrides.
3. Database identity verification and fail-closed closure on mismatch.
4. Mandatory pre-execution SQL validation (blocking mutations, multi-statements, invalid syntax).
5. Schema and table allowlist enforcement.
6. Additive sensitive-column policy preserving canonical protections.
7. Enforcement of statement execution timeout at the connection/driver level (clamped <= 15s).
8. Enforcement of bounded row fetching (clamped <= 100 rows) with cursor truncation.
9. Sanitized error messages free of credentials, connection strings, or DBAPI tracebacks.
10. Deterministic cleanup of cursor and connection resources on success and failure paths.
"""

from typing import Any, Dict, List
from unittest.mock import MagicMock, call, patch

import pytest
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import OperationalError, ProgrammingError, SQLAlchemyError

from query_pilot.db.connection import (
    DatabaseConnectionError,
    DatabaseSecurityError,
)
from query_pilot.db.executor import (
    QueryResult,
    QueryTimeoutError,
    UnsafeQueryError,
)
from query_pilot.db.factory import (
    ProfileConnectionFactory,
    ProfileEngineConfiguration,
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
)
from query_pilot.db.profile_executor import (
    ProfileExecutionBoundary,
    ProfileExecutionError,
    ProfileQueryExecutor,
)


# ==============================================================================
# Deterministic Fake Fixtures (Pure Mock Data — Zero Real Credentials or Hostnames)
# ==============================================================================


@pytest.fixture
def fake_schema() -> DatabaseSchema:
    """Provide a deterministic fake DatabaseSchema for offline testing."""
    return DatabaseSchema(
        database_name="Analytics_DW_Prod",
        tables=[
            TableMetadata(
                table_name="Customers",
                schema_name="dbo",
                columns=[
                    ColumnMetadata(name="CustomerID", data_type="int", primary_key=True),
                    ColumnMetadata(name="CustomerName", data_type="varchar(100)"),
                    ColumnMetadata(name="Country", data_type="varchar(50)"),
                ],
            ),
            TableMetadata(
                table_name="Orders",
                schema_name="dbo",
                columns=[
                    ColumnMetadata(name="OrderID", data_type="int", primary_key=True),
                    ColumnMetadata(name="CustomerID", data_type="int"),
                    ColumnMetadata(name="OrderAmount", data_type="decimal(10,2)"),
                ],
            ),
            TableMetadata(
                table_name="Employees",
                schema_name="dbo",
                columns=[
                    ColumnMetadata(name="EmployeeID", data_type="int", primary_key=True),
                    ColumnMetadata(name="EmployeeName", data_type="varchar(100)"),
                    ColumnMetadata(name="Salary", data_type="decimal(10,2)"),
                    ColumnMetadata(name="SSN", data_type="varchar(11)"),
                    ColumnMetadata(name="PasswordHash", data_type="varchar(64)"),
                ],
            ),
        ],
        is_model_facing=True,
    )


@pytest.fixture
def fake_trusted_profile() -> ConnectionProfile:
    """Provide a valid fake profile with table allowlist, custom sensitive column, and strict limits."""
    return ConnectionProfile(
        profile_id="analytics_dw",
        display_name="Enterprise Analytics DW",
        server="corp-dw.internal,1433",
        database_name="Analytics_DW_Prod",
        use_trusted_connection=True,
        timeout_seconds=12,
        max_row_limit=80,
        allowed_tables=["Customers", "Orders"],
        sensitive_columns=["ExecutiveBonus"],
        schema_name="dbo",
    )


@pytest.fixture
def mock_db_connection() -> MagicMock:
    """Mock SQLAlchemy Connection with underlying DBAPI connection."""
    conn = MagicMock(spec=Connection)
    raw_dbapi = MagicMock()
    conn.connection.dbapi_connection = raw_dbapi
    # Default to expected DB name on SELECT DB_NAME()
    conn.execute.return_value.scalar.return_value = "Analytics_DW_Prod"
    return conn


@pytest.fixture
def mock_db_engine(mock_db_connection) -> MagicMock:
    """Mock SQLAlchemy Engine returning mock_db_connection."""
    engine = MagicMock(spec=Engine)
    engine.connect.return_value = mock_db_connection
    return engine


# ==============================================================================
# 1. Trusted Configuration and Identity Derivation
# ==============================================================================


def test_adapter_accepts_trusted_profile(fake_trusted_profile, fake_schema, mock_db_engine):
    """Execution boundary accepts a trusted ConnectionProfile and derives expected_db."""
    adapter = ProfileExecutionBoundary(
        config=fake_trusted_profile,
        schema=fake_schema,
        engine=mock_db_engine,
    )
    assert adapter.config.profile_id == "analytics_dw"
    assert adapter.expected_db == "Analytics_DW_Prod"
    assert adapter.effective_max_timeout == 12
    assert adapter.effective_max_rows == 80


def test_adapter_accepts_profile_engine_configuration(fake_trusted_profile, fake_schema, mock_db_engine):
    """Execution boundary accepts an immutable ProfileEngineConfiguration directly."""
    factory = ProfileConnectionFactory(store=ProfileStore(profiles={"analytics_dw": fake_trusted_profile}))
    cfg = factory.get_configuration("analytics_dw")

    adapter = ProfileExecutionBoundary(
        config=cfg,
        schema=fake_schema,
        engine=mock_db_engine,
    )
    assert adapter.config is cfg
    assert adapter.expected_db == "Analytics_DW_Prod"


def test_missing_or_invalid_config_fails_closed():
    """Initializing execution boundary with None or malformed config raises ProfileExecutionError."""
    with pytest.raises(ProfileExecutionError, match="Trusted profile configuration is required"):
        ProfileExecutionBoundary(config=None)  # type: ignore

    with pytest.raises(ProfileExecutionError, match="Expected ProfileEngineConfiguration or ConnectionProfile"):
        ProfileExecutionBoundary(config="not_a_config_object")  # type: ignore


def test_missing_schema_context_fails_closed(fake_trusted_profile, mock_db_engine):
    """Executing without schema context raises ProfileExecutionError and does not touch database."""
    adapter = ProfileExecutionBoundary(
        config=fake_trusted_profile,
        schema=None,
        engine=mock_db_engine,
    )
    with pytest.raises(ProfileExecutionError, match="Missing schema context"):
        adapter.execute("SELECT CustomerID FROM Customers;")

    mock_db_engine.connect.assert_not_called()


def test_schema_database_mismatch_fails_closed(fake_trusted_profile, fake_schema, mock_db_engine):
    """Schema context from a different database than profile expected_db fails closed."""
    from dataclasses import replace
    mismatched_schema = replace(fake_schema, database_name="Different_DB")
    adapter = ProfileExecutionBoundary(
        config=fake_trusted_profile,
        schema=mismatched_schema,
        engine=mock_db_engine,
    )
    with pytest.raises(ProfileExecutionError, match="does not match profile expected database"):
        adapter.execute("SELECT CustomerID FROM Customers;")

    mock_db_engine.connect.assert_not_called()


def test_client_payload_cannot_override_connection_targets(fake_trusted_profile, fake_schema, mock_db_engine):
    """Client request payloads containing connection target overrides are rejected immediately."""
    adapter = ProfileExecutionBoundary(
        config=fake_trusted_profile,
        schema=fake_schema,
        engine=mock_db_engine,
    )
    forbidden_payloads = [
        {"server": "10.0.0.1"},
        {"database": "evil_db"},
        {"expected_db": "evil_db"},
        {"user": "sa"},
        {"password": "password"},
        {"timeout": 999},
        {"max_rows": 5000},
    ]
    for payload in forbidden_payloads:
        with pytest.raises(ConnectionTargetOverrideError, match="SECURITY VIOLATION"):
            adapter.execute(
                "SELECT CustomerID FROM Customers;",
                client_request=payload,
            )

    # Database engine was never connected
    mock_db_engine.connect.assert_not_called()


def test_database_identity_mismatch_closes_connection_and_blocks_user_sql(
    fake_trusted_profile, fake_schema, mock_db_engine, mock_db_connection
):
    """When SELECT DB_NAME() yields an unauthorized database, connection is closed and user SQL is blocked."""
    # Mock SQL Server returning 'master' instead of 'Analytics_DW_Prod'
    mock_db_connection.execute.return_value.scalar.return_value = "master"

    adapter = ProfileExecutionBoundary(
        config=fake_trusted_profile,
        schema=fake_schema,
        engine=mock_db_engine,
    )

    with pytest.raises(DatabaseSecurityError, match="SECURITY VIOLATION: Connected to unauthorized database 'master'"):
        adapter.execute("SELECT CustomerID FROM Customers;")

    # SELECT DB_NAME() was executed
    mock_db_connection.execute.assert_called_once()
    assert "SELECT DB_NAME()" in str(mock_db_connection.execute.call_args[0][0])

    # Connection was strictly closed in finally block
    mock_db_connection.close.assert_called_once()

    # User SQL statement was never passed to execution
    execution_options_mock = mock_db_connection.execution_options
    execution_options_mock.assert_not_called()


# ==============================================================================
# 2. SQL AST Validation & Policy Enforcement
# ==============================================================================


@pytest.mark.parametrize(
    "mutating_sql",
    [
        "DELETE FROM Customers WHERE CustomerID = 1;",
        "INSERT INTO Customers (CustomerID, CustomerName) VALUES (1, 'Alice');",
        "UPDATE Customers SET CustomerName = 'Alice' WHERE CustomerID = 1;",
        "DROP TABLE Customers;",
        "ALTER TABLE Customers ADD Notes varchar(50);",
        "TRUNCATE TABLE Customers;",
        "EXEC sp_who2;",
    ],
)
def test_mutating_sql_rejected_before_database_connection(
    mutating_sql, fake_trusted_profile, fake_schema, mock_db_engine
):
    """Mutating or privileged SQL is blocked by AST validation before opening a database connection."""
    adapter = ProfileExecutionBoundary(
        config=fake_trusted_profile,
        schema=fake_schema,
        engine=mock_db_engine,
    )
    with pytest.raises(UnsafeQueryError):
        adapter.execute(mutating_sql)

    mock_db_engine.connect.assert_not_called()


def test_multi_statement_sql_rejected_before_database_connection(
    fake_trusted_profile, fake_schema, mock_db_engine
):
    """Multi-statement SQL queries are rejected before opening a database connection."""
    adapter = ProfileExecutionBoundary(
        config=fake_trusted_profile,
        schema=fake_schema,
        engine=mock_db_engine,
    )
    with pytest.raises(UnsafeQueryError, match="MULTI_STATEMENT"):
        adapter.execute("SELECT CustomerID FROM Customers; SELECT OrderID FROM Orders;")

    mock_db_engine.connect.assert_not_called()


def test_profile_table_allowlist_enforcement(fake_trusted_profile, fake_schema, mock_db_engine):
    """Tables outside the profile's allowed_tables tuple are rejected before execution."""
    # Profile allows: ['Customers', 'Orders']
    # Schema contains: ['Customers', 'Orders', 'Employees']
    adapter = ProfileExecutionBoundary(
        config=fake_trusted_profile,
        schema=fake_schema,
        engine=mock_db_engine,
    )

    # Querying permitted table succeeds validation
    # (will proceed to connection, mocked to succeed)
    mock_cursor = MagicMock()
    mock_cursor.keys.return_value = ["CustomerID"]
    mock_cursor.fetchmany.return_value = [(1,), (2,)]
    mock_conn = mock_db_engine.connect.return_value
    mock_conn.execution_options.return_value.execute.return_value = mock_cursor

    res = adapter.execute("SELECT CustomerID FROM Customers;")
    assert res.row_count == 2

    # Querying unlisted table 'Employees' is blocked
    with pytest.raises(UnsafeQueryError, match="Access to table 'Employees' is forbidden by profile policy"):
        adapter.execute("SELECT EmployeeID FROM Employees;")


def test_sensitive_columns_canonical_defaults_strictly_preserved(
    fake_trusted_profile, fake_schema, mock_db_engine
):
    """Canonical sensitive columns (PasswordHash, SSN) and profile-additive columns (ExecutiveBonus) are blocked."""
    adapter = ProfileExecutionBoundary(
        config=fake_trusted_profile,
        schema=fake_schema,
        engine=mock_db_engine,
    )

    # 1. Canonical sensitive columns blocked
    with pytest.raises(UnsafeQueryError, match="SENSITIVE_COLUMN"):
        adapter.execute("SELECT PasswordHash FROM Employees;")

    with pytest.raises(UnsafeQueryError, match="SENSITIVE_COLUMN"):
        adapter.execute("SELECT SSN FROM Employees;")

    # 2. Additive profile sensitive column blocked
    policy = adapter.get_sensitive_column_policy()
    assert policy.is_sensitive("ExecutiveBonus") is True
    assert policy.is_sensitive("passwordhash") is True
    assert policy.is_sensitive("ssn") is True


# ==============================================================================
# 3. Resource Limits & Bounded Execution
# ==============================================================================


def test_execution_timeout_applied_at_driver_and_statement_boundary(
    fake_trusted_profile, fake_schema, mock_db_engine, mock_db_connection
):
    """Execution timeout is set on raw DBAPI connection and passed to execution_options."""
    mock_cursor = MagicMock()
    mock_cursor.keys.return_value = ["CustomerID"]
    mock_cursor.fetchmany.return_value = [(101,)]
    mock_db_connection.execution_options.return_value.execute.return_value = mock_cursor

    adapter = ProfileExecutionBoundary(
        config=fake_trusted_profile,
        schema=fake_schema,
        engine=mock_db_engine,
    )

    res = adapter.execute("SELECT CustomerID FROM Customers;")
    assert res.row_count == 1

    # Effective profile timeout is 12s
    raw_dbapi = mock_db_connection.connection.dbapi_connection
    assert raw_dbapi.timeout == 12

    mock_db_connection.execution_options.assert_called_once_with(
        timeout=12,
        yield_per=80,
    )


def test_effective_timeout_cannot_exceed_global_15s_ceiling(fake_schema, mock_db_engine, mock_db_connection):
    """Even if profile or caller attempts >15s timeout, effective timeout is strictly clamped to 15s."""
    p_high = ConnectionProfile(
        profile_id="high_limit_dw",
        display_name="High Limit DW",
        server="corp-dw.internal",
        database_name="Analytics_DW_Prod",
        use_trusted_connection=True,
        timeout_seconds=15,  # Max allowed in profile model is 15
        max_row_limit=100,
    )
    mock_cursor = MagicMock()
    mock_cursor.keys.return_value = ["CustomerID"]
    mock_cursor.fetchmany.return_value = [(101,)]
    mock_db_connection.execution_options.return_value.execute.return_value = mock_cursor

    adapter = ProfileExecutionBoundary(
        config=p_high,
        schema=fake_schema,
        engine=mock_db_engine,
    )

    # Caller attempts to pass timeout_seconds=999
    adapter.execute("SELECT CustomerID FROM Customers;", timeout_seconds=999)

    raw_dbapi = mock_db_connection.connection.dbapi_connection
    assert raw_dbapi.timeout == 15
    mock_db_connection.execution_options.assert_called_with(
        timeout=15,
        yield_per=100,
    )


def test_profile_can_specify_stricter_limits(fake_schema, mock_db_engine, mock_db_connection):
    """A profile with stricter limits (e.g. 5s timeout, 20 rows) enforces those strict limits."""
    p_strict = ConnectionProfile(
        profile_id="strict_dw",
        display_name="Strict DW",
        server="corp-dw.internal",
        database_name="Analytics_DW_Prod",
        use_trusted_connection=True,
        timeout_seconds=5,
        max_row_limit=20,
    )
    mock_cursor = MagicMock()
    mock_cursor.keys.return_value = ["CustomerID"]
    mock_cursor.fetchmany.return_value = [(i,) for i in range(10)]
    mock_db_connection.execution_options.return_value.execute.return_value = mock_cursor

    adapter = ProfileExecutionBoundary(
        config=p_strict,
        schema=fake_schema,
        engine=mock_db_engine,
    )

    res = adapter.execute("SELECT CustomerID FROM Customers;")
    assert res.row_count == 10

    raw_dbapi = mock_db_connection.connection.dbapi_connection
    assert raw_dbapi.timeout == 5
    mock_db_connection.execution_options.assert_called_with(
        timeout=5,
        yield_per=20,
    )


def test_bounded_row_fetching_and_truncation_detection(
    fake_trusted_profile, fake_schema, mock_db_engine, mock_db_connection
):
    """Fetching fetches at most max_rows + 1, truncates to max_rows, and sets truncated=True."""
    # Profile max_row_limit = 80
    mock_cursor = MagicMock()
    mock_cursor.keys.return_value = ["CustomerID"]
    # Return 81 rows (1 extra to boundedly detect truncation)
    fake_81_rows = [(i,) for i in range(81)]
    mock_cursor.fetchmany.return_value = fake_81_rows
    mock_db_connection.execution_options.return_value.execute.return_value = mock_cursor

    adapter = ProfileExecutionBoundary(
        config=fake_trusted_profile,
        schema=fake_schema,
        engine=mock_db_engine,
    )

    res = adapter.execute("SELECT CustomerID FROM Customers;")
    # fetchmany must be called with 81 (80 + 1)
    mock_cursor.fetchmany.assert_called_once_with(81)

    assert res.truncated is True
    assert res.row_count == 80
    assert len(res.rows) == 80

    # Ensure cursor and connection closed
    mock_cursor.close.assert_called_once()
    mock_db_connection.close.assert_called_once()


def test_query_timeout_error_handling_and_resource_cleanup(
    fake_trusted_profile, fake_schema, mock_db_engine, mock_db_connection
):
    """Driver operational error with timeout message raises QueryTimeoutError and closes connection."""
    timeout_exc = OperationalError("SELECT 1", {}, Exception("HYT00: [Microsoft][ODBC] Query timeout expired"))
    mock_db_connection.execution_options.return_value.execute.side_effect = timeout_exc

    adapter = ProfileExecutionBoundary(
        config=fake_trusted_profile,
        schema=fake_schema,
        engine=mock_db_engine,
    )

    with pytest.raises(QueryTimeoutError, match="timed out after 12 seconds") as exc_info:
        adapter.execute("SELECT CustomerID FROM Customers;")

    assert exc_info.value.__cause__ is None
    assert exc_info.value.__suppress_context__ is True
    mock_db_connection.close.assert_called_once()


# ==============================================================================
# 4. Privacy, Credential Masking, and Error Sanitization
# ==============================================================================


def test_database_exceptions_are_sanitized(fake_trusted_profile, fake_schema, mock_db_engine, mock_db_connection):
    """Raw database exception strings with credentials or internal connection URLs are sanitized."""
    raw_error_msg = "Login failed for user 'sa'. Connection string: PWD=SecretPassword123;UID=admin;"
    mock_db_connection.execution_options.return_value.execute.side_effect = OperationalError(
        "SELECT 1", {}, Exception(raw_error_msg)
    )

    adapter = ProfileExecutionBoundary(
        config=fake_trusted_profile,
        schema=fake_schema,
        engine=mock_db_engine,
    )

    with pytest.raises(ProfileExecutionError) as exc_info:
        adapter.execute("SELECT CustomerID FROM Customers;")

    err_str = str(exc_info.value)
    # Must not contain plaintext password
    assert "SecretPassword123" not in err_str
    # Must be sanitized
    assert "Database operational error:" in err_str
    # Must not expose raw driver exception through __cause__ (GAP-02 hardening)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__suppress_context__ is True


def test_exceptions_suppress_raw_cause_chains(
    fake_trusted_profile, fake_schema, mock_db_engine, mock_db_connection
):
    """Verify all database and driver exceptions suppress raw causes across all failure modes."""
    adapter = ProfileExecutionBoundary(
        config=fake_trusted_profile,
        schema=fake_schema,
        engine=mock_db_engine,
    )

    # 1. OperationalError during execution suppresses __cause__
    mock_db_connection.execution_options.return_value.execute.side_effect = OperationalError(
        "SELECT 1", {}, Exception("Database driver crashed: server=corp-dw.internal")
    )
    with pytest.raises(ProfileExecutionError) as exc_op:
        adapter.execute("SELECT CustomerID FROM Customers;")
    assert exc_op.value.__cause__ is None
    assert exc_op.value.__suppress_context__ is True
    assert "corp-dw.internal" not in str(exc_op.value)

    # 2. General SQLAlchemyError during execution suppresses __cause__
    mock_db_connection.execution_options.return_value.execute.side_effect = SQLAlchemyError(
        "Internal engine failure: data source=prod-sql;pwd=secret"
    )
    with pytest.raises(ProfileExecutionError) as exc_sa:
        adapter.execute("SELECT CustomerID FROM Customers;")
    assert exc_sa.value.__cause__ is None
    assert exc_sa.value.__suppress_context__ is True
    assert "secret" not in str(exc_sa.value)

    # 3. Connection acquisition failure suppresses __cause__
    mock_db_engine.connect.side_effect = SQLAlchemyError("Cannot connect to server=corp-dw.internal:1433;pwd=secret")
    with pytest.raises(DatabaseConnectionError) as exc_conn:
        adapter.execute("SELECT CustomerID FROM Customers;")
    assert exc_conn.value.__cause__ is None
    assert exc_conn.value.__suppress_context__ is True
    assert "secret" not in str(exc_conn.value)


def test_validation_failure_does_not_leak_partial_data(fake_trusted_profile, fake_schema, mock_db_engine):
    """If validation fails, no connection is opened and no data is returned or leaked."""
    adapter = ProfileExecutionBoundary(
        config=fake_trusted_profile,
        schema=fake_schema,
        engine=mock_db_engine,
    )

    with pytest.raises(UnsafeQueryError):
        adapter.execute("SELECT NonExistentColumn FROM Customers;")

    mock_db_engine.connect.assert_not_called()

