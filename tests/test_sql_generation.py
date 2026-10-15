"""Unit tests for SQL generation contract, schema context formatting, and prompt building."""

import json
from pathlib import Path
from typing import List, Optional
import pytest
from pydantic import ValidationError

from unittest.mock import MagicMock

from query_pilot.db.models import ColumnMetadata, DatabaseSchema, ForeignKeyMetadata, TableMetadata
from query_pilot.observability import LLMUsageMetadata
from query_pilot.rag.retriever import DomainContextRetriever, RetrievalResult
from query_pilot.sql.gemini_generator import GeminiSQLGenerator, _GeminiSQLPayloadSchema
from query_pilot.sql.generation import (
    MAX_CLARIFICATION_OPTIONS,
    MAX_CONVERSATION_TURNS,
    ClarificationOption,
    ConfigurationError,
    ConversationTurnContext,
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


# ==============================================================================
# Conversation Context Prompt Assembly Tests (Task 1.29-B2)
# ==============================================================================

def test_prompt_empty_context_preserves_standalone_behavior(mock_model_facing_schema):
    """Verify prompt without conversation context preserves standalone question structure."""
    req = SQLGenerationRequest(
        question="Which users have role admin?",
        schema_context=mock_model_facing_schema,
        conversation_context=[],
    )
    prompt = build_sql_generation_prompt(req)

    assert "=== PRIOR CONVERSATION CONTEXT" not in prompt
    assert "Turn 1:" not in prompt
    assert "=== USER QUESTION ===" in prompt
    assert "=== CURRENT USER QUESTION ===" not in prompt
    assert "Question: Which users have role admin?" in prompt
    assert "=== TASK ===" in prompt


def test_prompt_includes_one_prior_user_question(mock_model_facing_schema):
    """Verify single prior user question interaction is included cleanly."""
    turns = [
        ConversationTurnContext(user_question="List all users"),
    ]
    req = SQLGenerationRequest(
        question="Filter to active admins",
        schema_context=mock_model_facing_schema,
        conversation_context=turns,
    )
    prompt = build_sql_generation_prompt(req)

    assert "=== PRIOR CONVERSATION CONTEXT (HISTORICAL REFERENCE ONLY) ===" in prompt
    assert "NOTE: The following represents prior user-question interactions for historical reference only." in prompt
    assert "Turn 1:" in prompt
    assert "- Prior Question: List all users" in prompt
    assert "- Resolved Intent:" not in prompt
    assert "- Result Columns:" not in prompt
    assert "=== CURRENT USER QUESTION ===" in prompt
    assert "Question: Filter to active admins" in prompt


def test_prompt_three_prior_interactions_bounded(mock_model_facing_schema):
    """Verify prompt bounds historical context to at most 3 interactions."""
    turns = [
        ConversationTurnContext(user_question="Q1: List departments"),
        ConversationTurnContext(user_question="Q2: Filter to engineering"),
        ConversationTurnContext(user_question="Q3: Count staff"),
    ]
    req = SQLGenerationRequest(
        question="Q4: Show averages",
        schema_context=mock_model_facing_schema,
        conversation_context=turns,
    )
    prompt = build_sql_generation_prompt(req)

    assert "Turn 1:" in prompt
    assert "- Prior Question: Q1: List departments" in prompt
    assert "Turn 2:" in prompt
    assert "- Prior Question: Q2: Filter to engineering" in prompt
    assert "Turn 3:" in prompt
    assert "- Prior Question: Q3: Count staff" in prompt
    assert "Turn 4:" not in prompt
    assert "Question: Q4: Show averages" in prompt


def test_prompt_includes_resolved_intent_and_result_columns(mock_model_facing_schema):
    """Verify optional resolved intent and result columns are rendered when present."""
    turns = [
        ConversationTurnContext(
            user_question="List all orders",
            resolved_intent="Retrieve orders with status",
            result_columns=["Id", "UserId", "TotalAmount"],
        ),
    ]
    req = SQLGenerationRequest(
        question="Show orders above 100",
        schema_context=mock_model_facing_schema,
        conversation_context=turns,
    )
    prompt = build_sql_generation_prompt(req)

    assert "Turn 1:" in prompt
    assert "- Prior Question: List all orders" in prompt
    assert "- Resolved Intent: Retrieve orders with status" in prompt
    assert "- Result Columns: Id, UserId, TotalAmount" in prompt


def test_prompt_handles_missing_optional_fields_cleanly(mock_model_facing_schema):
    """Verify missing optional fields (None or empty) are omitted without placeholder strings."""
    turns = [
        ConversationTurnContext(
            user_question="Show users",
            resolved_intent=None,
            result_columns=[],
        ),
    ]
    req = SQLGenerationRequest(
        question="Follow up question",
        schema_context=mock_model_facing_schema,
        conversation_context=turns,
    )
    prompt = build_sql_generation_prompt(req)

    assert "Turn 1:" in prompt
    assert "- Prior Question: Show users" in prompt
    assert "- Resolved Intent:" not in prompt
    assert "- Result Columns:" not in prompt
    assert "None" not in prompt.split("Turn 1:")[1].split("=== CURRENT")[0]
    assert "[]" not in prompt


def test_prompt_forbidden_fields_cannot_enter_through_supported_inputs(mock_model_facing_schema):
    """Verify assistant turns, executed SQL, and unexpected fields are rejected before prompt assembly."""
    with pytest.raises(ValidationError):
        ConversationTurnContext(role="assistant", user_question="Q1")

    with pytest.raises(ValidationError):
        ConversationTurnContext.model_validate({"user_question": "Q1", "executed_sql": "SELECT 1"})

    with pytest.raises(ValidationError):
        ConversationTurnContext.model_validate({"user_question": "Q1", "credentials": "secret"})


def test_prompt_instructions_distinguish_three_statuses():
    """Verify system instruction distinguishes answerable, clarification_needed, and unsupported."""
    assert 'Return "status": "answerable"' in SYSTEM_INSTRUCTION
    assert 'Return "status": "clarification_needed"' in SYSTEM_INSTRUCTION
    assert 'Return "status": "unsupported"' in SYSTEM_INSTRUCTION
    assert "clarification_prompt" in SYSTEM_INSTRUCTION
    assert "clarification_options" in SYSTEM_INSTRUCTION
    assert "Multiple materially different interpretations remain plausible" in SYSTEM_INSTRUCTION


# ==============================================================================
# Gemini Generator Response Parsing and Invariant Tests (Task 1.29-B2)
# ==============================================================================

def _mock_gemini_generator(response_text: str, usage_mock=None) -> GeminiSQLGenerator:
    """Helper to instantiate GeminiSQLGenerator with a mocked client response."""
    gen = GeminiSQLGenerator(api_key="test-mock-api-key")
    mock_resp = MagicMock()
    mock_resp.text = response_text
    mock_resp.usage_metadata = usage_mock
    gen.client.models.generate_content = MagicMock(return_value=mock_resp)
    return gen


def test_gemini_generator_valid_answerable_response(mock_model_facing_schema):
    """Verify valid answerable response parses correctly and preserves all fields."""
    response_json = json.dumps({
        "status": "answerable",
        "sql": "SELECT Id, FullName FROM dbo.Users",
        "explanation": "Retrieves users from database.",
        "assumptions": ["Assumes active users"],
        "clarification_prompt": None,
        "clarification_options": [],
    })
    gen = _mock_gemini_generator(response_json)
    req = SQLGenerationRequest(
        question="List all users",
        schema_context=mock_model_facing_schema,
    )
    resp = gen.generate(req)

    assert resp.status == SQLGenerationStatus.ANSWERABLE
    assert resp.sql == "SELECT Id, FullName FROM dbo.Users"
    assert resp.explanation == "Retrieves users from database."
    assert resp.assumptions == ["Assumes active users"]
    assert resp.clarification_prompt is None
    assert resp.clarification_options == []
    assert resp.usage_metadata is not None


def test_gemini_generator_valid_unsupported_response(mock_model_facing_schema):
    """Verify valid unsupported response parses correctly with null SQL."""
    response_json = json.dumps({
        "status": "unsupported",
        "sql": None,
        "explanation": "Salary information is not stored in the Users table.",
        "assumptions": [],
    })
    gen = _mock_gemini_generator(response_json)
    req = SQLGenerationRequest(
        question="What is the average salary?",
        schema_context=mock_model_facing_schema,
    )
    resp = gen.generate(req)

    assert resp.status == SQLGenerationStatus.UNSUPPORTED
    assert resp.sql is None
    assert resp.explanation == "Salary information is not stored in the Users table."


def test_gemini_generator_valid_clarification_response(mock_model_facing_schema):
    """Verify valid clarification response parses with prompt and structured options."""
    response_json = json.dumps({
        "status": "clarification_needed",
        "sql": None,
        "explanation": "The term 'active' is ambiguous.",
        "clarification_prompt": "Did you mean users with recent logins or active account status?",
        "clarification_options": [
            {
                "option_id": "recent_login",
                "description": "Users who logged in within the last 30 days",
                "suggested_question": "Show users with recent logins",
            },
            {
                "option_id": "active_status",
                "description": "Users with Role equal to active",
            },
        ],
        "assumptions": [],
    })
    gen = _mock_gemini_generator(response_json)
    req = SQLGenerationRequest(
        question="Show active users",
        schema_context=mock_model_facing_schema,
    )
    resp = gen.generate(req)

    assert resp.status == SQLGenerationStatus.CLARIFICATION_NEEDED
    assert resp.sql is None
    assert resp.clarification_prompt == "Did you mean users with recent logins or active account status?"
    assert len(resp.clarification_options) == 2
    assert resp.clarification_options[0].option_id == "recent_login"
    assert resp.clarification_options[0].suggested_question == "Show users with recent logins"
    assert resp.clarification_options[1].option_id == "active_status"
    assert resp.clarification_options[1].suggested_question is None


def test_gemini_generator_clarification_with_sql_rejected(mock_model_facing_schema):
    """Verify clarification response containing SQL raises MalformedResponseError."""
    response_json = json.dumps({
        "status": "clarification_needed",
        "sql": "SELECT Id FROM dbo.Users",
        "clarification_prompt": "Which users?",
        "clarification_options": [],
    })
    gen = _mock_gemini_generator(response_json)
    req = SQLGenerationRequest(
        question="Show users",
        schema_context=mock_model_facing_schema,
    )
    with pytest.raises(MalformedResponseError) as exc_info:
        gen.generate(req)
    assert exc_info.value.usage_metadata is not None


def test_gemini_generator_clarification_missing_prompt_and_explanation_rejected(mock_model_facing_schema):
    """Verify clarification response missing both prompt and explanation is rejected."""
    response_json = json.dumps({
        "status": "clarification_needed",
        "sql": None,
        "clarification_prompt": None,
        "explanation": None,
    })
    gen = _mock_gemini_generator(response_json)
    req = SQLGenerationRequest(
        question="Show users",
        schema_context=mock_model_facing_schema,
    )
    with pytest.raises(MalformedResponseError) as exc_info:
        gen.generate(req)
    assert exc_info.value.usage_metadata is not None


def test_gemini_generator_clarification_excessive_options_rejected(mock_model_facing_schema):
    """Verify clarification response with more than 5 options is rejected."""
    response_json = json.dumps({
        "status": "clarification_needed",
        "sql": None,
        "clarification_prompt": "Please pick:",
        "clarification_options": [
            {"option_id": f"opt_{i}", "description": f"Option {i}"}
            for i in range(6)
        ],
    })
    gen = _mock_gemini_generator(response_json)
    req = SQLGenerationRequest(
        question="Show users",
        schema_context=mock_model_facing_schema,
    )
    with pytest.raises(MalformedResponseError):
        gen.generate(req)


def test_gemini_generator_clarification_duplicate_option_ids_rejected(mock_model_facing_schema):
    """Verify clarification response with duplicate option IDs is rejected."""
    response_json = json.dumps({
        "status": "clarification_needed",
        "sql": None,
        "clarification_prompt": "Please pick:",
        "clarification_options": [
            {"option_id": "dup", "description": "Choice A"},
            {"option_id": "dup", "description": "Choice B"},
        ],
    })
    gen = _mock_gemini_generator(response_json)
    req = SQLGenerationRequest(
        question="Show users",
        schema_context=mock_model_facing_schema,
    )
    with pytest.raises(MalformedResponseError):
        gen.generate(req)


def test_gemini_generator_invalid_status_rejected(mock_model_facing_schema):
    """Verify invalid status string raises MalformedResponseError."""
    response_json = json.dumps({
        "status": "maybe_answerable",
        "sql": "SELECT 1",
    })
    gen = _mock_gemini_generator(response_json)
    req = SQLGenerationRequest(
        question="Show users",
        schema_context=mock_model_facing_schema,
    )
    with pytest.raises(MalformedResponseError):
        gen.generate(req)


def test_gemini_generator_malformed_json_rejected(mock_model_facing_schema):
    """Verify non-JSON response text raises MalformedResponseError."""
    gen = _mock_gemini_generator("Not a valid JSON document.")
    req = SQLGenerationRequest(
        question="Show users",
        schema_context=mock_model_facing_schema,
    )
    with pytest.raises(MalformedResponseError):
        gen.generate(req)


def test_gemini_generator_empty_response_rejected(mock_model_facing_schema):
    """Verify null or empty response raises MalformedResponseError."""
    gen = _mock_gemini_generator("")
    req = SQLGenerationRequest(
        question="Show users",
        schema_context=mock_model_facing_schema,
    )
    with pytest.raises(MalformedResponseError):
        gen.generate(req)


def test_gemini_generator_provider_api_error_reraised(mock_model_facing_schema):
    """Verify network or client exception raises ProviderAPIError."""
    gen = GeminiSQLGenerator(api_key="test-mock-api-key")
    gen.client.models.generate_content = MagicMock(side_effect=RuntimeError("Connection reset by peer"))
    req = SQLGenerationRequest(
        question="Show users",
        schema_context=mock_model_facing_schema,
    )
    with pytest.raises(ProviderAPIError):
        gen.generate(req)


def test_gemini_generator_preserves_usage_metadata(mock_model_facing_schema):
    """Verify provider usage metadata is attached to response and generator state."""
    mock_usage = MagicMock()
    mock_usage.prompt_token_count = 120
    mock_usage.candidates_token_count = 40
    mock_usage.total_token_count = 160

    response_json = json.dumps({
        "status": "answerable",
        "sql": "SELECT Id FROM dbo.Users",
    })
    gen = _mock_gemini_generator(response_json, usage_mock=mock_usage)
    req = SQLGenerationRequest(
        question="Show users",
        schema_context=mock_model_facing_schema,
    )
    resp = gen.generate(req)

    assert resp.usage_metadata is not None
    assert resp.usage_metadata.prompt_tokens == 120
    assert resp.usage_metadata.completion_tokens == 40
    assert resp.usage_metadata.total_tokens == 160
    assert gen.last_usage == resp.usage_metadata


def test_gemini_payload_schema_structure():
    """Verify internal Gemini payload schema can generate OpenAPI JSON schema."""
    schema = _GeminiSQLPayloadSchema.model_json_schema()
    assert "properties" in schema
    assert "status" in schema["properties"]
    assert "sql" in schema["properties"]
    assert "clarification_prompt" in schema["properties"]
    assert "clarification_options" in schema["properties"]


def test_gemini_generator_answerable_with_clarification_prompt_rejected(mock_model_facing_schema):
    """Verify Gemini returning answerable status with clarification prompt raises MalformedResponseError."""
    response_json = json.dumps({
        "status": "answerable",
        "sql": "SELECT Id FROM dbo.Users",
        "clarification_prompt": "Are you sure?",
    })
    gen = _mock_gemini_generator(response_json)
    req = SQLGenerationRequest(
        question="Show users",
        schema_context=mock_model_facing_schema,
    )
    with pytest.raises(MalformedResponseError):
        gen.generate(req)


def test_gemini_generator_unsupported_with_clarification_prompt_rejected(mock_model_facing_schema):
    """Verify Gemini returning unsupported status with clarification prompt raises MalformedResponseError."""
    response_json = json.dumps({
        "status": "unsupported",
        "sql": None,
        "explanation": "Not supported.",
        "clarification_prompt": "Which table?",
    })
    gen = _mock_gemini_generator(response_json)
    req = SQLGenerationRequest(
        question="Show users",
        schema_context=mock_model_facing_schema,
    )
    with pytest.raises(MalformedResponseError):
        gen.generate(req)


def test_gemini_generator_valid_clarification_explanation_only(mock_model_facing_schema):
    """Verify Gemini returning clarification with explanation-only and no prompt is accepted."""
    response_json = json.dumps({
        "status": "clarification_needed",
        "sql": None,
        "explanation": "Please specify whether you mean undergraduate or graduate courses.",
        "clarification_prompt": None,
        "clarification_options": [],
    })
    gen = _mock_gemini_generator(response_json)
    req = SQLGenerationRequest(
        question="Show courses",
        schema_context=mock_model_facing_schema,
    )
    resp = gen.generate(req)

    assert resp.status == SQLGenerationStatus.CLARIFICATION_NEEDED
    assert resp.sql is None
    assert resp.clarification_prompt is None
    assert resp.clarification_options == []
    assert resp.explanation == "Please specify whether you mean undergraduate or graduate courses."


def test_gemini_generator_malformed_response_preserves_usage_metadata(mock_model_facing_schema):
    """Verify usage metadata is attached to MalformedResponseError when JSON parsing fails."""
    mock_usage = MagicMock()
    mock_usage.prompt_token_count = 85
    mock_usage.candidates_token_count = 15
    mock_usage.total_token_count = 100

    gen = _mock_gemini_generator("NOT JSON", usage_mock=mock_usage)
    req = SQLGenerationRequest(
        question="Show courses",
        schema_context=mock_model_facing_schema,
    )
    with pytest.raises(MalformedResponseError) as exc_info:
        gen.generate(req)

    assert exc_info.value.usage_metadata is not None
    assert exc_info.value.usage_metadata.total_tokens == 100
    assert gen.last_usage is not None
    assert gen.last_usage.total_tokens == 100


def test_gemini_generator_unsupported_with_clarification_options_rejected(mock_model_facing_schema):
    """Verify unsupported response with clarification_options raises MalformedResponseError."""
    response_json = json.dumps({
        "status": "unsupported",
        "sql": None,
        "explanation": "Not supported.",
        "clarification_options": [{"option_id": "1", "description": "Opt 1"}],
    })
    gen = _mock_gemini_generator(response_json)
    req = SQLGenerationRequest(
        question="Show users",
        schema_context=mock_model_facing_schema,
    )
    with pytest.raises(MalformedResponseError):
        gen.generate(req)


def test_gemini_generator_answerable_with_clarification_options_rejected(mock_model_facing_schema):
    """Verify answerable response with clarification_options raises MalformedResponseError."""
    response_json = json.dumps({
        "status": "answerable",
        "sql": "SELECT 1",
        "clarification_options": [{"option_id": "1", "description": "Opt 1"}],
    })
    gen = _mock_gemini_generator(response_json)
    req = SQLGenerationRequest(
        question="Show users",
        schema_context=mock_model_facing_schema,
    )
    with pytest.raises(MalformedResponseError):
        gen.generate(req)


# ==============================================================================
# Domain Knowledge Integration Tests (Task 1.31-D)
# ==============================================================================

def test_gemini_generator_includes_relevant_domain_guidance_in_prompt(mock_model_facing_schema):
    """Verify relevant domain guidance from knowledge catalog is included in the prompt."""
    response_json = json.dumps({
        "status": "answerable",
        "sql": "SELECT u.FullName FROM dbo.Students s JOIN dbo.Users u ON s.Id = u.Id",
        "explanation": "Joins Students to Users.",
    })
    gen = _mock_gemini_generator(response_json)
    req = SQLGenerationRequest(
        question="How are student names joined from users?",
        schema_context=mock_model_facing_schema,
    )
    resp = gen.generate(req)

    assert resp.status == SQLGenerationStatus.ANSWERABLE
    prompt = gen.client.models.generate_content.call_args.kwargs["contents"]

    assert "=== RETRIEVED DOMAIN KNOWLEDGE (INFORMATIONAL ONLY) ===" in prompt
    assert "NOTE: The following domain definitions and join guidance are informational only." in prompt
    assert "[KB_STU_USERS_INHERITANCE]" in prompt
    assert "Students.Id = Users.Id" in prompt
    assert "=== USER QUESTION ===" in prompt


def test_gemini_generator_omits_domain_guidance_for_irrelevant_questions(mock_model_facing_schema):
    """Verify irrelevant questions omit the domain guidance section cleanly."""
    response_json = json.dumps({
        "status": "unsupported",
        "sql": None,
        "explanation": "Weather is not stored in the database.",
    })
    gen = _mock_gemini_generator(response_json)
    req = SQLGenerationRequest(
        question="What is the weather forecast for tomorrow?",
        schema_context=mock_model_facing_schema,
    )
    resp = gen.generate(req)

    assert resp.status == SQLGenerationStatus.UNSUPPORTED
    prompt = gen.client.models.generate_content.call_args.kwargs["contents"]

    assert "=== RETRIEVED DOMAIN KNOWLEDGE" not in prompt
    assert "=== DOMAIN GUIDANCE" not in prompt
    assert "=== USER QUESTION ===" in prompt
    assert "Question: What is the weather forecast for tomorrow?" in prompt


def test_gemini_generator_retrieves_semantic_traps_q028_q029_q030(mock_model_facing_schema):
    """Verify Q028, Q029, and Q030 questions retrieve their respective semantic trap guidance."""
    response_json = json.dumps({
        "status": "answerable",
        "sql": "SELECT 1",
        "explanation": "Test.",
    })
    gen = _mock_gemini_generator(response_json)

    # Q028: student-to-department vs student-to-user
    req_q28 = SQLGenerationRequest(
        question="Students joined with department and user dean",
        schema_context=mock_model_facing_schema,
    )
    gen.generate(req_q28)
    prompt_q28 = gen.client.models.generate_content.call_args.kwargs["contents"]
    assert "[KB_TRAP_STUDENT_DEPARTMENT_JOIN]" in prompt_q28
    assert "Students.DepartmentId references Departments.Id, NOT Users.Id" in prompt_q28

    # Q029: faculty primary key vs department foreign key
    req_q29 = SQLGenerationRequest(
        question="Faculties primary key vs department foreign key collision",
        schema_context=mock_model_facing_schema,
    )
    gen.generate(req_q29)
    prompt_q29 = gen.client.models.generate_content.call_args.kwargs["contents"]
    assert "[KB_TRAP_FACULTY_DEPARTMENT_JOIN]" in prompt_q29
    assert "Faculties.Id represents primary key user inheritance" in prompt_q29

    # Q030: course offering coordinator multi-hop join
    req_q30 = SQLGenerationRequest(
        question="Course offerings coordinator professor name",
        schema_context=mock_model_facing_schema,
    )
    gen.generate(req_q30)
    prompt_q30 = gen.client.models.generate_content.call_args.kwargs["contents"]
    assert "[KB_TRAP_OFFERING_COORDINATOR_JOIN]" in prompt_q30
    assert "CourseOfferings.SubjectCoordinatorId" in prompt_q30


def test_schema_and_foreign_keys_preserved_when_domain_guidance_present(mock_model_facing_schema):
    """Verify schema, columns, and foreign keys remain intact when domain guidance is included."""
    response_json = json.dumps({"status": "answerable", "sql": "SELECT 1"})
    gen = _mock_gemini_generator(response_json)
    req = SQLGenerationRequest(
        question="Students user inheritance",
        schema_context=mock_model_facing_schema,
    )
    gen.generate(req)
    prompt = gen.client.models.generate_content.call_args.kwargs["contents"]

    # Original schema sections intact
    assert "=== DATABASE SCHEMA CONTEXT ===" in prompt
    assert "TABLE [Users]:" in prompt
    assert "TABLE [Students]:" in prompt
    assert "RELATIONSHIPS (FOREIGN KEYS):" in prompt
    assert "[Students].[Id] -> [Users].[Id]" in prompt

    # Both schema and domain guidance coexist in correct order
    schema_idx = prompt.index("=== DATABASE SCHEMA CONTEXT ===")
    domain_idx = prompt.index("=== RETRIEVED DOMAIN KNOWLEDGE (INFORMATIONAL ONLY) ===")
    question_idx = prompt.index("=== USER QUESTION ===")
    assert schema_idx < domain_idx < question_idx


def test_domain_guidance_is_bounded_and_contains_no_sensitive_columns(mock_model_facing_schema):
    """Verify retrieved guidance adheres to character budget and excludes sensitive fields."""
    response_json = json.dumps({"status": "answerable", "sql": "SELECT 1"})
    gen = _mock_gemini_generator(response_json)
    broad_query = "students users faculties departments marks offerings coordinator batches"
    req = SQLGenerationRequest(
        question=broad_query,
        schema_context=mock_model_facing_schema,
    )
    gen.generate(req)
    prompt = gen.client.models.generate_content.call_args.kwargs["contents"]

    # Must contain domain guidance
    assert "=== RETRIEVED DOMAIN KNOWLEDGE (INFORMATIONAL ONLY) ===" in prompt
    domain_section = prompt.split("=== RETRIEVED DOMAIN KNOWLEDGE (INFORMATIONAL ONLY) ===")[1].split("=== USER QUESTION ===")[0]
    # Bound check: rendered block must be <= 1200 characters
    assert len(domain_section.strip()) <= 1200

    # Sensitive column prohibition
    assert "PasswordHash" not in prompt


def test_unresolved_business_rules_cannot_be_loaded_into_generator():
    """Verify unresolved business rules markdown file cannot be loaded into generator."""
    with pytest.raises(ValueError, match="Unresolved business rules markdown files cannot be loaded"):
        GeminiSQLGenerator(
            api_key="test-key",
            catalog_path="data/knowledge/unresolved_business_rules.md",
        )


def test_missing_or_malformed_catalog_fails_explicitly(tmp_path: Path):
    """Verify missing or malformed catalog files fail explicitly and do not silently disable safeguards."""
    with pytest.raises(FileNotFoundError, match="Knowledge catalog not found"):
        GeminiSQLGenerator(
            api_key="test-key",
            catalog_path="nonexistent_catalog.json",
        )

    bad_json = tmp_path / "broken_catalog.json"
    bad_json.write_text("{broken json", encoding="utf-8")
    with pytest.raises(ValueError, match="Malformed JSON"):
        GeminiSQLGenerator(
            api_key="test-key",
            catalog_path=bad_json,
        )


def test_build_sql_generation_prompt_with_explicit_domain_context(mock_model_facing_schema):
    """Verify build_sql_generation_prompt handles explicit domain_context parameter."""
    req = SQLGenerationRequest(
        question="Find student records",
        schema_context=mock_model_facing_schema,
    )

    # Empty domain context
    prompt_no_ctx = build_sql_generation_prompt(req, domain_context=None)
    assert "=== RETRIEVED DOMAIN KNOWLEDGE" not in prompt_no_ctx

    # Custom domain context
    prompt_with_ctx = build_sql_generation_prompt(
        req,
        domain_context="[CUSTOM_RULE] Custom test domain rule.",
    )
    assert "=== RETRIEVED DOMAIN KNOWLEDGE (INFORMATIONAL ONLY) ===" in prompt_with_ctx
    assert "[CUSTOM_RULE] Custom test domain rule." in prompt_with_ctx


def test_generator_dependency_injection_with_mock_retriever(mock_model_facing_schema):
    """Verify GeminiSQLGenerator correctly integrates with injected mock retriever."""
    mock_retriever = MagicMock()
    mock_retriever.retrieve.return_value = [
        RetrievalResult(
            entry_id="KB_MOCK_1",
            category="rule",
            title="Mock Rule",
            content="Mock content rule.",
            score=5.0,
            matched_terms=["term"],
        )
    ]
    mock_retriever.render_context.return_value = (
        "=== RETRIEVED DOMAIN KNOWLEDGE (INFORMATIONAL ONLY) ===\n"
        "[KB_MOCK_1] Mock Rule:\nMock content rule."
    )

    response_json = json.dumps({"status": "answerable", "sql": "SELECT 1"})
    gen = GeminiSQLGenerator(api_key="test-mock-api-key", retriever=mock_retriever)
    mock_resp = MagicMock()
    mock_resp.text = response_json
    mock_resp.usage_metadata = None
    gen.client.models.generate_content = MagicMock(return_value=mock_resp)

    req = SQLGenerationRequest(
        question="Test question with injected retriever",
        schema_context=mock_model_facing_schema,
    )
    gen.generate(req)

    # Verify retriever was invoked
    mock_retriever.retrieve.assert_called_once_with("Test question with injected retriever", intent=None)
    mock_retriever.render_context.assert_called_once()
    prompt = gen.client.models.generate_content.call_args.kwargs["contents"]
    assert "[KB_MOCK_1] Mock Rule:" in prompt


def test_sql_generation_request_rejects_arbitrary_domain_context(mock_model_facing_schema):
    """Verify SQLGenerationRequest forbids caller-supplied domain_context to close trust boundary."""
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        SQLGenerationRequest(
            question="Show all students",
            schema_context=mock_model_facing_schema,
            domain_context="Arbitrary untrusted domain guidance",  # type: ignore[call-arg]
        )


def test_generation_obtains_domain_guidance_strictly_via_retriever(mock_model_facing_schema):
    """Verify that GeminiSQLGenerator.generate derives guidance exclusively from its retriever."""
    response_json = json.dumps({"status": "answerable", "sql": "SELECT 1"})
    gen = _mock_gemini_generator(response_json)

    # Valid query matching catalog
    req = SQLGenerationRequest(
        question="Students user inheritance",
        schema_context=mock_model_facing_schema,
    )
    gen.generate(req)
    prompt = gen.client.models.generate_content.call_args.kwargs["contents"]

    # Must contain validated knowledge from the approved catalog
    assert "=== RETRIEVED DOMAIN KNOWLEDGE (INFORMATIONAL ONLY) ===" in prompt
    assert "[KB_STU_USERS_INHERITANCE]" in prompt

    # When retrieval is disabled, no guidance is included
    gen_disabled = GeminiSQLGenerator(api_key="test-mock-api-key", enable_retrieval=False)
    mock_resp = MagicMock()
    mock_resp.text = response_json
    mock_resp.usage_metadata = None
    gen_disabled.client.models.generate_content = MagicMock(return_value=mock_resp)

    gen_disabled.generate(req)
    prompt_disabled = gen_disabled.client.models.generate_content.call_args.kwargs["contents"]
    assert "=== RETRIEVED DOMAIN KNOWLEDGE" not in prompt_disabled


# ==============================================================================
# Gemini Response Schema Compatibility Tests (Task 1.33-B9)
# ==============================================================================

def test_gemini_response_schema_contains_no_additional_properties():
    """Verify Gemini response_schema does not contain unsupported additional_properties anywhere in schema tree."""
    from google.genai import _transformers as t

    schema = t.t_schema(None, _GeminiSQLPayloadSchema)
    assert schema is not None

    def assert_no_additional_properties(node, path="root"):
        assert getattr(node, "additional_properties", None) is None, (
            f"Found unsupported additional_properties at {path}: {getattr(node, 'additional_properties', None)}"
        )
        if hasattr(node, "properties") and node.properties:
            for prop_name, prop_schema in node.properties.items():
                assert_no_additional_properties(prop_schema, f"{path}.properties['{prop_name}']")
        if hasattr(node, "items") and node.items:
            assert_no_additional_properties(node.items, f"{path}.items")

    assert_no_additional_properties(schema)

    # Verify clarification_options specifically (the offending property from Task 1.33-B8)
    clarification_items = schema.properties["clarification_options"].items
    assert clarification_items is not None
    assert getattr(clarification_items, "additional_properties", None) is None

    # Verify dictionary serialization omits any additional_properties / additionalProperties keys
    dumped = schema.model_dump(exclude_none=True, by_alias=True)

    def assert_no_forbidden_keys(obj, path="root"):
        if isinstance(obj, dict):
            for k, v in obj.items():
                assert k not in ("additional_properties", "additionalProperties"), (
                    f"Found forbidden key '{k}' in serialized schema at {path}"
                )
                assert_no_forbidden_keys(v, f"{path}['{k}']")
        elif isinstance(obj, list):
            for i, elem in enumerate(obj):
                assert_no_forbidden_keys(elem, f"{path}[{i}]")

    assert_no_forbidden_keys(dumped)


def test_gemini_response_schema_retains_required_fields_and_nested_structure():
    """Verify Gemini schema retains all required fields, correct types, and nullability."""
    from google.genai import _transformers as t
    from google.genai import types

    schema = t.t_schema(None, _GeminiSQLPayloadSchema)
    assert schema.type == types.Type.OBJECT

    expected_root_props = {
        "status",
        "sql",
        "explanation",
        "assumptions",
        "clarification_prompt",
        "clarification_options",
    }
    assert set(schema.properties.keys()) == expected_root_props

    # Status enum
    status_schema = schema.properties["status"]
    assert status_schema.type == types.Type.STRING
    assert set(status_schema.enum) == {"answerable", "unsupported", "clarification_needed"}

    # Nullable string fields
    assert schema.properties["sql"].type == types.Type.STRING
    assert schema.properties["sql"].nullable is True

    assert schema.properties["explanation"].type == types.Type.STRING
    assert schema.properties["explanation"].nullable is True

    assert schema.properties["clarification_prompt"].type == types.Type.STRING
    assert schema.properties["clarification_prompt"].nullable is True

    # Assumptions array
    assert schema.properties["assumptions"].type == types.Type.ARRAY
    assert schema.properties["assumptions"].items.type == types.Type.STRING

    # Clarification options array and nested items
    options_schema = schema.properties["clarification_options"]
    assert options_schema.type == types.Type.ARRAY
    items_schema = options_schema.items
    assert items_schema.type == types.Type.OBJECT

    expected_item_props = {"option_id", "description", "suggested_question"}
    assert set(items_schema.properties.keys()) == expected_item_props
    assert set(items_schema.required) == {"option_id", "description"}
    assert items_schema.properties["option_id"].type == types.Type.STRING
    assert items_schema.properties["description"].type == types.Type.STRING
    assert items_schema.properties["suggested_question"].type == types.Type.STRING
    assert items_schema.properties["suggested_question"].nullable is True


def test_gemini_generator_configures_response_schema_offline(mock_model_facing_schema):
    """Verify GeminiSQLGenerator configures GenerateContentConfig with the schema offline without API calls."""
    from google.genai import _transformers as t

    response_json = json.dumps({"status": "answerable", "sql": "SELECT 1"})
    gen = _mock_gemini_generator(response_json)

    req = SQLGenerationRequest(
        question="Count total students",
        schema_context=mock_model_facing_schema,
    )
    gen.generate(req)

    assert gen.client.models.generate_content.called
    call_kwargs = gen.client.models.generate_content.call_args.kwargs
    config = call_kwargs["config"]

    assert config.response_mime_type == "application/json"
    assert config.response_schema is _GeminiSQLPayloadSchema
    assert config.temperature == 0.0

    # Ensure SDK transformation on configured response_schema produces clean Schema without additional_properties
    converted_schema = t.t_schema(None, config.response_schema)
    clarification_items = converted_schema.properties["clarification_options"].items
    assert getattr(clarification_items, "additional_properties", None) is None


def test_gemini_generator_parses_clarification_response_into_domain_model(mock_model_facing_schema):
    """Verify Gemini generator parses clarification output into strict ClarificationOption domain models."""
    response_json = json.dumps({
        "status": "clarification_needed",
        "sql": None,
        "explanation": "Multiple matching semesters found.",
        "assumptions": [],
        "clarification_prompt": "Which semester did you mean?",
        "clarification_options": [
            {
                "option_id": "opt_spring26",
                "description": "Spring 2026 current semester",
                "suggested_question": "Show courses in Spring 2026",
            },
            {
                "option_id": "opt_fall25",
                "description": "Fall 2025 previous semester",
                "suggested_question": None,
            },
        ],
    })
    gen = _mock_gemini_generator(response_json)
    req = SQLGenerationRequest(
        question="Show courses",
        schema_context=mock_model_facing_schema,
    )
    result = gen.generate(req)

    assert result.status == SQLGenerationStatus.CLARIFICATION_NEEDED
    assert len(result.clarification_options) == 2

    opt1 = result.clarification_options[0]
    assert isinstance(opt1, ClarificationOption)
    assert opt1.option_id == "opt_spring26"
    assert opt1.description == "Spring 2026 current semester"
    assert opt1.suggested_question == "Show courses in Spring 2026"

    opt2 = result.clarification_options[1]
    assert isinstance(opt2, ClarificationOption)
    assert opt2.option_id == "opt_fall25"
    assert opt2.description == "Fall 2025 previous semester"
    assert opt2.suggested_question is None
