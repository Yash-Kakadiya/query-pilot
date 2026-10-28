"""Tests for operator CLI recovery and bootstrap token adoption lifecycle (Milestone B18-S1).

Covers:
1. 'auth rotate' success, file creation, output instructions, and exit code 0.
2. '--show-token' displays raw token on stdout; default 'auth rotate' suppresses it.
3. Legacy '--rotate-token' flag compatibility and identical dispatch.
4. 'auth info' never discloses token bytes and handles missing, uninitialized, or present files.
5. Token directory override (--token-dir) and configuration precedence.
6. Symlink/reparse-point rejection without modifying the symlink target.
7. POSIX permission and ownership checks where supported.
8. Windows ACL verification through mocks and platform checks.
9. Provisioning failure, partial write, permission-hardening failure, and sanitized errors.
10. Daemon startup safely adopts a valid CLI-provisioned token after restart.
11. Daemon startup fails closed on malformed or non-compliant token files without silently replacing them.
12. Successful bootstrap consumes the adopted token; subsequent replay fails.
13. Concurrent bootstrap requests yield exactly one successful consumption.
14. In-memory session established before restart is no longer valid after restart.
15. HTTP token rotation still requires a valid authenticated session.
16. Existing profile CLI behavior remains compatible.
"""

from __future__ import annotations

import concurrent.futures
import os
from pathlib import Path
import platform
import stat
import subprocess
from unittest.mock import patch

from fastapi import status
from fastapi.testclient import TestClient
import pytest

from query_pilot.agent.app import create_agent_app
from query_pilot.agent.auth import (
    SESSION_COOKIE_NAME,
    BootstrapTokenManager,
    BootstrapTokenProvisioningError,
    SessionManager,
)
from query_pilot.agent.cli import run_agent_cli
from query_pilot.agent.config import AgentConfig
from query_pilot.db.profile import ConnectionProfile, ProfileStore


@pytest.fixture
def temp_token_dir(tmp_path: Path) -> Path:
    """Provide an isolated temporary token directory."""
    token_dir = tmp_path / "agent_tokens"
    token_dir.mkdir(parents=True, exist_ok=True)
    return token_dir


@pytest.fixture
def mock_windows_acl():
    """Mock subprocess.run for icacls on Windows/POSIX test harnesses."""
    proc = subprocess.CompletedProcess(
        args=["icacls"],
        returncode=0,
        stdout="Successfully processed 1 files.\nNT AUTHORITY\\SYSTEM:(R,W)\n",
        stderr="",
    )
    with patch("subprocess.run", return_value=proc):
        with patch("query_pilot.agent.auth._get_active_windows_trustee", return_value="NT AUTHORITY\\SYSTEM"):
            yield proc


def test_auth_rotate_success(temp_token_dir: Path, mock_windows_acl, capsys):
    """1. 'auth rotate' creates valid token file, outputs instructions, suppresses token, exits 0."""
    exit_code = run_agent_cli(["auth", "rotate", "--token-dir", str(temp_token_dir)])
    assert exit_code == 0

    token_file = temp_token_dir / "agent.token"
    assert token_file.exists()
    assert token_file.is_file()

    token_content = token_file.read_text(encoding="utf-8").strip()
    assert len(token_content) == 64
    int(token_content, 16)  # Valid hex

    captured = capsys.readouterr()
    assert "SUCCESS: Bootstrap token provisioned successfully" in captured.out
    assert str(token_file) in captured.out
    assert "Restart the QueryPilot local agent daemon" in captured.out
    # Raw token secret is suppressed by default
    assert token_content not in captured.out
    assert "Token:" not in captured.out


def test_auth_rotate_show_token(temp_token_dir: Path, mock_windows_acl, capsys):
    """2. '--show-token' displays the raw token secret on stdout."""
    exit_code = run_agent_cli(["auth", "rotate", "--token-dir", str(temp_token_dir), "--show-token"])
    assert exit_code == 0

    token_file = temp_token_dir / "agent.token"
    token_content = token_file.read_text(encoding="utf-8").strip()

    captured = capsys.readouterr()
    assert f"Token: {token_content}" in captured.out


def test_legacy_rotate_token_flag(temp_token_dir: Path, mock_windows_acl, capsys):
    """3. Legacy '--rotate-token' flag dispatches to the same handler."""
    exit_code = run_agent_cli(["--rotate-token", "--token-dir", str(temp_token_dir)])
    assert exit_code == 0

    token_file = temp_token_dir / "agent.token"
    assert token_file.exists()
    captured = capsys.readouterr()
    assert "SUCCESS: Bootstrap token provisioned successfully" in captured.out
    assert "Restart the QueryPilot local agent daemon" in captured.out


def test_auth_info_diagnostic_output(temp_token_dir: Path, mock_windows_acl, capsys):
    """4. 'auth info' reports file presence and diagnostics without revealing token contents."""
    # When file is missing
    exit_code = run_agent_cli(["auth", "info", "--token-dir", str(temp_token_dir)])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Token File Status:  Not present" in captured.out
    assert "DIAGNOSTIC NOTICE" in captured.out

    # After file is provisioned
    run_agent_cli(["auth", "rotate", "--token-dir", str(temp_token_dir)])
    capsys.readouterr()  # Clear buffer

    exit_code = run_agent_cli(["auth", "info", "--token-dir", str(temp_token_dir)])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Token File Status:  Present on disk" in captured.out
    assert "Permissions Status:" in captured.out

    # Ensure token contents never leaked
    token_content = (temp_token_dir / "agent.token").read_text(encoding="utf-8").strip()
    assert token_content not in captured.out


def test_token_dir_override_precedence(tmp_path: Path, mock_windows_acl):
    """5. Explicit --token-dir CLI flag overrides default and environment variable."""
    env_dir = tmp_path / "env_token_dir"
    cli_dir = tmp_path / "cli_token_dir"

    with patch.dict(os.environ, {"QUERYPILOT_TOKEN_DIR": str(env_dir)}):
        exit_code = run_agent_cli(["auth", "rotate", "--token-dir", str(cli_dir)])
        assert exit_code == 0
        assert (cli_dir / "agent.token").exists()
        assert not (env_dir / "agent.token").exists()


def test_symlink_rejection_without_modifying_target(temp_token_dir: Path, capsys):
    """6. Symlink/reparse-point at token path is rejected without modifying target."""
    target_file = temp_token_dir / "canary.txt"
    target_file.write_text("precious_operator_data", encoding="utf-8")

    with patch.object(Path, "is_symlink", return_value=True):
        exit_code = run_agent_cli(["auth", "rotate", "--token-dir", str(temp_token_dir)])
        assert exit_code == 1

    captured = capsys.readouterr()
    assert "ERROR: Failed to provision bootstrap token" in captured.err
    assert "SECURITY VIOLATION" in captured.err
    assert target_file.read_text(encoding="utf-8") == "precious_operator_data"


def test_posix_permission_hardening(temp_token_dir: Path):
    """7. On POSIX, token file is created with 0600 mode and current UID."""
    if platform.system() == "Windows":
        pytest.skip("POSIX permission test skipped on Windows")

    manager = BootstrapTokenManager(token_dir=temp_token_dir)
    manager.initialize_token()
    st = manager.token_path.stat()
    assert (st.st_mode & 0o777) == (stat.S_IRUSR | stat.S_IWUSR)
    assert st.st_uid == os.getuid()


def test_windows_acl_hardening_failure(temp_token_dir: Path, capsys):
    """8 & 9. Windows ACL failure aborts cleanly, fails closed, and cleans up partial file."""
    mock_fail_proc = subprocess.CompletedProcess(
        args=["icacls"],
        returncode=5,
        stdout="",
        stderr="Access is denied.",
    )
    with patch("subprocess.run", return_value=mock_fail_proc):
        with patch("query_pilot.agent.auth._get_active_windows_trustee", return_value="DESKTOP\\User"):
            exit_code = run_agent_cli(["auth", "rotate", "--token-dir", str(temp_token_dir)])
            assert exit_code == 1

    captured = capsys.readouterr()
    assert "ERROR: Failed to provision bootstrap token" in captured.err
    assert "Windows ACL hardening via icacls failed with exit code 5" in captured.err
    # Partial file cleaned up fail-closed
    assert not (temp_token_dir / "agent.token").exists()


def test_daemon_startup_adopts_valid_cli_token(temp_token_dir: Path, mock_windows_acl):
    """10. Daemon startup adopts a pre-existing, validly provisioned CLI token on disk."""
    # 1. Provision via CLI
    exit_code = run_agent_cli(["auth", "rotate", "--token-dir", str(temp_token_dir)])
    assert exit_code == 0
    cli_token = (temp_token_dir / "agent.token").read_text(encoding="utf-8").strip()

    # 2. Start daemon (simulate fresh restart)
    config = AgentConfig(token_dir=temp_token_dir, max_concurrent_requests=10)
    token_mgr = BootstrapTokenManager(token_dir=temp_token_dir)
    app = create_agent_app(config=config, token_manager=token_mgr)

    with TestClient(app, base_url="http://127.0.0.1:8001") as client:
        # In-memory token matches CLI token
        assert token_mgr.has_active_token
        with token_mgr._lock:
            assert token_mgr._token == cli_token

        # Bootstrap succeeds with the adopted token
        resp = client.post(
            "/agent/v1/auth/bootstrap",
            headers={"X-QueryPilot-Request": "1"},
            json={"bootstrap_token": cli_token},
        )
        assert resp.status_code == status.HTTP_200_OK
        assert SESSION_COOKIE_NAME in resp.cookies


def test_daemon_startup_fails_closed_on_malformed_token_file(temp_token_dir: Path, mock_windows_acl):
    """11. Startup fails closed on malformed existing token files without deleting them."""
    token_path = temp_token_dir / "agent.token"
    token_path.write_text("malformed_short_token", encoding="utf-8")

    config = AgentConfig(token_dir=temp_token_dir, max_concurrent_requests=10)
    token_mgr = BootstrapTokenManager(token_dir=temp_token_dir)
    app = create_agent_app(config=config, token_manager=token_mgr)

    # Startup lifespan raises BootstrapTokenProvisioningError
    with pytest.raises(BootstrapTokenProvisioningError, match="malformed or invalid format"):
        with TestClient(app, base_url="http://127.0.0.1:8001"):
            pass

    # Malformed file was NOT silently deleted or overwritten
    assert token_path.exists()
    assert token_path.read_text(encoding="utf-8") == "malformed_short_token"


def test_bootstrap_consumes_token_and_replay_fails(temp_token_dir: Path, mock_windows_acl):
    """12. Bootstrap consumes token from memory and unlinks file; replay fails with 401."""
    run_agent_cli(["auth", "rotate", "--token-dir", str(temp_token_dir)])
    cli_token = (temp_token_dir / "agent.token").read_text(encoding="utf-8").strip()

    config = AgentConfig(token_dir=temp_token_dir, max_concurrent_requests=10)
    token_mgr = BootstrapTokenManager(token_dir=temp_token_dir)
    app = create_agent_app(config=config, token_manager=token_mgr)

    with TestClient(app, base_url="http://127.0.0.1:8001") as client:
        # First exchange succeeds
        resp1 = client.post(
            "/agent/v1/auth/bootstrap",
            headers={"X-QueryPilot-Request": "1"},
            json={"bootstrap_token": cli_token},
        )
        assert resp1.status_code == status.HTTP_200_OK
        assert not (temp_token_dir / "agent.token").exists()
        assert not token_mgr.has_active_token

        # Replay attempt fails with 401
        resp2 = client.post(
            "/agent/v1/auth/bootstrap",
            headers={"X-QueryPilot-Request": "1"},
            json={"bootstrap_token": cli_token},
        )
        assert resp2.status_code == status.HTTP_401_UNAUTHORIZED


def test_concurrent_bootstrap_requests(temp_token_dir: Path, mock_windows_acl):
    """13. Exactly one concurrent bootstrap request succeeds; others fail with 401."""
    run_agent_cli(["auth", "rotate", "--token-dir", str(temp_token_dir)])
    cli_token = (temp_token_dir / "agent.token").read_text(encoding="utf-8").strip()

    config = AgentConfig(token_dir=temp_token_dir, max_concurrent_requests=10)
    token_mgr = BootstrapTokenManager(token_dir=temp_token_dir)
    app = create_agent_app(config=config, token_manager=token_mgr)

    with TestClient(app, base_url="http://127.0.0.1:8001") as client:
        def attempt_bootstrap():
            return client.post(
                "/agent/v1/auth/bootstrap",
                headers={"X-QueryPilot-Request": "1"},
                json={"bootstrap_token": cli_token},
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(attempt_bootstrap) for _ in range(5)]
            results = [f.result() for f in futures]

        status_codes = [r.status_code for r in results]
        assert status_codes.count(status.HTTP_200_OK) == 1
        assert status_codes.count(status.HTTP_401_UNAUTHORIZED) == 4


def test_session_before_restart_invalid_after_restart(temp_token_dir: Path, mock_windows_acl):
    """14. In-memory session established before daemon restart is invalid after restart."""
    # Instance 1: Generate token, bootstrap session
    config = AgentConfig(token_dir=temp_token_dir)
    token_mgr1 = BootstrapTokenManager(token_dir=temp_token_dir)
    session_mgr1 = SessionManager()
    app1 = create_agent_app(config=config, token_manager=token_mgr1, session_manager=session_mgr1)

    with TestClient(app1, base_url="http://127.0.0.1:8001") as client1:
        with token_mgr1._lock:
            token1 = token_mgr1._token

        login_resp = client1.post(
            "/agent/v1/auth/bootstrap",
            headers={"X-QueryPilot-Request": "1"},
            json={"bootstrap_token": token1},
        )
        session_id = login_resp.cookies.get(SESSION_COOKIE_NAME)
        assert session_id is not None

        # Verify session works
        prof_resp = client1.get("/agent/v1/profiles", cookies={SESSION_COOKIE_NAME: session_id})
        assert prof_resp.status_code == status.HTTP_200_OK

    # Simulate restart: New daemon process with fresh SessionManager
    token_mgr2 = BootstrapTokenManager(token_dir=temp_token_dir)
    session_mgr2 = SessionManager()
    app2 = create_agent_app(config=config, token_manager=token_mgr2, session_manager=session_mgr2)

    with TestClient(app2, base_url="http://127.0.0.1:8001") as client2:
        # Pre-restart session is unrecognized / invalid
        old_prof_resp = client2.get("/agent/v1/profiles", cookies={SESSION_COOKIE_NAME: session_id})
        assert old_prof_resp.status_code == status.HTTP_401_UNAUTHORIZED


def test_http_rotate_token_requires_valid_session(temp_token_dir: Path, mock_windows_acl):
    """15. HTTP rotate-token endpoint remains strictly session-authenticated."""
    config = AgentConfig(token_dir=temp_token_dir, max_concurrent_requests=10)
    token_mgr = BootstrapTokenManager(token_dir=temp_token_dir)
    app = create_agent_app(config=config, token_manager=token_mgr)

    with TestClient(app, base_url="http://127.0.0.1:8001") as client:
        # Unauthenticated call fails 401
        resp = client.post("/agent/v1/auth/rotate-token", headers={"X-QueryPilot-Request": "1"})
        assert resp.status_code == status.HTTP_401_UNAUTHORIZED


def test_profile_cli_backward_compatibility(tmp_path: Path, capsys):
    """16. Existing profile CLI subcommands ('profile list') remain completely unaffected."""
    profiles_file = tmp_path / "profiles.json"
    exit_code = run_agent_cli(["profile", "list", "--profiles-file", str(profiles_file)])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "No profiles file found" in captured.out


def test_safe_wipe_preserves_substituted_token_file(temp_token_dir: Path, caplog):
    """17. _safe_wipe_token_file preserves file when contents differ from consumed token and leaks no secrets."""
    token_mgr = BootstrapTokenManager(token_dir=temp_token_dir)
    token_path = token_mgr.token_path

    synthetic_different_token = "1111111111111111111111111111111111111111111111111111111111111111"
    consumed_expected_token = "2222222222222222222222222222222222222222222222222222222222222222"

    token_path.write_text(synthetic_different_token, encoding="utf-8")

    # Attempt wipe with mismatched expected token
    with caplog.at_level("WARNING"):
        token_mgr._safe_wipe_token_file(expected_token=consumed_expected_token)

    # File remains intact
    assert token_path.exists()
    assert token_path.read_text(encoding="utf-8").strip() == synthetic_different_token

    # Logs warn of skip without disclosing either token secret
    assert "Token file wipe skipped" in caplog.text
    assert "content does not match consumed token" in caplog.text
    assert synthetic_different_token not in caplog.text
    assert consumed_expected_token not in caplog.text


def test_safe_wipe_skips_symlink_substitution(temp_token_dir: Path, caplog):
    """18. _safe_wipe_token_file skips unlinking if path is a symlink or reparse point."""
    token_mgr = BootstrapTokenManager(token_dir=temp_token_dir)
    token_path = token_mgr.token_path
    token_path.write_text("synthetic_token_value", encoding="utf-8")

    with patch.object(Path, "is_symlink", return_value=True):
        with caplog.at_level("WARNING"):
            token_mgr._safe_wipe_token_file(expected_token="synthetic_token_value")

    assert token_path.exists()
    assert "is a symlink or reparse point" in caplog.text


def test_safe_wipe_handles_concurrent_unlink_error_safely(temp_token_dir: Path, caplog):
    """19. _safe_wipe_token_file handles unexpected unlink errors without unhandled exceptions or secret leakage."""
    token_mgr = BootstrapTokenManager(token_dir=temp_token_dir)
    token_path = token_mgr.token_path
    token = "3333333333333333333333333333333333333333333333333333333333333333"
    token_path.write_text(token, encoding="utf-8")

    with patch.object(Path, "unlink", side_effect=PermissionError("File locked by external process")):
        with caplog.at_level("WARNING"):
            token_mgr._safe_wipe_token_file(expected_token=token)

    assert "Failed to delete bootstrap token file" in caplog.text
    assert token not in caplog.text


def test_windows_acl_rejects_inherited_permissions(temp_token_dir: Path):
    """20. Startup adoption rejects pre-existing Windows token file with inherited permissions."""
    token_path = temp_token_dir / "agent.token"
    token_path.write_text("4444444444444444444444444444444444444444444444444444444444444444", encoding="utf-8")

    mock_proc = subprocess.CompletedProcess(
        args=["icacls"],
        returncode=0,
        stdout=f"{token_path} NT AUTHORITY\\SYSTEM:(I)(F)\n",
        stderr="",
    )
    with patch("platform.system", return_value="Windows"), \
         patch("subprocess.run", return_value=mock_proc), \
         patch("query_pilot.agent.auth._get_active_windows_trustee", return_value="NT AUTHORITY\\SYSTEM"):
        token_mgr = BootstrapTokenManager(token_dir=temp_token_dir)
        with pytest.raises(BootstrapTokenProvisioningError, match="contains inherited permissions"):
            token_mgr.adopt_or_initialize_token()


def test_windows_acl_rejects_broad_principals(temp_token_dir: Path):
    """21. Startup adoption rejects Windows token file with broad principal access (e.g. Everyone)."""
    token_path = temp_token_dir / "agent.token"
    token_path.write_text("5555555555555555555555555555555555555555555555555555555555555555", encoding="utf-8")

    mock_proc = subprocess.CompletedProcess(
        args=["icacls"],
        returncode=0,
        stdout=f"{token_path} NT AUTHORITY\\SYSTEM:(R,W)\n                   Everyone:(R)\n",
        stderr="",
    )
    with patch("platform.system", return_value="Windows"), \
         patch("subprocess.run", return_value=mock_proc), \
         patch("query_pilot.agent.auth._get_active_windows_trustee", return_value="NT AUTHORITY\\SYSTEM"):
        token_mgr = BootstrapTokenManager(token_dir=temp_token_dir)
        with pytest.raises(BootstrapTokenProvisioningError, match="grants access to broad principal"):
            token_mgr.adopt_or_initialize_token()


def test_windows_acl_rejects_unexpected_explicit_aces(temp_token_dir: Path):
    """22. Startup adoption rejects Windows token file containing explicit ACE for unrelated user."""
    token_path = temp_token_dir / "agent.token"
    token_path.write_text("6666666666666666666666666666666666666666666666666666666666666666", encoding="utf-8")

    mock_proc = subprocess.CompletedProcess(
        args=["icacls"],
        returncode=0,
        stdout=f"{token_path} NT AUTHORITY\\SYSTEM:(R,W)\n                   CORP\\unauthorized_user:(R)\n",
        stderr="",
    )
    with patch("platform.system", return_value="Windows"), \
         patch("subprocess.run", return_value=mock_proc), \
         patch("query_pilot.agent.auth._get_active_windows_trustee", return_value="NT AUTHORITY\\SYSTEM"):
        token_mgr = BootstrapTokenManager(token_dir=temp_token_dir)
        with pytest.raises(BootstrapTokenProvisioningError, match="unexpected explicit ACE"):
            token_mgr.adopt_or_initialize_token()


def test_windows_acl_accepts_compliant_exclusive_acl(temp_token_dir: Path):
    """23. Startup adoption accepts compliant Windows token file with exclusive active trustee grant."""
    token_path = temp_token_dir / "agent.token"
    valid_hex = "7777777777777777777777777777777777777777777777777777777777777777"
    token_path.write_text(valid_hex, encoding="utf-8")

    mock_proc = subprocess.CompletedProcess(
        args=["icacls"],
        returncode=0,
        stdout=f"{token_path} NT AUTHORITY\\SYSTEM:(R,W)\n",
        stderr="",
    )
    with patch("platform.system", return_value="Windows"), \
         patch("subprocess.run", return_value=mock_proc), \
         patch("query_pilot.agent.auth._get_active_windows_trustee", return_value="NT AUTHORITY\\SYSTEM"):
        token_mgr = BootstrapTokenManager(token_dir=temp_token_dir)
        adopted = token_mgr.adopt_or_initialize_token()
        assert adopted == valid_hex
        assert token_mgr.has_active_token
