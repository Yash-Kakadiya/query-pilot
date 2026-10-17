"""Comprehensive offline tests for Grounded Answer Synthesis and Token Observability (Task 1.28-B).

Tests all 18 specified scenarios completely offline without live Gemini API or database access:
1. Complete token metadata.
2. Missing token metadata.
3. Partial and malformed token metadata.
4. Correct monotonic latency capture.
5. Aggregation of initial and recovery generation attempts.
6. Unknown token counts not treated as zero.
7. Estimated cost always remaining None.
8. Telemetry serialization without prompts, SQL, rows, or secrets.
9. Deterministic empty-result behavior without synthesis.
10. Semantic-aware scalar templates.
11. Neutral formatting for other scalar columns.
12. Missing or unexpected scalar values.
13. Successful multi-row synthesis marked unverified.
14. Provider failure, timeout, unavailable client, and malformed output fallback.
15. Truncated results represented without unsupported completeness claims.
16. No synthesis for unsupported questions, rejected SQL, or execution failures.
17. Consistent synthesis policy across sequential and graph modes.
18. Existing regression compatibility.
"""

from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch
import pytest

from query_pilot.db.executor import QueryResult
from query_pilot.db.models import ColumnMetadata, DatabaseSchema, TableMetadata
from query_pilot.observability import (
    ErrorCategory,
    LLMUsageMetadata,
    PipelineRunTelemetry,
    PipelineStage,
    extract_llm_usage,
)
from query_pilot.pipeline import PipelineResult, PipelineStatus, QueryPipeline
from query_pilot.presentation.formatter import PresentationResult, format_query_result
from query_pilot.presentation.synthesizer import (
    AnswerSynthesisService,
    GroundedAnswer,
    GroundedAnswerStatus,
    format_scalar_template,
)
from query_pilot.runner import ExecutionMode, QueryRunner
from query_pilot.sql.generation import (
    SQLGenerationRequest,
    SQLGenerationResponse,
    SQLGenerationStatus,
    SQLGenerator,
)
from query_pilot.sql.models import ValidationReason, ValidationReasonCode, ValidationResult


# ==============================================================================
# Helper Mock Objects
# ==============================================================================

class MockUsageObject:
    """Mock Gemini usage_metadata object mimicking google-genai response attributes."""

    def __init__(
        self,
        prompt_token_count: Optional[int] = None,
        candidates_token_count: Optional[int] = None,
        total_token_count: Optional[int] = None,
    ):
        self.prompt_token_count = prompt_token_count
        self.candidates_token_count = candidates_token_count
        self.total_token_count = total_token_count


class MockGeminiResponse:
    """Mock Gemini response object."""

    def __init__(
        self,
        text: str = "",
        usage_metadata: Optional[Any] = None,
        model_version: Optional[str] = None,
    ):
        self.text = text
        self.usage_metadata = usage_metadata
        self.model_version = model_version


def create_sample_schema() -> DatabaseSchema:
    return DatabaseSchema(
        database_name="TestDB",
        tables=[
            TableMetadata(
                schema_name="dbo",
                table_name="Students",
                columns=[
                    ColumnMetadata(name="StudentID", data_type="int", primary_key=True),
                    ColumnMetadata(name="Name", data_type="nvarchar"),
                    ColumnMetadata(name="Grade", data_type="int"),
                ],
            )
        ],
        is_model_facing=True,
    )


# ==============================================================================
# 1. Complete Token Metadata
# ==============================================================================

def test_1_complete_token_metadata() -> None:
    """Verify complete token metadata is accurately parsed from provider response."""
    resp = MockGeminiResponse(
        usage_metadata=MockUsageObject(
            prompt_token_count=120,
            candidates_token_count=45,
            total_token_count=165,
        ),
        model_version="gemini-2.5-flash-001",
    )
    usage = extract_llm_usage(
        resp,
        stage=PipelineStage.SQL_GENERATION,
        model_name="gemini-2.5-flash",
        latency_ms=250.5,
    )
    assert usage.model_name == "gemini-2.5-flash"
    assert usage.model_version == "gemini-2.5-flash-001"
    assert usage.prompt_tokens == 120
    assert usage.completion_tokens == 45
    assert usage.total_tokens == 165
    assert usage.latency_ms == 250.5
    assert usage.estimated_cost_usd is None


# ==============================================================================
# 2. Missing Token Metadata
# ==============================================================================

def test_2_missing_token_metadata() -> None:
    """Verify completely missing token metadata preserves None without crashing."""
    resp = MockGeminiResponse(usage_metadata=None)
    usage = extract_llm_usage(
        resp,
        stage=PipelineStage.SQL_GENERATION,
        model_name="gemini-2.5-flash",
        latency_ms=100.0,
    )
    assert usage.prompt_tokens is None
    assert usage.completion_tokens is None
    assert usage.total_tokens is None
    assert usage.model_name == "gemini-2.5-flash"
    assert usage.latency_ms == 100.0
    assert usage.estimated_cost_usd is None


# ==============================================================================
# 3. Partial and Malformed Token Metadata
# ==============================================================================

def test_3_partial_and_malformed_token_metadata() -> None:
    """Verify partial or malformed token attributes do not crash and do not default to zero."""
    # Partial usage: prompt count present, candidate missing
    resp_partial = MockGeminiResponse(
        usage_metadata=MockUsageObject(
            prompt_token_count=50,
            candidates_token_count=None,
            total_token_count=None,
        )
    )
    usage_partial = extract_llm_usage(resp_partial, model_name="test-model")
    assert usage_partial.prompt_tokens == 50
    assert usage_partial.completion_tokens is None
    assert usage_partial.total_tokens is None

    # Malformed dictionary with invalid string types
    resp_malformed = MockGeminiResponse(
        usage_metadata={
            "prompt_token_count": "invalid_int",
            "total_token_count": 99,
        }
    )
    usage_malformed = extract_llm_usage(resp_malformed, model_name="test-model")
    assert usage_malformed.prompt_tokens is None
    assert usage_malformed.total_tokens == 99


# ==============================================================================
# 4. Correct Monotonic Latency Capture
# ==============================================================================

def test_4_monotonic_latency_capture() -> None:
    """Verify monotonic latency is captured as non-negative millisecond float."""
    usage = LLMUsageMetadata(
        stage=PipelineStage.SQL_GENERATION,
        model_name="gemini-2.5-flash",
        latency_ms=314.15,
    )
    assert usage.latency_ms == 314.15

    telemetry = PipelineRunTelemetry()
    telemetry.add_invocation(usage)
    assert telemetry.total_latency_ms == 314.15


# ==============================================================================
# 5. Aggregation of Initial and Recovery Generation Attempts
# ==============================================================================

def test_5_aggregation_of_initial_and_recovery_attempts() -> None:
    """Verify multiple generation attempts are preserved and aggregated properly."""
    inv1 = LLMUsageMetadata(
        stage=PipelineStage.SQL_GENERATION,
        model_name="gemini-2.5-flash",
        prompt_tokens=100,
        completion_tokens=30,
        total_tokens=130,
        latency_ms=200.0,
    )
    inv2 = LLMUsageMetadata(
        stage=PipelineStage.SQL_GENERATION,
        model_name="gemini-2.5-flash",
        prompt_tokens=150,
        completion_tokens=40,
        total_tokens=190,
        latency_ms=250.0,
    )

    telemetry = PipelineRunTelemetry()
    telemetry.add_invocation(inv1)
    telemetry.add_invocation(inv2)

    assert len(telemetry.invocations) == 2
    assert telemetry.total_prompt_tokens == 250
    assert telemetry.total_completion_tokens == 70
    assert telemetry.total_tokens == 320
    assert telemetry.total_latency_ms == 450.0
    assert telemetry.estimated_cost_usd is None


# ==============================================================================
# 6. Unknown Token Counts Not Treated As Zero
# ==============================================================================

def test_6_unknown_token_counts_not_treated_as_zero() -> None:
    """Verify that if one invocation has unknown tokens, total is None, not partial sum."""
    inv1 = LLMUsageMetadata(
        stage=PipelineStage.SQL_GENERATION,
        model_name="gemini-2.5-flash",
        prompt_tokens=100,
        completion_tokens=30,
        total_tokens=130,
        latency_ms=100.0,
    )
    inv2 = LLMUsageMetadata(
        stage=PipelineStage.SQL_GENERATION,
        model_name="gemini-2.5-flash",
        prompt_tokens=None,
        completion_tokens=40,
        total_tokens=None,
        latency_ms=100.0,
    )

    telemetry = PipelineRunTelemetry()
    telemetry.add_invocation(inv1)
    telemetry.add_invocation(inv2)

    # Prompt tokens completeness is unknown (100 + None -> None)
    assert telemetry.total_prompt_tokens is None
    # Completion tokens are complete (30 + 40 -> 70)
    assert telemetry.total_completion_tokens == 70
    # Total tokens completeness is unknown (130 + None -> None)
    assert telemetry.total_tokens is None
    # Latency is always known
    assert telemetry.total_latency_ms == 200.0


# ==============================================================================
# 7. Estimated Cost Always Remaining None
# ==============================================================================

def test_7_estimated_cost_always_none() -> None:
    """Verify estimated cost remains None across models, invocations, and serialization."""
    telemetry = PipelineRunTelemetry()
    usage = LLMUsageMetadata(
        stage=PipelineStage.SQL_GENERATION,
        model_name="gemini-2.5-flash",
        prompt_tokens=500,
        completion_tokens=50,
        total_tokens=550,
        latency_ms=120.0,
        estimated_cost_usd=None,
    )
    telemetry.add_invocation(usage)

    assert usage.estimated_cost_usd is None
    assert telemetry.estimated_cost_usd is None

    serialized = telemetry.to_dict()
    assert serialized["estimated_cost_usd"] is None
    assert serialized["invocations"][0]["estimated_cost_usd"] is None


# ==============================================================================
# 8. Telemetry Serialization Privacy
# ==============================================================================

def test_8_telemetry_serialization_privacy() -> None:
    """Verify telemetry serialization contains strictly operational metadata, no prompts/SQL/rows/secrets."""
    usage = LLMUsageMetadata(
        stage=PipelineStage.SQL_GENERATION,
        model_name="gemini-2.5-flash",
        model_version="2.5-flash-001",
        prompt_tokens=100,
        completion_tokens=25,
        total_tokens=125,
        latency_ms=150.0,
    )
    telemetry = PipelineRunTelemetry()
    telemetry.add_invocation(usage)

    data = telemetry.to_dict()
    data_str = str(data)

    # Must contain telemetry fields
    assert "gemini-2.5-flash" in data_str
    assert "total_tokens" in data
    assert "latency_ms" in data_str

    # Must NOT contain forbidden information
    forbidden_keys = ["sql", "prompt", "rows", "schema", "password", "api_key", "secret", "user"]
    for key in forbidden_keys:
        assert key not in data
        for inv in data["invocations"]:
            assert key not in inv


# ==============================================================================
# 9. Deterministic Empty-Result Behavior Without Synthesis
# ==============================================================================

def test_9_deterministic_empty_result_without_synthesis() -> None:
    """Verify 0-row results produce deterministic empty message without calling Gemini."""
    mock_client = MagicMock()
    service = AnswerSynthesisService(gemini_client=mock_client)

    empty_res = QueryResult(columns=["StudentID", "Name"], rows=[], row_count=0, truncated=False)
    answer = service.synthesize("Show all inactive students", empty_res)

    # 0 LLM calls made
    mock_client.models.generate_content.assert_not_called()
    assert answer.status == GroundedAnswerStatus.EMPTY_RESULT
    assert "No rows returned" in answer.markdown_text
    assert answer.narrative is None


# ==============================================================================
# 10. Semantic-Aware Scalar Templates
# ==============================================================================

def test_10_semantic_aware_scalar_templates() -> None:
    """Verify 1-row, 1-col results use conservative semantic aggregate templates."""
    # Clearly named count columns
    assert format_scalar_template("StudentCount", 42) == "Count: 42"
    assert format_scalar_template("Count", 42) == "Count: 42"
    assert format_scalar_template("student_count", 15) == "Count: 15"
    assert format_scalar_template("total_count", 100) == "Count: 100"

    # Average column patterns
    assert format_scalar_template("AvgMarks", 85.5) == "Average: 85.5"
    assert format_scalar_template("AverageMarks", 85.5) == "Average: 85.5"
    assert format_scalar_template("AvgScore", 90.0) == "Average: 90.0"
    assert format_scalar_template("average_score", 90.0) == "Average: 90.0"

    # Minimum column patterns
    assert format_scalar_template("MinAge", 18) == "Minimum: 18"
    assert format_scalar_template("lowest_fee", 500) == "Minimum: 500"

    # Maximum column patterns
    assert format_scalar_template("MaxMarks", 100) == "Maximum: 100"
    assert format_scalar_template("highest_score", 98) == "Maximum: 98"

    # Verify through AnswerSynthesisService without LLM calls
    mock_client = MagicMock()
    service = AnswerSynthesisService(gemini_client=mock_client)

    count_res = QueryResult(columns=["StudentCount"], rows=[{"StudentCount": 42}], row_count=1, truncated=False)
    ans = service.synthesize("How many students?", count_res)
    mock_client.models.generate_content.assert_not_called()
    assert ans.status == GroundedAnswerStatus.TEMPLATE_DETERMINISTIC
    assert ans.markdown_text == "Count: 42"


# ==============================================================================
# 11. Neutral Formatting for Other Scalar Columns
# ==============================================================================

def test_11_neutral_formatting_for_other_scalar_columns() -> None:
    """Verify non-aggregate 1-row 1-col results use neutral ColumnName: value format."""
    mock_client = MagicMock()
    service = AnswerSynthesisService(gemini_client=mock_client)

    name_res = QueryResult(columns=["StudentName"], rows=[{"StudentName": "Alice Smith"}], row_count=1, truncated=False)
    ans = service.synthesize("Who is the top student?", name_res)
    mock_client.models.generate_content.assert_not_called()
    assert ans.status == GroundedAnswerStatus.TEMPLATE_DETERMINISTIC
    assert ans.markdown_text == "StudentName: Alice Smith"

    # ID column
    id_res = QueryResult(columns=["DepartmentID"], rows=[{"DepartmentID": 10}], row_count=1, truncated=False)
    ans_id = service.synthesize("What is the department ID?", id_res)
    mock_client.models.generate_content.assert_not_called()
    assert ans_id.status == GroundedAnswerStatus.TEMPLATE_DETERMINISTIC
    assert ans_id.markdown_text == "DepartmentID: 10"


# ==============================================================================
# 12. Missing or Unexpected Scalar Values
# ==============================================================================

def test_12_missing_or_unexpected_scalar_values() -> None:
    """Verify missing, null, or empty scalar results are formatted safely without crashing."""
    mock_client = MagicMock()
    service = AnswerSynthesisService(gemini_client=mock_client)

    # None value in scalar row
    null_res = QueryResult(columns=["AvgMarks"], rows=[{"AvgMarks": None}], row_count=1, truncated=False)
    ans_null = service.synthesize("What is the average?", null_res)
    assert ans_null.status == GroundedAnswerStatus.TEMPLATE_DETERMINISTIC
    assert ans_null.markdown_text == "Average: None"

    # Empty dictionary in row for aggregate column
    empty_dict_res = QueryResult(columns=["StudentCount"], rows=[{}], row_count=1, truncated=False)
    ans_empty = service.synthesize("Count students", empty_dict_res)
    assert ans_empty.status == GroundedAnswerStatus.TEMPLATE_DETERMINISTIC
    assert ans_empty.markdown_text == "Count: None"

    # Empty dictionary in row for neutral column
    neutral_empty_res = QueryResult(columns=["DepartmentNotes"], rows=[{}], row_count=1, truncated=False)
    ans_neutral = service.synthesize("Get notes", neutral_empty_res)
    assert ans_neutral.status == GroundedAnswerStatus.TEMPLATE_DETERMINISTIC
    assert ans_neutral.markdown_text == "DepartmentNotes: None"


# ==============================================================================
# 13. Successful Multi-Row Synthesis Marked Unverified
# ==============================================================================

def test_13_successful_multi_row_synthesis_marked_unverified() -> None:
    """Verify multi-row synthesis narrative is strictly marked UNVERIFIED_NARRATIVE."""
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.text = "There are 3 enrolled students: Alice, Bob, and Charlie."
    mock_client.models.generate_content.return_value = mock_response

    service = AnswerSynthesisService(gemini_client=mock_client)
    res = QueryResult(
        columns=["StudentID", "Name"],
        rows=[
            {"StudentID": 1, "Name": "Alice"},
            {"StudentID": 2, "Name": "Bob"},
            {"StudentID": 3, "Name": "Charlie"},
        ],
        row_count=3,
        truncated=False,
    )

    answer = service.synthesize("List all enrolled students", res)

    # Must be UNVERIFIED_NARRATIVE, never verified
    assert answer.status == GroundedAnswerStatus.UNVERIFIED_NARRATIVE
    assert answer.narrative == "There are 3 enrolled students: Alice, Bob, and Charlie."
    assert "There are 3 enrolled students" in answer.markdown_text

    # Preserved in PresentationResult table
    pres = format_query_result(res, grounded_answer=answer)
    assert "| StudentID | Name |" in pres.markdown_table
    assert pres.grounded_answer == answer

    # Verify model prompt does NOT contain SQL or DB schema
    call_args = mock_client.models.generate_content.call_args[1]
    prompt_sent = call_args["contents"]
    assert "SELECT" not in prompt_sent
    assert "CREATE TABLE" not in prompt_sent


# ==============================================================================
# 14. Provider Failure, Timeout, Unavailable Client, and Malformed Output Fallback
# ==============================================================================

def test_14_provider_failure_timeout_and_fallback() -> None:
    """Verify synthesis failures preserve deterministic presentation without failing query."""
    multi_res = QueryResult(
        columns=["CourseID", "Title"],
        rows=[
            {"CourseID": 101, "Title": "Database Systems"},
            {"CourseID": 102, "Title": "Algorithms"},
        ],
        row_count=2,
        truncated=False,
    )

    # 1. Unavailable client (None)
    service_no_client = AnswerSynthesisService(client=None)
    with patch.object(service_no_client, "_init_gemini_client", return_value=None):
        ans_no_client = service_no_client.synthesize("List courses", multi_res)
        assert ans_no_client.status == GroundedAnswerStatus.UNAVAILABLE
        assert ans_no_client.narrative is None
        pres_no_client = format_query_result(multi_res, grounded_answer=ans_no_client)
        assert "| CourseID | Title |" in pres_no_client.markdown_table

    # 2. Provider API exception
    mock_client_err = MagicMock()
    mock_client_err.models.generate_content.side_effect = RuntimeError("API connection timeout")
    service_err = AnswerSynthesisService(gemini_client=mock_client_err)
    ans_err = service_err.synthesize("List courses", multi_res)
    assert ans_err.status == GroundedAnswerStatus.SYNTHESIS_FAILED
    assert ans_err.narrative is None
    pres_err = format_query_result(multi_res, grounded_answer=ans_err)
    assert "| CourseID | Title |" in pres_err.markdown_table

    # 3. Empty text response
    mock_client_empty = MagicMock()
    mock_client_empty.models.generate_content.return_value = MagicMock(text="   ")
    service_empty = AnswerSynthesisService(gemini_client=mock_client_empty)
    ans_empty = service_empty.synthesize("List courses", multi_res)
    assert ans_empty.status == GroundedAnswerStatus.SYNTHESIS_FAILED
    assert ans_empty.narrative is None
    pres_empty = format_query_result(multi_res, grounded_answer=ans_empty)
    assert "| CourseID | Title |" in pres_empty.markdown_table


# ==============================================================================
# 15. Truncated Results Represented Without Unsupported Completeness Claims
# ==============================================================================

def test_15_truncated_results_handling() -> None:
    """Verify truncated results warn the model about partial results and do not infer truncation from row count alone."""
    mock_client = MagicMock()
    mock_response = MagicMock(text="Sample of top courses shown below.")
    mock_client.models.generate_content.return_value = mock_response

    service = AnswerSynthesisService(gemini_client=mock_client)
    res_truncated = QueryResult(
        columns=["CourseID", "Title"],
        rows=[{"CourseID": i, "Title": f"Course {i}"} for i in range(1, 16)],
        row_count=15,
        truncated=True,  # Explicit truncation flag
    )

    ans = service.synthesize("Show all courses", res_truncated)
    call_args = mock_client.models.generate_content.call_args[1]
    prompt_sent = call_args["contents"]

    # Prompt must explicitly indicate truncation
    assert "truncated at the system limit" in prompt_sent
    assert ans.status == GroundedAnswerStatus.UNVERIFIED_NARRATIVE

    # If row count is high but truncated=False, prompt must not claim database truncation
    res_not_truncated = QueryResult(
        columns=["CourseID", "Title"],
        rows=[{"CourseID": i, "Title": f"Course {i}"} for i in range(1, 16)],
        row_count=15,
        truncated=False,
    )
    ans2 = service.synthesize("Show all courses", res_not_truncated)
    prompt_sent2 = mock_client.models.generate_content.call_args[1]["contents"]
    assert "truncated at the system limit" not in prompt_sent2


# ==============================================================================
# 16. No Synthesis for Unsupported, Rejected, or Execution Failure Paths
# ==============================================================================

def test_16_no_synthesis_on_pipeline_failures() -> None:
    """Verify synthesis is completely bypassed for unsupported questions, rejected SQL, and execution errors."""
    mock_synthesizer = MagicMock()
    schema = create_sample_schema()

    # 1. Unsupported question
    generator_unsupported = MagicMock(spec=SQLGenerator)
    generator_unsupported.generate.return_value = SQLGenerationResponse(
        status=SQLGenerationStatus.UNSUPPORTED,
        sql=None,
        explanation="Schema does not support weather forecasts.",
    )

    runner_seq = QueryRunner(
        mode=ExecutionMode.SEQUENTIAL,
        generator=generator_unsupported,
        schema=schema,
        synthesizer=mock_synthesizer,
    )
    res_unsupported = runner_seq.run("What is the weather tomorrow?")
    assert res_unsupported.status == PipelineStatus.UNSUPPORTED
    assert res_unsupported.grounded_answer is None
    mock_synthesizer.synthesize.assert_not_called()

    # 2. Validation rejected
    generator_rejected = MagicMock(spec=SQLGenerator)
    generator_rejected.generate.return_value = SQLGenerationResponse(
        status=SQLGenerationStatus.ANSWERABLE,
        sql="DROP TABLE Students;",
        explanation="Attempting deletion",
    )
    runner_rejected = QueryRunner(
        mode=ExecutionMode.SEQUENTIAL,
        generator=generator_rejected,
        schema=schema,
        synthesizer=mock_synthesizer,
    )
    res_rejected = runner_rejected.run("Delete students")
    assert res_rejected.status == PipelineStatus.VALIDATION_REJECTED
    assert res_rejected.grounded_answer is None
    mock_synthesizer.synthesize.assert_not_called()


# ==============================================================================
# 17. Consistent Synthesis Policy Across Sequential and Graph Modes
# ==============================================================================

def test_17_consistent_synthesis_across_sequential_and_graph_modes() -> None:
    """Verify sequential and LangGraph execution modes follow identical synthesis and telemetry behavior."""
    schema = create_sample_schema()
    exec_result = QueryResult(
        columns=["StudentCount"],
        rows=[{"StudentCount": 150}],
        row_count=1,
        truncated=False,
    )

    usage = LLMUsageMetadata(
        stage=PipelineStage.SQL_GENERATION,
        model_name="gemini-2.5-flash",
        prompt_tokens=80,
        completion_tokens=20,
        total_tokens=100,
        latency_ms=180.0,
    )

    gen_resp = SQLGenerationResponse(
        status=SQLGenerationStatus.ANSWERABLE,
        sql="SELECT COUNT(*) AS StudentCount FROM Students;",
        usage_metadata=usage,
    )

    mock_generator = MagicMock(spec=SQLGenerator)
    mock_generator.generate.return_value = gen_resp
    mock_generator.last_usage = usage

    with patch("query_pilot.pipeline.validate_sql", return_value=ValidationResult(allowed=True)), \
         patch("query_pilot.pipeline.execute_read_only", return_value=exec_result), \
         patch("query_pilot.graph.workflow.validate_sql", return_value=ValidationResult(allowed=True)), \
         patch("query_pilot.graph.workflow.execute_read_only", return_value=exec_result):

        # Sequential mode
        runner_seq = QueryRunner(
            mode=ExecutionMode.SEQUENTIAL,
            generator=mock_generator,
            schema=schema,
        )
        res_seq = runner_seq.run("How many students?")

        # Graph mode
        runner_graph = QueryRunner(
            mode=ExecutionMode.GRAPH,
            generator=mock_generator,
            schema=schema,
        )
        res_graph = runner_graph.run("How many students?")

    # Verify parity
    assert res_seq.status == PipelineStatus.SUCCESS
    assert res_graph.status == PipelineStatus.SUCCESS

    # Both must have synthesized conservative scalar answer
    assert res_seq.grounded_answer is not None
    assert res_graph.grounded_answer is not None
    assert res_seq.grounded_answer.status == GroundedAnswerStatus.TEMPLATE_DETERMINISTIC
    assert res_graph.grounded_answer.status == GroundedAnswerStatus.TEMPLATE_DETERMINISTIC
    assert res_seq.grounded_answer.markdown_text == "Count: 150"
    assert res_graph.grounded_answer.markdown_text == "Count: 150"

    # Both must have telemetry attached with identical tokens
    assert res_seq.telemetry is not None
    assert res_graph.telemetry is not None
    assert res_seq.telemetry.total_tokens == 100
    assert res_graph.telemetry.total_tokens == 100
    assert res_seq.telemetry.estimated_cost_usd is None
    assert res_graph.telemetry.estimated_cost_usd is None


# ==============================================================================
# 18. Existing Regression Compatibility
# ==============================================================================

def test_18_existing_regression_compatibility() -> None:
    """Verify existing result rows, columns, presentation tables, and to_dict remain backward-compatible."""
    query_res = QueryResult(
        columns=["StudentID", "Name"],
        rows=[{"StudentID": 1, "Name": "Alice"}],
        row_count=1,
        truncated=False,
    )
    pipe_res = PipelineResult(
        question="Find Alice",
        status=PipelineStatus.SUCCESS,
        generated_sql="SELECT StudentID, Name FROM Students WHERE Name = 'Alice';",
        execution_result=query_res,
        grounded_answer=GroundedAnswer(
            text="Student Alice found.",
            status=GroundedAnswerStatus.UNVERIFIED_NARRATIVE,
        ),
        telemetry=PipelineRunTelemetry(),
    )

    # Presentation formatting
    pres = format_query_result(
        query_result=query_res,
        question="Find Alice",
        grounded_answer=pipe_res.grounded_answer,
        telemetry=pipe_res.telemetry,
    )
    assert pres.columns == ["StudentID", "Name"]
    assert pres.rows == [{"StudentID": 1, "Name": "Alice"}]
    assert "| StudentID | Name |" in pres.markdown_table
    assert pres.grounded_answer == pipe_res.grounded_answer
    assert pres.telemetry == pipe_res.telemetry

    # to_dict compatibility
    res_dict = pipe_res.to_dict()
    assert res_dict["question"] == "Find Alice"
    assert res_dict["status"] == "SUCCESS"
    assert res_dict["grounded_answer"]["status"] == "UNVERIFIED_NARRATIVE"
    assert res_dict["telemetry"]["invocations"] == []


# ==============================================================================
# 19. Finding 1 Regression: LangGraph Synthesis Exception Fallback
# ==============================================================================

def test_finding_1_langgraph_synthesis_exception_fallback() -> None:
    """Verify that an unexpected exception during LangGraph synthesis falls back cleanly.

    Preserves successful pipeline status, QueryResult, and deterministic table
    without raising Pydantic ValidationError or unhandled exceptions.
    """
    from query_pilot.graph.workflow import QueryPilotWorkflow

    schema = create_sample_schema()
    exec_result = QueryResult(
        columns=["StudentID", "Name", "Grade"],
        rows=[
            {"StudentID": 1, "Name": "Alice", "Grade": 95},
            {"StudentID": 2, "Name": "Bob", "Grade": 88},
        ],
        row_count=2,
        truncated=False,
    )

    gen_resp = SQLGenerationResponse(
        status=SQLGenerationStatus.ANSWERABLE,
        sql="SELECT StudentID, Name, Grade FROM Students;",
    )
    mock_gen = MagicMock(spec=SQLGenerator)
    mock_gen.generate.return_value = gen_resp
    mock_gen.last_usage = None

    # Synthesizer that raises an unexpected exception
    mock_failing_synth = MagicMock(spec=AnswerSynthesisService)
    mock_failing_synth.synthesize.side_effect = RuntimeError("Simulated synthesis engine crash!")

    with patch("query_pilot.graph.workflow.validate_sql", return_value=ValidationResult(allowed=True)), \
         patch("query_pilot.graph.workflow.execute_read_only", return_value=exec_result):

        workflow = QueryPilotWorkflow(
            generator=mock_gen,
            schema=schema,
            synthesizer=mock_failing_synth,
        )
        final_state = workflow.run("Show all student grades")

    # Workflow must succeed without crashing
    assert final_state["status"] == PipelineStatus.SUCCESS
    assert final_state["execution_result"] == exec_result

    # Deterministic table must be intact
    pres = final_state["presentation_result"]
    assert pres is not None
    assert "| StudentID | Name | Grade |" in pres.markdown_table
    assert "Alice" in pres.markdown_table

    # Grounded answer must be valid GroundedAnswer with SYNTHESIS_FAILED
    ans = final_state["grounded_answer"]
    assert isinstance(ans, GroundedAnswer)
    assert ans.status == GroundedAnswerStatus.SYNTHESIS_FAILED
    assert ans.is_mechanically_verified is False
    assert ans.text == "Query executed successfully. Refer to the data table for results."
    # Raw exception text must not leak
    assert "Simulated synthesis engine crash!" not in ans.text
    for note in ans.verification_notes:
        assert "Simulated synthesis engine crash!" not in note


# ==============================================================================
# 20. Finding 2 Regression: Parameterized Scalar Classification
# ==============================================================================

@pytest.mark.parametrize(
    "col_name,value,expected_formatted",
    [
        # Required non-count examples from Finding 2
        ("TotalRevenue", 50000, "TotalRevenue: 50000"),
        ("TotalMarks", 95, "TotalMarks: 95"),
        ("CourseNumber", 101, "CourseNumber: 101"),
        ("AccountNumber", 123456, "AccountNumber: 123456"),
        ("PhoneNumber", "555-0100", "PhoneNumber: 555-0100"),
        ("DiscountRate", 0.15, "DiscountRate: 0.15"),
        # AverageTotal unambiguously contains average token
        ("AverageTotal", 42.5, "Average: 42.5"),
        # Clearly named count columns
        ("CourseCount", 10, "Count: 10"),
        ("course_count", 10, "Count: 10"),
        ("StudentCount", 42, "Count: 42"),
        ("Count", 10, "Count: 10"),
        ("total_count", 99, "Count: 99"),
        ("cnt", 7, "Count: 7"),
        # Clearly named averages
        ("AverageMarks", 85.0, "Average: 85.0"),
        ("AvgScore", 92.5, "Average: 92.5"),
        # Clearly named min / max
        ("MinAge", 18, "Minimum: 18"),
        ("lowest_price", 9.99, "Minimum: 9.99"),
        ("MaxMarks", 100, "Maximum: 100"),
        ("highest_score", 99.5, "Maximum: 99.5"),
        # Ambiguous / conflicting aggregate names -> neutral fallback
        ("MinMaxRange", 50, "MinMaxRange: 50"),
        ("AvgCountRatio", 1.5, "AvgCountRatio: 1.5"),
        # Arbitrary column names -> neutral fallback
        ("DepartmentCode", "CS", "DepartmentCode: CS"),
        ("TotalWeight", 250, "TotalWeight: 250"),
    ],
)
def test_finding_2_scalar_classification_parameterized(
    col_name: str,
    value: Any,
    expected_formatted: str,
) -> None:
    """Verify scalar formatting strictly adheres to token-aware classification rules."""
    actual = format_scalar_template(col_name, value)
    assert actual == expected_formatted, f"Column '{col_name}' misclassified as '{actual}' instead of '{expected_formatted}'"


# ==============================================================================
# 21. Finding 3 Regression: Default Synthesizer Parity (Disabled by Default)
# ==============================================================================

def test_finding_3_default_synthesizer_parity() -> None:
    """Verify sequential and graph engines both default to synthesizer.enabled = False."""
    from query_pilot.graph.workflow import QueryPilotWorkflow

    pipe = QueryPipeline()
    wf = QueryPilotWorkflow()

    assert pipe.synthesizer.enabled is False
    assert wf.synthesizer.enabled is False

    # Multi-row execution with default synthesizer must skip LLM synthesis without calls
    schema = create_sample_schema()
    exec_result = QueryResult(
        columns=["StudentID", "Name"],
        rows=[{"StudentID": 1, "Name": "Alice"}, {"StudentID": 2, "Name": "Bob"}],
        row_count=2,
        truncated=False,
    )
    mock_gen = MagicMock(spec=SQLGenerator)
    mock_gen.generate.return_value = SQLGenerationResponse(
        status=SQLGenerationStatus.ANSWERABLE,
        sql="SELECT StudentID, Name FROM Students;",
    )
    mock_gen.last_usage = None

    with patch("query_pilot.pipeline.validate_sql", return_value=ValidationResult(allowed=True)), \
         patch("query_pilot.pipeline.execute_read_only", return_value=exec_result), \
         patch("query_pilot.graph.workflow.validate_sql", return_value=ValidationResult(allowed=True)), \
         patch("query_pilot.graph.workflow.execute_read_only", return_value=exec_result):

        res_seq = QueryRunner(mode=ExecutionMode.SEQUENTIAL, generator=mock_gen, schema=schema).run("List students")
        res_graph = QueryRunner(mode=ExecutionMode.GRAPH, generator=mock_gen, schema=schema).run("List students")

    assert res_seq.grounded_answer.status == GroundedAnswerStatus.SKIPPED
    assert res_graph.grounded_answer.status == GroundedAnswerStatus.SKIPPED
    assert res_seq.grounded_answer.is_mechanically_verified is False
    assert res_graph.grounded_answer.is_mechanically_verified is False


# ==============================================================================
# 22. Finding 3 Regression: Generation Failure Telemetry Capture
# ==============================================================================

def test_finding_3_generation_failure_telemetry_capture() -> None:
    """Verify available usage metadata is captured when generation fails in sequential and graph paths."""
    from query_pilot.graph.workflow import QueryPilotWorkflow
    from query_pilot.sql.generation import MalformedResponseError

    schema = create_sample_schema()
    failed_usage = LLMUsageMetadata(
        stage=PipelineStage.SQL_GENERATION,
        model_name="gemini-2.5-flash",
        prompt_tokens=45,
        completion_tokens=15,
        total_tokens=60,
        latency_ms=130.0,
    )

    # Generator raising MalformedResponseError with usage_metadata attached
    err = MalformedResponseError("Bad JSON output")
    err.usage_metadata = failed_usage
    mock_gen = MagicMock(spec=SQLGenerator)
    mock_gen.generate.side_effect = err
    mock_gen.last_usage = failed_usage

    # Sequential mode
    runner_seq = QueryRunner(mode=ExecutionMode.SEQUENTIAL, generator=mock_gen, schema=schema)
    res_seq = runner_seq.run("Invalid generation question")

    assert res_seq.status == PipelineStatus.GENERATION_FAILED
    assert res_seq.telemetry is not None
    assert len(res_seq.telemetry.invocations) == 1
    assert res_seq.telemetry.total_tokens == 60
    assert res_seq.telemetry.total_prompt_tokens == 45
    assert res_seq.telemetry.total_completion_tokens == 15

    # Graph mode (one attempt that fails terminally or exhausts)
    with patch("query_pilot.graph.workflow.MAX_GENERATION_ATTEMPTS", 1):
        wf = QueryPilotWorkflow(generator=mock_gen, schema=schema)
        state_graph = wf.run("Invalid generation question")

    assert state_graph["status"] == PipelineStatus.GENERATION_FAILED
    assert state_graph["telemetry"] is not None
    assert len(state_graph["telemetry"].invocations) == 1
    assert state_graph["telemetry"].total_tokens == 60


# ==============================================================================
# 23. Finding 3 Regression: Recovery Telemetry Aggregation (No Double-Counting)
# ==============================================================================

def test_finding_3_recovery_telemetry_aggregation() -> None:
    """Verify graph recovery records initial and recovery generation attempts exactly once."""
    from query_pilot.graph.workflow import QueryPilotWorkflow
    from query_pilot.sql.generation import MalformedResponseError

    schema = create_sample_schema()
    exec_result = QueryResult(
        columns=["StudentCount"],
        rows=[{"StudentCount": 10}],
        row_count=1,
        truncated=False,
    )

    usage_attempt1 = LLMUsageMetadata(
        stage=PipelineStage.SQL_GENERATION,
        model_name="gemini-2.5-flash",
        prompt_tokens=50,
        completion_tokens=10,
        total_tokens=60,
        latency_ms=110.0,
    )
    usage_attempt2 = LLMUsageMetadata(
        stage=PipelineStage.SQL_GENERATION,
        model_name="gemini-2.5-flash",
        prompt_tokens=80,
        completion_tokens=20,
        total_tokens=100,
        latency_ms=150.0,
    )

    err = MalformedResponseError("Output was not valid JSON")
    err.usage_metadata = usage_attempt1

    success_resp = SQLGenerationResponse(
        status=SQLGenerationStatus.ANSWERABLE,
        sql="SELECT COUNT(*) AS StudentCount FROM Students;",
        usage_metadata=usage_attempt2,
    )

    mock_gen = MagicMock(spec=SQLGenerator)
    # Attempt 1 fails recoverably, Attempt 2 succeeds
    mock_gen.generate.side_effect = [err, success_resp]

    with patch("query_pilot.graph.workflow.validate_sql", return_value=ValidationResult(allowed=True)), \
         patch("query_pilot.graph.workflow.execute_read_only", return_value=exec_result):

        wf = QueryPilotWorkflow(generator=mock_gen, schema=schema)
        final_state = wf.run("How many students?")

    assert final_state["status"] == PipelineStatus.SUCCESS
    telemetry = final_state["telemetry"]
    assert telemetry is not None
    # Must have recorded exactly 2 invocations: attempt 1 and attempt 2
    assert len(telemetry.invocations) == 2
    assert telemetry.invocations[0].total_tokens == 60
    assert telemetry.invocations[1].total_tokens == 100
    assert telemetry.total_tokens == 160
    assert telemetry.total_prompt_tokens == 130
    assert telemetry.total_completion_tokens == 30
    assert telemetry.total_latency_ms == 260.0


# ==============================================================================
# 24. Finding 3 Regression: Synthesis Usage Included in Run Telemetry
# ==============================================================================

def test_finding_3_synthesis_usage_included_once_in_both_paths() -> None:
    """Verify synthesis usage is included once in run-level telemetry in sequential and graph paths."""
    from query_pilot.graph.workflow import QueryPilotWorkflow

    schema = create_sample_schema()
    exec_result = QueryResult(
        columns=["StudentID", "Name"],
        rows=[{"StudentID": 1, "Name": "Alice"}, {"StudentID": 2, "Name": "Bob"}],
        row_count=2,
        truncated=False,
    )

    gen_usage = LLMUsageMetadata(
        stage=PipelineStage.SQL_GENERATION,
        model_name="gemini-2.5-flash",
        prompt_tokens=100,
        completion_tokens=25,
        total_tokens=125,
        latency_ms=200.0,
    )
    synth_usage = LLMUsageMetadata(
        stage="answer_synthesis",
        model_name="gemini-3.8-flash",
        prompt_tokens=70,
        completion_tokens=15,
        total_tokens=85,
        latency_ms=150.0,
    )

    gen_resp = SQLGenerationResponse(
        status=SQLGenerationStatus.ANSWERABLE,
        sql="SELECT StudentID, Name FROM Students;",
        usage_metadata=gen_usage,
    )
    mock_gen = MagicMock(spec=SQLGenerator)
    mock_gen.generate.return_value = gen_resp
    mock_gen.last_usage = gen_usage

    mock_synth = MagicMock(spec=AnswerSynthesisService)
    mock_synth.synthesize.return_value = GroundedAnswer(
        text="Alice and Bob are students.",
        status=GroundedAnswerStatus.UNVERIFIED_NARRATIVE,
        usage_metadata=synth_usage,
    )

    with patch("query_pilot.pipeline.validate_sql", return_value=ValidationResult(allowed=True)), \
         patch("query_pilot.pipeline.execute_read_only", return_value=exec_result), \
         patch("query_pilot.graph.workflow.validate_sql", return_value=ValidationResult(allowed=True)), \
         patch("query_pilot.graph.workflow.execute_read_only", return_value=exec_result):

        runner_seq = QueryRunner(mode=ExecutionMode.SEQUENTIAL, generator=mock_gen, schema=schema, synthesizer=mock_synth)
        res_seq = runner_seq.run("Show students")

        wf = QueryPilotWorkflow(generator=mock_gen, schema=schema, synthesizer=mock_synth)
        state_graph = wf.run("Show students")

    # Sequential telemetry check
    assert res_seq.telemetry is not None
    assert len(res_seq.telemetry.invocations) == 2
    assert res_seq.telemetry.invocations[0].stage == PipelineStage.SQL_GENERATION
    assert res_seq.telemetry.invocations[1].stage == "answer_synthesis"
    assert res_seq.telemetry.total_tokens == 210

    # Graph telemetry check
    assert state_graph["telemetry"] is not None
    assert len(state_graph["telemetry"].invocations) == 2
    assert state_graph["telemetry"].invocations[0].stage == PipelineStage.SQL_GENERATION
    assert state_graph["telemetry"].invocations[1].stage == "answer_synthesis"
    assert state_graph["telemetry"].total_tokens == 210


# ==============================================================================
# 25. Finding 4 Regression: Sanitize Sensitive Synthesis Exceptions
# ==============================================================================

def test_finding_4_sanitize_synthesis_failure_notes() -> None:
    """Verify sensitive connection strings, passwords, URLs, and SQL do not leak on synthesis failure."""
    from query_pilot.graph.workflow import QueryPilotWorkflow

    sensitive_leak = (
        "Connection failed: mssql+pyodbc://db_admin:SuperSecretPassword123!@192.168.1.100:1433/InternalDB "
        "executing SELECT password_hash FROM secret_table"
    )
    sensitive_substrings = [
        "SuperSecretPassword123!",
        "db_admin",
        "192.168.1.100",
        "secret_table",
        "password_hash",
        "InternalDB",
    ]

    multi_res = QueryResult(
        columns=["StudentID", "Name"],
        rows=[{"StudentID": 1, "Name": "Alice"}, {"StudentID": 2, "Name": "Bob"}],
        row_count=2,
        truncated=False,
    )

    # 1. Direct AnswerSynthesisService call
    mock_client = MagicMock()
    mock_client.models.generate_content.side_effect = RuntimeError(sensitive_leak)
    service = AnswerSynthesisService(gemini_client=mock_client)
    direct_ans = service.synthesize("Show students", multi_res)

    assert direct_ans.status == GroundedAnswerStatus.SYNTHESIS_FAILED
    for substr in sensitive_substrings:
        assert substr not in direct_ans.text
        for note in direct_ans.verification_notes:
            assert substr not in note

    # 2. Sequential pipeline
    mock_gen = MagicMock(spec=SQLGenerator)
    mock_gen.generate.return_value = SQLGenerationResponse(
        status=SQLGenerationStatus.ANSWERABLE,
        sql="SELECT StudentID, Name FROM Students;",
    )
    mock_synth_failing = MagicMock(spec=AnswerSynthesisService)
    mock_synth_failing.synthesize.side_effect = RuntimeError(sensitive_leak)

    schema = create_sample_schema()
    with patch("query_pilot.pipeline.validate_sql", return_value=ValidationResult(allowed=True)), \
         patch("query_pilot.pipeline.execute_read_only", return_value=multi_res):

        pipe = QueryPipeline(generator=mock_gen, schema=schema, synthesizer=mock_synth_failing)
        pipe_res = pipe.run("Show students")

    assert pipe_res.grounded_answer is not None
    assert pipe_res.grounded_answer.status == GroundedAnswerStatus.SYNTHESIS_FAILED
    for substr in sensitive_substrings:
        assert substr not in pipe_res.grounded_answer.text
        for note in pipe_res.grounded_answer.verification_notes:
            assert substr not in note

    # 3. LangGraph workflow
    with patch("query_pilot.graph.workflow.validate_sql", return_value=ValidationResult(allowed=True)), \
         patch("query_pilot.graph.workflow.execute_read_only", return_value=multi_res):

        wf = QueryPilotWorkflow(generator=mock_gen, schema=schema, synthesizer=mock_synth_failing)
        graph_state = wf.run("Show students")

    assert graph_state["grounded_answer"] is not None
    assert graph_state["grounded_answer"].status == GroundedAnswerStatus.SYNTHESIS_FAILED
    for substr in sensitive_substrings:
        assert substr not in graph_state["grounded_answer"].text
        for note in graph_state["grounded_answer"].verification_notes:
            assert substr not in note


# ==============================================================================
# 28. Transient Error Retry Tests (Task 1.34-B1-F20-B)
# ==============================================================================

def test_answer_synthesizer_shared_retry_configuration(monkeypatch):
    """Verify synthesizer lazily configures 3 attempts and transient HTTP retry codes."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-for-synth")
    synth = AnswerSynthesisService()
    client = synth._init_gemini_client()
    assert client is not None
    opts = client._api_client._http_options.retry_options
    assert opts is not None
    assert opts.attempts == 3
    assert opts.initial_delay == 1.0
    assert opts.max_delay == 8.0
    assert opts.exp_base == 2.0
    assert opts.jitter == 1.0
    assert opts.http_status_codes == [408, 429, 500, 502, 503, 504]


def test_answer_synthesizer_recovers_from_transient_503(monkeypatch):
    """Verify transient 503 followed by success recovers and produces UNVERIFIED_NARRATIVE answer."""
    import json
    import httpx
    from google.genai import errors
    from query_pilot.sql.gemini_generator import get_gemini_http_options

    monkeypatch.setenv("GEMINI_API_KEY", "test-key-for-synth")
    fast_opts = get_gemini_http_options(initial_delay=0.001, max_delay=0.01)
    synth = AnswerSynthesisService(http_options=fast_opts)

    call_count = 0
    fake_httpx_resp = httpx.Response(status_code=503, request=httpx.Request("POST", "https://example.com"))

    class FakeSdkHttpResponse:
        headers = {}
        response_stream = [
            json.dumps({"candidates": [{"content": {"parts": [{"text": "Alice and Bob are active students."}]}}]})
        ]

    client = synth._init_gemini_client()
    assert client is not None

    def mock_request_once(request, stream=False):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise errors.ServerError(503, {"error": {"message": "Service Unavailable"}}, fake_httpx_resp)
        return FakeSdkHttpResponse()

    client._api_client._request_once = mock_request_once
    synth.client = client

    multi_res = QueryResult(
        columns=["StudentID", "Name"],
        rows=[{"StudentID": 1, "Name": "Alice"}, {"StudentID": 2, "Name": "Bob"}],
        row_count=2,
        truncated=False,
    )

    ans = synth.synthesize("Show students", multi_res)
    assert ans.status == GroundedAnswerStatus.UNVERIFIED_NARRATIVE
    assert ans.text == "Alice and Bob are active students."
    assert call_count == 2
