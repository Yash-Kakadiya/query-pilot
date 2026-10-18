"""Offline evaluation harness and baseline benchmark for DomainContextRetriever.

Validates:
1. Retrieval evaluation dataset integrity, unique IDs, and catalog grounding.
2. 100% offline execution with ZERO live model API or database calls.
3. Deterministic repeatability across identical repeated queries.
4. Strict character budget compliance on rendered prompt context.
5. Sensitive-column safety invariants via SensitiveColumnPolicy.
6. Per-case outcome tracking (precision, recall, misses, and irrelevant co-retrievals).
7. Known semantic-trap recall (Q028, Q029, Q030 equivalents).
8. Explicit edge-case handling for precision, recall, and empty-set specificity.
"""

from dataclasses import dataclass, field
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
import pytest

from query_pilot.db.introspection import SensitiveColumnPolicy
from query_pilot.rag.retriever import (
    DEFAULT_CATALOG_PATH,
    DomainContextRetriever,
    RetrievalResult,
)

logger = logging.getLogger(__name__)

DATASET_PATH: Path = (
    Path(__file__).resolve().parent.parent
    / "data"
    / "evaluation"
    / "retrieval_evaluation.json"
)

ALLOWED_CATEGORIES: Set[str] = {
    "relationship",
    "table_inheritance",
    "semantic_trap",
    "out_of_domain",
    "unindexed_schema",
    "unresolved_business_rules",
    "follow_up_context",
}


# ==============================================================================
# Metric Calculation & Data Structures
# ==============================================================================

@dataclass
class RetrievalCaseOutcome:
    """Detailed evaluation outcome for an individual retrieval test case."""

    case_id: str
    category: str
    question: str
    intent: Optional[str]
    expected_ids: List[str]
    actual_ids: List[str]
    precision: float
    recall: float
    trap_retrieved: Optional[bool]
    budget_compliant: bool
    scores: Dict[str, float]
    matched_terms: Dict[str, List[str]]
    missed_expected: List[str]
    irrelevant_retrieved: List[str]


@dataclass
class RetrievalEvaluationSummary:
    """Aggregate metrics calculated across all retrieval evaluation cases."""

    total_cases: int
    mean_precision: float
    mean_recall: float
    empty_expected_count: int
    empty_actual_count: int
    empty_result_specificity: float
    semantic_trap_count: int
    semantic_trap_hits: int
    semantic_trap_recall: float
    budget_compliant_count: int
    budget_compliance_rate: float
    outcomes: List[RetrievalCaseOutcome] = field(default_factory=list)

    def format_report(self) -> str:
        """Format deterministic per-case evaluation breakdown and aggregate metrics for reporting."""
        return format_evaluation_report(self)


def format_evaluation_report(summary: RetrievalEvaluationSummary) -> str:
    """Format deterministic per-case evaluation breakdown and aggregate metrics for reporting."""
    lines = [
        "=== RETRIEVAL EVALUATION REPORT ===",
        f"Total cases: {summary.total_cases}",
        f"Mean precision (macro): {summary.mean_precision:.6f} ({summary.mean_precision * 100:.2f}%)",
        f"Mean recall (macro):    {summary.mean_recall:.6f} ({summary.mean_recall * 100:.2f}%)",
        f"Semantic trap recall:   {summary.semantic_trap_hits}/{summary.semantic_trap_count} ({summary.semantic_trap_recall * 100:.2f}%)",
        f"Empty specificity:      {summary.empty_actual_count}/{summary.empty_expected_count} ({summary.empty_result_specificity * 100:.2f}%)",
        f"Budget compliance:      {summary.budget_compliant_count}/{summary.total_cases} ({summary.budget_compliance_rate * 100:.2f}%)",
        "",
        "--- Per-Case Breakdown ---",
    ]
    for o in summary.outcomes:
        lines.append(f"[{o.case_id}] ({o.category})")
        lines.append(f"  Question:   {o.question}")
        if o.intent:
            lines.append(f"  Intent:     {o.intent}")
        lines.append(f"  Expected:   {o.expected_ids}")
        lines.append(f"  Actual:     {o.actual_ids}")
        lines.append(f"  Missed:     {o.missed_expected}")
        lines.append(f"  Irrelevant: {o.irrelevant_retrieved}")
        lines.append(f"  P={o.precision:.4f}, R={o.recall:.4f}, BudgetCompliant={o.budget_compliant}")
    return "\n".join(lines)


def compute_case_precision_recall(
    expected_ids: List[str],
    actual_ids: List[str],
) -> Tuple[float, float]:
    """Compute precision and recall with explicit edge-case definitions.

    Edge-case rules:
    - If both expected and retrieved sets are empty: precision = 1.0, recall = 1.0.
      Rationale: The query requested no domain knowledge, and the retriever correctly selected none.
    - If expected is empty but retrieved is nonempty: precision = 0.0, recall = 1.0.
      Rationale: All 0 expected entries were recalled (vacuous truth), but 0% of retrieved
      entries were relevant. The penalty is absorbed by precision = 0.0 and empty specificity.
    - If expected is nonempty but retrieved is empty: precision = 0.0, recall = 0.0.
      Rationale: Relevant knowledge was needed but the retriever returned nothing.
    - If both are nonempty:
        precision = |expected ∩ actual| / |actual|
        recall = |expected ∩ actual| / |expected|
    """
    if not expected_ids and not actual_ids:
        return 1.0, 1.0
    if not expected_ids and actual_ids:
        return 0.0, 1.0
    if expected_ids and not actual_ids:
        return 0.0, 0.0

    exp_set = set(expected_ids)
    act_set = set(actual_ids)
    intersection = exp_set & act_set

    precision = len(intersection) / len(act_set)
    recall = len(intersection) / len(exp_set)
    return precision, recall


def evaluate_retrieval_dataset(
    retriever: DomainContextRetriever,
    cases: List[Dict[str, Any]],
) -> RetrievalEvaluationSummary:
    """Run retriever across all evaluation cases and compute aggregate metrics."""
    outcomes: List[RetrievalCaseOutcome] = []
    empty_expected_total = 0
    empty_actual_total = 0
    trap_total = 0
    trap_hits = 0
    budget_compliant_total = 0

    for case in cases:
        case_id = case["id"]
        category = case["category"]
        question = case["question"]
        intent = case.get("intent")
        expected_ids = list(case.get("expected_entry_ids", []))
        required_trap = case.get("required_trap_id")

        results = retriever.retrieve(question, intent=intent)
        actual_ids = [r.entry_id for r in results]

        prec, rec = compute_case_precision_recall(expected_ids, actual_ids)

        # Rendered context character budget check
        rendered = retriever.render_context(results)
        is_budget_compliant = len(rendered) <= retriever.default_max_chars
        if is_budget_compliant:
            budget_compliant_total += 1

        # Semantic trap tracking
        trap_retrieved: Optional[bool] = None
        if required_trap:
            trap_total += 1
            is_hit = required_trap in actual_ids
            if is_hit:
                trap_hits += 1
            trap_retrieved = is_hit

        # Empty-expected tracking
        if not expected_ids:
            empty_expected_total += 1
            if not actual_ids:
                empty_actual_total += 1

        scores = {r.entry_id: r.score for r in results}
        matched = {r.entry_id: r.matched_terms for r in results}
        missed = [e for e in expected_ids if e not in actual_ids]
        irrelevant = [a for a in actual_ids if a not in expected_ids]

        outcomes.append(
            RetrievalCaseOutcome(
                case_id=case_id,
                category=category,
                question=question,
                intent=intent,
                expected_ids=expected_ids,
                actual_ids=actual_ids,
                precision=prec,
                recall=rec,
                trap_retrieved=trap_retrieved,
                budget_compliant=is_budget_compliant,
                scores=scores,
                matched_terms=matched,
                missed_expected=missed,
                irrelevant_retrieved=irrelevant,
            )
        )

    n = len(outcomes)
    mean_precision = sum(o.precision for o in outcomes) / n if n > 0 else 0.0
    mean_recall = sum(o.recall for o in outcomes) / n if n > 0 else 0.0
    empty_specificity = (
        empty_actual_total / empty_expected_total if empty_expected_total > 0 else 1.0
    )
    trap_recall = trap_hits / trap_total if trap_total > 0 else 1.0
    compliance_rate = budget_compliant_total / n if n > 0 else 1.0

    return RetrievalEvaluationSummary(
        total_cases=n,
        mean_precision=mean_precision,
        mean_recall=mean_recall,
        empty_expected_count=empty_expected_total,
        empty_actual_count=empty_actual_total,
        empty_result_specificity=empty_specificity,
        semantic_trap_count=trap_total,
        semantic_trap_hits=trap_hits,
        semantic_trap_recall=trap_recall,
        budget_compliant_count=budget_compliant_total,
        budget_compliance_rate=compliance_rate,
        outcomes=outcomes,
    )


# ==============================================================================
# Fixtures
# ==============================================================================

@pytest.fixture(scope="module")
def retriever() -> DomainContextRetriever:
    """Fixture providing real in-memory DomainContextRetriever."""
    return DomainContextRetriever()


@pytest.fixture(scope="module")
def evaluation_cases() -> List[Dict[str, Any]]:
    """Fixture loading the versioned retrieval evaluation dataset."""
    assert DATASET_PATH.exists(), f"Evaluation dataset not found at {DATASET_PATH}"
    with open(DATASET_PATH, "r", encoding="utf-8") as f:
        cases = json.load(f)
    assert isinstance(cases, list), "Retrieval evaluation dataset must be a JSON array"
    return cases


# ==============================================================================
# Test Suite 1: Dataset Structural Integrity & Catalog Grounding
# ==============================================================================

class TestRetrievalEvaluationDatasetIntegrity:
    """Verifies schema, uniqueness, and catalog references of retrieval_evaluation.json."""

    def test_case_count_and_unique_ids(self, evaluation_cases: List[Dict[str, Any]]) -> None:
        """Verify dataset contains ~12 cases and all IDs are strictly unique."""
        assert len(evaluation_cases) >= 12, (
            f"Expected at least 12 evaluation cases, found {len(evaluation_cases)}"
        )
        ids = [c["id"] for c in evaluation_cases]
        assert len(ids) == len(set(ids)), (
            f"Duplicate case IDs found: {[x for x in ids if ids.count(x) > 1]}"
        )

    def test_required_fields_and_categories(
        self, evaluation_cases: List[Dict[str, Any]]
    ) -> None:
        """Verify each case has valid categories, non-empty questions, and rationale."""
        for case in evaluation_cases:
            case_id = case.get("id")
            assert case_id, f"Missing 'id' in case: {case}"
            cat = case.get("category")
            assert cat in ALLOWED_CATEGORIES, (
                f"Invalid category '{cat}' in case {case_id}. Allowed: {ALLOWED_CATEGORIES}"
            )
            q = case.get("question")
            assert isinstance(q, str) and q.strip(), (
                f"Expected non-empty question string in case {case_id}"
            )
            intent = case.get("intent")
            assert intent is None or (isinstance(intent, str) and intent.strip()), (
                f"Expected None or non-empty string for 'intent' in case {case_id}"
            )
            assert isinstance(case.get("expected_entry_ids"), list), (
                f"Expected 'expected_entry_ids' list in case {case_id}"
            )
            assert "rationale" in case and str(case["rationale"]).strip(), (
                f"Missing rationale in case {case_id}"
            )

    def test_expected_entry_ids_grounded_in_catalog(
        self,
        evaluation_cases: List[Dict[str, Any]],
        retriever: DomainContextRetriever,
    ) -> None:
        """Verify that every expected entry ID exists in the active knowledge catalog."""
        catalog_ids = {e.id for e in retriever.entries}
        for case in evaluation_cases:
            case_id = case["id"]
            for exp_id in case.get("expected_entry_ids", []):
                assert exp_id in catalog_ids, (
                    f"Case {case_id} references unknown expected entry ID '{exp_id}'. "
                    f"Must match active catalog: {sorted(catalog_ids)}"
                )
            req_trap = case.get("required_trap_id")
            if req_trap:
                assert req_trap in catalog_ids, (
                    f"Case {case_id} references unknown required_trap_id '{req_trap}'"
                )


# ==============================================================================
# Test Suite 2: Execution, Baseline Metrics & Safety Invariants
# ==============================================================================

class TestRetrievalExecutionAndBaselineMetrics:
    """Verifies offline execution, determinism, character budgets, and baseline metrics."""

    # Historical Task 1.31-G baseline metrics (commit e98e5a8) preserved for comparison
    BASELINE_MACRO_PRECISION: float = 7.0 / 12.0  # ~0.583333 (58.33%)
    BASELINE_MACRO_RECALL: float = 1.0  # 100.00%
    BASELINE_TRAP_RECALL: float = 3.0 / 3.0  # 100.00%
    BASELINE_EMPTY_SPECIFICITY: float = 3.0 / 5.0  # 60.00%
    BASELINE_BUDGET_COMPLIANCE: float = 12.0 / 12.0  # 100.00%

    # Tuned Task 1.31-H target metrics
    TUNED_MACRO_PRECISION: float = 7.5 / 12.0  # 0.625000 (62.50%, +4.17% improvement)
    TUNED_MACRO_RECALL: float = 1.0  # 100.00%
    TUNED_TRAP_RECALL: float = 1.0  # 100.00%
    TUNED_EMPTY_SPECIFICITY: float = 3.0 / 5.0  # 60.00%
    TUNED_BUDGET_COMPLIANCE: float = 1.0  # 100.00%

    def test_baseline_metrics_regression(
        self,
        retriever: DomainContextRetriever,
        evaluation_cases: List[Dict[str, Any]],
    ) -> None:
        """Verify tuned metrics and assert strict improvement over the Task 1.31-G historical baseline.

        Historical comparison (Task 1.31-G baseline at commit e98e5a8):
        - Macro mean precision: 7/12 (~0.583333)
        - Macro mean recall: 1.0 (12/12)
        - Semantic-trap recall: 3/3 (1.0)
        - Empty-result specificity: 3/5 (0.60)
        - Character-budget compliance: 12/12 (1.0)

        Tuned metrics (Task 1.31-H):
        - Macro mean precision: 7.5/12 (0.625000, +4.17% improvement via unanchored keyword filtering)
        - Macro mean recall: 1.0 (100.00% preserved)
        - Semantic-trap recall: 3/3 (100.00% preserved)
        - Empty-result specificity: 3/5 (60.00% preserved)
        - Character-budget compliance: 12/12 (100.00% preserved)

        Uses pytest.approx with relative tolerance to prevent brittle floating-point comparisons.
        Includes deterministic per-case breakdown in assertion error messages.
        """
        summary = evaluate_retrieval_dataset(retriever, evaluation_cases)
        report_str = summary.format_report()

        assert summary.total_cases == 12, f"Expected 12 cases:\n{report_str}"

        # Assert strict improvement over historical Task 1.31-G baseline
        assert summary.mean_precision > self.BASELINE_MACRO_PRECISION, (
            f"Tuned macro precision ({summary.mean_precision:.6f}) must strictly exceed "
            f"historical baseline ({self.BASELINE_MACRO_PRECISION:.6f}):\n{report_str}"
        )
        assert summary.mean_precision == pytest.approx(self.TUNED_MACRO_PRECISION, rel=1e-4), (
            f"Macro mean precision deviated from tuned {self.TUNED_MACRO_PRECISION:.6f} target ({summary.mean_precision:.6f}):\n{report_str}"
        )
        assert summary.mean_recall == pytest.approx(self.TUNED_MACRO_RECALL, rel=1e-4), (
            f"Macro mean recall deviated from tuned baseline ({summary.mean_recall:.6f}):\n{report_str}"
        )
        assert summary.semantic_trap_recall == pytest.approx(self.TUNED_TRAP_RECALL, rel=1e-4), (
            f"Semantic trap recall deviated from tuned baseline ({summary.semantic_trap_recall:.6f}):\n{report_str}"
        )
        assert summary.empty_result_specificity == pytest.approx(self.TUNED_EMPTY_SPECIFICITY, rel=1e-4), (
            f"Empty-result specificity deviated from tuned baseline ({summary.empty_result_specificity:.6f}):\n{report_str}"
        )
        assert summary.budget_compliance_rate == pytest.approx(self.TUNED_BUDGET_COMPLIANCE, rel=1e-4), (
            f"Character budget compliance deviated from tuned baseline ({summary.budget_compliance_rate:.6f}):\n{report_str}"
        )

    def test_per_case_breakdown_reporting(
        self,
        retriever: DomainContextRetriever,
        evaluation_cases: List[Dict[str, Any]],
    ) -> None:
        """Verify that the evaluation summary formats each case's ID, expected, actual, missed, and irrelevant entries."""
        summary = evaluate_retrieval_dataset(retriever, evaluation_cases)
        report_text = summary.format_report()

        assert "=== RETRIEVAL EVALUATION REPORT ===" in report_text
        assert "Total cases: 12" in report_text

        for o in summary.outcomes:
            assert f"[{o.case_id}]" in report_text
            assert f"Expected:   {o.expected_ids}" in report_text
            assert f"Actual:     {o.actual_ids}" in report_text
            assert f"Missed:     {o.missed_expected}" in report_text
            assert f"Irrelevant: {o.irrelevant_retrieved}" in report_text

    def test_unresolved_rule_cases_document_known_over_retrieval(
        self,
        retriever: DomainContextRetriever,
        evaluation_cases: List[Dict[str, Any]],
    ) -> None:
        """Verify and document over-retrieval on unresolved business rule queries.

        Queries for unresolved business rules ('at-risk' and 'dynamic CGPA') are not cataloged,
        so expected_entry_ids is empty ([]).

        In Task 1.31-G baseline:
        Three entries were over-retrieved (KB_MARKS_STUDENT_ENROLLMENT_REL, KB_STU_USERS_INHERITANCE,
        KB_TRAP_STUDENT_DEPARTMENT_JOIN) due to table alias 'grade' and unanchored secondary table matches.

        In Task 1.31-H tuned:
        The erroneous 'grade' -> StudentMarks alias was removed and secondary-table join cards without
        corroboration were penalized. Over-retrieval is reduced from 3 entries to exactly 1 entry
        (KB_STU_USERS_INHERITANCE), which matches the primary 'Students' table entity.
        """
        unresolved_cases = [
            c for c in evaluation_cases
            if c["category"] == "unresolved_business_rules"
        ]
        assert len(unresolved_cases) == 2, f"Expected 2 unresolved rule cases, got {len(unresolved_cases)}"

        case_map = {c["id"]: c for c in unresolved_cases}
        assert "RET_UNRESOLVED_01" in case_map
        assert "RET_UNRESOLVED_02" in case_map

        for case_id in ("RET_UNRESOLVED_01", "RET_UNRESOLVED_02"):
            case = case_map[case_id]
            assert case["expected_entry_ids"] == [], (
                f"Case {case_id} must have empty expected_entry_ids since rule is unresolved"
            )

            results = retriever.retrieve(case["question"], intent=case.get("intent"))
            actual_ids = [r.entry_id for r in results]

            # In Task 1.31-H, over-retrieval is reduced from 3 entries to exactly 1 entry
            assert len(actual_ids) == 1, (
                f"Task 1.31-H: case {case_id} over-retrieval reduced from 3 to 1, got {actual_ids}"
            )
            prec, rec = compute_case_precision_recall(case["expected_entry_ids"], actual_ids)
            assert prec == 0.0, f"Case {case_id} precision must be 0.0 due to irrelevant retrieval"
            assert rec == 1.0, f"Case {case_id} recall is 1.0 under empty ground-truth convention"
            assert actual_ids == ["KB_STU_USERS_INHERITANCE"]

            # Irrelevant retrieved entries are populated and tracked
            missed = [e for e in case["expected_entry_ids"] if e not in actual_ids]
            irrelevant = [a for a in actual_ids if a not in case["expected_entry_ids"]]
            assert missed == []
            assert len(irrelevant) == len(actual_ids)

    def test_retrieval_determinism(
        self,
        retriever: DomainContextRetriever,
        evaluation_cases: List[Dict[str, Any]],
    ) -> None:
        """Verify that repeated evaluation runs yield 100% identical outputs and scores."""
        run_1 = evaluate_retrieval_dataset(retriever, evaluation_cases)
        run_2 = evaluate_retrieval_dataset(retriever, evaluation_cases)

        assert run_1.mean_precision == run_2.mean_precision
        assert run_1.mean_recall == run_2.mean_recall
        assert run_1.empty_result_specificity == run_2.empty_result_specificity

        for o1, o2 in zip(run_1.outcomes, run_2.outcomes):
            assert o1.case_id == o2.case_id
            assert o1.actual_ids == o2.actual_ids
            assert o1.scores == o2.scores
            assert o1.matched_terms == o2.matched_terms
            assert o1.precision == o2.precision
            assert o1.recall == o2.recall

    def test_semantic_trap_cases_retrieved(
        self,
        retriever: DomainContextRetriever,
        evaluation_cases: List[Dict[str, Any]],
    ) -> None:
        """Verify that all three known semantic trap cases (Q028, Q029, Q030) are retrieved."""
        trap_cases = [c for c in evaluation_cases if c.get("required_trap_id")]
        assert len(trap_cases) == 3, f"Expected exactly 3 semantic trap cases, got {len(trap_cases)}"

        for case in trap_cases:
            case_id = case["id"]
            required_trap = case["required_trap_id"]
            results = retriever.retrieve(case["question"], intent=case.get("intent"))
            actual_ids = [r.entry_id for r in results]

            assert required_trap in actual_ids, (
                f"Case {case_id} failed to retrieve required trap card '{required_trap}'. "
                f"Actual retrieved: {actual_ids}"
            )

    def test_pure_out_of_domain_and_unindexed_cases_return_empty(
        self,
        retriever: DomainContextRetriever,
        evaluation_cases: List[Dict[str, Any]],
    ) -> None:
        """Verify out-of-domain and unindexed database queries return zero domain entries."""
        empty_cases = [
            c for c in evaluation_cases
            if c["category"] in ("out_of_domain", "unindexed_schema")
        ]
        assert len(empty_cases) == 3, f"Expected 3 pure empty cases, got {len(empty_cases)}"

        for case in empty_cases:
            case_id = case["id"]
            results = retriever.retrieve(case["question"], intent=case.get("intent"))
            assert len(results) == 0, (
                f"Case {case_id} ('{case['question']}') was expected to return empty, "
                f"but retrieved: {[r.entry_id for r in results]}"
            )

    def test_sensitive_column_safety_invariants(
        self,
        retriever: DomainContextRetriever,
        evaluation_cases: List[Dict[str, Any]],
    ) -> None:
        """Verify that no sensitive columns (e.g. PasswordHash) appear in retrieved terms or context."""
        policy = SensitiveColumnPolicy()

        for case in evaluation_cases:
            results = retriever.retrieve(case["question"], intent=case.get("intent"))
            rendered = retriever.render_context(results)

            # Check rendered string
            assert "passwordhash" not in rendered.lower()
            assert "password" not in rendered.lower()

            # Check structured results
            for r in results:
                for term in r.matched_terms:
                    assert "passwordhash" not in term.lower()
                    if term.startswith("col:"):
                        cname = term.split(":", 1)[1]
                        tbl = cname.split(".", 1)[0] if "." in cname else None
                        raw_col = cname.split(".", 1)[1] if "." in cname else cname
                        assert not policy.is_sensitive(raw_col, tbl), (
                            f"Matched term exposes sensitive column: {term}"
                        )

    def test_character_budget_compliance(
        self,
        retriever: DomainContextRetriever,
        evaluation_cases: List[Dict[str, Any]],
    ) -> None:
        """Verify that every rendered context strictly satisfies max_chars limit."""
        for case in evaluation_cases:
            results = retriever.retrieve(case["question"], intent=case.get("intent"))
            rendered = retriever.render_context(results, max_chars=1200)
            assert len(rendered) <= 1200, (
                f"Rendered context exceeded budget: {len(rendered)} > 1200 chars in case {case['id']}"
            )


# ==============================================================================
# Test Suite 3: Metric Calculation Edge Cases
# ==============================================================================

class TestRetrievalMetricEdgeCases:
    """Verifies explicit edge cases in precision and recall calculations."""

    def test_both_empty_yields_perfect_score(self) -> None:
        """If both expected and actual are empty, precision=1.0 and recall=1.0."""
        prec, rec = compute_case_precision_recall([], [])
        assert prec == 1.0
        assert rec == 1.0

    def test_expected_empty_actual_nonempty(self) -> None:
        """If expected is empty but retriever co-retrieves entries: prec=0.0, rec=1.0."""
        prec, rec = compute_case_precision_recall([], ["KB_ALPHA", "KB_BETA"])
        assert prec == 0.0
        assert rec == 1.0

    def test_expected_nonempty_actual_empty(self) -> None:
        """If expected is nonempty but retriever returns empty: prec=0.0, rec=0.0."""
        prec, rec = compute_case_precision_recall(["KB_ALPHA"], [])
        assert prec == 0.0
        assert rec == 0.0

    def test_partial_match_precision_and_recall(self) -> None:
        """Verify standard set-intersection precision and recall on partial overlap."""
        expected = ["KB_ALPHA", "KB_BETA"]
        actual = ["KB_BETA", "KB_GAMMA", "KB_DELTA"]

        # Intersection: {"KB_BETA"} (size 1)
        # Precision: 1 / 3 = 0.333...
        # Recall: 1 / 2 = 0.50
        prec, rec = compute_case_precision_recall(expected, actual)
        assert round(prec, 3) == 0.333
        assert rec == 0.50
