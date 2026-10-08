"""Real Gemini Smoke Test for Task 1.13 Structured Unsupported-Question Handling.

Tests:
1. Supported Question: "How many students are there?"
   Expected: status = answerable, candidate SQL generated, validated, and executed.
2. Unsupported Question: "What is the tuition fee amount paid by each student?"
   Expected: status = unsupported, sql = null, validator NOT invoked, executor NOT invoked.
3. Database Invariance Verification against GradeSense_Local (17 tables, 7077 rows, 32 FKs).
"""

import sys
from pathlib import Path

# Add src to Python path if executed standalone
root_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root_dir / "src"))

from sqlalchemy import text
from query_pilot.config import get_settings
from query_pilot.db.connection import create_db_engine
from query_pilot.db.introspection import DatabaseIntrospector
from query_pilot.pipeline import QueryPipeline, PipelineStatus
from query_pilot.sql.generation import SQLGenerationStatus


def run_smoke_test():
    print("=" * 75)
    print("QueryPilot Task 1.13 — Structured Unsupported-Question Handling Smoke Test")
    print("=" * 75)

    # 1. Setup Pipeline with Real Gemini Provider
    settings = get_settings()
    print(f"[1/4] Loaded settings. Model: {settings.GEMINI_MODEL}")
    engine = create_db_engine()
    introspector = DatabaseIntrospector(engine)
    schema = introspector.introspect_model_facing()
    pipeline = QueryPipeline(engine=engine, schema=schema)

    # --------------------------------------------------------------------------
    # 2. Case A: Supported Question
    # --------------------------------------------------------------------------
    q_supported = "How many students are there?"
    print(f"\n[2/4] Testing Supported Question:")
    print(f"      Question: '{q_supported}'")
    res_supported = pipeline.run(q_supported)

    print(f"      Pipeline Status:   {res_supported.status.value}")
    print(f"      Generation Status: {res_supported.generation_status.value if res_supported.generation_status else None}")
    print(f"      Generated SQL:     {res_supported.generated_sql}")
    print(f"      Explanation:       {res_supported.generation_explanation}")
    print(f"      Validation OK:     {res_supported.validation_result.allowed if res_supported.validation_result else None}")
    print(f"      Executed Rows:     {res_supported.execution_result.rows if res_supported.execution_result else None}")

    assert res_supported.status == PipelineStatus.SUCCESS, f"Expected SUCCESS, got {res_supported.status}"
    assert res_supported.generation_status == SQLGenerationStatus.ANSWERABLE, f"Expected ANSWERABLE, got {res_supported.generation_status}"
    assert res_supported.generated_sql is not None and len(res_supported.generated_sql.strip()) > 0
    assert res_supported.validation_result is not None and res_supported.validation_result.allowed is True
    assert res_supported.execution_result is not None
    print("      --> Supported question test PASSED.")

    # --------------------------------------------------------------------------
    # 3. Case B: Unsupported Question
    # --------------------------------------------------------------------------
    q_unsupported = "What is the tuition fee amount paid by each student?"
    print(f"\n[3/4] Testing Unsupported Question:")
    print(f"      Question: '{q_unsupported}'")
    res_unsupported = pipeline.run(q_unsupported)

    print(f"      Pipeline Status:   {res_unsupported.status.value}")
    print(f"      Generation Status: {res_unsupported.generation_status.value if res_unsupported.generation_status else None}")
    print(f"      Generated SQL:     {res_unsupported.generated_sql}")
    print(f"      Explanation:       {res_unsupported.generation_explanation}")
    print(f"      Assumptions:       {res_unsupported.generation_assumptions}")
    print(f"      Validation Result: {res_unsupported.validation_result}")
    print(f"      Execution Result:  {res_unsupported.execution_result}")

    assert res_unsupported.status == PipelineStatus.UNSUPPORTED, f"Expected UNSUPPORTED, got {res_unsupported.status}"
    assert res_unsupported.generation_status == SQLGenerationStatus.UNSUPPORTED, f"Expected UNSUPPORTED, got {res_unsupported.generation_status}"
    assert res_unsupported.generated_sql is None, f"Expected SQL to be None, got: {res_unsupported.generated_sql}"
    assert res_unsupported.validation_result is None, "Validator should NOT have been invoked for unsupported query"
    assert res_unsupported.execution_result is None, "Executor should NOT have been invoked for unsupported query"
    print("      --> Unsupported question test PASSED (zero validation, zero execution).")

    # --------------------------------------------------------------------------
    # 4. Database Invariance Verification
    # --------------------------------------------------------------------------
    print("\n[4/4] Verifying database invariance against GradeSense_Local...")
    with engine.connect() as conn:
        # Table count
        table_count = conn.execute(
            text("SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_TYPE = 'BASE TABLE';")
        ).scalar()
        print(f"      Total tables: {table_count} (expected: 17)")
        assert table_count == 17, f"Expected 17 tables, got {table_count}"

        # Row count
        row_count_sql = text("""
            SELECT SUM(p.rows)
            FROM sys.tables t
            JOIN sys.partitions p ON t.object_id = p.object_id
            WHERE p.index_id IN (0, 1) AND t.is_ms_shipped = 0;
        """)
        total_rows = conn.execute(row_count_sql).scalar()
        print(f"      Total rows:   {total_rows} (expected: 7077)")
        assert total_rows == 7077, f"Expected 7077 rows, got {total_rows}"

        # Foreign keys count & active/trusted status
        fk_sql = text("""
            SELECT COUNT(*) AS total_fks,
                   SUM(CASE WHEN is_disabled = 0 AND is_not_trusted = 0 THEN 1 ELSE 0 END) AS active_trusted_fks
            FROM sys.foreign_keys;
        """)
        fk_row = conn.execute(fk_sql).fetchone()
        print(f"      Foreign keys: {fk_row.active_trusted_fks} active/trusted of {fk_row.total_fks} (expected: 32)")
        assert fk_row.total_fks == 32 and fk_row.active_trusted_fks == 32

    print("\n" + "=" * 75)
    print("SMOKE TEST COMPLETE: All checks PASSED successfully.")
    print("=" * 75)


if __name__ == "__main__":
    run_smoke_test()
