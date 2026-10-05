"""Unit and integration tests for safe read-only SQL executor."""

import json
import pytest
from sqlalchemy import text

from query_pilot.db.connection import create_db_engine, get_connection
from query_pilot.db.executor import (
    QueryExecutionError,
    QueryResult,
    ReadOnlyQueryExecutor,
    UnsafeQueryError,
    execute_read_only,
    validate_read_only_sql,
)


# ==============================================================================
# Unit Tests — AST Validation & Read-Only Policy (No Database Connection)
# ==============================================================================

@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM Students;",
        "SELECT s.Id, s.EnrollmentNumber FROM Students s WHERE s.Id = 22;",
        "WITH x AS (SELECT Id FROM Students) SELECT * FROM x;",
        "SELECT * FROM Students WHERE Status = 'DELETE';",
        "SELECT * FROM Students WHERE Status = 'DROP TABLE Students';",
        "SELECT * /* DROP TABLE Students */ FROM Students;",
        "SELECT * -- DELETE FROM Students\nFROM Students;",
        "SELECT DepartmentId, COUNT(*) FROM Students GROUP BY DepartmentId HAVING COUNT(*) > 1;",
        "SELECT s.Id, u.FullName FROM Students s JOIN Users u ON s.Id = u.Id ORDER BY s.Id ASC;",
        "SELECT Id FROM Students UNION ALL SELECT Id FROM Faculties;",
        "SELECT TOP 10 Id FROM Students;",
    ],
)
def test_validate_read_only_allowed_queries(sql):
    """Verify that legitimate read-only queries pass AST verification."""
    stmt = validate_read_only_sql(sql)
    assert stmt is not None


@pytest.mark.parametrize(
    "sql,expected_keyword",
    [
        ("INSERT INTO Students (Id) VALUES (1);", "Insert"),
        ("UPDATE Students SET Status = 'Inactive';", "Update"),
        ("DELETE FROM Students;", "Delete"),
        ("MERGE Target AS T USING Source AS S ON (T.Id = S.Id) WHEN MATCHED THEN UPDATE SET T.Val = S.Val;", "Merge"),
        ("DROP TABLE Students;", "Drop"),
        ("ALTER TABLE Students ADD TempCol int;", "Alter"),
        ("CREATE TABLE TestTable (Id int);", "Create"),
        ("TRUNCATE TABLE Students;", "TruncateTable"),
        ("EXEC sp_help;", "Execute"),
        ("EXECUTE sp_who;", "Execute"),
        ("GRANT SELECT ON Students TO user1;", "Grant"),
        ("REVOKE SELECT ON Students FROM user1;", "Revoke"),
        ("SELECT * INTO BackupStudents FROM Students;", "Into"),
    ],
)
def test_validate_read_only_rejected_mutating_queries(sql, expected_keyword):
    """Verify that all mutating operations are rejected by AST validation."""
    with pytest.raises(UnsafeQueryError) as exc_info:
        validate_read_only_sql(sql)
    assert "forbidden" in str(exc_info.value).lower() or expected_keyword.lower() in str(exc_info.value).lower()


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM Students; DELETE FROM Students;",
        "SELECT 1; SELECT 2;",
        "SELECT * FROM Users; DROP TABLE Users;",
        "SET NOCOUNT ON; SELECT 1;",
    ],
)
def test_validate_read_only_rejected_multi_statements(sql):
    """Verify that multi-statement queries are strictly rejected."""
    with pytest.raises(UnsafeQueryError) as exc_info:
        validate_read_only_sql(sql)
    assert "multi-statement" in str(exc_info.value).lower()


@pytest.mark.parametrize(
    "sql",
    [
        "",
        "   ",
        "\n\t",
        "NOT A VALID SQL STATEMENT !!!",
        "SELECT FROM WHERE;",
    ],
)
def test_validate_read_only_invalid_syntax_fails_closed(sql):
    """Verify that empty or unparseable queries fail closed."""
    with pytest.raises(UnsafeQueryError):
        validate_read_only_sql(sql)


def test_query_result_to_dict_and_serialization():
    """Verify QueryResult dataclass serialization."""
    res = QueryResult(
        columns=["Id", "Name"],
        rows=[{"Id": 1, "Name": "Test"}],
        row_count=1,
        truncated=False,
        execution_time_ms=12.34,
    )
    d = res.to_dict()
    assert d["columns"] == ["Id", "Name"]
    assert d["row_count"] == 1
    assert d["truncated"] is False
    assert d["execution_time_ms"] == 12.34
    # Ensure JSON serializable
    serialized = json.dumps(d)
    assert "Test" in serialized


# ==============================================================================
# Live Integration Tests — GradeSense_Local (Read-Only)
# ==============================================================================

@pytest.fixture(scope="module")
def executor():
    """Fixture providing ReadOnlyQueryExecutor configured for GradeSense_Local."""
    return ReadOnlyQueryExecutor()


def test_live_simple_select(executor):
    """1. Verify simple SELECT succeeds and returns expected schema and data."""
    res = executor.execute("SELECT TOP 5 Id, EnrollmentNumber, DepartmentId FROM Students;")
    assert res.columns == ["Id", "EnrollmentNumber", "DepartmentId"]
    assert res.row_count == 5
    assert res.truncated is False
    assert res.rows[0]["EnrollmentNumber"] == "21CE001"


def test_live_select_with_where(executor):
    """2. Verify SELECT with WHERE clause filters correctly."""
    res = executor.execute("SELECT Id, EnrollmentNumber FROM Students WHERE Id = 22;")
    assert res.row_count == 1
    assert res.rows[0]["Id"] == 22
    assert res.rows[0]["EnrollmentNumber"] == "21CE001"


def test_live_join(executor):
    """3. Verify JOIN across tables succeeds."""
    sql = """
    SELECT s.Id, u.FullName, u.Email, s.EnrollmentNumber
    FROM Students s
    JOIN Users u ON s.Id = u.Id
    WHERE s.Id = 22;
    """
    res = executor.execute(sql)
    assert res.row_count == 1
    assert res.columns == ["Id", "FullName", "Email", "EnrollmentNumber"]
    assert res.rows[0]["FullName"] == "Priya Kothari"
    assert res.rows[0]["Email"] == "priya.kothari.22@gradesense.edu"


def test_live_aggregate_and_group_by(executor):
    """4. Verify aggregate and GROUP BY queries succeed."""
    sql = "SELECT DepartmentId, COUNT(*) as TotalStudents FROM Students GROUP BY DepartmentId ORDER BY DepartmentId;"
    res = executor.execute(sql)
    assert res.row_count == 2
    assert res.rows[0]["DepartmentId"] == 1
    assert res.rows[0]["TotalStudents"] == 20
    assert res.rows[1]["DepartmentId"] == 2
    assert res.rows[1]["TotalStudents"] == 20


def test_live_empty_result(executor):
    """5. Verify empty result returns correct columns, zero rows, and truncated=False."""
    res = executor.execute("SELECT * FROM Students WHERE 1 = 0;")
    assert len(res.columns) == 10
    assert "EnrollmentNumber" in res.columns
    assert res.rows == []
    assert res.row_count == 0
    assert res.truncated is False


def test_live_column_order_preservation(executor):
    """6. Verify returned result columns preserve exact SELECT order."""
    sql = "SELECT TOP 1 DepartmentId, Id, Status, EnrollmentNumber FROM Students;"
    res = executor.execute(sql)
    assert res.columns == ["DepartmentId", "Id", "Status", "EnrollmentNumber"]
    assert list(res.rows[0].keys()) == ["DepartmentId", "Id", "Status", "EnrollmentNumber"]


def test_live_json_friendly_types(executor):
    """7. Verify returned values serialize to JSON cleanly without error."""
    sql = "SELECT TOP 2 Id, Email, Role, IsActive, CreatedAt FROM Users;"
    res = executor.execute(sql)
    # json.dumps must not raise TypeError for datetimes or booleans
    payload = json.dumps(res.rows)
    assert "admin@gradesense.edu" in payload
    assert isinstance(res.rows[0]["CreatedAt"], str)
    assert isinstance(res.rows[0]["IsActive"], bool)


def test_live_row_limit_enforced(executor):
    """8. Verify row limit is enforced without fetching unbounded rows into Python."""
    # AttendanceRecords has 6000 rows
    res = executor.execute("SELECT * FROM AttendanceRecords;", max_rows=25)
    assert res.row_count == 25
    assert len(res.rows) == 25
    assert res.truncated is True


def test_live_mutating_query_rejected_before_db(executor):
    """9. Verify mutating query is rejected before hitting the database."""
    with pytest.raises(UnsafeQueryError):
        executor.execute("DELETE FROM Students WHERE Id = 9999;")


def test_live_multi_statement_rejected(executor):
    """10. Verify multi-statement execution is rejected before execution."""
    with pytest.raises(UnsafeQueryError):
        executor.execute("SELECT 1; SELECT 2;")


def test_live_database_row_count_integrity(executor):
    """11. Verify total database row count remains exactly 7,077 after all executions."""
    res = executor.execute("""
        SELECT SUM(p.rows) as TotalRows
        FROM sys.tables t
        INNER JOIN sys.partitions p ON t.object_id = p.object_id
        WHERE p.index_id IN (0, 1);
    """)
    assert res.rows[0]["TotalRows"] == 7077
