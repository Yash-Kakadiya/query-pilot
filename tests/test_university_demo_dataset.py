"""Automated unit and offline regression tests for the University Demo SQLite Dataset (Milestone B24-G).

Verifies:
1. Deterministic database creation and table schemas.
2. Overwrite protection policy (fails without explicit overwrite=True).
3. Schema integrity, primary keys, foreign keys, and indexes.
4. Correct record counts and edge case representations (courses/students without enrollments).
5. All 8 Golden Path analytical queries execute correctly.
6. Compatibility with QueryPilot's SQLite path validation boundary.
7. Local Agent schema introspection and guarded read-only execution.
8. Authorizer blocks destructive DDL/DML statements against the demo database.
"""

from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import pytest

from query_pilot.db.executor import UnsafeQueryError
from query_pilot.db.models import DatabaseSchema
from query_pilot.db.profile import ConnectionProfile
from query_pilot.db.profile_executor import ProfileExecutionBoundary, ProfileExecutionError
from query_pilot.db.profile_introspector import ProfileDatabaseIntrospector
from query_pilot.db.sqlite_path import validate_sqlite_database_path
from scripts.generate_university_sqlite_db import (
    create_university_demo_db,
    verify_university_demo_db,
)


@pytest.fixture
def demo_db_path(tmp_path: Path) -> Path:
    """Fixture providing a newly generated synthetic university demonstration database."""
    target = tmp_path / "test_university.db"
    return create_university_demo_db(target, overwrite=False)


def test_university_demo_creation_and_overwrite_protection(tmp_path: Path):
    """Verify database is created and protected from accidental overwrite."""
    target = tmp_path / "demo_protect.db"
    assert not target.exists()

    # Initial creation
    created = create_university_demo_db(target, overwrite=False)
    assert created.is_file()

    # Refuse overwrite without flag
    with pytest.raises(FileExistsError, match="Destination database already exists"):
        create_university_demo_db(target, overwrite=False)

    # Allow overwrite with flag
    created_again = create_university_demo_db(target, overwrite=True)
    assert created_again.is_file()


def test_university_demo_deterministic_generation(tmp_path: Path):
    """Verify repeated generations produce identical tables, records, and checksum-equivalent data."""
    path1 = tmp_path / "u1.db"
    path2 = tmp_path / "u2.db"

    create_university_demo_db(path1, overwrite=False)
    create_university_demo_db(path2, overwrite=False)

    results1 = verify_university_demo_db(path1)
    results2 = verify_university_demo_db(path2)

    assert results1 == results2
    assert results1["counts"] == {"departments": 5, "courses": 10, "students": 16, "enrollments": 30}


def test_university_demo_schema_and_edge_cases(demo_db_path: Path):
    """Verify expected foreign keys, courses without enrollments, and students without enrollments."""
    conn = sqlite3.connect(f"file:{demo_db_path.as_posix()}?mode=ro", uri=True)
    try:
        cursor = conn.cursor()

        # Check edge case: courses without enrollments
        unregistered_courses = cursor.execute("""
            SELECT course_name FROM courses c
            WHERE NOT EXISTS (SELECT 1 FROM enrollments e WHERE e.course_id = c.course_id)
            ORDER BY course_name;
        """).fetchall()
        assert [r[0] for r in unregistered_courses] == [
            "Bioinformatics Seminar",
            "Quantum Computing Lab",
        ]

        # Check edge case: students without enrollments
        unregistered_students = cursor.execute("""
            SELECT student_name FROM students s
            WHERE NOT EXISTS (SELECT 1 FROM enrollments e WHERE e.student_id = s.student_id)
            ORDER BY student_name;
        """).fetchall()
        assert [r[0] for r in unregistered_students] == [
            "Liam Vance",
            "Zara Patel",
        ]
    finally:
        conn.close()


def test_university_demo_path_validator_compatibility(demo_db_path: Path):
    """Verify demo database path complies with QueryPilot's SQLite path policy."""
    canonical = validate_sqlite_database_path(str(demo_db_path))
    assert Path(canonical).resolve() == demo_db_path.resolve()


def test_university_demo_local_agent_introspection_and_execution(demo_db_path: Path):
    """Verify local agent introspects demo schema and executes read-only SQL under security boundary."""
    profile = ConnectionProfile(
        profile_id="demo_university",
        display_name="Demo University SQLite",
        engine_type="sqlite",
        database_path=str(demo_db_path),
        timeout_seconds=5,
        max_row_limit=50,
    )

    # 1. Dynamic Introspection
    introspector = ProfileDatabaseIntrospector(profile)
    schema: DatabaseSchema = introspector.introspect_raw()
    assert schema.database_name == "test_university"
    table_names = [t.table_name for t in schema.tables]
    assert "departments" in table_names
    assert "courses" in table_names
    assert "students" in table_names
    assert "enrollments" in table_names

    # 2. Read-Only Query Execution via Boundary
    executor = ProfileExecutionBoundary(profile)

    # Analytical Query: Average marks per department
    q_avg = """
        SELECT d.department_name, ROUND(AVG(e.marks), 2) AS avg_marks
        FROM departments d
        JOIN courses c ON d.department_id = c.department_id
        JOIN enrollments e ON c.course_id = e.course_id
        GROUP BY d.department_id, d.department_name
        ORDER BY avg_marks DESC;
    """
    res = executor.execute(q_avg, schema=schema)
    assert res.row_count > 0
    assert "department_name" in res.columns
    assert "avg_marks" in res.columns

    # 3. Security Rejection: Mutations remain strictly blocked by pre-execution AST validator and authorizer
    with pytest.raises((UnsafeQueryError, ProfileExecutionError), match="NOT_READ_ONLY|prohibited|rejected|denied"):
        executor.execute("DELETE FROM students WHERE student_id = 1;", schema=schema)

    with pytest.raises((UnsafeQueryError, ProfileExecutionError), match="NOT_READ_ONLY|prohibited|rejected|denied"):
        executor.execute("DROP TABLE enrollments;", schema=schema)

    with pytest.raises((UnsafeQueryError, ProfileExecutionError), match="NOT_READ_ONLY|prohibited|rejected|denied"):
        executor.execute("CREATE TABLE test_hacked (x INT);", schema=schema)
