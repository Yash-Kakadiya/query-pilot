"""Unit tests for SQL generation contract, schema context formatting, and prompt building."""

import json
from typing import List, Optional
import pytest
from pydantic import ValidationError

from query_pilot.db.models import ColumnMetadata, DatabaseSchema, ForeignKeyMetadata, TableMetadata
from query_pilot.sql.gemini_generator import GeminiSQLGenerator
from query_pilot.sql.generation import (
    ConfigurationError,
    EmptySQLError,
    MalformedResponseError,
    ProviderAPIError,
    SQLGenerationError,
    SQLGenerationRequest,
    SQLGenerationResponse,
    SQLGenerationStatus,
    SQLGenerator,
    format_schema_context,
)
from query_pilot.sql.prompt import SYSTEM_INSTRUCTION, build_sql_generation_prompt


# ==============================================================================
# In-Memory Mock Schema Fixture (Zero DB Connection Required)
# ==============================================================================

@pytest.fixture
def mock_model_facing_schema() -> DatabaseSchema:
    """Provides a deterministic in-memory model-facing schema for testing."""
    users_table = TableMetadata(
        schema_name="dbo",
        table_name="Users",
        columns=[
            ColumnMetadata(name="Id", data_type="int", nullable=False, primary_key=True),
            ColumnMetadata(name="Email", data_type="varchar(255)", nullable=False),
            ColumnMetadata(name="FullName", data_type="varchar(255)", nullable=False),
            ColumnMetadata(name="Role", data_type="varchar(50)", nullable=False),
            # Notice PasswordHash is absent (redacted from model-facing schema)
        ],
        primary_keys=["Id"],
    )

    students_table = TableMetadata(
        schema_name="dbo",
        table_name="Students",
        columns=[
            ColumnMetadata(name="Id", data_type="int", nullable=False, primary_key=True),
            ColumnMetadata(name="DepartmentId", data_type="int", nullable=False),
            ColumnMetadata(name="EnrollmentNumber", data_type="varchar(50)", nullable=False),
            ColumnMetadata(name="Status", data_type="varchar(50)", nullable=False),
        ],
        primary_keys=["Id"],
        foreign_keys=[
            ForeignKeyMetadata(
                name="FK_Students_Users",
                source_table="Students",
                source_column="Id",
                target_table="Users",
                target_column="Id",
            )
        ],
    )

    return DatabaseSchema(
        database_name="TestDB",
        tables=[users_table, students_table],
        is_model_facing=True,
    )


# ==============================================================================
# Deterministic Fake Provider Implementation
# ==============================================================================

class FakeSQLGenerator(SQLGenerator):
    """Deterministic fake generator for testing provider contract without network calls."""

    def __init__(self, response_sql: Optional[str] = None):
        self.response_sql = response_sql or (
            "SELECT TOP 5 s.Id, u.FullName "
            "FROM Students AS s "
            "JOIN Users AS u ON s.Id = u.Id "
            "ORDER BY s.Id;"
        )
        self.generate_called = False
        self.last_request: Optional[SQLGenerationRequest] = None

    def generate(self, request: SQLGenerationRequest) -> SQLGenerationResponse:
        self.generate_called = True
        self.last_request = request
        return SQLGenerationResponse(
            sql=self.response_sql,
            explanation="Selected top 5 students joined with user full names.",
            assumptions=["Students.Id maps to Users.Id."],
        )


# ==============================================================================
# Schema Context Formatter Tests
# ==============================================================================

def test_format_schema_context_contents(mock_model_facing_schema):
    """Verify that format_schema_context formats tables, columns, and foreign keys."""
    text = format_schema_context(mock_model_facing_schema, dialect="T-SQL")

    # Dialect and DB name
    assert "DATABASE DIALECT: T-SQL" in text
    assert "DATABASE NAME: TestDB" in text

    # Tables and columns
    assert "TABLE [Users]:" in text
    assert "- [FullName] (varchar(255)" in text
    assert "TABLE [Students]:" in text
    assert "- [EnrollmentNumber] (varchar(50)" in text
    assert "(PRIMARY KEY)" in text

    # Foreign-key relationships
    assert "RELATIONSHIPS (FOREIGN KEYS):" in text
    assert "[Students].[Id] -> [Users].[Id]" in text

    # Sensitive column must be completely absent
    assert "PasswordHash" not in text

    # Row data must never be present
    assert "admin@gradesense.edu" not in text


def test_format_schema_context_determinism(mock_model_facing_schema):
    """Verify that multiple formatting runs produce exactly identical text."""
    text1 = format_schema_context(mock_model_facing_schema)
    text2 = format_schema_context(mock_model_facing_schema)
    assert text1 == text2


def test_format_schema_context_arbitrary_tables():
    """Verify that format_schema_context works for any generic schema without hardcoding."""
    custom_schema = DatabaseSchema(
        database_name="ECommerce",
        tables=[
            TableMetadata(
                schema_name="dbo",
                table_name="Orders",
                columns=[
                    ColumnMetadata(name="OrderId", data_type="int", nullable=False, primary_key=True),
                    ColumnMetadata(name="Amount", data_type="decimal(10,2)", nullable=False),
                ],
                primary_keys=["OrderId"],
            )
        ],
        is_model_facing=True,
    )
    text = format_schema_context(custom_schema, dialect="T-SQL")
    assert "TABLE [Orders]:" in text
    assert "- [OrderId] (int" in text
    assert "- [Amount] (decimal(10,2)" in text


# ==============================================================================
# SQLGenerationRequest & Response Model Tests
# ==============================================================================

def test_request_requires_model_facing_schema(mock_model_facing_schema):
    """Verify that requests reject raw schemas (is_model_facing=False)."""
    raw_schema = DatabaseSchema(
        database_name="TestDB",
        tables=mock_model_facing_schema.tables,
        is_model_facing=False,  # Not marked model-facing
    )
    with pytest.raises(ValidationError) as exc_info:
        SQLGenerationRequest(
            question="List students",
            schema_context=raw_schema,
        )
    assert "Security boundary violation" in str(exc_info.value)


@pytest.mark.parametrize("empty_q", ["", "   ", "\t\n"])
def test_request_rejects_empty_question(mock_model_facing_schema, empty_q):
    """Verify that requests reject empty questions."""
    with pytest.raises(ValidationError):
        SQLGenerationRequest(
            question=empty_q,
            schema_context=mock_model_facing_schema,
        )


def test_response_valid_sql_and_fields():
    """Verify that valid responses parse and strip code blocks if present."""
    # Plain SQL
    resp1 = SQLGenerationResponse(
        sql="SELECT * FROM Students;",
        explanation="Lists all students.",
        assumptions=["None"],
    )
    assert resp1.sql == "SELECT * FROM Students;"
    assert resp1.explanation == "Lists all students."
    assert resp1.assumptions == ["None"]

    # Markdown code fence stripping
    resp2 = SQLGenerationResponse(
        sql="```sql\nSELECT * FROM Students;\n```",
    )
    assert resp2.sql == "SELECT * FROM Students;"


@pytest.mark.parametrize("empty_sql", ["", "   ", "```sql\n   \n```"])
def test_response_rejects_empty_sql(empty_sql):
    """Verify that empty or whitespace SQL is rejected."""
    with pytest.raises(ValidationError):
        SQLGenerationResponse(sql=empty_sql)


def test_response_answerable_valid():
    """Verify answerable response with non-empty SQL."""
    resp = SQLGenerationResponse(
        status=SQLGenerationStatus.ANSWERABLE,
        sql="SELECT COUNT(*) FROM Students;",
        explanation="Counts all students.",
        assumptions=["Active students only"],
    )
    assert resp.status == SQLGenerationStatus.ANSWERABLE
    assert resp.sql == "SELECT COUNT(*) FROM Students;"
    assert resp.explanation == "Counts all students."
    assert resp.assumptions == ["Active students only"]


def test_response_unsupported_valid():
    """Verify unsupported response with None sql."""
    resp = SQLGenerationResponse(
        status=SQLGenerationStatus.UNSUPPORTED,
        sql=None,
        explanation="Tuition fees are not stored in the database.",
        assumptions=[],
    )
    assert resp.status == SQLGenerationStatus.UNSUPPORTED
    assert resp.sql is None
    assert "Tuition fees" in resp.explanation
    assert resp.assumptions == []


def test_response_rejects_unsupported_with_sql():
    """Verify unsupported response rejects non-null SQL."""
    with pytest.raises(ValidationError) as exc_info:
        SQLGenerationResponse(
            status=SQLGenerationStatus.UNSUPPORTED,
            sql="SELECT * FROM Students;",
        )
    assert "Unsupported response must not contain SQL" in str(exc_info.value)


def test_response_rejects_answerable_with_none_or_empty_sql():
    """Verify answerable response rejects None or empty SQL."""
    with pytest.raises(ValidationError) as exc_info1:
        SQLGenerationResponse(
            status=SQLGenerationStatus.ANSWERABLE,
            sql=None,
        )
    assert "Answerable response requires a non-empty SQL query" in str(exc_info1.value)

    with pytest.raises(ValidationError) as exc_info2:
        SQLGenerationResponse(
            status=SQLGenerationStatus.ANSWERABLE,
            sql="",
        )
    assert "Answerable response requires a non-empty SQL query" in str(exc_info2.value)

    with pytest.raises(ValidationError) as exc_info3:
        SQLGenerationResponse(
            status=SQLGenerationStatus.ANSWERABLE,
            sql="   ",
        )
    assert "Answerable response requires a non-empty SQL query" in str(exc_info3.value)


def test_response_serialization_deserialization():
    """Verify JSON round-trip serialization and deserialization for both statuses."""
    # Answerable round-trip
    ans_orig = SQLGenerationResponse(
        status=SQLGenerationStatus.ANSWERABLE,
        sql="SELECT * FROM Departments;",
        explanation="Departments list",
        assumptions=["All departments"],
    )
    ans_json = ans_orig.model_dump_json()
    ans_data = json.loads(ans_json)
    assert ans_data["status"] == "answerable"
    assert ans_data["sql"] == "SELECT * FROM Departments;"
    ans_restored = SQLGenerationResponse.model_validate_json(ans_json)
    assert ans_restored == ans_orig

    # Unsupported round-trip
    unsupp_orig = SQLGenerationResponse(
        status=SQLGenerationStatus.UNSUPPORTED,
        sql=None,
        explanation="Data not available in schema.",
        assumptions=[],
    )
    unsupp_json = unsupp_orig.model_dump_json()
    unsupp_data = json.loads(unsupp_json)
    assert unsupp_data["status"] == "unsupported"
    assert unsupp_data["sql"] is None
    unsupp_restored = SQLGenerationResponse.model_validate_json(unsupp_json)
    assert unsupp_restored == unsupp_orig


# ==============================================================================
# Prompt Construction Tests
# ==============================================================================

def test_build_sql_generation_prompt(mock_model_facing_schema):
    """Verify prompt builder includes question, dialect, schema, and instructions."""
    req = SQLGenerationRequest(
        question="Which students have enrolled?",
        schema_context=mock_model_facing_schema,
        dialect="T-SQL",
        instructions="Limit results to 10 rows.",
    )
    prompt = build_sql_generation_prompt(req)

    assert "Which students have enrolled?" in prompt
    assert "DATABASE DIALECT: T-SQL" in prompt
    assert "TABLE [Students]:" in prompt
    assert "Limit results to 10 rows." in prompt

    # Verify system instruction contains critical constraints
    assert "Target Dialect: You are generating SQL for Microsoft SQL Server (T-SQL)" in SYSTEM_INSTRUCTION
    assert "Read-Only Only" in SYSTEM_INSTRUCTION
    assert "Grounding in Schema" in SYSTEM_INSTRUCTION
    assert "No Execution" in SYSTEM_INSTRUCTION


# ==============================================================================
# Provider Protocol & Fake Generator Tests
# ==============================================================================

def test_fake_generator_protocol_conformance(mock_model_facing_schema):
    """Verify FakeSQLGenerator adheres to SQLGenerator protocol without executing SQL."""
    fake = FakeSQLGenerator()
    assert isinstance(fake, SQLGenerator)

    req = SQLGenerationRequest(
        question="Get 5 students",
        schema_context=mock_model_facing_schema,
    )
    resp = fake.generate(req)

    assert fake.generate_called is True
    assert isinstance(resp, SQLGenerationResponse)
    assert "SELECT TOP 5" in resp.sql
    assert resp.explanation is not None


def test_generator_boundary_does_not_execute_sql(mock_model_facing_schema, monkeypatch):
    """Verify that generator does not execute SQL or import/call executor."""
    from unittest.mock import MagicMock
    import query_pilot.db.executor as executor_module

    mock_execute = MagicMock()
    monkeypatch.setattr(executor_module, "execute_read_only", mock_execute)

    fake = FakeSQLGenerator()
    req = SQLGenerationRequest(
        question="Get 5 students",
        schema_context=mock_model_facing_schema,
    )
    fake.generate(req)

    # Must NEVER have called execute_read_only
    mock_execute.assert_not_called()


# ==============================================================================
# Gemini Generator Configuration Error Tests
# ==============================================================================

def test_gemini_generator_raises_configuration_error_on_missing_key(monkeypatch):
    """Verify GeminiSQLGenerator raises ConfigurationError when API key is missing or invalid."""
    # 1. Explicit empty string
    with pytest.raises(ConfigurationError) as exc_info:
        GeminiSQLGenerator(api_key="")
    assert "GEMINI_API_KEY" in str(exc_info.value)

    # 2. Explicit placeholder value
    with pytest.raises(ConfigurationError):
        GeminiSQLGenerator(api_key="your_gemini_api_key_here")

    # 3. Settings lookup failure
    def mock_bad_get_settings():
        raise RuntimeError("No .env file available")

    monkeypatch.setattr("query_pilot.sql.gemini_generator.get_settings", mock_bad_get_settings)
    with pytest.raises(ConfigurationError) as exc_info:
        GeminiSQLGenerator(settings=None, api_key=None)
    assert "Failed to load application settings" in str(exc_info.value)
