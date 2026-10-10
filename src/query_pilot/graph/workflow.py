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
from typing import Any, Dict, Optional

from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph
from sqlalchemy.engine import Engine

from query_pilot.db.connection import create_db_engine
from query_pilot.db.executor import (
    QueryExecutionError,
    QueryResult,
    execute_read_only,
)
from query_pilot.db.introspection import DatabaseIntrospector
from query_pilot.db.models import DatabaseSchema
from query_pilot.graph.state import (
    QueryPilotState,
    create_initial_state,
    validate_initial_state,
)
from query_pilot.observability import ErrorCategory, PipelineStage, classify_error
from query_pilot.pipeline import PipelineResult, PipelineStatus
from query_pilot.presentation.formatter import format_pipeline_result, sanitize_message
from query_pilot.sql.generation import (
    SQLGenerationError,
    SQLGenerationRequest,
    SQLGenerationResponse,
    SQLGenerationStatus,
    SQLGenerator,
)
from query_pilot.sql.models import ValidationResult
from query_pilot.sql.validator import validate_sql

logger = logging.getLogger(__name__)


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

        request = SQLGenerationRequest(
            question=question,
            schema_context=schema,
            dialect=self.dialect,
            instructions=self.additional_instructions,
        )

        try:
            gen_resp = self.generator.generate(request)
        except SQLGenerationError as exc:
            logger.warning(f"Graph SQL generation failed with controlled error: {exc}")
            return {
                "status": PipelineStatus.GENERATION_FAILED,
                "error_category": ErrorCategory.SQL_GENERATION_FAILED,
                "error_message": sanitize_message(str(exc)),
            }
        except Exception as exc:
            logger.error(f"Graph SQL generation encountered unexpected error: {exc}")
            return {
                "status": PipelineStatus.GENERATION_FAILED,
                "error_category": ErrorCategory.SQL_GENERATION_FAILED,
                "error_message": sanitize_message(f"SQL generation error: {exc}"),
            }

        if gen_resp.status == SQLGenerationStatus.UNSUPPORTED:
            logger.info(f"Question identified as unsupported by schema: {question}")
            return {
                "generation_response": gen_resp,
                "status": PipelineStatus.UNSUPPORTED,
                "error_category": ErrorCategory.UNSUPPORTED_QUESTION,
            }

        return {"generation_response": gen_resp}

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
            }

        if not val_result.allowed:
            reasons_str = "; ".join(f"{r.code.value}: {r.message}" for r in val_result.reasons)
            logger.info(f"Candidate SQL rejected by validator: {reasons_str}")
            return {
                "validation_result": val_result,
                "status": PipelineStatus.VALIDATION_REJECTED,
                "error_category": ErrorCategory.VALIDATION_REJECTED,
                "error_message": f"Validation rejected: {reasons_str}",
            }

        return {"validation_result": val_result}

    def execute_sql(self, state: QueryPilotState) -> Dict[str, Any]:
        """Node 4: Execute validated SQL under read-only transaction safeguards."""
        val_result = state.get("validation_result")
        if val_result is None or val_result.allowed is not True:
            return {
                "status": PipelineStatus.VALIDATION_REJECTED,
                "error_category": ErrorCategory.VALIDATION_REJECTED,
                "error_message": "Execution blocked: query has not been validated and approved.",
            }

        gen_resp = state.get("generation_response")
        candidate_sql = gen_resp.sql if gen_resp else None

        if not candidate_sql:
            return {
                "status": PipelineStatus.EXECUTION_FAILED,
                "error_category": ErrorCategory.EXECUTION_FAILED,
                "error_message": "No candidate SQL available for execution.",
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
            }
        except QueryExecutionError as exc:
            logger.warning(f"Read-only query execution failed: {exc}")
            return {
                "status": PipelineStatus.EXECUTION_FAILED,
                "error_category": ErrorCategory.EXECUTION_FAILED,
                "error_message": sanitize_message(f"Execution error: {exc}"),
            }
        except Exception as exc:
            logger.error(f"Unexpected error during query execution: {exc}")
            return {
                "status": PipelineStatus.EXECUTION_FAILED,
                "error_category": ErrorCategory.EXECUTION_FAILED,
                "error_message": sanitize_message(f"Execution error: {exc}"),
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
        """Route to validate_sql if answerable SQL exists; present_result on failure/unsupported."""
        status = state.get("status")
        if status in (PipelineStatus.UNSUPPORTED, PipelineStatus.GENERATION_FAILED):
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
        """Route to execute_sql if validation allowed query; present_result if rejected."""
        val_result = state.get("validation_result")
        if val_result and val_result.allowed is True:
            return "execute_sql"
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
                "present_result": "present_result",
            },
        )
        builder.add_conditional_edges(
            "validate_sql",
            self.route_after_validation,
            {
                "execute_sql": "execute_sql",
                "present_result": "present_result",
            },
        )

        # Terminal transitions
        builder.add_edge("execute_sql", "present_result")
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
