"""Deterministic evaluation report generator and regression metrics for QueryPilot (Task 1.17).

Consumes saved evaluation results (e.g. baseline_results_1_14.json) and produces
a reproducible, evidence-based Markdown reliability report without secondary
LLM calls, database queries, or data hallucination.
"""

from datetime import datetime, timezone
import json
from pathlib import Path
import statistics
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from query_pilot.eval.evaluator import compute_evaluation_metrics
from query_pilot.eval.models import (
    CaseEvaluationResult,
    EvaluationMetrics,
    FailureStage,
    MismatchClassification,
    SemanticStatus,
)


class EvaluationReportData(BaseModel):
    """Encapsulates loaded evaluation results, computed metrics, and provenance metadata."""

    model_config = {"arbitrary_types_allowed": True}

    source_path: str = Field(..., description="Path to the source evaluation JSON artifact.")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Evaluation run metadata.")
    metrics: EvaluationMetrics = Field(..., description="Computed aggregate evaluation metrics.")
    cases: List[CaseEvaluationResult] = Field(default_factory=list, description="Individual case evaluation results.")


def format_rate(numerator: int, denominator: int) -> str:
    """Format numerator/denominator and percentage, safely handling zero denominator.

    Args:
        numerator: Count of successful items.
        denominator: Total count of items.

    Returns:
        Formatted string like "26/26 (100.0%)" or "0/0 (N/A)".
    """
    if denominator == 0:
        return f"{numerator}/{denominator} (N/A)"
    pct = (numerator / denominator) * 100.0
    return f"{numerator}/{denominator} ({pct:.1f}%)"


def load_evaluation_artifact(file_path: Path | str) -> EvaluationReportData:
    """Load and validate a saved evaluation JSON artifact.

    Args:
        file_path: Path to the evaluation JSON file.

    Returns:
        EvaluationReportData object containing verified cases, metrics, and metadata.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If JSON is malformed or structure is unsupported.
    """
    path = Path(file_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Evaluation artifact not found at: {path}")

    try:
        with open(path, "r", encoding="utf-8") as f:
            raw_data = json.load(f)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Malformed JSON in evaluation artifact {path.name}: {exc}") from exc
    except Exception as exc:
        raise ValueError(f"Failed to read evaluation artifact {path.name}: {exc}") from exc

    metadata: Dict[str, Any] = {}
    cases_raw: List[Dict[str, Any]] = []

    if isinstance(raw_data, dict):
        metadata = raw_data.get("metadata", {})
        if "cases" in raw_data and isinstance(raw_data["cases"], list):
            cases_raw = raw_data["cases"]
        elif "results" in raw_data and isinstance(raw_data["results"], list):
            cases_raw = raw_data["results"]
        else:
            raise ValueError(
                f"Unsupported evaluation artifact structure in {path.name}: missing 'cases' or 'results' array."
            )
    elif isinstance(raw_data, list):
        cases_raw = raw_data
    else:
        raise ValueError(
            f"Unsupported evaluation artifact root type in {path.name}: expected dict or list, got {type(raw_data).__name__}."
        )

    if not cases_raw:
        raise ValueError(f"Evaluation artifact in {path.name} contains zero evaluation cases.")

    parsed_cases: List[CaseEvaluationResult] = []
    for idx, c in enumerate(cases_raw):
        if not isinstance(c, dict):
            raise ValueError(f"Case at index {idx} in {path.name} is not a JSON object.")
        if "case_id" not in c or "category" not in c:
            raise ValueError(f"Case at index {idx} in {path.name} missing required 'case_id' or 'category' fields.")
        try:
            parsed_cases.append(CaseEvaluationResult(**c))
        except Exception as exc:
            raise ValueError(f"Failed to parse case {c.get('case_id', f'at index {idx}')} in {path.name}: {exc}") from exc

    # Compute deterministic metrics directly from the validated cases
    metrics = compute_evaluation_metrics(parsed_cases)

    return EvaluationReportData(
        source_path=str(path),
        metadata=metadata,
        metrics=metrics,
        cases=parsed_cases,
    )


def compute_operational_metrics(cases: List[CaseEvaluationResult], metadata: Dict[str, Any]) -> Dict[str, Any]:
    """Extract operational performance metrics from cases and metadata.

    Distinguishes between metrics supported by the data (e.g. database execution latency)
    and metrics that were unavailable in historical evaluation artifacts (e.g. per-stage
    pipeline durations, request correlation IDs, truncation flags).

    Args:
        cases: List of evaluated cases.
        metadata: Artifact metadata dictionary.

    Returns:
        Dictionary of operational statistics and availability statuses.
    """
    exec_times = [
        c.execution_time_ms
        for c in cases
        if c.execution_time_ms is not None and c.execution_status == "success"
    ]

    latency_stats: Optional[Dict[str, Any]] = None
    if exec_times:
        latency_stats = {
            "count": len(exec_times),
            "min_ms": round(min(exec_times), 2),
            "median_ms": round(statistics.median(exec_times), 2),
            "mean_ms": round(statistics.mean(exec_times), 2),
            "max_ms": round(max(exec_times), 2),
        }

    total_eval_duration = metadata.get("execution_time_seconds")

    return {
        "database_execution_latency": latency_stats,
        "total_evaluation_duration_seconds": total_eval_duration,
        "stage_durations_available": False,
        "request_ids_available": False,
        "truncation_metadata_available": False,
    }


def analyze_failures(cases: List[CaseEvaluationResult]) -> Dict[str, List[CaseEvaluationResult]]:
    """Group cases into failure categories based on evaluation classifications.

    Does not classify textual SQL differences as failures if semantic correctness holds.

    Args:
        cases: Evaluated cases.

    Returns:
        Mapping of failure stage/type to list of failed CaseEvaluationResult items.
    """
    failures: Dict[str, List[CaseEvaluationResult]] = {
        "generation_failures": [],
        "validation_failures": [],
        "execution_failures": [],
        "semantic_incorrect": [],
        "unsupported_handling_failures": [],
        "safety_failures": [],
    }

    for c in cases:
        # 1. Generation failure
        if c.category != "safety" and c.generation_status not in ("success", "not_applicable"):
            failures["generation_failures"].append(c)

        # 2. Validation failure on answerable query
        if c.expected_behavior == "answerable" and not c.validation_allowed:
            failures["validation_failures"].append(c)

        # 3. Execution failure on validated query
        if c.execution_status == "failed":
            failures["execution_failures"].append(c)

        # 4. Semantic failure on answerable query
        if c.expected_behavior == "answerable":
            if c.semantic_correct is False or c.semantic_status in (SemanticStatus.INCORRECT, "incorrect"):
                failures["semantic_incorrect"].append(c)

        # 5. Unsupported handling failure
        if c.expected_behavior == "unsupported":
            if c.semantic_status not in (SemanticStatus.CORRECTLY_REFUSED, "correctly_refused") and c.unsupported_classification != "correctly_unsupported":
                failures["unsupported_handling_failures"].append(c)

        # 6. Safety failure (safety fixture not rejected)
        if c.category == "safety" and c.validation_allowed:
            failures["safety_failures"].append(c)

    return failures


def generate_markdown_report(
    report_data: EvaluationReportData,
    title: Optional[str] = None,
    timestamp: Optional[str] = None,
) -> str:
    """Generate a clean, deterministic Markdown reliability report.

    Args:
        report_data: Validated EvaluationReportData.
        title: Optional custom report title.
        timestamp: Optional ISO timestamp string (uses UTC now if None).

    Returns:
        Formatted Markdown document.
    """
    m = report_data.metrics
    meta = report_data.metadata
    cases = report_data.cases
    src_filename = Path(report_data.source_path).name

    report_title = title or "QueryPilot — Evaluation Reliability & Regression Report"
    gen_time = timestamp or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    model_name = meta.get("model", "N/A (not recorded in source)")
    temp = meta.get("temperature", "N/A")
    task_ver = meta.get("task_version") or meta.get("reclassification_version") or "Baseline"
    eval_duration = meta.get("execution_time_seconds")
    eval_duration_str = f"{eval_duration:.2f}s" if isinstance(eval_duration, (int, float)) else "N/A"

    ops = compute_operational_metrics(cases, meta)
    failures = analyze_failures(cases)
    total_failures = sum(len(v) for v in failures.values())

    lines: List[str] = []

    # Title & Provenance
    lines.append(f"# {report_title}\n")
    lines.append("## Provenance & Evaluation Context\n")
    lines.append(f"- **Source Artifact:** `{src_filename}`")
    lines.append(f"- **Evaluation Dataset Version:** Task {task_ver}")
    lines.append(f"- **Target Model:** `{model_name}`")
    lines.append(f"- **Model Temperature:** `{temp}`")
    lines.append(f"- **Evaluation Run Duration:** {eval_duration_str}")
    lines.append(f"- **Report Generated:** {gen_time}")
    lines.append("- **Evaluation Mode:** Saved Artifact Replay (Offline Deterministic Analysis)\n")

    # Executive Summary / Overall Metrics
    lines.append("## 1. Executive Summary & Overall Metrics\n")
    lines.append(f"- **Total Test Cases:** {m.total_cases}")
    lines.append(f"- **Natural-Language Generation Success:** {format_rate(m.generation_success_count, m.total_nl_cases)}")
    lines.append(f"- **Validator Acceptance (NL Queries):** {format_rate(m.validator_accepted_count, m.total_nl_cases)}")
    lines.append(f"- **Query Execution Success (Validated Queries):** {format_rate(m.execution_success_count, m.execution_attempted_count)}")
    lines.append(f"- **Semantic Correctness Rate (Answerable Cases):** {format_rate(m.semantic_correct_count, m.answerable_cases_count)}")
    lines.append(f"- **Exact Result Match Rate (Answerable Cases):** {format_rate(m.exact_match_count, m.answerable_cases_count)}")
    lines.append(f"- **Semantic Trap Correctness (Q028–Q030):** {format_rate(m.semantic_trap_correct_count, m.semantic_trap_count)} (Exact match: {format_rate(m.semantic_trap_exact_match_count, m.semantic_trap_count)})")
    lines.append(f"- **Structured Unsupported Refusal (Q024–Q027):** {format_rate(m.unsupported_correct_count, m.unsupported_count)}")
    lines.append(f"- **Safety Policy Enforcement (S001–S002):** {format_rate(m.safety_rejected_count, m.safety_fixture_count)} rejected before database execution\n")

    # Important Measurement Distinction Callout
    lines.append("> [!NOTE]")
    lines.append("> **Measurement Boundary Distinction:**")
    lines.append("> - **Exact Result Match** requires literal equality of returned tabular result sets against the stored reference output.")
    lines.append("> - **Semantic Correctness** evaluates whether the generated SQL accurately satisfies the user's business question without hallucinations, allowing safe differences in column aliasing, extra projections, or equivalent date bounds.")
    lines.append("> - Syntactic SQL differences are NOT classified as pipeline failures when semantic correctness holds.\n")

    # Category Breakdown
    lines.append("## 2. Category Breakdown\n")
    lines.append("| Category | Total Cases | Generation | Validation | Execution | Exact Match | Semantic Correct |")
    lines.append("|---|:---:|:---:|:---:|:---:|:---:|:---:|")

    for cat, data in m.category_breakdown.items():
        total = data.get("total", 0)
        gen = data.get("generation", "N/A")
        val = data.get("validation", "N/A")
        exec_rate = data.get("execution", "N/A")
        exact = data.get("exact_match", "N/A")
        sem = data.get("semantic_correct", "N/A")
        lines.append(f"| `{cat}` | {total} | {gen} | {val} | {exec_rate} | {exact} | {sem} |")

    lines.append(
        f"| **Overall Total** | **{m.total_cases}** | **{m.generation_success_count}/{m.total_nl_cases}** | "
        f"**{m.validator_accepted_count + m.safety_rejected_count}/{m.total_cases}** | "
        f"**{m.execution_success_count}/{m.execution_attempted_count}** | "
        f"**{m.exact_match_count}/{m.answerable_cases_count}** | "
        f"**{m.semantic_correct_count}/{m.answerable_cases_count}** |\n"
    )

    # Failure Analysis
    lines.append("## 3. Failure & Rejection Analysis\n")
    if total_failures == 0:
        lines.append("- **Total Pipeline Failures:** 0")
        lines.append("- **Total Semantic Errors:** 0")
        lines.append("- **Status:** All answerable questions executed successfully with verified semantic correctness.")
        lines.append("- **Refusals:** All unsupported questions were recognized with structured refusals without attempting database execution.")
        lines.append("- **Safety Fixtures:** All safety violations were blocked deterministically by the SQL policy validator.\n")
    else:
        lines.append(f"- **Total Detected Failures:** {total_failures}\n")
        lines.append("| Failure Category | Count | Affected Cases | Details |")
        lines.append("|---|:---:|---|---|")
        for cat_name, items in failures.items():
            if items:
                case_ids = ", ".join(f"`{c.case_id}`" for c in items)
                reasons = "; ".join(c.execution_error or c.generation_error or c.notes or "Failed" for c in items[:3])
                lines.append(f"| `{cat_name}` | {len(items)} | {case_ids} | {reasons} |")
        lines.append("")

    # Operational Metrics & Data Availability
    lines.append("## 4. Operational Metrics & Timing\n")
    lat = ops.get("database_execution_latency")
    if lat:
        lines.append("### Database Query Execution Latency (ms)\n")
        lines.append(f"- **Executed Queries Measured:** {lat['count']}")
        lines.append(f"- **Minimum Latency:** {lat['min_ms']} ms")
        lines.append(f"- **Median Latency:** {lat['median_ms']} ms")
        lines.append(f"- **Mean Latency:** {lat['mean_ms']} ms")
        lines.append(f"- **Maximum Latency:** {lat['max_ms']} ms\n")
    else:
        lines.append("- **Database Query Execution Latency:** Unavailable (no queries executed or timing absent).\n")

    lines.append("### Metric Availability in Source Artifact\n")
    lines.append("| Operational Metric | Status | Note |")
    lines.append("|---|:---:|---|")
    lines.append(f"| Database Execution Latency | {'Available' if lat else 'Unavailable'} | Captured via executor `execution_time_ms` |")
    lines.append(f"| Total Benchmark Duration | {'Available' if eval_duration else 'Unavailable'} | Captured in artifact metadata (`execution_time_seconds`) |")
    lines.append("| Per-Stage Pipeline Durations | Unavailable | Stage-level durations were not recorded in historical baseline JSON |")
    lines.append("| Correlated Request IDs | Unavailable | Request IDs were introduced in Task 1.16 observability and are absent from historical data |")
    lines.append("| Query Result Truncation Flags | Unavailable | Truncation flags were not stored in historical CaseEvaluationResult models |\n")

    return "\n".join(lines)
