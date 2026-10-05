"""Database connection management and security enforcement for QueryPilot.

Provides connection establishment, connection pooling, and strict runtime database
context validation via SQL Server session state (SELECT DB_NAME()).
Guarantees that QueryPilot only interacts with the approved isolated database.
"""

from contextlib import contextmanager
import logging
from typing import Iterator, Optional
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from query_pilot.config import DatabaseSettings, get_db_settings

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
