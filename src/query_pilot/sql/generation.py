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

    def __init__(self, message: str, usage_metadata: Optional[Any] = None):
        super().__init__(message)
        self.usage_metadata = usage_metadata



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

MAX_CONVERSATION_TURNS: int = 3
MAX_CLARIFICATION_OPTIONS: int = 5
ALLOWED_ROLES: tuple[str, ...] = ("user",)
FORBIDDEN_SQL_FIELDS: tuple[str, ...] = ("executed_sql", "sql", "query", "candidate_sql")


def validate_clarification_options(
    options: List["ClarificationOption"],
) -> List["ClarificationOption"]:
    """Validate clarification options count and uniqueness of option IDs.

    Args:
        options: List of ClarificationOption instances.

    Returns:
        The validated list of ClarificationOption instances.

    Raises:
        ValueError: If option count exceeds MAX_CLARIFICATION_OPTIONS (5) or if
                    duplicate option IDs are found.
    """
    if len(options) > MAX_CLARIFICATION_OPTIONS:
        raise ValueError(
            f"clarification_options cannot exceed {MAX_CLARIFICATION_OPTIONS} options, received {len(options)}."
        )
    seen_ids: set[str] = set()
    for opt in options:
        opt_id = opt.option_id.strip().lower()
        if opt_id in seen_ids:
            raise ValueError(
                f"Duplicate clarification option_id found: {opt.option_id!r}. Option IDs must be unique within a response."
            )
        seen_ids.add(opt_id)
    return options


class ClarificationOption(BaseModel):
    """Structured, bounded clarification choice for an ambiguous query."""

    model_config = {"extra": "forbid"}

    option_id: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="Unique identifier for the clarification option.",
    )
    description: str = Field(
        ...,
        min_length=1,
        max_length=500,
        description="Concise description of this interpretation.",
    )
    suggested_question: Optional[str] = Field(
        default=None,
        max_length=500,
        description="Optional unambiguous reformulated question corresponding to this option.",
    )

    @field_validator("option_id", "description")
    @classmethod
    def validate_non_empty_str(cls, v: str) -> str:
        """Ensure string fields are not empty or whitespace."""
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("Clarification option string fields cannot be empty or whitespace.")
        return cleaned

    @field_validator("suggested_question")
    @classmethod
    def validate_suggested_question(cls, v: Optional[str]) -> Optional[str]:
        """Ensure suggested_question is non-empty if provided."""
        if v is None:
            return None
        cleaned = v.strip()
        if not cleaned:
            return None
        return cleaned


class ConversationTurnContext(BaseModel):
    """Compact, bounded semantic summary of a single prior conversation turn.

    Contains only semantic context (user question, resolved intent, result columns).
    Does NOT contain raw executed SQL, database rows, or authentication secrets.
    """

    model_config = {"extra": "forbid"}

    role: str = Field(
        default="user",
        description="Role of the participant (must be 'user').",
    )
    user_question: str = Field(
        ...,
        min_length=1,
        max_length=1000,
        description="The user question asked in this turn.",
    )
    resolved_intent: Optional[str] = Field(
        default=None,
        max_length=1000,
        description="Optional brief description of the resolved intent or domain operation.",
    )
    result_columns: List[str] = Field(
        default_factory=list,
        description="Names of output columns returned by the query in this turn.",
    )

    @model_validator(mode="before")
    @classmethod
    def normalize_turn_data(cls, data: Any) -> Any:
        """Enforce SQL field prohibition, user-only role, and restricted legacy fallback."""
        if isinstance(data, dict):
            # Explicitly reject unexpected SQL-like fields
            for forbidden_field in FORBIDDEN_SQL_FIELDS:
                if forbidden_field in data:
                    raise ValueError(
                        f"Field '{forbidden_field}' is forbidden in ConversationTurnContext."
                    )

            role = str(data.get("role", "user")).strip().lower()
            if role == "assistant":
                raise ValueError(
                    "ConversationTurnContext strictly represents prior user-question interactions. "
                    "Role 'assistant' is not permitted."
                )
            if role != "user":
                raise ValueError(
                    f"Invalid role: {data.get('role')!r}. ConversationTurnContext role must be 'user'."
                )

            if "content" in data:
                data = dict(data)
                content_val = data.pop("content")
                if "user_question" not in data:
                    data["user_question"] = content_val

            if "user_question" not in data:
                raise ValueError("Conversation turn must contain 'user_question'.")
        return data

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        """Ensure role is 'user', rejecting 'assistant' with clear explanation."""
        cleaned = v.strip().lower()
        if cleaned == "assistant":
            raise ValueError(
                "ConversationTurnContext strictly represents prior user-question interactions. "
                "Role 'assistant' is not permitted."
            )
        if cleaned != "user":
            raise ValueError(
                f"Invalid role: {v!r}. ConversationTurnContext role must be 'user'."
            )
        return cleaned

    @field_validator("user_question")
    @classmethod
    def validate_user_question(cls, v: str) -> str:
        """Ensure user question is not empty or whitespace."""
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("user_question cannot be empty or whitespace.")
        return cleaned

    @field_validator("resolved_intent")
    @classmethod
    def validate_resolved_intent(cls, v: Optional[str]) -> Optional[str]:
        """Ensure resolved intent is stripped and non-empty if present."""
        if v is None:
            return None
        cleaned = v.strip()
        if not cleaned:
            return None
        return cleaned

    @field_validator("result_columns")
    @classmethod
    def validate_result_columns(cls, v: List[str]) -> List[str]:
        """Ensure result columns are bounded and non-empty strings."""
        if len(v) > 50:
            raise ValueError("result_columns cannot exceed 50 columns.")
        cleaned_cols: List[str] = []
        for col in v:
            if not isinstance(col, str) or not col.strip():
                raise ValueError("Column name in result_columns must be a non-empty string.")
            cleaned_cols.append(col.strip())
        return cleaned_cols


class SQLGenerationStatus(str, Enum):
    """Status indicating whether a question is supported, unsupported, or ambiguous."""

    ANSWERABLE = "answerable"
    UNSUPPORTED = "unsupported"
    CLARIFICATION_NEEDED = "clarification_needed"


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
    conversation_context: List[ConversationTurnContext] = Field(
        default_factory=list,
        description="Bounded semantic context from recent conversation turns (at most 3).",
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

    @field_validator("conversation_context", mode="before")
    @classmethod
    def normalize_conversation_context(cls, v: Any) -> Any:
        """Allow None as empty list for conversation context."""
        if v is None:
            return []
        return v

    @field_validator("conversation_context")
    @classmethod
    def validate_conversation_context_size(
        cls, v: List[ConversationTurnContext]
    ) -> List[ConversationTurnContext]:
        """Ensure conversation_context does not exceed MAX_CONVERSATION_TURNS."""
        if len(v) > MAX_CONVERSATION_TURNS:
            raise ValueError(
                f"conversation_context cannot exceed {MAX_CONVERSATION_TURNS} turns, received {len(v)}."
            )
        return v


class SQLGenerationResponse(BaseModel):
    """Structured response expected from the LLM SQL generator.

    Invariants:
    - If status == 'answerable': sql must be a non-empty read-only SQL string.
    - If status == 'unsupported': sql must be None/null.
    - If status == 'clarification_needed': sql must be None/null, and explanation
      or clarification_prompt must be provided.
    """

    status: SQLGenerationStatus = Field(
        default=SQLGenerationStatus.ANSWERABLE,
        description="Indicates whether the question is answerable using the schema ('answerable'), unsupported ('unsupported'), or requires clarification ('clarification_needed').",
    )
    sql: Optional[str] = Field(
        default=None,
        description="The candidate read-only SQL query answering the user question (non-empty when status is 'answerable', null when status is 'unsupported' or 'clarification_needed').",
    )
    explanation: Optional[str] = Field(
        default=None,
        description="Brief natural language explanation of how the query answers the user question, why the question is unsupported, or why clarification is needed.",
    )
    assumptions: List[str] = Field(
        default_factory=list,
        description="Any assumptions made regarding business logic, joins, or filters.",
    )
    clarification_prompt: Optional[str] = Field(
        default=None,
        max_length=1000,
        description="Clarifying question or guidance when the query is ambiguous.",
    )
    clarification_options: List[ClarificationOption] = Field(
        default_factory=list,
        description="Structured candidate interpretation options for clarification.",
    )
    usage_metadata: Optional[Any] = Field(
        default=None,
        description="Provider token usage and latency telemetry for this generation invocation.",
    )

    @field_validator("clarification_options")
    @classmethod
    def validate_options_bound(
        cls, v: List[ClarificationOption]
    ) -> List[ClarificationOption]:
        """Ensure clarification_options is bounded to MAX_CLARIFICATION_OPTIONS with unique IDs."""
        return validate_clarification_options(v)

    @field_validator("clarification_prompt")
    @classmethod
    def validate_clarification_prompt(cls, v: Optional[str]) -> Optional[str]:
        """Ensure clarification_prompt is non-empty if provided and within length bounds."""
        if v is None:
            return None
        cleaned = v.strip()
        if not cleaned:
            return None
        if len(cleaned) > 1000:
            raise ValueError("clarification_prompt cannot exceed 1000 characters.")
        return cleaned

    @model_validator(mode="after")
    def validate_status_and_sql(self) -> "SQLGenerationResponse":
        """Enforce contract invariants between status, SQL presence, and clarification fields."""
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
            if self.clarification_prompt is not None:
                raise ValueError(
                    f"Answerable response must not contain clarification_prompt (received: {self.clarification_prompt!r})."
                )
            if self.clarification_options:
                raise ValueError(
                    f"Answerable response must not contain clarification_options (received {len(self.clarification_options)} options)."
                )
        elif self.status == SQLGenerationStatus.UNSUPPORTED:
            if self.sql is not None:
                raise ValueError(
                    f"Unsupported response must not contain SQL (sql must be null/None, received: {self.sql!r})."
                )
            if self.clarification_prompt is not None:
                raise ValueError(
                    f"Unsupported response must not contain clarification_prompt (received: {self.clarification_prompt!r})."
                )
            if self.clarification_options:
                raise ValueError(
                    f"Unsupported response must not contain clarification_options (received {len(self.clarification_options)} options)."
                )
        elif self.status == SQLGenerationStatus.CLARIFICATION_NEEDED:
            if self.sql is not None:
                raise ValueError(
                    f"Clarification response must not contain SQL (sql must be null/None, received: {self.sql!r})."
                )
            has_prompt = bool(self.clarification_prompt and self.clarification_prompt.strip())
            has_explanation = bool(self.explanation and self.explanation.strip())
            if not has_prompt and not has_explanation:
                raise ValueError(
                    "Clarification response requires a non-empty clarification_prompt or explanation."
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
