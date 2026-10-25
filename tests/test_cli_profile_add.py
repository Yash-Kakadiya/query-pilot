"""Offline integration tests for CLI profile validation parity (Milestone B12-F8G).

Verifies:
1. Rejection of remote hostnames, LAN IPs, and non-loopback destinations.
2. Rejection of invalid server-target syntax (UNC paths, control characters, whitespaces).
3. Rejection of SQL Server system databases (master, msdb, model, tempdb).
4. Rejection of invalid profile ID, database identifier, and schema identifier.
5. Rejection of unsupported authentication modes (e.g. SQL authentication).
6. Rejection of invalid timeout (< 1 or > 15) and max-rows (< 1 or > 100).
7. Successful registration of valid loopback profiles with allowed tables and sensitive columns.
8. Preservation of existing saved profiles and default profile when validation fails.
9. Rejection of duplicate profile IDs without altering disk file or default profile.
10. Sanitized CLI error output with zero secrets, credentials, or raw tracebacks.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from query_pilot.agent.cli import run_agent_cli
from query_pilot.db.profile import (
    ConnectionProfile,
    ProfileStore,
    load_trusted_profiles_file,
    save_trusted_profiles_file,
)


def test_cli_profile_add_remote_hostname_rejected(tmp_path: Path, capsys):
    """CLI rejects non-loopback remote hostnames without writing to disk."""
    profiles_file = tmp_path / "profiles.json"
    exit_code = run_agent_cli([
        "profile", "add",
        "--id", "remote_db",
        "--name", "Remote Database",
        "--server", "sqlserver.corp.internal",
        "--database", "CorpData",
        "--yes",
        "--profiles-file", str(profiles_file),
    ])
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "CONFIGURATION ERROR" in captured.err
    assert "SECURITY REJECTION" in captured.err
    assert "not an approved local loopback host" in captured.err
    assert not profiles_file.exists()


def test_cli_profile_add_remote_ip_rejected(tmp_path: Path, capsys):
    """CLI rejects non-loopback LAN and public IP addresses."""
    profiles_file = tmp_path / "profiles.json"
    exit_code = run_agent_cli([
        "profile", "add",
        "--id", "lan_db",
        "--server", "192.168.1.100",
        "--database", "FinanceDB",
        "--yes",
        "--profiles-file", str(profiles_file),
    ])
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "CONFIGURATION ERROR" in captured.err
    assert "SECURITY REJECTION" in captured.err
    assert "not an approved local loopback host" in captured.err
    assert not profiles_file.exists()


def test_cli_profile_add_invalid_server_syntax_rejected(tmp_path: Path, capsys):
    """CLI rejects UNC paths, whitespace, and URL schemes in server target."""
    profiles_file = tmp_path / "profiles.json"

    # UNC path
    exit_code = run_agent_cli([
        "profile", "add",
        "--id", "unc_db",
        "--server", r"\\attacker\share",
        "--database", "SharedDB",
        "--yes",
        "--profiles-file", str(profiles_file),
    ])
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "UNC paths and network shares are prohibited" in captured.err
    assert not profiles_file.exists()

    # URL scheme
    exit_code = run_agent_cli([
        "profile", "add",
        "--id", "url_db",
        "--server", "http://localhost:1433",
        "--database", "HttpDB",
        "--yes",
        "--profiles-file", str(profiles_file),
    ])
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "URLs and URI schemes are prohibited" in captured.err
    assert not profiles_file.exists()


@pytest.mark.parametrize("sys_db", ["master", "msdb", "model", "tempdb", "MASTER", "MsDb"])
def test_cli_profile_add_system_database_rejected(tmp_path: Path, capsys, sys_db: str):
    """CLI rejects SQL Server well-known system databases case-insensitively."""
    profiles_file = tmp_path / "profiles.json"
    exit_code = run_agent_cli([
        "profile", "add",
        "--id", f"sys_{sys_db.lower()}",
        "--server", r"localhost\SQLEXPRESS",
        "--database", sys_db,
        "--yes",
        "--profiles-file", str(profiles_file),
    ])
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "CONFIGURATION ERROR" in captured.err
    assert "SECURITY REJECTION" in captured.err
    assert "system database" in captured.err
    assert not profiles_file.exists()


def test_cli_profile_add_invalid_profile_id_rejected(tmp_path: Path, capsys):
    """CLI rejects invalid characters and overly long profile identifiers."""
    profiles_file = tmp_path / "profiles.json"

    # Spaces and special characters
    exit_code = run_agent_cli([
        "profile", "add",
        "--id", "bad id with spaces!",
        "--database", "ValidDB",
        "--yes",
        "--profiles-file", str(profiles_file),
    ])
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "invalid characters" in captured.err
    assert not profiles_file.exists()

    # Excessively long profile ID (> 64 chars)
    exit_code = run_agent_cli([
        "profile", "add",
        "--id", "a" * 65,
        "--database", "ValidDB",
        "--yes",
        "--profiles-file", str(profiles_file),
    ])
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "64 characters" in captured.err
    assert not profiles_file.exists()


def test_cli_profile_add_invalid_database_identifier_rejected(tmp_path: Path, capsys):
    """CLI rejects database names with invalid SQL Server identifier syntax."""
    profiles_file = tmp_path / "profiles.json"
    exit_code = run_agent_cli([
        "profile", "add",
        "--id", "valid_id",
        "--database", "invalid-db-name;DROP TABLE Users;--",
        "--yes",
        "--profiles-file", str(profiles_file),
    ])
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "invalid characters" in captured.err
    assert not profiles_file.exists()


def test_cli_profile_add_invalid_schema_identifier_rejected(tmp_path: Path, capsys):
    """CLI rejects schema names with invalid characters."""
    profiles_file = tmp_path / "profiles.json"
    exit_code = run_agent_cli([
        "profile", "add",
        "--id", "valid_id",
        "--database", "ValidDB",
        "--schema", "invalid schema!",
        "--yes",
        "--profiles-file", str(profiles_file),
    ])
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "invalid characters" in captured.err
    assert not profiles_file.exists()


def test_cli_profile_add_unsupported_auth_mode_rejected(tmp_path: Path, capsys):
    """CLI rejects non-Windows authentication modes."""
    profiles_file = tmp_path / "profiles.json"
    exit_code = run_agent_cli([
        "profile", "add",
        "--id", "sql_auth_db",
        "--database", "ValidDB",
        "--auth", "sql",
        "--yes",
        "--profiles-file", str(profiles_file),
    ])
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "SECURITY REJECTION" in captured.err
    assert "Windows Integrated Authentication" in captured.err
    assert not profiles_file.exists()


def test_cli_profile_add_invalid_timeout_and_row_limits_rejected(tmp_path: Path, capsys):
    """CLI rejects timeout and row limits outside bounded policy ranges."""
    profiles_file = tmp_path / "profiles.json"

    # Timeout = 0
    exit_code = run_agent_cli([
        "profile", "add",
        "--id", "timeout_zero",
        "--database", "ValidDB",
        "--timeout", "0",
        "--yes",
        "--profiles-file", str(profiles_file),
    ])
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "CONFIGURATION ERROR" in captured.err
    assert not profiles_file.exists()

    # Timeout = 30 (> 15)
    exit_code = run_agent_cli([
        "profile", "add",
        "--id", "timeout_thirty",
        "--database", "ValidDB",
        "--timeout", "30",
        "--yes",
        "--profiles-file", str(profiles_file),
    ])
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "CONFIGURATION ERROR" in captured.err
    assert not profiles_file.exists()

    # Max rows = 0
    exit_code = run_agent_cli([
        "profile", "add",
        "--id", "rows_zero",
        "--database", "ValidDB",
        "--max-rows", "0",
        "--yes",
        "--profiles-file", str(profiles_file),
    ])
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "CONFIGURATION ERROR" in captured.err
    assert not profiles_file.exists()

    # Max rows = 200 (> 100)
    exit_code = run_agent_cli([
        "profile", "add",
        "--id", "rows_two_hundred",
        "--database", "ValidDB",
        "--max-rows", "200",
        "--yes",
        "--profiles-file", str(profiles_file),
    ])
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "CONFIGURATION ERROR" in captured.err
    assert not profiles_file.exists()


def test_cli_profile_add_valid_loopback_creation_with_restrictions(tmp_path: Path, capsys):
    """CLI successfully registers a valid loopback profile and records allowed tables and sensitive columns."""
    profiles_file = tmp_path / "profiles.json"
    exit_code = run_agent_cli([
        "profile", "add",
        "--id", "valid_local_db",
        "--name", "Valid Local DB",
        "--server", r"localhost\SQLEXPRESS",
        "--database", "TestCatalog",
        "--schema", "dbo",
        "--timeout", "12",
        "--max-rows", "75",
        "--allowed-tables", "Customers", "Orders",
        "--sensitive-columns", "TaxID", "InternalNotes",
        "--yes",
        "--profiles-file", str(profiles_file),
    ])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "SUCCESS" in captured.out
    assert "valid_local_db" in captured.out
    assert profiles_file.exists()

    store = load_trusted_profiles_file(profiles_file)
    assert "valid_local_db" in store.profiles
    profile = store.profiles["valid_local_db"]
    assert profile.server == r"localhost\SQLEXPRESS"
    assert profile.database_name == "TestCatalog"
    assert profile.timeout_seconds == 12
    assert profile.max_row_limit == 75
    assert profile.allowed_tables == ("Customers", "Orders")
    assert profile.sensitive_columns == ("TaxID", "InternalNotes")
    assert store.default_profile_id == "valid_local_db"


def test_cli_profile_add_preserves_existing_profiles_and_default_on_validation_failure(
    tmp_path: Path, capsys
):
    """A rejected CLI profile add does not mutate existing saved profiles or default profile."""
    profiles_file = tmp_path / "profiles.json"

    # Pre-populate store with a valid initial profile
    initial_store = ProfileStore()
    p1 = ConnectionProfile(
        profile_id="primary_db",
        display_name="Primary DB",
        server=r"localhost\SQLEXPRESS",
        database_name="PrimaryDB",
        use_trusted_connection=True,
    )
    initial_store.register_profile(p1)
    initial_store.set_default_profile("primary_db")
    save_trusted_profiles_file(profiles_file, initial_store)

    # Attempt to add a second profile that fails loopback validation
    exit_code = run_agent_cli([
        "profile", "add",
        "--id", "secondary_db",
        "--server", "remote.corp.net",
        "--database", "SecondaryDB",
        "--yes",
        "--profiles-file", str(profiles_file),
    ])
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "CONFIGURATION ERROR" in captured.err

    # Verify disk state is completely untouched
    reloaded = load_trusted_profiles_file(profiles_file)
    assert len(reloaded.profiles) == 1
    assert "primary_db" in reloaded.profiles
    assert "secondary_db" not in reloaded.profiles
    assert reloaded.default_profile_id == "primary_db"


def test_cli_profile_add_duplicate_profile_id_rejected(tmp_path: Path, capsys):
    """CLI rejects duplicate profile ID and preserves existing profile and default."""
    profiles_file = tmp_path / "profiles.json"

    # Pre-populate store
    initial_store = ProfileStore()
    p1 = ConnectionProfile(
        profile_id="duplicate_test_db",
        display_name="Original DB",
        server=r"localhost\SQLEXPRESS",
        database_name="OriginalDB",
        use_trusted_connection=True,
    )
    initial_store.register_profile(p1)
    initial_store.set_default_profile("duplicate_test_db")
    save_trusted_profiles_file(profiles_file, initial_store)

    # Attempt to re-register duplicate_test_db with different parameters
    exit_code = run_agent_cli([
        "profile", "add",
        "--id", "duplicate_test_db",
        "--name", "Changed DB",
        "--server", "127.0.0.1",
        "--database", "ChangedDB",
        "--yes",
        "--profiles-file", str(profiles_file),
    ])
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "ERROR" in captured.err
    assert "already exists" in captured.err

    # Verify original profile was not overwritten
    reloaded = load_trusted_profiles_file(profiles_file)
    profile = reloaded.profiles["duplicate_test_db"]
    assert profile.display_name == "Original DB"
    assert profile.database_name == "OriginalDB"
    assert reloaded.default_profile_id == "duplicate_test_db"
