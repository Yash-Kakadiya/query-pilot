"""Offline tests for production frontend serving from QueryPilot Local Agent Daemon (Task 1.34-B1-F28-A9-B9-A1).

Verifies:
1. Production frontend assets served successfully from /app/.
2. SPA client-side fallback for extensionless frontend routes.
3. Missing build directory returns actionable HTTP 503 rather than server error.
4. Incomplete build directory (missing index.html) returns actionable HTTP 503.
5. Missing static asset (with extension or in assets/) returns HTTP 404 without being swallowed by SPA fallback.
6. Path traversal attempts outside static directory fail closed with HTTP 403.
7. API and auth routes (/agent/v1/...) are strictly isolated from SPA fallback.
8. Unknown API endpoints return 404 JSON, never index.html.
9. Security headers (CSP, nosniff, frame-ancestors) applied to static frontend responses.
10. CLI parser and loopback safety enforcement for the optional browser launch flag.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi import status
from fastapi.testclient import TestClient
import pytest

from query_pilot.agent.app import create_agent_app
from query_pilot.agent.cli import _launch_browser_when_ready, build_parser, run_agent_cli
from query_pilot.agent.config import AgentConfig


@pytest.fixture
def mock_static_dir(tmp_path: Path) -> Path:
    """Create a temporary static frontend build directory with valid assets."""
    dist_dir = tmp_path / "frontend_dist"
    dist_dir.mkdir(parents=True, exist_ok=True)

    assets_dir = dist_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)

    # index.html
    index_html = (
        "<!doctype html>\n"
        "<html lang=\"en\">\n"
        "  <head>\n"
        "    <title>QueryPilot</title>\n"
        "    <script type=\"module\" crossorigin src=\"/app/assets/index-test.js\"></script>\n"
        "    <link rel=\"stylesheet\" crossorigin href=\"/app/assets/index-test.css\">\n"
        "  </head>\n"
        "  <body><div id=\"root\">Mock App</div></body>\n"
        "</html>\n"
    )
    (dist_dir / "index.html").write_text(index_html, encoding="utf-8")

    # JavaScript asset
    (assets_dir / "index-test.js").write_text(
        "console.log('QueryPilot Local Agent SPA');",
        encoding="utf-8",
    )

    # CSS asset
    (assets_dir / "index-test.css").write_text(
        "body { margin: 0; background-color: #000; }",
        encoding="utf-8",
    )

    # Favicon asset
    (dist_dir / "favicon.svg").write_text(
        "<svg xmlns=\"http://www.w3.org/2000/svg\"><circle cx=\"5\" cy=\"5\" r=\"5\"/></svg>",
        encoding="utf-8",
    )

    return dist_dir


@pytest.fixture
def agent_client_with_static(mock_static_dir: Path, tmp_path: Path) -> TestClient:
    """Instantiate a TestClient configured with an isolated mock static directory."""
    token_dir = tmp_path / "agent_tokens"
    token_dir.mkdir(parents=True, exist_ok=True)

    config = AgentConfig(
        bind_host="127.0.0.1",
        bind_port=8001,
        token_dir=token_dir,
        static_dir=mock_static_dir,
    )
    app = create_agent_app(config=config)
    return TestClient(app, base_url="http://127.0.0.1:8001")


def test_redirect_root_and_app_to_app_slash(agent_client_with_static: TestClient):
    """GET / and GET /app return 307 redirect to /app/."""
    resp_root = agent_client_with_static.get("/", follow_redirects=False)
    assert resp_root.status_code == status.HTTP_307_TEMPORARY_REDIRECT
    assert resp_root.headers.get("location") == "/app/"

    resp_app = agent_client_with_static.get("/app", follow_redirects=False)
    assert resp_app.status_code == status.HTTP_307_TEMPORARY_REDIRECT
    assert resp_app.headers.get("location") == "/app/"


def test_serve_frontend_index_html(agent_client_with_static: TestClient):
    """GET /app/ serves index.html with HTTP 200 and text/html content-type."""
    resp = agent_client_with_static.get("/app/")
    assert resp.status_code == status.HTTP_200_OK
    assert "text/html" in resp.headers.get("content-type", "")
    assert "<title>QueryPilot</title>" in resp.text
    assert "Mock App" in resp.text


def test_serve_frontend_static_assets(agent_client_with_static: TestClient):
    """GET /app/assets/... serves static JS and CSS files with correct media types."""
    js_resp = agent_client_with_static.get("/app/assets/index-test.js")
    assert js_resp.status_code == status.HTTP_200_OK
    assert "javascript" in js_resp.headers.get("content-type", "")
    assert "QueryPilot Local Agent SPA" in js_resp.text

    css_resp = agent_client_with_static.get("/app/assets/index-test.css")
    assert css_resp.status_code == status.HTTP_200_OK
    assert "text/css" in css_resp.headers.get("content-type", "")
    assert "background-color" in css_resp.text

    svg_resp = agent_client_with_static.get("/app/favicon.svg")
    assert svg_resp.status_code == status.HTTP_200_OK
    assert "image/svg+xml" in svg_resp.headers.get("content-type", "")


def test_spa_client_side_fallback(agent_client_with_static: TestClient):
    """Client-side extensionless routes under /app/ fall back to index.html."""
    for path in ["/app/query", "/app/query/view", "/app/profiles/selection", "/app/settings"]:
        resp = agent_client_with_static.get(path)
        assert resp.status_code == status.HTTP_200_OK
        assert "text/html" in resp.headers.get("content-type", "")
        assert "<div id=\"root\">Mock App</div>" in resp.text


def test_missing_asset_returns_404_not_swallowed_by_spa(agent_client_with_static: TestClient):
    """Missing assets with extensions or within assets/ return 404, not index.html."""
    missing_assets = [
        "/app/assets/nonexistent.js",
        "/app/assets/missing.css",
        "/app/missing-file.png",
        "/app/favicon.ico",
    ]
    for path in missing_assets:
        resp = agent_client_with_static.get(path)
        assert resp.status_code == status.HTTP_404_NOT_FOUND, f"Expected 404 for {path}, got {resp.status_code}"
        assert "Mock App" not in resp.text


def test_missing_build_dir_returns_actionable_503(tmp_path: Path):
    """Missing frontend build directory returns HTTP 503 with actionable compilation guidance and no path leaks."""
    token_dir = tmp_path / "agent_tokens"
    nonexistent_dist = tmp_path / "does_not_exist"

    config = AgentConfig(
        bind_host="127.0.0.1",
        bind_port=8001,
        token_dir=token_dir,
        static_dir=nonexistent_dist,
    )
    app = create_agent_app(config=config)
    client = TestClient(app, base_url="http://127.0.0.1:8001")

    resp = client.get("/app/")
    assert resp.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert "text/html" in resp.headers.get("content-type", "")
    assert "QueryPilot Frontend Build Not Found" in resp.text
    assert "npm run build" in resp.text
    # Verify no absolute system paths leaked into the HTML response
    assert str(nonexistent_dist) not in resp.text
    assert str(tmp_path) not in resp.text


def test_incomplete_build_dir_returns_actionable_503(tmp_path: Path):
    """Build directory present without index.html returns HTTP 503 with actionable guidance and no path leaks."""
    token_dir = tmp_path / "agent_tokens"
    empty_dist = tmp_path / "empty_dist"
    empty_dist.mkdir(parents=True, exist_ok=True)

    config = AgentConfig(
        bind_host="127.0.0.1",
        bind_port=8001,
        token_dir=token_dir,
        static_dir=empty_dist,
    )
    app = create_agent_app(config=config)
    client = TestClient(app, base_url="http://127.0.0.1:8001")

    resp = client.get("/app/")
    assert resp.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert "text/html" in resp.headers.get("content-type", "")
    assert "QueryPilot Frontend Build Incomplete" in resp.text
    assert "index.html" in resp.text
    # Verify no absolute system paths leaked into the HTML response
    assert str(empty_dist) not in resp.text
    assert str(tmp_path) not in resp.text


def test_path_traversal_fails_closed(agent_client_with_static: TestClient, tmp_path: Path):
    """Attempts to traverse outside static_dir are rejected and never leak files."""
    secret_file = tmp_path / "secret.env"
    secret_file.write_text("SUPER_SECRET_TOKEN=12345", encoding="utf-8")

    traversal_paths = [
        "/app/../secret.env",
        "/app/..%2fsecret.env",
        "/app/../../../../etc/passwd",
    ]
    for path in traversal_paths:
        resp = agent_client_with_static.get(path)
        # Should be rejected with 403 or 404, never 200
        assert resp.status_code in {status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND, status.HTTP_400_BAD_REQUEST}
        assert "SUPER_SECRET_TOKEN" not in resp.text


def test_api_routes_isolated_from_spa_fallback(agent_client_with_static: TestClient):
    """Existing API and auth routes are strictly isolated and never hit SPA fallback."""
    # Auth-required endpoint returns 401 JSON, not index.html
    resp_profiles = agent_client_with_static.get("/agent/v1/profiles")
    assert resp_profiles.status_code == status.HTTP_401_UNAUTHORIZED
    assert resp_profiles.headers.get("content-type") == "application/json"
    assert "Mock App" not in resp_profiles.text

    # Unknown API route returns 404 JSON, not index.html
    resp_unknown_api = agent_client_with_static.get("/agent/v1/unknown-endpoint")
    assert resp_unknown_api.status_code == status.HTTP_404_NOT_FOUND
    assert "Mock App" not in resp_unknown_api.text

    # Non-app arbitrary route returns 404 JSON, not index.html
    resp_unknown_root = agent_client_with_static.get("/random-route")
    assert resp_unknown_root.status_code == status.HTTP_404_NOT_FOUND
    assert "Mock App" not in resp_unknown_root.text


def test_security_headers_on_static_response(agent_client_with_static: TestClient):
    """Static asset responses include mandatory security headers (production CSP)."""
    resp = agent_client_with_static.get("/app/")
    assert resp.status_code == status.HTTP_200_OK

    csp = resp.headers.get("content-security-policy", "")
    assert "default-src 'self'" in csp
    assert "style-src 'self' 'unsafe-inline'" in csp
    assert "connect-src 'self' https:;" in csp or "connect-src 'self' https:" in csp
    # Development origins must NOT be present in production CSP
    assert "http://127.0.0.1:8000" not in csp
    assert "http://localhost:8000" not in csp
    assert resp.headers.get("x-frame-options") == "DENY"
    assert resp.headers.get("x-content-type-options") == "nosniff"


def test_security_headers_development_csp(mock_static_dir: Path, tmp_path: Path):
    """When allow_dev_origin=True, development origins are included in connect-src."""
    token_dir = tmp_path / "dev_agent_tokens"
    token_dir.mkdir(parents=True, exist_ok=True)

    config = AgentConfig(
        bind_host="127.0.0.1",
        bind_port=8001,
        allow_dev_origin=True,
        token_dir=token_dir,
        static_dir=mock_static_dir,
    )
    app = create_agent_app(config=config)
    client = TestClient(app, base_url="http://127.0.0.1:8001")

    resp = client.get("/app/")
    assert resp.status_code == status.HTTP_200_OK

    csp = resp.headers.get("content-security-policy", "")
    assert "default-src 'self'" in csp
    assert "http://127.0.0.1:8000" in csp
    assert "http://localhost:8000" in csp
    assert "https:" in csp


def test_launch_browser_when_ready_success():
    """Browser opens when readiness probe confirms server is up."""
    mock_opener = MagicMock()
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value.__enter__.return_value = MagicMock()
        success = _launch_browser_when_ready("http://127.0.0.1:8001/app/", timeout=1.0, opener=mock_opener)

    assert success is True
    mock_opener.assert_called_once_with("http://127.0.0.1:8001/app/")


def test_launch_browser_when_ready_timeout(capsys):
    """Browser does not open when readiness probe times out, and warning is logged to stderr."""
    mock_opener = MagicMock()
    with patch("urllib.request.urlopen", side_effect=Exception("Connection refused")):
        success = _launch_browser_when_ready("http://127.0.0.1:8001/app/", timeout=0.3, opener=mock_opener)

    assert success is False
    mock_opener.assert_not_called()
    captured = capsys.readouterr()
    assert "Browser launch skipped" in captured.err
    assert "http://127.0.0.1:8001/app/" in captured.err


def test_launch_browser_when_ready_server_failure(capsys):
    """Immediate socket failure prevents browser launch without exception escape."""
    mock_opener = MagicMock()
    with patch("urllib.request.urlopen", side_effect=OSError("Socket unavailable")):
        success = _launch_browser_when_ready("http://127.0.0.1:8001/app/", timeout=0.1, opener=mock_opener)

    assert success is False
    mock_opener.assert_not_called()
    captured = capsys.readouterr()
    assert "Browser launch skipped" in captured.err


def test_real_production_frontend_dist_if_present(tmp_path: Path):
    """If real frontend/dist exists, verify default AgentConfig resolves and serves it."""
    repo_root = Path(__file__).resolve().parents[1]
    real_dist = repo_root / "frontend" / "dist"
    if not (real_dist / "index.html").is_file():
        pytest.skip("frontend/dist/index.html not built; skipping live dist check.")

    token_dir = tmp_path / "agent_tokens"
    config = AgentConfig(
        bind_host="127.0.0.1",
        bind_port=8001,
        token_dir=token_dir,
    )
    app = create_agent_app(config=config)
    client = TestClient(app, base_url="http://127.0.0.1:8001")

    resp = client.get("/app/")
    assert resp.status_code == status.HTTP_200_OK
    assert "<title>QueryPilot</title>" in resp.text
    assert "/app/assets/" in resp.text


def test_cli_parser_defaults_and_open_browser_flag():
    """Verify CLI parser defaults and optional --open-browser flag."""
    parser = build_parser()

    defaults = parser.parse_args([])
    assert defaults.host == "127.0.0.1"
    assert defaults.port == 8001
    assert defaults.allow_dev_origin is False
    assert defaults.open_browser is False
    assert defaults.static_dir is None

    with_browser = parser.parse_args(["--open-browser"])
    assert with_browser.open_browser is True

    custom = parser.parse_args(["--host", "localhost", "--port", "8888", "--allow-dev-origin"])
    assert custom.host == "localhost"
    assert custom.port == 8888
    assert custom.allow_dev_origin is True


def test_cli_rejects_non_loopback_host():
    """CLI fails closed when a non-loopback host is provided."""
    exit_code = run_agent_cli(["--host", "0.0.0.0"])
    assert exit_code == 1

    exit_code_public = run_agent_cli(["--host", "192.168.1.100"])
    assert exit_code_public == 1
