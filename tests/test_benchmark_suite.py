"""Automated Test Suite for QueryPilot AI Evaluation Benchmark Framework (Pillar 4).

Verifies:
1. Benchmark dataset schema integrity and categorical coverage (28 canonical cases).
2. Reference SQL syntactic validity and precomputed result ground-truth.
3. Fail-closed deterministic safety rejections for all DDL, DML, and injection attacks.
4. Dynamic semantic comparator fidelity (exact match, alias equivalence, value mismatch).
5. Offline end-to-end benchmark execution delivering >= 90% EX and 100% Safety Rejection.
6. CLI runner reporting and artifact generation.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from typing import Any, Dict, List

import pytest
import sqlglot

from query_pilot.db.models import DatabaseSchema
from query_pilot.db.profile import ConnectionProfile
from query_pilot.db.profile_executor import ProfileQueryExecutor
from query_pilot.db.profile_introspector import ProfileDatabaseIntrospector
from query_pilot.eval.semantic import SemanticOutcome, compare_results_semantically
from query_pilot.sql.models import ValidationReasonCode
from query_pilot.sql.validator import validate_sql
from scripts.run_evaluation_benchmark import BenchmarkRunner, calculate_percentile

ROOT_DIR = Path(__file__).resolve().parent.parent
DATASET_PATH = ROOT_DIR / "data" / "evaluation" / "benchmark_suite.json"
DB_PATH = ROOT_DIR / "data" / "demo" / "university_demo.db"
OUTPUT_DIR = ROOT_DIR / "data" / "evaluation"

EXPECTED_CATEGORIES = {
    "simple_retrieval",
    "aggregation",
    "grouping",
    "multi_table_join",
    "analytical",
    "safety",
    "unsupported",
    "clarification",
}


@pytest.fixture(scope="module")
def benchmark_dataset() -> List[Dict[str, Any]]:
    assert DATASET_PATH.is_file(), f"Benchmark dataset missing at: {DATASET_PATH}"
    with open(DATASET_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def sqlite_schema() -> DatabaseSchema:
    assert DB_PATH.is_file(), f"Target database missing at: {DB_PATH}"
    profile = ConnectionProfile(
        profile_id="test_benchmark_sqlite",
        display_name="Test Benchmark SQLite",
        engine_type="sqlite",
        database_name=DB_PATH.name,
        database_path=str(DB_PATH),
    )
    introspector = ProfileDatabaseIntrospector(profile)
    return introspector.introspect_raw()


# ==============================================================================
# 1. Benchmark Dataset Integrity Tests
# ==============================================================================

class TestBenchmarkDatasetIntegrity:
    """Verifies that the canonical benchmark dataset adheres strictly to schema specs."""

    def test_total_case_count_and_uniqueness(self, benchmark_dataset: List[Dict[str, Any]]):
        assert len(benchmark_dataset) == 28, f"Expected 28 canonical benchmark cases, got {len(benchmark_dataset)}"
        ids = [c["id"] for c in benchmark_dataset]
        assert len(ids) == len(set(ids)), f"Duplicate case IDs found: {set([x for x in ids if ids.count(x) > 1])}"

    def test_category_distribution(self, benchmark_dataset: List[Dict[str, Any]]):
        categories = {c["category"] for c in benchmark_dataset}
        assert categories == EXPECTED_CATEGORIES, f"Mismatch in expected categories. Found: {categories}"

        counts = {}
        for c in benchmark_dataset:
            counts[c["category"]] = counts.get(c["category"], 0) + 1

        assert counts["simple_retrieval"] == 4
        assert counts["aggregation"] == 4
        assert counts["grouping"] == 4
        assert counts["multi_table_join"] == 4
        assert counts["analytical"] == 4
        assert counts["safety"] == 4
        assert counts["unsupported"] == 2
        assert counts["clarification"] == 2

    def test_answerable_cases_have_reference_sql_and_results(self, benchmark_dataset: List[Dict[str, Any]]):
        for c in benchmark_dataset:
            if c["expected_behavior"] == "answerable":
                assert c.get("reference_sql") is not None, f"Case {c['id']} missing reference_sql"
                assert c.get("reference_result") is not None, f"Case {c['id']} missing precomputed reference_result"
                assert isinstance(c["reference_result"], list), f"Case {c['id']} reference_result must be a list"
                assert len(c["reference_result"]) > 0, f"Case {c['id']} reference_result should not be empty"


# ==============================================================================
# 2. Reference SQL Syntactic and Dialect Tests
# ==============================================================================

class TestReferenceSQLValidity:
    """Verifies that all 20 answerable reference queries parse cleanly in SQLite dialect."""

    def test_all_reference_queries_parse_sqlite(self, benchmark_dataset: List[Dict[str, Any]]):
        for c in benchmark_dataset:
            if c["expected_behavior"] == "answerable":
                sql = c["reference_sql"]
                parsed = sqlglot.parse(sql, read="sqlite")
                assert len(parsed) == 1, f"Expected single statement in {c['id']}, parsed: {len(parsed)}"
                assert parsed[0] is not None, f"Failed parsing SQL in {c['id']}"

    def test_reference_queries_pass_validator(
        self,
        benchmark_dataset: List[Dict[str, Any]],
        sqlite_schema: DatabaseSchema,
    ):
        for c in benchmark_dataset:
            if c["expected_behavior"] == "answerable":
                val = validate_sql(c["reference_sql"], sqlite_schema, dialect="sqlite")
                assert val.allowed is True, f"Reference SQL rejected in {c['id']}: {[r.to_dict() for r in val.reasons]}"


# ==============================================================================
# 3. Deterministic Safety Rejection Fixture Tests
# ==============================================================================

class TestSafetyFixtures:
    """Verifies that malicious or mutating queries fail-closed deterministically."""

    def test_dml_delete_rejected(self, sqlite_schema: DatabaseSchema):
        val = validate_sql("DELETE FROM students WHERE student_id = 1;", sqlite_schema, dialect="sqlite")
        assert val.allowed is False
        assert any(r.code == ValidationReasonCode.NOT_READ_ONLY for r in val.reasons)

    def test_ddl_drop_rejected(self, sqlite_schema: DatabaseSchema):
        val = validate_sql("DROP TABLE enrollments;", sqlite_schema, dialect="sqlite")
        assert val.allowed is False
        assert any(r.code == ValidationReasonCode.NOT_READ_ONLY for r in val.reasons)

    def test_multi_statement_injection_rejected(self, sqlite_schema: DatabaseSchema):
        val = validate_sql("SELECT * FROM courses; DROP TABLE courses;", sqlite_schema, dialect="sqlite")
        assert val.allowed is False
        assert any(r.code == ValidationReasonCode.MULTI_STATEMENT for r in val.reasons)

    def test_dml_update_rejected(self, sqlite_schema: DatabaseSchema):
        val = validate_sql("UPDATE students SET student_name = 'Hacked' WHERE student_id = 1;", sqlite_schema, dialect="sqlite")
        assert val.allowed is False
        assert any(r.code == ValidationReasonCode.NOT_READ_ONLY for r in val.reasons)

    def test_all_dataset_safety_cases_rejected(
        self,
        benchmark_dataset: List[Dict[str, Any]],
        sqlite_schema: DatabaseSchema,
    ):
        for c in benchmark_dataset:
            if c["category"] == "safety":
                candidate_sql = c.get("candidate_sql") or c.get("reference_sql")
                assert candidate_sql is not None, f"Safety case {c['id']} missing candidate SQL"
                val = validate_sql(candidate_sql, sqlite_schema, dialect="sqlite")
                assert val.allowed is False, f"Dangerous SQL was not rejected for {c['id']}: {candidate_sql}"


# ==============================================================================
# 4. Semantic Comparator Correctness Tests
# ==============================================================================

class TestSemanticComparatorIntegration:
    """Verifies that compare_results_semantically behaves reliably."""

    def test_exact_match(self):
        gen = [{"dept": "CS", "cnt": 10}, {"dept": "Math", "cnt": 5}]
        ref = [{"dept": "CS", "cnt": 10}, {"dept": "Math", "cnt": 5}]
        res = compare_results_semantically(gen, ref)
        assert res.outcome == SemanticOutcome.EXACT_MATCH
        assert res.is_correct is True

    def test_column_alias_stem_match(self):
        gen = [{"department_name": "CS", "student_count": 10}]
        ref = [{"department_name": "CS", "total_students": 10}]
        res = compare_results_semantically(gen, ref)
        assert res.is_correct is True
        assert res.outcome == SemanticOutcome.EQUIVALENT_MATCH

    def test_value_mismatch_detected(self):
        gen = [{"dept": "CS", "cnt": 10}]
        ref = [{"dept": "CS", "cnt": 99}]
        res = compare_results_semantically(gen, ref)
        assert res.is_correct is False
        assert res.outcome == SemanticOutcome.MISMATCH


# ==============================================================================
# 5. Offline End-to-End Benchmark Execution Tests
# ==============================================================================

class TestOfflineBenchmarkRunner:
    """Verifies that BenchmarkRunner executes all 28 cases and generates reports."""

    def test_runner_offline_execution(self, tmp_path: Path):
        runner = BenchmarkRunner(
            dataset_path=DATASET_PATH,
            db_path=DB_PATH,
            output_dir=tmp_path,
            mode="offline",
        )
        summary = runner.run()

        # KPIs
        assert summary["total_cases"] == 28
        assert summary["answerable_cases"] == 20
        assert summary["safety_fixtures"] == 4
        assert summary["unsupported_and_clarifications"] == 4

        # Strict Benchmark Thresholds
        assert summary["valid_sql_rate_vsr"] == 100.0
        assert summary["execution_accuracy_ex"] == 100.0
        assert summary["exact_match_rate"] == 100.0
        assert summary["schema_grounding_score_sgs"] == 100.0
        assert summary["safety_rejection_rate"] == 100.0
        assert summary["unsupported_and_clarification_rate"] == 100.0

        # Latency check: fast local SQLite queries should be < 25ms median
        assert summary["latency_ms"]["p50"] < 25.0

    def test_percentile_helper(self):
        vals = [10.0, 20.0, 30.0, 40.0, 50.0]
        assert calculate_percentile(vals, 50.0) == 30.0
        assert calculate_percentile(vals, 0.0) == 10.0
        assert calculate_percentile(vals, 100.0) == 50.0


# ==============================================================================
# 6. CLI Runner Subprocess Invariant Tests
# ==============================================================================

class TestBenchmarkCLI:
    """Verifies that scripts/run_evaluation_benchmark.py runs as a standalone CLI tool."""

    def test_cli_offline_run(self):
        cmd = [
            sys.executable,
            str(ROOT_DIR / "scripts" / "run_evaluation_benchmark.py"),
            "--mode",
            "offline",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT_DIR))
        assert proc.returncode == 0, f"Benchmark CLI failed with stderr:\n{proc.stderr}\nstdout:\n{proc.stdout}"
        assert "QUERYPILOT AI BENCHMARK RESULTS SUMMARY" in proc.stdout
        assert "Execution Accuracy (EX):        100.0%" in proc.stdout
        assert "Safety Rejection Rate:          100.0%" in proc.stdout

        # Verify generated report and JSON artifacts exist
        results_json = ROOT_DIR / "data" / "evaluation" / "benchmark_results.json"
        report_md = ROOT_DIR / "data" / "evaluation" / "BENCHMARK_REPORT.md"
        assert results_json.is_file(), "benchmark_results.json was not generated"
        assert report_md.is_file(), "BENCHMARK_REPORT.md was not generated"
