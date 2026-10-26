"""Automated tests for QueryPilot deterministic explanation evaluation harness (B13-EVAL2).

Verifies:
- All 12 canonical explanation scenario definitions execute offline with exact expected outcomes.
- Secondary metrics are strictly evidence-based and dynamically decrease when scenarios fail.
- Real production functions in query_pilot.presentation.synthesizer are directly exercised.
- Distinction between regex/heuristic checks and full semantic faithfulness is preserved.
- JSON and Markdown report serialization and consistency.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from tests.eval_explanation import (
    ExplanationCategory,
    ExplanationEvaluationArtifact,
    ExplanationEvaluationMetrics,
    ExplanationScenarioDefinition,
    ExplanationScenarioResult,
    SystemBoundaryType,
    build_explanation_scenarios,
    compute_explanation_metrics,
    execute_explanation_scenario,
    generate_explanation_markdown_report,
    run_explanation_evaluation,
    save_explanation_evaluation,
)


# ==============================================================================
# 1. Canonical Scenario Suite Verification (EXP-01 to EXP-12)
# ==============================================================================

def test_canonical_scenarios_count_and_uniqueness() -> None:
    """Verify that build_explanation_scenarios produces exactly 12 unique scenarios."""
    scenarios = build_explanation_scenarios()
    assert len(scenarios) == 12

    scenario_ids = [s.scenario_id for s in scenarios]
    assert len(set(scenario_ids)) == 12
    assert scenario_ids == [f"EXP-{i:02d}" for i in range(1, 13)]


@pytest.mark.parametrize("scenario_idx", list(range(12)))
def test_each_canonical_scenario_passes(scenario_idx: int) -> None:
    """Verify that each individual canonical scenario passes offline deterministically."""
    scenarios = build_explanation_scenarios()
    scenario = scenarios[scenario_idx]

    result = execute_explanation_scenario(scenario)
    assert result.passed is True, f"Scenario {scenario.scenario_id} failed: {result.failure_reason}"
    assert result.scenario_id == scenario.scenario_id
    assert result.category == scenario.category.value
    assert len(result.assertion_details) > 0
    assert len(result.production_functions) > 0


# ==============================================================================
# 2. Dynamic Evidence-Based Metric Calculations
# ==============================================================================

def test_explanation_metrics_calculation_all_passing() -> None:
    """Verify metrics calculation dynamically aggregates evidence counts across all passing scenarios."""
    scenarios = build_explanation_scenarios()
    results = [execute_explanation_scenario(s) for s in scenarios]

    metrics = compute_explanation_metrics(results)
    assert metrics.total_scenarios == 12
    assert metrics.passed_scenarios == 12
    assert metrics.failed_scenarios == 0
    assert metrics.pass_rate == 1.0

    # Dynamic metrics derived from scenario evidence
    assert metrics.scalar_templates_verified == 3        # EXP-01, EXP-02, EXP-03
    assert metrics.hallucination_checks_verified == 2    # EXP-05, EXP-06
    assert metrics.privacy_boundaries_verified == 3      # EXP-04, EXP-08, EXP-09
    assert metrics.failure_fallbacks_verified == 2       # EXP-11, EXP-12
    assert len(metrics.category_counts) == 8
    assert len(metrics.boundary_counts) == 5


def test_dynamic_metrics_drop_on_scenario_failure() -> None:
    """Verify that secondary metrics are NOT hardcoded and dynamically drop when scenarios fail."""
    scenarios = build_explanation_scenarios()
    results = [execute_explanation_scenario(s) for s in scenarios]

    baseline_metrics = compute_explanation_metrics(results)
    assert baseline_metrics.hallucination_checks_verified == 2

    # Simulate failure in EXP-05 (hallucination scenario)
    tampered_results = []
    for r in results:
        r_copy = r.model_copy()
        if r_copy.scenario_id == "EXP-05":
            r_copy.passed = False
            r_copy.failure_reason = "Simulated failure in EXP-05"
        tampered_results.append(r_copy)

    tampered_metrics = compute_explanation_metrics(tampered_results)
    assert tampered_metrics.passed_scenarios == 11
    assert tampered_metrics.failed_scenarios == 1
    assert tampered_metrics.pass_rate == 11 / 12
    # Verify hallucination count dynamically dropped from 2 to 1
    assert tampered_metrics.hallucination_checks_verified == 1


# ==============================================================================
# 3. Report Generation & Serialization
# ==============================================================================

def test_explanation_report_generation(tmp_path: Path) -> None:
    """Verify JSON and Markdown report generation, contents, and consistency."""
    scenarios = build_explanation_scenarios()
    artifact = run_explanation_evaluation(scenarios=scenarios, git_commit="test_commit_hash")

    json_path, md_path = save_explanation_evaluation(
        artifact=artifact,
        output_dir=tmp_path,
        base_name="test_explanation_eval",
    )

    assert json_path.is_file()
    assert md_path.is_file()

    # Verify JSON structure
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    assert data["metadata"]["git_commit"] == "test_commit_hash"
    assert data["metrics"]["total_scenarios"] == 12
    assert data["metrics"]["passed_scenarios"] == 12
    assert len(data["scenario_results"]) == 12

    # Verify Markdown contents
    with open(md_path, "r", encoding="utf-8") as f:
        md_text = f.read()

    assert "# QueryPilot B13-EVAL2" in md_text
    assert "EXP-01" in md_text
    assert "EXP-12" in md_text
    assert "Heuristic Boundaries & Semantic Limitations Disclosure" in md_text
    assert "test_commit_hash" in md_text


# ==============================================================================
# 4. Production Function Direct Execution
# ==============================================================================

def test_production_functions_directly_referenced() -> None:
    """Verify every scenario explicitly names real production functions in query_pilot.presentation.synthesizer."""
    scenarios = build_explanation_scenarios()
    all_funcs = set()
    for s in scenarios:
        for f in s.production_functions:
            all_funcs.add(f)

    expected_production_targets = {
        "format_scalar_template",
        "AnswerSynthesisService.synthesize",
        "AnswerSynthesisService.synthesize_from_metadata",
        "_detect_hallucinated_values",
        "_question_requires_row_disclosure",
        "mask_sql_literals",
    }

    assert expected_production_targets.issubset(all_funcs)
