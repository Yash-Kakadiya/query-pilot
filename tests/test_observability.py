"""Focused unit tests for structured pipeline observability (Task 1.16).

Verifies lifecycle event sequencing, error category sanitization, monotonic timing,
security redaction (no SQL, question text, row contents, or credentials in logs),
and failure isolation.
"""

import json
import logging
from unittest.mock import MagicMock, patch
import pytest

from query_pilot.db.executor import QueryExecutionError, QueryResult, QueryTimeoutError
from query_pilot.db.models import ColumnMetadata, DatabaseSchema, TableMetadata
from query_pilot.observability import (
    ErrorCategory,
    ObservabilityEvent,
    PipelineEvent,
    PipelineObserver,
    PipelineStage,
    StageStatus,
    classify_error,
)
from query_pilot.pipeline import PipelineResult, PipelineStatus, QueryPipeline, run_question
from query_pilot.presentation.formatter import PresentationResult
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
def obs_schema() -> DatabaseSchema:
    """Deterministic in-memory model-facing schema."""
    users_table = TableMetadata(
        schema_name="dbo",
        table_name="Users",
        columns=[
            ColumnMetadata(name="Id", data_type="int", nullable=False, primary_key=True),
            ColumnMetadata(name="FullName", data_type="varchar(255)", nullable=False),
        ],
        primary_keys=["Id"],
    )
    return DatabaseSchema(database_name="GradeSense_Local", tables=[users_table], is_model_facing=True)


class FakeGenerator(SQLGenerator):
    """Deterministic fake generator for observability tests."""

    def __init__(
        self,
        sql: str = "SELECT Id, FullName FROM Users;",
        status: SQLGenerationStatus = SQLGenerationStatus.ANSWERABLE,
        explanation: str = "Query users",
        raise_exc: Exception | None = None,
    ):
        self._sql = sql
        self._status = status
        self._explanation = explanation
        self._raise_exc = raise_exc

    def generate(self, request: SQLGenerationRequest) -> SQLGenerationResponse:
        if self._raise_exc:
            raise self._raise_exc
        return SQLGenerationResponse(
            sql=self._sql if self._status == SQLGenerationStatus.ANSWERABLE else None,
            status=self._status,
            explanation=self._explanation,
            assumptions=[],
        )


class LogCaptureHandler(logging.Handler):
    """Handler capturing structured JSON logs for test assertion."""

    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []
        self.events: list[dict] = []

    def emit(self, record: logging.LogRecord):
        self.records.append(record)
        try:
            msg = record.getMessage()
            self.events.append(json.loads(msg))
        except Exception:
            pass


@pytest.fixture
def log_capture() -> LogCaptureHandler:
    """Fixture providing an isolated log capture handler on the observability logger."""
    obs_logger = logging.getLogger("query_pilot.observability")
    handler = LogCaptureHandler()
    handler.setLevel(logging.DEBUG)
    obs_logger.addHandler(handler)
    obs_logger.setLevel(logging.DEBUG)
    yield handler
    obs_logger.removeHandler(handler)


# ==============================================================================
# 1. Unique Request IDs & Correlation
# ==============================================================================

def test_unique_request_ids_for_separate_invocations(obs_schema: DatabaseSchema):
    """Verify separate pipeline invocations receive distinct request IDs."""
    pipeline = QueryPipeline(generator=FakeGenerator(), schema=obs_schema)

    with patch("query_pilot.pipeline.execute_read_only") as mock_exec:
        mock_exec.return_value = QueryResult(
            columns=["Id", "FullName"],
            rows=[{"Id": 1, "FullName": "Alice"}],
            row_count=1,
            truncated=False,
        )

        res1 = pipeline.run("Question 1")
        res2 = pipeline.run("Question 2")

        assert res1.request_id is not None
        assert res2.request_id is not None
        assert res1.request_id != res2.request_id


def test_custom_request_id_preserved(obs_schema: DatabaseSchema):
    """Verify caller-provided request_id is preserved on result and presentation."""
    pipeline = QueryPipeline(generator=FakeGenerator(), schema=obs_schema)

    with patch("query_pilot.pipeline.execute_read_only") as mock_exec:
        mock_exec.return_value = QueryResult(
            columns=["Id", "FullName"],
            rows=[{"Id": 1, "FullName": "Alice"}],
            row_count=1,
            truncated=False,
        )

        custom_id = "req_custom_trace_9999"
        res = pipeline.run("Question with custom ID", request_id=custom_id)

        assert res.request_id == custom_id
        assert res.presentation is not None
        assert res.presentation.request_id == custom_id


# ==============================================================================
# 2. Lifecycle Events Sequence on Success
# ==============================================================================

def test_successful_request_lifecycle_events(obs_schema: DatabaseSchema, log_capture: LogCaptureHandler):
    """Verify the exact sequence of lifecycle events for a successful query."""
    pipeline = QueryPipeline(generator=FakeGenerator(), schema=obs_schema)

    with patch("query_pilot.pipeline.execute_read_only") as mock_exec:
        mock_exec.return_value = QueryResult(
            columns=["Id", "FullName"],
            rows=[{"Id": 1, "FullName": "Alice"}],
            row_count=1,
            truncated=False,
            execution_time_ms=3.5,
        )

        res = pipeline.run("Show all users")
        assert res.status == PipelineStatus.SUCCESS

    # Check event sequence
    event_names = [e["event"] for e in log_capture.events]
    expected_names = [
        ObservabilityEvent.PIPELINE_START.value,
        ObservabilityEvent.STAGE_START.value,  # schema_acquisition
        ObservabilityEvent.STAGE_END.value,
        ObservabilityEvent.STAGE_START.value,  # sql_generation
        ObservabilityEvent.STAGE_END.value,
        ObservabilityEvent.STAGE_START.value,  # sql_validation
        ObservabilityEvent.STAGE_END.value,
        ObservabilityEvent.STAGE_START.value,  # read_only_execution
        ObservabilityEvent.STAGE_END.value,
        ObservabilityEvent.STAGE_START.value,  # result_presentation
        ObservabilityEvent.STAGE_END.value,
        ObservabilityEvent.PIPELINE_END.value,
    ]
    assert event_names == expected_names

    # Check correlation: all events share the same request_id
    req_ids = {e["request_id"] for e in log_capture.events}
    assert len(req_ids) == 1
    assert res.request_id in req_ids

    # Check execution metadata in read_only_execution stage.end
    exec_end = [e for e in log_capture.events if e.get("stage") == PipelineStage.READ_ONLY_EXECUTION.value and e["event"] == ObservabilityEvent.STAGE_END.value][0]
    assert exec_end["status"] == StageStatus.SUCCEEDED.value
    assert exec_end["row_count"] == 1
    assert exec_end["truncated"] is False


# ==============================================================================
# 3. Unsupported Question Stops Without Validation or Execution
# ==============================================================================

def test_unsupported_question_lifecycle_events(obs_schema: DatabaseSchema, log_capture: LogCaptureHandler):
    """Verify unsupported question skips validation and execution events."""
    gen = FakeGenerator(status=SQLGenerationStatus.UNSUPPORTED, explanation="Salary data not in schema")
    pipeline = QueryPipeline(generator=gen, schema=obs_schema)

    with (
        patch("query_pilot.pipeline.validate_sql") as mock_val,
        patch("query_pilot.pipeline.execute_read_only") as mock_exec,
    ):
        res = pipeline.run("What are the employee salaries?")
        mock_val.assert_not_called()
        mock_exec.assert_not_called()

    assert res.status == PipelineStatus.UNSUPPORTED

    # Ensure validation and execution stages were NEVER emitted
    emitted_stages = [e["stage"] for e in log_capture.events]
    assert PipelineStage.SQL_VALIDATION.value not in emitted_stages
    assert PipelineStage.READ_ONLY_EXECUTION.value not in emitted_stages

    # Generation ended with 'unsupported' status
    gen_end = [e for e in log_capture.events if e.get("stage") == PipelineStage.SQL_GENERATION.value and e["event"] == ObservabilityEvent.STAGE_END.value][0]
    assert gen_end["status"] == StageStatus.UNSUPPORTED.value
    assert gen_end["error_code"] == ErrorCategory.UNSUPPORTED_QUESTION.value

    # Pipeline completion ended with 'unsupported'
    pipe_end = log_capture.events[-1]
    assert pipe_end["event"] == ObservabilityEvent.PIPELINE_END.value
    assert pipe_end["status"] == StageStatus.UNSUPPORTED.value
    assert pipe_end["final_status"] == PipelineStatus.UNSUPPORTED.value


# ==============================================================================
# 4. Validation Rejection Stops Without Execution
# ==============================================================================

def test_validation_rejection_lifecycle_events(obs_schema: DatabaseSchema, log_capture: LogCaptureHandler):
    """Verify validation rejection skips execution and logs rejection reason codes."""
    gen = FakeGenerator(sql="DELETE FROM Users;")
    pipeline = QueryPipeline(generator=gen, schema=obs_schema)

    with patch("query_pilot.pipeline.execute_read_only") as mock_exec:
        res = pipeline.run("Delete all users")
        mock_exec.assert_not_called()

    assert res.status == PipelineStatus.VALIDATION_REJECTED

    # Execution stage was NEVER emitted
    emitted_stages = [e["stage"] for e in log_capture.events]
    assert PipelineStage.READ_ONLY_EXECUTION.value not in emitted_stages

    # Validation end event has 'rejected' status and rejection_codes
    val_end = [e for e in log_capture.events if e.get("stage") == PipelineStage.SQL_VALIDATION.value and e["event"] == ObservabilityEvent.STAGE_END.value][0]
    assert val_end["status"] == StageStatus.REJECTED.value
    assert val_end["error_code"] == ErrorCategory.VALIDATION_REJECTED.value
    assert "NOT_READ_ONLY" in val_end["rejection_codes"]

    # Pipeline end has 'rejected'
    pipe_end = log_capture.events[-1]
    assert pipe_end["event"] == ObservabilityEvent.PIPELINE_END.value
    assert pipe_end["status"] == StageStatus.REJECTED.value
    assert pipe_end["final_status"] == PipelineStatus.VALIDATION_REJECTED.value


# ==============================================================================
# 5. Distinguishable Generation vs Execution Failures
# ==============================================================================

def test_generation_failure_distinguishable(obs_schema: DatabaseSchema, log_capture: LogCaptureHandler):
    """Verify generation failure emits SQL_GENERATION_FAILED and stops before validation."""
    gen = FakeGenerator(raise_exc=SQLGenerationError("Model connection failed"))
    pipeline = QueryPipeline(generator=gen, schema=obs_schema)

    res = pipeline.run("Fail generation")
    assert res.status == PipelineStatus.GENERATION_FAILED

    emitted_stages = [e["stage"] for e in log_capture.events]
    assert PipelineStage.SQL_VALIDATION.value not in emitted_stages
    assert PipelineStage.READ_ONLY_EXECUTION.value not in emitted_stages

    gen_end = [e for e in log_capture.events if e.get("stage") == PipelineStage.SQL_GENERATION.value and e["event"] == ObservabilityEvent.STAGE_END.value][0]
    assert gen_end["status"] == StageStatus.FAILED.value
    assert gen_end["error_code"] == ErrorCategory.SQL_GENERATION_FAILED.value


def test_execution_failure_distinguishable(obs_schema: DatabaseSchema, log_capture: LogCaptureHandler):
    """Verify execution failure emits EXECUTION_FAILED after validation succeeded."""
    pipeline = QueryPipeline(generator=FakeGenerator(), schema=obs_schema)

    with patch("query_pilot.pipeline.execute_read_only") as mock_exec:
        mock_exec.side_effect = QueryExecutionError("Database connection dropped")
        res = pipeline.run("Execute query")

    assert res.status == PipelineStatus.EXECUTION_FAILED

    # Validation succeeded
    val_end = [e for e in log_capture.events if e.get("stage") == PipelineStage.SQL_VALIDATION.value and e["event"] == ObservabilityEvent.STAGE_END.value][0]
    assert val_end["status"] == StageStatus.SUCCEEDED.value

    # Execution failed
    exec_end = [e for e in log_capture.events if e.get("stage") == PipelineStage.READ_ONLY_EXECUTION.value and e["event"] == ObservabilityEvent.STAGE_END.value][0]
    assert exec_end["status"] == StageStatus.FAILED.value
    assert exec_end["error_code"] == ErrorCategory.EXECUTION_FAILED.value


def test_execution_timeout_categorized():
    """Verify QueryTimeoutError maps specifically to QUERY_TIMEOUT error code."""
    code, err_type = classify_error(QueryTimeoutError("Query timed out"), PipelineStage.READ_ONLY_EXECUTION)
    assert code == ErrorCategory.QUERY_TIMEOUT.value
    assert err_type == "QueryTimeoutError"


# ==============================================================================
# 6. Monotonic Timing and Duration Metrics
# ==============================================================================

def test_timing_durations_non_negative_and_recorded(obs_schema: DatabaseSchema, log_capture: LogCaptureHandler):
    """Verify all stage durations and total duration are non-negative floats."""
    pipeline = QueryPipeline(generator=FakeGenerator(), schema=obs_schema)

    with patch("query_pilot.pipeline.execute_read_only") as mock_exec:
        mock_exec.return_value = QueryResult(
            columns=["Id"],
            rows=[{"Id": 1}],
            row_count=1,
            truncated=False,
        )
        res = pipeline.run("Check timing")

    assert res.duration_ms is not None
    assert res.duration_ms >= 0.0

    # Check stage durations dict on PipelineResult
    assert "schema_acquisition" in res.stage_durations
    assert "sql_generation" in res.stage_durations
    assert "sql_validation" in res.stage_durations
    assert "read_only_execution" in res.stage_durations
    assert "result_presentation" in res.stage_durations

    for stage_name, duration in res.stage_durations.items():
        assert duration >= 0.0, f"Stage {stage_name} had negative duration: {duration}"

    # Check durations in emitted events
    for event in log_capture.events:
        if event["event"] in (ObservabilityEvent.STAGE_END.value, ObservabilityEvent.PIPELINE_END.value):
            assert "duration_ms" in event
            assert event["duration_ms"] >= 0.0


# ==============================================================================
# 7. Execution Metadata Without Leaking Returned Rows
# ==============================================================================

def test_execution_metadata_captured_without_logging_rows(obs_schema: DatabaseSchema, log_capture: LogCaptureHandler):
    """Verify row_count and truncated are captured without emitting row data or values."""
    secret_value = "CONFIDENTIAL_STUDENT_RECORD_999"
    pipeline = QueryPipeline(generator=FakeGenerator(), schema=obs_schema)

    with patch("query_pilot.pipeline.execute_read_only") as mock_exec:
        mock_exec.return_value = QueryResult(
            columns=["Id", "SecretNote"],
            rows=[{"Id": 1, "SecretNote": secret_value}],
            row_count=1,
            truncated=True,
        )
        res = pipeline.run("Get secret record")

    assert res.status == PipelineStatus.SUCCESS

    # Verify log messages
    for record in log_capture.records:
        msg = record.getMessage()
        # Row content must NOT appear anywhere in log message
        assert secret_value not in msg
        assert "SecretNote" not in msg

    exec_end = [e for e in log_capture.events if e.get("stage") == PipelineStage.READ_ONLY_EXECUTION.value and e["event"] == ObservabilityEvent.STAGE_END.value][0]
    assert exec_end["row_count"] == 1
    assert exec_end["truncated"] is True
    assert "rows" not in exec_end
    assert "columns" not in exec_end


# ==============================================================================
# 8. Security and Privacy: No Questions, SQL, Credentials, or Tracebacks
# ==============================================================================

def test_sensitive_content_never_emitted_in_logs(obs_schema: DatabaseSchema, log_capture: LogCaptureHandler):
    """Verify user questions, generated SQL, passwords, tokens, and raw tracebacks are absent."""
    sensitive_question = "What is the secret question for user Aarav?"
    sensitive_sql = "SELECT PasswordHash FROM Users WHERE Secret = 'SuperSecretPass!';"
    sensitive_conn = "Server=127.0.0.1;UID=sa;PWD=VerySecretDBPassword!;Database=GradeSense_Local"

    gen = FakeGenerator(sql=sensitive_sql)
    pipeline = QueryPipeline(generator=gen, schema=obs_schema)

    with patch("query_pilot.pipeline.execute_read_only") as mock_exec:
        mock_exec.side_effect = QueryExecutionError(f"Database error: {sensitive_conn}")
        res = pipeline.run(sensitive_question)

    # Check all captured records
    for record in log_capture.records:
        msg = record.getMessage()
        assert sensitive_question not in msg
        assert sensitive_sql not in msg
        assert "PasswordHash" not in msg
        assert "SuperSecretPass!" not in msg
        assert "VerySecretDBPassword!" not in msg
        assert "Traceback" not in msg
        assert "Database error:" not in msg  # Raw exception message excluded


# ==============================================================================
# 9. Failure Isolation: Logging Errors Do Not Break Pipeline
# ==============================================================================

def test_observability_failure_does_not_alter_pipeline_result(obs_schema: DatabaseSchema):
    """Verify an error inside the logging/observability handler does not disrupt the pipeline."""
    pipeline = QueryPipeline(generator=FakeGenerator(), schema=obs_schema)

    with (
        patch("query_pilot.pipeline.execute_read_only") as mock_exec,
        patch("query_pilot.observability.PipelineObserver._emit") as mock_emit,
    ):
        mock_exec.return_value = QueryResult(
            columns=["Id", "FullName"],
            rows=[{"Id": 1, "FullName": "Alice"}],
            row_count=1,
            truncated=False,
        )
        # Simulate a crash in logging subsystem
        mock_emit.side_effect = RuntimeError("Disk full / logger crashed")

        res = pipeline.run("Test failure isolation")

        # Pipeline must still complete successfully and return valid result
        assert res.status == PipelineStatus.SUCCESS
        assert res.is_success is True
        assert res.execution_result is not None
        assert res.execution_result.row_count == 1


# ==============================================================================
# 10. Backward Compatibility & Public Model Contract
# ==============================================================================

def test_pipeline_result_to_dict_and_present_compatibility():
    """Verify PipelineResult.to_dict() and .present() behave cleanly with observability fields."""
    qr = QueryResult(
        columns=["Count"],
        rows=[{"Count": 5}],
        row_count=1,
        truncated=False,
        execution_time_ms=1.2,
    )
    result = PipelineResult(
        question="Count users",
        status=PipelineStatus.SUCCESS,
        generated_sql="SELECT COUNT(*) AS Count FROM Users;",
        execution_result=qr,
        request_id="req_test_compat_123",
        duration_ms=45.2,
        stage_durations={"sql_generation": 40.0, "sql_validation": 1.5},
    )

    d = result.to_dict()
    assert d["question"] == "Count users"
    assert d["status"] == "SUCCESS"
    assert d["request_id"] == "req_test_compat_123"
    assert d["duration_ms"] == 45.2
    assert d["stage_durations"] == {"sql_generation": 40.0, "sql_validation": 1.5}

    pres = result.present()
    assert isinstance(pres, PresentationResult)
    assert pres.status == "success"
    assert pres.request_id == "req_test_compat_123"
    assert pres.scalar_value == 5


def test_observability_disabled_flag(obs_schema: DatabaseSchema, log_capture: LogCaptureHandler):
    """Verify enable_observability=False suppresses log emission but still populates timing."""
    pipeline = QueryPipeline(
        generator=FakeGenerator(),
        schema=obs_schema,
        enable_observability=False,
    )

    with patch("query_pilot.pipeline.execute_read_only") as mock_exec:
        mock_exec.return_value = QueryResult(
            columns=["Id"],
            rows=[{"Id": 1}],
            row_count=1,
            truncated=False,
        )
        res = pipeline.run("Quiet run")

    assert res.status == PipelineStatus.SUCCESS
    assert res.duration_ms is not None
    # No logs should have been emitted
    assert len(log_capture.records) == 0


def test_run_question_convenience_helper(obs_schema: DatabaseSchema):
    """Verify run_question helper accepts request_id."""
    with patch("query_pilot.pipeline.execute_read_only") as mock_exec:
        mock_exec.return_value = QueryResult(
            columns=["Id"],
            rows=[{"Id": 1}],
            row_count=1,
            truncated=False,
        )
        res = run_question(
            "Count",
            pipeline=QueryPipeline(generator=FakeGenerator(), schema=obs_schema),
            request_id="custom_req_via_helper",
        )
        assert res.request_id == "custom_req_via_helper"
