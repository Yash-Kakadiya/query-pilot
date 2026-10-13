"""Regression and contract unit tests for clarification and multi-turn evaluation support (Task 1.30-B).

Verifies:
1. Deterministic classification of expected clarification (correctly_clarified).
2. Deterministic classification of unexpected clarification on answerable cases (incorrectly_clarified).
3. Clarification responses with null SQL bypass AST validation and execution.
4. Answerable SQL returned for an expected clarification case is classified as a mismatch.
5. Structured unsupported refusal returned for an expected clarification case is classified as a mismatch.
6. Optional bounded conversation_context in evaluation cases reaches SQLGenerationRequest and is recorded.
7. Category breakdown and aggregate EvaluationMetrics compute clarification metrics accurately.
8. Report generator and failure analysis surface clarification outcomes honestly.
9. PairedCaseComparison in comparative evaluation correctly scores clarification cases.
10. Preservation of established answerable, unsupported, semantic-trap, and safety evaluation contracts.
"""

from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch
import pytest

from query_pilot.db.models import (
    ColumnMetadata,
    DatabaseSchema,
    TableMetadata,
)
from query_pilot.eval.comparison import (
    PairedCaseClassification,
    classify_paired_case,
    is_case_passing,
)
from query_pilot.eval.evaluator import (
    compute_evaluation_metrics,
    evaluate_case,
)
from query_pilot.eval.models import (
    CaseEvaluationResult,
    EvaluationMetrics,
    FailureStage,
    MismatchClassification,
    SemanticStatus,
)
from query_pilot.eval.report import (
    EvaluationReportData,
    analyze_failures,
    generate_markdown_report,
)
from query_pilot.sql.generation import (
    ClarificationOption,
    ConversationTurnContext,
    SQLGenerationRequest,
    SQLGenerationResponse,
    SQLGenerationStatus,
    SQLGenerator,
)


# ==============================================================================
# Fixtures
# ==============================================================================

@pytest.fixture
def mock_schema() -> DatabaseSchema:
    """Minimal model-facing schema fixture for evaluation tests."""
    return DatabaseSchema(
        database_name="GradeSense_Test",
        is_model_facing=True,
        tables=[
            TableMetadata(
                schema_name="dbo",
                table_name="Students",
                columns=[
                    ColumnMetadata(name="Id", data_type="int", nullable=False, primary_key=True),
                    ColumnMetadata(name="FullName", data_type="varchar(255)", nullable=False),
                    ColumnMetadata(name="DepartmentId", data_type="int", nullable=False),
                    ColumnMetadata(name="IsActive", data_type="bit", nullable=False),
                ],
                primary_keys=["Id"],
            ),
        ],
    )


class StubGenerator(SQLGenerator):
    """Deterministic generator stub for evaluation testing."""

    def __init__(self, response: SQLGenerationResponse):
        self.response = response
        self.last_request: Optional[SQLGenerationRequest] = None

    def generate(self, request: SQLGenerationRequest) -> SQLGenerationResponse:
        self.last_request = request
        return self.response


# ==============================================================================
# 1. Clarification Classification Tests
# ==============================================================================

def test_correct_clarification_classification(mock_schema):
    """Verify that a clarification-expected case returning clarification is classified as correctly_clarified."""
    case = {
        "id": "C001",
        "category": "clarification",
        "question": "Show student performance metrics.",
        "expected_behavior": "clarification_needed",
        "reference_sql": None,
        "reference_result": None,
        "notes": "Ambiguous performance metric query.",
    }

    resp = SQLGenerationResponse(
        status=SQLGenerationStatus.CLARIFICATION_NEEDED,
        sql=None,
        explanation="Performance could mean attendance, marks, or risk prediction.",
        clarification_prompt="Which metric would you like to view?",
        clarification_options=[
            ClarificationOption(option_id="opt_marks", description="Assessment marks"),
            ClarificationOption(option_id="opt_att", description="Attendance percentage"),
        ],
    )
    generator = StubGenerator(resp)

    with patch("query_pilot.eval.evaluator.validate_sql") as mock_val, \
         patch("query_pilot.eval.evaluator.execute_read_only") as mock_exec:
        result = evaluate_case(case, generator, mock_schema)

        # Invariants: no validation or execution occurs
        mock_val.assert_not_called()
        mock_exec.assert_not_called()

    assert result.case_id == "C001"
    assert result.expected_behavior == "clarification_needed"
    assert result.generation_status == "success"
    assert result.generated_sql is None
    assert result.validation_allowed is True
    assert result.validator_accepted is None
    assert result.execution_status == "not_executed"
    assert result.executed is False
    assert result.semantic_status == SemanticStatus.CORRECTLY_CLARIFIED
    assert result.semantic_correct is True
    assert result.clarification_classification == "correctly_clarified"
    assert result.mismatch_classification == MismatchClassification.CLARIFICATION_HANDLING.value
    assert result.failure_stage is None
    assert result.clarification_prompt == "Which metric would you like to view?"
    assert len(result.clarification_options) == 2


def test_incorrect_clarification_classification_for_answerable_case(mock_schema):
    """Verify that an answerable case unexpectedly returning clarification is classified as a mismatch."""
    case = {
        "id": "Q001",
        "category": "simple_retrieval",
        "question": "List all active students.",
        "expected_behavior": "answerable",
        "reference_sql": "SELECT Id, FullName FROM Students WHERE IsActive = 1;",
        "reference_result": [{"Id": 1, "FullName": "Alice"}],
    }

    resp = SQLGenerationResponse(
        status=SQLGenerationStatus.CLARIFICATION_NEEDED,
        sql=None,
        explanation="Clarification requested unnecessarily.",
        clarification_prompt="Do you mean all departments?",
    )
    generator = StubGenerator(resp)

    with patch("query_pilot.eval.evaluator.validate_sql") as mock_val, \
         patch("query_pilot.eval.evaluator.execute_read_only") as mock_exec:
        result = evaluate_case(case, generator, mock_schema)

        mock_val.assert_not_called()
        mock_exec.assert_not_called()

    assert result.case_id == "Q001"
    assert result.expected_behavior == "answerable"
    assert result.generation_status == "success"
    assert result.generated_sql is None
    assert result.validation_allowed is False
    assert result.execution_status == "not_executed"
    assert result.result_matches_reference is False
    assert result.exact_result_match is False
    assert result.semantic_status == SemanticStatus.INCORRECTLY_CLARIFIED
    assert result.semantic_correct is False
    assert result.clarification_classification == "incorrectly_clarified"
    assert result.mismatch_classification == MismatchClassification.CLARIFICATION_MISMATCH.value
    assert result.failure_stage == FailureStage.CLARIFICATION


def test_answerable_sql_for_clarification_expected_case_is_mismatch(mock_schema):
    """Verify that returning answerable SQL for a query expected to require clarification is a mismatch."""
    case = {
        "id": "C001",
        "category": "clarification",
        "question": "Show student performance metrics.",
        "expected_behavior": "clarification_needed",
        "reference_sql": None,
        "reference_result": None,
    }

    resp = SQLGenerationResponse(
        status=SQLGenerationStatus.ANSWERABLE,
        sql="SELECT Id, FullName FROM Students;",
        explanation="Assumed all students without clarifying metric.",
    )
    generator = StubGenerator(resp)

    with patch("query_pilot.eval.evaluator.execute_read_only") as mock_exec:
        result = evaluate_case(case, generator, mock_schema)

        # Execution must not occur for clarification test cases
        mock_exec.assert_not_called()

    assert result.case_id == "C001"
    assert result.expected_behavior == "clarification_needed"
    assert result.generated_sql == "SELECT Id, FullName FROM Students;"
    assert result.execution_status == "not_executed"
    assert result.executed is False
    assert result.semantic_status == SemanticStatus.INCORRECT
    assert result.semantic_correct is False
    assert result.clarification_classification == "incorrectly_answered"
    assert result.mismatch_classification == MismatchClassification.CLARIFICATION_MISMATCH.value
    assert result.failure_stage == FailureStage.GENERATION


def test_unsupported_refusal_for_clarification_expected_case_is_mismatch(mock_schema):
    """Verify that returning unsupported refusal for a query expected to clarify is classified as a mismatch."""
    case = {
        "id": "C001",
        "category": "clarification",
        "question": "Show student performance metrics.",
        "expected_behavior": "clarification_needed",
        "reference_sql": None,
        "reference_result": None,
    }

    resp = SQLGenerationResponse(
        status=SQLGenerationStatus.UNSUPPORTED,
        sql=None,
        explanation="Performance metrics are completely untracked.",
    )
    generator = StubGenerator(resp)

    result = evaluate_case(case, generator, mock_schema)

    assert result.case_id == "C001"
    assert result.expected_behavior == "clarification_needed"
    assert result.semantic_status == SemanticStatus.INCORRECT
    assert result.semantic_correct is False
    assert result.clarification_classification == "incorrectly_unsupported"
    assert result.mismatch_classification == MismatchClassification.CLARIFICATION_MISMATCH.value
    assert result.failure_stage == FailureStage.GENERATION


# ==============================================================================
# 2. Conversation Context Propagation Tests
# ==============================================================================

def test_conversation_context_propagated_to_generation_request(mock_schema):
    """Verify that conversation_context in a case reaches SQLGenerationRequest and is saved in result."""
    case = {
        "id": "C002",
        "category": "clarification",
        "question": "How did they perform on the recent assessments?",
        "expected_behavior": "clarification_needed",
        "conversation_context": [
            {
                "role": "user",
                "user_question": "List all active students in CE.",
                "resolved_intent": "Retrieved students in department CE",
                "result_columns": ["StudentId", "FullName"],
            }
        ],
        "reference_sql": None,
        "reference_result": None,
    }

    resp = SQLGenerationResponse(
        status=SQLGenerationStatus.CLARIFICATION_NEEDED,
        sql=None,
        explanation="Resolved students to CE from context, but assessment type is ambiguous.",
        clarification_prompt="Which assessments: mid-term or end-term?",
    )
    generator = StubGenerator(resp)

    result = evaluate_case(case, generator, mock_schema)

    # Verify context reached generator
    assert generator.last_request is not None
    assert len(generator.last_request.conversation_context) == 1
    turn = generator.last_request.conversation_context[0]
    assert turn.role == "user"
    assert turn.user_question == "List all active students in CE."
    assert turn.resolved_intent == "Retrieved students in department CE"
    assert turn.result_columns == ["StudentId", "FullName"]

    # Verify context is retained in result
    assert result.conversation_context is not None
    assert len(result.conversation_context) == 1
    assert result.conversation_context[0]["user_question"] == "List all active students in CE."
    assert result.clarification_classification == "correctly_clarified"


def test_empty_conversation_context_handled_gracefully(mock_schema):
    """Verify that omitting conversation_context defaults cleanly to empty list."""
    case = {
        "id": "C001",
        "category": "clarification",
        "question": "Show student metrics.",
        "expected_behavior": "clarification_needed",
    }

    resp = SQLGenerationResponse(
        status=SQLGenerationStatus.CLARIFICATION_NEEDED,
        sql=None,
        explanation="Ambiguous metric.",
    )
    generator = StubGenerator(resp)

    result = evaluate_case(case, generator, mock_schema)

    assert generator.last_request is not None
    assert generator.last_request.conversation_context == []
    assert result.conversation_context is None


# ==============================================================================
# 3. Aggregate Metrics & Category Breakdown Tests
# ==============================================================================

def test_compute_evaluation_metrics_includes_clarification():
    """Verify that compute_evaluation_metrics correctly computes clarification statistics."""
    results = [
        CaseEvaluationResult(
            case_id="Q001",
            category="simple_retrieval",
            expected_behavior="answerable",
            generation_status="success",
            validation_allowed=True,
            execution_status="success",
            result_matches_reference=True,
            exact_result_match=True,
            semantic_status=SemanticStatus.CORRECT,
            semantic_correct=True,
        ),
        CaseEvaluationResult(
            case_id="Q024",
            category="unsupported",
            expected_behavior="unsupported",
            generation_status="success",
            validation_allowed=True,
            unsupported_classification="correctly_unsupported",
            semantic_status=SemanticStatus.CORRECTLY_REFUSED,
        ),
        CaseEvaluationResult(
            case_id="C001",
            category="clarification",
            expected_behavior="clarification_needed",
            generation_status="success",
            validation_allowed=True,
            clarification_classification="correctly_clarified",
            semantic_status=SemanticStatus.CORRECTLY_CLARIFIED,
            semantic_correct=True,
        ),
        CaseEvaluationResult(
            case_id="C002",
            category="clarification",
            expected_behavior="clarification_needed",
            generation_status="success",
            validation_allowed=True,
            clarification_classification="correctly_clarified",
            semantic_status=SemanticStatus.CORRECTLY_CLARIFIED,
            semantic_correct=True,
        ),
        CaseEvaluationResult(
            case_id="S001",
            category="safety",
            expected_behavior="rejected",
            generation_status="not_applicable",
            validation_allowed=False,
        ),
    ]

    metrics = compute_evaluation_metrics(results)

    assert metrics.total_cases == 5
    assert metrics.total_nl_cases == 4
    assert metrics.answerable_cases_count == 1
    assert metrics.unsupported_count == 1
    assert metrics.unsupported_correct_count == 1

    # Clarification metrics
    assert metrics.clarification_count == 2
    assert metrics.clarification_correct_count == 2
    assert metrics.clarification_recognition_rate == 1.0

    # Category breakdown for clarification
    assert "clarification" in metrics.category_breakdown
    cat_data = metrics.category_breakdown["clarification"]
    assert cat_data["total"] == 2
    assert cat_data["generation"] == "2/2"
    assert cat_data["validation"] == "2/2"
    assert cat_data["execution"] == "N/A"
    assert cat_data["exact_match"] == "N/A"
    assert cat_data["semantic_correct"] == "2/2"


# ==============================================================================
# 4. Report Generation & Failure Analysis Tests
# ==============================================================================

def test_report_generation_surfaces_clarification():
    """Verify that report generator surfaces clarification recognition rate and failure tracking."""
    cases = [
        CaseEvaluationResult(
            case_id="C001",
            category="clarification",
            expected_behavior="clarification_needed",
            generation_status="success",
            validation_allowed=True,
            clarification_classification="correctly_clarified",
            semantic_status=SemanticStatus.CORRECTLY_CLARIFIED,
        ),
        CaseEvaluationResult(
            case_id="C002",
            category="clarification",
            expected_behavior="clarification_needed",
            generation_status="success",
            validation_allowed=False,
            clarification_classification="incorrectly_answered",
            semantic_status=SemanticStatus.INCORRECT,
            notes="Generated SQL instead of clarifying.",
        ),
    ]

    failures = analyze_failures(cases)
    assert len(failures["clarification_handling_failures"]) == 1
    assert failures["clarification_handling_failures"][0].case_id == "C002"

    metrics = compute_evaluation_metrics(cases)
    report_data = EvaluationReportData(
        source_path="data/evaluation/test_results.json",
        metadata={"model": "gemini-2.0-flash", "task_version": "1.30"},
        metrics=metrics,
        cases=cases,
    )

    report_md = generate_markdown_report(report_data)

    assert "Structured Clarification Recognition:" in report_md
    assert "1/2 (50.0%)" in report_md
    assert "`clarification_handling_failures`" in report_md
    assert "`C002`" in report_md


# ==============================================================================
# 5. Comparative Evaluation Parity Tests
# ==============================================================================

def test_paired_case_comparison_handles_clarification():
    """Verify that PairedCaseComparison accurately classifies clarification outcomes across execution modes."""
    seq_pass = CaseEvaluationResult(
        case_id="C001",
        category="clarification",
        expected_behavior="clarification_needed",
        generation_status="success",
        validation_allowed=True,
        clarification_classification="correctly_clarified",
        semantic_status=SemanticStatus.CORRECTLY_CLARIFIED,
    )
    graph_pass = CaseEvaluationResult(
        case_id="C001",
        category="clarification",
        expected_behavior="clarification_needed",
        generation_status="success",
        validation_allowed=True,
        clarification_classification="correctly_clarified",
        semantic_status=SemanticStatus.CORRECTLY_CLARIFIED,
    )

    assert is_case_passing(seq_pass) is True
    assert is_case_passing(graph_pass) is True

    comparison = classify_paired_case(seq_pass, graph_pass)
    assert comparison.classification == PairedCaseClassification.BOTH_PASS

    # Regression case: sequential passes, graph fails
    graph_fail = CaseEvaluationResult(
        case_id="C001",
        category="clarification",
        expected_behavior="clarification_needed",
        generation_status="success",
        validation_allowed=False,
        clarification_classification="incorrectly_answered",
        semantic_status=SemanticStatus.INCORRECT,
    )
    assert is_case_passing(graph_fail) is False
    comp_reg = classify_paired_case(seq_pass, graph_fail)
    assert comp_reg.classification == PairedCaseClassification.SEQUENTIAL_ONLY_PASS
