"""Offline tests for the ProfileConnectionFactory boundary.

Verifies:
1. Profile resolution and configuration construction.
2. Complete profile isolation across distinct profiles.
3. Strict rejection of unknown profiles (fail closed).
4. Strict rejection of client override attempts (host, db, credentials, timeout, limits).
5. Expected database identity derived strictly from the trusted profile, never from client input.
6. Global resource ceiling enforcement (15s timeout, 100 rows).
7. Lazy, offline engine creation without opening database connections.
8. Runtime session verification (SELECT DB_NAME()) and fail-closed defense on mismatch.
9. Integration with create_profile_db_engine helper.
"""

from dataclasses import FrozenInstanceError
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.sql import text

from query_pilot.db.connection import (
    DatabaseConnectionError,
    DatabaseSecurityError,
    create_profile_db_engine,
    verify_database_context,
)
from query_pilot.db.factory import (
    ProfileConnectionFactory,
    ProfileEngineConfiguration,
)
from query_pilot.db.introspection import DEFAULT_SENSITIVE_COLUMNS
from query_pilot.db.profile import (
    GLOBAL_MAX_ROW_LIMIT,
    GLOBAL_MAX_TIMEOUT_SECONDS,
    ConnectionProfile,
    ConnectionTargetOverrideError,
    ProfileStore,
    UnknownProfileError,
)


@pytest.fixture
def fake_profiles() -> Dict[str, ConnectionProfile]:
    """Create a dictionary of isolated, validated test profiles."""
    p1 = ConnectionProfile(
        profile_id="analytics_dw",
        display_name="Enterprise Analytics DW",
        server="corp-dw.internal,1433",
        database_name="Analytics_DW_Prod",
        use_trusted_connection=True,
        timeout_seconds=12,
        max_row_limit=80,
        allowed_tables=["FactSales", "DimCustomer", "DimDate"],
        sensitive_columns=["SSN", "TaxId", "ExecutiveCompensation"],
        schema_name="dw",
    )
    p2 = ConnectionProfile(
        profile_id="finance_local",
        display_name="Finance Department Local",
        server="localhost\\sqlexpress",
        database_name="Finance_Local",
        use_trusted_connection=False,
        user="fin_reader",
        password="FinSecurePassword123!",
        timeout_seconds=5,
        max_row_limit=25,
        allowed_tables=None,
        sensitive_columns=None,
        schema_name="dbo",
    )
    p3 = ConnectionProfile(
        profile_id="boundary_ceilings",
        display_name="Ceilings Boundary Profile",
        server="srv-max.internal",
        database_name="MaxLimits_DB",
        use_trusted_connection=True,
        timeout_seconds=GLOBAL_MAX_TIMEOUT_SECONDS,
        max_row_limit=GLOBAL_MAX_ROW_LIMIT,
    )
    return {
        "analytics_dw": p1,
        "finance_local": p2,
        "boundary_ceilings": p3,
    }


@pytest.fixture
def fake_store(fake_profiles) -> ProfileStore:
    """Create a ProfileStore containing the fake profiles."""
    return ProfileStore(profiles=fake_profiles)


@pytest.fixture
def factory(fake_store) -> ProfileConnectionFactory:
    """Create a ProfileConnectionFactory backed by fake_store."""
    return ProfileConnectionFactory(store=fake_store)


# ==============================================================================
# 1. ProfileEngineConfiguration Dataclass Invariants
# ==============================================================================

def test_profile_engine_configuration_immutability(factory):
    """ProfileEngineConfiguration must be a frozen dataclass preventing mutation."""
    config = factory.get_configuration("analytics_dw")
    assert isinstance(config, ProfileEngineConfiguration)

    with pytest.raises(FrozenInstanceError):
        config.expected_db = "Hacked_DB"  # type: ignore

    with pytest.raises(FrozenInstanceError):
        config.timeout_seconds = 999  # type: ignore

    with pytest.raises(FrozenInstanceError):
        config.max_row_limit = 5000  # type: ignore


def test_profile_engine_configuration_password_masking(factory):
    """safe_url must mask passwords while sqlalchemy_url contains credentials."""
    import urllib.parse

    config = factory.get_configuration("finance_local")

    # Raw URL contains the password for engine creation
    unquoted_raw = urllib.parse.unquote_plus(config.sqlalchemy_url)
    assert "FinSecurePassword123!" in unquoted_raw
    assert "UID=fin_reader" in unquoted_raw

    # Safe diagnostic URL must mask password
    unquoted_safe = urllib.parse.unquote_plus(config.safe_url)
    assert "PWD=********" in unquoted_safe
    assert "FinSecurePassword123!" not in config.safe_url
    assert "UID=fin_reader" in unquoted_safe


# ==============================================================================
# 2. Profile Resolution, Expected Database Derivation & Profile Isolation
# ==============================================================================

def test_resolve_profile_and_isolation(factory):
    """Resolving different profiles yields completely isolated configurations."""
    import urllib.parse

    cfg_analytics = factory.get_configuration("analytics_dw")
    cfg_finance = factory.get_configuration("finance_local")

    # Analytics profile assertions
    assert cfg_analytics.profile_id == "analytics_dw"
    assert cfg_analytics.display_name == "Enterprise Analytics DW"
    assert cfg_analytics.expected_db == "Analytics_DW_Prod"
    assert cfg_analytics.timeout_seconds == 12
    assert cfg_analytics.max_row_limit == 80
    assert cfg_analytics.allowed_tables == ("FactSales", "DimCustomer", "DimDate")
    assert cfg_analytics.schema_name == "dw"
    assert "corp-dw.internal" in cfg_analytics.sqlalchemy_url
    assert "Trusted_Connection=yes" in urllib.parse.unquote_plus(cfg_analytics.sqlalchemy_url)

    # Finance profile assertions
    assert cfg_finance.profile_id == "finance_local"
    assert cfg_finance.display_name == "Finance Department Local"
    assert cfg_finance.expected_db == "Finance_Local"
    assert cfg_finance.timeout_seconds == 5
    assert cfg_finance.max_row_limit == 25
    assert cfg_finance.allowed_tables is None
    assert cfg_finance.schema_name == "dbo"
    assert "localhost\\sqlexpress" in urllib.parse.unquote_plus(cfg_finance.sqlalchemy_url)
    assert "UID=fin_reader" in urllib.parse.unquote_plus(cfg_finance.sqlalchemy_url)

    # Verify zero bleed-over
    assert cfg_analytics.expected_db != cfg_finance.expected_db
    assert cfg_analytics.timeout_seconds != cfg_finance.timeout_seconds
    assert cfg_analytics.max_row_limit != cfg_finance.max_row_limit
    assert cfg_analytics.sqlalchemy_url != cfg_finance.sqlalchemy_url


def test_expected_db_derived_strictly_from_profile_not_profile_id_or_request(factory):
    """expected_db must be derived strictly from profile.database_name, never profile_id."""
    # Profile ID is "analytics_dw", but database name is "Analytics_DW_Prod"
    cfg = factory.get_configuration("analytics_dw")
    assert cfg.expected_db == "Analytics_DW_Prod"
    assert cfg.expected_db != "analytics_dw"


def test_unknown_profile_rejection(factory):
    """Resolving an unknown profile ID must fail closed with UnknownProfileError."""
    with pytest.raises(UnknownProfileError, match="Unknown profile 'ghost_profile'"):
        factory.resolve_profile("ghost_profile")

    with pytest.raises(UnknownProfileError, match="Unknown profile 'master'"):
        factory.get_configuration("master")

    with pytest.raises(UnknownProfileError):
        factory.create_engine("unregistered_id")


# ==============================================================================
# 3. Client Override Rejection (Security Boundary)
# ==============================================================================

@pytest.mark.parametrize(
    "override_payload",
    [
        {"server": "evil-sql.attacker.com"},
        {"database": "master"},
        {"database_name": "CreditCardVault"},
        {"expected_db": "Unauthorized_DB"},
        {"user": "sa"},
        {"password": "AttackerPassword1"},
        {"pwd": "InjectedPassword"},
        {"connection_string": "DRIVER={ODBC Driver 18};SERVER=evil;"},
        {"url": "mssql+pyodbc://attacker:pass@evil/db"},
        {"timeout": 120},
        {"timeout_seconds": 60},
        {"max_row_limit": 50000},
        {"row_limit": 1000},
        {"allowed_tables": ["Users", "Passwords"]},
        {"sensitive_columns": []},
    ],
)
def test_client_override_rejection_at_factory_boundary(factory, override_payload):
    """Any client request containing connection target or security override keys is rejected."""
    with pytest.raises(ConnectionTargetOverrideError, match="SECURITY VIOLATION: Client is not permitted to provide or override"):
        factory.resolve_profile("analytics_dw", client_request=override_payload)

    with pytest.raises(ConnectionTargetOverrideError, match="SECURITY VIOLATION: Client is not permitted to provide or override"):
        factory.get_configuration("analytics_dw", client_request=override_payload)

    with pytest.raises(ConnectionTargetOverrideError, match="SECURITY VIOLATION: Client is not permitted to provide or override"):
        factory.create_engine("analytics_dw", client_request=override_payload)

    with pytest.raises(ConnectionTargetOverrideError, match="SECURITY VIOLATION: Client is not permitted to provide or override"):
        with factory.get_connection("analytics_dw", client_request=override_payload):
            pass


def test_nested_client_override_rejection(factory):
    """Nested client override payloads must be caught and rejected recursively."""
    nested_payload = {
        "client_options": {
            "advanced": {
                "server": "10.0.0.1",
            }
        }
    }
    with pytest.raises(ConnectionTargetOverrideError, match="SECURITY VIOLATION: Client is not permitted to provide or override"):
        factory.get_configuration("analytics_dw", client_request=nested_payload)


# ==============================================================================
# 4. Global Resource Ceiling Enforcement
# ==============================================================================

def test_resource_ceilings_enforced_in_configuration(factory):
    """Timeout and row limit must never exceed hard global ceilings (15s, 100 rows)."""
    cfg_max = factory.get_configuration("boundary_ceilings")
    assert cfg_max.timeout_seconds == 15
    assert cfg_max.max_row_limit == 100
    assert cfg_max.engine_kwargs["connect_args"]["timeout"] == 15

    # Stricter profile preserves strict boundaries
    cfg_fin = factory.get_configuration("finance_local")
    assert cfg_fin.timeout_seconds == 5
    assert cfg_fin.max_row_limit == 25
    assert cfg_fin.engine_kwargs["connect_args"]["timeout"] == 5


def test_sensitive_column_policy_includes_canonical_defaults(factory):
    """Sensitive columns in configuration must include all canonical defaults."""
    cfg = factory.get_configuration("analytics_dw")

    # Canonical columns must all be present
    for canon in DEFAULT_SENSITIVE_COLUMNS:
        assert canon in cfg.sensitive_columns

    # Custom sensitive columns must also be present (lowercased)
    assert "ssn" in cfg.sensitive_columns
    assert "taxid" in cfg.sensitive_columns
    assert "executivecompensation" in cfg.sensitive_columns


# ==============================================================================
# 5. Offline Engine Creation without Database Connections
# ==============================================================================

def test_create_engine_lazy_without_connection(factory):
    """factory.create_engine creates an Engine object without calling connect()."""
    engine = factory.create_engine("analytics_dw")
    assert isinstance(engine, Engine)

    # Engine dialect must be mssql+pyodbc
    assert engine.dialect.name == "mssql"
    assert engine.dialect.driver == "pyodbc"

    # Confirm engine has not opened any connection pool connections
    pool = engine.pool
    assert pool.checkedin() == 0
    assert pool.checkedout() == 0


def test_create_engine_with_mock_engine_creator(fake_store):
    """factory.create_engine delegates to engine_creator with expected URL and kwargs."""
    mock_engine = MagicMock(spec=Engine)
    mock_creator = MagicMock(return_value=mock_engine)

    factory = ProfileConnectionFactory(store=fake_store, engine_creator=mock_creator)
    result = factory.create_engine("analytics_dw", echo=True)

    assert result is mock_engine
    mock_creator.assert_called_once()
    call_args, call_kwargs = mock_creator.call_args

    # First positional arg is sqlalchemy_url
    assert "corp-dw.internal" in call_args[0]
    assert "Analytics_DW_Prod" in call_args[0]

    # Kwargs include pool_pre_ping, connect_args with timeout, and caller overrides
    assert call_kwargs["pool_pre_ping"] is True
    assert call_kwargs["connect_args"] == {"timeout": 12}
    assert call_kwargs["echo"] is True


def test_create_profile_db_engine_helper(fake_profiles):
    """create_profile_db_engine helper creates an engine from a ConnectionProfile."""
    profile = fake_profiles["finance_local"]
    engine = create_profile_db_engine(profile)
    assert isinstance(engine, Engine)
    assert engine.dialect.name == "mssql"


def test_create_engine_wraps_errors_in_database_connection_error(fake_store):
    """Exceptions during engine creation must be wrapped in DatabaseConnectionError."""
    def broken_creator(*args, **kwargs):
        raise ValueError("Simulated engine creation failure")

    factory = ProfileConnectionFactory(store=fake_store, engine_creator=broken_creator)
    with pytest.raises(DatabaseConnectionError, match="Failed to create SQLAlchemy engine"):
        factory.create_engine("analytics_dw")


# ==============================================================================
# 6. Runtime Session Context Verification (SELECT DB_NAME())
# ==============================================================================

def test_get_connection_success_when_database_context_matches(factory):
    """When DB_NAME() matches profile.expected_db, connection is yielded successfully."""
    mock_conn = MagicMock(spec=Connection)
    mock_conn.execute.return_value.scalar.return_value = "Analytics_DW_Prod"

    mock_engine = MagicMock(spec=Engine)
    mock_engine.connect.return_value = mock_conn

    with factory.get_connection("analytics_dw", engine=mock_engine) as conn:
        assert conn is mock_conn

    # Verify SELECT DB_NAME() was executed
    mock_conn.execute.assert_called_once()
    query_text = str(mock_conn.execute.call_args[0][0])
    assert "SELECT DB_NAME()" in query_text

    # Verify connection was closed on exit
    mock_conn.close.assert_called_once()


def test_get_connection_fails_closed_on_database_context_mismatch(factory):
    """When DB_NAME() does not match expected_db, DatabaseSecurityError is raised immediately."""
    mock_conn = MagicMock(spec=Connection)
    # Target expects Analytics_DW_Prod, but SQL Server returns master
    mock_conn.execute.return_value.scalar.return_value = "master"

    mock_engine = MagicMock(spec=Engine)
    mock_engine.connect.return_value = mock_conn

    with pytest.raises(DatabaseSecurityError, match="SECURITY VIOLATION: Connected to unauthorized database 'master'"):
        with factory.get_connection("analytics_dw", engine=mock_engine):
            pass

    # Connection must still be closed on failure
    mock_conn.close.assert_called_once()


def test_get_connection_handles_connection_establishment_error(factory):
    """Connection errors during engine.connect() are wrapped in DatabaseConnectionError."""
    mock_engine = MagicMock(spec=Engine)
    mock_engine.connect.side_effect = OperationalError("statement", {}, Exception("Network unreachable"))

    with pytest.raises(DatabaseConnectionError, match="Failed to establish connection for profile 'analytics_dw'"):
        with factory.get_connection("analytics_dw", engine=mock_engine):
            pass


# ==============================================================================
# 7. Factory Default Profile Resolution & Resource Limits
# ==============================================================================


def test_factory_resolves_default_profile(fake_profiles):
    """Factory methods accept profile_id=None and resolve the store default profile."""
    store = ProfileStore(profiles=fake_profiles, default_profile_id="analytics_dw")
    factory = ProfileConnectionFactory(store=store)

    # 1. resolve_profile(None)
    profile = factory.resolve_profile(None)
    assert profile.profile_id == "analytics_dw"
    assert profile.database_name == "Analytics_DW_Prod"

    # 2. get_configuration(None)
    config = factory.get_configuration(None)
    assert config.profile_id == "analytics_dw"
    assert config.expected_db == "Analytics_DW_Prod"
    assert config.timeout_seconds == 12
    assert config.max_row_limit == 80
    assert config.engine_kwargs["connect_args"]["timeout"] == 12

    # 3. create_engine(None)
    engine = factory.create_engine(None)
    assert isinstance(engine, Engine)
    assert engine.dialect.name == "mssql"


def test_factory_fails_closed_when_default_profile_unconfigured(fake_profiles):
    """Factory raises UnknownProfileError when profile_id=None and store has no default profile."""
    store = ProfileStore(profiles=fake_profiles, default_profile_id=None)
    factory = ProfileConnectionFactory(store=store)

    with pytest.raises(UnknownProfileError) as exc_info:
        factory.resolve_profile(None)
    assert "No default connection profile is configured" in str(exc_info.value)

    with pytest.raises(UnknownProfileError):
        factory.get_configuration(None)

    with pytest.raises(UnknownProfileError):
        factory.create_engine(None)


# ==============================================================================
# 8. Boundary Distinctions, Typo Fail-Closed, & Session Identity Verification
# ==============================================================================


def test_factory_typo_profile_fails_closed(factory):
    """Factory methods MUST fail closed on profile typos and never fall back to default profile or other DB."""
    typos = ["analytics_dw_typo", "ANALYTICS_DW", "GradeSense_Local", "master", "tempdb", "  "]

    for typo in typos:
        with pytest.raises(UnknownProfileError):
            factory.resolve_profile(typo)

        with pytest.raises(UnknownProfileError):
            factory.get_configuration(typo)

        with pytest.raises(UnknownProfileError):
            factory.create_engine(typo)

        with pytest.raises(UnknownProfileError):
            with factory.get_connection(typo):
                pass


def test_resource_limits_boundary_distinction_driver_timeout_vs_statement_timeout(factory):
    """Verify clean separation between connection-establishment timeout and SQL statement timeout.

    Security & Operational Boundary:
    1. Driver connection timeout is passed via engine_kwargs["connect_args"]["timeout"].
       This controls ODBC login/handshake timeout (SQL_ATTR_LOGIN_TIMEOUT).
    2. SQL statement execution timeout is stored as timeout_seconds (clamped <= 15s).
       This does NOT automatically cancel long-running SQL statements; it MUST be applied
       downstream by the query executor (via raw_conn.timeout or conn.execution_options(timeout=...)).
    """
    config = factory.get_configuration("analytics_dw")

    # 1. Driver connection timeout
    assert "connect_args" in config.engine_kwargs
    assert config.engine_kwargs["connect_args"]["timeout"] == config.timeout_seconds
    assert config.timeout_seconds <= GLOBAL_MAX_TIMEOUT_SECONDS

    # 2. Mock execution demonstrating that engine connection creation alone does not set statement timeout
    mock_conn = MagicMock(spec=Connection)
    mock_conn.execute.return_value.scalar.return_value = "Analytics_DW_Prod"
    # Underlying dbapi raw connection
    raw_dbapi = MagicMock()
    mock_conn.connection.dbapi_connection = raw_dbapi

    mock_engine = MagicMock(spec=Engine)
    mock_engine.connect.return_value = mock_conn

    with factory.get_connection("analytics_dw", engine=mock_engine) as conn:
        # At connection yield time, verify_database_context has run, but statement timeout has NOT been set
        assert getattr(raw_dbapi, "timeout", None) != config.timeout_seconds

        # The execution layer must explicitly enforce timeout at statement execution time:
        raw_dbapi.timeout = config.timeout_seconds
        assert raw_dbapi.timeout == 12

    mock_conn.close.assert_called_once()


def test_row_ceiling_boundary_distinction_stored_limit_vs_bounded_fetching(factory):
    """Verify that row ceiling in configuration requires bounded cursor fetching to truncate.

    Security & Resource Boundary:
    1. ProfileEngineConfiguration.max_row_limit is clamped <= 100 rows.
    2. Storing max_row_limit does not truncate results on its own; bounded fetching (fetchmany)
       is required at the executor layer to prevent unbounded memory consumption.
    """
    config = factory.get_configuration("analytics_dw")
    assert config.max_row_limit == 80
    assert config.max_row_limit <= GLOBAL_MAX_ROW_LIMIT

    # Mock cursor producing 150 rows (exceeding limit)
    mock_cursor = MagicMock()
    fake_rows = [{"id": i} for i in range(150)]
    mock_cursor.fetchmany.side_effect = lambda n: fake_rows[:n]

    # Executor-level bounded fetching simulation:
    # Only fetches limit + 1 rows to detect truncation without unbounded buffering
    fetched_rows = mock_cursor.fetchmany(config.max_row_limit + 1)
    assert len(fetched_rows) == 81
    is_truncated = len(fetched_rows) > config.max_row_limit
    effective_rows = fetched_rows[:config.max_row_limit] if is_truncated else fetched_rows

    assert is_truncated is True
    assert len(effective_rows) == 80


def test_expected_database_identity_cannot_be_overridden_and_mismatch_fails_closed(factory):
    """Caller-supplied expected_db or database overrides are rejected, and identity mismatch closes connection."""
    # 1. Untrusted client attempt to override expected database identity is rejected
    override_payload = {"expected_db": "Unauthorized_Target_DB"}
    with pytest.raises(ConnectionTargetOverrideError, match="SECURITY VIOLATION"):
        factory.get_configuration("analytics_dw", client_request=override_payload)

    # 2. Mock connection returning unexpected DB name raises DatabaseSecurityError and closes connection
    mock_conn = MagicMock(spec=Connection)
    mock_conn.execute.return_value.scalar.return_value = "Compromised_DB"

    mock_engine = MagicMock(spec=Engine)
    mock_engine.connect.return_value = mock_conn

    with pytest.raises(DatabaseSecurityError, match="SECURITY VIOLATION"):
        with factory.get_connection("analytics_dw", engine=mock_engine):
            pass

    # Connection MUST be closed despite exception
    mock_conn.close.assert_called_once()


