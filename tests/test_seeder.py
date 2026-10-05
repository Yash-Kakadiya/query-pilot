"""Unit tests for DatabaseSeeder (pure in-memory, no database modification)."""

import pytest
from pathlib import Path
from query_pilot.db.seeder import (
    DatabaseSeeder,
    TABLE_DEPENDENCY_ORDER,
    CSV_TABLE_MAP,
    SANITIZED_PASSWORD_HASH,
    ALLOWED_TARGET_DATABASE,
)


def test_target_database_safeguard():
    """Verify that seeder refuses any target database other than GradeSense_Local."""
    dummy_csv_dir = Path("data/raw")

    # Allowed target database
    seeder = DatabaseSeeder(csv_dir=dummy_csv_dir, target_db_name=ALLOWED_TARGET_DATABASE)
    assert seeder.target_db_name == "GradeSense_Local"

    # Blocked databases
    forbidden_targets = ["GradeSense", "GradeSenseCodeFirst", "master", "tempdb", "Production_DB"]
    for forbidden in forbidden_targets:
        with pytest.raises(ValueError) as exc_info:
            DatabaseSeeder(csv_dir=dummy_csv_dir, target_db_name=forbidden)
        assert "SAFETY VIOLATION" in str(exc_info.value)


def test_import_dependency_order():
    """Verify that foreign-key parent tables strictly precede child tables."""
    order_index = {tbl: i for i, tbl in enumerate(TABLE_DEPENDENCY_ORDER)}

    # Users is the root identity parent
    assert order_index["Users"] < order_index["Faculties"]
    assert order_index["Users"] < order_index["Students"]

    # Departments must precede Batches and Subjects
    assert order_index["Departments"] < order_index["Batches"]
    assert order_index["Departments"] < order_index["Subjects"]

    # Subjects must precede SubjectUnits and CourseOfferings
    assert order_index["Subjects"] < order_index["SubjectUnits"]
    assert order_index["Subjects"] < order_index["CourseOfferings"]

    # CourseOfferings and Students must precede CourseEnrollments
    assert order_index["CourseOfferings"] < order_index["CourseEnrollments"]
    assert order_index["Students"] < order_index["CourseEnrollments"]

    # CourseEnrollments and AssessmentItems must precede StudentMarks
    assert order_index["CourseEnrollments"] < order_index["StudentMarks"]
    assert order_index["AssessmentItems"] < order_index["StudentMarks"]

    # CourseEnrollments must precede AttendanceRecords and Predictions
    assert order_index["CourseEnrollments"] < order_index["AttendanceRecords"]
    assert order_index["CourseEnrollments"] < order_index["Predictions"]


def test_password_hash_sanitization():
    """Verify that PasswordHash is consistently masked in-memory."""
    raw_users = [
        {"Id": 1, "Email": "user1@example.com", "PasswordHash": "$2a$10$abcFakeHash123", "Role": "Admin"},
        {"Id": 2, "Email": "user2@example.com", "PasswordHash": "$2a$10$xyzSecretHash456", "Role": "Faculty"},
    ]

    sanitized = DatabaseSeeder.sanitize_records("Users", raw_users)

    assert len(sanitized) == 2
    for user in sanitized:
        assert user["PasswordHash"] == SANITIZED_PASSWORD_HASH
        assert "$2a$10$" not in user["PasswordHash"]

    # Original list should not mutate
    assert raw_users[0]["PasswordHash"] == "$2a$10$abcFakeHash123"

    # Non-Users tables should pass through untouched
    other_records = [{"Id": 1, "Name": "Computer Science"}]
    assert DatabaseSeeder.sanitize_records("Departments", other_records) == other_records


def test_missing_csv_file_error(tmp_path):
    """Verify that a missing CSV raises FileNotFoundError."""
    seeder = DatabaseSeeder(csv_dir=tmp_path)
    with pytest.raises(FileNotFoundError) as exc_info:
        seeder.load_table_data("Users")
    assert "users.csv" in str(exc_info.value)


def test_parameterized_insert_builder():
    """Verify that the SQL statement builder creates parameterized queries."""
    sql = DatabaseSeeder.build_parameterized_insert("Users", ["Id", "Email", "PasswordHash", "Role"])

    assert "INSERT INTO [Users]" in sql
    assert "[Id], [Email], [PasswordHash], [Role]" in sql
    assert "VALUES (:Id, :Email, :PasswordHash, :Role)" in sql


def test_supplemental_records_loading():
    """Verify that supplemental records load and maintain relational integrity."""
    supplemental_dir = Path("data/seeds")
    if not supplemental_dir.exists():
        pytest.skip("Supplemental directory not initialized yet.")

    seeder = DatabaseSeeder(csv_dir=Path("data/raw"), supplemental_dir=supplemental_dir)

    fac_assignments = seeder.load_table_data("FacultyAssignments")
    assert isinstance(fac_assignments, list)
    assert len(fac_assignments) > 0
    for assign in fac_assignments:
        assert "CourseOfferingId" in assign
        assert "FacultyId" in assign
        assert "Role" in assign

    predictions = seeder.load_table_data("Predictions")
    assert isinstance(predictions, list)
    assert len(predictions) > 0
    for pred in predictions:
        assert "CourseEnrollmentId" in pred
        assert "PredictedCategory" in pred
        assert "RiskScore" in pred
        # Enforce that CourseEnrollmentId is within the valid sample range (<= 200)
        assert pred["CourseEnrollmentId"] <= 200
