"""Deterministic in-memory knowledge retriever for QueryPilot (Task 1.31-C).

Selects relevant schema and domain knowledge entries from the version-controlled
knowledge catalog without external network services, embeddings, or vector databases.
Preserves deterministic ranking, bounded token/character budgets, and sensitive-column boundaries.
"""

from dataclasses import dataclass
import json
import logging
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from query_pilot.db.introspection import SensitiveColumnPolicy

logger = logging.getLogger(__name__)

# Canonical relative path to the catalog from the repository root
DEFAULT_CATALOG_PATH: Path = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "data"
    / "knowledge"
    / "catalog.json"
)

# English stop words filtered out during query tokenization
STOP_WORDS: Set[str] = {
    "a", "an", "the", "and", "or", "in", "on", "at", "to", "for", "of", "with",
    "by", "from", "as", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "can", "could", "should", "would",
    "all", "any", "some", "each", "every", "what", "which", "who", "whom", "whose",
    "where", "when", "why", "how", "list", "show", "find", "get", "retrieve",
    "give", "tell", "count", "their", "its", "first", "last", "top", "per",
    "please", "me", "into", "than", "then",
}

# Mapping of common singular/colloquial terms to canonical GradeSense table names
TABLE_ALIAS_MAP: Dict[str, str] = {
    "user": "Users",
    "users": "Users",
    "student": "Students",
    "students": "Students",
    "faculty": "Faculties",
    "faculties": "Faculties",
    "professor": "Faculties",
    "professors": "Faculties",
    "instructor": "Faculties",
    "department": "Departments",
    "departments": "Departments",
    "batch": "Batches",
    "batches": "Batches",
    "subject": "Subjects",
    "subjects": "Subjects",
    "subjectunit": "SubjectUnits",
    "subjectunits": "SubjectUnits",
    "courseoffering": "CourseOfferings",
    "courseofferings": "CourseOfferings",
    "offering": "CourseOfferings",
    "offerings": "CourseOfferings",
    "courseenrollment": "CourseEnrollments",
    "courseenrollments": "CourseEnrollments",
    "enrollment": "CourseEnrollments",
    "enrollments": "CourseEnrollments",
    "evaluationscheme": "EvaluationSchemes",
    "evaluationschemes": "EvaluationSchemes",
    "assessmentitem": "AssessmentItems",
    "assessmentitems": "AssessmentItems",
    "assessment": "AssessmentItems",
    "assessments": "AssessmentItems",
    "studentmark": "StudentMarks",
    "studentmarks": "StudentMarks",
    "mark": "StudentMarks",
    "marks": "StudentMarks",
    "facultyassignment": "FacultyAssignments",
    "facultyassignments": "FacultyAssignments",
    "attendancerecord": "AttendanceRecords",
    "attendancerecords": "AttendanceRecords",
    "attendance": "AttendanceRecords",
    "uploadhistory": "UploadHistory",
    "auditlog": "AuditLog",
    "prediction": "Predictions",
    "predictions": "Predictions",
}

# Multi-word table phrase aliases requiring word-boundary phrase matching
TABLE_PHRASE_ALIAS_MAP: Dict[str, str] = {
    "course offering": "CourseOfferings",
    "course offerings": "CourseOfferings",
    "course enrollment": "CourseEnrollments",
    "course enrollments": "CourseEnrollments",
    "student mark": "StudentMarks",
    "student marks": "StudentMarks",
    "assessment item": "AssessmentItems",
    "assessment items": "AssessmentItems",
    "evaluation scheme": "EvaluationSchemes",
    "evaluation schemes": "EvaluationSchemes",
    "faculty assignment": "FacultyAssignments",
    "faculty assignments": "FacultyAssignments",
    "attendance record": "AttendanceRecords",
    "attendance records": "AttendanceRecords",
    "upload history": "UploadHistory",
    "audit log": "AuditLog",
}

# Scoring weight constants
SCORE_COLUMN_EXACT: float = 5.0
SCORE_TABLE_EXACT: float = 3.0
SCORE_KEYWORD_PHRASE: float = 3.5
SCORE_KEYWORD_EXACT: float = 2.0
SCORE_TITLE_TOKEN: float = 1.0
SCORE_CONTENT_TOKEN: float = 0.5

# Hard ceiling for top_k results
HARD_MAX_TOP_K: int = 3

# Default boundaries
DEFAULT_MIN_SCORE: float = 2.0
DEFAULT_TOP_K: int = 3
DEFAULT_MAX_CHARS: int = 1200


@dataclass(frozen=True)
class KnowledgeEntry:
    """Typed domain knowledge entry loaded from the catalog."""

    id: str
    category: str
    title: str
    keywords: List[str]
    tables_referenced: List[str]
    columns_referenced: List[str]
    content: str
    source: str


@dataclass(frozen=True)
class RetrievalResult:
    """Structured result returned by the retriever for a relevant knowledge entry."""

    entry_id: str
    category: str
    title: str
    content: str
    score: float
    matched_terms: List[str]

    def to_dict(self) -> Dict[str, Any]:
        """Convert result to dictionary representation."""
        return {
            "entry_id": self.entry_id,
            "category": self.category,
            "title": self.title,
            "content": self.content,
            "score": round(self.score, 2),
            "matched_terms": list(self.matched_terms),
        }


def normalize_query_text(text: str) -> str:
    """Normalize query string for deterministic matching."""
    return re.sub(r"\s+", " ", text.strip().lower())


def tokenize_query(text: str) -> List[str]:
    """Extract informative lowercase tokens, filtering punctuation and stop words."""
    cleaned = re.sub(r"[^\w\.]", " ", text.lower())
    raw_tokens = [t.strip().strip(".") for t in cleaned.split() if t.strip()]
    return [t for t in raw_tokens if len(t) > 1 and t not in STOP_WORDS]


def _matches_column_reference(col_ref: str, normalized_query: str) -> bool:
    """Check if qualified column reference matches query with strict identifier boundaries.

    Ensures that a column reference such as Students.DepartmentId matches the intended
    identifier rather than an unrelated longer identifier containing the same substring
    (e.g. Students.DepartmentIdOther or NonStudents.DepartmentId).
    """
    col_ref_lower = col_ref.lower().strip()
    if "." in col_ref_lower:
        tbl_part, col_part = col_ref_lower.split(".", 1)
        # Match standard 'Students.DepartmentId' or SQL bracketed '[Students].[DepartmentId]'
        # with strict word/bracket boundaries around table and column parts
        pattern = (
            rf"(?:\b|\[){re.escape(tbl_part)}(?:\b|\])"
            rf"\s*\.\s*"
            rf"(?:\b|\[){re.escape(col_part)}(?:\b|\])"
        )
    else:
        pattern = rf"\b{re.escape(col_ref_lower)}\b"
    return bool(re.search(pattern, normalized_query))


class DomainContextRetriever:
    """Deterministic in-memory retriever for QueryPilot domain knowledge cards.

    Loads the validated catalog at initialization and matches natural-language questions
    against schema tables, columns, curated keywords, and domain concepts.
    """

    def __init__(
        self,
        catalog_path: Optional[Union[str, Path]] = None,
        *,
        default_top_k: int = DEFAULT_TOP_K,
        default_min_score: float = DEFAULT_MIN_SCORE,
        default_max_chars: int = DEFAULT_MAX_CHARS,
    ) -> None:
        """Initialize retriever with catalog file and bounds.

        Args:
            catalog_path: Optional path to catalog.json (defaults to data/knowledge/catalog.json).
            default_top_k: Maximum number of entries returned per query (default: 3, hard max: 3).
            default_min_score: Minimum relevance score required for inclusion (default: 2.0).
            default_max_chars: Maximum total character length of rendered context (default: 1200).

        Raises:
            FileNotFoundError: If catalog file does not exist.
            ValueError: If catalog path is invalid, references unresolved business rules,
                configuration values are invalid, or catalog entries are malformed.
        """
        if not isinstance(default_top_k, int) or default_top_k <= 0:
            raise ValueError(f"default_top_k must be a positive integer, got {default_top_k!r}")
        if not isinstance(default_min_score, (int, float)) or default_min_score < 0:
            raise ValueError(f"default_min_score must be a non-negative number, got {default_min_score!r}")
        if not isinstance(default_max_chars, int) or default_max_chars <= 0:
            raise ValueError(f"default_max_chars must be a positive integer, got {default_max_chars!r}")

        resolved_path = Path(catalog_path or DEFAULT_CATALOG_PATH).resolve()

        # Guard: Unresolved business rules markdown cannot be loaded as active knowledge
        path_str = str(resolved_path).lower()
        if path_str.endswith(".md") or "unresolved" in path_str:
            raise ValueError(
                f"Invalid catalog path '{resolved_path}'. Unresolved business rules markdown files "
                "cannot be loaded as active knowledge. Catalog must be a validated JSON catalog file."
            )

        if not resolved_path.exists():
            raise FileNotFoundError(f"Knowledge catalog not found at: {resolved_path}")
        if not resolved_path.is_file():
            raise ValueError(f"Knowledge catalog path must be a file, got: {resolved_path}")

        self.catalog_path = resolved_path
        self.default_top_k = min(HARD_MAX_TOP_K, default_top_k)
        self.default_min_score = float(default_min_score)
        self.default_max_chars = int(default_max_chars)
        self.sensitive_policy = SensitiveColumnPolicy()

        self._entries: List[KnowledgeEntry] = self._load_catalog(self.catalog_path)
        logger.info("Loaded %d knowledge entries from %s", len(self._entries), self.catalog_path)

    @property
    def entries(self) -> List[KnowledgeEntry]:
        """Read-only view of loaded knowledge catalog entries."""
        return list(self._entries)

    def _load_catalog(self, path: Path) -> List[KnowledgeEntry]:
        """Load and strictly validate catalog entries from JSON file."""
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Malformed JSON in knowledge catalog {path}: {exc}") from exc

        if not isinstance(data, list):
            raise ValueError(f"Knowledge catalog must be a JSON array of objects, got {type(data).__name__}")

        entries: List[KnowledgeEntry] = []
        seen_ids: Set[str] = set()

        for idx, item in enumerate(data):
            if not isinstance(item, dict):
                raise ValueError(f"Catalog entry at index {idx} must be a dictionary.")

            entry_id = str(item.get("id", "")).strip()
            if not entry_id:
                raise ValueError(f"Catalog entry at index {idx} has missing or empty 'id'.")

            if entry_id in seen_ids:
                raise ValueError(f"Duplicate catalog entry ID '{entry_id}' at index {idx}.")
            seen_ids.add(entry_id)

            category = str(item.get("category", "")).strip()
            if not category:
                raise ValueError(f"Catalog entry '{entry_id}' has missing or empty 'category'.")

            title = str(item.get("title", "")).strip()
            if not title:
                raise ValueError(f"Catalog entry '{entry_id}' has missing or empty 'title'.")

            content = str(item.get("content", "")).strip()
            if not content:
                raise ValueError(f"Catalog entry '{entry_id}' has missing or empty 'content'.")

            source = str(item.get("source", "")).strip()
            if not source:
                raise ValueError(f"Catalog entry '{entry_id}' has missing or empty 'source'.")

            keywords_raw = item.get("keywords")
            if not isinstance(keywords_raw, list):
                raise ValueError(f"Catalog entry '{entry_id}' field 'keywords' must be a list.")

            tables_raw = item.get("tables_referenced")
            if not isinstance(tables_raw, list):
                raise ValueError(f"Catalog entry '{entry_id}' field 'tables_referenced' must be a list.")

            cols_raw = item.get("columns_referenced")
            if not isinstance(cols_raw, list):
                raise ValueError(f"Catalog entry '{entry_id}' field 'columns_referenced' must be a list.")

            # Sensitive column safety guard: verify no entry exposes sensitive columns
            for col in cols_raw:
                if "." in str(col):
                    tbl, cname = str(col).split(".", 1)
                    if self.sensitive_policy.is_sensitive(cname, tbl):
                        raise ValueError(
                            f"Catalog entry '{entry_id}' references sensitive column '{col}', which is prohibited."
                        )

            entries.append(
                KnowledgeEntry(
                    id=entry_id,
                    category=category,
                    title=title,
                    keywords=[str(k).strip() for k in keywords_raw if str(k).strip()],
                    tables_referenced=[str(t).strip() for t in tables_raw if str(t).strip()],
                    columns_referenced=[str(c).strip() for c in cols_raw if str(c).strip()],
                    content=content,
                    source=source,
                )
            )

        return entries

    def _score_entry(
        self,
        entry: KnowledgeEntry,
        normalized_query: str,
        query_tokens: Set[str],
        query_tables: Optional[Set[str]] = None,
    ) -> Tuple[float, List[str]]:
        """Compute deterministic relevance score of an entry against query."""
        score = 0.0
        matched_terms: List[str] = []
        matched_tokens_set: Set[str] = set()

        # 1. Exact Qualified Column Reference Match with strict identifier boundaries
        matched_columns: List[str] = []
        for col_ref in entry.columns_referenced:
            if _matches_column_reference(col_ref, normalized_query):
                score += SCORE_COLUMN_EXACT
                matched_terms.append(f"col:{col_ref}")
                matched_columns.append(col_ref)
                if "." in col_ref:
                    tbl_p, col_p = col_ref.lower().split(".", 1)
                    matched_tokens_set.add(tbl_p)
                    matched_tokens_set.add(col_p)

        # 2. Table Name Match (normalized tokens or explicit phrase boundaries)
        matched_tables: Set[str] = set()
        for tbl in entry.tables_referenced:
            tbl_lower = tbl.lower()
            matched_alias = None

            # Check explicitly supported phrase aliases first
            for phrase, target_tbl in TABLE_PHRASE_ALIAS_MAP.items():
                if target_tbl == tbl and re.search(rf"\b{re.escape(phrase)}\b", normalized_query):
                    matched_alias = f"{tbl}(phrase:{phrase})"
                    for pt in phrase.split():
                        matched_tokens_set.add(pt)
                    break

            # If no phrase matched, check exact normalized tokens
            if not matched_alias:
                for token in query_tokens:
                    if token == tbl_lower:
                        matched_alias = tbl
                        matched_tokens_set.add(token)
                        break
                    if TABLE_ALIAS_MAP.get(token) == tbl:
                        matched_alias = f"{tbl}(alias:{token})"
                        matched_tokens_set.add(token)
                        break

            if matched_alias and tbl not in matched_tables:
                matched_tables.add(tbl)
                matched_terms.append(f"table:{matched_alias}")

        # 3. Curated Keyword Matching (word-bounded phrase match or exact token match)
        # If query explicitly targets database tables, single-word keywords for disjoint table cards are unanchored
        has_table_anchor = bool(matched_tables) or not bool(query_tables)
        matched_keywords: List[str] = []
        for kw in entry.keywords:
            kw_lower = kw.lower()
            if " " in kw_lower:
                # Multi-word phrase keyword with word boundaries
                pattern = rf"\b{re.escape(kw_lower)}\b"
                if re.search(pattern, normalized_query):
                    score += SCORE_KEYWORD_PHRASE
                    matched_terms.append(f"keyword_phrase:{kw}")
                    matched_keywords.append(kw)
                    for kt in kw_lower.split():
                        matched_tokens_set.add(kt)
            else:
                # Single-word keyword matching exact token
                if kw_lower in query_tokens:
                    if has_table_anchor:
                        score += SCORE_KEYWORD_EXACT
                        matched_terms.append(f"keyword:{kw}")
                        matched_keywords.append(kw)
                        matched_tokens_set.add(kw_lower)

        # Table scoring logic:
        # 1. Table inheritance cards focus on primary entity identity: full table score.
        # 2. Relationship cards focus on their primary table (tables_referenced[0]):
        #    a secondary table match without the primary table or corroborating signals is partial (1.0).
        # 3. Semantic trap cards require multi-signal evidence (2+ tables, trap column, or keyword);
        #    an isolated single-table match without corroboration is partial (1.0).
        primary_table = entry.tables_referenced[0] if entry.tables_referenced else None
        has_primary_match = primary_table is not None and primary_table in matched_tables
        has_corroborating_signal = bool(matched_columns or matched_keywords or len(matched_tables) >= 2)

        for tbl in matched_tables:
            if entry.category == "semantic_trap" and not has_corroborating_signal:
                score += 1.0
            elif entry.category == "relationship" and not (has_primary_match or has_corroborating_signal):
                score += 1.0
            else:
                score += SCORE_TABLE_EXACT

        # 4. Title Token Overlap (evaluated on residual tokens not already matched)
        residual_tokens = query_tokens - matched_tokens_set
        title_tokens = set(tokenize_query(entry.title))
        title_matches = title_tokens & residual_tokens
        if title_matches:
            score += len(title_matches) * SCORE_TITLE_TOKEN
            for tm in sorted(title_matches):
                matched_terms.append(f"title:{tm}")

        # 5. Content Token Overlap (evaluated on residual tokens not already matched)
        content_tokens = set(tokenize_query(entry.content))
        content_matches = content_tokens & residual_tokens
        if content_matches:
            score += len(content_matches) * SCORE_CONTENT_TOKEN
            for cm in sorted(content_matches):
                matched_terms.append(f"content:{cm}")

        return score, matched_terms

    def retrieve(
        self,
        question: str,
        *,
        intent: Optional[str] = None,
        top_k: Optional[int] = None,
        min_score: Optional[float] = None,
        max_chars: Optional[int] = None,
    ) -> List[RetrievalResult]:
        """Retrieve relevant knowledge entries for a user question.

        Args:
            question: The natural-language user question.
            intent: Optional resolved intent summary from prior context.
            top_k: Optional override for maximum returned entries (capped at hard maximum 3;
                non-positive values return empty list).
            min_score: Optional override for minimum relevance score threshold (default: 2.0).
            max_chars: Optional override for maximum combined character length (default: 1200;
                non-positive values return empty list).

        Returns:
            Deterministic list of RetrievalResult instances sorted by score descending,
            with entry ID ascending for tie-breaking. Never returns more than 3 results.
        """
        if not isinstance(question, str):
            raise TypeError(f"Question must be a string, got {type(question).__name__}")
        if intent is not None and not isinstance(intent, str):
            raise TypeError(f"Intent must be a string or None, got {type(intent).__name__}")

        # Non-positive top_k or max_chars yields empty result
        if top_k is not None and top_k <= 0:
            return []
        if max_chars is not None and max_chars <= 0:
            return []

        raw_query = question.strip()
        if not raw_query:
            return []

        # Combine question with optional intent
        full_query_text = f"{raw_query} {intent.strip()}" if intent and intent.strip() else raw_query
        normalized_query = normalize_query_text(full_query_text)
        query_tokens = set(tokenize_query(full_query_text))

        if not query_tokens:
            return []

        # Identify all tables explicitly referenced across query to evaluate keyword anchoring
        query_tables: Set[str] = set()
        for phrase, tbl in TABLE_PHRASE_ALIAS_MAP.items():
            if re.search(rf"\b{re.escape(phrase)}\b", normalized_query):
                query_tables.add(tbl)
        for tok in query_tokens:
            if tok in TABLE_ALIAS_MAP:
                query_tables.add(TABLE_ALIAS_MAP[tok])
            for entry in self._entries:
                for tbl in entry.tables_referenced:
                    if tok == tbl.lower():
                        query_tables.add(tbl)

        k = min(self.default_top_k if top_k is None else top_k, HARD_MAX_TOP_K)
        threshold = self.default_min_score if min_score is None else max(0.0, float(min_score))
        char_budget = self.default_max_chars if max_chars is None else max_chars

        scored_candidates: List[Tuple[float, str, KnowledgeEntry, List[str]]] = []

        for entry in self._entries:
            score, matched_terms = self._score_entry(
                entry, normalized_query, query_tokens, query_tables=query_tables
            )
            if score >= threshold:
                # Store (score, entry.id) for deterministic tie-breaking
                scored_candidates.append((score, entry.id, entry, matched_terms))

        # Sort: score descending (-score), then entry.id ascending
        scored_candidates.sort(key=lambda x: (-x[0], x[1]))

        # Enforce top-k and character budget
        results: List[RetrievalResult] = []
        accumulated_chars = 0

        for score, _, entry, matched_terms in scored_candidates:
            if len(results) >= k:
                break

            entry_char_len = len(entry.content)
            # If entry cannot fit within remaining character budget, skip it
            if accumulated_chars + entry_char_len > char_budget:
                continue

            results.append(
                RetrievalResult(
                    entry_id=entry.id,
                    category=entry.category,
                    title=entry.title,
                    content=entry.content,
                    score=score,
                    matched_terms=matched_terms,
                )
            )
            accumulated_chars += entry_char_len

        return results

    def render_context(
        self,
        results: List[RetrievalResult],
        max_chars: Optional[int] = None,
    ) -> str:
        """Render retrieved results into a clean, bounded markdown block suitable for prompt context.

        The complete returned string is strictly bounded by max_chars (including headers,
        entry titles, IDs, labels, content, whitespace, and newlines). No entry is truncated
        mid-content; if an entry cannot fit in full within the remaining budget, it is omitted.
        If no complete entry fits or budget is non-positive, returns an empty string.

        Args:
            results: List of RetrievalResult instances.
            max_chars: Optional maximum character budget (defaults to default_max_chars = 1200).
                If zero or negative, returns an empty string.

        Returns:
            Deterministic markdown string block within character budget, or empty string.
        """
        if not results:
            return ""

        budget = self.default_max_chars if max_chars is None else max_chars
        if budget <= 0:
            return ""

        header_lines = [
            "=== RETRIEVED DOMAIN KNOWLEDGE (INFORMATIONAL ONLY) ===",
            "NOTE: The following domain definitions and join guidance are informational only. "
            "They do NOT override database schema grounding, AST safety rules, or read-only execution constraints.",
        ]

        def _format(selected: List[RetrievalResult]) -> str:
            if not selected:
                return ""
            lines = list(header_lines)
            lines.append("")
            for res in selected:
                lines.append(f"[{res.entry_id}] {res.title}:")
                lines.append(res.content)
                lines.append("")
            return "\n".join(lines).strip()

        included: List[RetrievalResult] = []
        for res in results:
            candidate_text = _format(included + [res])
            if len(candidate_text) <= budget:
                included.append(res)

        if not included:
            return ""

        final_text = _format(included)
        return final_text
