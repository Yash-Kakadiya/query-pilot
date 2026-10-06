"""Manually-invoked smoke test demonstrating Gemini candidate SQL generation.

Executes a live call to Google Gemini to translate a natural language question into
candidate SQL, validates the structured contract response, and verifies it with the
Task 1.8 deterministic validator.

STRICT SAFETY: Does NOT execute the generated SQL. Never prints the API key.
"""

import sys
from pathlib import Path

# Add src to Python path if executed standalone
root_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root_dir / "src"))

from query_pilot.config import get_settings
from query_pilot.db.connection import create_db_engine
from query_pilot.db.introspection import DatabaseIntrospector
from query_pilot.sql import (
    GeminiSQLGenerator,
    SQLGenerationRequest,
    validate_sql,
)


def run_gemini_smoke_test():
    print("=" * 70)
    print("QueryPilot Task 1.9 — Gemini SQL Generation Smoke Test")
    print("=" * 70)

    # 1. Verify Configuration
    try:
        settings = get_settings()
        print(f"[1/5] Loaded settings. Target model: {settings.GEMINI_MODEL}")
        print("      GEMINI_API_KEY is configured and masked: [SECURE]")
    except Exception as exc:
        print(f"FAILED: Could not load Gemini settings: {exc}")
        sys.exit(1)

    # 2. Introspect Model-Facing Schema
    print("[2/5] Introspecting model-facing schema from GradeSense_Local...")
    try:
        engine = create_db_engine()
        introspector = DatabaseIntrospector(engine)
        schema = introspector.introspect_model_facing()
        print(f"      Discovered {len(schema.tables)} tables in schema (is_model_facing={schema.is_model_facing}).")
        users_table = schema.get_table("Users")
        has_hash = "PasswordHash" in users_table.column_names if users_table else False
        print(f"      PasswordHash in model-facing Users table: {has_hash} (expected: False)")
    except Exception as exc:
        print(f"FAILED: Schema introspection failed: {exc}")
        sys.exit(1)

    # 3. Create Generation Request
    question = "List the first 5 students with their full names."
    print(f"[3/5] Formulating user request:")
    print(f"      Question: '{question}'")
    request = SQLGenerationRequest(
        question=question,
        schema_context=schema,
        dialect="T-SQL",
    )

    # 4. Generate Candidate SQL via Gemini
    print("[4/5] Calling Gemini SQL Generator (structured output)...")
    try:
        generator = GeminiSQLGenerator(settings=settings)
        response = generator.generate(request)
        print("      Gemini returned structured SQLGenerationResponse successfully:")
        print("      " + "-" * 60)
        print("      Candidate SQL:")
        for line in response.sql.strip().splitlines():
            print(f"        {line}")
        print("      " + "-" * 60)
        if response.explanation:
            print(f"      Explanation: {response.explanation}")
        if response.assumptions:
            print(f"      Assumptions: {response.assumptions}")
    except Exception as exc:
        print(f"FAILED: Gemini SQL generation failed: {exc}")
        sys.exit(1)

    # 5. Deterministic Validation (Task 1.8 Validator)
    print("[5/5] Passing candidate SQL to Task 1.8 deterministic validator...")
    val_result = validate_sql(response.sql, schema)
    print(f"      Validation Allowed: {val_result.allowed}")
    print(f"      Referenced Tables: {val_result.referenced_tables}")
    print(f"      Referenced Columns: {val_result.referenced_columns}")
    if val_result.reasons:
        for r in val_result.reasons:
            print(f"      Rejection Reason [{r.code}]: {r.message}")
    if val_result.normalized_sql:
        print(f"      Normalized SQL: {val_result.normalized_sql}")

    print("=" * 70)
    print("SMOKE TEST COMPLETE: Candidate SQL was generated and validated.")
    print("CONFIRMATION: ZERO SQL execution was performed. Database untouched.")
    print("=" * 70)


if __name__ == "__main__":
    run_gemini_smoke_test()
