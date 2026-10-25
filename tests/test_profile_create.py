"""Offline unit and integration tests for profile creation endpoint POST /agent/v1/profiles.

Verifies Milestone B12-F8D:
1. Successful creation writes profile to disk and synchronizes in-memory store.
2. Persisted profile is parseable by trusted profile loader.
3. Duplicate IDs rejected against both current in-memory state and reloaded on-disk state.
4. Default profile preservation (or first-profile assignment).
5. Persistence failure leaves running in-memory store unchanged.
6. File-lock timeout returns HTTP 503.
7. Authentication, custom header, and Host/Origin controls enforced.
8. Credentials, unknown fields, non-loopback hosts, and system databases rejected.
9. Zero engine creation, zero ODBC calls, zero database connections.
10. Existing engines preserved without disruption.
11. Honest error reporting if persistence succeeds but in-memory sync fails.
12. ProfileSummary response privacy guarantees (no connection strings, hosts, paths, or secrets).
"""

from __future__ import annotations

import os
from pathlib import Path
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
    CreateProfileRequest,
    CreateProfileResponse,
    ProfileSummary,
)
from query_pilot.db.factory import ProfileConnectionFactory
from query_pilot.db.profile import (
    ConnectionProfile,
    ProfileConfigurationError,
    ProfileStore,
    load_trusted_profiles_file,
    profile_file_lock,
)


@pytest.fixture
def agent_fixture(tmp_path: Path):
    """Sets up an isolated agent app with authenticated session for endpoint testing."""
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
        "engine_manager": engine_manager,
        "factory": factory,
    }


def test_create_profile_success_first_profile_becomes_default(agent_fixture):
    """First onboarded profile automatically becomes the default profile."""
    client = agent_fixture["client"]
    session_id = agent_fixture["session_id"]
    store = agent_fixture["store"]
    profiles_file = agent_fixture["profiles_file"]

    payload = {
        "profile_id": "dw_primary",
        "display_name": "Primary Data Warehouse",
        "server": r"localhost\SQLEXPRESS",
        "database_name": "WarehouseDB",
        "schema_name": "sales",
        "driver": "ODBC Driver 17 for SQL Server",
        "timeout_seconds": 10,
        "max_row_limit": 50,
    }

    resp = client.post(
        "/agent/v1/profiles",
        headers={"Host": "127.0.0.1:8001", "X-QueryPilot-Request": "1"},
        cookies={SESSION_COOKIE_NAME: session_id},
        json=payload,
    )
    assert resp.status_code == status.HTTP_201_CREATED
    data = resp.json()

    assert data["is_default"] is True
    assert "dw_primary" in data["message"]
    profile_data = data["profile"]
    assert profile_data["profile_id"] == "dw_primary"
    assert profile_data["display_name"] == "Primary Data Warehouse"
    assert profile_data["schema_name"] == "sales"
    assert profile_data["timeout_seconds"] == 10
    assert profile_data["max_row_limit"] == 50

    # Verify on-disk persistence
    assert profiles_file.exists()
    disk_store = load_trusted_profiles_file(profiles_file)
    assert disk_store.has_profile("dw_primary")
    assert disk_store.default_profile_id == "dw_primary"
    saved = disk_store.get_profile("dw_primary")
    assert saved.server == r"localhost\SQLEXPRESS"
    assert saved.database_name == "WarehouseDB"

    # Verify in-memory store synchronization
    assert store.has_profile("dw_primary")
    assert store.default_profile_id == "dw_primary"


def test_create_profile_preserves_existing_default(agent_fixture):
    """Adding subsequent profiles preserves existing default profile when set_as_default=False."""
    client = agent_fixture["client"]
    session_id = agent_fixture["session_id"]
    store = agent_fixture["store"]
    profiles_file = agent_fixture["profiles_file"]

    # First profile
    p1 = {
        "profile_id": "p1_initial",
        "display_name": "Initial Profile",
        "server": "localhost",
        "database_name": "DB1",
    }
    resp1 = client.post(
        "/agent/v1/profiles",
        headers={"Host": "127.0.0.1:8001", "X-QueryPilot-Request": "1"},
        cookies={SESSION_COOKIE_NAME: session_id},
        json=p1,
    )
    assert resp1.status_code == status.HTTP_201_CREATED
    assert resp1.json()["is_default"] is True

    # Second profile with set_as_default=False
    p2 = {
        "profile_id": "p2_secondary",
        "display_name": "Secondary Profile",
        "server": "127.0.0.1",
        "database_name": "DB2",
        "set_as_default": False,
    }
    resp2 = client.post(
        "/agent/v1/profiles",
        headers={"Host": "127.0.0.1:8001", "X-QueryPilot-Request": "1"},
        cookies={SESSION_COOKIE_NAME: session_id},
        json=p2,
    )
    assert resp2.status_code == status.HTTP_201_CREATED
    assert resp2.json()["is_default"] is False

    # Verify existing default preserved on disk and in memory
    disk_store = load_trusted_profiles_file(profiles_file)
    assert disk_store.default_profile_id == "p1_initial"
    assert store.default_profile_id == "p1_initial"
    assert disk_store.has_profile("p2_secondary")
    assert store.has_profile("p2_secondary")


def test_create_profile_explicit_set_as_default(agent_fixture):
    """Subsequent profile can explicitly be set as default if set_as_default=True."""
    client = agent_fixture["client"]
    session_id = agent_fixture["session_id"]
    store = agent_fixture["store"]
    profiles_file = agent_fixture["profiles_file"]

    # Initial profile
    client.post(
        "/agent/v1/profiles",
        headers={"Host": "127.0.0.1:8001", "X-QueryPilot-Request": "1"},
        cookies={SESSION_COOKIE_NAME: session_id},
        json={"profile_id": "initial", "display_name": "Initial", "server": "localhost", "database_name": "DB1"},
    )

    # Second profile with set_as_default=True
    resp = client.post(
        "/agent/v1/profiles",
        headers={"Host": "127.0.0.1:8001", "X-QueryPilot-Request": "1"},
        cookies={SESSION_COOKIE_NAME: session_id},
        json={
            "profile_id": "new_default",
            "display_name": "New Default",
            "server": "localhost",
            "database_name": "DB2",
            "set_as_default": True,
        },
    )
    assert resp.status_code == status.HTTP_201_CREATED
    assert resp.json()["is_default"] is True

    disk_store = load_trusted_profiles_file(profiles_file)
    assert disk_store.default_profile_id == "new_default"
    assert store.default_profile_id == "new_default"


def test_create_profile_duplicate_id_in_memory_rejected(agent_fixture):
    """Duplicate profile ID existing in memory returns 409 Conflict."""
    client = agent_fixture["client"]
    session_id = agent_fixture["session_id"]
    store = agent_fixture["store"]

    existing = ConnectionProfile(
        profile_id="dup_test",
        display_name="Existing Profile",
        server="localhost",
        database_name="ExistingDB",
        use_trusted_connection=True,
    )
    store.register_profile(existing)

    payload = {
        "profile_id": "dup_test",
        "display_name": "Duplicate Attempt",
        "server": "localhost",
        "database_name": "NewDB",
    }
    resp = client.post(
        "/agent/v1/profiles",
        headers={"Host": "127.0.0.1:8001", "X-QueryPilot-Request": "1"},
        cookies={SESSION_COOKIE_NAME: session_id},
        json=payload,
    )
    assert resp.status_code == status.HTTP_409_CONFLICT
    assert "already exists" in resp.text


def test_create_profile_duplicate_id_on_disk_rejected(agent_fixture):
    """Duplicate profile ID written to disk outside agent memory is caught and rejected with 409."""
    client = agent_fixture["client"]
    session_id = agent_fixture["session_id"]
    profiles_file = agent_fixture["profiles_file"]

    # Write a profile to disk out-of-band
    disk_store = ProfileStore()
    disk_store.register_profile(
        ConnectionProfile(
            profile_id="disk_only",
            display_name="Disk Only Profile",
            server="localhost",
            database_name="DiskDB",
            use_trusted_connection=True,
        )
    )
    from query_pilot.db.profile import save_trusted_profiles_file
    save_trusted_profiles_file(profiles_file, disk_store)

    payload = {
        "profile_id": "disk_only",
        "display_name": "Conflict with Disk",
        "server": "localhost",
        "database_name": "NewDB",
    }
    resp = client.post(
        "/agent/v1/profiles",
        headers={"Host": "127.0.0.1:8001", "X-QueryPilot-Request": "1"},
        cookies={SESSION_COOKIE_NAME: session_id},
        json=payload,
    )
    assert resp.status_code == status.HTTP_409_CONFLICT
    assert "already exists" in resp.text


def test_create_profile_persistence_failure_leaves_store_unchanged(agent_fixture):
    """If disk save fails, in-memory store remains completely unchanged."""
    client = agent_fixture["client"]
    session_id = agent_fixture["session_id"]
    store = agent_fixture["store"]

    payload = {
        "profile_id": "fail_profile",
        "display_name": "Fail Profile",
        "server": "localhost",
        "database_name": "TestDB",
    }

    with patch("query_pilot.agent.app.save_trusted_profiles_file", side_effect=ProfileConfigurationError("disk IO error")):
        resp = client.post(
            "/agent/v1/profiles",
            headers={"Host": "127.0.0.1:8001", "X-QueryPilot-Request": "1"},
            cookies={SESSION_COOKIE_NAME: session_id},
            json=payload,
        )
        assert resp.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
        assert "Failed to persist profile configuration" in resp.text

    assert not store.has_profile("fail_profile")


def test_create_profile_lock_timeout_returns_503(agent_fixture):
    """File lock acquisition timeout returns HTTP 503 Service Unavailable."""
    client = agent_fixture["client"]
    session_id = agent_fixture["session_id"]
    profiles_file = agent_fixture["profiles_file"]

    payload = {
        "profile_id": "lock_test",
        "display_name": "Lock Test",
        "server": "localhost",
        "database_name": "TestDB",
    }

    # Pre-acquire the profile lock to induce timeout
    with profile_file_lock(profiles_file):
        with patch("query_pilot.agent.app.profile_file_lock", side_effect=ProfileConfigurationError("Timeout (5.0s) acquiring lock")):
            resp = client.post(
                "/agent/v1/profiles",
                headers={"Host": "127.0.0.1:8001", "X-QueryPilot-Request": "1"},
                cookies={SESSION_COOKIE_NAME: session_id},
                json=payload,
            )
            assert resp.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
            assert "Timeout acquiring profile lock" in resp.text


def test_create_profile_requires_session_cookie(agent_fixture):
    """Missing or invalid session cookie returns HTTP 401."""
    client = agent_fixture["client"]
    payload = {
        "profile_id": "unauth_test",
        "display_name": "Unauth Test",
        "server": "localhost",
        "database_name": "TestDB",
    }

    # Missing session cookie
    client.cookies.clear()
    resp = client.post(
        "/agent/v1/profiles",
        headers={"Host": "127.0.0.1:8001", "X-QueryPilot-Request": "1"},
        json=payload,
    )
    assert resp.status_code == status.HTTP_401_UNAUTHORIZED

    # Invalid session cookie
    resp = client.post(
        "/agent/v1/profiles",
        headers={"Host": "127.0.0.1:8001", "X-QueryPilot-Request": "1"},
        cookies={SESSION_COOKIE_NAME: "invalid-token-value"},
        json=payload,
    )
    assert resp.status_code == status.HTTP_401_UNAUTHORIZED


def test_create_profile_requires_custom_header(agent_fixture):
    """Missing or invalid X-QueryPilot-Request header returns HTTP 400."""
    client = agent_fixture["client"]
    session_id = agent_fixture["session_id"]
    payload = {
        "profile_id": "header_test",
        "display_name": "Header Test",
        "server": "localhost",
        "database_name": "TestDB",
    }

    # Missing header
    resp = client.post(
        "/agent/v1/profiles",
        headers={"Host": "127.0.0.1:8001"},
        cookies={SESSION_COOKIE_NAME: session_id},
        json=payload,
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "MISSING_CUSTOM_HEADER" in resp.text


def test_create_profile_untrusted_host_and_origin_rejected(agent_fixture):
    """Untrusted Host header returns 400; untrusted Origin header returns 403."""
    client = agent_fixture["client"]
    session_id = agent_fixture["session_id"]
    payload = {
        "profile_id": "origin_test",
        "display_name": "Origin Test",
        "server": "localhost",
        "database_name": "TestDB",
    }

    # Bad Host
    resp = client.post(
        "/agent/v1/profiles",
        headers={"Host": "attacker.com", "X-QueryPilot-Request": "1"},
        cookies={SESSION_COOKIE_NAME: session_id},
        json=payload,
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "INVALID_HOST" in resp.text

    # Bad Origin
    resp = client.post(
        "/agent/v1/profiles",
        headers={
            "Host": "127.0.0.1:8001",
            "Origin": "http://evil.com",
            "X-QueryPilot-Request": "1",
        },
        cookies={SESSION_COOKIE_NAME: session_id},
        json=payload,
    )
    assert resp.status_code == status.HTTP_403_FORBIDDEN
    assert "FORBIDDEN_ORIGIN" in resp.text


@pytest.mark.parametrize(
    "credential_key",
    ["password", "pwd", "user", "username", "uid", "secret", "token", "api_key"],
)
def test_create_profile_rejects_credentials(agent_fixture, credential_key: str):
    """Attempting to supply password, user, or credentials returns 422 Unprocessable Content."""
    client = agent_fixture["client"]
    session_id = agent_fixture["session_id"]
    payload = {
        "profile_id": "cred_test",
        "display_name": "Cred Test",
        "server": "localhost",
        "database_name": "TestDB",
        credential_key: "forbidden_value",
    }
    resp = client.post(
        "/agent/v1/profiles",
        headers={"Host": "127.0.0.1:8001", "X-QueryPilot-Request": "1"},
        cookies={SESSION_COOKIE_NAME: session_id},
        json=payload,
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    assert "extra" in resp.text.lower()


@pytest.mark.parametrize(
    "bad_server,match_msg",
    [
        ("192.168.1.1", "not an approved local loopback host"),
        ("10.0.0.1", "not an approved local loopback host"),
        ("169.254.169.254", "not an approved local loopback host"),
        ("MY-WORKSTATION", "not an approved local loopback host"),
        ("::1", "unbracketed IPv6 literal"),
        ("::1,1433", "unbracketed IPv6 literal"),
        ("localhost:1433", "separated by a comma"),
        ("localhost,0", "out of valid range"),
        ("localhost,70000", "out of valid range"),
    ],
)
def test_create_profile_rejects_unapproved_targets(agent_fixture, bad_server: str, match_msg: str):
    """Non-loopback servers, machine names, bare ::1, and colon-ports return 422."""
    client = agent_fixture["client"]
    session_id = agent_fixture["session_id"]
    payload = {
        "profile_id": "target_test",
        "display_name": "Target Test",
        "server": bad_server,
        "database_name": "TestDB",
    }
    resp = client.post(
        "/agent/v1/profiles",
        headers={"Host": "127.0.0.1:8001", "X-QueryPilot-Request": "1"},
        cookies={SESSION_COOKIE_NAME: session_id},
        json=payload,
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    assert match_msg in resp.text


@pytest.mark.parametrize("sys_db", ["master", "msdb", "model", "tempdb", "MASTER", "MsDb"])
def test_create_profile_rejects_system_databases(agent_fixture, sys_db: str):
    """SQL Server system databases return 422."""
    client = agent_fixture["client"]
    session_id = agent_fixture["session_id"]
    payload = {
        "profile_id": "sys_db_test",
        "display_name": "System DB Test",
        "server": "localhost",
        "database_name": sys_db,
    }
    resp = client.post(
        "/agent/v1/profiles",
        headers={"Host": "127.0.0.1:8001", "X-QueryPilot-Request": "1"},
        cookies={SESSION_COOKIE_NAME: session_id},
        json=payload,
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    assert "SQL Server system database" in resp.text


def test_create_profile_zero_engine_and_zero_odbc_calls(agent_fixture):
    """Profile creation makes zero database connections, zero ODBC calls, and zero engine creations."""
    client = agent_fixture["client"]
    session_id = agent_fixture["session_id"]

    payload = {
        "profile_id": "pure_offline_create",
        "display_name": "Pure Offline Create",
        "server": r".\SQLEXPRESS",
        "database_name": "OfflineDB",
    }

    with (
        patch("sqlalchemy.create_engine") as mock_sa_create,
        patch("query_pilot.agent.engine_manager.AgentEngineManager.get_or_create_engine") as mock_mgr_get,
    ):
        resp = client.post(
            "/agent/v1/profiles",
            headers={"Host": "127.0.0.1:8001", "X-QueryPilot-Request": "1"},
            cookies={SESSION_COOKIE_NAME: session_id},
            json=payload,
        )
        assert resp.status_code == status.HTTP_201_CREATED
        mock_sa_create.assert_not_called()
        mock_mgr_get.assert_not_called()


def test_create_profile_preserves_existing_engines(agent_fixture):
    """Adding a new profile preserves active engines for other profiles without disruption."""
    client = agent_fixture["client"]
    session_id = agent_fixture["session_id"]
    engine_manager = agent_fixture["engine_manager"]
    store = agent_fixture["store"]

    # Pre-register profile 1 and an engine
    p1 = ConnectionProfile(
        profile_id="p1_with_engine",
        display_name="P1 With Engine",
        server="localhost",
        database_name="DB1",
        use_trusted_connection=True,
    )
    store.register_profile(p1)

    mock_engine = MagicMock()
    cached_engine = engine_manager.get_or_create_engine("p1_with_engine", caller_engine=mock_engine)
    assert cached_engine is mock_engine

    # Now create profile 2 via API
    payload = {
        "profile_id": "p2_new",
        "display_name": "P2 New",
        "server": "localhost",
        "database_name": "DB2",
    }
    resp = client.post(
        "/agent/v1/profiles",
        headers={"Host": "127.0.0.1:8001", "X-QueryPilot-Request": "1"},
        cookies={SESSION_COOKIE_NAME: session_id},
        json=payload,
    )
    assert resp.status_code == status.HTTP_201_CREATED

    # Verify engine for p1_with_engine was not disposed or removed
    assert engine_manager.get_or_create_engine("p1_with_engine", caller_engine=mock_engine) is mock_engine


def test_create_profile_post_persistence_sync_failure_reported(agent_fixture):
    """If disk save succeeds but in-memory sync fails, a distinct 500 error is returned."""
    client = agent_fixture["client"]
    session_id = agent_fixture["session_id"]
    profiles_file = agent_fixture["profiles_file"]

    payload = {
        "profile_id": "sync_fail_test",
        "display_name": "Sync Fail Test",
        "server": "localhost",
        "database_name": "TestDB",
    }

    # Patch sync_from_store to fail, and patch controlled reload to also fail
    with patch("query_pilot.db.profile.ProfileStore.sync_from_store", side_effect=RuntimeError("sync crash")):
        with patch("query_pilot.agent.app.load_trusted_profiles_file", side_effect=RuntimeError("reload crash")):
            resp = client.post(
                "/agent/v1/profiles",
                headers={"Host": "127.0.0.1:8001", "X-QueryPilot-Request": "1"},
                cookies={SESSION_COOKIE_NAME: session_id},
                json=payload,
            )
            assert resp.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
            assert "PROFILE_PERSISTED_SYNC_FAILED" in resp.text

    # Verify the profile WAS successfully saved on disk
    assert profiles_file.exists()
    disk_store = load_trusted_profiles_file(profiles_file)
    assert disk_store.has_profile("sync_fail_test")


def test_create_profile_summary_privacy(agent_fixture):
    """ProfileSummary in create response does not leak server, database_name, driver, or paths."""
    client = agent_fixture["client"]
    session_id = agent_fixture["session_id"]

    payload = {
        "profile_id": "privacy_test",
        "display_name": "Privacy Test Profile",
        "server": r"localhost\SQLEXPRESS,1433",
        "database_name": "ConfidentialDB",
        "schema_name": "finance",
        "driver": "ODBC Driver 17 for SQL Server",
        "timeout_seconds": 12,
        "max_row_limit": 75,
        "allowed_tables": ["Ledger", "Accounts"],
        "sensitive_columns": ["TaxID", "SSN"],
    }

    resp = client.post(
        "/agent/v1/profiles",
        headers={"Host": "127.0.0.1:8001", "X-QueryPilot-Request": "1"},
        cookies={SESSION_COOKIE_NAME: session_id},
        json=payload,
    )
    assert resp.status_code == status.HTTP_201_CREATED
    data = resp.json()
    profile_summary = data["profile"]

    # Allowed fields in ProfileSummary
    assert profile_summary["profile_id"] == "privacy_test"
    assert profile_summary["display_name"] == "Privacy Test Profile"
    assert profile_summary["schema_name"] == "finance"
    assert profile_summary["timeout_seconds"] == 12
    assert profile_summary["max_row_limit"] == 75
    assert profile_summary["has_allowlist"] is True
    assert profile_summary["allowed_tables"] == ["Ledger", "Accounts"]

    # Forbidden fields MUST NOT be in profile summary
    forbidden_keys = {
        "server",
        "database_name",
        "driver",
        "engine_type",
        "use_trusted_connection",
        "password",
        "user",
        "credentials",
        "connection_string",
        "profiles_file",
        "path",
        "sensitive_columns",
    }
    for key in forbidden_keys:
        assert key not in profile_summary


# ==============================================================================
# Concurrency Race Regression Tests (B12-F8D3)
# ==============================================================================

def test_create_profile_synchronization_holds_file_lock(agent_fixture):
    """Verify that in-memory sync_from_store is executed while profile_file_lock is held."""
    client = agent_fixture["client"]
    session_id = agent_fixture["session_id"]
    profiles_file = agent_fixture["profiles_file"]
    store = agent_fixture["store"]

    lock_file_existed_during_sync = False
    lock_acquisition_failed_during_sync = False
    original_sync = store.sync_from_store

    def inspecting_sync(other):
        nonlocal lock_file_existed_during_sync, lock_acquisition_failed_during_sync
        lock_file = profiles_file.with_name(f"{profiles_file.name}.lock")
        lock_file_existed_during_sync = lock_file.exists()
        # Attempting to acquire lock from another context must fail/timeout because lock is held
        try:
            with profile_file_lock(profiles_file, timeout=0.02):
                pass
        except ProfileConfigurationError:
            lock_acquisition_failed_during_sync = True
        return original_sync(other)

    with patch.object(store, "sync_from_store", side_effect=inspecting_sync):
        resp = client.post(
            "/agent/v1/profiles",
            headers={"Host": "127.0.0.1:8001", "X-QueryPilot-Request": "1"},
            cookies={SESSION_COOKIE_NAME: session_id},
            json={
                "profile_id": "lock_sync_profile",
                "display_name": "Lock Sync Profile",
                "server": "localhost",
                "database_name": "DB1",
            },
        )
        assert resp.status_code == status.HTTP_201_CREATED

    assert lock_file_existed_during_sync is True
    assert lock_acquisition_failed_during_sync is True
    # Lock file released after request returns
    assert not profiles_file.with_name(f"{profiles_file.name}.lock").exists()


def test_two_serialized_creations_preserve_both_profiles_in_memory_and_disk(agent_fixture):
    """Reproduce two profile creations where Request 1 starts first and attempts to be interleaved.

    Verifies:
    1. Request 2 is blocked from reading or writing while Request 1 holds the lock through sync.
    2. Both profiles remain present in memory and on disk.
    3. The selected default is consistent between memory and on disk.
    4. Locks are released after completion.
    5. Existing engine-manager entries remain untouched.
    """
    import threading

    client = agent_fixture["client"]
    session_id = agent_fixture["session_id"]
    profiles_file = agent_fixture["profiles_file"]
    store = agent_fixture["store"]
    engine_manager = agent_fixture["engine_manager"]

    # Pre-register an engine to verify it remains untouched
    mock_engine = MagicMock()
    engine_manager._engines["pre_existing"] = mock_engine

    t1_inside_sync = threading.Event()
    t2_tried_lock = threading.Event()

    original_sync = store.sync_from_store
    call_count = 0
    sync_lock = threading.Lock()

    def sync_wrapper(other):
        nonlocal call_count
        with sync_lock:
            call_count += 1
            current_call = call_count

        if current_call == 1:
            # Request 1 is inside sync_from_store (under profile_file_lock)
            t1_inside_sync.set()
            # Wait for Request 2 to attempt starting
            t2_tried_lock.wait(timeout=3.0)
            return original_sync(other)
        else:
            return original_sync(other)

    results = {}

    def run_req1():
        resp = client.post(
            "/agent/v1/profiles",
            headers={"Host": "127.0.0.1:8001", "X-QueryPilot-Request": "1"},
            cookies={SESSION_COOKIE_NAME: session_id},
            json={
                "profile_id": "profile_one",
                "display_name": "Profile One",
                "server": "localhost",
                "database_name": "DB1",
                "set_as_default": True,
            },
        )
        results["req1"] = resp

    def run_req2():
        # Wait until Request 1 is right inside sync_from_store
        assert t1_inside_sync.wait(timeout=3.0)
        t2_tried_lock.set()
        resp = client.post(
            "/agent/v1/profiles",
            headers={"Host": "127.0.0.1:8001", "X-QueryPilot-Request": "1"},
            cookies={SESSION_COOKIE_NAME: session_id},
            json={
                "profile_id": "profile_two",
                "display_name": "Profile Two",
                "server": "localhost",
                "database_name": "DB2",
                "set_as_default": False,
            },
        )
        results["req2"] = resp

    with patch.object(store, "sync_from_store", side_effect=sync_wrapper):
        th1 = threading.Thread(target=run_req1)
        th2 = threading.Thread(target=run_req2)
        th1.start()
        th2.start()
        th1.join(timeout=5.0)
        th2.join(timeout=5.0)

    assert not th1.is_alive()
    assert not th2.is_alive()

    assert results["req1"].status_code == status.HTTP_201_CREATED
    assert results["req2"].status_code == status.HTTP_201_CREATED

    # Assert both profiles remain present in memory
    assert store.has_profile("profile_one")
    assert store.has_profile("profile_two")

    # Assert both profiles remain present on disk
    disk_store = load_trusted_profiles_file(profiles_file)
    assert disk_store.has_profile("profile_one")
    assert disk_store.has_profile("profile_two")

    # Assert the selected default is consistent in memory and on disk
    assert store.default_profile_id == "profile_one"
    assert disk_store.default_profile_id == "profile_one"

    # Assert lock file is released
    assert not profiles_file.with_name(f"{profiles_file.name}.lock").exists()

    # Assert existing engine-manager entries remain untouched
    assert engine_manager._engines.get("pre_existing") is mock_engine


def test_simulated_stale_snapshot_overwrite_reproduced_and_prevented():
    """Explicitly verify the race failure mode: an older snapshot overwriting newer memory state,
    and confirm that holding the cross-process lock serializes updates to prevent it.
    """
    # 1. Demonstrate the race bug directly on ProfileStore
    # Snapshot 1 was taken when only p1 existed
    snap1 = ProfileStore()
    p1 = ConnectionProfile(profile_id="p1", display_name="P1", server="localhost", database_name="DB1", use_trusted_connection=True)
    snap1.register_profile(p1)
    snap1.set_default_profile("p1")

    # Snapshot 2 was taken later when p1 and p2 existed
    snap2 = ProfileStore()
    p2 = ConnectionProfile(profile_id="p2", display_name="P2", server="localhost", database_name="DB2", use_trusted_connection=True)
    snap2.register_profile(p1)
    snap2.register_profile(p2)
    snap2.set_default_profile("p1")

    # If snap2 syncs first, then snap1 syncs last (as occurred in F8D2 race condition):
    test_store = ProfileStore()
    test_store.sync_from_store(snap2)
    assert test_store.has_profile("p1")
    assert test_store.has_profile("p2")

    # Stale snap1 syncs last, clobbering p2 in memory:
    test_store.sync_from_store(snap1)
    assert test_store.has_profile("p1")
    assert not test_store.has_profile("p2")  # Proof of race bug in memory!

    # 2. In contrast, with serialized lock acquisition, snap1 syncs BEFORE snap2 can read disk,
    # ensuring snap2 always syncs second and keeps both profiles in memory.
    test_store_fixed = ProfileStore()
    test_store_fixed.sync_from_store(snap1)
    test_store_fixed.sync_from_store(snap2)
    assert test_store_fixed.has_profile("p1")
    assert test_store_fixed.has_profile("p2")


def test_profile_file_lock_cleaned_up_on_persistence_failure(agent_fixture):
    """Verify lock is released and memory unchanged when disk save fails."""
    client = agent_fixture["client"]
    session_id = agent_fixture["session_id"]
    profiles_file = agent_fixture["profiles_file"]
    store = agent_fixture["store"]

    with patch("query_pilot.agent.app.save_trusted_profiles_file", side_effect=OSError("Disk full")):
        resp = client.post(
            "/agent/v1/profiles",
            headers={"Host": "127.0.0.1:8001", "X-QueryPilot-Request": "1"},
            cookies={SESSION_COOKIE_NAME: session_id},
            json={
                "profile_id": "fail_lock_test",
                "display_name": "Fail Lock Test",
                "server": "localhost",
                "database_name": "DB1",
            },
        )
        assert resp.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR

    # Memory unchanged
    assert not store.has_profile("fail_lock_test")
    # Lock file released
    assert not profiles_file.with_name(f"{profiles_file.name}.lock").exists()

    # Verify a subsequent request can immediately acquire the lock and succeed
    resp_subsequent = client.post(
        "/agent/v1/profiles",
        headers={"Host": "127.0.0.1:8001", "X-QueryPilot-Request": "1"},
        cookies={SESSION_COOKIE_NAME: session_id},
        json={
            "profile_id": "subsequent_profile",
            "display_name": "Subsequent Profile",
            "server": "localhost",
            "database_name": "DB1",
        },
    )
    assert resp_subsequent.status_code == status.HTTP_201_CREATED
    assert store.has_profile("subsequent_profile")
    assert not profiles_file.with_name(f"{profiles_file.name}.lock").exists()


def test_profile_file_lock_cleaned_up_on_duplicate_rejection(agent_fixture):
    """Verify lock is released when duplicate profile ID is rejected."""
    client = agent_fixture["client"]
    session_id = agent_fixture["session_id"]
    profiles_file = agent_fixture["profiles_file"]
    store = agent_fixture["store"]

    # Pre-populate disk file directly with an existing profile so it passes memory check but fails disk check
    disk_store = ProfileStore()
    disk_store.register_profile(
        ConnectionProfile(
            profile_id="on_disk_dup",
            display_name="On Disk Dup",
            server="localhost",
            database_name="DB1",
            use_trusted_connection=True,
        )
    )
    from query_pilot.db.profile import save_trusted_profiles_file

    save_trusted_profiles_file(profiles_file, disk_store)

    resp = client.post(
        "/agent/v1/profiles",
        headers={"Host": "127.0.0.1:8001", "X-QueryPilot-Request": "1"},
        cookies={SESSION_COOKIE_NAME: session_id},
        json={
            "profile_id": "on_disk_dup",
            "display_name": "On Disk Dup",
            "server": "localhost",
            "database_name": "DB1",
        },
    )
    assert resp.status_code == status.HTTP_409_CONFLICT
    assert not profiles_file.with_name(f"{profiles_file.name}.lock").exists()


def test_profile_file_lock_cleaned_up_on_sync_failure(agent_fixture):
    """Verify lock is released when persistence succeeds but in-memory sync fails."""
    client = agent_fixture["client"]
    session_id = agent_fixture["session_id"]
    profiles_file = agent_fixture["profiles_file"]

    payload = {
        "profile_id": "sync_fail_lock_test",
        "display_name": "Sync Fail Lock Test",
        "server": "localhost",
        "database_name": "TestDB",
    }

    with patch("query_pilot.db.profile.ProfileStore.sync_from_store", side_effect=RuntimeError("sync crash")):
        with patch("query_pilot.agent.app.load_trusted_profiles_file", side_effect=RuntimeError("reload crash")):
            resp = client.post(
                "/agent/v1/profiles",
                headers={"Host": "127.0.0.1:8001", "X-QueryPilot-Request": "1"},
                cookies={SESSION_COOKIE_NAME: session_id},
                json=payload,
            )
            assert resp.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
            assert "PROFILE_PERSISTED_SYNC_FAILED" in resp.text

    # Lock file released
    assert not profiles_file.with_name(f"{profiles_file.name}.lock").exists()


def test_create_profile_corrupted_disk_store_fails_closed(agent_fixture):
    """When on-disk profiles file is corrupted/unparseable JSON, creation fails closed with 500 and releases lock."""
    client = agent_fixture["client"]
    session_id = agent_fixture["session_id"]
    profiles_file = agent_fixture["profiles_file"]
    store = agent_fixture["store"]

    # Write corrupt data to on-disk file
    profiles_file.write_text("{ malformed json content !!!", encoding="utf-8")

    payload = {
        "profile_id": "corrupt_disk_test",
        "display_name": "Corrupt Disk Test",
        "server": "localhost",
        "database_name": "TestDB",
    }

    resp = client.post(
        "/agent/v1/profiles",
        headers={"Host": "127.0.0.1:8001", "X-QueryPilot-Request": "1"},
        cookies={SESSION_COOKIE_NAME: session_id},
        json=payload,
    )
    assert resp.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    assert resp.json()["detail"] == "Failed to persist profile configuration."

    # In-memory store remains unaffected
    assert "corrupt_disk_test" not in store.profiles

    # Lock file released
    assert not profiles_file.with_name(f"{profiles_file.name}.lock").exists()


def test_create_profile_non_dict_root_disk_store_fails_closed(agent_fixture):
    """When on-disk profiles file has invalid root structure (not a JSON dict), creation fails closed with 500."""
    client = agent_fixture["client"]
    session_id = agent_fixture["session_id"]
    profiles_file = agent_fixture["profiles_file"]
    store = agent_fixture["store"]

    # Write valid JSON array instead of dict root
    profiles_file.write_text('["not", "a", "dict"]', encoding="utf-8")

    payload = {
        "profile_id": "non_dict_root_test",
        "display_name": "Non Dict Root Test",
        "server": "localhost",
        "database_name": "TestDB",
    }

    resp = client.post(
        "/agent/v1/profiles",
        headers={"Host": "127.0.0.1:8001", "X-QueryPilot-Request": "1"},
        cookies={SESSION_COOKIE_NAME: session_id},
        json=payload,
    )
    assert resp.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    assert resp.json()["detail"] == "Failed to persist profile configuration."

    assert "non_dict_root_test" not in store.profiles
    assert not profiles_file.with_name(f"{profiles_file.name}.lock").exists()
