"""Acceptance tests for B24-C Phase 2: SQLite Profile Schema & Path Validation.

Covers:
1. Loading legacy/old MSSQL profiles unchanged.
2. SQLite profile creation, serialization, and reloading.
3. Rejection of missing database files.
4. Rejection of directories and non-regular files.
5. Canonical path resolution and rejection of invalid traversals.
6. Rejection of UNC paths (\\\\server\\share) and Windows device paths (\\\\?\\, \\\\.\\, CON, NUL, etc.).
7. Rejection of protected state (.env, .git, .query_pilot, agent.token, profiles.json) and OS roots.
8. Symlink/junction resolution to protected targets.
9. Rejection of invalid engine types.
10. Strict blocking of SQLite profiles from entering MSSQL connection-building paths:
    - profile.get_odbc_connection_string()
    - profile.get_sqlalchemy_url()
    - ProfileConnectionFactory.get_configuration()
    - create_profile_db_engine()
11. Guarantee of zero database file creation during validation and profile creation.
12. CLI integration: 'querypilot-agent profile add --engine sqlite --path <path>' and 'profile list'.
13. Agent HTTP endpoint guards for SQLite execution and schema introspection in Phase 2.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import stat
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from starlette import status
from starlette.testclient import TestClient

from query_pilot.agent.app import create_agent_app
from query_pilot.agent.cli import run_agent_cli
from query_pilot.agent.config import AgentConfig
from query_pilot.agent.auth import BootstrapTokenManager, SESSION_COOKIE_NAME
from query_pilot.agent.models import ValidateProfileConfigRequest
from query_pilot.db.connection import DatabaseConnectionError, create_profile_db_engine
from query_pilot.db.factory import ProfileConnectionFactory
from query_pilot.agent.models import ProfileSummary
from query_pilot.db.profile import (
    ConnectionProfile,
    ProfileConfigurationError,
    ProfileStore,
    load_trusted_profiles_file,
    save_trusted_profiles_file,
    validate_safe_profile_data,
)
from query_pilot.db.sqlite_path import (
    DOS_DEVICE_NAMES,
    validate_sqlite_database_path,
)


@pytest.fixture
def sqlite_test_db(tmp_path: Path) -> Path:
    """Create a real, minimal SQLite database file on disk."""
    db_file = tmp_path / "valid_test.db"
    conn = sqlite3.connect(str(db_file))
    cur = conn.cursor()
    cur.execute("CREATE TABLE products (id INTEGER PRIMARY KEY, name TEXT, price REAL)")
    cur.execute("INSERT INTO products VALUES (1, 'Widget', 19.99)")
    conn.commit()
    conn.close()
    return db_file


@pytest.fixture
def profiles_json_path(tmp_path: Path) -> Path:
    """Provide a path for a temporary profiles.json file."""
    return tmp_path / "profiles.json"


# ==============================================================================
# 1. Legacy MSSQL Profiles Load Unchanged
# ==============================================================================


def test_legacy_mssql_profiles_load_without_modification(profiles_json_path: Path):
    """Existing profiles.json files without engine_type or database_path load cleanly."""
    legacy_data = {
        "schema_version": "1.0",
        "default_profile_id": "legacy_mssql",
        "profiles": {
            "legacy_mssql": {
                "profile_id": "legacy_mssql",
                "display_name": "Legacy SQL Server",
                "server": r"localhost\SQLEXPRESS",
                "database_name": "LegacyDB",
                "driver": "ODBC Driver 17 for SQL Server",
                "use_trusted_connection": True,
                "timeout_seconds": 10,
                "max_row_limit": 50,
                "schema_name": "dbo",
                "allowed_tables": ["Customers", "Orders"],
                "sensitive_columns": ["TaxID"],
            }
        },
    }
    profiles_json_path.write_text(json.dumps(legacy_data), encoding="utf-8")

    store = load_trusted_profiles_file(profiles_json_path)
    assert len(store) == 1
    assert store.has_profile("legacy_mssql")
    profile = store.get_profile("legacy_mssql")

    assert profile.engine_type == "mssql"
    assert profile.database_path is None
    assert profile.server == r"localhost\SQLEXPRESS"
    assert profile.database_name == "LegacyDB"
    assert profile.driver == "ODBC Driver 17 for SQL Server"
    assert profile.schema_name == "dbo"
    assert profile.use_trusted_connection is True
    assert profile.allowed_tables == ("Customers", "Orders")
    assert profile.sensitive_columns == ("TaxID",)


# ==============================================================================
# 2. SQLite Profile Creation, Serialization, and Reloading
# ==============================================================================


def test_sqlite_profile_creation_serialization_reload(sqlite_test_db: Path, profiles_json_path: Path):
    """SQLite profile can be constructed, saved to ProfileStore/disk, and reloaded accurately."""
    profile = ConnectionProfile(
        profile_id="sqlite_store_test",
        display_name="Store Test DB",
        engine_type="sqlite",
        database_path=str(sqlite_test_db),
    )

    # Derived and default attributes
    assert profile.engine_type == "sqlite"
    assert profile.database_name == "valid_test"
    assert profile.schema_name == "main"
    assert profile.database_path == str(sqlite_test_db.resolve())
    assert profile.server is None
    assert profile.driver is None

    store = ProfileStore()
    store.register_profile(profile)
    store.set_default_profile("sqlite_store_test")
    save_trusted_profiles_file(profiles_json_path, store)

    # Check disk JSON structure: must NOT serialize server, driver, or use_trusted_connection
    raw_disk = json.loads(profiles_json_path.read_text(encoding="utf-8"))
    disk_profile = raw_disk["profiles"]["sqlite_store_test"]
    assert disk_profile["engine_type"] == "sqlite"
    assert disk_profile["database_path"] == str(sqlite_test_db.resolve())
    assert disk_profile["database_name"] == "valid_test"
    assert disk_profile["schema_name"] == "main"
    assert "server" not in disk_profile
    assert "driver" not in disk_profile
    assert "use_trusted_connection" not in disk_profile

    # Reload from disk
    reloaded_store = load_trusted_profiles_file(profiles_json_path)
    assert len(reloaded_store) == 1
    reloaded = reloaded_store.get_profile("sqlite_store_test")
    assert reloaded.engine_type == "sqlite"
    assert reloaded.database_path == str(sqlite_test_db.resolve())
    assert reloaded.database_name == "valid_test"
    assert reloaded.schema_name == "main"
    assert reloaded.server is None
    assert reloaded.driver is None


def test_sqlite_path_privacy_in_safe_summary(sqlite_test_db: Path):
    """to_safe_summary() strictly omits database_path to prevent path disclosure."""
    profile = ConnectionProfile(
        profile_id="privacy_sqlite",
        display_name="Privacy SQLite",
        engine_type="sqlite",
        database_path=str(sqlite_test_db),
    )
    summary = profile.to_safe_summary()
    assert summary["profile_id"] == "privacy_sqlite"
    assert summary["display_name"] == "Privacy SQLite"
    assert summary["engine_type"] == "sqlite"
    assert summary["database_name"] == "valid_test"
    # Connection targets and internal paths MUST be absent
    assert "database_path" not in summary
    assert "server" not in summary
    assert "path" not in summary
    assert str(sqlite_test_db) not in str(summary)


def test_sqlite_profile_lifecycle_revalidation_on_file_removal(
    sqlite_test_db: Path, profiles_json_path: Path
):
    """When a database file is removed after profile creation, reloading fails closed."""
    profile = ConnectionProfile(
        profile_id="lifecycle_sqlite",
        display_name="Lifecycle SQLite",
        engine_type="sqlite",
        database_path=str(sqlite_test_db),
    )
    store = ProfileStore()
    store.register_profile(profile)
    save_trusted_profiles_file(profiles_json_path, store)

    # Initial reload succeeds
    assert len(load_trusted_profiles_file(profiles_json_path)) == 1

    # Remove the database file
    sqlite_test_db.unlink()
    assert not sqlite_test_db.exists()

    # Revalidation during load must fail closed
    with pytest.raises(ProfileConfigurationError, match="does not exist"):
        load_trusted_profiles_file(profiles_json_path)


# ==============================================================================
# 3. Path Validation: Missing Files, Directories, Non-Regular Files
# ==============================================================================


def test_sqlite_missing_database_file_rejected(tmp_path: Path):
    """Missing SQLite database path raises ProfileConfigurationError / SQLitePathValidationError."""
    missing = tmp_path / "does_not_exist.db"
    assert not missing.exists()

    with pytest.raises(ProfileConfigurationError, match="does not exist"):
        validate_sqlite_database_path(str(missing))

    with pytest.raises(ValueError, match="does not exist"):
        ConnectionProfile(
            profile_id="missing_sqlite",
            display_name="Missing SQLite",
            engine_type="sqlite",
            database_path=str(missing),
        )


def test_sqlite_directory_rejected(tmp_path: Path):
    """Directory paths are rejected even if they exist."""
    dir_path = tmp_path / "somedir"
    dir_path.mkdir()

    with pytest.raises(ProfileConfigurationError, match="not a regular file"):
        validate_sqlite_database_path(str(dir_path))

    with pytest.raises(ValueError, match="not a regular file"):
        ConnectionProfile(
            profile_id="dir_sqlite",
            display_name="Directory SQLite",
            engine_type="sqlite",
            database_path=str(dir_path),
        )


def test_sqlite_non_regular_file_rejected(sqlite_test_db: Path):
    """Non-regular files (pipes, devices, sockets) are rejected via S_ISREG."""
    with patch("pathlib.Path.stat") as mock_stat:
        fake_stat = os.stat_result((stat.S_IFIFO | 0o600, 0, 0, 0, 0, 0, 0, 0, 0, 0))
        mock_stat.return_value = fake_stat
        with pytest.raises(ProfileConfigurationError, match="not a regular file"):
            validate_sqlite_database_path(str(sqlite_test_db))


# ==============================================================================
# 4. Canonical Path Resolution, Traversal, and Relative Paths
# ==============================================================================


def test_sqlite_relative_path_canonical_resolution(sqlite_test_db: Path, monkeypatch):
    """Relative paths are expanded and canonicalized to absolute paths."""
    monkeypatch.chdir(sqlite_test_db.parent)
    rel_path = f"./{sqlite_test_db.name}"

    canonical = validate_sqlite_database_path(rel_path)
    assert Path(canonical).is_absolute()
    assert canonical == str(sqlite_test_db.resolve())


def test_sqlite_user_home_expansion(sqlite_test_db: Path, monkeypatch):
    """Tilde (~) paths are expanded to the user home directory."""
    monkeypatch.setenv("HOME", str(sqlite_test_db.parent))
    monkeypatch.setenv("USERPROFILE", str(sqlite_test_db.parent))

    home_path = f"~/{sqlite_test_db.name}"
    canonical = validate_sqlite_database_path(home_path)
    assert canonical == str(sqlite_test_db.resolve())


def test_sqlite_path_traversal_to_nonexistent(tmp_path: Path):
    """Path traversal containing '..' resolving to non-existent location is rejected."""
    traversal_path = tmp_path / "subdir" / ".." / "nonexistent.db"
    with pytest.raises(ProfileConfigurationError, match="does not exist"):
        validate_sqlite_database_path(str(traversal_path))


# ==============================================================================
# 5. UNC Paths and Windows Device Names
# ==============================================================================


@pytest.mark.parametrize(
    "unc_path",
    [
        r"\\server\share\database.db",
        r"\\192.168.1.1\share\database.db",
        r"\\localhost\c$\database.db",
        r"//server/share/database.db",
        r"//127.0.0.1/share/database.db",
    ],
)
def test_sqlite_unc_paths_rejected(unc_path: str):
    """UNC paths are rejected upfront."""
    with pytest.raises(ProfileConfigurationError, match="UNC network paths"):
        validate_sqlite_database_path(unc_path)


@pytest.mark.parametrize(
    "device_prefix",
    [
        r"\\?\C:\database.db",
        r"\\.\COM1",
        r"\\.\pipe\test",
    ],
)
def test_sqlite_windows_extended_device_paths_rejected(device_prefix: str):
    """Windows device namespace and extended paths are rejected."""
    with pytest.raises(ProfileConfigurationError, match="Windows extended-length paths|Windows device paths"):
        validate_sqlite_database_path(device_prefix)


@pytest.mark.parametrize("dev_name", list(DOS_DEVICE_NAMES)[:8])
def test_sqlite_dos_device_names_rejected(dev_name: str, tmp_path: Path):
    """DOS reserved device names (CON, NUL, AUX, PRN, COM1, LPT1, etc.) are rejected."""
    path_with_device = str(tmp_path / f"{dev_name}.db")
    with pytest.raises(ProfileConfigurationError, match="DOS reserved device name"):
        validate_sqlite_database_path(path_with_device)


@pytest.mark.parametrize(
    "dev_with_ext",
    [
        "NUL.db",
        "CON.txt",
        "PRN.sqlite",
        "AUX.db",
        "COM1.log",
        "LPT1.dat",
        "conin$.db",
        "conout$.db",
        "subdir/NUL.db",
        r"subdir\CON.sqlite3",
    ],
)
def test_sqlite_dos_device_names_with_extensions_rejected(dev_with_ext: str):
    """DOS reserved device names with arbitrary extensions are strictly rejected."""
    with pytest.raises(ProfileConfigurationError, match="DOS reserved device name"):
        validate_sqlite_database_path(dev_with_ext)


@pytest.mark.parametrize(
    "ads_path",
    [
        "test.db:stream",
        "test.db:hidden:$DATA",
        "test.db::$DATA",
        r"C:\data\test.db:stream",
        r"D:\sqlite\app.db:payload:$DATA",
    ],
)
def test_sqlite_alternate_data_streams_rejected(ads_path: str):
    """Alternate Data Streams (ADS) and ':' stream specifiers are strictly rejected."""
    with pytest.raises(ProfileConfigurationError, match="Alternate Data Streams"):
        validate_sqlite_database_path(ads_path)


@pytest.mark.parametrize(
    "trailing_path",
    [
        "test.db.",
        "test.db..",
        "test.db ",
        "test.db   ",
        "folder ./test.db",
        r"folder.\test.db",
    ],
)
def test_sqlite_trailing_dots_and_spaces_rejected(trailing_path: str):
    """Components with trailing spaces or dots (Win32 normalization bypass) are rejected."""
    with pytest.raises(ProfileConfigurationError, match="trailing spaces or dots"):
        validate_sqlite_database_path(trailing_path)


@pytest.mark.parametrize(
    "env_file",
    [
        ".env",
        ".env.local",
        ".env.production",
        ".env.staging",
        ".env.development",
        ".env.test",
        ".env.backup",
        "subdir/.env.local",
        r"subdir\.env.production",
    ],
)
def test_sqlite_environment_files_rejected(env_file: str, tmp_path: Path):
    """Environment files (.env, .env.local, .env.production, etc.) are strictly rejected."""
    target = tmp_path / env_file
    target.parent.mkdir(parents=True, exist_ok=True)
    target.touch()

    with pytest.raises(ProfileConfigurationError, match="environment / secret files"):
        validate_sqlite_database_path(str(target))


@pytest.mark.parametrize(
    "git_path",
    [
        ".git",
        ".git/config",
        ".gitignore",
        ".gitmodules",
        ".gitattributes",
        "repo/.git/HEAD",
    ],
)
def test_sqlite_git_components_rejected(git_path: str, tmp_path: Path):
    """Source control (.git, .gitignore, .gitmodules) paths and subpaths are strictly rejected."""
    target = tmp_path / git_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.touch()

    with pytest.raises(ProfileConfigurationError, match="source control"):
        validate_sqlite_database_path(str(target))


@pytest.mark.parametrize(
    "prohibited_name",
    [
        ":memory:",
        "file:test.db?mode=ro",
        "sqlite:///:memory:",
        "test.db\x00extra",
        "test.db\r\n",
    ],
)
def test_sqlite_prohibited_characters_and_uris_rejected(prohibited_name: str):
    """In-memory markers, URI schemes, null bytes, and control characters are rejected."""
    with pytest.raises(ProfileConfigurationError):
        validate_sqlite_database_path(prohibited_name)


# ==============================================================================
# 6. Protected State and Sensitive Directories
# ==============================================================================


@pytest.mark.parametrize(
    "protected_segment",
    [
        ".query_pilot",
        ".git",
        ".env",
        "agent.token",
        "profiles.json",
    ],
)
def test_sqlite_protected_state_and_secrets_rejected(protected_segment: str, tmp_path: Path):
    """Paths referencing QueryPilot internal state, tokens, profiles, or git are rejected."""
    secret_dir = tmp_path / protected_segment
    secret_dir.mkdir(exist_ok=True)
    target = secret_dir / "target.db"
    target.touch()

    with pytest.raises(ProfileConfigurationError, match="strictly prohibited"):
        validate_sqlite_database_path(str(target))


def test_sqlite_windows_system_root_rejected():
    """Windows system root directories are rejected."""
    win_dir = os.environ.get("SystemRoot", r"C:\Windows")
    # Point to an existing file in SystemRoot (e.g. notepad.exe or explorer.exe) or test with must_exist=False
    existing_file = Path(win_dir) / "notepad.exe"
    if not existing_file.exists():
        existing_file = Path(win_dir) / "explorer.exe"

    if existing_file.exists():
        with pytest.raises(ProfileConfigurationError, match="operating-system directory"):
            validate_sqlite_database_path(str(existing_file))
    else:
        target = os.path.join(win_dir, "System32", "test.db")
        with pytest.raises(ProfileConfigurationError, match="operating-system directory"):
            validate_sqlite_database_path(target, must_exist=False)


def test_sqlite_symlink_to_protected_file_rejected(tmp_path: Path):
    """Symlink pointing to a protected file (.env) resolves to target and is rejected."""
    env_file = tmp_path / ".env"
    env_file.write_text("SECRET=123", encoding="utf-8")

    link_path = tmp_path / "link_to_env.db"
    try:
        link_path.symlink_to(env_file)
    except (OSError, NotImplementedError):
        pytest.skip("Symlinks not supported on this platform/privilege level")

    with pytest.raises(ProfileConfigurationError, match="strictly prohibited"):
        validate_sqlite_database_path(str(link_path))


# ==============================================================================
# 7. Invalid Engine Types
# ==============================================================================


@pytest.mark.parametrize("invalid_engine", ["postgres", "mysql", "oracle", "mariadb", ""])
def test_invalid_engine_type_rejected(invalid_engine: str, sqlite_test_db: Path):
    """Unsupported engine types are rejected by ConnectionProfile and ValidateProfileConfigRequest."""
    with pytest.raises(ValueError, match="Unsupported engine_type"):
        ConnectionProfile(
            profile_id="bad_engine",
            display_name="Bad Engine",
            engine_type=invalid_engine,
            database_path=str(sqlite_test_db),
        )

    with pytest.raises(ValueError, match="Unsupported engine_type"):
        ValidateProfileConfigRequest(
            profile_id="bad_engine_req",
            display_name="Bad Engine Request",
            engine_type=invalid_engine,
            database_path=str(sqlite_test_db),
        )

    with pytest.raises(ProfileConfigurationError, match="Unsupported engine_type"):
        validate_safe_profile_data({
            "profile_id": "bad_engine_raw",
            "display_name": "Bad Engine Raw",
            "engine_type": invalid_engine,
            "database_path": str(sqlite_test_db),
        })


# ==============================================================================
# 8. SQLite Profiles Blocked from MSSQL Connection Construction
# ==============================================================================


def test_sqlite_profile_blocked_from_odbc_connection_string(sqlite_test_db: Path):
    """get_odbc_connection_string() raises ProfileConfigurationError for SQLite profiles."""
    profile = ConnectionProfile(
        profile_id="sqlite_prof",
        display_name="SQLite Prof",
        engine_type="sqlite",
        database_path=str(sqlite_test_db),
    )
    with pytest.raises(ProfileConfigurationError, match="SQLite profiles do not use ODBC connection strings"):
        profile.get_odbc_connection_string()


def test_sqlite_profile_sqlalchemy_url(sqlite_test_db: Path):
    """get_sqlalchemy_url() produces read-only URI for SQLite profiles."""
    profile = ConnectionProfile(
        profile_id="sqlite_prof",
        display_name="SQLite Prof",
        engine_type="sqlite",
        database_path=str(sqlite_test_db),
    )
    url = profile.get_sqlalchemy_url()
    assert url.startswith("sqlite:///file:")
    assert "mode=ro" in url
    assert "uri=true" in url


def test_sqlite_profile_factory_configuration(sqlite_test_db: Path):
    """ProfileConnectionFactory.get_configuration() derives configuration for SQLite profiles."""
    profile = ConnectionProfile(
        profile_id="sqlite_prof",
        display_name="SQLite Prof",
        engine_type="sqlite",
        database_path=str(sqlite_test_db),
    )
    store = ProfileStore()
    store.register_profile(profile)
    factory = ProfileConnectionFactory(store=store)
    config = factory.get_configuration("sqlite_prof")
    assert config.engine_type == "sqlite"
    assert config.expected_db == profile.expected_db
    assert config.database_path == profile.database_path


def test_sqlite_profile_db_engine_creation(sqlite_test_db: Path):
    """create_profile_db_engine() creates a valid SQLite Engine for SQLite profiles."""
    profile = ConnectionProfile(
        profile_id="sqlite_prof",
        display_name="SQLite Prof",
        engine_type="sqlite",
        database_path=str(sqlite_test_db),
    )
    engine = create_profile_db_engine(profile)
    assert engine is not None
    engine.dispose()


# ==============================================================================
# 9. Zero Database Creation Invariant
# ==============================================================================


def test_sqlite_validation_never_creates_missing_file(tmp_path: Path):
    """Validating a non-existent SQLite path fails without creating the file on disk."""
    candidate = tmp_path / "never_created.db"
    assert not candidate.exists()

    with pytest.raises(ProfileConfigurationError):
        validate_sqlite_database_path(str(candidate))

    # Assert that the file was NOT created as a side effect
    assert not candidate.exists()


# ==============================================================================
# 10. CLI Integration: profile add --engine sqlite and profile list
# ==============================================================================


def test_cli_profile_add_sqlite_success(sqlite_test_db: Path, profiles_json_path: Path):
    """querypilot-agent profile add --engine sqlite creates and persists a valid SQLite profile."""
    cli_args = [
        "profile",
        "add",
        "--id",
        "cli_sqlite_1",
        "--name",
        "CLI SQLite Profile",
        "--engine",
        "sqlite",
        "--path",
        str(sqlite_test_db),
        "--yes",
        "--profiles-file",
        str(profiles_json_path),
    ]

    ret = run_agent_cli(cli_args)
    assert ret == 0

    # Verify store content
    store = load_trusted_profiles_file(profiles_json_path)
    assert store.has_profile("cli_sqlite_1")
    prof = store.get_profile("cli_sqlite_1")
    assert prof.engine_type == "sqlite"
    assert prof.database_path == str(sqlite_test_db.resolve())
    assert prof.database_name == sqlite_test_db.stem
    assert prof.schema_name == "main"


def test_cli_profile_add_sqlite_missing_path_rejected(profiles_json_path: Path):
    """querypilot-agent profile add --engine sqlite without --path fails cleanly."""
    cli_args = [
        "profile",
        "add",
        "--id",
        "cli_sqlite_fail",
        "--engine",
        "sqlite",
        "--yes",
        "--profiles-file",
        str(profiles_json_path),
    ]

    captured_err = io.StringIO()
    with patch("sys.stderr", captured_err), patch("sys.stdin.isatty", return_value=False):
        ret = run_agent_cli(cli_args)

    assert ret != 0
    assert "--path / database_path is required" in captured_err.getvalue()


def test_cli_profile_list_sqlite_display(sqlite_test_db: Path, profiles_json_path: Path):
    """querypilot-agent profile list displays SQLite-specific details and omits SQL Server fields."""
    store = ProfileStore()
    prof = ConnectionProfile(
        profile_id="list_sqlite_prof",
        display_name="List SQLite DB",
        engine_type="sqlite",
        database_path=str(sqlite_test_db),
    )
    store.register_profile(prof)
    store.set_default_profile("list_sqlite_prof")
    save_trusted_profiles_file(profiles_json_path, store)

    captured_out = io.StringIO()
    with patch("sys.stdout", captured_out):
        ret = run_agent_cli(["profile", "list", "--profiles-file", str(profiles_json_path)])

    assert ret == 0
    out = captured_out.getvalue()
    assert "Profile ID:    list_sqlite_prof [DEFAULT]" in out
    assert "Display Name:  List SQLite DB" in out
    assert "Engine Type:   sqlite" in out
    assert f"Database Path: {sqlite_test_db.resolve()}" in out
    assert "ODBC Driver:" not in out
    assert "Trusted Auth:  Windows Integrated Authentication" not in out


# ==============================================================================
# 11. Agent HTTP Endpoint Protection for SQLite in Phase 2
# ==============================================================================


@pytest.fixture
def agent_client_with_sqlite(sqlite_test_db: Path, tmp_path: Path):
    """Create a test TestClient with a registered SQLite profile."""
    token_dir = tmp_path / "tokens"
    token_dir.mkdir()
    profiles_file = tmp_path / "profiles.json"

    store = ProfileStore()
    sqlite_prof = ConnectionProfile(
        profile_id="sqlite_active",
        display_name="Active SQLite DB",
        engine_type="sqlite",
        database_path=str(sqlite_test_db),
    )
    store.register_profile(sqlite_prof)
    store.set_default_profile("sqlite_active")
    save_trusted_profiles_file(profiles_file, store)

    token_manager = BootstrapTokenManager(token_dir=token_dir)
    token = token_manager.initialize_token()

    config = AgentConfig(
        bind_host="127.0.0.1",
        bind_port=8001,
        token_dir=token_dir,
        profiles_file=profiles_file,
    )
    app = create_agent_app(
        config=config,
        store=store,
        token_manager=token_manager,
    )
    client = TestClient(app, base_url="http://127.0.0.1:8001")

    # Perform bootstrap
    boot_resp = client.post(
        "/agent/v1/auth/bootstrap",
        headers={"Host": "127.0.0.1:8001", "X-QueryPilot-Request": "1"},
        json={"bootstrap_token": token},
    )
    assert boot_resp.status_code == status.HTTP_200_OK
    session_id = boot_resp.cookies.get(SESSION_COOKIE_NAME)
    return client, session_id


def test_sqlite_test_connection_endpoint_in_phase3(agent_client_with_sqlite):
    """POST /agent/v1/profiles/{id}/test-connection succeeds for SQLite in Phase 3."""
    client, session_id = agent_client_with_sqlite
    resp = client.post(
        "/agent/v1/profiles/sqlite_active/test-connection",
        headers={"Host": "127.0.0.1:8001", "X-QueryPilot-Request": "1"},
        cookies={SESSION_COOKIE_NAME: session_id},
    )
    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert data["connected"] is True
    assert data["failure_code"] is None
    assert data["database_name"] == "valid_test"


def test_sqlite_schema_introspection_endpoint_in_phase3(agent_client_with_sqlite):
    """GET /agent/v1/profiles/{id}/schema returns HTTP 200 with schema metadata for SQLite."""
    client, session_id = agent_client_with_sqlite
    resp = client.get(
        "/agent/v1/profiles/sqlite_active/schema",
        headers={"Host": "127.0.0.1:8001", "X-QueryPilot-Request": "1"},
        cookies={SESSION_COOKIE_NAME: session_id},
    )
    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert data["database_name"] == "valid_test"
    tables = [t["table_name"] for t in data["tables"]]
    assert "products" in tables


def test_sqlite_query_execution_endpoint_in_phase3(agent_client_with_sqlite):
    """POST /agent/v1/profiles/{id}/execute returns HTTP 200 with query results for SQLite."""
    client, session_id = agent_client_with_sqlite
    resp = client.post(
        "/agent/v1/profiles/sqlite_active/execute",
        headers={"Host": "127.0.0.1:8001", "X-QueryPilot-Request": "1"},
        cookies={SESSION_COOKIE_NAME: session_id},
        json={"sql": "SELECT id, name, price FROM products"},
    )
    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert data["columns"] == ["id", "name", "price"]
    assert len(data["rows"]) == 1
    assert data["rows"][0]["name"] == "Widget"


# ==============================================================================
# B24-F.1 Profile Summary Contract & Safe Metadata Tests
# ==============================================================================

def test_sqlite_profile_list_safe_summary(agent_client_with_sqlite):
    """GET /agent/v1/profiles returns explicit engine_type='sqlite', zero database_path or credentials."""
    client, session_id = agent_client_with_sqlite
    resp = client.get(
        "/agent/v1/profiles",
        headers={"Host": "127.0.0.1:8001"},
        cookies={SESSION_COOKIE_NAME: session_id},
    )
    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert "profiles" in data
    sqlite_prof = next((p for p in data["profiles"] if p["profile_id"] == "sqlite_active"), None)
    assert sqlite_prof is not None
    assert sqlite_prof["display_name"] == "Active SQLite DB"
    assert sqlite_prof["engine_type"] == "sqlite"
    assert sqlite_prof["schema_name"] == "main"
    assert sqlite_prof["database_name"] == "valid_test"
    assert sqlite_prof["timeout_seconds"] == 15
    assert sqlite_prof["max_row_limit"] == 100

    # Ensure sensitive fields are NEVER in response dict
    assert "database_path" not in sqlite_prof
    assert "server" not in sqlite_prof
    assert "driver" not in sqlite_prof
    assert "password" not in sqlite_prof
    assert "connection_string" not in sqlite_prof

    # Ensure raw file path does not leak into the JSON payload string
    resp_text = resp.text.lower()
    assert ".db" not in resp_text
    assert "valid_test.db" not in resp_text


def test_sqlite_profile_create_safe_summary(agent_client_with_sqlite, tmp_path):
    """POST /agent/v1/profiles creates SQLite profile and returns safe summary with engine_type='sqlite'."""
    client, session_id = agent_client_with_sqlite

    # Create dummy sqlite db
    new_db = tmp_path / "summary_create_test.db"
    conn = sqlite3.connect(str(new_db))
    conn.execute("CREATE TABLE t (x INT)")
    conn.commit()
    conn.close()

    payload = {
        "profile_id": "sqlite_new_summary",
        "display_name": "New SQLite Summary",
        "engine_type": "sqlite",
        "database_path": str(new_db),
        "timeout_seconds": 8,
        "max_row_limit": 25,
    }
    resp = client.post(
        "/agent/v1/profiles",
        headers={"Host": "127.0.0.1:8001", "X-QueryPilot-Request": "1"},
        cookies={SESSION_COOKIE_NAME: session_id},
        json=payload,
    )
    assert resp.status_code == status.HTTP_201_CREATED
    data = resp.json()
    summary = data["profile"]
    assert summary["profile_id"] == "sqlite_new_summary"
    assert summary["display_name"] == "New SQLite Summary"
    assert summary["engine_type"] == "sqlite"
    assert summary["schema_name"] == "main"
    assert summary["database_name"] == "summary_create_test"
    assert summary["timeout_seconds"] == 8
    assert summary["max_row_limit"] == 25

    # Strictly verify zero path leakage in summary
    assert "database_path" not in summary
    assert "path" not in summary
    assert "server" not in summary
    assert str(new_db) not in resp.text


def test_legacy_profile_without_engine_type_defaults_to_mssql():
    """Verify ConnectionProfile and ProfileSummary default to mssql when engine_type is omitted."""
    raw_legacy = {
        "profile_id": "legacy_corp",
        "display_name": "Legacy Corp DW",
        "server": "localhost",
        "database_name": "LegacyDB",
        "driver": "ODBC Driver 17 for SQL Server",
    }
    prof = ConnectionProfile(**raw_legacy)
    assert prof.engine_type == "mssql"

    summary = prof.to_safe_summary()
    assert summary["engine_type"] == "mssql"
    assert summary["database_name"] == "LegacyDB"
    assert "database_path" not in summary

    # Verify agent ProfileSummary default
    api_summary = ProfileSummary(
        profile_id="legacy_prof",
        display_name="Legacy Profile",
        timeout_seconds=10,
        max_row_limit=50,
        has_allowlist=False,
    )
    assert api_summary.engine_type == "mssql"
    assert api_summary.schema_name == "dbo"
    assert api_summary.database_name is None

