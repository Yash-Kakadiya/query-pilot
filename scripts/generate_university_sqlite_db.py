"""Synthetic University SQLite Database Generator for QueryPilot (Milestone B24-G).

Creates a reproducible, deterministic demonstration database containing:
- departments (department_id, department_name)
- courses (course_id, course_name, department_id, credits)
- students (student_id, student_name, department_id, enrollment_year)
- enrollments (enrollment_id, student_id, course_id, semester, academic_year, marks)

Includes realistic distributions, filtering, aggregations, and edge cases:
- Courses with zero enrollments
- Students with zero enrollments

Enforces:
- Overwrite protection (requires explicit --overwrite flag).
- Schema integrity and foreign key constraints.
- Compatibility with QueryPilot's SQLite path validation policy.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any, Dict, List, Tuple


def get_default_db_path() -> Path:
    """Return default output path within the repository: data/demo/university_demo.db."""
    repo_root = Path(__file__).resolve().parent.parent
    return repo_root / "data" / "demo" / "university_demo.db"


# ------------------------------------------------------------------------------
# Deterministic Synthetic Data Tuples
# ------------------------------------------------------------------------------

DEPARTMENTS_DATA: List[Tuple[int, str]] = [
    (1, "Computer Science"),
    (2, "Mathematics"),
    (3, "Physics"),
    (4, "Electrical Engineering"),
    (5, "Biology"),
]

COURSES_DATA: List[Tuple[int, str, int, int]] = [
    (101, "Introduction to Programming", 1, 4),
    (102, "Data Structures and Algorithms", 1, 4),
    (103, "Database Management Systems", 1, 3),
    (104, "Linear Algebra", 2, 3),
    (105, "Probability and Statistics", 2, 3),
    (106, "Classical Mechanics", 3, 4),
    (107, "Digital Circuit Design", 4, 3),
    (108, "Molecular Genetics", 5, 4),
    # Edge case: Courses with NO enrollments
    (109, "Quantum Computing Lab", 1, 3),
    (110, "Bioinformatics Seminar", 5, 2),
]

STUDENTS_DATA: List[Tuple[int, str, int, int]] = [
    (1, "Alice Chen", 1, 2022),
    (2, "Bob Smith", 1, 2022),
    (3, "Clara Oswald", 1, 2023),
    (4, "David Kim", 2, 2022),
    (5, "Elena Rostova", 2, 2023),
    (6, "Felix Wright", 2, 2023),
    (7, "Grace Hopper", 3, 2022),
    (8, "Henry Cavill", 3, 2023),
    (9, "Iris West", 4, 2022),
    (10, "James Wilson", 4, 2023),
    (11, "Kavita Rao", 5, 2022),
    (12, "Leo Martinez", 5, 2023),
    (13, "Maya Lin", 1, 2024),
    (14, "Noah Taylor", 2, 2024),
    # Edge case: Students with NO enrollments
    (15, "Zara Patel", 1, 2024),
    (16, "Liam Vance", 3, 2024),
]

ENROLLMENTS_DATA: List[Tuple[int, int, int, str, int, float]] = [
    # Fall 2022
    (1, 1, 101, "Fall", 2022, 92.5),
    (2, 2, 101, "Fall", 2022, 78.0),
    (3, 4, 104, "Fall", 2022, 88.0),
    (4, 7, 106, "Fall", 2022, 95.0),
    (5, 9, 107, "Fall", 2022, 84.0),
    (6, 11, 108, "Fall", 2022, 91.0),
    # Spring 2023
    (7, 1, 102, "Spring", 2023, 89.0),
    (8, 2, 102, "Spring", 2023, 74.5),
    (9, 4, 105, "Spring", 2023, 90.0),
    (10, 7, 106, "Spring", 2023, 88.5),
    (11, 9, 107, "Spring", 2023, 82.0),
    (12, 11, 108, "Spring", 2023, 86.5),
    # Fall 2023
    (13, 1, 103, "Fall", 2023, 96.0),
    (14, 2, 103, "Fall", 2023, 81.0),
    (15, 3, 101, "Fall", 2023, 85.0),
    (16, 5, 104, "Fall", 2023, 93.0),
    (17, 6, 104, "Fall", 2023, 79.5),
    (18, 8, 106, "Fall", 2023, 87.0),
    (19, 10, 107, "Fall", 2023, 90.5),
    (20, 12, 108, "Fall", 2023, 83.0),
    # Spring 2024
    (21, 3, 102, "Spring", 2024, 91.5),
    (22, 5, 105, "Spring", 2024, 88.0),
    (23, 6, 105, "Spring", 2024, 82.5),
    (24, 8, 106, "Spring", 2024, 94.0),
    (25, 10, 107, "Spring", 2024, 89.0),
    (26, 12, 108, "Spring", 2024, 90.0),
    (27, 13, 101, "Spring", 2024, 87.5),
    (28, 14, 104, "Spring", 2024, 84.0),
    (29, 3, 103, "Spring", 2024, 92.0),
    (30, 4, 105, "Spring", 2024, 95.5),
]


def create_university_demo_db(output_path: Path, overwrite: bool = False) -> Path:
    """Generate the synthetic university demonstration SQLite database.

    Args:
        output_path: Canonical or relative destination file path.
        overwrite: When True, replaces existing destination file. When False, raises FileExistsError.

    Returns:
        The resolved Path of the created database.
    """
    dest = Path(output_path).resolve()

    if dest.exists():
        if not overwrite:
            raise FileExistsError(
                f"Destination database already exists: '{dest}'. "
                f"Specify overwrite=True or pass --overwrite to replace it."
            )
        dest.unlink()

    dest.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(dest))
    try:
        cursor = conn.cursor()
        cursor.execute("PRAGMA foreign_keys = ON;")

        # 1. DDL Schema Definition
        cursor.executescript("""
            CREATE TABLE departments (
                department_id INTEGER PRIMARY KEY,
                department_name TEXT NOT NULL UNIQUE
            );

            CREATE TABLE courses (
                course_id INTEGER PRIMARY KEY,
                course_name TEXT NOT NULL,
                department_id INTEGER NOT NULL,
                credits INTEGER NOT NULL CHECK (credits > 0),
                FOREIGN KEY (department_id) REFERENCES departments(department_id)
            );

            CREATE TABLE students (
                student_id INTEGER PRIMARY KEY,
                student_name TEXT NOT NULL,
                department_id INTEGER NOT NULL,
                enrollment_year INTEGER NOT NULL CHECK (enrollment_year >= 2000),
                FOREIGN KEY (department_id) REFERENCES departments(department_id)
            );

            CREATE TABLE enrollments (
                enrollment_id INTEGER PRIMARY KEY,
                student_id INTEGER NOT NULL,
                course_id INTEGER NOT NULL,
                semester TEXT NOT NULL CHECK (semester IN ('Fall', 'Spring', 'Summer')),
                academic_year INTEGER NOT NULL CHECK (academic_year >= 2000),
                marks REAL NOT NULL CHECK (marks >= 0.0 AND marks <= 100.0),
                FOREIGN KEY (student_id) REFERENCES students(student_id),
                FOREIGN KEY (course_id) REFERENCES courses(course_id)
            );

            CREATE INDEX idx_courses_dept ON courses(department_id);
            CREATE INDEX idx_students_dept ON students(department_id);
            CREATE INDEX idx_enrollments_student ON enrollments(student_id);
            CREATE INDEX idx_enrollments_course ON enrollments(course_id);
            CREATE INDEX idx_enrollments_year_sem ON enrollments(academic_year, semester);
        """)

        # 2. Deterministic DML Insertion
        cursor.executemany(
            "INSERT INTO departments (department_id, department_name) VALUES (?, ?);",
            DEPARTMENTS_DATA,
        )
        cursor.executemany(
            "INSERT INTO courses (course_id, course_name, department_id, credits) VALUES (?, ?, ?, ?);",
            COURSES_DATA,
        )
        cursor.executemany(
            "INSERT INTO students (student_id, student_name, department_id, enrollment_year) VALUES (?, ?, ?, ?);",
            STUDENTS_DATA,
        )
        cursor.executemany(
            "INSERT INTO enrollments (enrollment_id, student_id, course_id, semester, academic_year, marks) VALUES (?, ?, ?, ?, ?, ?);",
            ENROLLMENTS_DATA,
        )

        conn.commit()
    finally:
        conn.close()

    return dest


def verify_university_demo_db(db_path: Path) -> Dict[str, Any]:
    """Verify integrity, record counts, and analytical query execution on the created database.

    Returns:
        Dictionary containing verified record counts and analytical test results.
    """
    dest = Path(db_path).resolve()
    if not dest.is_file():
        raise FileNotFoundError(f"Database file not found: '{dest}'")

    conn = sqlite3.connect(f"file:{dest.as_posix()}?mode=ro", uri=True)
    try:
        cursor = conn.cursor()

        # Check foreign keys
        fk_violations = cursor.execute("PRAGMA foreign_key_check;").fetchall()
        if fk_violations:
            raise RuntimeError(f"Foreign key violations found: {fk_violations}")

        # Record counts
        counts = {
            "departments": cursor.execute("SELECT COUNT(*) FROM departments;").fetchone()[0],
            "courses": cursor.execute("SELECT COUNT(*) FROM courses;").fetchone()[0],
            "students": cursor.execute("SELECT COUNT(*) FROM students;").fetchone()[0],
            "enrollments": cursor.execute("SELECT COUNT(*) FROM enrollments;").fetchone()[0],
        }

        assert counts["departments"] == 5, f"Expected 5 departments, got {counts['departments']}"
        assert counts["courses"] == 10, f"Expected 10 courses, got {counts['courses']}"
        assert counts["students"] == 16, f"Expected 16 students, got {counts['students']}"
        assert counts["enrollments"] == 30, f"Expected 30 enrollments, got {counts['enrollments']}"

        # Analytical checks corresponding to Golden Path questions:
        # Q1: Total number of students in each department
        q1_results = cursor.execute("""
            SELECT d.department_name, COUNT(s.student_id) AS student_count
            FROM departments d
            LEFT JOIN students s ON d.department_id = s.department_id
            GROUP BY d.department_id, d.department_name
            ORDER BY student_count DESC;
        """).fetchall()
        assert len(q1_results) == 5

        # Q2: Average marks for each course
        q2_results = cursor.execute("""
            SELECT c.course_name, ROUND(AVG(e.marks), 2) AS avg_marks
            FROM courses c
            JOIN enrollments e ON c.course_id = e.course_id
            GROUP BY c.course_id, c.course_name
            ORDER BY avg_marks DESC;
        """).fetchall()
        assert len(q2_results) == 8  # 8 courses with enrollments

        # Q3: Top-performing students in a selected semester (Fall 2023)
        q3_results = cursor.execute("""
            SELECT s.student_name, e.marks
            FROM enrollments e
            JOIN students s ON e.student_id = s.student_id
            WHERE e.semester = 'Fall' AND e.academic_year = 2023
            ORDER BY e.marks DESC
            LIMIT 3;
        """).fetchall()
        assert len(q3_results) == 3
        assert q3_results[0][0] == "Alice Chen"
        assert q3_results[0][1] == 96.0

        # Q4: Enrollment count by academic year
        q4_results = cursor.execute("""
            SELECT academic_year, COUNT(*) AS count
            FROM enrollments
            GROUP BY academic_year
            ORDER BY academic_year ASC;
        """).fetchall()
        assert len(q4_results) == 3
        # 2022: 6, 2023: 14, 2024: 10
        assert q4_results == [(2022, 6), (2023, 14), (2024, 10)]

        # Q5: Departments with the highest average marks
        q5_results = cursor.execute("""
            SELECT d.department_name, ROUND(AVG(e.marks), 2) AS avg_marks
            FROM departments d
            JOIN students s ON d.department_id = s.department_id
            JOIN enrollments e ON s.student_id = e.student_id
            GROUP BY d.department_id, d.department_name
            ORDER BY avg_marks DESC;
        """).fetchall()
        assert len(q5_results) == 5

        # Q6: Courses with no enrollments
        q6_results = cursor.execute("""
            SELECT c.course_name
            FROM courses c
            LEFT JOIN enrollments e ON c.course_id = e.course_id
            WHERE e.enrollment_id IS NULL
            ORDER BY c.course_name;
        """).fetchall()
        assert [r[0] for r in q6_results] == ["Bioinformatics Seminar", "Quantum Computing Lab"]

        # Q7: Students without any enrollments
        q7_results = cursor.execute("""
            SELECT s.student_name
            FROM students s
            LEFT JOIN enrollments e ON s.student_id = e.student_id
            WHERE e.enrollment_id IS NULL
            ORDER BY s.student_name;
        """).fetchall()
        assert [r[0] for r in q7_results] == ["Liam Vance", "Zara Patel"]

        # Q8: Contextual follow-up: Departments offering courses with no enrollments
        q8_results = cursor.execute("""
            SELECT d.department_name, c.course_name
            FROM courses c
            JOIN departments d ON c.department_id = d.department_id
            LEFT JOIN enrollments e ON c.course_id = e.course_id
            WHERE e.enrollment_id IS NULL
            ORDER BY d.department_name;
        """).fetchall()
        assert len(q8_results) == 2
        assert q8_results == [
            ("Biology", "Bioinformatics Seminar"),
            ("Computer Science", "Quantum Computing Lab"),
        ]

        return {
            "status": "verified",
            "counts": counts,
            "top_student_fall_2023": q3_results[0],
            "courses_without_enrollments": [r[0] for r in q6_results],
            "students_without_enrollments": [r[0] for r in q7_results],
        }
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate reproducible synthetic university SQLite database for QueryPilot B24-G."
    )
    parser.add_argument(
        "--output-path",
        type=Path,
        default=get_default_db_path(),
        help="Path where the SQLite database will be written (default: data/demo/university_demo.db).",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite destination database if it already exists.",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Only verify an existing database file without regenerating it.",
    )

    args = parser.parse_args()
    db_path = args.output_path.resolve()

    if args.verify_only:
        print(f"[*] Verifying existing database: {db_path}")
        results = verify_university_demo_db(db_path)
        print("[OK] Verification passed successfully:")
        for k, v in results.items():
            print(f"    - {k}: {v}")
        return 0

    print(f"[*] Creating synthetic university database at: {db_path}")
    try:
        created_path = create_university_demo_db(db_path, overwrite=args.overwrite)
    except FileExistsError as err:
        print(f"[!] ERROR: {err}", file=sys.stderr)
        return 1

    print(f"[OK] Database generated successfully: {created_path}")
    print("[*] Running analytical and integrity self-verification...")
    results = verify_university_demo_db(created_path)
    print("[OK] Verification passed:")
    for k, v in results.items():
        print(f"    - {k}: {v}")

    # Optional: Validate via QueryPilot SQLite path validator if available on sys.path
    try:
        repo_root = Path(__file__).resolve().parent.parent
        sys.path.insert(0, str(repo_root / "src"))
        from query_pilot.db.sqlite_path import validate_sqlite_database_path
        canonical = validate_sqlite_database_path(str(created_path))
        print(f"[OK] QueryPilot SQLite path policy check passed: {canonical}")
    except Exception as exc:
        print(f"[!] Path policy verification note: {exc}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
