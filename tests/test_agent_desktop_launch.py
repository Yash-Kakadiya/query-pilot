"""Tests for B26-B1 Python Agent Desktop Launch Support.

Verifies:
1. AgentConfig accepts port 0 (OS-assigned ephemeral) while preserving default (8001) and rejecting invalid ports.
2. CLI accepts --desktop / --desktop-mode flag and routes to desktop sidecar launch.
3. Desktop launch mode binds explicitly to 127.0.0.1:0 and passes pre-bound socket to Uvicorn.
4. Machine-readable QUERYPILOT_READY handshake is emitted to stdout only after successful startup.
5. Handshake format contains strictly status, bound port, and pid, with zero secrets or paths.
6. Startup failures (socket bind error, app initialization error) never emit a ready handshake.
7. Diagnostic and access logs are routed to stderr, keeping stdout clean for the handshake.
8. End-to-end loopback request and clean shutdown lifecycle without socket leaks.
"""

from __future__ import annotations

import io
import json
import os
import socket
import sys
import threading
import time
from typing import Any, Optional
import urllib.request
from unittest.mock import MagicMock, patch

from pydantic import ValidationError
import pytest

from query_pilot.agent.cli import (
    DESKTOP_READY_PREFIX,
    _get_desktop_agent_server_class,
    build_parser,
    emit_desktop_ready_handshake,
    run_agent_cli,
    run_desktop_sidecar,
)
from query_pilot.agent.config import AgentConfig


class TestAgentConfigPortValidation:
    """Test port configuration and validation in AgentConfig."""

    def test_default_port_behavior_unchanged(self):
        """Verify default bind_port is 8001 when unset."""
        cfg = AgentConfig()
        assert cfg.bind_port == 8001
        assert cfg.desktop_mode is False

    def test_port_0_accepted_for_ephemeral_binding(self):
        """Verify port 0 is accepted by AgentConfig for ephemeral binding."""
        cfg = AgentConfig(bind_port=0)
        assert cfg.bind_port == 0

    def test_desktop_mode_flag_accepted(self):
        """Verify desktop_mode flag can be set explicitly."""
        cfg = AgentConfig(desktop_mode=True)
        assert cfg.desktop_mode is True

    @pytest.mark.parametrize("invalid_port", [-1, 80, 443, 1023, 65536, 70000])
    def test_invalid_ports_rejected(self, invalid_port: int):
        """Verify ports outside 1024-65535 (except 0) are rejected."""
        with pytest.raises(ValidationError):
            AgentConfig(bind_port=invalid_port)

    @pytest.mark.parametrize("valid_port", [0, 1024, 8001, 8080, 65535])
    def test_valid_ports_accepted(self, valid_port: int):
        """Verify valid ports including 0 and boundaries are accepted."""
        cfg = AgentConfig(bind_port=valid_port)
        assert cfg.bind_port == valid_port


class TestAgentCliParser:
    """Test CLI argument parsing for desktop launch mode."""

    def test_cli_default_parser_values(self):
        """Verify CLI parser defaults to standard browser mode on port 8001."""
        parser = build_parser()
        args = parser.parse_args([])
        assert args.port == 8001
        assert args.desktop_mode is False
        assert args.open_browser is False

    def test_cli_desktop_mode_flags(self):
        """Verify --desktop and --desktop-mode flags set desktop_mode to True."""
        parser = build_parser()

        args1 = parser.parse_args(["--desktop"])
        assert args1.desktop_mode is True

        args2 = parser.parse_args(["--desktop-mode"])
        assert args2.desktop_mode is True

    def test_cli_port_0_flag(self):
        """Verify --port 0 is parsed correctly by CLI."""
        parser = build_parser()
        args = parser.parse_args(["--port", "0"])
        assert args.port == 0


class TestDesktopHandshakeFormat:
    """Test emission and format of machine-readable readiness handshake."""

    def test_handshake_format_and_fields(self, monkeypatch: pytest.MonkeyPatch):
        """Verify handshake contains strictly status, port, and pid, with no secrets or paths."""
        captured = io.StringIO()
        monkeypatch.setattr(sys, "stdout", captured)

        emit_desktop_ready_handshake(port=54321, pid=9999)

        output = captured.getvalue()
        assert output.startswith(DESKTOP_READY_PREFIX)

        payload_str = output[len(DESKTOP_READY_PREFIX) :].strip()
        data = json.loads(payload_str)

        assert data["status"] == "ready"
        assert data["port"] == 54321
        assert data["pid"] == 9999

        # Security invariant: zero sensitive fields leaked in handshake
        forbidden_keys = {"token", "token_dir", "token_path", "database_path", "profiles", "password", "secret"}
        for k in forbidden_keys:
            assert k not in data


class TestDesktopServerStartupLifecycle:
    """Test Uvicorn DesktopAgentServer startup lifecycle and failure behaviors."""

    def test_handshake_emitted_only_after_successful_startup(self):
        """Verify handshake is emitted only once startup completes and self.started is True."""
        import uvicorn
        from fastapi import FastAPI

        app = FastAPI()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

        handshake_calls = []

        def mock_on_ready(p: int, pid: int):
            handshake_calls.append((p, pid))

        server_cls = _get_desktop_agent_server_class()
        cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        server = server_cls(cfg, on_ready=mock_on_ready)

        # Before startup: no handshake
        assert len(handshake_calls) == 0

        # Run startup in thread
        t = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
        t.start()

        deadline = time.time() + 5.0
        while len(handshake_calls) == 0 and time.time() < deadline:
            time.sleep(0.05)

        try:
            assert len(handshake_calls) == 1
            assert handshake_calls[0][0] == port
            assert handshake_calls[0][1] == os.getpid()
            assert server.started is True
        finally:
            server.should_exit = True
            t.join(timeout=3.0)
            sock.close()

    def test_startup_failure_never_emits_handshake(self, monkeypatch: pytest.MonkeyPatch, tmp_path):
        """Verify that server startup failure does not emit a readiness handshake."""
        captured_stdout = io.StringIO()
        captured_stderr = io.StringIO()
        monkeypatch.setattr(sys, "stdout", captured_stdout)
        monkeypatch.setattr(sys, "stderr", captured_stderr)

        handshake_calls = []

        def mock_on_ready(p: int, pid: int):
            handshake_calls.append((p, pid))

        # Pass a mock create_agent_app that raises an error to simulate initialization failure
        config = AgentConfig(token_dir=tmp_path)

        with patch("query_pilot.agent.cli.create_agent_app", side_effect=RuntimeError("Simulated app init failure")):
            exit_code = run_desktop_sidecar(config=config, on_ready=mock_on_ready)

        assert exit_code == 1
        assert len(handshake_calls) == 0
        assert DESKTOP_READY_PREFIX not in captured_stdout.getvalue()
        assert "Simulated app init failure" in captured_stderr.getvalue()

    def test_socket_binding_failure_handled_cleanly(self, monkeypatch: pytest.MonkeyPatch, tmp_path):
        """Verify socket binding failure reports to stderr and exits 1 without handshake."""
        captured_stdout = io.StringIO()
        captured_stderr = io.StringIO()
        monkeypatch.setattr(sys, "stdout", captured_stdout)
        monkeypatch.setattr(sys, "stderr", captured_stderr)

        config = AgentConfig(token_dir=tmp_path)

        with patch("socket.socket.bind", side_effect=OSError("Address already in use")):
            exit_code = run_desktop_sidecar(config=config)

        assert exit_code == 1
        assert DESKTOP_READY_PREFIX not in captured_stdout.getvalue()
        assert "Failed to bind ephemeral socket" in captured_stderr.getvalue()


class TestDesktopEndToEndExecution:
    """End-to-end test of desktop sidecar launch, HTTP request handling, and clean shutdown."""

    def test_desktop_sidecar_serves_requests_and_shuts_down_cleanly(self, tmp_path):
        """Verify desktop sidecar starts on ephemeral port, answers HTTP requests, and cleans up socket."""
        token_dir = tmp_path / "tokens"
        token_dir.mkdir()
        profiles_file = tmp_path / "profiles.json"
        profiles_file.write_text("{}", encoding="utf-8")

        config = AgentConfig(
            token_dir=token_dir,
            profiles_file=profiles_file,
            desktop_mode=True,
        )

        ready_event = threading.Event()
        captured_port = []

        def on_ready(port: int, pid: int):
            captured_port.append(port)
            ready_event.set()

        # Run desktop sidecar in a background thread
        server_instance = None

        server_cls = _get_desktop_agent_server_class()
        orig_init = server_cls.__init__

        def intercepted_init(self, *args, **kwargs):
            nonlocal server_instance
            orig_init(self, *args, **kwargs)
            server_instance = self

        server_cls.__init__ = intercepted_init

        t = threading.Thread(
            target=lambda: run_desktop_sidecar(config=config, on_ready=on_ready),
            daemon=True,
        )
        t.start()

        try:
            assert ready_event.wait(timeout=6.0), "Desktop sidecar did not emit readiness signal"
            bound_port = captured_port[0]
            assert bound_port > 1024

            # Make live loopback request to frontend static mount endpoint
            req = urllib.request.Request(f"http://127.0.0.1:{bound_port}/app/")
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                assert resp.status in {200, 503}  # 200 if static files exist, 503 HTML if static_dir unbuilt
        finally:
            if server_instance is not None:
                server_instance.should_exit = True
            t.join(timeout=4.0)

        # Verify port is released after shutdown (using SO_REUSEADDR to handle TCP TIME_WAIT)
        test_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        test_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            test_sock.bind(("127.0.0.1", bound_port))
            # Successfully rebound -> socket was properly closed
            test_sock.close()
        except OSError:
            pytest.fail(f"Socket port {bound_port} was leaked after server shutdown")


class TestDesktopTokenIsolation:
    """Test bootstrap token isolation and lifecycle in desktop mode."""

    def test_stale_token_in_default_dir_is_not_adopted_by_isolated_launch(self, tmp_path):
        """Verify an existing token in an older/default directory is completely ignored."""
        from query_pilot.agent.app import create_agent_app
        from query_pilot.agent.auth import BootstrapTokenManager
        from fastapi.testclient import TestClient

        default_dir = tmp_path / "old_default_dir"
        default_dir.mkdir()
        stale_mgr = BootstrapTokenManager(token_dir=default_dir)
        stale_token = stale_mgr.initialize_token()
        assert (default_dir / "agent.token").is_file()

        # Launch agent pointing to brand new isolated directory
        isolated_dir = tmp_path / "querypilot_token_isolated"
        isolated_dir.mkdir()
        config = AgentConfig(
            token_dir=isolated_dir,
            bind_port=8001,
            desktop_mode=True,
        )

        app = create_agent_app(config=config)
        with TestClient(app, base_url="http://127.0.0.1:8001") as client:
            # Verify the isolated directory has its own distinct token file
            isolated_token_file = isolated_dir / "agent.token"
            assert isolated_token_file.is_file()
            isolated_token = isolated_token_file.read_text(encoding="utf-8").strip()

            assert isolated_token != stale_token, "Isolated launch must generate fresh token, not adopt stale token"

            # Attempting bootstrap with stale token must be rejected
            resp = client.post(
                "/agent/v1/auth/bootstrap",
                json={"bootstrap_token": stale_token},
                headers={"X-QueryPilot-Request": "1"},
            )
            assert resp.status_code == 401

            # Bootstrapping with fresh isolated token must succeed
            resp_valid = client.post(
                "/agent/v1/auth/bootstrap",
                json={"bootstrap_token": isolated_token},
                headers={"X-QueryPilot-Request": "1"},
            )
            assert resp_valid.status_code == 200
            assert "qpt_session" in resp_valid.cookies

            # Verify token file was safely wiped and consumed from disk
            assert not isolated_token_file.exists(), "Bootstrap token file must be deleted upon successful consumption"

    def test_consecutive_isolated_launches_use_independent_locations_and_tokens(self, tmp_path):
        """Verify two consecutive desktop launches receive independent directories and tokens."""
        from query_pilot.agent.auth import BootstrapTokenManager

        dir1 = tmp_path / "querypilot_token_1"
        dir2 = tmp_path / "querypilot_token_2"
        dir1.mkdir()
        dir2.mkdir()

        mgr1 = BootstrapTokenManager(token_dir=dir1)
        token1 = mgr1.adopt_or_initialize_token()

        mgr2 = BootstrapTokenManager(token_dir=dir2)
        token2 = mgr2.adopt_or_initialize_token()

        assert dir1 != dir2
        assert token1 != token2
        assert len(token1) == 64
        assert len(token2) == 64

    def test_failed_startup_or_error_does_not_expose_token_contents(self, tmp_path, monkeypatch):
        """Verify that errors and startup rejections never leak the secret token string to stderr."""
        from query_pilot.agent.app import create_agent_app
        from fastapi.testclient import TestClient

        isolated_dir = tmp_path / "querypilot_token_err"
        isolated_dir.mkdir()
        config = AgentConfig(token_dir=isolated_dir, bind_port=8001, desktop_mode=True)
        app = create_agent_app(config=config)

        stderr_capture = io.StringIO()
        monkeypatch.setattr(sys, "stderr", stderr_capture)

        with TestClient(app, base_url="http://127.0.0.1:8001") as client:
            token = (isolated_dir / "agent.token").read_text(encoding="utf-8").strip()

            # Send invalid bootstrap token
            resp = client.post(
                "/agent/v1/auth/bootstrap",
                json={"bootstrap_token": "invalid_wrong_token_value_attempt"},
                headers={"X-QueryPilot-Request": "1"},
            )
            assert resp.status_code == 401
            # Check stderr did not leak either the valid token or the candidate token
            stderr_text = stderr_capture.getvalue()
            assert token not in stderr_text
            assert "invalid_wrong_token_value_attempt" not in stderr_text
