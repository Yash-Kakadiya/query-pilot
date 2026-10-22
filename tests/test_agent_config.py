"""Offline test suite for QueryPilot Agent Configuration Contract (Milestone B12-B).

Verifies:
1. Code defaults when no arguments or environment variables are provided.
2. Environment variable overrides for agent configuration.
3. CLI argument overrides taking strict precedence over environment variables.
4. Fail-closed validation for non-loopback hosts, invalid ports, and out-of-range timeouts.
5. Path resolution contracts for token_dir, static_dir, and profiles_file (future onboarding).
6. Console script entry points (querypilot-agent, querypilot-api) in package metadata.
7. Regression behavior with create_agent_app startup contracts.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Optional
from unittest.mock import patch

from pydantic import ValidationError
import pytest

from query_pilot.agent.app import create_agent_app
from query_pilot.agent.cli import build_parser as build_agent_parser, main as agent_main, run_agent_cli
from query_pilot.agent.config import (
    AgentConfig,
    get_default_profiles_file,
    get_default_static_dir,
    get_default_token_dir,
    is_loopback_host,
)
from query_pilot.api.cli import build_parser as build_api_parser, main as api_main, run_api_cli


# ==============================================================================
# 1. Configuration Defaults
# ==============================================================================


def test_agent_config_defaults(monkeypatch):
    """Verify that AgentConfig defaults match the documented production contract."""
    # Ensure no environment variable overrides interfere
    for var in [
        "QUERYPILOT_AGENT_HOST",
        "QUERYPILOT_AGENT_BIND_HOST",
        "QUERYPILOT_AGENT_PORT",
        "QUERYPILOT_AGENT_ALLOW_DEV_ORIGIN",
        "QUERYPILOT_AGENT_TOKEN_DIR",
        "QUERYPILOT_TOKEN_DIR",
        "QUERYPILOT_AGENT_TOKEN_FILENAME",
        "QUERYPILOT_PROFILES_FILE",
        "QUERYPILOT_AGENT_SESSION_IDLE_SECONDS",
        "QUERYPILOT_AGENT_SESSION_MAX_LIFETIME_SECONDS",
        "QUERYPILOT_AGENT_MAX_REQUEST_BYTES",
        "QUERYPILOT_AGENT_MAX_CONCURRENT_REQUESTS",
        "QUERYPILOT_AGENT_STATIC_DIR",
        "QUERYPILOT_UPSTREAM_API_URL",
        "QUERYPILOT_AGENT_UPSTREAM_API_URL",
        "QUERYPILOT_UPSTREAM_TIMEOUT_SECONDS",
        "QUERYPILOT_AGENT_UPSTREAM_TIMEOUT_SECONDS",
    ]:
        monkeypatch.delenv(var, raising=False)

    config = AgentConfig()

    assert config.bind_host == "127.0.0.1"
    assert config.bind_port == 8001
    assert config.allow_dev_origin is False
    assert config.token_dir == Path.home() / ".query_pilot"
    assert config.token_filename == "agent.token"
    assert config.token_path == Path.home() / ".query_pilot" / "agent.token"
    assert config.profiles_file == Path.home() / ".query_pilot" / "profiles.json"
    assert config.resolved_profiles_file == (Path.home() / ".query_pilot" / "profiles.json").resolve()
    assert config.session_idle_seconds == 3600
    assert config.session_max_lifetime_seconds == 43200
    assert config.max_request_bytes == 65536
    assert config.max_concurrent_requests == 2
    assert config.static_dir is None
    assert config.upstream_api_url == "http://127.0.0.1:8000"
    assert config.upstream_timeout_seconds == 30.0


# ==============================================================================
# 2. Environment Variable Overrides
# ==============================================================================


def test_agent_config_environment_overrides(monkeypatch, tmp_path):
    """Verify that supported environment variables override code defaults."""
    custom_token_dir = tmp_path / "custom_tokens"
    custom_profiles_file = tmp_path / "custom_profiles.json"
    custom_static_dir = tmp_path / "custom_static"

    monkeypatch.setenv("QUERYPILOT_AGENT_HOST", "localhost")
    monkeypatch.setenv("QUERYPILOT_AGENT_PORT", "8888")
    monkeypatch.setenv("QUERYPILOT_AGENT_ALLOW_DEV_ORIGIN", "true")
    monkeypatch.setenv("QUERYPILOT_TOKEN_DIR", str(custom_token_dir))
    monkeypatch.setenv("QUERYPILOT_AGENT_TOKEN_FILENAME", "custom.token")
    monkeypatch.setenv("QUERYPILOT_PROFILES_FILE", str(custom_profiles_file))
    monkeypatch.setenv("QUERYPILOT_AGENT_SESSION_IDLE_SECONDS", "1800")
    monkeypatch.setenv("QUERYPILOT_AGENT_SESSION_MAX_LIFETIME_SECONDS", "21600")
    monkeypatch.setenv("QUERYPILOT_AGENT_MAX_REQUEST_BYTES", "131072")
    monkeypatch.setenv("QUERYPILOT_AGENT_MAX_CONCURRENT_REQUESTS", "5")
    monkeypatch.setenv("QUERYPILOT_AGENT_STATIC_DIR", str(custom_static_dir))
    monkeypatch.setenv("QUERYPILOT_UPSTREAM_API_URL", "http://localhost:9000")
    monkeypatch.setenv("QUERYPILOT_UPSTREAM_TIMEOUT_SECONDS", "45.0")

    config = AgentConfig()

    assert config.bind_host == "localhost"
    assert config.bind_port == 8888
    assert config.allow_dev_origin is True
    assert config.token_dir == custom_token_dir
    assert config.token_filename == "custom.token"
    assert config.token_path == custom_token_dir / "custom.token"
    assert config.profiles_file == custom_profiles_file
    assert config.session_idle_seconds == 1800
    assert config.session_max_lifetime_seconds == 21600
    assert config.max_request_bytes == 131072
    assert config.max_concurrent_requests == 5
    assert config.static_dir == custom_static_dir
    assert config.resolved_static_dir == custom_static_dir.resolve()
    assert config.upstream_api_url == "http://localhost:9000"
    assert config.upstream_timeout_seconds == 45.0


# ==============================================================================
# 3. CLI Argument Precedence over Environment Variables
# ==============================================================================


def test_cli_precedence_over_environment_variables(monkeypatch, tmp_path):
    """Verify that CLI flags take precedence over environment variables."""
    env_token_dir = tmp_path / "env_tokens"
    cli_token_dir = tmp_path / "cli_tokens"
    env_profiles_file = tmp_path / "env_profiles.json"
    cli_profiles_file = tmp_path / "cli_profiles.json"

    # Set environment variables
    monkeypatch.setenv("QUERYPILOT_AGENT_HOST", "localhost")
    monkeypatch.setenv("QUERYPILOT_AGENT_PORT", "8888")
    monkeypatch.setenv("QUERYPILOT_AGENT_ALLOW_DEV_ORIGIN", "false")
    monkeypatch.setenv("QUERYPILOT_TOKEN_DIR", str(env_token_dir))
    monkeypatch.setenv("QUERYPILOT_PROFILES_FILE", str(env_profiles_file))
    monkeypatch.setenv("QUERYPILOT_UPSTREAM_API_URL", "http://localhost:9000")

    # Build parser with env vars active
    parser = build_agent_parser()

    # Pass explicit CLI arguments overriding the environment variables
    args = parser.parse_args([
        "--host", "127.0.0.1",
        "--port", "8002",
        "--allow-dev-origin",
        "--token-dir", str(cli_token_dir),
        "--profiles-file", str(cli_profiles_file),
        "--upstream-api-url", "http://127.0.0.1:8000",
    ])

    assert args.host == "127.0.0.1"
    assert args.port == 8002
    assert args.allow_dev_origin is True
    assert args.token_dir == cli_token_dir
    assert args.profiles_file == cli_profiles_file
    assert args.upstream_api_url == "http://127.0.0.1:8000"

    # Instantiate config from parsed args to verify config-level precedence
    config = AgentConfig(
        bind_host=args.host,
        bind_port=args.port,
        allow_dev_origin=args.allow_dev_origin,
        token_dir=args.token_dir,
        profiles_file=args.profiles_file,
        upstream_api_url=args.upstream_api_url,
    )

    assert config.bind_host == "127.0.0.1"
    assert config.bind_port == 8002
    assert config.allow_dev_origin is True
    assert config.token_dir == cli_token_dir
    assert config.profiles_file == cli_profiles_file
    assert config.upstream_api_url == "http://127.0.0.1:8000"


def test_cli_falls_back_to_environment_variables(monkeypatch, tmp_path):
    """Verify that CLI defaults adopt environment variables when flags are omitted."""
    custom_profiles_file = tmp_path / "env_fallback_profiles.json"
    custom_token_dir = tmp_path / "env_fallback_tokens"

    monkeypatch.setenv("QUERYPILOT_AGENT_HOST", "localhost")
    monkeypatch.setenv("QUERYPILOT_AGENT_PORT", "8005")
    monkeypatch.setenv("QUERYPILOT_AGENT_ALLOW_DEV_ORIGIN", "true")
    monkeypatch.setenv("QUERYPILOT_TOKEN_DIR", str(custom_token_dir))
    monkeypatch.setenv("QUERYPILOT_PROFILES_FILE", str(custom_profiles_file))
    monkeypatch.setenv("QUERYPILOT_UPSTREAM_API_URL", "http://localhost:8000")

    parser = build_agent_parser()
    args = parser.parse_args([])

    assert args.host == "localhost"
    assert args.port == 8005
    assert args.allow_dev_origin is True
    assert args.token_dir == custom_token_dir
    assert args.profiles_file == custom_profiles_file
    assert args.upstream_api_url == "http://localhost:8000"


# ==============================================================================
# 4. Fail-Closed Validation
# ==============================================================================


def test_agent_config_rejects_non_loopback_host():
    """Verify that AgentConfig rejects non-loopback bind hosts."""
    with pytest.raises(ValidationError) as exc:
        AgentConfig(bind_host="0.0.0.0")
    assert "SECURITY VIOLATION" in str(exc.value)

    with pytest.raises(ValidationError) as exc:
        AgentConfig(bind_host="192.168.1.100")
    assert "SECURITY VIOLATION" in str(exc.value)

    with pytest.raises(ValidationError) as exc:
        AgentConfig(bind_host="remote.service.internal")
    assert "SECURITY VIOLATION" in str(exc.value)


def test_agent_config_rejects_invalid_ports():
    """Verify that AgentConfig rejects ports out of 1024-65535 range."""
    with pytest.raises(ValidationError):
        AgentConfig(bind_port=80)  # Privileged port below 1024

    with pytest.raises(ValidationError):
        AgentConfig(bind_port=70000)  # Port above 65535


def test_agent_config_rejects_unencrypted_non_loopback_upstream():
    """Verify that AgentConfig rejects unencrypted HTTP upstream URLs on public hosts."""
    with pytest.raises(ValidationError) as exc:
        AgentConfig(upstream_api_url="http://external-api.example.com")
    assert "SECURITY VIOLATION" in str(exc.value)

    # Valid loopback HTTP passes
    cfg_loopback = AgentConfig(upstream_api_url="http://127.0.0.1:8000")
    assert cfg_loopback.upstream_api_url == "http://127.0.0.1:8000"

    # Valid HTTPS on any host passes
    cfg_https = AgentConfig(upstream_api_url="https://api.querypilot.internal")
    assert cfg_https.upstream_api_url == "https://api.querypilot.internal"


def test_agent_config_rejects_invalid_timeouts():
    """Verify that AgentConfig rejects out-of-range timeouts."""
    with pytest.raises(ValidationError):
        AgentConfig(upstream_timeout_seconds=0.5)  # ge=1.0

    with pytest.raises(ValidationError):
        AgentConfig(upstream_timeout_seconds=300.0)  # le=120.0


def test_agent_config_rejects_empty_paths():
    """Verify that AgentConfig rejects empty profile or token paths."""
    with pytest.raises(ValidationError):
        AgentConfig(profiles_file=Path(""))

    with pytest.raises(ValidationError):
        AgentConfig(token_dir=Path(""))


def test_env_var_invalid_integer_raises(monkeypatch):
    """Verify that an invalid integer in environment variable fails fast."""
    monkeypatch.setenv("QUERYPILOT_AGENT_PORT", "not_a_number")
    with pytest.raises(ValueError) as exc:
        AgentConfig()
    assert "must be a valid integer" in str(exc.value)


def test_env_var_invalid_boolean_raises(monkeypatch):
    """Verify that an invalid boolean in environment variable fails fast."""
    monkeypatch.setenv("QUERYPILOT_AGENT_ALLOW_DEV_ORIGIN", "maybe")
    with pytest.raises(ValueError) as exc:
        AgentConfig()
    assert "must be a boolean" in str(exc.value)


# ==============================================================================
# 5. CLI Entry Points & Argument Parsing
# ==============================================================================


def test_agent_cli_help_output(capsys):
    """Verify that agent CLI help renders without error."""
    parser = build_agent_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["--help"])
    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "--host" in captured.out
    assert "--port" in captured.out
    assert "--allow-dev-origin" in captured.out
    assert "--token-dir" in captured.out
    assert "--profiles-file" in captured.out
    assert "--upstream-api-url" in captured.out


def test_api_cli_help_output(capsys):
    """Verify that API CLI help renders without error."""
    parser = build_api_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["--help"])
    assert exc.value.code == 0
    captured = capsys.readouterr()
    assert "--host" in captured.out
    assert "--port" in captured.out
    assert "--reload" in captured.out


def test_api_cli_defaults(monkeypatch):
    """Verify API CLI defaults and environment variable overrides."""
    monkeypatch.delenv("QUERYPILOT_API_HOST", raising=False)
    monkeypatch.delenv("QUERYPILOT_API_PORT", raising=False)

    parser = build_api_parser()
    args = parser.parse_args([])
    assert args.host == "127.0.0.1"
    assert args.port == 8000
    assert args.reload is False

    # With environment variables
    monkeypatch.setenv("QUERYPILOT_API_HOST", "0.0.0.0")
    monkeypatch.setenv("QUERYPILOT_API_PORT", "9000")
    parser_env = build_api_parser()
    args_env = parser_env.parse_args([])
    assert args_env.host == "0.0.0.0"
    assert args_env.port == 9000

    # CLI override wins
    args_override = parser_env.parse_args(["--host", "127.0.0.1", "--port", "8000", "--reload"])
    assert args_override.host == "127.0.0.1"
    assert args_override.port == 8000
    assert args_override.reload is True


def test_pyproject_contains_console_scripts():
    """Verify that pyproject.toml defines querypilot-agent and querypilot-api console scripts."""
    pyproject_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    assert pyproject_path.exists()
    content = pyproject_path.read_text(encoding="utf-8")

    assert "[project.scripts]" in content
    assert 'querypilot-agent = "query_pilot.agent.cli:main"' in content
    assert 'querypilot-api = "query_pilot.api.cli:main"' in content


# ==============================================================================
# 6. Integration Contract Regression
# ==============================================================================


def test_create_agent_app_preserves_config_contract(tmp_path):
    """Verify that create_agent_app preserves the configured profiles_file on app.state."""
    profiles_path = tmp_path / "profiles.json"
    config = AgentConfig(profiles_file=profiles_path)

    app = create_agent_app(config=config)
    assert app.state.config.profiles_file == profiles_path
    assert app.state.config.resolved_profiles_file == profiles_path.resolve()


# ==============================================================================
# 7. Packaging & Static Asset Resolution
# ==============================================================================


def test_pyproject_contains_runtime_dependencies():
    """Verify that pyproject.toml defines required runtime dependencies and package data."""
    import tomllib

    pyproject_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    assert pyproject_path.exists()
    data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))

    deps = data.get("project", {}).get("dependencies", [])
    expected_deps = [
        "fastapi>=0.110.0",
        "google-genai>=0.1.0",
        "httpx>=0.27.0",
        "langgraph>=1.0.0",
        "langsmith>=0.1.0",
        "pydantic>=2.7.0",
        "pydantic-settings>=2.2.0",
        "pyodbc>=5.1.0",
        "python-dotenv>=1.0.1",
        "sqlalchemy>=2.0.0",
        "sqlglot>=25.0.0",
        "uvicorn>=0.29.0",
    ]
    assert deps == expected_deps

    optional_deps = data.get("project", {}).get("optional-dependencies", {})
    assert "test" in optional_deps
    assert optional_deps["test"] == ["pytest>=8.0.0"]

    tool_setuptools = data.get("tool", {}).get("setuptools", {})
    package_data = tool_setuptools.get("package-data", {})
    assert package_data.get("query_pilot.agent") == ["static/**/*"]


def test_get_default_static_dir_prefers_wheel_package_static(tmp_path):
    """Verify that get_default_static_dir prefers internal package static dir if index.html exists."""
    fake_pkg_agent = tmp_path / "site-packages" / "query_pilot" / "agent"
    fake_pkg_agent.mkdir(parents=True)
    fake_config_file = fake_pkg_agent / "config.py"
    fake_config_file.touch()

    fake_pkg_static = fake_pkg_agent / "static"
    fake_pkg_static.mkdir()
    (fake_pkg_static / "index.html").write_text("<html>Package</html>", encoding="utf-8")

    with patch("query_pilot.agent.config.__file__", str(fake_config_file)):
        resolved = get_default_static_dir()
        assert resolved == fake_pkg_static


def test_get_default_static_dir_falls_back_to_repo_checkout(tmp_path):
    """Verify that get_default_static_dir falls back to repo frontend/dist when package static is absent."""
    fake_repo = tmp_path / "query-pilot"
    fake_pkg_agent = fake_repo / "src" / "query_pilot" / "agent"
    fake_pkg_agent.mkdir(parents=True)
    fake_config_file = fake_pkg_agent / "config.py"
    fake_config_file.touch()

    # Package static does not exist, but repo frontend/dist does
    fake_dist = fake_repo / "frontend" / "dist"
    fake_dist.mkdir(parents=True)
    (fake_dist / "index.html").write_text("<html>Repo</html>", encoding="utf-8")

    with patch("query_pilot.agent.config.__file__", str(fake_config_file)):
        resolved = get_default_static_dir()
        assert resolved == fake_dist


def test_get_default_static_dir_fallback_when_neither_exists(tmp_path):
    """Verify fallback behavior when neither directory contains index.html."""
    fake_repo = tmp_path / "empty-layout"
    fake_pkg_agent = fake_repo / "src" / "query_pilot" / "agent"
    fake_pkg_agent.mkdir(parents=True)
    fake_config_file = fake_pkg_agent / "config.py"
    fake_config_file.touch()

    with patch("query_pilot.agent.config.__file__", str(fake_config_file)):
        resolved = get_default_static_dir()
        assert resolved == fake_repo / "frontend" / "dist"

