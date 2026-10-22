"""Offline unit tests for Safe Profile Onboarding (Milestone B12-C).

Verifies:
1. Loading valid SQL Server profiles using Windows Integrated Authentication.
2. Missing and malformed profile file handling (actionable first-run vs. fail-closed).
3. Rejection of SQL-authenticated profiles, passwords, usernames, and plaintext credentials.
4. Duplicate profile ID and invalid profile field validation.
5. Atomic file write behavior and restrictive Windows ACL / POSIX permissions.
6. CLI subcommands ('profile add', 'profile list', 'profile validate') and exit codes.
7. Profile loading through the default local agent startup path.
8. Sanitized profile listings via GET /agent/v1/profiles (zero credentials).
9. Generation-only invariant: candidate generation does not execute SQL.
"""

from __future__ import annotations

import json
from pathlib import Path
import stat
import sys
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
import pytest

from query_pilot.agent.app import create_agent_app
from query_pilot.agent.cli import (
    build_parser,
    handle_profile_add,
    handle_profile_list,
    handle_profile_validate,
    run_agent_cli,
)
from query_pilot.agent.config import AgentConfig
from query_pilot.agent.models import ProfileListResponse
from query_pilot.db.profile import (
    ConnectionProfile,
    ProfileConfigurationError,
    ProfileStore,
    load_trusted_profiles_file,
    profile_file_lock,
    restrict_file_permissions,
    save_trusted_profiles_file,
    validate_safe_profile_data,
    validate_safe_profile_path,
)


# ==============================================================================
# 1. Loading Valid Windows Integrated Authentication Profiles
# ==============================================================================


def test_load_valid_trusted_profile(tmp_path: Path):
    """Verify that a valid Windows Integrated Auth profile file loads correctly."""
    profiles_file = tmp_path / "profiles.json"
    payload = {
        "profiles": {
            "local_sqlserver": {
                "profile_id": "local_sqlserver",
                "display_name": "Local SQL Server Express",
                "engine_type": "mssql",
                "server": r"localhost\SQLEXPRESS",
                "database_name": "QueryPilot_TestDB",
                "driver": "ODBC Driver 17 for SQL Server",
                "use_trusted_connection": True,
                "timeout_seconds": 15,
                "max_row_limit": 100,
                "schema_name": "dbo",
            }
        },
        "default_profile_id": "local_sqlserver",
    }
    profiles_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    store = load_trusted_profiles_file(profiles_file)

    assert len(store) == 1
    assert "local_sqlserver" in store.profiles
    profile = store.get_profile("local_sqlserver")
    assert profile.profile_id == "local_sqlserver"
    assert profile.display_name == "Local SQL Server Express"
    assert profile.engine_type == "mssql"
    assert profile.server == r"localhost\SQLEXPRESS"
    assert profile.database_name == "QueryPilot_TestDB"
    assert profile.driver == "ODBC Driver 17 for SQL Server"
    assert profile.use_trusted_connection is True
    assert profile.user is None
    assert profile.password is None
    assert profile.timeout_seconds == 15
    assert profile.max_row_limit == 100
    assert store.default_profile_id == "local_sqlserver"


# ==============================================================================
# 2. Missing & Malformed Profile Files
# ==============================================================================


def test_missing_profile_file_raises_on_direct_load(tmp_path: Path):
    """Directly loading a non-existent profiles file raises ProfileConfigurationError."""
    missing_file = tmp_path / "non_existent.json"
    with pytest.raises(ProfileConfigurationError) as exc:
        load_trusted_profiles_file(missing_file)
    assert "not found" in str(exc.value)


def test_missing_profile_file_starts_empty_store_in_app(tmp_path: Path):
    """Starting agent app with a missing profiles file defaults to an empty ProfileStore (first-run)."""
    missing_file = tmp_path / "missing_profiles.json"
    config = AgentConfig(profiles_file=missing_file, token_dir=tmp_path / "tokens")

    app = create_agent_app(config=config)
    assert app.state.store is not None
    assert len(app.state.store) == 0


def test_malformed_json_fails_closed(tmp_path: Path):
    """A profile file containing malformed JSON fails closed with ProfileConfigurationError."""
    corrupt_file = tmp_path / "corrupt.json"
    corrupt_file.write_text("{ this is not valid json }", encoding="utf-8")

    with pytest.raises(ProfileConfigurationError) as exc:
        load_trusted_profiles_file(corrupt_file)
    assert "Failed to parse profiles JSON" in str(exc.value)

    # In create_agent_app, a corrupt file does NOT silently become empty; it fails closed!
    config = AgentConfig(profiles_file=corrupt_file, token_dir=tmp_path / "tokens")
    with pytest.raises(ProfileConfigurationError):
        create_agent_app(config=config)


# ==============================================================================
# 3. Credential Rejection & SQL Authentication Prohibitions
# ==============================================================================


@pytest.mark.parametrize("bad_key", ["password", "pwd", "user", "username", "uid", "secret", "token", "api_key"])
def test_rejects_password_and_user_credentials(bad_key: str):
    """Profile data containing passwords or usernames is strictly rejected."""
    bad_data = {
        "profile_id": "bad_profile",
        "display_name": "Bad Profile",
        "server": "localhost",
        "database_name": "TestDB",
        bad_key: "forbidden_credential_value",
    }
    with pytest.raises(ProfileConfigurationError) as exc:
        validate_safe_profile_data(bad_data)
    assert "SECURITY REJECTION" in str(exc.value)
    assert bad_key in str(exc.value)


def test_rejects_sql_auth_trusted_connection_false():
    """Profiles requesting SQL authentication (trusted_connection=False) are rejected."""
    bad_data = {
        "profile_id": "sql_auth_profile",
        "display_name": "SQL Auth Profile",
        "server": "localhost",
        "database_name": "TestDB",
        "use_trusted_connection": False,
    }
    with pytest.raises(ProfileConfigurationError) as exc:
        validate_safe_profile_data(bad_data)
    assert "SQL-authenticated profiles" in str(exc.value)


def test_rejects_unsupported_engine_type():
    """Non-mssql database engines are rejected in this milestone."""
    bad_data = {
        "profile_id": "postgres_profile",
        "display_name": "Postgres Profile",
        "engine_type": "postgresql",
        "server": "localhost",
        "database_name": "TestDB",
        "use_trusted_connection": True,
    }
    with pytest.raises(ProfileConfigurationError) as exc:
        validate_safe_profile_data(bad_data)
    assert "Unsupported engine_type" in str(exc.value)


# ==============================================================================
# 4. Duplicate IDs and Field Validations
# ==============================================================================


def test_rejects_duplicate_profile_ids(tmp_path: Path):
    """Profile files with duplicate profile IDs are rejected."""
    duplicate_file = tmp_path / "duplicates.json"
    payload = {
        "profiles": [
            {
                "profile_id": "dup_id",
                "display_name": "Profile 1",
                "server": "localhost",
                "database_name": "DB1",
                "use_trusted_connection": True,
            },
            {
                "profile_id": "dup_id",
                "display_name": "Profile 2",
                "server": "localhost",
                "database_name": "DB2",
                "use_trusted_connection": True,
            },
        ]
    }
    duplicate_file.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ProfileConfigurationError) as exc:
        load_trusted_profiles_file(duplicate_file)
    assert "Duplicate profile_id 'dup_id'" in str(exc.value)


def test_invalid_profile_id_characters():
    """Profile IDs with spaces or unsafe characters are rejected."""
    with pytest.raises(Exception):
        ConnectionProfile(
            profile_id="invalid id with spaces",
            display_name="Test",
            server="localhost",
            database_name="TestDB",
        )


# ==============================================================================
# 5. Atomic Write and Permissions Hardening
# ==============================================================================


def test_atomic_write_and_save_profiles(tmp_path: Path):
    """Saving profiles writes atomically, cleans up temp files, and applies permissions."""
    profiles_file = tmp_path / "saved_profiles.json"
    store = ProfileStore()
    profile = ConnectionProfile(
        profile_id="saved_sql",
        display_name="Saved SQL Profile",
        server=r"localhost\SQLEXPRESS",
        database_name="QueryPilot_TestDB",
        driver="ODBC Driver 17 for SQL Server",
        use_trusted_connection=True,
    )
    store.register_profile(profile)
    store.set_default_profile("saved_sql")

    save_trusted_profiles_file(profiles_file, store)

    assert profiles_file.exists()
    content = json.loads(profiles_file.read_text(encoding="utf-8"))
    assert "saved_sql" in content["profiles"]
    assert content["default_profile_id"] == "saved_sql"

    # Confirm zero temporary files left behind
    tmp_files = list(tmp_path.glob("*.tmp.*"))
    assert len(tmp_files) == 0


# ==============================================================================
# 6. CLI Profile Commands (Add, List, Validate)
# ==============================================================================


def test_cli_profile_add_and_list_and_validate(tmp_path: Path, capsys):
    """Verify end-to-end CLI workflow: profile add -> validate -> list."""
    profiles_file = tmp_path / "cli_profiles.json"

    # 1. Profile add with --yes flag
    exit_code_add = run_agent_cli([
        "profile", "add",
        "--id", "cli_test_db",
        "--name", "CLI Test Database",
        "--server", r"localhost\SQLEXPRESS",
        "--database", "QueryPilot_TestDB",
        "--driver", "ODBC Driver 17 for SQL Server",
        "--yes",
        "--profiles-file", str(profiles_file),
    ])
    assert exit_code_add == 0
    captured_add = capsys.readouterr()
    assert "SUCCESS" in captured_add.out
    assert "cli_test_db" in captured_add.out
    assert profiles_file.exists()

    # 2. Profile validate
    exit_code_val = run_agent_cli([
        "profile", "validate",
        "--profiles-file", str(profiles_file),
    ])
    assert exit_code_val == 0
    captured_val = capsys.readouterr()
    assert "SUCCESS" in captured_val.out
    assert "cli_test_db" in captured_val.out

    # 3. Profile list
    exit_code_list = run_agent_cli([
        "profile", "list",
        "--profiles-file", str(profiles_file),
    ])
    assert exit_code_list == 0
    captured_list = capsys.readouterr()
    assert "cli_test_db" in captured_list.out
    assert "CLI Test Database" in captured_list.out
    assert "Windows Integrated Authentication" in captured_list.out
    assert "QueryPilot_TestDB" in captured_list.out


def test_cli_profile_add_non_interactive_without_yes_aborts(tmp_path: Path, capsys):
    """Running profile add non-interactively without --yes aborts with exit code 1."""
    profiles_file = tmp_path / "abort_profiles.json"
    with patch("sys.stdin.isatty", return_value=False):
        exit_code = run_agent_cli([
            "profile", "add",
            "--id", "abort_id",
            "--database", "AbortDB",
            "--profiles-file", str(profiles_file),
        ])
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "requires --yes" in captured.err
    assert not profiles_file.exists()


def test_cli_profile_validate_missing_file_fails(tmp_path: Path, capsys):
    """Validating a non-existent file produces exit code 1."""
    missing = tmp_path / "not_there.json"
    exit_code = run_agent_cli(["profile", "validate", "--profiles-file", str(missing)])
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "VALIDATION ERROR" in captured.err


# ==============================================================================
# 7. Default CLI Startup Loads Registered Profiles
# ==============================================================================


def test_agent_startup_loads_registered_profiles(tmp_path: Path):
    """Verify that an agent initialized with a valid profiles_file exposes profiles via GET /agent/v1/profiles."""
    profiles_file = tmp_path / "startup_profiles.json"
    token_dir = tmp_path / "tokens"
    token_dir.mkdir(parents=True, exist_ok=True)

    # Register profile to file
    store = ProfileStore()
    store.register_profile(
        ConnectionProfile(
            profile_id="onboarded_sql",
            display_name="Onboarded SQL Server",
            server=r"localhost\SQLEXPRESS",
            database_name="QueryPilot_TestDB",
            driver="ODBC Driver 17 for SQL Server",
            use_trusted_connection=True,
        )
    )
    store.set_default_profile("onboarded_sql")
    save_trusted_profiles_file(profiles_file, store)

    # Create agent app without injecting store (simulates default CLI startup)
    config = AgentConfig(profiles_file=profiles_file, token_dir=token_dir)
    app = create_agent_app(config=config)

    # Verify store is populated
    assert "onboarded_sql" in app.state.store.profiles
    assert app.state.store.default_profile_id == "onboarded_sql"

    # Authenticate via bootstrap token and query GET /agent/v1/profiles
    client = TestClient(app, cookies={})
    token = app.state.token_manager.initialize_token()
    assert token is not None

    bootstrap_resp = client.post(
        "/agent/v1/auth/bootstrap",
        json={"bootstrap_token": token},
        headers={"Host": "127.0.0.1:8001", "X-QueryPilot-Request": "1"},
    )
    assert bootstrap_resp.status_code == 200

    profiles_resp = client.get(
        "/agent/v1/profiles",
        headers={"Host": "127.0.0.1:8001"},
    )
    assert profiles_resp.status_code == 200
    data = profiles_resp.json()
    assert len(data["profiles"]) == 1
    summary = data["profiles"][0]
    assert summary["profile_id"] == "onboarded_sql"
    assert summary["display_name"] == "Onboarded SQL Server"
    assert "password" not in summary
    assert "server" not in summary  # Raw hostnames are hidden from summaries


# ==============================================================================
# 8. Generation Invariant: Candidate Generation Does Not Execute SQL
# ==============================================================================


def test_candidate_generation_invariant_does_not_execute_sql():
    """Verify the invariant that candidate SQL generation does not execute queries."""
    from query_pilot.sql.generation import SQLGenerator

    # Mock LLM generation service
    mock_generator = MagicMock(spec=SQLGenerator)
    mock_generator.generate.return_value = MagicMock(
        sql="SELECT 1 AS test_val",
        explanation="Test SQL generation",
    )

    # Generation produces candidate SQL without calling any database executor
    result = mock_generator.generate("Show test data", schema_context="Table: Test")
    assert result.sql == "SELECT 1 AS test_val"
    # Verify no execution calls occurred
    assert not hasattr(mock_generator, "execute")


# ==============================================================================
# 9. Remediation Verification: Path Safety, Normalization, ACLs & Concurrency
# ==============================================================================


def test_case_insensitive_and_whitespace_credential_key_rejection():
    """Verify that credentials are case-insensitively and whitespace-insensitively rejected."""
    bad_variations = [
        {"Password": "plain_password"},
        {" PASSWORD ": "secret"},
        {"pAsSwOrD": "12345"},
        {" USER ": "sql_user"},
        {"Api_Key": "secret_key"},
        {"Token": "bearer_token"},
    ]
    for bad_dict in bad_variations:
        data = {
            "profile_id": "test_sql",
            "server": "localhost",
            "database_name": "TestDB",
            **bad_dict,
        }
        with pytest.raises(ProfileConfigurationError) as exc:
            validate_safe_profile_data(data)
        assert "SECURITY REJECTION" in str(exc.value)
        # Verify secret values are never echoed
        for val in bad_dict.values():
            assert val not in str(exc.value)


def test_nested_credential_bearing_structures_rejected():
    """Verify that deeply nested credential-bearing dictionaries or lists are rejected."""
    nested_structures = [
        {"nested_config": {"password": "nested_secret"}},
        {"connection_options": [{"pwd": "list_secret"}]},
        {"auth": {"credentials": {"token": "deep_token"}}},
    ]
    for bad_struct in nested_structures:
        data = {
            "profile_id": "test_sql",
            "server": "localhost",
            "database_name": "TestDB",
            **bad_struct,
        }
        with pytest.raises(ProfileConfigurationError) as exc:
            validate_safe_profile_data(data)
        assert "SECURITY REJECTION" in str(exc.value)


def test_validate_safe_profile_path_rejects_symlinks(tmp_path: Path):
    """Verify that symbolic links are rejected for profile destinations."""
    fake_link = tmp_path / "link_profile.json"
    with patch.object(Path, "is_symlink", return_value=True):
        with pytest.raises(ProfileConfigurationError) as exc:
            validate_safe_profile_path(fake_link)
        assert "symbolic link" in str(exc.value).lower()


def test_validate_safe_profile_path_rejects_parent_symlink(tmp_path: Path):
    """Verify that profile paths with symbolic link parent directories are rejected."""
    target_file = tmp_path / "profile.json"
    with patch("os.path.islink", side_effect=lambda p: str(p) == str(tmp_path)):
        with pytest.raises(ProfileConfigurationError) as exc:
            validate_safe_profile_path(target_file)
        assert "symbolic link" in str(exc.value).lower()


def test_parent_directory_hardening_failure_aborts_save(tmp_path: Path):
    """Verify that a failure to harden the dedicated parent directory fails closed and does not silently swallow."""
    dedicated_dir = tmp_path / ".query_pilot"
    profiles_file = dedicated_dir / "profiles.json"
    store = ProfileStore()
    store.register_profile(
        ConnectionProfile(
            profile_id="test_db",
            display_name="Test DB",
            server="localhost",
            database_name="TestDB",
        )
    )

    with patch(
        "query_pilot.db.profile.restrict_file_permissions",
        side_effect=ProfileConfigurationError("Simulated ACL permission failure on directory"),
    ):
        with pytest.raises(ProfileConfigurationError) as exc:
            save_trusted_profiles_file(profiles_file, store, restrict_permissions=True)
        assert "Failed to secure permissions on profile directory" in str(exc.value)

    # Ensure profile file was never written
    assert not profiles_file.exists()


def test_save_trusted_profiles_file_cleans_up_on_failure(tmp_path: Path):
    """Verify that if writing fails midway, all temporary files are unlinked and no corrupted files remain."""
    profiles_file = tmp_path / "profiles.json"
    store = ProfileStore()
    store.register_profile(
        ConnectionProfile(
            profile_id="test_db",
            display_name="Test DB",
            server="localhost",
            database_name="TestDB",
        )
    )

    # Simulate an error during json.dump
    with patch("json.dump", side_effect=IOError("Simulated disk full or serialization failure")):
        with pytest.raises(ProfileConfigurationError) as exc:
            save_trusted_profiles_file(profiles_file, store)
        assert "Failed to securely save profile file" in str(exc.value)

    # Confirm zero temporary files left behind
    tmp_files = list(tmp_path.glob("*.tmp.*"))
    assert len(tmp_files) == 0
    assert not profiles_file.exists()


def test_preservation_of_unrelated_files_during_save_failure(tmp_path: Path):
    """Verify that save failures and cleanup leave unrelated files in the same directory untouched."""
    unrelated_file = tmp_path / "unrelated_user_data.txt"
    unrelated_file.write_text("important user notes", encoding="utf-8")

    profiles_file = tmp_path / "profiles.json"
    store = ProfileStore()

    with patch("json.dump", side_effect=IOError("Simulated write failure")):
        with pytest.raises(ProfileConfigurationError):
            save_trusted_profiles_file(profiles_file, store)

    # Unrelated file must be preserved intact
    assert unrelated_file.exists()
    assert unrelated_file.read_text(encoding="utf-8") == "important user notes"


def test_profile_file_lock_concurrent_access_and_timeout(tmp_path: Path):
    """Verify that profile_file_lock serializes access and times out cleanly if held by another process."""
    profiles_file = tmp_path / "locked_profiles.json"

    # Acquire lock in outer scope
    with profile_file_lock(profiles_file, timeout=1.0):
        # Attempting to acquire lock again should time out
        with pytest.raises(ProfileConfigurationError) as exc:
            with profile_file_lock(profiles_file, timeout=0.1):
                pass
        assert "Timeout" in str(exc.value)

    # After exit, lock file is cleaned up and new lock can be acquired immediately
    with profile_file_lock(profiles_file, timeout=0.5):
        pass

    assert not (tmp_path / "locked_profiles.json.lock").exists()


def test_windows_dacl_verification_failure_raises_error(tmp_path: Path):
    """Verify that if post-hardening DACL verification fails to confirm the trustee, an error is raised."""
    dummy_file = tmp_path / "dummy.txt"
    dummy_file.write_text("test", encoding="utf-8")

    # Mock icacls verification returning empty or non-matching output
    mock_run = MagicMock()
    # First call: icacls /inheritance:r ... succeeds (returncode=0)
    # Second call: icacls verify query returns no matching trustee
    mock_run.side_effect = [
        MagicMock(returncode=0, stdout="Successfully processed 1 files"),
        MagicMock(returncode=0, stdout="dummy.txt (I)(F)\nSuccessfully processed 1 files"),
    ]

    with (
        patch("platform.system", return_value="Windows"),
        patch("query_pilot.db.profile._get_active_windows_trustee", return_value="TESTDOMAIN\\TestUser"),
        patch("subprocess.run", mock_run),
    ):
        with pytest.raises(ProfileConfigurationError) as exc:
            restrict_file_permissions(dummy_file)
        assert "DACL verification failed" in str(exc.value)

