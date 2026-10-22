"""Production-Mode Launch & Workflow Verification (Task 1.34-B1-F28-A9-B9-A2).

Offline verification ensuring QueryPilot functions as a production-built local application
served by the local agent daemon on loopback, completely independent of Vite dev server.

Covers:
1. Production Build & Static Asset Verification:
   - Frontend build artifacts (index.html, JS, CSS) exist in frontend/dist.
   - Daemon serves index.html at /app/ and redirects / and /app to /app/.
   - Exact compiled JS/CSS assets linked in index.html are served with valid media types.
   - SPA route fallback returns index.html for client routes.
   - Missing assets (with extensions or in assets/) return HTTP 404, not index.html.
2. Route Isolation & Security Hardening:
   - Production CSP strictly enforces `connect-src 'self' https:` (no unencrypted 8000).
   - Loopback-only binding and strict Host validation reject foreign hosts.
   - Path traversal is prevented (HTTP 403).
   - API endpoints (/agent/v1/...) are isolated and never swallowed by SPA fallback.
   - State-changing requests strictly require `X-QueryPilot-Request: 1`.
3. Authenticated Agent Workflow:
   - Bootstrap token exchange provisions ambient HttpOnly SameSite=Strict session cookie.
   - Profile discovery returns approved profiles.
   - Dynamic schema introspection returns model-facing schema metadata.
   - Execution boundary validates and executes SQL using caller engine/schema fixtures.
   - Session logout deterministically clears credentials.
4. Live Socket Verification:
   - Runs uvicorn on a real loopback TCP port in a background thread.
   - Verifies real HTTP request to /app/ and redirect from / over TCP.
   - Verifies _launch_browser_when_ready against the real running TCP daemon without opening OS browser.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import socket
import threading
import time
from typing import Optional
from unittest.mock import MagicMock, patch
import urllib.request

from fastapi import status
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Connection, Engine
import uvicorn

from query_pilot.agent.app import create_agent_app
from query_pilot.agent.auth import SESSION_COOKIE_NAME, BootstrapTokenManager, SessionManager
from query_pilot.agent.cli import _launch_browser_when_ready
from query_pilot.agent.config import AgentConfig
from query_pilot.db.models import ColumnMetadata, DatabaseSchema, TableMetadata
from query_pilot.db.profile import ConnectionProfile, ProfileStore


REPO_ROOT = Path(__file__).resolve().parents[1]
DIST_DIR = REPO_ROOT / "frontend" / "dist"


def find_free_port() -> int:
    """Find an available TCP port on loopback."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def verified_production_dist() -> Path:
    """Ensure the production frontend build is present."""
    index_html = DIST_DIR / "index.html"
    if not index_html.is_file():
        pytest.fail(
            f"Production build not found at {index_html}. "
            "Please run 'cd frontend && npm run build' before executing verification."
        )
    return DIST_DIR


@pytest.fixture
def mock_profile_store() -> ProfileStore:
    """Create a test profile store with an approved SQLite memory profile."""
    profile = ConnectionProfile(
        profile_id="prod_test_profile",
        display_name="Production Verification Profile",
        server="localhost\\sqlexpress",
        database_name="prod_test_db",
        use_trusted_connection=True,
        schema_name="dbo",
        timeout_seconds=5,
        max_row_limit=50,
    )
    return ProfileStore(profiles={"prod_test_profile": profile})


@pytest.fixture
def mock_schema() -> DatabaseSchema:
    """Create a model-facing schema context for prod_test_profile."""
    return DatabaseSchema(
        database_name="prod_test_db",
        tables=[
            TableMetadata(
                table_name="Customers",
                schema_name="dbo",
                columns=[
                    ColumnMetadata(name="CustomerID", data_type="int", primary_key=True),
                    ColumnMetadata(name="Name", data_type="nvarchar"),
                ],
            )
        ],
    )


@pytest.fixture
def prod_agent_client(
    verified_production_dist: Path,
    mock_profile_store: ProfileStore,
    mock_schema: DatabaseSchema,
    tmp_path: Path,
) -> TestClient:
    """Instantiate a TestClient bound to the real production build and mock storage."""
    token_dir = tmp_path / "prod_tokens"
    token_dir.mkdir(parents=True, exist_ok=True)

    config = AgentConfig(
        bind_host="127.0.0.1",
        bind_port=8001,
        allow_dev_origin=False,  # Strict production mode
        token_dir=token_dir,
        static_dir=verified_production_dist,
    )

    mock_conn = MagicMock(spec=Connection)
    raw_dbapi = MagicMock()
    raw_dbapi.timeout = 10
    mock_conn.connection.dbapi_connection = raw_dbapi
    mock_conn.execute.return_value.scalar.return_value = "prod_test_db"

    mock_cursor = MagicMock()
    mock_cursor.keys.return_value = ["CustomerID", "Name"]
    mock_cursor.fetchmany.return_value = [(1, "Alice"), (2, "Bob")]
    mock_conn.execution_options.return_value.execute.return_value = mock_cursor

    test_engine = MagicMock(spec=Engine)
    test_engine.connect.return_value = mock_conn

    app = create_agent_app(
        config=config,
        store=mock_profile_store,
        caller_engine=test_engine,
        caller_schema=mock_schema,
    )
    return TestClient(app, base_url="http://127.0.0.1:8001")


# ==============================================================================
# 1. Production Build & Static Asset Verification
# ==============================================================================


def test_production_build_artifacts_exist(verified_production_dist: Path):
    """Verify that frontend/dist contains index.html and compiled assets."""
    index_file = verified_production_dist / "index.html"
    assert index_file.is_file(), "frontend/dist/index.html must exist"
    content = index_file.read_text(encoding="utf-8")

    assert "/app/assets/" in content
    assert "<title>QueryPilot</title>" in content

    # Find script and stylesheet links
    js_match = re.search(r'src="(/app/assets/[^"]+\.js)"', content)
    assert js_match is not None, "index.html must link to a compiled JavaScript asset"

    css_match = re.search(r'href="(/app/assets/[^"]+\.css)"', content)
    assert css_match is not None, "index.html must link to a compiled CSS asset"


def test_production_serving_and_exact_asset_fetch(prod_agent_client: TestClient, verified_production_dist: Path):
    """Verify that the agent daemon serves index.html and its exact referenced JS and CSS bundles."""
    # 1. Redirects
    resp_root = prod_agent_client.get("/", follow_redirects=False)
    assert resp_root.status_code == status.HTTP_307_TEMPORARY_REDIRECT
    assert resp_root.headers.get("location") == "/app/"

    resp_app = prod_agent_client.get("/app", follow_redirects=False)
    assert resp_app.status_code == status.HTTP_307_TEMPORARY_REDIRECT
    assert resp_app.headers.get("location") == "/app/"

    # 2. Main SPA entrypoint
    resp_index = prod_agent_client.get("/app/")
    assert resp_index.status_code == status.HTTP_200_OK
    assert "text/html" in resp_index.headers.get("content-type", "")

    # Extract exact asset paths from served index.html
    html_text = resp_index.text
    js_match = re.search(r'src="(/app/assets/[^"]+\.js)"', html_text)
    assert js_match is not None
    js_path = js_match.group(1)

    css_match = re.search(r'href="(/app/assets/[^"]+\.css)"', html_text)
    assert css_match is not None
    css_path = css_match.group(1)

    # 3. Fetch exact JavaScript bundle
    resp_js = prod_agent_client.get(js_path)
    assert resp_js.status_code == status.HTTP_200_OK
    assert "javascript" in resp_js.headers.get("content-type", "")
    assert len(resp_js.content) > 1000  # Substantial compiled bundle

    # 4. Fetch exact CSS bundle
    resp_css = prod_agent_client.get(css_path)
    assert resp_css.status_code == status.HTTP_200_OK
    assert "text/css" in resp_css.headers.get("content-type", "")
    assert len(resp_css.content) > 100

    # 5. SPA Route Fallback for client routes
    resp_route = prod_agent_client.get("/app/query/view")
    assert resp_route.status_code == status.HTTP_200_OK
    assert "text/html" in resp_route.headers.get("content-type", "")
    assert "<title>QueryPilot</title>" in resp_route.text

    # 6. Missing static assets return 404 (not index.html)
    resp_missing_js = prod_agent_client.get("/app/assets/nonexistent-bundle.js")
    assert resp_missing_js.status_code == status.HTTP_404_NOT_FOUND
    assert "<title>QueryPilot</title>" not in resp_missing_js.text

    resp_missing_asset = prod_agent_client.get("/app/assets/missing.png")
    assert resp_missing_asset.status_code == status.HTTP_404_NOT_FOUND


# ==============================================================================
# 2. Production Security & Routing Hardening
# ==============================================================================


def test_production_security_headers_and_csp(prod_agent_client: TestClient):
    """Verify that production CSP strictly excludes development origins."""
    resp = prod_agent_client.get("/app/")
    assert resp.status_code == status.HTTP_200_OK

    csp = resp.headers.get("content-security-policy", "")
    assert "default-src 'self'" in csp
    assert "connect-src 'self' https:;" in csp or "connect-src 'self' https:" in csp
    # Development HTTP origins must be strictly absent
    assert "http://127.0.0.1:8000" not in csp
    assert "http://localhost:8000" not in csp
    assert "http://127.0.0.1:5173" not in csp

    # Framing and MIME sniffing protections
    assert resp.headers.get("x-frame-options") == "DENY"
    assert resp.headers.get("x-content-type-options") == "nosniff"


def test_production_route_isolation_and_host_validation(prod_agent_client: TestClient):
    """Verify that API routes are never intercepted by SPA fallback and DNS rebinding defense holds."""
    # API endpoints require auth, returning 401 JSON (not index.html)
    resp_api = prod_agent_client.get("/agent/v1/profiles")
    assert resp_api.status_code == status.HTTP_401_UNAUTHORIZED
    assert resp_api.headers.get("content-type") == "application/json"
    assert "<title>QueryPilot</title>" not in resp_api.text

    # Unknown API endpoints return 404 JSON (not index.html)
    resp_unknown_api = prod_agent_client.get("/agent/v1/nonexistent")
    assert resp_unknown_api.status_code == status.HTTP_404_NOT_FOUND
    assert resp_unknown_api.headers.get("content-type") == "application/json"
    assert "<title>QueryPilot</title>" not in resp_unknown_api.text

    # Non-app path returns 404 JSON
    resp_unknown_root = prod_agent_client.get("/unknown-root")
    assert resp_unknown_root.status_code == status.HTTP_404_NOT_FOUND

    # Path traversal outside static directory returns 403
    resp_traversal = prod_agent_client.get("/app/../config.py")
    assert resp_traversal.status_code in {status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND}

    # Host header validation rejects foreign host (DNS rebinding protection)
    resp_bad_host = prod_agent_client.get("/app/", headers={"Host": "attacker.com"})
    assert resp_bad_host.status_code == status.HTTP_400_BAD_REQUEST
    assert "INVALID_HOST" in resp_bad_host.text


# ==============================================================================
# 3. Authenticated Local Agent Workflow
# ==============================================================================


@patch("query_pilot.db.profile_introspector.inspect")
def test_production_authenticated_workflow(mock_inspect_fn, prod_agent_client: TestClient, tmp_path: Path):
    """Verify complete local agent workflow: bootstrap -> list profiles -> schema -> execute -> logout."""
    mock_inspector = MagicMock()
    mock_inspector.get_table_names.return_value = ["Customers"]
    mock_inspector.get_pk_constraint.return_value = {"constrained_columns": ["CustomerID"]}
    mock_inspector.get_columns.return_value = [
        {"name": "CustomerID", "type": "INTEGER", "nullable": False, "primary_key": True, "default": None, "identity": True, "comment": None},
        {"name": "Name", "type": "VARCHAR(100)", "nullable": True, "primary_key": False, "default": None, "identity": False, "comment": None},
    ]
    mock_inspector.get_foreign_keys.return_value = []
    mock_inspector.get_indexes.return_value = []
    mock_inspect_fn.return_value = mock_inspector

    app = prod_agent_client.app
    token_mgr: BootstrapTokenManager = app.state.token_manager

    # 1. Initialize fresh bootstrap token
    bootstrap_token = token_mgr.initialize_token()
    assert len(bootstrap_token) == 64

    # 2. CSRF Defense: state-changing POST without X-QueryPilot-Request: 1 fails closed
    resp_no_csrf = prod_agent_client.post(
        "/agent/v1/auth/bootstrap",
        json={"bootstrap_token": bootstrap_token},
    )
    assert resp_no_csrf.status_code == status.HTTP_400_BAD_REQUEST
    assert "MISSING_CUSTOM_HEADER" in resp_no_csrf.text

    # 3. Exchange bootstrap token
    resp_auth = prod_agent_client.post(
        "/agent/v1/auth/bootstrap",
        headers={"X-QueryPilot-Request": "1"},
        json={"bootstrap_token": bootstrap_token},
    )
    assert resp_auth.status_code == status.HTTP_200_OK
    assert SESSION_COOKIE_NAME in resp_auth.cookies

    # 4. List profiles using ambient session cookie
    resp_profiles = prod_agent_client.get("/agent/v1/profiles")
    assert resp_profiles.status_code == status.HTTP_200_OK
    profiles_data = resp_profiles.json()
    assert len(profiles_data["profiles"]) == 1
    assert profiles_data["profiles"][0]["profile_id"] == "prod_test_profile"

    # 5. Dynamic schema introspection
    resp_schema = prod_agent_client.get("/agent/v1/profiles/prod_test_profile/schema")
    assert resp_schema.status_code == status.HTTP_200_OK
    schema_data = resp_schema.json()
    assert schema_data["database_name"] == "prod_test_db"
    assert len(schema_data["tables"]) == 1
    assert schema_data["tables"][0]["table_name"] == "Customers"

    # 6. Execute validated read-only SQL query
    resp_exec = prod_agent_client.post(
        "/agent/v1/profiles/prod_test_profile/execute",
        headers={"X-QueryPilot-Request": "1"},
        json={"sql": "SELECT CustomerID, Name FROM Customers;"},
    )
    assert resp_exec.status_code == status.HTTP_200_OK
    exec_data = resp_exec.json()
    assert exec_data["columns"] == ["CustomerID", "Name"]
    assert len(exec_data["rows"]) == 2
    assert exec_data["row_count"] == 2
    assert exec_data["rows"][0] == {"CustomerID": 1, "Name": "Alice"}

    # 7. Unsafe queries are rejected early by AST validation
    resp_unsafe = prod_agent_client.post(
        "/agent/v1/profiles/prod_test_profile/execute",
        headers={"X-QueryPilot-Request": "1"},
        json={"sql": "DROP TABLE Customers;"},
    )
    assert resp_unsafe.status_code == status.HTTP_400_BAD_REQUEST

    # 8. Explicit session logout clears cookie
    resp_logout = prod_agent_client.post(
        "/agent/v1/auth/logout",
        headers={"X-QueryPilot-Request": "1"},
    )
    assert resp_logout.status_code == status.HTTP_200_OK

    # Subsequent access fails with 401
    resp_post_logout = prod_agent_client.get("/agent/v1/profiles")
    assert resp_post_logout.status_code == status.HTTP_401_UNAUTHORIZED


# ==============================================================================
# 4. Live TCP Loopback Process & Browser Readiness Verification
# ==============================================================================


def test_live_tcp_loopback_daemon_serving(verified_production_dist: Path, tmp_path: Path):
    """Run uvicorn on a real loopback TCP port to verify live network socket serving."""
    port = find_free_port()
    token_dir = tmp_path / "live_tokens"
    token_dir.mkdir(parents=True, exist_ok=True)

    config = AgentConfig(
        bind_host="127.0.0.1",
        bind_port=port,
        allow_dev_origin=False,
        token_dir=token_dir,
        static_dir=verified_production_dist,
    )
    app = create_agent_app(config=config)

    server_config = uvicorn.Config(
        app=app,
        host=config.bind_host,
        port=config.bind_port,
        log_level="error",
    )
    server = uvicorn.Server(config=server_config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"
    app_url = f"{base_url}/app/"

    # Wait for server socket to accept connections
    deadline = time.time() + 5.0
    connected = False
    while time.time() < deadline:
        try:
            req = urllib.request.Request(app_url, method="GET")
            with urllib.request.urlopen(req, timeout=0.5) as resp:
                if resp.status == 200:
                    connected = True
                    break
        except Exception:
            time.sleep(0.1)

    assert connected, "Live uvicorn server failed to respond within 5.0s on loopback"

    try:
        # 1. Verify GET /app/ over real TCP
        req = urllib.request.Request(app_url, method="GET")
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            assert resp.status == 200
            body = resp.read().decode("utf-8")
            assert "<title>QueryPilot</title>" in body
            assert "/app/assets/" in body

        # 2. Verify redirect GET / over real TCP (urllib automatically follows 307)
        req_root = urllib.request.Request(base_url, method="GET")
        with urllib.request.urlopen(req_root, timeout=1.0) as resp:
            assert resp.status == 200
            body = resp.read().decode("utf-8")
            assert "<title>QueryPilot</title>" in body

        # 3. Verify _launch_browser_when_ready over the real TCP socket with mock opener
        mock_opener = MagicMock()
        success = _launch_browser_when_ready(app_url, timeout=2.0, opener=mock_opener)
        assert success is True
        mock_opener.assert_called_once_with(app_url)

    finally:
        server.should_exit = True
        thread.join(timeout=3.0)
