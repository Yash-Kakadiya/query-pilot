"""Typed shared state contract for QueryPilot LangGraph workflows.

Defines the core state schema and validation helpers for future agentic orchestration.
Preserves the existing deterministic text-to-SQL pipeline as the authoritative
execution standard while establishing a typed contract for bounded recovery,
validation retries, and multi-stage coordination.

State Lifecycle:
----------------
1. INITIALIZATION:
   Only `question` (and optionally `schema_context`, `conversation_context`,
   `request_id`, or `max_retries`) is provided at invocation.
   Intermediate stage fields are strictly absent until their respective nodes execute.
   Recovery counters initialize to `retry_count = 0` and `max_retries = 2`.

2. GENERATION:
   Populates `generation_response` (`SQLGenerationResponse`), including status
   ('answerable' vs. 'unsupported'), candidate SQL, and assumptions.

3. VALIDATION:
   Populates `validation_result` (`ValidationResult`) containing AST policy decisions,
   read-only verification, and rejection reason codes. If validation fails and
   retries remain (`retry_count < max_retries`), workflow routes to recovery/correction.

4. EXECUTION:
   Populates `execution_result` (`QueryResult`) with column names, sanitized row
   records, row counts, and truncation flags. Operates strictly read-only.

5. PRESENTATION & TERMINATION:
   Populates `presentation_result` (`PresentationResult`), terminal `status`
   (`PipelineStatus`), and any controlled `error_category` / `error_message`.

Retry Counter Semantics:
------------------------
- `retry_count`: The number of recovery/correction retries already performed,
  excluding the initial attempt. Initial value: 0.
- `max_retries`: The maximum permitted recovery retries beyond the initial attempt.
  Initial default: 2.
- The maximum number of generation attempts is enforced by future graph routing
  (`1 + max_retries`), not by an overlapping counter pair.

Runtime Typing Note:
--------------------
Python `TypedDict` and `Required`/`NotRequired` annotations provide static type hints
for static checkers (e.g., Mypy, Pyright) and LangGraph schema binding, but they do NOT
enforce runtime validation or raise exceptions upon dictionary instantiation. Runtime
invariants (non-empty question, non-negative counters, and absence of intermediate
fields) are explicitly validated by `create_initial_state()` and `validate_initial_state()`.

Safety & Isolation Invariants:
-------------------------------
- No database credentials, API keys, connection strings, or raw secrets are stored in state.
- No raw exception tracebacks or unredacted internal error dumps are stored in state.
- No fabricated intermediate values are populated at initialization.
- State defaults do NOT bypass SQL validation or execution safeguards.
"""

from typing import Any, Dict, List, NotRequired, Optional, Required, Sequence, TypedDict, Union

from query_pilot.db.executor import QueryResult
from query_pilot.db.models import DatabaseSchema
from query_pilot.observability import ErrorCategory, PipelineRunTelemetry
from query_pilot.pipeline import PipelineStatus
from query_pilot.presentation.formatter import PresentationResult
from query_pilot.presentation.synthesizer import GroundedAnswer
from query_pilot.sql.generation import (
    ALLOWED_ROLES,
    FORBIDDEN_SQL_FIELDS,
    MAX_CONVERSATION_TURNS,
    ClarificationOption,
    ConversationTurnContext,
    SQLGenerationResponse,
)
from query_pilot.sql.models import ValidationResult

# Fixed maximum generation attempts ceiling for bounded recovery (1 initial + at most 1 recovery)
MAX_GENERATION_ATTEMPTS: int = 2

# Default bounded recovery retry limit (excluding initial attempt)
DEFAULT_MAX_RETRIES: int = 1

# Intermediate result fields that must remain absent from initial state
INTERMEDIATE_RESULT_FIELDS: tuple[str, ...] = (
    "generation_response",
    "validation_result",
    "execution_result",
    "presentation_result",
    "status",
    "error_category",
    "error_message",
    "recovery_feedback",
    "grounded_answer",
    "telemetry",
)


class ConversationTurn(TypedDict, total=False):
    """Compact semantic representation of an optional prior conversation turn.

    Used strictly for optional contextual grounding without implementing
    conversational memory or stateful chat history in this layer.
    """

    role: str
    content: str
    user_question: str
    resolved_intent: Optional[str]
    result_columns: Optional[List[str]]


def validate_conversation_turn(turn: Any, index: int = 0) -> None:
    """Validate a single conversation turn representation.

    Enforces:
    - turn must be a dict or ConversationTurnContext.
    - 'user_question' must be present, or 'content' if role is 'user'.
    - 'role' if present must be 'user' (role 'assistant' is forbidden).
    - Unexpected SQL-like fields ('executed_sql', 'sql', 'query', 'candidate_sql') are forbidden.
    - Unexpected extra fields are strictly forbidden.
    - 'resolved_intent' if present must be a string or None.
    - 'result_columns' if present must be a list of non-empty strings or None.

    Args:
        turn: The conversation turn dictionary or ConversationTurnContext instance.
        index: Turn index in the sequence for diagnostic errors.

    Raises:
        TypeError: If turn is not a dict or ConversationTurnContext, or if fields have wrong types.
        ValueError: If turn fields violate boundaries (empty content, invalid role, forbidden fields).
    """
    if isinstance(turn, ConversationTurnContext):
        return

    if not isinstance(turn, dict):
        raise TypeError(
            f"Conversation turn at index {index} must be a dict or ConversationTurnContext, got {type(turn).__name__}."
        )

    # Forbid unexpected SQL-like fields
    for forbidden_field in FORBIDDEN_SQL_FIELDS:
        if forbidden_field in turn:
            raise ValueError(
                f"Conversation turn at index {index} must not contain '{forbidden_field}'."
            )

    # Forbid unexpected extra keys
    allowed_keys = {"role", "content", "user_question", "resolved_intent", "result_columns"}
    extra_keys = set(turn.keys()) - allowed_keys
    if extra_keys:
        raise ValueError(
            f"Conversation turn at index {index} contains unexpected fields: {sorted(extra_keys)}."
        )

    role = str(turn.get("role", "user")).strip().lower()
    if role == "assistant":
        raise ValueError(
            f"Conversation turn at index {index} has role 'assistant'. "
            "Conversation context strictly represents prior user-question interactions. Role must be 'user'."
        )
    if role != "user":
        raise ValueError(
            f"Conversation turn at index {index} has invalid role {turn.get('role')!r}. Role must be 'user'."
        )

    user_question = turn.get("user_question")
    content = turn.get("content")

    if user_question is not None:
        if not isinstance(user_question, str) or not user_question.strip():
            raise ValueError(
                f"Conversation turn at index {index} has empty or non-string 'user_question'."
            )
    elif content is not None:
        if role != "user":
            raise ValueError(
                f"Conversation turn at index {index} has role {turn.get('role')!r} with 'content'. "
                "Legacy 'content' fallback is only permitted for 'user' turns to prevent misrepresenting assistant prose as a user question."
            )
        if not isinstance(content, str) or not content.strip():
            raise ValueError(
                f"Conversation turn at index {index} has empty or non-string 'content'."
            )
    else:
        raise ValueError(
            f"Conversation turn at index {index} must contain 'user_question'."
        )

    resolved_intent = turn.get("resolved_intent")
    if resolved_intent is not None and not isinstance(resolved_intent, str):
        raise TypeError(
            f"Conversation turn at index {index} field 'resolved_intent' must be a str or None."
        )

    result_columns = turn.get("result_columns")
    if result_columns is not None:
        if not isinstance(result_columns, list):
            raise TypeError(
                f"Conversation turn at index {index} field 'result_columns' must be a list or None."
            )
        for col in result_columns:
            if not isinstance(col, str) or not col.strip():
                raise ValueError(
                    f"Conversation turn at index {index} has invalid column in 'result_columns': {col!r}."
                )


class QueryPilotState(TypedDict):
    """Typed shared state for QueryPilot agent workflows.

    Uses `Required` for mandatory invocation inputs and `NotRequired` for
    optional context and downstream intermediate stage outputs.
    """

    # -------------------------------------------------------------------------
    # 1. Mandatory Initial Input
    # -------------------------------------------------------------------------
    question: Required[str]
    """The natural language question submitted by the user."""

    # -------------------------------------------------------------------------
    # 2. Contextual & Operational Inputs (Optional at start)
    # -------------------------------------------------------------------------
    conversation_context: NotRequired[Optional[List[Union[ConversationTurn, ConversationTurnContext, Dict[str, Any]]]]]
    """Optional bounded semantic conversation context (at most 3 completed turns)."""

    schema_context: NotRequired[Optional[DatabaseSchema]]
    """Model-facing database schema metadata for SQL generation; redaction must be preserved."""

    request_id: NotRequired[Optional[str]]
    """Correlated unique request ID for structured observability across stages."""

    # -------------------------------------------------------------------------
    # 3. Bounded Recovery Attempt & Retry Counters
    #
    # SEMANTICS:
    # - `generation_attempts`: Authoritative count of SQL generation attempts begun (initial=0, max=2).
    # - `retry_count`: Number of recovery retries already performed, excluding the initial attempt.
    # - `max_retries`: Maximum permitted recovery retries (default: 1).
    # -------------------------------------------------------------------------
    generation_attempts: NotRequired[int]
    """Authoritative count of SQL generation attempts begun so far (initialized to 0)."""

    retry_count: NotRequired[int]
    """Number of recovery retries already performed, excluding the initial attempt."""

    max_retries: NotRequired[int]
    """Maximum permitted recovery retries."""

    recovery_feedback: NotRequired[Optional[str]]
    """Sanitized diagnostic guidance provided to next generation attempt."""

    # -------------------------------------------------------------------------
    # 4. Intermediate Stage Outputs (Populated by graph nodes; absent initially)
    # -------------------------------------------------------------------------
    generation_response: NotRequired[Optional[SQLGenerationResponse]]
    """Candidate SQL and structured status ('answerable' or 'unsupported') from generator."""

    validation_result: NotRequired[Optional[ValidationResult]]
    """Deterministic validation decision, rejection reason codes, and AST inspection details."""

    execution_result: NotRequired[Optional[QueryResult]]
    """Read-only database query execution result (columns, rows, truncation)."""

    presentation_result: NotRequired[Optional[PresentationResult]]
    """Deterministic user-facing presentation model (markdown table, scalar, or error message)."""

    # -------------------------------------------------------------------------
    # 5. Terminal Status & Controlled Error Classification
    # -------------------------------------------------------------------------
    status: NotRequired[Optional[PipelineStatus]]
    """High-level terminal pipeline outcome status (e.g., SUCCESS, UNSUPPORTED, etc.)."""

    error_category: NotRequired[Optional[ErrorCategory]]
    """Controlled, sanitized error category enum preventing secret or schema leaks."""

    error_message: NotRequired[Optional[str]]
    """Sanitized human-readable error description, free of stack traces or credentials."""

    # -------------------------------------------------------------------------
    # 6. Observability & Grounded Answer Synthesis (Populated downstream)
    # -------------------------------------------------------------------------
    grounded_answer: NotRequired[Optional[GroundedAnswer]]
    """Grounded natural language answer model or None if unavailable/unsupported."""

    telemetry: NotRequired[Optional[PipelineRunTelemetry]]
    """Aggregated LLM invocation usage metadata and execution latency."""


# Convenience alias
GraphState = QueryPilotState


def create_initial_state(
    question: str,
    *,
    schema_context: Optional[DatabaseSchema] = None,
    conversation_context: Optional[Sequence[Union[ConversationTurn, ConversationTurnContext, Dict[str, Any]]]] = None,
    request_id: Optional[str] = None,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> QueryPilotState:
    """Create a valid minimal initial state for graph execution.

    Invariants:
    - `question` is strictly validated to be a non-empty, non-whitespace string.
    - `max_retries` cannot be negative.
    - Initial values: `retry_count = 0`, `max_retries = max_retries`.
    - `conversation_context` is bounded to at most `MAX_CONVERSATION_TURNS` (3).
    - No intermediate stage outputs (SQL, validation, execution, presentation) are fabricated.

    Args:
        question: The user's natural language question.
        schema_context: Optional pre-acquired model-facing database schema.
        conversation_context: Optional prior conversation turns.
        request_id: Optional correlated request identifier.
        max_retries: Configured maximum retry attempts (default: 2). Cannot be negative.

    Returns:
        A cleanly initialized QueryPilotState.

    Raises:
        TypeError: If question is not a string, or max_retries is not an integer.
        ValueError: If question is empty or whitespace, or max_retries is negative.
    """
    if not isinstance(question, str):
        raise TypeError(f"Question must be a string, got {type(question).__name__}.")
    cleaned_question = question.strip()
    if not cleaned_question:
        raise ValueError("Question cannot be empty or whitespace.")

    if not isinstance(max_retries, int) or isinstance(max_retries, bool):
        raise TypeError(f"max_retries must be an integer, got {type(max_retries).__name__}.")
    if max_retries < 0:
        raise ValueError(f"max_retries cannot be negative, got {max_retries}.")

    state: QueryPilotState = {
        "question": cleaned_question,
        "generation_attempts": 0,
        "retry_count": 0,
        "max_retries": max_retries,
    }

    if schema_context is not None:
        state["schema_context"] = schema_context
    if conversation_context is not None:
        if not isinstance(conversation_context, (list, tuple)):
            raise TypeError(
                f"conversation_context must be a list, got {type(conversation_context).__name__}."
            )
        if len(conversation_context) > MAX_CONVERSATION_TURNS:
            raise ValueError(
                f"conversation_context cannot exceed {MAX_CONVERSATION_TURNS} turns, got {len(conversation_context)}."
            )
        for idx, turn in enumerate(conversation_context):
            validate_conversation_turn(turn, idx)
        state["conversation_context"] = list(conversation_context)
    if request_id is not None:
        state["request_id"] = request_id

    return state


def validate_initial_state(state: object) -> QueryPilotState:
    """Validate that an existing dictionary satisfies the initial state contract.

    Enforces runtime validation of:
    - Dict container type.
    - Presence of non-empty string `question`.
    - Non-negative `max_retries`, `retry_count`, and `generation_attempts`.
    - `retry_count <= max_retries`.
    - Strict absence of intermediate result fields in initial state.

    Args:
        state: The dictionary or mapping to validate.

    Returns:
        The validated state as a QueryPilotState.

    Raises:
        TypeError: If state is not a dict, or if field types are invalid.
        KeyError: If required key 'question' is missing.
        ValueError: If values violate domain constraints (empty question,
                    negative counters, retry_count > max_retries, or presence
                    of intermediate result fields).
    """
    if not isinstance(state, dict):
        raise TypeError(f"Graph state must be a dict, got {type(state).__name__}.")

    if "question" not in state:
        raise KeyError("Missing required initial state field: 'question'.")

    question = state["question"]
    if not isinstance(question, str):
        raise TypeError(f"Field 'question' must be a str, got {type(question).__name__}.")

    if not question.strip():
        raise ValueError("Field 'question' cannot be empty or whitespace.")

    # Validate counter fields if present
    max_retries = state.get("max_retries", DEFAULT_MAX_RETRIES)
    if "max_retries" in state:
        val = state["max_retries"]
        if not isinstance(val, int) or isinstance(val, bool):
            raise TypeError(f"Field 'max_retries' must be an integer, got {type(val).__name__}.")
        if val < 0:
            raise ValueError(f"max_retries cannot be negative, got {val}.")
        max_retries = val

    if "retry_count" in state:
        val = state["retry_count"]
        if not isinstance(val, int) or isinstance(val, bool):
            raise TypeError(f"Field 'retry_count' must be an integer, got {type(val).__name__}.")
        if val < 0:
            raise ValueError(f"retry_count cannot be negative, got {val}.")
        if val > max_retries:
            raise ValueError(
                f"retry_count ({val}) cannot exceed max_retries ({max_retries})."
            )

    if "generation_attempts" in state:
        val = state["generation_attempts"]
        if not isinstance(val, int) or isinstance(val, bool):
            raise TypeError(f"Field 'generation_attempts' must be an integer, got {type(val).__name__}.")
        if val < 0:
            raise ValueError(f"generation_attempts cannot be negative, got {val}.")

    if "conversation_context" in state and state["conversation_context"] is not None:
        cc = state["conversation_context"]
        if not isinstance(cc, (list, tuple)):
            raise TypeError(
                f"Field 'conversation_context' must be a list, got {type(cc).__name__}."
            )
        if len(cc) > MAX_CONVERSATION_TURNS:
            raise ValueError(
                f"conversation_context cannot exceed {MAX_CONVERSATION_TURNS} turns, got {len(cc)}."
            )
        for idx, turn in enumerate(cc):
            validate_conversation_turn(turn, idx)

    # Ensure intermediate result fields remain absent in initial state
    for field_name in INTERMEDIATE_RESULT_FIELDS:
        if field_name in state and state[field_name] is not None:
            raise ValueError(
                f"Intermediate field '{field_name}' must remain absent from initial state "
                "until populated by downstream graph nodes."
            )

    return state  # type: ignore[return-value]
