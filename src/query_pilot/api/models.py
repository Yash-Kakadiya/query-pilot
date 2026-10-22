"""Strict Pydantic request and response models for QueryPilot API layer.

Defines:
- QueryRequest: strictly validated input model enforcing trimmed non-empty questions
  and bounded semantic conversation context (at most 3 turns, role='user', no SQL fields).
  Rejects extra client inputs (sql, schema_context, max_retries, db_server, etc.).
- QueryResponse: public-safe projection of PipelineResult and PresentationResult preserving
  exact domain status codes, table structures, and sanitized error messages.
  Excludes generated SQL and internal trace/telemetry metadata by default.
- HealthResponse: operational readiness probe grounded in configuration without secret leaks.
"""

from enum import Enum
import json
import re
from typing import Any, Dict, List, Literal, Optional, Union
from pydantic import BaseModel, Field, field_validator, model_validator

from query_pilot.pipeline import PipelineResult, PipelineStatus
from query_pilot.presentation.formatter import PresentationResult, sanitize_message
from query_pilot.presentation.synthesizer import AnswerGroundingStatus, GroundedAnswer
from query_pilot.sql.generation import (
    MAX_CONVERSATION_TURNS,
    ClarificationOption,
    ConversationTurnContext,
)


# ==============================================================================
# 1. Request Models
# ==============================================================================

class QueryRequest(BaseModel):
    """Client request model for natural-language database query execution.

    Invariants:
    - question: string, trimmed, non-empty, max 1000 characters.
    - conversation_context: optional list of at most 3 ConversationTurnContext turns.
    - extra="forbid": strictly rejects unknown client inputs (e.g. sql, max_retries,
      schema_context, internal graph controls).
    """

    model_config = {"extra": "forbid"}

    question: str = Field(
        ...,
        description="The natural-language question to answer.",
    )
    conversation_context: Optional[List[ConversationTurnContext]] = Field(
        default=None,
        description="Optional prior user conversation turns (at most 3 turns).",
    )
    execution_mode: str = Field(
        default="execute",
        description="Execution mode: 'execute' (default) or 'sql_only'.",
    )
    include_sql: bool = Field(
        default=False,
        description="Whether to include validated SQL in response (default: false).",
    )

    @field_validator("question")
    @classmethod
    def validate_question(cls, v: str) -> str:
        """Enforce non-empty string and bounded length after trimming."""
        if not isinstance(v, str):
            raise TypeError(f"Question must be a string, got {type(v).__name__}.")
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("Question cannot be empty or whitespace.")
        if len(cleaned) > 1000:
            raise ValueError(
                f"Question exceeds maximum length of 1000 characters ({len(cleaned)} characters)."
            )
        return cleaned

    @field_validator("conversation_context")
    @classmethod
    def validate_context_turns(
        cls, v: Optional[List[ConversationTurnContext]]
    ) -> Optional[List[ConversationTurnContext]]:
        """Enforce maximum 3 conversation context turns."""
        if v is None:
            return None
        if len(v) > MAX_CONVERSATION_TURNS:
            raise ValueError(
                f"conversation_context cannot exceed {MAX_CONVERSATION_TURNS} turns, received {len(v)}."
            )
        return v

    @field_validator("execution_mode", mode="before")
    @classmethod
    def validate_execution_mode(cls, v: Any) -> str:
        """Enforce execution_mode is strictly 'execute' or 'sql_only'."""
        if not isinstance(v, str):
            raise ValueError(f"execution_mode must be a string, got {type(v).__name__}.")
        cleaned = v.strip().lower()
        if cleaned not in ("execute", "sql_only"):
            raise ValueError(
                f"Invalid execution_mode {v!r}. Must be 'execute' or 'sql_only'."
            )
        return cleaned

    @field_validator("include_sql", mode="before")
    @classmethod
    def validate_include_sql(cls, v: Any) -> bool:
        """Enforce include_sql is strictly a boolean."""
        if not isinstance(v, bool):
            raise ValueError(f"include_sql must be a boolean, got {type(v).__name__}.")
        return v


# ==============================================================================
# 2. Response Sub-Models
# ==============================================================================

class ValidationReasonResponse(BaseModel):
    """Sanitized validation policy rejection reason.

    Contains only high-level policy codes and sanitized user-facing messages.
    Does not expose internal AST structures or raw query fragments.
    """

    model_config = {"extra": "forbid"}

    code: str = Field(..., description="Machine-readable validation reason code.")
    message: str = Field(..., description="Sanitized policy rejection message.")


class GroundedAnswerResponse(BaseModel):
    """Structured natural-language grounded answer.

    Excludes provider telemetry, token counts, and internal latency for public API safety.
    """

    model_config = {"extra": "forbid"}

    text: str = Field(..., description="User-facing synthesized answer text.")
    status: str = Field(..., description="Factual confidence and answer status.")
    is_mechanically_verified: bool = Field(
        default=False,
        description="True strictly for deterministic templates; False for LLM prose.",
    )
    verification_notes: List[str] = Field(
        default_factory=list,
        description="Disclosures, caveats, or verification notes.",
    )


# ==============================================================================
# 3. Main Query Response Model
# ==============================================================================

class QueryResponse(BaseModel):
    """Unified client response model for query execution outcomes.

    Provides a clean, public-safe representation of PipelineResult and PresentationResult:
    - Retains exact domain status values: SUCCESS, UNSUPPORTED, CLARIFICATION_NEEDED,
      GENERATION_FAILED, VALIDATION_REJECTED, EXECUTION_FAILED.
    - Excludes generated SQL by default (unless include_sql=True is explicitly requested).
    - Excludes database credentials, connection strings, raw exceptions, and internal trace IDs.
    """

    model_config = {"extra": "forbid"}

    request_id: Optional[str] = Field(
        default=None,
        description="Correlated unique request ID.",
    )
    status: PipelineStatus = Field(
        ...,
        description="High-level execution status code.",
    )
    question: str = Field(
        ...,
        description="Original user question.",
    )
    message: Optional[str] = Field(
        default=None,
        description="User-facing summary or status message.",
    )
    columns: List[str] = Field(
        default_factory=list,
        description="Preserved column names in query result order.",
    )
    rows: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Preserved row records in query result order.",
    )
    row_count: int = Field(
        default=0,
        description="Number of rows returned.",
    )
    truncated: bool = Field(
        default=False,
        description="Whether the result set was truncated by row limits.",
    )
    scalar_value: Optional[Any] = Field(
        default=None,
        description="Deterministic scalar value if result is 1 row and 1 column.",
    )
    markdown_table: Optional[str] = Field(
        default=None,
        description="Deterministic Markdown table representation.",
    )
    explanation: Optional[str] = Field(
        default=None,
        description="Generator explanation if available.",
    )
    clarification_prompt: Optional[str] = Field(
        default=None,
        description="Clarifying prompt when clarification is needed.",
    )
    clarification_options: List[ClarificationOption] = Field(
        default_factory=list,
        description="Disambiguation choices when clarification is needed.",
    )
    validation_reasons: List[ValidationReasonResponse] = Field(
        default_factory=list,
        description="Sanitized policy rejection reasons if validation was rejected.",
    )
    grounded_answer: Optional[GroundedAnswerResponse] = Field(
        default=None,
        description="Structured natural-language answer if synthesized.",
    )
    generated_sql: Optional[str] = Field(
        default=None,
        description="Generated SQL statement; omitted by default for public API safety.",
    )

    @classmethod
    def from_pipeline_result(
        cls,
        result: PipelineResult,
        *,
        include_sql: bool = False,
    ) -> "QueryResponse":
        """Project a PipelineResult into a safe, client-facing QueryResponse.

        By default, excludes generated SQL. Sanitizes all messages and validation reasons.
        """
        presentation: Optional[PresentationResult] = None
        if result.presentation is not None:
            if isinstance(result.presentation, PresentationResult):
                presentation = result.presentation
            elif isinstance(result.presentation, dict):
                try:
                    presentation = PresentationResult(**result.presentation)
                except Exception:
                    pass

        # If presentation is missing, generate it deterministically
        if presentation is None:
            from query_pilot.presentation.formatter import format_pipeline_result
            presentation = format_pipeline_result(result)

        # Build validation reasons safely
        validation_reasons: List[ValidationReasonResponse] = []
        if result.validation_result and result.validation_result.reasons:
            for r in result.validation_result.reasons:
                code_str = r.code.value if hasattr(r.code, "value") else str(r.code)
                validation_reasons.append(
                    ValidationReasonResponse(
                        code=code_str,
                        message=sanitize_message(r.message) or "",
                    )
                )
        elif presentation.validation_reasons:
            for vr in presentation.validation_reasons:
                code_raw = vr.get("code", "")
                code_str = code_raw.value if hasattr(code_raw, "value") else str(code_raw)
                validation_reasons.append(
                    ValidationReasonResponse(
                        code=code_str,
                        message=sanitize_message(vr.get("message", "")) or "",
                    )
                )

        # Grounded answer safe projection
        grounded_answer_resp: Optional[GroundedAnswerResponse] = None
        raw_answer = result.grounded_answer or presentation.grounded_answer
        if raw_answer is not None:
            if isinstance(raw_answer, GroundedAnswer):
                status_str = raw_answer.status.value if hasattr(raw_answer.status, "value") else str(raw_answer.status)
                grounded_answer_resp = GroundedAnswerResponse(
                    text=raw_answer.text,
                    status=status_str,
                    is_mechanically_verified=raw_answer.is_mechanically_verified,
                    verification_notes=list(raw_answer.verification_notes),
                )
            elif isinstance(raw_answer, dict):
                raw_st = raw_answer.get("status", "")
                status_str = raw_st.value if hasattr(raw_st, "value") else str(raw_st)
                grounded_answer_resp = GroundedAnswerResponse(
                    text=str(raw_answer.get("text", "")),
                    status=status_str,
                    is_mechanically_verified=bool(raw_answer.get("is_mechanically_verified", False)),
                    verification_notes=list(raw_answer.get("verification_notes", [])),
                )

        # Clarification options safe projection
        clarification_opts: List[ClarificationOption] = []
        raw_opts = result.clarification_options or presentation.clarification_options
        if raw_opts:
            for opt in raw_opts:
                if isinstance(opt, ClarificationOption):
                    clarification_opts.append(opt)
                elif isinstance(opt, dict):
                    clarification_opts.append(ClarificationOption(**opt))

        raw_explanation = presentation.explanation or result.generation_explanation
        raw_clarification = result.clarification_prompt or presentation.clarification_prompt

        # SQL disclosure policy (Section C):
        # - Validated-only result: SQL may be returned when requested.
        # - Successful execution: SQL may be returned when requested.
        # - Execution failure after validation: validated SQL may be returned when requested.
        # - Validation rejection: never return rejected candidate SQL as approved/safe SQL.
        # - Generation failure, unsupported, or clarification: return no SQL.
        # - include_sql=False: return None.
        disclosed_sql: Optional[str] = None
        if include_sql:
            if result.status in (PipelineStatus.SUCCESS, PipelineStatus.VALIDATED_ONLY):
                disclosed_sql = result.generated_sql
            elif (
                result.status == PipelineStatus.EXECUTION_FAILED
                and result.validation_result
                and result.validation_result.allowed is True
            ):
                disclosed_sql = result.generated_sql
            else:
                disclosed_sql = None

        return cls(
            request_id=result.request_id or presentation.request_id,
            status=result.status,
            question=result.question or presentation.title or "",
            message=sanitize_message(presentation.message) if presentation.message else None,
            columns=list(presentation.columns),
            rows=list(presentation.rows),
            row_count=presentation.row_count,
            truncated=presentation.truncated,
            scalar_value=presentation.scalar_value,
            markdown_table=presentation.markdown_table,
            explanation=sanitize_message(raw_explanation) if raw_explanation else None,
            clarification_prompt=sanitize_message(raw_clarification) if raw_clarification else None,
            clarification_options=clarification_opts,
            validation_reasons=validation_reasons,
            grounded_answer=grounded_answer_resp,
            generated_sql=disclosed_sql,
        )

    @classmethod
    def from_presentation_result(
        cls,
        presentation: PresentationResult,
        question: str,
        *,
        status: Optional[Union[PipelineStatus, str]] = None,
        include_sql: bool = False,
    ) -> "QueryResponse":
        """Project a PresentationResult into a safe, client-facing QueryResponse.

        Raises:
            ValueError: If presentation.status or supplied status is unrecognized.
            TypeError: If status is of an invalid type.
        """
        resolved_status: PipelineStatus
        if status is not None:
            if isinstance(status, PipelineStatus):
                resolved_status = status
            elif isinstance(status, str):
                try:
                    resolved_status = PipelineStatus(status.upper())
                except ValueError:
                    try:
                        resolved_status = PipelineStatus[status.upper()]
                    except KeyError:
                        raise ValueError(f"Unrecognized status: {status!r}")
            else:
                raise TypeError(f"Status must be a PipelineStatus or str, got {type(status).__name__}.")
        else:
            raw_status = presentation.status
            if isinstance(raw_status, PipelineStatus):
                resolved_status = raw_status
            elif isinstance(raw_status, str):
                try:
                    resolved_status = PipelineStatus(raw_status.upper())
                except ValueError:
                    try:
                        resolved_status = PipelineStatus[raw_status.upper()]
                    except KeyError:
                        raise ValueError(f"Unrecognized presentation status: {raw_status!r}")
            else:
                raise ValueError(f"Unrecognized presentation status: {raw_status!r}")

        validation_reasons: List[ValidationReasonResponse] = []
        if presentation.validation_reasons:
            for vr in presentation.validation_reasons:
                code_raw = vr.get("code", "")
                code_str = code_raw.value if hasattr(code_raw, "value") else str(code_raw)
                validation_reasons.append(
                    ValidationReasonResponse(
                        code=code_str,
                        message=sanitize_message(vr.get("message", "")) or "",
                    )
                )

        grounded_answer_resp: Optional[GroundedAnswerResponse] = None
        if presentation.grounded_answer is not None:
            raw_answer = presentation.grounded_answer
            if isinstance(raw_answer, GroundedAnswer):
                status_str = raw_answer.status.value if hasattr(raw_answer.status, "value") else str(raw_answer.status)
                grounded_answer_resp = GroundedAnswerResponse(
                    text=raw_answer.text,
                    status=status_str,
                    is_mechanically_verified=raw_answer.is_mechanically_verified,
                    verification_notes=list(raw_answer.verification_notes),
                )
            elif isinstance(raw_answer, dict):
                raw_st = raw_answer.get("status", "")
                status_str = raw_st.value if hasattr(raw_st, "value") else str(raw_st)
                grounded_answer_resp = GroundedAnswerResponse(
                    text=str(raw_answer.get("text", "")),
                    status=status_str,
                    is_mechanically_verified=bool(raw_answer.get("is_mechanically_verified", False)),
                    verification_notes=list(raw_answer.get("verification_notes", [])),
                )

        clarification_opts: List[ClarificationOption] = []
        if presentation.clarification_options:
            for opt in presentation.clarification_options:
                if isinstance(opt, ClarificationOption):
                    clarification_opts.append(opt)
                elif isinstance(opt, dict):
                    clarification_opts.append(ClarificationOption(**opt))

        disclosed_sql: Optional[str] = None
        if include_sql:
            if resolved_status in (PipelineStatus.SUCCESS, PipelineStatus.VALIDATED_ONLY):
                disclosed_sql = presentation.generated_sql
            elif (
                resolved_status == PipelineStatus.EXECUTION_FAILED
                and presentation.status == "execution_failed"
                and not presentation.validation_reasons
            ):
                disclosed_sql = presentation.generated_sql
            else:
                disclosed_sql = None

        return cls(
            request_id=presentation.request_id,
            status=resolved_status,
            question=question or presentation.title or "",
            message=sanitize_message(presentation.message) if presentation.message else None,
            columns=list(presentation.columns),
            rows=list(presentation.rows),
            row_count=presentation.row_count,
            truncated=presentation.truncated,
            scalar_value=presentation.scalar_value,
            markdown_table=presentation.markdown_table,
            explanation=sanitize_message(presentation.explanation) if presentation.explanation else None,
            clarification_prompt=sanitize_message(presentation.clarification_prompt) if presentation.clarification_prompt else None,
            clarification_options=clarification_opts,
            validation_reasons=validation_reasons,
            grounded_answer=grounded_answer_resp,
            generated_sql=disclosed_sql,
        )


# ==============================================================================
# 4. Health Response Model
# ==============================================================================

class HealthResponse(BaseModel):
    """Health and operational readiness status.

    Grounded in actual application metadata and settings; never leaks credentials.
    """

    model_config = {"extra": "forbid"}

    status: str = Field(default="healthy", description="Application service status.")
    version: str = Field(default="0.1.0", description="Application release version.")
    dialect: str = Field(default="T-SQL", description="Target SQL dialect.")
    tracing_enabled: bool = Field(default=False, description="Whether LangSmith tracing is active.")

    @classmethod
    def from_config(cls, settings: Optional[Any] = None) -> "HealthResponse":
        """Construct HealthResponse grounded in application settings without exposing secrets."""
        from query_pilot.tracing import is_tracing_enabled

        tracing = False
        try:
            tracing = is_tracing_enabled(settings)
        except Exception:
            pass

        return cls(
            status="healthy",
            version="0.1.0",
            dialect="T-SQL",
            tracing_enabled=tracing,
        )


# ==============================================================================
# 5. Hosted API V1 Models (Decoupled SQL Generation & Metadata Synthesis)
# ==============================================================================

IDENTIFIER_REGEX = re.compile(r"^[a-zA-Z0-9_#$]+$")
DATA_TYPE_REGEX = re.compile(r"^[a-zA-Z0-9_#$(), ]+$")
MAX_TABLES: int = 50
MAX_COLUMNS_PER_TABLE: int = 100
MAX_IDENTIFIER_LENGTH: int = 128
MAX_DESCRIPTION_LENGTH: int = 250
MAX_SCHEMA_PAYLOAD_BYTES: int = 48 * 1024  # 49,152 bytes (48 KB)


class GenerationStatus(str, Enum):
    """High-level status for hosted SQL generation."""

    GENERATED = "generated"
    CLARIFICATION_NEEDED = "clarification_needed"
    UNSUPPORTED = "unsupported"
    GENERATION_FAILED = "generation_failed"


class ForeignKeySchemaContext(BaseModel):
    """Untrusted client foreign-key metadata under bounded validation."""

    model_config = {"extra": "forbid"}

    source_table: str = Field(..., min_length=1, max_length=MAX_IDENTIFIER_LENGTH)
    source_column: str = Field(..., min_length=1, max_length=MAX_IDENTIFIER_LENGTH)
    target_table: str = Field(..., min_length=1, max_length=MAX_IDENTIFIER_LENGTH)
    target_column: str = Field(..., min_length=1, max_length=MAX_IDENTIFIER_LENGTH)
    name: Optional[str] = Field(default=None, max_length=MAX_IDENTIFIER_LENGTH)

    @field_validator("source_table", "source_column", "target_table", "target_column")
    @classmethod
    def validate_identifier(cls, v: str) -> str:
        cleaned = v.strip()
        if not cleaned or not IDENTIFIER_REGEX.match(cleaned):
            raise ValueError(f"Invalid identifier {v!r}. Must match regex {IDENTIFIER_REGEX.pattern}")
        return cleaned

    @field_validator("name")
    @classmethod
    def validate_optional_identifier(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        cleaned = v.strip()
        if not cleaned:
            return None
        if not IDENTIFIER_REGEX.match(cleaned):
            raise ValueError(f"Invalid identifier {v!r}. Must match regex {IDENTIFIER_REGEX.pattern}")
        return cleaned


class ColumnSchemaContext(BaseModel):
    """Untrusted client column metadata under bounded validation."""

    model_config = {"extra": "forbid"}

    name: str = Field(..., min_length=1, max_length=MAX_IDENTIFIER_LENGTH)
    data_type: str = Field(..., min_length=1, max_length=MAX_IDENTIFIER_LENGTH)
    nullable: bool = True
    primary_key: bool = False
    comment: Optional[str] = Field(default=None, max_length=MAX_DESCRIPTION_LENGTH)

    @field_validator("name")
    @classmethod
    def validate_column_name(cls, v: str) -> str:
        cleaned = v.strip()
        if not cleaned or not IDENTIFIER_REGEX.match(cleaned):
            raise ValueError(f"Invalid column name {v!r}. Must match regex {IDENTIFIER_REGEX.pattern}")
        return cleaned

    @field_validator("data_type")
    @classmethod
    def validate_data_type(cls, v: str) -> str:
        cleaned = v.strip()
        if not cleaned or not DATA_TYPE_REGEX.match(cleaned):
            raise ValueError(f"Invalid data type {v!r}. Must match regex {DATA_TYPE_REGEX.pattern}")
        return cleaned

    @field_validator("comment")
    @classmethod
    def validate_comment(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        cleaned = re.sub(r"[\r\n]+", " ", v).strip()
        if not cleaned:
            return None
        if len(cleaned) > MAX_DESCRIPTION_LENGTH:
            raise ValueError(f"Comment exceeds maximum length of {MAX_DESCRIPTION_LENGTH} characters.")
        return cleaned


class TableSchemaContext(BaseModel):
    """Untrusted client table metadata under bounded validation."""

    model_config = {"extra": "forbid"}

    table_name: str = Field(..., min_length=1, max_length=MAX_IDENTIFIER_LENGTH)
    schema_name: str = Field(default="dbo", min_length=1, max_length=MAX_IDENTIFIER_LENGTH)
    columns: List[ColumnSchemaContext] = Field(..., min_length=1, max_length=MAX_COLUMNS_PER_TABLE)
    primary_keys: List[str] = Field(default_factory=list)
    foreign_keys: List[ForeignKeySchemaContext] = Field(default_factory=list)
    description: Optional[str] = Field(default=None, max_length=MAX_DESCRIPTION_LENGTH)

    @field_validator("table_name", "schema_name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        cleaned = v.strip()
        if not cleaned or not IDENTIFIER_REGEX.match(cleaned):
            raise ValueError(f"Invalid identifier {v!r}. Must match regex {IDENTIFIER_REGEX.pattern}")
        return cleaned

    @field_validator("primary_keys")
    @classmethod
    def validate_primary_keys(cls, pks: List[str]) -> List[str]:
        cleaned_pks = []
        for pk in pks:
            cleaned = pk.strip()
            if not cleaned or not IDENTIFIER_REGEX.match(cleaned):
                raise ValueError(f"Invalid primary key identifier {pk!r}. Must match regex {IDENTIFIER_REGEX.pattern}")
            if len(cleaned) > MAX_IDENTIFIER_LENGTH:
                raise ValueError(f"Primary key {pk!r} exceeds maximum length of {MAX_IDENTIFIER_LENGTH} characters.")
            cleaned_pks.append(cleaned)
        return cleaned_pks

    @field_validator("description")
    @classmethod
    def validate_description(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        cleaned = re.sub(r"[\r\n]+", " ", v).strip()
        if not cleaned:
            return None
        if len(cleaned) > MAX_DESCRIPTION_LENGTH:
            raise ValueError(f"Description exceeds maximum length of {MAX_DESCRIPTION_LENGTH} characters.")
        return cleaned

    @model_validator(mode="after")
    def validate_table_integrity(self) -> "TableSchemaContext":
        if not self.columns:
            raise ValueError(f"Table {self.table_name!r} must contain at least 1 column.")
        if len(self.columns) > MAX_COLUMNS_PER_TABLE:
            raise ValueError(f"Table {self.table_name!r} cannot exceed {MAX_COLUMNS_PER_TABLE} columns.")

        col_names = set()
        for col in self.columns:
            lower = col.name.lower()
            if lower in col_names:
                raise ValueError(f"Duplicate column name {col.name!r} in table {self.table_name!r}.")
            col_names.add(lower)

        for pk in self.primary_keys:
            if pk.lower() not in col_names:
                raise ValueError(f"Primary key {pk!r} does not match any column in table {self.table_name!r}.")

        return self


class ModelFacingSchemaContext(BaseModel):
    """Untrusted client-provided schema context with strict bounds and validation."""

    model_config = {"extra": "forbid"}

    database_name: str = Field(..., min_length=1, max_length=MAX_IDENTIFIER_LENGTH)
    tables: List[TableSchemaContext] = Field(..., min_length=1, max_length=MAX_TABLES)

    @field_validator("database_name")
    @classmethod
    def validate_database_name(cls, v: str) -> str:
        cleaned = v.strip()
        if not cleaned or not IDENTIFIER_REGEX.match(cleaned):
            raise ValueError(f"Invalid database name {v!r}. Must match regex {IDENTIFIER_REGEX.pattern}")
        return cleaned

    @model_validator(mode="after")
    def validate_schema_bounds(self) -> "ModelFacingSchemaContext":
        if not self.tables:
            raise ValueError("schema_context must contain at least 1 table.")
        if len(self.tables) > MAX_TABLES:
            raise ValueError(f"schema_context cannot exceed {MAX_TABLES} tables, received {len(self.tables)}.")

        seen_tables = set()
        for tbl in self.tables:
            lower = tbl.table_name.lower()
            if lower in seen_tables:
                raise ValueError(f"Duplicate table name {tbl.table_name!r} in schema_context.")
            seen_tables.add(lower)

        raw_bytes = len(json.dumps(self.model_dump(), separators=(",", ":")).encode("utf-8"))
        if raw_bytes > MAX_SCHEMA_PAYLOAD_BYTES:
            raise ValueError(
                f"schema_context payload exceeds maximum allowed size of {MAX_SCHEMA_PAYLOAD_BYTES} bytes (48 KB), "
                f"received {raw_bytes} bytes."
            )
        return self

    def to_database_schema(self) -> Any:
        """Convert validated client schema context to internal model-facing DatabaseSchema."""
        from query_pilot.db.models import ColumnMetadata, DatabaseSchema, ForeignKeyMetadata, TableMetadata

        table_meta_list = []
        for tbl in self.tables:
            col_meta_list = [
                ColumnMetadata(
                    name=col.name,
                    data_type=col.data_type,
                    nullable=col.nullable,
                    primary_key=col.primary_key or (col.name.lower() in [pk.lower() for pk in tbl.primary_keys]),
                    comment=col.comment,
                )
                for col in tbl.columns
            ]
            fk_meta_list = [
                ForeignKeyMetadata(
                    source_table=fk.source_table,
                    source_column=fk.source_column,
                    target_table=fk.target_table,
                    target_column=fk.target_column,
                    name=fk.name,
                )
                for fk in tbl.foreign_keys
            ]
            table_meta_list.append(
                TableMetadata(
                    schema_name=tbl.schema_name or "dbo",
                    table_name=tbl.table_name,
                    columns=col_meta_list,
                    primary_keys=list(tbl.primary_keys),
                    foreign_keys=fk_meta_list,
                )
            )
        return DatabaseSchema(
            database_name=self.database_name,
            tables=table_meta_list,
            is_model_facing=True,
        )


class GenerateSQLRequest(BaseModel):
    """Untrusted client request to generate candidate SQL from schema context."""

    model_config = {"extra": "forbid"}

    question: str = Field(..., description="Natural-language question asked by user.")
    schema_context: ModelFacingSchemaContext = Field(..., description="Model-facing table/column metadata.")
    conversation_context: Optional[List[ConversationTurnContext]] = Field(
        default=None,
        description="Optional bounded prior user turns (at most 3 turns).",
    )
    request_id: Optional[str] = Field(
        default=None,
        max_length=128,
        description="Non-secret client correlation tracking ID.",
    )

    @field_validator("question")
    @classmethod
    def validate_question(cls, v: str) -> str:
        if not isinstance(v, str):
            raise TypeError("Question must be a string.")
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("Question cannot be empty or whitespace.")
        if len(cleaned) > 1000:
            raise ValueError(f"Question exceeds maximum length of 1000 characters ({len(cleaned)} chars).")
        return cleaned

    @field_validator("conversation_context")
    @classmethod
    def validate_conversation_context(
        cls, v: Optional[List[ConversationTurnContext]]
    ) -> Optional[List[ConversationTurnContext]]:
        if v is None:
            return None
        if len(v) > MAX_CONVERSATION_TURNS:
            raise ValueError(f"conversation_context cannot exceed {MAX_CONVERSATION_TURNS} turns, received {len(v)}.")
        return v


class GenerateSQLResponse(BaseModel):
    """Safe response model for candidate SQL generation."""

    model_config = {"extra": "forbid"}

    candidate_sql: Optional[str] = Field(
        default=None,
        max_length=10000,
        description="Generated read-only T-SQL candidate (max 10,000 chars). Present ONLY when status == 'generated'.",
    )
    status: GenerationStatus = Field(
        ...,
        description="High-level SQL generation outcome status.",
    )
    clarification_prompt: Optional[str] = Field(
        default=None,
        description="Clarification prompt. Present ONLY when status == 'clarification_needed'.",
    )
    unsupported_reason: Optional[str] = Field(
        default=None,
        description="Safe explanation of refusal. Present ONLY when status == 'unsupported'.",
    )
    execution_disclaimer: str = Field(
        default="Candidate SQL is unexecuted.",
        description="Fixed disclaimer confirming the candidate SQL has not been executed.",
    )
    is_executed: Literal[False] = Field(
        default=False,
        description="Fixed flag confirming the candidate SQL has not been executed.",
    )
    request_id: str = Field(..., description="Correlated request ID.")

    @model_validator(mode="after")
    def validate_generation_invariants(self) -> "GenerateSQLResponse":
        if self.status == GenerationStatus.GENERATED:
            if not self.candidate_sql or not self.candidate_sql.strip():
                raise ValueError("Generated status requires a non-empty candidate_sql.")
            if self.clarification_prompt is not None:
                raise ValueError("Generated status must not contain clarification_prompt.")
            if self.unsupported_reason is not None:
                raise ValueError("Generated status must not contain unsupported_reason.")
        elif self.status == GenerationStatus.CLARIFICATION_NEEDED:
            if self.candidate_sql is not None:
                raise ValueError("Clarification status must not contain candidate_sql.")
            if not self.clarification_prompt or not self.clarification_prompt.strip():
                raise ValueError("Clarification status requires a non-empty clarification_prompt.")
            if self.unsupported_reason is not None:
                raise ValueError("Clarification status must not contain unsupported_reason.")
        elif self.status == GenerationStatus.UNSUPPORTED:
            if self.candidate_sql is not None:
                raise ValueError("Unsupported status must not contain candidate_sql.")
            if not self.unsupported_reason or not self.unsupported_reason.strip():
                raise ValueError("Unsupported status requires a non-empty unsupported_reason.")
            if self.clarification_prompt is not None:
                raise ValueError("Unsupported status must not contain clarification_prompt.")
        elif self.status == GenerationStatus.GENERATION_FAILED:
            if self.candidate_sql is not None:
                raise ValueError("Generation failed status must not contain candidate_sql.")
        return self


class SynthesizeAnswerRequest(BaseModel):
    """Untrusted client request for metadata-only answer synthesis."""

    model_config = {"extra": "forbid"}

    question: str = Field(..., description="Original user natural language question.")
    candidate_sql: str = Field(..., description="The validated SQL query executed locally.")
    columns: List[str] = Field(
        ...,
        description="List of column names returned by query (names only, never cell values).",
    )
    row_count: int = Field(..., ge=0, le=10000, description="Total count of rows returned.")
    truncated: bool = Field(default=False, description="True if result set was truncated by row limit.")
    execution_status: str = Field(..., description="Local execution status; must be 'succeeded'.")
    execution_time_ms: float = Field(..., ge=0.0, le=60000.0, description="Local execution latency in milliseconds.")
    request_id: str = Field(..., min_length=1, max_length=128, description="Correlated request tracking ID.")

    @field_validator("question")
    @classmethod
    def validate_question(cls, v: str) -> str:
        if not isinstance(v, str):
            raise TypeError("Question must be a string.")
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("Question cannot be empty or whitespace.")
        if len(cleaned) > 1000:
            raise ValueError(f"Question exceeds maximum length of 1000 characters ({len(cleaned)} chars).")
        return cleaned

    @field_validator("candidate_sql")
    @classmethod
    def validate_sql(cls, v: str) -> str:
        if not isinstance(v, str):
            raise TypeError("candidate_sql must be a string.")
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("candidate_sql cannot be empty or whitespace.")
        if len(cleaned) > 10000:
            raise ValueError(f"candidate_sql exceeds maximum length of 10000 characters ({len(cleaned)} chars).")
        return cleaned

    @field_validator("columns")
    @classmethod
    def validate_columns(cls, cols: List[str]) -> List[str]:
        if not cols:
            raise ValueError("columns list cannot be empty.")
        if len(cols) > 100:
            raise ValueError(f"columns list cannot exceed 100 items, received {len(cols)}.")

        cleaned = []
        column_identifier_pattern = re.compile(r"^(?:[a-zA-Z0-9_#$]+|\[[a-zA-Z0-9_#$ ]+\])$")
        for col in cols:
            if not isinstance(col, str):
                raise ValueError(f"Column name must be a string, got {type(col).__name__}.")
            c = col.strip()
            if not c:
                raise ValueError("Column name cannot be empty or whitespace.")
            if len(c) > 128:
                raise ValueError(f"Column name exceeds maximum length of 128 characters ({len(c)} chars).")
            # Strict enforcement of column identifier syntax (rejects prose, JSON, tuples, control chars)
            if not column_identifier_pattern.match(c):
                raise ValueError(
                    f"Invalid column name {col!r}: must be a valid column identifier matching "
                    "^[a-zA-Z0-9_#$]+$ or bracket-delimited [identifier]. Serialized objects, prose, "
                    "and control characters are forbidden."
                )
            cleaned.append(c)
        return cleaned

    @field_validator("execution_status")
    @classmethod
    def validate_execution_status(cls, v: str) -> str:
        if not isinstance(v, str):
            raise TypeError("execution_status must be a string.")
        cleaned = v.strip().lower()
        if cleaned != "succeeded":
            raise ValueError(
                f"Invalid execution_status {v!r}. Answer synthesis requires execution_status to be 'succeeded'."
            )
        return cleaned


class SynthesizeAnswerResponse(BaseModel):
    """Response model for metadata-only answer synthesis."""

    model_config = {"extra": "forbid"}

    summary: str = Field(
        ...,
        description="Grounded prose narrative explaining dimensional findings or explaining why row disclosure is required.",
    )
    status: AnswerGroundingStatus = Field(
        ...,
        description="Answer grounding status.",
    )
    grounding_notes: Optional[str] = Field(
        default=None,
        description="Disclaimers or transparency notes on grounding.",
    )
    request_id: str = Field(..., description="Correlated request tracking ID.")
