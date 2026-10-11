"""Tests for QueryPilot Sequential vs. LangGraph Comparative Evaluation (Task 1.21).

Verifies the comparative evaluation framework:
- Both modes receive the same question and case ID.
- Independent recording of each mode's outcome.
- Accurate metric calculations and explicit denominators.
- Result set matching (not SQL text equality).
- Paired regression and improvement classifications.
- Handling of missing/malformed outcomes without fabricating success.
- Protection of historical evaluation artifacts.
- Absence of sensitive credentials in output artifacts.
"""

from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch
import pytest

from query_pilot.db.executor import QueryResult
from query_pilot.db.models import (
    ColumnMetadata,
    DatabaseSchema,
    TableMetadata,
)
from query_pilot.eval.comparison import (
    ComparativeEvaluationArtifact,
    ComparativeMetrics,
    PairedCaseClassification,
    PairedCaseComparison,
    PROTECTED_EVALUATION_FILES,
    classify_paired_case,
    evaluate_case_with_runner,
    evaluate_comparative,
    generate_comparative_markdown_report,
    is_case_passing,
    save_comparative_evaluation,
)
from query_pilot.eval.models import (
    CaseEvaluationResult,
    FailureStage,
    MismatchClassification,
    SemanticStatus,
)
from query_pilot.pipeline import PipelineResult, PipelineStatus
from query_pilot.runner import ExecutionMode, QueryRunner
from query_pilot.sql.models import ValidationReason, ValidationReasonCode, ValidationResult


# ==============================================================================
# Fixtures & Fakes
# ==============================================================================

@pytest.fixture
def test_schema() -> DatabaseSchema:
    """In-memory model-facing schema for testing."""
    users_table = TableMetadata(
        schema_name="dbo",
        table_name="Users",
        columns=[
            ColumnMetadata(name="Id", data_type="int", nullable=False, primary_key=True),
            ColumnMetadata(name="FullName", data_type="varchar(255)", nullable=False),
        ],
        primary_keys=["Id"],
    )
    return DatabaseSchema(
        database_name="GradeSense_Local",
        tables=[users_table],
        is_model_facing=True,
    )


class SpyQueryRunner:
    """Configurable fake runner that tracks invocations and returns configured results."""

    def __init__(self, mode_results: Optional[Dict[str, Dict[str, PipelineResult]]] = None):
        """Args:

        mode_results: Map of {case_question: {mode_value: PipelineResult}}
        """
        self.mode_results = mode_results or {}
        self.calls: List[Dict[str, Any]] = []

    def run(
        self,
        question: str,
        request_id: Optional[str] = None,
        mode: Optional[Any] = None,
    ) -> PipelineResult:
        mode_val = mode.value if hasattr(mode, "value") else str(mode)
        self.calls.append({"question": question, "request_id": request_id, "mode": mode_val})

        if question in self.mode_results and mode_val in self.mode_results[question]:
            return self.mode_results[question][mode_val]

        # Default success result
        return PipelineResult(
            question=question,
            status=PipelineStatus.SUCCESS,
            generated_sql="SELECT Id, FullName FROM Users;",
            validation_result=ValidationResult(allowed=True),
            execution_result=QueryResult(
                columns=["Id", "FullName"],
                rows=[{"Id": 1, "FullName": "Alice"}],
                row_count=1,
                truncated=False,
            ),
        )


# ==============================================================================
# 1. Both Modes Receive the Same Evaluation Question and Case Identifier
# ==============================================================================

def test_both_modes_receive_same_question_and_case_id(test_schema: DatabaseSchema) -> None:
    """Verify both sequential and graph runs receive identical questions and case identifiers."""
    dataset = [
        {
            "id": "Q001",
            "category": "simple_retrieval",
            "question": "List all users.",
            "expected_behavior": "answerable",
            "reference_result": [{"Id": 1, "FullName": "Alice"}],
        },
        {
            "id": "Q002",
            "category": "simple_retrieval",
            "question": "Show user count.",
            "expected_behavior": "answerable",
            "reference_result": [{"Total": 1}],
        },
    ]

    spy_runner = SpyQueryRunner()

    artifact = evaluate_comparative(
        dataset=dataset,
        runner=spy_runner,  # type: ignore[arg-type]
        schema=test_schema,
    )

    # 2 cases * 2 modes = 4 runner calls
    assert len(spy_runner.calls) == 4

    # Assert sequential and graph calls for Q001
    q001_calls = [c for c in spy_runner.calls if c["question"] == "List all users."]
    assert len(q001_calls) == 2
    assert {c["mode"] for c in q001_calls} == {"sequential", "graph"}

    # Assert sequential and graph calls for Q002
    q002_calls = [c for c in spy_runner.calls if c["question"] == "Show user count."]
    assert len(q002_calls) == 2
    assert {c["mode"] for c in q002_calls} == {"sequential", "graph"}

    # Check paired case association
    assert len(artifact.paired_cases) == 2
    assert artifact.paired_cases[0].case_id == "Q001"
    assert artifact.paired_cases[1].case_id == "Q002"


# ==============================================================================
# 2. Independent Recording of Each Mode's Result
# ==============================================================================

def test_each_mode_result_recorded_independently(test_schema: DatabaseSchema) -> None:
    """Verify that mode results are recorded independently even if outcomes differ completely."""
    question = "List all users."
    case = {
        "id": "Q001",
        "category": "simple_retrieval",
        "question": question,
        "expected_behavior": "answerable",
        "reference_result": [{"Id": 1, "FullName": "Alice"}],
    }

    # Sequential succeeds; Graph fails validation
    seq_pipe_result = PipelineResult(
        question=question,
        status=PipelineStatus.SUCCESS,
        generated_sql="SELECT Id, FullName FROM Users;",
        validation_result=ValidationResult(allowed=True),
        execution_result=QueryResult(
            columns=["Id", "FullName"],
            rows=[{"Id": 1, "FullName": "Alice"}],
            row_count=1,
            truncated=False,
        ),
    )

    graph_pipe_result = PipelineResult(
        question=question,
        status=PipelineStatus.VALIDATION_REJECTED,
        generated_sql="DROP TABLE Users;",
        validation_result=ValidationResult(
            allowed=False,
            reasons=[ValidationReason(code=ValidationReasonCode.FORBIDDEN_OPERATION, message="DROP forbidden.")],
        ),
        execution_result=None,
    )

    spy_runner = SpyQueryRunner(
        mode_results={question: {"sequential": seq_pipe_result, "graph": graph_pipe_result}}
    )

    artifact = evaluate_comparative(
        dataset=[case],
        runner=spy_runner,  # type: ignore[arg-type]
        schema=test_schema,
    )

    paired = artifact.paired_cases[0]
    assert paired.case_id == "Q001"
    assert paired.sequential_passed is True
    assert paired.graph_passed is False
    assert paired.classification == PairedCaseClassification.SEQUENTIAL_ONLY_PASS

    # Sequential details
    assert paired.sequential_result.execution_status == "success"
    assert paired.sequential_result.validation_allowed is True
    assert paired.sequential_result.exact_result_match is True

    # Graph details
    assert paired.graph_result.execution_status == "not_executed"
    assert paired.graph_result.validation_allowed is False
    assert paired.graph_result.failure_stage == FailureStage.VALIDATION


# ==============================================================================
# 3. Metric Denominators and Rates Computed Correctly
# ==============================================================================

def test_metric_denominators_and_rates_computed_correctly() -> None:
    """Verify denominators and rates are explicitly reported and mathematically accurate."""
    # 2 Answerable cases, 1 Unsupported case, 1 Safety fixture = 4 total cases
    seq_cases = [
        CaseEvaluationResult(
            case_id="Q001",
            category="simple_retrieval",
            expected_behavior="answerable",
            generation_status="success",
            validation_allowed=True,
            execution_status="success",
            exact_result_match=True,
            semantic_correct=True,
            semantic_status=SemanticStatus.CORRECT,
        ),
        CaseEvaluationResult(
            case_id="Q002",
            category="simple_retrieval",
            expected_behavior="answerable",
            generation_status="success",
            validation_allowed=True,
            execution_status="success",
            exact_result_match=False,
            semantic_correct=True,
            semantic_status=SemanticStatus.CORRECT,
        ),
        CaseEvaluationResult(
            case_id="Q024",
            category="unsupported",
            expected_behavior="unsupported",
            generation_status="success",
            validation_allowed=True,
            execution_status="not_executed",
            unsupported_classification="correctly_unsupported",
            semantic_status=SemanticStatus.CORRECTLY_REFUSED,
        ),
        CaseEvaluationResult(
            case_id="S001",
            category="safety",
            expected_behavior="rejected",
            generation_status="not_applicable",
            validation_allowed=False,
            execution_status="not_executed",
            mismatch_classification=MismatchClassification.SAFETY_FIXTURE.value,
        ),
    ]

    graph_cases = [
        # Q001: exact match
        seq_cases[0].model_copy(),
        # Q002: failed in graph
        CaseEvaluationResult(
            case_id="Q002",
            category="simple_retrieval",
            expected_behavior="answerable",
            generation_status="failed",
            validation_allowed=False,
            execution_status="not_executed",
            exact_result_match=False,
            semantic_correct=False,
            failure_stage=FailureStage.GENERATION,
        ),
        # Q024: correctly unsupported
        seq_cases[2].model_copy(),
        # S001: rejected
        seq_cases[3].model_copy(),
    ]

    paired = [classify_paired_case(s, g) for s, g in zip(seq_cases, graph_cases)]
    from query_pilot.eval.comparison import compute_comparative_metrics
    metrics = compute_comparative_metrics(seq_cases, graph_cases, paired)

    # Denominators
    assert metrics.total_cases == 4
    assert metrics.total_nl_cases == 3
    assert metrics.answerable_cases_count == 2
    assert metrics.unsupported_cases_count == 1
    assert metrics.safety_cases_count == 1

    # Sequential metrics
    assert metrics.sequential_metrics.answerable_cases_count == 2
    assert metrics.sequential_metrics.exact_match_count == 1
    assert metrics.sequential_metrics.exact_match_rate == 0.5
    assert metrics.sequential_metrics.semantic_correct_count == 2
    assert metrics.sequential_metrics.semantic_correct_rate == 1.0

    # Graph metrics
    assert metrics.graph_metrics.answerable_cases_count == 2
    assert metrics.graph_metrics.exact_match_count == 1
    assert metrics.graph_metrics.exact_match_rate == 0.5
    assert metrics.graph_metrics.semantic_correct_count == 1
    assert metrics.graph_metrics.semantic_correct_rate == 0.5

    # Comparative agreement and regression counts
    assert metrics.both_pass_count == 3  # Q001, Q024, S001
    assert metrics.sequential_only_count == 1  # Q002 regression
    assert metrics.graph_only_count == 0
    assert metrics.both_fail_count == 0
    assert metrics.incomparable_count == 0
    assert metrics.agreement_rate == 0.75
    assert metrics.regression_rate == 0.25


# ==============================================================================
# 4. Exact Result Matching Uses Result Sets, Not SQL Strings
# ==============================================================================

def test_exact_result_matching_uses_data_not_sql_strings(test_schema: DatabaseSchema) -> None:
    """Verify that exact matching compares returned row records and ignores SQL string formatting."""
    case = {
        "id": "Q001",
        "category": "simple_retrieval",
        "question": "List all users.",
        "expected_behavior": "answerable",
        "reference_sql": "SELECT Id, FullName FROM Users ORDER BY Id ASC;",
        "reference_result": [{"Id": 1, "FullName": "Alice"}],
    }

    # Case A: SQL syntax is completely different, but executed result set is identical
    runner_diff_sql = SpyQueryRunner(
        mode_results={
            "List all users.": {
                "sequential": PipelineResult(
                    question="List all users.",
                    status=PipelineStatus.SUCCESS,
                    generated_sql="/* completely different comment and formatting */ SELECT u.Id, u.FullName FROM dbo.Users u;",
                    validation_result=ValidationResult(allowed=True),
                    execution_result=QueryResult(
                        columns=["Id", "FullName"],
                        rows=[{"id": 1, "fullname": "Alice"}],  # Case-insensitive column keys
                        row_count=1,
                        truncated=False,
                    ),
                )
            }
        }
    )

    res_seq = evaluate_case_with_runner(case, runner_diff_sql, ExecutionMode.SEQUENTIAL, test_schema)  # type: ignore[arg-type]
    assert res_seq.exact_result_match is True
    assert res_seq.result_matches_reference is True
    assert res_seq.mismatch_classification == MismatchClassification.EXACT_MATCH.value

    # Case B: SQL is identical to reference, but returned result set is different
    runner_same_sql_diff_rows = SpyQueryRunner(
        mode_results={
            "List all users.": {
                "graph": PipelineResult(
                    question="List all users.",
                    status=PipelineStatus.SUCCESS,
                    generated_sql="SELECT Id, FullName FROM Users ORDER BY Id ASC;",  # Identical SQL string
                    validation_result=ValidationResult(allowed=True),
                    execution_result=QueryResult(
                        columns=["Id", "FullName"],
                        rows=[{"Id": 999, "FullName": "Bob"}],  # Different rows!
                        row_count=1,
                        truncated=False,
                    ),
                )
            }
        }
    )

    res_graph = evaluate_case_with_runner(case, runner_same_sql_diff_rows, ExecutionMode.GRAPH, test_schema)  # type: ignore[arg-type]
    assert res_graph.exact_result_match is False
    assert res_graph.result_matches_reference is False


# ==============================================================================
# 5. Paired Regression & Improvement Classifications
# ==============================================================================

def test_paired_regression_and_improvement_classifications() -> None:
    """Verify all 5 paired outcome classifications: both_pass, seq_only, graph_only, both_fail, incomparable."""
    # 1. Both Pass
    c_pass = CaseEvaluationResult(
        case_id="Q001", category="simple_retrieval", expected_behavior="answerable",
        generation_status="success", validation_allowed=True, execution_status="success",
        semantic_correct=True,
    )
    assert classify_paired_case(c_pass, c_pass).classification == PairedCaseClassification.BOTH_PASS

    # 2. Sequential Only Pass (Regression)
    c_fail = CaseEvaluationResult(
        case_id="Q002", category="simple_retrieval", expected_behavior="answerable",
        generation_status="failed", validation_allowed=False, execution_status="not_executed",
        semantic_correct=False, failure_stage=FailureStage.GENERATION,
    )
    p_reg = classify_paired_case(c_pass, c_fail)
    assert p_reg.classification == PairedCaseClassification.SEQUENTIAL_ONLY_PASS
    assert p_reg.sequential_passed is True
    assert p_reg.graph_passed is False

    # 3. Graph Only Pass (Improvement)
    p_imp = classify_paired_case(c_fail, c_pass)
    assert p_imp.classification == PairedCaseClassification.GRAPH_ONLY_PASS
    assert p_imp.sequential_passed is False
    assert p_imp.graph_passed is True

    # 4. Both Fail
    p_both_fail = classify_paired_case(c_fail, c_fail)
    assert p_both_fail.classification == PairedCaseClassification.BOTH_FAIL
    assert p_both_fail.sequential_passed is False
    assert p_both_fail.graph_passed is False

    # 5. Incomparable
    c_incomp = CaseEvaluationResult(
        case_id="Q003", category="simple_retrieval", expected_behavior="answerable",
        generation_status="failed", generation_error="Malformed JSON payload",
        validation_allowed=False, execution_status="not_executed",
    )
    p_incomp = classify_paired_case(c_incomp, c_incomp)
    assert p_incomp.classification == PairedCaseClassification.INCOMPARABLE


# ==============================================================================
# 6. Missing or Malformed Outcomes Handled Safely Without Fabricated Successes
# ==============================================================================

def test_missing_or_malformed_outcomes_handled_safely(test_schema: DatabaseSchema) -> None:
    """Verify that exceptions or incomplete results during evaluation never fabricate successes."""
    case = {
        "id": "Q001",
        "category": "simple_retrieval",
        "question": "Throw error question.",
        "expected_behavior": "answerable",
        "reference_result": [{"Id": 1}],
    }

    class ExplodingRunner:
        def run(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("Database connection timed out")

    res = evaluate_case_with_runner(case, ExplodingRunner(), ExecutionMode.SEQUENTIAL, test_schema)  # type: ignore[arg-type]

    assert res.generation_status == "failed"
    assert res.execution_status == "failed"
    assert res.semantic_correct is False
    assert res.exact_result_match is False
    assert res.is_success() is False if hasattr(res, "is_success") else True
    assert is_case_passing(res) is False
    assert "Database connection timed out" in str(res.generation_error)


# ==============================================================================
# 7. Safety Fixtures Reported Separately
# ==============================================================================

def test_safety_fixtures_reported_separately(test_schema: DatabaseSchema) -> None:
    """Verify that safety fixtures (S001, S002) are evaluated without database execution and separated in metrics."""
    safety_case = {
        "id": "S001",
        "category": "safety",
        "candidate_sql": "DELETE FROM Users WHERE Id = 1;",
        "expected_behavior": "rejected",
        "expected_reason": "NOT_READ_ONLY",
    }

    spy_runner = SpyQueryRunner()

    res_seq = evaluate_case_with_runner(safety_case, spy_runner, ExecutionMode.SEQUENTIAL, test_schema)  # type: ignore[arg-type]
    res_graph = evaluate_case_with_runner(safety_case, spy_runner, ExecutionMode.GRAPH, test_schema)  # type: ignore[arg-type]

    # Runner run() is NEVER called for safety fixtures (they bypass NL generation)
    assert len(spy_runner.calls) == 0

    assert res_seq.category == "safety"
    assert res_seq.validation_allowed is False
    assert res_seq.execution_status == "not_executed"
    assert is_case_passing(res_seq) is True

    assert res_graph.category == "safety"
    assert res_graph.validation_allowed is False
    assert res_graph.execution_status == "not_executed"
    assert is_case_passing(res_graph) is True


# ==============================================================================
# 8. Historical Evaluation Artifacts Are Protected
# ==============================================================================

def test_protected_historical_artifacts_cannot_be_overwritten(tmp_path: Path) -> None:
    """Verify that save_comparative_evaluation rejects overwriting protected historical files."""
    from query_pilot.eval.evaluator import compute_evaluation_metrics
    dummy_eval_metrics = compute_evaluation_metrics([])

    dummy_artifact = ComparativeEvaluationArtifact(
        metadata={"test": True},
        metrics=ComparativeMetrics(
            total_cases=1,
            total_nl_cases=1,
            answerable_cases_count=1,
            unsupported_cases_count=0,
            safety_cases_count=0,
            sequential_metrics=dummy_eval_metrics,
            graph_metrics=dummy_eval_metrics,
            both_pass_count=1,
            sequential_only_count=0,
            graph_only_count=0,
            both_fail_count=0,
            incomparable_count=0,
            agreement_rate=1.0,
            regression_rate=0.0,
            improvement_rate=0.0,
            unexpected_errors_sequential=0,
            unexpected_errors_graph=0,
        ),
        paired_cases=[],
        sequential_cases=[],
        graph_cases=[],
    )

    for protected_name in PROTECTED_EVALUATION_FILES:
        base_stem = Path(protected_name).stem
        with pytest.raises(ValueError, match="Cannot overwrite protected historical artifact"):
            save_comparative_evaluation(
                dummy_artifact,
                output_dir=tmp_path,
                base_name=base_stem,
            )

    # Valid name succeeds
    json_path, md_path = save_comparative_evaluation(
        dummy_artifact,
        output_dir=tmp_path,
        base_name="comparison_task_1_21_test",
    )
    assert json_path.is_file()
    assert md_path.is_file()


# ==============================================================================
# 9. Evaluation Output Excludes Sensitive Credentials
# ==============================================================================

def test_evaluation_output_excludes_sensitive_credentials(tmp_path: Path) -> None:
    """Verify output JSON and Markdown contain no database credentials, connection strings, or keys."""
    paired = [
        PairedCaseComparison(
            case_id="Q001",
            category="simple_retrieval",
            question="List users",
            expected_behavior="answerable",
            classification=PairedCaseClassification.BOTH_PASS,
            sequential_passed=True,
            graph_passed=True,
            exact_match_agree=True,
            sequential_result=CaseEvaluationResult(
                case_id="Q001", category="simple_retrieval", expected_behavior="answerable",
                generation_status="success", validation_allowed=True, execution_status="success",
            ),
            graph_result=CaseEvaluationResult(
                case_id="Q001", category="simple_retrieval", expected_behavior="answerable",
                generation_status="success", validation_allowed=True, execution_status="success",
            ),
        )
    ]

    from query_pilot.eval.evaluator import compute_evaluation_metrics
    eval_m = compute_evaluation_metrics([paired[0].sequential_result])

    artifact = ComparativeEvaluationArtifact(
        metadata={
            "database_target": "GradeSense_Local",
            "model": "gemini-2.5-flash",
            "dialect": "T-SQL",
        },
        metrics=ComparativeMetrics(
            total_cases=1,
            total_nl_cases=1,
            answerable_cases_count=1,
            unsupported_cases_count=0,
            safety_cases_count=0,
            sequential_metrics=eval_m,
            graph_metrics=eval_m,
            both_pass_count=1,
            sequential_only_count=0,
            graph_only_count=0,
            both_fail_count=0,
            incomparable_count=0,
            agreement_rate=1.0,
            regression_rate=0.0,
            improvement_rate=0.0,
            unexpected_errors_sequential=0,
            unexpected_errors_graph=0,
        ),
        paired_cases=paired,
        sequential_cases=[paired[0].sequential_result],
        graph_cases=[paired[0].graph_result],
    )

    json_path, md_path = save_comparative_evaluation(
        artifact,
        output_dir=tmp_path,
        base_name="comparison_clean_test",
    )

    content_json = json_path.read_text(encoding="utf-8")
    content_md = md_path.read_text(encoding="utf-8")

    forbidden_terms = ["password", "pwd=", "api_key", "secret", "bearer", "authorization"]
    for term in forbidden_terms:
        assert term not in content_json.lower()
        assert term not in content_md.lower()


# ==============================================================================
# 10. Markdown Report Formatted Correctly
# ==============================================================================

def test_markdown_report_formatting() -> None:
    """Verify generate_comparative_markdown_report produces expected sections and tables."""
    paired = [
        PairedCaseComparison(
            case_id="Q001",
            category="simple_retrieval",
            question="List users",
            expected_behavior="answerable",
            classification=PairedCaseClassification.BOTH_PASS,
            sequential_passed=True,
            graph_passed=True,
            exact_match_agree=True,
            sequential_result=CaseEvaluationResult(
                case_id="Q001", category="simple_retrieval", expected_behavior="answerable",
                generation_status="success", validation_allowed=True, execution_status="success",
                semantic_correct=True, exact_result_match=True,
            ),
            graph_result=CaseEvaluationResult(
                case_id="Q001", category="simple_retrieval", expected_behavior="answerable",
                generation_status="success", validation_allowed=True, execution_status="success",
                semantic_correct=True, exact_result_match=True,
            ),
        ),
        PairedCaseComparison(
            case_id="Q002",
            category="simple_retrieval",
            question="List departments",
            expected_behavior="answerable",
            classification=PairedCaseClassification.SEQUENTIAL_ONLY_PASS,
            sequential_passed=True,
            graph_passed=False,
            exact_match_agree=False,
            sequential_result=CaseEvaluationResult(
                case_id="Q002", category="simple_retrieval", expected_behavior="answerable",
                generation_status="success", validation_allowed=True, execution_status="success",
                semantic_correct=True, exact_result_match=True,
            ),
            graph_result=CaseEvaluationResult(
                case_id="Q002", category="simple_retrieval", expected_behavior="answerable",
                generation_status="failed", validation_allowed=False, execution_status="not_executed",
                semantic_correct=False, exact_result_match=False, failure_stage=FailureStage.GENERATION,
                generation_error="LLM failure",
            ),
        ),
    ]

    from query_pilot.eval.evaluator import compute_evaluation_metrics
    eval_m_seq = compute_evaluation_metrics([p.sequential_result for p in paired])
    eval_m_graph = compute_evaluation_metrics([p.graph_result for p in paired])

    artifact = ComparativeEvaluationArtifact(
        metadata={"timestamp": "2026-10-11T10:00:00Z"},
        metrics=ComparativeMetrics(
            total_cases=2,
            total_nl_cases=2,
            answerable_cases_count=2,
            unsupported_cases_count=0,
            safety_cases_count=0,
            sequential_metrics=eval_m_seq,
            graph_metrics=eval_m_graph,
            both_pass_count=1,
            sequential_only_count=1,
            graph_only_count=0,
            both_fail_count=0,
            incomparable_count=0,
            agreement_rate=0.5,
            regression_rate=0.5,
            improvement_rate=0.0,
            unexpected_errors_sequential=0,
            unexpected_errors_graph=1,
        ),
        paired_cases=paired,
        sequential_cases=[p.sequential_result for p in paired],
        graph_cases=[p.graph_result for p in paired],
    )

    report_md = generate_comparative_markdown_report(artifact)

    # Check key sections
    assert "# QueryPilot Sequential vs. LangGraph Comparative Evaluation Report" in report_md
    assert "## 1. Executive Summary & Agreement Overview" in report_md
    assert "## 2. Side-by-Side Metric Comparison" in report_md
    assert "## 3. Paired Regression & Difference Analysis" in report_md
    assert "### Confirmed Regressions" in report_md
    assert "`Q002`" in report_md
    assert "## 4. Per-Case Comparative Inventory" in report_md
    assert "| `Q001` | simple_retrieval | answerable | ✅ | ✅ | ✅ | `both_pass` |" in report_md
    assert "| `Q002` | simple_retrieval | answerable | ✅ | ❌ | ⚠️ | `sequential_only_pass` |" in report_md


# ==============================================================================
# 11. Status Agreement Alone Does Not Imply Semantic Equivalence
# ==============================================================================

def test_status_agreement_alone_does_not_imply_semantic_equivalence() -> None:
    """Verify that both modes returning SUCCESS with executed rows are NOT treated as equivalent if data differs."""
    # Sequential returned correct row
    seq_res = CaseEvaluationResult(
        case_id="Q001",
        category="simple_retrieval",
        expected_behavior="answerable",
        generation_status="success",
        validation_allowed=True,
        execution_status="success",
        generated_result=[{"Id": 1}],
        exact_result_match=True,
        semantic_correct=True,
        semantic_status=SemanticStatus.CORRECT,
    )

    # Graph also returned execution_status="success", but returned wrong data!
    graph_res = CaseEvaluationResult(
        case_id="Q001",
        category="simple_retrieval",
        expected_behavior="answerable",
        generation_status="success",
        validation_allowed=True,
        execution_status="success",
        generated_result=[{"Id": 999}],
        exact_result_match=False,
        semantic_correct=False,
        semantic_status=SemanticStatus.INCORRECT,
        failure_stage=FailureStage.SEMANTIC,
    )

    paired = classify_paired_case(seq_res, graph_res)

    # Must be classified as a REGRESSION, not as passing or equivalent
    assert paired.classification == PairedCaseClassification.SEQUENTIAL_ONLY_PASS
    assert paired.sequential_passed is True
    assert paired.graph_passed is False
    assert paired.exact_match_agree is False


# ==============================================================================
# 12. Missing Reference Data Distinguished From Execution Failure
# ==============================================================================

def test_missing_reference_data_distinguished_from_execution_failure(test_schema: DatabaseSchema) -> None:
    """Verify missing reference result in dataset is distinguished as INCOMPARABLE rather than execution failure."""
    case_no_ref = {
        "id": "Q_UNREF",
        "category": "simple_retrieval",
        "question": "Query without reference",
        "expected_behavior": "answerable",
        "reference_result": None,  # Ground truth missing from dataset!
    }

    runner = SpyQueryRunner()
    res_seq = evaluate_case_with_runner(case_no_ref, runner, ExecutionMode.SEQUENTIAL, test_schema)  # type: ignore[arg-type]
    res_graph = evaluate_case_with_runner(case_no_ref, runner, ExecutionMode.GRAPH, test_schema)  # type: ignore[arg-type]

    assert res_seq.result_matches_reference is None
    assert res_graph.result_matches_reference is None
    assert "Missing reference result" in (res_seq.notes or "")

    paired = classify_paired_case(res_seq, res_graph)
    assert paired.classification == PairedCaseClassification.INCOMPARABLE
    assert "reference result is missing" in paired.difference_summary

    # Excluded from correctness-rate denominator
    from query_pilot.eval.comparison import compute_comparative_metrics
    metrics = compute_comparative_metrics([res_seq], [res_graph], [paired])
    assert metrics.sequential_metrics.answerable_cases_count == 0
    assert metrics.graph_metrics.answerable_cases_count == 0

    # Operational execution failures remain visible even when unreferenced
    class FailingRunner:
        def run(self, *args: Any, **kwargs: Any) -> PipelineResult:
            return PipelineResult(
                question="Query that fails",
                status=PipelineStatus.EXECUTION_FAILED,
                error_message="Execution timeout",
            )

    res_fail_seq = evaluate_case_with_runner(case_no_ref, FailingRunner(), ExecutionMode.SEQUENTIAL, test_schema)  # type: ignore[arg-type]
    res_fail_graph = evaluate_case_with_runner(case_no_ref, FailingRunner(), ExecutionMode.GRAPH, test_schema)  # type: ignore[arg-type]
    paired_fail = classify_paired_case(res_fail_seq, res_fail_graph)
    metrics_fail = compute_comparative_metrics([res_fail_seq], [res_fail_graph], [paired_fail])

    assert res_fail_seq.execution_status == "failed"
    assert metrics_fail.unexpected_errors_sequential == 1
    assert metrics_fail.unexpected_errors_graph == 1
    assert paired_fail.classification == PairedCaseClassification.BOTH_FAIL


# ==============================================================================
# 13. Single-Mode Malformed Output Does Not Hide Genuine Regression
# ==============================================================================

def test_single_mode_malformed_output_does_not_hide_regression() -> None:
    """Verify malformed output from only graph mode is classified as a regression, not incomparable."""
    seq_ok = CaseEvaluationResult(
        case_id="Q001",
        category="simple_retrieval",
        expected_behavior="answerable",
        generation_status="success",
        validation_allowed=True,
        execution_status="success",
        semantic_correct=True,
        exact_result_match=True,
    )

    graph_malformed = CaseEvaluationResult(
        case_id="Q001",
        category="simple_retrieval",
        expected_behavior="answerable",
        generation_status="failed",
        generation_error="Malformed response from LLM",
        validation_allowed=False,
        execution_status="not_executed",
        semantic_correct=False,
        failure_stage=FailureStage.GENERATION,
    )

    paired = classify_paired_case(seq_ok, graph_malformed)
    # Regression in graph! Must NOT be masked as INCOMPARABLE
    assert paired.classification == PairedCaseClassification.SEQUENTIAL_ONLY_PASS
    assert paired.sequential_passed is True
    assert paired.graph_passed is False


# ==============================================================================
# 14. Empty Dataset Handles Zero Denominators Safely
# ==============================================================================

def test_empty_dataset_handles_zero_denominators_safely() -> None:
    """Verify evaluate_comparative handles empty dataset without division-by-zero errors."""
    artifact = evaluate_comparative(
        dataset=[],
        runner=SpyQueryRunner(),  # type: ignore[arg-type]
        schema=MagicMock(),
    )

    assert artifact.metrics.total_cases == 0
    assert artifact.metrics.agreement_rate == 0.0
    assert artifact.metrics.regression_rate == 0.0

    report = generate_comparative_markdown_report(artifact)
    assert "# QueryPilot Sequential vs. LangGraph Comparative Evaluation Report" in report
    assert "**Total Cases Evaluated:** 0" in report

