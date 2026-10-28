"""Command-line interface for the QueryPilot Local Agent Daemon.

Supports running the agent daemon locally on loopback (127.0.0.1:8001)
with optional browser launch for opening the production UI at http://127.0.0.1:8001/app/.
"""

from __future__ import annotations

import argparse
import datetime
import os
from pathlib import Path
import platform
import sys
import threading
import time
from typing import Callable, Optional
import urllib.request
import webbrowser

from pydantic import ValidationError

from query_pilot.agent.app import create_agent_app
from query_pilot.agent.auth import (
    BootstrapTokenManager,
    BootstrapTokenProvisioningError,
)
from query_pilot.agent.config import (
    AgentConfig,
    _get_env_bool,
    _get_env_int,
    _get_env_path,
    _get_env_str,
    get_default_profiles_file,
    get_default_token_dir,
    is_loopback_host,
)
from query_pilot.agent.models import ValidateProfileConfigRequest
from query_pilot.db.profile import (
    ConnectionProfile,
    ProfileConfigurationError,
    ProfileStore,
    load_trusted_profiles_file,
    profile_file_lock,
    save_trusted_profiles_file,
)


def _launch_browser_when_ready(
    url: str,
    timeout: float = 5.0,
    opener: Optional[Callable[[str], bool]] = None,
) -> bool:
    """Poll the agent endpoint in a background thread and open the default browser once ready.

    Returns:
        True if the endpoint became ready and browser was opened, False if timed out or failed.
    """
    open_func = opener or webbrowser.open
    deadline = time.time() + timeout
    ready = False
    while time.time() < deadline:
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=0.5):
                ready = True
                break
        except Exception:
            time.sleep(0.2)

    if ready:
        open_func(url)
        return True

    sys.stderr.write(
        f"Browser launch skipped: Agent daemon did not become ready at {url} within {timeout:.1f}s.\n"
    )
    return False


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the agent CLI.

    Default values respect environment variables if set, falling back to code defaults.
    CLI arguments explicitly supplied at launch take highest precedence.
    Supports subcommands for profile onboarding: 'profile add', 'profile list', 'profile validate'.
    """
    parser = argparse.ArgumentParser(
        prog="query-pilot-agent",
        description="QueryPilot Local Agent Daemon - loopback service for local execution and frontend serving.",
    )
    parser.add_argument(
        "--host",
        type=str,
        default=_get_env_str(["QUERYPILOT_AGENT_HOST", "QUERYPILOT_AGENT_BIND_HOST"], "127.0.0.1"),
        help="Loopback host to bind (default: 127.0.0.1 or QUERYPILOT_AGENT_HOST). Non-loopback addresses fail closed.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=_get_env_int(["QUERYPILOT_AGENT_PORT"], 8001),
        help="TCP port to bind (default: 8001 or QUERYPILOT_AGENT_PORT).",
    )
    parser.add_argument(
        "--allow-dev-origin",
        action=argparse.BooleanOptionalAction,
        default=_get_env_bool(["QUERYPILOT_AGENT_ALLOW_DEV_ORIGIN"], False),
        help="Permit Vite development server origin (http://127.0.0.1:5173). Default is False or QUERYPILOT_AGENT_ALLOW_DEV_ORIGIN.",
    )
    parser.add_argument(
        "--static-dir",
        type=Path,
        default=_get_env_path(["QUERYPILOT_AGENT_STATIC_DIR"], None),
        help="Path to frontend production build directory (defaults to frontend/dist or QUERYPILOT_AGENT_STATIC_DIR).",
    )
    parser.add_argument(
        "--upstream-api-url",
        type=str,
        default=_get_env_str(["QUERYPILOT_UPSTREAM_API_URL", "QUERYPILOT_AGENT_UPSTREAM_API_URL"], "http://127.0.0.1:8000"),
        help="Trusted upstream URL for QueryPilot Hosted API (default: http://127.0.0.1:8000 or QUERYPILOT_UPSTREAM_API_URL).",
    )
    parser.add_argument(
        "--token-dir",
        type=Path,
        default=get_default_token_dir(),
        help="Path to directory for bootstrap token file (default: ~/.query_pilot or QUERYPILOT_TOKEN_DIR).",
    )
    parser.add_argument(
        "--profiles-file",
        type=Path,
        default=get_default_profiles_file(),
        help="Path to local database connection profiles file (default: ~/.query_pilot/profiles.json or QUERYPILOT_PROFILES_FILE).",
    )
    parser.add_argument(
        "--open-browser",
        action="store_true",
        default=False,
        help="Open the local UI in the default browser after the daemon is ready (default: False).",
    )
    parser.add_argument(
        "--rotate-token",
        action="store_true",
        default=False,
        help="Rotate bootstrap token and exit (out-of-band operator recovery).",
    )

    # Subparsers for profile management commands
    subparsers = parser.add_subparsers(dest="subcommand", help="Agent subcommands.")

    # auth subparser
    auth_parser = subparsers.add_parser("auth", help="Manage daemon authentication and bootstrap tokens.")
    auth_subparsers = auth_parser.add_subparsers(dest="auth_action", help="Auth actions.")

    # auth rotate
    rotate_parser = auth_subparsers.add_parser("rotate", help="Provision a replacement bootstrap token.")
    rotate_parser.add_argument(
        "--token-dir",
        type=Path,
        default=None,
        help="Path to directory for bootstrap token file (default: ~/.query_pilot or QUERYPILOT_TOKEN_DIR).",
    )
    rotate_parser.add_argument(
        "--show-token",
        action="store_true",
        default=False,
        help="Display the raw token secret on stdout (default: False).",
    )

    # auth info
    info_parser = auth_subparsers.add_parser("info", help="Inspect bootstrap token file state and daemon reachability.")
    info_parser.add_argument(
        "--token-dir",
        type=Path,
        default=None,
        help="Path to directory for bootstrap token file (default: ~/.query_pilot or QUERYPILOT_TOKEN_DIR).",
    )

    profile_parser = subparsers.add_parser("profile", help="Manage approved database connection profiles.")
    profile_subparsers = profile_parser.add_subparsers(dest="profile_action", help="Profile actions.")

    # profile add
    add_parser = profile_subparsers.add_parser("add", help="Register an approved SQL Server database profile.")
    add_parser.add_argument("--id", "--profile-id", dest="profile_id", type=str, required=False, help="Unique profile ID.")
    add_parser.add_argument("--name", "--display-name", dest="display_name", type=str, required=False, help="Human-readable display name.")
    add_parser.add_argument("--server", type=str, required=False, default=None, help="SQL Server instance (default: localhost\\SQLEXPRESS).")
    add_parser.add_argument("--database", "--database-name", dest="database_name", type=str, required=False, default=None, help="Target database name.")
    add_parser.add_argument("--driver", type=str, required=False, default=None, help="ODBC Driver (default: ODBC Driver 17 for SQL Server).")
    add_parser.add_argument("--schema", "--schema-name", dest="schema_name", type=str, required=False, default="dbo", help="Default schema (default: dbo).")
    add_parser.add_argument("--timeout", "--timeout-seconds", dest="timeout_seconds", type=int, required=False, default=15, help="Query timeout seconds (1-15, default: 15).")
    add_parser.add_argument("--max-rows", "--max-row-limit", dest="max_row_limit", type=int, required=False, default=100, help="Max row limit (1-100, default: 100).")
    add_parser.add_argument("--auth", "--auth-mode", dest="auth_mode", type=str, required=False, default="windows", help="Authentication mode (only 'windows' / Windows Integrated Authentication is supported).")
    add_parser.add_argument("--allowed-tables", dest="allowed_tables", type=str, nargs="*", required=False, default=None, help="Optional permitted tables list.")
    add_parser.add_argument("--sensitive-columns", dest="sensitive_columns", type=str, nargs="*", required=False, default=None, help="Optional custom sensitive columns denylist.")
    add_parser.add_argument("--profiles-file", type=Path, default=None, help="Path to profiles JSON file.")
    add_parser.add_argument("--yes", "-y", action="store_true", default=False, help="Confirm profile registration without interactive prompt.")

    # profile list
    list_parser = profile_subparsers.add_parser("list", help="List approved database connection profiles.")
    list_parser.add_argument("--profiles-file", type=Path, default=None, help="Path to profiles JSON file.")

    # profile validate
    val_parser = profile_subparsers.add_parser("validate", help="Validate approved database connection profiles file.")
    val_parser.add_argument("--profiles-file", type=Path, default=None, help="Path to profiles JSON file.")

    return parser


def handle_profile_list(args: argparse.Namespace) -> int:
    """Handle 'querypilot-agent profile list' command."""
    profiles_path = args.profiles_file or get_default_profiles_file()
    if not profiles_path.exists():
        sys.stdout.write(
            f"No profiles file found at '{profiles_path}'.\n"
            "Register an approved database profile with:\n"
            "  querypilot-agent profile add --id <id> --name <name> --database <db>\n"
        )
        return 0

    try:
        store = load_trusted_profiles_file(profiles_path)
    except Exception as exc:
        sys.stderr.write(f"ERROR: Failed to read profiles from '{profiles_path}': {exc}\n")
        return 1

    if len(store) == 0:
        sys.stdout.write(f"Zero profiles configured in '{profiles_path}'.\n")
        return 0

    sys.stdout.write(f"Approved Database Profiles ({len(store)} total) in '{profiles_path}':\n")
    sys.stdout.write("-" * 75 + "\n")
    default_id = store.default_profile_id
    for pid, profile in store.profiles.items():
        is_default_marker = " [DEFAULT]" if pid == default_id else ""
        sys.stdout.write(
            f"Profile ID:    {profile.profile_id}{is_default_marker}\n"
            f"Display Name:  {profile.display_name}\n"
            f"Engine Type:   {getattr(profile, 'engine_type', 'mssql')}\n"
            f"Server:        {profile.server}\n"
            f"Database Name: {profile.database_name}\n"
            f"ODBC Driver:   {profile.driver}\n"
            f"Trusted Auth:  Windows Integrated Authentication (trusted_connection=True)\n"
            f"Timeout:       {profile.timeout_seconds}s (max limit: 15s)\n"
            f"Max Rows:      {profile.max_row_limit} (max limit: 100)\n"
        )
        if profile.allowed_tables:
            sys.stdout.write(f"Allowed Tables: {', '.join(profile.allowed_tables)}\n")
        sys.stdout.write("-" * 75 + "\n")

    return 0


def handle_profile_validate(args: argparse.Namespace) -> int:
    """Handle 'querypilot-agent profile validate' command."""
    profiles_path = args.profiles_file or get_default_profiles_file()
    if not profiles_path.exists():
        sys.stderr.write(
            f"VALIDATION ERROR: Profiles file does not exist at '{profiles_path}'.\n"
            "Register a profile first with 'querypilot-agent profile add'.\n"
        )
        return 1

    try:
        store = load_trusted_profiles_file(profiles_path)
        sys.stdout.write(
            f"SUCCESS: Profiles file at '{profiles_path}' is valid.\n"
            f"Contains {len(store)} approved profile(s): {sorted(store.profiles.keys())}\n"
            f"Default profile: {store.default_profile_id or 'None'}\n"
        )
        return 0
    except Exception as exc:
        sys.stderr.write(f"VALIDATION ERROR: Profiles file at '{profiles_path}' is invalid: {exc}\n")
        return 1


def handle_profile_add(args: argparse.Namespace) -> int:
    """Handle 'querypilot-agent profile add' command."""
    profiles_path = args.profiles_file or get_default_profiles_file()

    profile_id = (args.profile_id or "").strip()
    display_name = (args.display_name or "").strip()
    server = (args.server or "").strip() or r"localhost\SQLEXPRESS"
    database_name = (args.database_name or "").strip()
    driver = (args.driver or "").strip() or "ODBC Driver 17 for SQL Server"
    schema_name = (args.schema_name or "dbo").strip() or "dbo"

    # Prompt interactively if missing required arguments in an interactive terminal
    if not profile_id:
        if sys.stdin.isatty():
            profile_id = input("Enter unique profile ID (e.g. 'local_db'): ").strip()
        if not profile_id:
            sys.stderr.write("ERROR: --id / profile_id is required.\n")
            return 1

    if not database_name:
        if sys.stdin.isatty():
            database_name = input("Enter SQL Server database name: ").strip()
        if not database_name:
            sys.stderr.write("ERROR: --database / database_name is required.\n")
            return 1

    if not display_name:
        display_name = f"SQL Server ({database_name})"

    auth_mode = (getattr(args, "auth_mode", "windows") or "windows").strip().lower()
    if auth_mode not in {"windows", "integrated"}:
        sys.stderr.write(
            "CONFIGURATION ERROR: Invalid profile parameters: SECURITY REJECTION: "
            f"Unsupported authentication mode '{auth_mode}'. "
            "QueryPilot currently requires Windows Integrated Authentication (use_trusted_connection=True).\n"
        )
        return 1

    timeout_seconds = getattr(args, "timeout_seconds", 15)
    if timeout_seconds is None:
        timeout_seconds = 15

    max_row_limit = getattr(args, "max_row_limit", 100)
    if max_row_limit is None:
        max_row_limit = 100

    allowed_tables = getattr(args, "allowed_tables", None)
    sensitive_columns = getattr(args, "sensitive_columns", None)

    try:
        validated_req = ValidateProfileConfigRequest(
            profile_id=profile_id,
            display_name=display_name,
            server=server,
            database_name=database_name,
            schema_name=schema_name,
            driver=driver,
            engine_type="mssql",
            use_trusted_connection=True,
            timeout_seconds=timeout_seconds,
            max_row_limit=max_row_limit,
            allowed_tables=allowed_tables,
            sensitive_columns=sensitive_columns,
        )
    except ValidationError as exc:
        first_error = exc.errors()[0]
        msg = first_error.get("msg", "Invalid configuration parameter.")
        if msg.startswith("Value error, "):
            msg = msg[len("Value error, "):]
        sys.stderr.write(f"CONFIGURATION ERROR: Invalid profile parameters: {msg}\n")
        return 1
    except Exception as exc:
        sys.stderr.write(f"CONFIGURATION ERROR: Invalid profile parameters: {exc}\n")
        return 1

    new_profile = ConnectionProfile(
        profile_id=validated_req.profile_id,
        display_name=validated_req.display_name,
        engine_type="mssql",
        server=validated_req.server,
        database_name=validated_req.database_name,
        driver=validated_req.driver,
        use_trusted_connection=True,
        timeout_seconds=validated_req.timeout_seconds,
        max_row_limit=validated_req.max_row_limit,
        schema_name=validated_req.schema_name,
        allowed_tables=tuple(validated_req.allowed_tables) if validated_req.allowed_tables is not None else None,
        sensitive_columns=tuple(validated_req.sensitive_columns) if validated_req.sensitive_columns is not None else None,
    )

    # Require explicit confirmation unless --yes is passed
    if not getattr(args, "yes", False):
        if sys.stdin.isatty():
            sys.stdout.write(
                f"\nProposed Profile Configuration:\n"
                f"  Profile ID:    {new_profile.profile_id}\n"
                f"  Display Name:  {new_profile.display_name}\n"
                f"  Engine Type:   {new_profile.engine_type}\n"
                f"  Server:        {new_profile.server}\n"
                f"  Database Name: {new_profile.database_name}\n"
                f"  ODBC Driver:   {new_profile.driver}\n"
                f"  Auth:          Windows Integrated Authentication (trusted_connection=True)\n"
                f"  Timeout:       {new_profile.timeout_seconds}s\n"
                f"  Max Rows:      {new_profile.max_row_limit}\n"
                f"Target file:     {profiles_path}\n\n"
            )
            confirm = input("Confirm registering this profile? [y/N]: ").strip().lower()
            if confirm not in {"y", "yes"}:
                sys.stdout.write("Profile registration cancelled.\n")
                return 0
        else:
            sys.stderr.write("ERROR: Non-interactive execution requires --yes / -y flag to confirm profile creation.\n")
            return 1

    try:
        with profile_file_lock(profiles_path):
            # Load existing store if present or create new
            store: ProfileStore
            if profiles_path.exists():
                try:
                    store = load_trusted_profiles_file(profiles_path)
                except Exception as exc:
                    sys.stderr.write(
                        f"ERROR: Cannot add profile because existing file at '{profiles_path}' is corrupted: {exc}\n"
                    )
                    return 1
            else:
                store = ProfileStore()

            if store.has_profile(new_profile.profile_id):
                sys.stderr.write(
                    f"ERROR: Profile ID '{new_profile.profile_id}' already exists in '{profiles_path}'. Duplicate profile IDs are not permitted.\n"
                )
                return 1

            store.register_profile(new_profile)
            if store.default_profile_id is None:
                store.set_default_profile(new_profile.profile_id)

            save_trusted_profiles_file(profiles_path, store)
            sys.stdout.write(
                f"SUCCESS: Profile '{new_profile.profile_id}' registered successfully in '{profiles_path}'.\n"
                f"Permissions hardened: restricted exclusively to current user.\n"
            )
            return 0
    except Exception as exc:
        sys.stderr.write(f"ERROR: Failed to save profile to '{profiles_path}': {exc}\n")
        return 1


def handle_auth_rotate(args: argparse.Namespace) -> int:
    """Handle 'querypilot-agent auth rotate' and '--rotate-token' commands."""
    resolved_dir = getattr(args, "token_dir", None) or get_default_token_dir()
    show_token = getattr(args, "show_token", False)

    try:
        manager = BootstrapTokenManager(token_dir=resolved_dir)
        new_token = manager.initialize_token()
    except Exception as exc:
        sys.stderr.write(f"ERROR: Failed to provision bootstrap token: {exc}\n")
        return 1

    sys.stdout.write(
        f"SUCCESS: Bootstrap token provisioned successfully at '{manager.token_path}'.\n"
        "Permissions: Verified restricted exclusively to current user.\n"
        "ACTION REQUIRED: Restart the QueryPilot local agent daemon to activate this token and clear in-memory sessions.\n"
    )
    if show_token:
        sys.stdout.write(f"Token: {new_token}\n")

    return 0


def handle_auth_info(args: argparse.Namespace) -> int:
    """Handle 'querypilot-agent auth info' command."""
    resolved_dir = getattr(args, "token_dir", None) or get_default_token_dir()
    manager = BootstrapTokenManager(token_dir=resolved_dir)
    token_path = manager.token_path

    if not token_path.exists():
        file_status = "Not present (consumed by bootstrap, deleted, or uninitialized)"
        perm_status = "N/A"
        mtime_str = "N/A"
    else:
        if token_path.is_symlink() or os.path.islink(str(token_path)):
            file_status = "WARNING: Symlink or reparse point detected (untrusted)"
            perm_status = "REJECTED (symlinks not permitted)"
        elif not token_path.is_file():
            file_status = "WARNING: Path is not a regular file"
            perm_status = "REJECTED (non-regular file)"
        else:
            file_status = "Present on disk (pending daemon startup or browser bootstrap)"
            if platform.system() != "Windows":
                st = token_path.stat()
                if (st.st_mode & 0o077) != 0:
                    perm_status = f"WARNING: Insecure mode {oct(st.st_mode)} (expected 0600)"
                else:
                    perm_status = "Verified mode 0600 (restricted to user)"
            else:
                try:
                    manager._verify_windows_acl(token_path)
                    perm_status = "Verified restricted to current user via DACL"
                except Exception as exc:
                    perm_status = f"WARNING: DACL verification failed ({exc})"

        try:
            mtime_epoch = token_path.stat().st_mtime
            mtime_str = datetime.datetime.fromtimestamp(mtime_epoch, tz=datetime.timezone.utc).isoformat()
        except Exception:
            mtime_str = "Unknown"

    daemon_host = getattr(args, "host", "127.0.0.1")
    daemon_port = getattr(args, "port", 8001)
    daemon_status = "Not reachable on loopback (daemon appears stopped)"
    try:
        req = urllib.request.Request(f"http://{daemon_host}:{daemon_port}/app/", method="GET")
        with urllib.request.urlopen(req, timeout=0.5):
            daemon_status = f"Reachable at http://{daemon_host}:{daemon_port} (Note: Daemon may hold a different in-memory token until restarted)"
    except Exception:
        pass

    sys.stdout.write(
        "QueryPilot Bootstrap Token Diagnostic Info:\n"
        f"  Token Directory:    {resolved_dir}\n"
        f"  Token File Path:    {token_path}\n"
        f"  Token File Status:  {file_status}\n"
        f"  Permissions Status: {perm_status}\n"
        f"  Last Modified (UTC):{mtime_str}\n"
        f"  Daemon Status:      {daemon_status}\n\n"
        "[DIAGNOSTIC NOTICE] The file and daemon states above are point-in-time diagnostics.\n"
        "File presence does not guarantee that a running daemon has adopted the token.\n"
        "If the daemon was already running when the token was provisioned, restart the daemon.\n"
    )
    return 0


def run_agent_cli(args: Optional[list[str]] = None) -> int:
    """Parse arguments and execute agent command or start daemon."""
    parser = build_parser()
    parsed = parser.parse_args(args)

    # Check for legacy --rotate-token flag
    if getattr(parsed, "rotate_token", False):
        return handle_auth_rotate(parsed)

    # Route auth subcommands
    if parsed.subcommand == "auth":
        if parsed.auth_action == "rotate":
            return handle_auth_rotate(parsed)
        elif parsed.auth_action == "info":
            return handle_auth_info(parsed)
        else:
            parser.parse_args(["auth", "--help"])
            return 1

    # Route profile subcommands
    if parsed.subcommand == "profile":
        if parsed.profile_action == "add":
            return handle_profile_add(parsed)
        elif parsed.profile_action == "list":
            return handle_profile_list(parsed)
        elif parsed.profile_action == "validate":
            return handle_profile_validate(parsed)
        else:
            parser.parse_args(["profile", "--help"])
            return 1

    # Daemon launch path (subcommand is None)
    if not is_loopback_host(parsed.host):
        sys.stderr.write(
            f"SECURITY ERROR: Bind host '{parsed.host}' is not a loopback address. "
            "The QueryPilot Local Agent daemon only permits loopback binding (127.0.0.1, localhost, ::1).\n"
        )
        return 1

    try:
        config = AgentConfig(
            bind_host=parsed.host,
            bind_port=parsed.port,
            allow_dev_origin=parsed.allow_dev_origin,
            static_dir=parsed.static_dir,
            upstream_api_url=parsed.upstream_api_url,
            token_dir=parsed.token_dir,
            profiles_file=parsed.profiles_file,
        )
    except Exception as exc:
        sys.stderr.write(f"CONFIGURATION ERROR: Invalid agent configuration: {exc}\n")
        return 1

    try:
        app = create_agent_app(config=config)
    except Exception as exc:
        sys.stderr.write(f"CONFIGURATION ERROR: Failed to initialize agent application: {exc}\n")
        return 1

    app_url = f"http://{config.bind_host}:{config.bind_port}/app/"

    if parsed.open_browser:
        thread = threading.Thread(
            target=_launch_browser_when_ready,
            args=(app_url,),
            daemon=True,
        )
        thread.start()

    try:
        import uvicorn
        uvicorn.run(app, host=config.bind_host, port=config.bind_port, log_level="info")
        return 0
    except ImportError:
        sys.stderr.write("uvicorn is required to run the local agent server.\n")
        return 1


def main() -> None:
    """Console script entry point for querypilot-agent."""
    sys.exit(run_agent_cli())


if __name__ == "__main__":
    main()
