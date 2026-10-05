"""Unit tests for DatabaseSeeder (pure in-memory, no database modification)."""

import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock
from query_pilot.config import get_db_settings
from query_pilot.db.seeder import (
    DatabaseSeeder,
    TABLE_DEPENDENCY_ORDER,
    CSV_TABLE_MAP,
    SANITIZED_PASSWORD_HASH,
    ALLOWED_TARGET_DATABASE,
)


def test_seeder_does_not_require_gemini_api_key(monkeypatch):
    """Verify that DatabaseSeeder configuration works with zero LLM environment variables."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("LANGCHAIN_API_KEY", raising=False)

    db_settings = get_db_settings()
    assert db_settings.DB_NAME == "GradeSense_Local"

    seeder = DatabaseSeeder(
        csv_dir=Path("data/raw"),
        target_db_name=db_settings.DB_NAME,
    )
    assert seeder.target_db_name == "GradeSense_Local"


def test_target_database_safeguard():
    """Verify that seeder refuses any target database other than GradeSense_Local."""
    dummy_csv_dir = Path("data/raw")

    # Allowed target database
    seeder = DatabaseSeeder(csv_dir=dummy_csv_dir, target_db_name=ALLOWED_TARGET_DATABASE)
    assert seeder.target_db_name == "GradeSense_Local"

    # Blocked databases
    forbidden_targets = ["GradeSense", "GradeSenseCodeFirst", "master", "tempdb", "Production_DB", "", None]
    for forbidden in forbidden_targets:
        with pytest.raises(ValueError) as exc_info:
            DatabaseSeeder(csv_dir=dummy_csv_dir, target_db_name=forbidden)
        assert "SAFETY VIOLATION" in str(exc_info.value)


def test_seed_all_session_validation_targets():
    """Verify that seed_all validates target database using SELECT DB_NAME() on session."""
    mock_engine = MagicMock()
    mock_conn = MagicMock()
    mock_engine.begin.return_value.__enter__.return_value = mock_conn

    seeder = DatabaseSeeder(csv_dir=Path("data/raw"))
    seeder.load_table_data = MagicMock(return_value=[])

    # 1. Connection reporting GradeSense_Local is accepted
    mock_conn.execute.return_value.scalar.return_value = "GradeSense_Local"
    results = seeder.seed_all(mock_engine)
    assert isinstance(results, dict)

    # 2. Connection reporting GradeSense is rejected
    mock_conn.execute.return_value.scalar.return_value = "GradeSense"
    with pytest.raises(ValueError) as exc:
        seeder.seed_all(mock_engine)
    assert "SAFETY VIOLATION" in str(exc.value)

    # 3. Connection reporting GradeSenseCodeFirst is rejected
    mock_conn.execute.return_value.scalar.return_value = "GradeSenseCodeFirst"
    with pytest.raises(ValueError) as exc:
        seeder.seed_all(mock_engine)
    assert "SAFETY VIOLATION" in str(exc.value)

    # 4. Empty or NULL database name is rejected
    for invalid in [None, ""]:
        mock_conn.execute.return_value.scalar.return_value = invalid
        with pytest.raises(ValueError) as exc:
            seeder.seed_all(mock_engine)
        assert "SAFETY VIOLATION" in str(exc.value)


def test_seed_all_no_inserts_before_validation():
    """Verify that seed_all performs NO inserts if target database validation fails."""
    mock_engine = MagicMock()
    mock_conn = MagicMock()
    mock_engine.begin.return_value.__enter__.return_value = mock_conn
    mock_conn.execute.return_value.scalar.return_value = "GradeSense"

    seeder = DatabaseSeeder(csv_dir=Path("data/raw"))
    seeder.seed_table = MagicMock()

    with pytest.raises(ValueError):
        seeder.seed_all(mock_engine)

    # Assert seed_table was never invoked
    seeder.seed_table.assert_not_called()


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


def test_predictions_schema_filtered_unique_index():
    """Verify that gradesense_schema.sql defines a filtered unique index on Predictions (WHERE IsActive = 1)."""
    schema_path = Path("data/schemas/gradesense_schema.sql")
    assert schema_path.exists(), "gradesense_schema.sql must exist"

    with open(schema_path, "r", encoding="utf-8") as f:
        schema_text = f.read()

    expected_pattern = "CREATE UNIQUE INDEX [idx_predictions_active_unique] ON [Predictions] ([CourseEnrollmentId]) WHERE [IsActive] = 1"
    assert expected_pattern in schema_text, (
        "idx_predictions_active_unique must be a filtered unique index with WHERE [IsActive] = 1"
    )


def test_supplemental_predictions_filtered_uniqueness():
    """Verify that supplemental predictions have exactly one active prediction per CourseEnrollmentId."""
    json_path = Path("data/seeds/supplemental_predictions.json")
    assert json_path.exists(), "supplemental_predictions.json must exist"

    with open(json_path, "r", encoding="utf-8") as f:
        records = json.load(f)

    active_enrollments = [r["CourseEnrollmentId"] for r in records if r.get("IsActive") in (1, 1.0, True)]
    # All active enrollments must be unique
    assert len(active_enrollments) == len(set(active_enrollments)), (
        f"Duplicate active predictions found for enrollments: {active_enrollments}"
    )
