"""Deterministic Result Explanation & Faithfulness Benchmark Harness (B13-EVAL2).

Implements pure offline evaluation of QueryPilot's result presentation and answer synthesis layer:
- Deterministic scalar formatting and neutral aggregate fallback.
- Empty result set handling (zero LLM calls).
- Heuristic detection of unsupported currency symbols and numeric values.
- Unverified narrative tagging for multi-row LLM prose.
- Metadata-only answer synthesis vs. row-disclosure pre-screening.
- AST SQL literal masking and fail-closed protections.
- Truncation caveats and provider failure fallbacks.

Key Guarantees:
- 100% offline and deterministic (zero live Gemini API calls, zero database connections).
- Directly exercises production functions in query_pilot.presentation.synthesizer.
- Evidence-based metrics dynamically derived from executed scenario assertions.
- Explicit disclosure of heuristic boundaries: distinguishes regex/token heuristics from full semantic faithfulness.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import json
import logging
from pathlib import Path
import subprocess
import sys
from typing import Any, Callable, Dict, List, Optional, Tuple
from unittest.mock import MagicMock

from pydantic import BaseModel, Field

from query_pilot.db.executor import QueryResult
from query_pilot.presentation.synthesizer import (
    AnswerGroundingStatus,
    AnswerSynthesisService,
    GroundedAnswer,
    GroundedAnswerStatus,
    _detect_hallucinated_values,
    _extract_column_tokens,
    _question_requires_row_disclosure,
    format_scalar_template,
    mask_sql_literals,
)

logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parent.parent


# ==============================================================================
# 1. Enums and Data Models
# ==============================================================================

class ExplanationCategory(str, Enum):
    """Categorization of explanation evaluation scenarios."""

    SCALAR_FORMATTING = "scalar_formatting"
    EMPTY_RESULT = "empty_result"
    HALLUCINATION_DETECTION = "hallucination_detection"
    NARRATIVE_CLASSIFICATION = "narrative_classification"
    PRIVACY_BOUNDARY = "privacy_boundary"
    SQL_LITERAL_MASKING = "sql_literal_masking"
    TRUNCATION_DISCLOSURE = "truncation_disclosure"
    FAILURE_FALLBACK = "failure_fallback"


class SystemBoundaryType(str, Enum):
    """Explicit production component or contract boundary under test."""

    PRODUCTION_SCALAR_TEMPLATE = "production_scalar_template"
    PRODUCTION_SYNTHESIZER_SERVICE = "production_synthesizer_service"
    PRODUCTION_HALLUCINATION_HEURISTIC = "production_hallucination_heuristic"
    PRODUCTION_METADATA_SYNTHESIS = "production_metadata_synthesis"
    PRODUCTION_SQL_LITERAL_MASKING = "production_sql_literal_masking"


@dataclass
class ExplanationScenarioDefinition:
    """Specification of an offline explanation evaluation scenario."""

    scenario_id: str
    name: str
    category: ExplanationCategory
    system_boundary: SystemBoundaryType
    description: str
    production_functions: List[str]
    evaluator: Callable[[], Tuple[bool, List[str], Dict[str, Any], Optional[str]]]


class ExplanationScenarioResult(BaseModel):
    """Recorded result for an individual scenario."""

    scenario_id: str
    name: str
    category: str
    system_boundary: str
    passed: bool
    production_functions: List[str] = Field(default_factory=list)
    assertion_details: List[str] = Field(default_factory=list)
    evidence_data: Dict[str, Any] = Field(default_factory=dict)
    failure_reason: Optional[str] = None
    limitation_note: Optional[str] = None


class ExplanationEvaluationMetrics(BaseModel):
    """Aggregate metrics computed across the evaluation suite."""

    total_scenarios: int
    passed_scenarios: int
    failed_scenarios: int
    pass_rate: float
    scalar_templates_verified: int
    hallucination_checks_verified: int
    privacy_boundaries_verified: int
    failure_fallbacks_verified: int
    category_counts: Dict[str, int] = Field(default_factory=dict)
    boundary_counts: Dict[str, int] = Field(default_factory=dict)


class ExplanationEvaluationArtifact(BaseModel):
    """Complete machine-readable evaluation report payload."""

    metadata: Dict[str, Any]
    metrics: ExplanationEvaluationMetrics
    scenario_results: List[ExplanationScenarioResult]


# ==============================================================================
# 2. Mock Helpers for Offline Synthesis Testing
# ==============================================================================

class MockGeminiResponse:
    """Deterministic mock for google.genai response."""

    def __init__(self, text: str = ""):
        self.text = text


class MockGeminiModels:
    """Mock models interface supporting generate_content."""

    def __init__(self, text: str = "", side_effect: Optional[Exception] = None):
        self._text = text
        self._side_effect = side_effect

    def generate_content(self, *args: Any, **kwargs: Any) -> MockGeminiResponse:
        if self._side_effect:
            raise self._side_effect
        return MockGeminiResponse(text=self._text)


class MockGeminiClient:
    """Offline mock client injecting controlled Gemini behavior."""

    def __init__(self, text: str = "", side_effect: Optional[Exception] = None):
        self.models = MockGeminiModels(text=text, side_effect=side_effect)


# ==============================================================================
# 3. Scenario Evaluators (EXP-01 to EXP-12)
# ==============================================================================

def _eval_exp_01() -> Tuple[bool, List[str], Dict[str, Any], Optional[str]]:
    """EXP-01: Unambiguous Count Scalar Template Formatting."""
    assertions: List[str] = []
    evidence: Dict[str, Any] = {}

    # 1. Direct function call
    formatted = format_scalar_template("StudentCount", 42)
    assert formatted == "Count: 42"
    assertions.append(f"format_scalar_template('StudentCount', 42) == 'Count: 42' [got '{formatted}']")

    # 2. End-to-end synthesizer service Tier 1
    qr = QueryResult(columns=["TotalCount"], rows=[{"TotalCount": 108}], row_count=1, truncated=False)
    service = AnswerSynthesisService(enabled=False)  # Tier 1 requires zero LLM calls
    ans = service.synthesize(question="How many students?", query_result=qr)

    assert ans.text == "Count: 108"
    assert ans.status == GroundedAnswerStatus.TEMPLATE_DETERMINISTIC
    assert ans.is_mechanically_verified is True
    assert ans.verification_notes == []
    assertions.append("AnswerSynthesisService produced TEMPLATE_DETERMINISTIC with is_mechanically_verified=True")

    evidence["formatted"] = formatted
    evidence["answer_text"] = ans.text
    evidence["status"] = ans.status.value
    evidence["mechanically_verified"] = ans.is_mechanically_verified

    limitation = "Token-based template heuristic formats unambiguous aggregate column names; does not inspect query AST."
    return True, assertions, evidence, limitation


def _eval_exp_02() -> Tuple[bool, List[str], Dict[str, Any], Optional[str]]:
    """EXP-02: Unambiguous Average, Min, and Max Scalar Templates."""
    assertions: List[str] = []
    evidence: Dict[str, Any] = {}

    res_avg = format_scalar_template("avg_score", 87.5)
    res_min = format_scalar_template("min_marks", 35)
    res_max = format_scalar_template("highest_gpa", 3.95)

    assert res_avg == "Average: 87.5"
    assert res_min == "Minimum: 35"
    assert res_max == "Maximum: 3.95"

    assertions.append("Verified unambiguous average prefix ('Average: 87.5')")
    assertions.append("Verified unambiguous minimum prefix ('Minimum: 35')")
    assertions.append("Verified unambiguous maximum prefix ('Maximum: 3.95')")

    evidence["avg"] = res_avg
    evidence["min"] = res_min
    evidence["max"] = res_max

    limitation = "Recognizes tokens 'avg', 'average', 'mean', 'min', 'lowest', 'max', 'highest'; relies on non-conflicting naming."
    return True, assertions, evidence, limitation


def _eval_exp_03() -> Tuple[bool, List[str], Dict[str, Any], Optional[str]]:
    """EXP-03: Neutral Formatting for Ambiguous / Non-Count Scalar Columns."""
    assertions: List[str] = []
    evidence: Dict[str, Any] = {}

    # Competing non-count term (revenue) prevents false 'Count:' classification
    res_rev = format_scalar_template("total_revenue", 125000)
    assert res_rev == "total_revenue: 125000"
    assertions.append("Column 'total_revenue' formatted neutrally without assuming Count")

    # Percentage / rate column
    res_pct = format_scalar_template("discount_pct", 15)
    assert res_pct == "discount_pct: 15"
    assertions.append("Column 'discount_pct' formatted neutrally")

    # Conflicting aggregate tokens (min AND max)
    res_spread = format_scalar_template("min_max_spread", 10)
    assert res_spread == "min_max_spread: 10"
    assertions.append("Conflicting aggregate column 'min_max_spread' fell back neutrally")

    # Synthesizer integration check
    qr = QueryResult(columns=["discount_pct"], rows=[{"discount_pct": 15}], row_count=1, truncated=False)
    service = AnswerSynthesisService(enabled=False)
    ans = service.synthesize(question="What is the discount?", query_result=qr)
    assert ans.text == "discount_pct: 15"
    assert ans.status == GroundedAnswerStatus.TEMPLATE_DETERMINISTIC
    assert len(ans.verification_notes) == 1
    assert "does not match standard aggregate naming" in ans.verification_notes[0]
    assertions.append("Synthesizer appended neutral-formatting disclosure note")

    evidence["revenue"] = res_rev
    evidence["percentage"] = res_pct
    evidence["competing"] = res_spread
    evidence["notes"] = ans.verification_notes

    limitation = "Does not guess semantic meaning when column names contain ambiguous or non-count keywords."
    return True, assertions, evidence, limitation


def _eval_exp_04() -> Tuple[bool, List[str], Dict[str, Any], Optional[str]]:
    """EXP-04: Deterministic Empty Result Set Handling."""
    assertions: List[str] = []
    evidence: Dict[str, Any] = {}

    qr = QueryResult(columns=["StudentId", "FullName"], rows=[], row_count=0, truncated=False)
    service = AnswerSynthesisService(enabled=True)  # LLM enabled, but must NOT be invoked
    ans = service.synthesize(question="Find students with 100% attendance", query_result=qr)

    assert ans.text == "Query executed successfully. No rows returned."
    assert ans.status == GroundedAnswerStatus.EMPTY_RESULT
    assert ans.is_mechanically_verified is True
    assert ans.verification_notes == []
    assertions.append("synthesize() returns EMPTY_RESULT with is_mechanically_verified=True on 0 rows")

    # Also test metadata-only empty result path
    summary, status, note = service.synthesize_from_metadata(
        question="Find students",
        candidate_sql="SELECT * FROM Students WHERE 1=0",
        columns=["StudentId"],
        row_count=0,
    )
    assert status == AnswerGroundingStatus.GROUNDED_FROM_METADATA
    assert summary == "Query executed successfully. No rows returned."
    assert note == "Determined from metadata: zero rows returned."
    assertions.append("synthesize_from_metadata() grounds 0-row outcome directly from metadata")

    evidence["status"] = ans.status.value
    evidence["mechanically_verified"] = ans.is_mechanically_verified
    evidence["metadata_status"] = status.value

    limitation = "Verifies empty-result deterministic short-circuit; assumes upstream execution correctly reported 0 rows."
    return True, assertions, evidence, limitation


def _eval_exp_05() -> Tuple[bool, List[str], Dict[str, Any], Optional[str]]:
    """EXP-05: Heuristic Detection of Fabricated Currency Claims."""
    assertions: List[str] = []
    evidence: Dict[str, Any] = {}

    question = "How many students are enrolled?"
    candidate_sql = "SELECT COUNT(*) FROM Students"
    columns = ["StudentCount"]
    row_count = 1
    exec_time = 4.2

    # Hallucinated text with dollar currency symbol not present in metadata
    text_dollar = "There are 150 students with an average fee of $500."
    detected_dollar = _detect_hallucinated_values(
        text=text_dollar,
        question=question,
        candidate_sql=candidate_sql,
        columns=columns,
        row_count=row_count,
        execution_time_ms=exec_time,
    )
    assert detected_dollar is True
    assertions.append("Detected fabricated dollar symbol '$' in text")

    # Other currency symbols: euro, pound, rupee, yen
    for sym in ("€", "£", "₹", "¥"):
        t = f"Total amount is {sym}1000"
        det = _detect_hallucinated_values(t, question, candidate_sql, columns, row_count, exec_time)
        assert det is True, f"Failed to detect currency symbol: {sym}"
    assertions.append("Detected euro (€), pound (£), rupee (₹), and yen (¥) symbols")

    # End-to-end synthesize_from_metadata downgrade
    mock_client = MockGeminiClient(text="The department collected $5,000 in student dues.")
    service = AnswerSynthesisService(gemini_client=mock_client)
    summary, status, note = service.synthesize_from_metadata(
        question="Show department summary",
        candidate_sql="SELECT DepartmentId FROM Departments",
        columns=["DepartmentId"],
        row_count=5,
    )
    assert status == AnswerGroundingStatus.REQUIRES_ROW_DISCLOSURE
    assert "cannot be displayed in this narrative summary" in summary
    assertions.append("synthesize_from_metadata safely downgraded currency hallucination to REQUIRES_ROW_DISCLOSURE")

    evidence["detected_dollar"] = detected_dollar
    evidence["downgraded_status"] = status.value

    limitation = "Heuristic regex check flags currency symbols; cannot detect non-currency financial claims (e.g. '500 dollars')."
    return True, assertions, evidence, limitation


def _eval_exp_06() -> Tuple[bool, List[str], Dict[str, Any], Optional[str]]:
    """EXP-06: Heuristic Detection of Unsupported Numeric Constants."""
    assertions: List[str] = []
    evidence: Dict[str, Any] = {}

    question = "Count students in department 3"
    candidate_sql = "SELECT COUNT(*) FROM Students WHERE DepartmentId = 3"
    columns = ["cnt"]
    row_count = 1
    exec_time = 12.4

    # Text containing ungrounded number '99.5'
    text_bad = "Found 1 row. Average score was 99.5 for this cohort."
    detected_bad = _detect_hallucinated_values(
        text=text_bad,
        question=question,
        candidate_sql=candidate_sql,
        columns=columns,
        row_count=row_count,
        execution_time_ms=exec_time,
    )
    assert detected_bad is True
    assertions.append("Detected ungrounded numeric constant '99.5' outside allowed metadata bounds")

    # Text containing ONLY allowed numbers (row_count=1, department 3 from SQL, 12.4 from latency)
    text_good = "The query executed in 12.4 ms and returned 1 row for department 3."
    detected_good = _detect_hallucinated_values(
        text=text_good,
        question=question,
        candidate_sql=candidate_sql,
        columns=columns,
        row_count=row_count,
        execution_time_ms=exec_time,
    )
    assert detected_good is False
    assertions.append("Permitted grounded numbers matching metadata, SQL, and latency without false alarm")

    evidence["detected_unsupported"] = detected_bad
    evidence["permitted_grounded"] = not detected_good

    limitation = "Bounds check checks whether numbers exist in {row_count, column count, latency, question, SQL}; does not verify semantic truth."
    return True, assertions, evidence, limitation


def _eval_exp_07() -> Tuple[bool, List[str], Dict[str, Any], Optional[str]]:
    """EXP-07: Faithful Multi-Row Narrative Classification as Unverified."""
    assertions: List[str] = []
    evidence: Dict[str, Any] = {}

    qr = QueryResult(
        columns=["DepartmentId", "Name"],
        rows=[
            {"DepartmentId": 1, "Name": "Computer Engineering"},
            {"DepartmentId": 2, "Name": "Mechanical Engineering"},
        ],
        row_count=2,
        truncated=False,
    )

    mock_client = MockGeminiClient(
        text="There are two academic departments: Computer Engineering and Mechanical Engineering."
    )
    service = AnswerSynthesisService(gemini_client=mock_client)
    ans = service.synthesize(question="List departments", query_result=qr)

    assert ans.status == GroundedAnswerStatus.UNVERIFIED_NARRATIVE
    assert ans.is_mechanically_verified is False
    assert ans.narrative == "There are two academic departments: Computer Engineering and Mechanical Engineering."
    assertions.append("Multi-row LLM output classified strictly as UNVERIFIED_NARRATIVE with is_mechanically_verified=False")

    evidence["status"] = ans.status.value
    evidence["mechanically_verified"] = ans.is_mechanically_verified
    evidence["narrative_length"] = len(ans.text)

    limitation = "Enforces the policy that LLM prose is never marked mechanically verified; does not verify semantic completeness."
    return True, assertions, evidence, limitation


def _eval_exp_08() -> Tuple[bool, List[str], Dict[str, Any], Optional[str]]:
    """EXP-08: Entity Query Pre-screening for Required Row Disclosure."""
    assertions: List[str] = []
    evidence: Dict[str, Any] = {}

    entity_questions = [
        "Who is the top student in semester 1?",
        "Which department has the lowest average marks?",
        "What is the email address of Alice?",
        "List all students with attendance below 75%",
        "Show the highest salary among faculty members",
    ]

    for q in entity_questions:
        req = _question_requires_row_disclosure(q)
        assert req is True, f"Failed to identify row-disclosure requirement for: {q}"
    assertions.append(f"Correctly flagged all {len(entity_questions)} entity queries as requiring row disclosure")

    # Synthesizer pre-screening test
    service = AnswerSynthesisService(enabled=True)
    summary, status, note = service.synthesize_from_metadata(
        question="Who scored highest?",
        candidate_sql="SELECT FullName FROM Students ORDER BY Score DESC",
        columns=["FullName"],
        row_count=1,
    )
    assert status == AnswerGroundingStatus.REQUIRES_ROW_DISCLOSURE
    assert "cannot be displayed in this narrative summary" in summary
    assert "zero-disclosure privacy policy" in note
    assertions.append("synthesize_from_metadata safely refused cloud synthesis under zero-disclosure policy")

    evidence["questions_tested"] = len(entity_questions)
    evidence["status"] = status.value

    limitation = "Pattern-based heuristic looks for question phrasing (who, which, what is, list); may not catch novel paraphrases."
    return True, assertions, evidence, limitation


def _eval_exp_09() -> Tuple[bool, List[str], Dict[str, Any], Optional[str]]:
    """EXP-09: Metadata-Answerable Query Classification."""
    assertions: List[str] = []
    evidence: Dict[str, Any] = {}

    question = "How many records were processed?"
    assert _question_requires_row_disclosure(question) is False
    assertions.append("Non-entity question passed pre-screening without requiring row disclosure")

    mock_client = MockGeminiClient(text="The query processed 15 rows in 8.5 ms.")
    service = AnswerSynthesisService(gemini_client=mock_client)
    summary, status, note = service.synthesize_from_metadata(
        question=question,
        candidate_sql="SELECT * FROM Departments",
        columns=["DepartmentId", "Name"],
        row_count=15,
        execution_time_ms=8.5,
    )
    assert status == AnswerGroundingStatus.GROUNDED_FROM_METADATA
    assert summary == "The query processed 15 rows in 8.5 ms."
    assertions.append("Produced GROUNDED_FROM_METADATA response for metadata-only query")

    evidence["status"] = status.value
    evidence["summary"] = summary

    limitation = "Demonstrates that metadata-only queries can proceed when no cell disclosure is required."
    return True, assertions, evidence, limitation


def _eval_exp_10() -> Tuple[bool, List[str], Dict[str, Any], Optional[str]]:
    """EXP-10: AST SQL Literal Masking & Fail-Closed Protection."""
    assertions: List[str] = []
    evidence: Dict[str, Any] = {}

    sql = "SELECT * FROM Students WHERE Email = 'student@example.com' AND Age >= 21 AND Score > 85.5"
    masked = mask_sql_literals(sql, dialect="tsql")

    assert masked is not None
    assert "student@example.com" not in masked
    assert "21" not in masked
    assert "85.5" not in masked
    assert "[REDACTED_VALUE]" in masked
    assert "[REDACTED_NUMBER]" in masked
    assert "Students" in masked
    assert "Email" in masked
    assertions.append("AST parser masked string and numeric literals while preserving tables, columns, and operators")

    # Fail-closed behavior on unparseable SQL
    malformed_sql = "SELECT * FROM %%% INVALID SYNTAX %%%"
    masked_malformed = mask_sql_literals(malformed_sql, dialect="tsql")
    assert masked_malformed is None
    assertions.append("mask_sql_literals returned None on unparseable SQL AST (fail-closed)")

    evidence["masked_sql"] = masked
    evidence["fail_closed_result"] = masked_malformed

    limitation = "Relies on sqlglot AST parsing; dialect-specific syntax errors fall back closed to None."
    return True, assertions, evidence, limitation


def _eval_exp_11() -> Tuple[bool, List[str], Dict[str, Any], Optional[str]]:
    """EXP-11: Truncation Disclosures on Sampled Multi-Row Results."""
    assertions: List[str] = []
    evidence: Dict[str, Any] = {}

    qr = QueryResult(
        columns=["Id", "Title"],
        rows=[{"Id": 1, "Title": "Course A"}],
        row_count=100,
        truncated=True,
    )

    mock_client = MockGeminiClient(text="Retrieved courses.")
    service = AnswerSynthesisService(gemini_client=mock_client)
    ans = service.synthesize(question="List all courses", query_result=qr)

    assert any("truncated at the system limit" in note for note in ans.verification_notes)
    assertions.append("synthesize() attached truncation disclosure note to GroundedAnswer")

    # Metadata synthesis truncation note
    summary, status, note = service.synthesize_from_metadata(
        question="How many courses were found?",
        candidate_sql="SELECT * FROM Courses",
        columns=["CourseId", "Title"],
        row_count=100,
        truncated=True,
    )
    assert note is not None
    assert "Results were truncated at the system limit" in note
    assertions.append("synthesize_from_metadata attached truncation caveat note")

    evidence["notes"] = ans.verification_notes
    evidence["metadata_note"] = note

    limitation = "Verifies that truncation metadata is attached; does not evaluate whether prose itself discusses truncation."
    return True, assertions, evidence, limitation


def _eval_exp_12() -> Tuple[bool, List[str], Dict[str, Any], Optional[str]]:
    """EXP-12: Provider Failure and Timeout Safe Fallback."""
    assertions: List[str] = []
    evidence: Dict[str, Any] = {}

    qr = QueryResult(
        columns=["StudentId", "FullName"],
        rows=[{"StudentId": 1, "FullName": "Aarav Patel"}],
        row_count=2,  # Multi-row to trigger LLM synthesis
        truncated=False,
    )

    # Simulate timeout exception from Gemini provider
    mock_client = MockGeminiClient(side_effect=TimeoutError("Request timed out after 3000ms"))
    service = AnswerSynthesisService(gemini_client=mock_client)
    ans = service.synthesize(question="Show students", query_result=qr)

    assert ans.status == GroundedAnswerStatus.SYNTHESIS_FAILED
    assert ans.text == "Query executed successfully. Refer to the data table for results."
    assert ans.is_mechanically_verified is False
    assert ans.verification_notes == ["Answer synthesis provider call failed."]
    assertions.append("synthesize() caught TimeoutError and safely fell back to SYNTHESIS_FAILED")

    # Metadata synthesis failure fallback
    summary, status, note = service.synthesize_from_metadata(
        question="Summarize rows",
        candidate_sql="SELECT * FROM Students",
        columns=["StudentId"],
        row_count=5,
    )
    assert status == AnswerGroundingStatus.UNAVAILABLE
    assert note == "Answer synthesis provider call failed."
    assertions.append("synthesize_from_metadata caught provider error and returned UNAVAILABLE")

    evidence["status"] = ans.status.value
    evidence["fallback_text"] = ans.text
    evidence["metadata_status"] = status.value

    limitation = "Tests safe error handling and graceful degradation; does not retry transient provider failures."
    return True, assertions, evidence, limitation


# ==============================================================================
# 4. Scenario Catalog Builder
# ==============================================================================

def build_explanation_scenarios() -> List[ExplanationScenarioDefinition]:
    """Assemble the 12 canonical explanation evaluation scenarios."""
    return [
        ExplanationScenarioDefinition(
            scenario_id="EXP-01",
            name="Unambiguous Count Scalar Template",
            category=ExplanationCategory.SCALAR_FORMATTING,
            system_boundary=SystemBoundaryType.PRODUCTION_SCALAR_TEMPLATE,
            description="Verifies format_scalar_template produces 'Count: <val>' for clean count projections.",
            production_functions=["format_scalar_template", "AnswerSynthesisService.synthesize"],
            evaluator=_eval_exp_01,
        ),
        ExplanationScenarioDefinition(
            scenario_id="EXP-02",
            name="Unambiguous Average, Min, and Max Templates",
            category=ExplanationCategory.SCALAR_FORMATTING,
            system_boundary=SystemBoundaryType.PRODUCTION_SCALAR_TEMPLATE,
            description="Verifies standard aggregate tokens (avg, min, max) map to exact aggregate prefixes.",
            production_functions=["format_scalar_template"],
            evaluator=_eval_exp_02,
        ),
        ExplanationScenarioDefinition(
            scenario_id="EXP-03",
            name="Neutral Formatting for Ambiguous / Non-Count Columns",
            category=ExplanationCategory.SCALAR_FORMATTING,
            system_boundary=SystemBoundaryType.PRODUCTION_SCALAR_TEMPLATE,
            description="Verifies non-count terms (revenue, rate) and conflicting aggregates fall back to '{col}: {val}'.",
            production_functions=["format_scalar_template", "AnswerSynthesisService.synthesize"],
            evaluator=_eval_exp_03,
        ),
        ExplanationScenarioDefinition(
            scenario_id="EXP-04",
            name="Deterministic Empty Result Set Handling",
            category=ExplanationCategory.EMPTY_RESULT,
            system_boundary=SystemBoundaryType.PRODUCTION_SYNTHESIZER_SERVICE,
            description="Verifies 0-row results produce EMPTY_RESULT with is_mechanically_verified=True without LLM calls.",
            production_functions=["AnswerSynthesisService.synthesize", "AnswerSynthesisService.synthesize_from_metadata"],
            evaluator=_eval_exp_04,
        ),
        ExplanationScenarioDefinition(
            scenario_id="EXP-05",
            name="Heuristic Detection of Fabricated Currency Claims",
            category=ExplanationCategory.HALLUCINATION_DETECTION,
            system_boundary=SystemBoundaryType.PRODUCTION_HALLUCINATION_HEURISTIC,
            description="Verifies _detect_hallucinated_values flags unsupported currency symbols ($, €, £, ₹, ¥).",
            production_functions=["_detect_hallucinated_values", "AnswerSynthesisService.synthesize_from_metadata"],
            evaluator=_eval_exp_05,
        ),
        ExplanationScenarioDefinition(
            scenario_id="EXP-06",
            name="Heuristic Detection of Unsupported Numeric Constants",
            category=ExplanationCategory.HALLUCINATION_DETECTION,
            system_boundary=SystemBoundaryType.PRODUCTION_HALLUCINATION_HEURISTIC,
            description="Verifies _detect_hallucinated_values flags numbers not present in metadata, SQL, or latency.",
            production_functions=["_detect_hallucinated_values"],
            evaluator=_eval_exp_06,
        ),
        ExplanationScenarioDefinition(
            scenario_id="EXP-07",
            name="Faithful Multi-Row Narrative Classification as Unverified",
            category=ExplanationCategory.NARRATIVE_CLASSIFICATION,
            system_boundary=SystemBoundaryType.PRODUCTION_SYNTHESIZER_SERVICE,
            description="Verifies multi-row LLM summaries are strictly tagged UNVERIFIED_NARRATIVE with is_mechanically_verified=False.",
            production_functions=["AnswerSynthesisService.synthesize"],
            evaluator=_eval_exp_07,
        ),
        ExplanationScenarioDefinition(
            scenario_id="EXP-08",
            name="Entity Query Pre-screening for Required Row Disclosure",
            category=ExplanationCategory.PRIVACY_BOUNDARY,
            system_boundary=SystemBoundaryType.PRODUCTION_METADATA_SYNTHESIS,
            description="Verifies entity questions (who, which, list) are safely identified as REQUIRES_ROW_DISCLOSURE.",
            production_functions=["_question_requires_row_disclosure", "AnswerSynthesisService.synthesize_from_metadata"],
            evaluator=_eval_exp_08,
        ),
        ExplanationScenarioDefinition(
            scenario_id="EXP-09",
            name="Metadata-Answerable Query Classification",
            category=ExplanationCategory.PRIVACY_BOUNDARY,
            system_boundary=SystemBoundaryType.PRODUCTION_METADATA_SYNTHESIS,
            description="Verifies non-entity questions proceed to GROUNDED_FROM_METADATA without disclosing cell records.",
            production_functions=["_question_requires_row_disclosure", "AnswerSynthesisService.synthesize_from_metadata"],
            evaluator=_eval_exp_09,
        ),
        ExplanationScenarioDefinition(
            scenario_id="EXP-10",
            name="AST SQL Literal Masking & Fail-Closed Protection",
            category=ExplanationCategory.SQL_LITERAL_MASKING,
            system_boundary=SystemBoundaryType.PRODUCTION_SQL_LITERAL_MASKING,
            description="Verifies mask_sql_literals replaces constants with generic placeholders and fails closed on syntax errors.",
            production_functions=["mask_sql_literals", "AnswerSynthesisService.synthesize_from_metadata"],
            evaluator=_eval_exp_10,
        ),
        ExplanationScenarioDefinition(
            scenario_id="EXP-11",
            name="Truncation Disclosures on Sampled Results",
            category=ExplanationCategory.TRUNCATION_DISCLOSURE,
            system_boundary=SystemBoundaryType.PRODUCTION_SYNTHESIZER_SERVICE,
            description="Verifies truncated result sets attach explicit truncation caveats in verification notes.",
            production_functions=["AnswerSynthesisService.synthesize", "AnswerSynthesisService.synthesize_from_metadata"],
            evaluator=_eval_exp_11,
        ),
        ExplanationScenarioDefinition(
            scenario_id="EXP-12",
            name="Provider Failure and Timeout Safe Fallback",
            category=ExplanationCategory.FAILURE_FALLBACK,
            system_boundary=SystemBoundaryType.PRODUCTION_SYNTHESIZER_SERVICE,
            description="Verifies provider exceptions (e.g. TimeoutError) gracefully fall back to SYNTHESIS_FAILED.",
            production_functions=["AnswerSynthesisService.synthesize", "AnswerSynthesisService.synthesize_from_metadata"],
            evaluator=_eval_exp_12,
        ),
    ]


# ==============================================================================
# 5. Execution & Metric Computation
# ==============================================================================

def execute_explanation_scenario(scenario: ExplanationScenarioDefinition) -> ExplanationScenarioResult:
    """Execute an individual explanation scenario and capture detailed evidence."""
    try:
        passed, assertions, evidence, limitation = scenario.evaluator()
        return ExplanationScenarioResult(
            scenario_id=scenario.scenario_id,
            name=scenario.name,
            category=scenario.category.value,
            system_boundary=scenario.system_boundary.value,
            passed=passed,
            production_functions=scenario.production_functions,
            assertion_details=assertions,
            evidence_data=evidence,
            failure_reason=None if passed else "Evaluator returned failed status",
            limitation_note=limitation,
        )
    except Exception as exc:
        logger.exception("Scenario %s encountered unexpected error", scenario.scenario_id)
        return ExplanationScenarioResult(
            scenario_id=scenario.scenario_id,
            name=scenario.name,
            category=scenario.category.value,
            system_boundary=scenario.system_boundary.value,
            passed=False,
            production_functions=scenario.production_functions,
            assertion_details=[],
            evidence_data={},
            failure_reason=f"Unhandled exception during evaluation: {type(exc).__name__}: {exc}",
            limitation_note=None,
        )


def compute_explanation_metrics(results: List[ExplanationScenarioResult]) -> ExplanationEvaluationMetrics:
    """Derive dynamic metrics across executed explanation scenarios."""
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    failed = total - passed
    pass_rate = (passed / total) if total > 0 else 0.0

    category_counts: Dict[str, int] = {}
    boundary_counts: Dict[str, int] = {}

    scalar_verified = 0
    hallucinations_detected = 0
    privacy_verified = 0
    fallbacks_verified = 0

    for r in results:
        category_counts[r.category] = category_counts.get(r.category, 0) + (1 if r.passed else 0)
        boundary_counts[r.system_boundary] = boundary_counts.get(r.system_boundary, 0) + (1 if r.passed else 0)

        if r.passed:
            if r.category == ExplanationCategory.SCALAR_FORMATTING.value:
                scalar_verified += 1
            elif r.category == ExplanationCategory.HALLUCINATION_DETECTION.value:
                hallucinations_detected += 1
            elif r.category in (ExplanationCategory.PRIVACY_BOUNDARY.value, ExplanationCategory.EMPTY_RESULT.value):
                privacy_verified += 1
            elif r.category in (ExplanationCategory.FAILURE_FALLBACK.value, ExplanationCategory.TRUNCATION_DISCLOSURE.value):
                fallbacks_verified += 1

    return ExplanationEvaluationMetrics(
        total_scenarios=total,
        passed_scenarios=passed,
        failed_scenarios=failed,
        pass_rate=pass_rate,
        scalar_templates_verified=scalar_verified,
        hallucination_checks_verified=hallucinations_detected,
        privacy_boundaries_verified=privacy_verified,
        failure_fallbacks_verified=fallbacks_verified,
        category_counts=category_counts,
        boundary_counts=boundary_counts,
    )


def run_explanation_evaluation(
    scenarios: Optional[List[ExplanationScenarioDefinition]] = None,
    git_commit: str = "unknown",
) -> ExplanationEvaluationArtifact:
    """Execute the full offline explanation evaluation suite."""
    if scenarios is None:
        scenarios = build_explanation_scenarios()

    results = [execute_explanation_scenario(s) for s in scenarios]
    metrics = compute_explanation_metrics(results)

    metadata = {
        "benchmark": "QueryPilot Deterministic Explanation Evaluation",
        "task_version": "B13-EVAL2",
        "git_commit": git_commit,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "environment": "100% Offline (Mocked Gemini & Zero Database Calls)",
        "total_scenarios": len(scenarios),
        "target_module": "src/query_pilot/presentation/synthesizer.py",
    }

    return ExplanationEvaluationArtifact(
        metadata=metadata,
        metrics=metrics,
        scenario_results=results,
    )


# ==============================================================================
# 6. Report Generation & Serialization
# ==============================================================================

def generate_explanation_markdown_report(artifact: ExplanationEvaluationArtifact) -> str:
    """Format explanation evaluation artifact as human-readable Markdown."""
    meta = artifact.metadata
    metrics = artifact.metrics
    results = artifact.scenario_results

    lines: List[str] = [
        "# QueryPilot B13-EVAL2 — Deterministic Result Explanation & Faithfulness Benchmark Report",
        "",
        f"- **Benchmark Run:** Result Presentation & Answer Synthesis Offline Evaluation",
        f"- **Environment:** {meta.get('environment', 'Offline')}",
        f"- **Git Commit:** `{meta.get('git_commit', 'unknown')}`",
        f"- **Target Module:** `{meta.get('target_module', 'src/query_pilot/presentation/synthesizer.py')}`",
        f"- **Timestamp (UTC):** `{meta.get('timestamp_utc', 'unknown')}`",
        "",
        "## 1. Executive Summary",
        "",
        f"- **Total Scenarios Evaluated:** {metrics.total_scenarios}",
        f"- **Scenarios Passed:** {metrics.passed_scenarios} ({metrics.pass_rate:.1%})",
        f"- **Scenarios Failed:** {metrics.failed_scenarios}",
        f"- **Scalar Template Formatting Checks:** {metrics.scalar_templates_verified}",
        f"- **Hallucination Detection Checks:** {metrics.hallucination_checks_verified}",
        f"- **Privacy & Zero-Disclosure Boundaries:** {metrics.privacy_boundaries_verified}",
        f"- **Truncation & Failure Fallbacks:** {metrics.failure_fallbacks_verified}",
        "",
        "## 2. Category Summary",
        "",
        "| Category | Scenarios | Passed | Status |",
        "|---|:---:|:---:|:---:|",
    ]

    # Category grouping
    from collections import defaultdict
    by_cat = defaultdict(list)
    for r in results:
        by_cat[r.category].append(r)

    for cat_name, cat_results in sorted(by_cat.items()):
        cat_total = len(cat_results)
        cat_passed = sum(1 for r in cat_results if r.passed)
        cat_status = "PASS" if cat_passed == cat_total else "FAIL"
        lines.append(f"| `{cat_name}` | {cat_total} | {cat_passed}/{cat_total} | **{cat_status}** |")

    lines.extend([
        "",
        "## 3. Scenario Results & Assertions",
        "",
        "| ID | Status | Category | System Boundary | Functions Exercised |",
        "|---|:---:|---|---|---|",
    ])

    for r in results:
        status_badge = "PASS" if r.passed else "FAIL"
        funcs = ", ".join(f"`{f}`" for f in r.production_functions)
        lines.append(f"| `{r.scenario_id}` | **{status_badge}** | `{r.category}` | `{r.system_boundary}` | {funcs} |")

    lines.extend([
        "",
        "### Detailed Scenario Verification Details",
        "",
    ])

    for r in results:
        badge = "PASS" if r.passed else "FAIL"
        lines.append(f"#### `{r.scenario_id}`: {r.name} — **{badge}**")
        lines.append(f"- **Category:** `{r.category}`")
        lines.append(f"- **Boundary:** `{r.system_boundary}`")
        lines.append(f"- **Functions Exercised:** {', '.join(f'`{f}`' for f in r.production_functions)}")
        lines.append("- **Assertions Verified:**")
        for a in r.assertion_details:
            lines.append(f"  - {a}")
        if r.limitation_note:
            lines.append(f"- **Heuristic Boundary & Limitation:** {r.limitation_note}")
        if r.failure_reason:
            lines.append(f"- **Failure Reason:** `{r.failure_reason}`")
        lines.append("")

    lines.extend([
        "## 4. Heuristic Boundaries & Semantic Limitations Disclosure",
        "",
        "> **CRITICAL METHODOLOGICAL DISTINCTION:**",
        "> This benchmark deterministically verifies the runtime guardrails, classification logic, and heuristic checks",
        "> implemented in `src/query_pilot/presentation/synthesizer.py`. It specifically establishes:",
        "> 1. **Scalar Formatting vs. Neutral Fallback**: Verified that column token heuristics prevent count over-generalization.",
        "> 2. **Heuristic Hallucination Screening**: Verified that `_detect_hallucinated_values` flags currency symbols and numbers outside metadata bounds.",
        "> 3. **Unverified Narrative Policy**: Verified that multi-row LLM prose is classified as `UNVERIFIED_NARRATIVE` with `is_mechanically_verified=False`.",
        "> 4. **Privacy Boundaries**: Verified that entity queries trigger `REQUIRES_ROW_DISCLOSURE` and that SQL literals are masked.",
        "> 5. **Safe Failure Degradation**: Verified that provider errors and timeouts fall back cleanly without unhandled exceptions.",
        ">",
        "> **WHAT THIS BENCHMARK DOES NOT ESTABLISH:**",
        "> - It does NOT claim to provide an exhaustive semantic fact-checker. Non-numeric or qualitative hallucinations (e.g. inventing dates or false business logic)",
        ">   cannot be detected by regex heuristics.",
        "> - A passing status on `EXP-07` confirms that output is classified as `UNVERIFIED_NARRATIVE`; it does NOT prove the narrative is semantically faithful to the sample rows.",
        "> - Offline evaluation with mocked clients does not evaluate live Gemini model latency or cloud generation quality.",
        "",
    ])

    return "\n".join(lines)


def save_explanation_evaluation(
    artifact: ExplanationEvaluationArtifact,
    output_dir: Path,
    base_name: str = "explanation_evaluation",
) -> Tuple[Path, Path]:
    """Persist machine-readable JSON and human-readable Markdown artifacts."""
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{base_name}.json"
    md_path = output_dir / f"{base_name}_report.md"

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(artifact.model_dump(), f, indent=2)

    report_content = generate_explanation_markdown_report(artifact)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(report_content)

    return json_path, md_path
