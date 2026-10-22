"""Offline tests for ProfileDatabaseIntrospector (Task 1.34-B1-F28-A4).

Verifies profile-aware dynamic database schema introspection:
1. Derivation of expected database identity strictly from trusted profile configuration.
2. Rejection of client connection-target and security parameter overrides.
3. Database identity verification and fail-closed closure on mismatch.
4. Correct discovery of tables, columns, primary keys, foreign keys, and indexes.
5. Strict enforcement of profile table allowlists (allowed_tables); unlisted tables omitted.
6. Safe foreign-key filtering to prevent references to excluded tables.
7. Model-facing redaction preserving canonical defaults and profile-additive sensitive columns.
8. Complete profile isolation (no cross-tenant schema leakage or shared cache).
9. Connection and resource cleanup on success and failure paths.
10. End-to-end compatibility with SQLValidator and ProfileExecutionBoundary.
"""

from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from query_pilot.db.connection import (
    DatabaseConnectionError,
    DatabaseSecurityError,
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
    ConnectionProfile,
    ConnectionTargetOverrideError,
    ProfileStore,
)
from query_pilot.db.profile_executor import ProfileExecutionBoundary
from query_pilot.db.profile_introspector import (
    ProfileDatabaseIntrospector,
    ProfileIntrospectionError,
    ProfileSchemaIntrospector,
)
from query_pilot.sql.models import ValidationReasonCode
from query_pilot.sql.validator import validate_sql


# ==============================================================================
# Pure Mock Fixtures (Fake Data Only — Zero Real Hostnames or Credentials)
# ==============================================================================


@pytest.fixture
def fake_corp_profile() -> ConnectionProfile:
    """Fake profile for Corporate Analytics with table allowlist and additive sensitive column."""
    return ConnectionProfile(
        profile_id="corp_analytics",
        display_name="Corporate Analytics DW",
        server="corp-dw.internal:1433",
        database_name="CorpAnalyticsDB",
        use_trusted_connection=True,
        timeout_seconds=12,
        max_row_limit=80,
        allowed_tables=["Customers", "Orders"],
        sensitive_columns=["CommissionRate"],
        schema_name="analytics",
    )


@pytest.fixture
def fake_finance_profile() -> ConnectionProfile:
    """Fake profile for Finance DB with no table allowlist."""
    return ConnectionProfile(
        profile_id="finance_local",
        display_name="Finance Department Local",
        server="localhost\\sqlexpress",
        database_name="Finance_Local",
        use_trusted_connection=True,
        timeout_seconds=5,
        max_row_limit=25,
        allowed_tables=None,
        sensitive_columns=["ExecutiveBonus"],
        schema_name="dbo",
    )


@pytest.fixture
def mock_db_connection() -> MagicMock:
    """Mock SQLAlchemy Connection with default SELECT DB_NAME() returning CorpAnalyticsDB."""
    conn = MagicMock(spec=Connection)
    raw_dbapi = MagicMock()
    conn.connection.dbapi_connection = raw_dbapi
    conn.execute.return_value.scalar.return_value = "CorpAnalyticsDB"
    return conn


@pytest.fixture
def mock_db_engine(mock_db_connection) -> MagicMock:
    """Mock SQLAlchemy Engine returning mock_db_connection."""
    engine = MagicMock(spec=Engine)
    engine.connect.return_value = mock_db_connection
    return engine


@pytest.fixture
def mock_inspector() -> MagicMock:
    """Mock SQLAlchemy Inspector providing multi-table schema metadata."""
    insp = MagicMock()

    # Tables in 'analytics' schema
    insp.get_table_names.return_value = ["AuditLogs", "Customers", "Orders", "Users"]

    def get_pk(tbl, schema="dbo"):
        if tbl == "Customers":
            return {"constrained_columns": ["CustomerID"]}
        if tbl == "Orders":
            return {"constrained_columns": ["OrderID"]}
        if tbl == "Users":
            return {"constrained_columns": ["UserID"]}
        return {}

    def get_cols(tbl, schema="dbo"):
        if tbl == "Customers":
            return [
                {"name": "CustomerID", "type": "INTEGER", "nullable": False, "primary_key": True, "default": None, "identity": True, "comment": None},
                {"name": "CustomerName", "type": "VARCHAR(100)", "nullable": False, "primary_key": False, "default": None, "identity": False, "comment": None},
                {"name": "Country", "type": "VARCHAR(50)", "nullable": True, "primary_key": False, "default": "'USA'", "identity": False, "comment": None},
            ]
        if tbl == "Orders":
            return [
                {"name": "OrderID", "type": "INTEGER", "nullable": False, "primary_key": True, "default": None, "identity": True, "comment": None},
                {"name": "CustomerID", "type": "INTEGER", "nullable": False, "primary_key": False, "default": None, "identity": False, "comment": None},
                {"name": "OrderAmount", "type": "DECIMAL(10,2)", "nullable": False, "primary_key": False, "default": None, "identity": False, "comment": None},
                {"name": "CommissionRate", "type": "DECIMAL(5,2)", "nullable": True, "primary_key": False, "default": None, "identity": False, "comment": None},
            ]
        if tbl == "Users":
            return [
                {"name": "UserID", "type": "INTEGER", "nullable": False, "primary_key": True, "default": None, "identity": True, "comment": None},
                {"name": "Username", "type": "VARCHAR(50)", "nullable": False, "primary_key": False, "default": None, "identity": False, "comment": None},
                {"name": "PasswordHash", "type": "VARCHAR(64)", "nullable": False, "primary_key": False, "default": None, "identity": False, "comment": None},
            ]
        if tbl == "AuditLogs":
            return [
                {"name": "LogID", "type": "INTEGER", "nullable": False, "primary_key": True, "default": None, "identity": True, "comment": None},
                {"name": "Message", "type": "VARCHAR(255)", "nullable": True, "primary_key": False, "default": None, "identity": False, "comment": None},
            ]
        return []

    def get_fks(tbl, schema="dbo"):
        if tbl == "Orders":
            return [
                {
                    "name": "FK_Orders_Customers",
                    "referred_table": "Customers",
                    "constrained_columns": ["CustomerID"],
                    "referred_columns": ["CustomerID"],
                },
                # Foreign key pointing to an unallowed table 'Users'
                {
                    "name": "FK_Orders_Users",
                    "referred_table": "Users",
                    "constrained_columns": ["CustomerID"],
                    "referred_columns": ["UserID"],
                },
            ]
        return []

    def get_idxs(tbl, schema="dbo"):
        if tbl == "Customers":
            return [
                {"name": "idx_cust_name", "column_names": ["CustomerName"], "unique": False, "dialect_options": {}},
            ]
        return []

    def get_uniques(tbl, schema="dbo"):
        return []

    insp.get_pk_constraint.side_effect = get_pk
    insp.get_columns.side_effect = get_cols
    insp.get_foreign_keys.side_effect = get_fks
    insp.get_indexes.side_effect = get_idxs
    insp.get_unique_constraints.side_effect = get_uniques

    return insp


# ==============================================================================
# 1. Trusted Configuration and Database Identity Boundaries
# ==============================================================================


def test_introspector_initialization_with_profile(fake_corp_profile, mock_db_engine):
    """ProfileDatabaseIntrospector accepts a trusted ConnectionProfile and derives expected_db."""
    introspector = ProfileDatabaseIntrospector(
        config=fake_corp_profile,
        engine=mock_db_engine,
    )
    assert introspector.config.profile_id == "corp_analytics"
    assert introspector.expected_db == "CorpAnalyticsDB"
    assert introspector.default_schema_name == "analytics"


def test_introspector_initialization_with_profile_engine_config(fake_corp_profile, mock_db_engine):
    """ProfileDatabaseIntrospector accepts an immutable ProfileEngineConfiguration directly."""
    factory = ProfileConnectionFactory(store=ProfileStore(profiles={"corp_analytics": fake_corp_profile}))
    cfg = factory.get_configuration("corp_analytics")

    introspector = ProfileDatabaseIntrospector(
        config=cfg,
        engine=mock_db_engine,
    )
    assert introspector.config is cfg
    assert introspector.expected_db == "CorpAnalyticsDB"
    assert introspector.default_schema_name == "analytics"


def test_introspector_missing_or_invalid_config_fails_closed():
    """Initializing introspector with None or invalid config raises ProfileIntrospectionError."""
    with pytest.raises(ProfileIntrospectionError, match="Trusted profile configuration is required"):
        ProfileDatabaseIntrospector(config=None)  # type: ignore

    with pytest.raises(ProfileIntrospectionError, match="Expected ProfileEngineConfiguration or ConnectionProfile"):
        ProfileDatabaseIntrospector(config="invalid_type")  # type: ignore


def test_introspector_rejects_client_overrides(fake_corp_profile, mock_db_engine):
    """Untrusted client request payloads attempting connection target overrides are rejected."""
    introspector = ProfileDatabaseIntrospector(
        config=fake_corp_profile,
        engine=mock_db_engine,
    )
    forbidden_payloads = [
        {"database": "evil_db"},
        {"server": "10.0.0.1"},
        {"expected_db": "evil_db"},
        {"schema": "malicious_schema"},
        {"user": "sa"},
        {"password": "secret"},
    ]
    for payload in forbidden_payloads:
        with pytest.raises(ConnectionTargetOverrideError, match="SECURITY VIOLATION"):
            introspector.introspect_raw(client_request=payload)

        with pytest.raises(ConnectionTargetOverrideError, match="SECURITY VIOLATION"):
            introspector.introspect_model_facing(client_request=payload)

    # Database engine was never contacted
    mock_db_engine.connect.assert_not_called()


def test_database_identity_mismatch_aborts_introspection_and_closes_connection(
    fake_corp_profile, mock_db_engine, mock_db_connection
):
    """When live DB_NAME() does not match profile expected_db, DatabaseSecurityError is raised and connection closed."""
    # Target expects CorpAnalyticsDB, but SQL Server reports 'master'
    mock_db_connection.execute.return_value.scalar.return_value = "master"

    introspector = ProfileDatabaseIntrospector(
        config=fake_corp_profile,
        engine=mock_db_engine,
    )

    with pytest.raises(DatabaseSecurityError, match="SECURITY VIOLATION: Connected to unauthorized database 'master'"):
        introspector.introspect_raw()

    # Connection was strictly closed in finally block
    mock_db_connection.close.assert_called_once()


# ==============================================================================
# 2. Schema Discovery Correctness & Table Allowlist Enforcement
# ==============================================================================


@patch("query_pilot.db.profile_introspector.inspect")
def test_table_allowlist_filters_discovered_tables(
    mock_inspect_fn, fake_corp_profile, mock_db_engine, mock_db_connection, mock_inspector
):
    """Only tables in profile.allowed_tables are discovered; unlisted tables are strictly omitted."""
    mock_inspect_fn.return_value = mock_inspector

    introspector = ProfileDatabaseIntrospector(
        config=fake_corp_profile,
        engine=mock_db_engine,
    )

    schema = introspector.introspect_raw()

    # Target schema checked
    mock_inspector.get_table_names.assert_called_once_with(schema="analytics")

    # Inspector reported: ["AuditLogs", "Customers", "Orders", "Users"]
    # Profile allowlist: ["Customers", "Orders"]
    assert schema.database_name == "CorpAnalyticsDB"
    assert schema.table_names == ["Customers", "Orders"]
    assert "Users" not in schema.table_names
    assert "AuditLogs" not in schema.table_names

    # Connection closed cleanly
    mock_db_connection.close.assert_called_once()


@patch("query_pilot.db.profile_introspector.inspect")
def test_foreign_keys_to_unallowed_tables_are_filtered(
    mock_inspect_fn, fake_corp_profile, mock_db_engine, mock_db_connection, mock_inspector
):
    """Foreign keys referencing tables outside the allowlist are omitted to prevent invalid references."""
    mock_inspect_fn.return_value = mock_inspector

    introspector = ProfileDatabaseIntrospector(
        config=fake_corp_profile,
        engine=mock_db_engine,
    )

    schema = introspector.introspect_raw()
    orders_table = schema.get_table("Orders")
    assert orders_table is not None

    # In mock_inspector, Orders had FK_Orders_Customers (allowed) and FK_Orders_Users (unallowed)
    fk_names = [fk.name for fk in orders_table.foreign_keys]
    assert "FK_Orders_Customers" in fk_names
    assert "FK_Orders_Users" not in fk_names


@patch("query_pilot.db.profile_introspector.inspect")
def test_columns_types_nullability_and_primary_keys_preserved(
    mock_inspect_fn, fake_corp_profile, mock_db_engine, mock_db_connection, mock_inspector
):
    """Column metadata including data types, nullability, defaults, and primary key status are preserved."""
    mock_inspect_fn.return_value = mock_inspector

    introspector = ProfileDatabaseIntrospector(
        config=fake_corp_profile,
        engine=mock_db_engine,
    )

    schema = introspector.introspect_raw()
    customers_table = schema.get_table("Customers")
    assert customers_table is not None

    cust_id = customers_table.get_column("CustomerID")
    assert cust_id is not None
    assert cust_id.primary_key is True
    assert cust_id.nullable is False
    assert cust_id.is_identity is True
    assert "INTEGER" in cust_id.data_type

    country = customers_table.get_column("Country")
    assert country is not None
    assert country.primary_key is False
    assert country.nullable is True
    assert country.default == "'USA'"


@patch("query_pilot.db.profile_introspector.inspect")
def test_sensitive_columns_model_facing_redaction(
    mock_inspect_fn, fake_corp_profile, mock_db_engine, mock_db_connection, mock_inspector
):
    """introspect_model_facing redacts canonical sensitive columns and profile additive sensitive columns."""
    mock_inspect_fn.return_value = mock_inspector

    introspector = ProfileDatabaseIntrospector(
        config=fake_corp_profile,
        engine=mock_db_engine,
    )

    model_schema = introspector.introspect_model_facing()
    assert model_schema.is_model_facing is True

    orders_table = model_schema.get_table("Orders")
    assert orders_table is not None

    # CommissionRate was marked sensitive in fake_corp_profile
    assert "CommissionRate" not in orders_table.column_names
    assert orders_table.get_column("CommissionRate") is None

    # Non-sensitive columns are preserved
    assert "OrderID" in orders_table.column_names
    assert "CustomerID" in orders_table.column_names
    assert "OrderAmount" in orders_table.column_names


# ==============================================================================
# 3. Profile Isolation & Zero Shared Cache Leakage
# ==============================================================================


@patch("query_pilot.db.profile_introspector.inspect")
def test_profile_isolation_across_distinct_profiles(
    mock_inspect_fn,
    fake_corp_profile,
    fake_finance_profile,
    mock_db_engine,
    mock_db_connection,
    mock_inspector,
):
    """Distinct profiles return isolated schemas without state leakage or cache pollution."""
    mock_inspect_fn.return_value = mock_inspector

    # 1. Introspect Corp Analytics Profile
    mock_db_connection.execute.return_value.scalar.return_value = "CorpAnalyticsDB"
    introspector_corp = ProfileDatabaseIntrospector(
        config=fake_corp_profile,
        engine=mock_db_engine,
    )
    schema_corp = introspector_corp.introspect_raw()

    # 2. Introspect Finance Profile
    mock_db_connection.execute.return_value.scalar.return_value = "Finance_Local"
    introspector_fin = ProfileDatabaseIntrospector(
        config=fake_finance_profile,
        engine=mock_db_engine,
    )
    schema_fin = introspector_fin.introspect_raw()

    # Verify complete isolation
    assert schema_corp.database_name == "CorpAnalyticsDB"
    assert schema_fin.database_name == "Finance_Local"

    # Corp had allowlist ["Customers", "Orders"]
    assert schema_corp.table_names == ["Customers", "Orders"]
    # Finance had allowed_tables=None, discovers all tables
    assert schema_fin.table_names == ["AuditLogs", "Customers", "Orders", "Users"]


@patch("query_pilot.db.profile_introspector.inspect")
def test_failed_introspection_does_not_pollute_state(
    mock_inspect_fn, fake_corp_profile, mock_db_engine, mock_db_connection, mock_inspector
):
    """A failed introspection cleans up resources and raises ProfileIntrospectionError without stale state."""
    mock_inspect_fn.return_value = mock_inspector
    mock_inspector.get_table_names.side_effect = OperationalError("statement", {}, Exception("Network error"))

    introspector = ProfileDatabaseIntrospector(
        config=fake_corp_profile,
        engine=mock_db_engine,
    )

    with pytest.raises(ProfileIntrospectionError, match="Failed to list tables in schema 'analytics'"):
        introspector.introspect_raw()

    mock_db_connection.close.assert_called_once()


# ==============================================================================
# 4. End-to-End Interoperability with Validator & ProfileExecutionBoundary
# ==============================================================================


@patch("query_pilot.db.profile_introspector.inspect")
def test_introspected_schema_integrates_cleanly_with_sql_validator(
    mock_inspect_fn, fake_corp_profile, mock_db_engine, mock_db_connection, mock_inspector
):
    """Model-facing schema from introspector seamlessly validates compliant SQL and rejects unallowed tables/columns."""
    mock_inspect_fn.return_value = mock_inspector

    introspector = ProfileDatabaseIntrospector(
        config=fake_corp_profile,
        engine=mock_db_engine,
    )

    model_schema = introspector.introspect_model_facing()
    policy = introspector.get_sensitive_column_policy()

    # 1. Permitted query against discovered allowed table succeeds
    val_ok = validate_sql(
        sql="SELECT CustomerID, CustomerName FROM Customers;",
        schema=model_schema,
        policy=policy,
    )
    assert val_ok.allowed is True

    # 2. Query against table omitted by allowlist ('Users') fails validation with UNKNOWN_TABLE
    val_unallowed_table = validate_sql(
        sql="SELECT UserID FROM Users;",
        schema=model_schema,
        policy=policy,
    )
    assert val_unallowed_table.allowed is False
    codes = [r.code for r in val_unallowed_table.reasons]
    assert ValidationReasonCode.UNKNOWN_TABLE in codes

    # 3. Query against profile-sensitive column ('CommissionRate') fails validation with SENSITIVE_COLUMN
    val_sensitive = validate_sql(
        sql="SELECT CommissionRate FROM Orders;",
        schema=model_schema,
        policy=policy,
    )
    assert val_sensitive.allowed is False
    codes = [r.code for r in val_sensitive.reasons]
    assert ValidationReasonCode.SENSITIVE_COLUMN in codes


@patch("query_pilot.db.profile_introspector.inspect")
def test_introspected_schema_integrates_with_profile_execution_boundary(
    mock_inspect_fn, fake_corp_profile, mock_db_engine, mock_db_connection, mock_inspector
):
    """ProfileExecutionBoundary executes queries using schema introspected by ProfileDatabaseIntrospector."""
    mock_inspect_fn.return_value = mock_inspector

    introspector = ProfileDatabaseIntrospector(
        config=fake_corp_profile,
        engine=mock_db_engine,
    )
    model_schema = introspector.introspect_model_facing()

    # Configure mock cursor for execution
    mock_cursor = MagicMock()
    mock_cursor.keys.return_value = ["CustomerID", "CustomerName"]
    mock_cursor.fetchmany.return_value = [(1, "Acme Corp"), (2, "Globex Inc")]
    mock_db_connection.execution_options.return_value.execute.return_value = mock_cursor

    boundary = ProfileExecutionBoundary(
        config=fake_corp_profile,
        schema=model_schema,
        engine=mock_db_engine,
    )

    result = boundary.execute("SELECT CustomerID, CustomerName FROM Customers;")
    assert result.row_count == 2
    assert result.columns == ["CustomerID", "CustomerName"]
    assert result.rows[0]["CustomerName"] == "Acme Corp"
