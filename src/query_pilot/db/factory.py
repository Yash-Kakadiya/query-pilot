"""Connection factory boundary for resolving ConnectionProfiles and engine configuration.

This module provides the ProfileConnectionFactory and ProfileEngineConfiguration,
enabling QueryPilot to resolve trusted, local database profiles and construct isolated
database engine configurations without relying on hardcoded settings or accepting
untrusted client-supplied connection parameters.
"""

from contextlib import contextmanager
from dataclasses import dataclass
import logging
from typing import Any, Callable, Dict, Iterator, Optional, Tuple

import sqlalchemy
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from query_pilot.db.connection import (
    DatabaseConnectionError,
    DatabaseSecurityError,
    verify_database_context,
)
from query_pilot.db.profile import (
    GLOBAL_MAX_ROW_LIMIT,
    GLOBAL_MAX_TIMEOUT_SECONDS,
    ConnectionProfile,
    ConnectionTargetOverrideError,
    ProfileStore,
    UnknownProfileError,
    enforce_global_row_limit_ceiling,
    enforce_global_timeout_ceiling,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProfileEngineConfiguration:
    """Immutable engine and execution configuration derived strictly from a trusted profile.

    Attributes:
        profile_id: Identifier of the resolved connection profile.
        display_name: Human-readable display label.
        expected_db: The expected database name verified via SELECT DB_NAME() at runtime.
                     Always derived from the trusted profile, never from client input.
        sqlalchemy_url: SQLAlchemy-compatible connection URL with credentials.
        safe_url: Diagnostic URL with password masked as PWD=********.
        engine_kwargs: Parameters for sqlalchemy.create_engine (pool_pre_ping, connect_args).
                       Note: `connect_args["timeout"]` configures the driver's connection-establishment /
                       login timeout (e.g. ODBC SQL_ATTR_LOGIN_TIMEOUT). It does NOT enforce query
                       statement execution timeouts.
        timeout_seconds: Query execution timeout in seconds, strictly clamped <= 15s.
                         Note: Storing this limit on the configuration does not by itself cancel
                         long-running SQL statements. Query timeout MUST be enforced at statement
                         execution time (e.g. raw_conn.timeout or conn.execution_options(timeout=...)).
        max_row_limit: Maximum returned row limit, strictly clamped <= 100 rows.
                       Note: Storing this limit does not truncate database query results on its own;
                       row ceilings MUST be enforced by the query execution layer via bounded cursor
                       fetching (e.g. fetchmany(max_row_limit + 1)).
        allowed_tables: Optional immutable whitelist of permitted tables.
        sensitive_columns: Immutable tuple of sensitive column names to redact.
        schema_name: Target schema name (e.g. 'dbo').
    """

    profile_id: str
    display_name: str
    expected_db: str
    sqlalchemy_url: str
    safe_url: str
    engine_kwargs: Dict[str, Any]
    timeout_seconds: int
    max_row_limit: int
    allowed_tables: Optional[Tuple[str, ...]]
    sensitive_columns: Tuple[str, ...]
    schema_name: str


class ProfileConnectionFactory:
    """Factory boundary for resolving connection profiles and constructing engine configs.

    Enforces critical security invariants:
    1. Connection-target authority is strictly vested in the local ProfileStore.
    2. Client requests cannot supply connection parameters (server, database, user, password,
       connection string, timeout, row limit, or expected_db); any attempt raises
       ConnectionTargetOverrideError.
    3. The expected database identity is derived solely from the resolved trusted profile,
       never from the profile_id string or untrusted request data.
    4. Hard global ceilings (15s query timeout, 100 row ceiling) are independently enforced.
    5. Resource limits are cleanly separated: `connect_args["timeout"]` is the connection-establishment
       timeout; statement execution timeout and row limits are enforced downstream at query execution time.
    6. Completely testable offline: engine creation is lazy and supports custom engine creators
       or mocks without opening network connections.
    """

    def __init__(
        self,
        store: ProfileStore,
        engine_creator: Optional[Callable[..., Engine]] = None,
    ):
        """Initialize the factory with a trusted ProfileStore.

        Args:
            store: Preconfigured ProfileStore containing validated profiles.
            engine_creator: Optional engine creation callable (defaults to sqlalchemy.create_engine).
                            Enables offline testing and mocking without real database connections.
        """
        self._store = store
        self._engine_creator = engine_creator or sqlalchemy.create_engine

    @property
    def store(self) -> ProfileStore:
        """Return the underlying trusted ProfileStore."""
        return self._store

    def resolve_profile(
        self,
        profile_id: Optional[str] = None,
        client_request: Optional[Dict[str, Any]] = None,
    ) -> ConnectionProfile:
        """Resolve a trusted profile by ID and verify no client override keys are present.

        Args:
            profile_id: Optional profile identifier to look up. If None, resolves the
                        explicitly configured default profile from trusted configuration.
            client_request: Optional untrusted request payload to inspect for override attempts.

        Returns:
            The resolved ConnectionProfile.

        Raises:
            ConnectionTargetOverrideError: If client_request contains any forbidden connection keys.
            UnknownProfileError: If profile_id is not registered in the store or if profile_id is None
                                 and no default profile is configured.
        """
        return self._store.resolve(profile_id, client_request=client_request)

    def get_configuration(
        self,
        profile_id: Optional[str] = None,
        client_request: Optional[Dict[str, Any]] = None,
    ) -> ProfileEngineConfiguration:
        """Construct the immutable engine and execution configuration for a profile.

        Args:
            profile_id: Optional profile identifier (or None for default profile).
            client_request: Optional untrusted request payload to inspect.

        Returns:
            ProfileEngineConfiguration with strictly derived expected_db and clamped resource limits.
        """
        profile = self.resolve_profile(profile_id, client_request=client_request)

        # Enforce global resource ceilings
        timeout = profile.clamp_timeout()
        max_rows = profile.clamp_row_limit()

        # Build engine_kwargs: connect_args["timeout"] configures driver connection login timeout
        engine_kwargs = {
            "pool_pre_ping": True,
            "connect_args": {"timeout": timeout},
        }

        # Build sensitive column tuple (canonical + profile-specific)
        policy = profile.get_sensitive_column_policy()
        sensitive_cols = tuple(sorted(policy.denylist))

        return ProfileEngineConfiguration(
            profile_id=profile.profile_id,
            display_name=profile.display_name,
            expected_db=profile.expected_db,
            sqlalchemy_url=profile.get_sqlalchemy_url(),
            safe_url=profile.get_sqlalchemy_url(hide_password=True),
            engine_kwargs=engine_kwargs,
            timeout_seconds=timeout,
            max_row_limit=max_rows,
            allowed_tables=profile.allowed_tables,
            sensitive_columns=sensitive_cols,
            schema_name=profile.schema_name,
        )

    def create_engine(
        self,
        profile_id: Optional[str] = None,
        client_request: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> Engine:
        """Construct a SQLAlchemy Engine for the resolved profile without connecting.

        Args:
            profile_id: Optional identifier of the profile to use (or None for default profile).
            client_request: Optional untrusted request payload.
            **kwargs: Additional keyword arguments merged into sqlalchemy.create_engine.

        Returns:
            SQLAlchemy Engine.

        Raises:
            ConnectionTargetOverrideError: If client_request contains forbidden keys.
            UnknownProfileError: If profile_id is unknown or missing default.
            DatabaseConnectionError: If engine instantiation fails.
        """
        config = self.get_configuration(profile_id, client_request=client_request)

        # Merge engine kwargs while preserving security critical connect_args
        merged_kwargs = dict(config.engine_kwargs)
        merged_kwargs.update(kwargs)

        try:
            logger.debug(
                f"Creating engine for profile '{config.profile_id}' "
                f"targeting database '{config.expected_db}'"
            )
            return self._engine_creator(config.sqlalchemy_url, **merged_kwargs)
        except Exception as exc:
            raise DatabaseConnectionError(
                f"Failed to create SQLAlchemy engine for profile '{config.profile_id}': {exc}"
            ) from exc

    @contextmanager
    def get_connection(
        self,
        profile_id: Optional[str] = None,
        engine: Optional[Engine] = None,
        client_request: Optional[Dict[str, Any]] = None,
    ) -> Iterator[Connection]:
        """Provide a managed database connection with verified database context for a profile.

        Args:
            profile_id: Optional identifier of the trusted profile (or None for default profile).
            engine: Optional pre-existing Engine (if None, created from profile).
            client_request: Optional untrusted request payload.

        Yields:
            Verified SQLAlchemy Connection.

        Raises:
            ConnectionTargetOverrideError: If client_request attempts target override.
            UnknownProfileError: If profile_id is unknown or missing default.
            DatabaseSecurityError: If live DB_NAME() does not match profile expected_db.
            DatabaseConnectionError: If connection fails.
        """
        config = self.get_configuration(profile_id, client_request=client_request)
        eng = engine or self.create_engine(profile_id, client_request=client_request)

        try:
            conn = eng.connect()
        except SQLAlchemyError as exc:
            raise DatabaseConnectionError(
                f"Failed to establish connection for profile '{config.profile_id}': {exc}"
            ) from exc

        try:
            verify_database_context(conn, expected_db=config.expected_db)
            yield conn
        finally:
            conn.close()
