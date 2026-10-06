"""Tests for QueryPilot end-to-end Text-to-SQL pipeline (Task 1.10).

Verifies the strict orchestration boundary:
    Question
       ↓
    Generation (SQLGenerator)
       ↓
    Deterministic validation (validate_sql)
       ↓
    Conditional execution (execute_read_only)
       ↓
    PipelineResult

Ensures:
- Invalid SQL never reaches the executor.
- Generator can be replaced by a fake provider in tests without network calls.
- Unit tests cover generation, validation, and execution boundaries.
- Live integration tests against GradeSense_Local verify valid analytical queries.
- Semantic correctness vs safety validation limitation is formally tested and documented.
"""

from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch
import pytest

from query_pilot.db.connection import create_db_engine
from query_pilot.db.executor import (
    QueryExecutionError,
    QueryResult,
    execute_read_only,
)
from query_pilot.db.introspection import DatabaseIntrospector
from query_pilot.db.models import (
    ColumnMetadata,
    DatabaseSchema,
    ForeignKeyMetadata,
    TableMetadata,
)
from query_pilot.pipeline import (
    PipelineResult,
    PipelineStatus,
    QueryPipeline,
    run_question,
)
from query_pilot.sql.generation import (
    EmptySQLError,
    ProviderAPIError,
    SQLGenerationRequest,
    SQLGenerationResponse,
    SQLGenerator,
)
from query_pilot.sql.models import ValidationReasonCode


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
            ColumnMetadata(name="Role", data_type="varchar(50)", nullable=False),
            # Note: PasswordHash is redacted
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
            ),
        ],
    )

    departments_table = TableMetadata(
        schema_name="dbo",
        table_name="Departments",
        columns=[
            ColumnMetadata(name="Id", data_type="int", nullable=False, primary_key=True),
            ColumnMetadata(name="Name", data_type="varchar(100)", nullable=False),
            ColumnMetadata(name="Code", data_type="varchar(10)", nullable=False),
        ],
        primary_keys=["Id"],
    )

    return DatabaseSchema(
        database_name="GradeSense_Local",
        tables=[users_table, students_table, departments_table],
        is_model_facing=True,
    )


class DeterministicFakeGenerator(SQLGenerator):
    """Fake SQL generator returning a predetermined SQL statement."""

    def __init__(
        self,
        sql: str,
        explanation: Optional[str] = "Generated for test.",
        assumptions: Optional[List[str]] = None,
    ):
        self.sql = sql
        self.explanation = explanation
        self.assumptions = assumptions or ["Test assumption"]
        self.calls: List[SQLGenerationRequest] = []

    def generate(self, request: SQLGenerationRequest) -> SQLGenerationResponse:
        self.calls.append(request)
        return SQLGenerationResponse(
            sql=self.sql,
            explanation=self.explanation,
            assumptions=self.assumptions,
        )


class FaultyGenerator(SQLGenerator):
    """Fake SQL generator that raises a specified exception."""

    def __init__(self, exception_to_raise: Exception):
        self.exception_to_raise = exception_to_raise
        self.calls: List[SQLGenerationRequest] = []

    def generate(self, request: SQLGenerationRequest) -> SQLGenerationResponse:
        self.calls.append(request)
        raise self.exception_to_raise


# ==============================================================================
# Unit Tests — Pipeline Boundaries & Edge Cases (Offline, No DB)
# ==============================================================================

class TestPipelineUnitBoundaries:
    """Unit tests verifying generation, validation, and execution isolation."""

    def test_pipeline_valid_sql_execution(self, test_schema: DatabaseSchema):
        """Test A: Valid SQL passes generation, validation, and executes successfully."""
        sql = "SELECT TOP 5 s.Id, u.FullName FROM Students AS s JOIN Users AS u ON s.Id = u.Id ORDER BY s.Id;"
        generator = DeterministicFakeGenerator(sql=sql)
        pipeline = QueryPipeline(generator=generator, schema=test_schema)

        mock_query_result = QueryResult(
            columns=["Id", "FullName"],
            rows=[{"Id": 1, "FullName": "Alice Student"}],
            row_count=1,
            truncated=False,
            execution_time_ms=5.0,
        )

        with patch("query_pilot.pipeline.execute_read_only", return_value=mock_query_result) as mock_exec:
            result = pipeline.run("Show top 5 students")

            assert result.status == PipelineStatus.SUCCESS
            assert result.is_success is True
            assert result.generated_sql == sql
            assert result.validation_result is not None
            assert result.validation_result.allowed is True
            assert result.execution_result == mock_query_result
            assert len(generator.calls) == 1
            mock_exec.assert_called_once_with(
                sql=sql,
                max_rows=None,
                timeout_seconds=None,
                engine=pipeline.engine,
            )

    def test_pipeline_unsafe_sql_stopped_before_execution(self, test_schema: DatabaseSchema):
        """Test B: Unsafe SQL (DELETE) is rejected by validator and NEVER touches the executor."""
        generator = DeterministicFakeGenerator(sql="DELETE FROM Students;")
        pipeline = QueryPipeline(generator=generator, schema=test_schema)

        with patch("query_pilot.pipeline.execute_read_only") as mock_exec:
            result = pipeline.run("Delete all students")

            assert result.status == PipelineStatus.VALIDATION_REJECTED
            assert result.is_success is False
            assert result.validation_result is not None
            assert result.validation_result.allowed is False
            assert ValidationReasonCode.NOT_READ_ONLY.value in result.validation_result.reason_codes
            # Critical boundary assertion: executor was NEVER invoked
            mock_exec.assert_not_called()

    def test_pipeline_unknown_table_rejected(self, test_schema: DatabaseSchema):
        """Test C: Candidate SQL referencing a nonexistent table is stopped before execution."""
        generator = DeterministicFakeGenerator(sql="SELECT * FROM UnknownTable;")
        pipeline = QueryPipeline(generator=generator, schema=test_schema)

        with patch("query_pilot.pipeline.execute_read_only") as mock_exec:
            result = pipeline.run("Query ghost table")

            assert result.status == PipelineStatus.VALIDATION_REJECTED
            assert result.validation_result.allowed is False
            assert ValidationReasonCode.UNKNOWN_TABLE.value in result.validation_result.reason_codes
            mock_exec.assert_not_called()

    def test_pipeline_unknown_column_rejected(self, test_schema: DatabaseSchema):
        """Test D: Candidate SQL referencing a nonexistent column is stopped before execution."""
        generator = DeterministicFakeGenerator(sql="SELECT NonExistentColumn FROM Students;")
        pipeline = QueryPipeline(generator=generator, schema=test_schema)

        with patch("query_pilot.pipeline.execute_read_only") as mock_exec:
            result = pipeline.run("Query invalid column")

            assert result.status == PipelineStatus.VALIDATION_REJECTED
            assert result.validation_result.allowed is False
            assert ValidationReasonCode.UNKNOWN_COLUMN.value in result.validation_result.reason_codes
            mock_exec.assert_not_called()

    def test_pipeline_sensitive_column_rejected(self, test_schema: DatabaseSchema):
        """Test E: Candidate SQL referencing sensitive PasswordHash is rejected immediately."""
        generator = DeterministicFakeGenerator(sql="SELECT PasswordHash FROM Users;")
        pipeline = QueryPipeline(generator=generator, schema=test_schema)

        with patch("query_pilot.pipeline.execute_read_only") as mock_exec:
            result = pipeline.run("Extract user passwords")

            assert result.status == PipelineStatus.VALIDATION_REJECTED
            assert result.validation_result.allowed is False
            # Either SENSITIVE_COLUMN or UNKNOWN_COLUMN (since redacted from schema)
            reasons = result.validation_result.reason_codes
            assert (
                ValidationReasonCode.SENSITIVE_COLUMN.value in reasons
                or ValidationReasonCode.UNKNOWN_COLUMN.value in reasons
            )
            mock_exec.assert_not_called()

    def test_pipeline_empty_generation_error(self, test_schema: DatabaseSchema):
        """Test F: Empty or missing generation output produces controlled failure without validation/execution."""
        generator = FaultyGenerator(EmptySQLError("Candidate SQL is empty."))
        pipeline = QueryPipeline(generator=generator, schema=test_schema)

        with (
            patch("query_pilot.pipeline.validate_sql") as mock_val,
            patch("query_pilot.pipeline.execute_read_only") as mock_exec,
        ):
            result = pipeline.run("Return nothing")

            assert result.status == PipelineStatus.GENERATION_FAILED
            assert result.is_success is False
            assert "Candidate SQL is empty" in str(result.error_message)
            assert result.validation_result is None
            assert result.execution_result is None
            mock_val.assert_not_called()
            mock_exec.assert_not_called()

    def test_pipeline_provider_failure_handled(self, test_schema: DatabaseSchema):
        """Test G: External LLM provider failure is captured cleanly without crashing."""
        generator = FaultyGenerator(ProviderAPIError("Gemini quota exhausted."))
        pipeline = QueryPipeline(generator=generator, schema=test_schema)

        with (
            patch("query_pilot.pipeline.validate_sql") as mock_val,
            patch("query_pilot.pipeline.execute_read_only") as mock_exec,
        ):
            result = pipeline.run("Count students")

            assert result.status == PipelineStatus.GENERATION_FAILED
            assert result.is_success is False
            assert "Gemini quota exhausted" in str(result.error_message)
            mock_val.assert_not_called()
            mock_exec.assert_not_called()

    def test_pipeline_execution_failure_without_retry(self, test_schema: DatabaseSchema):
        """Test H: Execution failure is captured without automatic retry loops."""
        sql = "SELECT TOP 5 Id FROM Students;"
        generator = DeterministicFakeGenerator(sql=sql)
        pipeline = QueryPipeline(generator=generator, schema=test_schema)

        with patch(
            "query_pilot.pipeline.execute_read_only",
            side_effect=QueryExecutionError("Simulated database timeout."),
        ) as mock_exec:
            result = pipeline.run("Count students")

            assert result.status == PipelineStatus.EXECUTION_FAILED
            assert result.is_success is False
            assert result.validation_result is not None
            assert result.validation_result.allowed is True
            assert "Simulated database timeout" in str(result.error_message)
            # Executed exactly once — NO retry loop
            assert mock_exec.call_count == 1

    def test_pipeline_result_to_dict(self, test_schema: DatabaseSchema):
        """Test serializability of PipelineResult to dict format."""
        result = PipelineResult(
            question="What is the student count?",
            status=PipelineStatus.SUCCESS,
            generated_sql="SELECT COUNT(*) FROM Students;",
            generation_explanation="Counts all students",
            generation_assumptions=["Active students only"],
        )
        d = result.to_dict()
        assert d["question"] == "What is the student count?"
        assert d["status"] == "SUCCESS"
        assert d["generated_sql"] == "SELECT COUNT(*) FROM Students;"
        assert d["generation_explanation"] == "Counts all students"


# ==============================================================================
# Live Integration Tests against GradeSense_Local (Deterministic SQL)
# ==============================================================================

class TestLivePipelineIntegration:
    """Live database integration tests against verified GradeSense_Local."""

    @pytest.fixture(scope="class")
    def live_pipeline(self) -> QueryPipeline:
        """Create a pipeline connected to GradeSense_Local with real introspection."""
        engine = create_db_engine()
        introspector = DatabaseIntrospector(engine)
        schema = introspector.introspect_model_facing()
        return QueryPipeline(engine=engine, schema=schema)

    def test_live_pipeline_query_1_student_count(self, live_pipeline: QueryPipeline):
        """Integration Query 1: Simple aggregate count of all students."""
        sql = "SELECT COUNT(*) AS TotalStudents FROM Students;"
        live_pipeline._generator = DeterministicFakeGenerator(sql=sql)

        result = live_pipeline.run("How many students are registered?")

        assert result.status == PipelineStatus.SUCCESS
        assert result.is_success is True
        assert result.validation_result is not None
        assert result.validation_result.allowed is True
        assert result.execution_result is not None
        assert result.execution_result.row_count == 1
        assert result.execution_result.rows[0]["TotalStudents"] == 40

    def test_live_pipeline_query_2_students_per_department(self, live_pipeline: QueryPipeline):
        """Integration Query 2: Grouping aggregation with known valid foreign key relationship."""
        sql = (
            "SELECT d.Name AS DepartmentName, COUNT(s.Id) AS StudentCount "
            "FROM Departments AS d "
            "JOIN Students AS s ON s.DepartmentId = d.Id "
            "GROUP BY d.Name "
            "ORDER BY d.Name;"
        )
        live_pipeline._generator = DeterministicFakeGenerator(sql=sql)

        result = live_pipeline.run("How many students belong to each department?")

        assert result.status == PipelineStatus.SUCCESS
        assert result.is_success is True
        assert result.validation_result is not None
        assert result.validation_result.allowed is True
        assert result.execution_result is not None
        assert result.execution_result.row_count == 2
        # Verify 40 total students partitioned across the 2 departments
        total_students = sum(r["StudentCount"] for r in result.execution_result.rows)
        assert total_students == 40

    def test_live_pipeline_unsafe_query_does_not_mutate_db(self, live_pipeline: QueryPipeline):
        """Integration Test: Unsafe mutating query is blocked and leaves database intact."""
        live_pipeline._generator = DeterministicFakeGenerator(sql="DELETE FROM Students WHERE 1=1;")

        result = live_pipeline.run("Delete all student records")

        assert result.status == PipelineStatus.VALIDATION_REJECTED
        assert result.is_success is False
        assert result.validation_result is not None
        assert result.validation_result.allowed is False

        # Verify database remained intact (read directly)
        check_result = execute_read_only(
            sql="SELECT COUNT(*) AS StudentCount FROM Students;",
            engine=live_pipeline.engine,
        )
        assert check_result.rows[0]["StudentCount"] == 40


# ==============================================================================
# Semantic Correctness vs Safety Boundary Evaluation
# ==============================================================================

class TestSemanticCorrectnessLimitation:
    """Demonstrates that deterministic validation enforces safety, not business correctness.

    A query can be:
    1. Syntactically valid T-SQL.
    2. Structurally compliant with model-facing schema.
    3. Strictly read-only with no forbidden AST nodes.
    4. Approved by the validator (allowed=True).
    5. Executed successfully by the database.

    YET still be semantically INCORRECT according to domain intent.
    This establishes the motivation for future semantic evaluation and agent reasoning.
    """

    def test_semantically_mismatched_join_passes_safety_validation(
        self,
        test_schema: DatabaseSchema,
    ):
        """Verify that joining Students.DepartmentId = Users.Id passes validation despite being semantically absurd."""
        # Semantically invalid join: DepartmentId (1 or 2) joined to User.Id (1 or 2)
        semantically_wrong_sql = (
            "SELECT TOP 5 s.Id AS StudentId, u.FullName "
            "FROM Students AS s "
            "JOIN Users AS u ON s.DepartmentId = u.Id "
            "ORDER BY s.Id;"
        )
        generator = DeterministicFakeGenerator(sql=semantically_wrong_sql)
        pipeline = QueryPipeline(generator=generator, schema=test_schema)

        # Mock query result representing what SQL Server would return (the Dean/HOD user name!)
        mock_result = QueryResult(
            columns=["StudentId", "FullName"],
            rows=[{"StudentId": 1, "FullName": "Dr. Rajesh Sharma"}],  # HOD, NOT the student!
            row_count=1,
            truncated=False,
            execution_time_ms=3.0,
        )

        with patch("query_pilot.pipeline.execute_read_only", return_value=mock_result):
            result = pipeline.run("List the first 5 students with their full names.")

            # Safety validator correctly passes it because schema access rules are not violated
            assert result.status == PipelineStatus.SUCCESS
            assert result.validation_result.allowed is True

            # BUT the resulting data maps DepartmentId to a User record instead of the Student's user record!
            # The correct join is: Students.Id = Users.Id (table inheritance in GradeSense).
            assert result.execution_result.rows[0]["FullName"] == "Dr. Rajesh Sharma"
