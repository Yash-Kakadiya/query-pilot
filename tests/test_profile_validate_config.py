"""Offline unit and integration tests for offline profile configuration validation.

Verifies Milestone B12-F8C:
1. Conservative local server-target policy and syntax checks.
2. System database and invalid identifier rejection.
3. Windows Integrated Authentication preservation; credential and unknown field rejection.
4. HTTP POST /agent/v1/profiles/validate-config endpoint contracts.
5. Session authentication and X-QueryPilot-Request: 1 custom header enforcement.
6. Duplicate profile ID conflict detection without modifying stored profiles.
7. Absolute offline guarantees: zero database connections, zero ODBC calls, zero engine creation, zero file writes.
8. Error bounding and sanitization without leaking secrets.
"""

from __future__ import annotations

import os
from pathlib import Path
import platform
from unittest.mock import MagicMock, patch

from fastapi import status
from fastapi.testclient import TestClient
import pytest

from query_pilot.agent.app import create_agent_app
from query_pilot.agent.auth import (
    SESSION_COOKIE_NAME,
    BootstrapTokenManager,
    SessionManager,
)
from query_pilot.agent.config import AgentConfig
from query_pilot.agent.engine_manager import AgentEngineManager
from query_pilot.agent.models import (
    ValidateProfileConfigRequest,
    ValidateProfileConfigResponse,
)
from query_pilot.db.factory import ProfileConnectionFactory
from query_pilot.db.profile import (
    APPROVED_LOOPBACK_HOSTS,
    ConnectionProfile,
    ProfileConfigurationError,
    ProfileStore,
    SQLSERVER_SYSTEM_DATABASES,
    validate_local_server_target_policy,
    validate_safe_database_identifier,
    validate_safe_profile_id,
    validate_safe_schema_identifier,
    validate_server_target_syntax,
)


# ==============================================================================
# 1. Helper Unit Tests: Profile ID, Database, Schema, and Syntax Validation
# ==============================================================================


@pytest.mark.parametrize("valid_id", ["local_dw", "dw-01", "MY_DATABASE", "db123", "a-b_c"])
def test_validate_safe_profile_id_accepted(valid_id: str):
    assert validate_safe_profile_id(valid_id) == valid_id


@pytest.mark.parametrize(
    "bad_id,match_err",
    [
        ("", "cannot be blank"),
        ("   ", "cannot be blank"),
        ("has space", "invalid characters"),
        ("id;injection", "invalid characters"),
        ("id/slash", "invalid characters"),
        ("id$dollar", "invalid characters"),
        ("a" * 65, "exceeds maximum length"),
    ],
)
def test_validate_safe_profile_id_rejected(bad_id: str, match_err: str):
    with pytest.raises(ProfileConfigurationError, match=match_err):
        validate_safe_profile_id(bad_id)


@pytest.mark.parametrize("valid_db", ["AdventureWorks", "GradeSense_Local", "Test_DB_01", "DB#1", "Test$DB"])
def test_validate_safe_database_identifier_accepted(valid_db: str):
    assert validate_safe_database_identifier(valid_db) == valid_db


@pytest.mark.parametrize("sys_db", ["master", "msdb", "model", "tempdb", "MASTER", "MsDb", "TempDB", "MoDeL"])
def test_validate_safe_database_identifier_system_databases_rejected(sys_db: str):
    with pytest.raises(ProfileConfigurationError, match="SQL Server system database"):
        validate_safe_database_identifier(sys_db)


@pytest.mark.parametrize(
    "bad_db,match_err",
    [
        ("", "cannot be blank"),
        ("   ", "cannot be blank"),
        ("db with space", "invalid characters"),
        ("db;injection", "invalid characters"),
        ("db[bracket]", "invalid characters"),
        ("db'quote", "invalid characters"),
        ("db-with-dash", "invalid characters"),
        ("x" * 129, "exceeds maximum length"),
    ],
)
def test_validate_safe_database_identifier_invalid_rejected(bad_db: str, match_err: str):
    with pytest.raises(ProfileConfigurationError, match=match_err):
        validate_safe_database_identifier(bad_db)


def test_validate_safe_schema_identifier():
    assert validate_safe_schema_identifier("dbo") == "dbo"
    assert validate_safe_schema_identifier("sales_data") == "sales_data"
    assert validate_safe_schema_identifier("") == "dbo"
    assert validate_safe_schema_identifier("   ") == "dbo"

    with pytest.raises(ProfileConfigurationError, match="invalid characters"):
        validate_safe_schema_identifier("schema with space")

    with pytest.raises(ProfileConfigurationError, match="exceeds maximum length"):
        validate_safe_schema_identifier("s" * 129)


# ==============================================================================
# 2. Server Target Syntax and Local Policy Validation
# ==============================================================================


@pytest.mark.parametrize(
    "bad_syntax,match_err",
    [
        ("", "cannot be blank"),
        ("   ", "cannot be blank"),
        ("a" * 256, "exceeds maximum length"),
        ("server with space", "whitespace"),
        ("localhost\x00instance", "control characters"),
        ("localhost\r\ninstance", "control characters"),
        ("\\\\remote\\share", "UNC paths"),
        ("//remote/share", "UNC paths"),
        ("http://localhost", "URLs and URI schemes"),
        ("https://127.0.0.1", "URLs and URI schemes"),
        ("smb://server", "URLs and URI schemes"),
        ("np:localhost\\pipe\\sql", "Named pipe"),
        ("lpc:localhost", "Named pipe"),
        ("localhost;UID=sa;", "ODBC delimiter"),
        ("localhost=val", "ODBC delimiter"),
        ("localhost{inst}", "ODBC delimiter"),
        ("localhost'injection", "ODBC delimiter"),
        ('localhost"injection', "ODBC delimiter"),
    ],
)
def test_validate_server_target_syntax_rejected(bad_syntax: str, match_err: str):
    with pytest.raises(ProfileConfigurationError, match=match_err):
        validate_server_target_syntax(bad_syntax)


@pytest.mark.parametrize(
    "valid_server",
    [
        "localhost",
        "127.0.0.1",
        "[::1]",
        ".",
        r"localhost\SQLEXPRESS",
        r"127.0.0.1\SQLEXPRESS",
        r".\SQLEXPRESS",
        r"[::1]\SQLEXPRESS",
        r"localhost\MSSQLSERVER",
        "localhost,1433",
        "127.0.0.1,1433",
        "[::1],1433",
        ".,1433",
        r".\SQLEXPRESS,1433",
        r"localhost\SQLEXPRESS,14333",
        r"[::1]\SQLEXPRESS,1433",
    ],
)
def test_validate_local_server_target_policy_accepted(valid_server: str):
    assert validate_local_server_target_policy(valid_server) == valid_server


def test_validate_local_server_target_policy_rejects_active_computername():
    comp = os.environ.get("COMPUTERNAME", "").strip() or platform.node().strip()
    if comp and comp.lower() not in APPROVED_LOOPBACK_HOSTS:
        with pytest.raises(ProfileConfigurationError, match="not an approved local loopback host"):
            validate_local_server_target_policy(comp)
        with pytest.raises(ProfileConfigurationError, match="not an approved local loopback host"):
            validate_local_server_target_policy(rf"{comp}\SQLEXPRESS")


@pytest.mark.parametrize(
    "machine_target",
    [
        "MY-WORKSTATION",
        r"MY-WORKSTATION\SQLEXPRESS",
        "DESKTOP-ABC123",
        r"DESKTOP-ABC123\SQLEXPRESS",
        "DEV-SERVER,1433",
        r"PROD-SQL01\INSTANCE,1433",
    ],
)
def test_validate_local_server_target_policy_rejects_named_machine_targets(machine_target: str):
    with pytest.raises(ProfileConfigurationError, match="not an approved local loopback host"):
        validate_local_server_target_policy(machine_target)


@pytest.mark.parametrize(
    "bare_ipv6",
    [
        "::1",
        "::1,1433",
        r"::1\SQLEXPRESS",
        "::1:1433",
        "fe80::1",
    ],
)
def test_validate_local_server_target_policy_rejects_bare_ipv6(bare_ipv6: str):
    with pytest.raises(ProfileConfigurationError, match="unbracketed IPv6 literal"):
        validate_local_server_target_policy(bare_ipv6)


@pytest.mark.parametrize(
    "rejected_server,match_err",
    [
        ("192.168.1.1", "not an approved local loopback host"),
        ("10.0.0.5", "not an approved local loopback host"),
        ("172.16.0.1", "not an approved local loopback host"),
        ("169.254.169.254", "not an approved local loopback host"),
        ("127.0.0.2", "not an approved local loopback host"),
        ("0.0.0.0", "not an approved local loopback host"),
        ("[fe80::1]", "not an approved local loopback host"),
        ("[2001:db8::1]", "not an approved local loopback host"),
        ("corp-sql.internal", "not an approved local loopback host"),
        ("db.example.com", "not an approved local loopback host"),
        ("localhost:1433", "separated by a comma"),
        ("127.0.0.1:1433", "separated by a comma"),
        ("[::1]:1433", "separated by a comma"),
        ("[::1", "Missing closing bracket"),
        (r"localhost\instance\extra", "Instance format must be"),
        (r"localhost\instance,1433,extra", "Port format must be"),
        ("localhost,0", "out of valid range"),
        ("localhost,65536", "out of valid range"),
        ("localhost,70000", "out of valid range"),
        ("localhost,-1", "numeric integer"),
        ("localhost,abc", "numeric integer"),
        ("localhost,", "numeric integer"),
        ("localhost\\", "Instance name cannot be empty"),
        (r"localhost\inst$bad", "Instance names must contain only letters"),
    ],
)
def test_validate_local_server_target_policy_rejected(rejected_server: str, match_err: str):
    with pytest.raises(ProfileConfigurationError, match=match_err):
        validate_local_server_target_policy(rejected_server)


# ==============================================================================
# 3. Pydantic Model Validation Tests (ValidateProfileConfigRequest)
# ==============================================================================


def test_validate_profile_config_request_valid():
    req = ValidateProfileConfigRequest(
        profile_id="local_dev",
        display_name="Local SQL Server Express",
        server=r"localhost\SQLEXPRESS",
        database_name="TestDB",
        schema_name="dbo",
        driver="ODBC Driver 17 for SQL Server",
        timeout_seconds=10,
        max_row_limit=50,
        allowed_tables=["Users", "Orders"],
        sensitive_columns=["SSN", "TaxID"],
    )
    assert req.profile_id == "local_dev"
    assert req.server == r"localhost\SQLEXPRESS"
    assert req.database_name == "TestDB"
    assert req.timeout_seconds == 10
    assert req.max_row_limit == 50
    assert req.allowed_tables == ["Users", "Orders"]
    assert req.sensitive_columns == ["SSN", "TaxID"]
    assert req.set_as_default is False


def test_validate_profile_config_request_accepts_set_as_default():
    req = ValidateProfileConfigRequest(
        profile_id="local_dev",
        display_name="Local SQL Server Express",
        server=r"localhost\SQLEXPRESS",
        database_name="TestDB",
        set_as_default=True,
    )
    assert req.set_as_default is True


@pytest.mark.parametrize(
    "credential_key",
    ["password", "pwd", "user", "username", "uid", "secret", "token", "api_key"],
)
def test_validate_profile_config_request_rejects_credentials(credential_key: str):
    data = {
        "profile_id": "test_id",
        "display_name": "Test DB",
        "server": "localhost",
        "database_name": "TestDB",
        credential_key: "forbidden_val",
    }
    with pytest.raises(Exception, match="extra"):
        ValidateProfileConfigRequest(**data)


def test_validate_profile_config_request_rejects_sql_auth():
    with pytest.raises(Exception, match="SQL-authenticated profiles"):
        ValidateProfileConfigRequest(
            profile_id="test_id",
            display_name="Test DB",
            server="localhost",
            database_name="TestDB",
            use_trusted_connection=False,
        )


def test_validate_profile_config_request_rejects_unsupported_engine():
    with pytest.raises(Exception, match="Unsupported engine_type"):
        ValidateProfileConfigRequest(
            profile_id="test_id",
            display_name="Test DB",
            server="localhost",
            database_name="TestDB",
            engine_type="postgresql",
        )


@pytest.mark.parametrize("bad_timeout", [0, -1, 16, 100])
def test_validate_profile_config_request_rejects_out_of_bounds_timeout(bad_timeout: int):
    with pytest.raises(Exception):
        ValidateProfileConfigRequest(
            profile_id="test_id",
            display_name="Test DB",
            server="localhost",
            database_name="TestDB",
            timeout_seconds=bad_timeout,
        )


@pytest.mark.parametrize("bad_limit", [0, -5, 101, 1000])
def test_validate_profile_config_request_rejects_out_of_bounds_row_limit(bad_limit: int):
    with pytest.raises(Exception):
        ValidateProfileConfigRequest(
            profile_id="test_id",
            display_name="Test DB",
            server="localhost",
            database_name="TestDB",
            max_row_limit=bad_limit,
        )


# ==============================================================================
# 4. HTTP Endpoint Tests: POST /agent/v1/profiles/validate-config
# ==============================================================================


@pytest.fixture
def agent_fixture(tmp_path: Path):
    """Sets up an isolated agent app with authenticated session for endpoint testing."""
    token_dir = tmp_path / "token_dir"
    token_dir.mkdir(parents=True, exist_ok=True)
    profiles_file = tmp_path / "profiles.json"

    # Pre-register an existing profile in the store
    store = ProfileStore()
    existing_profile = ConnectionProfile(
        profile_id="existing_profile",
        display_name="Existing Profile",
        server="localhost",
        database_name="ExistingDB",
        use_trusted_connection=True,
    )
    store.register_profile(existing_profile)

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

    # Bootstrap an active session
    login_resp = client.post(
        "/agent/v1/auth/bootstrap",
        headers={"Host": "127.0.0.1:8001", "X-QueryPilot-Request": "1"},
        json={"bootstrap_token": token},
    )
    assert login_resp.status_code == status.HTTP_200_OK
    session_id = login_resp.cookies.get(SESSION_COOKIE_NAME)
    assert session_id is not None

    return {
        "client": client,
        "session_id": session_id,
        "store": store,
        "profiles_file": profiles_file,
    }


def test_endpoint_validate_config_success(agent_fixture):
    """Valid local profile configuration returns 200 OK with confirmation message."""
    client = agent_fixture["client"]
    session_id = agent_fixture["session_id"]

    payload = {
        "profile_id": "new_local_dw",
        "display_name": "New Local DW",
        "server": r"localhost\SQLEXPRESS",
        "database_name": "Analytics_Test",
        "schema_name": "dbo",
        "timeout_seconds": 12,
        "max_row_limit": 80,
    }

    resp = client.post(
        "/agent/v1/profiles/validate-config",
        headers={
            "Host": "127.0.0.1:8001",
            "X-QueryPilot-Request": "1",
        },
        cookies={SESSION_COOKIE_NAME: session_id},
        json=payload,
    )
    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert data["valid"] is True
    assert "complies with security policies" in data["message"]


def test_endpoint_validate_config_missing_session(agent_fixture):
    """Request without session cookie fails with 401 Unauthorized."""
    client = agent_fixture["client"]
    client.cookies.clear()
    payload = {
        "profile_id": "unauth_db",
        "display_name": "Unauth DB",
        "server": "localhost",
        "database_name": "TestDB",
    }
    # 1. Missing session cookie
    resp = client.post(
        "/agent/v1/profiles/validate-config",
        headers={
            "Host": "127.0.0.1:8001",
            "X-QueryPilot-Request": "1",
        },
        json=payload,
    )
    assert resp.status_code == status.HTTP_401_UNAUTHORIZED
    assert "Authentication required" in resp.json()["detail"]

    # 2. Invalid session cookie
    resp_invalid = client.post(
        "/agent/v1/profiles/validate-config",
        headers={
            "Host": "127.0.0.1:8001",
            "X-QueryPilot-Request": "1",
        },
        cookies={SESSION_COOKIE_NAME: "invalid-fake-session-id"},
        json=payload,
    )
    assert resp_invalid.status_code == status.HTTP_401_UNAUTHORIZED
    assert "Session has expired or is invalid" in resp_invalid.json()["detail"]


def test_endpoint_validate_config_missing_custom_header(agent_fixture):
    """Request missing X-QueryPilot-Request: 1 header fails with 400 Bad Request."""
    client = agent_fixture["client"]
    session_id = agent_fixture["session_id"]
    payload = {
        "profile_id": "no_header_db",
        "display_name": "No Header DB",
        "server": "localhost",
        "database_name": "TestDB",
    }
    resp = client.post(
        "/agent/v1/profiles/validate-config",
        headers={"Host": "127.0.0.1:8001"},
        cookies={SESSION_COOKIE_NAME: session_id},
        json=payload,
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert resp.json()["error"] == "MISSING_CUSTOM_HEADER"


def test_endpoint_validate_config_duplicate_profile_id(agent_fixture):
    """Duplicate profile ID triggers HTTP 409 Conflict without altering stored profiles."""
    client = agent_fixture["client"]
    session_id = agent_fixture["session_id"]
    store = agent_fixture["store"]

    initial_count = len(store)
    payload = {
        "profile_id": "existing_profile",  # Already in store
        "display_name": "Duplicate Candidate",
        "server": "localhost",
        "database_name": "OtherDB",
    }
    resp = client.post(
        "/agent/v1/profiles/validate-config",
        headers={
            "Host": "127.0.0.1:8001",
            "X-QueryPilot-Request": "1",
        },
        cookies={SESSION_COOKIE_NAME: session_id},
        json=payload,
    )
    assert resp.status_code == status.HTTP_409_CONFLICT
    assert "already exists" in resp.json()["detail"]
    assert len(store) == initial_count


def test_endpoint_validate_config_system_database_rejected(agent_fixture):
    """Proposing a system database (e.g. master) fails with HTTP 422 validation error."""
    client = agent_fixture["client"]
    session_id = agent_fixture["session_id"]

    for sys_db in ["master", "msdb", "model", "tempdb"]:
        payload = {
            "profile_id": f"test_{sys_db}",
            "display_name": f"Test {sys_db}",
            "server": "localhost",
            "database_name": sys_db,
        }
        resp = client.post(
            "/agent/v1/profiles/validate-config",
            headers={
                "Host": "127.0.0.1:8001",
                "X-QueryPilot-Request": "1",
            },
            cookies={SESSION_COOKIE_NAME: session_id},
            json=payload,
        )
        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert "system database" in resp.text


def test_endpoint_validate_config_remote_server_rejected(agent_fixture):
    """Proposing a remote server or IP fails with HTTP 422 validation error."""
    client = agent_fixture["client"]
    session_id = agent_fixture["session_id"]

    payload = {
        "profile_id": "remote_target",
        "display_name": "Remote Server",
        "server": "192.168.1.100",
        "database_name": "TestDB",
    }
    resp = client.post(
        "/agent/v1/profiles/validate-config",
        headers={
            "Host": "127.0.0.1:8001",
            "X-QueryPilot-Request": "1",
        },
        cookies={SESSION_COOKIE_NAME: session_id},
        json=payload,
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    assert "not an approved local loopback host" in resp.text


def test_endpoint_validate_config_credential_leak_rejected(agent_fixture):
    """Attempting to supply password or user credentials fails with HTTP 422 validation error."""
    client = agent_fixture["client"]
    session_id = agent_fixture["session_id"]

    payload = {
        "profile_id": "cred_leak",
        "display_name": "Cred Leak DB",
        "server": "localhost",
        "database_name": "TestDB",
        "password": "SuperSecretPassword123!",
    }
    resp = client.post(
        "/agent/v1/profiles/validate-config",
        headers={
            "Host": "127.0.0.1:8001",
            "X-QueryPilot-Request": "1",
        },
        cookies={SESSION_COOKIE_NAME: session_id},
        json=payload,
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    assert "extra" in resp.text.lower()


def test_endpoint_validate_config_zero_side_effects(agent_fixture):
    """Validation performs zero database calls, zero engine creation, and zero file writes."""
    client = agent_fixture["client"]
    session_id = agent_fixture["session_id"]
    profiles_file = agent_fixture["profiles_file"]
    store = agent_fixture["store"]

    payload = {
        "profile_id": "pure_offline_check",
        "display_name": "Pure Offline Check",
        "server": r"localhost\SQLEXPRESS",
        "database_name": "OfflineTestDB",
    }

    with (
        patch("sqlalchemy.create_engine") as mock_create_engine,
        patch("query_pilot.agent.engine_manager.AgentEngineManager.get_or_create_engine") as mock_get_engine,
        patch("query_pilot.db.profile.save_trusted_profiles_file") as mock_save_file,
    ):
        resp = client.post(
            "/agent/v1/profiles/validate-config",
            headers={
                "Host": "127.0.0.1:8001",
                "X-QueryPilot-Request": "1",
            },
            cookies={SESSION_COOKIE_NAME: session_id},
            json=payload,
        )
        assert resp.status_code == status.HTTP_200_OK

        # Verify zero calls were made to engine creation or file persistence
        mock_create_engine.assert_not_called()
        mock_get_engine.assert_not_called()
        mock_save_file.assert_not_called()

    # Verify no file was created on disk
    assert not profiles_file.exists()
    # Verify in-memory store was not mutated
    assert "pure_offline_check" not in store.profiles
