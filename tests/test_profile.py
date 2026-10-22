"""Unit tests for the connection-profile abstraction (offline only).

Verifies typed ConnectionProfile representation, trusted local ProfileStore
resolution, rejection of browser-supplied connection target overrides (including
nested dictionaries, aliases, and casing variants), hard global resource ceilings (15s/100 rows),
additive sensitive-column policies, safe credential handling/escaping, and immutable policies.
"""

import json
from pathlib import Path
import pytest
from pydantic import ValidationError

from query_pilot.db.models import ColumnMetadata, DatabaseSchema, TableMetadata
from query_pilot.db.profile import (
    GLOBAL_MAX_ROW_LIMIT,
    GLOBAL_MAX_TIMEOUT_SECONDS,
    ConnectionProfile,
    ConnectionTargetOverrideError,
    FORBIDDEN_CLIENT_OVERRIDE_KEYS,
    ProfileConfigurationError,
    ProfileStore,
    UnknownProfileError,
    enforce_global_row_limit_ceiling,
    enforce_global_timeout_ceiling,
    escape_odbc_value,
)


# ==============================================================================
# Pure Mock Fixtures (Fake Data Only — Zero Real DB Hostnames or Credentials)
# ==============================================================================


@pytest.fixture
def fake_trusted_profile() -> ConnectionProfile:
    """Provides a valid fake profile using Windows Trusted Connection."""
    return ConnectionProfile(
        profile_id="mock_corp_dw",
        display_name="Mock Corporate Warehouse",
        server=r"mock-sql-host\DEVINSTANCE",
        database_name="MockWarehouseDB",
        driver="ODBC Driver 17 for SQL Server",
        use_trusted_connection=True,
        timeout_seconds=15,
        max_row_limit=100,
        allowed_tables=["MockEmployees", "MockDepartments"],
        sensitive_columns=["Salary", "SSN"],
    )


@pytest.fixture
def fake_sql_auth_profile() -> ConnectionProfile:
    """Provides a valid fake profile using SQL Authentication with fake credentials."""
    return ConnectionProfile(
        profile_id="mock_sales_db",
        display_name="Mock Sales Database",
        server="mock-db-cluster.internal:1433",
        database_name="MockSalesDB",
        driver="ODBC Driver 18 for SQL Server",
        use_trusted_connection=False,
        user="mock_readonly_user",
        password="mock_dummy_secret_password_123",
        timeout_seconds=10,
        max_row_limit=50,
    )


@pytest.fixture
def fake_schema_alpha() -> DatabaseSchema:
    """Provides a fake DatabaseSchema for Profile Alpha."""
    return DatabaseSchema(
        database_name="MockWarehouseDB",
        tables=[
            TableMetadata(
                schema_name="dbo",
                table_name="MockEmployees",
                columns=[
                    ColumnMetadata(name="EmployeeId", data_type="int", primary_key=True),
                    ColumnMetadata(name="FullName", data_type="varchar(100)"),
                    ColumnMetadata(name="DepartmentId", data_type="int"),
                    ColumnMetadata(name="HireDate", data_type="date"),
                ],
            ),
            TableMetadata(
                schema_name="dbo",
                table_name="MockDepartments",
                columns=[
                    ColumnMetadata(name="DepartmentId", data_type="int", primary_key=True),
                    ColumnMetadata(name="DepartmentName", data_type="varchar(50)"),
                ],
            ),
        ],
        is_model_facing=True,
    )


# ==============================================================================
# 1. Valid Profile Resolution & Attributes
# ==============================================================================


def test_valid_profile_resolution(fake_trusted_profile: ConnectionProfile):
    """Test that a valid profile resolves cleanly with expected attributes."""
    store = ProfileStore(profiles={"mock_corp_dw": fake_trusted_profile})

    resolved = store.resolve("mock_corp_dw")
    assert resolved.profile_id == "mock_corp_dw"
    assert resolved.display_name == "Mock Corporate Warehouse"
    assert resolved.server == r"mock-sql-host\DEVINSTANCE"
    assert resolved.database_name == "MockWarehouseDB"
    assert resolved.expected_db == "MockWarehouseDB"
    assert resolved.driver == "ODBC Driver 17 for SQL Server"
    assert resolved.use_trusted_connection is True
    assert resolved.timeout_seconds == 15
    assert resolved.max_row_limit == 100


def test_sql_auth_profile_formatting(fake_sql_auth_profile: ConnectionProfile):
    """Test connection string and password masking for SQL Authentication."""
    conn_str = fake_sql_auth_profile.get_odbc_connection_string()
    assert "SERVER=mock-db-cluster.internal:1433" in conn_str
    assert "DATABASE=MockSalesDB" in conn_str
    assert "UID=mock_readonly_user" in conn_str
    assert "PWD=mock_dummy_secret_password_123" in conn_str
    assert "Trusted_Connection=yes" not in conn_str

    # Safe logging connection string
    safe_conn_str = fake_sql_auth_profile.get_odbc_connection_string(hide_password=True)
    assert "PWD=********" in safe_conn_str
    assert "mock_dummy_secret_password_123" not in safe_conn_str

    # SQLAlchemy URL check
    url = fake_sql_auth_profile.get_sqlalchemy_url()
    assert url.startswith("mssql+pyodbc:///?odbc_connect=")

    safe_url = fake_sql_auth_profile.get_sqlalchemy_url(hide_password=True)
    assert "PWD%3D%2A%2A%2A%2A%2A%2A%2A%2A" in safe_url

    # Ensure password is a SecretStr and not exposed in repr
    assert "mock_dummy_secret_password_123" not in repr(fake_sql_auth_profile)
    assert "mock_dummy_secret_password_123" not in str(fake_sql_auth_profile)


def test_trusted_conn_string_formatting(fake_trusted_profile: ConnectionProfile):
    """Test ODBC connection string for trusted connection."""
    conn_str = fake_trusted_profile.get_odbc_connection_string()
    assert r"SERVER=mock-sql-host\DEVINSTANCE" in conn_str
    assert "DATABASE=MockWarehouseDB" in conn_str
    assert "Trusted_Connection=yes" in conn_str
    assert "UID=" not in conn_str


# ==============================================================================
# 2. Hard Global Resource Ceilings (15s / 100 rows)
# ==============================================================================


@pytest.mark.parametrize(
    "exceeded_kwargs,expected_err",
    [
        ({"timeout_seconds": 16}, "Input should be less than or equal to 15"),
        ({"timeout_seconds": 20}, "Input should be less than or equal to 15"),
        ({"timeout_seconds": 120}, "Input should be less than or equal to 15"),
        ({"max_row_limit": 101}, "Input should be less than or equal to 100"),
        ({"max_row_limit": 150}, "Input should be less than or equal to 100"),
        ({"max_row_limit": 1000}, "Input should be less than or equal to 100"),
    ],
)
def test_global_ceilings_rejected_at_profile_validation(exceeded_kwargs, expected_err):
    """Verify that configuration attempting to exceed global ceilings is strictly rejected."""
    base_params = {
        "profile_id": "test_ceilings",
        "display_name": "Test Ceilings",
        "server": "mock-server",
        "database_name": "MockDB",
        "use_trusted_connection": True,
    }
    base_params.update(exceeded_kwargs)

    with pytest.raises(ValidationError, match=expected_err):
        ConnectionProfile(**base_params)


def test_stricter_limits_permitted():
    """Verify that profiles may configure stricter limits than the global maximums."""
    profile = ConnectionProfile(
        profile_id="strict_profile",
        display_name="Strict Profile",
        server="mock-server",
        database_name="MockDB",
        timeout_seconds=5,
        max_row_limit=25,
    )
    assert profile.timeout_seconds == 5
    assert profile.max_row_limit == 25


def test_independent_execution_boundary_clamping():
    """Verify that execution boundary clamping independently enforces global ceilings."""
    profile = ConnectionProfile(
        profile_id="clamped_profile",
        display_name="Clamped Profile",
        server="mock-server",
        database_name="MockDB",
        timeout_seconds=10,
        max_row_limit=50,
    )

    # Within limits
    assert profile.clamp_timeout(5) == 5
    assert profile.clamp_row_limit(20) == 20

    # None falls back to profile limit
    assert profile.clamp_timeout(None) == 10
    assert profile.clamp_row_limit(None) == 50

    # Attempts to exceed profile limit are clamped to profile limit
    assert profile.clamp_timeout(12) == 10
    assert profile.clamp_row_limit(75) == 50

    # Attempts to exceed global ceiling are clamped to profile limit
    assert profile.clamp_timeout(999) == 10
    assert profile.clamp_row_limit(9999) == 50

    # Negative or 0 values clamped to 1
    assert profile.clamp_timeout(0) == 1
    assert profile.clamp_timeout(-10) == 1
    assert profile.clamp_row_limit(0) == 1

    # Standalone boundary functions
    assert enforce_global_timeout_ceiling(999) == GLOBAL_MAX_TIMEOUT_SECONDS
    assert enforce_global_timeout_ceiling(5) == 5
    assert enforce_global_timeout_ceiling(-5) == 1

    assert enforce_global_row_limit_ceiling(9999) == GLOBAL_MAX_ROW_LIMIT
    assert enforce_global_row_limit_ceiling(30) == 30
    assert enforce_global_row_limit_ceiling(-10) == 1


# ==============================================================================
# 3. Additive Sensitive-Column Protections
# ==============================================================================


def test_sensitive_columns_are_additive_and_preserve_canonical_defaults():
    """Verify custom sensitive columns do not replace or weaken canonical defaults."""
    profile = ConnectionProfile(
        profile_id="additive_profile",
        display_name="Additive Profile",
        server="mock-server",
        database_name="MockDB",
        sensitive_columns=["CustomSalary", "CustomTaxID"],
    )

    # Custom entries are protected
    assert profile.is_column_permitted("CustomSalary") is False
    assert profile.is_column_permitted("CustomTaxID") is False
    assert profile.is_column_permitted("customsalary") is False

    # Canonical defaults MUST remain protected
    assert profile.is_column_permitted("PasswordHash") is False
    assert profile.is_column_permitted("password") is False
    assert profile.is_column_permitted("token") is False
    assert profile.is_column_permitted("secret") is False
    assert profile.is_column_permitted("credit_card") is False
    assert profile.is_column_permitted("ssn") is False
    assert profile.is_column_permitted("private_key") is False

    # Non-sensitive columns permitted
    assert profile.is_column_permitted("DepartmentName") is True
    assert profile.is_column_permitted("CreatedAt") is True


# ==============================================================================
# 4. Safe Escaping & Credential Handling
# ==============================================================================


@pytest.mark.parametrize(
    "raw_val,expected_escaped",
    [
        ("simple_val", "simple_val"),
        ("val with spaces", "{val with spaces}"),
        ("val;with;semicolons", "{val;with;semicolons}"),
        ("val{with}braces", "{val{with}}braces}"),
        ("P@ss;w{o}rd!", "{P@ss;w{o}}rd!}"),
        (";pwd=injected;", "{;pwd=injected;}"),
        ("}}double_brace}}", "{}}}}double_brace}}}}}"),
        (" space_padded ", "{ space_padded }"),
    ],
)
def test_escape_odbc_value(raw_val, expected_escaped):
    """Test safe ODBC value escaping according to Microsoft ODBC specification."""
    assert escape_odbc_value(raw_val) == expected_escaped


def test_special_characters_in_credentials():
    """Verify that credentials containing semicolons, braces, and spaces are safely escaped."""
    profile = ConnectionProfile(
        profile_id="special_cred_profile",
        display_name="Special Creds",
        server="mock-server;1433",
        database_name="Mock DB;Test",
        use_trusted_connection=False,
        user="mock;user{1}",
        password="P@ss;w{o}rd!}}test",
    )

    conn_str = profile.get_odbc_connection_string()
    assert "SERVER={mock-server;1433}" in conn_str
    assert "DATABASE={Mock DB;Test}" in conn_str
    assert "UID={mock;user{1}}" in conn_str
    assert "PWD={P@ss;w{o}}rd!}}}}test}" in conn_str

    # Safe version masks the password
    safe_conn_str = profile.get_odbc_connection_string(hide_password=True)
    assert "PWD=********" in safe_conn_str
    assert "P@ss" not in safe_conn_str


# ==============================================================================
# 5. Policy Immutability (Tuple Collections & Defense Against Mutation)
# ==============================================================================


def test_policy_immutability_retained_references():
    """Verify that mutating original lists passed to constructor does not affect the profile."""
    allowed_list = ["Employees", "Departments"]
    sensitive_list = ["Salary"]

    profile = ConnectionProfile(
        profile_id="immut_profile",
        display_name="Immutable Profile",
        server="mock-server",
        database_name="MockDB",
        allowed_tables=allowed_list,
        sensitive_columns=sensitive_list,
    )

    # Mutate the caller's original collections
    allowed_list.append("MaliciousHackedTable")
    sensitive_list.append("FakeColumn")

    # Profile collections must remain unchanged
    assert "MaliciousHackedTable" not in profile.allowed_tables
    assert profile.is_table_permitted("MaliciousHackedTable") is False
    assert "FakeColumn" not in profile.sensitive_columns


def test_policy_immutability_direct_mutation():
    """Verify that attempting to mutate profile collections directly raises exceptions."""
    profile = ConnectionProfile(
        profile_id="immut_profile_2",
        display_name="Immutable Profile 2",
        server="mock-server",
        database_name="MockDB",
        allowed_tables=["Employees"],
        sensitive_columns=["Salary"],
    )

    # Profile collections are immutable tuples
    assert isinstance(profile.allowed_tables, tuple)
    assert isinstance(profile.sensitive_columns, tuple)

    with pytest.raises((AttributeError, TypeError)):
        profile.allowed_tables.append("Orders")  # type: ignore

    with pytest.raises(TypeError):
        profile.allowed_tables[0] = "Hacked"  # type: ignore


# ==============================================================================
# 6. Strengthened Client-Override Rejection (Aliases, Case Variants, Nested Payloads)
# ==============================================================================


@pytest.mark.parametrize(
    "case_variant",
    [
        "SERVER",
        "Server",
        "sErVeR",
        "DATABASE_NAME",
        "Database_Name",
        "EXPECTED_DB",
        "UID",
        "PWD",
        "PASSWORD",
        "CONNECTION_STRING",
    ],
)
def test_client_override_case_variants_rejected(
    fake_trusted_profile: ConnectionProfile,
    case_variant: str,
):
    """Verify that casing variants of forbidden keys are rejected."""
    store = ProfileStore(profiles={"mock_corp_dw": fake_trusted_profile})

    payload = {"question": "Show all data", case_variant: "attacker_value"}
    with pytest.raises(ConnectionTargetOverrideError) as exc_info:
        store.resolve("mock_corp_dw", client_request=payload)

    assert "SECURITY VIOLATION" in str(exc_info.value)
    assert case_variant.lower() in str(exc_info.value).lower()


@pytest.mark.parametrize(
    "alias_key",
    [
        "data_source",
        "datasource",
        "addr",
        "address",
        "network_address",
        "port",
        "endpoint",
        "instance",
        "server_name",
        "db",
        "db_name",
        "dbname",
        "initial_catalog",
        "initialcatalog",
        "catalog",
        "target_db",
        "target_database",
        "odbc_driver",
        "dialect",
        "username",
        "user_id",
        "pwd",
        "auth",
        "authentication",
        "integrated_security",
        "conn_str",
        "connstr",
        "connectionstring",
        "odbc_connect",
        "dsn",
        "table_whitelist",
        "column_denylist",
        "query_timeout",
        "row_limit",
        "limit",
        "max_rows",
    ],
)
def test_client_override_aliases_rejected(
    fake_trusted_profile: ConnectionProfile,
    alias_key: str,
):
    """Verify that connection-target aliases and synonyms are rejected."""
    store = ProfileStore(profiles={"mock_corp_dw": fake_trusted_profile})

    payload = {"question": "Show all data", alias_key: "attacker_value"}
    with pytest.raises(ConnectionTargetOverrideError) as exc_info:
        store.resolve("mock_corp_dw", client_request=payload)

    assert "SECURITY VIOLATION" in str(exc_info.value)
    assert alias_key.lower() in str(exc_info.value).lower()


@pytest.mark.parametrize(
    "nested_payload",
    [
        {"question": "Query", "options": {"server": "evil.host"}},
        {"question": "Query", "config": {"db_name": "master"}},
        {"question": "Query", "auth": {"password": "injected"}},
        {"question": "Query", "filters": [{"params": {"data_source": "hacked"}}]},
        {"question": "Query", "deep": {"nested": {"catalog": "sensitive_db"}}},
    ],
)
def test_client_override_nested_payloads_rejected(
    fake_trusted_profile: ConnectionProfile,
    nested_payload: dict,
):
    """Verify that nested dictionaries and lists containing forbidden keys are rejected."""
    store = ProfileStore(profiles={"mock_corp_dw": fake_trusted_profile})

    with pytest.raises(ConnectionTargetOverrideError) as exc_info:
        store.resolve("mock_corp_dw", client_request=nested_payload)

    assert "SECURITY VIOLATION" in str(exc_info.value)


# ==============================================================================
# 7. Safe Summary & Exposure Control
# ==============================================================================


def test_safe_summary_omits_secrets_and_connection_strings(
    fake_sql_auth_profile: ConnectionProfile,
):
    """Verify that to_safe_summary exposes only metadata suitable for UI dropdowns."""
    summary = fake_sql_auth_profile.to_safe_summary()

    assert summary["profile_id"] == "mock_sales_db"
    assert summary["display_name"] == "Mock Sales Database"
    assert summary["database_name"] == "MockSalesDB"
    assert summary["timeout_seconds"] == 10
    assert summary["max_row_limit"] == 50

    # Ensure credentials and network targets are absent
    assert "server" not in summary
    assert "user" not in summary
    assert "password" not in summary
    assert "connection_string" not in summary
    assert "mock_dummy_secret_password_123" not in str(summary)


# ==============================================================================
# 8. ProfileStore Dictionary & JSON Loading
# ==============================================================================


def test_profile_store_from_dict():
    """Test loading ProfileStore from raw dictionary formats."""
    data = {
        "profiles": {
            "profile_one": {
                "display_name": "Profile One",
                "server": "mock-server-1",
                "database_name": "DB_One",
                "use_trusted_connection": True,
            },
            "profile_two": {
                "display_name": "Profile Two",
                "server": "mock-server-2",
                "database_name": "DB_Two",
                "use_trusted_connection": True,
            },
        }
    }
    store = ProfileStore.from_dict(data)
    assert len(store) == 2
    assert store.resolve("profile_one").database_name == "DB_One"
    assert store.resolve("profile_two").database_name == "DB_Two"


def test_profile_store_from_json_file(tmp_path: Path):
    """Test loading ProfileStore from a JSON file."""
    config_file = tmp_path / "agent_profiles.json"
    data = {
        "profiles": {
            "local_test": {
                "display_name": "Local Test Profile",
                "server": "mock-host",
                "database_name": "MockTestDB",
                "use_trusted_connection": True,
            }
        }
    }
    config_file.write_text(json.dumps(data), encoding="utf-8")

    store = ProfileStore.from_json_file(config_file)
    assert len(store) == 1
    profile = store.resolve("local_test")
    assert profile.display_name == "Local Test Profile"
    assert profile.database_name == "MockTestDB"


def test_profile_store_from_json_file_missing():
    """Test that loading from a non-existent file raises ProfileConfigurationError."""
    with pytest.raises(ProfileConfigurationError) as exc_info:
        ProfileStore.from_json_file("non_existent_profiles_file_xyz.json")
    assert "not found" in str(exc_info.value)


def test_profile_store_from_dict_malformed():
    """Test that malformed dictionary raises ProfileConfigurationError."""
    with pytest.raises(ProfileConfigurationError):
        ProfileStore.from_dict("not a dictionary")  # type: ignore

    with pytest.raises(ProfileConfigurationError):
        ProfileStore.from_dict({"profiles": {"bad_profile": "not an object"}})


# ==============================================================================
# 9. Default Profile Resolution
# ==============================================================================


def test_profile_store_default_profile_resolution(fake_trusted_profile, fake_sql_auth_profile):
    """ProfileStore resolves default_profile_id when profile_id is None."""
    p_trusted = fake_trusted_profile.model_copy(update={"profile_id": "trusted"})
    p_sql = fake_sql_auth_profile.model_copy(update={"profile_id": "sql_auth"})
    store = ProfileStore(
        profiles={
            "trusted": p_trusted,
            "sql_auth": p_sql,
        },
        default_profile_id="trusted",
    )
    assert store.default_profile_id == "trusted"
    resolved = store.resolve(None)
    assert resolved.profile_id == "trusted"
    assert resolved.display_name == fake_trusted_profile.display_name

    # Client request override detection still triggers when resolving default profile
    with pytest.raises(ConnectionTargetOverrideError):
        store.resolve(None, client_request={"database": "evil_db"})


def test_profile_store_default_profile_missing_raises_unknown_profile_error(fake_trusted_profile):
    """ProfileStore raises UnknownProfileError when profile_id is None and no default is configured."""
    store = ProfileStore(profiles={"trusted": fake_trusted_profile})
    assert store.default_profile_id is None

    with pytest.raises(UnknownProfileError) as exc_info:
        store.resolve(None)
    assert "No default connection profile is configured" in str(exc_info.value)
    assert exc_info.value.profile_id == "None"


def test_profile_store_invalid_default_profile_raises_configuration_error(fake_trusted_profile):
    """Configuring a default_profile_id that is not in profiles raises ProfileConfigurationError."""
    with pytest.raises(ProfileConfigurationError, match="Default profile 'non_existent' is not present"):
        ProfileStore(
            profiles={"trusted": fake_trusted_profile},
            default_profile_id="non_existent",
        )


def test_profile_store_from_dict_with_default_profile():
    """ProfileStore.from_dict parses default_profile correctly."""
    data = {
        "default_profile": "profile_one",
        "profiles": {
            "profile_one": {
                "display_name": "Profile One",
                "server": "mock-server-1",
                "database_name": "DB_One",
                "use_trusted_connection": True,
            },
        },
    }
    store = ProfileStore.from_dict(data)
    assert store.default_profile_id == "profile_one"
    assert store.resolve(None).profile_id == "profile_one"


def test_profile_store_from_dict_with_invalid_default_profile():
    """ProfileStore.from_dict rejects unknown default_profile."""
    data = {
        "default_profile": "unknown_profile",
        "profiles": {
            "profile_one": {
                "display_name": "Profile One",
                "server": "mock-server-1",
                "database_name": "DB_One",
                "use_trusted_connection": True,
            },
        },
    }
    with pytest.raises(ProfileConfigurationError, match="Default profile 'unknown_profile' is not present"):
        ProfileStore.from_dict(data)


# ==============================================================================
# 10. Fail-Closed Unknown Profile Resolution & Explicit DatabaseSettings
# ==============================================================================


def test_unknown_profile_id_typo_fails_closed_without_fallback(fake_trusted_profile):
    """Unknown profile IDs and typos MUST fail closed and never fall back to default profile or GradeSense_Local."""
    store = ProfileStore(
        profiles={"corp_dw": fake_trusted_profile},
        default_profile_id="corp_dw",
    )

    # 1. Obvious typo must fail closed
    with pytest.raises(UnknownProfileError) as exc_info:
        store.resolve("corp_dw_typo")
    assert "corp_dw_typo" in str(exc_info.value)
    assert exc_info.value.profile_id == "corp_dw_typo"

    # 2. GradeSense_Local is NOT an implicit fallback when unconfigured
    with pytest.raises(UnknownProfileError) as exc_info:
        store.resolve("GradeSense_Local")
    assert "GradeSense_Local" in str(exc_info.value)

    # 3. System database names must fail closed
    for sys_db in ["master", "model", "msdb", "tempdb"]:
        with pytest.raises(UnknownProfileError):
            store.resolve(sys_db)

    # 4. Empty/whitespace profile strings fail closed
    for bad_id in ["", "   ", "\t"]:
        with pytest.raises(UnknownProfileError):
            store.resolve(bad_id)


def test_profile_store_from_database_settings_explicit_bridge():
    """ProfileStore.from_database_settings explicitly registers and selects the profile."""
    from unittest.mock import MagicMock
    from query_pilot.config import DatabaseSettings

    mock_settings = MagicMock(spec=DatabaseSettings)
    mock_settings.DB_SERVER = r"localhost\SQLEXPRESS"
    mock_settings.DB_NAME = "GradeSense_ExplicitDev"
    mock_settings.DB_DRIVER = "ODBC Driver 17 for SQL Server"
    mock_settings.DB_USE_TRUSTED_CONNECTION = True
    mock_settings.DB_USER = None
    mock_settings.DB_PASSWORD = None
    mock_settings.DB_QUERY_TIMEOUT_SECONDS = 15
    mock_settings.DB_MAX_ROW_LIMIT = 100

    # 1. With set_as_default=True
    store = ProfileStore.from_database_settings(
        mock_settings,
        profile_id="gradesense_dev",
        set_as_default=True,
    )
    assert store.has_profile("gradesense_dev")
    assert store.default_profile_id == "gradesense_dev"

    # Explicit resolution
    prof = store.resolve("gradesense_dev")
    assert prof.database_name == "GradeSense_ExplicitDev"
    assert prof.profile_id == "gradesense_dev"

    # Default resolution
    default_prof = store.resolve(None)
    assert default_prof.profile_id == "gradesense_dev"
    assert default_prof.database_name == "GradeSense_ExplicitDev"

    # Typos still fail closed
    with pytest.raises(UnknownProfileError):
        store.resolve("gradesense_dev_typo")

    # 2. With set_as_default=False
    store_no_default = ProfileStore.from_database_settings(
        mock_settings,
        profile_id="gradesense_dev",
        set_as_default=False,
    )
    assert store_no_default.default_profile_id is None
    # Explicit resolution works
    assert store_no_default.resolve("gradesense_dev").database_name == "GradeSense_ExplicitDev"
    # Default resolution fails closed
    with pytest.raises(UnknownProfileError):
        store_no_default.resolve(None)


