"""Command-line interface for the QueryPilot Local Agent Daemon.

Supports running the agent daemon locally on loopback (127.0.0.1:8001)
with optional browser launch for opening the production UI at http://127.0.0.1:8001/app/.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import threading
import time
from typing import Callable, Optional
import urllib.request
import webbrowser

from query_pilot.agent.app import create_agent_app
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

    # Subparsers for profile management commands
    subparsers = parser.add_subparsers(dest="subcommand", help="Agent subcommands.")

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
    timeout_seconds = min(max(int(args.timeout_seconds or 15), 1), 15)
    max_row_limit = min(max(int(args.max_row_limit or 100), 1), 100)

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

    try:
        new_profile = ConnectionProfile(
            profile_id=profile_id,
            display_name=display_name,
            engine_type="mssql",
            server=server,
            database_name=database_name,
            driver=driver,
            use_trusted_connection=True,
            timeout_seconds=timeout_seconds,
            max_row_limit=max_row_limit,
            schema_name=schema_name,
        )
    except Exception as exc:
        sys.stderr.write(f"CONFIGURATION ERROR: Invalid profile parameters: {exc}\n")
        return 1

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


def run_agent_cli(args: Optional[list[str]] = None) -> int:
    """Parse arguments and execute agent command or start daemon."""
    parser = build_parser()
    parsed = parser.parse_args(args)

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
