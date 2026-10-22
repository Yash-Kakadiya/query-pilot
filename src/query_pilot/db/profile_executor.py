"""Profile-governed execution boundary and resource enforcement contract.

Defines the ProfileExecutionBoundary (and alias ProfileQueryExecutor) ensuring that
future database query execution adheres strictly to:
1. Pre-resolved, immutable profile configuration (never client-supplied connection strings).
2. Mandatory pre-execution SQL validation using active profile schema and policies.
3. Connected database identity verification (SELECT DB_NAME() == expected_db) before user SQL.
4. Statement execution timeout enforcement at the actual connection/driver boundary (clamped <= 15s).
5. Bounded cursor fetching (fetchmany(max_rows + 1)) strictly clamped <= 100 rows.
6. Profile-specific table allowlists and additive sensitive-column redaction policies.
7. Sanitized error messages free of credentials, connection strings, or raw DBAPI internals.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Tuple, Union

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DBAPIError, OperationalError, SQLAlchemyError

from query_pilot.db.connection import (
    DatabaseConnectionError,
    DatabaseSecurityError,
    verify_database_context,
)
from query_pilot.db.executor import (
    QueryExecutionError,
    QueryResult,
    QueryTimeoutError,
    UnsafeQueryError,
    serialize_value,
)
from query_pilot.db.factory import (
    ProfileConnectionFactory,
    ProfileEngineConfiguration,
)
from query_pilot.db.introspection import (
    DEFAULT_SENSITIVE_COLUMNS,
    SensitiveColumnPolicy,
)
from query_pilot.db.models import DatabaseSchema
from query_pilot.db.profile import (
    GLOBAL_MAX_ROW_LIMIT,
    GLOBAL_MAX_TIMEOUT_SECONDS,
    ConnectionProfile,
    ConnectionTargetOverrideError,
    inspect_for_forbidden_client_keys,
)
from query_pilot.presentation.formatter import sanitize_message
from query_pilot.sql.validator import validate_sql

logger = logging.getLogger(__name__)


class ProfileExecutionError(QueryExecutionError):
    """Raised when profile-governed query execution or boundary contract fails."""

    pass


class ProfileExecutionBoundary:
    """Execution boundary contract for profile-governed SQL execution.

    Enforces that database execution strictly requires:
    1. A trusted, pre-resolved ProfileEngineConfiguration or ConnectionProfile.
    2. Model-facing DatabaseSchema context for mandatory pre-execution SQL AST validation.
    3. Session database identity verification (SELECT DB_NAME() == config.expected_db) prior to user SQL execution.
    4. Statement execution timeout enforcement at the connection/driver level (clamped <= 15s).
    5. Bounded cursor fetching (fetchmany(max_row_limit + 1)) strictly clamped <= 100 rows.
    6. Profile-specific table allowlists and additive sensitive-column redaction policies.
    7. Sanitized error messages free of credentials, connection strings, or raw DBAPI internals.
    """

    def __init__(
        self,
        config: Union[ProfileEngineConfiguration, ConnectionProfile],
        schema: Optional[DatabaseSchema] = None,
        engine: Optional[Engine] = None,
        factory: Optional[ProfileConnectionFactory] = None,
    ):
        """Initialize the execution boundary with trusted profile configuration.

        Args:
            config: Trusted, resolved ProfileEngineConfiguration or ConnectionProfile.
            schema: Optional model-facing DatabaseSchema for pre-execution SQL validation.
            engine: Optional pre-configured SQLAlchemy Engine (supports mocks for offline testing).
            factory: Optional ProfileConnectionFactory for engine resolution.

        Raises:
            ProfileExecutionError: If config is None or an invalid type.
        """
        if config is None:
            raise ProfileExecutionError("Trusted profile configuration is required and cannot be None.")

        if isinstance(config, ConnectionProfile):
            # Derive immutable ProfileEngineConfiguration from ConnectionProfile
            timeout = config.clamp_timeout()
            max_rows = config.clamp_row_limit()
            engine_kwargs = {
                "pool_pre_ping": True,
                "connect_args": {"timeout": timeout},
            }
            policy = config.get_sensitive_column_policy()
            self._config = ProfileEngineConfiguration(
                profile_id=config.profile_id,
                display_name=config.display_name,
                expected_db=config.database_name,
                sqlalchemy_url=config.get_sqlalchemy_url(),
                safe_url=config.get_sqlalchemy_url(hide_password=True),
                engine_kwargs=engine_kwargs,
                timeout_seconds=timeout,
                max_row_limit=max_rows,
                allowed_tables=config.allowed_tables,
                sensitive_columns=tuple(sorted(list(policy.denylist))),
                schema_name=config.schema_name,
            )
        elif isinstance(config, ProfileEngineConfiguration):
            self._config = config
        else:
            raise ProfileExecutionError(
                f"Expected ProfileEngineConfiguration or ConnectionProfile, got {type(config).__name__}."
            )

        self._schema = schema
        self._engine = engine
        self._factory = factory

    @property
    def config(self) -> ProfileEngineConfiguration:
        """Return the immutable profile engine configuration."""
        return self._config

    @property
    def expected_db(self) -> str:
        """Return the expected database identity derived strictly from the profile."""
        return self._config.expected_db

    @property
    def effective_max_timeout(self) -> int:
        """Maximum allowable query timeout (clamped to profile limit and 15s ceiling)."""
        return min(self._config.timeout_seconds, GLOBAL_MAX_TIMEOUT_SECONDS)

    @property
    def effective_max_rows(self) -> int:
        """Maximum allowable returned rows (clamped to profile limit and 100 row ceiling)."""
        return min(self._config.max_row_limit, GLOBAL_MAX_ROW_LIMIT)

    def get_sensitive_column_policy(self) -> SensitiveColumnPolicy:
        """Return the SensitiveColumnPolicy enforcing canonical defaults plus additive profile entries."""
        combined = set(DEFAULT_SENSITIVE_COLUMNS)
        if self._config.sensitive_columns:
            for col in self._config.sensitive_columns:
                c = col.strip().lower()
                if c:
                    combined.add(c)
        return SensitiveColumnPolicy(denylist=combined)

    def is_table_permitted(self, table_name: str) -> bool:
        """Check if a table is permitted under this profile's schema policy."""
        if not table_name or not table_name.strip():
            return False
        if self._config.allowed_tables is None:
            return True
        clean = table_name.strip().lower()
        return clean in {t.strip().lower() for t in self._config.allowed_tables if t and t.strip()}

    def _acquire_connection(self) -> Connection:
        """Acquire an active SQLAlchemy Connection using injected engine or factory."""
        if self._engine is not None:
            try:
                return self._engine.connect()
            except SQLAlchemyError as exc:
                sanitized = sanitize_message(str(exc))
                raise DatabaseConnectionError(
                    f"Failed to establish connection for profile '{self._config.profile_id}': {sanitized}"
                ) from None

        if self._factory is not None:
            try:
                eng = self._factory.create_engine(self._config.profile_id)
                return eng.connect()
            except SQLAlchemyError as exc:
                sanitized = sanitize_message(str(exc))
                raise DatabaseConnectionError(
                    f"Failed to establish connection for profile '{self._config.profile_id}': {sanitized}"
                ) from None

        # Create engine lazily from profile config URL and engine_kwargs
        try:
            eng = create_engine(self._config.sqlalchemy_url, **self._config.engine_kwargs)
            return eng.connect()
        except Exception as exc:
            sanitized = sanitize_message(str(exc))
            raise DatabaseConnectionError(
                f"Failed to create engine/connection for profile '{self._config.profile_id}': {sanitized}"
            ) from None

    def execute(
        self,
        sql: str,
        params: Optional[Dict[str, Any]] = None,
        max_rows: Optional[int] = None,
        timeout_seconds: Optional[int] = None,
        client_request: Optional[Dict[str, Any]] = None,
        schema: Optional[DatabaseSchema] = None,
    ) -> QueryResult:
        """Validate and safely execute a read-only SQL query against the profile database.

        Security Enforcement Pipeline:
        1. Inspects untrusted client request for connection target override keys.
        2. Verifies presence and database identity match of model-facing schema context.
        3. Computes effective limits (clamped <= profile settings and global ceilings 15s/100 rows).
        4. Deterministically validates SQL via AST inspection using active schema and policy.
        5. Validates referenced tables against profile allowlist (if configured).
        6. Acquires connection and performs runtime session database verification (SELECT DB_NAME()).
        7. Enforces statement execution timeout at the driver/connection execution options.
        8. Executes query and fetches at most (effective_max_rows + 1) to boundedly detect truncation.
        9. Sanitizes exceptions to prevent credential, connection string, or driver traceback leaks.
        10. Safely closes all cursor and connection resources in finally blocks.

        Args:
            sql: SQL statement string to validate and execute.
            params: Optional parameter dictionary for parameterized queries.
            max_rows: Optional row limit (can only tighten limits, cannot exceed 100).
            timeout_seconds: Optional timeout (can only tighten limits, cannot exceed 15s).
            client_request: Optional untrusted request payload to inspect for override attempts.
            schema: Optional schema override (must match expected_db).

        Returns:
            QueryResult containing columns, JSON-serializable rows, count, and truncation flag.

        Raises:
            ConnectionTargetOverrideError: If client_request contains forbidden connection keys.
            ProfileExecutionError: If profile or schema context is missing or invalid.
            UnsafeQueryError: If query fails AST read-only validation or violates table/column policy.
            DatabaseSecurityError: If runtime DB_NAME() does not match expected_db.
            QueryTimeoutError: If statement execution times out.
        """
        # Step 1: Reject untrusted client overrides
        if client_request:
            inspect_for_forbidden_client_keys(client_request)

        # Step 2: Resolve and verify schema context
        active_schema = schema or self._schema
        if active_schema is None:
            raise ProfileExecutionError(
                "Missing schema context required for pre-execution SQL validation."
            )
        if active_schema.database_name.lower() != self._config.expected_db.lower():
            raise ProfileExecutionError(
                f"Schema database '{active_schema.database_name}' does not match "
                f"profile expected database '{self._config.expected_db}'."
            )

        # Step 3: Compute effective resource limits
        effective_timeout = self.effective_max_timeout
        if timeout_seconds is not None:
            try:
                req_t = int(timeout_seconds)
                if req_t >= 1:
                    effective_timeout = min(effective_timeout, req_t)
            except (ValueError, TypeError):
                pass

        effective_rows = self.effective_max_rows
        if max_rows is not None:
            try:
                req_r = int(max_rows)
                if req_r >= 1:
                    effective_rows = min(effective_rows, req_r)
            except (ValueError, TypeError):
                pass

        # Step 4: Pre-execution SQL Validation
        sensitive_policy = self.get_sensitive_column_policy()
        val_result = validate_sql(
            sql=sql,
            schema=active_schema,
            policy=sensitive_policy,
            max_rows=effective_rows,
        )
        if not val_result.allowed:
            reasons_str = "; ".join(f"{r.code.value}: {r.message}" for r in val_result.reasons)
            raise UnsafeQueryError(
                f"SQL validation rejected query: {sanitize_message(reasons_str)}"
            )

        # Step 5: Check allowed tables if profile specifies an allowlist
        if self._config.allowed_tables is not None:
            allowed_set = {t.strip().lower() for t in self._config.allowed_tables if t and t.strip()}
            for tbl in val_result.referenced_tables:
                if tbl.strip().lower() not in allowed_set:
                    raise UnsafeQueryError(
                        f"Access to table '{tbl}' is forbidden by profile policy."
                    )

        # Step 6: Acquire connection, verify database identity, and apply statement timeout
        conn = self._acquire_connection()
        try:
            # Runtime identity verification: SELECT DB_NAME()
            verify_database_context(conn, expected_db=self._config.expected_db)

            # Statement execution timeout applied at DBAPI level
            raw_conn = getattr(conn.connection, "dbapi_connection", None)
            if raw_conn is None or not hasattr(raw_conn, "timeout"):
                raise ProfileExecutionError(
                    "Hard driver-level query timeout enforcement is unavailable for this connection."
                )
            try:
                raw_conn.timeout = effective_timeout
            except Exception as exc:
                raise ProfileExecutionError(
                    f"Failed to set driver-level query timeout: {sanitize_message(str(exc))}"
                ) from None

            start_time = time.perf_counter()
            try:
                cursor_result = conn.execution_options(
                    timeout=effective_timeout,
                    yield_per=effective_rows,
                ).execute(text(val_result.normalized_sql), params or {})

                try:
                    columns = list(cursor_result.keys())
                    raw_rows = cursor_result.fetchmany(effective_rows + 1)
                    truncated = len(raw_rows) > effective_rows
                    if truncated:
                        raw_rows = raw_rows[:effective_rows]
                finally:
                    cursor_result.close()

                elapsed_ms = (time.perf_counter() - start_time) * 1000.0

            except OperationalError as exc:
                err_msg = str(exc).lower()
                if "timeout" in err_msg or "hyt00" in err_msg:
                    raise QueryTimeoutError(
                        f"Query execution timed out after {effective_timeout} seconds."
                    ) from None
                sanitized_err = sanitize_message(str(exc))
                raise ProfileExecutionError(
                    f"Database operational error: {sanitized_err}"
                ) from None
            except SQLAlchemyError as exc:
                sanitized_err = sanitize_message(str(exc))
                raise ProfileExecutionError(
                    f"Database execution error: {sanitized_err}"
                ) from None

        finally:
            conn.close()

        # Step 7: Format rows into JSON-safe serialized dictionary representations
        formatted_rows: List[Dict[str, Any]] = []
        for row in raw_rows:
            formatted_rows.append(
                {col: serialize_value(val) for col, val in zip(columns, row)}
            )

        return QueryResult(
            columns=columns,
            rows=formatted_rows,
            row_count=len(formatted_rows),
            truncated=truncated,
            execution_time_ms=elapsed_ms,
        )


# Backward-compatible and clear alias
ProfileQueryExecutor = ProfileExecutionBoundary
