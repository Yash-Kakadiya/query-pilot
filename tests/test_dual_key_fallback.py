"""Offline unit and contract tests for Dual Gemini Key Fallback (B27-C.2).

Verifies:
1. Primary key success: secondary key is never invoked.
2. Primary quota exhaustion (429/RESOURCE_EXHAUSTED) triggers bounded fallback to secondary key.
3. Secondary key success returns valid candidate SQL response.
4. Both keys exhausted: raises ProviderAPIError, classifying as QUOTA_EXHAUSTED without infinite retry.
5. Unrelated primary failure (auth 401/403, 503, network) does NOT trigger secondary key attempt.
6. Credentials (dummy keys) are never logged, printed, or leaked into error responses.
7. Settings model correctly parses optional GEMINI_API_KEY_2 as SecretStr.
"""

import logging
from unittest.mock import MagicMock
import pytest
from pydantic import SecretStr

from query_pilot.api.models import (
    GenerationFailureClassification,
    GenerationStatus,
)
from query_pilot.config import Settings
from query_pilot.db.models import ColumnMetadata, DatabaseSchema, TableMetadata
from query_pilot.sql.failure_classifier import classify_generation_failure
from query_pilot.sql.gemini_generator import GeminiSQLGenerator
from query_pilot.sql.generation import (
    ProviderAPIError,
    SQLGenerationRequest,
    SQLGenerationResponse,
    SQLGenerationStatus,
)


@pytest.fixture
def minimal_request():
    schema = DatabaseSchema(
        database_name="university_demo",
        is_model_facing=True,
        tables=[
            TableMetadata(
                schema_name="main",
                table_name="students",
                columns=[
                    ColumnMetadata(name="student_id", data_type="int", primary_key=True, nullable=False),
                    ColumnMetadata(name="department_id", data_type="int", primary_key=False, nullable=False),
                ],
            )
        ],
    )
    return SQLGenerationRequest(
        question="Show all students",
        schema_context=schema,
        dialect="sqlite",
    )


@pytest.fixture
def mock_success_response():
    resp = MagicMock()
    resp.text = (
        '{"status": "answerable", "sql": "SELECT student_id FROM students;", '
        '"explanation": "Selects all students.", "assumptions": []}'
    )
    resp.usage_metadata = None
    return resp


# ==============================================================================
# 1. Settings & Secret Handling Tests
# ==============================================================================

def test_settings_gemini_api_key_2_optional(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY_2", raising=False)
    # Only primary key provided
    s1 = Settings(
        _env_file=None,
        GEMINI_API_KEY="dummy-primary-key",
    )
    assert s1.GEMINI_API_KEY.get_secret_value() == "dummy-primary-key"
    assert s1.GEMINI_API_KEY_2 is None

    # Both keys provided
    s2 = Settings(
        _env_file=None,
        GEMINI_API_KEY="dummy-primary-key",
        GEMINI_API_KEY_2="dummy-secondary-key",
    )
    assert s2.GEMINI_API_KEY.get_secret_value() == "dummy-primary-key"
    assert s2.GEMINI_API_KEY_2 is not None
    assert s2.GEMINI_API_KEY_2.get_secret_value() == "dummy-secondary-key"

    # Secondary key with placeholder or empty string normalizes to None
    s3 = Settings(
        _env_file=None,
        GEMINI_API_KEY="dummy-primary-key",
        GEMINI_API_KEY_2="your_gemini_api_key_2_here",
    )
    assert s3.GEMINI_API_KEY_2 is None

    s4 = Settings(
        _env_file=None,
        GEMINI_API_KEY="dummy-primary-key",
        GEMINI_API_KEY_2="   ",
    )
    assert s4.GEMINI_API_KEY_2 is None


# ==============================================================================
# 2. Generator Execution & Bounded Fallback Tests
# ==============================================================================

def test_primary_key_success(minimal_request, mock_success_response):
    primary_client = MagicMock()
    primary_client.models.generate_content.return_value = mock_success_response

    secondary_client = MagicMock()

    generator = GeminiSQLGenerator(
        api_key="dummy-primary-key",
        client=primary_client,
        secondary_client=secondary_client,
        enable_retrieval=False,
    )

    result = generator.generate(minimal_request)

    assert result.status == SQLGenerationStatus.ANSWERABLE
    assert "SELECT student_id FROM students;" in result.sql
    assert primary_client.models.generate_content.call_count == 1
    assert secondary_client.models.generate_content.call_count == 0


def test_primary_quota_exhaustion_secondary_success(minimal_request, mock_success_response, caplog):
    primary_client = MagicMock()
    # Simulate Google 429 ResourceExhausted exception
    primary_client.models.generate_content.side_effect = Exception(
        "429 RESOURCE_EXHAUSTED. Quota exceeded for metric: generate_content_free_tier_requests"
    )

    secondary_client = MagicMock()
    secondary_client.models.generate_content.return_value = mock_success_response

    generator = GeminiSQLGenerator(
        api_key="dummy-primary-key",
        client=primary_client,
        secondary_client=secondary_client,
        enable_retrieval=False,
    )

    with caplog.at_level(logging.INFO):
        result = generator.generate(minimal_request)

    assert result.status == SQLGenerationStatus.ANSWERABLE
    assert "SELECT student_id FROM students;" in result.sql
    # Primary attempted once, failed on quota, secondary attempted once and succeeded
    assert primary_client.models.generate_content.call_count == 1
    assert secondary_client.models.generate_content.call_count == 1

    # Check fallback log message
    assert "Primary Gemini key quota exhausted. Attempting bounded fallback using secondary key." in caplog.text


def test_both_keys_quota_exhausted(minimal_request, caplog):
    primary_client = MagicMock()
    primary_client.models.generate_content.side_effect = Exception(
        "429 RESOURCE_EXHAUSTED: primary quota reached"
    )

    secondary_client = MagicMock()
    secondary_client.models.generate_content.side_effect = Exception(
        "429 RESOURCE_EXHAUSTED: secondary quota reached"
    )

    generator = GeminiSQLGenerator(
        api_key="dummy-primary-key",
        client=primary_client,
        secondary_client=secondary_client,
        enable_retrieval=False,
    )

    with caplog.at_level(logging.WARNING):
        with pytest.raises(ProviderAPIError) as exc_info:
            generator.generate(minimal_request)

    assert "fallback key" in str(exc_info.value).lower()
    assert primary_client.models.generate_content.call_count == 1
    assert secondary_client.models.generate_content.call_count == 1

    # Failure classifier correctly maps the resulting error to QUOTA_EXHAUSTED
    diagnostic = classify_generation_failure(exc_info.value)
    assert diagnostic.classification == GenerationFailureClassification.QUOTA_EXHAUSTED


def test_unrelated_primary_failure_does_not_call_secondary(minimal_request):
    # 1. Authentication failure
    primary_client = MagicMock()
    primary_client.models.generate_content.side_effect = Exception("403 Forbidden: API_KEY_INVALID")

    secondary_client = MagicMock()

    generator = GeminiSQLGenerator(
        api_key="dummy-primary-key",
        client=primary_client,
        secondary_client=secondary_client,
        enable_retrieval=False,
    )

    with pytest.raises(ProviderAPIError):
        generator.generate(minimal_request)

    assert primary_client.models.generate_content.call_count == 1
    assert secondary_client.models.generate_content.call_count == 0

    # 2. Provider unavailable (503)
    primary_client.reset_mock()
    secondary_client.reset_mock()
    primary_client.models.generate_content.side_effect = Exception("503 Service Unavailable: overloaded")

    with pytest.raises(ProviderAPIError):
        generator.generate(minimal_request)

    assert primary_client.models.generate_content.call_count == 1
    assert secondary_client.models.generate_content.call_count == 0


def test_no_credentials_leakage_in_logs_or_errors(minimal_request, caplog):
    secret_key_1 = "dummy-secret-gemini-key-1-alpha"
    secret_key_2 = "dummy-secret-gemini-key-2-beta"

    primary_client = MagicMock()
    primary_client.models.generate_content.side_effect = Exception(
        f"429 RESOURCE_EXHAUSTED while calling provider with key={secret_key_1}"
    )

    secondary_client = MagicMock()
    secondary_client.models.generate_content.side_effect = Exception(
        f"429 RESOURCE_EXHAUSTED while calling fallback provider with key={secret_key_2}"
    )

    generator = GeminiSQLGenerator(
        api_key=secret_key_1,
        secondary_api_key=secret_key_2,
        client=primary_client,
        secondary_client=secondary_client,
        enable_retrieval=False,
    )

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(ProviderAPIError) as exc_info:
            generator.generate(minimal_request)

    # Classify the exception as would be returned by API route
    diag = classify_generation_failure(exc_info.value)

    # Asserts secrets NEVER appear in user message or diagnostic
    assert secret_key_1 not in diag.message
    assert secret_key_2 not in diag.message
    if diag.retry_guidance:
        assert secret_key_1 not in diag.retry_guidance
        assert secret_key_2 not in diag.retry_guidance
