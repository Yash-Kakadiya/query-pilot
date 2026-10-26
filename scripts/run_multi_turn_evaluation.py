"""Dedicated CLI Runner for QueryPilot Deterministic Multi-Turn Evaluation (Milestone B13-M1).

Executes the 10 canonical multi-turn evaluation scenarios offline:
- Exercises conversational context bounding (MAX_CONVERSATION_TURNS = 3).
- Validates FIFO sliding-window eviction across 4+ sequential turns.
- Verifies failure context preservation and unapproved candidate isolation.
- Enforces topic and profile boundary clearing.
- Tests prohibited data exclusion (zero SQL, rows, or credentials in context).
- Replays existing baseline case C002 and isolates LangGraph recovery internals.
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

from tests.eval_multi_turn import (
    MultiTurnEvaluationArtifact,
    build_multi_turn_scenarios,
    generate_multi_turn_markdown_report,
    run_multi_turn_evaluation,
    save_multi_turn_evaluation,
)

logger = logging.getLogger(__name__)

DEFAULT_OUTPUT_DIR = ROOT_DIR / "data" / "evaluation"
DEFAULT_BASE_NAME = "multi_turn_evaluation"


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
    """CLI entry point for running multi-turn evaluation."""
    parser = argparse.ArgumentParser(
        description="Run QueryPilot deterministic multi-turn conversational benchmark."
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
    print(" QueryPilot B13-M1 - Deterministic Multi-Turn Conversational Benchmark")
    print("=" * 80)
    print(f"Timestamp:   {datetime.now(timezone.utc).isoformat()}")
    print("Environment: 100% Offline (Mocked Model & Database Dependencies)")
    print(f"Output JSON: {args.output_dir / (args.base_name + '.json')}")
    print(f"Report MD:   {args.output_dir / (args.base_name + '_report.md')}")
    print("-" * 80)

    git_commit = get_git_commit()
    scenarios = build_multi_turn_scenarios()
    print(f"Loaded {len(scenarios)} deterministic multi-turn scenarios.")
    print("Executing evaluation...")

    artifact = run_multi_turn_evaluation(scenarios=scenarios, git_commit=git_commit)
    metrics = artifact.metrics

    print("\n" + "=" * 80)
    print(" Scenario Results Summary")
    print("=" * 80)
    print(f"{'ID':<8} {'Status':<8} {'Category':<28} {'Scenario Name'}")
    print("-" * 80)

    for r in artifact.scenario_results:
        badge = "[PASS]" if r.passed else "[FAIL]"
        print(f"{r.scenario_id:<8} {badge:<8} {r.category:<28} {r.name}")
        if not r.passed:
            for err in r.assertion_details:
                print(f"         |-- [ERROR] {err}")

    print("-" * 80)
    print(f"Scenarios:                     {metrics.passed_scenarios}/{metrics.total_scenarios} passed ({metrics.pass_rate * 100:.1f}%)")
    print(f"Context Violations Prevented:  {metrics.context_violations_prevented}")
    print(f"FIFO Evictions Verified:       {metrics.fifo_evictions_verified}")
    print(f"Data Leak Attempts Blocked:    {metrics.data_leak_attempts_blocked}")
    print(f"Total Turns Evaluated:         {metrics.total_turns_executed}")
    print("=" * 80)

    # Persist artifacts
    try:
        json_path, md_path = save_multi_turn_evaluation(
            artifact=artifact,
            output_dir=args.output_dir,
            base_name=args.base_name,
        )
        print("\n[INFO] Evaluation artifacts successfully written:")
        print(f"  - JSON:   {json_path}")
        print(f"  - Report: {md_path}")
    except Exception as exc:
        print(f"\n[FATAL] Error writing evaluation artifacts: {exc}")
        return 1

    if metrics.failed_scenarios > 0:
        print(f"\n[FAILED] {metrics.failed_scenarios} scenario(s) failed.")
        return 1

    print("\n[SUCCESS] All deterministic multi-turn scenarios and security assertions PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
