"""Unit tests for Structured Generation Failure Diagnostics (B27-B.3).

Verifies:
1. classify_generation_failure maps provider exceptions to structured classifications:
   - 429 / RESOURCE_EXHAUSTED -> QUOTA_EXHAUSTED with reliable retry delay extraction.
   - 429 / RESOURCE_EXHAUSTED without retry delay -> QUOTA_EXHAUSTED (no fabricated countdown).
   - 401 / 403 / API_KEY_INVALID -> AUTHENTICATION_FAILED.
   - 500 / 502 / 503 / 504 / UNAVAILABLE -> PROVIDER_UNAVAILABLE.
   - ConnectionError / Timeout / DNS -> NETWORK_ERROR.
   - Generic exceptions -> UNKNOWN.
   - Secrets, API keys, raw traceback bodies are NEVER leaked in user-facing message.
2. Pydantic model invariants for GenerateSQLResponse:
   - Failure diagnostic fields strictly forbidden when status == GENERATED.
   - Candidate SQL strictly forbidden when status == GENERATION_FAILED.
   - Nested failure_diagnostic and top-level fields stay synchronized.
3. API route integration (/api/v1/generate-sql):
   - Returns HTTP 200 with status="generation_failed" and structured diagnostic.
   - Request ID is preserved for correlation.
   - Zero database execution or candidate leakage.
"""

from unittest.mock import MagicMock
import pytest
from fastapi.testclient import TestClient

from query_pilot.api.app import create_app
from query_pilot.api.models import (
    ColumnSchemaContext,
    GenerateSQLRequest,
    GenerateSQLResponse,
    GenerationFailureClassification,
    GenerationFailureDiagnostic,
    GenerationStatus,
    ModelFacingSchemaContext,
    TableSchemaContext,
)
from query_pilot.sql.failure_classifier import (
    classify_generation_failure,
    extract_retry_guidance,
)
from query_pilot.sql.generation import (
    ProviderAPIError,
    SQLGenerationError,
    SQLGenerationResponse,
    SQLGenerationStatus,
)


# ==============================================================================
# 1. Failure Classifier Unit Tests
# ==============================================================================

def test_classify_quota_exhausted_with_retry_delay():
    # Simulates Google GenAI 429 RESOURCE_EXHAUSTED with retryDelay in error details
    raw_msg = (
        "429 Resource has been exhausted (e.g. check quota). "
        "{'error': {'code': 429, 'message': 'Resource has been exhausted', "
        "'status': 'RESOURCE_EXHAUSTED', 'details': [{'@type': 'type.googleapis.com/google.rpc.RetryInfo', "
        "'retryDelay': '12070s'}]}}"
    )
    exc = ProviderAPIError(raw_msg)
    diagnostic = classify_generation_failure(exc)

    assert diagnostic.classification == GenerationFailureClassification.QUOTA_EXHAUSTED
    assert "quota exhausted" in diagnostic.message.lower()
    assert diagnostic.retry_guidance is not None
    assert "3.4 hours" in diagnostic.retry_guidance
    # Ensure raw dict string is not dumped as the message
    assert "type.googleapis.com" not in diagnostic.message


def test_classify_quota_exhausted_without_retry_delay():
    # Simulates 429 without retryDelay
    exc = Exception("429 Too Many Requests: free-tier request limit reached")
    diagnostic = classify_generation_failure(exc)

    assert diagnostic.classification == GenerationFailureClassification.QUOTA_EXHAUSTED
    assert "quota exhausted" in diagnostic.message.lower()
    # Must NOT fabricate a countdown or reset time
    assert diagnostic.retry_guidance is None


def test_classify_authentication_failure():
    # Simulates invalid API key error
    exc = ProviderAPIError("403 Forbidden: API key not valid. Please pass a valid API_KEY_INVALID")
    diagnostic = classify_generation_failure(exc)

    assert diagnostic.classification == GenerationFailureClassification.AUTHENTICATION_FAILED
    assert "authentication" in diagnostic.message.lower() or "credentials" in diagnostic.message.lower()
    assert diagnostic.retry_guidance is None


def test_classify_provider_unavailable():
    # Simulates 503 Service Unavailable
    exc = ProviderAPIError("503 Service Unavailable: The model is currently overloaded. Please try again.")
    diagnostic = classify_generation_failure(exc)

    assert diagnostic.classification == GenerationFailureClassification.PROVIDER_UNAVAILABLE
    assert "unavailable" in diagnostic.message.lower() or "overloaded" in diagnostic.message.lower()


def test_classify_network_error():
    # Simulates connection timeout
    exc = TimeoutError("Connection to generativelanguage.googleapis.com timed out after 30 seconds")
    diagnostic = classify_generation_failure(exc)

    assert diagnostic.classification == GenerationFailureClassification.NETWORK_ERROR
    assert "network" in diagnostic.message.lower() or "connectivity" in diagnostic.message.lower()


def test_classify_unknown_error():
    # Simulates generic unknown error
    exc = ValueError("Unexpected internal state in parser")
    diagnostic = classify_generation_failure(exc)

    assert diagnostic.classification == GenerationFailureClassification.UNKNOWN
    assert "unexpected error" in diagnostic.message.lower()
    assert diagnostic.retry_guidance is None


def test_classify_never_leaks_secrets():
    # Simulates exception string that accidentally includes an API key or bearer token
    exc = Exception("Error calling upstream with key=AIzaSyB_secret_token_123456 and password=supersecret")
    diagnostic = classify_generation_failure(exc)

    # User message must never contain the secret key or raw password
    assert "AIzaSyB_secret_token_123456" not in diagnostic.message
    assert "supersecret" not in diagnostic.message
    assert diagnostic.retry_guidance is None or "secret" not in diagnostic.retry_guidance


def test_extract_retry_guidance_helper():
    assert extract_retry_guidance("retryDelay': '45s'") == "Provider requested waiting at least 45 seconds before retrying."
    assert extract_retry_guidance("retry in 120s") == "Provider requested waiting at least 2 minutes before retrying."
    assert extract_retry_guidance("retryDelay': '7200s'") == "Provider requested waiting at least 2 hours before retrying."
    assert extract_retry_guidance("no retry delay mentioned") is None


# ==============================================================================
# 2. Pydantic Invariant Tests on GenerateSQLResponse
# ==============================================================================

def test_generate_sql_response_failure_invariants():
    # Valid generation_failed response with diagnostic
    diag = GenerationFailureDiagnostic(
        classification=GenerationFailureClassification.QUOTA_EXHAUSTED,
        message="Quota exceeded",
        retry_guidance="Retry after 60 seconds.",
    )
    resp = GenerateSQLResponse(
        candidate_sql=None,
        status=GenerationStatus.GENERATION_FAILED,
        failure_diagnostic=diag,
        request_id="req-diag-001",
    )
    assert resp.status == GenerationStatus.GENERATION_FAILED
    assert resp.candidate_sql is None
    # Automatically synchronized top-level fields
    assert resp.failure_classification == GenerationFailureClassification.QUOTA_EXHAUSTED
    assert resp.failure_message == "Quota exceeded"
    assert resp.retry_guidance == "Retry after 60 seconds."


def test_generate_sql_response_rejects_candidate_when_failed():
    diag = GenerationFailureDiagnostic(
        classification=GenerationFailureClassification.QUOTA_EXHAUSTED,
        message="Quota exceeded",
    )
    with pytest.raises(ValueError, match="Generation failed status must not contain candidate_sql"):
        GenerateSQLResponse(
            candidate_sql="SELECT 1;",
            status=GenerationStatus.GENERATION_FAILED,
            failure_diagnostic=diag,
            request_id="req-diag-002",
        )


def test_generate_sql_response_rejects_failure_fields_when_generated():
    with pytest.raises(ValueError, match="Generated status must not contain failure diagnostics"):
        GenerateSQLResponse(
            candidate_sql="SELECT 1;",
            status=GenerationStatus.GENERATED,
            failure_classification=GenerationFailureClassification.QUOTA_EXHAUSTED,
            request_id="req-diag-003",
        )


# ==============================================================================
# 3. Route Tests for POST /api/v1/generate-sql
# ==============================================================================

@pytest.fixture
def mock_gen():
    gen = MagicMock()
    return gen


@pytest.fixture
def api_client(mock_gen):
    mock_runner = MagicMock()
    mock_synth = MagicMock()
    app = create_app(
        runner=mock_runner,
        sql_generator=mock_gen,
        synthesizer=mock_synth,
    )
    return TestClient(app)


@pytest.fixture
def valid_schema_dict():
    return {
        "database_name": "university_demo",
        "tables": [
            {
                "table_name": "courses",
                "schema_name": "dbo",
                "columns": [
                    {"name": "course_id", "data_type": "int", "primary_key": True, "nullable": False},
                    {"name": "title", "data_type": "varchar", "primary_key": False, "nullable": False},
                ],
            }
        ],
    }


def test_generate_sql_route_surfaces_quota_failure(api_client, mock_gen, valid_schema_dict):
    # Simulate upstream Gemini 429 quota exhaustion
    mock_gen.generate.side_effect = SQLGenerationError(
        "Provider error: 429 Resource has been exhausted (e.g. check quota). retryDelay': '3600s'"
    )

    req_payload = {
        "question": "What is the total number of courses?",
        "schema_context": valid_schema_dict,
        "request_id": "req-route-quota-429",
    }

    response = api_client.post("/api/v1/generate-sql", json=req_payload)

    # Must return HTTP 200 with status="generation_failed"
    assert response.status_code == 200
    data = response.json()

    assert data["status"] == "generation_failed"
    assert data["candidate_sql"] is None
    assert data["failure_classification"] == "quota_exhausted"
    assert "quota" in data["failure_message"].lower()
    assert "1 hour" in data["retry_guidance"]
    assert data["failure_diagnostic"]["classification"] == "quota_exhausted"
    assert data["failure_diagnostic"]["retry_guidance"] == data["retry_guidance"]
    assert data["request_id"] == "req-route-quota-429"


def test_generate_sql_route_surfaces_empty_candidate_failure(api_client, mock_gen, valid_schema_dict):
    # Simulate provider returning empty candidate via mock object
    mock_resp = MagicMock()
    mock_resp.status = SQLGenerationStatus.ANSWERABLE
    mock_resp.sql = "   "
    mock_gen.generate.return_value = mock_resp

    req_payload = {
        "question": "List courses",
        "schema_context": valid_schema_dict,
        "request_id": "req-route-empty-candidate",
    }

    response = api_client.post("/api/v1/generate-sql", json=req_payload)

    assert response.status_code == 200
    data = response.json()

    assert data["status"] == "generation_failed"
    assert data["candidate_sql"] is None
    assert data["failure_classification"] == "unknown"
    assert "empty candidate sql" in data["failure_message"].lower()
    assert data["request_id"] == "req-route-empty-candidate"
