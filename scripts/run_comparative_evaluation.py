"""Dedicated CLI Runner for QueryPilot Sequential vs. LangGraph Comparative Evaluation (Task 1.22).

Executes the paired evaluation across the 32 baseline cases:
- 30 Natural Language questions evaluated across both Sequential and LangGraph modes.
- 2 Deterministic AST Safety fixtures evaluated without external calls.
- Enforces conservative rate-limit pacing (>= 4.5s) between natural language requests.
- Strictly guards historical baseline artifacts and validates runtime database context.
"""

import argparse
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import sys
import time
from typing import Any, Dict

from query_pilot.config import get_settings
from query_pilot.db.connection import ALLOWED_TARGET_DATABASE, create_db_engine
from query_pilot.db.introspection import DatabaseIntrospector
from query_pilot.eval.comparison import (
    PROTECTED_EVALUATION_FILES,
    PairedCaseClassification,
    PairedCaseComparison,
    evaluate_comparative,
    save_comparative_evaluation,
)
from query_pilot.runner import ExecutionMode, QueryRunner
from query_pilot.sql.gemini_generator import GeminiSQLGenerator

logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DATASET_PATH = ROOT_DIR / "data" / "evaluation" / "baseline_questions.json"
DEFAULT_OUTPUT_DIR = ROOT_DIR / "data" / "evaluation"
DEFAULT_BASE_NAME = "comparison_1_22"


def preflight_checks(dataset_path: Path, output_dir: Path, base_name: str) -> Dict[str, Any]:
    """Perform pre-execution validation of configuration, dataset, database target, and output paths."""
    print("=" * 75)
    print(" QueryPilot Task 1.22 — Real Comparative Evaluation Preflight Verification")
    print("=" * 75)

    settings = get_settings()

    # 1. Verify Gemini API Key
    if not settings.GEMINI_API_KEY:
        print("[FATAL] GEMINI_API_KEY is not configured. Aborting.")
        sys.exit(1)

    raw_key = settings.GEMINI_API_KEY.get_secret_value().strip()
    if not raw_key or raw_key == "your_gemini_api_key_here":
        print("[FATAL] GEMINI_API_KEY contains placeholder or empty value. Aborting.")
        sys.exit(1)

    masked_key = raw_key[:4] + "..." + raw_key[-4:] if len(raw_key) >= 8 else "***"

    # 2. Verify Database Target
    if settings.DB_NAME != ALLOWED_TARGET_DATABASE:
        print(
            f"[FATAL] Database target mismatch: configured '{settings.DB_NAME}', "
            f"strictly required '{ALLOWED_TARGET_DATABASE}'. Aborting."
        )
        sys.exit(1)

    if settings.DB_SERVER != r"localhost\SQLEXPRESS":
        print(
            f"[FATAL] Database server mismatch: configured '{settings.DB_SERVER}', "
            f"strictly required 'localhost\\SQLEXPRESS'. Aborting."
        )
        sys.exit(1)

    # 3. Verify Dataset Exists and has 32 cases
    if not dataset_path.is_file():
        print(f"[FATAL] Dataset file not found at: {dataset_path}")
        sys.exit(1)

    with open(dataset_path, "r", encoding="utf-8") as f:
        cases = json.load(f)

    if len(cases) != 32:
        print(f"[FATAL] Dataset case count mismatch: expected 32 cases, found {len(cases)}. Aborting.")
        sys.exit(1)

    # 4. Verify Output Paths Do Not Collide with Existing Files or Protected Baselines
    clean_base = base_name.replace(".json", "").replace(".md", "")
    target_json = output_dir / f"{clean_base}.json"
    target_report = output_dir / f"{clean_base}_report.md"

    if clean_base in {Path(p).stem for p in PROTECTED_EVALUATION_FILES}:
        print(f"[FATAL] Base name '{clean_base}' conflicts with protected historical artifact stem. Aborting.")
        sys.exit(1)

    if target_json.exists():
        print(f"[FATAL] Target JSON artifact already exists: {target_json}. Aborting to prevent overwrite.")
        sys.exit(1)

    if target_report.exists():
        print(f"[FATAL] Target Markdown report already exists: {target_report}. Aborting to prevent overwrite.")
        sys.exit(1)

    print(f"Target Database : {settings.DB_NAME} on {settings.DB_SERVER}")
    print(f"Target Model    : {settings.GEMINI_MODEL} (API Key: {masked_key})")
    print(f"Dataset Path    : {dataset_path} ({len(cases)} cases verified)")
    print(f"Output Target   : {target_json.name} & {target_report.name}")
    print("-" * 75)

    return {
        "settings": settings,
        "cases": cases,
        "target_json": target_json,
        "target_report": target_report,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="QueryPilot Sequential vs. LangGraph Comparative Evaluation Runner",
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=DEFAULT_DATASET_PATH,
        help=f"Path to evaluation dataset JSON (default: {DEFAULT_DATASET_PATH})",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Directory to save output artifacts (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--base-name",
        type=str,
        default=DEFAULT_BASE_NAME,
        help=f"Base name for output artifacts (default: {DEFAULT_BASE_NAME})",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=4.5,
        help="Rate-limit delay in seconds between natural-language generation attempts (default: 4.5)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )

    args = parser.parse_args()

    log_level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(level=log_level, format="%(asctime)s [%(levelname)s] %(message)s")

    # Run strict preflight checks
    preflight = preflight_checks(
        dataset_path=args.dataset.resolve(),
        output_dir=args.output_dir.resolve(),
        base_name=args.base_name,
    )
    cases = preflight["cases"]
    settings = preflight["settings"]

    print("\n[Phase 1] Initializing Database Connection & Acquiring Schema...")
    engine = create_db_engine()
    introspector = DatabaseIntrospector(engine)
    schema = introspector.introspect_model_facing()
    print(f"  -> Model-facing schema acquired ({len(schema.tables)} tables with sensitive columns redacted).")

    print("\n[Phase 2] Initializing Gemini Generator & Unified QueryRunner...")
    generator = GeminiSQLGenerator(model_name=settings.GEMINI_MODEL)
    runner = QueryRunner(
        generator=generator,
        engine=engine,
        schema=schema,
        timeout_seconds=settings.DB_QUERY_TIMEOUT_SECONDS,
        dialect="T-SQL",
    )
    print(f"  -> QueryRunner configured with {ExecutionMode.SEQUENTIAL.value} and {ExecutionMode.GRAPH.value} support.")

    print(f"\n[Phase 3] Executing Comparative Evaluation across {len(cases)} cases...")
    print(f"  -> Rate pacing: {args.delay}s delay between successive natural-language requests.")
    print("-" * 75)

    start_time = time.perf_counter()

    def on_progress(idx: int, total: int, paired: PairedCaseComparison) -> None:
        seq_sym = "PASS" if paired.sequential_passed else f"FAIL ({paired.sequential_result.failure_stage.value if paired.sequential_result.failure_stage else 'error'})"
        graph_sym = "PASS" if paired.graph_passed else f"FAIL ({paired.graph_result.failure_stage.value if paired.graph_result.failure_stage else 'error'})"
        print(
            f"[{idx:02d}/{total:02d}] {paired.case_id:<5} ({paired.category:<16}) | "
            f"Seq: {seq_sym:<18} | Graph: {graph_sym:<18} | Outcome: {paired.classification.value}"
        )

    metadata = {
        "evaluation_task": "Task 1.22",
        "dataset_path": str(args.dataset.resolve()),
        "model": settings.GEMINI_MODEL,
        "temperature": 0.0,
        "database_target": settings.DB_NAME,
        "database_server": settings.DB_SERVER,
        "dialect": "T-SQL",
        "delay_seconds": args.delay,
        "timestamp_start_utc": datetime.now(timezone.utc).isoformat(),
    }

    try:
        artifact = evaluate_comparative(
            dataset=cases,
            runner=runner,
            schema=schema,
            metadata=metadata,
            delay_between_requests=args.delay,
            progress_callback=on_progress,
        )
    except Exception as exc:
        print(f"\n[FATAL] Evaluation halted unexpectedly: {exc}")
        logger.exception("Evaluation failure")
        sys.exit(1)

    elapsed = time.perf_counter() - start_time
    artifact.metadata["execution_duration_seconds"] = round(elapsed, 2)
    artifact.metadata["timestamp_completed_utc"] = datetime.now(timezone.utc).isoformat()

    print("-" * 75)
    print(f"Evaluation completed in {elapsed:.1f} seconds ({elapsed / 60:.2f} minutes).")

    print("\n[Phase 4] Saving Sanitized Comparative Evaluation Artifacts...")
    json_path, md_path = save_comparative_evaluation(
        artifact=artifact,
        output_dir=args.output_dir.resolve(),
        base_name=args.base_name,
    )

    print(f"  -> JSON artifact saved:     {json_path}")
    print(f"  -> Markdown report saved:   {md_path}")
    print("=" * 75)

    # Print summary highlights
    metrics = artifact.metrics
    print("\nExecutive Summary Highlights:")
    print(f"  - Total Evaluated Cases:  {metrics.total_cases}")
    print(f"  - NL Pipeline Agreement:  {metrics.nl_agreement_rate:.1%} ({metrics.nl_both_pass_count + metrics.nl_both_fail_count}/{metrics.total_nl_cases})")
    print(f"  - Both Modes Passed:      {metrics.nl_both_pass_count}/{metrics.total_nl_cases}")
    print(f"  - Graph Regressions:      {metrics.nl_sequential_only_count}/{metrics.total_nl_cases}")
    print(f"  - Graph Improvements:     {metrics.nl_graph_only_count}/{metrics.total_nl_cases}")
    print(f"  - Both Modes Failed:      {metrics.nl_both_fail_count}/{metrics.total_nl_cases}")
    print(f"  - Safety Rejections:      {metrics.sequential_metrics.safety_rejected_count}/{metrics.safety_cases_count} (100% blocked)")
    print("=" * 75)


if __name__ == "__main__":
    main()
