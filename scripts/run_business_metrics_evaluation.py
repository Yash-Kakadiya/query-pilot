"""Dedicated CLI Runner for QueryPilot Business Metrics & Schema Retrieval Evaluation (Milestone B23-A).

Evaluates 10 canonical offline scenarios verifying deterministic business metric matching,
conversational follow-up gating, entity compatibility, budget caps, and schema budget enforcement:
- BM-01: Exact metric name matching
- BM-02: Substantive description matching
- BM-03: Conversational follow-up continuity ("them")
- BM-04: Topic change anti-bleeding (stale context isolation)
- BM-05: Entity incompatibility blocking ("What about those courses?")
- BM-06: Top-3 metric selection budget cap
- BM-07: 1,200-character rendered prompt budget cap
- BM-08: 48 KB (49,152 bytes) formatted schema context budget enforcement
- BM-09: Formula literal text safety (inert text)
- BM-10: Request isolation (empty/none metrics)
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from query_pilot.api.models import (
    BusinessMetricDefinition,
    ColumnSchemaContext,
    ModelFacingSchemaContext,
    TableSchemaContext,
)
from query_pilot.rag.metrics_matcher import (
    MAX_SELECTED_METRICS,
    MAX_RENDERED_METRICS_CHARS,
    match_business_metrics,
    render_business_metrics_context,
)
from query_pilot.sql.generation import format_schema_context


def run_evaluation() -> Dict[str, Any]:
    schema = ModelFacingSchemaContext(
        database_name="AcademicDW",
        tables=[
            TableSchemaContext(
                table_name="Students",
                columns=[
                    ColumnSchemaContext(name="Id", data_type="int", primary_key=True),
                    ColumnSchemaContext(name="FullName", data_type="nvarchar(100)"),
                ],
                primary_keys=["Id"],
            ),
            TableSchemaContext(
                table_name="CourseEnrollments",
                columns=[
                    ColumnSchemaContext(name="Id", data_type="int", primary_key=True),
                    ColumnSchemaContext(name="StudentId", data_type="int"),
                    ColumnSchemaContext(name="AttendancePercentage", data_type="decimal(5,2)"),
                ],
                primary_keys=["Id"],
            ),
            TableSchemaContext(
                table_name="Courses",
                columns=[
                    ColumnSchemaContext(name="Id", data_type="int", primary_key=True),
                    ColumnSchemaContext(name="Title", data_type="nvarchar(100)"),
                ],
                primary_keys=["Id"],
            ),
        ],
    )
    schema_tables = [t.table_name for t in schema.tables]

    metric_at_risk = BusinessMetricDefinition(
        name="At-Risk Attendance",
        description="Identifies students whose course attendance is under 75 percent.",
        calculation_formula="AttendancePercentage < 75.0",
        applicable_tables=["CourseEnrollments", "Students"],
    )
    metric_honors = BusinessMetricDefinition(
        name="Dean's Honors",
        description="Students achieving academic excellence with high marks.",
        calculation_formula="GPA >= 3.8",
        applicable_tables=["Students"],
    )
    metric_course_cap = BusinessMetricDefinition(
        name="Course Capacity",
        description="Total student enrollment relative to course seat limits.",
        calculation_formula="COUNT(StudentId) / MaxSeats",
        applicable_tables=["Courses", "CourseEnrollments"],
    )

    all_metrics = [metric_at_risk, metric_honors, metric_course_cap]
    results: List[Dict[str, Any]] = []

    # Scenario 1: Exact metric name match
    m1 = match_business_metrics(metrics=all_metrics, question="What is the at-risk attendance?", schema=schema_tables)
    r1_pass = len(m1) >= 1 and m1[0].name == "At-Risk Attendance"
    results.append({"id": "BM-01", "name": "Exact metric name match", "passed": r1_pass, "matched": [m.name for m in m1]})

    # Scenario 2: Substantive description match
    m2 = match_business_metrics(metrics=all_metrics, question="Find learners who have high marks and academic excellence", schema=schema_tables)
    r2_pass = len(m2) >= 1 and m2[0].name == "Dean's Honors"
    results.append({"id": "BM-02", "name": "Substantive description match", "passed": r2_pass, "matched": [m.name for m in m2]})

    # Scenario 3: Conversational follow-up continuity ("them")
    m3 = match_business_metrics(
        metrics=all_metrics,
        question="Filter them by computer science department.",
        schema=schema_tables,
        resolved_intent="At-Risk Attendance for enrolled students",
    )
    r3_pass = any(m.name == "At-Risk Attendance" for m in m3)
    results.append({"id": "BM-03", "name": "Conversational follow-up continuity", "passed": r3_pass, "matched": [m.name for m in m3]})

    # Scenario 4: Topic change anti-bleeding
    m4 = match_business_metrics(
        metrics=all_metrics,
        question="Show all available classrooms in building A.",
        schema=schema_tables,
        resolved_intent="At-Risk Attendance for enrolled students",
    )
    r4_pass = len(m4) == 0
    results.append({"id": "BM-04", "name": "Topic change anti-bleeding", "passed": r4_pass, "matched": [m.name for m in m4]})

    # Scenario 5: Entity incompatibility blocking
    m5 = match_business_metrics(
        metrics=[metric_at_risk],
        question="What about those courses?",
        schema=schema_tables,
        resolved_intent="At-Risk Attendance for enrolled students",
    )
    r5_pass = len(m5) == 0
    results.append({"id": "BM-05", "name": "Entity incompatibility blocking", "passed": r5_pass, "matched": [m.name for m in m5]})

    # Scenario 6: Top-3 metric budget cap
    many_metrics = [
        BusinessMetricDefinition(name=f"Metric {i}", description=f"Relevant metric calculation {i}", calculation_formula=f"col_{i} > 0")
        for i in range(1, 6)
    ]
    m6 = match_business_metrics(metrics=many_metrics, question="Relevant metric calculation", schema=schema_tables)
    r6_pass = len(m6) <= MAX_SELECTED_METRICS
    results.append({"id": "BM-06", "name": "Top-3 metric selection budget cap", "passed": r6_pass, "count": len(m6)})

    # Scenario 7: 1,200 character rendered budget cap
    long_desc = "Very long business metric description detailed notes " * 8  # ~424 chars (< 500 max limit)
    big_metrics = [
        BusinessMetricDefinition(name=f"Big Metric {i}", description=long_desc, calculation_formula="col > 10")
        for i in range(3)
    ]
    rendered = render_business_metrics_context(big_metrics)
    r7_pass = len(rendered) <= MAX_RENDERED_METRICS_CHARS
    results.append({"id": "BM-07", "name": "1,200-char rendered prompt budget cap", "passed": r7_pass, "rendered_chars": len(rendered)})

    # Scenario 8: 48 KB (49,152 bytes) formatted schema context budget enforcement on oversized schema
    from query_pilot.db.models import ColumnMetadata, DatabaseSchema, TableMetadata
    heavy_tables = [
        TableMetadata(
            schema_name="dbo",
            table_name=f"Entity_{t_idx:02d}",
            columns=[
                ColumnMetadata(
                    name=f"Col_{c_idx:02d}",
                    data_type="nvarchar(255)",
                    comment=f"Long descriptive comment for column {c_idx} in table {t_idx} explaining business logic and usage rules.",
                )
                for c_idx in range(25)
            ],
            comment=f"Table comment for Entity_{t_idx:02d} detailing domain relationships, lifecycle, retention policies, and compliance.",
        )
        for t_idx in range(30)
    ]
    schema_heavy = DatabaseSchema(database_name="HeavyDW", tables=heavy_tables)
    formatted_heavy = format_schema_context(schema_heavy)
    heavy_bytes = len(formatted_heavy.encode("utf-8"))
    r8_pass = heavy_bytes <= 49152 and heavy_bytes > 10000 and "DATABASE DIALECT:" in formatted_heavy
    results.append({"id": "BM-08", "name": "48 KB formatted schema budget enforcement on oversized schema", "passed": r8_pass, "utf8_bytes": heavy_bytes})

    # Scenario 9: Formula literal text safety
    rendered_formula = render_business_metrics_context([metric_at_risk])
    r9_pass = "Formula (Literal Text): AttendancePercentage < 75.0" in rendered_formula and "=== BUSINESS METRIC DEFINITIONS (INFORMATIONAL ONLY) ===" in rendered_formula
    results.append({"id": "BM-09", "name": "Formula literal text safety", "passed": r9_pass})

    # Scenario 10: Request isolation (empty/none metrics)
    m10_none = match_business_metrics(metrics=None, question="Test question", schema=schema_tables)
    m10_empty = match_business_metrics(metrics=[], question="Test question", schema=schema_tables)
    r10_pass = m10_none == [] and m10_empty == []
    results.append({"id": "BM-10", "name": "Request isolation on empty/none metrics", "passed": r10_pass})

    total = len(results)
    passed = sum(1 for r in results if r["passed"])
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_scenarios": total,
        "passed_scenarios": passed,
        "pass_rate": passed / total if total > 0 else 0.0,
        "scenarios": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run QueryPilot business metrics & schema retrieval benchmark.")
    parser.add_argument("--json", action="store_true", help="Output JSON results to stdout")
    args = parser.parse_args()

    data = run_evaluation()
    print("=" * 70)
    print(" QueryPilot Business Metrics & Schema Retrieval Evaluation")
    print("=" * 70)
    print(f"Timestamp: {data['timestamp']}")
    print(f"Scenarios: {data['passed_scenarios']} / {data['total_scenarios']} passed ({data['pass_rate'] * 100:.1f}%)\n")

    for s in data["scenarios"]:
        status = "[PASS]" if s["passed"] else "[FAIL]"
        print(f"  {status} {s['id']}: {s['name']}")

    # Save artifact
    out_dir = ROOT_DIR / "data" / "evaluation"
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "business_metrics_evaluation.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    md_path = out_dir / "business_metrics_evaluation_report.md"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("# Business Metrics & Schema Retrieval Evaluation Report\n\n")
        f.write(f"- **Execution Timestamp**: {data['timestamp']}\n")
        f.write(f"- **Total Scenarios**: {data['total_scenarios']}\n")
        f.write(f"- **Passed Scenarios**: {data['passed_scenarios']}\n")
        f.write(f"- **Pass Rate**: {data['pass_rate'] * 100:.1f}%\n\n")
        f.write("| Scenario ID | Scenario Name | Status |\n")
        f.write("|---|---|---|\n")
        for s in data["scenarios"]:
            f.write(f"| **{s['id']}** | {s['name']} | {'PASS' if s['passed'] else 'FAIL'} |\n")
        f.write("\n\n*Evaluation methodology: 100% deterministic offline fixtures; zero external model or network calls.*\n")

    print(f"\nSaved artifacts:\n  - {json_path}\n  - {md_path}")
    return 0 if data["passed_scenarios"] == data["total_scenarios"] else 1


if __name__ == "__main__":
    sys.exit(main())
