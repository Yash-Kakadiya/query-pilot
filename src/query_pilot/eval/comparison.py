"""Sequential vs. LangGraph comparative evaluation framework for QueryPilot (Task 1.21).

Enables paired, reproducible evaluation of natural language cases across both:
1. The sequential `QueryPipeline` (ExecutionMode.SEQUENTIAL).
2. The LangGraph `QueryPilotWorkflow` (ExecutionMode.GRAPH).

Provides per-case paired comparison, regression detection, side-by-side metric computation,
and structured reporting while strictly protecting historical evaluation artifacts.
"""

from datetime import datetime, timezone
from enum import Enum
import json
import logging
from pathlib import Path
import re
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

from pydantic import BaseModel, Field

from query_pilot.db.models import DatabaseSchema
from query_pilot.eval.evaluator import (
    BASELINE_CLASSIFICATIONS,
    compare_results,
    compute_evaluation_metrics,
)
from query_pilot.eval.models import (
    CaseEvaluationResult,
    EvaluationMetrics,
    FailureStage,
    MismatchClassification,
    SemanticStatus,
)
from query_pilot.eval.report import format_rate
from query_pilot.pipeline import PipelineResult, PipelineStatus
from query_pilot.runner import ExecutionMode, QueryRunner
from query_pilot.sql.validator import validate_sql

logger = logging.getLogger(__name__)

# Protected evaluation artifacts that MUST NEVER be overwritten
PROTECTED_EVALUATION_FILES: Set[str] = {
    "baseline_results_1_14.json",
    "baseline_results.json",
    "baseline_questions.json",
    "baseline_report_1_14.md",
    "baseline_report.md",
    "reliability_report_1_14.md",
}

# Domain entity word mappings for GradeSense_Local to detect entity mismatch in projections
DOMAIN_ENTITIES: Dict[str, Set[str]] = {
    "student": {"student", "students"},
    "faculty": {"faculty", "faculties", "prof", "professor", "professors", "lecturer", "lecturers"},
    "department": {"department", "departments", "dept"},
    "course": {"course", "courses"},
    "offering": {"offering", "offerings"},
    "enrollment": {"enrollment", "enrollments"},
    "attendance": {"attendance"},
    "mark": {"mark", "marks", "score", "scores", "cgpa", "grade", "grades"},
    "batch": {"batch", "batches"},
    "user": {"user", "users"},
    "semester": {"semester"},
    "risk": {"risk", "risks", "profile", "profiles"},
}


def split_identifier_words(name: str) -> Set[str]:
    """Extract individual normalized word stems from a camelCase or snake_case identifier."""
    words = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z][a-z]|\b)|\d+", str(name))
    result: Set[str] = set()
    for w in words:
        w_low = w.lower()
        result.add(w_low)
        if w_low.endswith("ies"):
            result.add(w_low[:-3] + "y")
        elif w_low.endswith("s") and len(w_low) > 3:
            result.add(w_low[:-1])
    return result


def are_column_aliases_compatible(gen_col: str, ref_col: str) -> bool:
    """Verify that a generated column alias is semantically compatible with a reference column.

    Rejects aliases representing different business entities (e.g. TotalFaculties vs TotalStudents)
    even if their underlying row counts happen to be identical.
    """
    g_str = str(gen_col).strip().lower()
    r_str = str(ref_col).strip().lower()
    if g_str == r_str:
        return True

    g_tokens = split_identifier_words(gen_col)
    r_tokens = split_identifier_words(ref_col)

    # Check for domain entity conflict
    g_entities = {ent for ent, words in DOMAIN_ENTITIES.items() if words & g_tokens}
    r_entities = {ent for ent, words in DOMAIN_ENTITIES.items() if words & r_tokens}

    if g_entities and r_entities and not (g_entities & r_entities):
        return False  # Conflicting business entities (e.g., student vs faculty)

    return bool(g_tokens & r_tokens)


def is_alias_difference_semantically_equivalent(
    generated_rows: Optional[List[Dict[str, Any]]],
    reference_rows: Optional[List[Dict[str, Any]]],
) -> bool:
    """Determine whether generated rows match reference rows with compatible column aliases.

    Ensures that matching positional values alone do not establish semantic correctness
    when the field meaning or domain entity differs.

    Handles:
    - Single-column alias differences (e.g. TotalRegisteredStudents vs TotalStudents).
    - Multiple-column results in matching or permuted column order.
    - NULL values (None == None) and numeric precision (40 == 40.0) following the evaluator contract.
    """
    import itertools

    if not generated_rows or not reference_rows:
        return False
    if len(generated_rows) != len(reference_rows):
        return False

    ref_keys = list(reference_rows[0].keys())
    gen_keys = list(generated_rows[0].keys())

    if len(ref_keys) != len(gen_keys):
        return False

    # Check natural column order first
    natural_compatible = all(
        are_column_aliases_compatible(g_k, r_k) for g_k, r_k in zip(gen_keys, ref_keys)
    )
    if natural_compatible:
        all_values_match = True
        for g_row, r_row in zip(generated_rows, reference_rows):
            if len(g_row) != len(r_row):
                all_values_match = False
                break
            for g_k, r_k in zip(gen_keys, ref_keys):
                if g_row.get(g_k) != r_row.get(r_k):
                    all_values_match = False
                    break
            if not all_values_match:
                break
        if all_values_match:
            return True

    # If natural order didn't match and there are multiple columns (up to 6),
    # check if a permutation of reference columns satisfies both alias compatibility
    # and value equality across all rows (handling column reordering).
    if 1 < len(ref_keys) <= 6:
        for p_ref_keys in itertools.permutations(ref_keys):
            if p_ref_keys == tuple(ref_keys):
                continue
            if not all(
                are_column_aliases_compatible(g_k, r_k) for g_k, r_k in zip(gen_keys, p_ref_keys)
            ):
                continue

            perm_values_match = True
            for g_row, r_row in zip(generated_rows, reference_rows):
                if len(g_row) != len(r_row):
                    perm_values_match = False
                    break
                for g_k, r_k in zip(gen_keys, p_ref_keys):
                    if g_row.get(g_k) != r_row.get(r_k):
                        perm_values_match = False
                        break
                if not perm_values_match:
                    break

            if perm_values_match:
                return True

    return False


class PairedCaseClassification(str, Enum):
    """Comparative outcome classification for a single case across both execution modes."""

    BOTH_PASS = "both_pass"
    SEQUENTIAL_ONLY_PASS = "sequential_only_pass"  # Graph regression
    GRAPH_ONLY_PASS = "graph_only_pass"            # Graph improvement
    BOTH_FAIL = "both_fail"
    INCOMPARABLE = "incomparable"


class PairedCaseComparison(BaseModel):
    """Paired evaluation outcome for an individual test case across both modes."""

    model_config = {"arbitrary_types_allowed": True}

    case_id: str
    category: str
    question: Optional[str] = None
    expected_behavior: str
    classification: PairedCaseClassification

    sequential_result: CaseEvaluationResult
    graph_result: CaseEvaluationResult

    sequential_passed: bool
    graph_passed: bool
    exact_match_agree: bool
    difference_summary: Optional[str] = None
    notes: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert paired case comparison to a dictionary."""
        return {
            "case_id": self.case_id,
            "category": self.category,
            "question": self.question,
            "expected_behavior": self.expected_behavior,
            "classification": self.classification.value,
            "sequential_passed": self.sequential_passed,
            "graph_passed": self.graph_passed,
            "exact_match_agree": self.exact_match_agree,
            "difference_summary": self.difference_summary,
            "sequential_result": self.sequential_result.to_dict(),
            "graph_result": self.graph_result.to_dict(),
            "notes": self.notes,
        }


class ComparativeMetrics(BaseModel):
    """Summary metrics comparing sequential and graph execution modes."""

    total_cases: int
    total_nl_cases: int
    answerable_cases_count: int
    unsupported_cases_count: int
    safety_cases_count: int

    # Mode-specific aggregate metrics
    sequential_metrics: EvaluationMetrics
    graph_metrics: EvaluationMetrics

    # Paired agreement and regression counts (all cases)
    both_pass_count: int
    sequential_only_count: int  # Regressions in graph
    graph_only_count: int       # Improvements in graph
    both_fail_count: int
    incomparable_count: int

    agreement_rate: float
    regression_rate: float
    improvement_rate: float

    # NL-specific paired counts and rates (excluding safety fixtures)
    nl_both_pass_count: int = 0
    nl_sequential_only_count: int = 0
    nl_graph_only_count: int = 0
    nl_both_fail_count: int = 0
    nl_incomparable_count: int = 0
    nl_agreement_rate: float = 0.0
    nl_regression_rate: float = 0.0
    nl_improvement_rate: float = 0.0

    # Error tracking
    unexpected_errors_sequential: int
    unexpected_errors_graph: int


class ComparativeEvaluationArtifact(BaseModel):
    """Top-level comparative evaluation result container."""

    model_config = {"arbitrary_types_allowed": True}

    metadata: Dict[str, Any] = Field(default_factory=dict)
    metrics: ComparativeMetrics
    paired_cases: List[PairedCaseComparison]
    sequential_cases: List[CaseEvaluationResult]
    graph_cases: List[CaseEvaluationResult]

    def to_dict(self) -> Dict[str, Any]:
        """Convert full comparative evaluation to serializable dictionary."""
        return {
            "metadata": self.metadata,
            "metrics": self.metrics.model_dump(),
            "paired_cases": [c.to_dict() for c in self.paired_cases],
            "sequential_cases": [c.to_dict() for c in self.sequential_cases],
            "graph_cases": [c.to_dict() for c in self.graph_cases],
        }


def is_case_passing(result: CaseEvaluationResult) -> bool:
    """Determine whether an individual case evaluation result represents a pass.

    - Answerable cases: pass if semantically correct (or exact result match).
    - Unsupported cases: pass if correctly identified as unsupported and refused.
    - Safety fixtures: pass if correctly rejected by validator policy without execution.

    Args:
        result: Individual CaseEvaluationResult.

    Returns:
        True if the case passed according to its expected behavior; False otherwise.
    """
    if result.category == "safety":
        return result.validation_allowed is False

    if result.expected_behavior == "unsupported":
        return (
            result.unsupported_classification == "correctly_unsupported"
            or result.semantic_status == SemanticStatus.CORRECTLY_REFUSED
            or (result.validation_allowed is True and result.execution_status == "not_executed" and result.semantic_status == SemanticStatus.CORRECTLY_REFUSED)
        )

    if result.expected_behavior == "answerable":
        return bool(
            result.semantic_correct is True
            or result.exact_result_match is True
            or result.result_matches_reference is True
            or result.semantic_status in (SemanticStatus.CORRECT, "correct")
        )

    return False


def classify_paired_case(
    seq_result: CaseEvaluationResult,
    graph_result: CaseEvaluationResult,
) -> PairedCaseComparison:
    """Classify the paired outcome and identify regressions or improvements.

    Args:
        seq_result: Case evaluation result from sequential execution.
        graph_result: Case evaluation result from graph execution.

    Returns:
        PairedCaseComparison with detailed difference diagnosis.
    """
    case_id = seq_result.case_id
    category = seq_result.category
    question = seq_result.question
    expected_behavior = seq_result.expected_behavior

    # Check for missing ground-truth reference data on answerable cases
    if (
        expected_behavior == "answerable"
        and seq_result.result_matches_reference is None
        and graph_result.result_matches_reference is None
        and seq_result.semantic_correct is None
        and graph_result.semantic_correct is None
        and seq_result.execution_status == "success"
        and graph_result.execution_status == "success"
    ):
        classification = PairedCaseClassification.INCOMPARABLE
        seq_pass = False
        graph_pass = False
        diff_summary = "Incomparable: ground-truth reference result is missing from evaluation dataset."
    # Check for malformed execution data across both modes
    elif (
        seq_result.generation_status == "failed" and graph_result.generation_status == "failed"
        and (
            (seq_result.generation_error and "malformed" in seq_result.generation_error.lower())
            or (graph_result.generation_error and "malformed" in graph_result.generation_error.lower())
        )
    ):
        classification = PairedCaseClassification.INCOMPARABLE
        seq_pass = False
        graph_pass = False
        diff_summary = "Incomparable: execution data malformed or missing across both modes."
    else:
        seq_pass = is_case_passing(seq_result)
        graph_pass = is_case_passing(graph_result)

        exact_agree = bool(
            (seq_result.exact_result_match is True and graph_result.exact_result_match is True)
            or (seq_result.exact_result_match is False and graph_result.exact_result_match is False)
            or (seq_result.exact_result_match is None and graph_result.exact_result_match is None)
        )

        if seq_pass and graph_pass:
            classification = PairedCaseClassification.BOTH_PASS
            if seq_result.expected_behavior == "answerable" and not exact_agree:
                diff_summary = (
                    f"Both modes passed semantic criteria, but exact result matches differ "
                    f"(sequential exact: {seq_result.exact_result_match}, graph exact: {graph_result.exact_result_match})."
                )
            else:
                diff_summary = "Both modes passed expected evaluation criteria."
        elif seq_pass and not graph_pass:
            classification = PairedCaseClassification.SEQUENTIAL_ONLY_PASS
            diff_summary = f"Regression: sequential passed but graph failed (graph stage: {graph_result.failure_stage})."
        elif not seq_pass and graph_pass:
            classification = PairedCaseClassification.GRAPH_ONLY_PASS
            diff_summary = f"Improvement: graph passed while sequential failed (seq stage: {seq_result.failure_stage})."
        else:
            classification = PairedCaseClassification.BOTH_FAIL
            seq_failed_op = seq_result.execution_status == "failed" or seq_result.generation_status == "failed" or not seq_result.validation_allowed
            graph_failed_op = graph_result.execution_status == "failed" or graph_result.generation_status == "failed" or not graph_result.validation_allowed

            if not seq_failed_op and not graph_failed_op:
                diff_summary = "Both modes executed successfully, but produced semantically incorrect answers."
            elif seq_failed_op and graph_failed_op:
                seq_stg = seq_result.failure_stage.value if seq_result.failure_stage else "pipeline"
                graph_stg = graph_result.failure_stage.value if graph_result.failure_stage else "pipeline"
                diff_summary = f"Both modes encountered pipeline execution failures (seq failed at {seq_stg}, graph failed at {graph_stg})."
            else:
                seq_desc = f"failed at {seq_result.failure_stage.value if seq_result.failure_stage else 'pipeline'}" if seq_failed_op else "completed with incorrect answer"
                graph_desc = f"failed at {graph_result.failure_stage.value if graph_result.failure_stage else 'pipeline'}" if graph_failed_op else "completed with incorrect answer"
                diff_summary = f"Mixed failure: sequential {seq_desc}, graph {graph_desc}."

    exact_agree = bool(
        (seq_result.exact_result_match is True and graph_result.exact_result_match is True)
        or (seq_result.exact_result_match is False and graph_result.exact_result_match is False)
        or (seq_result.exact_result_match is None and graph_result.exact_result_match is None)
    )

    return PairedCaseComparison(
        case_id=case_id,
        category=category,
        question=question,
        expected_behavior=expected_behavior,
        classification=classification,
        sequential_result=seq_result,
        graph_result=graph_result,
        sequential_passed=seq_pass,
        graph_passed=graph_pass,
        exact_match_agree=exact_agree,
        difference_summary=diff_summary,
        notes=seq_result.notes or graph_result.notes,
    )


def evaluate_case_with_runner(
    case: Dict[str, Any],
    runner: QueryRunner,
    mode: ExecutionMode,
    schema: DatabaseSchema,
) -> CaseEvaluationResult:
    """Evaluate a single test case using the unified QueryRunner in the specified mode.

    Reuses existing baseline comparison logic and AST safety validation without
    external network or live database access.

    Args:
        case: Case dictionary from evaluation dataset.
        runner: Preconfigured QueryRunner.
        mode: ExecutionMode.SEQUENTIAL or ExecutionMode.GRAPH.
        schema: DatabaseSchema metadata.

    Returns:
        Structured CaseEvaluationResult for the case under this mode.
    """
    case_id = case["id"]
    category = case["category"]
    question = case.get("question")
    expected_behavior = case.get("expected_behavior", "answerable")
    notes = case.get("notes")

    # 1. Safety fixtures (S001–S002)
    if category == "safety":
        candidate_sql = case.get("candidate_sql", "")
        val_res = validate_sql(candidate_sql, schema)
        is_rejected = not val_res.allowed
        reasons = [r.to_dict() for r in val_res.reasons]
        expected_reason = case.get("expected_reason")
        reasons_match = (
            expected_reason in val_res.reason_codes
            if expected_reason
            else is_rejected
        )

        return CaseEvaluationResult(
            case_id=case_id,
            category=category,
            question=None,
            expected_behavior=expected_behavior,
            generated_sql=candidate_sql,
            generation_status="not_applicable",
            validation_allowed=val_res.allowed,
            validation_reasons=reasons,
            execution_status="not_executed",
            result_matches_reference=None,
            exact_result_match=None,
            semantic_status=SemanticStatus.CORRECT if (is_rejected and reasons_match) else SemanticStatus.INCORRECT,
            semantic_correct=None,
            mismatch_classification=MismatchClassification.SAFETY_FIXTURE.value,
            structurally_valid=True,
            validator_accepted=False,
            executed=False,
            failure_stage=FailureStage.VALIDATION if not is_rejected else None,
            notes=notes,
        )

    # 2. Natural language cases: execute via unified QueryRunner
    try:
        pipe_res: PipelineResult = runner.run(question, mode=mode)
    except Exception as exc:
        logger.error(f"Execution error on case {case_id} ({mode.value}): {exc}")
        return CaseEvaluationResult(
            case_id=case_id,
            category=category,
            question=question,
            expected_behavior=expected_behavior,
            generation_status="failed",
            generation_error=str(exc),
            validation_allowed=False,
            execution_status="failed",
            execution_error=str(exc),
            result_matches_reference=False,
            exact_result_match=False,
            semantic_status=SemanticStatus.NOT_APPLICABLE,
            semantic_correct=False,
            failure_stage=FailureStage.GENERATION,
            notes=notes,
        )

    candidate_sql = pipe_res.generated_sql
    val_res = pipe_res.validation_result
    exec_res = pipe_res.execution_result
    explanation = pipe_res.generation_explanation
    assumptions = pipe_res.generation_assumptions or []
    val_allowed = val_res.allowed if val_res else (pipe_res.status == PipelineStatus.SUCCESS)
    val_reasons = [r.to_dict() for r in val_res.reasons] if val_res else []

    # Handle Unsupported Questions
    if expected_behavior == "unsupported":
        if pipe_res.status == PipelineStatus.UNSUPPORTED:
            return CaseEvaluationResult(
                case_id=case_id,
                category=category,
                question=question,
                expected_behavior=expected_behavior,
                generated_sql=None,
                generation_status="success",
                generation_explanation=explanation,
                generation_assumptions=assumptions,
                validation_allowed=True,
                validation_reasons=[],
                execution_status="not_executed",
                result_matches_reference=None,
                exact_result_match=None,
                semantic_status=SemanticStatus.CORRECTLY_REFUSED,
                semantic_correct=True,
                mismatch_classification=MismatchClassification.UNSUPPORTED_HANDLING.value,
                unsupported_classification="correctly_unsupported",
                structurally_valid=True,
                validator_accepted=None,
                executed=False,
                failure_stage=None,
                notes=notes or "Structured refusal from generator.",
            )
        elif pipe_res.status == PipelineStatus.VALIDATION_REJECTED:
            return CaseEvaluationResult(
                case_id=case_id,
                category=category,
                question=question,
                expected_behavior=expected_behavior,
                generated_sql=candidate_sql,
                generation_status="success",
                generation_explanation=explanation,
                generation_assumptions=assumptions,
                validation_allowed=False,
                validation_reasons=val_reasons,
                execution_status="not_executed",
                result_matches_reference=None,
                exact_result_match=None,
                semantic_status=SemanticStatus.HALLUCINATED_UNSUPPORTED,
                semantic_correct=None,
                mismatch_classification=MismatchClassification.UNSUPPORTED_HANDLING.value,
                unsupported_classification="incorrectly_attempted",
                structurally_valid=True,
                validator_accepted=False,
                executed=False,
                failure_stage=FailureStage.VALIDATION,
                notes=notes,
            )
        else:
            return CaseEvaluationResult(
                case_id=case_id,
                category=category,
                question=question,
                expected_behavior=expected_behavior,
                generated_sql=candidate_sql,
                generation_status="success",
                generation_explanation=explanation,
                generation_assumptions=assumptions,
                validation_allowed=val_allowed,
                validation_reasons=val_reasons,
                execution_status="success" if exec_res else "not_executed",
                result_matches_reference=None,
                exact_result_match=None,
                semantic_status=SemanticStatus.INCORRECTLY_ATTEMPTED,
                semantic_correct=False,
                mismatch_classification=MismatchClassification.UNSUPPORTED_HANDLING.value,
                unsupported_classification="incorrectly_attempted",
                structurally_valid=True,
                validator_accepted=val_allowed,
                executed=bool(exec_res),
                failure_stage=FailureStage.UNSUPPORTED,
                notes=notes,
            )

    # Handle Answerable & Semantic Trap Questions
    if pipe_res.status == PipelineStatus.GENERATION_FAILED:
        return CaseEvaluationResult(
            case_id=case_id,
            category=category,
            question=question,
            expected_behavior=expected_behavior,
            generation_status="failed",
            generation_error=pipe_res.error_message,
            validation_allowed=False,
            execution_status="not_executed",
            result_matches_reference=False,
            exact_result_match=False,
            semantic_status=SemanticStatus.NOT_APPLICABLE,
            semantic_correct=False,
            failure_stage=FailureStage.GENERATION,
            notes=notes,
        )

    if pipe_res.status == PipelineStatus.VALIDATION_REJECTED:
        return CaseEvaluationResult(
            case_id=case_id,
            category=category,
            question=question,
            expected_behavior=expected_behavior,
            generated_sql=candidate_sql,
            generation_status="success",
            generation_explanation=explanation,
            generation_assumptions=assumptions,
            validation_allowed=False,
            validation_reasons=val_reasons,
            execution_status="not_executed",
            result_matches_reference=False,
            exact_result_match=False,
            semantic_status=SemanticStatus.INCORRECT,
            semantic_correct=False,
            mismatch_classification=MismatchClassification.SEMANTIC_INCORRECT.value,
            structurally_valid=False,
            validator_accepted=False,
            executed=False,
            failure_stage=FailureStage.VALIDATION,
            notes=notes,
        )

    if pipe_res.status == PipelineStatus.EXECUTION_FAILED:
        return CaseEvaluationResult(
            case_id=case_id,
            category=category,
            question=question,
            expected_behavior=expected_behavior,
            generated_sql=candidate_sql,
            generation_status="success",
            generation_explanation=explanation,
            generation_assumptions=assumptions,
            validation_allowed=True,
            validation_reasons=val_reasons,
            execution_status="failed",
            execution_error=pipe_res.error_message,
            result_matches_reference=False,
            exact_result_match=False,
            semantic_status=SemanticStatus.INCORRECT,
            semantic_correct=False,
            mismatch_classification=MismatchClassification.SEMANTIC_INCORRECT.value,
            structurally_valid=True,
            validator_accepted=True,
            executed=False,
            failure_stage=FailureStage.EXECUTION,
            notes=notes,
        )

    # SUCCESS: compare query results with ground-truth reference
    generated_rows = exec_res.rows if exec_res else []
    ref_result = case.get("reference_result")

    if ref_result is None and not case.get("is_semantic_equivalent", False):
        matches = None
        is_semantic_correct = None
        mismatch_type = None
        failure_stg = None
        notes_str = (notes or "") + " [Missing reference result in evaluation dataset]"
    else:
        matches = compare_results(generated_rows, ref_result)
        classification_entry = BASELINE_CLASSIFICATIONS.get(case_id)
        if matches:
            is_semantic_correct = True
            mismatch_type = MismatchClassification.EXACT_MATCH.value
        elif (
            classification_entry
            and classification_entry.get("mismatch") == MismatchClassification.SEMANTIC_CORRECT_EXACT_DIFFERENCE
            and ref_result is not None
            and len(generated_rows) == len(ref_result)
            and len(generated_rows) > 0
        ):
            is_semantic_correct = True
            mismatch_type = MismatchClassification.SEMANTIC_CORRECT_EXACT_DIFFERENCE.value
        elif case.get("is_semantic_equivalent", False):
            is_semantic_correct = True
            mismatch_type = MismatchClassification.SEMANTIC_CORRECT_EXACT_DIFFERENCE.value
        elif is_alias_difference_semantically_equivalent(generated_rows, ref_result):
            is_semantic_correct = True
            mismatch_type = MismatchClassification.SEMANTIC_CORRECT_EXACT_DIFFERENCE.value
        else:
            is_semantic_correct = False
            mismatch_type = MismatchClassification.SEMANTIC_INCORRECT.value
        failure_stg = None if is_semantic_correct else FailureStage.SEMANTIC
        default_note = "Column alias difference with identical returned data values." if (is_semantic_correct and not matches) else None
        notes_str = notes or (classification_entry.get("notes") if classification_entry else None) or default_note

    return CaseEvaluationResult(
        case_id=case_id,
        category=category,
        question=question,
        expected_behavior=expected_behavior,
        generated_sql=candidate_sql,
        generation_status="success",
        generation_explanation=explanation,
        generation_assumptions=assumptions,
        validation_allowed=True,
        validation_reasons=val_reasons,
        execution_status="success",
        execution_time_ms=exec_res.execution_time_ms if exec_res else None,
        generated_result=generated_rows,
        result_matches_reference=matches,
        exact_result_match=matches,
        semantic_status=SemanticStatus.CORRECT if is_semantic_correct else (SemanticStatus.NOT_APPLICABLE if is_semantic_correct is None else SemanticStatus.INCORRECT),
        semantic_correct=is_semantic_correct,
        mismatch_classification=mismatch_type,
        structurally_valid=True,
        validator_accepted=True,
        executed=True,
        failure_stage=failure_stg,
        notes=notes_str,
    )


def compute_comparative_metrics(
    sequential_cases: List[CaseEvaluationResult],
    graph_cases: List[CaseEvaluationResult],
    paired_cases: List[PairedCaseComparison],
) -> ComparativeMetrics:
    """Compute aggregate comparative evaluation metrics."""
    total_cases = len(paired_cases)
    nl_cases_count = len([c for c in paired_cases if c.category != "safety"])
    answerable_count = len([c for c in paired_cases if c.expected_behavior == "answerable"])
    unsupported_count = len([c for c in paired_cases if c.expected_behavior == "unsupported"])
    safety_count = len([c for c in paired_cases if c.category == "safety"])

    # For correctness-rate denominators, exclude answerable cases lacking ground-truth references
    seq_eval_cases = [
        r for r in sequential_cases
        if not (r.expected_behavior == "answerable" and r.result_matches_reference is None and r.semantic_correct is None and r.execution_status == "success")
    ]
    graph_eval_cases = [
        r for r in graph_cases
        if not (r.expected_behavior == "answerable" and r.result_matches_reference is None and r.semantic_correct is None and r.execution_status == "success")
    ]
    seq_metrics = compute_evaluation_metrics(seq_eval_cases)
    graph_metrics = compute_evaluation_metrics(graph_eval_cases)

    both_pass = sum(1 for c in paired_cases if c.classification == PairedCaseClassification.BOTH_PASS)
    seq_only = sum(1 for c in paired_cases if c.classification == PairedCaseClassification.SEQUENTIAL_ONLY_PASS)
    graph_only = sum(1 for c in paired_cases if c.classification == PairedCaseClassification.GRAPH_ONLY_PASS)
    both_fail = sum(1 for c in paired_cases if c.classification == PairedCaseClassification.BOTH_FAIL)
    incomparable = sum(1 for c in paired_cases if c.classification == PairedCaseClassification.INCOMPARABLE)

    agree_count = both_pass + both_fail
    agreement_rate = (agree_count / total_cases) if total_cases > 0 else 0.0
    regression_rate = (seq_only / total_cases) if total_cases > 0 else 0.0
    improvement_rate = (graph_only / total_cases) if total_cases > 0 else 0.0

    # NL-specific metrics (excluding safety fixtures from pipeline comparison)
    nl_paired = [c for c in paired_cases if c.category != "safety"]
    nl_both_pass = sum(1 for c in nl_paired if c.classification == PairedCaseClassification.BOTH_PASS)
    nl_seq_only = sum(1 for c in nl_paired if c.classification == PairedCaseClassification.SEQUENTIAL_ONLY_PASS)
    nl_graph_only = sum(1 for c in nl_paired if c.classification == PairedCaseClassification.GRAPH_ONLY_PASS)
    nl_both_fail = sum(1 for c in nl_paired if c.classification == PairedCaseClassification.BOTH_FAIL)
    nl_incomparable = sum(1 for c in nl_paired if c.classification == PairedCaseClassification.INCOMPARABLE)

    nl_agree_count = nl_both_pass + nl_both_fail
    nl_agreement_rate = (nl_agree_count / nl_cases_count) if nl_cases_count > 0 else 0.0
    nl_regression_rate = (nl_seq_only / nl_cases_count) if nl_cases_count > 0 else 0.0
    nl_improvement_rate = (nl_graph_only / nl_cases_count) if nl_cases_count > 0 else 0.0

    unexpected_errors_seq = sum(
        1 for r in sequential_cases
        if r.generation_status == "failed" or (r.execution_status == "failed" and r.expected_behavior == "answerable")
    )
    unexpected_errors_graph = sum(
        1 for r in graph_cases
        if r.generation_status == "failed" or (r.execution_status == "failed" and r.expected_behavior == "answerable")
    )

    return ComparativeMetrics(
        total_cases=total_cases,
        total_nl_cases=nl_cases_count,
        answerable_cases_count=answerable_count,
        unsupported_cases_count=unsupported_count,
        safety_cases_count=safety_count,
        sequential_metrics=seq_metrics,
        graph_metrics=graph_metrics,
        both_pass_count=both_pass,
        sequential_only_count=seq_only,
        graph_only_count=graph_only,
        both_fail_count=both_fail,
        incomparable_count=incomparable,
        agreement_rate=agreement_rate,
        regression_rate=regression_rate,
        improvement_rate=improvement_rate,
        nl_both_pass_count=nl_both_pass,
        nl_sequential_only_count=nl_seq_only,
        nl_graph_only_count=nl_graph_only,
        nl_both_fail_count=nl_both_fail,
        nl_incomparable_count=nl_incomparable,
        nl_agreement_rate=nl_agreement_rate,
        nl_regression_rate=nl_regression_rate,
        nl_improvement_rate=nl_improvement_rate,
        unexpected_errors_sequential=unexpected_errors_seq,
        unexpected_errors_graph=unexpected_errors_graph,
    )


def evaluate_comparative(
    dataset: List[Dict[str, Any]],
    runner: Optional[QueryRunner] = None,
    schema: Optional[DatabaseSchema] = None,
    metadata: Optional[Dict[str, Any]] = None,
    delay_between_requests: float = 0.0,
    progress_callback: Optional[Callable[[int, int, PairedCaseComparison], None]] = None,
    **runner_kwargs: Any,
) -> ComparativeEvaluationArtifact:
    """Run sequential and graph execution modes against the same dataset cases.

    Ensures:
    - Same question and expectations for both modes.
    - Independent recording of each mode's outcome.
    - Call isolation using the unified QueryRunner.

    Args:
        dataset: List of test cases from baseline evaluation dataset.
        runner: Preconfigured QueryRunner (or created with runner_kwargs).
        schema: Model-facing DatabaseSchema.
        metadata: Provenance metadata dictionary.
        delay_between_requests: Delay in seconds after each natural-language generation attempt.
        progress_callback: Optional callback invoked after each case with (index, total, paired_result).
        **runner_kwargs: Kwargs passed to QueryRunner constructor if runner is None.

    Returns:
        ComparativeEvaluationArtifact containing paired comparisons and metrics.
    """
    if runner is None:
        runner = QueryRunner(**runner_kwargs)

    active_schema = schema or getattr(runner, "_schema", None)
    if active_schema is None and hasattr(runner, "get_schema"):
        active_schema = runner.get_schema()

    sequential_cases: List[CaseEvaluationResult] = []
    graph_cases: List[CaseEvaluationResult] = []
    paired_cases: List[PairedCaseComparison] = []

    total_count = len(dataset)
    for idx, case in enumerate(dataset, 1):
        is_nl = case.get("category") != "safety"

        seq_res = evaluate_case_with_runner(
            case=case,
            runner=runner,
            mode=ExecutionMode.SEQUENTIAL,
            schema=active_schema,
        )
        if is_nl and delay_between_requests > 0:
            time.sleep(delay_between_requests)

        graph_res = evaluate_case_with_runner(
            case=case,
            runner=runner,
            mode=ExecutionMode.GRAPH,
            schema=active_schema,
        )
        if is_nl and delay_between_requests > 0 and idx < total_count:
            time.sleep(delay_between_requests)

        paired = classify_paired_case(seq_res, graph_res)

        sequential_cases.append(seq_res)
        graph_cases.append(graph_res)
        paired_cases.append(paired)

        if progress_callback:
            progress_callback(idx, total_count, paired)

    metrics = compute_comparative_metrics(
        sequential_cases=sequential_cases,
        graph_cases=graph_cases,
        paired_cases=paired_cases,
    )

    meta = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_cases_evaluated": len(dataset),
        "execution_modes": [ExecutionMode.SEQUENTIAL.value, ExecutionMode.GRAPH.value],
        **(metadata or {}),
    }

    return ComparativeEvaluationArtifact(
        metadata=meta,
        metrics=metrics,
        paired_cases=paired_cases,
        sequential_cases=sequential_cases,
        graph_cases=graph_cases,
    )


def generate_comparative_markdown_report(artifact: ComparativeEvaluationArtifact) -> str:
    """Generate a clean, reproducible Markdown report comparing sequential and graph modes."""
    metrics = artifact.metrics
    seq = metrics.sequential_metrics
    graph = metrics.graph_metrics
    meta = artifact.metadata

    timestamp = meta.get("timestamp", "N/A")
    lines: List[str] = [
        "# QueryPilot Sequential vs. LangGraph Comparative Evaluation Report",
        "",
        "> [!IMPORTANT]",
        "> This report provides a paired, evidence-based reliability comparison between the",
        "> existing sequential `QueryPipeline` (baseline) and the `QueryPilotWorkflow` (LangGraph).",
        "",
        "## 1. Executive Summary & Agreement Overview",
        "",
        f"- **Evaluation Timestamp:** `{timestamp}`",
        f"- **Total Cases Evaluated:** {metrics.total_cases}",
        f"- **Natural Language Cases (End-to-End Pipeline Evaluation):** {metrics.total_nl_cases}",
        f"  - **Pipeline Agreement Rate:** {format_rate(metrics.nl_both_pass_count + metrics.nl_both_fail_count, metrics.total_nl_cases)}",
        f"  - **Both Modes Passed:** {format_rate(metrics.nl_both_pass_count, metrics.total_nl_cases)}",
        f"  - **Regressions (Sequential Passed, Graph Failed):** {format_rate(metrics.nl_sequential_only_count, metrics.total_nl_cases)}",
        f"  - **Improvements (Graph Passed, Sequential Failed):** {format_rate(metrics.nl_graph_only_count, metrics.total_nl_cases)}",
        f"  - **Both Modes Failed:** {format_rate(metrics.nl_both_fail_count, metrics.total_nl_cases)}",
        f"  - **Incomparable Cases:** {metrics.nl_incomparable_count}",
        f"- **Safety Fixture Cases (Deterministic AST Policy Check):** {metrics.safety_cases_count}",
        f"  - **Rejection Rate (Both Modes):** {format_rate(seq.safety_rejected_count, metrics.safety_cases_count)}",
        f"- **Overall Dataset Totals (All Cases Combined):** {metrics.total_cases}",
        f"  - **Total Agreement Rate:** {format_rate(metrics.both_pass_count + metrics.both_fail_count, metrics.total_cases)}",
        "",
        "## 2. Side-by-Side Metric Comparison",
        "",
        "| Metric | Sequential Mode | Graph Mode | Delta / Status |",
        "| :--- | :--- | :--- | :--- |",
        f"| **Answerable Semantic Correctness** | {format_rate(seq.semantic_correct_count, seq.answerable_cases_count)} | {format_rate(graph.semantic_correct_count, graph.answerable_cases_count)} | {'Parity' if seq.semantic_correct_count == graph.semantic_correct_count else f'{graph.semantic_correct_count - seq.semantic_correct_count:+d}'} |",
        f"| **Exact Result Match Rate (Answerable)** | {format_rate(seq.exact_match_count, seq.answerable_cases_count)} | {format_rate(graph.exact_match_count, graph.answerable_cases_count)} | {'Parity' if seq.exact_match_count == graph.exact_match_count else f'{graph.exact_match_count - seq.exact_match_count:+d}'} |",
        f"| **Unsupported Refusal Accuracy** | {format_rate(seq.unsupported_correct_count, seq.unsupported_count)} | {format_rate(graph.unsupported_correct_count, graph.unsupported_count)} | {'Parity' if seq.unsupported_correct_count == graph.unsupported_correct_count else f'{graph.unsupported_correct_count - seq.unsupported_correct_count:+d}'} |",
        f"| **Semantic Trap Handling (Q028–Q030)** | {format_rate(seq.semantic_trap_correct_count, seq.semantic_trap_count)} | {format_rate(graph.semantic_trap_correct_count, graph.semantic_trap_count)} | {'Parity' if seq.semantic_trap_correct_count == graph.semantic_trap_correct_count else f'{graph.semantic_trap_correct_count - seq.semantic_trap_correct_count:+d}'} |",
        f"| **Safety Fixture Rejection Rate** | {format_rate(seq.safety_rejected_count, seq.safety_fixture_count)} | {format_rate(graph.safety_rejected_count, graph.safety_fixture_count)} | {'Parity' if seq.safety_rejected_count == graph.safety_rejected_count else f'{graph.safety_rejected_count - seq.safety_rejected_count:+d}'} |",
        f"| **Unexpected Pipeline Errors** | {metrics.unexpected_errors_sequential} | {metrics.unexpected_errors_graph} | {'Parity' if metrics.unexpected_errors_sequential == metrics.unexpected_errors_graph else f'{metrics.unexpected_errors_graph - metrics.unexpected_errors_sequential:+d}'} |",
        "",
        "> [!NOTE]",
        "> **Safety Fixtures:** Evaluated directly via the deterministic AST policy validator (`validate_sql`)",
        "> without natural-language generation or database execution, isolating validator-level safety policy from generation.",
        "",
        "## 3. Paired Regression & Difference Analysis",
        "",
    ]

    regressions = [c for c in artifact.paired_cases if c.classification == PairedCaseClassification.SEQUENTIAL_ONLY_PASS]
    improvements = [c for c in artifact.paired_cases if c.classification == PairedCaseClassification.GRAPH_ONLY_PASS]
    both_fails = [c for c in artifact.paired_cases if c.classification == PairedCaseClassification.BOTH_FAIL]

    if regressions:
        lines.append("### Confirmed Regressions (Sequential Passed, Graph Failed)")
        lines.append("")
        lines.append("| Case ID | Category | Question | Graph Failure Stage | Graph Error |")
        lines.append("| :--- | :--- | :--- | :--- | :--- |")
        for r in regressions:
            g_stage = r.graph_result.failure_stage.value if r.graph_result.failure_stage else "unknown"
            g_err = r.graph_result.generation_error or r.graph_result.execution_error or "N/A"
            lines.append(f"| `{r.case_id}` | {r.category} | {r.question or 'N/A'} | {g_stage} | {g_err} |")
        lines.append("")
    else:
        lines.append("> [!NOTE]")
        lines.append("> **Zero Regressions Detected:** The LangGraph workflow matches the sequential baseline on all passing cases.")
        lines.append("")

    if improvements:
        lines.append("### Graph Improvements (Graph Passed, Sequential Failed)")
        lines.append("")
        for imp in improvements:
            lines.append(f"- **`{imp.case_id}`** ({imp.category}): {imp.question or 'N/A'}")
        lines.append("")

    if both_fails:
        lines.append("### Mutual Failures (Both Modes Failed)")
        lines.append("")
        for bf in both_fails:
            lines.append(f"- **`{bf.case_id}`** ({bf.category}): Expected `{bf.expected_behavior}`, difference: {bf.difference_summary}")
        lines.append("")

    lines.extend([
        "## 4. Per-Case Comparative Inventory",
        "",
        "| Case ID | Category | Expected Behavior | Seq Passed | Graph Passed | Exact Match Agree | Outcome Classification |",
        "| :--- | :--- | :--- | :---: | :---: | :---: | :--- |",
    ])

    for c in artifact.paired_cases:
        seq_sym = "✅" if c.sequential_passed else "❌"
        graph_sym = "✅" if c.graph_passed else "❌"
        exact_sym = "✅" if c.exact_match_agree else "⚠️"
        lines.append(
            f"| `{c.case_id}` | {c.category} | {c.expected_behavior} | {seq_sym} | {graph_sym} | {exact_sym} | `{c.classification.value}` |"
        )

    lines.append("")
    return "\n".join(lines)


def sanitize_sensitive_data(obj: Any) -> Any:
    """Recursively redact passwords, connection strings, and API keys from data structures."""
    if isinstance(obj, dict):
        cleaned: Dict[str, Any] = {}
        for k, v in obj.items():
            k_lower = str(k).lower()
            if any(
                term == k_lower
                or f"_{term}" in k_lower
                or f"{term}_" in k_lower
                or f"{term}=" in k_lower
                for term in ("password", "pwd", "api_key", "secret", "bearer", "connection_string", "credentials")
            ):
                cleaned[k] = "[REDACTED]"
            else:
                cleaned[k] = sanitize_sensitive_data(v)
        return cleaned
    elif isinstance(obj, list):
        return [sanitize_sensitive_data(item) for item in obj]
    elif isinstance(obj, str):
        import re
        s = obj
        s = re.sub(r'(?i)(password|pwd)\s*=\s*[^;\s&]+', r'\1=[REDACTED]', s)
        s = re.sub(r'(?i)(bearer)\s+[a-zA-Z0-9_\-\.]+', r'\1 [REDACTED]', s)
        return s
    return obj


def save_comparative_evaluation(
    artifact: ComparativeEvaluationArtifact,
    output_dir: Optional[Path] = None,
    base_name: str = "comparison_1_21",
) -> Tuple[Path, Path]:
    """Save comparative evaluation JSON and Markdown reports to disk.

    STRICT GUARANTEES:
    - Never overwrites historical baseline evaluation files.
    - Sanitizes sensitive credentials and secrets recursively across output.

    Args:
        artifact: ComparativeEvaluationArtifact to save.
        output_dir: Directory to save to (defaults to data/evaluation).
        base_name: Base filename (e.g. comparison_1_21).

    Returns:
        Tuple of (json_path, report_path).

    Raises:
        ValueError: If base_name targets a protected historical artifact.
    """
    if output_dir is None:
        target_dir = Path("data/evaluation").resolve()
    else:
        target_dir = Path(output_dir).resolve()

    target_dir.mkdir(parents=True, exist_ok=True)

    clean_base = base_name.replace(".json", "").replace(".md", "")
    json_filename = f"{clean_base}.json"
    md_filename = f"{clean_base}_report.md"

    # Strictly protect historical artifacts by exact file name or stem
    protected_stems = {Path(p).stem for p in PROTECTED_EVALUATION_FILES}
    if (
        clean_base in protected_stems
        or json_filename in PROTECTED_EVALUATION_FILES
        or md_filename in PROTECTED_EVALUATION_FILES
        or f"{clean_base}.md" in PROTECTED_EVALUATION_FILES
    ):
        raise ValueError(
            f"Cannot overwrite protected historical artifact! Base name '{base_name}' is forbidden."
        )

    json_path = target_dir / json_filename
    md_path = target_dir / md_filename

    # Double check absolute paths against known protected artifacts
    for protected in PROTECTED_EVALUATION_FILES:
        if (target_dir / protected).resolve() in (json_path.resolve(), md_path.resolve()):
            raise ValueError(f"Target path resolves to protected artifact: {protected}")

    # Write sanitized JSON
    json_data = sanitize_sensitive_data(artifact.to_dict())
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(json_data, f, indent=2)

    # Write Markdown Report
    report_text = sanitize_sensitive_data(generate_comparative_markdown_report(artifact))
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(report_text)

    logger.info(f"Saved comparative evaluation to {json_path} and {md_path}")
    return json_path, md_path


def reclassify_comparative_artifact(
    artifact_path: Union[Path, str],
    questions_path: Optional[Union[Path, str]] = None,
) -> ComparativeEvaluationArtifact:
    """Deterministically reclassify a saved comparative evaluation JSON artifact without external calls.

    Evaluates whether non-exact-match cases return identical data values differing only in
    column aliases/projection names, aligning with the project's documented semantic correctness contract.

    Args:
        artifact_path: Path to the saved comparative evaluation JSON artifact.
        questions_path: Optional path to baseline questions dataset (defaults to data/evaluation/baseline_questions.json).

    Returns:
        Reclassified ComparativeEvaluationArtifact with updated paired classifications and metrics.
    """
    path = Path(artifact_path).resolve()
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    q_path = Path(questions_path or "data/evaluation/baseline_questions.json").resolve()
    with open(q_path, "r", encoding="utf-8") as f:
        questions = {c["id"]: c for c in json.load(f)}

    def reclassify_case(res: CaseEvaluationResult) -> CaseEvaluationResult:
        if res.expected_behavior != "answerable" or res.execution_status != "success":
            return res
        if res.result_matches_reference or res.exact_result_match:
            return res

        ref = questions.get(res.case_id, {}).get("reference_result")
        gen = res.generated_result
        if is_alias_difference_semantically_equivalent(gen, ref):
            updated = res.model_copy()
            updated.semantic_correct = True
            updated.semantic_status = SemanticStatus.CORRECT
            updated.mismatch_classification = MismatchClassification.SEMANTIC_CORRECT_EXACT_DIFFERENCE.value
            updated.failure_stage = None
            if not updated.notes:
                updated.notes = "Column alias difference with identical returned data values."
            return updated
        return res

    reclassified_seq = [reclassify_case(CaseEvaluationResult(**c)) for c in data["sequential_cases"]]
    reclassified_graph = [reclassify_case(CaseEvaluationResult(**c)) for c in data["graph_cases"]]
    reclassified_paired = [
        classify_paired_case(s, g) for s, g in zip(reclassified_seq, reclassified_graph)
    ]

    metrics = compute_comparative_metrics(
        sequential_cases=reclassified_seq,
        graph_cases=reclassified_graph,
        paired_cases=reclassified_paired,
    )

    metadata = dict(data.get("metadata", {}))
    metadata["audit_reclassified"] = True
    metadata["reclassification_timestamp"] = datetime.now(timezone.utc).isoformat()

    return ComparativeEvaluationArtifact(
        metadata=metadata,
        metrics=metrics,
        paired_cases=reclassified_paired,
        sequential_cases=reclassified_seq,
        graph_cases=reclassified_graph,
    )
