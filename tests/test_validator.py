"""Unit and integration tests for deterministic SQL validation and policy layer."""

import pytest
from query_pilot.config import DatabaseSettings
from query_pilot.db.connection import create_db_engine
from query_pilot.db.executor import execute_read_only
from query_pilot.db.introspection import DatabaseIntrospector
from query_pilot.db.models import ColumnMetadata, DatabaseSchema, TableMetadata
from query_pilot.sql.models import ValidationReasonCode, ValidationResult
from query_pilot.sql.validator import SQLValidator, validate_sql


# ==============================================================================
# In-Memory Mock Schema Fixture (Pure Unit Testing — Zero DB Required)
# ==============================================================================

@pytest.fixture(scope="module")
def mock_schema() -> DatabaseSchema:
    """Provides a deterministic in-memory DatabaseSchema for pure unit testing."""
    users_table = TableMetadata(
        schema_name="dbo",
        table_name="Users",
        columns=[
            ColumnMetadata(name="Id", data_type="int", nullable=False, primary_key=True),
            ColumnMetadata(name="Email", data_type="varchar(255)", nullable=False),
            ColumnMetadata(name="FullName", data_type="varchar(255)", nullable=False),
            ColumnMetadata(name="Role", data_type="varchar(50)", nullable=False),
            ColumnMetadata(name="IsActive", data_type="bit", nullable=False),
            # PasswordHash is deliberately omitted (model-facing representation)
        ],
    )

    students_table = TableMetadata(
        schema_name="dbo",
        table_name="Students",
        columns=[
            ColumnMetadata(name="Id", data_type="int", nullable=False, primary_key=True),
            ColumnMetadata(name="StudentId", data_type="int", nullable=False),
            ColumnMetadata(name="EnrollmentNumber", data_type="varchar(50)", nullable=False),
            ColumnMetadata(name="FullName", data_type="varchar(255)", nullable=True),
            ColumnMetadata(name="DepartmentId", data_type="int", nullable=False),
            ColumnMetadata(name="Status", data_type="varchar(50)", nullable=False),
        ],
    )

    enrollments_table = TableMetadata(
        schema_name="dbo",
        table_name="CourseEnrollments",
        columns=[
            ColumnMetadata(name="Id", data_type="int", nullable=False, primary_key=True),
            ColumnMetadata(name="StudentId", data_type="int", nullable=False),
            ColumnMetadata(name="CourseOfferingId", data_type="int", nullable=False),
        ],
    )

    return DatabaseSchema(
        database_name="GradeSense_Local",
        tables=[users_table, students_table, enrollments_table],
        is_model_facing=True,
    )


# ==============================================================================
# Live Model-Facing Schema Fixture (GradeSense_Local)
# ==============================================================================

@pytest.fixture(scope="module")
def live_model_facing_schema() -> DatabaseSchema:
    """Provides introspected model-facing schema directly from GradeSense_Local."""
    engine = create_db_engine()
    introspector = DatabaseIntrospector(engine)
    return introspector.introspect_model_facing()


# ==============================================================================
# Allowed Query Tests
# ==============================================================================

@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM Students;",
        "SELECT StudentId, FullName FROM Students WHERE StudentId = 1;",
        "SELECT s.FullName, c.CourseOfferingId FROM Students AS s JOIN CourseEnrollments AS c ON c.StudentId = s.StudentId;",
        "WITH x AS (SELECT StudentId FROM Students) SELECT * FROM x;",
        "SELECT StudentId FROM Students UNION SELECT StudentId FROM Students;",
        "SELECT COUNT(*) FROM Students;",
        "SELECT DepartmentId, COUNT(*) FROM Students GROUP BY DepartmentId;",
        "SELECT s.StudentId AS MyId FROM Students s ORDER BY MyId;",
        "SELECT s.* FROM Students AS s;",
        "SELECT TOP 500 * FROM Students;",
        "SELECT * FROM dbo.Students;",
        "SELECT * FROM [dbo].[Students];",
        "SELECT * FROM [Students];",
        "SELECT s.Id, u.FullName FROM Students s JOIN Users u ON s.Id = u.Id;",
    ],
)
def test_validate_sql_allowed_queries(mock_schema, sql):
    """Verify that legitimate read-only queries with known tables/columns are allowed."""
    res = validate_sql(sql, mock_schema, max_rows=1000)
    assert res.allowed is True, f"Query was rejected unexpectedly: {res.reasons}"
    assert len(res.reasons) == 0
    assert res.normalized_sql is not None
    assert len(res.referenced_tables) > 0


# ==============================================================================
# Empty & Syntax Error Rejections
# ==============================================================================

@pytest.mark.parametrize("sql", ["", "   ", "\n\t", "   \r\n  "])
def test_validate_sql_empty_queries(mock_schema, sql):
    """Verify empty or whitespace-only SQL is rejected with EMPTY_SQL."""
    res = validate_sql(sql, mock_schema)
    assert res.allowed is False
    assert ValidationReasonCode.EMPTY_SQL in res.reason_codes


@pytest.mark.parametrize(
    "sql",
    [
        "INVALID SQL STATEMENT !!!",
        "SELECT FROM WHERE;",
        "SELECT * FROM ;",
        "SELECT s. FROM Students s;",
    ],
)
def test_validate_sql_syntax_errors(mock_schema, sql):
    """Verify unparseable SQL fails closed with PARSE_ERROR."""
    res = validate_sql(sql, mock_schema)
    assert res.allowed is False
    assert ValidationReasonCode.PARSE_ERROR in res.reason_codes


# ==============================================================================
# Multi-Statement Rejections
# ==============================================================================

@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM Students; SELECT * FROM Users;",
        "SELECT 1; SELECT 2;",
        "SELECT * FROM Students; DELETE FROM Students;",
        "SET NOCOUNT ON; SELECT * FROM Students;",
    ],
)
def test_validate_sql_multi_statement(mock_schema, sql):
    """Verify multi-statement queries are rejected with MULTI_STATEMENT."""
    res = validate_sql(sql, mock_schema)
    assert res.allowed is False
    assert ValidationReasonCode.MULTI_STATEMENT in res.reason_codes


# ==============================================================================
# Mutating & Administrative Statement Rejections
# ==============================================================================

@pytest.mark.parametrize(
    "sql,expected_code",
    [
        ("INSERT INTO Students (StudentId) VALUES (1);", ValidationReasonCode.NOT_READ_ONLY),
        ("UPDATE Students SET FullName = 'x';", ValidationReasonCode.NOT_READ_ONLY),
        ("DELETE FROM Students;", ValidationReasonCode.NOT_READ_ONLY),
        ("MERGE Target AS T USING Source AS S ON (T.Id = S.Id) WHEN MATCHED THEN UPDATE SET T.Val = S.Val;", ValidationReasonCode.NOT_READ_ONLY),
        ("DROP TABLE Students;", ValidationReasonCode.NOT_READ_ONLY),
        ("ALTER TABLE Students ADD x int;", ValidationReasonCode.NOT_READ_ONLY),
        ("CREATE TABLE test (id int);", ValidationReasonCode.NOT_READ_ONLY),
        ("TRUNCATE TABLE Students;", ValidationReasonCode.NOT_READ_ONLY),
        ("EXEC sp_help;", ValidationReasonCode.NOT_READ_ONLY),
        ("EXECUTE sp_who;", ValidationReasonCode.NOT_READ_ONLY),
        ("GRANT SELECT ON Students TO user1;", ValidationReasonCode.NOT_READ_ONLY),
        ("REVOKE SELECT ON Students FROM user1;", ValidationReasonCode.NOT_READ_ONLY),
        ("SELECT * INTO BackupStudents FROM Students;", ValidationReasonCode.FORBIDDEN_OPERATION),
        ("BEGIN TRANSACTION; SELECT * FROM Students; COMMIT;", ValidationReasonCode.MULTI_STATEMENT),
    ],
)
def test_validate_sql_mutating_queries(mock_schema, sql, expected_code):
    """Verify that all mutating operations are rejected with read-only/forbidden codes."""
    res = validate_sql(sql, mock_schema)
    assert res.allowed is False
    assert expected_code in res.reason_codes or ValidationReasonCode.NOT_READ_ONLY in res.reason_codes


# ==============================================================================
# Unknown Table & Catalog Rejections
# ==============================================================================

def test_validate_sql_unknown_table(mock_schema):
    """Verify unknown tables trigger UNKNOWN_TABLE rejection."""
    res = validate_sql("SELECT * FROM DoesNotExist;", mock_schema)
    assert res.allowed is False
    assert ValidationReasonCode.UNKNOWN_TABLE in res.reason_codes
    assert "DoesNotExist" in res.reasons[0].message


def test_validate_sql_forbidden_catalog(mock_schema):
    """Verify foreign database catalogs trigger FORBIDDEN_TABLE rejection."""
    res = validate_sql("SELECT * FROM ExternalDB.dbo.Students;", mock_schema)
    assert res.allowed is False
    assert ValidationReasonCode.FORBIDDEN_TABLE in res.reason_codes


# ==============================================================================
# Unknown & Sensitive Column Rejections
# ==============================================================================

def test_validate_sql_unknown_column_unqualified(mock_schema):
    """Verify unknown column in unqualified query triggers UNKNOWN_COLUMN."""
    res = validate_sql("SELECT FakeColumn FROM Students;", mock_schema)
    assert res.allowed is False
    assert ValidationReasonCode.UNKNOWN_COLUMN in res.reason_codes
    assert "FakeColumn" in res.reasons[0].message


def test_validate_sql_unknown_column_qualified(mock_schema):
    """Verify unknown column on specific table triggers UNKNOWN_COLUMN."""
    res = validate_sql("SELECT s.NonExistentField FROM Students AS s;", mock_schema)
    assert res.allowed is False
    assert ValidationReasonCode.UNKNOWN_COLUMN in res.reason_codes


def test_validate_sql_sensitive_column_unqualified(mock_schema):
    """Verify sensitive column (PasswordHash) is rejected with SENSITIVE_COLUMN."""
    res = validate_sql("SELECT PasswordHash FROM Users;", mock_schema)
    assert res.allowed is False
    assert ValidationReasonCode.SENSITIVE_COLUMN in res.reason_codes
    assert "PasswordHash" in res.reasons[0].message


def test_validate_sql_sensitive_column_qualified(mock_schema):
    """Verify qualified sensitive column (u.PasswordHash) is rejected with SENSITIVE_COLUMN."""
    res = validate_sql("SELECT u.PasswordHash FROM Users AS u;", mock_schema)
    assert res.allowed is False
    assert ValidationReasonCode.SENSITIVE_COLUMN in res.reason_codes


def test_validate_sql_sensitive_column_case_insensitive(mock_schema):
    """Verify sensitive column detection is case-insensitive."""
    for variant in ["passwordhash", "PASSWORDHASH", "password", "token", "secret"]:
        res = validate_sql(f"SELECT {variant} FROM Users;", mock_schema)
        assert res.allowed is False
        assert ValidationReasonCode.SENSITIVE_COLUMN in res.reason_codes


# ==============================================================================
# Keyword Immunity in String Literals & Comments
# ==============================================================================

@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM Students WHERE Status = 'DELETE';",
        "SELECT * FROM Students WHERE Status = 'DROP TABLE Students';",
        "SELECT * FROM Students WHERE Status = 'UPDATE Students SET x = 1';",
        "SELECT * /* DROP TABLE Students */ FROM Students;",
        "SELECT * -- DELETE FROM Students\nFROM Students;",
        "SELECT * FROM Students WHERE FullName = 'DELETE FROM Students';",
    ],
)
def test_validate_sql_keyword_immunity(mock_schema, sql):
    """Verify mutating keywords inside string literals or comments do NOT trigger rejection."""
    res = validate_sql(sql, mock_schema)
    assert res.allowed is True, f"Query falsely rejected: {res.reasons}"


# ==============================================================================
# Explicit Row Limit Policy Tests
# ==============================================================================

def test_validate_sql_limit_within_bounds(mock_schema):
    """Verify TOP 500 is allowed when max_rows is 1000."""
    res = validate_sql("SELECT TOP 500 * FROM Students;", mock_schema, max_rows=1000)
    assert res.allowed is True


def test_validate_sql_limit_exceeds_bounds(mock_schema):
    """Verify TOP 5000 is rejected when max_rows is 1000."""
    res = validate_sql("SELECT TOP 5000 * FROM Students;", mock_schema, max_rows=1000)
    assert res.allowed is False
    assert ValidationReasonCode.EXCEEDS_MAX_ROWS in res.reason_codes


def test_validate_sql_fetch_next_exceeds_bounds(mock_schema):
    """Verify FETCH NEXT 2000 ROWS is rejected when max_rows is 1000."""
    sql = "SELECT * FROM Students ORDER BY Id OFFSET 0 ROWS FETCH NEXT 2000 ROWS ONLY;"
    res = validate_sql(sql, mock_schema, max_rows=1000)
    assert res.allowed is False
    assert ValidationReasonCode.EXCEEDS_MAX_ROWS in res.reason_codes


def test_validate_sql_percentage_limit_rejected(mock_schema):
    """Verify TOP PERCENT is rejected with UNSUPPORTED_QUERY_SHAPE."""
    res = validate_sql("SELECT TOP 10 PERCENT * FROM Students;", mock_schema)
    assert res.allowed is False
    assert ValidationReasonCode.UNSUPPORTED_QUERY_SHAPE in res.reason_codes


# ==============================================================================
# Determinism & Result Serialization Tests
# ==============================================================================

def test_validate_sql_determinism(mock_schema):
    """Verify that validating the same SQL multiple times produces identical results."""
    sql = "SELECT s.StudentId, s.FullName FROM Students AS s WHERE s.DepartmentId = 1;"
    res1 = validate_sql(sql, mock_schema)
    res2 = validate_sql(sql, mock_schema)

    assert res1.allowed == res2.allowed
    assert res1.normalized_sql == res2.normalized_sql
    assert res1.referenced_tables == res2.referenced_tables
    assert res1.referenced_columns == res2.referenced_columns
    assert res1.reason_codes == res2.reason_codes

    d = res1.to_dict()
    assert d["allowed"] is True
    assert d["referenced_tables"] == ["Students"]


# ==============================================================================
# Integration with Real Model-Facing Schema (GradeSense_Local) & Executor
# ==============================================================================

def test_live_schema_validation_and_execution(live_model_facing_schema):
    """Verify end-to-end pipeline: validate against live schema -> execute on GradeSense_Local."""
    # 1. Valid Query against real GradeSense_Local schema
    valid_sql = "SELECT TOP 3 Id, EnrollmentNumber, DepartmentId FROM Students WHERE Status = 'Active';"
    val_res = validate_sql(valid_sql, live_model_facing_schema, max_rows=100)
    assert val_res.allowed is True
    assert "Students" in val_res.referenced_tables

    # Safe to execute after validation
    exec_res = execute_read_only(val_res.normalized_sql)
    assert exec_res.row_count == 3
    assert exec_res.columns == ["Id", "EnrollmentNumber", "DepartmentId"]

    # 2. Sensitive column rejected during validation (never reaches executor)
    sensitive_sql = "SELECT Id, PasswordHash FROM Users;"
    val_sensitive = validate_sql(sensitive_sql, live_model_facing_schema)
    assert val_sensitive.allowed is False
    assert ValidationReasonCode.SENSITIVE_COLUMN in val_sensitive.reason_codes
