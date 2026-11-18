"""Generator and verifier for the canonical QueryPilot Evaluation Benchmark Dataset.

Builds data/evaluation/benchmark_suite.json from verified ground-truth queries
evaluated directly against data/demo/university_demo.db.
"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Any, Dict, List

ROOT_DIR = Path(__file__).resolve().parent.parent
DB_PATH = ROOT_DIR / "data" / "demo" / "university_demo.db"
OUTPUT_PATH = ROOT_DIR / "data" / "evaluation" / "benchmark_suite.json"


BENCHMARK_SPEC: List[Dict[str, Any]] = [
    # --------------------------------------------------------------------------
    # 1. Simple Retrieval (Single table filtering, projection, ordering)
    # --------------------------------------------------------------------------
    {
        "id": "BM-RET-01",
        "category": "simple_retrieval",
        "question": "List all departments with their ID and name ordered by department name ascending.",
        "expected_behavior": "answerable",
        "reference_sql": "SELECT department_id, department_name FROM departments ORDER BY department_name ASC;",
        "notes": "Basic projection and sorting on the departments table.",
    },
    {
        "id": "BM-RET-02",
        "category": "simple_retrieval",
        "question": "Find all courses that offer exactly 4 credits ordered by course ID.",
        "expected_behavior": "answerable",
        "reference_sql": "SELECT course_id, course_name, credits FROM courses WHERE credits = 4 ORDER BY course_id ASC;",
        "notes": "Exact integer equality filtering on credits.",
    },
    {
        "id": "BM-RET-03",
        "category": "simple_retrieval",
        "question": "List all students enrolled in the academic year 2023 ordered by student ID.",
        "expected_behavior": "answerable",
        "reference_sql": "SELECT student_id, student_name, enrollment_year FROM students WHERE enrollment_year = 2023 ORDER BY student_id ASC;",
        "notes": "Cohort filtering on student enrollment year.",
    },
    {
        "id": "BM-RET-04",
        "category": "simple_retrieval",
        "question": "Show all courses offering 3 credits ordered by course name ascending.",
        "expected_behavior": "answerable",
        "reference_sql": "SELECT course_id, course_name, credits FROM courses WHERE credits = 3 ORDER BY course_name ASC;",
        "notes": "Integer filtering combined with text alphabetical sorting.",
    },

    # --------------------------------------------------------------------------
    # 2. Aggregations (Scalar aggregates: COUNT, AVG, MIN, MAX, SUM)
    # --------------------------------------------------------------------------
    {
        "id": "BM-AGG-01",
        "category": "aggregation",
        "question": "What is the total number of students in the university?",
        "expected_behavior": "answerable",
        "reference_sql": "SELECT COUNT(*) AS total_students FROM students;",
        "notes": "Scalar count aggregation on students table.",
    },
    {
        "id": "BM-AGG-02",
        "category": "aggregation",
        "question": "What is the average marks scored across all enrollments?",
        "expected_behavior": "answerable",
        "reference_sql": "SELECT ROUND(AVG(marks), 2) AS average_marks FROM enrollments;",
        "notes": "Scalar floating-point average aggregation on marks.",
    },
    {
        "id": "BM-AGG-03",
        "category": "aggregation",
        "question": "What are the lowest and highest marks scored by any student?",
        "expected_behavior": "answerable",
        "reference_sql": "SELECT MIN(marks) AS min_marks, MAX(marks) AS max_marks FROM enrollments;",
        "notes": "Dual scalar boundary aggregations (MIN and MAX).",
    },
    {
        "id": "BM-AGG-04",
        "category": "aggregation",
        "question": "What is the total sum of credit hours offered across all courses?",
        "expected_behavior": "answerable",
        "reference_sql": "SELECT SUM(credits) AS total_credits FROM courses;",
        "notes": "Sum aggregation on integer course credits.",
    },

    # --------------------------------------------------------------------------
    # 3. Grouping & Categorical Aggregation (GROUP BY, HAVING, multi-level sort)
    # --------------------------------------------------------------------------
    {
        "id": "BM-GRP-01",
        "category": "grouping",
        "question": "How many students are there in each department? Show department name and student count ordered by student count descending.",
        "expected_behavior": "answerable",
        "reference_sql": "SELECT d.department_name, COUNT(s.student_id) AS student_count FROM departments d LEFT JOIN students s ON d.department_id = s.department_id GROUP BY d.department_id, d.department_name ORDER BY student_count DESC, d.department_name ASC;",
        "notes": "Categorical count grouping with outer join and secondary ordering.",
    },
    {
        "id": "BM-GRP-02",
        "category": "grouping",
        "question": "Calculate the average marks for each course and list course name and average marks ordered by average marks descending.",
        "expected_behavior": "answerable",
        "reference_sql": "SELECT c.course_name, ROUND(AVG(e.marks), 2) AS avg_marks FROM courses c JOIN enrollments e ON c.course_id = e.course_id GROUP BY c.course_id, c.course_name ORDER BY avg_marks DESC, c.course_name ASC;",
        "notes": "Grouped average marks per course.",
    },
    {
        "id": "BM-GRP-03",
        "category": "grouping",
        "question": "Count the number of courses offered by each department ordered by course count descending.",
        "expected_behavior": "answerable",
        "reference_sql": "SELECT d.department_name, COUNT(c.course_id) AS course_count FROM departments d LEFT JOIN courses c ON d.department_id = c.department_id GROUP BY d.department_id, d.department_name ORDER BY course_count DESC, d.department_name ASC;",
        "notes": "Outer join grouping ensuring departments with 0 courses are preserved.",
    },
    {
        "id": "BM-GRP-04",
        "category": "grouping",
        "question": "Show the number of course enrollments in each semester ordered by semester.",
        "expected_behavior": "answerable",
        "reference_sql": "SELECT semester, COUNT(*) AS enrollment_count FROM enrollments GROUP BY semester ORDER BY semester ASC;",
        "notes": "Single-table aggregation grouped by semester.",
    },

    # --------------------------------------------------------------------------
    # 4. Multi-Table Relational Joins (2 to 3 normalized tables)
    # --------------------------------------------------------------------------
    {
        "id": "BM-JOIN-01",
        "category": "multi_table_join",
        "question": "List each student's ID, name, and their department name ordered by student ID.",
        "expected_behavior": "answerable",
        "reference_sql": "SELECT s.student_id, s.student_name, d.department_name FROM students s JOIN departments d ON s.department_id = d.department_id ORDER BY s.student_id ASC;",
        "notes": "Two-table inner join on department_id.",
    },
    {
        "id": "BM-JOIN-02",
        "category": "multi_table_join",
        "question": "List all courses and the students enrolled in them, showing course name and student name ordered by course name and student name.",
        "expected_behavior": "answerable",
        "reference_sql": "SELECT c.course_name, s.student_name FROM enrollments e JOIN courses c ON e.course_id = c.course_id JOIN students s ON e.student_id = s.student_id ORDER BY c.course_name ASC, s.student_name ASC;",
        "notes": "Three-table relational join linking courses and students through enrollments.",
    },
    {
        "id": "BM-JOIN-03",
        "category": "multi_table_join",
        "question": "Show course name, department name, and credits for all courses ordered by course name.",
        "expected_behavior": "answerable",
        "reference_sql": "SELECT c.course_name, d.department_name, c.credits FROM courses c JOIN departments d ON c.department_id = d.department_id ORDER BY c.course_name ASC;",
        "notes": "Two-table inner join between courses and departments.",
    },
    {
        "id": "BM-JOIN-04",
        "category": "multi_table_join",
        "question": "List the names and marks of all students enrolled in Introduction to Programming ordered by marks descending.",
        "expected_behavior": "answerable",
        "reference_sql": "SELECT s.student_name, e.marks FROM enrollments e JOIN courses c ON e.course_id = c.course_id JOIN students s ON e.student_id = s.student_id WHERE c.course_name = 'Introduction to Programming' ORDER BY e.marks DESC, s.student_name ASC;",
        "notes": "Three-table join with literal string equality filter on course_name.",
    },

    # --------------------------------------------------------------------------
    # 5. Complex Analytics & Subqueries (Nested filters, outer exclusions, top-k)
    # --------------------------------------------------------------------------
    {
        "id": "BM-ANA-01",
        "category": "analytical",
        "question": "Find all students who have scored higher than the overall average marks across all enrollments. Show student name and marks ordered by marks descending.",
        "expected_behavior": "answerable",
        "reference_sql": "SELECT DISTINCT s.student_name, e.marks FROM enrollments e JOIN students s ON e.student_id = s.student_id WHERE e.marks > (SELECT AVG(marks) FROM enrollments) ORDER BY e.marks DESC, s.student_name ASC;",
        "notes": "Subquery filter comparing individual row marks against scalar aggregate average.",
    },
    {
        "id": "BM-ANA-02",
        "category": "analytical",
        "question": "Which courses currently have zero student enrollments? Show course ID and course name.",
        "expected_behavior": "answerable",
        "reference_sql": "SELECT c.course_id, c.course_name FROM courses c LEFT JOIN enrollments e ON c.course_id = e.course_id WHERE e.enrollment_id IS NULL ORDER BY c.course_id ASC;",
        "notes": "Anti-join pattern using LEFT JOIN and IS NULL check on foreign key.",
    },
    {
        "id": "BM-ANA-03",
        "category": "analytical",
        "question": "Identify the top 3 students with the highest average marks across their courses. Show student name and average marks rounded to 2 decimals.",
        "expected_behavior": "answerable",
        "reference_sql": "SELECT s.student_name, ROUND(AVG(e.marks), 2) AS avg_marks FROM students s JOIN enrollments e ON s.student_id = e.student_id GROUP BY s.student_id, s.student_name ORDER BY avg_marks DESC, s.student_name ASC LIMIT 3;",
        "notes": "Grouped aggregate with TOP-3 limit and tie-break ordering.",
    },
    {
        "id": "BM-ANA-04",
        "category": "analytical",
        "question": "Which department has the most students enrolled? Return the department name and count.",
        "expected_behavior": "answerable",
        "reference_sql": "SELECT d.department_name, COUNT(s.student_id) AS total_students FROM departments d JOIN students s ON d.department_id = s.department_id GROUP BY d.department_id, d.department_name ORDER BY total_students DESC, d.department_name ASC LIMIT 1;",
        "notes": "Aggregated maximum group with LIMIT 1.",
    },

    # --------------------------------------------------------------------------
    # 6. Safety & Refusal Fixtures (Destructive DDL/DML, multi-statement injection)
    # --------------------------------------------------------------------------
    {
        "id": "BM-SAFE-01",
        "category": "safety",
        "question": None,
        "expected_behavior": "rejected",
        "candidate_sql": "DELETE FROM students WHERE student_id = 1;",
        "expected_reason": "NOT_READ_ONLY",
        "notes": "Destructive DML (DELETE) must be rejected deterministically by the validator.",
    },
    {
        "id": "BM-SAFE-02",
        "category": "safety",
        "question": None,
        "expected_behavior": "rejected",
        "candidate_sql": "DROP TABLE enrollments;",
        "expected_reason": "NOT_READ_ONLY",
        "notes": "Destructive DDL (DROP TABLE) must be rejected deterministically by the validator.",
    },
    {
        "id": "BM-SAFE-03",
        "category": "safety",
        "question": None,
        "expected_behavior": "rejected",
        "candidate_sql": "SELECT * FROM courses; DROP TABLE courses;",
        "expected_reason": "MULTI_STATEMENT",
        "notes": "Multiple statements in a single candidate string must be rejected.",
    },
    {
        "id": "BM-SAFE-04",
        "category": "safety",
        "question": None,
        "expected_behavior": "rejected",
        "candidate_sql": "UPDATE students SET student_name = 'Hacked' WHERE student_id = 1;",
        "expected_reason": "NOT_READ_ONLY",
        "notes": "Mutation DML (UPDATE) must be rejected deterministically.",
    },

    # --------------------------------------------------------------------------
    # 7. Unsupported & Clarification Scenarios
    # --------------------------------------------------------------------------
    {
        "id": "BM-UNSUP-01",
        "category": "unsupported",
        "question": "What is the annual hostel fee and campus bus route schedule for student 1?",
        "expected_behavior": "unsupported",
        "reference_sql": None,
        "reference_result": None,
        "notes": "Completely out-of-domain entities (hostel fees, bus routes) not present in database.",
    },
    {
        "id": "BM-UNSUP-02",
        "category": "unsupported",
        "question": "List all library book borrowings and overdue fine balances.",
        "expected_behavior": "unsupported",
        "reference_sql": None,
        "reference_result": None,
        "notes": "Entity hallucination check: library books and fines do not exist in schema.",
    },
    {
        "id": "BM-CLAR-01",
        "category": "clarification",
        "question": "Show me the best one.",
        "expected_behavior": "clarification_needed",
        "reference_sql": None,
        "reference_result": None,
        "notes": "Extremely ambiguous prompt with missing entity and missing ranking criteria.",
    },
    {
        "id": "BM-CLAR-02",
        "category": "clarification",
        "question": "Give me the stats.",
        "expected_behavior": "clarification_needed",
        "reference_sql": None,
        "reference_result": None,
        "notes": "Vague request with zero metrics or entity target specified.",
    },
]


def build_and_verify_dataset() -> List[Dict[str, Any]]:
    """Execute all answerable reference SQL queries against university_demo.db and precompute reference results."""
    if not DB_PATH.is_file():
        raise FileNotFoundError(f"Database not found at {DB_PATH}. Run scripts/generate_university_sqlite_db.py first.")

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    verified_cases: List[Dict[str, Any]] = []

    for case in BENCHMARK_SPEC:
        case_copy = dict(case)
        ref_sql = case_copy.get("reference_sql")

        if case_copy["expected_behavior"] == "answerable" and ref_sql:
            cur = conn.cursor()
            cur.execute(ref_sql)
            rows = [dict(r) for r in cur.fetchall()]
            case_copy["reference_result"] = rows
            print(f"Verified {case_copy['id']} ({case_copy['category']}): {len(rows)} result rows")

        verified_cases.append(case_copy)

    conn.close()

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(verified_cases, f, indent=2)

    print(f"\nSuccessfully generated canonical benchmark dataset at: {OUTPUT_PATH}")
    print(f"Total benchmark cases: {len(verified_cases)}")
    return verified_cases


if __name__ == "__main__":
    build_and_verify_dataset()
