"""Offline unit and integration tests for profile connection testing endpoint
POST /agent/v1/profiles/{profile_id}/test-connection.

Verifies Milestone B12-F8E:
1. Successful connection and identity verification.
2. Authentication failure classification (AUTH_FAILED).
3. Unreachable server or network failure (UNREACHABLE).
4. ODBC/login timeout classification (TIMEOUT).
5. Driver error classification (DRIVER_ERROR).
6. Database identity mismatch (DATABASE_MISMATCH).
7. Deterministic connection closure and temporary engine disposal on success and all failure paths.
8. Session cookie (401), custom-header (400), Host (400), and Origin (403) enforcement.
9. Unknown profile ID returns 404.
10. Rejection of client-supplied connection fields or arbitrary SQL (422).
11. Zero Gemini, external API, ODBC, or live database calls.
12. Existing AgentEngineManager entries and active-query resources remain untouched.
13. Concurrent test requests do not share temporary engines.
14. Response privacy: no raw exceptions, server/database details on failure, connection strings, or usernames.
15. database_name is populated only after verified success (None on failure).
16. The intended NullPool and bounded login timeout (<= 15s) are configured correctly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

from fastapi import status
from fastapi.testclient import TestClient
import pytest
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import OperationalError, SQLAlchemyError
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
    TestConnectionResponse,
)
from query_pilot.db.connection import (
    DatabaseSecurityError,
)
from query_pilot.db.factory import ProfileConnectionFactory
from query_pilot.db.profile import (
    ConnectionProfile,
    ProfileStore,
)


@pytest.fixture
def agent_fixture(tmp_path: Path):
    """Sets up an isolated agent app with an authenticated session and pre-registered profile."""
    token_dir = tmp_path / "token_dir"
    token_dir.mkdir(parents=True, exist_ok=True)
    profiles_file = tmp_path / "profiles.json"

    store = ProfileStore()
    # Pre-register a trusted profile for connection testing
    profile = ConnectionProfile(
        profile_id="corp_dw",
        display_name="Corporate DW",
        server="localhost",
        database_name="CorpAnalytics",
        use_trusted_connection=True,
        timeout_seconds=10,
        max_row_limit=50,
        schema_name="dbo",
    )
    store.register_profile(profile)
    store.set_default_profile("corp_dw")

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

    # Authenticate session
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
        "factory": factory,
        "engine_manager": engine_manager,
        "profile": profile,
    }


# ==============================================================================
# 1. Successful Connection and Identity Verification
# ==============================================================================

def test_test_connection_success(agent_fixture):
    """Successful connection returns connected=True, verified database_name, latency, and clean message."""
    client = agent_fixture["client"]
    session_id = agent_fixture["session_id"]

    mock_conn = MagicMock(spec=Connection)
    mock_conn.execute.return_value.scalar.return_value = "CorpAnalytics"

    mock_engine = MagicMock(spec=Engine)
    mock_engine.connect.return_value.__enter__.return_value = mock_conn

    with patch.object(agent_fixture["factory"], "create_engine", return_value=mock_engine) as mock_create:
        resp = client.post(
            "/agent/v1/profiles/corp_dw/test-connection",
            headers={"Host": "127.0.0.1:8001", "X-QueryPilot-Request": "1"},
            cookies={SESSION_COOKIE_NAME: session_id},
        )
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()
        assert data["connected"] is True
        assert data["database_name"] == "CorpAnalytics"
        assert data["latency_ms"] is not None
        assert data["latency_ms"] >= 0
        assert data["failure_code"] is None
        assert "verified" in data["message"].lower()

        # Verify create_engine used NullPool
        mock_create.assert_called_once_with(profile_id="corp_dw", poolclass=NullPool)
        # Verify engine was disposed
        mock_engine.dispose.assert_called_once()
        # Verify connection was closed via context manager
        mock_engine.connect.return_value.__exit__.assert_called_once()


# ==============================================================================
# 2. Authentication Failure Classification
# ==============================================================================

def test_test_connection_auth_failure(agent_fixture):
    """Windows login failure returns connected=False, failure_code='AUTH_FAILED', and no username leak."""
    client = agent_fixture["client"]
    session_id = agent_fixture["session_id"]

    mock_engine = MagicMock(spec=Engine)
    mock_engine.connect.side_effect = OperationalError(
        "statement",
        {},
        Exception("Login failed for user 'CONTOSO\\Alice' (Error 18456, Severity 14, State 1)"),
    )

    with patch.object(agent_fixture["factory"], "create_engine", return_value=mock_engine):
        resp = client.post(
            "/agent/v1/profiles/corp_dw/test-connection",
            headers={"Host": "127.0.0.1:8001", "X-QueryPilot-Request": "1"},
            cookies={SESSION_COOKIE_NAME: session_id},
        )
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()
        assert data["connected"] is False
        assert data["database_name"] is None
        assert data["failure_code"] == "AUTH_FAILED"
        assert "authentication failed" in data["message"].lower()
        # Zero sensitive leakage
        assert "Alice" not in resp.text
        assert "18456" not in resp.text
        # Engine disposed
        mock_engine.dispose.assert_called_once()


# ==============================================================================
# 3. Unreachable Server and Driver Failure Classification
# ==============================================================================

def test_test_connection_unreachable_server(agent_fixture):
    """Unreachable server returns connected=False and failure_code='UNREACHABLE'."""
    client = agent_fixture["client"]
    session_id = agent_fixture["session_id"]

    mock_engine = MagicMock(spec=Engine)
    mock_engine.connect.side_effect = OperationalError(
        "statement",
        {},
        Exception("Server does not exist or access denied. Error 08001. TCP Provider: Connection refused"),
    )

    with patch.object(agent_fixture["factory"], "create_engine", return_value=mock_engine):
        resp = client.post(
            "/agent/v1/profiles/corp_dw/test-connection",
            headers={"Host": "127.0.0.1:8001", "X-QueryPilot-Request": "1"},
            cookies={SESSION_COOKIE_NAME: session_id},
        )
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()
        assert data["connected"] is False
        assert data["database_name"] is None
        assert data["failure_code"] == "UNREACHABLE"
        assert "unreachable" in data["message"].lower()
        mock_engine.dispose.assert_called_once()


def test_test_connection_driver_error(agent_fixture):
    """Missing or misconfigured ODBC driver returns connected=False and failure_code='DRIVER_ERROR'."""
    client = agent_fixture["client"]
    session_id = agent_fixture["session_id"]

    mock_engine = MagicMock(spec=Engine)
    mock_engine.connect.side_effect = OperationalError(
        "statement",
        {},
        Exception("Data source name not found and no default driver specified. Error IM002"),
    )

    with patch.object(agent_fixture["factory"], "create_engine", return_value=mock_engine):
        resp = client.post(
            "/agent/v1/profiles/corp_dw/test-connection",
            headers={"Host": "127.0.0.1:8001", "X-QueryPilot-Request": "1"},
            cookies={SESSION_COOKIE_NAME: session_id},
        )
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()
        assert data["connected"] is False
        assert data["database_name"] is None
        assert data["failure_code"] == "DRIVER_ERROR"
        assert "driver" in data["message"].lower()
        mock_engine.dispose.assert_called_once()


# ==============================================================================
# 4. Login Timeout Classification
# ==============================================================================

def test_test_connection_timeout(agent_fixture):
    """Connection timeout returns connected=False and failure_code='TIMEOUT'."""
    client = agent_fixture["client"]
    session_id = agent_fixture["session_id"]

    mock_engine = MagicMock(spec=Engine)
    mock_engine.connect.side_effect = OperationalError(
        "statement",
        {},
        Exception("Login timeout expired (Error HYT00)"),
    )

    with patch.object(agent_fixture["factory"], "create_engine", return_value=mock_engine):
        resp = client.post(
            "/agent/v1/profiles/corp_dw/test-connection",
            headers={"Host": "127.0.0.1:8001", "X-QueryPilot-Request": "1"},
            cookies={SESSION_COOKIE_NAME: session_id},
        )
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()
        assert data["connected"] is False
        assert data["database_name"] is None
        assert data["failure_code"] == "TIMEOUT"
        assert "timed out" in data["message"].lower()
        mock_engine.dispose.assert_called_once()


# ==============================================================================
# 5. Database Identity Mismatch
# ==============================================================================

def test_test_connection_database_identity_mismatch(agent_fixture):
    """When connected session is master instead of configured DB, failure_code='DATABASE_MISMATCH'."""
    client = agent_fixture["client"]
    session_id = agent_fixture["session_id"]

    mock_conn = MagicMock(spec=Connection)
    # Target expects CorpAnalytics, but connected session is master
    mock_conn.execute.return_value.scalar.return_value = "master"

    mock_engine = MagicMock(spec=Engine)
    mock_engine.connect.return_value.__enter__.return_value = mock_conn

    with patch.object(agent_fixture["factory"], "create_engine", return_value=mock_engine):
        resp = client.post(
            "/agent/v1/profiles/corp_dw/test-connection",
            headers={"Host": "127.0.0.1:8001", "X-QueryPilot-Request": "1"},
            cookies={SESSION_COOKIE_NAME: session_id},
        )
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()
        assert data["connected"] is False
        assert data["database_name"] is None  # database_name MUST NOT be leaked on mismatch
        assert data["failure_code"] == "DATABASE_MISMATCH"
        assert "did not match" in data["message"].lower()

        # Ensure connection was closed and engine disposed even on security error
        mock_engine.connect.return_value.__exit__.assert_called_once()
        mock_engine.dispose.assert_called_once()


# ==============================================================================
# 6. Resource Cleanup on Success and Failures
# ==============================================================================

def test_test_connection_cleanup_on_unexpected_exception(agent_fixture):
    """Unexpected exception still cleans up engine resources and returns CONNECTION_FAILED."""
    client = agent_fixture["client"]
    session_id = agent_fixture["session_id"]

    mock_engine = MagicMock(spec=Engine)
    mock_engine.connect.side_effect = RuntimeError("Fatal socket error in OS kernel")

    with patch.object(agent_fixture["factory"], "create_engine", return_value=mock_engine):
        resp = client.post(
            "/agent/v1/profiles/corp_dw/test-connection",
            headers={"Host": "127.0.0.1:8001", "X-QueryPilot-Request": "1"},
            cookies={SESSION_COOKIE_NAME: session_id},
        )
        assert resp.status_code == status.HTTP_200_OK
        data = resp.json()
        assert data["connected"] is False
        assert data["database_name"] is None
        assert data["failure_code"] == "CONNECTION_FAILED"
        mock_engine.dispose.assert_called_once()


# ==============================================================================
# 7. Session, Custom-Header, Host, and Origin Enforcement
# ==============================================================================

def test_test_connection_unauthenticated_fails_401(agent_fixture):
    """Missing or invalid session cookie returns HTTP 401 Unauthorized."""
    client = agent_fixture["client"]
    client.cookies.clear()
    resp = client.post(
        "/agent/v1/profiles/corp_dw/test-connection",
        headers={"Host": "127.0.0.1:8001", "X-QueryPilot-Request": "1"},
    )
    assert resp.status_code == status.HTTP_401_UNAUTHORIZED

    # Invalid session cookie
    resp = client.post(
        "/agent/v1/profiles/corp_dw/test-connection",
        headers={"Host": "127.0.0.1:8001", "X-QueryPilot-Request": "1"},
        cookies={SESSION_COOKIE_NAME: "invalid-token-value"},
    )
    assert resp.status_code == status.HTTP_401_UNAUTHORIZED


def test_test_connection_missing_custom_header_fails_400(agent_fixture):
    """Missing X-QueryPilot-Request: 1 header returns HTTP 400 Bad Request."""
    client = agent_fixture["client"]
    session_id = agent_fixture["session_id"]
    resp = client.post(
        "/agent/v1/profiles/corp_dw/test-connection",
        headers={"Host": "127.0.0.1:8001"},
        cookies={SESSION_COOKIE_NAME: session_id},
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST


def test_test_connection_invalid_host_fails_400(agent_fixture):
    """Non-loopback Host header returns HTTP 400."""
    client = agent_fixture["client"]
    session_id = agent_fixture["session_id"]
    resp = client.post(
        "/agent/v1/profiles/corp_dw/test-connection",
        headers={"Host": "attacker.com", "X-QueryPilot-Request": "1"},
        cookies={SESSION_COOKIE_NAME: session_id},
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST


def test_test_connection_disallowed_origin_fails_403(agent_fixture):
    """Disallowed external Origin returns HTTP 403 Forbidden."""
    client = agent_fixture["client"]
    session_id = agent_fixture["session_id"]
    resp = client.post(
        "/agent/v1/profiles/corp_dw/test-connection",
        headers={
            "Host": "127.0.0.1:8001",
            "Origin": "http://evil-site.com",
            "X-QueryPilot-Request": "1",
        },
        cookies={SESSION_COOKIE_NAME: session_id},
    )
    assert resp.status_code == status.HTTP_403_FORBIDDEN


# ==============================================================================
# 8. Unknown Profile ID
# ==============================================================================

def test_test_connection_unknown_profile_fails_404(agent_fixture):
    """Unregistered profile ID returns HTTP 404 Not Found."""
    client = agent_fixture["client"]
    session_id = agent_fixture["session_id"]
    resp = client.post(
        "/agent/v1/profiles/non_existent_profile/test-connection",
        headers={"Host": "127.0.0.1:8001", "X-QueryPilot-Request": "1"},
        cookies={SESSION_COOKIE_NAME: session_id},
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND
    assert "not found" in resp.json()["detail"].lower()


# ==============================================================================
# 9. Rejection of Client-Supplied Connection Overrides or SQL
# ==============================================================================

def test_test_connection_rejects_client_payload_overrides(agent_fixture):
    """Supplying connection overrides or SQL payload is rejected by extra='forbid'."""
    client = agent_fixture["client"]
    session_id = agent_fixture["session_id"]

    for payload in [
        {"server": "evil.server.com"},
        {"database_name": "SecretDB"},
        {"sql": "SELECT * FROM Users"},
        {"connection_string": "DRIVER=evil;"},
        {"password": "secret"},
    ]:
        resp = client.post(
            "/agent/v1/profiles/corp_dw/test-connection",
            headers={"Host": "127.0.0.1:8001", "X-QueryPilot-Request": "1"},
            cookies={SESSION_COOKIE_NAME: session_id},
            json=payload,
        )
        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


# ==============================================================================
# 10. Preserves Shared EngineManager Entries
# ==============================================================================

def test_test_connection_preserves_engine_manager_entries(agent_fixture):
    """Testing connection uses NullPool and does not touch or evict AgentEngineManager entries."""
    client = agent_fixture["client"]
    session_id = agent_fixture["session_id"]
    engine_manager = agent_fixture["engine_manager"]

    # Pre-register an engine in engine_manager
    mock_active_engine = MagicMock(spec=Engine)
    engine_manager._engines["corp_dw"] = mock_active_engine
    engine_manager._owned_engines.add("corp_dw")

    mock_conn = MagicMock(spec=Connection)
    mock_conn.execute.return_value.scalar.return_value = "CorpAnalytics"

    mock_test_engine = MagicMock(spec=Engine)
    mock_test_engine.connect.return_value.__enter__.return_value = mock_conn

    with patch.object(agent_fixture["factory"], "create_engine", return_value=mock_test_engine):
        resp = client.post(
            "/agent/v1/profiles/corp_dw/test-connection",
            headers={"Host": "127.0.0.1:8001", "X-QueryPilot-Request": "1"},
            cookies={SESSION_COOKIE_NAME: session_id},
        )
        assert resp.status_code == status.HTTP_200_OK
        assert resp.json()["connected"] is True

    # Assert active engine in manager was NOT disposed or replaced
    assert engine_manager._engines["corp_dw"] is mock_active_engine
    mock_active_engine.dispose.assert_not_called()


# ==============================================================================
# 11. Concurrent Test Requests Do Not Share Engines
# ==============================================================================

def test_test_connection_distinct_engines_per_request(agent_fixture):
    """Sequential or concurrent connection tests instantiate independent temporary engines."""
    client = agent_fixture["client"]
    session_id = agent_fixture["session_id"]

    mock_engines = [MagicMock(spec=Engine), MagicMock(spec=Engine)]
    mock_conns = [MagicMock(spec=Connection), MagicMock(spec=Connection)]

    for eng, conn in zip(mock_engines, mock_conns):
        conn.execute.return_value.scalar.return_value = "CorpAnalytics"
        eng.connect.return_value.__enter__.return_value = conn

    engine_iter = iter(mock_engines)

    with patch.object(agent_fixture["factory"], "create_engine", side_effect=lambda *a, **k: next(engine_iter)):
        resp1 = client.post(
            "/agent/v1/profiles/corp_dw/test-connection",
            headers={"Host": "127.0.0.1:8001", "X-QueryPilot-Request": "1"},
            cookies={SESSION_COOKIE_NAME: session_id},
        )
        resp2 = client.post(
            "/agent/v1/profiles/corp_dw/test-connection",
            headers={"Host": "127.0.0.1:8001", "X-QueryPilot-Request": "1"},
            cookies={SESSION_COOKIE_NAME: session_id},
        )
        assert resp1.status_code == status.HTTP_200_OK
        assert resp2.status_code == status.HTTP_200_OK

    # Both distinct engines were independently disposed
    mock_engines[0].dispose.assert_called_once()
    mock_engines[1].dispose.assert_called_once()


# ==============================================================================
# 12. Factory Applies NullPool and Driver Login Timeout
# ==============================================================================

def test_test_connection_factory_applies_nullpool_and_timeout(agent_fixture):
    """The factory configuration sets connect_args timeout and NullPool without real connection."""
    factory = agent_fixture["factory"]

    # Call factory.create_engine with poolclass=NullPool
    engine = factory.create_engine("corp_dw", poolclass=NullPool)
    assert isinstance(engine, Engine)
    assert isinstance(engine.pool, NullPool)

    # Get configuration and verify connect_args timeout is bounded to profile timeout <= 15s
    cfg = factory.get_configuration("corp_dw")
    assert cfg.engine_kwargs["connect_args"]["timeout"] == 10
    assert cfg.timeout_seconds == 10
