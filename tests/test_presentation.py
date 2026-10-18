"""Tests for the deterministic presentation layer.

Verifies that QueryResult and PipelineResult are converted into typed, clean,
user-facing representations without LLM calls, business inferences, or secret leaks.
"""

from decimal import Decimal
import datetime
import uuid
import pytest

from query_pilot.db.executor import QueryResult
from query_pilot.pipeline import PipelineResult, PipelineStatus
from query_pilot.presentation import (
    PresentationResult,
    escape_markdown_cell,
    extract_scalar_value,
    format_cell_value,
    format_markdown_table,
    format_pipeline_result,
    format_query_result,
    sanitize_message,
)
from query_pilot.sql.generation import ClarificationOption, SQLGenerationStatus
from query_pilot.sql.models import ValidationReason, ValidationReasonCode, ValidationResult


# ==============================================================================
# 1. Successful Query Results
# ==============================================================================

def test_successful_result_preserves_structure_and_order():
    """Verify columns, rows, order, counts, and execution time are preserved."""
    qr = QueryResult(
        columns=["StudentId", "FullName", "Email"],
        rows=[
            {"StudentId": 1, "FullName": "Aarav Patel", "Email": "aarav@example.com"},
            {"StudentId": 2, "FullName": "Diya Shah", "Email": "diya@example.com"},
        ],
        row_count=2,
        truncated=False,
        execution_time_ms=4.25,
    )

    pres = format_query_result(
        query_result=qr,
        question="Show all students",
        explanation="Selected from Students table",
        generated_sql="SELECT StudentId, FullName, Email FROM Students;",
    )

    assert pres.status == "success"
    assert pres.title == "Show all students"
    assert pres.columns == ["StudentId", "FullName", "Email"]
    assert len(pres.rows) == 2
    assert pres.rows[0]["StudentId"] == 1
    assert pres.rows[0]["FullName"] == "Aarav Patel"
    assert pres.rows[1]["StudentId"] == 2
    assert pres.rows[1]["FullName"] == "Diya Shah"
    assert pres.row_count == 2
    assert pres.truncated is False
    assert pres.execution_time_ms == 4.25
    assert pres.message == "Query executed successfully. 2 rows returned."
    assert pres.scalar_value is None
    assert pres.explanation == "Selected from Students table"
    assert pres.generated_sql == "SELECT StudentId, FullName, Email FROM Students;"
    assert pres.validation_reasons == []


def test_successful_single_row_message():
    """Verify singular row count message when 1 row is returned."""
    qr = QueryResult(
        columns=["StudentCount"],
        rows=[{"StudentCount": 42}],
        row_count=1,
        truncated=False,
        execution_time_ms=2.1,
    )
    pres = format_query_result(qr)
    assert pres.status == "success"
    assert pres.message == "Query executed successfully. 1 row returned."


# ==============================================================================
# 2. Empty Results
# ==============================================================================

def test_empty_result_preserves_columns():
    """Verify column names are preserved even when 0 rows are returned."""
    qr = QueryResult(
        columns=["StudentId", "FullName", "Major"],
        rows=[],
        row_count=0,
        truncated=False,
        execution_time_ms=1.5,
    )

    pres = format_query_result(qr, question="Find inactive students")

    assert pres.status == "success"
    assert pres.columns == ["StudentId", "FullName", "Major"]
    assert pres.rows == []
    assert pres.row_count == 0
    assert pres.truncated is False
    assert pres.message == "Query executed successfully. No rows returned."
    assert pres.scalar_value is None

    # Markdown table contains header and separator but no row data
    table = pres.markdown_table
    assert table is not None
    lines = table.strip().splitlines()
    assert len(lines) == 2
    assert lines[0] == "| StudentId | FullName | Major |"
    assert lines[1] == "|---|---|---|"


# ==============================================================================
# 3. Truncated Results
# ==============================================================================

def test_truncated_result_explicit_message():
    """Verify truncation is explicitly noted and total row count is not falsely claimed."""
    rows = [{"Id": i, "Val": f"val_{i}"} for i in range(1000)]
    qr = QueryResult(
        columns=["Id", "Val"],
        rows=rows,
        row_count=1000,
        truncated=True,
        execution_time_ms=18.4,
    )

    pres = format_query_result(qr)

    assert pres.status == "success"
    assert pres.truncated is True
    assert pres.row_count == 1000
    assert pres.message == "Query executed successfully. Showing the first 1000 rows; additional rows were not returned."
    # Total count of matching rows in table is NOT claimed to be 1000
    assert "total" not in pres.message.lower()


# ==============================================================================
# 4. Scalar Results
# ==============================================================================

def test_scalar_result_extracted():
    """Verify 1-row, 1-column results extract the deterministic scalar value."""
    qr = QueryResult(
        columns=["TotalCount"],
        rows=[{"TotalCount": 40}],
        row_count=1,
        truncated=False,
    )

    pres = format_query_result(qr)
    assert pres.scalar_value == 40
    assert extract_scalar_value(qr.columns, qr.rows) == 40


def test_scalar_result_multi_column_or_row_returns_none():
    """Verify scalar value is None for non-1x1 results."""
    # 1 row, 2 columns
    assert extract_scalar_value(["A", "B"], [{"A": 1, "B": 2}]) is None

    # 2 rows, 1 column
    assert extract_scalar_value(["A"], [{"A": 1}, {"A": 2}]) is None

    # 0 rows, 1 column
    assert extract_scalar_value(["A"], []) is None


# ==============================================================================
# 5. Markdown Table Formatting
# ==============================================================================

def test_markdown_table_formatting():
    """Verify Markdown table preserves order, escapes pipes/newlines, handles NULLs."""
    columns = ["Id", "Name", "Score", "Notes"]
    rows = [
        {"Id": 1, "Name": "Alice | Smith", "Score": 95.5, "Notes": "Line 1\nLine 2"},
        {"Id": 2, "Name": "Bob", "Score": 88, "Notes": None},
    ]

    table = format_markdown_table(columns, rows)
    lines = table.strip().splitlines()

    # Header
    assert lines[0] == "| Id | Name | Score | Notes |"
    # Separator: Id and Score are numeric (---:), Name and Notes are text (---)
    assert lines[1] == "|---:|---|---:|---|"
    # Row 1: pipe escaped, newline replaced with <br/>
    assert lines[2] == "| 1 | Alice \\| Smith | 95.5 | Line 1<br/>Line 2 |"
    # Row 2: None rendered as NULL
    assert lines[3] == "| 2 | Bob | 88 | NULL |"


def test_markdown_table_empty_columns():
    """Verify empty columns list returns empty string."""
    assert format_markdown_table([], []) == ""


# ==============================================================================
# 6. Data-Type Formatting
# ==============================================================================

def test_format_cell_values_deterministic():
    """Verify deterministic formatting for all supported Python/database types."""
    # None
    assert format_cell_value(None) == "NULL"

    # Booleans
    assert format_cell_value(True) == "true"
    assert format_cell_value(False) == "false"

    # Numbers
    assert format_cell_value(42) == "42"
    assert format_cell_value(3.14159) == "3.14159"
    assert format_cell_value(Decimal("99.99")) == "99.99"

    # Date / Time
    dt = datetime.datetime(2026, 10, 8, 14, 30, 0)
    assert format_cell_value(dt) == "2026-10-08T14:30:00"
    d = datetime.date(2026, 10, 8)
    assert format_cell_value(d) == "2026-10-08"
    t = datetime.time(14, 30, 0)
    assert format_cell_value(t) == "14:30:00"

    # UUID
    test_uid = uuid.UUID("12345678-1234-5678-1234-567812345678")
    assert format_cell_value(test_uid) == "12345678-1234-5678-1234-567812345678"

    # Bytes
    assert format_cell_value(b"\x01\x02\xab\xcd") == "0x0102abcd"
    assert format_cell_value(bytearray(b"\xde\xad")) == "0xdead"

    # Escape helper
    assert escape_markdown_cell("col | val") == "col \\| val"
    assert escape_markdown_cell("a\r\nb\nc") == "a<br/>b<br/>c"


# ==============================================================================
# 7. Unsupported Pipeline Result
# ==============================================================================

def test_unsupported_pipeline_result():
    """Verify PipelineStatus.UNSUPPORTED produces structured refusal without SQL."""
    pipe_res = PipelineResult(
        question="What are the teacher salaries?",
        status=PipelineStatus.UNSUPPORTED,
        generation_status=SQLGenerationStatus.UNSUPPORTED,
        generation_explanation="Salary information is not stored in the database schema.",
        generated_sql=None,
    )

    pres = format_pipeline_result(pipe_res)

    assert pres.status == "unsupported"
    assert pres.title == "What are the teacher salaries?"
    assert pres.message == "The database schema does not contain the information required to answer this question."
    assert pres.explanation == "Salary information is not stored in the database schema."
    assert pres.generated_sql is None
    assert pres.columns == []
    assert pres.rows == []
    assert pres.row_count == 0


# ==============================================================================
# 8. Validation-Rejected Pipeline Result
# ==============================================================================

def test_validation_rejected_pipeline_result():
    """Verify validation rejection preserves structured reasons and blocks execution."""
    val_reason = ValidationReason(
        code=ValidationReasonCode.FORBIDDEN_OPERATION,
        message="DELETE statement is forbidden.",
        location="Line 1",
    )
    val_res = ValidationResult(
        allowed=False,
        normalized_sql=None,
        reasons=[val_reason],
    )

    pipe_res = PipelineResult(
        question="Delete all students",
        status=PipelineStatus.VALIDATION_REJECTED,
        generated_sql="DELETE FROM Students;",
        validation_result=val_res,
    )

    pres = format_pipeline_result(pipe_res)

    assert pres.status == "validation_rejected"
    assert pres.message == "Query rejected by the SQL safety/policy validator."
    assert pres.generated_sql == "DELETE FROM Students;"
    assert len(pres.validation_reasons) == 1
    assert pres.validation_reasons[0]["code"] == "FORBIDDEN_OPERATION"
    assert pres.validation_reasons[0]["message"] == "DELETE statement is forbidden."
    assert pres.columns == []
    assert pres.rows == []


# ==============================================================================
# 9. Failure Pipeline Results
# ==============================================================================

def test_generation_failed_pipeline_result():
    """Verify generation failure returns clean message without leaking internals."""
    pipe_res = PipelineResult(
        question="How many users?",
        status=PipelineStatus.GENERATION_FAILED,
        error_message="Gemini API rate limit exceeded.",
    )

    pres = format_pipeline_result(pipe_res)

    assert pres.status == "generation_failed"
    assert pres.message == "Gemini API rate limit exceeded."
    assert pres.generated_sql is None


def test_execution_failed_pipeline_result():
    """Verify execution failure returns safe database error representation."""
    pipe_res = PipelineResult(
        question="Find student grades",
        status=PipelineStatus.EXECUTION_FAILED,
        generated_sql="SELECT InvalidCol FROM Students;",
        error_message="Invalid column name 'InvalidCol'.",
    )

    pres = format_pipeline_result(pipe_res)

    assert pres.status == "execution_failed"
    assert pres.message == "Invalid column name 'InvalidCol'."
    assert pres.generated_sql == "SELECT InvalidCol FROM Students;"


# ==============================================================================
# 10. Security Sanitization
# ==============================================================================

def test_sanitize_message_credentials_and_tokens():
    """Verify credentials, connection strings, API keys, and stack traces are redacted."""
    # Database connection string with password
    raw_conn_err = "Login failed: Server=127.0.0.1;Database=GradeSense_Local;UID=sa;PWD=SuperSecretPassword123!;"
    cleaned = sanitize_message(raw_conn_err)
    assert "SuperSecretPassword123!" not in cleaned
    assert "[REDACTED]" in cleaned

    # Gemini API Key pattern
    raw_key_err = "API error: AIzaSyD1234567890abcdefghijklmnopqrstuv"
    cleaned_key = sanitize_message(raw_key_err)
    assert "AIzaSy" not in cleaned_key
    assert "[REDACTED]" in cleaned_key

    # Bearer token
    raw_bearer = "Unauthorized: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.token"
    cleaned_bearer = sanitize_message(raw_bearer)
    assert "eyJhbGciOi" not in cleaned_bearer
    assert "[REDACTED]" in cleaned_bearer

    # Full Python exception traceback is stripped to summary
    raw_traceback = (
        "Traceback (most recent call last):\n"
        '  File "app/main.py", line 42, in run\n'
        '    raise ValueError("Internal failure details")\n'
        'ValueError: Internal failure details'
    )
    cleaned_tb = sanitize_message(raw_traceback)
    assert "Traceback" not in cleaned_tb
    assert 'File "app/main.py"' not in cleaned_tb
    assert cleaned_tb == "ValueError: Internal failure details"


def test_presentation_does_not_leak_password_hash():
    """Verify PasswordHash or confidential columns are not exposed by the presentation layer."""
    qr = QueryResult(
        columns=["UserId", "Username"],
        rows=[{"UserId": 1, "Username": "instructor_admin"}],
        row_count=1,
        truncated=False,
    )
    pres = format_query_result(qr)
    dumped = str(pres.to_dict())
    assert "PasswordHash" not in dumped


# ==============================================================================
# 11. Pipeline Integration (.present() method)
# ==============================================================================

def test_pipeline_result_present_convenience_method():
    """Verify PipelineResult.present() delegates cleanly to format_pipeline_result."""
    qr = QueryResult(
        columns=["Count"],
        rows=[{"Count": 5}],
        row_count=1,
        truncated=False,
    )
    pipe_res = PipelineResult(
        question="How many courses?",
        status=PipelineStatus.SUCCESS,
        generated_sql="SELECT COUNT(*) AS Count FROM Courses;",
        execution_result=qr,
    )

    pres = pipe_res.present()

    assert isinstance(pres, PresentationResult)
    assert pres.status == "success"
    assert pres.scalar_value == 5
    assert pres.columns == ["Count"]
    assert pres.rows == [{"Count": 5}]


# ==============================================================================
# 12. Clarification Presentation
# ==============================================================================

def test_presentation_clarification_with_prompt_and_options():
    """Verify presentation includes notice, prompt, and formatted bulleted options."""
    options = [
        ClarificationOption(
            option_id="opt_act",
            description="Active students only",
            suggested_question="Show active enrolled students",
        ),
        ClarificationOption(
            option_id="opt_all",
            description="All students including alumni",
        ),
    ]
    pipe_res = PipelineResult(
        question="Show enrolled students",
        status=PipelineStatus.CLARIFICATION_NEEDED,
        clarification_prompt="Did you mean active students or all historical students?",
        clarification_options=options,
        explanation="Student enrollment status filter is ambiguous.",
    )

    pres = format_pipeline_result(pipe_res)

    assert pres.status == "clarification_needed"
    assert pres.title == "Show enrolled students"
    assert pres.clarification_prompt == "Did you mean active students or all historical students?"
    assert len(pres.clarification_options) == 2
    assert pres.columns == []
    assert pres.rows == []
    assert pres.row_count == 0
    assert pres.generated_sql is None

    # Message text verification
    assert "Additional information is required to process this question." in pres.message
    assert "Did you mean active students or all historical students?" in pres.message
    assert "Available options:" in pres.message
    assert "- [opt_act] Active students only (Suggested: Show active enrolled students)" in pres.message
    assert "- [opt_all] All students including alumni" in pres.message

    # Safety: zero SQL and zero execution success wording
    assert "SELECT" not in pres.message
    assert "success" not in pres.message.lower()
    assert "rows returned" not in pres.message.lower()


def test_presentation_clarification_explanation_fallback_when_prompt_absent():
    """Verify presentation falls back to explanation when prompt is absent without inventing a question."""
    pipe_res = PipelineResult(
        question="Show classes",
        status=PipelineStatus.CLARIFICATION_NEEDED,
        clarification_prompt=None,
        clarification_options=[],
        generation_explanation="Please specify whether you mean spring or fall semester.",
    )

    pres = format_pipeline_result(pipe_res)

    assert pres.status == "clarification_needed"
    assert pres.clarification_prompt is None
    assert "Please specify whether you mean spring or fall semester." in pres.message
    assert pres.generated_sql is None
    assert pres.rows == []
    assert pres.row_count == 0


def test_pipeline_result_rejects_sql_for_clarification():
    """Verify PipelineResult validator rejects non-None generated_sql for CLARIFICATION_NEEDED."""
    with pytest.raises(Exception, match="Clarification result must not contain generated_sql"):
        PipelineResult(
            question="Ambiguous query",
            status=PipelineStatus.CLARIFICATION_NEEDED,
            generated_sql="SELECT * FROM SensitiveUsers;",
            clarification_prompt="Which role?",
            generation_explanation="Role ambiguous.",
        )


def test_presentation_clarification_does_not_expose_sql_or_imply_success():
    """Verify presentation strips any accidental SQL and never implies query ran."""
    from unittest.mock import MagicMock

    mock_res = MagicMock()
    mock_res.status = PipelineStatus.CLARIFICATION_NEEDED
    mock_res.question = "Ambiguous query"
    mock_res.generated_sql = "SELECT * FROM SensitiveUsers;"
    mock_res.clarification_prompt = "Which role?"
    mock_res.clarification_options = []
    mock_res.generation_explanation = "Role ambiguous."
    mock_res.request_id = None
    mock_res.grounded_answer = None
    mock_res.telemetry = None

    pres = format_pipeline_result(mock_res)

    assert pres.status == "clarification_needed"
    assert pres.generated_sql is None
    assert "SensitiveUsers" not in pres.message
    assert "SELECT" not in pres.message
    assert "executed" not in pres.message.lower()
    assert "success" not in pres.message.lower()


def test_presentation_clarification_whitespace_prompt_fallback():
    """Verify whitespace-only prompt is treated as absent and falls back to explanation."""
    pipe_res = PipelineResult(
        question="Show departments",
        status=PipelineStatus.CLARIFICATION_NEEDED,
        clarification_prompt="   \t\n   ",
        clarification_options=[],
        generation_explanation="Department categorization is ambiguous.",
    )

    pres = format_pipeline_result(pipe_res)

    assert pres.status == "clarification_needed"
    assert pres.clarification_prompt is None
    assert "Department categorization is ambiguous." in pres.message
    assert pres.generated_sql is None


def test_presentation_clarification_options_order_preservation():
    """Verify clarification options are formatted deterministically in the exact provided order."""
    options = [
        ClarificationOption(option_id="opt_z", description="Zebra department"),
        ClarificationOption(option_id="opt_a", description="Alpha department"),
        ClarificationOption(option_id="opt_m", description="Middle department"),
    ]
    pipe_res = PipelineResult(
        question="Which department?",
        status=PipelineStatus.CLARIFICATION_NEEDED,
        clarification_prompt="Choose department:",
        clarification_options=options,
        generation_explanation="Ambiguous department.",
    )

    pres = format_pipeline_result(pipe_res)

    assert pres.status == "clarification_needed"
    z_pos = pres.message.find("- [opt_z]")
    a_pos = pres.message.find("- [opt_a]")
    m_pos = pres.message.find("- [opt_m]")

    assert z_pos != -1
    assert a_pos != -1
    assert m_pos != -1
    assert z_pos < a_pos < m_pos
