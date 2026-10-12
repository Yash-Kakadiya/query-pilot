"""Dedicated CLI Runner for QueryPilot Deterministic Recovery Evaluation (Task 1.24).

Executes the 15 canonical recovery evaluation scenarios offline:
- Exercises actual LangGraph workflow routing and recovery nodes against deterministic fixtures.
- Validates exact generation, validation, and execution call counts.
- Verifies attempt ceilings, revalidation, and prior SQL redaction.
- Generates JSON and Markdown artifacts under data/evaluation/.
- Returns exit code 0 if all scenarios and invariants pass, exit code 1 otherwise.
"""

import argparse
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import subprocess
import sys
from typing import Any, Dict

from query_pilot.eval.recovery import (
    PROTECTED_EVALUATION_FILES,
    RecoveryEvaluationArtifact,
    build_recovery_scenarios,
    generate_recovery_markdown_report,
    run_recovery_evaluation,
    save_recovery_evaluation,
)

logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR = ROOT_DIR / "data" / "evaluation"
DEFAULT_BASE_NAME = "recovery_evaluation"


def get_git_commit() -> str:
    """Retrieve current Git HEAD commit hash if available."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(ROOT_DIR),
            capture_output=True,
            text=True,
            check=True,
        )
        return proc.stdout.strip()[:7]
    except Exception:
        return "unknown"


def main() -> int:
    """CLI entry point for running recovery evaluation."""
    parser = argparse.ArgumentParser(
        description="Run QueryPilot deterministic LangGraph recovery evaluation."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory to save evaluation artifacts.",
    )
    parser.add_argument(
        "--base-name",
        type=str,
        default=DEFAULT_BASE_NAME,
        help="Base filename for generated artifacts without extension.",
    )

    args = parser.parse_args()

    print("=" * 80)
    print(" QueryPilot Task 1.24 — Deterministic Recovery Evaluation Harness")
    print("=" * 80)
    print(f"Timestamp:   {datetime.now(timezone.utc).isoformat()}")
    print("Environment: 100% Offline (Mocked Model & Database Dependencies)")
    print(f"Output:      {args.output_dir / (args.base_name + '.json')}")
    print(f"Report:      {args.output_dir / (args.base_name + '_report.md')}")
    print("-" * 80)

    git_commit = get_git_commit()
    scenarios = build_recovery_scenarios()
    print(f"Loaded {len(scenarios)} deterministic recovery scenarios.")
    print("Executing evaluation...")

    artifact = run_recovery_evaluation(scenarios=scenarios, git_commit=git_commit)
    metrics = artifact.metrics

    print("\n" + "=" * 80)
    print(" Scenario Results Summary")
    print("=" * 80)
    print(f"{'ID':<8} {'Status':<6} {'Calls (G/V/E)':<16} {'Scenario Name'}")
    print("-" * 80)

    for r in artifact.scenario_results:
        badge = "[PASS]" if r.passed else "[FAIL]"
        calls = f"{r.actual_gen_calls}/{r.actual_val_calls}/{r.actual_exec_calls}"
        print(f"{r.scenario_id:<8} {badge:<6} {calls:<16} {r.name}")
        if not r.passed:
            for err in r.assertion_details:
                print(f"         └── [ERROR] {err}")

    print("-" * 80)
    print(f"Scenarios:            {metrics.passed_scenarios}/{metrics.total_scenarios} passed")
    rec_rate_str = f"{metrics.recovery_success_rate * 100:.1f}%" if metrics.recovery_success_rate is not None else "N/A"
    print(f"Recovery Success:     {rec_rate_str} ({metrics.successful_recoveries}/{metrics.recovery_attempts_made} attempts)")
    print(f"Terminal Protection:  {metrics.terminal_protection_passed}/{metrics.terminal_protection_total} passed")
    print(f"Safety Violations:    {metrics.safety_violations}")
    print(f"Total Gen Calls:      {metrics.total_gen_calls} (avg {metrics.gen_calls_per_scenario:.2f} calls/scenario)")
    print(f"Recovery Gen Overhead:{metrics.gen_overhead_ratio_in_recovery:.2f}x ({metrics.recovery_eligible_gen_calls}/{metrics.recovery_eligible_baseline_gen_calls} baseline)")
    print("=" * 80)

    # Persist artifacts
    try:
        json_path, md_path = save_recovery_evaluation(
            artifact=artifact,
            output_dir=args.output_dir,
            base_name=args.base_name,
        )
        print(f"\n[INFO] Artifacts successfully written:")
        print(f"  - JSON:   {json_path}")
        print(f"  - Report: {md_path}")
    except Exception as exc:
        print(f"\n[FATAL] Error writing artifacts: {exc}")
        return 1

    if metrics.failed_scenarios > 0 or metrics.safety_violations > 0:
        print(f"\n[FAILED] {metrics.failed_scenarios} scenario(s) or safety violations detected.")
        return 1

    print("\n[SUCCESS] All deterministic recovery scenarios and safety assertions PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
