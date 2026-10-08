"""Unit tests for baseline evaluation framework and metric calculations (Task 1.12).

Tests the evaluation runner, result comparison, semantic classification,
and metrics aggregation without requiring external Gemini network calls.
"""

from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch
import pytest

from query_pilot.db.executor import QueryResult
from query_pilot.db.models import (
    ColumnMetadata,
    DatabaseSchema,
    ForeignKeyMetadata,
    TableMetadata,
)
from query_pilot.eval.evaluator import (
    compare_results,
    compute_evaluation_metrics,
    evaluate_case,
)
from query_pilot.eval.models import (
    CaseEvaluationResult,
    FailureStage,
    MismatchClassification,
    SemanticStatus,
)
from query_pilot.sql.generation import (
    SQLGenerationRequest,
    SQLGenerationResponse,
    SQLGenerationStatus,
    SQLGenerator,
)


# ==============================================================================
# In-Memory Test Fixtures & Fake Generator
# ==============================================================================

@pytest.fixture
def mock_schema() -> DatabaseSchema:
    """Deterministic in-memory model-facing schema for testing."""
    users_table = TableMetadata(
        schema_name="dbo",
        table_name="Users",
        columns=[
            ColumnMetadata(name="Id", data_type="int", nullable=False, primary_key=True),
            ColumnMetadata(name="FullName", data_type="varchar(255)", nullable=False),
        ],
        primary_keys=["Id"],
    )
    students_table = TableMetadata(
        schema_name="dbo",
        table_name="Students",
        columns=[
            ColumnMetadata(name="Id", data_type="int", nullable=False, primary_key=True),
            ColumnMetadata(name="DepartmentId", data_type="int", nullable=False),
            ColumnMetadata(name="EnrollmentNumber", data_type="varchar(50)", nullable=False),
            ColumnMetadata(name="CGPA", data_type="decimal(3,2)", nullable=True),
        ],
        primary_keys=["Id"],
        foreign_keys=[
            ForeignKeyMetadata(
                name="FK_Students_Users",
                source_table="Students",
                source_column="Id",
                target_table="Users",
                target_column="Id",
            ),
        ],
    )
    course_offerings_table = TableMetadata(
        schema_name="dbo",
        table_name="CourseOfferings",
        columns=[
            ColumnMetadata(name="Id", data_type="int", nullable=False, primary_key=True),
            ColumnMetadata(name="IsActive", data_type="bit", nullable=False),
        ],
        primary_keys=["Id"],
    )
    return DatabaseSchema(
        database_name="GradeSense_Local",
        tables=[users_table, students_table, course_offerings_table],
        is_model_facing=True,
    )


class MockSQLGenerator(SQLGenerator):
    """Fake generator returning preset responses for deterministic unit testing."""

    def __init__(self, sql: str, explanation: str = "Test explanation", assumptions: Optional[List[str]] = None):
        self.sql = sql
        self.explanation = explanation
        self.assumptions = assumptions or []
        self.calls = []

    def generate(self, request: SQLGenerationRequest) -> SQLGenerationResponse:
        self.calls.append(request)
        return SQLGenerationResponse(
            sql=self.sql,
            explanation=self.explanation,
            assumptions=self.assumptions,
        )


# ==============================================================================
# Unit Tests — Result Comparison
# ==============================================================================

class TestResultComparison:
    """Verifies deterministic comparison between generated and reference query results."""

    def test_compare_results_exact_match(self):
        gen = [{"Id": 1, "Name": "Alice"}, {"Id": 2, "Name": "Bob"}]
        ref = [{"Id": 1, "Name": "Alice"}, {"Id": 2, "Name": "Bob"}]
        assert compare_results(gen, ref) is True

    def test_compare_results_case_insensitive_keys(self):
        gen = [{"id": 1, "NAME": "Alice"}]
        ref = [{"Id": 1, "Name": "Alice"}]
        assert compare_results(gen, ref) is True

    def test_compare_results_mismatched_length(self):
        gen = [{"Id": 1, "Name": "Alice"}]
        ref = [{"Id": 1, "Name": "Alice"}, {"Id": 2, "Name": "Bob"}]
        assert compare_results(gen, ref) is False

    def test_compare_results_mismatched_values(self):
        gen = [{"Id": 1, "Name": "Alice"}]
        ref = [{"Id": 1, "Name": "Charlie"}]
        assert compare_results(gen, ref) is False

    def test_compare_results_none_inputs(self):
        assert compare_results(None, [{"Id": 1}]) is False
        assert compare_results([{"Id": 1}], None) is False
        assert compare_results(None, None) is False


# ==============================================================================
# Unit Tests — Case Evaluation Pipeline
# ==============================================================================

class TestEvaluateCase:
    """Verifies evaluation stages across safety, answerable, semantic trap, and unsupported cases."""

    def test_evaluate_safety_fixture_rejected(self, mock_schema: DatabaseSchema):
        case = {
            "id": "S001",
            "category": "safety",
            "candidate_sql": "DELETE FROM Students WHERE Id = 1;",
            "expected_behavior": "rejected",
            "expected_reason": "NOT_READ_ONLY",
            "notes": "Safety regression",
        }
        gen = MockSQLGenerator(sql="NOT_USED")
        result = evaluate_case(case=case, generator=gen, schema=mock_schema, engine=None)

        assert result.case_id == "S001"
        assert result.category == "safety"
        assert result.generation_status == "not_applicable"
        assert result.validation_allowed is False
        assert result.execution_status == "not_executed"
        assert result.semantic_status == SemanticStatus.CORRECT
        assert result.failure_stage is None
        assert len(gen.calls) == 0  # Gemini was NEVER called for safety fixtures

    def test_evaluate_answerable_success(self, mock_schema: DatabaseSchema):
        case = {
            "id": "Q001",
            "category": "simple_retrieval",
            "question": "List student enrollment numbers",
            "expected_behavior": "answerable",
            "reference_sql": "SELECT EnrollmentNumber FROM Students;",
            "reference_result": [{"EnrollmentNumber": "21CE001"}],
            "notes": "Simple query",
        }
        gen = MockSQLGenerator(sql="SELECT EnrollmentNumber FROM Students;")
        mock_exec = QueryResult(
            columns=["EnrollmentNumber"],
            rows=[{"EnrollmentNumber": "21CE001"}],
            row_count=1,
            truncated=False,
            execution_time_ms=2.0,
        )

        with patch("query_pilot.eval.evaluator.execute_read_only", return_value=mock_exec):
            result = evaluate_case(case=case, generator=gen, schema=mock_schema, engine=None)

            assert result.case_id == "Q001"
            assert result.generation_status == "success"
            assert result.validation_allowed is True
            assert result.execution_status == "success"
            assert result.result_matches_reference is True
            assert result.semantic_status == SemanticStatus.CORRECT
            assert result.failure_stage is None

    def test_evaluate_answerable_validation_failure(self, mock_schema: DatabaseSchema):
        case = {
            "id": "Q002",
            "category": "simple_retrieval",
            "question": "Query unknown table",
            "expected_behavior": "answerable",
            "reference_sql": "SELECT 1;",
            "reference_result": [{"1": 1}],
            "notes": "Invalid query",
        }
        gen = MockSQLGenerator(sql="SELECT * FROM NonExistentTable;")

        result = evaluate_case(case=case, generator=gen, schema=mock_schema, engine=None)

        assert result.validation_allowed is False
        assert result.execution_status == "not_executed"
        assert result.failure_stage == FailureStage.VALIDATION
        assert result.semantic_status == SemanticStatus.INCORRECT

    def test_evaluate_answerable_semantic_mismatch(self, mock_schema: DatabaseSchema):
        case = {
            "id": "Q003",
            "category": "simple_retrieval",
            "question": "List students",
            "expected_behavior": "answerable",
            "reference_sql": "SELECT Id FROM Students;",
            "reference_result": [{"Id": 1}],
            "notes": "Query with wrong filter",
        }
        gen = MockSQLGenerator(sql="SELECT Id FROM Students WHERE Id = 999;")
        mock_exec = QueryResult(
            columns=["Id"],
            rows=[],  # Empty rows returned instead of expected
            row_count=0,
            truncated=False,
            execution_time_ms=2.0,
        )

        with patch("query_pilot.eval.evaluator.execute_read_only", return_value=mock_exec):
            result = evaluate_case(case=case, generator=gen, schema=mock_schema, engine=None)

            assert result.validation_allowed is True
            assert result.execution_status == "success"
            assert result.result_matches_reference is False
            assert result.semantic_status == SemanticStatus.INCORRECT
            assert result.failure_stage == FailureStage.SEMANTIC

    def test_evaluate_unsupported_hallucinated_table(self, mock_schema: DatabaseSchema):
        case = {
            "id": "Q024",
            "category": "unsupported",
            "question": "What is student tuition fee?",
            "expected_behavior": "unsupported",
            "reference_sql": None,
            "reference_result": None,
            "notes": "Tuition fees unsupported",
        }
        gen = MockSQLGenerator(sql="SELECT FeeAmount FROM TuitionFees;")

        result = evaluate_case(case=case, generator=gen, schema=mock_schema, engine=None)

        assert result.validation_allowed is False
        assert result.semantic_status == SemanticStatus.HALLUCINATED_UNSUPPORTED
        assert result.failure_stage == FailureStage.VALIDATION

    def test_evaluate_unsupported_correctly_refused(self, mock_schema: DatabaseSchema):
        case = {
            "id": "Q025",
            "category": "unsupported",
            "question": "What is student tuition fee?",
            "expected_behavior": "unsupported",
            "reference_sql": None,
            "reference_result": None,
            "notes": "Tuition fees unsupported",
        }
        gen = MockSQLGenerator(
            sql="SELECT 'Tuition fees are not tracked in the database' AS ErrorMessage;",
            explanation="Tuition fees are not supported by the schema.",
        )

        result = evaluate_case(case=case, generator=gen, schema=mock_schema, engine=None)

        assert result.validation_allowed is True
        assert result.semantic_status == SemanticStatus.CORRECTLY_REFUSED
        assert result.failure_stage is None


# ==============================================================================
# Unit Tests — Metrics Calculation
# ==============================================================================

class TestMetricsCalculation:
    """Verifies aggregate metric formulas and rate calculations."""

    def test_compute_evaluation_metrics(self):
        mock_results = [
            # 1 Answerable Success
            CaseEvaluationResult(
                case_id="Q001",
                category="simple_retrieval",
                expected_behavior="answerable",
                generation_status="success",
                validation_allowed=True,
                execution_status="success",
                result_matches_reference=True,
                semantic_status=SemanticStatus.CORRECT,
                failure_stage=None,
            ),
            # 1 Answerable Validation Failure
            CaseEvaluationResult(
                case_id="Q002",
                category="simple_retrieval",
                expected_behavior="answerable",
                generation_status="success",
                validation_allowed=False,
                execution_status="not_executed",
                result_matches_reference=False,
                semantic_status=SemanticStatus.INCORRECT,
                failure_stage=FailureStage.VALIDATION,
            ),
            # 1 Semantic Trap Correct
            CaseEvaluationResult(
                case_id="Q028",
                category="semantic_trap",
                expected_behavior="semantic_trap",
                generation_status="success",
                validation_allowed=True,
                execution_status="success",
                result_matches_reference=True,
                semantic_status=SemanticStatus.CORRECT,
                failure_stage=None,
            ),
            # 1 Unsupported Correctly Refused
            CaseEvaluationResult(
                case_id="Q024",
                category="unsupported",
                expected_behavior="unsupported",
                generation_status="success",
                validation_allowed=True,
                execution_status="not_executed",
                semantic_status=SemanticStatus.CORRECTLY_REFUSED,
                failure_stage=None,
            ),
            # 1 Safety Correctly Rejected
            CaseEvaluationResult(
                case_id="S001",
                category="safety",
                expected_behavior="rejected",
                generation_status="not_applicable",
                validation_allowed=False,
                execution_status="not_executed",
                semantic_status=SemanticStatus.CORRECT,
                failure_stage=None,
            ),
        ]

        metrics = compute_evaluation_metrics(mock_results)

        assert metrics.total_cases == 5
        assert metrics.total_nl_cases == 4
        assert metrics.generation_success_count == 4
        assert metrics.generation_success_rate == 1.0

        assert metrics.validator_accepted_count == 3  # Q001, Q028, Q024
        assert metrics.validator_acceptance_rate == 0.75

        assert metrics.execution_attempted_count == 2  # Q001, Q028 (Q024 was unsupported)
        assert metrics.execution_success_count == 2
        assert metrics.execution_success_rate == 1.0

        assert metrics.answerable_cases_count == 2  # Q001, Q002
        assert metrics.exact_match_count == 1
        assert metrics.exact_match_rate == 0.5
        assert metrics.semantic_correct_count == 1
        assert metrics.semantic_correct_rate == 0.5

        assert metrics.semantic_trap_count == 1
        assert metrics.semantic_trap_correct_count == 1
        assert metrics.semantic_trap_correct_rate == 1.0

        assert metrics.unsupported_count == 1
        assert metrics.unsupported_correct_count == 1
        assert metrics.unsupported_recognition_rate == 1.0

        assert metrics.safety_fixture_count == 1
        assert metrics.safety_rejected_count == 1
        assert metrics.safety_rejection_rate == 1.0


# ==============================================================================
# Unit Tests — Task 1.12-R Classification Logic
# ==============================================================================

class TestReclassificationLogic:
    """Verifies Task 1.12-R semantic reclassification rules."""

    def test_alias_only_difference_not_semantic_failure(self, mock_schema: DatabaseSchema):
        """Verify different column aliases alone do NOT constitute semantic failure."""
        case = {
            "id": "Q007",
            "category": "aggregation",
            "question": "How many active course offerings currently exist?",
            "expected_behavior": "answerable",
            "reference_sql": "SELECT COUNT(*) AS ActiveOfferingsCount FROM CourseOfferings WHERE IsActive = 1;",
            "reference_result": [{"ActiveOfferingsCount": 10}],
            "notes": "Testing alias difference",
        }
        # Generated SQL uses different alias 'ActiveCourseOfferingsCount'
        gen = MockSQLGenerator(sql="SELECT COUNT(*) AS ActiveCourseOfferingsCount FROM CourseOfferings WHERE IsActive = 1;")
        mock_exec = QueryResult(
            columns=["ActiveCourseOfferingsCount"],
            rows=[{"ActiveCourseOfferingsCount": 10}],
            row_count=1,
            truncated=False,
            execution_time_ms=1.5,
        )

        with patch("query_pilot.eval.evaluator.execute_read_only", return_value=mock_exec):
            result = evaluate_case(case=case, generator=gen, schema=mock_schema, engine=None)

            assert result.validation_allowed is True
            assert result.execution_status == "success"
            assert result.result_matches_reference is False  # Exact match is False due to alias key
            assert result.exact_result_match is False
            assert result.semantic_correct is True  # Semantically correct
            assert result.mismatch_classification == MismatchClassification.SEMANTIC_CORRECT_EXACT_DIFFERENCE.value
            assert result.semantic_status == SemanticStatus.CORRECT
            assert result.failure_stage is None

    def test_projection_difference_semantically_correct(self, mock_schema: DatabaseSchema):
        """Verify harmless projection difference preserves semantic correctness."""
        case = {
            "id": "Q003",
            "category": "simple_retrieval",
            "question": "Retrieve all students who have a CGPA of 9.0 or higher ordered by CGPA descending.",
            "expected_behavior": "answerable",
            "reference_sql": "SELECT Id, EnrollmentNumber, CGPA FROM Students WHERE CGPA >= 9.0 ORDER BY CGPA DESC, Id ASC;",
            "reference_result": [{"Id": 38, "EnrollmentNumber": "21CE017", "CGPA": 9.34}],
            "notes": "Testing harmless extra projection columns",
        }
        gen = MockSQLGenerator(
            sql="SELECT s.Id, s.EnrollmentNumber, s.CGPA, u.FullName FROM Students s JOIN Users u ON s.Id = u.Id WHERE s.CGPA >= 9.0 ORDER BY s.CGPA DESC;"
        )
        mock_exec = QueryResult(
            columns=["Id", "EnrollmentNumber", "CGPA", "FullName"],
            rows=[{"Id": 38, "EnrollmentNumber": "21CE017", "CGPA": 9.34, "FullName": "Reyansh Raval"}],
            row_count=1,
            truncated=False,
            execution_time_ms=2.0,
        )

        with patch("query_pilot.eval.evaluator.execute_read_only", return_value=mock_exec):
            result = evaluate_case(case=case, generator=gen, schema=mock_schema, engine=None)

            assert result.validation_allowed is True
            assert result.result_matches_reference is False
            assert result.semantic_correct is True
            assert result.mismatch_classification == MismatchClassification.SEMANTIC_CORRECT_EXACT_DIFFERENCE.value
            assert result.failure_stage is None

    def test_semantic_traps_separate_exact_and_semantic_status(self, mock_schema: DatabaseSchema):
        """Verify semantic traps track structural validity, execution, exact match, and semantic correctness independently."""
        case = {
            "id": "Q028",
            "category": "semantic_trap",
            "question": "List the first 5 students with their enrollment number and full name ordered by student ID.",
            "expected_behavior": "answerable",
            "reference_sql": "SELECT TOP 5 s.Id AS StudentId, s.EnrollmentNumber, u.FullName FROM Students s JOIN Users u ON s.Id = u.Id ORDER BY s.Id ASC;",
            "reference_result": [{"StudentId": 22, "EnrollmentNumber": "21CE001", "FullName": "Priya Kothari"}],
            "notes": "Testing trap classification with omitted Id",
        }
        gen = MockSQLGenerator(sql="SELECT TOP 5 s.EnrollmentNumber, u.FullName FROM Students s JOIN Users u ON s.Id = u.Id ORDER BY s.Id ASC;")
        mock_exec = QueryResult(
            columns=["EnrollmentNumber", "FullName"],
            rows=[{"EnrollmentNumber": "21CE001", "FullName": "Priya Kothari"}],
            row_count=1,
            truncated=False,
            execution_time_ms=1.8,
        )

        with patch("query_pilot.eval.evaluator.execute_read_only", return_value=mock_exec):
            result = evaluate_case(case=case, generator=gen, schema=mock_schema, engine=None)

            assert result.structurally_valid is True
            assert result.validator_accepted is True
            assert result.executed is True
            assert result.exact_result_match is False
            assert result.semantic_correct is True
            assert result.mismatch_classification == MismatchClassification.SEMANTIC_CORRECT_EXACT_DIFFERENCE.value
            assert result.failure_stage is None

    def test_unsupported_cases_tracked_separately(self):
        """Verify unsupported cases are excluded from answerable denominator and tracked via unsupported metrics."""
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
                semantic_correct=True,
            ),
            CaseEvaluationResult(
                case_id="Q024",
                category="unsupported",
                expected_behavior="unsupported",
                generation_status="success",
                validation_allowed=True,
                execution_status="not_executed",
                result_matches_reference=None,
                exact_result_match=None,
                semantic_correct=None,
                unsupported_classification="incorrectly_attempted",
            ),
        ]
        metrics = compute_evaluation_metrics(results)
        assert metrics.answerable_cases_count == 1  # Q024 excluded from answerable count!
        assert metrics.exact_match_count == 1
        assert metrics.semantic_correct_count == 1
        assert metrics.unsupported_count == 1
        assert metrics.unsupported_correct_count == 0

    def test_safety_fixtures_remain_blocked(self, mock_schema: DatabaseSchema):
        """Verify safety fixtures are blocked by validator and executor is never invoked."""
        case = {
            "id": "S002",
            "category": "safety",
            "candidate_sql": "DROP TABLE Students;",
            "expected_behavior": "rejected",
            "expected_reason": "NOT_READ_ONLY",
        }
        gen = MockSQLGenerator(sql="NOT_USED")
        result = evaluate_case(case=case, generator=gen, schema=mock_schema, engine=None)

        assert result.validation_allowed is False
        assert result.execution_status == "not_executed"
        assert result.executed is False
        assert result.semantic_correct is None
        assert result.mismatch_classification == MismatchClassification.SAFETY_FIXTURE.value
        assert len(gen.calls) == 0

    def test_stored_baseline_results_invariance_and_classification(self):
        """Verify stored baseline results have exactly 32 cases and expected metrics."""
        import json
        from pathlib import Path
        results_path = Path("data/evaluation/baseline_results.json")
        assert results_path.exists()

        with open(results_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        assert len(data["cases"]) == 32
        metrics = data["metrics"]
        assert metrics["total_cases"] == 32
        assert metrics["total_nl_cases"] == 30
        assert metrics["answerable_cases_count"] == 26
        assert metrics["exact_match_count"] == 11
        assert metrics["semantic_correct_count"] == 26
        assert metrics["semantic_trap_count"] == 3
        assert metrics["semantic_trap_correct_count"] == 3
        assert metrics["semantic_trap_exact_match_count"] == 1
        assert metrics["unsupported_count"] == 4
        assert metrics["safety_fixture_count"] == 2
        assert metrics["safety_rejected_count"] == 2

    def test_evaluate_case_structured_unsupported(self, mock_schema: DatabaseSchema):
        """Verify evaluate_case returns structured refusal without calling validator or executor."""
        case = {
            "id": "Q024",
            "category": "unsupported",
            "question": "What is the tuition fee amount paid by each student?",
            "expected_behavior": "unsupported",
        }

        class UnsupportedMockGenerator(SQLGenerator):
            def generate(self, req: SQLGenerationRequest) -> SQLGenerationResponse:
                return SQLGenerationResponse(
                    status=SQLGenerationStatus.UNSUPPORTED,
                    sql=None,
                    explanation="No tuition fee in schema.",
                )

        gen = UnsupportedMockGenerator()
        with (
            patch("query_pilot.eval.evaluator.validate_sql") as mock_val,
            patch("query_pilot.eval.evaluator.execute_read_only") as mock_exec,
        ):
            res = evaluate_case(case=case, generator=gen, schema=mock_schema, engine=None)

            assert res.generated_sql is None
            assert res.semantic_status == SemanticStatus.CORRECTLY_REFUSED
            assert res.semantic_correct is True
            assert res.unsupported_classification == "correctly_unsupported"
            assert res.executed is False
            assert res.execution_status == "not_executed"
            assert res.failure_stage is None
            mock_val.assert_not_called()
            mock_exec.assert_not_called()

    def test_task_1_14_results_and_regression_metrics(self):
        """Verify Task 1.14 evaluation artifacts, structured refusals, and regression invariance."""
        import json
        from pathlib import Path

        # Verify output artifacts exist
        results_1_14_path = Path("data/evaluation/baseline_results_1_14.json")
        report_1_14_path = Path("data/evaluation/baseline_report_1_14.md")
        assert results_1_14_path.exists(), "baseline_results_1_14.json must exist"
        assert report_1_14_path.exists(), "baseline_report_1_14.md must exist"

        # Verify original Task 1.12 artifacts remained intact
        results_1_12_path = Path("data/evaluation/baseline_results.json")
        report_1_12_path = Path("data/evaluation/baseline_report.md")
        assert results_1_12_path.exists(), "baseline_results.json must remain intact"
        assert report_1_12_path.exists(), "baseline_report.md must remain intact"

        with open(results_1_14_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        assert len(data["cases"]) == 32
        metrics = data["metrics"]
        assert metrics["total_cases"] == 32
        assert metrics["total_nl_cases"] == 30

        # Answerable cases: no regression (26/26 semantic correctness)
        assert metrics["answerable_cases_count"] == 26
        assert metrics["semantic_correct_count"] == 26
        assert metrics["semantic_correct_rate"] == 1.0

        # Exact matches: maintained/improved (12/26)
        assert metrics["exact_match_count"] >= 11

        # Unsupported recognition: 4/4 structured refusals
        assert metrics["unsupported_count"] == 4
        assert metrics["unsupported_correct_count"] == 4
        assert metrics["unsupported_recognition_rate"] == 1.0

        # Verify Q024-Q027 have sql is None and were never executed
        unsupported_cases = [c for c in data["cases"] if c["category"] == "unsupported"]
        assert len(unsupported_cases) == 4
        for c in unsupported_cases:
            assert c["generated_sql"] is None
            assert c["execution_status"] == "not_executed"
            assert c["executed"] is False
            assert c["unsupported_classification"] == "correctly_unsupported"
            assert c["semantic_status"] == "correctly_refused"

        # Semantic traps: 3/3
        assert metrics["semantic_trap_count"] == 3
        assert metrics["semantic_trap_correct_count"] == 3

        # Safety: 2/2 blocked
        assert metrics["safety_fixture_count"] == 2
        assert metrics["safety_rejected_count"] == 2

