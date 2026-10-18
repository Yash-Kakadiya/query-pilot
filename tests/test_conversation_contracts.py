"""Focused unit tests for conversational context and clarification contracts.

Verifies:
1. ClarificationOption validation, bounding, and forbidden extra fields.
2. Shared MAX_CLARIFICATION_OPTIONS bounding (0, 5, 6) and option ID uniqueness.
3. Clarification prompt length bounding (1,000 vs 1,001 chars) and non-empty requirement.
4. SQLGenerationStatus and SQLGenerationResponse clarification invariants.
5. ConversationTurnContext semantics, extra-field prohibition, alternate SQL rejection,
   and role-preserving fallback.
6. SQLGenerationRequest conversation context bounding (max 3 turns).
7. QueryPilotState and create_initial_state / validate_initial_state validation.
8. PipelineStatus.CLARIFICATION_NEEDED, PipelineResult invariants, and contradictory status rejection.
9. Backward compatibility for callers omitting context and existing response models.
"""

import pytest
from pydantic import ValidationError

from query_pilot.db.executor import QueryResult
from query_pilot.db.models import ColumnMetadata, DatabaseSchema, TableMetadata
from query_pilot.graph.state import (
    MAX_CONVERSATION_TURNS,
    ConversationTurn,
    create_initial_state,
    validate_conversation_turn,
    validate_initial_state,
)
from query_pilot.pipeline import PipelineResult, PipelineStatus
from query_pilot.sql.generation import (
    ALLOWED_ROLES,
    FORBIDDEN_SQL_FIELDS,
    MAX_CLARIFICATION_OPTIONS,
    ClarificationOption,
    ConversationTurnContext,
    SQLGenerationRequest,
    SQLGenerationResponse,
    SQLGenerationStatus,
    validate_clarification_options,
)


# ==============================================================================
# Fixtures
# ==============================================================================

@pytest.fixture
def mock_schema() -> DatabaseSchema:
    """Minimal model-facing schema fixture."""
    return DatabaseSchema(
        database_name="TestDB",
        tables=[
            TableMetadata(
                schema_name="dbo",
                table_name="Students",
                columns=[
                    ColumnMetadata(name="Id", data_type="int", nullable=False, primary_key=True),
                    ColumnMetadata(name="Name", data_type="varchar(100)", nullable=False),
                ],
                primary_keys=["Id"],
            )
        ],
        is_model_facing=True,
    )


# ==============================================================================
# 1. ClarificationOption Tests
# ==============================================================================

def test_clarification_option_valid():
    """Verify ClarificationOption with valid fields."""
    opt = ClarificationOption(
        option_id="cgpa_by_dept",
        description="Compare student performance by average CGPA across departments.",
        suggested_question="Show average CGPA by department",
    )
    assert opt.option_id == "cgpa_by_dept"
    assert opt.description.startswith("Compare student performance")
    assert opt.suggested_question == "Show average CGPA by department"


def test_clarification_option_strips_whitespace():
    """Verify string fields are stripped of leading/trailing whitespace."""
    opt = ClarificationOption(
        option_id="  option_1  ",
        description="  Description here  ",
        suggested_question="  Suggested query  ",
    )
    assert opt.option_id == "option_1"
    assert opt.description == "Description here"
    assert opt.suggested_question == "Suggested query"


def test_clarification_option_optional_suggested_question():
    """Verify suggested_question can be None or omitted."""
    opt = ClarificationOption(
        option_id="opt_only",
        description="Option without suggested query",
    )
    assert opt.suggested_question is None


def test_clarification_option_rejects_empty_fields():
    """Verify empty or whitespace-only option_id or description is rejected."""
    with pytest.raises(ValidationError):
        ClarificationOption(option_id="", description="Valid desc")

    with pytest.raises(ValidationError):
        ClarificationOption(option_id="  ", description="Valid desc")

    with pytest.raises(ValidationError):
        ClarificationOption(option_id="valid_id", description="")

    with pytest.raises(ValidationError):
        ClarificationOption(option_id="valid_id", description="   ")


def test_clarification_option_rejects_extra_fields():
    """Verify ClarificationOption forbids unexpected extra fields."""
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ClarificationOption(
            option_id="opt1",
            description="Valid description",
            extra_field="disallowed",  # type: ignore[call-arg]
        )


# ==============================================================================
# 2. Clarification Options Bounding & Uniqueness Tests
# ==============================================================================

def test_clarification_options_counts_0_and_5_valid():
    """Verify option counts of 0 and 5 are valid in response."""
    # 0 options
    resp_0 = SQLGenerationResponse(
        status=SQLGenerationStatus.CLARIFICATION_NEEDED,
        clarification_prompt="What did you mean?",
        clarification_options=[],
    )
    assert len(resp_0.clarification_options) == 0

    # 5 options (maximum)
    options_5 = [
        ClarificationOption(option_id=f"opt_{i}", description=f"Description {i}")
        for i in range(MAX_CLARIFICATION_OPTIONS)
    ]
    resp_5 = SQLGenerationResponse(
        status=SQLGenerationStatus.CLARIFICATION_NEEDED,
        clarification_prompt="Please choose from these 5 options:",
        clarification_options=options_5,
    )
    assert len(resp_5.clarification_options) == 5


def test_clarification_options_count_6_rejected():
    """Verify option count of 6 exceeds MAX_CLARIFICATION_OPTIONS and is rejected."""
    options_6 = [
        ClarificationOption(option_id=f"opt_{i}", description=f"Description {i}")
        for i in range(MAX_CLARIFICATION_OPTIONS + 1)
    ]
    with pytest.raises(ValidationError, match="clarification_options cannot exceed 5 options"):
        SQLGenerationResponse(
            status=SQLGenerationStatus.CLARIFICATION_NEEDED,
            clarification_prompt="Too many options:",
            clarification_options=options_6,
        )


def test_clarification_options_duplicate_ids_rejected():
    """Verify duplicate option_id values within a response are rejected."""
    duplicate_options = [
        ClarificationOption(option_id="same_id", description="First interpretation"),
        ClarificationOption(option_id="same_id", description="Second interpretation"),
    ]
    with pytest.raises(ValidationError, match="Duplicate clarification option_id found"):
        SQLGenerationResponse(
            status=SQLGenerationStatus.CLARIFICATION_NEEDED,
            clarification_prompt="Choose one:",
            clarification_options=duplicate_options,
        )

    # Case-insensitive duplicate
    case_duplicate_options = [
        ClarificationOption(option_id="OptA", description="First interpretation"),
        ClarificationOption(option_id="opta", description="Second interpretation"),
    ]
    with pytest.raises(ValidationError, match="Duplicate clarification option_id found"):
        SQLGenerationResponse(
            status=SQLGenerationStatus.CLARIFICATION_NEEDED,
            clarification_prompt="Choose one:",
            clarification_options=case_duplicate_options,
        )


# ==============================================================================
# 3. Clarification Prompt Length Bounding Tests
# ==============================================================================

def test_clarification_prompt_length_1000_valid():
    """Verify clarification_prompt of exactly 1000 characters is accepted."""
    prompt_1000 = "a" * 1000
    resp = SQLGenerationResponse(
        status=SQLGenerationStatus.CLARIFICATION_NEEDED,
        clarification_prompt=prompt_1000,
    )
    assert resp.clarification_prompt == prompt_1000

    pipe_res = PipelineResult(
        question="Question",
        status=PipelineStatus.CLARIFICATION_NEEDED,
        clarification_prompt=prompt_1000,
    )
    assert pipe_res.clarification_prompt == prompt_1000


def test_clarification_prompt_length_1001_rejected():
    """Verify clarification_prompt of 1001 characters is rejected."""
    prompt_1001 = "a" * 1001
    with pytest.raises(ValidationError):
        SQLGenerationResponse(
            status=SQLGenerationStatus.CLARIFICATION_NEEDED,
            clarification_prompt=prompt_1001,
        )

    with pytest.raises(ValidationError):
        PipelineResult(
            question="Question",
            status=PipelineStatus.CLARIFICATION_NEEDED,
            clarification_prompt=prompt_1001,
        )


# ==============================================================================
# 4. SQLGenerationStatus and SQLGenerationResponse Clarification Invariants
# ==============================================================================

def test_sql_generation_status_enum():
    """Verify SQLGenerationStatus contains all 3 statuses."""
    assert SQLGenerationStatus.ANSWERABLE == "answerable"
    assert SQLGenerationStatus.UNSUPPORTED == "unsupported"
    assert SQLGenerationStatus.CLARIFICATION_NEEDED == "clarification_needed"


def test_sql_generation_response_clarification_valid():
    """Verify valid CLARIFICATION_NEEDED response with prompt and options."""
    resp = SQLGenerationResponse(
        status=SQLGenerationStatus.CLARIFICATION_NEEDED,
        clarification_prompt="Did you mean CGPA across departments or marks per course?",
        clarification_options=[
            ClarificationOption(option_id="opt1", description="Average CGPA by department"),
            ClarificationOption(option_id="opt2", description="Marks per course offering"),
        ],
    )
    assert resp.status == SQLGenerationStatus.CLARIFICATION_NEEDED
    assert resp.sql is None
    assert resp.clarification_prompt is not None
    assert len(resp.clarification_options) == 2


def test_sql_generation_response_clarification_with_explanation():
    """Verify valid CLARIFICATION_NEEDED response with explanation instead of prompt."""
    resp = SQLGenerationResponse(
        status=SQLGenerationStatus.CLARIFICATION_NEEDED,
        explanation="The request is ambiguous because student performance can be measured by CGPA or marks.",
    )
    assert resp.status == SQLGenerationStatus.CLARIFICATION_NEEDED
    assert resp.sql is None


def test_sql_generation_response_clarification_forbids_sql():
    """Verify CLARIFICATION_NEEDED rejects responses containing candidate SQL."""
    with pytest.raises(ValidationError, match="Clarification response must not contain SQL"):
        SQLGenerationResponse(
            status=SQLGenerationStatus.CLARIFICATION_NEEDED,
            sql="SELECT * FROM Students",
            clarification_prompt="Which students?",
        )


def test_sql_generation_response_clarification_requires_prompt_or_explanation():
    """Verify CLARIFICATION_NEEDED requires non-empty clarification_prompt or explanation."""
    with pytest.raises(ValidationError, match="requires a non-empty clarification_prompt or explanation"):
        SQLGenerationResponse(
            status=SQLGenerationStatus.CLARIFICATION_NEEDED,
        )


def test_sql_generation_response_backward_compatibility():
    """Verify existing ANSWERABLE and UNSUPPORTED responses remain fully valid."""
    resp_ans = SQLGenerationResponse(
        status=SQLGenerationStatus.ANSWERABLE,
        sql="SELECT TOP 5 * FROM Students",
    )
    assert resp_ans.status == SQLGenerationStatus.ANSWERABLE
    assert resp_ans.sql == "SELECT TOP 5 * FROM Students"

    resp_uns = SQLGenerationResponse(
        status=SQLGenerationStatus.UNSUPPORTED,
        explanation="Schema does not contain this information.",
    )
    assert resp_uns.status == SQLGenerationStatus.UNSUPPORTED
    assert resp_uns.sql is None


def test_sql_generation_response_answerable_rejects_clarification_prompt():
    """Verify ANSWERABLE response with clarification_prompt is rejected."""
    with pytest.raises(ValidationError, match="Answerable response must not contain clarification_prompt"):
        SQLGenerationResponse(
            status=SQLGenerationStatus.ANSWERABLE,
            sql="SELECT TOP 5 * FROM Students",
            clarification_prompt="Which students?",
        )


def test_sql_generation_response_answerable_rejects_clarification_options():
    """Verify ANSWERABLE response with clarification_options is rejected."""
    with pytest.raises(ValidationError, match="Answerable response must not contain clarification_options"):
        SQLGenerationResponse(
            status=SQLGenerationStatus.ANSWERABLE,
            sql="SELECT TOP 5 * FROM Students",
            clarification_options=[ClarificationOption(option_id="opt1", description="Choice A")],
        )


def test_sql_generation_response_unsupported_rejects_clarification_prompt():
    """Verify UNSUPPORTED response with clarification_prompt is rejected."""
    with pytest.raises(ValidationError, match="Unsupported response must not contain clarification_prompt"):
        SQLGenerationResponse(
            status=SQLGenerationStatus.UNSUPPORTED,
            explanation="Schema does not contain salary.",
            clarification_prompt="Which salary?",
        )


def test_sql_generation_response_unsupported_rejects_clarification_options():
    """Verify UNSUPPORTED response with clarification_options is rejected."""
    with pytest.raises(ValidationError, match="Unsupported response must not contain clarification_options"):
        SQLGenerationResponse(
            status=SQLGenerationStatus.UNSUPPORTED,
            explanation="Schema does not contain salary.",
            clarification_options=[ClarificationOption(option_id="opt1", description="Choice A")],
        )


def test_sql_generation_response_clarification_explanation_only_with_options():
    """Verify valid explanation-only CLARIFICATION_NEEDED response allows clarification_options."""
    resp = SQLGenerationResponse(
        status=SQLGenerationStatus.CLARIFICATION_NEEDED,
        explanation="Multiple interpretations exist.",
        clarification_options=[
            ClarificationOption(option_id="opt1", description="Interpretation A"),
        ],
    )
    assert resp.status == SQLGenerationStatus.CLARIFICATION_NEEDED
    assert resp.clarification_prompt is None
    assert resp.explanation == "Multiple interpretations exist."
    assert len(resp.clarification_options) == 1


def test_sql_generation_response_clarification_whitespace_only_rejected():
    """Verify CLARIFICATION_NEEDED with whitespace-only prompt and explanation is rejected."""
    with pytest.raises(ValidationError, match="requires a non-empty clarification_prompt or explanation"):
        SQLGenerationResponse(
            status=SQLGenerationStatus.CLARIFICATION_NEEDED,
            clarification_prompt="   ",
            explanation="   ",
        )


def test_sql_generation_response_answerable_whitespace_prompt_normalized_to_none():
    """Verify whitespace-only clarification_prompt on ANSWERABLE normalizes to None and does not error."""
    resp = SQLGenerationResponse(
        status=SQLGenerationStatus.ANSWERABLE,
        sql="SELECT 1",
        clarification_prompt="   ",
    )
    assert resp.clarification_prompt is None


# ==============================================================================
# 5. ConversationTurnContext Hardening & Semantics Tests
# ==============================================================================

def test_conversation_turn_context_valid():
    """Verify valid ConversationTurnContext creation."""
    turn = ConversationTurnContext(
        role="user",
        user_question="Show top 5 students by CGPA",
        resolved_intent="Retrieve top 5 students ranked by CGPA descending",
        result_columns=["EnrollmentNumber", "FullName", "CGPA"],
    )
    assert turn.role == "user"
    assert turn.user_question == "Show top 5 students by CGPA"
    assert turn.resolved_intent is not None
    assert turn.result_columns == ["EnrollmentNumber", "FullName", "CGPA"]


def test_conversation_turn_context_rejects_extra_fields():
    """Verify ConversationTurnContext strictly forbids unexpected extra fields."""
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ConversationTurnContext(
            user_question="Show students",
            extra_field="unauthorized",  # type: ignore[call-arg]
        )


def test_conversation_turn_context_rejects_alternate_sql_fields():
    """Verify unexpected SQL-like fields are explicitly rejected."""
    for forbidden_field in FORBIDDEN_SQL_FIELDS:
        with pytest.raises(ValidationError, match=f"Field '{forbidden_field}' is forbidden"):
            ConversationTurnContext.model_validate(
                {
                    "user_question": "Show students",
                    forbidden_field: "SELECT * FROM Students",
                }
            )


def test_conversation_turn_context_legacy_user_content_fallback():
    """Verify dict with 'content' is mapped to 'user_question' for user turns."""
    # Role omitted defaults to user
    turn_implicit = ConversationTurnContext.model_validate(
        {"content": "Show top 5 students"}
    )
    assert turn_implicit.user_question == "Show top 5 students"
    assert turn_implicit.role == "user"

    # Explicit role user
    turn_explicit = ConversationTurnContext.model_validate(
        {"role": "user", "content": "Show top 5 students"}
    )
    assert turn_explicit.user_question == "Show top 5 students"
    assert turn_explicit.role == "user"


def test_conversation_turn_context_assistant_rejected():
    """Verify role='assistant' is rejected explaining user-question semantic requirement."""
    with pytest.raises(
        ValidationError,
        match="ConversationTurnContext strictly represents prior user-question interactions. Role 'assistant' is not permitted.",
    ):
        ConversationTurnContext(
            role="assistant",
            user_question="Show top 5 students",
            result_columns=["Id", "Name"],
        )


def test_conversation_turn_context_assistant_content_rejected():
    """Verify assistant turn with legacy 'content' is rejected."""
    with pytest.raises(
        ValidationError,
        match="ConversationTurnContext strictly represents prior user-question interactions. Role 'assistant' is not permitted.",
    ):
        ConversationTurnContext.model_validate(
            {"role": "assistant", "content": "Here are the top 5 students"}
        )


def test_conversation_turn_context_role_validation():
    """Verify role validation accepts only 'user', defaults to 'user', and rejects 'assistant'."""
    # Explicit role="user" is accepted
    t_user = ConversationTurnContext(role="user", user_question="Q1")
    assert t_user.role == "user"

    # Omitted role retains intended backward-compatible behavior (defaults to "user")
    t_default = ConversationTurnContext(user_question="Q1")
    assert t_default.role == "user"

    # role="assistant" is rejected
    with pytest.raises(ValidationError, match="Role 'assistant' is not permitted"):
        ConversationTurnContext(role="assistant", user_question="Q2")

    # Invalid roles are rejected
    with pytest.raises(ValidationError, match="Invalid role"):
        ConversationTurnContext(role="system", user_question="Q3")

    with pytest.raises(ValidationError, match="Invalid role"):
        ConversationTurnContext(role="admin", user_question="Q4")


def test_conversation_turn_context_rejects_empty_question():
    """Verify empty user_question is rejected."""
    with pytest.raises(ValidationError):
        ConversationTurnContext(user_question="")

    with pytest.raises(ValidationError):
        ConversationTurnContext(user_question="   ")


def test_conversation_turn_context_result_columns_validation():
    """Verify result_columns validation bounds and checks column names."""
    turn = ConversationTurnContext(
        user_question="Q1",
        result_columns=[" ColA ", "ColB"],
    )
    assert turn.result_columns == ["ColA", "ColB"]

    with pytest.raises(ValidationError, match="Column name in result_columns must be a non-empty string"):
        ConversationTurnContext(
            user_question="Q1",
            result_columns=["ColA", ""],
        )

    with pytest.raises(ValidationError, match="result_columns cannot exceed 50 columns"):
        ConversationTurnContext(
            user_question="Q1",
            result_columns=[f"Col_{i}" for i in range(51)],
        )


# ==============================================================================
# 6. SQLGenerationRequest Conversation Context Bounding
# ==============================================================================

def test_sql_generation_request_with_valid_context(mock_schema):
    """Verify SQLGenerationRequest with 1 to 3 valid turns."""
    turns = [
        ConversationTurnContext(user_question="Show students", result_columns=["Id", "Name"]),
        ConversationTurnContext(user_question="Filter to active"),
    ]
    req = SQLGenerationRequest(
        question="Sort by name",
        schema_context=mock_schema,
        conversation_context=turns,
    )
    assert len(req.conversation_context) == 2
    assert req.conversation_context[0].user_question == "Show students"


def test_sql_generation_request_omitted_context(mock_schema):
    """Verify SQLGenerationRequest defaults conversation_context to empty list."""
    req = SQLGenerationRequest(
        question="Show students",
        schema_context=mock_schema,
    )
    assert req.conversation_context == []


def test_sql_generation_request_none_context(mock_schema):
    """Verify SQLGenerationRequest coerces conversation_context=None to empty list."""
    req = SQLGenerationRequest(
        question="Show students",
        schema_context=mock_schema,
        conversation_context=None,
    )
    assert req.conversation_context == []


def test_sql_generation_request_coerces_dicts(mock_schema):
    """Verify SQLGenerationRequest coerces raw dictionaries into ConversationTurnContext."""
    req = SQLGenerationRequest(
        question="Show students",
        schema_context=mock_schema,
        conversation_context=[
            {"role": "user", "content": "Initial query"},
        ],
    )
    assert len(req.conversation_context) == 1
    assert isinstance(req.conversation_context[0], ConversationTurnContext)
    assert req.conversation_context[0].user_question == "Initial query"


def test_sql_generation_request_rejects_excessive_turns(mock_schema):
    """Verify SQLGenerationRequest rejects more than MAX_CONVERSATION_TURNS (3)."""
    turns = [
        ConversationTurnContext(user_question=f"Q{i}") for i in range(4)
    ]
    with pytest.raises(ValidationError, match="conversation_context cannot exceed 3 turns"):
        SQLGenerationRequest(
            question="Follow up",
            schema_context=mock_schema,
            conversation_context=turns,
        )


# ==============================================================================
# 7. QueryPilotState and Initial State Validation Tests
# ==============================================================================

def test_create_initial_state_with_valid_context(mock_schema):
    """Verify create_initial_state stores valid conversation context."""
    turns = [
        {"role": "user", "user_question": "Show students"},
        {"role": "user", "user_question": "Filter to active", "result_columns": ["Id", "Name"]},
    ]
    state = create_initial_state(
        "Now filter to Department 1",
        schema_context=mock_schema,
        conversation_context=turns,
    )
    assert "conversation_context" in state
    assert len(state["conversation_context"]) == 2


def test_create_initial_state_excessive_turns_rejected(mock_schema):
    """Verify create_initial_state raises ValueError for > 3 turns."""
    turns = [{"user_question": f"Q{i}"} for i in range(4)]
    with pytest.raises(ValueError, match="conversation_context cannot exceed 3 turns"):
        create_initial_state(
            "Question",
            schema_context=mock_schema,
            conversation_context=turns,
        )


def test_create_initial_state_malformed_turn_rejected(mock_schema):
    """Verify create_initial_state raises error for malformed turn structure."""
    with pytest.raises(TypeError, match="must be a dict or ConversationTurnContext"):
        create_initial_state(
            "Question",
            schema_context=mock_schema,
            conversation_context=["not a dict"],  # type: ignore[arg-type]
        )

    with pytest.raises(ValueError, match="must contain 'user_question'"):
        create_initial_state(
            "Question",
            schema_context=mock_schema,
            conversation_context=[{"role": "user"}],
        )

    # Forbids executed_sql
    with pytest.raises(ValueError, match="must not contain 'executed_sql'"):
        create_initial_state(
            "Question",
            schema_context=mock_schema,
            conversation_context=[{"user_question": "Q1", "executed_sql": "SELECT 1"}],
        )

    # Forbids alternate SQL field
    with pytest.raises(ValueError, match="must not contain 'sql'"):
        create_initial_state(
            "Question",
            schema_context=mock_schema,
            conversation_context=[{"user_question": "Q1", "sql": "SELECT 1"}],
        )

    # Forbids unexpected extra keys
    with pytest.raises(ValueError, match="contains unexpected fields"):
        create_initial_state(
            "Question",
            schema_context=mock_schema,
            conversation_context=[{"user_question": "Q1", "extra_key": "disallowed"}],
        )


def test_create_initial_state_assistant_turn_rejected(mock_schema):
    """Verify create_initial_state rejects assistant turns with clear validation error."""
    with pytest.raises(
        ValueError,
        match="Conversation turn at index 0 has role 'assistant'. "
        "Conversation context strictly represents prior user-question interactions. Role must be 'user'.",
    ):
        create_initial_state(
            "Question",
            schema_context=mock_schema,
            conversation_context=[{"role": "assistant", "user_question": "Assistant question"}],
        )


def test_create_initial_state_assistant_content_rejected(mock_schema):
    """Verify create_initial_state rejects assistant turns with only content."""
    with pytest.raises(ValueError, match="Role must be 'user'"):
        create_initial_state(
            "Question",
            schema_context=mock_schema,
            conversation_context=[{"role": "assistant", "content": "Assistant prose"}],
        )


def test_validate_initial_state_checks_context():
    """Verify validate_initial_state enforces conversation_context bounds and types."""
    valid_state = {
        "question": "Show students",
        "conversation_context": [
            {"user_question": "Prior turn"},
        ],
    }
    validated = validate_initial_state(valid_state)
    assert validated["question"] == "Show students"

    # Excessive turns
    invalid_excessive = {
        "question": "Show students",
        "conversation_context": [{"user_question": f"Q{i}"} for i in range(4)],
    }
    with pytest.raises(ValueError, match="conversation_context cannot exceed 3 turns"):
        validate_initial_state(invalid_excessive)

    # Invalid type for conversation_context
    invalid_type = {
        "question": "Show students",
        "conversation_context": "not a list",
    }
    with pytest.raises(TypeError, match="Field 'conversation_context' must be a list"):
        validate_initial_state(invalid_type)


# ==============================================================================
# 8. PipelineStatus and PipelineResult Clarification Tests
# ==============================================================================

def test_pipeline_status_clarification_enum():
    """Verify PipelineStatus contains CLARIFICATION_NEEDED."""
    assert PipelineStatus.CLARIFICATION_NEEDED == "CLARIFICATION_NEEDED"


def test_pipeline_result_clarification_valid():
    """Verify PipelineResult with CLARIFICATION_NEEDED."""
    res = PipelineResult(
        question="Compare performance",
        status=PipelineStatus.CLARIFICATION_NEEDED,
        generation_status=SQLGenerationStatus.CLARIFICATION_NEEDED,
        clarification_prompt="Please clarify if you mean CGPA or assessment marks.",
        clarification_options=[
            ClarificationOption(option_id="opt1", description="CGPA comparison"),
            ClarificationOption(option_id="opt2", description="Marks comparison"),
        ],
    )
    assert res.is_clarification_needed is True
    assert res.is_success is False
    assert res.is_unsupported is False
    assert res.generated_sql is None
    assert res.execution_result is None

    # Serialization test
    d = res.to_dict()
    assert d["status"] == "CLARIFICATION_NEEDED"
    assert d["clarification_prompt"] == "Please clarify if you mean CGPA or assessment marks."
    assert len(d["clarification_options"]) == 2


def test_pipeline_result_clarification_with_none_generation_status():
    """Verify PipelineResult allows generation_status to be None for existing callers."""
    res = PipelineResult(
        question="Compare performance",
        status=PipelineStatus.CLARIFICATION_NEEDED,
        generation_status=None,
        clarification_prompt="Please clarify.",
    )
    assert res.is_clarification_needed is True


def test_pipeline_result_clarification_rejects_contradictory_generation_status():
    """Verify PipelineResult rejects contradictory generation_status values."""
    with pytest.raises(ValueError, match="Contradictory generation_status 'answerable'"):
        PipelineResult(
            question="Compare performance",
            status=PipelineStatus.CLARIFICATION_NEEDED,
            generation_status=SQLGenerationStatus.ANSWERABLE,
            clarification_prompt="Please clarify.",
        )

    with pytest.raises(ValueError, match="Contradictory generation_status 'unsupported'"):
        PipelineResult(
            question="Compare performance",
            status=PipelineStatus.CLARIFICATION_NEEDED,
            generation_status=SQLGenerationStatus.UNSUPPORTED,
            clarification_prompt="Please clarify.",
        )


def test_pipeline_result_clarification_forbids_generated_sql():
    """Verify PipelineResult rejects CLARIFICATION_NEEDED if generated_sql is set."""
    with pytest.raises(ValueError, match="Clarification result must not contain generated_sql"):
        PipelineResult(
            question="Compare performance",
            status=PipelineStatus.CLARIFICATION_NEEDED,
            generated_sql="SELECT * FROM Students",
        )


def test_pipeline_result_clarification_forbids_execution_result():
    """Verify PipelineResult rejects CLARIFICATION_NEEDED if execution_result is set."""
    with pytest.raises(ValueError, match="Clarification result must not contain execution_result"):
        PipelineResult(
            question="Compare performance",
            status=PipelineStatus.CLARIFICATION_NEEDED,
            execution_result=QueryResult(columns=["Id"], rows=[{"Id": 1}], row_count=1, truncated=False),
        )


def test_pipeline_result_clarification_forbids_validation_result():
    """Verify PipelineResult rejects CLARIFICATION_NEEDED if validation_result is set."""
    from query_pilot.sql.models import ValidationResult

    with pytest.raises(ValueError, match="Clarification result must not contain validation_result"):
        PipelineResult(
            question="Compare performance",
            status=PipelineStatus.CLARIFICATION_NEEDED,
            validation_result=ValidationResult(allowed=False),
        )


def test_pipeline_result_clarification_bounds_options():
    """Verify PipelineResult enforces MAX_CLARIFICATION_OPTIONS and unique option IDs."""
    options_6 = [
        ClarificationOption(option_id=f"opt_{i}", description=f"Description {i}")
        for i in range(MAX_CLARIFICATION_OPTIONS + 1)
    ]
    with pytest.raises(ValidationError, match="clarification_options cannot exceed 5 options"):
        PipelineResult(
            question="Question",
            status=PipelineStatus.CLARIFICATION_NEEDED,
            clarification_options=options_6,
        )

    duplicate_options = [
        ClarificationOption(option_id="dup", description="Desc 1"),
        ClarificationOption(option_id="dup", description="Desc 2"),
    ]
    with pytest.raises(ValidationError, match="Duplicate clarification option_id found"):
        PipelineResult(
            question="Question",
            status=PipelineStatus.CLARIFICATION_NEEDED,
            clarification_options=duplicate_options,
        )
