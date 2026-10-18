"""Comprehensive unit and integration tests for Conservative Dynamic Semantic Evaluator.

Verifies:
- Exact equality (identical rows, casing, fast-path).
- Harmless column reordering and alias compatibility.
- Rejection of conflicting entities despite matching counts.
- Rejection of mismatched aggregate values (Q012 regression guard).
- Projection superset support and missing column rejection.
- Row multiplicity (multiset/bag semantics).
- Order-sensitive enforcement vs. order-insensitive unconstrained multisets.
- Numeric equivalence (int, float, Decimal) without broad tolerances.
- NULL-to-NULL equivalence and NULL vs. non-NULL rejection.
- Date and datetime parsing and boundary distinctions.
- Inconclusive outcomes on missing data or ambiguous column mappings.
- Integration test proving historical BASELINE_CLASSIFICATIONS cannot override dynamic mismatches.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List
import pytest

from query_pilot.eval.evaluator import BASELINE_CLASSIFICATIONS, evaluate_case
from query_pilot.eval.models import (
    CaseEvaluationResult,
    FailureStage,
    MismatchClassification,
    SemanticStatus,
)
from query_pilot.eval.semantic import (
    SemanticComparisonResult,
    SemanticOutcome,
    are_column_aliases_compatible,
    compare_results_semantically,
    is_order_constrained,
    values_are_equal,
)
from query_pilot.sql.generation import SQLGenerationRequest, SQLGenerationResponse, SQLGenerator


# ==============================================================================
# 1. Exact Equality
# ==============================================================================

def test_exact_equality_identical_rows():
    """Verify identical column names, ordering, and values produce EXACT_MATCH."""
    rows = [{"Id": 1, "Name": "Computer Engineering"}, {"Id": 2, "Name": "Mechanical Engineering"}]
    res = compare_results_semantically(rows, rows)
    assert res.outcome == SemanticOutcome.EXACT_MATCH
    assert res.is_correct is True
    assert "Exact" in res.reason


def test_exact_equality_case_insensitive_column_casing():
    """Verify case differences in column names (Id vs id) are treated as EXACT_MATCH."""
    gen = [{"id": 1, "fullname": "Alice"}, {"id": 2, "fullname": "Bob"}]
    ref = [{"Id": 1, "FullName": "Alice"}, {"Id": 2, "FullName": "Bob"}]
    res = compare_results_semantically(gen, ref)
    assert res.outcome == SemanticOutcome.EXACT_MATCH
    assert res.is_correct is True


# ==============================================================================
# 2. Column Reordering and Aliases
# ==============================================================================

def test_harmless_column_reordering():
    """Verify columns in different order with matching values produce EQUIVALENT_MATCH."""
    gen = [{"FullName": "Alice", "Id": 1}, {"FullName": "Bob", "Id": 2}]
    ref = [{"Id": 1, "FullName": "Alice"}, {"Id": 2, "FullName": "Bob"}]
    res = compare_results_semantically(gen, ref)
    assert res.outcome == SemanticOutcome.EQUIVALENT_MATCH
    assert res.is_correct is True
    assert res.column_mapping == {"Id": "Id", "FullName": "FullName"}


def test_clearly_compatible_aliases_single_column():
    """Verify compatible domain aliases (TotalRegisteredStudents vs TotalStudents) pass."""
    gen = [{"TotalRegisteredStudents": 40}]
    ref = [{"TotalStudents": 40}]
    res = compare_results_semantically(gen, ref)
    assert res.outcome == SemanticOutcome.EQUIVALENT_MATCH
    assert res.is_correct is True
    assert res.column_mapping == {"TotalStudents": "TotalRegisteredStudents"}


def test_clearly_compatible_aliases_multiple_columns():
    """Verify multiple compatible column aliases pass with correct alignment."""
    gen = [{"LowestMarks": 0, "HighestMarks": 9.64}]
    ref = [{"MinMarks": 0, "MaxMarks": 9.64}]
    res = compare_results_semantically(gen, ref)
    assert res.outcome == SemanticOutcome.EQUIVALENT_MATCH
    assert res.is_correct is True
    assert res.column_mapping == {"MinMarks": "LowestMarks", "MaxMarks": "HighestMarks"}


def test_clearly_compatible_aliases_synonyms():
    """Verify common abbreviation synonyms (avg vs average, dept vs department) pass."""
    gen = [{"AvgScore": 85.5, "DeptName": "CE"}]
    ref = [{"AverageScore": 85.5, "DepartmentName": "CE"}]
    res = compare_results_semantically(gen, ref)
    assert res.outcome == SemanticOutcome.EQUIVALENT_MATCH
    assert res.is_correct is True


def test_ambiguous_aliases_returns_inconclusive():
    """Verify ambiguous candidate column mappings produce INCONCLUSIVE outcome."""
    # Reference expects Val, generated has ValA and ValB both with identical compatible names and distinct matching values
    gen = [{"ScoreA": 10, "ScoreB": 20}]
    ref = [{"Score": 10}]
    # Both ScoreA and ScoreB are compatible with Score, but ScoreA=10 matches while ScoreB=20 does not.
    # What if both matched different rows or multiple mappings exist?
    gen_ambig = [{"MetricA": 100, "MetricB": 100}]
    ref_ambig = [{"Metric": 100}]
    res = compare_results_semantically(gen_ambig, ref_ambig)
    # Both MetricA and MetricB match 100; ambiguous binding
    assert res.outcome == SemanticOutcome.INCONCLUSIVE
    assert res.is_correct is False
    assert "Ambiguous" in res.reason


# ==============================================================================
# 3. Projection Supersets and Missing Columns
# ==============================================================================

def test_additional_projected_columns_superset():
    """Verify generated query returning all reference columns plus extras passes as superset."""
    gen = [{"Id": 38, "EnrollmentNumber": "21CE017", "CGPA": 9.34, "Status": "Active"}]
    ref = [{"Id": 38, "EnrollmentNumber": "21CE017", "CGPA": 9.34}]
    res = compare_results_semantically(gen, ref, allow_superset=True)
    assert res.outcome == SemanticOutcome.EQUIVALENT_MATCH
    assert res.is_correct is True
    assert "superset" in res.reason.lower()


def test_additional_projected_columns_disallowed():
    """Verify projection superset is rejected when allow_superset=False."""
    gen = [{"Id": 38, "EnrollmentNumber": "21CE017", "CGPA": 9.34, "Status": "Active"}]
    ref = [{"Id": 38, "EnrollmentNumber": "21CE017", "CGPA": 9.34}]
    res = compare_results_semantically(gen, ref, allow_superset=False)
    assert res.outcome == SemanticOutcome.MISMATCH
    assert res.is_correct is False


def test_missing_required_columns_fails():
    """Verify generated query missing required reference columns is rejected."""
    gen = [{"FullName": "Priya Kothari", "DepartmentName": "Computer Engineering"}]
    ref = [{"Id": 22, "FullName": "Priya Kothari", "DepartmentName": "Computer Engineering"}]
    res = compare_results_semantically(gen, ref)
    assert res.outcome == SemanticOutcome.MISMATCH
    assert res.is_correct is False
    assert "Missing required reference columns" in res.reason


# ==============================================================================
# 4. Duplicate Rows and Multiplicity
# ==============================================================================

def test_duplicate_rows_preserved_multiset():
    """Verify multiset matching respects identical duplicate multiplicity."""
    rows = [{"DeptId": 1}, {"DeptId": 1}, {"DeptId": 2}]
    res = compare_results_semantically(rows, rows)
    assert res.outcome == SemanticOutcome.EXACT_MATCH
    assert res.is_correct is True


def test_duplicate_rows_multiplicity_mismatch():
    """Verify multiset matching rejects when duplicate row multiplicity differs."""
    gen = [{"DeptId": 1}, {"DeptId": 2}, {"DeptId": 2}]  # Two 2s, one 1
    ref = [{"DeptId": 1}, {"DeptId": 1}, {"DeptId": 2}]  # Two 1s, one 2
    res = compare_results_semantically(gen, ref, order_sensitive=False)
    assert res.outcome == SemanticOutcome.MISMATCH
    assert res.is_correct is False


# ==============================================================================
# 5. Row Ordering Constraints
# ==============================================================================

def test_ordered_rows_order_sensitive_match():
    """Verify ordered rows matching natural order pass with order_sensitive=True."""
    gen = [{"Id": 1}, {"Id": 2}, {"Id": 3}]
    ref = [{"Id": 1}, {"Id": 2}, {"Id": 3}]
    res = compare_results_semantically(gen, ref, order_sensitive=True)
    assert res.outcome == SemanticOutcome.EXACT_MATCH
    assert res.is_correct is True


def test_ordered_rows_order_sensitive_violation():
    """Verify out-of-order rows fail when order_sensitive=True or question specifies ORDER BY."""
    gen = [{"Id": 3}, {"Id": 1}, {"Id": 2}]
    ref = [{"Id": 1}, {"Id": 2}, {"Id": 3}]
    res = compare_results_semantically(
        gen, ref, order_sensitive=True, question="List users ordered by id ascending."
    )
    assert res.outcome == SemanticOutcome.MISMATCH
    assert res.is_correct is False
    assert "ordering violation" in res.reason.lower()


def test_unordered_rows_multiset_match_unconstrained():
    """Verify unconstrained grouping/retrieval query matches as multiset when order is arbitrary."""
    gen = [
        {"Status": "Excused", "RecordCount": 183},
        {"Status": "Late", "RecordCount": 425},
        {"Status": "Absent", "RecordCount": 993},
        {"Status": "Present", "RecordCount": 4399},
    ]
    ref = [
        {"Status": "Absent", "RecordCount": 993},
        {"Status": "Excused", "RecordCount": 183},
        {"Status": "Late", "RecordCount": 425},
        {"Status": "Present", "RecordCount": 4399},
    ]
    res = compare_results_semantically(
        gen,
        ref,
        reference_sql="SELECT Status, COUNT(*) AS RecordCount FROM AttendanceRecords GROUP BY Status;",
        question="Count the number of attendance records for each attendance status.",
    )
    assert res.outcome == SemanticOutcome.EQUIVALENT_MATCH
    assert res.is_correct is True
    assert "multiset" in res.reason.lower()


def test_unspecified_order_constraint_returns_inconclusive():
    """Verify rows matching only out-of-order return INCONCLUSIVE when order requirement is unknown."""
    gen = [{"Id": 2}, {"Id": 1}]
    ref = [{"Id": 1}, {"Id": 2}]
    # Neither order_sensitive, reference_sql, nor question provided
    res = compare_results_semantically(gen, ref, order_sensitive=None)
    assert res.outcome == SemanticOutcome.INCONCLUSIVE
    assert res.is_correct is False
    assert "Ambiguous ordering constraint" in res.reason


# ==============================================================================
# 6. Numeric Normalization and Tolerance
# ==============================================================================

def test_integer_float_decimal_equivalence():
    """Verify 40 == 40.0 == Decimal('40.00') without broad tolerance."""
    assert values_are_equal(40, 40.0) is True
    assert values_are_equal(40.0, Decimal("40.00")) is True
    assert values_are_equal(Decimal("40"), 40) is True

    gen = [{"Count": 40.0}]
    ref = [{"Count": 40}]
    res = compare_results_semantically(gen, ref)
    assert res.outcome == SemanticOutcome.EXACT_MATCH
    assert res.is_correct is True


def test_meaningful_numeric_differences_rejected():
    """Verify meaningful numeric differences (40 vs 40.5) are strictly rejected."""
    assert values_are_equal(40, 40.5) is False
    gen = [{"Count": 40.5}]
    ref = [{"Count": 40}]
    res = compare_results_semantically(gen, ref)
    assert res.outcome == SemanticOutcome.MISMATCH
    assert res.is_correct is False


def test_small_numeric_rounding_difference_rejected():
    """Verify floating-point rounding difference (8.71 vs 8.706666) is rejected."""
    assert values_are_equal(8.71, 8.706666666666669) is False
    gen = [{"Average": 8.706666666666669}]
    ref = [{"Average": 8.71}]
    res = compare_results_semantically(gen, ref)
    assert res.outcome == SemanticOutcome.MISMATCH
    assert res.is_correct is False


# ==============================================================================
# 7. NULL Handling and Boolean Distinction
# ==============================================================================

def test_null_to_null_equivalence():
    """Verify NULL is equal to NULL."""
    assert values_are_equal(None, None) is True
    gen = [{"Id": 1, "Notes": None}]
    ref = [{"Id": 1, "Notes": None}]
    res = compare_results_semantically(gen, ref)
    assert res.outcome == SemanticOutcome.EXACT_MATCH
    assert res.is_correct is True


def test_null_versus_non_null_mismatch():
    """Verify NULL is NOT equal to 0, empty string, or False."""
    assert values_are_equal(None, 0) is False
    assert values_are_equal(None, "") is False
    assert values_are_equal(None, False) is False

    gen = [{"Id": 1, "Score": 0}]
    ref = [{"Id": 1, "Score": None}]
    res = compare_results_semantically(gen, ref)
    assert res.outcome == SemanticOutcome.MISMATCH
    assert res.is_correct is False


def test_boolean_versus_integer_distinction():
    """Verify boolean True/False is not silently coerced to int 1/0."""
    assert values_are_equal(True, 1) is False
    assert values_are_equal(False, 0) is False
    assert values_are_equal(True, True) is True


# ==============================================================================
# 8. Date and Datetime Comparison
# ==============================================================================

def test_date_and_datetime_iso_equivalence():
    """Verify ISO strings, date, and datetime with 00:00:00 time match."""
    dt_str = "2024-07-15T00:00:00"
    d_obj = date(2024, 7, 15)
    dt_obj = datetime(2024, 7, 15, 0, 0, 0)
    assert values_are_equal(dt_str, d_obj) is True
    assert values_are_equal(d_obj, dt_obj) is True

    gen = [{"AdmissionDate": "2024-07-15T00:00:00"}]
    ref = [{"AdmissionDate": date(2024, 7, 15)}]
    res = compare_results_semantically(gen, ref)
    assert res.outcome == SemanticOutcome.EXACT_MATCH
    assert res.is_correct is True


def test_date_boundary_difference_rejected():
    """Verify intraday timestamps (midnight vs end of day) are not equal."""
    d1 = "2024-09-30T00:00:00"
    d2 = "2024-09-30T23:59:59"
    assert values_are_equal(d1, d2) is False

    gen = [{"Cutoff": d1}]
    ref = [{"Cutoff": d2}]
    res = compare_results_semantically(gen, ref)
    assert res.outcome == SemanticOutcome.MISMATCH
    assert res.is_correct is False


# ==============================================================================
# 9. Adversarial Entity and Value Mismatch Checks
# ==============================================================================

def test_different_entities_with_identical_row_counts_rejected():
    """Verify conflicting business entities with identical counts (TotalFaculties=7 vs TotalStudents=7) fail."""
    assert are_column_aliases_compatible("TotalFaculties", "TotalStudents") is False
    gen = [{"TotalFaculties": 7}]
    ref = [{"TotalStudents": 7}]
    res = compare_results_semantically(gen, ref)
    assert res.outcome == SemanticOutcome.MISMATCH
    assert res.is_correct is False


def test_different_entities_multiple_rows_rejected():
    """Verify department names vs student names with matching row count fails."""
    assert are_column_aliases_compatible("DepartmentName", "StudentName") is False
    gen = [{"DepartmentName": "Computer Engineering"}]
    ref = [{"StudentName": "Computer Engineering"}]
    res = compare_results_semantically(gen, ref)
    assert res.outcome == SemanticOutcome.MISMATCH
    assert res.is_correct is False


def test_different_aggregate_values_identical_row_counts_rejected():
    """Verify Q012 regression: StudentCount=200 vs StatusCount=40 is strictly rejected as MISMATCH."""
    gen = [{"Status": "Active", "StudentCount": 200}]
    ref = [{"Status": "Active", "StatusCount": 40}]
    res = compare_results_semantically(gen, ref)
    assert res.outcome == SemanticOutcome.MISMATCH
    assert res.is_correct is False
    assert "values do not match" in res.reason.lower()


# ==============================================================================
# 10. Edge Cases: Empty Result Sets & Invalid Inputs
# ==============================================================================

def test_empty_result_sets_both_empty():
    """Verify both result sets empty is EXACT_MATCH."""
    res = compare_results_semantically([], [])
    assert res.outcome == SemanticOutcome.EXACT_MATCH
    assert res.is_correct is True


def test_empty_result_sets_one_empty_mismatch():
    """Verify one empty and one non-empty is MISMATCH."""
    res = compare_results_semantically([], [{"Id": 1}])
    assert res.outcome == SemanticOutcome.MISMATCH
    assert res.is_correct is False


def test_inconclusive_when_generated_or_reference_is_none():
    """Verify None input produces INCONCLUSIVE outcome."""
    res1 = compare_results_semantically(None, [{"Id": 1}])
    assert res1.outcome == SemanticOutcome.INCONCLUSIVE
    assert res1.is_correct is False

    res2 = compare_results_semantically([{"Id": 1}], None)
    assert res2.outcome == SemanticOutcome.INCONCLUSIVE
    assert res2.is_correct is False


def test_invalid_input_type_returns_inconclusive():
    """Verify non-list inputs return INCONCLUSIVE."""
    res = compare_results_semantically("not a list", [{"Id": 1}])  # type: ignore[arg-type]
    assert res.outcome == SemanticOutcome.INCONCLUSIVE
    assert res.is_correct is False


# ==============================================================================
# 11. Integration Test: BASELINE_CLASSIFICATIONS Cannot Override Dynamic Mismatches
# ==============================================================================

class FakeGenerator(SQLGenerator):
    """Fake generator returning preset SQL."""

    def __init__(self, sql: str):
        self.sql = sql

    def generate(self, request: Any) -> Any:
        return SQLGenerationResponse(status="answerable", sql=self.sql)


def test_historical_metadata_cannot_turn_dynamic_mismatch_into_pass(monkeypatch):
    """Verify that having an entry in BASELINE_CLASSIFICATIONS cannot force a pass on a dynamic mismatch."""
    from unittest.mock import MagicMock, patch
    from query_pilot.db.executor import QueryResult
    from query_pilot.db.models import ColumnMetadata, DatabaseSchema, TableMetadata

    schema = DatabaseSchema(
        database_name="GradeSense_Local",
        tables=[
            TableMetadata(
                schema_name="dbo",
                table_name="Students",
                columns=[
                    ColumnMetadata(name="Id", data_type="int", nullable=False, primary_key=True),
                    ColumnMetadata(name="Status", data_type="varchar(50)", nullable=False),
                ],
                primary_keys=["Id"],
            )
        ],
        is_model_facing=True,
    )

    # Q012 in BASELINE_CLASSIFICATIONS had SEMANTIC_CORRECT_EXACT_DIFFERENCE in historical snapshot
    assert "Q012" in BASELINE_CLASSIFICATIONS
    assert BASELINE_CLASSIFICATIONS["Q012"]["mismatch"] == MismatchClassification.SEMANTIC_CORRECT_EXACT_DIFFERENCE

    case = {
        "id": "Q012",
        "category": "grouping",
        "question": "Show count of active students.",
        "expected_behavior": "answerable",
        "reference_sql": "SELECT Status, COUNT(*) AS StatusCount FROM Students WHERE Status = 'Active' GROUP BY Status;",
        "reference_result": [{"Status": "Active", "StatusCount": 40}],
    }

    # Candidate query returns StudentCount: 200 (mismatched value!)
    gen = FakeGenerator(sql="SELECT Status, COUNT(*) AS StudentCount FROM Students GROUP BY Status;")
    mock_exec = QueryResult(
        columns=["Status", "StudentCount"],
        rows=[{"Status": "Active", "StudentCount": 200}],  # 200 != 40!
        row_count=1,
        truncated=False,
        execution_time_ms=1.5,
    )

    with patch("query_pilot.eval.evaluator.execute_read_only", return_value=mock_exec):
        result = evaluate_case(case=case, generator=gen, schema=schema, engine=None)

        # Under dynamic evaluation, this MUST be rejected as False!
        assert result.semantic_correct is False
        assert result.semantic_status == SemanticStatus.INCORRECT
        assert result.mismatch_classification == MismatchClassification.SEMANTIC_INCORRECT.value
        assert result.failure_stage == FailureStage.SEMANTIC
