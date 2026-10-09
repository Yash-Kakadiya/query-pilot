"""Focused tests for QueryPilot LangGraph shared state contract (Task 1.18-R).

Tests deterministic state representations, typing constraints, intermediate-field
isolation, counter semantics, and LangGraph schema compatibility entirely offline.
"""

import pytest
from langgraph.graph import StateGraph

from query_pilot.db.executor import QueryResult
from query_pilot.db.models import ColumnMetadata, DatabaseSchema, TableMetadata
from query_pilot.graph.state import (
    DEFAULT_MAX_RETRIES,
    ConversationTurn,
    GraphState,
    QueryPilotState,
    create_initial_state,
    validate_initial_state,
)
from query_pilot.observability import ErrorCategory
from query_pilot.pipeline import PipelineStatus
from query_pilot.presentation.formatter import PresentationResult
from query_pilot.sql.generation import (
    SQLGenerationResponse,
    SQLGenerationStatus,
)
from query_pilot.sql.models import (
    ValidationReason,
    ValidationReasonCode,
    ValidationResult,
)


# ==============================================================================
# 1. Minimal Initial State Representation & Counter Defaults
# ==============================================================================

def test_minimal_initial_state_representation() -> None:
    """Verify that a minimal valid initial state containing only a question can be represented."""
    raw_state: QueryPilotState = {"question": "How many courses are active?"}
    validated = validate_initial_state(raw_state)
    assert validated["question"] == "How many courses are active?"

    factory_state = create_initial_state("How many courses are active?")
    assert factory_state["question"] == "How many courses are active?"
    assert factory_state["retry_count"] == 0
    assert factory_state["max_retries"] == DEFAULT_MAX_RETRIES


def test_optional_initial_context_can_be_provided() -> None:
    """Verify optional context fields (schema, conversation, request_id) can be included."""
    schema = DatabaseSchema(
        database_name="GradeSense_Local",
        tables=[
            TableMetadata(
                schema_name="dbo",
                table_name="Students",
                columns=[ColumnMetadata(name="StudentID", data_type="int", primary_key=True)],
            )
        ],
        is_model_facing=True,
    )
    turns: list[ConversationTurn] = [{"role": "user", "content": "Previous question"}]
    state = create_initial_state(
        "Follow-up question",
        schema_context=schema,
        conversation_context=turns,
        request_id="req-12345",
        max_retries=1,
    )

    assert state["question"] == "Follow-up question"
    assert state.get("schema_context") is schema
    assert state.get("conversation_context") == turns
    assert state.get("request_id") == "req-12345"
    assert state.get("max_retries") == 1
    assert state.get("retry_count") == 0


# ==============================================================================
# 2. Intermediate Fields Absent Initially (No Fabrication)
# ==============================================================================

def test_intermediate_fields_absent_initially() -> None:
    """Verify intermediate fields are strictly absent before their nodes execute."""
    state = create_initial_state("List all departments")

    forbidden_initial_keys = [
        "generation_response",
        "validation_result",
        "execution_result",
        "presentation_result",
        "status",
        "error_category",
        "error_message",
    ]
    for key in forbidden_initial_keys:
        assert key not in state, f"Intermediate key '{key}' must not be fabricated at initialization."


def test_validate_initial_state_rejects_intermediate_fields() -> None:
    """Verify validate_initial_state rejects states containing intermediate outputs."""
    gen_resp = SQLGenerationResponse(
        status=SQLGenerationStatus.ANSWERABLE,
        sql="SELECT 1;",
    )
    invalid_state: dict = {
        "question": "What is the tuition?",
        "generation_response": gen_resp,
    }
    with pytest.raises(ValueError, match="must remain absent from initial state"):
        validate_initial_state(invalid_state)


def test_intermediate_fields_populated_downstream() -> None:
    """Verify downstream nodes can populate intermediate fields incrementally."""
    state = create_initial_state("List all departments")

    # Step 1: Generation node populates response
    gen_resp = SQLGenerationResponse(
        status=SQLGenerationStatus.ANSWERABLE,
        sql="SELECT DepartmentName FROM Departments;",
        explanation="Lists all department names.",
    )
    state["generation_response"] = gen_resp
    assert state["generation_response"] is gen_resp

    # Step 2: Validation node populates result
    val_res = ValidationResult(allowed=True, normalized_sql=gen_resp.sql)
    state["validation_result"] = val_res
    assert state["validation_result"].allowed is True

    # Step 3: Execution node populates result
    exec_res = QueryResult(
        columns=["DepartmentName"],
        rows=[{"DepartmentName": "Math"}, {"DepartmentName": "Science"}],
        row_count=2,
        truncated=False,
        execution_time_ms=12.5,
    )
    state["execution_result"] = exec_res
    assert state["execution_result"].row_count == 2

    # Step 4: Presentation node populates final output
    pres_res = PresentationResult(
        status="success",
        title=state["question"],
        columns=["DepartmentName"],
        rows=exec_res.rows,
        row_count=2,
        generated_sql=gen_resp.sql,
    )
    state["presentation_result"] = pres_res
    state["status"] = PipelineStatus.SUCCESS

    assert state["status"] == PipelineStatus.SUCCESS
    assert state["presentation_result"].status == "success"


# ==============================================================================
# 3. Existing Pipeline Domain Models Direct Representation
# ==============================================================================

def test_existing_pipeline_domain_models_slot_directly() -> None:
    """Verify existing domain models slot directly into state without wrappers or conversion."""
    schema = DatabaseSchema(
        database_name="GradeSense_Local",
        tables=[
            TableMetadata(
                schema_name="dbo",
                table_name="Courses",
                columns=[ColumnMetadata(name="CourseID", data_type="int")],
            )
        ],
        is_model_facing=True,
    )
    gen_resp = SQLGenerationResponse(
        status=SQLGenerationStatus.UNSUPPORTED,
        sql=None,
        explanation="Not supported by schema.",
    )
    val_res = ValidationResult(
        allowed=False,
        reasons=[
            ValidationReason(
                code=ValidationReasonCode.UNKNOWN_TABLE,
                message="Table does not exist.",
            )
        ],
    )
    exec_res = QueryResult(
        columns=[],
        rows=[],
        row_count=0,
        truncated=False,
    )
    pres_res = PresentationResult(
        status="unsupported",
        message="Question cannot be answered.",
    )

    state: QueryPilotState = {
        "question": "What is the tuition cost?",
        "schema_context": schema,
        "generation_response": gen_resp,
        "validation_result": val_res,
        "execution_result": exec_res,
        "presentation_result": pres_res,
        "status": PipelineStatus.UNSUPPORTED,
        "error_category": ErrorCategory.UNSUPPORTED_QUESTION,
        "error_message": "Unsupported schema question.",
    }

    assert isinstance(state["schema_context"], DatabaseSchema)
    assert isinstance(state["generation_response"], SQLGenerationResponse)
    assert isinstance(state["validation_result"], ValidationResult)
    assert isinstance(state["execution_result"], QueryResult)
    assert isinstance(state["presentation_result"], PresentationResult)
    assert state["status"] == PipelineStatus.UNSUPPORTED
    assert state["error_category"] == ErrorCategory.UNSUPPORTED_QUESTION
    assert state["error_message"] == "Unsupported schema question."


# ==============================================================================
# 4. Retry-Counter Semantics and Constraints
# ==============================================================================

def test_retry_counter_semantics_and_lifecycle() -> None:
    """Verify retry_count tracks recovery retries (excluding initial attempt) bounded by max_retries."""
    state = create_initial_state("Show course count", max_retries=2)

    # Initial state: 0 retries performed, limit is 2
    assert state["retry_count"] == 0
    assert state["max_retries"] == 2

    # Simulate first recovery retry (e.g. after initial validation rejection)
    assert state["retry_count"] < state["max_retries"]
    state["retry_count"] += 1
    assert state["retry_count"] == 1

    # Remaining retries helper logic
    retries_remaining = state["max_retries"] - state["retry_count"]
    assert retries_remaining == 1

    # Simulate second recovery retry
    assert state["retry_count"] < state["max_retries"]
    state["retry_count"] += 1
    assert state["retry_count"] == 2

    # Now recovery limit is reached
    assert state["retry_count"] >= state["max_retries"]
    retries_exhausted = state["max_retries"] - state["retry_count"]
    assert retries_exhausted == 0


def test_negative_max_retries_rejected() -> None:
    """Verify negative max_retries is rejected with ValueError."""
    with pytest.raises(ValueError, match="max_retries cannot be negative"):
        create_initial_state("Query", max_retries=-1)

    with pytest.raises(ValueError, match="max_retries cannot be negative"):
        validate_initial_state({"question": "Query", "max_retries": -1})


def test_negative_retry_count_rejected() -> None:
    """Verify negative retry_count is rejected with ValueError."""
    with pytest.raises(ValueError, match="retry_count cannot be negative"):
        validate_initial_state({"question": "Query", "retry_count": -1})


def test_retry_count_exceeding_max_retries_rejected() -> None:
    """Verify retry_count exceeding max_retries is rejected with ValueError."""
    with pytest.raises(ValueError, match="cannot exceed max_retries"):
        validate_initial_state({"question": "Query", "retry_count": 3, "max_retries": 2})

    # Default max_retries is 2; retry_count of 3 exceeds it
    with pytest.raises(ValueError, match="cannot exceed max_retries"):
        validate_initial_state({"question": "Query", "retry_count": 3})


def test_boolean_counters_rejected() -> None:
    """Verify booleans are not accepted as integer counters."""
    with pytest.raises(TypeError, match="must be an integer"):
        create_initial_state("Query", max_retries=True)  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="must be an integer"):
        validate_initial_state({"question": "Query", "max_retries": True})  # type: ignore[dict-item]

    with pytest.raises(TypeError, match="must be an integer"):
        validate_initial_state({"question": "Query", "retry_count": False})  # type: ignore[dict-item]


# ==============================================================================
# 5. Invalid or Missing Required Initial Input Handling & Runtime Typing
# ==============================================================================

def test_missing_question_in_validate_initial_state() -> None:
    """Verify missing 'question' raises KeyError."""
    with pytest.raises(KeyError, match="Missing required initial state field: 'question'"):
        validate_initial_state({})


def test_non_dict_in_validate_initial_state() -> None:
    """Verify non-dict input raises TypeError."""
    with pytest.raises(TypeError, match="Graph state must be a dict"):
        validate_initial_state(["invalid", "list"])  # type: ignore[arg-type]


def test_empty_or_whitespace_question_rejected() -> None:
    """Verify empty or whitespace-only question raises ValueError in both creation and validation."""
    with pytest.raises(ValueError, match="cannot be empty or whitespace"):
        create_initial_state("")

    with pytest.raises(ValueError, match="cannot be empty or whitespace"):
        create_initial_state("   \t\n  ")

    with pytest.raises(ValueError, match="cannot be empty or whitespace"):
        validate_initial_state({"question": ""})

    with pytest.raises(ValueError, match="cannot be empty or whitespace"):
        validate_initial_state({"question": "   \n\t  "})


def test_non_string_question_rejected() -> None:
    """Verify non-string question raises TypeError in both creation and validation."""
    with pytest.raises(TypeError, match="must be a string"):
        create_initial_state(None)  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="must be a string"):
        create_initial_state(42)  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="Field 'question' must be a str"):
        validate_initial_state({"question": 12345})  # type: ignore[dict-item]

    with pytest.raises(TypeError, match="Field 'question' must be a str"):
        validate_initial_state({"question": None})  # type: ignore[dict-item]


def test_type_annotations_alone_do_not_enforce_runtime_validation() -> None:
    """Verify that Python TypedDict annotations alone do not enforce runtime validation.

    Demonstrates that constructing a dictionary without create_initial_state or
    validate_initial_state does not raise exceptions at runtime, which is why explicit
    validation functions are necessary.
    """
    # At runtime, TypedDict instantiation does not raise TypeError/KeyError on missing keys
    unvalidated_dict: dict = {}
    assert "question" not in unvalidated_dict

    # Explicit validation catches the missing required field at runtime
    with pytest.raises(KeyError, match="Missing required initial state field: 'question'"):
        validate_initial_state(unvalidated_dict)


# ==============================================================================
# 6. LangGraph StateGraph Runtime Integration
# ==============================================================================

def test_langgraph_stategraph_schema_compatibility() -> None:
    """Verify LangGraph StateGraph accepts QueryPilotState and compiles offline."""
    builder = StateGraph(QueryPilotState)

    def dummy_node(state: QueryPilotState) -> dict:
        return {"retry_count": state.get("retry_count", 0) + 1}

    builder.add_node("recovery_step", dummy_node)
    builder.set_entry_point("recovery_step")
    builder.set_finish_point("recovery_step")

    app = builder.compile()

    initial = create_initial_state("What is the average GPA?")
    result = app.invoke(initial)

    assert result["question"] == "What is the average GPA?"
    assert result["retry_count"] == 1
    assert result["max_retries"] == DEFAULT_MAX_RETRIES


# ==============================================================================
# 7. Security Isolation: No Credential or Traceback Fields
# ==============================================================================

def test_no_credentials_or_tracebacks_in_schema() -> None:
    """Verify schema does not define credential, secret, connection, or traceback fields."""
    forbidden_field_substrings = [
        "password",
        "pwd",
        "secret",
        "api_key",
        "credential",
        "connection_string",
        "conn_str",
        "traceback",
        "stack_trace",
    ]
    annotations = QueryPilotState.__annotations__
    for field_name in annotations:
        for forbidden in forbidden_field_substrings:
            assert forbidden not in field_name.lower(), (
                f"Field '{field_name}' violates security isolation constraints."
            )
