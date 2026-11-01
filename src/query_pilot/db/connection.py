"""Database connection management and security enforcement for QueryPilot.

Provides connection establishment, connection pooling, and strict runtime database
context validation via SQL Server session state (SELECT DB_NAME()).
Guarantees that QueryPilot only interacts with the approved isolated database.
"""

from contextlib import contextmanager
import logging
import os
import sqlite3
from typing import TYPE_CHECKING, Iterator, Optional, Union
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

from query_pilot.config import DatabaseSettings, get_db_settings
from query_pilot.db.sqlite_security import SQLiteSecurityError, configure_sqlite_connection

if TYPE_CHECKING:
    from query_pilot.db.profile import ConnectionProfile

logger = logging.getLogger(__name__)

# Approved target database name (Strict Security Boundary)
ALLOWED_TARGET_DATABASE: str = "GradeSense_Local"


class DatabaseSecurityError(Exception):
    """Raised when runtime database context violates security boundaries."""
    pass


class DatabaseConnectionError(Exception):
    """Raised when establishing a database connection fails."""
    pass


def verify_database_context(
    conn: Connection,
    expected_db: str = ALLOWED_TARGET_DATABASE,
) -> str:
    """Query the active SQL Server session and verify the connected database.

    Executes 'SELECT DB_NAME()' directly on the connection to eliminate reliance
    on parsed connection strings or client-side configuration.

    Args:
        conn: An active SQLAlchemy Connection.
        expected_db: Expected database name (default: GradeSense_Local).

    Returns:
        The verified database name returned by SQL Server.

    Raises:
        DatabaseSecurityError: If the connected database does not match expected_db.
    """
    try:
        actual_db = conn.execute(text("SELECT DB_NAME();")).scalar()
    except SQLAlchemyError as exc:
        raise DatabaseConnectionError(
            f"Failed to query SQL Server session context (DB_NAME): {exc}"
        ) from exc

    if not actual_db or str(actual_db).strip() != expected_db:
        raise DatabaseSecurityError(
            f"SECURITY VIOLATION: Connected to unauthorized database '{actual_db}'. "
            f"QueryPilot is strictly restricted to '{expected_db}'. "
            f"Terminating connection to protect external databases."
        )

    return str(actual_db).strip()


def verify_sqlite_database_context(
    conn: Union[Connection, sqlite3.Connection],
    expected_path: str,
) -> str:
    """Verify that an active SQLite connection is attached strictly to the expected database file.

    Executes 'PRAGMA database_list;' directly to eliminate reliance on client configuration.
    Confirms that:
    1. Exactly one database ('main') is attached; no attached external databases.
    2. The attached database path matches the canonical expected_path.

    Args:
        conn: An active SQLAlchemy Connection or raw sqlite3.Connection.
        expected_path: Expected filesystem path to SQLite database.

    Returns:
        The verified canonical database path.

    Raises:
        DatabaseSecurityError: If database context does not match expected_path or has attached DBs.
        DatabaseConnectionError: If querying database_list fails.
    """
    if not expected_path:
        raise DatabaseSecurityError("Expected SQLite database path cannot be empty.")

    canonical_expected = os.path.normcase(os.path.abspath(expected_path))

    # Fast path: query connection-scoped verified path function bound to SQLite C handle
    from query_pilot.db.sqlite_security import VERIFIED_PATH_FUNCTION

    verified_path = None
    try:
        if isinstance(conn, sqlite3.Connection):
            cur = conn.cursor()
            try:
                cur.execute(f"SELECT {VERIFIED_PATH_FUNCTION}();")
                res = cur.fetchone()
                verified_path = res[0] if res else None
            finally:
                cur.close()
        else:
            verified_path = conn.execute(text(f"SELECT {VERIFIED_PATH_FUNCTION}();")).scalar()
    except Exception:
        verified_path = None

    if verified_path:
        if os.path.normcase(os.path.abspath(str(verified_path))) != canonical_expected:
            raise DatabaseSecurityError(
                f"SECURITY VIOLATION: Connected to unauthorized SQLite database '{verified_path}'. "
                f"Expected '{canonical_expected}'."
            )
        return str(verified_path)

    try:
        if isinstance(conn, sqlite3.Connection):
            cur = conn.cursor()
            try:
                cur.execute("PRAGMA database_list;")
                rows = cur.fetchall()
            finally:
                cur.close()
        else:
            rows = conn.execute(text("PRAGMA database_list;")).fetchall()
    except Exception as exc:
        raise DatabaseConnectionError(
            f"Failed to query SQLite database context (database_list): {exc}"
        ) from exc

    if not rows or len(rows) != 1 or rows[0][1] != "main":
        raise DatabaseSecurityError(
            f"SECURITY VIOLATION: Unexpected SQLite attached databases count {len(rows)}: {rows}. "
            "QueryPilot requires strictly a single main database."
        )

    actual_file = rows[0][2]
    if not actual_file or os.path.normcase(os.path.abspath(actual_file)) != canonical_expected:
        raise DatabaseSecurityError(
            f"SECURITY VIOLATION: Connected to unauthorized SQLite database '{actual_file}'. "
            f"QueryPilot is strictly restricted to '{canonical_expected}'. "
            f"Terminating connection to protect external databases."
        )

    canonical_actual = os.path.abspath(actual_file)
    return canonical_actual


def create_db_engine(
    settings: Optional[DatabaseSettings] = None,
    **kwargs,
) -> Engine:
    """Create a SQLAlchemy engine configured from DatabaseSettings.

    Args:
        settings: DatabaseSettings instance (defaults to cached settings).
        **kwargs: Additional parameters passed to sqlalchemy.create_engine.

    Returns:
        SQLAlchemy Engine.
    """
    cfg = settings or get_db_settings()
    url = cfg.get_sqlalchemy_url()

    # Default pool and execution parameters
    engine_kwargs = {
        "pool_pre_ping": True,
        "connect_args": {"timeout": cfg.DB_QUERY_TIMEOUT_SECONDS},
    }
    engine_kwargs.update(kwargs)

    try:
        # Note: Do NOT log the full URL or connection string as it may contain credentials
        logger.debug(
            f"Creating database engine for server={cfg.DB_SERVER}, db={cfg.DB_NAME}"
        )
        return create_engine(url, **engine_kwargs)
    except Exception as exc:
        raise DatabaseConnectionError(
            f"Failed to create SQLAlchemy database engine: {exc}"
        ) from exc


def create_profile_sqlite_engine(
    profile: "ConnectionProfile",
    mode: str = "execution",
    **kwargs,
) -> Engine:
    """Create a SQLAlchemy Engine for a SQLite profile configured with security enforcement.

    Enforces:
    - Canonical path re-validation immediately before engine/connection creation.
    - Read-only URI mode: sqlite:///file:{path}?mode=ro&uri=true.
    - NullPool pooling: ensures separate physical connections and zero pool poisoning.
    - Application-controlled PRAGMA query_only=ON and busy_timeout={ms} before authorizer attachment.
    - Verification of opened database path via PRAGMA database_list against expected canonical path;
      rejection of unexpected attached databases.
    - Installation of appropriate Phase 1 authorizer ('execution' or 'introspection').

    Args:
        profile: Validated ConnectionProfile instance with engine_type='sqlite'.
        mode: Connection security mode: 'execution' or 'introspection'.
        **kwargs: Additional parameters passed to sqlalchemy.create_engine.

    Returns:
        SQLAlchemy Engine.
    """
    if profile.engine_type != "sqlite":
        raise DatabaseConnectionError(
            f"Cannot create SQLite database engine for profile '{profile.profile_id}' with engine_type='{profile.engine_type}'."
        )

    if not profile.database_path:
        raise DatabaseConnectionError(
            f"SQLite profile '{profile.profile_id}' is missing required 'database_path'."
        )

    # Revalidate canonical path immediately before opening connection
    from query_pilot.db.sqlite_path import validate_sqlite_database_path
    from query_pilot.db.profile import format_sqlite_uri
    canonical_path = validate_sqlite_database_path(profile.database_path)
    url = format_sqlite_uri(canonical_path)

    timeout = profile.clamp_timeout()
    busy_timeout_ms = int(timeout * 1000)

    engine_kwargs = {
        "poolclass": kwargs.pop("poolclass", NullPool),
    }
    engine_kwargs.update(kwargs)

    try:
        engine = create_engine(url, **engine_kwargs)

        @event.listens_for(engine, "connect")
        def _on_sqlite_connect(dbapi_conn, connection_record):
            # Check file still exists and is regular file before configuring
            if not os.path.isfile(canonical_path):
                raise DatabaseSecurityError(
                    f"SQLite database file was removed or is not a regular file: {canonical_path}"
                )
            configure_sqlite_connection(
                dbapi_conn,
                mode=mode,
                allowed_tables=profile.allowed_tables,
                sensitive_columns=profile.sensitive_columns,
                busy_timeout_ms=busy_timeout_ms,
                expected_canonical_path=canonical_path,
            )

        return engine
    except Exception as exc:
        raise DatabaseConnectionError(
            f"Failed to create SQLite database engine for profile '{profile.profile_id}': {exc}"
        ) from exc


def create_profile_db_engine(
    profile: "ConnectionProfile",
    **kwargs,
) -> Engine:
    """Create a SQLAlchemy engine configured from a ConnectionProfile.

    Derives connection URL and driver timeout strictly from the profile.
    Timeout is clamped to the global 15-second ceiling.
    Dispatches to create_profile_sqlite_engine for SQLite profiles.

    Args:
        profile: Validated ConnectionProfile instance.
        **kwargs: Additional parameters passed to sqlalchemy.create_engine.

    Returns:
        SQLAlchemy Engine.
    """
    if profile.engine_type == "sqlite":
        mode = kwargs.pop("mode", "execution")
        return create_profile_sqlite_engine(profile, mode=mode, **kwargs)

    url = profile.get_sqlalchemy_url()
    timeout = profile.clamp_timeout()

    engine_kwargs = {
        "pool_pre_ping": True,
        "connect_args": {"timeout": timeout},
    }
    engine_kwargs.update(kwargs)

    try:
        logger.debug(
            f"Creating database engine for profile={profile.profile_id}, "
            f"server={profile.server}, db={profile.database_name}"
        )
        return create_engine(url, **engine_kwargs)
    except Exception as exc:
        raise DatabaseConnectionError(
            f"Failed to create SQLAlchemy database engine for profile '{profile.profile_id}': {exc}"
        ) from exc


@contextmanager
def get_connection(
    engine: Optional[Engine] = None,
    settings: Optional[DatabaseSettings] = None,
    expected_db: str = ALLOWED_TARGET_DATABASE,
) -> Iterator[Connection]:
    """Provide a managed database connection with automatic session verification.

    Opens a connection, verifies that SQL Server reports the expected database,
    yields the connection for operations, and ensures cleanup upon exit.

    Args:
        engine: Existing Engine to use (creates one from settings if None).
        settings: DatabaseSettings to use if engine is None.
        expected_db: Expected target database name.

    Yields:
        Verified SQLAlchemy Connection.

    Raises:
        DatabaseSecurityError: If connected to an unexpected database.
        DatabaseConnectionError: If connection cannot be established.
    """
    eng = engine or create_db_engine(settings)
    try:
        conn = eng.connect()
    except SQLAlchemyError as exc:
        raise DatabaseConnectionError(
            f"Failed to establish connection to database: {exc}"
        ) from exc

    try:
        verify_database_context(conn, expected_db=expected_db)
        yield conn
    finally:
        conn.close()
