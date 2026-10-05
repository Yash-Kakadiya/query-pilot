"""Database schema introspection and sensitive column redaction.

Uses SQLAlchemy inspection facilities to discover database metadata deterministically.
Provides strict separation between raw internal schema metadata and sanitized,
model-facing representations where sensitive columns (e.g., PasswordHash) are redacted.
"""

from collections.abc import Iterable
import logging
from typing import List, Optional, Set
from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from query_pilot.db.connection import get_connection, verify_database_context
from query_pilot.db.models import (
    ColumnMetadata,
    DatabaseSchema,
    ForeignKeyMetadata,
    IndexMetadata,
    TableMetadata,
)

logger = logging.getLogger(__name__)

# Canonical denylist of column names that must NEVER be exposed to the LLM
DEFAULT_SENSITIVE_COLUMNS: frozenset[str] = frozenset({
    "passwordhash",
    "password",
    "secret",
    "token",
    "private_key",
    "credit_card",
    "ssn",
})


class SensitiveColumnPolicy:
    """Enforces redaction of sensitive columns from model-facing schema context.
    
    Operates in a case-insensitive manner across column names.
    """

    def __init__(self, denylist: Optional[Iterable[str]] = None):
        """Initialize the policy with a default or custom denylist.
        
        Args:
            denylist: Optional collection of column names to redact.
                      Defaults to DEFAULT_SENSITIVE_COLUMNS.
        """
        if denylist is None:
            self._denylist = {c.strip().lower() for c in DEFAULT_SENSITIVE_COLUMNS}
        else:
            self._denylist = {c.strip().lower() for c in denylist if c and c.strip()}

    def is_sensitive(self, column_name: str, table_name: Optional[str] = None) -> bool:
        """Check whether a column name matches the sensitive denylist.
        
        Args:
            column_name: The name of the column to evaluate.
            table_name: Optional table context (for future table-specific policies).
            
        Returns:
            True if the column is deemed sensitive and must be redacted.
        """
        if not column_name:
            return False
        return column_name.strip().lower() in self._denylist

    @property
    def denylist(self) -> Set[str]:
        """Return a copy of the active denylist set."""
        return set(self._denylist)


class DatabaseIntrospector:
    """Discovers database schema metadata deterministically via SQLAlchemy inspection."""

    def __init__(
        self,
        engine: Engine,
        policy: Optional[SensitiveColumnPolicy] = None,
    ):
        """Initialize introspector with a SQLAlchemy engine.
        
        Args:
            engine: SQLAlchemy Engine connected to the target database.
            policy: SensitiveColumnPolicy for redaction (defaults to standard policy).
        """
        self.engine = engine
        self.policy = policy or SensitiveColumnPolicy()

    def introspect_raw(self, schema_name: Optional[str] = "dbo") -> DatabaseSchema:
        """Discover complete database schema including all raw columns.
        
        Verifies runtime database session before inspection.
        Returns tables, columns, foreign keys, and indexes in deterministic order.
        
        Args:
            schema_name: Database schema to inspect (default: 'dbo').
            
        Returns:
            DatabaseSchema containing raw metadata (is_model_facing=False).
        """
        with get_connection(self.engine) as conn:
            db_name = verify_database_context(conn)
            inspector = inspect(conn)

            table_names = sorted(inspector.get_table_names(schema=schema_name))
            tables: List[TableMetadata] = []

            for tbl_name in table_names:
                # Primary keys
                pk_constraint = inspector.get_pk_constraint(tbl_name, schema=schema_name) or {}
                pk_columns = pk_constraint.get("constrained_columns", [])
                pk_set = set(pk_columns)

                # Columns
                raw_columns = inspector.get_columns(tbl_name, schema=schema_name)
                columns: List[ColumnMetadata] = []
                for c in raw_columns:
                    col_name = c["name"]
                    data_type = str(c["type"])
                    nullable = bool(c.get("nullable", True))
                    is_pk = col_name in pk_set or bool(c.get("primary_key", False))
                    default_val = str(c["default"]) if c.get("default") is not None else None
                    is_ident = bool(c.get("identity", False))

                    columns.append(
                        ColumnMetadata(
                            name=col_name,
                            data_type=data_type,
                            nullable=nullable,
                            primary_key=is_pk,
                            default=default_val,
                            is_identity=is_ident,
                        )
                    )

                # Foreign keys
                raw_fks = inspector.get_foreign_keys(tbl_name, schema=schema_name)
                foreign_keys: List[ForeignKeyMetadata] = []
                for fk in raw_fks:
                    fk_name = fk.get("name")
                    ref_table = fk.get("referred_table") or ""
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
                # Sort foreign keys deterministically
                foreign_keys.sort(
                    key=lambda k: (k.source_table, k.source_column, k.target_table, k.target_column)
                )

                # Indexes
                raw_indexes = inspector.get_indexes(tbl_name, schema=schema_name)
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
                # Sort indexes deterministically by name
                indexes.sort(key=lambda x: x.name)

                # Unique constraints (handled safely if dialect raises NotImplementedError)
                try:
                    raw_unique = inspector.get_unique_constraints(tbl_name, schema=schema_name) or []
                    unique_constraints = sorted(
                        [{"name": u.get("name"), "column_names": u.get("column_names", [])} for u in raw_unique],
                        key=lambda x: str(x.get("name")),
                    )
                except NotImplementedError:
                    # MSSQL dialect implements unique constraints as unique indexes in get_indexes()
                    unique_constraints = [
                        {"name": idx.name, "column_names": idx.columns}
                        for idx in indexes
                        if idx.is_unique
                    ]

                tables.append(
                    TableMetadata(
                        schema_name=schema_name or "dbo",
                        table_name=tbl_name,
                        columns=columns,
                        primary_keys=pk_columns,
                        foreign_keys=foreign_keys,
                        indexes=indexes,
                        unique_constraints=unique_constraints,
                    )
                )

            return DatabaseSchema(
                database_name=db_name,
                tables=tables,
                is_model_facing=False,
            )

    def filter_model_facing(self, raw_schema: DatabaseSchema) -> DatabaseSchema:
        """Transform raw schema metadata into model-facing metadata by redacting sensitive columns.
        
        Args:
            raw_schema: Complete raw DatabaseSchema.
            
        Returns:
            Sanitized DatabaseSchema with is_model_facing=True.
        """
        sanitized_tables: List[TableMetadata] = []

        for tbl in raw_schema.tables:
            safe_columns = [
                col for col in tbl.columns
                if not self.policy.is_sensitive(col.name, tbl.table_name)
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

    def introspect_model_facing(self, schema_name: Optional[str] = "dbo") -> DatabaseSchema:
        """Introspect database and return sanitized, model-facing schema.
        
        Convenience method that runs introspect_raw() and filters sensitive columns.
        
        Args:
            schema_name: Database schema to inspect (default: 'dbo').
            
        Returns:
            Sanitized DatabaseSchema ready for LLM context generation.
        """
        raw = self.introspect_raw(schema_name=schema_name)
        return self.filter_model_facing(raw)
