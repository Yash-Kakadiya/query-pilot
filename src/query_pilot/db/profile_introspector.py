"""Profile-aware dynamic database schema introspection.

Provides ProfileDatabaseIntrospector (and alias ProfileSchemaIntrospector) for
discovering database schema metadata governed strictly by trusted connection profiles:
1. Derives expected database identity strictly from the trusted profile (expected_db).
2. Verifies connected database session identity (SELECT DB_NAME() == expected_db) before introspection.
3. Fails closed immediately on database mismatch (DatabaseSecurityError) and closes connections.
4. Enforces profile table allowlists (allowed_tables); tables outside the allowlist are omitted.
5. Preserves canonical sensitive columns and additive profile sensitive columns.
6. Operates without cross-profile schema caching to prevent multi-tenant state leakage.
7. Ensures deterministic resource cleanup on both success and failure paths.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Connection, Engine, Inspector
from sqlalchemy.exc import SQLAlchemyError

from query_pilot.db.connection import (
    DatabaseConnectionError,
    DatabaseSecurityError,
    verify_database_context,
)
from query_pilot.db.factory import (
    ProfileConnectionFactory,
    ProfileEngineConfiguration,
)
from query_pilot.db.introspection import (
    DEFAULT_SENSITIVE_COLUMNS,
    SensitiveColumnPolicy,
)
from query_pilot.db.models import (
    ColumnMetadata,
    DatabaseSchema,
    ForeignKeyMetadata,
    IndexMetadata,
    TableMetadata,
)
from query_pilot.db.profile import (
    ConnectionProfile,
    ConnectionTargetOverrideError,
    inspect_for_forbidden_client_keys,
)
from query_pilot.presentation.formatter import sanitize_message

logger = logging.getLogger(__name__)


class ProfileIntrospectionError(Exception):
    """Raised when profile-aware schema introspection fails or security validation blocks discovery."""

    pass


class ProfileDatabaseIntrospector:
    """Discovers database schema metadata for a trusted ConnectionProfile or ProfileEngineConfiguration.

    Enforces critical security invariants:
    1. Derives expected database strictly from profile (expected_db).
    2. Performs runtime session database context verification (SELECT DB_NAME() == expected_db).
    3. Fails closed immediately on database mismatch (DatabaseSecurityError) and closes the connection.
    4. Filters discovered tables through profile.allowed_tables (if configured). Unlisted tables
       are completely omitted from the resulting DatabaseSchema.
    5. Applies additive SensitiveColumnPolicy (canonical defaults + profile-specific columns)
       when producing model-facing schemas.
    6. Respects profile.schema_name (defaults to 'dbo').
    7. Operates without cross-profile schema caching to prevent multi-tenant state leakage.
    8. Ensures connections are strictly closed in finally blocks on success and failure.
    """

    def __init__(
        self,
        config: Union[ProfileEngineConfiguration, ConnectionProfile],
        engine: Optional[Engine] = None,
        factory: Optional[ProfileConnectionFactory] = None,
    ):
        """Initialize the introspector with trusted profile configuration.

        Args:
            config: Trusted, resolved ProfileEngineConfiguration or ConnectionProfile.
            engine: Optional pre-configured SQLAlchemy Engine (supports mocks for offline testing).
            factory: Optional ProfileConnectionFactory for engine resolution.

        Raises:
            ProfileIntrospectionError: If config is None or an invalid type.
        """
        if config is None:
            raise ProfileIntrospectionError("Trusted profile configuration is required and cannot be None.")

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
            raise ProfileIntrospectionError(
                f"Expected ProfileEngineConfiguration or ConnectionProfile, got {type(config).__name__}."
            )

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
    def default_schema_name(self) -> str:
        """Return the target relational schema name (e.g. 'dbo')."""
        return self._config.schema_name or "dbo"

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
                ) from exc

        if self._factory is not None:
            try:
                eng = self._factory.create_engine(self._config.profile_id)
                return eng.connect()
            except SQLAlchemyError as exc:
                sanitized = sanitize_message(str(exc))
                raise DatabaseConnectionError(
                    f"Failed to establish connection for profile '{self._config.profile_id}': {sanitized}"
                ) from exc

        try:
            eng = create_engine(self._config.sqlalchemy_url, **self._config.engine_kwargs)
            return eng.connect()
        except Exception as exc:
            sanitized = sanitize_message(str(exc))
            raise DatabaseConnectionError(
                f"Failed to create engine/connection for profile '{self._config.profile_id}': {sanitized}"
            ) from exc

    def introspect_raw(
        self,
        schema_name: Optional[str] = None,
        client_request: Optional[Dict[str, Any]] = None,
    ) -> DatabaseSchema:
        """Discover complete database schema metadata for the configured profile and schema.

        Security Enforcement Pipeline:
        1. Inspects untrusted client request for forbidden connection target parameters.
        2. Resolves target schema name (defaults to profile.schema_name or 'dbo').
        3. Connects to database and verifies session database context (SELECT DB_NAME()).
        4. Fails closed immediately on database mismatch (DatabaseSecurityError).
        5. Queries inspector for table names and filters them through profile.allowed_tables.
        6. Extracts columns, primary keys, foreign keys, and indexes deterministically.
        7. Returns raw DatabaseSchema (is_model_facing=False) without caching.
        8. Guarantees connection closure in a finally block.

        Args:
            schema_name: Optional schema name override (defaults to profile.schema_name or 'dbo').
            client_request: Optional untrusted request payload to inspect for override attempts.

        Returns:
            DatabaseSchema containing raw metadata (is_model_facing=False).

        Raises:
            ConnectionTargetOverrideError: If client_request attempts to override connection targets.
            DatabaseSecurityError: If runtime DB_NAME() does not match expected_db.
            ProfileIntrospectionError: If introspection fails.
        """
        if client_request:
            inspect_for_forbidden_client_keys(client_request)

        if schema_name is not None and schema_name.strip().lower() != self.default_schema_name.strip().lower():
            raise ConnectionTargetOverrideError(
                f"SECURITY VIOLATION: Caller is not permitted to override the profile's configured schema "
                f"('{self.default_schema_name}') with '{schema_name}'."
            )

        target_schema = self.default_schema_name

        conn = self._acquire_connection()
        try:
            db_name = verify_database_context(conn, expected_db=self._config.expected_db)
            inspector: Inspector = inspect(conn)

            try:
                all_tables = sorted(inspector.get_table_names(schema=target_schema))
            except Exception as exc:
                sanitized = sanitize_message(str(exc))
                raise ProfileIntrospectionError(
                    f"Failed to list tables in schema '{target_schema}' for profile '{self._config.profile_id}': {sanitized}"
                ) from exc

            # Apply table allowlist if configured on the profile
            if self._config.allowed_tables is not None:
                allowed_set = {t.strip().lower() for t in self._config.allowed_tables if t and t.strip()}
                table_names = [t for t in all_tables if t.strip().lower() in allowed_set]
            else:
                allowed_set = None
                table_names = all_tables

            tables: List[TableMetadata] = []

            for tbl_name in table_names:
                try:
                    # Primary keys
                    pk_constraint = inspector.get_pk_constraint(tbl_name, schema=target_schema) or {}
                    pk_columns = pk_constraint.get("constrained_columns", [])
                    pk_set = set(pk_columns)

                    # Columns
                    raw_columns = inspector.get_columns(tbl_name, schema=target_schema)
                    columns: List[ColumnMetadata] = []
                    for c in raw_columns:
                        col_name = c["name"]
                        data_type = str(c["type"])
                        nullable = bool(c.get("nullable", True))
                        is_pk = col_name in pk_set or bool(c.get("primary_key", False))
                        default_val = str(c["default"]) if c.get("default") is not None else None
                        is_ident = bool(c.get("identity", False))
                        comment = c.get("comment")

                        columns.append(
                            ColumnMetadata(
                                name=col_name,
                                data_type=data_type,
                                nullable=nullable,
                                primary_key=is_pk,
                                default=default_val,
                                is_identity=is_ident,
                                comment=comment,
                            )
                        )

                    # Foreign keys
                    raw_fks = inspector.get_foreign_keys(tbl_name, schema=target_schema) or []
                    foreign_keys: List[ForeignKeyMetadata] = []
                    for fk in raw_fks:
                        fk_name = fk.get("name")
                        ref_table = fk.get("referred_table") or ""
                        # If table allowlist is active, skip foreign keys referencing tables outside allowlist
                        if allowed_set is not None and ref_table.strip().lower() not in allowed_set:
                            continue

                        constrained_cols = fk.get("constrained_columns", [])
                        referred_cols = fk.get("referred_columns", [])
                        for src_col, tgt_col in zip(constrained_cols, referred_cols):
                            foreign_keys.append(
                                ForeignKeyMetadata(
                                    name=fk_name,
                                    source_table=tbl_name,
                                    source_column=src_col,
                                    target_table=ref_table,
                                    target_column=tgt_col,
                                )
                            )
                    foreign_keys.sort(
                        key=lambda k: (k.source_table, k.source_column, k.target_table, k.target_column)
                    )

                    # Indexes
                    raw_indexes = inspector.get_indexes(tbl_name, schema=target_schema) or []
                    indexes: List[IndexMetadata] = []
                    for idx in raw_indexes:
                        idx_name = idx.get("name") or ""
                        idx_cols = idx.get("column_names") or []
                        is_unique = bool(idx.get("unique", False))
                        dialect_opts = idx.get("dialect_options") or {}
                        filter_def = dialect_opts.get("mssql_where")

                        indexes.append(
                            IndexMetadata(
                                name=idx_name,
                                columns=list(idx_cols),
                                is_unique=is_unique,
                                filter_definition=filter_def,
                            )
                        )
                    indexes.sort(key=lambda x: x.name)

                    # Unique constraints (handled safely with unique index fallback)
                    try:
                        raw_unique = inspector.get_unique_constraints(tbl_name, schema=target_schema) or []
                        unique_constraints = sorted(
                            [{"name": u.get("name"), "column_names": u.get("column_names", [])} for u in raw_unique],
                            key=lambda x: str(x.get("name")),
                        )
                    except NotImplementedError:
                        unique_constraints = [
                            {"name": idx.name, "column_names": idx.columns}
                            for idx in indexes
                            if idx.is_unique
                        ]

                    tables.append(
                        TableMetadata(
                            schema_name=target_schema,
                            table_name=tbl_name,
                            columns=columns,
                            primary_keys=pk_columns,
                            foreign_keys=foreign_keys,
                            indexes=indexes,
                            unique_constraints=unique_constraints,
                        )
                    )

                except Exception as exc:
                    sanitized = sanitize_message(str(exc))
                    raise ProfileIntrospectionError(
                        f"Failed to inspect metadata for table '{tbl_name}' in profile '{self._config.profile_id}': {sanitized}"
                    ) from exc

            return DatabaseSchema(
                database_name=db_name,
                tables=tables,
                is_model_facing=False,
            )

        finally:
            conn.close()

    def filter_model_facing(self, raw_schema: DatabaseSchema) -> DatabaseSchema:
        """Transform raw schema metadata into model-facing metadata by redacting sensitive columns.

        Uses the profile's SensitiveColumnPolicy (canonical defaults + additive profile columns).
        Table allowlists were already applied during discovery, ensuring unlisted tables never reach this stage.

        Args:
            raw_schema: Complete raw DatabaseSchema from introspect_raw().

        Returns:
            Sanitized DatabaseSchema with is_model_facing=True suitable for LLM context generation.
        """
        if raw_schema is None:
            raise ProfileIntrospectionError("Cannot filter None raw_schema.")

        policy = self.get_sensitive_column_policy()
        sanitized_tables: List[TableMetadata] = []

        for tbl in raw_schema.tables:
            safe_columns = [
                col for col in tbl.columns
                if not policy.is_sensitive(col.name, tbl.table_name)
            ]

            sanitized_tables.append(
                TableMetadata(
                    schema_name=tbl.schema_name,
                    table_name=tbl.table_name,
                    columns=safe_columns,
                    primary_keys=tbl.primary_keys,
                    foreign_keys=tbl.foreign_keys,
                    indexes=tbl.indexes,
                    unique_constraints=tbl.unique_constraints,
                )
            )

        return DatabaseSchema(
            database_name=raw_schema.database_name,
            tables=sanitized_tables,
            is_model_facing=True,
        )

    def introspect_model_facing(
        self,
        schema_name: Optional[str] = None,
        client_request: Optional[Dict[str, Any]] = None,
    ) -> DatabaseSchema:
        """Introspect database and return sanitized, model-facing schema ready for generation and validation.

        Convenience method that discovers raw schema metadata and filters sensitive columns.

        Args:
            schema_name: Optional schema name override (defaults to profile.schema_name or 'dbo').
            client_request: Optional untrusted request payload to inspect.

        Returns:
            Sanitized DatabaseSchema with is_model_facing=True.
        """
        raw = self.introspect_raw(schema_name=schema_name, client_request=client_request)
        return self.filter_model_facing(raw)


# Backward-compatible and clear alias
ProfileSchemaIntrospector = ProfileDatabaseIntrospector
