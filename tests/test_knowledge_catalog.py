"""Offline validation tests for QueryPilot schema and domain knowledge catalog.

Validates that:
1. The catalog loads successfully and conforms to the knowledge entry schema.
2. Knowledge IDs are unique and required fields are present.
3. Allowed categories and reference formats are strictly enforced.
4. Every referenced table and column exists in the canonical offline schema (gradesense_schema.sql).
5. Physical DDL contains exactly 17 tables and 208 columns.
6. Sensitive columns (e.g. PasswordHash) are strictly prohibited from catalog entries.
7. Meaningful, non-whitespace source/evidence references are provided.
8. Unconfirmed business rules are excluded from active catalog entries.
9. Malformed entries, duplicate IDs, and policy violations trigger deterministic validation errors.

Runs entirely offline with ZERO live database or model API calls.
"""

import json
from pathlib import Path
import re
from typing import Any, Dict, List, Set
import pytest

from query_pilot.db.introspection import SensitiveColumnPolicy

CATALOG_PATH = Path(__file__).resolve().parent.parent / "data" / "knowledge" / "catalog.json"
CANONICAL_DDL_PATH = Path(__file__).resolve().parent.parent / "data" / "schemas" / "gradesense_schema.sql"

ALLOWED_CATEGORIES: Set[str] = {
    "table_inheritance",
    "relationship",
    "semantic_trap",
}

REQUIRED_ENTRY_FIELDS: Set[str] = {
    "id",
    "category",
    "title",
    "keywords",
    "tables_referenced",
    "columns_referenced",
    "content",
    "source",
}

# Forbidden keywords indicating unconfirmed business rules presented as active truth
FORBIDDEN_UNCONFIRMED_PHRASES: List[str] = [
    "official at-risk threshold",
    "official cgpa formula",
    "passing marks threshold",
    "institutional at-risk criteria",
]


def load_canonical_offline_schema() -> Dict[str, Set[str]]:
    """Extract tables and column sets from canonical DDL without connecting to a database."""
    assert CANONICAL_DDL_PATH.exists(), f"Canonical DDL file not found at {CANONICAL_DDL_PATH}"
    ddl_text = CANONICAL_DDL_PATH.read_text(encoding="utf-8", errors="ignore")

    raw_tables = re.findall(r"CREATE TABLE \[(\w+)\] \((.*?)\)\s*GO", ddl_text, re.DOTALL)
    assert len(raw_tables) == 17, f"Expected 17 tables in canonical DDL, found {len(raw_tables)}"

    schema_lookup: Dict[str, Set[str]] = {}
    for table_name, table_body in raw_tables:
        columns = re.findall(r"^\s*\[(\w+)\]", table_body, re.MULTILINE)
        schema_lookup[table_name] = set(columns)

    return schema_lookup


def validate_catalog_entry(
    entry: Dict[str, Any],
    schema_lookup: Dict[str, Set[str]],
    sensitive_policy: SensitiveColumnPolicy | None = None,
) -> None:
    """Validate a single knowledge entry against the schema and catalog rules.

    Raises:
        ValueError / TypeError on validation failure with an actionable message.
    """
    if not isinstance(entry, dict):
        raise TypeError(f"Catalog entry must be a dictionary, got {type(entry).__name__}")

    policy = sensitive_policy or SensitiveColumnPolicy()

    # Check required fields
    missing_fields = REQUIRED_ENTRY_FIELDS - set(entry.keys())
    if missing_fields:
        raise ValueError(f"Entry {entry.get('id', 'UNKNOWN')} missing required fields: {sorted(missing_fields)}")

    entry_id = str(entry["id"]).strip()
    if not entry_id:
        raise ValueError("Entry 'id' cannot be empty or whitespace.")

    # Check category
    category = entry["category"]
    if category not in ALLOWED_CATEGORIES:
        raise ValueError(
            f"Entry '{entry_id}' has invalid category '{category}'. Allowed: {sorted(ALLOWED_CATEGORIES)}"
        )

    # Check title and content
    if not isinstance(entry["title"], str) or not entry["title"].strip():
        raise ValueError(f"Entry '{entry_id}' must have a non-empty string title.")
    if not isinstance(entry["content"], str) or not entry["content"].strip():
        raise ValueError(f"Entry '{entry_id}' must have non-empty content.")

    # Check keywords
    keywords = entry["keywords"]
    if not isinstance(keywords, list) or len(keywords) == 0:
        raise ValueError(f"Entry '{entry_id}' keywords must be a non-empty list of strings.")
    for kw in keywords:
        if not isinstance(kw, str) or not kw.strip():
            raise ValueError(f"Entry '{entry_id}' contains an invalid keyword: {kw!r}")

    # Check source
    source = entry["source"]
    if not isinstance(source, str) or not source.strip():
        raise ValueError(f"Entry '{entry_id}' must have a non-empty, non-whitespace source reference.")

    # Check tables_referenced
    tables = entry["tables_referenced"]
    if not isinstance(tables, list) or len(tables) == 0:
        raise ValueError(f"Entry '{entry_id}' tables_referenced must be a non-empty list.")
    for tbl in tables:
        if tbl not in schema_lookup:
            raise ValueError(f"Entry '{entry_id}' references unknown table '{tbl}' not found in canonical schema.")

    # Check columns_referenced
    cols = entry["columns_referenced"]
    if not isinstance(cols, list):
        raise ValueError(f"Entry '{entry_id}' columns_referenced must be a list.")
    for col_ref in cols:
        if not isinstance(col_ref, str) or "." not in col_ref:
            raise ValueError(
                f"Entry '{entry_id}' column reference '{col_ref}' must follow 'TableName.ColumnName' format."
            )
        tbl_name, col_name = col_ref.split(".", 1)
        if tbl_name not in schema_lookup:
            raise ValueError(
                f"Entry '{entry_id}' column reference '{col_ref}' refers to unknown table '{tbl_name}'."
            )
        if col_name not in schema_lookup[tbl_name]:
            raise ValueError(
                f"Entry '{entry_id}' column reference '{col_ref}' refers to unknown column '{col_name}' "
                f"on table '{tbl_name}'. Available columns: {sorted(schema_lookup[tbl_name])}"
            )
        # Enforce sensitive column policy: sensitive columns must never appear in knowledge catalog
        if policy.is_sensitive(col_name, tbl_name):
            raise ValueError(
                f"Entry '{entry_id}' references sensitive column '{col_ref}'. "
                "Sensitive columns are prohibited in knowledge catalog."
            )


def validate_catalog_entries(
    entries: List[Dict[str, Any]],
    schema_lookup: Dict[str, Set[str]],
    sensitive_policy: SensitiveColumnPolicy | None = None,
) -> None:
    """Validate a list of knowledge entries enforcing ID uniqueness and per-entry correctness."""
    if not isinstance(entries, list):
        raise TypeError(f"Catalog entries must be a list, got {type(entries).__name__}")

    seen_ids: Set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise TypeError(f"Catalog entry must be a dictionary, got {type(entry).__name__}")
        entry_id = str(entry.get("id", "")).strip()
        if not entry_id:
            raise ValueError("Entry 'id' cannot be empty or whitespace.")
        if entry_id in seen_ids:
            raise ValueError(f"Duplicate knowledge entry ID found: '{entry_id}'. Entry IDs must be unique.")
        seen_ids.add(entry_id)

        validate_catalog_entry(entry, schema_lookup, sensitive_policy=sensitive_policy)


# ==============================================================================
# Pytest Test Cases
# ==============================================================================

def test_canonical_offline_schema_extraction():
    """Verify that canonical offline schema contains exactly 17 tables and 208 physical columns."""
    schema_lookup = load_canonical_offline_schema()

    # Explicit physical counts
    assert len(schema_lookup) == 17
    total_physical_columns = sum(len(cols) for cols in schema_lookup.values())
    assert total_physical_columns == 208

    # Targeted assertions for representative tables
    assert "Users" in schema_lookup
    assert "Students" in schema_lookup
    assert "Faculties" in schema_lookup
    assert "CourseOfferings" in schema_lookup
    assert "CourseEnrollments" in schema_lookup
    assert "StudentMarks" in schema_lookup

    # Verify column representations in Users (including physical PasswordHash)
    assert "Id" in schema_lookup["Users"]
    assert "FullName" in schema_lookup["Users"]
    assert "Email" in schema_lookup["Users"]
    assert "PasswordHash" in schema_lookup["Users"]


def test_catalog_file_exists_and_loads():
    """Verify data/knowledge/catalog.json exists and parses as a non-empty JSON list."""
    assert CATALOG_PATH.exists(), f"Catalog file not found at {CATALOG_PATH}"
    with open(CATALOG_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert isinstance(data, list)
    assert len(data) >= 5, f"Expected at least 5 knowledge entries, found {len(data)}"


def test_catalog_entry_uniqueness_and_structure():
    """Verify that all knowledge entry IDs are unique and all entries pass structural validation."""
    schema_lookup = load_canonical_offline_schema()
    with open(CATALOG_PATH, "r", encoding="utf-8") as f:
        entries = json.load(f)

    validate_catalog_entries(entries, schema_lookup)


def test_catalog_no_unconfirmed_business_rules():
    """Ensure catalog does not present unconfirmed domain rules as active definitions."""
    with open(CATALOG_PATH, "r", encoding="utf-8") as f:
        entries = json.load(f)

    for entry in entries:
        content_lower = entry["content"].lower()
        title_lower = entry["title"].lower()
        for phrase in FORBIDDEN_UNCONFIRMED_PHRASES:
            assert phrase not in content_lower, (
                f"Entry '{entry['id']}' contains forbidden unconfirmed phrase '{phrase}'."
            )
            assert phrase not in title_lower, (
                f"Entry '{entry['id']}' title contains forbidden unconfirmed phrase '{phrase}'."
            )


def test_malformed_entry_detection():
    """Verify that malformed entries fail validation with clear error messages."""
    schema_lookup = load_canonical_offline_schema()

    # 1. Missing required field
    bad_entry_missing = {
        "id": "KB_TEST_MISSING",
        "category": "table_inheritance",
        "title": "Test Title",
        "keywords": ["test"],
        "tables_referenced": ["Students"],
        "columns_referenced": ["Students.Id"],
        # 'content' is missing
        "source": "manual",
    }
    with pytest.raises(ValueError, match="missing required fields"):
        validate_catalog_entry(bad_entry_missing, schema_lookup)

    # 2. Invalid category
    bad_entry_category = {
        "id": "KB_TEST_CAT",
        "category": "invalid_category",
        "title": "Test Title",
        "keywords": ["test"],
        "tables_referenced": ["Students"],
        "columns_referenced": ["Students.Id"],
        "content": "Test content",
        "source": "manual",
    }
    with pytest.raises(ValueError, match="invalid category"):
        validate_catalog_entry(bad_entry_category, schema_lookup)

    # 3. Unknown table
    bad_entry_table = {
        "id": "KB_TEST_TBL",
        "category": "relationship",
        "title": "Test Title",
        "keywords": ["test"],
        "tables_referenced": ["NonExistentTable"],
        "columns_referenced": [],
        "content": "Test content",
        "source": "manual",
    }
    with pytest.raises(ValueError, match="unknown table 'NonExistentTable'"):
        validate_catalog_entry(bad_entry_table, schema_lookup)

    # 4. Unknown column
    bad_entry_col = {
        "id": "KB_TEST_COL",
        "category": "relationship",
        "title": "Test Title",
        "keywords": ["test"],
        "tables_referenced": ["Students"],
        "columns_referenced": ["Students.FakeColumnName"],
        "content": "Test content",
        "source": "manual",
    }
    with pytest.raises(ValueError, match="unknown column 'FakeColumnName' on table 'Students'"):
        validate_catalog_entry(bad_entry_col, schema_lookup)

    # 5. Invalid column reference format (missing dot)
    bad_entry_col_fmt = {
        "id": "KB_TEST_FMT",
        "category": "relationship",
        "title": "Test Title",
        "keywords": ["test"],
        "tables_referenced": ["Students"],
        "columns_referenced": ["StudentsIdWithoutDot"],
        "content": "Test content",
        "source": "manual",
    }
    with pytest.raises(ValueError, match="must follow 'TableName.ColumnName' format"):
        validate_catalog_entry(bad_entry_col_fmt, schema_lookup)

    # 6. Sensitive column reference prohibited (Users.PasswordHash)
    bad_entry_sensitive = {
        "id": "KB_TEST_SENSITIVE",
        "category": "table_inheritance",
        "title": "Test Title",
        "keywords": ["test"],
        "tables_referenced": ["Users"],
        "columns_referenced": ["Users.PasswordHash"],
        "content": "Test content",
        "source": "manual",
    }
    with pytest.raises(ValueError, match="references sensitive column 'Users.PasswordHash'"):
        validate_catalog_entry(bad_entry_sensitive, schema_lookup)

    # 7. Blank / whitespace-only source reference
    bad_entry_blank_source = {
        "id": "KB_TEST_BLANK_SOURCE",
        "category": "relationship",
        "title": "Test Title",
        "keywords": ["test"],
        "tables_referenced": ["Students"],
        "columns_referenced": ["Students.Id"],
        "content": "Test content",
        "source": "   ",
    }
    with pytest.raises(ValueError, match="non-empty, non-whitespace source reference"):
        validate_catalog_entry(bad_entry_blank_source, schema_lookup)


def test_duplicate_id_detection():
    """Verify that duplicate knowledge entry IDs are detected and rejected."""
    schema_lookup = load_canonical_offline_schema()
    mock_catalog = [
        {
            "id": "KB_DUPLICATE_ID",
            "category": "table_inheritance",
            "title": "Entry One",
            "keywords": ["test"],
            "tables_referenced": ["Students"],
            "columns_referenced": ["Students.Id"],
            "content": "Content one",
            "source": "manual",
        },
        {
            "id": "KB_DUPLICATE_ID",
            "category": "relationship",
            "title": "Entry Two",
            "keywords": ["test"],
            "tables_referenced": ["Departments"],
            "columns_referenced": ["Departments.Id"],
            "content": "Content two",
            "source": "manual",
        },
    ]
    with pytest.raises(ValueError, match="Duplicate knowledge entry ID found: 'KB_DUPLICATE_ID'"):
        validate_catalog_entries(mock_catalog, schema_lookup)
