"""Focused automated tests for QueryPilot LangGraph workflow (Task 1.19).

Tests end-to-end graph orchestration, explicit node execution, conditional routing,
safeguard enforcement, and call-count assertions entirely offline without live
Gemini API calls or database connections.
"""

from typing import Any, List, Optional
from unittest.mock import MagicMock, patch
import pytest
from sqlalchemy.exc import DataError, OperationalError, ProgrammingError

from query_pilot.db.executor import (
    QueryExecutionError,
    QueryResult,
    QueryTimeoutError,
    UnsafeQueryError,
)
from query_pilot.db.models import (
    ColumnMetadata,
    DatabaseSchema,
    ForeignKeyMetadata,
    TableMetadata,
)
from query_pilot.graph.state import (
    MAX_GENERATION_ATTEMPTS,
    QueryPilotState,
    create_initial_state,
    validate_initial_state,
)
from query_pilot.graph.workflow import (
    QueryPilotWorkflow,
    create_query_graph,
    run_query_graph,
)
from query_pilot.observability import ErrorCategory
from query_pilot.pipeline import PipelineResult, PipelineStatus, QueryPipeline
from query_pilot.presentation.formatter import PresentationResult
from query_pilot.runner import ExecutionMode, QueryRunner
from query_pilot.sql.generation import (
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


class SequenceFakeSQLGenerator(SQLGenerator):
    """Fake SQL generator returning a sequence of responses/exceptions across calls."""

    def __init__(self, sequence: List[Any]):
        self.sequence = list(sequence)
        self.calls: List[SQLGenerationRequest] = []

    def generate(self, request: SQLGenerationRequest) -> SQLGenerationResponse:
        self.calls.append(request)
        if not self.sequence:
            raise RuntimeError("SequenceFakeSQLGenerator ran out of configured responses")
        item = self.sequence.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


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


# ==============================================================================
# 13. Task 1.23-B Bounded LangGraph Recovery Tests (20 Required Scenarios)
# ==============================================================================

def test_first_attempt_success_invokes_once(test_schema: DatabaseSchema) -> None:
    """Requirement 1: First-attempt success invokes generation once and execution once."""
    fake_generator = FakeSQLGenerator(
        response=SQLGenerationResponse(
            status=SQLGenerationStatus.ANSWERABLE,
            sql="SELECT Id FROM Students;",
        )
    )
    exec_result = QueryResult(columns=["Id"], rows=[{"Id": 1}], row_count=1, truncated=False)
    with patch("query_pilot.graph.workflow.validate_sql", return_value=ValidationResult(allowed=True)) as mock_val, \
         patch("query_pilot.graph.workflow.execute_read_only", return_value=exec_result) as mock_exec:
        workflow = QueryPilotWorkflow(generator=fake_generator, schema=test_schema, engine=MagicMock())
        result = workflow.run("Show students")

        assert len(fake_generator.calls) == 1
        assert mock_val.call_count == 1
        assert mock_exec.call_count == 1
        assert result["status"] == PipelineStatus.SUCCESS
        assert result.get("generation_attempts") == 1
        assert result.get("retry_count") == 0
        assert result.get("recovery_feedback") is None


def test_recoverable_validation_error_succeeds_on_second_attempt(test_schema: DatabaseSchema) -> None:
    """Requirement 2: Recoverable validation error followed by corrected SQL succeeds on 2nd attempt."""
    gen = SequenceFakeSQLGenerator([
        SQLGenerationResponse(status=SQLGenerationStatus.ANSWERABLE, sql="SELECT BadCol FROM Students;"),
        SQLGenerationResponse(status=SQLGenerationStatus.ANSWERABLE, sql="SELECT Id FROM Students;"),
    ])
    val_side_effects = [
        ValidationResult(
            allowed=False,
            reasons=[ValidationReason(code=ValidationReasonCode.UNKNOWN_COLUMN, message="Unknown column 'BadCol'")],
        ),
        ValidationResult(allowed=True, normalized_sql="SELECT Id FROM Students;"),
    ]
    exec_result = QueryResult(columns=["Id"], rows=[{"Id": 1}], row_count=1, truncated=False)

    with patch("query_pilot.graph.workflow.validate_sql", side_effect=val_side_effects) as mock_val, \
         patch("query_pilot.graph.workflow.execute_read_only", return_value=exec_result) as mock_exec:
        workflow = QueryPilotWorkflow(generator=gen, schema=test_schema, engine=MagicMock())
        result = workflow.run("Show students")

        assert len(gen.calls) == 2
        assert mock_val.call_count == 2
        assert mock_exec.call_count == 1
        assert result["status"] == PipelineStatus.SUCCESS
        assert result.get("generation_attempts") == 2
        assert result.get("retry_count") == 1
        # Second call received sanitized feedback
        assert gen.calls[1].instructions is not None
        assert "UNKNOWN_COLUMN" in gen.calls[1].instructions
        assert "SELECT BadCol FROM Students;" not in gen.calls[1].instructions


def test_two_failed_attempts_terminate_with_correct_status(test_schema: DatabaseSchema) -> None:
    """Requirement 3: Two failed attempts terminate with correct non-success status."""
    gen = SequenceFakeSQLGenerator([
        SQLGenerationResponse(status=SQLGenerationStatus.ANSWERABLE, sql="SELECT ColA FROM Students;"),
        SQLGenerationResponse(status=SQLGenerationStatus.ANSWERABLE, sql="SELECT ColB FROM Students;"),
    ])
    val_side_effects = [
        ValidationResult(allowed=False, reasons=[ValidationReason(code=ValidationReasonCode.UNKNOWN_COLUMN, message="No ColA")]),
        ValidationResult(allowed=False, reasons=[ValidationReason(code=ValidationReasonCode.UNKNOWN_COLUMN, message="No ColB")]),
    ]
    with patch("query_pilot.graph.workflow.validate_sql", side_effect=val_side_effects) as mock_val, \
         patch("query_pilot.graph.workflow.execute_read_only") as mock_exec:
        workflow = QueryPilotWorkflow(generator=gen, schema=test_schema, engine=MagicMock())
        result = workflow.run("Show students")

        assert len(gen.calls) == 2
        assert mock_val.call_count == 2
        assert mock_exec.call_count == 0
        assert result["status"] == PipelineStatus.VALIDATION_REJECTED
        assert result.get("generation_attempts") == 2
        assert result.get("retry_count") == 1
        assert result["presentation_result"].status == "validation_rejected"


def test_maximum_attempt_count_cannot_be_exceeded(test_schema: DatabaseSchema) -> None:
    """Requirement 4: Maximum attempt count cannot be exceeded even if caller requests more."""
    gen = SequenceFakeSQLGenerator([
        SQLGenerationResponse(status=SQLGenerationStatus.ANSWERABLE, sql="SELECT Col1 FROM Students;"),
        SQLGenerationResponse(status=SQLGenerationStatus.ANSWERABLE, sql="SELECT Col2 FROM Students;"),
        SQLGenerationResponse(status=SQLGenerationStatus.ANSWERABLE, sql="SELECT Col3 FROM Students;"),
    ])
    with patch("query_pilot.graph.workflow.validate_sql", return_value=ValidationResult(allowed=False, reasons=[ValidationReason(code=ValidationReasonCode.UNKNOWN_COLUMN, message="Bad col")])):
        workflow = QueryPilotWorkflow(generator=gen, schema=test_schema, engine=MagicMock())
        initial_state = create_initial_state("Show students", max_retries=10)
        result = workflow.run(initial_state)

        assert len(gen.calls) == 2
        assert result.get("generation_attempts") == 2


def test_mutating_sql_rejected_without_execution_or_recovery(test_schema: DatabaseSchema) -> None:
    """Requirement 5: Mutating SQL is rejected without execution and without recovery."""
    gen = SequenceFakeSQLGenerator([
        SQLGenerationResponse(status=SQLGenerationStatus.ANSWERABLE, sql="DELETE FROM Students WHERE Id = 1;"),
        SQLGenerationResponse(status=SQLGenerationStatus.ANSWERABLE, sql="SELECT Id FROM Students;"),
    ])
    val_reject = ValidationResult(
        allowed=False,
        reasons=[ValidationReason(code=ValidationReasonCode.NOT_READ_ONLY, message="Query contains mutation")],
    )
    with patch("query_pilot.graph.workflow.validate_sql", return_value=val_reject) as mock_val, \
         patch("query_pilot.graph.workflow.execute_read_only") as mock_exec:
        workflow = QueryPilotWorkflow(generator=gen, schema=test_schema, engine=MagicMock())
        result = workflow.run("Delete student")

        assert len(gen.calls) == 1
        mock_val.assert_called_once()
        mock_exec.assert_not_called()
        assert result["status"] == PipelineStatus.VALIDATION_REJECTED
        assert result.get("generation_attempts") == 1
        assert result.get("retry_count") == 0
        assert result.get("recovery_feedback") is None


def test_multiple_statements_rejected_without_execution_or_recovery(test_schema: DatabaseSchema) -> None:
    """Requirement 6: Multiple statements rejected without execution and without recovery."""
    gen = SequenceFakeSQLGenerator([
        SQLGenerationResponse(status=SQLGenerationStatus.ANSWERABLE, sql="SELECT 1; SELECT 2;"),
        SQLGenerationResponse(status=SQLGenerationStatus.ANSWERABLE, sql="SELECT 1;"),
    ])
    val_reject = ValidationResult(
        allowed=False,
        reasons=[ValidationReason(code=ValidationReasonCode.MULTI_STATEMENT, message="Multiple statements prohibited")],
    )
    with patch("query_pilot.graph.workflow.validate_sql", return_value=val_reject) as mock_val, \
         patch("query_pilot.graph.workflow.execute_read_only") as mock_exec:
        workflow = QueryPilotWorkflow(generator=gen, schema=test_schema, engine=MagicMock())
        result = workflow.run("Run multiple")

        assert len(gen.calls) == 1
        mock_val.assert_called_once()
        mock_exec.assert_not_called()
        assert result["status"] == PipelineStatus.VALIDATION_REJECTED
        assert result.get("generation_attempts") == 1
        assert result.get("recovery_feedback") is None


def test_sensitive_forbidden_access_is_terminal(test_schema: DatabaseSchema) -> None:
    """Requirement 7: Sensitive/forbidden access is terminal."""
    gen = SequenceFakeSQLGenerator([
        SQLGenerationResponse(status=SQLGenerationStatus.ANSWERABLE, sql="SELECT PasswordHash FROM Users;"),
        SQLGenerationResponse(status=SQLGenerationStatus.ANSWERABLE, sql="SELECT Email FROM Users;"),
    ])
    val_reject = ValidationResult(
        allowed=False,
        reasons=[ValidationReason(code=ValidationReasonCode.SENSITIVE_COLUMN, message="Access to PasswordHash denied")],
    )
    with patch("query_pilot.graph.workflow.validate_sql", return_value=val_reject) as mock_val, \
         patch("query_pilot.graph.workflow.execute_read_only") as mock_exec:
        workflow = QueryPilotWorkflow(generator=gen, schema=test_schema, engine=MagicMock())
        result = workflow.run("Show passwords")

        assert len(gen.calls) == 1
        mock_val.assert_called_once()
        mock_exec.assert_not_called()
        assert result["status"] == PipelineStatus.VALIDATION_REJECTED
        assert result.get("generation_attempts") == 1
        assert result.get("recovery_feedback") is None


def test_timeout_is_terminal_and_not_retried(test_schema: DatabaseSchema) -> None:
    """Requirement 8: Timeout is terminal and is not retried."""
    gen = SequenceFakeSQLGenerator([
        SQLGenerationResponse(status=SQLGenerationStatus.ANSWERABLE, sql="SELECT * FROM HugeTable;"),
        SQLGenerationResponse(status=SQLGenerationStatus.ANSWERABLE, sql="SELECT TOP 1 * FROM HugeTable;"),
    ])
    with patch("query_pilot.graph.workflow.validate_sql", return_value=ValidationResult(allowed=True)) as mock_val, \
         patch("query_pilot.graph.workflow.execute_read_only", side_effect=QueryTimeoutError("Timed out after 30s")) as mock_exec:
        workflow = QueryPilotWorkflow(generator=gen, schema=test_schema, engine=MagicMock())
        result = workflow.run("Slow query")

        assert len(gen.calls) == 1
        assert mock_val.call_count == 1
        assert mock_exec.call_count == 1
        assert result["status"] == PipelineStatus.EXECUTION_FAILED
        assert result.get("error_category") == ErrorCategory.QUERY_TIMEOUT
        assert result.get("generation_attempts") == 1
        assert result.get("recovery_feedback") is None


def test_connection_failure_is_terminal_and_not_retried(test_schema: DatabaseSchema) -> None:
    """Requirement 9: Connection/operational failure is terminal and is not retried."""
    op_err = OperationalError("conn failed", None, Exception("Connection reset"))
    exec_err = QueryExecutionError("Database connection failed")
    exec_err.__cause__ = op_err

    gen = SequenceFakeSQLGenerator([
        SQLGenerationResponse(status=SQLGenerationStatus.ANSWERABLE, sql="SELECT Id FROM Users;"),
        SQLGenerationResponse(status=SQLGenerationStatus.ANSWERABLE, sql="SELECT Id FROM Users;"),
    ])
    with patch("query_pilot.graph.workflow.validate_sql", return_value=ValidationResult(allowed=True)) as mock_val, \
         patch("query_pilot.graph.workflow.execute_read_only", side_effect=exec_err) as mock_exec:
        workflow = QueryPilotWorkflow(generator=gen, schema=test_schema, engine=MagicMock())
        result = workflow.run("Query during network drop")

        assert len(gen.calls) == 1
        assert mock_val.call_count == 1
        assert mock_exec.call_count == 1
        assert result["status"] == PipelineStatus.EXECUTION_FAILED
        assert result.get("generation_attempts") == 1
        assert result.get("recovery_feedback") is None


def test_database_syntax_error_recovers_and_ambiguous_terminates(test_schema: DatabaseSchema) -> None:
    """Requirement 10: Safely classified DB syntax error recovers; ambiguous error terminates."""
    # 10a. Safely classified: ProgrammingError causes recovery
    prog_err = ProgrammingError("SELECT Foo", None, Exception("Invalid column Foo"))
    exec_err = QueryExecutionError("Execution failed")
    exec_err.__cause__ = prog_err

    gen = SequenceFakeSQLGenerator([
        SQLGenerationResponse(status=SQLGenerationStatus.ANSWERABLE, sql="SELECT Foo FROM Users;"),
        SQLGenerationResponse(status=SQLGenerationStatus.ANSWERABLE, sql="SELECT Id FROM Users;"),
    ])
    exec_result = QueryResult(columns=["Id"], rows=[{"Id": 1}], row_count=1, truncated=False)

    with patch("query_pilot.graph.workflow.validate_sql", return_value=ValidationResult(allowed=True)), \
         patch("query_pilot.graph.workflow.execute_read_only", side_effect=[exec_err, exec_result]) as mock_exec:
        workflow = QueryPilotWorkflow(generator=gen, schema=test_schema, engine=MagicMock())
        result = workflow.run("Query with DB syntax error")

        assert len(gen.calls) == 2
        assert mock_exec.call_count == 2
        assert result["status"] == PipelineStatus.SUCCESS
        assert result.get("generation_attempts") == 2
        assert gen.calls[1].instructions is not None
        assert "DATABASE_EXECUTION_ERROR" in gen.calls[1].instructions

    # 10b. Ambiguous error terminates immediately without retry
    ambig_err = QueryExecutionError("Unclassified generic DB failure")
    gen_ambig = SequenceFakeSQLGenerator([
        SQLGenerationResponse(status=SQLGenerationStatus.ANSWERABLE, sql="SELECT 1;"),
        SQLGenerationResponse(status=SQLGenerationStatus.ANSWERABLE, sql="SELECT 2;"),
    ])
    with patch("query_pilot.graph.workflow.validate_sql", return_value=ValidationResult(allowed=True)), \
         patch("query_pilot.graph.workflow.execute_read_only", side_effect=ambig_err) as mock_exec_ambig:
        workflow2 = QueryPilotWorkflow(generator=gen_ambig, schema=test_schema, engine=MagicMock())
        result2 = workflow2.run("Query with ambiguous failure")

        assert len(gen_ambig.calls) == 1
        assert mock_exec_ambig.call_count == 1
        assert result2["status"] == PipelineStatus.EXECUTION_FAILED
        assert result2.get("generation_attempts") == 1
        assert result2.get("recovery_feedback") is None


def test_malformed_model_output_recovers_only_when_explicitly_classified(test_schema: DatabaseSchema) -> None:
    """Requirement 11: Malformed model output recovers only when error is explicitly classified."""
    # 11a. MalformedResponseError recovers
    gen = SequenceFakeSQLGenerator([
        MalformedResponseError("Invalid JSON from LLM"),
        SQLGenerationResponse(status=SQLGenerationStatus.ANSWERABLE, sql="SELECT Id FROM Users;"),
    ])
    exec_result = QueryResult(columns=["Id"], rows=[{"Id": 1}], row_count=1, truncated=False)
    with patch("query_pilot.graph.workflow.validate_sql", return_value=ValidationResult(allowed=True)) as mock_val, \
         patch("query_pilot.graph.workflow.execute_read_only", return_value=exec_result) as mock_exec:
        workflow = QueryPilotWorkflow(generator=gen, schema=test_schema, engine=MagicMock())
        result = workflow.run("Show users")

        assert len(gen.calls) == 2
        assert mock_val.call_count == 1
        assert mock_exec.call_count == 1
        assert result["status"] == PipelineStatus.SUCCESS
        assert result.get("generation_attempts") == 2
        assert gen.calls[1].instructions is not None
        assert "MALFORMED_OUTPUT" in gen.calls[1].instructions

    # 11b. EmptySQLError recovers
    gen_empty = SequenceFakeSQLGenerator([
        EmptySQLError("Model returned empty SQL string"),
        SQLGenerationResponse(status=SQLGenerationStatus.ANSWERABLE, sql="SELECT Id FROM Users;"),
    ])
    with patch("query_pilot.graph.workflow.validate_sql", return_value=ValidationResult(allowed=True)), \
         patch("query_pilot.graph.workflow.execute_read_only", return_value=exec_result):
        workflow_empty = QueryPilotWorkflow(generator=gen_empty, schema=test_schema, engine=MagicMock())
        result_empty = workflow_empty.run("Show users")

        assert len(gen_empty.calls) == 2
        assert result_empty["status"] == PipelineStatus.SUCCESS
        assert result_empty.get("generation_attempts") == 2
        assert "EMPTY_SQL" in gen_empty.calls[1].instructions


def test_provider_configuration_failure_terminates_without_retry(test_schema: DatabaseSchema) -> None:
    """Requirement 12: Provider/configuration failure terminates without retry."""
    # 12a. ConfigurationError
    gen_cfg = SequenceFakeSQLGenerator([
        ConfigurationError("GEMINI_API_KEY is not configured"),
        SQLGenerationResponse(status=SQLGenerationStatus.ANSWERABLE, sql="SELECT 1;"),
    ])
    workflow_cfg = QueryPilotWorkflow(generator=gen_cfg, schema=test_schema, engine=MagicMock())
    result_cfg = workflow_cfg.run("Show users")
    assert len(gen_cfg.calls) == 1
    assert result_cfg["status"] == PipelineStatus.GENERATION_FAILED
    assert result_cfg.get("generation_attempts") == 1
    assert result_cfg.get("recovery_feedback") is None

    # 12b. ProviderAPIError
    gen_prov = SequenceFakeSQLGenerator([
        ProviderAPIError("503 Service Unavailable"),
        SQLGenerationResponse(status=SQLGenerationStatus.ANSWERABLE, sql="SELECT 1;"),
    ])
    workflow_prov = QueryPilotWorkflow(generator=gen_prov, schema=test_schema, engine=MagicMock())
    result_prov = workflow_prov.run("Show users")
    assert len(gen_prov.calls) == 1
    assert result_prov["status"] == PipelineStatus.GENERATION_FAILED
    assert result_prov.get("generation_attempts") == 1
    assert result_prov.get("recovery_feedback") is None


def test_unsupported_questions_terminate_without_retry(test_schema: DatabaseSchema) -> None:
    """Requirement 13: Unsupported questions terminate without retry."""
    gen = SequenceFakeSQLGenerator([
        SQLGenerationResponse(status=SQLGenerationStatus.UNSUPPORTED, sql=None, explanation="Not in database"),
        SQLGenerationResponse(status=SQLGenerationStatus.ANSWERABLE, sql="SELECT 1;"),
    ])
    with patch("query_pilot.graph.workflow.validate_sql") as mock_val, \
         patch("query_pilot.graph.workflow.execute_read_only") as mock_exec:
        workflow = QueryPilotWorkflow(generator=gen, schema=test_schema, engine=MagicMock())
        result = workflow.run("What is the cafeteria menu?")

        assert len(gen.calls) == 1
        mock_val.assert_not_called()
        mock_exec.assert_not_called()
        assert result["status"] == PipelineStatus.UNSUPPORTED
        assert result.get("generation_attempts") == 1
        assert result.get("recovery_feedback") is None


def test_empty_results_return_success_without_retry(test_schema: DatabaseSchema) -> None:
    """Requirement 14: Empty results return success without retry."""
    fake_generator = FakeSQLGenerator(
        response=SQLGenerationResponse(
            status=SQLGenerationStatus.ANSWERABLE,
            sql="SELECT Id FROM Students WHERE DepartmentId = 999;",
        )
    )
    empty_result = QueryResult(columns=["Id"], rows=[], row_count=0, truncated=False)
    with patch("query_pilot.graph.workflow.validate_sql", return_value=ValidationResult(allowed=True)) as mock_val, \
         patch("query_pilot.graph.workflow.execute_read_only", return_value=empty_result) as mock_exec:
        workflow = QueryPilotWorkflow(generator=fake_generator, schema=test_schema, engine=MagicMock())
        result = workflow.run("Show non-existent students")

        assert len(fake_generator.calls) == 1
        mock_val.assert_called_once()
        mock_exec.assert_called_once()
        assert result["status"] == PipelineStatus.SUCCESS
        assert result.get("generation_attempts") == 1
        assert result["presentation_result"].status == "success"
        assert result["presentation_result"].row_count == 0
        assert "No rows returned" in result["presentation_result"].message
        assert result.get("recovery_feedback") is None


def test_recovery_feedback_is_sanitized_and_contains_no_previous_sql(test_schema: DatabaseSchema) -> None:
    """Requirement 15: Recovery feedback is sanitized and contains no previous SQL by default."""
    sensitive_sql = "SELECT SecretKey, PasswordHash FROM SuperSecretUsers WHERE Token = 'xyz123';"
    gen = SequenceFakeSQLGenerator([
        SQLGenerationResponse(status=SQLGenerationStatus.ANSWERABLE, sql=sensitive_sql),
        SQLGenerationResponse(status=SQLGenerationStatus.ANSWERABLE, sql="SELECT Id FROM Users;"),
    ])
    val_reject = ValidationResult(
        allowed=False,
        reasons=[ValidationReason(code=ValidationReasonCode.UNKNOWN_TABLE, message="Table 'SuperSecretUsers' does not exist")],
    )
    with patch("query_pilot.graph.workflow.validate_sql", side_effect=[val_reject, ValidationResult(allowed=True)]), \
         patch("query_pilot.graph.workflow.execute_read_only", return_value=QueryResult(columns=["Id"], rows=[{"Id": 1}], row_count=1, truncated=False)):
        workflow = QueryPilotWorkflow(generator=gen, schema=test_schema, engine=MagicMock())
        workflow.run("Show secrets")

        assert len(gen.calls) == 2
        instructions = gen.calls[1].instructions
        assert instructions is not None
        # Must not contain previous SQL literals or statement
        assert sensitive_sql not in instructions
        assert "xyz123" not in instructions
        assert "SecretKey" not in instructions
        assert "PasswordHash" not in instructions
        assert "UNKNOWN_TABLE" in instructions


def test_second_candidate_must_be_validated_before_execution(test_schema: DatabaseSchema) -> None:
    """Requirement 16: A second candidate must be validated before execution."""
    gen = SequenceFakeSQLGenerator([
        SQLGenerationResponse(status=SQLGenerationStatus.ANSWERABLE, sql="SELECT UnknownCol FROM Students;"),
        SQLGenerationResponse(status=SQLGenerationStatus.ANSWERABLE, sql="DROP TABLE Students;"),
    ])
    val_side_effects = [
        ValidationResult(allowed=False, reasons=[ValidationReason(code=ValidationReasonCode.UNKNOWN_COLUMN, message="Unknown column")]),
        ValidationResult(allowed=False, reasons=[ValidationReason(code=ValidationReasonCode.FORBIDDEN_OPERATION, message="DROP statement forbidden")]),
    ]
    with patch("query_pilot.graph.workflow.validate_sql", side_effect=val_side_effects) as mock_val, \
         patch("query_pilot.graph.workflow.execute_read_only") as mock_exec:
        workflow = QueryPilotWorkflow(generator=gen, schema=test_schema, engine=MagicMock())
        result = workflow.run("Malicious attempt 2")

        assert len(gen.calls) == 2
        assert mock_val.call_count == 2
        # Execution MUST NOT be invoked for second unvalidated/rejected candidate
        mock_exec.assert_not_called()
        assert result["status"] == PipelineStatus.VALIDATION_REJECTED
        assert result["validation_result"].allowed is False


def test_recovery_exhaustion_preserves_established_terminal_contract(test_schema: DatabaseSchema) -> None:
    """Requirement 17: Recovery exhaustion preserves the established terminal result contract."""
    gen = SequenceFakeSQLGenerator([
        MalformedResponseError("Attempt 1 parse failed"),
        MalformedResponseError("Attempt 2 parse failed"),
    ])
    workflow = QueryPilotWorkflow(generator=gen, schema=test_schema, engine=MagicMock())
    result = workflow.run("Exhaustion test")

    assert len(gen.calls) == 2
    assert result["status"] == PipelineStatus.GENERATION_FAILED
    assert result.get("error_category") == ErrorCategory.SQL_GENERATION_FAILED
    assert result.get("generation_attempts") == 2
    assert result.get("retry_count") == 1
    assert result["presentation_result"].status == "generation_failed"
    assert "Attempt 2 parse failed" in result["presentation_result"].message


def test_two_invocations_do_not_share_retry_state(test_schema: DatabaseSchema) -> None:
    """Requirement 18: Two invocations using the same workflow do not share retry state or feedback."""
    gen = SequenceFakeSQLGenerator([
        # Run 1: attempt 1 recoverable error, attempt 2 success
        SQLGenerationResponse(status=SQLGenerationStatus.ANSWERABLE, sql="SELECT BadCol FROM Students;"),
        SQLGenerationResponse(status=SQLGenerationStatus.ANSWERABLE, sql="SELECT Id FROM Students;"),
        # Run 2: attempt 1 immediate success
        SQLGenerationResponse(status=SQLGenerationStatus.ANSWERABLE, sql="SELECT Id FROM Users;"),
    ])
    val_side_effects = [
        # Run 1 val 1 & val 2
        ValidationResult(allowed=False, reasons=[ValidationReason(code=ValidationReasonCode.UNKNOWN_COLUMN, message="Unknown column")]),
        ValidationResult(allowed=True, normalized_sql="SELECT Id FROM Students;"),
        # Run 2 val 1
        ValidationResult(allowed=True, normalized_sql="SELECT Id FROM Users;"),
    ]
    exec_result = QueryResult(columns=["Id"], rows=[{"Id": 1}], row_count=1, truncated=False)

    with patch("query_pilot.graph.workflow.validate_sql", side_effect=val_side_effects), \
         patch("query_pilot.graph.workflow.execute_read_only", return_value=exec_result):
        workflow = QueryPilotWorkflow(generator=gen, schema=test_schema, engine=MagicMock())

        # Invocation 1: required 2 attempts
        res1 = workflow.run("Question 1")
        assert res1["status"] == PipelineStatus.SUCCESS
        assert res1.get("generation_attempts") == 2
        assert res1.get("retry_count") == 1

        # Invocation 2: starts fresh
        res2 = workflow.run("Question 2")
        assert res2["status"] == PipelineStatus.SUCCESS
        assert res2.get("generation_attempts") == 1
        assert res2.get("retry_count") == 0
        assert res2.get("recovery_feedback") is None
        # Call 3 to generator had NO recovery instructions
        assert gen.calls[2].instructions is None


def test_query_runner_still_defaults_to_sequential_execution(test_schema: DatabaseSchema) -> None:
    """Requirement 19: QueryRunner still defaults to sequential execution."""
    runner = QueryRunner(schema=test_schema, engine=MagicMock())
    assert runner.default_mode == ExecutionMode.SEQUENTIAL

    fake_generator = FakeSQLGenerator(
        response=SQLGenerationResponse(
            status=SQLGenerationStatus.ANSWERABLE,
            sql="SELECT Id FROM Users;",
        )
    )
    runner._generator = fake_generator
    with patch("query_pilot.pipeline.validate_sql", return_value=ValidationResult(allowed=True)), \
         patch("query_pilot.pipeline.execute_read_only", return_value=QueryResult(columns=["Id"], rows=[{"Id": 1}], row_count=1, truncated=False)):
        res = runner.run("Show users")
        assert isinstance(res, PipelineResult)
        assert res.status == PipelineStatus.SUCCESS
        assert res.is_success is True


def test_sequential_pipeline_remains_single_pass_and_unchanged(test_schema: DatabaseSchema) -> None:
    """Requirement 20: The sequential pipeline remains single-pass and unchanged."""
    gen = SequenceFakeSQLGenerator([
        SQLGenerationResponse(status=SQLGenerationStatus.ANSWERABLE, sql="SELECT BadCol FROM Students;"),
        SQLGenerationResponse(status=SQLGenerationStatus.ANSWERABLE, sql="SELECT Id FROM Students;"),
    ])
    val_reject = ValidationResult(
        allowed=False,
        reasons=[ValidationReason(code=ValidationReasonCode.UNKNOWN_COLUMN, message="Unknown column")],
    )
    with patch("query_pilot.pipeline.validate_sql", return_value=val_reject) as mock_val, \
         patch("query_pilot.pipeline.execute_read_only") as mock_exec:
        pipeline = QueryPipeline(generator=gen, schema=test_schema, engine=MagicMock())
        res = pipeline.run("Show students")

        # In sequential pipeline, exactly ONE attempt is made and no retry loop exists
        assert len(gen.calls) == 1
        mock_val.assert_called_once()
        mock_exec.assert_not_called()
        assert res.status == PipelineStatus.VALIDATION_REJECTED
        assert res.is_success is False


# ==============================================================================
# Clarification Routing Tests (Task 1.29-B3)
# ==============================================================================

def test_graph_workflow_clarification_routes_directly_to_presentation(test_schema: DatabaseSchema) -> None:
    """Verify that CLARIFICATION_NEEDED routes directly to present_result and bypasses validation and execution."""
    options = [
        ClarificationOption(
            option_id="opt_ug",
            description="Undergraduate courses only",
            suggested_question="Show undergraduate courses",
        ),
        ClarificationOption(
            option_id="opt_grad",
            description="Graduate courses only",
            suggested_question="Show graduate courses",
        ),
    ]
    clarification_resp = SQLGenerationResponse(
        status=SQLGenerationStatus.CLARIFICATION_NEEDED,
        clarification_prompt="Do you want undergraduate or graduate courses?",
        clarification_options=options,
        explanation="The course level is not specified.",
    )
    fake_generator = FakeSQLGenerator(response=clarification_resp)
    workflow = QueryPilotWorkflow(generator=fake_generator, schema=test_schema, engine=MagicMock())

    with patch("query_pilot.graph.workflow.validate_sql") as mock_val, \
         patch("query_pilot.graph.workflow.execute_read_only") as mock_exec:
        final_state = workflow.run("Show all courses")

        # 1. State status is CLARIFICATION_NEEDED
        assert final_state["status"] == PipelineStatus.CLARIFICATION_NEEDED
        assert final_state.get("error_category") is None
        assert final_state.get("error_message") is None

        # 2. Validation and execution nodes were completely bypassed
        mock_val.assert_not_called()
        mock_exec.assert_not_called()
        assert final_state.get("validation_result") is None
        assert final_state.get("execution_result") is None

        # 3. No recovery feedback or extra retry attempts
        assert final_state.get("recovery_feedback") is None
        assert final_state.get("generation_attempts") == 1
        assert final_state.get("retry_count") == 0
        assert len(fake_generator.calls) == 1

        # 4. Presentation result is present and formatted cleanly
        pres = final_state["presentation_result"]
        assert isinstance(pres, PresentationResult)
        assert pres.status == "clarification_needed"
        assert pres.clarification_prompt == "Do you want undergraduate or graduate courses?"
        assert len(pres.clarification_options) == 2
        assert pres.generated_sql is None
        assert pres.rows == []
        assert pres.row_count == 0
        assert "Do you want undergraduate or graduate courses?" in pres.message
        assert "Undergraduate courses only" in pres.message


def test_graph_workflow_clarification_explanation_only_fallback(test_schema: DatabaseSchema) -> None:
    """Verify that CLARIFICATION_NEEDED with explanation-only falls back to explanation without inventing a question."""
    clarification_resp = SQLGenerationResponse(
        status=SQLGenerationStatus.CLARIFICATION_NEEDED,
        clarification_prompt=None,
        clarification_options=[],
        explanation="Please specify whether you mean spring or fall semester.",
    )
    fake_generator = FakeSQLGenerator(response=clarification_resp)
    workflow = QueryPilotWorkflow(generator=fake_generator, schema=test_schema, engine=MagicMock())

    with patch("query_pilot.graph.workflow.validate_sql") as mock_val, \
         patch("query_pilot.graph.workflow.execute_read_only") as mock_exec:
        final_state = workflow.run("Show enrollment")

        assert final_state["status"] == PipelineStatus.CLARIFICATION_NEEDED
        mock_val.assert_not_called()
        mock_exec.assert_not_called()

        pres = final_state["presentation_result"]
        assert pres.status == "clarification_needed"
        assert pres.clarification_prompt is None
        assert pres.generated_sql is None
        assert "Please specify whether you mean spring or fall semester." in pres.message


def test_route_after_generation_clarification_returns_present_result() -> None:
    """Verify route_after_generation returns present_result directly for CLARIFICATION_NEEDED."""
    workflow = QueryPilotWorkflow()
    route = workflow.route_after_generation({"status": PipelineStatus.CLARIFICATION_NEEDED})
    assert route == "present_result"


def test_runner_graph_mode_clarification(test_schema: DatabaseSchema) -> None:
    """Verify QueryRunner in graph mode correctly converts clarification state to PipelineResult."""
    clarification_resp = SQLGenerationResponse(
        status=SQLGenerationStatus.CLARIFICATION_NEEDED,
        clarification_prompt="Which semester?",
        clarification_options=[ClarificationOption(option_id="1", description="Spring 2024")],
        explanation="Semester not specified.",
    )
    runner = QueryRunner(
        generator=FakeSQLGenerator(response=clarification_resp),
        schema=test_schema,
        engine=MagicMock(),
        mode=ExecutionMode.GRAPH,
    )

    with patch("query_pilot.graph.workflow.validate_sql") as mock_val, \
         patch("query_pilot.graph.workflow.execute_read_only") as mock_exec:
        res = runner.run("Show schedule")

        assert res.status == PipelineStatus.CLARIFICATION_NEEDED
        assert res.is_clarification_needed is True
        assert res.generated_sql is None
        assert res.validation_result is None
        assert res.execution_result is None
        assert res.clarification_prompt == "Which semester?"
        assert len(res.clarification_options) == 1
        assert res.clarification_options[0].option_id == "1"

        mock_val.assert_not_called()
        mock_exec.assert_not_called()


def test_graph_workflow_context_propagation_to_generator(test_schema: DatabaseSchema) -> None:
    """Verify conversation_context reaches generator in LangGraph workflow."""
    context = [
        ConversationTurnContext(user_question="Show users"),
    ]
    fake_generator = FakeSQLGenerator(
        response=SQLGenerationResponse(
            status=SQLGenerationStatus.ANSWERABLE,
            sql="SELECT Id FROM Users;",
        )
    )
    workflow = QueryPilotWorkflow(generator=fake_generator, schema=test_schema, engine=MagicMock())

    with patch("query_pilot.graph.workflow.validate_sql", return_value=ValidationResult(allowed=True)), \
         patch("query_pilot.graph.workflow.execute_read_only", return_value=QueryResult(columns=["Id"], rows=[{"Id": 1}], row_count=1, truncated=False)):
        workflow.run("Active only", conversation_context=context)

        assert len(fake_generator.calls) == 1
        assert fake_generator.calls[0].conversation_context == context


def test_runner_context_propagation_both_modes(test_schema: DatabaseSchema) -> None:
    """Verify QueryRunner propagates conversation_context in both sequential and graph modes."""
    context = [ConversationTurnContext(user_question="Show all students")]

    # 1. Sequential mode
    gen_seq = FakeSQLGenerator(response=SQLGenerationResponse(status=SQLGenerationStatus.ANSWERABLE, sql="SELECT 1"))
    runner_seq = QueryRunner(generator=gen_seq, schema=test_schema, engine=MagicMock(), mode=ExecutionMode.SEQUENTIAL)
    with patch("query_pilot.pipeline.validate_sql", return_value=ValidationResult(allowed=True)), \
         patch("query_pilot.pipeline.execute_read_only", return_value=QueryResult(columns=["1"], rows=[{"1": 1}], row_count=1, truncated=False)):
        runner_seq.run("CS only", conversation_context=context)
        assert len(gen_seq.calls) == 1
        assert gen_seq.calls[0].conversation_context == context

    # 2. Graph mode
    gen_graph = FakeSQLGenerator(response=SQLGenerationResponse(status=SQLGenerationStatus.ANSWERABLE, sql="SELECT 1"))
    runner_graph = QueryRunner(generator=gen_graph, schema=test_schema, engine=MagicMock(), mode=ExecutionMode.GRAPH)
    with patch("query_pilot.graph.workflow.validate_sql", return_value=ValidationResult(allowed=True)), \
         patch("query_pilot.graph.workflow.execute_read_only", return_value=QueryResult(columns=["1"], rows=[{"1": 1}], row_count=1, truncated=False)):
        runner_graph.run("CS only", conversation_context=context)
        assert len(gen_graph.calls) == 1
        assert gen_graph.calls[0].conversation_context == context


def test_graph_clarification_with_prior_recovery_feedback_still_bypasses_retry(test_schema: DatabaseSchema) -> None:
    """Verify that route_after_generation never triggers recovery retry on CLARIFICATION_NEEDED even if feedback exists."""
    workflow = QueryPilotWorkflow()
    # Simulated state containing residual recovery feedback
    state = {
        "status": PipelineStatus.CLARIFICATION_NEEDED,
        "recovery_feedback": "PARSE_ERROR: syntax error",
        "generation_attempts": 1,
    }
    route = workflow.route_after_generation(state)
    assert route == "present_result"


def test_workflow_isolation_gemini_retry_does_not_repeat_schema_or_execution(test_schema: DatabaseSchema) -> None:
    """Verify transient SDK retry during SQL generation does not repeat schema acquisition or DB execution."""
    import json
    import httpx
    from google.genai import errors
    from query_pilot.sql import GeminiSQLGenerator, get_gemini_http_options

    class _WorkflowFakeSdkHttpResponse:
        def __init__(self, json_payload: str):
            self.headers = {}
            self.response_stream = [
                json.dumps({"candidates": [{"content": {"parts": [{"text": json_payload}]}}]})
            ]

    fast_opts = get_gemini_http_options(initial_delay=0.001, max_delay=0.01)
    generator = GeminiSQLGenerator(api_key="test-key", http_options=fast_opts)

    sdk_call_count = 0
    fake_httpx_resp = httpx.Response(status_code=503, request=httpx.Request("POST", "https://example.com"))
    valid_payload = json.dumps({
        "status": "answerable",
        "sql": "SELECT Id FROM Students;",
        "explanation": "Selects student IDs",
        "assumptions": [],
    })

    def mock_request_once(request, stream=False):
        nonlocal sdk_call_count
        sdk_call_count += 1
        if sdk_call_count == 1:
            raise errors.ServerError(503, {"error": {"message": "Service Unavailable"}}, fake_httpx_resp)
        return _WorkflowFakeSdkHttpResponse(valid_payload)

    generator.client._api_client._request_once = mock_request_once

    mock_introspector = MagicMock()
    mock_introspector.introspect_model_facing.return_value = test_schema
    mock_engine = MagicMock()

    workflow = QueryPilotWorkflow(
        generator=generator,
        schema=None,
        engine=mock_engine,
        introspector=mock_introspector,
    )

    with patch("query_pilot.graph.workflow.execute_read_only", return_value=QueryResult(columns=["Id"], rows=[{"Id": 1}], row_count=1, truncated=False)) as mock_exec:
        result = workflow.run("Show students")

        # Generator recovered via internal SDK retry (2 SDK transport calls)
        assert sdk_call_count == 2
        # Schema acquisition was performed exactly once
        mock_introspector.introspect_model_facing.assert_called_once()
        # Database execution was performed exactly once
        mock_exec.assert_called_once()
        # Workflow succeeded without workflow-level recovery loop
        assert result["status"] == PipelineStatus.SUCCESS
        assert result["generation_response"] is not None
        assert result["generation_response"].sql == "SELECT Id FROM Students;"
        assert result["execution_result"] is not None
        assert result["execution_result"].row_count == 1


def test_workflow_isolation_gemini_retry_exhaustion_does_not_execute_db(test_schema: DatabaseSchema) -> None:
    """Verify exhausted Gemini retries fail at generation without executing queries on the database."""
    import httpx
    from google.genai import errors
    from query_pilot.sql import GeminiSQLGenerator, get_gemini_http_options

    fast_opts = get_gemini_http_options(initial_delay=0.001, max_delay=0.01)
    generator = GeminiSQLGenerator(api_key="test-key", http_options=fast_opts)

    sdk_call_count = 0
    fake_httpx_resp = httpx.Response(status_code=503, request=httpx.Request("POST", "https://example.com"))

    def mock_request_once(request, stream=False):
        nonlocal sdk_call_count
        sdk_call_count += 1
        raise errors.ServerError(503, {"error": {"message": "Service Unavailable"}}, fake_httpx_resp)

    generator.client._api_client._request_once = mock_request_once

    mock_introspector = MagicMock()
    mock_introspector.introspect_model_facing.return_value = test_schema
    mock_engine = MagicMock()

    workflow = QueryPilotWorkflow(
        generator=generator,
        schema=None,
        engine=mock_engine,
        introspector=mock_introspector,
    )

    with patch("query_pilot.graph.workflow.execute_read_only") as mock_exec:
        result = workflow.run("Show students")

        # Generator exhausted retries (3 attempts total)
        assert sdk_call_count == 3
        # Schema acquisition ran once before generation
        mock_introspector.introspect_model_facing.assert_called_once()
        # Database execution was NEVER called
        mock_exec.assert_not_called()
        # Status correctly maps to GENERATION_FAILED
        assert result["status"] == PipelineStatus.GENERATION_FAILED
