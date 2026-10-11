"""Focused tests for QueryPilot unified execution interface (Task 1.20).

Verifies the unified QueryRunner and run_query facade:
- Default execution path is sequential QueryPipeline.
- Explicit selection of LangGraph QueryPilotWorkflow.
- Unselected implementation is never instantiated or executed.
- Common PipelineResult contract parity across all terminal outcomes.
- Strict rejection of invalid modes.
- Complete offline isolation with fake components.
- Preservation of existing QueryPipeline behavior.
"""

from typing import List, Optional
from unittest.mock import MagicMock, patch
import pytest

from query_pilot.db.executor import QueryResult
from query_pilot.db.models import (
    ColumnMetadata,
    DatabaseSchema,
    ForeignKeyMetadata,
    TableMetadata,
)
from query_pilot.pipeline import PipelineResult, PipelineStatus, QueryPipeline
from query_pilot.presentation.formatter import PresentationResult
from query_pilot.runner import (
    ExecutionMode,
    QueryRunner,
    normalize_execution_mode,
    run_query,
)
from query_pilot.sql.generation import (
    SQLGenerationError,
    SQLGenerationRequest,
    SQLGenerationResponse,
    SQLGenerationStatus,
    SQLGenerator,
)
from query_pilot.sql.models import ValidationReason, ValidationReasonCode, ValidationResult


# ==============================================================================
# In-Memory Test Fixtures & Fakes
# ==============================================================================

@pytest.fixture
def test_schema() -> DatabaseSchema:
    """Deterministic in-memory model-facing schema for unit tests."""
    users_table = TableMetadata(
        schema_name="dbo",
        table_name="Users",
        columns=[
            ColumnMetadata(name="Id", data_type="int", nullable=False, primary_key=True),
            ColumnMetadata(name="Email", data_type="varchar(255)", nullable=False),
            ColumnMetadata(name="FullName", data_type="varchar(255)", nullable=False),
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
        ],
        primary_keys=["Id"],
        foreign_keys=[
            ForeignKeyMetadata(
                name="FK_Students_Users",
                source_table="Students",
                source_column="Id",
                target_table="Users",
                target_column="Id",
            ),
        ],
    )

    return DatabaseSchema(
        database_name="GradeSense_Local",
        tables=[users_table, students_table],
        is_model_facing=True,
    )


class FakeSQLGenerator(SQLGenerator):
    """Configurable fake SQL generator for offline testing."""

    def __init__(
        self,
        response: Optional[SQLGenerationResponse] = None,
        exc_to_raise: Optional[Exception] = None,
    ):
        self.response = response or SQLGenerationResponse(
            status=SQLGenerationStatus.ANSWERABLE,
            sql="SELECT Id FROM Students;",
            explanation="Selects all student IDs.",
            assumptions=["Active enrollment"],
        )
        self.exc_to_raise = exc_to_raise
        self.calls: List[SQLGenerationRequest] = []

    def generate(self, request: SQLGenerationRequest) -> SQLGenerationResponse:
        self.calls.append(request)
        if self.exc_to_raise:
            raise self.exc_to_raise
        return self.response


# ==============================================================================
# 1. Default Selection Uses Sequential QueryPipeline
# ==============================================================================

def test_default_selection_uses_sequential_pipeline(test_schema: DatabaseSchema) -> None:
    """Verify that QueryRunner defaults to sequential pipeline and never calls graph."""
    runner = QueryRunner(
        generator=FakeSQLGenerator(),
        schema=test_schema,
        engine=MagicMock(),
    )
    assert runner.default_mode == ExecutionMode.SEQUENTIAL

    with patch("query_pilot.pipeline.QueryPipeline.run") as mock_pipe_run, \
         patch("query_pilot.graph.workflow.QueryPilotWorkflow.run") as mock_graph_run:

        mock_pipe_res = PipelineResult(
            question="Default question",
            status=PipelineStatus.SUCCESS,
        )
        mock_pipe_run.return_value = mock_pipe_res

        result = runner.run("Default question", request_id="req-default")

        # Call-count assertions
        mock_pipe_run.assert_called_once_with("Default question", request_id="req-default")
        mock_graph_run.assert_not_called()
        assert result is mock_pipe_res


# ==============================================================================
# 2. Explicit Graph Selection Invokes LangGraph Workflow
# ==============================================================================

def test_explicit_graph_selection_invokes_graph(test_schema: DatabaseSchema) -> None:
    """Verify explicit graph mode invokes QueryPilotWorkflow and never calls pipeline."""
    runner = QueryRunner(
        mode=ExecutionMode.GRAPH,
        generator=FakeSQLGenerator(),
        schema=test_schema,
        engine=MagicMock(),
    )
    assert runner.default_mode == ExecutionMode.GRAPH

    with patch("query_pilot.pipeline.QueryPipeline.run") as mock_pipe_run, \
         patch("query_pilot.graph.workflow.QueryPilotWorkflow.run") as mock_graph_run:

        mock_state = {
            "question": "Graph question",
            "status": PipelineStatus.SUCCESS,
            "request_id": "req-graph",
        }
        mock_graph_run.return_value = mock_state

        result = runner.run("Graph question", request_id="req-graph")

        # Call-count assertions
        mock_graph_run.assert_called_once_with("Graph question", request_id="req-graph")
        mock_pipe_run.assert_not_called()

        assert isinstance(result, PipelineResult)
        assert result.question == "Graph question"
        assert result.status == PipelineStatus.SUCCESS
        assert result.request_id == "req-graph"


# ==============================================================================
# 3. Per-Run Execution Mode Override
# ==============================================================================

def test_per_run_mode_override(test_schema: DatabaseSchema) -> None:
    """Verify that mode argument in runner.run overrides the instance default."""
    runner = QueryRunner(
        mode=ExecutionMode.SEQUENTIAL,
        generator=FakeSQLGenerator(),
        schema=test_schema,
        engine=MagicMock(),
    )

    with patch("query_pilot.pipeline.QueryPipeline.run") as mock_pipe_run, \
         patch("query_pilot.graph.workflow.QueryPilotWorkflow.run") as mock_graph_run:

        mock_pipe_run.return_value = PipelineResult(question="Q", status=PipelineStatus.SUCCESS)
        mock_graph_run.return_value = {"question": "Q", "status": PipelineStatus.SUCCESS}

        # Override default (sequential) with graph
        res_graph = runner.run("Q", mode="graph")
        assert mock_graph_run.call_count == 1
        assert mock_pipe_run.call_count == 0
        assert isinstance(res_graph, PipelineResult)

        # Run with explicit sequential
        res_seq = runner.run("Q", mode="sequential")
        assert mock_graph_run.call_count == 1
        assert mock_pipe_run.call_count == 1
        assert isinstance(res_seq, PipelineResult)


# ==============================================================================
# 4. Common Result Contract Parity on Success
# ==============================================================================

def test_success_contract_parity_across_modes(test_schema: DatabaseSchema) -> None:
    """Verify that both sequential and graph paths produce complete, equivalent PipelineResult models."""
    fake_generator = FakeSQLGenerator(
        response=SQLGenerationResponse(
            status=SQLGenerationStatus.ANSWERABLE,
            sql="SELECT Id, Email FROM Users;",
            explanation="Selects user IDs and emails.",
        )
    )

    mock_exec_result = QueryResult(
        columns=["Id", "Email"],
        rows=[{"Id": 1, "Email": "alice@example.com"}],
        row_count=1,
        truncated=False,
        execution_time_ms=5.0,
    )

    with patch("query_pilot.pipeline.execute_read_only", return_value=mock_exec_result), \
         patch("query_pilot.graph.workflow.execute_read_only", return_value=mock_exec_result), \
         patch("query_pilot.pipeline.validate_sql", return_value=ValidationResult(allowed=True)), \
         patch("query_pilot.graph.workflow.validate_sql", return_value=ValidationResult(allowed=True)):

        runner_seq = QueryRunner(mode=ExecutionMode.SEQUENTIAL, generator=fake_generator, schema=test_schema, engine=MagicMock())
        runner_graph = QueryRunner(mode=ExecutionMode.GRAPH, generator=fake_generator, schema=test_schema, engine=MagicMock())

        res_seq = runner_seq.run("List users", request_id="req-seq")
        res_graph = runner_graph.run("List users", request_id="req-graph")

        for res in [res_seq, res_graph]:
            assert isinstance(res, PipelineResult)
            assert res.status == PipelineStatus.SUCCESS
            assert res.is_success is True
            assert res.is_unsupported is False
            assert res.generated_sql == "SELECT Id, Email FROM Users;"
            assert res.generation_explanation == "Selects user IDs and emails."
            assert res.validation_result.allowed is True
            assert res.execution_result.row_count == 1
            assert res.execution_result.rows == [{"Id": 1, "Email": "alice@example.com"}]
            assert isinstance(res.presentation, PresentationResult)
            assert res.presentation.status == "success"
            assert res.presentation.scalar_value is None
            assert "| Id | Email |" in res.presentation.markdown_table


# ==============================================================================
# 5. Unsupported Question Parity (Zero Execution Fabrication)
# ==============================================================================

def test_unsupported_question_parity(test_schema: DatabaseSchema) -> None:
    """Verify unsupported questions preserve status and do not fabricate results across both modes."""
    fake_generator = FakeSQLGenerator(
        response=SQLGenerationResponse(
            status=SQLGenerationStatus.UNSUPPORTED,
            sql=None,
            explanation="Schema lacks tuition fee data.",
        )
    )

    runner_seq = QueryRunner(mode=ExecutionMode.SEQUENTIAL, generator=fake_generator, schema=test_schema, engine=MagicMock())
    runner_graph = QueryRunner(mode=ExecutionMode.GRAPH, generator=fake_generator, schema=test_schema, engine=MagicMock())

    with patch("query_pilot.pipeline.execute_read_only") as mock_exec_seq, \
         patch("query_pilot.graph.workflow.execute_read_only") as mock_exec_graph, \
         patch("query_pilot.pipeline.validate_sql") as mock_val_seq, \
         patch("query_pilot.graph.workflow.validate_sql") as mock_val_graph:

        res_seq = runner_seq.run("What is tuition?")
        res_graph = runner_graph.run("What is tuition?")

        # Neither path calls validation or execution
        mock_val_seq.assert_not_called()
        mock_val_graph.assert_not_called()
        mock_exec_seq.assert_not_called()
        mock_exec_graph.assert_not_called()

        for res in [res_seq, res_graph]:
            assert res.status == PipelineStatus.UNSUPPORTED
            assert res.is_unsupported is True
            assert res.is_success is False
            assert res.generated_sql is None
            assert res.execution_result is None
            assert res.validation_result is None
            assert res.presentation.status == "unsupported"
            assert "does not contain the information required" in res.presentation.message


# ==============================================================================
# 6. Validation Rejection Parity
# ==============================================================================

def test_validation_rejection_parity(test_schema: DatabaseSchema) -> None:
    """Verify rejected SQL terminates without database execution across both modes."""
    fake_generator = FakeSQLGenerator(
        response=SQLGenerationResponse(
            status=SQLGenerationStatus.ANSWERABLE,
            sql="DROP TABLE Users;",
            explanation="Unsafe SQL.",
        )
    )

    rejection = ValidationResult(
        allowed=False,
        reasons=[
            ValidationReason(
                code=ValidationReasonCode.FORBIDDEN_OPERATION,
                message="DROP operation is prohibited.",
            )
        ],
    )

    runner_seq = QueryRunner(mode=ExecutionMode.SEQUENTIAL, generator=fake_generator, schema=test_schema, engine=MagicMock())
    runner_graph = QueryRunner(mode=ExecutionMode.GRAPH, generator=fake_generator, schema=test_schema, engine=MagicMock())

    with patch("query_pilot.pipeline.validate_sql", return_value=rejection), \
         patch("query_pilot.graph.workflow.validate_sql", return_value=rejection), \
         patch("query_pilot.pipeline.execute_read_only") as mock_exec_seq, \
         patch("query_pilot.graph.workflow.execute_read_only") as mock_exec_graph:

        res_seq = runner_seq.run("Drop users")
        res_graph = runner_graph.run("Drop users")

        mock_exec_seq.assert_not_called()
        mock_exec_graph.assert_not_called()

        for res in [res_seq, res_graph]:
            assert res.status == PipelineStatus.VALIDATION_REJECTED
            assert res.validation_result.allowed is False
            assert res.execution_result is None
            assert res.presentation.status == "validation_rejected"
            assert res.presentation.validation_reasons[0]["code"] == "FORBIDDEN_OPERATION"


# ==============================================================================
# 7. Generation Failure Parity
# ==============================================================================

def test_generation_failure_parity(test_schema: DatabaseSchema) -> None:
    """Verify generation failure routes cleanly without execution across both modes."""
    fake_generator = FakeSQLGenerator(
        exc_to_raise=SQLGenerationError("LLM quota exceeded")
    )

    runner_seq = QueryRunner(mode=ExecutionMode.SEQUENTIAL, generator=fake_generator, schema=test_schema, engine=MagicMock())
    runner_graph = QueryRunner(mode=ExecutionMode.GRAPH, generator=fake_generator, schema=test_schema, engine=MagicMock())

    with patch("query_pilot.pipeline.execute_read_only") as mock_exec_seq, \
         patch("query_pilot.graph.workflow.execute_read_only") as mock_exec_graph:

        res_seq = runner_seq.run("Any question")
        res_graph = runner_graph.run("Any question")

        mock_exec_seq.assert_not_called()
        mock_exec_graph.assert_not_called()

        for res in [res_seq, res_graph]:
            assert res.status == PipelineStatus.GENERATION_FAILED
            assert res.execution_result is None
            assert res.presentation.status == "generation_failed"
            assert "LLM quota exceeded" in res.presentation.message


# ==============================================================================
# 8. Rejection of Invalid Implementation Selections
# ==============================================================================

def test_invalid_mode_selection_rejected() -> None:
    """Verify invalid execution modes raise clear errors and never silently fallback."""
    with pytest.raises(ValueError, match="Invalid execution mode: 'parallel'"):
        QueryRunner(mode="parallel")

    with pytest.raises(ValueError, match="Invalid execution mode: 'langchain'"):
        QueryRunner(mode="langchain")

    with pytest.raises(TypeError, match="Execution mode must be an ExecutionMode or string"):
        QueryRunner(mode=123)  # type: ignore[arg-type]

    runner = QueryRunner()  # Valid default
    with pytest.raises(ValueError, match="Invalid execution mode: 'invalid_override'"):
        runner.run("Question", mode="invalid_override")


def test_invalid_question_rejected() -> None:
    """Verify empty or non-string questions are rejected with ValueError/TypeError."""
    runner = QueryRunner()

    with pytest.raises(TypeError, match="Question must be a string"):
        runner.run(None)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="Question cannot be empty or whitespace"):
        runner.run("")

    with pytest.raises(ValueError, match="Question cannot be empty or whitespace"):
        runner.run("   \n\t ")


# ==============================================================================
# 9. Public run_query Convenience Function
# ==============================================================================

def test_public_run_query_function(test_schema: DatabaseSchema) -> None:
    """Verify standalone run_query function works with default and explicit modes."""
    fake_generator = FakeSQLGenerator()

    with patch("query_pilot.pipeline.validate_sql", return_value=ValidationResult(allowed=True)), \
         patch("query_pilot.graph.workflow.validate_sql", return_value=ValidationResult(allowed=True)), \
         patch("query_pilot.pipeline.execute_read_only", return_value=QueryResult(columns=["Id"], rows=[{"Id": 1}], row_count=1, truncated=False)), \
         patch("query_pilot.graph.workflow.execute_read_only", return_value=QueryResult(columns=["Id"], rows=[{"Id": 1}], row_count=1, truncated=False)):

        # Default mode (sequential)
        res_default = run_query(
            "Show user 1",
            generator=fake_generator,
            schema=test_schema,
            engine=MagicMock(),
            request_id="req-fn-seq",
        )
        assert isinstance(res_default, PipelineResult)
        assert res_default.status == PipelineStatus.SUCCESS
        assert res_default.request_id == "req-fn-seq"

        # Explicit graph mode
        res_graph = run_query(
            "Show user 1",
            mode=ExecutionMode.GRAPH,
            generator=fake_generator,
            schema=test_schema,
            engine=MagicMock(),
            request_id="req-fn-graph",
        )
        assert isinstance(res_graph, PipelineResult)
        assert res_graph.status == PipelineStatus.SUCCESS
        assert res_graph.request_id == "req-fn-graph"


# ==============================================================================
# 10. Dependency Safety: No Build or Execution Time Side Effects
# ==============================================================================

def test_fake_dependencies_prevent_external_calls(test_schema: DatabaseSchema) -> None:
    """Verify that using fake dependencies completely isolates runner from DB and Gemini."""
    with patch("query_pilot.db.connection.create_db_engine") as mock_engine, \
         patch("query_pilot.sql.gemini_generator.GeminiSQLGenerator") as mock_gemini:

        runner = QueryRunner(
            generator=FakeSQLGenerator(),
            schema=test_schema,
            engine=MagicMock(),
        )
        assert runner is not None

        # Neither Gemini nor DB engine is touched at init
        mock_engine.assert_not_called()
        mock_gemini.assert_not_called()


# ==============================================================================
# 11. Existing QueryPipeline Behavior Remained Completely Intact
# ==============================================================================

def test_existing_pipeline_unaffected_by_runner(test_schema: DatabaseSchema) -> None:
    """Verify direct QueryPipeline calls continue to function identically."""
    fake_generator = FakeSQLGenerator(
        response=SQLGenerationResponse(
            status=SQLGenerationStatus.ANSWERABLE,
            sql="SELECT Id FROM Users;",
        )
    )

    with patch("query_pilot.pipeline.validate_sql", return_value=ValidationResult(allowed=True)), \
         patch("query_pilot.pipeline.execute_read_only", return_value=QueryResult(columns=["Id"], rows=[{"Id": 1}], row_count=1, truncated=False)):

        pipe = QueryPipeline(
            generator=fake_generator,
            schema=test_schema,
            engine=MagicMock(),
        )

        res = pipe.run("Find users", request_id="direct-pipe-req")
        assert isinstance(res, PipelineResult)
        assert res.is_success is True
        assert res.request_id == "direct-pipe-req"


# ==============================================================================
# 12. Complete Terminal Status Mapping & ErrorCategory Assertions
# ==============================================================================

def test_terminal_status_mapping_and_error_categories(test_schema: DatabaseSchema) -> None:
    """Verify all 5 terminal graph statuses map to equivalent PipelineStatus and ErrorCategory."""
    from query_pilot.observability import ErrorCategory
    from query_pilot.graph.workflow import QueryPilotWorkflow

    # 1. SUCCESS
    gen_success = FakeSQLGenerator(
        response=SQLGenerationResponse(
            status=SQLGenerationStatus.ANSWERABLE,
            sql="SELECT Id FROM Users;",
        )
    )
    mock_exec = QueryResult(columns=["Id"], rows=[{"Id": 1}], row_count=1, truncated=False)

    with patch("query_pilot.graph.workflow.validate_sql", return_value=ValidationResult(allowed=True)), \
         patch("query_pilot.graph.workflow.execute_read_only", return_value=mock_exec):

        runner = QueryRunner(mode=ExecutionMode.GRAPH, generator=gen_success, schema=test_schema, engine=MagicMock())
        res = runner.run("List users")

        assert res.status == PipelineStatus.SUCCESS
        assert res.is_success is True
        assert res.generated_sql == "SELECT Id FROM Users;"
        assert res.execution_result is not None
        assert res.presentation.status == "success"

    # 2. UNSUPPORTED
    gen_unsupported = FakeSQLGenerator(
        response=SQLGenerationResponse(
            status=SQLGenerationStatus.UNSUPPORTED,
            sql=None,
            explanation="Schema lacks tuition data.",
        )
    )
    runner_unsup = QueryRunner(mode=ExecutionMode.GRAPH, generator=gen_unsupported, schema=test_schema, engine=MagicMock())
    res_unsup = runner_unsup.run("Tuition costs?")

    assert res_unsup.status == PipelineStatus.UNSUPPORTED
    assert res_unsup.is_unsupported is True
    assert res_unsup.generated_sql is None
    assert res_unsup.validation_result is None
    assert res_unsup.execution_result is None
    assert res_unsup.presentation.status == "unsupported"

    # Also verify the underlying graph workflow sets exact ErrorCategory
    wf_unsup = QueryPilotWorkflow(generator=gen_unsupported, schema=test_schema, engine=MagicMock())
    state_unsup = wf_unsup.run("Tuition costs?")
    assert state_unsup["status"] == PipelineStatus.UNSUPPORTED
    assert state_unsup["error_category"] == ErrorCategory.UNSUPPORTED_QUESTION

    # 3. GENERATION_FAILED
    gen_failed = FakeSQLGenerator(exc_to_raise=SQLGenerationError("LLM quota exceeded"))
    runner_gen_fail = QueryRunner(mode=ExecutionMode.GRAPH, generator=gen_failed, schema=test_schema, engine=MagicMock())
    res_gen_fail = runner_gen_fail.run("Fail query")

    assert res_gen_fail.status == PipelineStatus.GENERATION_FAILED
    assert res_gen_fail.generated_sql is None
    assert res_gen_fail.validation_result is None
    assert res_gen_fail.execution_result is None
    assert res_gen_fail.presentation.status == "generation_failed"

    wf_gen_fail = QueryPilotWorkflow(generator=gen_failed, schema=test_schema, engine=MagicMock())
    state_gen_fail = wf_gen_fail.run("Fail query")
    assert state_gen_fail["status"] == PipelineStatus.GENERATION_FAILED
    assert state_gen_fail["error_category"] == ErrorCategory.SQL_GENERATION_FAILED

    # 4. VALIDATION_REJECTED
    gen_forbid = FakeSQLGenerator(
        response=SQLGenerationResponse(
            status=SQLGenerationStatus.ANSWERABLE,
            sql="DROP TABLE Students;",
        )
    )
    val_reject = ValidationResult(
        allowed=False,
        reasons=[
            ValidationReason(
                code=ValidationReasonCode.FORBIDDEN_OPERATION,
                message="DROP forbidden.",
            )
        ],
    )
    with patch("query_pilot.graph.workflow.validate_sql", return_value=val_reject):
        runner_val_rej = QueryRunner(mode=ExecutionMode.GRAPH, generator=gen_forbid, schema=test_schema, engine=MagicMock())
        res_val_rej = runner_val_rej.run("Drop students")

        assert res_val_rej.status == PipelineStatus.VALIDATION_REJECTED
        assert res_val_rej.generated_sql == "DROP TABLE Students;"
        assert res_val_rej.validation_result.allowed is False
        assert res_val_rej.execution_result is None
        assert res_val_rej.presentation.status == "validation_rejected"

        wf_val_rej = QueryPilotWorkflow(generator=gen_forbid, schema=test_schema, engine=MagicMock())
        state_val_rej = wf_val_rej.run("Drop students")
        assert state_val_rej["status"] == PipelineStatus.VALIDATION_REJECTED
        assert state_val_rej["error_category"] == ErrorCategory.VALIDATION_REJECTED

    # 5. EXECUTION_FAILED
    from query_pilot.db.executor import QueryExecutionError
    with patch("query_pilot.graph.workflow.validate_sql", return_value=ValidationResult(allowed=True)), \
         patch("query_pilot.graph.workflow.execute_read_only", side_effect=QueryExecutionError("Deadlock detected")):

        runner_exec_fail = QueryRunner(mode=ExecutionMode.GRAPH, generator=gen_success, schema=test_schema, engine=MagicMock())
        res_exec_fail = runner_exec_fail.run("Deadlock query")

        assert res_exec_fail.status == PipelineStatus.EXECUTION_FAILED
        assert res_exec_fail.execution_result is None
        assert res_exec_fail.presentation.status == "execution_failed"
        assert "Deadlock detected" in res_exec_fail.presentation.message

        wf_exec_fail = QueryPilotWorkflow(generator=gen_success, schema=test_schema, engine=MagicMock())
        state_exec_fail = wf_exec_fail.run("Deadlock query")
        assert state_exec_fail["status"] == PipelineStatus.EXECUTION_FAILED
        assert state_exec_fail["error_category"] == ErrorCategory.EXECUTION_FAILED


# ==============================================================================
# 13. Adapter Resilience: Missing Fields & Status Inference
# ==============================================================================

def test_adapter_missing_intermediate_fields() -> None:
    """Verify graph_state_to_pipeline_result handles missing optional fields robustly."""
    from query_pilot.observability import ErrorCategory
    from query_pilot.runner import graph_state_to_pipeline_result

    # Minimal state with only question and status
    min_state = {
        "question": "What is x?",
        "status": PipelineStatus.SUCCESS,
    }
    res = graph_state_to_pipeline_result(min_state)  # type: ignore[arg-type]
    assert res.question == "What is x?"
    assert res.status == PipelineStatus.SUCCESS
    assert res.generated_sql is None
    assert res.validation_result is None
    assert res.execution_result is None
    assert res.presentation is not None
    assert res.presentation.status == "success"

    # State with missing status but error_category / error_message present:
    # Must NOT silently map to SUCCESS
    err_state = {
        "question": "Failed state",
        "error_category": ErrorCategory.SQL_GENERATION_FAILED,
        "error_message": "Generation aborted",
    }
    res_err = graph_state_to_pipeline_result(err_state)  # type: ignore[arg-type]
    assert res_err.status == PipelineStatus.GENERATION_FAILED
    assert res_err.error_message == "Generation aborted"
    assert res_err.presentation.status == "generation_failed"

    # Unsupported error category inference
    unsup_state = {
        "question": "Unsup state",
        "error_category": ErrorCategory.UNSUPPORTED_QUESTION,
    }
    res_unsup = graph_state_to_pipeline_result(unsup_state)  # type: ignore[arg-type]
    assert res_unsup.status == PipelineStatus.UNSUPPORTED
    assert res_unsup.presentation.status == "unsupported"

    # Validation rejected error category inference
    val_state = {
        "question": "Val state",
        "error_category": ErrorCategory.VALIDATION_REJECTED,
    }
    res_val = graph_state_to_pipeline_result(val_state)  # type: ignore[arg-type]
    assert res_val.status == PipelineStatus.VALIDATION_REJECTED
    assert res_val.presentation.status == "validation_rejected"

    # Execution failed error category inference
    exec_state = {
        "question": "Exec state",
        "error_category": ErrorCategory.EXECUTION_FAILED,
    }
    res_exec = graph_state_to_pipeline_result(exec_state)  # type: ignore[arg-type]
    assert res_exec.status == PipelineStatus.EXECUTION_FAILED
    assert res_exec.presentation.status == "execution_failed"


def test_adapter_rejects_invalid_status() -> None:
    """Verify graph_state_to_pipeline_result rejects invalid status types or names."""
    from query_pilot.runner import graph_state_to_pipeline_result

    with pytest.raises(ValueError, match="Unrecognized pipeline status"):
        graph_state_to_pipeline_result({"question": "Q", "status": "NONEXISTENT_STATUS"})  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="Status must be a PipelineStatus or str"):
        graph_state_to_pipeline_result({"question": "Q", "status": 999})  # type: ignore[arg-type]


# ==============================================================================
# 14. Constructor Call-Count Assertions (Zero Unselected Construction)
# ==============================================================================

def test_unselected_implementation_not_constructed(test_schema: DatabaseSchema) -> None:
    """Verify that selecting sequential never instantiates workflow, and vice-versa."""
    with patch("query_pilot.pipeline.QueryPipeline.__init__", return_value=None) as mock_pipe_init, \
         patch("query_pilot.pipeline.QueryPipeline.run") as mock_pipe_run, \
         patch("query_pilot.graph.workflow.QueryPilotWorkflow.__init__", return_value=None) as mock_graph_init, \
         patch("query_pilot.graph.workflow.QueryPilotWorkflow.run") as mock_graph_run:

        mock_pipe_run.return_value = PipelineResult(question="Q", status=PipelineStatus.SUCCESS)
        mock_graph_run.return_value = {"question": "Q", "status": PipelineStatus.SUCCESS}

        # 1. Sequential mode
        runner_seq = QueryRunner(mode=ExecutionMode.SEQUENTIAL, schema=test_schema)
        # At init: neither is constructed
        assert mock_pipe_init.call_count == 0
        assert mock_graph_init.call_count == 0

        # At run: only pipeline is constructed and executed
        runner_seq.run("Question for sequential")
        assert mock_pipe_init.call_count == 1
        assert mock_pipe_run.call_count == 1
        assert mock_graph_init.call_count == 0
        assert mock_graph_run.call_count == 0

        # Reset mocks
        mock_pipe_init.reset_mock()
        mock_pipe_run.reset_mock()
        mock_graph_init.reset_mock()
        mock_graph_run.reset_mock()

        # 2. Graph mode
        runner_graph = QueryRunner(mode=ExecutionMode.GRAPH, schema=test_schema)
        assert mock_pipe_init.call_count == 0
        assert mock_graph_init.call_count == 0

        runner_graph.run("Question for graph")
        assert mock_graph_init.call_count == 1
        assert mock_graph_run.call_count == 1
        assert mock_pipe_init.call_count == 0
        assert mock_pipe_run.call_count == 0


# ==============================================================================
# 15. Default Mode Immutability Across Overrides
# ==============================================================================

def test_per_call_mode_does_not_mutate_default(test_schema: DatabaseSchema) -> None:
    """Verify runner.default_mode remains unchanged after per-call mode overrides."""
    runner = QueryRunner(mode=ExecutionMode.SEQUENTIAL, schema=test_schema)
    assert runner.default_mode == ExecutionMode.SEQUENTIAL

    with patch("query_pilot.graph.workflow.QueryPilotWorkflow.run", return_value={"question": "Q", "status": PipelineStatus.SUCCESS}):
        runner.run("Override to graph", mode="graph")

    # Default mode must remain SEQUENTIAL
    assert runner.default_mode == ExecutionMode.SEQUENTIAL

