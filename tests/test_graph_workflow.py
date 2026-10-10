"""Focused automated tests for QueryPilot LangGraph workflow (Task 1.19).

Tests end-to-end graph orchestration, explicit node execution, conditional routing,
safeguard enforcement, and call-count assertions entirely offline without live
Gemini API calls or database connections.
"""

from typing import List, Optional
from unittest.mock import MagicMock, patch
import pytest

from query_pilot.db.executor import (
    QueryExecutionError,
    QueryResult,
)
from query_pilot.db.models import (
    ColumnMetadata,
    DatabaseSchema,
    ForeignKeyMetadata,
    TableMetadata,
)
from query_pilot.graph.state import (
    QueryPilotState,
    create_initial_state,
    validate_initial_state,
)
from query_pilot.graph.workflow import (
    QueryPilotWorkflow,
    create_query_graph,
    run_query_graph,
)
from query_pilot.pipeline import PipelineResult, PipelineStatus, QueryPipeline
from query_pilot.presentation.formatter import PresentationResult
from query_pilot.sql.generation import (
    SQLGenerationError,
    SQLGenerationRequest,
    SQLGenerationResponse,
    SQLGenerationStatus,
    SQLGenerator,
)
from query_pilot.sql.models import (
    ValidationReason,
    ValidationReasonCode,
    ValidationResult,
)


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
# 1. Successful Path: Schema -> Gen -> Val -> Exec -> Present
# ==============================================================================

def test_successful_workflow_execution(test_schema: DatabaseSchema) -> None:
    """Verify complete successful pipeline flow across all 5 nodes."""
    fake_generator = FakeSQLGenerator(
        response=SQLGenerationResponse(
            status=SQLGenerationStatus.ANSWERABLE,
            sql="SELECT Id, EnrollmentNumber FROM Students;",
            explanation="Selects students.",
        )
    )

    mock_exec_result = QueryResult(
        columns=["Id", "EnrollmentNumber"],
        rows=[{"Id": 1, "EnrollmentNumber": "ENR001"}, {"Id": 2, "EnrollmentNumber": "ENR002"}],
        row_count=2,
        truncated=False,
        execution_time_ms=8.5,
    )

    with patch("query_pilot.graph.workflow.execute_read_only", return_value=mock_exec_result) as mock_exec, \
         patch("query_pilot.graph.workflow.validate_sql") as mock_val:

        mock_val.return_value = ValidationResult(
            allowed=True,
            normalized_sql="SELECT Id, EnrollmentNumber FROM Students;",
        )

        workflow = QueryPilotWorkflow(
            generator=fake_generator,
            schema=test_schema,
            engine=MagicMock(),
        )

        result: QueryPilotState = workflow.run("Show all student enrollment numbers", request_id="req-success")

        # Call-count assertions
        assert len(fake_generator.calls) == 1
        mock_val.assert_called_once()
        mock_exec.assert_called_once()

        # State outcome assertions
        assert result["status"] == PipelineStatus.SUCCESS
        assert result["request_id"] == "req-success"
        assert result["schema_context"] is test_schema
        assert result["generation_response"] is not None
        assert result["generation_response"].sql == "SELECT Id, EnrollmentNumber FROM Students;"
        assert result["validation_result"].allowed is True
        assert result["execution_result"] is mock_exec_result
        assert result["presentation_result"] is not None
        assert result["presentation_result"].status == "success"
        assert result["presentation_result"].row_count == 2
        assert "| Id | EnrollmentNumber |" in result["presentation_result"].markdown_table


# ==============================================================================
# 2. Unsupported Question: Terminates without Validation/Execution
# ==============================================================================

def test_unsupported_question_routing(test_schema: DatabaseSchema) -> None:
    """Verify unsupported questions route directly to present_result without validation or execution."""
    fake_generator = FakeSQLGenerator(
        response=SQLGenerationResponse(
            status=SQLGenerationStatus.UNSUPPORTED,
            sql=None,
            explanation="Tuition fees are not stored in this database.",
        )
    )

    with patch("query_pilot.graph.workflow.validate_sql") as mock_val, \
         patch("query_pilot.graph.workflow.execute_read_only") as mock_exec:

        workflow = QueryPilotWorkflow(
            generator=fake_generator,
            schema=test_schema,
            engine=MagicMock(),
        )

        result: QueryPilotState = workflow.run("What is the average student tuition fee?")

        # Generator is called once
        assert len(fake_generator.calls) == 1

        # Downstream operations MUST NOT be called
        mock_val.assert_not_called()
        mock_exec.assert_not_called()

        # Terminal state assertions
        assert result["status"] == PipelineStatus.UNSUPPORTED
        assert result["generation_response"].status == SQLGenerationStatus.UNSUPPORTED
        assert result.get("validation_result") is None
        assert result.get("execution_result") is None
        assert result["presentation_result"].status == "unsupported"
        assert "does not contain the information required" in result["presentation_result"].message


# ==============================================================================
# 3. Generation Failure: Terminates without Validation/Execution
# ==============================================================================

def test_generation_failure_routing(test_schema: DatabaseSchema) -> None:
    """Verify generation failure routes to present_result without calling validation or execution."""
    fake_generator = FakeSQLGenerator(
        exc_to_raise=SQLGenerationError("LLM rate limit reached")
    )

    with patch("query_pilot.graph.workflow.validate_sql") as mock_val, \
         patch("query_pilot.graph.workflow.execute_read_only") as mock_exec:

        workflow = QueryPilotWorkflow(
            generator=fake_generator,
            schema=test_schema,
            engine=MagicMock(),
        )

        result: QueryPilotState = workflow.run("List departments")

        assert len(fake_generator.calls) == 1
        mock_val.assert_not_called()
        mock_exec.assert_not_called()

        assert result["status"] == PipelineStatus.GENERATION_FAILED
        assert result.get("validation_result") is None
        assert result.get("execution_result") is None
        assert result["presentation_result"].status == "generation_failed"
        assert "LLM rate limit reached" in result["presentation_result"].message


# ==============================================================================
# 4. Validation Rejection: Execution is NEVER called
# ==============================================================================

def test_validation_rejection_routing(test_schema: DatabaseSchema) -> None:
    """Verify validation rejection stops execution and formats rejection properly."""
    fake_generator = FakeSQLGenerator(
        response=SQLGenerationResponse(
            status=SQLGenerationStatus.ANSWERABLE,
            sql="DROP TABLE Students;",
            explanation="Unsafe SQL.",
        )
    )

    rejection_result = ValidationResult(
        allowed=False,
        reasons=[
            ValidationReason(
                code=ValidationReasonCode.FORBIDDEN_OPERATION,
                message="Statement contains forbidden DROP operation.",
            )
        ],
    )

    with patch("query_pilot.graph.workflow.validate_sql", return_value=rejection_result) as mock_val, \
         patch("query_pilot.graph.workflow.execute_read_only") as mock_exec:

        workflow = QueryPilotWorkflow(
            generator=fake_generator,
            schema=test_schema,
            engine=MagicMock(),
        )

        result: QueryPilotState = workflow.run("Drop student table")

        assert len(fake_generator.calls) == 1
        mock_val.assert_called_once()
        mock_exec.assert_not_called()

        assert result["status"] == PipelineStatus.VALIDATION_REJECTED
        assert result["validation_result"].allowed is False
        assert result.get("execution_result") is None
        assert result["presentation_result"].status == "validation_rejected"
        assert len(result["presentation_result"].validation_reasons) == 1
        assert result["presentation_result"].validation_reasons[0]["code"] == "FORBIDDEN_OPERATION"


# ==============================================================================
# 5. Successful Validation: Execution is called exactly once
# ==============================================================================

def test_validation_success_calls_execution_once(test_schema: DatabaseSchema) -> None:
    """Verify that successful validation triggers execution exactly once."""
    fake_generator = FakeSQLGenerator(
        response=SQLGenerationResponse(
            status=SQLGenerationStatus.ANSWERABLE,
            sql="SELECT COUNT(*) FROM Students;",
        )
    )

    exec_result = QueryResult(
        columns=["Total"],
        rows=[{"Total": 42}],
        row_count=1,
        truncated=False,
    )

    with patch("query_pilot.graph.workflow.validate_sql") as mock_val, \
         patch("query_pilot.graph.workflow.execute_read_only", return_value=exec_result) as mock_exec:

        mock_val.return_value = ValidationResult(allowed=True, normalized_sql="SELECT COUNT(*) FROM Students;")

        workflow = QueryPilotWorkflow(
            generator=fake_generator,
            schema=test_schema,
            engine=MagicMock(),
        )

        result: QueryPilotState = workflow.run("Count students")

        assert mock_val.call_count == 1
        assert mock_exec.call_count == 1
        assert result["status"] == PipelineStatus.SUCCESS
        assert result["presentation_result"].scalar_value == 42


# ==============================================================================
# 6. Execution Failure: Terminates without Retries
# ==============================================================================

def test_execution_failure_terminates_without_retries(test_schema: DatabaseSchema) -> None:
    """Verify execution failure terminates cleanly without retries."""
    fake_generator = FakeSQLGenerator(
        response=SQLGenerationResponse(
            status=SQLGenerationStatus.ANSWERABLE,
            sql="SELECT 1;",
        )
    )

    with patch("query_pilot.graph.workflow.validate_sql") as mock_val, \
         patch("query_pilot.graph.workflow.execute_read_only", side_effect=QueryExecutionError("Query execution timeout")) as mock_exec:

        mock_val.return_value = ValidationResult(allowed=True, normalized_sql="SELECT 1;")

        workflow = QueryPilotWorkflow(
            generator=fake_generator,
            schema=test_schema,
            engine=MagicMock(),
        )

        result: QueryPilotState = workflow.run("Run timeout query")

        # Each stage is called exactly once (no retry loops)
        assert len(fake_generator.calls) == 1
        assert mock_val.call_count == 1
        assert mock_exec.call_count == 1

        assert result["status"] == PipelineStatus.EXECUTION_FAILED
        assert result["retry_count"] == 0
        assert result.get("execution_result") is None
        assert result["presentation_result"].status == "execution_failed"
        assert "Query execution timeout" in result["presentation_result"].message


# ==============================================================================
# 7. Schema Acquisition Failure: Terminates before Generation
# ==============================================================================

def test_schema_acquisition_failure() -> None:
    """Verify failure to acquire schema routes to present_result without calling generator."""
    fake_generator = FakeSQLGenerator()

    workflow = QueryPilotWorkflow(
        generator=fake_generator,
        schema=None,
        introspector=MagicMock(introspect_model_facing=MagicMock(side_effect=RuntimeError("DB Connection Lost"))),
        engine=MagicMock(),
    )

    with patch("query_pilot.graph.workflow.validate_sql") as mock_val, \
         patch("query_pilot.graph.workflow.execute_read_only") as mock_exec:

        result: QueryPilotState = workflow.run("Any question")

        assert len(fake_generator.calls) == 0
        mock_val.assert_not_called()
        mock_exec.assert_not_called()

        assert result["status"] == PipelineStatus.EXECUTION_FAILED
        assert result["presentation_result"].status == "execution_failed"
        assert "DB Connection Lost" in result["presentation_result"].message


# ==============================================================================
# 8. Node Ordering & Execution Sequence
# ==============================================================================

def test_node_execution_sequence(test_schema: DatabaseSchema) -> None:
    """Verify exact sequence of executed nodes in a successful run."""
    trace: List[str] = []

    workflow = QueryPilotWorkflow(
        generator=FakeSQLGenerator(),
        schema=test_schema,
        engine=MagicMock(),
    )

    orig_acquire = workflow.acquire_schema
    orig_generate = workflow.generate_sql
    orig_validate = workflow.validate_sql
    orig_execute = workflow.execute_sql
    orig_present = workflow.present_result

    def traced_acquire(state):
        trace.append("acquire_schema")
        return orig_acquire(state)

    def traced_generate(state):
        trace.append("generate_sql")
        return orig_generate(state)

    def traced_validate(state):
        trace.append("validate_sql")
        return orig_validate(state)

    def traced_execute(state):
        trace.append("execute_sql")
        return orig_execute(state)

    def traced_present(state):
        trace.append("present_result")
        return orig_present(state)

    workflow.acquire_schema = traced_acquire
    workflow.generate_sql = traced_generate
    workflow.validate_sql = traced_validate
    workflow.execute_sql = traced_execute
    workflow.present_result = traced_present

    with patch("query_pilot.graph.workflow.validate_sql", return_value=ValidationResult(allowed=True)), \
         patch("query_pilot.graph.workflow.execute_read_only", return_value=QueryResult(columns=[], rows=[], row_count=0, truncated=False)):

        workflow.run("Sequence test")

    assert trace == [
        "acquire_schema",
        "generate_sql",
        "validate_sql",
        "execute_sql",
        "present_result",
    ]


# ==============================================================================
# 9. No Import or Build Time Side Effects
# ==============================================================================

def test_building_graph_does_not_call_external_services() -> None:
    """Verify creating and compiling the graph incurs zero DB or Gemini activity."""
    with patch("query_pilot.db.connection.create_db_engine") as mock_engine, \
         patch("query_pilot.sql.gemini_generator.GeminiSQLGenerator") as mock_gemini:

        graph = create_query_graph(
            generator=FakeSQLGenerator(),
            schema=MagicMock(is_model_facing=True),
        )
        assert graph is not None

        mock_engine.assert_not_called()
        mock_gemini.assert_not_called()


# ==============================================================================
# 10. Public run_query_graph Entry Point
# ==============================================================================

def test_public_run_query_graph_entry_point(test_schema: DatabaseSchema) -> None:
    """Verify run_query_graph helper accepts string question and precompiled graph."""
    fake_generator = FakeSQLGenerator()
    graph = create_query_graph(
        generator=fake_generator,
        schema=test_schema,
        engine=MagicMock(),
    )

    with patch("query_pilot.graph.workflow.validate_sql", return_value=ValidationResult(allowed=True)), \
         patch("query_pilot.graph.workflow.execute_read_only", return_value=QueryResult(columns=[], rows=[], row_count=0, truncated=False)):

        # Run with string question
        res1 = run_query_graph("How many students?", graph=graph, request_id="req-1")
        assert res1["status"] == PipelineStatus.SUCCESS
        assert res1["request_id"] == "req-1"

        # Run with pre-created initial state
        initial_state = create_initial_state("How many students?", request_id="req-2")
        res2 = run_query_graph(initial_state, graph=graph)
        assert res2["status"] == PipelineStatus.SUCCESS
        assert res2["request_id"] == "req-2"


# ==============================================================================
# 11. Existing Pipeline Remains Unchanged and Fully Functional
# ==============================================================================

def test_existing_pipeline_unaffected(test_schema: DatabaseSchema) -> None:
    """Verify QueryPipeline remains completely functional and unchanged."""
    fake_generator = FakeSQLGenerator(
        response=SQLGenerationResponse(
            status=SQLGenerationStatus.ANSWERABLE,
            sql="SELECT Id FROM Users;",
        )
    )

    with patch("query_pilot.pipeline.validate_sql", return_value=ValidationResult(allowed=True)), \
         patch("query_pilot.pipeline.execute_read_only", return_value=QueryResult(columns=["Id"], rows=[{"Id": 1}], row_count=1, truncated=False)):

        pipeline = QueryPipeline(
            generator=fake_generator,
            schema=test_schema,
            engine=MagicMock(),
        )

        pipe_res = pipeline.run("Find users")
        assert isinstance(pipe_res, PipelineResult)
        assert pipe_res.status == PipelineStatus.SUCCESS
        assert pipe_res.is_success is True


# ==============================================================================
# 12. Audit Verification Tests: State Immutability & Isolation
# ==============================================================================

def test_caller_initial_state_immutability(test_schema: DatabaseSchema) -> None:
    """Verify workflow never mutates caller's initial state dictionary in place."""
    caller_dict: dict = {"question": "Are there any active students?"}
    caller_dict_copy = dict(caller_dict)

    workflow = QueryPilotWorkflow(
        generator=FakeSQLGenerator(),
        schema=test_schema,
        engine=MagicMock(),
    )

    with patch("query_pilot.graph.workflow.validate_sql", return_value=ValidationResult(allowed=True)), \
         patch("query_pilot.graph.workflow.execute_read_only", return_value=QueryResult(columns=[], rows=[], row_count=0, truncated=False)):

        res = workflow.run(caller_dict, request_id="caller-immutable-req")
        assert res["status"] == PipelineStatus.SUCCESS
        assert res["request_id"] == "caller-immutable-req"

    # Caller's original dictionary MUST NOT have been mutated
    assert caller_dict == caller_dict_copy
    assert "request_id" not in caller_dict
    assert "status" not in caller_dict


def test_isolation_between_separate_graph_invocations(test_schema: DatabaseSchema) -> None:
    """Verify sequential invocations on the same workflow instance remain completely isolated."""
    gen1 = FakeSQLGenerator(
        response=SQLGenerationResponse(
            status=SQLGenerationStatus.UNSUPPORTED,
            sql=None,
            explanation="Unsupported topic.",
        )
    )
    gen2 = FakeSQLGenerator(
        response=SQLGenerationResponse(
            status=SQLGenerationStatus.ANSWERABLE,
            sql="SELECT Id FROM Users;",
            explanation="Select users.",
        )
    )

    workflow = QueryPilotWorkflow(
        generator=gen1,
        schema=test_schema,
        engine=MagicMock(),
    )

    # Invocation 1: Unsupported question
    res1 = workflow.run("Unsupported query")
    assert res1["status"] == PipelineStatus.UNSUPPORTED
    assert res1.get("execution_result") is None

    # Switch generator to gen2 on the same workflow instance
    workflow._generator = gen2

    with patch("query_pilot.graph.workflow.validate_sql", return_value=ValidationResult(allowed=True)), \
         patch("query_pilot.graph.workflow.execute_read_only", return_value=QueryResult(columns=["Id"], rows=[{"Id": 1}], row_count=1, truncated=False)):

        # Invocation 2: Answerable question
        res2 = workflow.run("Answerable query")
        assert res2["status"] == PipelineStatus.SUCCESS
        assert res2["generation_response"].status == SQLGenerationStatus.ANSWERABLE
        assert res2["execution_result"] is not None
        assert res2["execution_result"].row_count == 1
        assert res2["presentation_result"].status == "success"

    # Res1 must remain untouched
    assert res1["status"] == PipelineStatus.UNSUPPORTED
    assert res1.get("execution_result") is None


def test_execute_sql_defense_in_depth_blocks_unvalidated_query(test_schema: DatabaseSchema) -> None:
    """Verify execute_sql directly blocks execution if validation_result is missing or unallowed."""
    workflow = QueryPilotWorkflow(
        generator=FakeSQLGenerator(),
        schema=test_schema,
        engine=MagicMock(),
    )

    # State without validation_result
    state_no_val: QueryPilotState = {
        "question": "Unvalidated question",
        "generation_response": SQLGenerationResponse(
            status=SQLGenerationStatus.ANSWERABLE,
            sql="SELECT * FROM Passwords;",
        ),
    }

    result = workflow.execute_sql(state_no_val)
    assert result["status"] == PipelineStatus.VALIDATION_REJECTED
    assert "Execution blocked" in result["error_message"]

    # State with rejected validation_result
    state_rejected: QueryPilotState = {
        "question": "Rejected question",
        "generation_response": SQLGenerationResponse(
            status=SQLGenerationStatus.ANSWERABLE,
            sql="DROP TABLE Students;",
        ),
        "validation_result": ValidationResult(allowed=False),
    }

    result2 = workflow.execute_sql(state_rejected)
    assert result2["status"] == PipelineStatus.VALIDATION_REJECTED
    assert "Execution blocked" in result2["error_message"]

    # State with truthy non-boolean allowed value (not strictly True)
    state_not_exact_true: QueryPilotState = {
        "question": "Non-boolean allowed question",
        "generation_response": SQLGenerationResponse(
            status=SQLGenerationStatus.ANSWERABLE,
            sql="SELECT 1;",
        ),
        "validation_result": MagicMock(allowed="true"),  # truthy string, not boolean True
    }

    result3 = workflow.execute_sql(state_not_exact_true)
    assert result3["status"] == PipelineStatus.VALIDATION_REJECTED
    assert "Execution blocked" in result3["error_message"]


def test_sanitized_error_messages_in_state(test_schema: DatabaseSchema) -> None:
    """Verify that credentials and raw tracebacks are redacted from state error_message."""
    workflow = QueryPilotWorkflow(
        generator=FakeSQLGenerator(exc_to_raise=SQLGenerationError("Failed with password=SuperSecret123;")),
        schema=test_schema,
        engine=MagicMock(),
    )

    result = workflow.run("Question with sensitive error")
    assert result["status"] == PipelineStatus.GENERATION_FAILED
    assert "SuperSecret123" not in result["error_message"]
    assert "[REDACTED]" in result["error_message"]

