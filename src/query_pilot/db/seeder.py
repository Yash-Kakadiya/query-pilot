"""Database Seeder for QueryPilot.

Safely loads, sanitizes, and seeds GradeSense_Local using CSV and supplemental
data in strict foreign-key dependency order. Enforces fail-safe target database
verification and memory-level credential sanitization.
Uses Python's standard library csv module to avoid heavy third-party dependencies.
"""

import csv
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

logger = logging.getLogger(__name__)

# Canonical dummy hash replacing any password hashes during memory load
SANITIZED_PASSWORD_HASH = "SANITIZED_DUMMY_CREDENTIAL_HASH"

# Approved target database name (Strict Safeguard)
ALLOWED_TARGET_DATABASE = "GradeSense_Local"

# Batch size for database insert chunks
BATCH_SIZE = 500

# Strict foreign-key dependency order for database seeding
TABLE_DEPENDENCY_ORDER: List[str] = [
    "Users",
    "Departments",
    "Faculties",
    "Students",
    "Batches",
    "Subjects",
    "SubjectUnits",
    "CourseOfferings",
    "CourseEnrollments",
    "EvaluationSchemes",
    "AssessmentItems",
    "StudentMarks",
    "AttendanceRecords",
    "FacultyAssignments",
    "Predictions",
]

# Mapping of table names to their raw CSV filenames
CSV_TABLE_MAP: Dict[str, str] = {
    "Users": "users.csv",
    "Departments": "departments.csv",
    "Faculties": "faculties.csv",
    "Students": "students.csv",
    "Batches": "batches.csv",
    "Subjects": "subjects.csv",
    "SubjectUnits": "subject_units.csv",
    "CourseOfferings": "course_offerings.csv",
    "CourseEnrollments": "course_enrollments.csv",
    "EvaluationSchemes": "evaluation_schemes.csv",
    "AssessmentItems": "assessment_items.csv",
    "StudentMarks": "student_marks.csv",
    "AttendanceRecords": "attendance_records.csv",
}

# Tables with SQL Server IDENTITY columns requiring IDENTITY_INSERT ON
IDENTITY_TABLES: Set[str] = {
    "Users",
    "Departments",
    "Batches",
    "Subjects",
    "SubjectUnits",
    "CourseOfferings",
    "CourseEnrollments",
    "EvaluationSchemes",
    "AssessmentItems",
    "StudentMarks",
    "AttendanceRecords",
    "FacultyAssignments",
}


def _clean_cell_value(val: Any) -> Any:
    """Convert empty strings to None for SQL NULL compatibility."""
    if val is None:
        return None
    val_str = str(val).strip()
    if val_str == "" or val_str.lower() == "null" or val_str.lower() == "nan":
        return None
    return val


class DatabaseSeeder:
    """Safely seeds GradeSense_Local with sanitized records."""

    def __init__(
        self,
        csv_dir: Path,
        supplemental_dir: Optional[Path] = None,
        target_db_name: str = ALLOWED_TARGET_DATABASE,
    ):
        self.csv_dir = Path(csv_dir)
        self.supplemental_dir = Path(supplemental_dir) if supplemental_dir else self.csv_dir
        self.target_db_name = target_db_name
        self.validate_target_database_name(self.target_db_name)

    @staticmethod
    def validate_target_database_name(db_name: str) -> None:
        """Enforce that seeding operations only target GradeSense_Local."""
        if db_name != ALLOWED_TARGET_DATABASE:
            raise ValueError(
                f"SAFETY VIOLATION: Seeder is strictly restricted to '{ALLOWED_TARGET_DATABASE}'. "
                f"Target database '{db_name}' is forbidden to prevent accidental mutation of other databases."
            )

    @staticmethod
    def sanitize_records(table_name: str, records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Sanitize sensitive fields in-memory before database staging."""
        sanitized_list = []
        for rec in records:
            cleaned = {k: _clean_cell_value(v) for k, v in rec.items()}
            if table_name == "Users":
                if "PasswordHash" in cleaned:
                    cleaned["PasswordHash"] = SANITIZED_PASSWORD_HASH
            sanitized_list.append(cleaned)
        return sanitized_list

    def load_table_data(self, table_name: str) -> List[Dict[str, Any]]:
        """Load and sanitize data for a specific table."""
        if table_name in CSV_TABLE_MAP:
            csv_path = self.csv_dir / CSV_TABLE_MAP[table_name]
            if not csv_path.exists():
                raise FileNotFoundError(
                    f"Required seed CSV file for table '{table_name}' not found: {csv_path}"
                )
            records = []
            with open(csv_path, mode="r", encoding="utf-8", errors="ignore") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    records.append(row)
            return self.sanitize_records(table_name, records)

        elif table_name == "FacultyAssignments":
            json_path = self.supplemental_dir / "supplemental_faculty_assignments.json"
            if not json_path.exists():
                logger.warning("Supplemental file for FacultyAssignments not found. Skipping.")
                return []
            with open(json_path, "r", encoding="utf-8") as f:
                return json.load(f)

        elif table_name == "Predictions":
            json_path = self.supplemental_dir / "supplemental_predictions.json"
            if not json_path.exists():
                logger.warning("Supplemental file for Predictions not found. Skipping.")
                return []
            with open(json_path, "r", encoding="utf-8") as f:
                return json.load(f)

        return []

    @staticmethod
    def build_parameterized_insert(table_name: str, columns: List[str]) -> str:
        """Construct parameterized SQL INSERT statement."""
        col_list = ", ".join(f"[{c}]" for c in columns)
        param_list = ", ".join(f":{c}" for c in columns)
        return f"INSERT INTO [{table_name}] ({col_list}) VALUES ({param_list})"

    def seed_table(self, conn: Connection, table_name: str, records: List[Dict[str, Any]]) -> int:
        """Seed a single table using parameterized statements in chunks."""
        if not records:
            return 0

        columns = list(records[0].keys())
        insert_sql = text(self.build_parameterized_insert(table_name, columns))
        has_identity = table_name in IDENTITY_TABLES

        if has_identity:
            conn.execute(text(f"SET IDENTITY_INSERT [{table_name}] ON;"))

        try:
            # Chunked batch execute to respect ODBC / TDS limits
            for i in range(0, len(records), BATCH_SIZE):
                chunk = records[i : i + BATCH_SIZE]
                conn.execute(insert_sql, chunk)
        finally:
            if has_identity:
                conn.execute(text(f"SET IDENTITY_INSERT [{table_name}] OFF;"))

        return len(records)

    def seed_all(self, engine: Engine) -> Dict[str, int]:
        """Execute the full database seeding sequence."""
        # Safeguard: verify database name from engine URL
        engine_db = engine.url.database
        self.validate_target_database_name(engine_db)

        results: Dict[str, int] = {}
        with engine.begin() as conn:
            # Temporarily disable foreign-key constraints for clean ingestion
            for table in TABLE_DEPENDENCY_ORDER:
                conn.execute(text(f"ALTER TABLE [{table}] NOCHECK CONSTRAINT ALL;"))

            try:
                for table in TABLE_DEPENDENCY_ORDER:
                    records = self.load_table_data(table)
                    count = self.seed_table(conn, table, records)
                    results[table] = count
                    logger.info(f"Seeded {count} records into [{table}].")
            finally:
                # Re-enable and verify all foreign key constraints
                for table in TABLE_DEPENDENCY_ORDER:
                    conn.execute(text(f"ALTER TABLE [{table}] WITH CHECK CHECK CONSTRAINT ALL;"))

        return results
