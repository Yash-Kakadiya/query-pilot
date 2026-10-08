"""SQL generation contract and typed models for QueryPilot.

Defines the boundary between natural language questions and candidate SQL generation.
Ensures provider independence, deterministic model-facing schema formatting, and
structured response validation. The LLM layer never executes SQL.
"""

from enum import Enum
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable
from pydantic import BaseModel, Field, field_validator, model_validator

from query_pilot.db.models import DatabaseSchema


# ==============================================================================
# Controlled Error Hierarchy
# ==============================================================================

class SQLGenerationError(Exception):
    """Base exception for all SQL generation errors."""
    pass


class ConfigurationError(SQLGenerationError):
    """Raised when LLM configuration (such as API keys) is missing or invalid."""
    pass


class ProviderAPIError(SQLGenerationError):
    """Raised when an external LLM provider API call fails or encounters network errors."""
    pass


class MalformedResponseError(SQLGenerationError):
    """Raised when the LLM response cannot be parsed into the expected structured format."""
    pass


class EmptySQLError(SQLGenerationError):
    """Raised when the LLM returns an empty or whitespace-only SQL statement."""
    pass


# ==============================================================================
# Generation Contract Models
# ==============================================================================

class SQLGenerationStatus(str, Enum):
    """Status indicating whether a question is supported by the schema or unsupported."""

    ANSWERABLE = "answerable"
    UNSUPPORTED = "unsupported"


class SQLGenerationRequest(BaseModel):
    """Typed request representing a user question and model-facing schema context."""

    model_config = {"arbitrary_types_allowed": True}

    question: str = Field(
        ...,
        description="The natural language question from the user.",
    )
    schema_context: DatabaseSchema = Field(
        ...,
        description="The model-facing database schema (must have sensitive columns redacted).",
    )
    dialect: str = Field(
        default="T-SQL",
        description="The target SQL dialect (e.g., T-SQL for SQL Server).",
    )
    instructions: Optional[str] = Field(
        default=None,
        description="Optional application instructions or domain context.",
    )

    @field_validator("question")
    @classmethod
    def validate_question_not_empty(cls, v: str) -> str:
        """Ensure question is not empty or whitespace."""
        val = v.strip()
        if not val:
            raise ValueError("User question cannot be empty or whitespace.")
        return val

    @field_validator("schema_context")
    @classmethod
    def validate_schema_is_model_facing(cls, v: DatabaseSchema) -> DatabaseSchema:
        """Ensure schema provided to the generation layer is model-facing."""
        if not v.is_model_facing:
            raise ValueError(
                "Security boundary violation: SQLGenerationRequest requires a model-facing "
                "DatabaseSchema (is_model_facing=True) to prevent credential leakage."
            )
        return v


class SQLGenerationResponse(BaseModel):
    """Structured response expected from the LLM SQL generator.

    Invariants:
    - If status == 'answerable': sql must be a non-empty read-only SQL string.
    - If status == 'unsupported': sql must be None/null.
    """

    status: SQLGenerationStatus = Field(
        default=SQLGenerationStatus.ANSWERABLE,
        description="Indicates whether the question is answerable using the schema ('answerable') or unsupported ('unsupported').",
    )
    sql: Optional[str] = Field(
        default=None,
        description="The candidate read-only SQL query answering the user question (non-empty when status is 'answerable', null when status is 'unsupported').",
    )
    explanation: Optional[str] = Field(
        default=None,
        description="Brief natural language explanation of how the query answers the user question, or why the question is unsupported.",
    )
    assumptions: List[str] = Field(
        default_factory=list,
        description="Any assumptions made regarding business logic, joins, or filters.",
    )

    @model_validator(mode="after")
    def validate_status_and_sql(self) -> "SQLGenerationResponse":
        """Enforce contract invariants between status and SQL presence."""
        if self.status == SQLGenerationStatus.ANSWERABLE:
            if self.sql is None or not self.sql.strip():
                raise ValueError("Answerable response requires a non-empty SQL query.")
            val = self.sql.strip()
            # Strip markdown code blocks if the model wrapped them in ```sql ... ```
            if val.startswith("```"):
                lines = val.splitlines()
                if lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].startswith("```"):
                    lines = lines[:-1]
                val = "\n".join(lines).strip()
                if not val:
                    raise ValueError("Generated SQL cannot be empty after stripping code fences.")
            self.sql = val
        elif self.status == SQLGenerationStatus.UNSUPPORTED:
            if self.sql is not None:
                raise ValueError(
                    f"Unsupported response must not contain SQL (sql must be null/None, received: {self.sql!r})."
                )
        return self


# ==============================================================================
# Provider-Independent Generator Interface
# ==============================================================================

@runtime_checkable
class SQLGenerator(Protocol):
    """Provider-independent protocol for SQL generation components."""

    def generate(self, request: SQLGenerationRequest) -> SQLGenerationResponse:
        """Generate candidate SQL from a structured request without executing it.

        Args:
            request: SQLGenerationRequest containing question and model-facing schema.

        Returns:
            SQLGenerationResponse containing candidate SQL and optional explanations.

        Raises:
            ConfigurationError: If provider configuration or API keys are missing.
            ProviderAPIError: If the external provider API call fails.
            MalformedResponseError: If the model output fails structured validation.
            EmptySQLError: If the model returns empty SQL.
        """
        ...


# ==============================================================================
# Deterministic Schema Context Formatter
# ==============================================================================

def format_schema_context(schema: DatabaseSchema, dialect: str = "T-SQL") -> str:
    """Format a model-facing DatabaseSchema into a stable, deterministic textual context.

    Includes tables, model-facing columns, data types, primary keys, and foreign-key
    relationships. Strictly excludes sensitive columns, row data, and credentials.

    Args:
        schema: Model-facing DatabaseSchema.
        dialect: SQL dialect name (default: T-SQL).

    Returns:
        Deterministic formatted string suitable for LLM prompt context.
    """
    lines: List[str] = [
        f"DATABASE DIALECT: {dialect}",
        f"DATABASE NAME: {schema.database_name}",
        "",
        "TABLES AND COLUMNS:",
    ]

    # Deterministic table ordering by table_name
    sorted_tables = sorted(schema.tables, key=lambda t: t.table_name.lower())

    all_relationships: List[str] = []

    for table in sorted_tables:
        lines.append(f"TABLE [{table.table_name}]:")
        pk_set = {pk.lower() for pk in table.primary_keys}

        # Deterministic column ordering as inspected
        for col in table.columns:
            pk_tag = " (PRIMARY KEY)" if col.name.lower() in pk_set or col.primary_key else ""
            null_tag = " NULL" if col.nullable else " NOT NULL"
            lines.append(f"  - [{col.name}] ({col.data_type}{null_tag}{pk_tag})")

        # Collect foreign keys
        for fk in table.foreign_keys:
            all_relationships.append(
                f"[{fk.source_table}].[{fk.source_column}] -> [{fk.target_table}].[{fk.target_column}]"
            )

        lines.append("")

    # Relationships section (sorted deterministically)
    lines.append("RELATIONSHIPS (FOREIGN KEYS):")
    if all_relationships:
        for rel in sorted(all_relationships):
            lines.append(f"  - {rel}")
    else:
        lines.append("  (No foreign-key relationships defined)")

    return "\n".join(lines).strip()
