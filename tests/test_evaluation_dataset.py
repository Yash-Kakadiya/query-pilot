"""Test suite verifying the baseline evaluation dataset integrity and reproducibility (Task 1.11).

Verifies:
1. Dataset structure: unique IDs, required fields, allowed categories, no duplicate questions.
2. Safety cases: rejected deterministically by the Task 1.8 validator and never executed.
3. Unsupported cases: correctly flagged with null reference SQL/results.
4. Answerable cases: pass Task 1.8 validation, read-only policy, and reference no sensitive fields.
5. Reference execution: reference SQL executed against GradeSense_Local matches stored reference results.
"""

import json
from pathlib import Path
from typing import Any, Dict, List
import pytest

from query_pilot.db.connection import create_db_engine
from query_pilot.db.executor import execute_read_only
from query_pilot.db.introspection import DatabaseIntrospector
from query_pilot.db.models import DatabaseSchema
from query_pilot.sql.validator import validate_sql

DATASET_PATH = Path(__file__).resolve().parent.parent / "data" / "evaluation" / "baseline_questions.json"

ALLOWED_CATEGORIES = frozenset({
    "simple_retrieval",
    "aggregation",
    "grouping",
    "multi_table_join",
    "analytical",
    "datetime",
    "unsupported",
    "semantic_trap",
    "safety",
})

ALLOWED_BEHAVIORS = frozenset({
    "answerable",
    "unsupported",
    "semantic_trap",
    "rejected",
})


@pytest.fixture(scope="module")
def evaluation_cases() -> List[Dict[str, Any]]:
    """Load and parse baseline evaluation cases from JSON."""
    assert DATASET_PATH.exists(), f"Dataset file does not exist at {DATASET_PATH}"
    with open(DATASET_PATH, "r", encoding="utf-8") as f:
        cases = json.load(f)
    assert isinstance(cases, list), "Evaluation dataset must be a JSON array"
    return cases


@pytest.fixture(scope="module")
def model_facing_schema() -> DatabaseSchema:
    """Introspect model-facing GradeSense_Local schema for evaluation validation."""
    engine = create_db_engine()
    introspector = DatabaseIntrospector(engine)
    return introspector.introspect_model_facing()


class TestEvaluationDatasetStructure:
    """Verifies structural integrity, unique keys, and metadata completeness of the dataset."""

    def test_case_count_and_unique_ids(self, evaluation_cases: List[Dict[str, Any]]):
        """Verify dataset has approximately 30 cases and all IDs are strictly unique."""
        assert len(evaluation_cases) >= 28, f"Expected ~30 cases, found {len(evaluation_cases)}"
        ids = [case["id"] for case in evaluation_cases]
        assert len(ids) == len(set(ids)), f"Duplicate case IDs found: {[x for x in ids if ids.count(x) > 1]}"

    def test_required_fields_and_categories(self, evaluation_cases: List[Dict[str, Any]]):
        """Verify each case has valid categories, allowed behaviors, and required fields."""
        seen_questions = set()

        for case in evaluation_cases:
            case_id = case.get("id")
            assert case_id, f"Missing id in case: {case}"
            assert case.get("category") in ALLOWED_CATEGORIES, (
                f"Invalid category '{case.get('category')}' in case {case_id}"
            )
            assert case.get("expected_behavior") in ALLOWED_BEHAVIORS, (
                f"Invalid expected_behavior '{case.get('expected_behavior')}' in case {case_id}"
            )
            assert "notes" in case and case["notes"], f"Missing notes in case {case_id}"

            # Questions must be unique for user-facing queries
            if case["category"] != "safety":
                question = case.get("question")
                assert question and isinstance(question, str), (
                    f"Expected non-empty question string in case {case_id}"
                )
                assert question not in seen_questions, f"Duplicate question detected in case {case_id}: {question}"
                seen_questions.add(question)
            else:
                assert case.get("question") is None, f"Safety case {case_id} should not have a question prompt"
                assert case.get("candidate_sql"), f"Safety case {case_id} must have candidate_sql"
                assert case.get("expected_reason"), f"Safety case {case_id} must have expected_reason"

    def test_unsupported_cases_integrity(self, evaluation_cases: List[Dict[str, Any]]):
        """Verify that unsupported cases have no reference SQL and no reference result."""
        unsupported = [c for c in evaluation_cases if c["category"] == "unsupported"]
        assert len(unsupported) >= 4, f"Expected at least 4 unsupported cases, found {len(unsupported)}"

        for case in unsupported:
            assert case["expected_behavior"] == "unsupported"
            assert case.get("reference_sql") is None, (
                f"Unsupported case {case['id']} must have null reference_sql"
            )
            assert case.get("reference_result") is None, (
                f"Unsupported case {case['id']} must have null reference_result"
            )


class TestEvaluationDatasetValidation:
    """Verifies that reference SQL passes Task 1.8 validation and safety fixtures are rejected."""

    def test_safety_fixtures_rejected_by_validator(
        self,
        evaluation_cases: List[Dict[str, Any]],
        model_facing_schema: DatabaseSchema,
    ):
        """Verify safety fixtures are rejected by Task 1.8 validator and NEVER executed."""
        safety_cases = [c for c in evaluation_cases if c["category"] == "safety"]
        assert len(safety_cases) >= 2, f"Expected at least 2 safety cases, found {len(safety_cases)}"

        for case in safety_cases:
            candidate_sql = case["candidate_sql"]
            val_res = validate_sql(candidate_sql, model_facing_schema)

            assert not val_res.allowed, f"Safety fixture {case['id']} was unexpectedly allowed by validator!"
            assert case["expected_reason"] in val_res.reason_codes, (
                f"Safety fixture {case['id']} expected {case['expected_reason']}, got {val_res.reason_codes}"
            )

    def test_answerable_reference_sql_passes_validator(
        self,
        evaluation_cases: List[Dict[str, Any]],
        model_facing_schema: DatabaseSchema,
    ):
        """Verify that all answerable reference SQL queries pass Task 1.8 validation."""
        answerable_cases = [c for c in evaluation_cases if c.get("reference_sql") is not None]
        assert len(answerable_cases) >= 24, f"Expected at least 24 answerable cases, found {len(answerable_cases)}"

        for case in answerable_cases:
            sql = case["reference_sql"]
            val_res = validate_sql(sql, model_facing_schema)

            assert val_res.allowed, (
                f"Reference SQL for case {case['id']} failed validation: {val_res.reasons}"
            )
            # Ensure PasswordHash or other sensitive columns are never referenced
            assert not any("password" in col.lower() for col in val_res.referenced_columns), (
                f"Sensitive column referenced in case {case['id']}"
            )


class TestEvaluationReferenceExecution:
    """Executes all reference SQL against GradeSense_Local and asserts exact match with stored results."""

    def test_reference_results_match_live_database(
        self,
        evaluation_cases: List[Dict[str, Any]],
    ):
        """Execute reference SQL against GradeSense_Local and assert exact match with stored reference_result."""
        engine = create_db_engine()
        answerable_cases = [c for c in evaluation_cases if c.get("reference_sql") is not None]

        mismatches = []
        for case in answerable_cases:
            case_id = case["id"]
            sql = case["reference_sql"]
            stored_result = case["reference_result"]

            assert stored_result is not None, f"Case {case_id} has reference_sql but null reference_result"

            exec_res = execute_read_only(sql=sql, engine=engine)
            actual_rows = exec_res.rows

            if actual_rows != stored_result:
                mismatches.append(
                    f"Case {case_id} mismatch:\n  Expected ({len(stored_result)} rows): {stored_result[:2]}\n  Actual ({len(actual_rows)} rows): {actual_rows[:2]}"
                )

        assert not mismatches, "Dataset reference results do not match GradeSense_Local:\n" + "\n".join(mismatches)
