"""Controlled CLI demo for QueryPilot Text-to-SQL Pipeline (Task 1.10).

Demonstrates the strict safety pipeline on live GradeSense_Local:
    Natural-language question
            ↓
    Gemini SQL generation (GeminiSQLGenerator)
            ↓
    Deterministic SQL validation (validate_sql)
            ↓
    Conditional read-only execution (execute_read_only)
            ↓
    Structured result display

Does NOT expose secrets or database connection strings.
Does NOT execute SQL if validation fails.
"""

import argparse
import json
import logging
import sys

from query_pilot.config import get_settings
from query_pilot.db.connection import create_db_engine
from query_pilot.pipeline import PipelineStatus, QueryPipeline


def main():
    parser = argparse.ArgumentParser(
        description="QueryPilot Controlled Text-to-SQL Pipeline Runner",
    )
    parser.add_argument(
        "question",
        nargs="?",
        default="List the first 5 students with their full names.",
        help="Natural-language question to answer (default: 'List the first 5 students with their full names.')",
    )
    parser.add_argument(
        "--max-rows",
        type=int,
        default=10,
        help="Maximum rows to return from execution (default: 10)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )

    args = parser.parse_args()

    log_level = logging.DEBUG if args.debug else logging.WARNING
    logging.basicConfig(level=log_level, format="%(levelname)s: %(message)s")

    print("=" * 70)
    print(" QueryPilot Text-to-SQL Controlled Pipeline")
    print("=" * 70)

    # Verify Gemini API key is configured
    settings = get_settings()
    if not settings.GEMINI_API_KEY:
        print("\n[ERROR] GEMINI_API_KEY is not configured in environment or .env file.")
        sys.exit(1)

    raw_key = settings.GEMINI_API_KEY.get_secret_value()
    masked_key = raw_key[:4] + "..." + raw_key[-4:] if len(raw_key) >= 8 else "***"
    print(f"Target Database : GradeSense_Local (Verified Local Session Context)")
    print(f"Target LLM Model: {settings.GEMINI_MODEL} (API Key: {masked_key})")
    print(f"User Question   : \"{args.question}\"")
    print("-" * 70)

    # Initialize pipeline
    print("\n[Stage A] Acquiring model-facing schema metadata...")
    engine = create_db_engine()
    pipeline = QueryPipeline(engine=engine, max_rows=args.max_rows)
    schema = pipeline.get_schema()
    print(f"  -> Introspected {len(schema.tables)} tables with sensitive fields redacted.")

    print("\n[Stage B] Invoking Gemini SQL generator for candidate query...")
    result = pipeline.run(args.question)

    print(f"  -> Generation Status: {'OK' if result.generated_sql else 'FAILED'}")
    if result.generated_sql:
        print("  -> Candidate SQL:")
        for line in result.generated_sql.strip().splitlines():
            print(f"       {line}")
        if result.generation_explanation:
            print(f"  -> Explanation: {result.generation_explanation}")
        if result.generation_assumptions:
            print(f"  -> Assumptions: {result.generation_assumptions}")

    # Stage C: Validation
    print("\n[Stage C] Deterministic SQL Validation (Task 1.8 Policy Layer)...")
    if result.validation_result:
        val = result.validation_result
        print(f"  -> Validation Decision : {'APPROVED (Allowed)' if val.allowed else 'REJECTED'}")
        if val.referenced_tables:
            print(f"  -> Referenced Tables   : {val.referenced_tables}")
        if val.referenced_columns:
            print(f"  -> Referenced Columns  : {val.referenced_columns}")
        if val.reasons:
            print("  -> Validation Reasons  :")
            for r in val.reasons:
                print(f"       - [{r.code.value}] {r.message}")
        if val.warnings:
            print(f"  -> Warnings            : {val.warnings}")
    else:
        print("  -> Validation was not performed (stopped before validation stage).")

    # Stage D & E: Execution & Pipeline Result
    print("\n[Stage D & E] Conditional Safe Execution & Final Result...")
    print(f"  -> Overall Pipeline Status: {result.status.value}")

    if result.status == PipelineStatus.SUCCESS:
        exec_res = result.execution_result
        print(f"  -> Execution Successful: {exec_res.row_count} rows retrieved ({exec_res.execution_time_ms:.2f} ms)")
        print(f"  -> Columns: {exec_res.columns}")
        print("  -> Sample Result Rows:")
        for i, row in enumerate(exec_res.rows[:5], 1):
            print(f"       [{i}] {json.dumps(row, default=str)}")
        if exec_res.row_count > 5:
            print(f"       ... and {exec_res.row_count - 5} more rows.")
    elif result.status == PipelineStatus.VALIDATION_REJECTED:
        print("  -> EXECUTION BLOCKED: The query failed safety validation and was NOT sent to SQL Server.")
        print(f"  -> Detail: {result.error_message}")
    else:
        print(f"  -> PIPELINE FAILED: {result.error_message}")

    print("=" * 70)


if __name__ == "__main__":
    main()
