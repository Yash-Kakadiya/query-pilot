"""Focused offline unit tests for QueryPilot API models (Task 1.32-B).

Verifies:
1. QueryRequest validation, trimming, length bounding, and empty-check invariants.
2. Context turns bounding (0, 1, 2, 3 allowed; 4 rejected).
3. Rejection of forbidden client fields (sql, schema_context, max_retries, db_server, etc.).
4. Rejection of forbidden nested context fields (role='assistant', sql, extra keys).
5. QueryResponse safe projection and mapping from PipelineResult and PresentationResult.
6. Exclusion of generated SQL by default in public responses.
7. Status preservation across all 6 terminal pipeline states.
8. Sanitization of error messages and rejection reasons (no tracebacks or passwords).
9. Grounded HealthResponse metadata and secret-free guarantees.
"""

from decimal import Decimal
import pytest
from pydantic import ValidationError

from query_pilot.api.models import (
    GroundedAnswerResponse,
    HealthResponse,
    QueryRequest,
    QueryResponse,
    ValidationReasonResponse,
)
from query_pilot.db.executor import QueryResult
from query_pilot.pipeline import PipelineResult, PipelineStatus
from query_pilot.presentation.formatter import PresentationResult, format_pipeline_result
from query_pilot.presentation.synthesizer import GroundedAnswer, GroundedAnswerStatus
from query_pilot.sql.generation import (
    ClarificationOption,
    ConversationTurnContext,
    SQLGenerationResponse,
    SQLGenerationStatus,
)
from query_pilot.sql.models import ValidationReason, ValidationReasonCode, ValidationResult


# ==============================================================================
# 1. QueryRequest Tests
# ==============================================================================

def test_query_request_valid_minimal():
    """Verify minimal QueryRequest with valid question."""
    req = QueryRequest(question="  What is the average GPA?  ")
    assert req.question == "What is the average GPA?"
    assert req.conversation_context is None


def test_query_request_empty_and_whitespace():
    """Verify QueryRequest rejects empty and whitespace-only questions."""
    with pytest.raises(ValidationError, match="Question cannot be empty or whitespace"):
        QueryRequest(question="")

    with pytest.raises(ValidationError, match="Question cannot be empty or whitespace"):
        QueryRequest(question="   \t\n  ")


def test_query_request_non_string():
    """Verify QueryRequest rejects non-string questions."""
    with pytest.raises(ValidationError):
        QueryRequest(question=123)  # type: ignore

    with pytest.raises(ValidationError):
        QueryRequest(question=None)  # type: ignore


def test_query_request_length_boundaries():
    """Verify QueryRequest length bounding at 1000 characters."""
    # Exactly 1000 chars should pass
    exact_1000 = "a" * 1000
    req = QueryRequest(question=exact_1000)
    assert len(req.question) == 1000

    # 1001 chars should fail
    over_1000 = "a" * 1001
    with pytest.raises(ValidationError, match="Question exceeds maximum length of 1000 characters"):
        QueryRequest(question=over_1000)

    # 1002 chars with whitespace that trims to 1000 should pass
    padded = f"   {'a' * 1000}   "
    req_padded = QueryRequest(question=padded)
    assert len(req_padded.question) == 1000

    # Padded string trimming to 1001 should fail
    padded_over = f"   {'a' * 1001}   "
    with pytest.raises(ValidationError, match="Question exceeds maximum length of 1000 characters"):
        QueryRequest(question=padded_over)


def test_query_request_conversation_context_allowed_turns():
    """Verify QueryRequest accepts 1, 2, and 3 valid context turns."""
    turn1 = ConversationTurnContext(user_question="List all departments.")
    turn2 = ConversationTurnContext(
        user_question="Show student count in CS.",
        resolved_intent="student count",
        result_columns=["count"],
    )
    turn3 = ConversationTurnContext(user_question="Filter by year 2024.")

    # 1 turn
    req1 = QueryRequest(question="Who is the dean?", conversation_context=[turn1])
    assert len(req1.conversation_context) == 1

    # 3 turns
    req3 = QueryRequest(question="Who is the dean?", conversation_context=[turn1, turn2, turn3])
    assert len(req3.conversation_context) == 3


def test_query_request_conversation_context_rejection_of_four_turns():
    """Verify QueryRequest strictly rejects 4 or more conversation turns."""
    turns = [ConversationTurnContext(user_question=f"Question {i}") for i in range(4)]
    with pytest.raises(ValidationError, match="conversation_context cannot exceed 3 turns"):
        QueryRequest(question="Final question", conversation_context=turns)


def test_query_request_forbidden_top_level_fields():
    """Verify QueryRequest strictly rejects unapproved client controls via extra='forbid'."""
    forbidden_payloads = [
        {"question": "Valid question?", "sql": "SELECT 1"},
        {"question": "Valid question?", "schema_context": {"tables": []}},
        {"question": "Valid question?", "max_retries": 5},
        {"question": "Valid question?", "generation_attempts": 2},
        {"question": "Valid question?", "db_server": "localhost"},
        {"question": "Valid question?", "timeout_seconds": 60},
        {"question": "Valid question?", "max_rows": 500},
        {"question": "Valid question?", "internal_flag": True},
    ]

    for payload in forbidden_payloads:
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            QueryRequest(**payload)


def test_query_request_forbidden_nested_context_fields():
    """Verify QueryRequest rejects context turns with forbidden SQL or assistant roles."""
    # Rejects SQL in context turn
    with pytest.raises(ValidationError, match="forbidden"):
        QueryRequest(
            question="Next question?",
            conversation_context=[
                {"user_question": "Prior question", "sql": "SELECT * FROM Students"}  # type: ignore
            ],
        )

    # Rejects assistant role
    with pytest.raises(ValidationError, match="Role 'assistant' is not permitted"):
        QueryRequest(
            question="Next question?",
            conversation_context=[
                {"user_question": "Prior question", "role": "assistant"}  # type: ignore
            ],
        )

    # Rejects unknown extra nested fields
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        QueryRequest(
            question="Next question?",
            conversation_context=[
                {"user_question": "Prior question", "unapproved_field": "injected"}  # type: ignore
            ],
        )


# ==============================================================================
# 2. QueryResponse & Projection Tests
# ==============================================================================

def test_query_response_mapping_success():
    """Verify QueryResponse correctly projects a successful PipelineResult."""
    exec_res = QueryResult(
        columns=["Department", "AvgGrade"],
        rows=[{"Department": "Computer Science", "AvgGrade": Decimal("88.5")}],
        row_count=1,
        truncated=False,
        execution_time_ms=12.4,
    )
    answer = GroundedAnswer(
        text="The average grade in Computer Science is 88.5.",
        status=GroundedAnswerStatus.TEMPLATE_DETERMINISTIC,
        is_mechanically_verified=True,
    )
    pipe_res = PipelineResult(
        question="What is the average grade in CS?",
        status=PipelineStatus.SUCCESS,
        generated_sql="SELECT Department, AVG(Grade) AS AvgGrade FROM Enrollments GROUP BY Department",
        generation_explanation="Calculates average grade grouped by department.",
        execution_result=exec_res,
        request_id="req-12345",
        grounded_answer=answer,
    )

    # Default projection: generated_sql is excluded
    resp = QueryResponse.from_pipeline_result(pipe_res)
    assert resp.request_id == "req-12345"
    assert resp.status == PipelineStatus.SUCCESS
    assert resp.question == "What is the average grade in CS?"
    assert resp.columns == ["Department", "AvgGrade"]
    assert resp.row_count == 1
    assert resp.truncated is False
    assert resp.generated_sql is None  # Excluded by default
    assert resp.grounded_answer is not None
    assert resp.grounded_answer.text == "The average grade in Computer Science is 88.5."
    assert resp.grounded_answer.is_mechanically_verified is True
    assert resp.markdown_table is not None

    # Opt-in projection: generated_sql is included
    resp_with_sql = QueryResponse.from_pipeline_result(pipe_res, include_sql=True)
    assert resp_with_sql.generated_sql == "SELECT Department, AVG(Grade) AS AvgGrade FROM Enrollments GROUP BY Department"


def test_query_response_mapping_clarification_needed():
    """Verify QueryResponse correctly projects a CLARIFICATION_NEEDED PipelineResult."""
    opts = [
        ClarificationOption(
            option_id="opt_fall",
            description="Fall 2025 semester",
            suggested_question="Show grades for Fall 2025",
        ),
        ClarificationOption(
            option_id="opt_spring",
            description="Spring 2026 semester",
            suggested_question="Show grades for Spring 2026",
        ),
    ]
    pipe_res = PipelineResult(
        question="Show grades.",
        status=PipelineStatus.CLARIFICATION_NEEDED,
        request_id="req-clarify-1",
        clarification_prompt="Which semester would you like to view?",
        clarification_options=opts,
    )

    resp = QueryResponse.from_pipeline_result(pipe_res)
    assert resp.status == PipelineStatus.CLARIFICATION_NEEDED
    assert resp.clarification_prompt == "Which semester would you like to view?"
    assert len(resp.clarification_options) == 2
    assert resp.clarification_options[0].option_id == "opt_fall"
    assert resp.generated_sql is None


def test_query_response_mapping_unsupported():
    """Verify QueryResponse correctly projects an UNSUPPORTED PipelineResult."""
    pipe_res = PipelineResult(
        question="What is the weather tomorrow?",
        status=PipelineStatus.UNSUPPORTED,
        request_id="req-unsupp-1",
        generation_explanation="The database schema only covers student enrollment and grades.",
    )

    resp = QueryResponse.from_pipeline_result(pipe_res)
    assert resp.status == PipelineStatus.UNSUPPORTED
    assert resp.explanation == "The database schema only covers student enrollment and grades."
    assert resp.rows == []
    assert resp.generated_sql is None


def test_query_response_mapping_validation_rejected():
    """Verify QueryResponse correctly projects a VALIDATION_REJECTED PipelineResult."""
    val_res = ValidationResult(
        allowed=False,
        reasons=[
            ValidationReason(
                code=ValidationReasonCode.FORBIDDEN_OPERATION,
                message="Query contains forbidden mutating statement: DROP.",
            )
        ],
    )
    pipe_res = PipelineResult(
        question="DROP TABLE Students;",
        status=PipelineStatus.VALIDATION_REJECTED,
        request_id="req-val-1",
        validation_result=val_res,
        generated_sql="DROP TABLE Students;",
    )

    resp = QueryResponse.from_pipeline_result(pipe_res)
    assert resp.status == PipelineStatus.VALIDATION_REJECTED
    assert len(resp.validation_reasons) == 1
    assert resp.validation_reasons[0].code == "FORBIDDEN_OPERATION"
    assert "DROP" in resp.validation_reasons[0].message
    assert resp.generated_sql is None


def test_query_response_sanitizes_credentials_in_error_message():
    """Verify QueryResponse redacts passwords and connection strings from error messages."""
    pipe_res = PipelineResult(
        question="Show my account.",
        status=PipelineStatus.EXECUTION_FAILED,
        request_id="req-fail-1",
        error_message="Failed connecting to DB with UID=admin;PWD=supersecret_pass123; at host.",
    )

    resp = QueryResponse.from_pipeline_result(pipe_res)
    assert resp.status == PipelineStatus.EXECUTION_FAILED
    assert "supersecret_pass123" not in resp.message
    assert "[REDACTED]" in resp.message


def test_query_response_from_presentation_result():
    """Verify QueryResponse can be cleanly projected from PresentationResult."""
    pres = PresentationResult(
        status="success",
        request_id="req-pres-9",
        title="What is the course count?",
        message="Query executed successfully. 1 row returned.",
        columns=["CourseCount"],
        rows=[{"CourseCount": 42}],
        row_count=1,
        scalar_value=42,
        markdown_table="| CourseCount |\n|---:|\n| 42 |",
        generated_sql="SELECT COUNT(*) AS CourseCount FROM Courses",
    )

    resp = QueryResponse.from_presentation_result(pres, question="What is the course count?")
    assert resp.status == PipelineStatus.SUCCESS
    assert resp.question == "What is the course count?"
    assert resp.scalar_value == 42
    assert resp.generated_sql is None  # Excluded by default

    resp_with_sql = QueryResponse.from_presentation_result(
        pres, question="What is the course count?", include_sql=True
    )
    assert resp_with_sql.generated_sql == "SELECT COUNT(*) AS CourseCount FROM Courses"


def test_query_response_extra_fields_forbidden():
    """Verify QueryResponse strictly rejects unknown fields via extra='forbid'."""
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        QueryResponse(
            status=PipelineStatus.SUCCESS,
            question="Test?",
            internal_trace_id="should_be_forbidden",  # type: ignore
        )


# ==============================================================================
# 3. HealthResponse Tests
# ==============================================================================

def test_health_response_defaults():
    """Verify HealthResponse default values and configuration grounding."""
    health = HealthResponse()
    assert health.status == "healthy"
    assert health.version == "0.1.0"
    assert health.dialect == "T-SQL"
    assert health.tracing_enabled is False


def test_health_response_from_config():
    """Verify HealthResponse.from_config() safely reads settings without secret leaks."""
    health = HealthResponse.from_config()
    assert health.status == "healthy"
    assert health.version == "0.1.0"
    assert health.dialect == "T-SQL"
    # Never leaks database credentials, server names, or API keys
    dumped = health.model_dump()
    assert "api_key" not in dumped
    assert "db_password" not in dumped
    assert "db_server" not in dumped


def test_health_response_extra_fields_forbidden():
    """Verify HealthResponse rejects arbitrary extra keys."""
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        HealthResponse(injected_secret="hidden")  # type: ignore


# ==============================================================================
# 4. Regression Tests (Task 1.32-B2)
# ==============================================================================

def test_query_response_mapping_generation_failed():
    """Verify QueryResponse correctly projects a GENERATION_FAILED PipelineResult."""
    pipe_res = PipelineResult(
        question="How many students?",
        status=PipelineStatus.GENERATION_FAILED,
        request_id="req-gen-fail",
        error_message="Model failed to generate valid SQL with apiKey: aq.secret_api_key_123.",
    )

    resp = QueryResponse.from_pipeline_result(pipe_res)
    assert resp.status == PipelineStatus.GENERATION_FAILED
    assert resp.request_id == "req-gen-fail"
    assert "aq.secret_api_key_123" not in (resp.message or "")
    assert "[REDACTED]" in (resp.message or "")
    assert resp.generated_sql is None


def test_query_response_sanitizes_credentials_in_explanation():
    """Verify explanation field is sanitized of credential-like patterns."""
    pipe_res = PipelineResult(
        question="Show course list.",
        status=PipelineStatus.SUCCESS,
        generation_explanation="Executed against host using UID=sa; PWD=SuperSecretPass999; for filtering.",
    )

    resp = QueryResponse.from_pipeline_result(pipe_res)
    assert "SuperSecretPass999" not in (resp.explanation or "")
    assert "[REDACTED]" in (resp.explanation or "")

    # Also verify via from_presentation_result
    pres = PresentationResult(
        status="success",
        title="Show course list.",
        explanation="Connected with bearer eyJhbGciOi.token to query courses.",
    )
    resp_pres = QueryResponse.from_presentation_result(pres, question="Show course list.")
    assert "eyJhbGciOi" not in (resp_pres.explanation or "")
    assert "[REDACTED]" in (resp_pres.explanation or "")


def test_query_response_sanitizes_credentials_in_clarification_prompt():
    """Verify clarification_prompt is sanitized of credential-like patterns."""
    pipe_res = PipelineResult(
        question="Find user.",
        status=PipelineStatus.CLARIFICATION_NEEDED,
        clarification_prompt="Did you mean password=plain_secret_val for user admin?",
    )

    resp = QueryResponse.from_pipeline_result(pipe_res)
    assert "plain_secret_val" not in (resp.clarification_prompt or "")
    assert "[REDACTED]" in (resp.clarification_prompt or "")

    # Also verify via from_presentation_result
    pres = PresentationResult(
        status="clarification_needed",
        title="Find user.",
        clarification_prompt="Verify password=plain_secret_val before proceeding.",
    )
    resp_pres = QueryResponse.from_presentation_result(pres, question="Find user.")
    assert "plain_secret_val" not in (resp_pres.clarification_prompt or "")
    assert "[REDACTED]" in (resp_pres.clarification_prompt or "")


def test_query_response_wire_serialization_contract():
    """Verify model_dump(mode='json') and model_dump_json() maintain safe wire representation."""
    resp = QueryResponse(
        request_id="req-wire-1",
        status=PipelineStatus.SUCCESS,
        question="What is the average GPA?",
        columns=["GPA"],
        rows=[{"GPA": 3.75}],
        row_count=1,
    )

    # 1. model_dump(mode="json")
    dumped = resp.model_dump(mode="json")
    assert dumped["status"] == "SUCCESS"
    assert isinstance(dumped["status"], str)
    assert dumped["generated_sql"] is None
    assert dumped["request_id"] == "req-wire-1"
    assert dumped["row_count"] == 1

    # 2. model_dump_json()
    json_str = resp.model_dump_json()
    assert '"status":"SUCCESS"' in json_str or '"status": "SUCCESS"' in json_str
    assert '"generated_sql":null' in json_str or '"generated_sql": null' in json_str
    assert "password" not in json_str


def test_query_response_from_presentation_result_unknown_status_raises_value_error():
    """Verify unknown presentation status strictly raises ValueError instead of becoming SUCCESS."""
    pres_unknown = PresentationResult(
        status="unrecognized_state_code",
        title="Test question",
        message="Unknown state message",
    )

    with pytest.raises(ValueError, match="Unrecognized presentation status: 'unrecognized_state_code'"):
        QueryResponse.from_presentation_result(pres_unknown, question="Test question")


def test_query_response_from_presentation_result_explicit_status_handling():
    """Verify explicit status argument handling in from_presentation_result."""
    pres = PresentationResult(
        status="success",
        title="Test question",
    )

    # Valid PipelineStatus instance
    resp1 = QueryResponse.from_presentation_result(
        pres, question="Test question", status=PipelineStatus.UNSUPPORTED
    )
    assert resp1.status == PipelineStatus.UNSUPPORTED

    # Valid string status
    resp2 = QueryResponse.from_presentation_result(
        pres, question="Test question", status="unsupported"
    )
    assert resp2.status == PipelineStatus.UNSUPPORTED

    # Invalid string status raises ValueError
    with pytest.raises(ValueError, match="Unrecognized status: 'invalid_code'"):
        QueryResponse.from_presentation_result(
            pres, question="Test question", status="invalid_code"
        )

    # Invalid type raises TypeError
    with pytest.raises(TypeError, match="Status must be a PipelineStatus or str"):
        QueryResponse.from_presentation_result(
            pres, question="Test question", status=123  # type: ignore
        )
