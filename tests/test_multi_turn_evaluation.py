"""Focused automated tests for QueryPilot deterministic multi-turn conversational evaluation harness (B13-M1B).

Verifies:
- All 10 canonical multi-turn scenario definitions execute offline with exact expected outcomes.
- Secondary metrics are strictly evidence-based and dynamically decrease when scenarios fail.
- MT-10 genuinely executes the recovery harness (REC-01) and isolates recovery artifacts.
- Boundary classifications transparently distinguish production from reference-simulator cases.
- JSON and Markdown report serialization and consistency.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from tests.eval_multi_turn import (
    MultiTurnCategory,
    MultiTurnEvaluationArtifact,
    MultiTurnEvaluationMetrics,
    ScenarioDefinition,
    SystemBoundaryType,
    build_multi_turn_scenarios,
    compute_multi_turn_metrics,
    create_multi_turn_eval_schema,
    execute_scenario,
    generate_multi_turn_markdown_report,
    run_multi_turn_evaluation,
    save_multi_turn_evaluation,
)


# ==============================================================================
# 1. Canonical Scenario Suite Verification (MT-01 to MT-10)
# ==============================================================================

def test_canonical_scenarios_count_and_uniqueness() -> None:
    """Verify that build_multi_turn_scenarios produces exactly 10 unique scenarios."""
    scenarios = build_multi_turn_scenarios()
    assert len(scenarios) == 10

    scenario_ids = [s.scenario_id for s in scenarios]
    assert len(set(scenario_ids)) == 10
    assert scenario_ids == [f"MT-{i:02d}" for i in range(1, 11)]


@pytest.mark.parametrize("scenario_idx", list(range(10)))
def test_each_canonical_scenario_passes(scenario_idx: int) -> None:
    """Verify that each individual canonical scenario passes offline deterministically."""
    scenarios = build_multi_turn_scenarios()
    scenario = scenarios[scenario_idx]
    schema = create_multi_turn_eval_schema()

    result = execute_scenario(scenario, schema)
    assert result.passed is True, f"Scenario {scenario.scenario_id} failed: {result.failure_reason}"
    assert result.scenario_id == scenario.scenario_id
    assert result.category == scenario.category.value


# ==============================================================================
# 2. Dynamic Evidence-Based Metric Calculations
# ==============================================================================

def test_multi_turn_metrics_calculation_all_passing() -> None:
    """Verify metrics calculation dynamically aggregates evidence counts across all passing scenarios."""
    scenarios = build_multi_turn_scenarios()
    schema = create_multi_turn_eval_schema()
    results = [execute_scenario(s, schema) for s in scenarios]

    metrics = compute_multi_turn_metrics(results)
    assert metrics.total_scenarios == 10
    assert metrics.passed_scenarios == 10
    assert metrics.failed_scenarios == 0
    assert metrics.pass_rate == 1.0
    assert metrics.total_turns_executed == 13

    # Dynamic metrics derived from scenario evidence
    assert metrics.fifo_evictions_verified == 2       # MT-02 turns 4 and 5
    assert metrics.data_leak_attempts_blocked == 14   # MT-05 (4 SQL + 4 rows + 4 roles + 1 space + 1 cols)
    assert metrics.context_violations_prevented == 22  # Sum across MT-02, MT-03, MT-04, MT-05, MT-06, MT-07, MT-08, MT-10
    assert len(metrics.category_counts) == 10
    assert len(metrics.boundary_counts) >= 5


def test_dynamic_metrics_drop_on_scenario_failure() -> None:
    """Verify that secondary metrics are NOT hardcoded and dynamically drop when scenarios fail."""
    scenarios = build_multi_turn_scenarios()
    schema = create_multi_turn_eval_schema()
    results = [execute_scenario(s, schema) for s in scenarios]

    baseline_metrics = compute_multi_turn_metrics(results)
    assert baseline_metrics.data_leak_attempts_blocked == 14
    assert baseline_metrics.fifo_evictions_verified == 2

    # Simulate failure in MT-05 (data leak scenario)
    tampered_results = []
    for r in results:
        r_copy = r.model_copy()
        if r_copy.scenario_id == "MT-05":
            r_copy.passed = False
            r_copy.failure_reason = "Simulated failure in MT-05"
        tampered_results.append(r_copy)

    metrics_after_mt05_fail = compute_multi_turn_metrics(tampered_results)
    assert metrics_after_mt05_fail.passed_scenarios == 9
    assert metrics_after_mt05_fail.failed_scenarios == 1
    # Evidence from MT-05 must drop to 0
    assert metrics_after_mt05_fail.data_leak_attempts_blocked == 0
    assert metrics_after_mt05_fail.context_violations_prevented == baseline_metrics.context_violations_prevented - 14

    # Simulate failure in MT-02 (FIFO scenario)
    tampered_fifo_results = []
    for r in results:
        r_copy = r.model_copy()
        if r_copy.scenario_id == "MT-02":
            r_copy.passed = False
            r_copy.failure_reason = "Simulated failure in MT-02"
        tampered_fifo_results.append(r_copy)

    metrics_after_mt02_fail = compute_multi_turn_metrics(tampered_fifo_results)
    assert metrics_after_mt02_fail.fifo_evictions_verified == 0
    assert metrics_after_mt02_fail.context_violations_prevented == baseline_metrics.context_violations_prevented - 1


# ==============================================================================
# 3. Genuine Recovery Harness Execution (MT-10)
# ==============================================================================

def test_mt10_genuinely_executes_recovery_harness() -> None:
    """Verify MT-10 genuinely executes canonical REC-01 and proves context isolation."""
    scenarios = build_multi_turn_scenarios()
    mt10 = next(s for s in scenarios if s.scenario_id == "MT-10")
    schema = create_multi_turn_eval_schema()

    result = execute_scenario(mt10, schema)
    assert result.passed is True
    assert result.system_boundary == SystemBoundaryType.PRODUCTION_RECOVERY_HARNESS.value
    assert result.evidence_counts.get("recovery_executions_verified") == 1

    # Verify diagnostic log proves genuine REC-01 execution with 2 attempts
    diag_text = " ".join(result.assertion_details)
    assert "REC-01" in diag_text
    assert "attempts=2" in diag_text
    assert "status=SUCCESS" in diag_text


# ==============================================================================
# 4. Boundary Classification & Transparency
# ==============================================================================

def test_boundary_classification_transparency() -> None:
    """Verify that scenarios clearly distinguish production boundaries from reference-simulator boundaries."""
    scenarios = build_multi_turn_scenarios()
    boundaries = {s.scenario_id: s.system_boundary for s in scenarios}

    # Production boundaries
    assert boundaries["MT-01"] == SystemBoundaryType.PRODUCTION_PROMPT_AND_SCHEMA
    assert boundaries["MT-05"] == SystemBoundaryType.PRODUCTION_SECURITY_VALIDATOR
    assert boundaries["MT-09"] == SystemBoundaryType.PRODUCTION_BASELINE_DATASET
    assert boundaries["MT-10"] == SystemBoundaryType.PRODUCTION_RECOVERY_HARNESS

    # Reference simulator boundaries
    assert boundaries["MT-02"] == SystemBoundaryType.REFERENCE_SIMULATOR_FIFO
    assert boundaries["MT-03"] == SystemBoundaryType.REFERENCE_SIMULATOR_STATE
    assert boundaries["MT-04"] == SystemBoundaryType.REFERENCE_SIMULATOR_STATE
    assert boundaries["MT-06"] == SystemBoundaryType.REFERENCE_SIMULATOR_STATE
    assert boundaries["MT-07"] == SystemBoundaryType.REFERENCE_SIMULATOR_STATE
    assert boundaries["MT-08"] == SystemBoundaryType.REFERENCE_SIMULATOR_STATE


# ==============================================================================
# 5. Artifact Serialization & Markdown Report Generation
# ==============================================================================

def test_multi_turn_report_generation_and_persistence(tmp_path: Path) -> None:
    """Verify JSON and Markdown artifact persistence and formatting."""
    artifact = run_multi_turn_evaluation(git_commit="b0261aa")
    assert artifact.metrics.total_scenarios == 10

    json_path, md_path = save_multi_turn_evaluation(
        artifact=artifact,
        output_dir=tmp_path,
        base_name="test_multi_turn_eval",
    )

    assert json_path.exists()
    assert md_path.exists()

    md_content = md_path.read_text(encoding="utf-8")
    assert "# QueryPilot Multi-Turn Conversational Benchmark" in md_content
    assert "MT-01" in md_content
    assert "MT-10" in md_content
    assert "100.0% (10/10 passed)" in md_content
    assert "Production Backend" in md_content
    assert "Reference Simulator" in md_content
    assert "Production Client State Verification" in md_content


def test_multi_turn_failure_reporting() -> None:
    """Verify that if a scenario fails, the result correctly marks passed=False and records reason."""
    scenarios = build_multi_turn_scenarios()
    failing_scenario = scenarios[0]

    def broken_evaluator(_scenario, _schema):
        return False, ["Simulated regression in context preservation"], {}

    failing_scenario_copy = ScenarioDefinition(
        scenario_id="MT-FAIL",
        name="Broken Scenario",
        category=MultiTurnCategory.DEPENDENT_FOLLOW_UP,
        system_boundary=failing_scenario.system_boundary,
        description="Failing test",
        turns=[],
        custom_evaluator=broken_evaluator,
    )

    schema = create_multi_turn_eval_schema()
    res = execute_scenario(failing_scenario_copy, schema)
    assert res.passed is False
    assert "Simulated regression" in str(res.failure_reason)


# ==============================================================================
# 6. CLI Runner Exit Code Behavior
# ==============================================================================

def test_cli_runner_exit_code_behavior(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Verify CLI runner returns exit code 0 on success and exit code 1 on failure."""
    from scripts.run_multi_turn_evaluation import main

    monkeypatch.setattr("sys.argv", ["run_multi_turn_evaluation.py", "--output-dir", str(tmp_path)])
    assert main() == 0

    # Patch build_multi_turn_scenarios to include a failing scenario
    broken_scenario = ScenarioDefinition(
        scenario_id="MT-BREAK",
        name="Broken",
        category=MultiTurnCategory.DEPENDENT_FOLLOW_UP,
        system_boundary=SystemBoundaryType.PRODUCTION_PROMPT_AND_SCHEMA,
        description="Fails",
        turns=[],
        custom_evaluator=lambda _s, _sc: (False, ["Forced failure"], {}),
    )
    monkeypatch.setattr(
        "scripts.run_multi_turn_evaluation.build_multi_turn_scenarios",
        lambda: [broken_scenario],
    )
    assert main() == 1
