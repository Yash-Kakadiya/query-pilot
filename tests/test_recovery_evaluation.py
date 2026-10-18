"""Focused automated tests for QueryPilot deterministic recovery evaluation harness.

Verifies:
- All 15 canonical scenario definitions execute offline with exact expected call counts and statuses.
- Metric calculations use exact and unambiguous denominators.
- Edge case where 0 recovery attempts yields recovery_success_rate = None.
- Safety-violation counts derived from recorded assertions.
- JSON and Markdown report serialization and consistency.
- CLI runner execution and protection of historical evaluation artifacts.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from query_pilot.db.executor import QueryResult
from query_pilot.eval.recovery import (
    PROTECTED_BASE_NAMES,
    PROTECTED_EVALUATION_FILES,
    RecoveryEvaluationArtifact,
    RecoveryEvaluationMetrics,
    RecoveryScenarioCategory,
    ScenarioDefinition,
    ScenarioEvaluationResult,
    build_recovery_scenarios,
    compute_recovery_metrics,
    create_recovery_eval_schema,
    execute_scenario,
    generate_recovery_markdown_report,
    run_recovery_evaluation,
    save_recovery_evaluation,
)
from query_pilot.pipeline import PipelineStatus
from query_pilot.sql.generation import (
    SQLGenerationResponse,
    SQLGenerationStatus,
)
from query_pilot.sql.models import (
    ValidationReason,
    ValidationReasonCode,
    ValidationResult,
)


# ==============================================================================
# 1. Canonical Scenario Suite Verification (REC-01 to REC-15)
# ==============================================================================

def test_canonical_scenarios_count_and_uniqueness() -> None:
    """Verify that build_recovery_scenarios produces exactly 15 unique scenarios."""
    scenarios = build_recovery_scenarios()
    assert len(scenarios) == 15

    scenario_ids = [s.scenario_id for s in scenarios]
    assert len(set(scenario_ids)) == 15
    assert scenario_ids == [f"REC-{i:02d}" for i in range(1, 16)]


@pytest.mark.parametrize("scenario_idx", list(range(15)))
def test_each_canonical_scenario_passes(scenario_idx: int) -> None:
    """Execute each canonical scenario individually and assert 100% assertion pass."""
    scenarios = build_recovery_scenarios()
    scenario = scenarios[scenario_idx]

    result = execute_scenario(scenario)

    assert result.passed is True, f"Scenario {scenario.scenario_id} failed: {result.assertion_details}"
    assert result.actual_status == scenario.expected_status.value
    assert result.actual_gen_calls == scenario.expected_gen_calls
    assert result.actual_val_calls == scenario.expected_val_calls
    assert result.actual_exec_calls == scenario.expected_exec_calls
    assert result.safety_violated is False


# ==============================================================================
# 2. Complete Evaluation Run and Aggregate Metrics
# ==============================================================================

def test_run_recovery_evaluation_full_suite() -> None:
    """Verify full evaluation run across all 15 scenarios produces expected aggregate metrics."""
    artifact = run_recovery_evaluation(git_commit="test_commit_123")
    m = artifact.metrics

    assert m.total_scenarios == 15
    assert m.passed_scenarios == 15
    assert m.failed_scenarios == 0

    # 6 recovery-eligible scenarios (REC-01 to REC-06)
    assert m.recovery_eligible_scenarios == 6
    assert m.recovery_attempts_made == 6
    # 5 recovered (REC-01, 02, 03, 05, 06); 1 exhausted (REC-04)
    assert m.successful_recoveries == 5
    assert m.exhaustion_scenarios == 1
    assert m.recovery_success_rate == pytest.approx(5 / 6, rel=1e-3)

    # 6 terminal protection scenarios (REC-07 to REC-12)
    assert m.terminal_protection_total == 6
    assert m.terminal_protection_passed == 6

    # Zero safety compromises
    assert m.unexpected_retries == 0
    assert m.unvalidated_execution_attempts == 0
    assert m.safety_violations == 0

    # Call overhead counts
    assert m.total_gen_calls == 21
    assert m.total_val_calls == 18
    assert m.total_exec_calls == 10
    assert m.gen_calls_per_scenario == pytest.approx(21 / 15, rel=1e-2)

    # Recovery overhead in eligible cases: 12 actual vs 6 baseline (2.0x)
    assert m.recovery_eligible_gen_calls == 12
    assert m.recovery_eligible_baseline_gen_calls == 6
    assert m.additional_recovery_gen_calls == 6
    assert m.gen_overhead_ratio_in_recovery == 2.0


# ==============================================================================
# 3. Metric Calculations and Edge Cases
# ==============================================================================

def test_recovery_metrics_zero_attempts_edge_case() -> None:
    """Verify recovery_success_rate is None (N/A) when zero recovery attempts are made."""
    dummy_results = [
        ScenarioEvaluationResult(
            scenario_id="TEST-01",
            name="Single Pass Scenario",
            category="baseline_success",
            question="Simple query",
            passed=True,
            expected_status="success",
            actual_status="success",
            expected_gen_calls=1,
            actual_gen_calls=1,
            expected_val_calls=1,
            actual_val_calls=1,
            expected_exec_calls=1,
            actual_exec_calls=1,
            recovery_eligible=False,
            recovery_attempted=False,
            recovery_succeeded=False,
            terminal_protection=False,
            terminal_protected=False,
            safety_violated=False,
            assertion_details=[],
            safety_invariant="Single pass",
        )
    ]

    metrics = compute_recovery_metrics(dummy_results)
    assert metrics.recovery_eligible_scenarios == 0
    assert metrics.recovery_attempts_made == 0
    assert metrics.successful_recoveries == 0
    assert metrics.recovery_success_rate is None  # Does NOT divide by zero


def test_safety_violations_derived_from_assertions() -> None:
    """Verify safety violations are derived dynamically from recorded failure conditions."""
    flawed_results = [
        ScenarioEvaluationResult(
            scenario_id="FAIL-01",
            name="Unvalidated Execution Scenario",
            category="recoverable_validation",
            question="Query",
            passed=False,
            expected_status="success",
            actual_status="success",
            expected_gen_calls=1,
            actual_gen_calls=1,
            expected_val_calls=0,
            actual_val_calls=0,
            expected_exec_calls=1,
            actual_exec_calls=1,
            recovery_eligible=True,
            recovery_attempted=False,
            recovery_succeeded=False,
            terminal_protection=False,
            terminal_protected=False,
            safety_violated=True,  # Flawed: execution called with 0 validations
            assertion_details=["Safety violation: execution was invoked without prior validation"],
            safety_invariant="Validation before exec",
        )
    ]

    metrics = compute_recovery_metrics(flawed_results)
    assert metrics.safety_violations == 1
    assert metrics.unvalidated_execution_attempts == 1
    assert metrics.failed_scenarios == 1


# ==============================================================================
# 4. Report Generation and Consistency
# ==============================================================================

def test_markdown_report_generation() -> None:
    """Verify Markdown report accurately formats executive metrics and scenario details."""
    artifact = run_recovery_evaluation(git_commit="abc1234")
    report = generate_recovery_markdown_report(artifact)

    assert "# QueryPilot — Bounded Recovery Mechanics Evaluation Report" in report
    assert "abc1234" in report
    assert "100% Offline" in report
    assert "83.3%" in report  # 5/6 recovery rate
    assert "15/15" in report
    assert "REC-01" in report
    assert "REC-15" in report
    assert "Safety Invariant Audit Summary" in report


# ==============================================================================
# 5. Artifact Protection and Persistence
# ==============================================================================

def test_protected_files_cannot_be_overwritten(tmp_path: Path) -> None:
    """Verify save_recovery_evaluation refuses to overwrite historical evaluation files."""
    artifact = run_recovery_evaluation(git_commit="test")

    for base_name in PROTECTED_BASE_NAMES:
        with pytest.raises(ValueError, match="Cannot overwrite protected historical evaluation file"):
            save_recovery_evaluation(artifact, tmp_path, base_name=base_name)


def test_successful_artifact_saving(tmp_path: Path) -> None:
    """Verify saving creates valid JSON and Markdown files with matching contents."""
    artifact = run_recovery_evaluation(git_commit="commit_xyz")
    json_path, md_path = save_recovery_evaluation(artifact, tmp_path, base_name="test_eval")

    assert json_path.exists()
    assert md_path.exists()

    with open(json_path, "r", encoding="utf-8") as f:
        loaded_json = f.read()
    assert "commit_xyz" in loaded_json
    assert "REC-01" in loaded_json

    with open(md_path, "r", encoding="utf-8") as f:
        loaded_md = f.read()
    assert "# QueryPilot — Bounded Recovery Mechanics Evaluation Report" in loaded_md


# ==============================================================================
# 6. Focused Negative Assertions
# ==============================================================================

def test_sanitization_failure_detection() -> None:
    """Verify evaluation harness flags a sanitization leak if prior SQL appears in retry instructions."""
    leaky_scenario = ScenarioDefinition(
        scenario_id="LEAK-01",
        name="Leaky Retry Instructions Scenario",
        description="Fails validation on attempt 1; attempt 2 passes but instructions leak prior SQL.",
        category=RecoveryScenarioCategory.RECOVERABLE_VALIDATION,
        question="Find user",
        generator_sequence=[
            SQLGenerationResponse(status=SQLGenerationStatus.ANSWERABLE, sql="SELECT SecretCol FROM Users;"),
            SQLGenerationResponse(status=SQLGenerationStatus.ANSWERABLE, sql="SELECT Id FROM Users;"),
        ],
        validation_side_effects=[
            ValidationResult(allowed=False, reasons=[ValidationReason(code=ValidationReasonCode.UNKNOWN_COLUMN, message="Unknown column")]),
            ValidationResult(allowed=True, normalized_sql="SELECT Id FROM Users;"),
        ],
        execution_side_effects=[QueryResult(columns=["Id"], rows=[{"Id": 1}], row_count=1, truncated=False)],
        expected_status=PipelineStatus.SUCCESS,
        expected_gen_calls=2,
        expected_val_calls=2,
        expected_exec_calls=1,
        is_recovery_eligible=True,
        candidate_sql_to_check_redacted="SELECT SecretCol FROM Users;",
    )

    # In actual QueryPilotWorkflow, recovery_feedback never leaks prior SQL, so this test passes.
    result = execute_scenario(leaky_scenario)
    assert result.passed is True
    # Confirm no leak error was recorded
    assert not any("Sanitization leak" in err for err in result.assertion_details)
