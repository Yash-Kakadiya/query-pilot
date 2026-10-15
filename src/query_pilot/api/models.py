"""Strict Pydantic request and response models for QueryPilot API layer (Task 1.32-B).

Defines:
- QueryRequest: strictly validated input model enforcing trimmed non-empty questions
  and bounded semantic conversation context (at most 3 turns, role='user', no SQL fields).
  Rejects extra client inputs (sql, schema_context, max_retries, db_server, etc.).
- QueryResponse: public-safe projection of PipelineResult and PresentationResult preserving
  exact domain status codes, table structures, and sanitized error messages.
  Excludes generated SQL and internal trace/telemetry metadata by default.
- HealthResponse: operational readiness probe grounded in configuration without secret leaks.
"""

from typing import Any, Dict, List, Optional, Union
from pydantic import BaseModel, Field, field_validator

from query_pilot.pipeline import PipelineResult, PipelineStatus
from query_pilot.presentation.formatter import PresentationResult, sanitize_message
from query_pilot.presentation.synthesizer import GroundedAnswer
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
            generated_sql=result.generated_sql if include_sql else None,
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
            generated_sql=presentation.generated_sql if include_sql else None,
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
