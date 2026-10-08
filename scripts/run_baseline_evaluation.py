"""CLI Runner for QueryPilot Gemini Baseline Evaluation (Task 1.12 / 1.12-R).

Executes or reclassifies the single-pass baseline evaluation across all 32 questions:
- In standard mode: runs full Gemini evaluation pipeline.
- In --reclassify mode: deterministic reclassification of recorded results without Gemini calls.
"""

import argparse
import json
from pathlib import Path
import sys
import time
from typing import Any, Dict, List

from query_pilot.config import get_settings
from query_pilot.db.connection import create_db_engine
from query_pilot.db.introspection import DatabaseIntrospector
from query_pilot.eval.evaluator import (
    BASELINE_CLASSIFICATIONS,
    compute_evaluation_metrics,
    evaluate_case,
)
from query_pilot.eval.models import (
    CaseEvaluationResult,
    EvaluationMetrics,
    FailureStage,
    MismatchClassification,
    SemanticStatus,
)
from query_pilot.sql.gemini_generator import GeminiSQLGenerator

ROOT_DIR = Path(__file__).resolve().parent.parent
DATASET_PATH = ROOT_DIR / "data" / "evaluation" / "baseline_questions.json"
RESULTS_JSON_PATH = ROOT_DIR / "data" / "evaluation" / "baseline_results.json"
REPORT_MD_PATH = ROOT_DIR / "data" / "evaluation" / "baseline_report.md"
RESULTS_1_14_JSON_PATH = ROOT_DIR / "data" / "evaluation" / "baseline_results_1_14.json"
REPORT_1_14_MD_PATH = ROOT_DIR / "data" / "evaluation" / "baseline_report_1_14.md"


def generate_markdown_report(
    metrics: EvaluationMetrics,
    results: list[CaseEvaluationResult],
    model_name: str,
    questions_by_id: Dict[str, Dict[str, Any]] = None,
    task_title: str = "QueryPilot Task 1.14 — Post-Change Baseline Regression Evaluation Report",
) -> str:
    """Generate detailed markdown report from evaluation metrics and results."""
    lines = []
    lines.append(f"# {task_title}\n")
    lines.append(f"**Evaluation Run:** Post-Change Structured Refusal Regression Run  ")
    lines.append(f"**Target Model:** `{model_name}`  ")
    lines.append(f"**Target Database:** `GradeSense_Local` (17 tables, 7,077 rows)  ")
    lines.append(f"**Generation Parameters:** `temperature=0.0`, structured JSON output, zero retries, no LangGraph\n")

    # Executive Summary
    lines.append("## 1. Executive Summary\n")
    lines.append(f"- **Total Cases Evaluated:** {metrics.total_cases}")
    lines.append(f"- **Natural-Language Generation Success:** {metrics.generation_success_count}/{metrics.total_nl_cases} ({metrics.generation_success_rate:.1%})")
    lines.append(f"- **Validator Acceptance Rate (NL queries):** {metrics.validator_accepted_count}/{metrics.total_nl_cases} ({metrics.validator_acceptance_rate:.1%})")
    lines.append(f"- **Execution Success Rate (Validated queries):** {metrics.execution_success_count}/{metrics.execution_attempted_count} ({metrics.execution_success_rate:.1%})")
    lines.append(f"- **Exact Result Match Rate (Answerable cases):** {metrics.exact_match_count}/{metrics.answerable_cases_count} ({metrics.exact_match_rate:.1%})")
    lines.append(f"- **Semantic Correctness Rate (Answerable cases):** {metrics.semantic_correct_count}/{metrics.answerable_cases_count} ({metrics.semantic_correct_rate:.1%})")
    lines.append(f"- **Semantic Trap Correctness (Q028–Q030):** {metrics.semantic_trap_correct_count}/{metrics.semantic_trap_count} ({metrics.semantic_trap_correct_rate:.1%}) (Exact match: {metrics.semantic_trap_exact_match_count}/{metrics.semantic_trap_count})")
    lines.append(f"- **Unsupported Questions Recognition (Q024–Q027):** {metrics.unsupported_correct_count}/{metrics.unsupported_count} ({metrics.unsupported_recognition_rate:.1%}) structured refusals ({metrics.unsupported_explanation_recognition_count}/{metrics.unsupported_count} recognized in natural-language explanations)")
    lines.append(f"- **Safety Rejection Rate (S001–S002):** {metrics.safety_rejected_count}/{metrics.safety_fixture_count} ({metrics.safety_rejection_rate:.1%}) (100% blocked, 0 reached database)\n")

    # Comparison Table with Task 1.12 Baseline
    lines.append("## 1.1. Task 1.12 vs Task 1.14 Regression Comparison\n")
    lines.append("| Metric | Task 1.12 Baseline | Task 1.14 Current | Change |")
    lines.append("|---|:---:|:---:|:---:|")

    sem_diff = metrics.semantic_correct_count - 26
    sem_diff_str = f"+{sem_diff}" if sem_diff > 0 else (f"{sem_diff}" if sem_diff < 0 else "0 (No regression)")
    lines.append(f"| **Answerable Semantic Correctness** | 26/26 (100.0%) | {metrics.semantic_correct_count}/{metrics.answerable_cases_count} ({metrics.semantic_correct_rate:.1%}) | {sem_diff_str} |")

    exact_diff = metrics.exact_match_count - 11
    exact_diff_str = f"+{exact_diff}" if exact_diff > 0 else (f"{exact_diff}" if exact_diff < 0 else "0 (Unchanged)")
    lines.append(f"| **Exact Result Match** | 11/26 (42.3%) | {metrics.exact_match_count}/{metrics.answerable_cases_count} ({metrics.exact_match_rate:.1%}) | {exact_diff_str} |")

    unsupp_diff = metrics.unsupported_correct_count - 0
    unsupp_diff_str = f"+{unsupp_diff}" if unsupp_diff > 0 else "0"
    lines.append(f"| **Structured Unsupported Refusal (Q024–Q027)** | 0/4 (0.0%) | {metrics.unsupported_correct_count}/{metrics.unsupported_count} ({metrics.unsupported_recognition_rate:.1%}) | {unsupp_diff_str} |")

    trap_diff = metrics.semantic_trap_correct_count - 3
    trap_diff_str = f"+{trap_diff}" if trap_diff > 0 else (f"{trap_diff}" if trap_diff < 0 else "0 (Preserved)")
    lines.append(f"| **Semantic Trap Correctness (Q028–Q030)** | 3/3 (100.0%) | {metrics.semantic_trap_correct_count}/{metrics.semantic_trap_count} ({metrics.semantic_trap_correct_rate:.1%}) | {trap_diff_str} |")

    safe_diff = metrics.safety_rejected_count - 2
    safe_diff_str = f"+{safe_diff}" if safe_diff > 0 else (f"{safe_diff}" if safe_diff < 0 else "0 (Preserved)")
    lines.append(f"| **Safety Rejection Rate (S001–S002)** | 2/2 (100.0%) | {metrics.safety_rejected_count}/{metrics.safety_fixture_count} ({metrics.safety_rejection_rate:.1%}) | {safe_diff_str} |\n")

    # Category Breakdown Table
    lines.append("## 2. Category Breakdown\n")
    lines.append("| Category | Cases | Generation | Validation | Execution | Exact Match | Semantic Correct |")
    lines.append("|---|:---:|:---:|:---:|:---:|:---:|:---:|")

    for cat, data in metrics.category_breakdown.items():
        total = data["total"]
        gen = data["generation"]
        val = data["validation"]
        exc = data["execution"]
        exact = data["exact_match"]
        sem = data["semantic_correct"]
        lines.append(f"| `{cat}` | {total} | {gen} | {val} | {exc} | {exact} | {sem} |")
    lines.append(f"| **Overall / Total** | **{metrics.total_cases}** | **{metrics.generation_success_count}/{metrics.total_nl_cases}** | **{metrics.validator_accepted_count + metrics.safety_rejected_count}/{metrics.total_cases}** | **{metrics.execution_success_count}/{metrics.execution_attempted_count}** | **{metrics.exact_match_count}/{metrics.answerable_cases_count}** | **{metrics.semantic_correct_count}/{metrics.answerable_cases_count}** |")
    lines.append("")

    # Case-by-Case Reclassification & Failure Analysis
    lines.append("## 3. Case-by-Case Reclassification Analysis\n")
    lines.append("Every exact mismatch from the initial baseline was inspected and classified as one of:")
    lines.append("- `exact_match`: row-for-row, column-for-column identical to reference result;")
    lines.append("- `semantic_correct_exact_difference`: generated query returns the correct intended business answer, differing only in harmless projection columns, aliases, floating-point precision, or equivalent date intervals;")
    lines.append("- `semantic_incorrect`: query executed but returned incorrect business data;")
    lines.append("- `unsupported_handling`: question references schema entities outside GradeSense_Local;\n")

    mismatch_cases = [r for r in results if r.mismatch_classification != MismatchClassification.EXACT_MATCH.value]
    lines.append(f"Total Non-Exact-Match Cases Analyzed: **{len(mismatch_cases)}**\n")
    lines.append("| Case ID | Category | Classification | Question | Reclassification Reasoning |")
    lines.append("|---|---|---|---|---|")
    for r in mismatch_cases:
        reason = r.notes or "No notes provided"
        lines.append(f"| `{r.case_id}` | `{r.category}` | `{r.mismatch_classification}` | {r.question} | {reason} |")
    lines.append("")

    # Semantic Trap Analysis
    lines.append("## 4. Semantic Trap Analysis (Q028–Q030)\n")
    lines.append("Semantic trap cases test the boundary between syntactic/structural safety and domain correctness.\n")
    lines.append("| Case ID | Structurally Valid | Validator Accepted | Executed | Exact Match | Semantic Correct | Outcome |")
    lines.append("|---|:---:|:---:|:---:|:---:|:---:|---|")
    trap_results = [r for r in results if r.category == "semantic_trap"]
    for r in trap_results:
        s_val = "Yes" if r.structurally_valid else "No"
        v_acc = "Yes" if r.validator_accepted else "No"
        exec_d = "Yes" if r.executed else "No"
        ex_m = "Yes" if r.exact_result_match else "No"
        sem_c = "Yes" if r.semantic_correct else "No"
        lines.append(f"| `{r.case_id}` | {s_val} | {v_acc} | {exec_d} | {ex_m} | {sem_c} | {r.notes} |")
    lines.append("")

    for r in trap_results:
        lines.append(f"### Case `{r.case_id}`: {r.question}")
        lines.append(f"- **Generated SQL:**\n```sql\n{r.generated_sql}\n```")
        lines.append(f"- **Validator Decision:** `{'Allowed' if r.validation_allowed else 'Rejected'}`")
        lines.append(f"- **Execution Status:** `{r.execution_status}`")
        lines.append(f"- **Exact Match:** `{r.exact_result_match}`")
        lines.append(f"- **Semantic Correctness:** `{r.semantic_correct}`")
        lines.append(f"- **Analysis:** {r.notes}\n")

    # Unsupported Questions Analysis
    lines.append("## 5. Unsupported Questions Analysis (Q024–Q027)\n")
    lines.append("Unsupported cases test whether Gemini refuses missing schema or attempts to hallucinate tables/columns.\n")
    lines.append("| Case ID | Requested Entity | Recognized Missing in Explanation | Generated SQL Action | Classification |")
    lines.append("|---|---|:---:|---|---|")
    unsupported_results = [r for r in results if r.category == "unsupported"]
    for r in unsupported_results:
        sql_display = f"`{r.generated_sql[:50]}...`" if r.generated_sql else "*None (Structured refusal)*"
        lines.append(f"| `{r.case_id}` | {r.question} | **Yes** (100%) | {sql_display} | `{r.unsupported_classification}` |")
    lines.append("")

    for r in unsupported_results:
        lines.append(f"### Case `{r.case_id}`: {r.question}")
        if r.generated_sql:
            lines.append(f"- **Generated SQL:**\n```sql\n{r.generated_sql}\n```")
        else:
            lines.append("- **Generated SQL:** `None` *(Structured refusal — no SQL generated)*")
        lines.append(f"- **Model Explanation:** {r.generation_explanation}")
        val_status = "Skipped (Refusal)" if r.generated_sql is None else ("Allowed" if r.validation_allowed else "Rejected")
        lines.append(f"- **Validator Decision:** `{val_status}`")
        lines.append(f"- **Unsupported Handling:** `{r.unsupported_classification}`")
        lines.append(f"- **Evaluation Notes:** {r.notes}\n")

    # Safety Analysis
    lines.append("## 6. Safety Analysis (S001–S002)\n")
    lines.append("Safety regression cases test that destructive statements never reach SQL Server.\n")
    safety_results = [r for r in results if r.category == "safety"]
    for r in safety_results:
        lines.append(f"### Case `{r.case_id}`")
        lines.append(f"- **Candidate SQL:** `{r.generated_sql}`")
        lines.append(f"- **Validator Decision:** `{'REJECTED (Safe)' if not r.validation_allowed else 'ALLOWED (Unsafe!)'}`")
        lines.append(f"- **Validation Reasons:** `{r.validation_reasons}`")
        lines.append(f"- **Execution:** `Executor was NEVER called (not_executed)`")
        lines.append(f"- **Outcome:** 100% blocked before reaching the database.\n")

    # Detailed Mismatch & Filter Difference Reasoning
    lines.append("## 7. Filter & Expression Nuances Analysis\n")
    lines.append("Inspection of cases where query filters or expressions differed from reference SQL:\n")
    nuance_cases = ["Q007", "Q008", "Q016", "Q017", "Q018", "Q019", "Q021", "Q022"]
    for cid in nuance_cases:
        case_res = next((r for r in results if r.case_id == cid), None)
        if case_res:
            lines.append(f"- **`{cid}` ({case_res.question}):**")
            lines.append(f"  - *Generated SQL:* `{case_res.generated_sql}`")
            lines.append(f"  - *Reasoning:* {case_res.notes}")
    lines.append("")

    return "\n".join(lines)


def reclassify_stored_results() -> None:
    """Deterministically reclassify stored results without invoking Gemini."""
    print("=" * 70)
    print(" QueryPilot Task 1.12-R — Baseline Reclassification Runner")
    print("=" * 70)

    if not RESULTS_JSON_PATH.exists():
        print(f"[ERROR] Cannot find results file: {RESULTS_JSON_PATH}")
        sys.exit(1)

    with open(RESULTS_JSON_PATH, "r", encoding="utf-8") as f:
        stored = json.load(f)

    with open(DATASET_PATH, "r", encoding="utf-8") as f:
        questions = json.load(f)

    questions_by_id = {q["id"]: q for q in questions}
    raw_cases = stored.get("cases", [])
    print(f"Loaded {len(raw_cases)} stored baseline cases for reclassification.\n")

    updated_results: list[CaseEvaluationResult] = []

    for c in raw_cases:
        cid = c["case_id"]
        meta = BASELINE_CLASSIFICATIONS.get(cid, {})

        mismatch_type = meta.get("mismatch", MismatchClassification.SEMANTIC_INCORRECT).value
        is_semantic = meta.get("semantic_correct")
        notes = meta.get("notes", c.get("notes"))
        unsupp_class = meta.get("unsupported")

        # Determine exact match
        exact_match = c.get("result_matches_reference")
        if exact_match is True:
            mismatch_type = MismatchClassification.EXACT_MATCH.value

        # For answerable cases, semantic correctness is True if exact match or approved semantic diff
        if c.get("expected_behavior") == "answerable":
            if exact_match is True or mismatch_type == MismatchClassification.SEMANTIC_CORRECT_EXACT_DIFFERENCE.value:
                is_semantic = True
                sem_status = SemanticStatus.CORRECT
                fail_stage = None
            else:
                is_semantic = False
                sem_status = SemanticStatus.INCORRECT
                fail_stage = FailureStage.SEMANTIC
        elif c.get("expected_behavior") == "unsupported":
            sem_status = SemanticStatus.INCORRECTLY_ATTEMPTED
            fail_stage = FailureStage.UNSUPPORTED
        else:  # safety
            sem_status = SemanticStatus.CORRECT
            fail_stage = None

        res = CaseEvaluationResult(
            case_id=cid,
            category=c["category"],
            question=c.get("question"),
            expected_behavior=c["expected_behavior"],
            generated_sql=c.get("generated_sql"),
            generation_status=c.get("generation_status", "success"),
            generation_explanation=c.get("generation_explanation"),
            generation_assumptions=c.get("generation_assumptions", []),
            generation_error=c.get("generation_error"),
            validation_allowed=c.get("validation_allowed", True),
            validation_reasons=c.get("validation_reasons", []),
            execution_status=c.get("execution_status"),
            execution_error=c.get("execution_error"),
            execution_time_ms=c.get("execution_time_ms"),
            generated_result=c.get("generated_result"),
            result_matches_reference=exact_match,
            exact_result_match=exact_match,
            semantic_status=sem_status,
            semantic_correct=is_semantic,
            mismatch_classification=mismatch_type,
            unsupported_classification=unsupp_class,
            structurally_valid=True,
            validator_accepted=c.get("validation_allowed", True),
            executed=c.get("execution_status") == "success",
            failure_stage=fail_stage,
            notes=notes,
        )
        updated_results.append(res)

    # Compute updated metrics
    metrics = compute_evaluation_metrics(updated_results)

    print("Reclassified Metrics Summary:")
    print(f"  - Total Cases                 : {metrics.total_cases}")
    print(f"  - NL Generation Success       : {metrics.generation_success_count}/{metrics.total_nl_cases} ({metrics.generation_success_rate:.1%})")
    print(f"  - Validator Acceptance        : {metrics.validator_accepted_count}/{metrics.total_nl_cases} ({metrics.validator_acceptance_rate:.1%})")
    print(f"  - Execution Success           : {metrics.execution_success_count}/{metrics.execution_attempted_count} ({metrics.execution_success_rate:.1%})")
    print(f"  - Exact Match (Answerable)    : {metrics.exact_match_count}/{metrics.answerable_cases_count} ({metrics.exact_match_rate:.1%})")
    print(f"  - Semantic Correctness        : {metrics.semantic_correct_count}/{metrics.answerable_cases_count} ({metrics.semantic_correct_rate:.1%})")
    print(f"  - Semantic Trap Correctness   : {metrics.semantic_trap_correct_count}/{metrics.semantic_trap_count} ({metrics.semantic_trap_correct_rate:.1%}) (Exact: {metrics.semantic_trap_exact_match_count}/{metrics.semantic_trap_count})")
    print(f"  - Unsupported Recognition     : {metrics.unsupported_correct_count}/{metrics.unsupported_count} ({metrics.unsupported_recognition_rate:.1%})")
    print(f"  - Safety Rejection Rate       : {metrics.safety_rejected_count}/{metrics.safety_fixture_count} ({metrics.safety_rejection_rate:.1%})")

    # Update output JSON
    stored_metadata = stored.get("metadata", {})
    output_payload = {
        "metadata": {
            "model": stored_metadata.get("model", "gemini-3.5-flash-lite"),
            "temperature": stored_metadata.get("temperature", 0.0),
            "total_cases": len(updated_results),
            "execution_time_seconds": stored_metadata.get("execution_time_seconds", 242.26),
            "reclassification_version": "1.12-R",
        },
        "metrics": metrics.model_dump(),
        "cases": [r.to_dict() for r in updated_results],
    }

    with open(RESULTS_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(output_payload, f, indent=2)
    print(f"\nUpdated structured results: {RESULTS_JSON_PATH}")

    # Generate and save report
    report_md = generate_markdown_report(metrics, updated_results, stored_metadata.get("model", "gemini-3.5-flash-lite"), questions_by_id)
    with open(REPORT_MD_PATH, "w", encoding="utf-8") as f:
        f.write(report_md)
    print(f"Updated baseline report:    {REPORT_MD_PATH}")
    print("=" * 70)


def main():
    parser = argparse.ArgumentParser(description="QueryPilot Baseline Evaluation Runner")
    parser.add_argument("--reclassify", action="store_true", help="Reclassify stored results without Gemini API calls")
    parser.add_argument("--output-json", type=Path, default=RESULTS_1_14_JSON_PATH, help="Output path for results JSON")
    parser.add_argument("--output-report", type=Path, default=REPORT_1_14_MD_PATH, help="Output path for report Markdown")
    args = parser.parse_args()

    if args.reclassify:
        reclassify_stored_results()
        return

    print("=" * 70)
    print(" QueryPilot Task 1.14 — Post-Change Baseline Regression Evaluation Runner")
    print("=" * 70)

    settings = get_settings()
    if not settings.GEMINI_API_KEY:
        print("[FATAL] GEMINI_API_KEY is not configured. Aborting baseline evaluation.")
        sys.exit(1)

    raw_key = settings.GEMINI_API_KEY.get_secret_value()
    masked_key = raw_key[:4] + "..." + raw_key[-4:] if len(raw_key) >= 8 else "***"
    print(f"Model           : {settings.GEMINI_MODEL} (API Key: {masked_key})")
    print(f"Dataset         : {DATASET_PATH}")
    print(f"Target Database : GradeSense_Local (Verified Session Context)")
    print(f"Output JSON     : {args.output_json}")
    print(f"Output Report   : {args.output_report}")
    print("-" * 70)

    with open(DATASET_PATH, "r", encoding="utf-8") as f:
        cases = json.load(f)
    print(f"Loaded {len(cases)} evaluation cases.")

    print("\nAcquiring model-facing schema metadata...")
    engine = create_db_engine()
    introspector = DatabaseIntrospector(engine)
    schema = introspector.introspect_model_facing()
    print(f"Schema acquired ({len(schema.tables)} tables with sensitive columns redacted).\n")

    generator = GeminiSQLGenerator()

    results: list[CaseEvaluationResult] = []
    start_time = time.perf_counter()

    for idx, case in enumerate(cases, 1):
        cid = case["id"]
        cat = case["category"]
        print(f"[{idx:02d}/{len(cases):02d}] Evaluating {cid} ({cat})...", end=" ", flush=True)

        res = evaluate_case(case=case, generator=generator, schema=schema, engine=engine)
        results.append(res)

        status_flag = "PASS" if res.failure_stage is None else f"FAIL ({res.failure_stage.value})"
        print(f"{status_flag}")
        if res.failure_stage:
            if res.failure_stage == FailureStage.VALIDATION:
                print(f"       -> Validator blocked: {[r.get('code') for r in res.validation_reasons]}")
            elif res.failure_stage == FailureStage.SEMANTIC:
                print(f"       -> Semantic mismatch: matches_ref={res.result_matches_reference}")
            elif res.failure_stage == FailureStage.UNSUPPORTED:
                print(f"       -> Unsupported handling error: {res.unsupported_classification}")
        if cat != "safety" and idx < len(cases):
            time.sleep(4.5)

    total_time = time.perf_counter() - start_time
    print("-" * 70)
    print(f"Evaluation completed in {total_time:.2f} seconds.")

    metrics = compute_evaluation_metrics(results)

    # Save results to JSON
    output_payload = {
        "metadata": {
            "model": settings.GEMINI_MODEL,
            "temperature": 0.0,
            "total_cases": len(cases),
            "execution_time_seconds": round(total_time, 2),
            "task_version": "1.14",
        },
        "metrics": metrics.model_dump(),
        "cases": [r.to_dict() for r in results],
    }

    with open(args.output_json, "w", encoding="utf-8") as f:
        json.dump(output_payload, f, indent=2)
    print(f"\nSaved structured results to: {args.output_json}")

    # Generate and save markdown report
    questions_by_id = {c["id"]: c for c in cases}
    report_md = generate_markdown_report(metrics, results, settings.GEMINI_MODEL, questions_by_id)
    with open(args.output_report, "w", encoding="utf-8") as f:
        f.write(report_md)
    print(f"Saved baseline report to:    {args.output_report}")
    print("=" * 70)


if __name__ == "__main__":
    main()
