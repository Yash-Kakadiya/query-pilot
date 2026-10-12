"""LangGraph workflow for QueryPilot text-to-SQL processing (Task 1.19).

Coordinates the five logical pipeline stages as explicit graph nodes:
1. acquire_schema: Acquires model-facing database schema metadata.
2. generate_sql: Generates candidate SQL or structured unsupported refusal via LLM.
3. validate_sql: Deterministically validates candidate SQL using AST safety policies.
4. execute_sql: Executes read-only SQL under safe timeout and row limits.
5. present_result: Deterministically formats the final user-facing output.

Security & Safety Principles:
- Generator never executes SQL.
- Validation ALWAYS precedes execution.
- Invalid or unsafe SQL terminates without database access.
- Unsupported questions terminate without validation or execution.
- Execution failures terminate without retries.
- Every supported path terminates cleanly at END.
- State errors are sanitized to prevent credential/traceback leaks.
- Caller initial state objects are never mutated.
"""

import logging
from typing import Any, Dict, List, Optional, Set

from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DataError, OperationalError, ProgrammingError

from query_pilot.db.connection import create_db_engine
from query_pilot.db.executor import (
    QueryExecutionError,
    QueryResult,
    QueryTimeoutError,
    UnsafeQueryError,
    execute_read_only,
)
from query_pilot.db.introspection import DatabaseIntrospector
from query_pilot.db.models import DatabaseSchema
from query_pilot.graph.state import (
    MAX_GENERATION_ATTEMPTS,
    QueryPilotState,
    create_initial_state,
    validate_initial_state,
)
from query_pilot.observability import ErrorCategory, PipelineStage, classify_error
from query_pilot.pipeline import PipelineResult, PipelineStatus
from query_pilot.presentation.formatter import format_pipeline_result, sanitize_message
from query_pilot.sql.generation import (
    EmptySQLError,
    MalformedResponseError,
    SQLGenerationError,
    SQLGenerationRequest,
    SQLGenerationResponse,
    SQLGenerationStatus,
    SQLGenerator,
)
from query_pilot.sql.models import ValidationReasonCode, ValidationResult
from query_pilot.sql.validator import validate_sql

logger = logging.getLogger(__name__)

# Explicit policy sets for deterministic validation failure classification
RECOVERABLE_VALIDATION_CODES: Set[ValidationReasonCode] = {
    ValidationReasonCode.PARSE_ERROR,
    ValidationReasonCode.UNKNOWN_TABLE,
    ValidationReasonCode.UNKNOWN_COLUMN,
    ValidationReasonCode.EXCEEDS_MAX_ROWS,
    ValidationReasonCode.UNSUPPORTED_QUERY_SHAPE,
    ValidationReasonCode.EMPTY_SQL,
}

TERMINAL_VALIDATION_CODES: Set[ValidationReasonCode] = {
    ValidationReasonCode.NOT_READ_ONLY,
    ValidationReasonCode.MULTI_STATEMENT,
    ValidationReasonCode.FORBIDDEN_OPERATION,
    ValidationReasonCode.SENSITIVE_COLUMN,
    ValidationReasonCode.FORBIDDEN_TABLE,
}


def is_recoverable_validation_result(val_result: ValidationResult) -> bool:
    """Classify whether a validation rejection represents a safely recoverable issue.

    Deterministic Safety Policy:
    - If allowed is True: not a failure (False).
    - If reasons list is empty: unclassified/unknown failure is terminal (False).
    - If ANY reason code is terminal or not recognized: strictly terminal (False).
    - Only if ALL reason codes are in RECOVERABLE_VALIDATION_CODES: True.
    """
    if val_result.allowed:
        return False
    if not val_result.reasons:
        return False

    for reason in val_result.reasons:
        if reason.code in TERMINAL_VALIDATION_CODES or reason.code not in RECOVERABLE_VALIDATION_CODES:
            return False

    return True


def is_recoverable_generation_error(exc: Exception) -> bool:
    """Classify whether an LLM generation error represents a safely recoverable issue.

    Recoverable:
    - MalformedResponseError (structured JSON output parsing error).
    - EmptySQLError (empty candidate SQL string).

    Terminal:
    - ConfigurationError (missing API key/config).
    - ProviderAPIError (external API / network / auth failure).
    - SQLGenerationError (generic/unclassified generation failure).
    - Other unexpected exceptions.
    """
    return isinstance(exc, (MalformedResponseError, EmptySQLError))


def is_recoverable_execution_error(exc: Exception) -> bool:
    """Classify whether a database execution failure is a correctable SQL syntax/schema issue.

    Recoverable:
    - Database syntax or schema errors where cause is ProgrammingError or DataError.

    Terminal:
    - QueryTimeoutError (timeout).
    - UnsafeQueryError (mutating or multi-statement).
    - OperationalError (connection drop, server down).
    - Generic QueryExecutionError without explicit syntax/schema cause.
    """
    if isinstance(exc, (QueryTimeoutError, UnsafeQueryError)):
        return False

    cause = getattr(exc, "__cause__", None)
    if cause is not None:
        if isinstance(cause, OperationalError):
            return False
        if isinstance(cause, (ProgrammingError, DataError)):
            return True

    if isinstance(exc, (ProgrammingError, DataError)):
        return True

    return False


def build_sanitized_recovery_feedback(
    *,
    validation_result: Optional[ValidationResult] = None,
    generation_error: Optional[Exception] = None,
    execution_error: Optional[Exception] = None,
) -> str:
    """Construct safe, sanitized diagnostic guidance for the next generation attempt.

    SECURITY GUARANTEES:
    - Never includes previously generated candidate SQL.
    - Never includes database credentials, connection strings, or raw stack traces.
    - Contains only stable reason codes and safe diagnostic messages.
    """
    feedback_lines: List[str] = []

    if validation_result is not None:
        for reason in validation_result.reasons:
            code = reason.code
            if code == ValidationReasonCode.PARSE_ERROR:
                feedback_lines.append(
                    "PARSE_ERROR: The previous query had a syntax error. Please ensure valid Microsoft SQL Server (T-SQL) syntax."
                )
            elif code == ValidationReasonCode.UNKNOWN_TABLE:
                feedback_lines.append(
                    f"UNKNOWN_TABLE: {sanitize_message(reason.message)}. Please query only tables defined in the schema."
                )
            elif code == ValidationReasonCode.UNKNOWN_COLUMN:
                feedback_lines.append(
                    f"UNKNOWN_COLUMN: {sanitize_message(reason.message)}. Please query only valid columns defined in the schema."
                )
            elif code == ValidationReasonCode.EXCEEDS_MAX_ROWS:
                feedback_lines.append(
                    "EXCEEDS_MAX_ROWS: The query exceeds row limits. Please include a TOP clause (e.g. SELECT TOP 100 ...)."
                )
            elif code == ValidationReasonCode.UNSUPPORTED_QUERY_SHAPE:
                feedback_lines.append(
                    "UNSUPPORTED_QUERY_SHAPE: The query structure was rejected. Please generate a simpler read-only query."
                )
            elif code == ValidationReasonCode.EMPTY_SQL:
                feedback_lines.append(
                    "EMPTY_SQL: Generated SQL was empty. Please generate a non-empty read-only T-SQL query."
                )
            else:
                feedback_lines.append(
                    f"VALIDATION_REJECTED: {code.value}. Please generate a compliant read-only query."
                )

    elif generation_error is not None:
        if isinstance(generation_error, MalformedResponseError):
            feedback_lines.append(
                "MALFORMED_OUTPUT: Previous response was not valid structured JSON. "
                "Please return valid JSON conforming to the schema with 'status', 'sql', 'explanation', and 'assumptions'."
            )
        elif isinstance(generation_error, EmptySQLError):
            feedback_lines.append(
                "EMPTY_SQL: Generated SQL was empty. Please provide a non-empty read-only T-SQL query."
            )
        else:
            feedback_lines.append(
                "GENERATION_ERROR: Generation failed. Please provide a valid structured JSON response."
            )

    elif execution_error is not None:
        feedback_lines.append(
            "DATABASE_EXECUTION_ERROR: The query encountered a database execution error. "
            "Please check table/column names, data types, join conditions, and GROUP BY expressions."
        )

    return "\n".join(feedback_lines)


class QueryPilotWorkflow:
    """Orchestrates QueryPilot stages as an explicit LangGraph state machine.

    Maintains dependency injection seams for generators, engines, schemas,
    and introspectors to enable fully offline, mocked testing.
    """

    def __init__(
        self,
        generator: Optional[SQLGenerator] = None,
        engine: Optional[Engine] = None,
        schema: Optional[DatabaseSchema] = None,
        introspector: Optional[DatabaseIntrospector] = None,
        max_rows: Optional[int] = None,
        timeout_seconds: Optional[int] = None,
        dialect: str = "T-SQL",
        additional_instructions: Optional[str] = None,
    ):
        self._generator = generator
        self._engine = engine
        self._schema = schema
        self._introspector = introspector
        self.max_rows = max_rows
        self.timeout_seconds = timeout_seconds
        self.dialect = dialect
        self.additional_instructions = additional_instructions
        self._compiled_app: Optional[CompiledStateGraph] = None

    @property
    def generator(self) -> SQLGenerator:
        """Lazily initialize Gemini SQL generator if none was injected."""
        if self._generator is None:
            from query_pilot.sql.gemini_generator import GeminiSQLGenerator

            self._generator = GeminiSQLGenerator()
        return self._generator

    @property
    def engine(self) -> Engine:
        """Lazily initialize database engine if none was injected."""
        if self._engine is None:
            self._engine = create_db_engine()
        return self._engine

    def get_schema(self) -> DatabaseSchema:
        """Obtain model-facing schema, using injected schema or introspecting from engine."""
        if self._schema is not None:
            if not self._schema.is_model_facing:
                introspector = self._introspector or DatabaseIntrospector(engine=self.engine)
                self._schema = introspector.filter_model_facing(self._schema)
            return self._schema

        if self._introspector is None:
            self._introspector = DatabaseIntrospector(self.engine)
        self._schema = self._introspector.introspect_model_facing()
        return self._schema

    # =========================================================================
    # Explicit Graph Nodes
    # =========================================================================

    def acquire_schema(self, state: QueryPilotState) -> Dict[str, Any]:
        """Node 1: Acquire or verify model-facing database schema metadata."""
        try:
            schema = state.get("schema_context")
            if schema is None:
                schema = self.get_schema()
            elif not schema.is_model_facing:
                introspector = self._introspector or DatabaseIntrospector(engine=self.engine)
                schema = introspector.filter_model_facing(schema)

            return {"schema_context": schema}
        except Exception as exc:
            logger.error(f"Graph schema acquisition failed: {exc}")
            err_code, _ = classify_error(exc, PipelineStage.SCHEMA_ACQUISITION)
            return {
                "status": PipelineStatus.EXECUTION_FAILED,
                "error_category": ErrorCategory.SCHEMA_ACQUISITION_ERROR,
                "error_message": sanitize_message(f"Schema acquisition failed: {exc}"),
            }

    def generate_sql(self, state: QueryPilotState) -> Dict[str, Any]:
        """Node 2: Generate candidate SQL using question and schema context."""
        question = state["question"]
        schema = state.get("schema_context")
        if schema is None:
            return {
                "status": PipelineStatus.GENERATION_FAILED,
                "error_category": ErrorCategory.SQL_GENERATION_FAILED,
                "error_message": "Missing schema context for SQL generation.",
            }

        # Monotonically track attempts
        current_attempts = state.get("generation_attempts", 0) + 1
        retry_count = max(0, current_attempts - 1)
        can_retry = current_attempts < MAX_GENERATION_ATTEMPTS

        # Incorporate sanitized recovery feedback into instructions if present
        instructions = self.additional_instructions
        recovery_feedback = state.get("recovery_feedback")
        if recovery_feedback and recovery_feedback.strip():
            logger.info(
                "Generating SQL with recovery feedback (attempt %d of %d, request_id=%s)",
                current_attempts,
                MAX_GENERATION_ATTEMPTS,
                state.get("request_id"),
            )
            if instructions and instructions.strip():
                instructions = f"{instructions.strip()}\n\nRECOVERY INSTRUCTIONS:\n{recovery_feedback.strip()}"
            else:
                instructions = f"RECOVERY INSTRUCTIONS:\n{recovery_feedback.strip()}"

        request = SQLGenerationRequest(
            question=question,
            schema_context=schema,
            dialect=self.dialect,
            instructions=instructions,
        )

        try:
            gen_resp = self.generator.generate(request)
        except SQLGenerationError as exc:
            logger.warning(f"Graph SQL generation failed with controlled error: {exc}")
            is_rec = is_recoverable_generation_error(exc) and can_retry
            feedback = (
                build_sanitized_recovery_feedback(generation_error=exc)
                if is_rec
                else None
            )
            if is_rec:
                logger.info(
                    "Recovery scheduled for generation failure (attempt %d of %d, error=%s)",
                    current_attempts,
                    MAX_GENERATION_ATTEMPTS,
                    type(exc).__name__,
                )
            elif current_attempts >= MAX_GENERATION_ATTEMPTS:
                logger.warning(
                    "Recovery exhausted after %d attempts for generation error (%s)",
                    current_attempts,
                    type(exc).__name__,
                )
            return {
                "generation_attempts": current_attempts,
                "retry_count": retry_count,
                "recovery_feedback": feedback,
                "status": PipelineStatus.GENERATION_FAILED,
                "error_category": ErrorCategory.SQL_GENERATION_FAILED,
                "error_message": sanitize_message(str(exc)),
            }
        except Exception as exc:
            logger.error(f"Graph SQL generation encountered unexpected error: {exc}")
            return {
                "generation_attempts": current_attempts,
                "retry_count": retry_count,
                "recovery_feedback": None,
                "status": PipelineStatus.GENERATION_FAILED,
                "error_category": ErrorCategory.SQL_GENERATION_FAILED,
                "error_message": sanitize_message(f"SQL generation error: {exc}"),
            }

        if gen_resp.status == SQLGenerationStatus.UNSUPPORTED:
            logger.info(f"Question identified as unsupported by schema: {question}")
            return {
                "generation_attempts": current_attempts,
                "retry_count": retry_count,
                "recovery_feedback": None,
                "generation_response": gen_resp,
                "status": PipelineStatus.UNSUPPORTED,
                "error_category": ErrorCategory.UNSUPPORTED_QUESTION,
            }

        return {
            "generation_attempts": current_attempts,
            "retry_count": retry_count,
            "recovery_feedback": None,
            "generation_response": gen_resp,
            "status": None,
            "error_category": None,
            "error_message": None,
        }

    def validate_sql(self, state: QueryPilotState) -> Dict[str, Any]:
        """Node 3: Deterministically validate generated candidate SQL."""
        gen_resp = state.get("generation_response")
        candidate_sql = gen_resp.sql if gen_resp else None
        schema = state.get("schema_context")

        if not candidate_sql or schema is None:
            return {
                "status": PipelineStatus.VALIDATION_REJECTED,
                "error_category": ErrorCategory.VALIDATION_ERROR,
                "error_message": "Candidate SQL or schema context missing for validation.",
                "recovery_feedback": None,
            }

        try:
            val_result = validate_sql(
                sql=candidate_sql,
                schema=schema,
                max_rows=self.max_rows,
            )
        except Exception as exc:
            logger.error(f"Graph SQL validation encountered unexpected error: {exc}")
            return {
                "status": PipelineStatus.VALIDATION_REJECTED,
                "error_category": ErrorCategory.VALIDATION_ERROR,
                "error_message": sanitize_message(f"Validation failed with unexpected error: {exc}"),
                "recovery_feedback": None,
            }

        if not val_result.allowed:
            reasons_str = "; ".join(f"{r.code.value}: {r.message}" for r in val_result.reasons)
            logger.info(f"Candidate SQL rejected by validator: {reasons_str}")
            can_retry = state.get("generation_attempts", 0) < MAX_GENERATION_ATTEMPTS
            is_rec = is_recoverable_validation_result(val_result) and can_retry
            feedback = (
                build_sanitized_recovery_feedback(validation_result=val_result)
                if is_rec
                else None
            )
            if is_rec:
                reason_codes = [r.code.value for r in val_result.reasons]
                logger.info(
                    "Recovery scheduled for validation rejection (attempt %d of %d, reasons=%s)",
                    state.get("generation_attempts", 0),
                    MAX_GENERATION_ATTEMPTS,
                    reason_codes,
                )
            elif state.get("generation_attempts", 0) >= MAX_GENERATION_ATTEMPTS:
                logger.warning(
                    "Recovery exhausted after %d attempts for validation rejection",
                    state.get("generation_attempts", 0),
                )
            return {
                "validation_result": val_result,
                "recovery_feedback": feedback,
                "status": PipelineStatus.VALIDATION_REJECTED,
                "error_category": ErrorCategory.VALIDATION_REJECTED,
                "error_message": f"Validation rejected: {reasons_str}",
            }

        return {
            "validation_result": val_result,
            "recovery_feedback": None,
            "status": None,
            "error_category": None,
            "error_message": None,
        }

    def execute_sql(self, state: QueryPilotState) -> Dict[str, Any]:
        """Node 4: Execute validated SQL under read-only transaction safeguards."""
        val_result = state.get("validation_result")
        if val_result is None or val_result.allowed is not True:
            return {
                "status": PipelineStatus.VALIDATION_REJECTED,
                "error_category": ErrorCategory.VALIDATION_REJECTED,
                "error_message": "Execution blocked: query has not been validated and approved.",
                "recovery_feedback": None,
            }

        gen_resp = state.get("generation_response")
        candidate_sql = gen_resp.sql if gen_resp else None

        if not candidate_sql:
            return {
                "status": PipelineStatus.EXECUTION_FAILED,
                "error_category": ErrorCategory.EXECUTION_FAILED,
                "error_message": "No candidate SQL available for execution.",
                "recovery_feedback": None,
            }

        try:
            exec_result = execute_read_only(
                sql=candidate_sql,
                max_rows=self.max_rows,
                timeout_seconds=self.timeout_seconds,
                engine=self.engine,
            )
            return {
                "execution_result": exec_result,
                "status": PipelineStatus.SUCCESS,
                "recovery_feedback": None,
                "error_category": None,
                "error_message": None,
            }
        except QueryTimeoutError as exc:
            logger.warning(f"Read-only query execution timed out: {exc}")
            return {
                "status": PipelineStatus.EXECUTION_FAILED,
                "error_category": ErrorCategory.QUERY_TIMEOUT,
                "error_message": sanitize_message(f"Query timeout: {exc}"),
                "recovery_feedback": None,
            }
        except QueryExecutionError as exc:
            logger.warning(f"Read-only query execution failed: {exc}")
            can_retry = state.get("generation_attempts", 0) < MAX_GENERATION_ATTEMPTS
            is_rec = is_recoverable_execution_error(exc) and can_retry
            feedback = (
                build_sanitized_recovery_feedback(execution_error=exc)
                if is_rec
                else None
            )
            if is_rec:
                logger.info(
                    "Recovery scheduled for database execution error (attempt %d of %d)",
                    state.get("generation_attempts", 0),
                    MAX_GENERATION_ATTEMPTS,
                )
            elif state.get("generation_attempts", 0) >= MAX_GENERATION_ATTEMPTS:
                logger.warning(
                    "Recovery exhausted after %d attempts for database execution error",
                    state.get("generation_attempts", 0),
                )
            return {
                "status": PipelineStatus.EXECUTION_FAILED,
                "error_category": ErrorCategory.EXECUTION_FAILED,
                "error_message": sanitize_message(f"Execution error: {exc}"),
                "recovery_feedback": feedback,
            }
        except Exception as exc:
            logger.error(f"Unexpected error during query execution: {exc}")
            return {
                "status": PipelineStatus.EXECUTION_FAILED,
                "error_category": ErrorCategory.EXECUTION_FAILED,
                "error_message": sanitize_message(f"Execution error: {exc}"),
                "recovery_feedback": None,
            }

    def present_result(self, state: QueryPilotState) -> Dict[str, Any]:
        """Node 5: Convert state into deterministic user-facing presentation."""
        status = state.get("status") or PipelineStatus.SUCCESS
        gen_resp = state.get("generation_response")
        val_result = state.get("validation_result")
        exec_result = state.get("execution_result")

        pipe_res = PipelineResult(
            question=state["question"],
            status=status,
            generation_status=gen_resp.status if gen_resp else None,
            generated_sql=gen_resp.sql if gen_resp else None,
            generation_explanation=gen_resp.explanation if gen_resp else None,
            generation_assumptions=gen_resp.assumptions if gen_resp else [],
            validation_result=val_result,
            execution_result=exec_result,
            error_message=state.get("error_message"),
            request_id=state.get("request_id"),
        )

        presentation = format_pipeline_result(pipe_res)
        return {
            "presentation_result": presentation,
            "status": status,
        }

    # =========================================================================
    # Conditional Routing Logic
    # =========================================================================

    def route_after_schema(self, state: QueryPilotState) -> str:
        """Route to generate_sql on schema success; present_result on error."""
        if state.get("status") == PipelineStatus.EXECUTION_FAILED or state.get("error_category") is not None:
            return "present_result"
        return "generate_sql"

    def route_after_generation(self, state: QueryPilotState) -> str:
        """Route to validate_sql if answerable SQL exists; generate_sql if recoverable; present_result on termination."""
        status = state.get("status")
        if status == PipelineStatus.UNSUPPORTED:
            return "present_result"

        if state.get("recovery_feedback") is not None and state.get("generation_attempts", 0) < MAX_GENERATION_ATTEMPTS:
            return "generate_sql"

        if status == PipelineStatus.GENERATION_FAILED:
            return "present_result"

        gen_resp = state.get("generation_response")
        if (
            gen_resp
            and gen_resp.status == SQLGenerationStatus.ANSWERABLE
            and gen_resp.sql
            and gen_resp.sql.strip()
        ):
            return "validate_sql"

        return "present_result"

    def route_after_validation(self, state: QueryPilotState) -> str:
        """Route to execute_sql if validation allowed query; generate_sql if recoverable; present_result if terminal."""
        val_result = state.get("validation_result")
        if val_result and val_result.allowed is True:
            return "execute_sql"

        if state.get("recovery_feedback") is not None and state.get("generation_attempts", 0) < MAX_GENERATION_ATTEMPTS:
            return "generate_sql"

        return "present_result"

    def route_after_execution(self, state: QueryPilotState) -> str:
        """Route to present_result on success or terminal error; generate_sql if recoverable."""
        if state.get("status") == PipelineStatus.SUCCESS:
            return "present_result"

        if state.get("recovery_feedback") is not None and state.get("generation_attempts", 0) < MAX_GENERATION_ATTEMPTS:
            return "generate_sql"

        return "present_result"

    # =========================================================================
    # Graph Construction & Compilation
    # =========================================================================

    def compile(self) -> CompiledStateGraph:
        """Build and compile the LangGraph StateGraph, caching the compiled app."""
        if self._compiled_app is not None:
            return self._compiled_app

        builder = StateGraph(QueryPilotState)

        # Register nodes
        builder.add_node("acquire_schema", self.acquire_schema)
        builder.add_node("generate_sql", self.generate_sql)
        builder.add_node("validate_sql", self.validate_sql)
        builder.add_node("execute_sql", self.execute_sql)
        builder.add_node("present_result", self.present_result)

        # Entry point
        builder.set_entry_point("acquire_schema")

        # Conditional edges
        builder.add_conditional_edges(
            "acquire_schema",
            self.route_after_schema,
            {
                "generate_sql": "generate_sql",
                "present_result": "present_result",
            },
        )
        builder.add_conditional_edges(
            "generate_sql",
            self.route_after_generation,
            {
                "validate_sql": "validate_sql",
                "generate_sql": "generate_sql",
                "present_result": "present_result",
            },
        )
        builder.add_conditional_edges(
            "validate_sql",
            self.route_after_validation,
            {
                "execute_sql": "execute_sql",
                "generate_sql": "generate_sql",
                "present_result": "present_result",
            },
        )
        builder.add_conditional_edges(
            "execute_sql",
            self.route_after_execution,
            {
                "generate_sql": "generate_sql",
                "present_result": "present_result",
            },
        )

        # Terminal transitions
        builder.add_edge("present_result", END)

        self._compiled_app = builder.compile()
        return self._compiled_app

    def run(
        self,
        question: str | QueryPilotState,
        request_id: Optional[str] = None,
    ) -> QueryPilotState:
        """Execute the compiled workflow for a given question or state.

        Guarantees that caller-provided dictionary objects are never mutated.
        """
        if isinstance(question, str):
            state = create_initial_state(
                question,
                schema_context=self._schema,
                request_id=request_id,
            )
        else:
            state = dict(validate_initial_state(question))
            if request_id and "request_id" not in state:
                state["request_id"] = request_id

        app = self.compile()
        return app.invoke(state)


def create_query_graph(
    generator: Optional[SQLGenerator] = None,
    engine: Optional[Engine] = None,
    schema: Optional[DatabaseSchema] = None,
    introspector: Optional[DatabaseIntrospector] = None,
    max_rows: Optional[int] = None,
    timeout_seconds: Optional[int] = None,
    dialect: str = "T-SQL",
    additional_instructions: Optional[str] = None,
) -> CompiledStateGraph:
    """Construct and compile an executable QueryPilot LangGraph workflow.

    Args:
        generator: Optional SQLGenerator instance (defaults to GeminiSQLGenerator).
        engine: Optional SQLAlchemy Engine.
        schema: Optional pre-loaded model-facing DatabaseSchema.
        introspector: Optional DatabaseIntrospector for schema discovery.
        max_rows: Optional row limit for validation and execution.
        timeout_seconds: Optional execution timeout in seconds.
        dialect: SQL dialect for generation (default: 'T-SQL').
        additional_instructions: Optional context passed to generator.

    Returns:
        CompiledStateGraph ready for execution.
    """
    workflow = QueryPilotWorkflow(
        generator=generator,
        engine=engine,
        schema=schema,
        introspector=introspector,
        max_rows=max_rows,
        timeout_seconds=timeout_seconds,
        dialect=dialect,
        additional_instructions=additional_instructions,
    )
    return workflow.compile()


def run_query_graph(
    question: str | QueryPilotState,
    graph: Optional[CompiledStateGraph] = None,
    request_id: Optional[str] = None,
    **kwargs: Any,
) -> QueryPilotState:
    """Convenience public entry point to run a question through the QueryPilot graph.

    Guarantees that caller-provided state dictionary objects are never mutated.

    Args:
        question: Natural language question string or valid initial QueryPilotState.
        graph: Optional pre-compiled CompiledStateGraph instance.
        request_id: Optional correlation ID for the request.
        **kwargs: Arguments passed to QueryPilotWorkflow if graph is None.

    Returns:
        Final QueryPilotState after reaching END.
    """
    if graph is not None:
        if isinstance(question, str):
            state = create_initial_state(question, request_id=request_id)
        else:
            state = dict(validate_initial_state(question))
            if request_id and "request_id" not in state:
                state["request_id"] = request_id
        return graph.invoke(state)

    workflow = QueryPilotWorkflow(**kwargs)
    return workflow.run(question, request_id=request_id)
