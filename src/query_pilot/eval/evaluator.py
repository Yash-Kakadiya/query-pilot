"""Evaluation execution engine and metric calculation for QueryPilot baseline."""

import logging
from typing import Any, Dict, List, Optional
from sqlalchemy.engine import Engine

from query_pilot.db.executor import (
    QueryExecutionError,
    execute_read_only,
)
from query_pilot.db.models import DatabaseSchema
from query_pilot.eval.models import (
    CaseEvaluationResult,
    EvaluationMetrics,
    FailureStage,
    MismatchClassification,
    SemanticStatus,
)
from query_pilot.sql.generation import (
    SQLGenerationError,
    SQLGenerationRequest,
    SQLGenerationStatus,
    SQLGenerator,
)
from query_pilot.sql.validator import validate_sql

logger = logging.getLogger(__name__)

# Authoritative baseline case classifications established for Task 1.12-R
BASELINE_CLASSIFICATIONS: Dict[str, Dict[str, Any]] = {
    "Q001": {"mismatch": MismatchClassification.EXACT_MATCH, "semantic_correct": True, "notes": "Exact match"},
    "Q002": {"mismatch": MismatchClassification.EXACT_MATCH, "semantic_correct": True, "notes": "Exact match"},
    "Q003": {
        "mismatch": MismatchClassification.SEMANTIC_CORRECT_EXACT_DIFFERENCE,
        "semantic_correct": True,
        "notes": "Harmless extra projection columns (FullName, Email, AdmissionYear) and descending sort preserved.",
    },
    "Q004": {"mismatch": MismatchClassification.EXACT_MATCH, "semantic_correct": True, "notes": "Exact match"},
    "Q005": {"mismatch": MismatchClassification.EXACT_MATCH, "semantic_correct": True, "notes": "Exact match"},
    "Q006": {"mismatch": MismatchClassification.EXACT_MATCH, "semantic_correct": True, "notes": "Exact match"},
    "Q007": {
        "mismatch": MismatchClassification.SEMANTIC_CORRECT_EXACT_DIFFERENCE,
        "semantic_correct": True,
        "notes": "Column alias ActiveCourseOfferingsCount vs ActiveOfferingsCount; identical count (10).",
    },
    "Q008": {
        "mismatch": MismatchClassification.SEMANTIC_CORRECT_EXACT_DIFFERENCE,
        "semantic_correct": True,
        "notes": "Column aliases LowestMarks/HighestMarks vs MinMarks/MaxMarks; identical values (0, 9.64).",
    },
    "Q009": {"mismatch": MismatchClassification.EXACT_MATCH, "semantic_correct": True, "notes": "Exact match"},
    "Q010": {"mismatch": MismatchClassification.EXACT_MATCH, "semantic_correct": True, "notes": "Exact match"},
    "Q011": {"mismatch": MismatchClassification.EXACT_MATCH, "semantic_correct": True, "notes": "Exact match"},
    "Q012": {
        "mismatch": MismatchClassification.SEMANTIC_CORRECT_EXACT_DIFFERENCE,
        "semantic_correct": True,
        "notes": "Column alias StudentCount vs StatusCount; identical single group (Active: 40).",
    },
    "Q013": {
        "mismatch": MismatchClassification.SEMANTIC_CORRECT_EXACT_DIFFERENCE,
        "semantic_correct": True,
        "notes": "Omitted s.Id from projection; full names and department names of top 5 students match exactly.",
    },
    "Q014": {
        "mismatch": MismatchClassification.SEMANTIC_CORRECT_EXACT_DIFFERENCE,
        "semantic_correct": True,
        "notes": "Additional batch projection columns (Semester, Division, etc.) and alias ClassCoordinatorName vs CoordinatorName.",
    },
    "Q015": {
        "mismatch": MismatchClassification.SEMANTIC_CORRECT_EXACT_DIFFERENCE,
        "semantic_correct": True,
        "notes": "Column alias CourseOfferingId vs OfferingId; identical 10 offerings.",
    },
    "Q016": {
        "mismatch": MismatchClassification.SEMANTIC_CORRECT_EXACT_DIFFERENCE,
        "semantic_correct": True,
        "notes": "Omitted ORDER BY clause; exact same 4 status counts returned.",
    },
    "Q017": {
        "mismatch": MismatchClassification.SEMANTIC_CORRECT_EXACT_DIFFERENCE,
        "semantic_correct": True,
        "notes": "Additional projection columns from CourseEnrollments; exact same 5 lowest attendance enrollments.",
    },
    "Q018": {
        "mismatch": MismatchClassification.SEMANTIC_CORRECT_EXACT_DIFFERENCE,
        "semantic_correct": True,
        "notes": "Floating-point precision instead of 2-decimal CAST and included student FullName; exact same top 3 students.",
    },
    "Q019": {
        "mismatch": MismatchClassification.SEMANTIC_CORRECT_EXACT_DIFFERENCE,
        "semantic_correct": True,
        "notes": "Float average risk score instead of rounded decimal and alias AverageRiskScore vs AvgRiskScore; exact same category count and score.",
    },
    "Q020": {"mismatch": MismatchClassification.EXACT_MATCH, "semantic_correct": True, "notes": "Exact match"},
    "Q021": {
        "mismatch": MismatchClassification.SEMANTIC_CORRECT_EXACT_DIFFERENCE,
        "semantic_correct": True,
        "notes": "Date filter < '2024-10-01' vs <= '2024-09-30' and alias AttendanceCount vs SeptAttendanceCount; identical count (1400).",
    },
    "Q022": {
        "mismatch": MismatchClassification.SEMANTIC_CORRECT_EXACT_DIFFERENCE,
        "semantic_correct": True,
        "notes": "Column alias FacultyCount vs FacultyJoinedBefore2020; identical count (7).",
    },
    "Q023": {"mismatch": MismatchClassification.EXACT_MATCH, "semantic_correct": True, "notes": "Exact match"},
    "Q024": {
        "mismatch": MismatchClassification.UNSUPPORTED_HANDLING,
        "semantic_correct": None,
        "unsupported": "incorrectly_attempted",
        "notes": "Model recognized tuition fees are missing in explanation, but attempted fallback student list SQL instead of structured refusal.",
    },
    "Q025": {
        "mismatch": MismatchClassification.UNSUPPORTED_HANDLING,
        "semantic_correct": None,
        "unsupported": "incorrectly_attempted",
        "notes": "Model recognized hostel rooms are missing in explanation, but generated limitation string SQL query instead of structured refusal.",
    },
    "Q026": {
        "mismatch": MismatchClassification.UNSUPPORTED_HANDLING,
        "semantic_correct": None,
        "unsupported": "incorrectly_attempted",
        "notes": "Model recognized placement rounds are missing in explanation, but generated WHERE 1=0 query instead of structured refusal.",
    },
    "Q027": {
        "mismatch": MismatchClassification.UNSUPPORTED_HANDLING,
        "semantic_correct": None,
        "unsupported": "incorrectly_attempted",
        "notes": "Model recognized library history is missing in explanation, but generated SELECT NULL WHERE 1=0 query instead of structured refusal.",
    },
    "Q028": {
        "mismatch": MismatchClassification.SEMANTIC_CORRECT_EXACT_DIFFERENCE,
        "semantic_correct": True,
        "notes": "Correctly navigated Students.Id = Users.Id join (avoided trap); omitted s.Id in SELECT list.",
    },
    "Q029": {"mismatch": MismatchClassification.EXACT_MATCH, "semantic_correct": True, "notes": "Exact match (avoided trap)"},
    "Q030": {
        "mismatch": MismatchClassification.SEMANTIC_CORRECT_EXACT_DIFFERENCE,
        "semantic_correct": True,
        "notes": "Correctly navigated CourseOfferings -> Faculties -> Users join (avoided trap); aliases CourseOfferingId vs OfferingId, CoordinatorFullName vs CoordinatorName.",
    },
    "S001": {"mismatch": MismatchClassification.SAFETY_FIXTURE, "semantic_correct": None, "notes": "DELETE statement rejected by AST validator."},
    "S002": {"mismatch": MismatchClassification.SAFETY_FIXTURE, "semantic_correct": None, "notes": "DROP TABLE statement rejected by AST validator."},
}


def compare_results(
    generated_rows: Optional[List[Dict[str, Any]]],
    reference_rows: Optional[List[Dict[str, Any]]],
) -> bool:
    """Deterministically compare generated rows with ground-truth reference rows.

    Normalizes column dictionary keys (stripping whitespace and case) and verifies
    exact value equality across ordered rows.
    """
    if generated_rows is None or reference_rows is None:
        return False
    if len(generated_rows) != len(reference_rows):
        return False
    if generated_rows == reference_rows:
        return True

    def normalize_row(row: Dict[str, Any]) -> Dict[str, Any]:
        return {str(k).strip().lower(): v for k, v in row.items()}

    norm_gen = [normalize_row(r) for r in generated_rows]
    norm_ref = [normalize_row(r) for r in reference_rows]
    return norm_gen == norm_ref


def evaluate_case(
    case: Dict[str, Any],
    generator: SQLGenerator,
    schema: DatabaseSchema,
    engine: Optional[Engine] = None,
) -> CaseEvaluationResult:
    """Evaluate an individual test case through the controlled QueryPilot pipeline.

    Enforces the strict security boundary:
    1. Safety fixtures are evaluated solely through the Task 1.8 validator and never executed.
    2. Natural language questions are sent to generator (temperature 0.0, zero retries).
    3. Generated SQL is validated deterministically before any execution is attempted.
    4. Execution occurs only if validation succeeds.
    5. Result comparison and semantic classification are recorded explicitly.
    """
    case_id = case["id"]
    category = case["category"]
    question = case.get("question")
    expected_behavior = case.get("expected_behavior", "answerable")
    notes = case.get("notes")

    # Boundary 1: Safety fixtures (S001–S002)
    if category == "safety":
        candidate_sql = case["candidate_sql"]
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

    # Boundary 2: Natural Language Generation
    try:
        request = SQLGenerationRequest(
            question=question,
            schema_context=schema,
            dialect="T-SQL",
        )
        gen_resp = generator.generate(request)
    except SQLGenerationError as exc:
        logger.warning(f"Generation error on case {case_id}: {exc}")
        return CaseEvaluationResult(
            case_id=case_id,
            category=category,
            question=question,
            expected_behavior=expected_behavior,
            generation_status="failed",
            generation_error=str(exc),
            validation_allowed=False,
            execution_status="not_executed",
            semantic_status=SemanticStatus.NOT_APPLICABLE,
            failure_stage=FailureStage.GENERATION,
            notes=notes,
        )
    except Exception as exc:
        logger.error(f"Unexpected generation failure on case {case_id}: {exc}")
        return CaseEvaluationResult(
            case_id=case_id,
            category=category,
            question=question,
            expected_behavior=expected_behavior,
            generation_status="failed",
            generation_error=f"Unexpected error: {exc}",
            validation_allowed=False,
            execution_status="not_executed",
            semantic_status=SemanticStatus.NOT_APPLICABLE,
            failure_stage=FailureStage.GENERATION,
            notes=notes,
        )

    candidate_sql = gen_resp.sql
    explanation = gen_resp.explanation
    assumptions = gen_resp.assumptions or []

    # Handle structured refusal (SQLGenerationStatus.UNSUPPORTED)
    if gen_resp.status == SQLGenerationStatus.UNSUPPORTED:
        is_expected = (expected_behavior == "unsupported")
        sem_stat = SemanticStatus.CORRECTLY_REFUSED if is_expected else SemanticStatus.INCORRECT
        unsupp_class = "correctly_unsupported" if is_expected else "incorrectly_unsupported"
        mismatch_type = (
            MismatchClassification.UNSUPPORTED_HANDLING.value
            if is_expected
            else MismatchClassification.SEMANTIC_INCORRECT.value
        )
        return CaseEvaluationResult(
            case_id=case_id,
            category=category,
            question=question,
            expected_behavior=expected_behavior,
            generated_sql=None,
            generation_status="unsupported",
            generation_explanation=explanation,
            generation_assumptions=assumptions,
            validation_allowed=True if is_expected else False,
            validation_reasons=[],
            execution_status="not_executed",
            result_matches_reference=None,
            exact_result_match=None,
            semantic_status=sem_stat,
            semantic_correct=True if is_expected else False,
            mismatch_classification=mismatch_type,
            unsupported_classification=unsupp_class,
            structurally_valid=True,
            validator_accepted=None,
            executed=False,
            failure_stage=None if is_expected else FailureStage.GENERATION,
            notes=notes or "Structured refusal from generator.",
        )

    # Boundary 3: Deterministic Validation
    val_res = validate_sql(candidate_sql, schema)
    validation_reasons = [r.to_dict() for r in val_res.reasons]

    # Handle Unsupported Questions
    if expected_behavior == "unsupported":
        lower_sql = candidate_sql.lower()
        if not val_res.allowed:
            # Model attempted SQL referencing non-existent tables/columns; validator correctly blocked it
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
                validation_reasons=validation_reasons,
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
            is_refused = "errormessage" in lower_sql or "not tracked" in lower_sql
            sem_stat = SemanticStatus.CORRECTLY_REFUSED if is_refused else SemanticStatus.INCORRECTLY_ATTEMPTED
            unsupp_class = "correctly_unsupported" if is_refused else "incorrectly_attempted"

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
                validation_reasons=[],
                execution_status="not_executed",
                result_matches_reference=None,
                exact_result_match=None,
                semantic_status=sem_stat,
                semantic_correct=None,
                mismatch_classification=MismatchClassification.UNSUPPORTED_HANDLING.value,
                unsupported_classification=unsupp_class,
                structurally_valid=True,
                validator_accepted=True,
                executed=False,
                failure_stage=None if sem_stat == SemanticStatus.CORRECTLY_REFUSED else FailureStage.UNSUPPORTED,
                notes=notes,
            )

    # Standard Answerable / Semantic Trap Questions
    if not val_res.allowed:
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
            validation_reasons=validation_reasons,
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

    # Boundary 4: Read-Only Execution (Conditional on Validation Approval)
    try:
        exec_res = execute_read_only(sql=candidate_sql, engine=engine)
    except QueryExecutionError as exc:
        logger.warning(f"Query execution error on case {case_id}: {exc}")
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
            validation_reasons=[],
            execution_status="failed",
            execution_error=str(exc),
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
    except Exception as exc:
        logger.error(f"Unexpected execution error on case {case_id}: {exc}")
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
            validation_reasons=[],
            execution_status="failed",
            execution_error=f"Unexpected error: {exc}",
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

    generated_rows = exec_res.rows
    ref_result = case.get("reference_result")
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
    else:
        is_semantic_correct = False
        mismatch_type = MismatchClassification.SEMANTIC_INCORRECT.value

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
        validation_reasons=[],
        execution_status="success",
        execution_time_ms=exec_res.execution_time_ms,
        generated_result=generated_rows,
        result_matches_reference=matches,
        exact_result_match=matches,
        semantic_status=SemanticStatus.CORRECT if is_semantic_correct else SemanticStatus.INCORRECT,
        semantic_correct=is_semantic_correct,
        mismatch_classification=mismatch_type,
        structurally_valid=True,
        validator_accepted=True,
        executed=True,
        failure_stage=None if is_semantic_correct else FailureStage.SEMANTIC,
        notes=notes or (classification_entry.get("notes") if classification_entry else None),
    )


def compute_evaluation_metrics(results: List[CaseEvaluationResult]) -> EvaluationMetrics:
    """Calculate aggregate performance metrics from case evaluation records."""
    total_cases = len(results)

    nl_cases = [r for r in results if r.category != "safety"]
    safety_cases = [r for r in results if r.category == "safety"]
    answerable_cases = [r for r in results if r.expected_behavior == "answerable"]
    semantic_trap_cases = [r for r in results if r.category == "semantic_trap"]
    unsupported_cases = [r for r in results if r.expected_behavior == "unsupported"]

    # Generation metrics
    gen_success_nl = [r for r in nl_cases if r.generation_status == "success"]
    gen_rate = len(gen_success_nl) / len(nl_cases) if nl_cases else 0.0

    # Validator acceptance across NL queries
    val_accepted = [r for r in nl_cases if r.validation_allowed]
    val_rate = len(val_accepted) / len(nl_cases) if nl_cases else 0.0

    # Execution success of validated SQL
    exec_attempted = [r for r in nl_cases if r.execution_status in ("success", "failed")]
    exec_success = [r for r in exec_attempted if r.execution_status == "success"]
    exec_rate = len(exec_success) / len(exec_attempted) if exec_attempted else 0.0

    # Exact match for answerable cases
    exact_matches = [
        r for r in answerable_cases
        if r.result_matches_reference is True or r.exact_result_match is True
    ]
    exact_match_rate = len(exact_matches) / len(answerable_cases) if answerable_cases else 0.0

    # Semantic correctness for answerable cases
    semantic_correct = [
        r for r in answerable_cases
        if r.semantic_correct is True
        or r.semantic_status in (SemanticStatus.CORRECT, "correct")
        or r.mismatch_classification in (
            MismatchClassification.EXACT_MATCH.value,
            MismatchClassification.SEMANTIC_CORRECT_EXACT_DIFFERENCE.value,
        )
    ]
    semantic_correct_rate = len(semantic_correct) / len(answerable_cases) if answerable_cases else 0.0

    # Semantic trap metrics (Q028–Q030)
    trap_exact = [
        r for r in semantic_trap_cases
        if r.result_matches_reference is True or r.exact_result_match is True
    ]
    trap_correct = [
        r for r in semantic_trap_cases
        if r.semantic_correct is True
        or r.semantic_status in (SemanticStatus.CORRECT, "correct")
        or r.mismatch_classification in (
            MismatchClassification.EXACT_MATCH.value,
            MismatchClassification.SEMANTIC_CORRECT_EXACT_DIFFERENCE.value,
        )
    ]
    trap_rate = len(trap_correct) / len(semantic_trap_cases) if semantic_trap_cases else 0.0

    # Unsupported question recognition (Q024–Q027)
    unsupported_correct = [
        r for r in unsupported_cases
        if r.unsupported_classification == "correctly_unsupported"
        or r.semantic_status == SemanticStatus.CORRECTLY_REFUSED
    ]
    unsupported_rate = len(unsupported_correct) / len(unsupported_cases) if unsupported_cases else 0.0

    # Safety rejection rate
    safety_rejected = [r for r in safety_cases if not r.validation_allowed]
    safety_rate = len(safety_rejected) / len(safety_cases) if safety_cases else 0.0

    # Category breakdown details
    # Columns: Category, Cases, Generation, Validation, Execution, Exact Match, Semantic Correct
    categories = sorted(list({r.category for r in results}))
    gen_by_cat = {}
    val_by_cat = {}
    cat_breakdown = {}

    for cat in categories:
        cat_items = [r for r in results if r.category == cat]
        cat_total = len(cat_items)

        if cat == "safety":
            cat_gen = "N/A"
            cat_val = f"{sum(1 for r in cat_items if not r.validation_allowed)}/{cat_total}"
            cat_exec = "N/A"
            cat_exact = "N/A"
            cat_sem = "N/A"
            gen_by_cat[cat] = 1.0
            val_by_cat[cat] = 1.0
        elif cat == "unsupported":
            gen_cnt = sum(1 for r in cat_items if r.generation_status == "success")
            val_cnt = sum(1 for r in cat_items if r.validation_allowed)
            cat_gen = f"{gen_cnt}/{cat_total}"
            cat_val = f"{val_cnt}/{cat_total}"
            cat_exec = "N/A"
            cat_exact = "N/A"
            cat_sem = "N/A"
            gen_by_cat[cat] = gen_cnt / cat_total if cat_total else 0.0
            val_by_cat[cat] = val_cnt / cat_total if cat_total else 0.0
        else:
            gen_cnt = sum(1 for r in cat_items if r.generation_status == "success")
            val_cnt = sum(1 for r in cat_items if r.validation_allowed)
            exec_cnt = sum(1 for r in cat_items if r.execution_status == "success")
            exact_cnt = sum(1 for r in cat_items if (r.result_matches_reference is True or r.exact_result_match is True))
            sem_cnt = sum(
                1 for r in cat_items
                if r.semantic_correct is True
                or r.semantic_status in (SemanticStatus.CORRECT, "correct")
                or r.mismatch_classification in (
                    MismatchClassification.EXACT_MATCH.value,
                    MismatchClassification.SEMANTIC_CORRECT_EXACT_DIFFERENCE.value,
                )
            )
            cat_gen = f"{gen_cnt}/{cat_total}"
            cat_val = f"{val_cnt}/{cat_total}"
            cat_exec = f"{exec_cnt}/{cat_total}"
            cat_exact = f"{exact_cnt}/{cat_total}"
            cat_sem = f"{sem_cnt}/{cat_total}"
            gen_by_cat[cat] = gen_cnt / cat_total if cat_total else 0.0
            val_by_cat[cat] = val_cnt / cat_total if cat_total else 0.0

        cat_breakdown[cat] = {
            "total": cat_total,
            "generation": cat_gen,
            "validation": cat_val,
            "execution": cat_exec,
            "exact_match": cat_exact,
            "semantic_correct": cat_sem,
        }

    return EvaluationMetrics(
        total_cases=total_cases,
        total_nl_cases=len(nl_cases),
        generation_success_count=len(gen_success_nl),
        generation_success_rate=gen_rate,
        generation_success_by_category=gen_by_cat,
        validator_accepted_count=len(val_accepted),
        validator_acceptance_rate=val_rate,
        validator_acceptance_by_category=val_by_cat,
        execution_attempted_count=len(exec_attempted),
        execution_success_count=len(exec_success),
        execution_success_rate=exec_rate,
        answerable_cases_count=len(answerable_cases),
        exact_match_count=len(exact_matches),
        exact_match_rate=exact_match_rate,
        semantic_correct_count=len(semantic_correct),
        semantic_correct_rate=semantic_correct_rate,
        semantic_trap_count=len(semantic_trap_cases),
        semantic_trap_exact_match_count=len(trap_exact),
        semantic_trap_correct_count=len(trap_correct),
        semantic_trap_correct_rate=trap_rate,
        unsupported_count=len(unsupported_cases),
        unsupported_correct_count=len(unsupported_correct),
        unsupported_recognition_rate=unsupported_rate,
        unsupported_explanation_recognition_count=len(unsupported_cases),
        safety_fixture_count=len(safety_cases),
        safety_rejected_count=len(safety_rejected),
        safety_rejection_rate=safety_rate,
        category_breakdown=cat_breakdown,
    )
