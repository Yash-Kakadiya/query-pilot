"""Unit and integration tests for DomainContextRetriever.

Validates:
- Catalog loading and structured result fields.
- Exact table-name, alias, and column-identifier matching.
- Curated keyword and semantic-trap matching.
- Deterministic ranking and score-based differentiation.
- Deterministic tie-breaking (entry ID ascending).
- Minimum relevance threshold enforcement.
- Empty, whitespace, and irrelevant query handling.
- Top-k budget enforcement.
- Character budget enforcement (skipping entries that exceed remaining capacity).
- Exception handling for missing, malformed, or unauthorized catalog files.
- Rejection of unresolved business rules markdown files as active knowledge.
- Sensitive-column prohibition invariants.
- Determinism on repeated identical queries.
- Context rendering into informational markdown blocks.

Runs strictly offline with ZERO live database or model API calls.
"""

import json
from pathlib import Path
import tempfile
import pytest

from query_pilot.rag.retriever import (
    DEFAULT_CATALOG_PATH,
    DomainContextRetriever,
    HARD_MAX_TOP_K,
    KnowledgeEntry,
    RetrievalResult,
)


# ==============================================================================
# Fixtures
# ==============================================================================

@pytest.fixture
def default_retriever() -> DomainContextRetriever:
    """Fixture providing retriever initialized with default validated catalog."""
    return DomainContextRetriever()


@pytest.fixture
def sample_catalog_file(tmp_path: Path) -> Path:
    """Create a temporary custom catalog for controlled ranking tests."""
    catalog_data = [
        {
            "id": "KB_ALPHA_STUDENTS",
            "category": "table_inheritance",
            "title": "Students User Inheritance",
            "keywords": ["student", "user", "inheritance"],
            "tables_referenced": ["Students", "Users"],
            "columns_referenced": ["Students.Id", "Users.Id"],
            "content": "Students inherits identity from Users using Students.Id = Users.Id.",
            "source": "schema DDL",
        },
        {
            "id": "KB_BETA_STUDENT_MARKS",
            "category": "relationship",
            "title": "Student Marks Join Path",
            "keywords": ["marks", "grades", "enrollment"],
            "tables_referenced": ["StudentMarks", "CourseEnrollments"],
            "columns_referenced": ["StudentMarks.EnrollmentId", "CourseEnrollments.Id"],
            "content": "StudentMarks joins to CourseEnrollments via EnrollmentId.",
            "source": "schema DDL",
        },
        {
            "id": "KB_GAMMA_OFFERINGS",
            "category": "relationship",
            "title": "Course Offerings Coordinator",
            "keywords": ["coordinator", "course offering", "faculty"],
            "tables_referenced": ["CourseOfferings", "Faculties"],
            "columns_referenced": ["CourseOfferings.SubjectCoordinatorId", "Faculties.Id"],
            "content": "CourseOfferings coordinator references Faculties.Id.",
            "source": "schema DDL",
        },
    ]
    file_path = tmp_path / "custom_catalog.json"
    file_path.write_text(json.dumps(catalog_data), encoding="utf-8")
    return file_path


# ==============================================================================
# Core Retrieval & Ranking Tests
# ==============================================================================

def test_default_catalog_loads_successfully(default_retriever: DomainContextRetriever) -> None:
    """Verify that retriever loads the canonical catalog with exactly 7 entries."""
    assert len(default_retriever.entries) == 7
    entry_ids = [e.id for e in default_retriever.entries]
    assert "KB_STU_USERS_INHERITANCE" in entry_ids
    assert "KB_FAC_USERS_INHERITANCE" in entry_ids
    assert "KB_MARKS_STUDENT_ENROLLMENT_REL" in entry_ids
    assert "KB_COURSE_OFFERING_HIERARCHY" in entry_ids
    assert "KB_TRAP_STUDENT_DEPARTMENT_JOIN" in entry_ids
    assert "KB_TRAP_FACULTY_DEPARTMENT_JOIN" in entry_ids
    assert "KB_TRAP_OFFERING_COORDINATOR_JOIN" in entry_ids


def test_structured_result_fields(default_retriever: DomainContextRetriever) -> None:
    """Verify that retrieved results contain all required structured fields."""
    results = default_retriever.retrieve("How are student names joined from users?")
    assert len(results) > 0
    top_res = results[0]

    assert isinstance(top_res, RetrievalResult)
    assert top_res.entry_id == "KB_STU_USERS_INHERITANCE"
    assert top_res.category == "table_inheritance"
    assert "Student" in top_res.title
    assert "Students.Id = Users.Id" in top_res.content
    assert top_res.score >= 2.0
    assert len(top_res.matched_terms) > 0

    d = top_res.to_dict()
    assert d["entry_id"] == "KB_STU_USERS_INHERITANCE"
    assert isinstance(d["score"], float)
    assert isinstance(d["matched_terms"], list)


def test_exact_table_and_column_matching(default_retriever: DomainContextRetriever) -> None:
    """Verify that exact table and column identifiers boost relevant entries."""
    # Query specifying explicit column identifier
    results = default_retriever.retrieve("Query referencing Students.DepartmentId")
    assert len(results) > 0
    assert results[0].entry_id == "KB_TRAP_STUDENT_DEPARTMENT_JOIN"
    assert any("col:Students.DepartmentId" in term for term in results[0].matched_terms)


def test_table_alias_and_singular_plural_matching(default_retriever: DomainContextRetriever) -> None:
    """Verify that singular and plural table mentions both match canonical tables."""
    # Singular 'faculty'
    results_sing = default_retriever.retrieve("Find faculty coordinator")
    top_ids_sing = [r.entry_id for r in results_sing]
    assert "KB_TRAP_OFFERING_COORDINATOR_JOIN" in top_ids_sing or "KB_FAC_USERS_INHERITANCE" in top_ids_sing

    # Plural 'faculties'
    results_plur = default_retriever.retrieve("List all faculties")
    top_ids_plur = [r.entry_id for r in results_plur]
    assert "KB_FAC_USERS_INHERITANCE" in top_ids_plur


def test_semantic_trap_retrieval(default_retriever: DomainContextRetriever) -> None:
    """Verify that questions matching semantic trap concepts retrieve trap cards."""
    # Q028 trap: student department join vs user
    q28_results = default_retriever.retrieve("Students joined with department and user dean")
    q28_ids = [r.entry_id for r in q28_results]
    assert "KB_TRAP_STUDENT_DEPARTMENT_JOIN" in q28_ids

    # Q030 trap: course offering coordinator
    q30_results = default_retriever.retrieve("Course offerings coordinator professor name")
    q30_ids = [r.entry_id for r in q30_results]
    assert "KB_TRAP_OFFERING_COORDINATOR_JOIN" in q30_ids


def test_differential_ranking_stronger_match_first(sample_catalog_file: Path) -> None:
    """Verify that an entry matching multiple criteria ranks higher than a partial match."""
    retriever = DomainContextRetriever(catalog_path=sample_catalog_file)

    # Query with specific marks and enrollment keywords
    results = retriever.retrieve("Show student marks and course enrollments")
    assert len(results) >= 2
    # Marks entry matches table 'StudentMarks', table 'CourseEnrollments', keyword 'marks', keyword 'enrollment'
    assert results[0].entry_id == "KB_BETA_STUDENT_MARKS"
    assert results[0].score > results[1].score


def test_deterministic_tie_breaking(tmp_path: Path) -> None:
    """Verify that identical scores break ties deterministically by entry ID ascending."""
    tied_data = [
        {
            "id": "KB_ZULU_ENTRY",
            "category": "relationship",
            "title": "Topic Zulu",
            "keywords": ["database"],
            "tables_referenced": ["Users"],
            "columns_referenced": ["Users.Id"],
            "content": "Zulu content.",
            "source": "manual",
        },
        {
            "id": "KB_ALPHA_ENTRY",
            "category": "relationship",
            "title": "Topic Alpha",
            "keywords": ["database"],
            "tables_referenced": ["Users"],
            "columns_referenced": ["Users.Id"],
            "content": "Alpha content.",
            "source": "manual",
        },
    ]
    catalog_path = tmp_path / "tied_catalog.json"
    catalog_path.write_text(json.dumps(tied_data), encoding="utf-8")

    retriever = DomainContextRetriever(catalog_path=catalog_path)
    results = retriever.retrieve("database")

    assert len(results) == 2
    assert results[0].score == results[1].score
    # Must be ordered by ID ascending: KB_ALPHA_ENTRY before KB_ZULU_ENTRY
    assert results[0].entry_id == "KB_ALPHA_ENTRY"
    assert results[1].entry_id == "KB_ZULU_ENTRY"


def test_minimum_relevance_threshold(default_retriever: DomainContextRetriever) -> None:
    """Verify that entries below min_score threshold are discarded."""
    # With standard threshold (2.0), unrelated question returns empty
    results = default_retriever.retrieve("What is the weather in Tokyo?")
    assert len(results) == 0

    # High threshold filters out weak matches
    high_threshold_results = default_retriever.retrieve("student", min_score=20.0)
    assert len(high_threshold_results) == 0


def test_empty_and_whitespace_queries(default_retriever: DomainContextRetriever) -> None:
    """Verify that empty, whitespace-only, or stop-word-only queries return empty lists."""
    assert default_retriever.retrieve("") == []
    assert default_retriever.retrieve("   ") == []
    assert default_retriever.retrieve("\t\n") == []
    assert default_retriever.retrieve("what is the of and to") == []


def test_top_k_enforcement(default_retriever: DomainContextRetriever) -> None:
    """Verify that returned results never exceed top_k bound."""
    results_k1 = default_retriever.retrieve("student marks course offering faculty", top_k=1)
    assert len(results_k1) == 1

    results_k2 = default_retriever.retrieve("student marks course offering faculty", top_k=2)
    assert len(results_k2) == 2

    # Default top_k is 3
    results_default = default_retriever.retrieve("student marks course offering faculty")
    assert len(results_default) <= 3


def test_character_budget_enforcement(tmp_path: Path) -> None:
    """Verify that entries exceeding the remaining character budget are not included."""
    budget_data = [
        {
            "id": "KB_SHORT_1",
            "category": "relationship",
            "title": "Short One",
            "keywords": ["benchmark"],
            "tables_referenced": ["Students"],
            "columns_referenced": ["Students.Id"],
            "content": "Short description number one." * 2,  # ~60 chars
            "source": "manual",
        },
        {
            "id": "KB_LONG_2",
            "category": "relationship",
            "title": "Long Two",
            "keywords": ["benchmark"],
            "tables_referenced": ["Students"],
            "columns_referenced": ["Students.Id"],
            "content": "Very long content block. " * 30,  # ~750 chars
            "source": "manual",
        },
        {
            "id": "KB_SHORT_3",
            "category": "relationship",
            "title": "Short Three",
            "keywords": ["benchmark"],
            "tables_referenced": ["Students"],
            "columns_referenced": ["Students.Id"],
            "content": "Short description number three.",  # ~31 chars
            "source": "manual",
        },
    ]
    catalog_path = tmp_path / "budget_catalog.json"
    catalog_path.write_text(json.dumps(budget_data), encoding="utf-8")

    # Set budget to 120 chars: KB_SHORT_1 fits (~60 chars), KB_LONG_2 exceeds budget (skipped), KB_SHORT_3 fits
    retriever = DomainContextRetriever(catalog_path=catalog_path, default_max_chars=120)
    results = retriever.retrieve("benchmark", top_k=3)

    retrieved_ids = [r.entry_id for r in results]
    assert "KB_SHORT_1" in retrieved_ids
    assert "KB_LONG_2" not in retrieved_ids
    assert "KB_SHORT_3" in retrieved_ids

    total_chars = sum(len(r.content) for r in results)
    assert total_chars <= 120


# ==============================================================================
# Security & Safety Guards Tests
# ==============================================================================

def test_missing_catalog_file_raises_error() -> None:
    """Verify that a non-existent catalog file raises FileNotFoundError."""
    with pytest.raises(FileNotFoundError, match="Knowledge catalog not found"):
        DomainContextRetriever(catalog_path="non_existent_file.json")


def test_malformed_json_raises_error(tmp_path: Path) -> None:
    """Verify that malformed JSON raises ValueError."""
    bad_json = tmp_path / "broken.json"
    bad_json.write_text("{ broken json content", encoding="utf-8")
    with pytest.raises(ValueError, match="Malformed JSON"):
        DomainContextRetriever(catalog_path=bad_json)


def test_rejection_of_unresolved_business_rules_md(tmp_path: Path) -> None:
    """Verify that attempting to load unresolved_business_rules.md raises ValueError."""
    md_file = tmp_path / "unresolved_business_rules.md"
    md_file.write_text("# Unresolved Rules", encoding="utf-8")

    with pytest.raises(ValueError, match="Unresolved business rules markdown files cannot be loaded"):
        DomainContextRetriever(catalog_path=md_file)


def test_catalog_with_sensitive_column_rejected_at_load_time(tmp_path: Path) -> None:
    """Verify that any catalog referencing PasswordHash is rejected at load time."""
    sensitive_data = [
        {
            "id": "KB_LEAK",
            "category": "table_inheritance",
            "title": "Sensitive Leak",
            "keywords": ["password"],
            "tables_referenced": ["Users"],
            "columns_referenced": ["Users.PasswordHash"],
            "content": "Forbidden password hash guidance.",
            "source": "manual",
        }
    ]
    catalog_path = tmp_path / "leak_catalog.json"
    catalog_path.write_text(json.dumps(sensitive_data), encoding="utf-8")

    with pytest.raises(ValueError, match="references sensitive column 'Users.PasswordHash'"):
        DomainContextRetriever(catalog_path=catalog_path)


# ==============================================================================
# Determinism & Rendering Tests
# ==============================================================================

def test_repeated_queries_yield_identical_results(default_retriever: DomainContextRetriever) -> None:
    """Verify that repeated identical queries return 100% deterministic output."""
    query = "Find students and their course offerings coordinator"

    run_1 = default_retriever.retrieve(query)
    run_2 = default_retriever.retrieve(query)

    assert len(run_1) == len(run_2)
    for r1, r2 in zip(run_1, run_2):
        assert r1.entry_id == r2.entry_id
        assert r1.score == r2.score
        assert r1.content == r2.content
        assert r1.matched_terms == r2.matched_terms


def test_render_context_formatting(default_retriever: DomainContextRetriever) -> None:
    """Verify that render_context produces the expected structured markdown block."""
    results = default_retriever.retrieve("Students user inheritance")
    assert len(results) > 0

    rendered = default_retriever.render_context(results)
    assert "=== RETRIEVED DOMAIN KNOWLEDGE (INFORMATIONAL ONLY) ===" in rendered
    assert "NOTE: The following domain definitions and join guidance are informational only." in rendered
    assert "[KB_STU_USERS_INHERITANCE]" in rendered
    assert "Students.Id = Users.Id" in rendered


def test_render_context_empty_results(default_retriever: DomainContextRetriever) -> None:
    """Verify that render_context returns an empty string when results are empty."""
    assert default_retriever.render_context([]) == ""


# ==============================================================================
# Edge-Case Hardening Tests
# ==============================================================================

def test_hard_top_k_maximum_enforced(default_retriever: DomainContextRetriever) -> None:
    """Verify that retrieve never returns more than 3 results, even if caller requests more."""
    # Query matching multiple catalog cards (students, faculties, departments, offerings, etc.)
    broad_query = "student faculty department course offering users marks"

    # Requesting top_k > 3 must be capped at 3
    results_k5 = default_retriever.retrieve(broad_query, top_k=5)
    assert len(results_k5) == HARD_MAX_TOP_K
    assert len(results_k5) == 3

    results_k100 = default_retriever.retrieve(broad_query, top_k=100)
    assert len(results_k100) == HARD_MAX_TOP_K
    assert len(results_k100) == 3

    # Verify score descending, entry-ID ascending ordering
    for i in range(len(results_k5) - 1):
        if results_k5[i].score == results_k5[i + 1].score:
            assert results_k5[i].entry_id < results_k5[i + 1].entry_id
        else:
            assert results_k5[i].score >= results_k5[i + 1].score


def test_top_k_zero_or_negative_returns_empty(default_retriever: DomainContextRetriever) -> None:
    """Verify that zero or negative top_k values return an empty list deterministically."""
    assert default_retriever.retrieve("students", top_k=0) == []
    assert default_retriever.retrieve("students", top_k=-1) == []
    assert default_retriever.retrieve("students", top_k=-10) == []


def test_init_top_k_clamped_and_validated() -> None:
    """Verify that default_top_k is capped at 3 on init, and non-positive values raise ValueError."""
    # Clamped at hard maximum 3
    retriever = DomainContextRetriever(default_top_k=10)
    assert retriever.default_top_k == 3

    # Non-positive values raise ValueError
    with pytest.raises(ValueError, match="default_top_k must be a positive integer"):
        DomainContextRetriever(default_top_k=0)
    with pytest.raises(ValueError, match="default_top_k must be a positive integer"):
        DomainContextRetriever(default_top_k=-2)


def test_render_context_character_budget_strictly_enforced(default_retriever: DomainContextRetriever) -> None:
    """Verify that the rendered context string length never exceeds max_chars."""
    results = default_retriever.retrieve("student faculty course offering")
    assert len(results) > 0

    # Default budget (1200 characters)
    rendered_default = default_retriever.render_context(results)
    assert len(rendered_default) <= 1200

    # Custom budgets: every character in header, titles, labels, content, newlines is counted
    for budget in [300, 450, 600, 800]:
        rendered = default_retriever.render_context(results, max_chars=budget)
        assert len(rendered) <= budget


def test_render_context_does_not_truncate_content_mid_entry(default_retriever: DomainContextRetriever) -> None:
    """Verify that entries fitting within budget are included in full without mid-entry truncation."""
    results = default_retriever.retrieve("student faculty course offering")
    assert len(results) >= 2

    # Restrict budget so only the first complete entry fits (entry 1 formatted is ~727 chars)
    rendered = default_retriever.render_context(results, max_chars=800)
    assert len(rendered) <= 800
    assert len(rendered) > 0

    # The included entry must contain its entire content (not cut off mid-sentence)
    for res in results:
        if f"[{res.entry_id}]" in rendered:
            assert res.content in rendered


def test_render_context_returns_empty_when_no_entry_fits(default_retriever: DomainContextRetriever) -> None:
    """Verify that if no complete entry fits in the budget, an empty string is returned."""
    results = default_retriever.retrieve("student faculty course offering")
    assert len(results) > 0

    # Header alone is ~235 characters; a budget of 50 cannot fit any entry
    assert default_retriever.render_context(results, max_chars=50) == ""


def test_render_context_zero_or_negative_budget_returns_empty(default_retriever: DomainContextRetriever) -> None:
    """Verify that zero or negative max_chars budget returns an empty context string."""
    results = default_retriever.retrieve("student")
    assert len(results) > 0
    assert default_retriever.render_context(results, max_chars=0) == ""
    assert default_retriever.render_context(results, max_chars=-10) == ""


def test_retrieve_zero_or_negative_max_chars_returns_empty(default_retriever: DomainContextRetriever) -> None:
    """Verify that retrieve with non-positive max_chars returns an empty list."""
    assert default_retriever.retrieve("students", max_chars=0) == []
    assert default_retriever.retrieve("students", max_chars=-50) == []


def test_column_identifier_matching_boundaries(default_retriever: DomainContextRetriever) -> None:
    """Verify strict identifier boundaries for column references (positive & negative cases)."""
    # Positive: exact qualified column reference
    res_pos = default_retriever.retrieve("Query referencing Students.DepartmentId")
    assert len(res_pos) > 0
    assert res_pos[0].entry_id == "KB_TRAP_STUDENT_DEPARTMENT_JOIN"
    assert any("col:Students.DepartmentId" in t for t in res_pos[0].matched_terms)

    # Positive: case differences
    res_lower = default_retriever.retrieve("query referencing students.departmentid")
    assert len(res_lower) > 0
    assert any("col:Students.DepartmentId" in t for t in res_lower[0].matched_terms)

    # Positive: SQL bracketed notation
    res_brackets = default_retriever.retrieve("query referencing [Students].[DepartmentId]")
    assert len(res_brackets) > 0
    assert any("col:Students.DepartmentId" in t for t in res_brackets[0].matched_terms)

    # Positive: trailing period / sentence punctuation
    res_punct = default_retriever.retrieve("Check Students.DepartmentId.")
    assert len(res_punct) > 0
    assert any("col:Students.DepartmentId" in t for t in res_punct[0].matched_terms)

    # Negative: longer identifier containing column name as substring
    res_longer = default_retriever.retrieve("Query referencing Students.DepartmentIdOther")
    assert not any("col:Students.DepartmentId" in t for r in res_longer for t in r.matched_terms)

    # Negative: longer table name prefix
    res_prefix = default_retriever.retrieve("Query referencing NonStudents.DepartmentId")
    assert not any("col:Students.DepartmentId" in t for r in res_prefix for t in r.matched_terms)

    # Negative: underscore suffix
    res_under = default_retriever.retrieve("Query referencing Students.DepartmentId_Old")
    assert not any("col:Students.DepartmentId" in t for r in res_under for t in r.matched_terms)

    # Negative: partial column name
    res_partial = default_retriever.retrieve("Query referencing Students.Department")
    assert not any("col:Students.DepartmentId" in t for r in res_partial for t in r.matched_terms)

    # Negative: unqualified column name alone
    res_unqual = default_retriever.retrieve("Query referencing DepartmentId alone")
    assert not any("col:Students.DepartmentId" in t for r in res_unqual for t in r.matched_terms)


def test_table_alias_and_phrase_matching_boundaries(default_retriever: DomainContextRetriever) -> None:
    """Verify that table aliases match complete normalized tokens or explicit phrase boundaries."""
    # Positive phrase alias
    res_phrase = default_retriever.retrieve("Show all course offerings")
    assert len(res_phrase) > 0
    matched_offerings = [t for r in res_phrase for t in r.matched_terms if "CourseOfferings" in t]
    assert len(matched_offerings) > 0

    # Negative phrase prefix (intercourse offering must not match course offering)
    res_prefix = default_retriever.retrieve("intercourse offering")
    assert not any("phrase:course offering" in t for r in res_prefix for t in r.matched_terms)

    # Negative token substring (subdepartments must not match Departments)
    res_subdept = default_retriever.retrieve("subdepartments")
    assert not any("table:Departments" in t for r in res_subdept for t in r.matched_terms)

    # Negative token suffix (students_temp must not match Students)
    res_temp = default_retriever.retrieve("students_temporary")
    assert not any("table:Students" in t for r in res_temp for t in r.matched_terms)

    # Positive singular/plural alias matching
    res_fac_sing = default_retriever.retrieve("Find faculty coordinator")
    assert any("Faculties" in t for r in res_fac_sing for t in r.matched_terms)

    res_fac_plur = default_retriever.retrieve("Find faculties coordinator")
    assert any("Faculties" in t for r in res_fac_plur for t in r.matched_terms)


def test_common_words_and_fragments_do_not_retrieve_unrelated(default_retriever: DomainContextRetriever) -> None:
    """Verify that non-schema common words or partial fragments do not trigger false retrieval."""
    # Common English words that may appear in content
    assert default_retriever.retrieve("Please show the general enthusiasm and delivery structure") == []
    assert default_retriever.retrieve("How does the overall administration coordinate?") == []


def test_semantic_traps_retained_with_high_confidence(default_retriever: DomainContextRetriever) -> None:
    """Verify that all documented semantic traps remain retrievable under hardened matching."""
    # Q028 trap
    q28 = default_retriever.retrieve("Students joined with department and user dean")
    assert len(q28) > 0
    assert q28[0].entry_id == "KB_TRAP_STUDENT_DEPARTMENT_JOIN"

    # Q029 trap
    q29 = default_retriever.retrieve("Faculties primary key vs department foreign key collision")
    assert len(q29) > 0
    assert q29[0].entry_id == "KB_TRAP_FACULTY_DEPARTMENT_JOIN"

    # Q030 trap
    q30 = default_retriever.retrieve("Course offerings coordinator professor name")
    assert len(q30) > 0
    assert q30[0].entry_id == "KB_TRAP_OFFERING_COORDINATOR_JOIN"


def test_invalid_configuration_raises_actionable_errors(tmp_path: Path) -> None:
    """Verify that invalid configuration values and input types raise explicit exceptions."""
    with pytest.raises(ValueError, match="default_max_chars must be a positive integer"):
        DomainContextRetriever(default_max_chars=0)

    with pytest.raises(ValueError, match="default_min_score must be a non-negative number"):
        DomainContextRetriever(default_min_score=-1.0)

    retriever = DomainContextRetriever()
    with pytest.raises(TypeError, match="Question must be a string"):
        retriever.retrieve(12345)  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="Intent must be a string or None"):
        retriever.retrieve("valid question", intent=123)  # type: ignore[arg-type]

    # Passing directory instead of file
    with pytest.raises(ValueError, match="must be a file"):
        DomainContextRetriever(catalog_path=tmp_path)


def test_malformed_catalog_entries_raise_actionable_errors(tmp_path: Path) -> None:
    """Verify that catalog files with malformed entries or duplicate IDs raise ValueError."""
    # Duplicate ID
    dup_data = [
        {
            "id": "KB_DUP",
            "category": "relationship",
            "title": "Dup 1",
            "keywords": ["test"],
            "tables_referenced": ["Users"],
            "columns_referenced": ["Users.Id"],
            "content": "Content 1",
            "source": "manual",
        },
        {
            "id": "KB_DUP",
            "category": "relationship",
            "title": "Dup 2",
            "keywords": ["test"],
            "tables_referenced": ["Users"],
            "columns_referenced": ["Users.Id"],
            "content": "Content 2",
            "source": "manual",
        },
    ]
    p_dup = tmp_path / "dup.json"
    p_dup.write_text(json.dumps(dup_data), encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate catalog entry ID 'KB_DUP'"):
        DomainContextRetriever(catalog_path=p_dup)

    # Missing title
    missing_title = [
        {
            "id": "KB_NO_TITLE",
            "category": "relationship",
            "title": "  ",
            "keywords": ["test"],
            "tables_referenced": ["Users"],
            "columns_referenced": ["Users.Id"],
            "content": "Content",
            "source": "manual",
        }
    ]
    p_notitle = tmp_path / "no_title.json"
    p_notitle.write_text(json.dumps(missing_title), encoding="utf-8")
    with pytest.raises(ValueError, match="missing or empty 'title'"):
        DomainContextRetriever(catalog_path=p_notitle)

    # Missing content
    missing_content = [
        {
            "id": "KB_NO_CONTENT",
            "category": "relationship",
            "title": "Valid Title",
            "keywords": ["test"],
            "tables_referenced": ["Users"],
            "columns_referenced": ["Users.Id"],
            "content": "",
            "source": "manual",
        }
    ]
    p_nocontent = tmp_path / "no_content.json"
    p_nocontent.write_text(json.dumps(missing_content), encoding="utf-8")
    with pytest.raises(ValueError, match="missing or empty 'content'"):
        DomainContextRetriever(catalog_path=p_nocontent)

    # Non-list keywords
    bad_keywords = [
        {
            "id": "KB_BAD_KW",
            "category": "relationship",
            "title": "Valid Title",
            "keywords": "not_a_list",
            "tables_referenced": ["Users"],
            "columns_referenced": ["Users.Id"],
            "content": "Content",
            "source": "manual",
        }
    ]
    p_badkw = tmp_path / "bad_kw.json"
    p_badkw.write_text(json.dumps(bad_keywords), encoding="utf-8")
    with pytest.raises(ValueError, match="field 'keywords' must be a list"):
        DomainContextRetriever(catalog_path=p_badkw)
