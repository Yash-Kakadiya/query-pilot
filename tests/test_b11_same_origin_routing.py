"""Offline tests for B11-B1 Constrained Same-Origin API Routing.

Verifies:
1. Successful forwarding of allowed endpoints:
   - GET  /api/health               -> Upstream /health
   - POST /api/v1/generate-sql      -> Upstream /api/v1/generate-sql
   - POST /api/v1/synthesize-answer -> Upstream /api/v1/synthesize-answer
   - POST /api/query                -> Upstream /query
2. Rejection of unsupported endpoints and methods (404/405).
3. Upstream URL is not controllable by client request inputs (headers, query params, body).
4. SSRF and loopback validation in AgentConfig.upstream_api_url.
5. Strict header filtering:
   - Cookies (qpt_session) and agent CSRF headers (X-QueryPilot-Request) are stripped.
   - Hop-by-hop headers are stripped.
   - Correlated request ID and access key are forwarded.
6. Honest error and health status representation (502, 503, 504) with zero traceback or path leakage.
7. Production CSP remains strictly 'self' https: with no relaxation.
8. Candidate SQL generation makes zero database execution calls.
9. Actual database query execution remains strictly within the local agent boundary.
10. Host/Origin validation and agent CSRF protection remain intact.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock

from fastapi import status
from fastapi.testclient import TestClient
import httpx
import pytest

from query_pilot.agent import create_agent_app
from query_pilot.agent.config import AgentConfig
from query_pilot.agent.models import BootstrapResponse
from query_pilot.db.models import (
    ColumnMetadata,
    DatabaseSchema,
    ForeignKeyMetadata,
    TableMetadata,
)
from query_pilot.db.profile import ConnectionProfile, ProfileStore


@pytest.fixture
def test_schema() -> DatabaseSchema:
    """Fixture providing a verified in-memory DatabaseSchema."""
    return DatabaseSchema(
        database_name="B11_TestDB",
        tables={
            "courses": TableMetadata(
                schema_name="dbo",
                table_name="courses",
                columns=[
                    ColumnMetadata(name="course_id", data_type="int", nullable=False, primary_key=True),
                    ColumnMetadata(name="course_name", data_type="varchar", nullable=False),
                ],
            ),
        },
    )


@pytest.fixture
def profile_store() -> ProfileStore:
    """Fixture providing a trusted ProfileStore with one approved connection profile."""
    profile = ConnectionProfile(
        profile_id="test_profile",
        display_name="Test Profile",
        server="127.0.0.1",
        database_name="B11_TestDB",
        schema_name="dbo",
        use_trusted_connection=True,
    )
    return ProfileStore(profiles={"test_profile": profile})


class MockUpstreamRouter:
    """Mock upstream HTTP transport capturing dispatched requests and returning controlled responses."""

    def __init__(self):
        self.dispatched_requests: List[httpx.Request] = []
        self.health_response_code: int = 200
        self.health_payload: Dict[str, Any] = {
            "status": "healthy",
            "version": "0.1.0",
            "dialect": "tsql",
        }
        self.generate_sql_code: int = 200
        self.generate_sql_payload: Dict[str, Any] = {
            "candidate_sql": "SELECT COUNT(*) AS total FROM dbo.courses;",
            "is_executed": False,
            "status": "generated",
            "request_id": "req-b11-test-01",
            "execution_disclaimer": "Candidate SQL is unexecuted.",
        }
        self.synthesize_code: int = 200
        self.synthesize_payload: Dict[str, Any] = {
            "answer_text": "There are 42 total courses offered.",
            "is_grounded": True,
            "confidence": "high",
        }
        self.legacy_query_code: int = 200
        self.legacy_query_payload: Dict[str, Any] = {
            "status": "success",
            "row_count": 1,
            "rows": [{"total": 42}],
            "columns": ["total"],
        }
        self.timeout_on_path: Optional[str] = None
        self.connect_error_on_path: Optional[str] = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.dispatched_requests.append(request)

        path = request.url.path
        if self.timeout_on_path and path == self.timeout_on_path:
            raise httpx.ReadTimeout("Mocked upstream timeout", request=request)

        if self.connect_error_on_path and path == self.connect_error_on_path:
            raise httpx.ConnectError("Mocked connection refused", request=request)

        if path == "/health":
            return httpx.Response(
                self.health_response_code,
                json=self.health_payload,
                headers={"Content-Type": "application/json", "X-Request-ID": "req-health-01"},
            )

        if path == "/api/v1/generate-sql":
            return httpx.Response(
                self.generate_sql_code,
                json=self.generate_sql_payload,
                headers={"Content-Type": "application/json", "X-Request-ID": "req-b11-test-01"},
            )

        if path == "/api/v1/synthesize-answer":
            return httpx.Response(
                self.synthesize_code,
                json=self.synthesize_payload,
                headers={"Content-Type": "application/json", "X-Request-ID": "req-b11-synth-01"},
            )

        if path == "/query":
            return httpx.Response(
                self.legacy_query_code,
                json=self.legacy_query_payload,
                headers={"Content-Type": "application/json"},
            )

        return httpx.Response(404, json={"detail": "Not found on mock upstream"})


@pytest.fixture
def mock_upstream() -> MockUpstreamRouter:
    return MockUpstreamRouter()


@pytest.fixture
def agent_client(
    tmp_path: Path,
    profile_store: ProfileStore,
    test_schema: DatabaseSchema,
    mock_upstream: MockUpstreamRouter,
):
    """Test client configured with mocked upstream transport and isolated in-memory state."""
    config = AgentConfig(
        bind_host="127.0.0.1",
        bind_port=8001,
        allow_dev_origin=False,  # strictly packaged production mode
        token_dir=tmp_path / "token",
        upstream_api_url="http://127.0.0.1:8000",
        upstream_timeout_seconds=5.0,
    )

    transport = httpx.MockTransport(mock_upstream.handler)
    mock_client = httpx.AsyncClient(transport=transport, base_url=config.upstream_api_url)

    app = create_agent_app(
        config=config,
        store=profile_store,
        caller_schema=test_schema,
        upstream_client=mock_client,
    )

    client = TestClient(app, base_url="http://127.0.0.1:8001")
    return client


# ==============================================================================
# 1. Forwarding of Explicitly Supported Endpoints & Methods
# ==============================================================================


def test_forward_health_endpoint_success(agent_client, mock_upstream):
    """GET /api/health proxies to upstream /health and returns 200 with operational metadata."""
    resp = agent_client.get("/api/health", headers={"Host": "127.0.0.1:8001"})
    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert data["status"] == "healthy"
    assert data["version"] == "0.1.0"
    assert len(mock_upstream.dispatched_requests) == 1
    assert mock_upstream.dispatched_requests[0].url.path == "/health"
    assert mock_upstream.dispatched_requests[0].method == "GET"


def test_forward_generate_sql_success(agent_client, mock_upstream, test_schema):
    """POST /api/v1/generate-sql proxies unexecuted candidate SQL generation to upstream."""
    payload = {
        "question": "Show all courses",
        "schema_context": {
            "database_name": "B11_TestDB",
            "tables": [{"name": "courses", "columns": ["course_id", "course_name"]}],
        },
    }
    resp = agent_client.post(
        "/api/v1/generate-sql",
        headers={"Host": "127.0.0.1:8001", "Content-Type": "application/json"},
        json=payload,
    )
    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert data["candidate_sql"] == "SELECT COUNT(*) AS total FROM dbo.courses;"
    assert data["is_executed"] is False
    assert data["status"] == "generated"
    assert len(mock_upstream.dispatched_requests) == 1
    assert mock_upstream.dispatched_requests[0].url.path == "/api/v1/generate-sql"
    assert mock_upstream.dispatched_requests[0].method == "POST"


def test_forward_synthesize_answer_success(agent_client, mock_upstream):
    """POST /api/v1/synthesize-answer proxies metadata-only answer synthesis to upstream."""
    payload = {
        "question": "Show total courses",
        "candidate_sql": "SELECT COUNT(*) AS total FROM dbo.courses;",
        "columns": ["total"],
        "row_count": 1,
    }
    resp = agent_client.post(
        "/api/v1/synthesize-answer",
        headers={"Host": "127.0.0.1:8001", "Content-Type": "application/json"},
        json=payload,
    )
    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert data["answer_text"] == "There are 42 total courses offered."
    assert len(mock_upstream.dispatched_requests) == 1
    assert mock_upstream.dispatched_requests[0].url.path == "/api/v1/synthesize-answer"


def test_forward_legacy_query_success(agent_client, mock_upstream):
    """POST /api/query proxies legacy query execution to upstream."""
    resp = agent_client.post(
        "/api/query",
        headers={"Host": "127.0.0.1:8001", "Content-Type": "application/json"},
        json={"question": "What is the average grade?"},
    )
    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert data["row_count"] == 1
    assert len(mock_upstream.dispatched_requests) == 1
    assert mock_upstream.dispatched_requests[0].url.path == "/query"


# ==============================================================================
# 2. Rejection of Unsupported Paths and Methods (Allowlist Enforcement)
# ==============================================================================


def test_unsupported_methods_rejected_with_405(agent_client):
    """Wrong HTTP methods on allowed routes return HTTP 405 Method Not Allowed."""
    # GET on generate-sql (which is POST only)
    resp = agent_client.get("/api/v1/generate-sql", headers={"Host": "127.0.0.1:8001"})
    assert resp.status_code == status.HTTP_405_METHOD_NOT_ALLOWED

    # GET on synthesize-answer (which is POST only)
    resp = agent_client.get("/api/v1/synthesize-answer", headers={"Host": "127.0.0.1:8001"})
    assert resp.status_code == status.HTTP_405_METHOD_NOT_ALLOWED

    # POST on health (which is GET only)
    resp = agent_client.post(
        "/api/health",
        headers={"Host": "127.0.0.1:8001", "Content-Type": "application/json"},
        json={},
    )
    assert resp.status_code == status.HTTP_405_METHOD_NOT_ALLOWED


def test_unsupported_paths_rejected_with_404(agent_client):
    """Arbitrary unapproved paths under /api return HTTP 404 Not Found."""
    resp = agent_client.get("/api/unknown", headers={"Host": "127.0.0.1:8001"})
    assert resp.status_code == status.HTTP_404_NOT_FOUND

    resp = agent_client.post(
        "/api/v1/nonexistent",
        headers={"Host": "127.0.0.1:8001", "Content-Type": "application/json"},
        json={},
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND


def test_cannot_proxy_agent_execution_through_api_routes(agent_client):
    """Attempting to access execution or introspection through /api returns 404."""
    resp = agent_client.post(
        "/api/v1/profiles/test_profile/execute",
        headers={"Host": "127.0.0.1:8001", "Content-Type": "application/json"},
        json={"sql": "SELECT 1"},
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND


# ==============================================================================
# 3. Upstream URL Security, SSRF Prevention & Configuration Immutability
# ==============================================================================


def test_upstream_url_cannot_be_overridden_by_request_inputs(agent_client, mock_upstream):
    """Client request headers, query parameters, or body fields cannot alter upstream target."""
    payload = {
        "question": "Show courses",
        "schema_context": {"database_name": "B11_TestDB", "tables": []},
    }
    resp = agent_client.post(
        "/api/v1/generate-sql?upstream=http://evil.com&host=192.168.1.1",
        headers={
            "Host": "127.0.0.1:8001",
            "X-Forwarded-Host": "attacker.com",
            "X-Upstream-Url": "http://attacker.com",
            "Content-Type": "application/json",
        },
        json=payload,
    )
    assert resp.status_code == status.HTTP_200_OK
    assert len(mock_upstream.dispatched_requests) == 1
    # Request dispatched strictly to configured upstream (base_url: http://127.0.0.1:8000)
    assert str(mock_upstream.dispatched_requests[0].url).startswith("http://127.0.0.1:8000")


def test_config_rejects_ssrf_and_non_loopback_unencrypted_upstream():
    """AgentConfig validates upstream_api_url to prevent SSRF and unencrypted non-loopback hosts."""
    # RFC 1918 Private IP rejected for unencrypted HTTP
    with pytest.raises(ValueError, match="must be a loopback host"):
        AgentConfig(upstream_api_url="http://192.168.1.50:8000")

    with pytest.raises(ValueError, match="must be a loopback host"):
        AgentConfig(upstream_api_url="http://10.0.0.1:8000")

    # Cloud metadata endpoint rejected
    with pytest.raises(ValueError, match="must be a loopback host"):
        AgentConfig(upstream_api_url="http://169.254.169.254")

    # Non-http/https scheme rejected
    with pytest.raises(ValueError, match="scheme must be 'http' or 'https'"):
        AgentConfig(upstream_api_url="ftp://127.0.0.1:8000")

    # Empty URL rejected
    with pytest.raises(ValueError, match="cannot be empty"):
        AgentConfig(upstream_api_url="")

    # Valid loopback HTTP accepted
    cfg_loopback = AgentConfig(upstream_api_url="http://127.0.0.1:8000")
    assert cfg_loopback.upstream_api_url == "http://127.0.0.1:8000"

    cfg_localhost = AgentConfig(upstream_api_url="http://localhost:8000/")
    assert cfg_localhost.upstream_api_url == "http://localhost:8000"

    # Valid HTTPS cloud endpoint accepted
    cfg_https = AgentConfig(upstream_api_url="https://api.querypilot.com")
    assert cfg_https.upstream_api_url == "https://api.querypilot.com"


# ==============================================================================
# 4. Strict Header Filtering & Zero Credential Leakage
# ==============================================================================


def test_header_sanitization_strips_cookies_and_agent_credentials(agent_client, mock_upstream):
    """The proxy strictly strips cookies, agent CSRF headers, and hop-by-hop headers."""
    payload = {
        "question": "Show courses",
        "schema_context": {"database_name": "B11_TestDB", "tables": []},
    }
    resp = agent_client.post(
        "/api/v1/generate-sql",
        headers={
            "Host": "127.0.0.1:8001",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Cookie": "qpt_session=secret_token_12345; other_cookie=xyz",
            "X-QueryPilot-Request": "1",
            "X-Request-ID": "corr-req-777",
            "X-QueryPilot-Access-Key": "test-access-key-888",
            "Connection": "keep-alive",
            "Upgrade": "websocket",
        },
        json=payload,
    )
    assert resp.status_code == status.HTTP_200_OK
    assert len(mock_upstream.dispatched_requests) == 1

    dispatched = mock_upstream.dispatched_requests[0]
    dispatched_header_keys = [k.lower() for k in dispatched.headers.keys()]

    # Critical security assertion: Ambient session cookie is NEVER sent to upstream API
    assert "cookie" not in dispatched_header_keys
    assert "qpt_session" not in str(dispatched.headers)

    # Local CSRF header is NOT forwarded upstream
    assert "x-querypilot-request" not in dispatched_header_keys

    # Hop-by-hop headers stripped
    assert "upgrade" not in dispatched_header_keys

    # Allowed correlation and API auth headers ARE forwarded
    assert dispatched.headers.get("x-request-id") == "corr-req-777"
    assert dispatched.headers.get("x-querypilot-access-key") == "test-access-key-888"


# ==============================================================================
# 5. Error & Timeout Handling (Truthful & Sanitized)
# ==============================================================================


def test_upstream_timeout_returns_sanitized_504(agent_client, mock_upstream):
    """Upstream timeout returns HTTP 504 with sanitized error detail (no tracebacks)."""
    mock_upstream.timeout_on_path = "/api/v1/generate-sql"
    resp = agent_client.post(
        "/api/v1/generate-sql",
        headers={"Host": "127.0.0.1:8001", "Content-Type": "application/json"},
        json={"question": "Test", "schema_context": {"database_name": "B11_TestDB", "tables": []}},
    )
    assert resp.status_code == status.HTTP_504_GATEWAY_TIMEOUT
    data = resp.json()
    assert data["detail"] == "Upstream API request timed out."
    assert "traceback" not in resp.text.lower()
    assert "127.0.0.1:8000" not in resp.text


def test_upstream_connect_failure_returns_sanitized_502(agent_client, mock_upstream):
    """Upstream connection failure returns HTTP 502 with safe error detail."""
    mock_upstream.connect_error_on_path = "/api/v1/generate-sql"
    resp = agent_client.post(
        "/api/v1/generate-sql",
        headers={"Host": "127.0.0.1:8001", "Content-Type": "application/json"},
        json={"question": "Test", "schema_context": {"database_name": "B11_TestDB", "tables": []}},
    )
    assert resp.status_code == status.HTTP_502_BAD_GATEWAY
    data = resp.json()
    assert data["detail"] == "Unable to connect to upstream API service."
    assert "traceback" not in resp.text.lower()


def test_upstream_health_check_failure_returns_503(agent_client, mock_upstream):
    """When upstream is unreachable, /api/health returns HTTP 503 unavailable."""
    mock_upstream.connect_error_on_path = "/health"
    resp = agent_client.get("/api/health", headers={"Host": "127.0.0.1:8001"})
    assert resp.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    data = resp.json()
    assert data["status"] == "unavailable"
    assert "unreachable" in data["detail"].lower()


# ==============================================================================
# 6. Production Content Security Policy (CSP) Verification
# ==============================================================================


def test_production_csp_remains_strictly_self_and_https(agent_client):
    """Production CSP must remain strictly 'self' https: without adding http://127.0.0.1:8000."""
    resp = agent_client.get("/api/health", headers={"Host": "127.0.0.1:8001"})
    csp = resp.headers.get("content-security-policy", "")
    assert "connect-src 'self' https:" in csp
    # Port 8000 must NOT be present in production CSP
    assert "http://127.0.0.1:8000" not in csp
    assert "http://localhost:8000" not in csp


# ==============================================================================
# 7. Candidate SQL Generation Does NOT Execute SQL
# ==============================================================================


def test_candidate_sql_generation_triggers_zero_database_execution(
    agent_client, mock_upstream
):
    """Candidate SQL generation through /api/v1/generate-sql causes ZERO database execution calls."""
    spy_engine = MagicMock()
    # Inject engine spy on app state
    agent_client.app.state.caller_engine = spy_engine

    payload = {
        "question": "Show all student grades",
        "schema_context": {"database_name": "B11_TestDB", "tables": []},
    }
    resp = agent_client.post(
        "/api/v1/generate-sql",
        headers={"Host": "127.0.0.1:8001", "Content-Type": "application/json"},
        json=payload,
    )
    assert resp.status_code == status.HTTP_200_OK

    # Assert zero calls to database execution engine
    spy_engine.connect.assert_not_called()
    spy_engine.execute.assert_not_called()


# ==============================================================================
# 8. Local Execution Boundary Remains Strict on Local Agent
# ==============================================================================


def test_sql_execution_executes_locally_and_requires_agent_session(
    agent_client, tmp_path
):
    """SQL query execution (/agent/v1/profiles/{id}/execute) requires session auth and custom header."""
    # 1. Unauthenticated execution fails with 401
    resp = agent_client.post(
        "/agent/v1/profiles/test_profile/execute",
        headers={"Host": "127.0.0.1:8001", "X-QueryPilot-Request": "1"},
        json={"sql": "SELECT 1"},
    )
    assert resp.status_code == status.HTTP_401_UNAUTHORIZED

    # 2. Authenticate session via bootstrap token
    token = agent_client.app.state.token_manager.initialize_token()
    auth_resp = agent_client.post(
        "/agent/v1/auth/bootstrap",
        headers={"Host": "127.0.0.1:8001", "X-QueryPilot-Request": "1"},
        json={"bootstrap_token": token},
    )
    assert auth_resp.status_code == status.HTTP_200_OK

    # 3. Missing X-QueryPilot-Request header on execution endpoint fails with 400
    no_header_resp = agent_client.post(
        "/agent/v1/profiles/test_profile/execute",
        headers={"Host": "127.0.0.1:8001"},  # missing custom header
        json={"sql": "SELECT 1"},
    )
    assert no_header_resp.status_code == status.HTTP_400_BAD_REQUEST
    assert no_header_resp.json()["error"] == "MISSING_CUSTOM_HEADER"


# ==============================================================================
# 9. Host & Origin Validation Protections Intact
# ==============================================================================


def test_host_and_origin_validation_remains_active_on_routed_api(agent_client):
    """DNS rebinding and unauthorized origins are rejected on routed API endpoints."""
    # Invalid Host header rejected
    resp_host = agent_client.get(
        "/api/health",
        headers={"Host": "attacker.com:8001"},
    )
    assert resp_host.status_code == status.HTTP_400_BAD_REQUEST
    assert resp_host.json()["error"] == "INVALID_HOST"

    # Unauthorized Origin header rejected
    resp_origin = agent_client.get(
        "/api/health",
        headers={"Host": "127.0.0.1:8001", "Origin": "http://evil.com"},
    )
    assert resp_origin.status_code == status.HTTP_403_FORBIDDEN
    assert resp_origin.json()["error"] == "FORBIDDEN_ORIGIN"
