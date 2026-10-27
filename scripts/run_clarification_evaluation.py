"""Dedicated CLI Runner for QueryPilot Deterministic Clarification Evaluation (Milestone B13-EVAL3).

Executes the 10 canonical clarification evaluation scenarios offline:
- Exercises lexical, temporal, aggregation, and scope ambiguity patterns (CLAR-01 to CLAR-04).
- Validates negative controls for answerable and unsupported queries (CLAR-05, CLAR-06).
- Verifies question reformulation and defensive fallback resolution (CLAR-07, CLAR-08).
- Enforces option count limits (<= 5) and non-empty string constraints (CLAR-09).
- Enforces case-insensitive option ID uniqueness and cross-field status invariants (CLAR-10).
- Generates JSON and Markdown evaluation artifacts under data/evaluation/.
- Returns exit code 0 if all scenarios pass, exit code 1 otherwise.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import logging
from pathlib import Path
import subprocess
import sys

# Ensure repository root is on sys.path for direct CLI execution
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from tests.eval_clarification import (
    ClarificationEvaluationArtifact,
    build_clarification_scenarios,
    generate_clarification_markdown_report,
    run_clarification_evaluation,
    save_clarification_evaluation,
)

logger = logging.getLogger(__name__)

DEFAULT_OUTPUT_DIR = ROOT_DIR / "data" / "evaluation"
DEFAULT_BASE_NAME = "clarification_evaluation"


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
    """CLI entry point for running clarification evaluation benchmark."""
    parser = argparse.ArgumentParser(
        description="Run QueryPilot deterministic structured clarification benchmark."
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
    print(" QueryPilot B13-EVAL3 - Deterministic Clarification Benchmark")
    print("=" * 80)
    print(f"Timestamp:   {datetime.now(timezone.utc).isoformat()}")
    print("Environment: 100% Offline (Deterministic Models & Mocked Database Dependencies)")
    print(f"Output JSON: {args.output_dir / (args.base_name + '.json')}")
    print(f"Report MD:   {args.output_dir / (args.base_name + '_report.md')}")
    print("-" * 80)

    git_commit = get_git_commit()
    scenarios = build_clarification_scenarios()
    print(f"Loaded {len(scenarios)} deterministic clarification scenarios.")
    print("Executing evaluation...")

    artifact = run_clarification_evaluation(scenarios=scenarios, git_commit=git_commit)
    metrics = artifact.metrics

    print("\n" + "=" * 80)
    print(" Scenario Results Summary")
    print("=" * 80)
    print(f"{'ID':<8} {'Status':<8} {'Category':<36} {'Scenario Name'}")
    print("-" * 80)

    for r in artifact.scenario_results:
        status_label = "PASS" if r.passed else "FAIL"
        print(f"{r.scenario_id:<8} {status_label:<8} {r.category:<36} {r.name}")

    print("-" * 80)
    print(f"Scenarios Total:  {metrics.total_scenarios}")
    print(f"Scenarios Passed: {metrics.passed_scenarios} ({metrics.pass_rate:.1%})")
    print(f"Scenarios Failed: {metrics.failed_scenarios}")
    print(f"Ambiguity Patterns Verified:    {metrics.ambiguity_types_verified} / 4")
    print(f"Negative Controls Verified:      {metrics.negative_controls_verified} / 2")
    print(f"Reformulation Workflows Verified:{metrics.resolution_workflows_verified} / 2")
    print(f"Bounds & Invariants Verified:    {metrics.validation_invariants_verified} / 2")
    print("=" * 80)

    # Save artifacts
    try:
        json_path, md_path = save_clarification_evaluation(
            artifact=artifact,
            output_dir=args.output_dir,
            base_name=args.base_name,
        )
        print(f"\n[OK] JSON artifact saved: {json_path}")
        print(f"[OK] Markdown report saved: {md_path}")
    except Exception as exc:
        print(f"\n[ERROR] Failed to save evaluation artifacts: {exc}", file=sys.stderr)
        return 1

    if metrics.failed_scenarios > 0:
        print(f"\n[FAIL] {metrics.failed_scenarios} scenario(s) failed evaluation.", file=sys.stderr)
        return 1

    print("\n[SUCCESS] All 10 clarification evaluation scenarios passed successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
