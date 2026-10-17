"""Conservative Dynamic Semantic Result Comparator for QueryPilot.

Provides automated, principled evaluation of database query results against
ground-truth reference results without relying on case IDs, hardcoded dictionaries,
or ungrounded row-count heuristics.

Outcomes:
- EXACT_MATCH: Fast-path exact equality of column names, ordering, and values.
- EQUIVALENT_MATCH: Verified semantic equivalence (compatible aliases, harmless
  column reordering, normalized numeric equivalence, valid projection supersets,
  or unconstrained multiset equality).
- MISMATCH: Definitive disagreement in row counts, column values, entity semantics,
  or order violations where ordering was required.
- INCONCLUSIVE: Insufficient result data, ambiguous column bindings, or
  unspecified row ordering where multiset matches but order constraint is unknown.
"""

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from pydantic import BaseModel, Field

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

# Standard abbreviations and stem synonyms for SQL column aliases
STEM_SYNONYMS: Dict[str, str] = {
    "avg": "average",
    "min": "minimum",
    "lowest": "minimum",
    "max": "maximum",
    "highest": "maximum",
    "cnt": "count",
    "total": "count",
    "num": "number",
    "no": "number",
    "dept": "department",
    "coord": "coordinator",
    "fullname": "name",
    "fname": "name",
    "lname": "name",
}


class SemanticOutcome(str, Enum):
    """Semantic outcome classification for result comparison."""

    EXACT_MATCH = "exact_match"
    EQUIVALENT_MATCH = "equivalent_match"
    MISMATCH = "mismatch"
    INCONCLUSIVE = "inconclusive"


class SemanticComparisonResult(BaseModel):
    """Structured result of semantic evaluation between generated and reference rows."""

    model_config = {"arbitrary_types_allowed": True}

    outcome: SemanticOutcome
    is_correct: bool
    reason: str
    column_mapping: Optional[Dict[str, str]] = None
    details: Dict[str, Any] = Field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert comparison result to dictionary."""
        return {
            "outcome": self.outcome.value,
            "is_correct": self.is_correct,
            "reason": self.reason,
            "column_mapping": self.column_mapping,
            "details": self.details,
        }


def split_identifier_words(name: str) -> Set[str]:
    """Extract individual normalized word stems from a camelCase, PascalCase, or snake_case identifier."""
    words = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z][a-z]|\b)|\d+", str(name))
    result: Set[str] = set()
    for w in words:
        w_low = w.lower()
        normalized = STEM_SYNONYMS.get(w_low, w_low)
        result.add(normalized)
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


def normalize_numeric(val: Any) -> Optional[Decimal]:
    """Convert an int, float, or Decimal to a normalized Decimal representation without precision loss."""
    if isinstance(val, bool):
        return None
    if isinstance(val, (int, float, Decimal)):
        try:
            return Decimal(str(val))
        except (InvalidOperation, ValueError):
            return None
    return None


def parse_date_or_datetime(val: Any) -> Optional[datetime]:
    """Parse a datetime, date, or ISO string into a canonical datetime object."""
    if isinstance(val, datetime):
        return val
    if isinstance(val, date):
        return datetime(val.year, val.month, val.day)
    if isinstance(val, str):
        cleaned = val.strip().replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(cleaned)
        except ValueError:
            pass
    return None


def values_are_equal(v_gen: Any, v_ref: Any) -> bool:
    """Deterministically check if two cell values are semantically equal.

    Handles:
    - Explicit NULL-to-NULL equivalence (None == None, None != 0, None != "").
    - Boolean distinction (True != 1, False != 0).
    - Normalized numeric equality (40 == 40.0 == Decimal("40.00")), rejecting meaningful differences.
    - Date and datetime parsing for ISO strings.
    - String whitespace trimming and equality.
    """
    # 1. NULL handling
    if v_gen is None and v_ref is None:
        return True
    if v_gen is None or v_ref is None:
        return False

    # 2. Boolean handling (prevent bool == int coercion in Python)
    if isinstance(v_gen, bool) or isinstance(v_ref, bool):
        return isinstance(v_gen, bool) and isinstance(v_ref, bool) and (v_gen == v_ref)

    # 3. Numeric handling
    d_gen = normalize_numeric(v_gen)
    d_ref = normalize_numeric(v_ref)
    if d_gen is not None and d_ref is not None:
        # Exact decimal normalization without broad tolerance
        return abs(d_gen - d_ref) <= Decimal("1e-9")

    # 4. Date / Datetime handling
    dt_gen = parse_date_or_datetime(v_gen)
    dt_ref = parse_date_or_datetime(v_ref)
    if dt_gen is not None and dt_ref is not None:
        return dt_gen == dt_ref

    # 5. String handling
    if isinstance(v_gen, str) and isinstance(v_ref, str):
        return v_gen.strip() == v_ref.strip()

    # 6. Direct equality fallback
    return bool(v_gen == v_ref)


def is_order_constrained(
    reference_sql: Optional[str] = None,
    question: Optional[str] = None,
) -> Optional[bool]:
    """Infer whether row ordering is semantically constrained.

    A query is semantically order-constrained if:
    1. The question explicitly requests ordering (e.g. 'ordered by', 'order by',
       'sorted by', 'sort by', 'top', 'first', 'highest', 'lowest', 'ranked').
    2. The query uses TOP / LIMIT with ORDER BY (where ordering defines which records
       are selected).

    If the question does not request ordering and does not use TOP / LIMIT,
    unconstrained grouping/aggregation results are evaluated as order-insensitive multisets.

    Returns:
    - True: If explicit ordering is semantically required.
    - False: If order is not semantically constrained.
    - None: If insufficient context is available to determine constraint.
    """
    if question:
        q_low = question.lower()
        order_keywords = (
            "ordered by",
            "order by",
            "sorted by",
            "sort by",
            "top ",
            "first ",
            "highest",
            "lowest",
            "ranked",
            "rank ",
        )
        if any(kw in q_low for kw in order_keywords):
            return True

    if reference_sql:
        sql_clean = reference_sql.strip().lower()
        # If TOP / LIMIT is used with ORDER BY, ordering is semantically binding
        if ("top " in sql_clean or "limit " in sql_clean) and "order by" in sql_clean:
            return True
        # If question is not provided, and reference SQL has ORDER BY
        if not question and "order by" in sql_clean:
            return True
        return False

    return None


def row_matches_reference(
    gen_row: Dict[str, Any],
    ref_row: Dict[str, Any],
    mapping: Dict[str, str],
) -> bool:
    """Check if a generated row matches a reference row under a specific column mapping."""
    for ref_col, gen_col in mapping.items():
        v_gen = gen_row.get(gen_col)
        v_ref = ref_row.get(ref_col)
        if not values_are_equal(v_gen, v_ref):
            return False
    return True


def compare_multiset_rows(
    gen_rows: List[Dict[str, Any]],
    ref_rows: List[Dict[str, Any]],
    mapping: Dict[str, str],
) -> bool:
    """Check order-insensitive multiset equality preserving duplicate-row multiplicity."""
    if len(gen_rows) != len(ref_rows):
        return False

    matched_gen_indices: Set[int] = set()

    for r_idx, ref_row in enumerate(ref_rows):
        found = False
        for g_idx, gen_row in enumerate(gen_rows):
            if g_idx in matched_gen_indices:
                continue
            if row_matches_reference(gen_row, ref_row, mapping):
                matched_gen_indices.add(g_idx)
                found = True
                break
        if not found:
            return False

    return len(matched_gen_indices) == len(ref_rows)


def find_column_mappings(
    gen_cols: List[str],
    ref_cols: List[str],
    gen_rows: List[Dict[str, Any]],
    ref_rows: List[Dict[str, Any]],
) -> Tuple[str, Optional[Dict[str, str]]]:
    """Find unambiguous column mapping from reference columns to generated columns.

    Returns:
    - ("unambiguous", mapping): A valid, unique reference_col -> generated_col mapping.
    - ("missing_columns", None): Generated result is missing one or more required reference columns.
    - ("ambiguous", None): Multiple candidate columns qualify with differing semantics.
    - ("no_value_match", None): Compatible aliases found but values do not match.
    """
    if len(gen_cols) < len(ref_cols):
        return "missing_columns", None

    # Step 1: Find candidate generated columns for each reference column
    candidates_per_ref: Dict[str, List[str]] = {}
    for r_col in ref_cols:
        matching_gen_cols = [
            g_col for g_col in gen_cols if are_column_aliases_compatible(g_col, r_col)
        ]
        candidates_per_ref[r_col] = matching_gen_cols

    # Check for unmapped reference columns
    unmapped = [r for r, cands in candidates_per_ref.items() if not cands]
    if unmapped:
        return "missing_columns", None

    # Step 2: Try exact column name identity mapping first (fast unambiguous path)
    exact_identity_mapping = {}
    gen_col_lower_map = {g.strip().lower(): g for g in gen_cols}
    all_exact = True
    for r_col in ref_cols:
        r_low = r_col.strip().lower()
        if r_low in gen_col_lower_map:
            exact_identity_mapping[r_col] = gen_col_lower_map[r_low]
        else:
            all_exact = False
            break

    if all_exact:
        return "unambiguous", exact_identity_mapping

    # Step 3: Check for candidate assignments
    import itertools

    valid_mappings: List[Dict[str, str]] = []
    # Cartesian product of candidate choices
    candidate_options = [candidates_per_ref[r] for r in ref_cols]
    for combo in itertools.product(*candidate_options):
        # A mapping must assign distinct generated columns to distinct reference columns
        if len(set(combo)) != len(ref_cols):
            continue
        mapping = dict(zip(ref_cols, combo))
        valid_mappings.append(mapping)

    if not valid_mappings:
        return "missing_columns", None

    if len(valid_mappings) == 1:
        return "unambiguous", valid_mappings[0]

    # Multiple candidate mappings exist: evaluate which ones actually match row values
    value_matching_mappings: List[Dict[str, str]] = []
    for mapping in valid_mappings:
        if compare_multiset_rows(gen_rows, ref_rows, mapping):
            value_matching_mappings.append(mapping)

    if len(value_matching_mappings) == 1:
        return "unambiguous", value_matching_mappings[0]
    elif len(value_matching_mappings) > 1:
        # Multiple distinct mappings satisfy value matching
        return "ambiguous", None
    else:
        return "no_value_match", None


def compare_results_semantically(
    generated_rows: Optional[List[Dict[str, Any]]],
    reference_rows: Optional[List[Dict[str, Any]]],
    *,
    order_sensitive: Optional[bool] = None,
    reference_sql: Optional[str] = None,
    question: Optional[str] = None,
    allow_superset: bool = True,
) -> SemanticComparisonResult:
    """Evaluate whether generated query rows match ground-truth reference rows semantically.

    This function never uses case IDs, hardcoded dictionaries, or external calls.

    Outcomes:
    - EXACT_MATCH: Fast-path exact row equality.
    - EQUIVALENT_MATCH: Valid column mapping, value equality, and proper order/multiset match.
    - MISMATCH: Row count difference, missing columns, value disagreement, or order violation.
    - INCONCLUSIVE: Missing inputs, ambiguous column mapping, or unspecified order constraint.

    Args:
        generated_rows: Rows returned by the candidate query.
        reference_rows: Authoritative reference rows from the database.
        order_sensitive: Explicit boolean override for ordering constraint.
        reference_sql: Optional reference SQL query string to infer ordering.
        question: Optional natural-language question string to infer ordering.
        allow_superset: Whether additional generated projection columns are permitted.

    Returns:
        Structured SemanticComparisonResult.
    """
    # Boundary 1: Missing or invalid result data
    if generated_rows is None or reference_rows is None:
        return SemanticComparisonResult(
            outcome=SemanticOutcome.INCONCLUSIVE,
            is_correct=False,
            reason="Insufficient result data: generated_rows or reference_rows is None.",
            details={"has_generated": generated_rows is not None, "has_reference": reference_rows is not None},
        )

    if not isinstance(generated_rows, list) or not isinstance(reference_rows, list):
        return SemanticComparisonResult(
            outcome=SemanticOutcome.INCONCLUSIVE,
            is_correct=False,
            reason="Invalid result format: rows must be provided as lists of dictionaries.",
        )

    # Boundary 2: Empty result set handling
    if len(generated_rows) == 0 and len(reference_rows) == 0:
        return SemanticComparisonResult(
            outcome=SemanticOutcome.EXACT_MATCH,
            is_correct=True,
            reason="Both generated and reference result sets are empty (0 rows).",
            details={"row_count": 0},
        )

    # Boundary 3: Row count difference rejection
    if len(generated_rows) != len(reference_rows):
        return SemanticComparisonResult(
            outcome=SemanticOutcome.MISMATCH,
            is_correct=False,
            reason=f"Row count mismatch: generated has {len(generated_rows)} rows, reference has {len(reference_rows)} rows.",
            details={"generated_count": len(generated_rows), "reference_count": len(reference_rows)},
        )

    # Boundary 4: Fast-path exact row and column-order equality
    if generated_rows == reference_rows and list(generated_rows[0].keys()) == list(reference_rows[0].keys()):
        return SemanticComparisonResult(
            outcome=SemanticOutcome.EXACT_MATCH,
            is_correct=True,
            reason="Exact row and column equality.",
            column_mapping={k: k for k in reference_rows[0].keys()},
            details={"row_count": len(generated_rows)},
        )

    # Extract column lists
    ref_cols = list(reference_rows[0].keys())
    gen_cols = list(generated_rows[0].keys())

    # Check projection superset restriction
    if not allow_superset and len(gen_cols) > len(ref_cols):
        return SemanticComparisonResult(
            outcome=SemanticOutcome.MISMATCH,
            is_correct=False,
            reason=f"Projection superset disallowed: generated has {len(gen_cols)} columns, reference has {len(ref_cols)}.",
            details={"generated_columns": gen_cols, "reference_columns": ref_cols},
        )

    # Boundary 5: Column Alignment and Mapping
    status, mapping = find_column_mappings(gen_cols, ref_cols, generated_rows, reference_rows)

    if status == "missing_columns":
        return SemanticComparisonResult(
            outcome=SemanticOutcome.MISMATCH,
            is_correct=False,
            reason=f"Missing required reference columns in generated result. Reference requires: {ref_cols}, Generated has: {gen_cols}.",
            details={"reference_columns": ref_cols, "generated_columns": gen_cols},
        )

    if status == "ambiguous":
        return SemanticComparisonResult(
            outcome=SemanticOutcome.INCONCLUSIVE,
            is_correct=False,
            reason=f"Ambiguous column mapping: multiple candidate column alignments exist between {ref_cols} and {gen_cols}.",
            details={"reference_columns": ref_cols, "generated_columns": gen_cols},
        )

    if status != "unambiguous" or mapping is None:
        return SemanticComparisonResult(
            outcome=SemanticOutcome.MISMATCH,
            is_correct=False,
            reason="No valid column mapping with matching values could be established.",
            details={"reference_columns": ref_cols, "generated_columns": gen_cols},
        )

    # Boundary 6: Determine row ordering requirement
    is_ordered = order_sensitive
    if is_ordered is None:
        is_ordered = is_order_constrained(reference_sql=reference_sql, question=question)

    # Check natural order row-by-row
    natural_order_matches = True
    for i in range(len(reference_rows)):
        if not row_matches_reference(generated_rows[i], reference_rows[i], mapping):
            natural_order_matches = False
            break

    is_superset = len(gen_cols) > len(ref_cols)
    is_exact_col_order = (
        not is_superset
        and len(gen_cols) == len(ref_cols)
        and [g.strip().lower() for g in gen_cols] == [r.strip().lower() for r in ref_cols]
    )

    if natural_order_matches:
        if is_exact_col_order:
            return SemanticComparisonResult(
                outcome=SemanticOutcome.EXACT_MATCH,
                is_correct=True,
                reason="Natural order and values match reference result.",
                column_mapping=mapping,
                details={"row_count": len(generated_rows)},
            )
        else:
            if is_superset:
                reason = "Projection superset with all required reference columns and values matching."
            elif [g.strip().lower() for g in gen_cols] != [r.strip().lower() for r in ref_cols] and len(gen_cols) == len(ref_cols) and set(g.strip().lower() for g in gen_cols) == set(r.strip().lower() for r in ref_cols):
                reason = "Harmless column reordering with identical ordered values."
            else:
                reason = "Semantically equivalent column aliases with identical ordered values."

            return SemanticComparisonResult(
                outcome=SemanticOutcome.EQUIVALENT_MATCH,
                is_correct=True,
                reason=reason,
                column_mapping=mapping,
                details={
                    "row_count": len(generated_rows),
                    "is_superset": is_superset,
                    "mapping": mapping,
                },
            )

    # Natural order failed: check multiset (bag) equality
    multiset_matches = compare_multiset_rows(generated_rows, reference_rows, mapping)

    if not multiset_matches:
        return SemanticComparisonResult(
            outcome=SemanticOutcome.MISMATCH,
            is_correct=False,
            reason="Row values do not match reference result under established column mapping.",
            column_mapping=mapping,
            details={"reference_columns": ref_cols, "generated_columns": gen_cols},
        )

    # Multiset matched, but rows are out of order
    if is_ordered is True:
        return SemanticComparisonResult(
            outcome=SemanticOutcome.MISMATCH,
            is_correct=False,
            reason="Row ordering violation: query requires specific row ordering, but rows appeared in different order.",
            column_mapping=mapping,
            details={"order_constrained": True},
        )
    elif is_ordered is False:
        return SemanticComparisonResult(
            outcome=SemanticOutcome.EQUIVALENT_MATCH,
            is_correct=True,
            reason="Order-insensitive multiset match on unconstrained query.",
            column_mapping=mapping,
            details={"order_constrained": False, "multiset_matched": True},
        )
    else:
        # is_ordered is None: insufficient context to decide ordering constraint
        return SemanticComparisonResult(
            outcome=SemanticOutcome.INCONCLUSIVE,
            is_correct=False,
            reason="Ambiguous ordering constraint: rows match as unordered multiset, but ordering requirement is unspecified.",
            column_mapping=mapping,
            details={"order_constrained": None, "multiset_matched": True},
        )
