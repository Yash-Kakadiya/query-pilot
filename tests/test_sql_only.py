"""Tests for Validated SQL-Only Mode and SQL Disclosure Policy.

Covers:
1. Request defaults and invalid execution_mode values.
2. Valid SQL-only request returns VALIDATED_ONLY and validated SQL when include_sql=True.
3. SQL-only execution does not invoke the database executor or connect to the database.
4. Unsafe or mutating SQL is rejected and is not returned as validated SQL.
5. SQL-only behavior when a trusted schema is unavailable.
6. include_sql=False preserves current API response behavior (generated_sql is None).
7. include_sql=True returns SQL for successful execution when safe.
8. include_sql=True may return validated SQL for execution failure, but not for validation rejection.
9. End-to-end SQL-only execution via QueryPipeline and QueryPilotWorkflow with demo schema.
10. Normal execution_mode="execute" behavior remains unchanged.
"""

from decimal import Decimal
from unittest.mock import MagicMock, patch
import pytest
from fastapi.testclient import TestClient

from query_pilot.api.app import create_app
from query_pilot.api.models import QueryRequest, QueryResponse
from query_pilot.db.demo_schema import get_demo_schema
from query_pilot.db.executor import QueryExecutionError, QueryResult
from query_pilot.db.models import DatabaseSchema
from query_pilot.graph.state import create_initial_state, validate_initial_state
from query_pilot.graph.workflow import QueryPilotWorkflow
from query_pilot.pipeline import PipelineResult, PipelineStatus, QueryPipeline
from query_pilot.runner import ExecutionMode, QueryRunner, run_query
from query_pilot.sql.generation import (
    SQLGenerationRequest,
    SQLGenerationResponse,
    SQLGenerationStatus,
    SQLGenerator,
)
from query_pilot.sql.models import ValidationReason, ValidationReasonCode, ValidationResult


class FakeDeterministicGenerator(SQLGenerator):
    """Fake SQL generator returning predetermined SQL response."""

    def __init__(self, sql: str, status: SQLGenerationStatus = SQLGenerationStatus.ANSWERABLE):
        self.sql = sql
        self.status = status
        self.calls: list[SQLGenerationRequest] = []

    def generate(self, request: SQLGenerationRequest) -> SQLGenerationResponse:
        self.calls.append(request)
        return SQLGenerationResponse(
            status=self.status,
            sql=self.sql,
            explanation="Deterministic test SQL explanation.",
            assumptions=["Test assumption"],
        )


# ==============================================================================
# 1. Request Defaults and Invalid execution_mode Validation Tests
# ==============================================================================

def test_query_request_defaults():
    """Verify QueryRequest defaults: execution_mode='execute', include_sql=False."""
    req = QueryRequest(question="How many students are enrolled?")
    assert req.question == "How many students are enrolled?"
    assert req.execution_mode == "execute"
    assert req.include_sql is False


def test_query_request_valid_modes():
    """Verify QueryRequest accepts 'execute' and 'sql_only' case-insensitively."""
    req1 = QueryRequest(question="test", execution_mode="execute", include_sql=True)
    assert req1.execution_mode == "execute"
    assert req1.include_sql is True

    req2 = QueryRequest(question="test", execution_mode="sql_only", include_sql=False)
    assert req2.execution_mode == "sql_only"

    req3 = QueryRequest(question="test", execution_mode="SQL_ONLY")
    assert req3.execution_mode == "sql_only"


def test_query_request_rejects_invalid_execution_mode():
    """Verify QueryRequest rejects invalid execution_mode values with 422 validation error."""
    app = create_app(runner=MagicMock())
    client = TestClient(app)

    # Invalid string mode
    resp = client.post("/query", json={"question": "Test?", "execution_mode": "invalid_mode"})
    assert resp.status_code == 422
    assert "execution_mode" in str(resp.json())

    # Invalid type (e.g. integer)
    resp = client.post("/query", json={"question": "Test?", "execution_mode": 123})
    assert resp.status_code == 422

    # Invalid include_sql type (e.g. string instead of boolean)
    resp = client.post("/query", json={"question": "Test?", "include_sql": "yes"})
    assert resp.status_code == 422


def test_query_request_rejects_client_database_controls():
    """Verify QueryRequest strictly rejects client-supplied db credentials, URLs, or schemas."""
    app = create_app(runner=MagicMock())
    client = TestClient(app)

    payloads = [
        {"question": "Test?", "database_url": "mssql+pyodbc://attacker/db"},
        {"question": "Test?", "connection_string": "Server=evil;Database=evil"},
        {"question": "Test?", "schema": {"tables": []}},
        {"question": "Test?", "trusted_schema": True},
    ]
    for p in payloads:
        resp = client.post("/query", json=p)
        assert resp.status_code == 422
        assert "extra_forbidden" in str(resp.json()) or "Extra inputs" in str(resp.json()) or "extra" in str(resp.json())


# ==============================================================================
# 2. Valid SQL-Only Request Returns VALIDATED_ONLY and SQL When include_sql=True
# ==============================================================================

def test_sql_only_request_returns_validated_only_with_sql():
    """Verify valid SQL-only request returns VALIDATED_ONLY and validated SQL when include_sql=True."""
    mock_runner = MagicMock()
    sql_text = "SELECT TOP 100 Id, FullName, Email FROM Users"
    mock_runner.run.return_value = PipelineResult(
        question="Show all users",
        status=PipelineStatus.VALIDATED_ONLY,
        generated_sql=sql_text,
        generation_explanation="Retrieves top 100 users.",
        validation_result=ValidationResult(allowed=True, reasons=[]),
        execution_result=None,
        request_id="req-sql-only-01",
    )

    app = create_app(runner=mock_runner)
    client = TestClient(app)

    response = client.post(
        "/query",
        json={"question": "Show all users", "execution_mode": "sql_only", "include_sql": True},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "VALIDATED_ONLY"
    assert data["generated_sql"] == sql_text
    assert data["rows"] == []
    assert data["row_count"] == 0
    mock_runner.run.assert_called_once_with(
        question="Show all users",
        request_id=mock_runner.run.call_args.kwargs["request_id"],
        conversation_context=None,
        execution_mode="sql_only",
    )


# ==============================================================================
# 3. SQL-Only Execution Bypasses Database Execution Completely
# ==============================================================================

def test_pipeline_sql_only_does_not_call_database_executor():
    """Verify QueryPipeline in sql_only mode never touches executor or database engine."""
    fake_generator = FakeDeterministicGenerator("SELECT TOP 10 Id, Name FROM Departments")
    mock_engine = MagicMock()

    with patch("query_pilot.pipeline.execute_read_only") as mock_exec:
        pipeline = QueryPipeline(
            generator=fake_generator,
            engine=mock_engine,
            schema=get_demo_schema(),
            max_rows=100,
        )

        result = pipeline.run(
            question="List departments",
            execution_mode="sql_only",
        )

        assert result.status == PipelineStatus.VALIDATED_ONLY
        assert result.is_validated_only is True
        assert result.execution_result is None
        assert result.generated_sql == "SELECT TOP 10 Id, Name FROM Departments"
        mock_exec.assert_not_called()
        mock_engine.connect.assert_not_called()


def test_workflow_sql_only_does_not_call_database_executor():
    """Verify LangGraph QueryPilotWorkflow in sql_only mode routes directly to present_result."""
    fake_generator = FakeDeterministicGenerator("SELECT TOP 5 Id, EnrollmentNumber FROM Students")
    mock_engine = MagicMock()

    with patch("query_pilot.graph.workflow.execute_read_only") as mock_exec:
        workflow = QueryPilotWorkflow(
            generator=fake_generator,
            engine=mock_engine,
            schema=get_demo_schema(),
            max_rows=100,
        )

        final_state = workflow.run(
            question="List students",
            execution_mode="sql_only",
        )

        assert final_state.get("status") == PipelineStatus.VALIDATED_ONLY
        assert final_state.get("execution_result") is None
        mock_exec.assert_not_called()
        mock_engine.connect.assert_not_called()


# ==============================================================================
# 4. Unsafe or Mutating SQL is Rejected and Never Returned
# ==============================================================================

def test_unsafe_sql_rejected_and_not_disclosed_in_sql_only_mode():
    """Verify mutating SQL is rejected by AST safety validator and NEVER returned as safe SQL."""
    mutating_sql = "DROP TABLE Students"
    fake_generator = FakeDeterministicGenerator(mutating_sql)

    pipeline = QueryPipeline(
        generator=fake_generator,
        engine=MagicMock(),
        schema=get_demo_schema(),
    )

    result = pipeline.run(
        question="Drop the students table",
        execution_mode="sql_only",
    )

    assert result.status == PipelineStatus.VALIDATION_REJECTED
    assert result.validation_result.allowed is False

    # Check projection via QueryResponse with include_sql=True
    response = QueryResponse.from_pipeline_result(result, include_sql=True)
    assert response.status == PipelineStatus.VALIDATION_REJECTED
    assert response.generated_sql is None  # MUST NEVER be disclosed


# ==============================================================================
# 5. SQL-Only Behavior When Schema is Unavailable
# ==============================================================================

def test_sql_only_schema_acquisition_failure():
    """Verify pipeline handles schema acquisition error gracefully in sql_only mode."""
    with patch("query_pilot.pipeline.QueryPipeline.get_schema", side_effect=RuntimeError("Schema unavailable")):
        pipeline = QueryPipeline(
            generator=FakeDeterministicGenerator("SELECT 1"),
            schema=None,
        )
        result = pipeline.run("What is X?", execution_mode="sql_only")
        assert result.status == PipelineStatus.EXECUTION_FAILED
        assert "Schema acquisition failed" in result.error_message


# ==============================================================================
# 6. include_sql=False Preserves Current API Behavior (generated_sql is None)
# ==============================================================================

def test_include_sql_false_preserves_omission():
    """Verify default include_sql=False omits generated_sql for all statuses."""
    res_success = PipelineResult(
        question="Test",
        status=PipelineStatus.SUCCESS,
        generated_sql="SELECT 1",
        validation_result=ValidationResult(allowed=True, reasons=[]),
    )
    resp = QueryResponse.from_pipeline_result(res_success, include_sql=False)
    assert resp.generated_sql is None

    res_val = PipelineResult(
        question="Test",
        status=PipelineStatus.VALIDATED_ONLY,
        generated_sql="SELECT 1",
        validation_result=ValidationResult(allowed=True, reasons=[]),
    )
    resp_val = QueryResponse.from_pipeline_result(res_val, include_sql=False)
    assert resp_val.generated_sql is None


# ==============================================================================
# 7. include_sql=True Returns SQL for Successful Execution
# ==============================================================================

def test_include_sql_true_returns_sql_on_success():
    """Verify include_sql=True returns validated SQL when execution succeeded."""
    sql = "SELECT TOP 10 Id FROM Users"
    res = PipelineResult(
        question="Test",
        status=PipelineStatus.SUCCESS,
        generated_sql=sql,
        validation_result=ValidationResult(allowed=True, reasons=[]),
    )
    resp = QueryResponse.from_pipeline_result(res, include_sql=True)
    assert resp.status == PipelineStatus.SUCCESS
    assert resp.generated_sql == sql


# ==============================================================================
# 8. Execution Failure After Validation Discloses SQL, Validation Rejection Never Does
# ==============================================================================

def test_include_sql_true_execution_failure_vs_validation_rejection():
    """Verify validated SQL may be disclosed on execution failure, but NEVER on validation rejection."""
    valid_sql = "SELECT TOP 10 Id FROM NonExistentView"
    val_allowed = ValidationResult(allowed=True, reasons=[])

    # Case A: Execution failed after passing safety validation
    res_exec_fail = PipelineResult(
        question="Test",
        status=PipelineStatus.EXECUTION_FAILED,
        generated_sql=valid_sql,
        validation_result=val_allowed,
        error_message="Database error: Invalid object name",
    )
    resp_exec = QueryResponse.from_pipeline_result(res_exec_fail, include_sql=True)
    assert resp_exec.status == PipelineStatus.EXECUTION_FAILED
    assert resp_exec.generated_sql == valid_sql

    # Case B: Validation rejected
    val_rejected = ValidationResult(
        allowed=False,
        reasons=[ValidationReason(code=ValidationReasonCode.NOT_READ_ONLY, message="Mutating statement")],
    )
    res_val_fail = PipelineResult(
        question="Test",
        status=PipelineStatus.VALIDATION_REJECTED,
        generated_sql="DELETE FROM Users",
        validation_result=val_rejected,
        error_message="Validation rejected",
    )
    resp_val = QueryResponse.from_pipeline_result(res_val_fail, include_sql=True)
    assert resp_val.status == PipelineStatus.VALIDATION_REJECTED
    assert resp_val.generated_sql is None

    # Case C: Generation failed
    res_gen_fail = PipelineResult(
        question="Test",
        status=PipelineStatus.GENERATION_FAILED,
        generated_sql=None,
        error_message="Model error",
    )
    resp_gen = QueryResponse.from_pipeline_result(res_gen_fail, include_sql=True)
    assert resp_gen.generated_sql is None


# ==============================================================================
# 9. QueryRunner SQL-Only Mode Using get_demo_schema()
# ==============================================================================

def test_query_runner_sql_only_with_demo_schema():
    """Verify QueryRunner seamlessly executes in sql_only mode using the demo schema provider."""
    fake_generator = FakeDeterministicGenerator("SELECT TOP 50 Id, FullName, Role FROM Users")
    mock_engine = MagicMock()

    runner = QueryRunner(
        generator=fake_generator,
        engine=mock_engine,
        # Intentionally passing schema=None so runner uses get_demo_schema() in sql_only mode
        schema=None,
    )

    result = runner.run(
        question="Show users",
        execution_mode="sql_only",
    )

    assert result.status == PipelineStatus.VALIDATED_ONLY
    assert result.is_validated_only is True
    assert result.generated_sql == "SELECT TOP 50 Id, FullName, Role FROM Users"
    assert result.execution_result is None
    # Verify database engine was never connected
    mock_engine.connect.assert_not_called()


# ==============================================================================
# 10. Execution Mode 'execute' Works Unchanged
# ==============================================================================

def test_query_runner_normal_execute_mode():
    """Verify normal execution_mode='execute' behaves as expected and executes query."""
    fake_generator = FakeDeterministicGenerator("SELECT TOP 5 Id FROM Departments")
    mock_engine = MagicMock()
    mock_exec_res = QueryResult(columns=["Id"], rows=[{"Id": 1}], row_count=1, truncated=False)

    with patch("query_pilot.pipeline.execute_read_only", return_value=mock_exec_res) as mock_exec:
        runner = QueryRunner(
            generator=fake_generator,
            engine=mock_engine,
            schema=get_demo_schema(),
        )

        result = runner.run(
            question="Show departments",
            execution_mode="execute",
        )

        assert result.status == PipelineStatus.SUCCESS
        assert result.execution_result == mock_exec_res
        mock_exec.assert_called_once()


# ==============================================================================
# 11. Deterministic Schema Fidelity Tests (Audit A)
# ==============================================================================

def test_demo_schema_fidelity_and_sensitive_column_redaction():
    """Verify demo schema table names, total columns, PKs, FKs, and sensitive column policy."""
    schema = get_demo_schema()
    assert schema.database_name == "GradeSense_Local"
    assert schema.is_model_facing is True

    expected_tables = [
        "Users", "Departments", "Faculties", "Students", "Batches",
        "Subjects", "SubjectUnits", "CourseOfferings", "CourseEnrollments",
        "EvaluationSchemes", "AssessmentItems", "StudentMarks",
        "FacultyAssignments", "AttendanceRecords", "UploadHistory",
        "AuditLog", "Predictions",
    ]
    assert len(schema.tables) == 17
    assert [t.table_name for t in schema.tables] == expected_tables

    # Verify PasswordHash is redacted from Users
    users_table = schema.get_table("Users")
    assert users_table is not None
    assert "PasswordHash" not in users_table.column_names
    assert "passwordhash" not in [c.lower() for c in users_table.column_names]

    # Verify Predictions table contains UpdatedAt and DeletedAt
    predictions_table = schema.get_table("Predictions")
    assert predictions_table is not None
    assert "UpdatedAt" in predictions_table.column_names
    assert "DeletedAt" in predictions_table.column_names

    # Verify total column count across all tables is exactly 207
    assert schema.total_columns_count == 207

    # Verify SensitiveColumnPolicy redaction
    from query_pilot.db.introspection import SensitiveColumnPolicy
    policy = SensitiveColumnPolicy()
    for table in schema.tables:
        for col in table.columns:
            assert not policy.is_sensitive(col.name), f"Sensitive column {table.table_name}.{col.name} leaked!"

    # Verify 32 canonical foreign key relationships are faithfully captured
    total_fks = sum(len(t.foreign_keys) for t in schema.tables)
    assert total_fks == 32

    # Verify primary keys on all tables
    for table in schema.tables:
        assert len(table.primary_keys) >= 1, f"Missing primary key for table {table.table_name}"


# ==============================================================================
# 12. Workflow Routing & Database Bypass Safeguards (Audit B)
# ==============================================================================

def test_workflow_execute_sql_node_safeguard_in_sql_only_mode():
    """Verify that even direct invocation of execute_sql node in sql_only mode safely returns VALIDATED_ONLY without execution."""
    mock_engine = MagicMock()
    workflow = QueryPilotWorkflow(
        generator=FakeDeterministicGenerator("SELECT 1"),
        engine=mock_engine,
        schema=get_demo_schema(),
    )

    state = create_initial_state(
        question="Count users",
        execution_mode="sql_only",
    )
    state["validation_result"] = ValidationResult(allowed=True, reasons=[])
    state["generation_response"] = SQLGenerationResponse(
        status=SQLGenerationStatus.ANSWERABLE,
        sql="SELECT COUNT(*) FROM Users",
        explanation="Count",
    )

    with patch("query_pilot.graph.workflow.execute_read_only") as mock_exec:
        result = workflow.execute_sql(state)
        assert result["status"] == PipelineStatus.VALIDATED_ONLY
        assert "execution_result" not in result or result.get("execution_result") is None
        mock_exec.assert_not_called()
        mock_engine.connect.assert_not_called()


def test_workflow_route_after_validation_routing():
    """Verify route_after_validation routes to present_result for sql_only, execute_sql for execute."""
    workflow = QueryPilotWorkflow(
        generator=FakeDeterministicGenerator("SELECT 1"),
        engine=MagicMock(),
        schema=get_demo_schema(),
    )

    allowed_val = ValidationResult(allowed=True, reasons=[])

    # sql_only mode -> present_result
    state_sql_only = create_initial_state(question="q", execution_mode="sql_only")
    state_sql_only["validation_result"] = allowed_val
    assert workflow.route_after_validation(state_sql_only) == "present_result"

    # execute mode -> execute_sql
    state_execute = create_initial_state(question="q", execution_mode="execute")
    state_execute["validation_result"] = allowed_val
    assert workflow.route_after_validation(state_execute) == "execute_sql"


# ==============================================================================
# 13. Comprehensive SQL Disclosure Policy Across All Terminal Statuses (Audit C)
# ==============================================================================

@pytest.mark.parametrize(
    "status, val_allowed, candidate_sql, expect_sql_when_true",
    [
        (PipelineStatus.SUCCESS, True, "SELECT TOP 10 * FROM Users", "SELECT TOP 10 * FROM Users"),
        (PipelineStatus.VALIDATED_ONLY, True, "SELECT TOP 10 * FROM Users", "SELECT TOP 10 * FROM Users"),
        (PipelineStatus.EXECUTION_FAILED, True, "SELECT TOP 10 * FROM Users", "SELECT TOP 10 * FROM Users"),
        (PipelineStatus.VALIDATION_REJECTED, False, "DROP TABLE Users", None),
        (PipelineStatus.UNSUPPORTED, None, None, None),
        (PipelineStatus.CLARIFICATION_NEEDED, None, None, None),
        (PipelineStatus.GENERATION_FAILED, None, None, None),
    ],
)
def test_sql_disclosure_policy_all_terminal_statuses(
    status: PipelineStatus,
    val_allowed: bool | None,
    candidate_sql: str | None,
    expect_sql_when_true: str | None,
):
    """Verify QueryResponse strictly obeys SQL disclosure rules for include_sql True and False."""
    val_result = (
        ValidationResult(allowed=val_allowed, reasons=[])
        if val_allowed is not None
        else None
    )
    pipe_res = PipelineResult(
        question="Test Question",
        status=status,
        generated_sql=candidate_sql,
        validation_result=val_result,
        error_message="Test error" if status in (PipelineStatus.EXECUTION_FAILED, PipelineStatus.GENERATION_FAILED) else None,
    )

    # When include_sql=False: MUST ALWAYS be None
    resp_false = QueryResponse.from_pipeline_result(pipe_res, include_sql=False)
    assert resp_false.generated_sql is None

    # When include_sql=True: MUST match expect_sql_when_true
    resp_true = QueryResponse.from_pipeline_result(pipe_res, include_sql=True)
    assert resp_true.generated_sql == expect_sql_when_true
