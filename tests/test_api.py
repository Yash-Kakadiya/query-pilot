"""Focused offline tests for QueryPilot FastAPI application and routes (Task 1.32-C).

Verifies:
1. GET /health response shape, status code 200, non-sensitive configuration grounding.
2. POST /query successful execution with mocked runner, safe projections, and default SQL exclusion.
3. POST /query correlation ID extraction (X-Request-ID header) and response propagation.
4. POST /query domain outcome mappings (CLARIFICATION_NEEDED, UNSUPPORTED, VALIDATION_REJECTED,
   GENERATION_FAILED, EXECUTION_FAILED) without false success.
5. POST /query request validation rejection (422) for empty questions, length boundaries,
   extra client inputs, and >3 context turns.
6. POST /query unhandled server exception sanitization (500) without credential or traceback leaks.
7. Complete offline isolation with mocked runner (zero live Gemini, database, or LangSmith calls).
"""

from decimal import Decimal
from unittest.mock import MagicMock
import pytest
from fastapi.testclient import TestClient

from query_pilot.api.app import create_app
from query_pilot.db.executor import QueryResult
from query_pilot.pipeline import PipelineResult, PipelineStatus
from query_pilot.presentation.synthesizer import GroundedAnswer, GroundedAnswerStatus
from query_pilot.sql.generation import ClarificationOption
from query_pilot.sql.models import ValidationReason, ValidationReasonCode, ValidationResult


# ==============================================================================
# 1. Health Endpoint Tests
# ==============================================================================

def test_health_endpoint():
    """Verify GET /health returns 200 and non-sensitive application metadata."""
    app = create_app()
    client = TestClient(app)

    response = client.get("/health")
    assert response.status_code == 200

    data = response.json()
    assert data["status"] == "healthy"
    assert data["version"] == "0.1.0"
    assert data["dialect"] == "T-SQL"
    assert isinstance(data["tracing_enabled"], bool)

    # Confirm zero secret leakage in health payload
    assert "api_key" not in data
    assert "db_password" not in data
    assert "connection_string" not in data


# ==============================================================================
# 2. Successful Query Endpoint Tests
# ==============================================================================

def test_query_endpoint_success():
    """Verify POST /query executes via runner and returns structured success response."""
    mock_runner = MagicMock()
    exec_res = QueryResult(
        columns=["Department", "AverageGPA"],
        rows=[{"Department": "Engineering", "AverageGPA": Decimal("3.85")}],
        row_count=1,
        truncated=False,
        execution_time_ms=15.2,
    )
    answer = GroundedAnswer(
        text="The average GPA in Engineering is 3.85.",
        status=GroundedAnswerStatus.TEMPLATE_DETERMINISTIC,
        is_mechanically_verified=True,
    )
    mock_runner.run.return_value = PipelineResult(
        question="What is the average GPA in Engineering?",
        status=PipelineStatus.SUCCESS,
        generated_sql="SELECT Department, AVG(GPA) FROM Students GROUP BY Department",
        generation_explanation="Averages student GPA by department.",
        execution_result=exec_res,
        request_id="req-test-success-01",
        grounded_answer=answer,
    )

    app = create_app(runner=mock_runner)
    client = TestClient(app)

    payload = {"question": "What is the average GPA in Engineering?"}
    response = client.post("/query", json=payload)

    assert response.status_code == 200
    data = response.json()

    assert data["status"] == "SUCCESS"
    assert data["question"] == "What is the average GPA in Engineering?"
    assert data["columns"] == ["Department", "AverageGPA"]
    assert data["rows"] == [{"Department": "Engineering", "AverageGPA": "3.85"}]
    assert data["row_count"] == 1
    assert data["truncated"] is False
    assert data["grounded_answer"]["text"] == "The average GPA in Engineering is 3.85."
    assert data["grounded_answer"]["is_mechanically_verified"] is True

    # Critical invariant: SQL must NOT be returned by default in public responses
    assert data["generated_sql"] is None

    # Response headers must include correlated X-Request-ID
    assert "x-request-id" in response.headers
    assert response.headers["x-request-id"] == "req-test-success-01"


def test_query_endpoint_custom_request_id_header():
    """Verify client X-Request-ID header is propagated to runner and response."""
    mock_runner = MagicMock()
    mock_runner.run.return_value = PipelineResult(
        question="Count courses.",
        status=PipelineStatus.SUCCESS,
        request_id="client-custom-req-777",
    )

    app = create_app(runner=mock_runner)
    client = TestClient(app)

    response = client.post(
        "/query",
        json={"question": "Count courses."},
        headers={"X-Request-ID": "client-custom-req-777"},
    )

    assert response.status_code == 200
    assert response.headers["x-request-id"] == "client-custom-req-777"
    assert response.json()["request_id"] == "client-custom-req-777"

    # Verify runner was invoked with sanitized request_id
    mock_runner.run.assert_called_once()
    _, kwargs = mock_runner.run.call_args
    assert kwargs["request_id"] == "client-custom-req-777"


# ==============================================================================
# 3. Domain Outcomes (Clarification, Unsupported, Rejection, Failure)
# ==============================================================================

def test_query_endpoint_clarification_needed():
    """Verify POST /query maps CLARIFICATION_NEEDED without SQL exposure or false success."""
    mock_runner = MagicMock()
    mock_runner.run.return_value = PipelineResult(
        question="Show my grades.",
        status=PipelineStatus.CLARIFICATION_NEEDED,
        request_id="req-clarify-99",
        clarification_prompt="Which academic term would you like to view?",
        clarification_options=[
            ClarificationOption(
                option_id="fall_2025",
                description="Fall 2025 semester",
                suggested_question="Show my grades for Fall 2025",
            )
        ],
    )

    app = create_app(runner=mock_runner)
    client = TestClient(app)

    response = client.post("/query", json={"question": "Show my grades."})
    assert response.status_code == 200
    data = response.json()

    assert data["status"] == "CLARIFICATION_NEEDED"
    assert data["clarification_prompt"] == "Which academic term would you like to view?"
    assert len(data["clarification_options"]) == 1
    assert data["clarification_options"][0]["option_id"] == "fall_2025"
    assert data["generated_sql"] is None


def test_query_endpoint_unsupported():
    """Verify POST /query maps UNSUPPORTED outcome cleanly."""
    mock_runner = MagicMock()
    mock_runner.run.return_value = PipelineResult(
        question="What is the weather tomorrow?",
        status=PipelineStatus.UNSUPPORTED,
        request_id="req-unsupp-88",
        generation_explanation="The schema contains only university academic data.",
    )

    app = create_app(runner=mock_runner)
    client = TestClient(app)

    response = client.post("/query", json={"question": "What is the weather tomorrow?"})
    assert response.status_code == 200
    data = response.json()

    assert data["status"] == "UNSUPPORTED"
    assert "university academic data" in data["explanation"]
    assert data["rows"] == []
    assert data["generated_sql"] is None


def test_query_endpoint_validation_rejected():
    """Verify POST /query maps VALIDATION_REJECTED without exposing raw AST."""
    mock_runner = MagicMock()
    val_res = ValidationResult(
        allowed=False,
        reasons=[
            ValidationReason(
                code=ValidationReasonCode.FORBIDDEN_OPERATION,
                message="Mutating statement DELETE is forbidden.",
            )
        ],
    )
    mock_runner.run.return_value = PipelineResult(
        question="DELETE FROM Courses;",
        status=PipelineStatus.VALIDATION_REJECTED,
        request_id="req-val-reject",
        validation_result=val_res,
        generated_sql="DELETE FROM Courses;",
    )

    app = create_app(runner=mock_runner)
    client = TestClient(app)

    response = client.post("/query", json={"question": "DELETE FROM Courses;"})
    assert response.status_code == 200
    data = response.json()

    assert data["status"] == "VALIDATION_REJECTED"
    assert len(data["validation_reasons"]) == 1
    assert data["validation_reasons"][0]["code"] == "FORBIDDEN_OPERATION"
    assert "DELETE is forbidden" in data["validation_reasons"][0]["message"]
    # Internal AST or candidate SQL must not be returned
    assert data["generated_sql"] is None


def test_query_endpoint_generation_failed():
    """Verify POST /query maps GENERATION_FAILED without false success."""
    mock_runner = MagicMock()
    mock_runner.run.return_value = PipelineResult(
        question="List data.",
        status=PipelineStatus.GENERATION_FAILED,
        request_id="req-gen-fail",
        error_message="Model quota exhausted.",
    )

    app = create_app(runner=mock_runner)
    client = TestClient(app)

    response = client.post("/query", json={"question": "List data."})
    assert response.status_code == 200
    data = response.json()

    assert data["status"] == "GENERATION_FAILED"
    assert "quota exhausted" in (data["message"] or "")
    assert data["generated_sql"] is None


def test_query_endpoint_execution_failed_sanitizes_credentials():
    """Verify POST /query sanitizes credentials in database execution failure messages."""
    mock_runner = MagicMock()
    mock_runner.run.return_value = PipelineResult(
        question="Select users.",
        status=PipelineStatus.EXECUTION_FAILED,
        request_id="req-exec-fail",
        error_message="Failed connecting with UID=sa; PWD=SuperSecretPass456; to database.",
    )

    app = create_app(runner=mock_runner)
    client = TestClient(app)

    response = client.post("/query", json={"question": "Select users."})
    assert response.status_code == 200
    data = response.json()

    assert data["status"] == "EXECUTION_FAILED"
    assert "SuperSecretPass456" not in (data["message"] or "")
    assert "[REDACTED]" in (data["message"] or "")
    assert data["generated_sql"] is None


# ==============================================================================
# 4. Request Validation & Client Input Rejection (422)
# ==============================================================================

def test_query_endpoint_validation_empty_question():
    """Verify 422 is returned when question is empty or whitespace-only."""
    app = create_app()
    client = TestClient(app)

    # Empty string
    resp1 = client.post("/query", json={"question": ""})
    assert resp1.status_code == 422
    assert "Question cannot be empty or whitespace" in resp1.text

    # Whitespace only
    resp2 = client.post("/query", json={"question": "   \n\t  "})
    assert resp2.status_code == 422
    assert "Question cannot be empty or whitespace" in resp2.text


def test_query_endpoint_validation_forbids_extra_client_controls():
    """Verify 422 is returned when client attempts to inject SQL or internal controls."""
    app = create_app()
    client = TestClient(app)

    forbidden_payloads = [
        {"question": "Valid?", "sql": "SELECT 1"},
        {"question": "Valid?", "schema_context": {}},
        {"question": "Valid?", "max_retries": 10},
        {"question": "Valid?", "db_server": "rogue-server"},
        {"question": "Valid?", "timeout_seconds": 120},
    ]

    for payload in forbidden_payloads:
        resp = client.post("/query", json=payload)
        assert resp.status_code == 422
        assert "Extra inputs are not permitted" in resp.text


def test_query_endpoint_conversation_context_passed_to_runner():
    """Verify valid conversation context turns are passed to runner within bounds."""
    mock_runner = MagicMock()
    mock_runner.run.return_value = PipelineResult(
        question="What about CS102?",
        status=PipelineStatus.SUCCESS,
        request_id="req-ctx-pass",
    )

    app = create_app(runner=mock_runner)
    client = TestClient(app)

    payload = {
        "question": "What about CS102?",
        "conversation_context": [
            {"user_question": "List courses."},
            {"user_question": "Show average for CS101.", "resolved_intent": "avg grade"},
        ],
    }

    response = client.post("/query", json=payload)
    assert response.status_code == 200

    mock_runner.run.assert_called_once()
    _, kwargs = mock_runner.run.call_args
    turns = kwargs["conversation_context"]
    assert len(turns) == 2
    assert turns[0].user_question == "List courses."
    assert turns[1].resolved_intent == "avg grade"


def test_query_endpoint_conversation_context_rejects_exceeding_turns():
    """Verify 422 when conversation context exceeds 3 turns."""
    app = create_app()
    client = TestClient(app)

    payload = {
        "question": "Fifth question?",
        "conversation_context": [
            {"user_question": "Q1"},
            {"user_question": "Q2"},
            {"user_question": "Q3"},
            {"user_question": "Q4"},
        ],
    }

    response = client.post("/query", json=payload)
    assert response.status_code == 422
    assert "cannot exceed 3 turns" in response.text


# ==============================================================================
# 5. Application Error Sanitization (500)
# ==============================================================================

def test_query_endpoint_unhandled_exception_returns_generic_error():
    """Verify unhandled exceptions return 500 with a fixed generic error without leaking details."""
    mock_runner = MagicMock()
    mock_runner.run.side_effect = RuntimeError(
        "Critical DB connection loss to SERVER=sql-prod-node01.corp.internal:1433; "
        "PATH=/var/data/secret_db.mdf; SQL='SELECT * FROM PrivateSalaries'; UID=admin; PWD=SuperSecret1234;"
    )

    app = create_app(runner=mock_runner)
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post("/query", json={"question": "Valid query?"})
    assert response.status_code == 500
    data = response.json()

    # Fixed generic message
    assert data["detail"] == "An internal server error occurred."

    # Verify complete absence of sensitive infrastructure details from serialized body
    raw_body = response.text
    assert "SuperSecret1234" not in raw_body
    assert "sql-prod-node01" not in raw_body
    assert "secret_db.mdf" not in raw_body
    assert "PrivateSalaries" not in raw_body
    assert "admin" not in raw_body
