"""Structured data models representing database schema metadata.

Defines deterministic, typed dataclasses for tables, columns, foreign keys,
and indexes. Separates internal raw schema representation from sanitized,
model-facing representations suitable for LLM reasoning.
"""

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class ColumnMetadata:
    """Metadata for a single database column."""

    name: str
    data_type: str
    nullable: bool = True
    primary_key: bool = False
    default: Optional[str] = None
    is_identity: bool = False
    comment: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert column metadata to dictionary."""
        return asdict(self)


@dataclass(frozen=True)
class ForeignKeyMetadata:
    """Metadata for a foreign key relationship."""

    source_table: str
    source_column: str
    target_table: str
    target_column: str
    name: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert foreign key metadata to dictionary."""
        return asdict(self)


@dataclass(frozen=True)
class IndexMetadata:
    """Metadata for a database index."""

    name: str
    columns: List[str]
    is_unique: bool = False
    filter_definition: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert index metadata to dictionary."""
        return asdict(self)


@dataclass(frozen=True)
class TableMetadata:
    """Metadata for a database table including columns, keys, and indexes."""

    schema_name: str
    table_name: str
    columns: List[ColumnMetadata]
    primary_keys: List[str] = field(default_factory=list)
    foreign_keys: List[ForeignKeyMetadata] = field(default_factory=list)
    indexes: List[IndexMetadata] = field(default_factory=list)
    unique_constraints: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def column_names(self) -> List[str]:
        """List of all column names for this table."""
        return [c.name for c in self.columns]

    def get_column(self, name: str) -> Optional[ColumnMetadata]:
        """Look up a column by case-insensitive name."""
        target = name.strip().lower()
        for col in self.columns:
            if col.name.lower() == target:
                return col
        return None

    def to_dict(self) -> Dict[str, Any]:
        """Convert table metadata to dictionary."""
        return {
            "schema_name": self.schema_name,
            "table_name": self.table_name,
            "columns": [c.to_dict() for c in self.columns],
            "primary_keys": list(self.primary_keys),
            "foreign_keys": [fk.to_dict() for fk in self.foreign_keys],
            "indexes": [idx.to_dict() for idx in self.indexes],
            "unique_constraints": list(self.unique_constraints),
        }


@dataclass(frozen=True)
class DatabaseSchema:
    """Complete structured metadata for a database."""

    database_name: str
    tables: List[TableMetadata]
    is_model_facing: bool = False

    @property
    def table_names(self) -> List[str]:
        """Sorted list of table names."""
        return [t.table_name for t in self.tables]

    def get_table(self, table_name: str) -> Optional[TableMetadata]:
        """Look up a table by case-insensitive name."""
        target = table_name.strip().lower()
        for tbl in self.tables:
            if tbl.table_name.lower() == target:
                return tbl
        return None

    @property
    def total_columns_count(self) -> int:
        """Total number of columns across all tables."""
        return sum(len(t.columns) for t in self.tables)

    @property
    def total_foreign_keys_count(self) -> int:
        """Total number of foreign-key relationships across all tables."""
        return sum(len(t.foreign_keys) for t in self.tables)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize complete database schema to dictionary."""
        return {
            "database_name": self.database_name,
            "is_model_facing": self.is_model_facing,
            "total_tables": len(self.tables),
            "total_columns": self.total_columns_count,
            "total_foreign_keys": self.total_foreign_keys_count,
            "tables": [t.to_dict() for t in self.tables],
        }
