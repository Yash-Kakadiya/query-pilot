"""Focused multi-turn tests for Business Metric matching with conversation context.

Verifies QueryPilot B21-A2.4 requirements:
1. Current-question relevance:
   A metric relevant to the current question should match normally.
2. Follow-up continuity:
   A short follow-up question using pronouns/references should use relevant prior resolved_intent.
3. Topic change:
   A new question unrelated to the previous turn must NOT receive a metric solely
   because the previous resolved_intent contains matching terms.
4. Stale context:
   Obsolete resolved_intent from a previous turn must not leak into an unrelated question.
5. Missing intent:
   Matching should continue to work using current question when resolved_intent is absent.
6. No accidental broadening:
   Previous intent must not bypass existing relevance thresholds or alignment requirements.
7. Ambiguous pronoun topic-change:
   Sentences with internal pronouns (e.g. 'their') bound to current sentence nouns
   must not falsely trigger adoption of prior turn's intent.
"""

import json
from unittest.mock import MagicMock
import pytest

from query_pilot.api.models import BusinessMetricDefinition
from query_pilot.db.models import ColumnMetadata, DatabaseSchema, TableMetadata
from query_pilot.rag.metrics_matcher import match_business_metrics
from query_pilot.sql.gemini_generator import GeminiSQLGenerator
from query_pilot.sql.generation import (
    ConversationTurnContext,
    SQLGenerationRequest,
)


# ==============================================================================
# In-Memory Test Schema & Mock Generator Fixtures
# ==============================================================================

@pytest.fixture
def multi_turn_schema() -> DatabaseSchema:
    """Deterministic in-memory schema with Departments, Students, and Courses."""
    departments_table = TableMetadata(
        schema_name="dbo",
        table_name="Departments",
        columns=[
            ColumnMetadata(name="Id", data_type="int", nullable=False, primary_key=True),
            ColumnMetadata(name="DepartmentName", data_type="varchar(100)", nullable=False),
        ],
        primary_keys=["Id"],
    )
    students_table = TableMetadata(
        schema_name="dbo",
        table_name="Students",
        columns=[
            ColumnMetadata(name="Id", data_type="int", nullable=False, primary_key=True),
            ColumnMetadata(name="DepartmentId", data_type="int", nullable=False),
            ColumnMetadata(name="AttendancePercentage", data_type="float", nullable=False),
            ColumnMetadata(name="CGPA", data_type="float", nullable=False),
        ],
        primary_keys=["Id"],
    )
    courses_table = TableMetadata(
        schema_name="dbo",
        table_name="Courses",
        columns=[
            ColumnMetadata(name="CourseId", data_type="int", nullable=False, primary_key=True),
            ColumnMetadata(name="Title", data_type="varchar(100)", nullable=False),
            ColumnMetadata(name="Credits", data_type="int", nullable=False),
        ],
        primary_keys=["CourseId"],
    )
    return DatabaseSchema(
        database_name="AcademicDW",
        tables=[departments_table, students_table, courses_table],
        is_model_facing=True,
    )


def _mock_gemini_gen() -> GeminiSQLGenerator:
    """Helper to instantiate GeminiSQLGenerator with a mocked client."""
    gen = GeminiSQLGenerator(api_key="test-mock-api-key", enable_retrieval=False)
    mock_resp = MagicMock()
    mock_resp.text = json.dumps({"status": "answerable", "sql": "SELECT 1", "explanation": "Test"})
    mock_resp.usage_metadata = None
    gen.client.models.generate_content = MagicMock(return_value=mock_resp)
    return gen


# ==============================================================================
# Scenario 1: Current-Question Relevance
# ==============================================================================

def test_01_current_question_relevance_matches_normally(multi_turn_schema):
    """1. Current-question relevance: metric relevant to current question matches normally."""
    metric = BusinessMetricDefinition(
        name="At-Risk Attendance",
        description="Students with attendance percentage below 75 percent.",
        calculation_formula="AttendancePercentage < 75.0",
        applicable_tables=["Students"],
    )

    prior_turn = ConversationTurnContext(
        user_question="Hello, can you help me query the database?",
        resolved_intent="General system greeting",
    )

    # Via direct matcher
    matched = match_business_metrics(
        metrics=[metric],
        question="Which students have at-risk attendance?",
        schema=multi_turn_schema,
        resolved_intent=prior_turn.resolved_intent,
    )
    assert len(matched) == 1
    assert matched[0].name == "At-Risk Attendance"

    # Via full generation prompt path
    gen = _mock_gemini_gen()
    req = SQLGenerationRequest(
        question="Which students have at-risk attendance?",
        schema_context=multi_turn_schema,
        conversation_context=[prior_turn],
        business_metrics=[metric],
    )
    gen.generate(req)
    prompt = gen.client.models.generate_content.call_args.kwargs["contents"]
    assert "=== BUSINESS METRIC DEFINITIONS (INFORMATIONAL ONLY) ===" in prompt
    assert "[METRIC] At-Risk Attendance:" in prompt


# ==============================================================================
# Scenario 2: Follow-Up Continuity
# ==============================================================================

def test_02_follow_up_continuity_uses_prior_resolved_intent(multi_turn_schema):
    """2. Follow-up continuity: pronoun/reference follow-up matches using prior resolved_intent."""
    metric = BusinessMetricDefinition(
        name="At-Risk Attendance",
        description="Students with attendance percentage below 75 percent.",
        calculation_formula="AttendancePercentage < 75.0",
        applicable_tables=["Students"],
    )

    # Turn 1 asked about at-risk attendance
    prior_turn = ConversationTurnContext(
        user_question="Find all students with attendance issues.",
        resolved_intent="At-Risk Attendance rate for enrolled learners",
    )

    # Turn 2 question uses a pronoun reference ("them") without explicit metric terms
    follow_up_question = "Filter them by computer science department."

    # Direct matcher
    matched = match_business_metrics(
        metrics=[metric],
        question=follow_up_question,
        schema=multi_turn_schema,
        resolved_intent=prior_turn.resolved_intent,
    )
    assert len(matched) == 1
    assert matched[0].name == "At-Risk Attendance"

    # Full generation path
    gen = _mock_gemini_gen()
    req = SQLGenerationRequest(
        question=follow_up_question,
        schema_context=multi_turn_schema,
        conversation_context=[prior_turn],
        business_metrics=[metric],
    )
    gen.generate(req)
    prompt = gen.client.models.generate_content.call_args.kwargs["contents"]
    assert "=== BUSINESS METRIC DEFINITIONS (INFORMATIONAL ONLY) ===" in prompt
    assert "[METRIC] At-Risk Attendance:" in prompt


# ==============================================================================
# Scenario 3: Topic Change (Defect Fixed)
# ==============================================================================

def test_03_topic_change_unrelated_question_excluded(multi_turn_schema):
    """3. Topic change: an unrelated new question does NOT receive metric from prior intent."""
    metric = BusinessMetricDefinition(
        name="At-Risk Attendance",
        description="Students with attendance percentage below 75 percent.",
        calculation_formula="AttendancePercentage < 75.0",
        applicable_tables=["Students"],
    )

    # Turn 1: investigated student attendance
    prior_turn = ConversationTurnContext(
        user_question="Who has poor attendance?",
        resolved_intent="At-Risk Attendance for students",
    )

    # Turn 2: complete topic change to academic departments
    new_topic_question = "List all academic departments ordered by department name."

    # Direct matcher: must NOT match because new topic question has zero attendance alignment
    matched = match_business_metrics(
        metrics=[metric],
        question=new_topic_question,
        schema=multi_turn_schema,
        resolved_intent=prior_turn.resolved_intent,
    )
    assert matched == []

    # Full generation path: prompt must NOT contain business metric definitions
    gen = _mock_gemini_gen()
    req = SQLGenerationRequest(
        question=new_topic_question,
        schema_context=multi_turn_schema,
        conversation_context=[prior_turn],
        business_metrics=[metric],
    )
    gen.generate(req)
    prompt = gen.client.models.generate_content.call_args.kwargs["contents"]
    assert "=== BUSINESS METRIC DEFINITIONS" not in prompt


# ==============================================================================
# Scenario 4: Stale Context (Defect Fixed)
# ==============================================================================

def test_04_stale_context_unrelated_intent_excluded(multi_turn_schema):
    """4. Stale context: obsolete resolved_intent from prior turn does not leak into unrelated query."""
    metric = BusinessMetricDefinition(
        name="Delinquent Tuition Surcharge",
        description="Late payment surcharge on delinquent tuition installments.",
        calculation_formula="TuitionDue * 1.15",
    )

    # Turn 1 had financial fee inquiry
    prior_turn = ConversationTurnContext(
        user_question="How are delinquent tuition penalties calculated?",
        resolved_intent="Delinquent Tuition Surcharge calculations",
    )

    # Turn 2 user asks completely unrelated course curriculum question
    unrelated_question = "What are the prerequisite courses for Quantum Mechanics?"

    # Direct matcher: stale fee intent must NOT pull in fee metric for course question
    matched = match_business_metrics(
        metrics=[metric],
        question=unrelated_question,
        schema=multi_turn_schema,
        resolved_intent=prior_turn.resolved_intent,
    )
    assert matched == []

    # Full generation path: prompt remains clean
    gen = _mock_gemini_gen()
    req = SQLGenerationRequest(
        question=unrelated_question,
        schema_context=multi_turn_schema,
        conversation_context=[prior_turn],
        business_metrics=[metric],
    )
    gen.generate(req)
    prompt = gen.client.models.generate_content.call_args.kwargs["contents"]
    assert "=== BUSINESS METRIC DEFINITIONS" not in prompt


# ==============================================================================
# Scenario 5: Missing Intent
# ==============================================================================

def test_05_missing_intent_preserves_question_matching(multi_turn_schema):
    """5. Missing intent: matching continues to work using current question when resolved_intent is None or empty."""
    metric = BusinessMetricDefinition(
        name="Dean's Honor List",
        description="Students with cumulative GPA above 3.80.",
        calculation_formula="CGPA >= 3.80",
        applicable_tables=["Students"],
    )

    # Turn with None resolved_intent
    turn_none_intent = ConversationTurnContext(
        user_question="Prior question without resolved intent",
        resolved_intent=None,
    )

    matched = match_business_metrics(
        metrics=[metric],
        question="Which students qualify for Dean's Honor List?",
        schema=multi_turn_schema,
        resolved_intent=turn_none_intent.resolved_intent,
    )
    assert len(matched) == 1
    assert matched[0].name == "Dean's Honor List"

    # Turn with whitespace-only resolved_intent
    matched_ws = match_business_metrics(
        metrics=[metric],
        question="Which students qualify for Dean's Honor List?",
        schema=multi_turn_schema,
        resolved_intent="   \t\n  ",
    )
    assert len(matched_ws) == 1
    assert matched_ws[0].name == "Dean's Honor List"


# ==============================================================================
# Scenario 6: No Accidental Broadening
# ==============================================================================

def test_06_no_accidental_broadening_relevance_threshold_preserved(multi_turn_schema):
    """6. No accidental broadening: previous intent cannot bypass threshold or alignment rules."""
    metric = BusinessMetricDefinition(
        name="Honors Distinction",
        description="Special Latin honors award for graduating seniors.",
        applicable_tables=["Students"],
    )

    # Prior intent containing only stop words and generic database filler tokens
    generic_intent = "list all data tables and records from query"
    unrelated_question = "What is the department building location?"

    matched = match_business_metrics(
        metrics=[metric],
        question=unrelated_question,
        schema=multi_turn_schema,
        resolved_intent=generic_intent,
    )
    # Generic intent filler must be suppressed by STOP_WORDS; score remains 0.0 < 2.0
    assert matched == []

    # Prior intent with only 1 single description token match (insufficient for Rule 6 alignment)
    single_token_intent = "distinction"
    unrelated_q = "Show department budget"
    matched_single = match_business_metrics(
        metrics=[metric],
        question=unrelated_q,
        schema=multi_turn_schema,
        resolved_intent=single_token_intent,
    )
    assert matched_single == []


# ==============================================================================
# Scenario 7: Ambiguous Pronoun Topic-Change Case
# ==============================================================================

def test_07_ambiguous_pronoun_topic_change_excluded(multi_turn_schema):
    """7. Ambiguous pronoun case: sentences with internally bound pronouns (e.g. 'their')
    preceded by substantive sentence nouns do NOT adopt prior turn's unrelated intent.
    """
    metric = BusinessMetricDefinition(
        name="At-Risk Attendance",
        description="Students with attendance percentage below 75 percent.",
        calculation_formula="AttendancePercentage < 75.0",
        applicable_tables=["Students"],
    )

    # Turn 1 investigated student attendance
    prior_turn = ConversationTurnContext(
        user_question="Find students with attendance below threshold",
        resolved_intent="At-Risk Attendance for students",
    )

    # Turn 2 asks about departments and their degree requirements.
    # Note: 'their' is present, but refers to 'departments' within this sentence, NOT attendance!
    ambiguous_question = "Which departments have graduate programs and what are their degree requirements?"

    # Direct matcher: must NOT match attendance metric
    matched = match_business_metrics(
        metrics=[metric],
        question=ambiguous_question,
        schema=multi_turn_schema,
        resolved_intent=prior_turn.resolved_intent,
    )
    assert matched == []

    # Full generation path: prompt must NOT contain attendance metric definitions
    gen = _mock_gemini_gen()
    req = SQLGenerationRequest(
        question=ambiguous_question,
        schema_context=multi_turn_schema,
        conversation_context=[prior_turn],
        business_metrics=[metric],
    )
    gen.generate(req)
    prompt = gen.client.models.generate_content.call_args.kwargs["contents"]
    assert "=== BUSINESS METRIC DEFINITIONS" not in prompt


# ==============================================================================
# Scenario 8: Adversarial Audit Cases (B21-A2.4R Self-Contained Imperatives)
# ==============================================================================

@pytest.mark.parametrize(
    "question_text",
    [
        "Filter the computer science courses by semester.",
        "Sort all departments by their names.",
        "Order the available products by price.",
        "Order products by price.",
        "And what are the prerequisites for advanced mathematics?",
        "Only show students enrolled in the current semester.",
        "What are their degree requirements?",
    ],
)
def test_08_adversarial_self_contained_imperatives_excluded(multi_turn_schema, question_text):
    """8. Adversarial cases from B21-A2.4R: Self-contained questions must NOT inherit prior intent.

    Verifies both direct matcher exclusion and prompt cleanliness.
    """
    metric = BusinessMetricDefinition(
        name="At-Risk Attendance",
        description="Students with attendance percentage below 75 percent.",
        calculation_formula="AttendancePercentage < 75.0",
        applicable_tables=["Students"],
    )

    prior_turn = ConversationTurnContext(
        user_question="Find all students with attendance issues.",
        resolved_intent="At-Risk Attendance for enrolled learners",
    )

    # 1. Direct matcher assertion: metric must be excluded
    matched = match_business_metrics(
        metrics=[metric],
        question=question_text,
        schema=multi_turn_schema,
        resolved_intent=prior_turn.resolved_intent,
    )
    assert matched == [], f"Expected zero matches for self-contained question: '{question_text}'"

    # 2. Generation prompt assertion: metric must NOT appear in prompt
    gen = _mock_gemini_gen()
    req = SQLGenerationRequest(
        question=question_text,
        schema_context=multi_turn_schema,
        conversation_context=[prior_turn],
        business_metrics=[metric],
    )
    gen.generate(req)
    prompt = gen.client.models.generate_content.call_args.kwargs["contents"]
    assert "=== BUSINESS METRIC DEFINITIONS" not in prompt


# ==============================================================================
# Scenario 9: Adversarial Genuine Follow-Ups (Pronoun & Elliptical)
# ==============================================================================

@pytest.mark.parametrize(
    "follow_up_text",
    [
        "Filter them by computer science department.",
        "Filter these by computer science department.",
        "Filter by computer science department.",
        "Sort by department name.",
        "Narrow down to computer science department.",
        "Only filter by computer science department.",
    ],
)
def test_09_adversarial_genuine_follow_ups_included(multi_turn_schema, follow_up_text):
    """9. Genuine follow-ups (pronoun or elliptical) must correctly inherit prior intent."""
    metric = BusinessMetricDefinition(
        name="At-Risk Attendance",
        description="Students with attendance percentage below 75 percent.",
        calculation_formula="AttendancePercentage < 75.0",
        applicable_tables=["Students"],
    )

    prior_turn = ConversationTurnContext(
        user_question="Find all students with attendance issues.",
        resolved_intent="At-Risk Attendance for enrolled learners",
    )

    # 1. Direct matcher assertion: metric must be selected
    matched = match_business_metrics(
        metrics=[metric],
        question=follow_up_text,
        schema=multi_turn_schema,
        resolved_intent=prior_turn.resolved_intent,
    )
    assert len(matched) == 1
    assert matched[0].name == "At-Risk Attendance"

    # 2. Generation prompt assertion: metric must reach prompt
    gen = _mock_gemini_gen()
    req = SQLGenerationRequest(
        question=follow_up_text,
        schema_context=multi_turn_schema,
        conversation_context=[prior_turn],
        business_metrics=[metric],
    )
    gen.generate(req)
    prompt = gen.client.models.generate_content.call_args.kwargs["contents"]
    assert "=== BUSINESS METRIC DEFINITIONS (INFORMATIONAL ONLY) ===" in prompt
    assert "[METRIC] At-Risk Attendance:" in prompt


# ==============================================================================
# Scenario 10: Imperative Question with Current-Question Relevance
# ==============================================================================

def test_10_imperative_question_with_current_question_relevance(multi_turn_schema):
    """10. Imperative sentences that explicitly mention the metric terms match normally via question relevance."""
    metric = BusinessMetricDefinition(
        name="At-Risk Attendance",
        description="Students with attendance percentage below 75 percent.",
        calculation_formula="AttendancePercentage < 75.0",
        applicable_tables=["Students"],
    )

    unrelated_prior = ConversationTurnContext(
        user_question="What are the tuition fees?",
        resolved_intent="Fee payment schedules",
    )

    # Self-contained imperative query that contains attendance terms
    question = "Filter the students by attendance percentage below threshold."

    matched = match_business_metrics(
        metrics=[metric],
        question=question,
        schema=multi_turn_schema,
        resolved_intent=unrelated_prior.resolved_intent,
    )
    assert len(matched) == 1
    assert matched[0].name == "At-Risk Attendance"

    # Generation path
    gen = _mock_gemini_gen()
    req = SQLGenerationRequest(
        question=question,
        schema_context=multi_turn_schema,
        conversation_context=[unrelated_prior],
        business_metrics=[metric],
    )
    gen.generate(req)
    prompt = gen.client.models.generate_content.call_args.kwargs["contents"]
    assert "=== BUSINESS METRIC DEFINITIONS (INFORMATIONAL ONLY) ===" in prompt


# ==============================================================================
# Scenario 11: Consecutive Requests Request Isolation
# ==============================================================================

def test_11_consecutive_multi_turn_requests_isolated(multi_turn_schema):
    """11. Consecutive requests alternating between follow-up and self-contained do not leak state."""
    metric = BusinessMetricDefinition(
        name="At-Risk Attendance",
        description="Students with attendance percentage below 75 percent.",
        calculation_formula="AttendancePercentage < 75.0",
        applicable_tables=["Students"],
    )

    gen = _mock_gemini_gen()

    # Request A: genuine follow-up inheriting attendance intent
    req_a = SQLGenerationRequest(
        question="Filter them by computer science department.",
        schema_context=multi_turn_schema,
        conversation_context=[
            ConversationTurnContext(
                user_question="Find students with attendance issues",
                resolved_intent="At-Risk Attendance for students",
            )
        ],
        business_metrics=[metric],
    )
    gen.generate(req_a)
    prompt_a = gen.client.models.generate_content.call_args.kwargs["contents"]
    assert "[METRIC] At-Risk Attendance:" in prompt_a

    # Request B: immediately following request with a self-contained imperative question
    req_b = SQLGenerationRequest(
        question="Sort all departments by their names.",
        schema_context=multi_turn_schema,
        conversation_context=[
            ConversationTurnContext(
                user_question="Find students with attendance issues",
                resolved_intent="At-Risk Attendance for students",
            )
        ],
        business_metrics=[metric],
    )
    gen.generate(req_b)
    prompt_b = gen.client.models.generate_content.call_args.kwargs["contents"]
    assert "=== BUSINESS METRIC DEFINITIONS" not in prompt_b


# ==============================================================================
# Scenario 12: SQLGenerationRequest Business Metrics Type Hardening
# ==============================================================================

def test_12_sql_generation_request_business_metrics_type_hardening(multi_turn_schema):
    """12. SQLGenerationRequest rejects non-BusinessMetricDefinition objects with TypeError."""
    with pytest.raises(TypeError) as exc:
        SQLGenerationRequest(
            question="Which students have at-risk attendance?",
            schema_context=multi_turn_schema,
            business_metrics=[{"name": "Dict Metric", "description": "Not an object"}],
        )
    assert "All business_metrics entries must be BusinessMetricDefinition instances" in str(exc.value)


# ==============================================================================
# Scenario 13: Entity Compatibility in Conversational Follow-Ups (B21-A2.6)
# ==============================================================================

def test_13_compatible_demonstrative_follow_up_matches(multi_turn_schema):
    """13a. Compatible demonstrative follow-up 'Show those students.' matches metric tied to Students."""
    metric = BusinessMetricDefinition(
        name="At-Risk Attendance",
        description="Students with attendance percentage below 75 percent.",
        calculation_formula="AttendancePercentage < 75.0",
        applicable_tables=["Students"],
    )
    prior_turn = ConversationTurnContext(
        user_question="Find all students with attendance issues.",
        resolved_intent="At-Risk Attendance for enrolled learners",
    )

    matched = match_business_metrics(
        metrics=[metric],
        question="Show those students.",
        schema=multi_turn_schema,
        resolved_intent=prior_turn.resolved_intent,
    )
    assert len(matched) == 1
    assert matched[0].name == "At-Risk Attendance"

    # Generation prompt assertion
    gen = _mock_gemini_gen()
    req = SQLGenerationRequest(
        question="Show those students.",
        schema_context=multi_turn_schema,
        conversation_context=[prior_turn],
        business_metrics=[metric],
    )
    gen.generate(req)
    prompt = gen.client.models.generate_content.call_args.kwargs["contents"]
    assert "[METRIC] At-Risk Attendance:" in prompt


@pytest.mark.parametrize(
    "incompatible_question",
    [
        "What about those courses?",
        "What about those departments?",
        "What about those products?",
        "Filter those courses by semester.",
    ],
)
def test_13_incompatible_demonstrative_follow_up_excluded(multi_turn_schema, incompatible_question):
    """13b. Incompatible demonstrative follow-ups explicitly targeting other entities are excluded."""
    metric = BusinessMetricDefinition(
        name="At-Risk Attendance",
        description="Students with attendance percentage below 75 percent.",
        calculation_formula="AttendancePercentage < 75.0",
        applicable_tables=["Students"],
    )
    prior_turn = ConversationTurnContext(
        user_question="Find all students with attendance issues.",
        resolved_intent="At-Risk Attendance for enrolled learners",
    )

    matched = match_business_metrics(
        metrics=[metric],
        question=incompatible_question,
        schema=multi_turn_schema,
        resolved_intent=prior_turn.resolved_intent,
    )
    assert matched == [], f"Expected zero matches for incompatible question: '{incompatible_question}'"

    # Generation prompt assertion
    gen = _mock_gemini_gen()
    req = SQLGenerationRequest(
        question=incompatible_question,
        schema_context=multi_turn_schema,
        conversation_context=[prior_turn],
        business_metrics=[metric],
    )
    gen.generate(req)
    prompt = gen.client.models.generate_content.call_args.kwargs["contents"]
    assert "=== BUSINESS METRIC DEFINITIONS" not in prompt


def test_13_incompatible_demonstrative_with_explicit_metric_terms_matches(multi_turn_schema):
    """13c. Questions with demonstratives that also explicitly ask about the metric match via current question."""
    metric = BusinessMetricDefinition(
        name="At-Risk Attendance",
        description="Students with attendance percentage below 75 percent.",
        calculation_formula="AttendancePercentage < 75.0",
        applicable_tables=["Students"],
    )
    prior_turn = ConversationTurnContext(
        user_question="Find all students with attendance issues.",
        resolved_intent="At-Risk Attendance for enrolled learners",
    )

    question = "What about those courses? Which students have at-risk attendance?"
    matched = match_business_metrics(
        metrics=[metric],
        question=question,
        schema=multi_turn_schema,
        resolved_intent=prior_turn.resolved_intent,
    )
    assert len(matched) == 1
    assert matched[0].name == "At-Risk Attendance"


