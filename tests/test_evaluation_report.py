"""Unit tests for deterministic evaluation reporting and regression metrics (Task 1.17).

Verifies metric aggregations, category breakdowns, zero-denominator safety,
exact match vs. semantic correctness separation, failure analysis,
malformed artifact validation, operational metric availability checks,
and preservation of historical evaluation artifacts.
"""

import hashlib
import json
from pathlib import Path
import pytest

from query_pilot.eval.models import (
    CaseEvaluationResult,
    FailureStage,
    MismatchClassification,
    SemanticStatus,
)
from query_pilot.eval.report import (
    EvaluationReportData,
    analyze_failures,
    compute_operational_metrics,
    format_rate,
    generate_markdown_report,
    load_evaluation_artifact,
)
import sys

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.generate_evaluation_report import main as cli_main

BASELINE_1_14_PATH = Path(__file__).resolve().parent.parent / "data" / "evaluation" / "baseline_results_1_14.json"
BASELINE_1_12_PATH = Path(__file__).resolve().parent.parent / "data" / "evaluation" / "baseline_results.json"


# ==============================================================================
# 1. Format Rate and Zero-Denominator Handling
# ==============================================================================

def test_format_rate_standard_and_zero_denominator():
    """Verify format_rate handles standard fractions and 0 denominators without division errors."""
    assert format_rate(26, 26) == "26/26 (100.0%)"
    assert format_rate(12, 26) == "12/26 (46.2%)"
    assert format_rate(0, 4) == "0/4 (0.0%)"
    assert format_rate(0, 0) == "0/0 (N/A)"


# ==============================================================================
# 2. Loading and Validating Real Baseline Artifact (1.14)
# ==============================================================================

def test_load_baseline_1_14_artifact():
    """Verify loading real Task 1.14 baseline results produces expected metrics."""
    assert BASELINE_1_14_PATH.exists()
    report_data = load_evaluation_artifact(BASELINE_1_14_PATH)

    assert report_data.metadata.get("model") == "gemini-3.5-flash-lite"
    assert len(report_data.cases) == 32

    m = report_data.metrics
    # Overall metrics match Task 1.14 benchmark exactly
    assert m.total_cases == 32
    assert m.total_nl_cases == 30
    assert m.generation_success_count == 30
    assert m.validator_accepted_count == 30
    assert m.execution_attempted_count == 26
    assert m.execution_success_count == 26

    # Answerable cases: 26/26 semantic correctness, 12/26 exact match
    assert m.answerable_cases_count == 26
    assert m.exact_match_count == 12
    assert m.semantic_correct_count == 26

    # Unsupported & Safety
    assert m.unsupported_count == 4
    assert m.unsupported_correct_count == 4
    assert m.safety_fixture_count == 2
    assert m.safety_rejected_count == 2


# ==============================================================================
# 3. Separation of Exact Match vs Semantic Correctness
# ==============================================================================

def test_exact_match_vs_semantic_correctness_separation():
    """Verify that exact match and semantic correctness are computed and explained separately."""
    report_data = load_evaluation_artifact(BASELINE_1_14_PATH)
    m = report_data.metrics

    # Strict inequality between exact match and semantic correctness in baseline 1.14
    assert m.exact_match_count == 12
    assert m.semantic_correct_count == 26
    assert m.exact_match_rate < m.semantic_correct_rate

    md = generate_markdown_report(report_data, timestamp="2026-10-09T13:00:00Z")
    assert "**Exact Result Match Rate (Answerable Cases):** 12/26 (46.2%)" in md
    assert "Exact SQL Match" not in md
    assert "**Semantic Correctness Rate (Answerable Cases):** 26/26 (100.0%)" in md
    assert "Measurement Boundary Distinction" in md


# ==============================================================================
# 4. Category-Level Aggregation
# ==============================================================================

def test_category_breakdown_preserves_all_categories():
    """Verify all 9 standard evaluation categories are present with correct fractions."""
    report_data = load_evaluation_artifact(BASELINE_1_14_PATH)
    cb = report_data.metrics.category_breakdown

    expected_categories = {
        "aggregation",
        "analytical",
        "datetime",
        "grouping",
        "multi_table_join",
        "safety",
        "semantic_trap",
        "simple_retrieval",
        "unsupported",
    }
    assert set(cb.keys()) == expected_categories

    assert cb["aggregation"]["total"] == 4
    assert cb["aggregation"]["generation"] == "4/4"
    assert cb["aggregation"]["exact_match"] == "2/4"
    assert cb["aggregation"]["semantic_correct"] == "4/4"

    assert cb["multi_table_join"]["total"] == 4
    assert cb["multi_table_join"]["exact_match"] == "0/4"
    assert cb["multi_table_join"]["semantic_correct"] == "4/4"

    assert cb["unsupported"]["total"] == 4
    assert cb["safety"]["total"] == 2


# ==============================================================================
# 5. Operational Metrics and Missing Historical Fields
# ==============================================================================

def test_operational_metrics_from_baseline():
    """Verify database execution times are aggregated while missing metrics are flagged as unavailable."""
    report_data = load_evaluation_artifact(BASELINE_1_14_PATH)
    ops = compute_operational_metrics(report_data.cases, report_data.metadata)

    lat = ops["database_execution_latency"]
    assert lat is not None
    assert lat["count"] == 26
    assert lat["min_ms"] > 0
    assert lat["max_ms"] >= lat["min_ms"]
    assert lat["median_ms"] > 0
    assert lat["mean_ms"] > 0

    assert ops["total_evaluation_duration_seconds"] == 204.14
    assert ops["stage_durations_available"] is False
    assert ops["request_ids_available"] is False
    assert ops["truncation_metadata_available"] is False

    md = generate_markdown_report(report_data, timestamp="2026-10-09T13:00:00Z")
    assert "Database Query Execution Latency (ms)" in md
    assert "Stage-level durations were not recorded in historical baseline JSON" in md
    assert "Request IDs were introduced in Task 1.16 observability and are absent from historical data" in md
    assert "Truncation flags were not stored in historical CaseEvaluationResult models" in md


# ==============================================================================
# 6. Failure Analysis
# ==============================================================================

def test_failure_analysis_zero_failures_in_1_14():
    """Verify zero failures reported for Task 1.14 baseline."""
    report_data = load_evaluation_artifact(BASELINE_1_14_PATH)
    failures = analyze_failures(report_data.cases)

    for cat_name, items in failures.items():
        assert len(items) == 0, f"Expected 0 failures in {cat_name}, found {len(items)}"

    md = generate_markdown_report(report_data, timestamp="2026-10-09T13:00:00Z")
    assert "**Total Pipeline Failures:** 0" in md
    assert "**Total Semantic Errors:** 0" in md


def test_failure_analysis_with_synthetic_failures(tmp_path: Path):
    """Verify failure analysis categorizes generation, validation, execution, and semantic failures."""
    cases = [
        CaseEvaluationResult(
            case_id="F001",
            category="simple_retrieval",
            expected_behavior="answerable",
            generation_status="failed",
            generation_error="Rate limit exceeded",
            validation_allowed=False,
        ),
        CaseEvaluationResult(
            case_id="F002",
            category="aggregation",
            expected_behavior="answerable",
            generation_status="success",
            validation_allowed=True,
            execution_status="failed",
            execution_error="Table not found in executor",
        ),
        CaseEvaluationResult(
            case_id="F003",
            category="grouping",
            expected_behavior="answerable",
            generation_status="success",
            validation_allowed=True,
            execution_status="success",
            semantic_correct=False,
            semantic_status=SemanticStatus.INCORRECT,
        ),
    ]

    failures = analyze_failures(cases)
    assert len(failures["generation_failures"]) == 1
    assert failures["generation_failures"][0].case_id == "F001"
    assert len(failures["execution_failures"]) == 1
    assert failures["execution_failures"][0].case_id == "F002"
    assert len(failures["semantic_incorrect"]) == 1
    assert failures["semantic_incorrect"][0].case_id == "F003"


# ==============================================================================
# 7. Malformed Input Validation & Error Handling
# ==============================================================================

def test_load_nonexistent_file_raises_not_found():
    """Verify missing file raises FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        load_evaluation_artifact("non_existent_results_file.json")


def test_load_malformed_json_raises_value_error(tmp_path: Path):
    """Verify invalid JSON syntax raises ValueError."""
    bad_json = tmp_path / "bad.json"
    bad_json.write_text("{ unquoted_key: 123", encoding="utf-8")

    with pytest.raises(ValueError, match="Malformed JSON"):
        load_evaluation_artifact(bad_json)


def test_load_unsupported_structure_raises_value_error(tmp_path: Path):
    """Verify missing cases array raises ValueError."""
    bad_struct = tmp_path / "empty_struct.json"
    bad_struct.write_text(json.dumps({"some_key": "val"}), encoding="utf-8")

    with pytest.raises(ValueError, match="missing 'cases' or 'results' array"):
        load_evaluation_artifact(bad_struct)


def test_load_empty_cases_raises_value_error(tmp_path: Path):
    """Verify zero cases raises ValueError."""
    empty_cases = tmp_path / "empty_cases.json"
    empty_cases.write_text(json.dumps({"cases": []}), encoding="utf-8")

    with pytest.raises(ValueError, match="contains zero evaluation cases"):
        load_evaluation_artifact(empty_cases)


# ==============================================================================
# 8. Deterministic Output Reproducibility
# ==============================================================================

def test_report_generation_is_strictly_deterministic():
    """Verify report text is identical across repeated runs when timestamp is fixed."""
    report_data = load_evaluation_artifact(BASELINE_1_14_PATH)
    fixed_ts = "2026-10-09T12:00:00Z"

    report1 = generate_markdown_report(report_data, timestamp=fixed_ts)
    report2 = generate_markdown_report(report_data, timestamp=fixed_ts)

    assert report1 == report2
    assert hashlib.sha256(report1.encode("utf-8")).hexdigest() == hashlib.sha256(report2.encode("utf-8")).hexdigest()


# ==============================================================================
# 9. CLI Script Execution
# ==============================================================================

def test_cli_report_generation_and_protected_file_guard(tmp_path: Path):
    """Verify CLI produces output report and refuses to overwrite protected artifacts."""
    out_file = tmp_path / "cli_test_report.md"

    # Successful generation
    exit_code = cli_main(["--input", str(BASELINE_1_14_PATH), "--output", str(out_file)])
    assert exit_code == 0
    assert out_file.exists()
    content = out_file.read_text(encoding="utf-8")
    assert "QueryPilot — Evaluation Reliability & Regression Report" in content
    assert "30/30 (100.0%)" in content

    # Guard against overwriting protected historical artifact
    guarded_exit = cli_main(["--input", str(BASELINE_1_14_PATH), "--output", "baseline_results_1_14.json"])
    assert guarded_exit != 0


# ==============================================================================
# 10. Historical Artifact Immutability Preservation
# ==============================================================================

def test_historical_artifact_remains_unmodified():
    """Verify that reading and reporting against baseline_results_1_14.json does not mutate it."""
    assert BASELINE_1_14_PATH.exists()
    with open(BASELINE_1_14_PATH, "rb") as f:
        hash_before = hashlib.sha256(f.read()).hexdigest()

    # Load and generate report
    report_data = load_evaluation_artifact(BASELINE_1_14_PATH)
    _ = generate_markdown_report(report_data)

    with open(BASELINE_1_14_PATH, "rb") as f:
        hash_after = hashlib.sha256(f.read()).hexdigest()

    assert hash_before == hash_after, "baseline_results_1_14.json was modified during evaluation reporting!"
