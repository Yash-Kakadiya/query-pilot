"""Offline Workflow Contract Tests for Milestone B9-C3.

Verifies end-to-end presentation, metadata-only hosted synthesis, and privacy contracts:
1. Local deterministic presentation displays actual returned scalar values faithfully (tested in frontend/tests/answerSynthesis.test.ts).
2. Hosted metadata synthesis receives no raw result rows or cell values.
3. Pre-screening refusal: representative row-disclosure questions are refused before any hosted model is called.
4. Post-generation screening: heuristic checks safely reject or downgrade unsupported numeric/currency claims.
5. Strict schema validation: invalid hosted synthesis payloads (e.g. unexpected row records, malformed column identifiers, invalid execution status) are rejected.
6. SQL literal masking: constants in candidate SQL ASTs are replaced with redaction placeholders.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

from fastapi import status
from fastapi.testclient import TestClient
import pytest

from query_pilot.api.app import create_app
from query_pilot.api.models import SynthesizeAnswerRequest, SynthesizeAnswerResponse
from query_pilot.config import Settings
from query_pilot.presentation.synthesizer import (
    AnswerGroundingStatus,
    AnswerSynthesisService,
    mask_sql_literals,
)


# ==============================================================================
# B9-C3 Hosted Synthesis Payload & Privacy Boundary Tests
# ==============================================================================

class TestB9C3HostedSynthesisPayloadContracts:
    """Verifies that the hosted synthesis endpoint enforces metadata-only boundaries."""

    def test_hosted_synthesis_rejects_payloads_with_raw_rows(self):
        """POST /api/v1/synthesize-answer rejects client payloads with raw row data (HTTP 422)."""
        app = create_app(settings=Settings(allowed_origins=["*"]))
        client = TestClient(app)

        # Attempt to pass raw row records into hosted synthesis
        payload_with_rows = {
            "question": "How many total records are in the sample table?",
            "candidate_sql": "SELECT COUNT(*) AS total_records FROM dbo.qp_test_sample;",
            "columns": ["total_records"],
            "row_count": 1,
            "truncated": False,
            "execution_status": "succeeded",
            "execution_time_ms": 14.5,
            "request_id": "b9-c3-meta-001",
            "rows": [{"total_records": 2}],  # Forbidden field!
        }

        resp = client.post("/api/v1/synthesize-answer", json=payload_with_rows)
        assert resp.status_code in (status.HTTP_422_UNPROCESSABLE_ENTITY, 422), (
            f"Expected HTTP 422 when raw rows are provided, got {resp.status_code}: {resp.text}"
        )
        assert "extra_forbidden" in resp.text or "extra fields not permitted" in resp.text.lower()

    def test_hosted_synthesis_rejects_invalid_column_syntax(self):
        """POST /api/v1/synthesize-answer rejects malformed, serialized, or prose column names."""
        app = create_app(settings=Settings(allowed_origins=["*"]))
        client = TestClient(app)

        invalid_columns = [
            "col; DROP TABLE users; --",
            '{"key": "value"}',
            "This is a prose sentence rather than a column name",
            "col\x00nullbyte",
        ]

        for bad_col in invalid_columns:
            payload = {
                "question": "How many total records?",
                "candidate_sql": "SELECT COUNT(*) AS total_records FROM dbo.qp_test_sample;",
                "columns": [bad_col],
                "row_count": 1,
                "truncated": False,
                "execution_status": "succeeded",
                "execution_time_ms": 10.0,
                "request_id": "b9-c3-bad-col",
            }
            resp = client.post("/api/v1/synthesize-answer", json=payload)
            assert resp.status_code in (status.HTTP_422_UNPROCESSABLE_ENTITY, 422), (
                f"Expected HTTP 422 for bad column name '{bad_col}', got {resp.status_code}: {resp.text}"
            )

    def test_hosted_synthesis_rejects_non_succeeded_execution_status(self):
        """POST /api/v1/synthesize-answer rejects requests where execution_status != 'succeeded'."""
        app = create_app(settings=Settings(allowed_origins=["*"]))
        client = TestClient(app)

        for bad_status in ["failed", "error", "pending", "timed_out"]:
            payload = {
                "question": "How many total records?",
                "candidate_sql": "SELECT COUNT(*) AS total_records FROM dbo.qp_test_sample;",
                "columns": ["total_records"],
                "row_count": 0,
                "truncated": False,
                "execution_status": bad_status,
                "execution_time_ms": 10.0,
                "request_id": "b9-c3-bad-status",
            }
            resp = client.post("/api/v1/synthesize-answer", json=payload)
            assert resp.status_code in (status.HTTP_422_UNPROCESSABLE_ENTITY, 422), (
                f"Expected HTTP 422 for execution_status '{bad_status}', got {resp.status_code}: {resp.text}"
            )


# ==============================================================================
# B9-C3 Hosted Metadata Synthesis Prompt & Non-Leakage Contracts
# ==============================================================================

class TestB9C3HostedSynthesisPromptContracts:
    """Verifies that hosted synthesis prompts receive strictly metadata and no row values."""

    def test_hosted_metadata_synthesis_prompt_receives_no_rows_or_cell_values(self):
        """Outbound LLM prompt contains execution metadata only and never receives scalar values."""
        captured_contents: List[str] = []

        mock_gemini_client = MagicMock()
        mock_response = MagicMock()
        mock_response.text = "The query executed successfully and returned 1 record in the result set."
        mock_response.usage_metadata = None

        def _capture_call(model, contents, config):
            captured_contents.append(str(contents))
            return mock_response

        mock_gemini_client.models.generate_content.side_effect = _capture_call

        service = AnswerSynthesisService(gemini_client=mock_gemini_client)

        # Feed metadata corresponding to B9-C2 execution (1 row returned for total_records count)
        summary, status_out, notes = service.synthesize_from_metadata(
            question="How many total records are in the sample table?",
            candidate_sql="SELECT COUNT(*) AS total_records FROM dbo.qp_test_sample;",
            columns=["total_records"],
            row_count=1,
            truncated=False,
            execution_time_ms=12.5,
            request_id="b9-c3-e2e-001",
        )

        assert status_out == AnswerGroundingStatus.GROUNDED_FROM_METADATA
        assert len(captured_contents) == 1
        prompt = captured_contents[0]

        # Verify metadata presence
        assert "Total Returned Rows: 1" in prompt
        assert "Columns: total_records" in prompt
        assert "SELECT COUNT(*) AS total_records FROM dbo.qp_test_sample" in prompt

        # Verify that scalar value '2' or raw row structures were NEVER passed into the prompt
        assert "total_records: 2" not in prompt
        assert "{'total_records': 2}" not in prompt
        assert '{"total_records": 2}' not in prompt

    def test_sql_literal_masking_in_synthesis_prompt(self):
        """Sensitive literal constants in WHERE clauses are redacted prior to model prompt."""
        captured_contents: List[str] = []

        mock_gemini_client = MagicMock()
        mock_response = MagicMock()
        mock_response.text = "The query completed and returned the matching row count."
        mock_gemini_client.models.generate_content.side_effect = lambda model, contents, config: (
            captured_contents.append(str(contents)) or mock_response
        )

        service = AnswerSynthesisService(gemini_client=mock_gemini_client)

        sensitive_sql = (
            "SELECT student_id FROM Students "
            "WHERE ssn = '999-88-7777' AND secret_token = 'sk_live_xyz123' AND salary > 125000;"
        )

        service.synthesize_from_metadata(
            question="Find the matching student record",
            candidate_sql=sensitive_sql,
            columns=["student_id"],
            row_count=1,
            truncated=False,
            execution_time_ms=8.0,
        )

        assert len(captured_contents) == 1
        prompt = captured_contents[0]

        # Raw literals must be redacted
        assert "999-88-7777" not in prompt
        assert "sk_live_xyz123" not in prompt
        assert "125000" not in prompt
        assert "[REDACTED_VALUE]" in prompt or "[REDACTED_NUMBER]" in prompt


# ==============================================================================
# B9-C3 Pre-Screening Refusal vs Post-Generation Screening Tests
# ==============================================================================

class TestB9C3RefusalAndScreeningContracts:
    """Verifies that pre-screening refusal and post-generation screening are separate defenses."""

    @pytest.mark.parametrize(
        "disclosure_question",
        [
            "Who is the lead instructor for CS101?",
            "Which employee received the highest bonus?",
            "What is the email of user admin?",
            "Show all names and phone numbers",
            "What is the GPA of student 4521?",
            "What is the total salary paid to department 10?",
        ],
    )
    def test_representative_row_disclosure_questions_refused_before_model_call(
        self,
        disclosure_question: str,
    ):
        """Pre-Screening: Questions requiring specific undisclosed cell values trigger refusal with 0 model calls."""
        mock_gemini_client = MagicMock()
        mock_gemini_client.models.generate_content.return_value.text = "Fallthrough text"
        service = AnswerSynthesisService(gemini_client=mock_gemini_client)

        summary, status_out, notes = service.synthesize_from_metadata(
            question=disclosure_question,
            candidate_sql="SELECT name, email FROM Users WHERE role = 'admin';",
            columns=["name", "email"],
            row_count=1,
            execution_time_ms=5.0,
        )

        # Refusal contract
        assert status_out == AnswerGroundingStatus.REQUIRES_ROW_DISCLOSURE
        assert "QueryPilot protects data privacy by not transmitting database records" in summary
        assert "Please view the result directly in the local data table below" in summary

        # Observable guarantee: Hosted model was NEVER invoked
        assert mock_gemini_client.models.generate_content.call_count == 0

    def test_post_generation_screening_rejects_hallucinated_currency(self):
        """Post-Screening: Model outputs containing currency symbols are caught and downgraded."""
        mock_gemini_client = MagicMock()
        mock_response = MagicMock()
        # Model invents a dollar figure not present in metadata
        mock_response.text = "Based on the records, the average fee is $1,500.00."
        mock_gemini_client.models.generate_content.return_value = mock_response

        service = AnswerSynthesisService(gemini_client=mock_gemini_client)

        # High-level question that passes pre-screening
        summary, status_out, notes = service.synthesize_from_metadata(
            question="Summarize the fee records",
            candidate_sql="SELECT fee FROM CourseFees;",
            columns=["fee"],
            row_count=5,
            execution_time_ms=10.0,
        )

        # Model was called because pre-screening passed
        assert mock_gemini_client.models.generate_content.call_count == 1

        # Post-screening detected currency and downgraded to REQUIRES_ROW_DISCLOSURE
        assert status_out == AnswerGroundingStatus.REQUIRES_ROW_DISCLOSURE
        assert "QueryPilot protects data privacy by not transmitting database records" in summary
        assert "$1,500.00" not in summary

    def test_post_generation_screening_rejects_hallucinated_unauthorized_numbers(self):
        """Post-Screening: Model outputs containing unauthorized numbers are caught and downgraded."""
        mock_gemini_client = MagicMock()
        mock_response = MagicMock()
        # Model invents an ungrounded numeric metric (9999) not present in question, sql, or metadata
        mock_response.text = "The analysis revealed 9999 distinct transactions in the dataset."
        mock_gemini_client.models.generate_content.return_value = mock_response

        service = AnswerSynthesisService(gemini_client=mock_gemini_client)

        # High-level question that passes pre-screening
        summary, status_out, notes = service.synthesize_from_metadata(
            question="Summarize the transactions dataset",
            candidate_sql="SELECT transaction_id FROM Transactions;",
            columns=["transaction_id"],
            row_count=10,
            execution_time_ms=15.0,
        )

        # Model was called because pre-screening passed
        assert mock_gemini_client.models.generate_content.call_count == 1

        # Post-screening caught ungrounded number 9999 and downgraded
        assert status_out == AnswerGroundingStatus.REQUIRES_ROW_DISCLOSURE
        assert "QueryPilot protects data privacy by not transmitting database records" in summary
        assert "9999" not in summary
