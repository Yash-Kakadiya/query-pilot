"""Offline test suite for QueryPilot Local Agent Daemon (Task 1.34-B1-F28-A5).

Deterministic offline tests verifying:
1. Loopback-only bind host validation (non-loopback fails closed).
2. Single-use bootstrap token provisioning, constant-time verification, and replay rejection.
3. Cookie flags (HttpOnly, SameSite=Strict), idle timeout, absolute lifetime, and logout.
4. Host header validation (DNS rebinding defense) and Origin validation (packaged vs dev mode).
5. State-changing custom header (X-QueryPilot-Request: 1) requirement.
6. Hardened response security headers (CSP, Cache-Control, Referrer-Policy, nosniff, DENY).
7. Profile discovery returning safe summaries with zero credentials, hostnames, or connection strings.
8. Unknown profile fails closed with HTTP 404 without falling back to GradeSense_Local.
9. Dynamic model-facing schema introspection route enforcing allowlist and sensitive-column redaction.
10. Database identity mismatch failing closed with HTTP 403 and closing the connection.
11. Daemon-owned engine disposal on shutdown vs. caller-owned engine preservation.
12. 64 KB request body ceiling (HTTP 413) and concurrency ceiling (HTTP 429).
13. No credential, password, or token leakage into logs or error messages.
14. Existing hosted API remains completely unchanged and functional.
"""

from __future__ import annotations

import json
from pathlib import Path
import platform
import time
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

from fastapi import status
from fastapi.testclient import TestClient
import pytest
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from query_pilot.agent.app import create_agent_app
from query_pilot.agent.auth import (
    SESSION_COOKIE_NAME,
    BootstrapTokenManager,
    BootstrapTokenProvisioningError,
    Session,
    SessionManager,
)
from query_pilot.agent.config import AgentConfig, is_loopback_host
from query_pilot.agent.engine_manager import AgentEngineManager
from query_pilot.api.app import create_app as create_hosted_app
from query_pilot.db.connection import DatabaseSecurityError
from query_pilot.db.factory import ProfileConnectionFactory
from query_pilot.db.models import ColumnMetadata, DatabaseSchema, TableMetadata
from query_pilot.db.profile import ConnectionProfile, ProfileStore


# ==============================================================================
# Fixtures
# ==============================================================================


@pytest.fixture
def temp_token_dir(tmp_path: Path) -> Path:
    """Provides an isolated temporary directory for agent bootstrap token files."""
    token_dir = tmp_path / "agent_security"
    token_dir.mkdir(parents=True, exist_ok=True)
    return token_dir


@pytest.fixture
def fake_corp_profile() -> ConnectionProfile:
    """Fake corporate analytics profile with table allowlist and sensitive column."""
    return ConnectionProfile(
        profile_id="corp_analytics",
        display_name="Corporate Analytics DW",
        server="corp-dw.internal:1433",
        database_name="CorpAnalyticsDB",
        use_trusted_connection=True,
        timeout_seconds=10,
        max_row_limit=50,
        allowed_tables=["Customers", "Orders"],
        sensitive_columns=["CommissionRate"],
        schema_name="analytics",
    )


@pytest.fixture
def fake_finance_profile() -> ConnectionProfile:
    """Fake finance profile without table allowlist."""
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
def test_profile_store(fake_corp_profile, fake_finance_profile) -> ProfileStore:
    """ProfileStore pre-populated with two isolated test profiles."""
    return ProfileStore(
        profiles={
            "corp_analytics": fake_corp_profile,
            "finance_local": fake_finance_profile,
        }
    )


@pytest.fixture
def mock_db_connection() -> MagicMock:
    """Mock SQLAlchemy Connection with default SELECT DB_NAME() returning CorpAnalyticsDB."""
    conn = MagicMock(spec=Connection)
    raw_dbapi = MagicMock()
    raw_dbapi.timeout = 10
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
    insp.get_table_names.return_value = ["AuditLogs", "Customers", "Orders", "Users"]

    def get_pk(tbl, schema="dbo"):
        if tbl in {"Customers", "Orders", "Users"}:
            return {"constrained_columns": [f"{tbl[:-1]}ID"]}
        return {}

    def get_cols(tbl, schema="dbo"):
        if tbl == "Customers":
            return [
                {"name": "CustomerID", "type": "INTEGER", "nullable": False, "primary_key": True, "default": None, "identity": True, "comment": None},
                {"name": "CustomerName", "type": "VARCHAR(100)", "nullable": False, "primary_key": False, "default": None, "identity": False, "comment": None},
            ]
        if tbl == "Orders":
            return [
                {"name": "OrderID", "type": "INTEGER", "nullable": False, "primary_key": True, "default": None, "identity": True, "comment": None},
                {"name": "CommissionRate", "type": "DECIMAL(5,2)", "nullable": True, "primary_key": False, "default": None, "identity": False, "comment": None},
            ]
        return []

    def get_fks(tbl, schema="dbo"):
        return []

    def get_idxs(tbl, schema="dbo"):
        return []

    def get_uniques(tbl, schema="dbo"):
        return []

    insp.get_pk_constraint.side_effect = get_pk
    insp.get_columns.side_effect = get_cols
    insp.get_foreign_keys.side_effect = get_fks
    insp.get_indexes.side_effect = get_idxs
    insp.get_unique_constraints.side_effect = get_uniques
    return insp


@pytest.fixture
def fake_corp_schema() -> DatabaseSchema:
    """Provide a deterministic fake DatabaseSchema for corp_analytics profile."""
    return DatabaseSchema(
        database_name="CorpAnalyticsDB",
        tables=[
            TableMetadata(
                table_name="Customers",
                schema_name="analytics",
                columns=[
                    ColumnMetadata(name="CustomerID", data_type="int", primary_key=True),
                    ColumnMetadata(name="CustomerName", data_type="varchar(100)"),
                ],
            ),
            TableMetadata(
                table_name="Orders",
                schema_name="analytics",
                columns=[
                    ColumnMetadata(name="OrderID", data_type="int", primary_key=True),
                    ColumnMetadata(name="CustomerID", data_type="int"),
                    ColumnMetadata(name="CommissionRate", data_type="decimal(5,2)"),
                ],
            ),
        ],
        is_model_facing=True,
    )


@pytest.fixture
def agent_test_setup(temp_token_dir, test_profile_store, mock_db_engine):
    """Sets up an initialized agent app and TestClient with default valid loopback headers."""
    config = AgentConfig(
        bind_host="127.0.0.1",
        bind_port=8001,
        allow_dev_origin=False,
        token_dir=temp_token_dir,
        token_filename="agent.token",
        session_idle_seconds=3600,
        session_max_lifetime_seconds=43200,
        max_request_bytes=65536,
        max_concurrent_requests=2,
    )
    factory = ProfileConnectionFactory(store=test_profile_store)
    engine_manager = AgentEngineManager(factory=factory)
    token_manager = BootstrapTokenManager(token_dir=temp_token_dir, token_filename="agent.token")
    session_manager = SessionManager(idle_timeout_seconds=3600, max_lifetime_seconds=43200)

    # Initialize a fresh bootstrap token
    bootstrap_token = token_manager.initialize_token()

    app = create_agent_app(
        config=config,
        store=test_profile_store,
        factory=factory,
        engine_manager=engine_manager,
        token_manager=token_manager,
        session_manager=session_manager,
        caller_engine=mock_db_engine,
    )

    client = TestClient(
        app,
        base_url="http://127.0.0.1:8001",
        headers={"Host": "127.0.0.1:8001"},
    )

    return {
        "app": app,
        "client": client,
        "config": config,
        "token_manager": token_manager,
        "session_manager": session_manager,
        "engine_manager": engine_manager,
        "bootstrap_token": bootstrap_token,
        "mock_engine": mock_db_engine,
    }


# ==============================================================================
# 1. Loopback-Only Socket Bind Tests
# ==============================================================================


def test_non_loopback_bind_rejected():
    """AgentConfig strictly rejects non-loopback bind hosts, failing closed."""
    disallowed_hosts = [
        "0.0.0.0",
        "192.168.1.50",
        "10.0.0.1",
        "example.com",
        "evil.attacker.com",
        "172.16.0.1",
    ]
    for host in disallowed_hosts:
        with pytest.raises(ValueError, match="SECURITY VIOLATION: Agent daemon cannot bind to non-loopback"):
            AgentConfig(bind_host=host)


def test_loopback_bind_accepted():
    """AgentConfig permits standard loopback hosts."""
    allowed_hosts = ["127.0.0.1", "localhost", "::1", "[::1]"]
    for host in allowed_hosts:
        cfg = AgentConfig(bind_host=host)
        assert is_loopback_host(cfg.bind_host) is True


# ==============================================================================
# 2. Authentication Lifecycle & Bootstrap Tests
# ==============================================================================


def test_bootstrap_success_sets_httponly_samesite_cookie(agent_test_setup):
    """POST /agent/v1/auth/bootstrap with valid token establishes HttpOnly SameSite=Strict session cookie."""
    client = agent_test_setup["client"]
    token = agent_test_setup["bootstrap_token"]

    resp = client.post(
        "/agent/v1/auth/bootstrap",
        headers={"X-QueryPilot-Request": "1"},
        json={"bootstrap_token": token},
    )
    assert resp.status_code == status.HTTP_200_OK
    assert resp.json()["status"] == "authenticated"

    # Verify cookie was set with hardened flags
    cookie_header = resp.headers.get("set-cookie", "")
    assert f"{SESSION_COOKIE_NAME}=" in cookie_header
    assert "HttpOnly" in cookie_header
    assert "samesite=strict" in cookie_header.lower()
    assert "Path=/" in cookie_header


def test_bootstrap_token_single_use_replay_fails(agent_test_setup):
    """A bootstrap token is single-use and fails immediately if replayed."""
    client = agent_test_setup["client"]
    token = agent_test_setup["bootstrap_token"]

    # First attempt succeeds
    resp1 = client.post(
        "/agent/v1/auth/bootstrap",
        headers={"X-QueryPilot-Request": "1"},
        json={"bootstrap_token": token},
    )
    assert resp1.status_code == status.HTTP_200_OK

    # Replay attempt fails closed with HTTP 401
    resp2 = client.post(
        "/agent/v1/auth/bootstrap",
        headers={"X-QueryPilot-Request": "1"},
        json={"bootstrap_token": token},
    )
    assert resp2.status_code == status.HTTP_401_UNAUTHORIZED


def test_bootstrap_invalid_or_missing_token_rejected(agent_test_setup):
    """Submitting invalid, malformed, or missing bootstrap tokens fails with HTTP 401."""
    client = agent_test_setup["client"]

    # Wrong token
    resp = client.post(
        "/agent/v1/auth/bootstrap",
        headers={"X-QueryPilot-Request": "1"},
        json={"bootstrap_token": "a" * 32},
    )
    assert resp.status_code == status.HTTP_401_UNAUTHORIZED

    # Empty payload rejected
    resp_empty = client.post(
        "/agent/v1/auth/bootstrap",
        headers={"X-QueryPilot-Request": "1"},
        json={"bootstrap_token": ""},
    )
    assert resp_empty.status_code == 422


def test_bootstrap_uses_constant_time_comparison(agent_test_setup):
    """BootstrapTokenManager verifies tokens using secrets.compare_digest."""
    import secrets as secrets_mod
    token_manager = agent_test_setup["token_manager"]
    token = token_manager.initialize_token()

    with patch("secrets.compare_digest", wraps=secrets_mod.compare_digest) as mock_compare:
        result = token_manager.verify_and_consume(token)
        assert result is True
        mock_compare.assert_called_once()


def test_bootstrap_token_file_wiped_after_consumption(agent_test_setup):
    """Bootstrap token file is deleted from disk upon consumption."""
    token_manager = agent_test_setup["token_manager"]
    token_path = token_manager.token_path
    token = token_manager.initialize_token()

    assert token_path.exists()
    assert token_manager.verify_and_consume(token) is True
    assert not token_path.exists()


# ==============================================================================
# 2B. Windows ACL Fail-Closed & Symlink Hardening Tests
# ==============================================================================


def test_windows_acl_failure_fails_closed(temp_token_dir):
    """When icacls returns a non-zero exit code on Windows, provisioning fails closed."""
    import subprocess
    token_manager = BootstrapTokenManager(token_dir=temp_token_dir)

    mock_proc = subprocess.CompletedProcess(
        args=["icacls"],
        returncode=5,
        stdout="",
        stderr="Access is denied.",
    )

    with patch("platform.system", return_value="Windows"), patch(
        "subprocess.run", return_value=mock_proc
    ):
        with pytest.raises(BootstrapTokenProvisioningError, match="Windows ACL hardening via icacls failed"):
            token_manager.initialize_token()

    # In-memory token was cleared and partial file was unlinked
    assert token_manager.has_active_token is False
    assert not token_manager.token_path.exists()


def test_windows_acl_timeout_fails_closed(temp_token_dir):
    """When icacls times out on Windows, provisioning fails closed and cleans up."""
    import subprocess
    token_manager = BootstrapTokenManager(token_dir=temp_token_dir)

    with patch("platform.system", return_value="Windows"), patch(
        "subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=["icacls"], timeout=5)
    ):
        with pytest.raises(BootstrapTokenProvisioningError, match="icacls ACL hardening command timed out"):
            token_manager.initialize_token()

    assert token_manager.has_active_token is False
    assert not token_manager.token_path.exists()


def test_windows_acl_missing_icacls_fails_closed(temp_token_dir):
    """When icacls executable is missing on Windows, provisioning fails closed and cleans up."""
    token_manager = BootstrapTokenManager(token_dir=temp_token_dir)

    with patch("platform.system", return_value="Windows"), patch(
        "subprocess.run", side_effect=FileNotFoundError("icacls not found")
    ):
        with pytest.raises(BootstrapTokenProvisioningError, match="icacls utility not found on Windows"):
            token_manager.initialize_token()

    assert token_manager.has_active_token is False
    assert not token_manager.token_path.exists()


def test_token_provisioning_rejects_preexisting_symlink(temp_token_dir):
    """Token provisioning refuses to write to a path that is a symlink or reparse point."""
    token_manager = BootstrapTokenManager(token_dir=temp_token_dir)
    target_file = temp_token_dir / "unrelated_secret.txt"
    target_file.write_text("critical_user_data", encoding="utf-8")

    # Mock token_path.is_symlink to simulate pre-existing symlink/reparse point
    with patch.object(Path, "is_symlink", return_value=True):
        with pytest.raises(BootstrapTokenProvisioningError, match="SECURITY VIOLATION.*symlink or reparse point"):
            token_manager.initialize_token()

    # Target file was never overwritten or deleted
    assert target_file.exists()
    assert target_file.read_text(encoding="utf-8") == "critical_user_data"
    assert token_manager.has_active_token is False


def test_token_provisioning_replaces_cleanly_preexisting_regular_file(temp_token_dir):
    """Token provisioning cleanly unlinks and replaces an existing regular file."""
    token_manager = BootstrapTokenManager(token_dir=temp_token_dir)
    token_path = token_manager.token_path

    # Pre-existing regular file
    token_path.write_text("old_stale_token", encoding="utf-8")

    # Mock icacls success for Windows compatibility
    import subprocess
    mock_proc = subprocess.CompletedProcess(args=["icacls"], returncode=0, stdout="Successfully processed 1 files.", stderr="")
    with patch("subprocess.run", return_value=mock_proc):
        new_token = token_manager.initialize_token()

    assert token_manager.has_active_token is True
    assert token_path.read_text(encoding="utf-8") == new_token


def test_windows_trustee_resolution_whoami():
    """Trustee resolution prefers whoami output when available."""
    import subprocess
    from query_pilot.agent.auth import _get_active_windows_trustee

    mock_whoami = subprocess.CompletedProcess(
        args=["whoami"], returncode=0, stdout="SYNTHETIC_DOMAIN\\synthetic_user\n", stderr=""
    )
    with patch("subprocess.run", return_value=mock_whoami):
        trustee = _get_active_windows_trustee()
    assert trustee == "SYNTHETIC_DOMAIN\\synthetic_user"


def test_windows_trustee_resolution_local_machine_identity():
    """Trustee resolution handles local machine accounts like COMPUTER\\user."""
    import subprocess
    from query_pilot.agent.auth import _get_active_windows_trustee

    mock_whoami = subprocess.CompletedProcess(
        args=["whoami"], returncode=0, stdout="WORKSTATION\\localadmin\r\n", stderr=""
    )
    with patch("subprocess.run", return_value=mock_whoami):
        trustee = _get_active_windows_trustee()
    assert trustee == "WORKSTATION\\localadmin"


def test_windows_trustee_resolution_username_equals_computername_whoami():
    """When username equals computer name, whoami returns qualified domain\\user preventing empty trustee."""
    import subprocess
    from query_pilot.agent.auth import _get_active_windows_trustee

    mock_whoami = subprocess.CompletedProcess(
        args=["whoami"], returncode=0, stdout="TESTBOX\\TESTBOX\n", stderr=""
    )
    with patch("subprocess.run", return_value=mock_whoami):
        trustee = _get_active_windows_trustee()
    assert trustee == "TESTBOX\\TESTBOX"


def test_windows_trustee_resolution_fallback_userdomain():
    """When whoami fails, falls back to getpass.getuser() qualified with USERDOMAIN."""
    from query_pilot.agent.auth import _get_active_windows_trustee

    with patch("subprocess.run", side_effect=Exception("whoami unavailable")), \
         patch("getpass.getuser", return_value="fallback_user"), \
         patch.dict("os.environ", {"USERDOMAIN": "FALLBACK_DOMAIN"}):
        trustee = _get_active_windows_trustee()
    assert trustee == "FALLBACK_DOMAIN\\fallback_user"


def test_windows_trustee_resolution_fails_closed_when_empty():
    """When both whoami and getpass.getuser() fail to resolve, fails closed."""
    from query_pilot.agent.auth import _get_active_windows_trustee

    with patch("subprocess.run", side_effect=Exception("whoami failed")), \
         patch("getpass.getuser", return_value="   "):
        with pytest.raises(BootstrapTokenProvisioningError, match="Cannot determine active Windows username"):
            _get_active_windows_trustee()


def test_windows_acl_rejects_empty_user_domain_grant(temp_token_dir):
    """When icacls interprets an unqualified trustee as an empty-user domain grant, DACL verification fails closed."""
    import subprocess
    token_manager = BootstrapTokenManager(token_dir=temp_token_dir)

    def mock_subprocess_run(cmd, *args, **kwargs):
        if cmd == ["whoami"]:
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="TESTUSER\\TESTUSER\n", stderr="")
        elif cmd[0] == "icacls" and "/inheritance:r" in cmd:
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="Successfully processed 1 files.", stderr="")
        elif cmd[0] == "icacls" and len(cmd) == 2:
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=f"{cmd[1]} TESTUSER\\:(R,W)\nSuccessfully processed 1 files.", stderr="")
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    with patch("platform.system", return_value="Windows"), patch("subprocess.run", side_effect=mock_subprocess_run):
        with pytest.raises(BootstrapTokenProvisioningError, match="interpreted as domain prefix without user"):
            token_manager.initialize_token()

    assert token_manager.has_active_token is False
    assert not token_manager.token_path.exists()


def test_windows_acl_dacl_verification_failure_fails_closed(temp_token_dir):
    """When the active trustee is not present in the verified DACL, fails closed."""
    import subprocess
    token_manager = BootstrapTokenManager(token_dir=temp_token_dir)

    def mock_subprocess_run(cmd, *args, **kwargs):
        if cmd == ["whoami"]:
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="DOMAIN\\alice\n", stderr="")
        elif cmd[0] == "icacls" and "/inheritance:r" in cmd:
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="Successfully processed 1 files.", stderr="")
        elif cmd[0] == "icacls" and len(cmd) == 2:
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=f"{cmd[1]} NT AUTHORITY\\SYSTEM:(F)\n", stderr="")
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    with patch("platform.system", return_value="Windows"), patch("subprocess.run", side_effect=mock_subprocess_run):
        with pytest.raises(BootstrapTokenProvisioningError, match="trustee 'DOMAIN\\\\alice' not confirmed in ACL"):
            token_manager.initialize_token()

    assert token_manager.has_active_token is False
    assert not token_manager.token_path.exists()


@pytest.mark.skipif(platform.system() != "Windows", reason="Windows OS-level ACL test")
def test_windows_acl_live_roundtrip_accessible_to_current_user(temp_token_dir):
    """Live OS-level Windows test verifying that real initialize_token sets ACLs readable by current process."""
    token_manager = BootstrapTokenManager(token_dir=temp_token_dir)
    token = token_manager.initialize_token()

    token_path = token_manager.token_path
    assert token_path.exists()

    # Verify current user can open and read the file without PermissionError
    content = token_path.read_text(encoding="utf-8")
    assert content == token

    # Verify single-use consumption wipes file
    assert token_manager.verify_and_consume(token) is True
    assert not token_path.exists()


def test_session_idle_timeout_expires(agent_test_setup):
    """An inactive session expires after idle timeout (default 1 hour)."""
    client = agent_test_setup["client"]
    token = agent_test_setup["bootstrap_token"]

    # Authenticate
    login_resp = client.post(
        "/agent/v1/auth/bootstrap",
        headers={"X-QueryPilot-Request": "1"},
        json={"bootstrap_token": token},
    )
    session_id = login_resp.cookies.get(SESSION_COOKIE_NAME)

    # Fast forward time beyond idle timeout (3601 seconds)
    future_time = time.time() + 3601
    with patch("time.time", return_value=future_time):
        resp = client.get("/agent/v1/profiles", cookies={SESSION_COOKIE_NAME: session_id})
        assert resp.status_code == status.HTTP_401_UNAUTHORIZED


def test_session_absolute_lifetime_expires(agent_test_setup):
    """A session expires after absolute duration (12 hours) even with active requests."""
    client = agent_test_setup["client"]
    token = agent_test_setup["bootstrap_token"]

    login_resp = client.post(
        "/agent/v1/auth/bootstrap",
        headers={"X-QueryPilot-Request": "1"},
        json={"bootstrap_token": token},
    )
    session_id = login_resp.cookies.get(SESSION_COOKIE_NAME)

    # Fast forward beyond 12 hours (43201 seconds)
    future_time = time.time() + 43201
    with patch("time.time", return_value=future_time):
        resp = client.get("/agent/v1/profiles", cookies={SESSION_COOKIE_NAME: session_id})
        assert resp.status_code == status.HTTP_401_UNAUTHORIZED


def test_logout_destroys_session_and_clears_cookie(agent_test_setup):
    """POST /agent/v1/auth/logout destroys session in memory and deletes the cookie."""
    client = agent_test_setup["client"]
    token = agent_test_setup["bootstrap_token"]

    login_resp = client.post(
        "/agent/v1/auth/bootstrap",
        headers={"X-QueryPilot-Request": "1"},
        json={"bootstrap_token": token},
    )
    session_id = login_resp.cookies.get(SESSION_COOKIE_NAME)

    # Logout
    logout_resp = client.post(
        "/agent/v1/auth/logout",
        headers={"X-QueryPilot-Request": "1"},
        cookies={SESSION_COOKIE_NAME: session_id},
    )
    assert logout_resp.status_code == status.HTTP_200_OK
    assert logout_resp.json()["status"] == "logged_out"

    # Subsequent access fails
    resp = client.get("/agent/v1/profiles", cookies={SESSION_COOKIE_NAME: session_id})
    assert resp.status_code == status.HTTP_401_UNAUTHORIZED


def test_rotate_token_revokes_all_active_sessions(agent_test_setup):
    """POST /agent/v1/auth/rotate-token immediately invalidates all active sessions."""
    client = agent_test_setup["client"]
    token = agent_test_setup["bootstrap_token"]

    login_resp = client.post(
        "/agent/v1/auth/bootstrap",
        headers={"X-QueryPilot-Request": "1"},
        json={"bootstrap_token": token},
    )
    session_id = login_resp.cookies.get(SESSION_COOKIE_NAME)

    # Rotate token
    rotate_resp = client.post(
        "/agent/v1/auth/rotate-token",
        headers={"X-QueryPilot-Request": "1"},
    )
    assert rotate_resp.status_code == status.HTTP_200_OK
    assert rotate_resp.json()["status"] == "token_rotated"

    # Previously active session is invalid
    resp = client.get("/agent/v1/profiles", cookies={SESSION_COOKIE_NAME: session_id})
    assert resp.status_code == status.HTTP_401_UNAUTHORIZED


# ==============================================================================
# 4. Host and Origin Protection (DNS Rebinding Defense)
# ==============================================================================


def test_invalid_host_header_rejected_with_400(agent_test_setup):
    """DNS rebinding attempts with unapproved Host headers are rejected with HTTP 400."""
    client = agent_test_setup["client"]
    malicious_hosts = [
        "evil.attacker.com",
        "rebind.attacker.com:8001",
        "192.168.1.1:8001",
        "example.com",
    ]
    for host in malicious_hosts:
        resp = client.get(
            "/agent/v1/profiles",
            headers={"Host": host},
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert resp.json()["error"] == "INVALID_HOST"


def test_forbidden_origin_rejected_with_403(agent_test_setup):
    """External browser origins are rejected with HTTP 403."""
    client = agent_test_setup["client"]
    resp = client.get(
        "/agent/v1/profiles",
        headers={
            "Host": "127.0.0.1:8001",
            "Origin": "https://malicious-website.com",
        },
    )
    assert resp.status_code == status.HTTP_403_FORBIDDEN
    assert resp.json()["error"] == "FORBIDDEN_ORIGIN"


def test_packaged_mode_disallows_dev_origin(agent_test_setup):
    """In packaged mode (allow_dev_origin=False), Vite dev server origin is rejected."""
    client = agent_test_setup["client"]
    resp = client.get(
        "/agent/v1/profiles",
        headers={
            "Host": "127.0.0.1:8001",
            "Origin": "http://127.0.0.1:5173",
        },
    )
    assert resp.status_code == status.HTTP_403_FORBIDDEN


def test_dev_mode_allows_dev_origin(temp_token_dir, test_profile_store, mock_db_engine):
    """When allow_dev_origin=True, Vite dev server origin http://127.0.0.1:5173 is accepted."""
    config = AgentConfig(allow_dev_origin=True, token_dir=temp_token_dir)
    token_mgr = BootstrapTokenManager(token_dir=temp_token_dir)
    token = token_mgr.initialize_token()
    session_mgr = SessionManager()

    app = create_agent_app(
        config=config,
        store=test_profile_store,
        token_manager=token_mgr,
        session_manager=session_mgr,
        caller_engine=mock_db_engine,
    )
    client = TestClient(app, base_url="http://127.0.0.1:8001")

    # Login from dev origin
    login_resp = client.post(
        "/agent/v1/auth/bootstrap",
        headers={
            "Host": "127.0.0.1:8001",
            "Origin": "http://127.0.0.1:5173",
            "X-QueryPilot-Request": "1",
        },
        json={"bootstrap_token": token},
    )
    assert login_resp.status_code == status.HTTP_200_OK


def test_state_changing_request_missing_custom_header_rejected(agent_test_setup):
    """State-changing requests (POST) missing X-QueryPilot-Request: 1 are rejected."""
    client = agent_test_setup["client"]
    token = agent_test_setup["bootstrap_token"]

    resp = client.post(
        "/agent/v1/auth/bootstrap",
        headers={"Host": "127.0.0.1:8001"},  # missing custom header
        json={"bootstrap_token": token},
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert resp.json()["error"] == "MISSING_CUSTOM_HEADER"


# ==============================================================================
# 5. Hardened Security Headers
# ==============================================================================


def test_security_headers_present_on_all_responses(agent_test_setup):
    """All agent responses include mandatory hardened security headers."""
    client = agent_test_setup["client"]
    resp = client.get("/agent/v1/profiles")

    # Sensitive route caching protection
    assert "no-store" in resp.headers.get("cache-control", "")
    assert "no-cache" in resp.headers.get("pragma", "")
    assert resp.headers.get("referrer-policy") == "no-referrer"

    # CSP and framing
    assert "default-src 'self'" in resp.headers.get("content-security-policy", "")
    assert resp.headers.get("x-frame-options") == "DENY"
    assert resp.headers.get("x-content-type-options") == "nosniff"


# ==============================================================================
# 6. Profile Discovery & Credential Redaction
# ==============================================================================


def test_list_profiles_authenticated_returns_redacted_summaries(agent_test_setup):
    """GET /agent/v1/profiles returns safe summaries with ZERO credentials or hostnames."""
    client = agent_test_setup["client"]
    token = agent_test_setup["bootstrap_token"]

    login_resp = client.post(
        "/agent/v1/auth/bootstrap",
        headers={"X-QueryPilot-Request": "1"},
        json={"bootstrap_token": token},
    )
    session_id = login_resp.cookies.get(SESSION_COOKIE_NAME)

    resp = client.get(
        "/agent/v1/profiles",
        cookies={SESSION_COOKIE_NAME: session_id},
    )
    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert "profiles" in data
    assert len(data["profiles"]) == 2

    # Verify attributes for corp_analytics
    corp = next(p for p in data["profiles"] if p["profile_id"] == "corp_analytics")
    assert corp["display_name"] == "Corporate Analytics DW"
    assert corp["schema_name"] == "analytics"
    assert corp["has_allowlist"] is True
    assert corp["allowed_tables"] == ["Customers", "Orders"]

    # Security Check: Ensure sensitive attributes are NEVER exposed
    resp_text = resp.text.lower()
    assert "corp-dw.internal" not in resp_text
    assert "password" not in resp_text
    assert "user" not in resp_text
    assert "connection_string" not in resp_text


def test_list_profiles_unauthenticated_fails_with_401(agent_test_setup):
    """GET /agent/v1/profiles without session cookie fails closed with HTTP 401."""
    client = agent_test_setup["client"]
    resp = client.get("/agent/v1/profiles")
    assert resp.status_code == status.HTTP_401_UNAUTHORIZED


# ==============================================================================
# 7. Dynamic Schema Introspection & Database Identity Verification
# ==============================================================================


@patch("query_pilot.db.profile_introspector.inspect")
def test_schema_route_authenticated_returns_model_facing_schema(
    mock_inspect_fn, agent_test_setup, mock_inspector
):
    """GET /agent/v1/profiles/{id}/schema returns model-facing schema with allowlist and redaction."""
    mock_inspect_fn.return_value = mock_inspector

    client = agent_test_setup["client"]
    token = agent_test_setup["bootstrap_token"]

    login_resp = client.post(
        "/agent/v1/auth/bootstrap",
        headers={"X-QueryPilot-Request": "1"},
        json={"bootstrap_token": token},
    )
    session_id = login_resp.cookies.get(SESSION_COOKIE_NAME)

    resp = client.get(
        "/agent/v1/profiles/corp_analytics/schema",
        cookies={SESSION_COOKIE_NAME: session_id},
    )
    assert resp.status_code == status.HTTP_200_OK
    schema_data = resp.json()

    assert schema_data["database_name"] == "CorpAnalyticsDB"
    assert schema_data["is_model_facing"] is True

    # Check allowlist filtered: only Customers and Orders
    tbl_names = [t["table_name"] for t in schema_data["tables"]]
    assert tbl_names == ["Customers", "Orders"]

    # Check sensitive column 'CommissionRate' was redacted from Orders
    orders_tbl = next(t for t in schema_data["tables"] if t["table_name"] == "Orders")
    col_names = [c["name"] for c in orders_tbl["columns"]]
    assert "CommissionRate" not in col_names


def test_unknown_profile_fails_closed_with_404(agent_test_setup):
    """Requesting an unregistered profile ID returns HTTP 404 (never falls back)."""
    client = agent_test_setup["client"]
    token = agent_test_setup["bootstrap_token"]

    login_resp = client.post(
        "/agent/v1/auth/bootstrap",
        headers={"X-QueryPilot-Request": "1"},
        json={"bootstrap_token": token},
    )
    session_id = login_resp.cookies.get(SESSION_COOKIE_NAME)

    resp = client.get(
        "/agent/v1/profiles/unknown_profile/schema",
        cookies={SESSION_COOKIE_NAME: session_id},
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND
    assert "not found" in resp.json()["detail"].lower()


def test_database_identity_mismatch_returns_403(agent_test_setup, mock_db_connection):
    """When SQL Server reports an unauthorized DB, schema route returns HTTP 403."""
    mock_db_connection.execute.return_value.scalar.return_value = "unauthorized_db"

    client = agent_test_setup["client"]
    token = agent_test_setup["bootstrap_token"]

    login_resp = client.post(
        "/agent/v1/auth/bootstrap",
        headers={"X-QueryPilot-Request": "1"},
        json={"bootstrap_token": token},
    )
    session_id = login_resp.cookies.get(SESSION_COOKIE_NAME)

    resp = client.get(
        "/agent/v1/profiles/corp_analytics/schema",
        cookies={SESSION_COOKIE_NAME: session_id},
    )
    assert resp.status_code == status.HTTP_403_FORBIDDEN
    assert "unauthorized database" in resp.json()["detail"].lower()


# ==============================================================================
# 8. Resource Ceilings & Concurrency Limits
# ==============================================================================


def test_oversized_payload_rejected_with_413(agent_test_setup):
    """Requests exceeding 64 KB are rejected with HTTP 413 Payload Too Large."""
    client = agent_test_setup["client"]
    oversized_body = {"bootstrap_token": "x" * 70000}

    resp = client.post(
        "/agent/v1/auth/bootstrap",
        headers={"X-QueryPilot-Request": "1"},
        json=oversized_body,
    )
    assert resp.status_code == status.HTTP_413_CONTENT_TOO_LARGE


def test_concurrency_guard_saturates_and_rejects_excess_requests_with_429(agent_test_setup):
    """Saturate concurrency limit (2 active requests) and assert excess request receives HTTP 429."""
    import concurrent.futures
    import threading

    client = agent_test_setup["client"]
    token = agent_test_setup["bootstrap_token"]

    login_resp = client.post(
        "/agent/v1/auth/bootstrap",
        headers={"X-QueryPilot-Request": "1"},
        json={"bootstrap_token": token},
    )
    session_id = login_resp.cookies.get(SESSION_COOKIE_NAME)

    from query_pilot.db.models import DatabaseSchema
    dummy_schema = DatabaseSchema(database_name="CorpAnalyticsDB", tables=[], is_model_facing=True)

    gate = threading.Event()
    req1_active = threading.Event()
    req2_active = threading.Event()

    call_count = 0
    lock = threading.Lock()

    def slow_introspect(self, *args, **kwargs):
        nonlocal call_count
        with lock:
            call_count += 1
            current = call_count
        if current == 1:
            req1_active.set()
        elif current == 2:
            req2_active.set()
        gate.wait(timeout=5)
        return dummy_schema

    with patch(
        "query_pilot.agent.app.ProfileDatabaseIntrospector.introspect_model_facing",
        slow_introspect,
    ):
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            f1 = executor.submit(
                client.get,
                "/agent/v1/profiles/corp_analytics/schema",
                cookies={SESSION_COOKIE_NAME: session_id},
            )
            assert req1_active.wait(timeout=3) is True

            f2 = executor.submit(
                client.get,
                "/agent/v1/profiles/corp_analytics/schema",
                cookies={SESSION_COOKIE_NAME: session_id},
            )
            assert req2_active.wait(timeout=3) is True

            # Both concurrency slots (2 out of 2) are occupied. Request 3 is rejected immediately with 429!
            resp3 = client.get(
                "/agent/v1/profiles/corp_analytics/schema",
                cookies={SESSION_COOKIE_NAME: session_id},
            )
            assert resp3.status_code == status.HTTP_429_TOO_MANY_REQUESTS
            assert resp3.json()["error"] == "CONCURRENCY_LIMIT_EXCEEDED"

            # Release gate so requests 1 and 2 finish cleanly
            gate.set()

            assert f1.result().status_code == status.HTTP_200_OK
            assert f2.result().status_code == status.HTTP_200_OK

    # Both slots were released; subsequent request succeeds with 200
    resp4 = client.get("/agent/v1/profiles", cookies={SESSION_COOKIE_NAME: session_id})
    assert resp4.status_code == status.HTTP_200_OK


def test_concurrency_guard_releases_permits_on_downstream_exception(agent_test_setup):
    """Permits are released cleanly when a downstream exception occurs."""
    client = agent_test_setup["client"]
    token = agent_test_setup["bootstrap_token"]

    login_resp = client.post(
        "/agent/v1/auth/bootstrap",
        headers={"X-QueryPilot-Request": "1"},
        json={"bootstrap_token": token},
    )
    session_id = login_resp.cookies.get(SESSION_COOKIE_NAME)

    with patch(
        "query_pilot.agent.app.ProfileDatabaseIntrospector.introspect_model_facing",
        side_effect=RuntimeError("Simulated server crash"),
    ):
        resp = client.get(
            "/agent/v1/profiles/corp_analytics/schema",
            cookies={SESSION_COOKIE_NAME: session_id},
        )
        assert resp.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR

    # Verify concurrency permit was released in finally block
    resp_ok = client.get("/agent/v1/profiles", cookies={SESSION_COOKIE_NAME: session_id})
    assert resp_ok.status_code == status.HTTP_200_OK


def test_concurrency_guard_releases_permits_on_cancellation():
    """Permits are released cleanly if an ASGI request is cancelled."""
    import asyncio
    from query_pilot.agent.app import ConcurrencyGuardMiddleware

    async def cancelling_app(scope, receive, send):
        raise asyncio.CancelledError("Client disconnected")

    guard = ConcurrencyGuardMiddleware(app=cancelling_app, max_concurrent=2)
    assert guard._current == 0

    scope = {"type": "http", "method": "GET"}
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(guard(scope, None, None))

    # Concurrency counter decremented in finally block
    assert guard._current == 0


# ==============================================================================
# 9. Engine Lifecycle & Deterministic Disposal
# ==============================================================================


def test_daemon_owned_engines_disposed_on_shutdown():
    """AgentEngineManager disposes daemon-owned engines on shutdown."""
    mock_factory = MagicMock(spec=ProfileConnectionFactory)
    mock_engine = MagicMock(spec=Engine)
    mock_factory.create_engine.return_value = mock_engine

    manager = AgentEngineManager(factory=mock_factory)
    engine = manager.get_or_create_engine("corp_analytics")
    assert engine is mock_engine
    assert manager.is_engine_owned("corp_analytics") is True

    # Dispose all
    disposed = manager.dispose_all()
    assert disposed == 1
    mock_engine.dispose.assert_called_once()


def test_caller_owned_engines_not_disposed():
    """Caller-owned engines (e.g. test mocks) are not disposed by AgentEngineManager."""
    mock_factory = MagicMock(spec=ProfileConnectionFactory)
    caller_engine = MagicMock(spec=Engine)

    manager = AgentEngineManager(factory=mock_factory)
    engine = manager.get_or_create_engine("corp_analytics", caller_engine=caller_engine)
    assert engine is caller_engine
    assert manager.is_engine_owned("corp_analytics") is False

    disposed = manager.dispose_all()
    assert disposed == 0
    caller_engine.dispose.assert_not_called()


# ==============================================================================
# 10. Regression Check on Existing Hosted API
# ==============================================================================


def test_existing_hosted_api_unchanged():
    """The existing hosted API continues to initialize and operate independently."""
    hosted_app = create_hosted_app()
    client = TestClient(hosted_app)

    # Health probe succeeds
    health_resp = client.get("/health")
    assert health_resp.status_code == status.HTTP_200_OK
    assert health_resp.json()["status"] == "healthy"


# ==============================================================================
# 11. Profile-Aware Query Execution Endpoint (Task 1.34-B1-F28-A6)
# ==============================================================================


def test_execute_query_authenticated_success_path(agent_test_setup, mock_db_connection, fake_corp_schema):
    """POST /agent/v1/profiles/{id}/execute succeeds with valid session, header, and read-only SQL."""
    app = agent_test_setup["app"]
    client = agent_test_setup["client"]
    token = agent_test_setup["bootstrap_token"]
    app.state.caller_schema = fake_corp_schema

    # Setup mock cursor
    mock_cursor = MagicMock()
    mock_cursor.keys.return_value = ["CustomerID", "CustomerName"]
    mock_cursor.fetchmany.return_value = [(1, "Acme Corp"), (2, "Beta LLC")]
    mock_db_connection.execution_options.return_value.execute.return_value = mock_cursor

    # Authenticate to get session cookie
    login_resp = client.post(
        "/agent/v1/auth/bootstrap",
        headers={"X-QueryPilot-Request": "1"},
        json={"bootstrap_token": token},
    )
    session_id = login_resp.cookies.get(SESSION_COOKIE_NAME)

    resp = client.post(
        "/agent/v1/profiles/corp_analytics/execute",
        headers={"X-QueryPilot-Request": "1"},
        cookies={SESSION_COOKIE_NAME: session_id},
        json={
            "sql": "SELECT CustomerID, CustomerName FROM Customers;",
            "request_id": "req-offline-001",
        },
    )
    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert data["columns"] == ["CustomerID", "CustomerName"]
    assert data["rows"] == [
        {"CustomerID": 1, "CustomerName": "Acme Corp"},
        {"CustomerID": 2, "CustomerName": "Beta LLC"},
    ]
    assert data["row_count"] == 2
    assert data["truncated"] is False
    assert data["request_id"] == "req-offline-001"
    assert data["execution_time_ms"] >= 0

    # Effective profile timeout is 10s (corp_analytics timeout)
    raw_dbapi = mock_db_connection.connection.dbapi_connection
    assert raw_dbapi.timeout == 10
    mock_cursor.close.assert_called_once()
    mock_db_connection.close.assert_called_once()


def test_execute_query_unauthenticated_and_missing_custom_header(agent_test_setup):
    """Execution endpoint rejects unauthenticated requests and requests missing custom header."""
    client = agent_test_setup["client"]
    token = agent_test_setup["bootstrap_token"]

    # 1. Missing cookie -> 401
    resp_no_cookie = client.post(
        "/agent/v1/profiles/corp_analytics/execute",
        headers={"X-QueryPilot-Request": "1"},
        json={"sql": "SELECT 1;"},
    )
    assert resp_no_cookie.status_code == status.HTTP_401_UNAUTHORIZED

    # 2. Invalid cookie -> 401
    resp_bad_cookie = client.post(
        "/agent/v1/profiles/corp_analytics/execute",
        headers={"X-QueryPilot-Request": "1"},
        cookies={SESSION_COOKIE_NAME: "invalid-cookie-token"},
        json={"sql": "SELECT 1;"},
    )
    assert resp_bad_cookie.status_code == status.HTTP_401_UNAUTHORIZED

    # 3. Missing X-QueryPilot-Request header -> 400
    login_resp = client.post(
        "/agent/v1/auth/bootstrap",
        headers={"X-QueryPilot-Request": "1"},
        json={"bootstrap_token": token},
    )
    session_id = login_resp.cookies.get(SESSION_COOKIE_NAME)

    resp_no_header = client.post(
        "/agent/v1/profiles/corp_analytics/execute",
        cookies={SESSION_COOKIE_NAME: session_id},
        json={"sql": "SELECT 1;"},
    )
    assert resp_no_header.status_code == status.HTTP_400_BAD_REQUEST
    assert resp_no_header.json()["error"] == "MISSING_CUSTOM_HEADER"


def test_execute_query_unknown_profile_fails_closed_404(agent_test_setup):
    """Executing against an unknown profile returns 404 and never falls back."""
    client = agent_test_setup["client"]
    token = agent_test_setup["bootstrap_token"]

    login_resp = client.post(
        "/agent/v1/auth/bootstrap",
        headers={"X-QueryPilot-Request": "1"},
        json={"bootstrap_token": token},
    )
    session_id = login_resp.cookies.get(SESSION_COOKIE_NAME)

    resp = client.post(
        "/agent/v1/profiles/non_existent_profile/execute",
        headers={"X-QueryPilot-Request": "1"},
        cookies={SESSION_COOKIE_NAME: session_id},
        json={"sql": "SELECT 1;"},
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND
    assert "not found" in resp.json()["detail"].lower()


def test_execute_query_client_overrides_forbidden_422(agent_test_setup):
    """Client attempts to override connection targets, timeout, or row limits are rejected with HTTP 422."""
    client = agent_test_setup["client"]
    token = agent_test_setup["bootstrap_token"]

    login_resp = client.post(
        "/agent/v1/auth/bootstrap",
        headers={"X-QueryPilot-Request": "1"},
        json={"bootstrap_token": token},
    )
    session_id = login_resp.cookies.get(SESSION_COOKIE_NAME)

    # Attempt to inject connection target parameters
    forbidden_payloads = [
        {"sql": "SELECT 1;", "database_name": "InjectedDB"},
        {"sql": "SELECT 1;", "server": "attacker.com"},
        {"sql": "SELECT 1;", "timeout_seconds": 999},
        {"sql": "SELECT 1;", "max_row_limit": 5000},
        {"sql": "SELECT 1;", "driver": "ODBC Driver 18 for SQL Server"},
        {"sql": "SELECT 1;", "allowed_tables": ["*"]},
    ]

    for payload in forbidden_payloads:
        resp = client.post(
            "/agent/v1/profiles/corp_analytics/execute",
            headers={"X-QueryPilot-Request": "1"},
            cookies={SESSION_COOKIE_NAME: session_id},
            json=payload,
        )
        assert resp.status_code == 422, f"Failed for {payload}"


def test_execute_query_mutating_and_multi_statement_sql_rejected(agent_test_setup, fake_corp_schema, mock_db_connection):
    """Mutating SQL, multi-statement SQL, and empty queries are rejected before database execution."""
    app = agent_test_setup["app"]
    client = agent_test_setup["client"]
    token = agent_test_setup["bootstrap_token"]
    app.state.caller_schema = fake_corp_schema

    login_resp = client.post(
        "/agent/v1/auth/bootstrap",
        headers={"X-QueryPilot-Request": "1"},
        json={"bootstrap_token": token},
    )
    session_id = login_resp.cookies.get(SESSION_COOKIE_NAME)

    unsafe_queries = [
        "DELETE FROM Customers WHERE CustomerID = 1;",
        "INSERT INTO Customers (CustomerID, CustomerName) VALUES (99, 'Evil');",
        "UPDATE Customers SET CustomerName = 'Hacked' WHERE CustomerID = 1;",
        "DROP TABLE Customers;",
        "SELECT 1; SELECT 2;",
        "SELECT * FROM Customers; DROP TABLE Orders;",
    ]

    for sql in unsafe_queries:
        resp = client.post(
            "/agent/v1/profiles/corp_analytics/execute",
            headers={"X-QueryPilot-Request": "1"},
            cookies={SESSION_COOKIE_NAME: session_id},
            json={"sql": sql},
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert "validation rejected" in resp.json()["detail"].lower()

    # Empty / whitespace query rejected by Pydantic validator with 422
    empty_resp = client.post(
        "/agent/v1/profiles/corp_analytics/execute",
        headers={"X-QueryPilot-Request": "1"},
        cookies={SESSION_COOKIE_NAME: session_id},
        json={"sql": "   "},
    )
    assert empty_resp.status_code == 422


def test_execute_query_sensitive_columns_and_disallowed_tables_rejected(
    agent_test_setup, fake_corp_schema, mock_db_connection
):
    """Queries referencing sensitive columns or tables outside the profile allowlist are rejected."""
    app = agent_test_setup["app"]
    client = agent_test_setup["client"]
    token = agent_test_setup["bootstrap_token"]
    app.state.caller_schema = fake_corp_schema

    login_resp = client.post(
        "/agent/v1/auth/bootstrap",
        headers={"X-QueryPilot-Request": "1"},
        json={"bootstrap_token": token},
    )
    session_id = login_resp.cookies.get(SESSION_COOKIE_NAME)

    # 1. Sensitive column: CommissionRate is configured as sensitive on corp_analytics
    resp_sens = client.post(
        "/agent/v1/profiles/corp_analytics/execute",
        headers={"X-QueryPilot-Request": "1"},
        cookies={SESSION_COOKIE_NAME: session_id},
        json={"sql": "SELECT CommissionRate FROM Orders;"},
    )
    assert resp_sens.status_code == status.HTTP_400_BAD_REQUEST
    assert "validation rejected" in resp_sens.json()["detail"].lower()

    # 2. Table outside allowlist: allowed_tables is ["Customers", "Orders"]
    resp_unallowed = client.post(
        "/agent/v1/profiles/corp_analytics/execute",
        headers={"X-QueryPilot-Request": "1"},
        cookies={SESSION_COOKIE_NAME: session_id},
        json={"sql": "SELECT * FROM AuditLogs;"},
    )
    assert resp_unallowed.status_code == status.HTTP_400_BAD_REQUEST


def test_execute_query_database_identity_mismatch_fails_closed_403(
    agent_test_setup, fake_corp_schema, mock_db_connection
):
    """When connected database does not match expected_db, execution fails closed with HTTP 403."""
    app = agent_test_setup["app"]
    client = agent_test_setup["client"]
    token = agent_test_setup["bootstrap_token"]
    app.state.caller_schema = fake_corp_schema

    # Simulate database identity mismatch on SELECT DB_NAME()
    mock_db_connection.execute.return_value.scalar.return_value = "UnauthorizedDatabase"

    login_resp = client.post(
        "/agent/v1/auth/bootstrap",
        headers={"X-QueryPilot-Request": "1"},
        json={"bootstrap_token": token},
    )
    session_id = login_resp.cookies.get(SESSION_COOKIE_NAME)

    resp = client.post(
        "/agent/v1/profiles/corp_analytics/execute",
        headers={"X-QueryPilot-Request": "1"},
        cookies={SESSION_COOKIE_NAME: session_id},
        json={"sql": "SELECT CustomerID FROM Customers;"},
    )
    assert resp.status_code == status.HTTP_403_FORBIDDEN
    assert "unauthorized database" in resp.json()["detail"].lower()

    # User SQL was never executed, and connection was closed
    mock_db_connection.execution_options.return_value.execute.assert_not_called()
    mock_db_connection.close.assert_called_once()


def test_execute_query_driver_timeout_enforced_and_times_out_504(
    agent_test_setup, fake_corp_schema, mock_db_connection
):
    """Query timeout at the driver/statement boundary returns HTTP 504 Gateway Timeout."""
    app = agent_test_setup["app"]
    client = agent_test_setup["client"]
    token = agent_test_setup["bootstrap_token"]
    app.state.caller_schema = fake_corp_schema

    # Simulate driver operational timeout
    timeout_exc = OperationalError(
        statement="SELECT CustomerID FROM Customers;",
        params={},
        orig=Exception("HYT00: [Microsoft][ODBC Driver 17 for SQL Server]Query timeout expired"),
    )
    mock_db_connection.execution_options.return_value.execute.side_effect = timeout_exc

    login_resp = client.post(
        "/agent/v1/auth/bootstrap",
        headers={"X-QueryPilot-Request": "1"},
        json={"bootstrap_token": token},
    )
    session_id = login_resp.cookies.get(SESSION_COOKIE_NAME)

    resp = client.post(
        "/agent/v1/profiles/corp_analytics/execute",
        headers={"X-QueryPilot-Request": "1"},
        cookies={SESSION_COOKIE_NAME: session_id},
        json={"sql": "SELECT CustomerID FROM Customers;"},
    )
    assert resp.status_code == status.HTTP_504_GATEWAY_TIMEOUT
    assert "timed out" in resp.json()["detail"].lower()

    # Connection was closed in finally block and no partial results returned
    mock_db_connection.close.assert_called_once()


def test_execute_query_fails_closed_when_hard_timeout_unavailable(
    agent_test_setup, fake_corp_schema, mock_db_connection
):
    """When driver-level query timeout cannot be set, execution fails closed with HTTP 500."""
    app = agent_test_setup["app"]
    client = agent_test_setup["client"]
    token = agent_test_setup["bootstrap_token"]
    app.state.caller_schema = fake_corp_schema

    # Remove timeout attribute from raw DBAPI connection
    raw_dbapi = MagicMock(spec=[])
    mock_db_connection.connection.dbapi_connection = raw_dbapi

    login_resp = client.post(
        "/agent/v1/auth/bootstrap",
        headers={"X-QueryPilot-Request": "1"},
        json={"bootstrap_token": token},
    )
    session_id = login_resp.cookies.get(SESSION_COOKIE_NAME)

    resp = client.post(
        "/agent/v1/profiles/corp_analytics/execute",
        headers={"X-QueryPilot-Request": "1"},
        cookies={SESSION_COOKIE_NAME: session_id},
        json={"sql": "SELECT CustomerID FROM Customers;"},
    )
    assert resp.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    assert "timeout enforcement is unavailable" in resp.json()["detail"].lower()

    # Connection was closed and user SQL was not executed
    mock_db_connection.close.assert_called_once()
    mock_db_connection.execution_options.return_value.execute.assert_not_called()


def test_execute_query_bounded_fetch_and_truncation_detection(
    agent_test_setup, fake_corp_schema, mock_db_connection
):
    """Results exceeding the profile limit (50 rows) are truncated without buffering unbounded rows."""
    app = agent_test_setup["app"]
    client = agent_test_setup["client"]
    token = agent_test_setup["bootstrap_token"]
    app.state.caller_schema = fake_corp_schema

    # Mock cursor returning 51 rows (1 over max limit of 50)
    mock_cursor = MagicMock()
    mock_cursor.keys.return_value = ["CustomerID", "CustomerName"]
    mock_cursor.fetchmany.return_value = [(i, f"Customer {i}") for i in range(51)]
    mock_db_connection.execution_options.return_value.execute.return_value = mock_cursor

    login_resp = client.post(
        "/agent/v1/auth/bootstrap",
        headers={"X-QueryPilot-Request": "1"},
        json={"bootstrap_token": token},
    )
    session_id = login_resp.cookies.get(SESSION_COOKIE_NAME)

    resp = client.post(
        "/agent/v1/profiles/corp_analytics/execute",
        headers={"X-QueryPilot-Request": "1"},
        cookies={SESSION_COOKIE_NAME: session_id},
        json={"sql": "SELECT CustomerID, CustomerName FROM Customers;"},
    )
    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert data["truncated"] is True
    assert data["row_count"] == 50
    assert len(data["rows"]) == 50

    # fetchmany was called with effective_rows + 1 (51)
    mock_cursor.fetchmany.assert_called_once_with(51)
    mock_cursor.close.assert_called_once()
    mock_db_connection.close.assert_called_once()


def test_execute_query_sanitizes_errors_no_secrets_leaked(
    agent_test_setup, fake_corp_schema, mock_db_connection
):
    """Exceptions containing passwords, connection strings, or hostnames are completely sanitized."""
    app = agent_test_setup["app"]
    client = agent_test_setup["client"]
    token = agent_test_setup["bootstrap_token"]
    app.state.caller_schema = fake_corp_schema

    # Simulate database driver error containing sensitive connection string
    db_err = SQLAlchemyError("Error connecting to 'corp-dw.internal:1433' with user 'sa' and pwd='SecretPassword123'")
    mock_db_connection.execution_options.return_value.execute.side_effect = db_err

    login_resp = client.post(
        "/agent/v1/auth/bootstrap",
        headers={"X-QueryPilot-Request": "1"},
        json={"bootstrap_token": token},
    )
    session_id = login_resp.cookies.get(SESSION_COOKIE_NAME)

    resp = client.post(
        "/agent/v1/profiles/corp_analytics/execute",
        headers={"X-QueryPilot-Request": "1"},
        cookies={SESSION_COOKIE_NAME: session_id},
        json={"sql": "SELECT CustomerID FROM Customers;"},
    )
    assert resp.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    resp_text = resp.text
    assert "SecretPassword123" not in resp_text
    assert "corp-dw.internal" not in resp_text
    assert "sa" not in resp_text.split()


def test_execute_query_suppresses_exception_cause_chain(
    agent_test_setup, fake_corp_schema, mock_db_connection
):
    """Verify that route handlers suppress exception chaining and preserve sanitized status mappings."""
    app = agent_test_setup["app"]
    client = agent_test_setup["client"]
    token = agent_test_setup["bootstrap_token"]
    app.state.caller_schema = fake_corp_schema

    # Simulate unexpected driver operational error
    raw_exc = OperationalError(
        statement="SELECT 1",
        params={},
        orig=Exception("Driver crash with raw secret: pwd=SuperSecret999;server=corp-dw.internal"),
    )
    mock_db_connection.execution_options.return_value.execute.side_effect = raw_exc

    login_resp = client.post(
        "/agent/v1/auth/bootstrap",
        headers={"X-QueryPilot-Request": "1"},
        json={"bootstrap_token": token},
    )
    session_id = login_resp.cookies.get(SESSION_COOKIE_NAME)

    resp = client.post(
        "/agent/v1/profiles/corp_analytics/execute",
        headers={"X-QueryPilot-Request": "1"},
        cookies={SESSION_COOKIE_NAME: session_id},
        json={"sql": "SELECT CustomerID FROM Customers;"},
    )
    assert resp.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    data = resp.json()
    assert "SuperSecret999" not in data["detail"]
    assert "corp-dw.internal" not in data["detail"]
    assert "[REDACTED]" in data["detail"]


def test_execute_query_cross_profile_schema_mismatch_rejected(agent_test_setup, fake_corp_schema):
    """Passing a schema belonging to Profile A when executing on Profile B is rejected with HTTP 400."""
    app = agent_test_setup["app"]
    client = agent_test_setup["client"]
    token = agent_test_setup["bootstrap_token"]

    # Provide fake_corp_schema (CorpAnalyticsDB) to app.state.caller_schema
    app.state.caller_schema = fake_corp_schema

    login_resp = client.post(
        "/agent/v1/auth/bootstrap",
        headers={"X-QueryPilot-Request": "1"},
        json={"bootstrap_token": token},
    )
    session_id = login_resp.cookies.get(SESSION_COOKIE_NAME)

    # Attempt to execute on finance_local (expected DB Finance_Local) with CorpAnalyticsDB schema
    resp = client.post(
        "/agent/v1/profiles/finance_local/execute",
        headers={"X-QueryPilot-Request": "1"},
        cookies={SESSION_COOKIE_NAME: session_id},
        json={"sql": "SELECT 1;"},
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "does not match profile expected database" in resp.json()["detail"].lower()


@patch("query_pilot.db.profile_introspector.inspect")
def test_execute_query_dynamic_introspection_when_caller_schema_omitted(
    mock_inspect_fn, agent_test_setup, mock_db_connection, mock_inspector
):
    """When caller_schema is None, endpoint dynamically introspects model-facing schema and executes."""
    mock_inspect_fn.return_value = mock_inspector

    app = agent_test_setup["app"]
    client = agent_test_setup["client"]
    token = agent_test_setup["bootstrap_token"]
    app.state.caller_schema = None  # Ensure dynamic introspection is triggered

    mock_cursor = MagicMock()
    mock_cursor.keys.return_value = ["CustomerID"]
    mock_cursor.fetchmany.return_value = [(42,)]
    mock_db_connection.execution_options.return_value.execute.return_value = mock_cursor

    login_resp = client.post(
        "/agent/v1/auth/bootstrap",
        headers={"X-QueryPilot-Request": "1"},
        json={"bootstrap_token": token},
    )
    session_id = login_resp.cookies.get(SESSION_COOKIE_NAME)

    resp = client.post(
        "/agent/v1/profiles/corp_analytics/execute",
        headers={"X-QueryPilot-Request": "1"},
        cookies={SESSION_COOKIE_NAME: session_id},
        json={"sql": "SELECT CustomerID FROM Customers;"},
    )
    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert data["columns"] == ["CustomerID"]
    assert data["rows"] == [{"CustomerID": 42}]
    assert data["row_count"] == 1


def test_execute_query_concurrency_guard_saturates_and_releases(agent_test_setup, fake_corp_schema, mock_db_connection):
    """Concurrent query executions saturate the concurrency ceiling (2) and reject excess with HTTP 429."""
    import concurrent.futures
    import threading

    app = agent_test_setup["app"]
    client = agent_test_setup["client"]
    token = agent_test_setup["bootstrap_token"]
    app.state.caller_schema = fake_corp_schema

    login_resp = client.post(
        "/agent/v1/auth/bootstrap",
        headers={"X-QueryPilot-Request": "1"},
        json={"bootstrap_token": token},
    )
    session_id = login_resp.cookies.get(SESSION_COOKIE_NAME)

    gate = threading.Event()
    req1_active = threading.Event()
    req2_active = threading.Event()
    call_count = 0
    lock = threading.Lock()

    def slow_execute(*args, **kwargs):
        nonlocal call_count
        with lock:
            call_count += 1
            current = call_count
        if current == 1:
            req1_active.set()
        elif current == 2:
            req2_active.set()
        gate.wait(timeout=5)
        mock_cursor = MagicMock()
        mock_cursor.keys.return_value = ["CustomerID"]
        mock_cursor.fetchmany.return_value = [(1,)]
        return mock_cursor

    mock_db_connection.execution_options.return_value.execute.side_effect = slow_execute

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        f1 = executor.submit(
            client.post,
            "/agent/v1/profiles/corp_analytics/execute",
            headers={"X-QueryPilot-Request": "1"},
            cookies={SESSION_COOKIE_NAME: session_id},
            json={"sql": "SELECT CustomerID FROM Customers;"},
        )
        assert req1_active.wait(timeout=3) is True

        f2 = executor.submit(
            client.post,
            "/agent/v1/profiles/corp_analytics/execute",
            headers={"X-QueryPilot-Request": "1"},
            cookies={SESSION_COOKIE_NAME: session_id},
            json={"sql": "SELECT CustomerID FROM Customers;"},
        )
        assert req2_active.wait(timeout=3) is True

        # Slots are saturated (2/2); 3rd request is rejected with HTTP 429
        resp3 = client.post(
            "/agent/v1/profiles/corp_analytics/execute",
            headers={"X-QueryPilot-Request": "1"},
            cookies={SESSION_COOKIE_NAME: session_id},
            json={"sql": "SELECT CustomerID FROM Customers;"},
        )
        assert resp3.status_code == status.HTTP_429_TOO_MANY_REQUESTS
        assert resp3.json()["error"] == "CONCURRENCY_LIMIT_EXCEEDED"

        # Release gate so active requests complete
        gate.set()

        assert f1.result().status_code == status.HTTP_200_OK
        assert f2.result().status_code == status.HTTP_200_OK

    # Permits released; next request succeeds
    mock_cursor = MagicMock()
    mock_cursor.keys.return_value = ["CustomerID"]
    mock_cursor.fetchmany.return_value = [(1,)]
    mock_db_connection.execution_options.return_value.execute.side_effect = None
    mock_db_connection.execution_options.return_value.execute.return_value = mock_cursor

    resp_after = client.post(
        "/agent/v1/profiles/corp_analytics/execute",
        headers={"X-QueryPilot-Request": "1"},
        cookies={SESSION_COOKIE_NAME: session_id},
        json={"sql": "SELECT CustomerID FROM Customers;"},
    )
    assert resp_after.status_code == status.HTTP_200_OK
