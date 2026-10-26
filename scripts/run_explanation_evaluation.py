"""Dedicated CLI Runner for QueryPilot Deterministic Explanation Evaluation (Milestone B13-EVAL2).

Executes the 12 canonical explanation evaluation scenarios offline:
- Exercises scalar template formatting and neutral fallback (EXP-01, EXP-02, EXP-03).
- Validates deterministic empty-result short-circuiting (EXP-04).
- Tests heuristic detection of currency symbols and out-of-bounds numbers (EXP-05, EXP-06).
- Verifies unverified narrative tagging for multi-row LLM output (EXP-07).
- Enforces privacy boundaries and zero row disclosure (EXP-08, EXP-09).
- Evaluates AST SQL literal masking and fail-closed syntax protections (EXP-10).
- Verifies truncation disclosures and provider timeout/failure fallbacks (EXP-11, EXP-12).
- Generates JSON and Markdown artifacts under data/evaluation/.
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

from tests.eval_explanation import (
    ExplanationEvaluationArtifact,
    build_explanation_scenarios,
    generate_explanation_markdown_report,
    run_explanation_evaluation,
    save_explanation_evaluation,
)

logger = logging.getLogger(__name__)

DEFAULT_OUTPUT_DIR = ROOT_DIR / "data" / "evaluation"
DEFAULT_BASE_NAME = "explanation_evaluation"


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
    """CLI entry point for running explanation evaluation."""
    parser = argparse.ArgumentParser(
        description="Run QueryPilot deterministic result explanation & faithfulness evaluation benchmark."
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
    print(" QueryPilot B13-EVAL2 - Deterministic Explanation & Faithfulness Benchmark")
    print("=" * 80)
    print(f"Timestamp:   {datetime.now(timezone.utc).isoformat()}")
    print("Environment: 100% Offline (Mocked Model & Database Dependencies)")
    print(f"Output JSON: {args.output_dir / (args.base_name + '.json')}")
    print(f"Report MD:   {args.output_dir / (args.base_name + '_report.md')}")
    print("-" * 80)

    git_commit = get_git_commit()
    scenarios = build_explanation_scenarios()
    print(f"Loaded {len(scenarios)} deterministic explanation scenarios.")
    print("Executing evaluation...")

    artifact = run_explanation_evaluation(scenarios=scenarios, git_commit=git_commit)
    metrics = artifact.metrics

    print("\n" + "=" * 80)
    print(" Scenario Results Summary")
    print("=" * 80)
    print(f"{'ID':<8} {'Status':<8} {'Category':<26} {'Scenario Name'}")
    print("-" * 80)

    for r in artifact.scenario_results:
        badge = "[PASS]" if r.passed else "[FAIL]"
        print(f"{r.scenario_id:<8} {badge:<8} {r.category:<26} {r.name}")
        if not r.passed:
            print(f"         |-- [ERROR] {r.failure_reason}")

    print("-" * 80)
    print(
        f"Results: {metrics.passed_scenarios}/{metrics.total_scenarios} passed "
        f"({metrics.pass_rate:.1%}) | Failed: {metrics.failed_scenarios}"
    )
    print(f"Scalar Templates:   {metrics.scalar_templates_verified} verified")
    print(f"Hallucination Heur: {metrics.hallucination_checks_verified} verified")
    print(f"Privacy Boundaries: {metrics.privacy_boundaries_verified} verified")
    print(f"Failure Fallbacks:  {metrics.failure_fallbacks_verified} verified")

    # Persist artifacts
    json_path, md_path = save_explanation_evaluation(
        artifact=artifact,
        output_dir=args.output_dir,
        base_name=args.base_name,
    )
    print(f"\nSaved structured results: {json_path}")
    print(f"Saved evaluation report:  {md_path}")
    print("=" * 80)

    # Return exit code 0 if all passed, 1 otherwise
    return 0 if metrics.failed_scenarios == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
