"""Unified execution interface for QueryPilot (Task 1.20).

Provides a stable, application-facing facade allowing callers to execute
queries using either:
1. The sequential `QueryPipeline` (default).
2. The LangGraph `QueryPilotWorkflow`.

Both paths return the established public `PipelineResult` model with
consistent status codes, presentation results, and safety safeguards.
"""

from enum import Enum
import logging
from typing import Any, Callable, Dict, List, Optional, Union

from sqlalchemy.engine import Engine

from query_pilot.db.introspection import DatabaseIntrospector
from query_pilot.db.models import DatabaseSchema
from query_pilot.graph.state import QueryPilotState
from query_pilot.observability import ErrorCategory, PipelineObserver
from query_pilot.pipeline import PipelineResult, PipelineStatus, QueryPipeline
from query_pilot.presentation.formatter import format_pipeline_result
from query_pilot.sql.generation import SQLGenerationStatus, SQLGenerator

logger = logging.getLogger(__name__)


class ExecutionMode(str, Enum):
    """Execution implementations supported by the unified QueryPilot interface."""

    SEQUENTIAL = "sequential"
    GRAPH = "graph"


def normalize_execution_mode(mode: Union[ExecutionMode, str]) -> ExecutionMode:
    """Validate and normalize execution mode.

    Args:
        mode: ExecutionMode enum member or case-insensitive string ('sequential', 'graph').

    Returns:
        The validated ExecutionMode enum member.

    Raises:
        TypeError: If mode is not an ExecutionMode or string.
        ValueError: If mode is not a recognized execution mode value.
    """
    if isinstance(mode, ExecutionMode):
        return mode

    if not isinstance(mode, str):
        raise TypeError(
            f"Execution mode must be an ExecutionMode or string, got {type(mode).__name__}."
        )

    normalized = mode.strip().lower()
    for member in ExecutionMode:
        if member.value == normalized:
            return member

    valid_modes = [m.value for m in ExecutionMode]
    raise ValueError(
        f"Invalid execution mode: {mode!r}. Supported modes are: {valid_modes}."
    )


def graph_state_to_pipeline_result(state: QueryPilotState) -> PipelineResult:
    """Normalize a LangGraph final state into the public PipelineResult contract.

    Preserves question, request ID, status, generated SQL, explanation, assumptions,
    validation result, execution result, presentation result, and error details without
    fabricating results on failure paths.

    Args:
        state: Final QueryPilotState returned by the LangGraph workflow.

    Returns:
        Public PipelineResult instance.

    Raises:
        ValueError: If an unrecognized status value is encountered.
        TypeError: If status is of an invalid type.
    """
    question = state.get("question") or ""
    raw_status = state.get("status")

    if raw_status is not None:
        if isinstance(raw_status, PipelineStatus):
            status = raw_status
        elif isinstance(raw_status, str):
            try:
                status = PipelineStatus(raw_status)
            except ValueError:
                try:
                    status = PipelineStatus[raw_status]
                except KeyError:
                    raise ValueError(f"Unrecognized pipeline status: {raw_status!r}")
        else:
            raise TypeError(
                f"Status must be a PipelineStatus or str, got {type(raw_status).__name__}."
            )
    elif state.get("error_category") is not None or state.get("error_message") is not None:
        # Prevent silently defaulting an error state to SUCCESS
        err_cat = state.get("error_category")
        if err_cat == ErrorCategory.UNSUPPORTED_QUESTION:
            status = PipelineStatus.UNSUPPORTED
        elif err_cat in (ErrorCategory.VALIDATION_REJECTED, ErrorCategory.VALIDATION_ERROR):
            status = PipelineStatus.VALIDATION_REJECTED
        elif err_cat == ErrorCategory.SQL_GENERATION_FAILED:
            status = PipelineStatus.GENERATION_FAILED
        else:
            status = PipelineStatus.EXECUTION_FAILED
    else:
        status = PipelineStatus.SUCCESS

    gen_resp = state.get("generation_response")
    val_result = state.get("validation_result")
    exec_result = state.get("execution_result")
    presentation = state.get("presentation_result")
    err_msg = state.get("error_message")

    # Defense-in-depth: enforce invariant that failure paths never retain fabricated results
    if status in (PipelineStatus.UNSUPPORTED, PipelineStatus.CLARIFICATION_NEEDED):
        candidate_sql = None
        val_result = None
        exec_result = None
    elif status == PipelineStatus.GENERATION_FAILED:
        candidate_sql = None
        val_result = None
        exec_result = None
    elif status == PipelineStatus.VALIDATION_REJECTED:
        candidate_sql = gen_resp.sql if gen_resp else None
        exec_result = None
    elif status == PipelineStatus.EXECUTION_FAILED:
        candidate_sql = gen_resp.sql if gen_resp else None
        exec_result = None
    elif status == PipelineStatus.VALIDATED_ONLY:
        candidate_sql = gen_resp.sql if gen_resp else None
        exec_result = None
    else:
        # SUCCESS
        candidate_sql = gen_resp.sql if gen_resp else None

    pipe_res = PipelineResult(
        question=question,
        status=status,
        generation_status=gen_resp.status if gen_resp else None,
        generated_sql=candidate_sql,
        generation_explanation=gen_resp.explanation if gen_resp else None,
        generation_assumptions=gen_resp.assumptions if gen_resp else [],
        validation_result=val_result,
        execution_result=exec_result,
        error_message=err_msg,
        request_id=state.get("request_id"),
        presentation=presentation,
        grounded_answer=state.get("grounded_answer"),
        telemetry=state.get("telemetry"),
        clarification_prompt=gen_resp.clarification_prompt if gen_resp else None,
        clarification_options=gen_resp.clarification_options if gen_resp else [],
    )

    # If presentation was not already generated, format it deterministically
    if pipe_res.presentation is None:
        pipe_res.presentation = format_pipeline_result(pipe_res)

    return pipe_res


class QueryRunner:
    """Unified application-facing execution interface for QueryPilot.

    Coordinates execution through either the sequential QueryPipeline or the
    LangGraph QueryPilotWorkflow. Defaults strictly to sequential execution.

    Maintains dependency injection seams for offline testing without live Gemini
    or database calls. Unselected implementations are never instantiated or executed.
    """

    def __init__(
        self,
        mode: Union[ExecutionMode, str] = ExecutionMode.SEQUENTIAL,
        generator: Optional[SQLGenerator] = None,
        engine: Optional[Engine] = None,
        schema: Optional[DatabaseSchema] = None,
        introspector: Optional[DatabaseIntrospector] = None,
        max_rows: Optional[int] = None,
        timeout_seconds: Optional[int] = None,
        dialect: str = "T-SQL",
        additional_instructions: Optional[str] = None,
        enable_observability: bool = True,
        observer_factory: Optional[Callable[..., PipelineObserver]] = None,
        synthesizer: Optional[Any] = None,
    ):
        """Initialize the unified QueryRunner.

        Args:
            mode: Default execution mode ('sequential' or 'graph'). Defaults to 'sequential'.
            generator: Optional SQLGenerator implementation.
            engine: Optional SQLAlchemy Engine.
            schema: Optional pre-loaded model-facing DatabaseSchema.
            introspector: Optional DatabaseIntrospector.
            max_rows: Optional row limit for queries.
            timeout_seconds: Optional query execution timeout in seconds.
            dialect: SQL dialect for generation (default: 'T-SQL').
            additional_instructions: Optional context passed to generator prompt.
            enable_observability: Whether structured lifecycle observability is enabled (sequential mode).
            observer_factory: Optional custom factory for PipelineObserver instances.
            synthesizer: Optional shared AnswerSynthesisService instance.

        Raises:
            ValueError: If mode is not a supported execution mode.
            TypeError: If mode is not a string or ExecutionMode.
        """
        self.default_mode = normalize_execution_mode(mode)
        self._generator = generator
        self._engine = engine
        self._schema = schema
        self._introspector = introspector
        self.max_rows = max_rows
        self.timeout_seconds = timeout_seconds
        self.dialect = dialect
        self.additional_instructions = additional_instructions
        self.enable_observability = enable_observability
        self.observer_factory = observer_factory
        self.synthesizer = synthesizer

    def _create_pipeline(self) -> QueryPipeline:
        """Instantiate the sequential QueryPipeline with configured dependencies."""
        return QueryPipeline(
            generator=self._generator,
            engine=self._engine,
            schema=self._schema,
            introspector=self._introspector,
            max_rows=self.max_rows,
            timeout_seconds=self.timeout_seconds,
            dialect=self.dialect,
            additional_instructions=self.additional_instructions,
            enable_observability=self.enable_observability,
            observer_factory=self.observer_factory,
            synthesizer=self.synthesizer,
        )

    def _create_workflow(self) -> Any:
        """Instantiate the LangGraph QueryPilotWorkflow with configured dependencies."""
        from query_pilot.graph.workflow import QueryPilotWorkflow

        return QueryPilotWorkflow(
            generator=self._generator,
            engine=self._engine,
            schema=self._schema,
            introspector=self._introspector,
            max_rows=self.max_rows,
            timeout_seconds=self.timeout_seconds,
            dialect=self.dialect,
            additional_instructions=self.additional_instructions,
            synthesizer=self.synthesizer,
        )

    def run(
        self,
        question: str,
        request_id: Optional[str] = None,
        mode: Optional[Union[ExecutionMode, str]] = None,
        conversation_context: Optional[List[Any]] = None,
        execution_mode: str = "execute",
    ) -> PipelineResult:
        """Execute a natural-language question using the selected execution path.

        Args:
            question: The user natural-language query string.
            request_id: Optional correlation ID for structured observability.
            mode: Optional per-run execution mode override. If None, uses default_mode.
            conversation_context: Optional caller-supplied prior-turn context.
            execution_mode: 'execute' (default) or 'sql_only'.

        Returns:
            Structured PipelineResult.

        Raises:
            ValueError: If an invalid execution mode is requested, or if question is empty.
            TypeError: If mode is not an ExecutionMode or string.
        """
        if not isinstance(question, str):
            raise TypeError(f"Question must be a string, got {type(question).__name__}.")
        if not question.strip():
            raise ValueError("Question cannot be empty or whitespace.")

        selected_mode = normalize_execution_mode(mode) if mode is not None else self.default_mode

        run_kwargs: Dict[str, Any] = {}
        if conversation_context is not None:
            run_kwargs["conversation_context"] = conversation_context
        if execution_mode != "execute":
            run_kwargs["execution_mode"] = execution_mode

        if selected_mode == ExecutionMode.SEQUENTIAL:
            pipeline = self._create_pipeline()
            return pipeline.run(
                question,
                request_id=request_id,
                **run_kwargs,
            )

        elif selected_mode == ExecutionMode.GRAPH:
            workflow = self._create_workflow()
            state = workflow.run(
                question,
                request_id=request_id,
                **run_kwargs,
            )
            return graph_state_to_pipeline_result(state)

        # Unreachable due to normalize_execution_mode validation, but defensive fallback
        raise ValueError(f"Unhandled execution mode: {selected_mode}")


def run_query(
    question: str,
    mode: Union[ExecutionMode, str] = ExecutionMode.SEQUENTIAL,
    request_id: Optional[str] = None,
    conversation_context: Optional[List[Any]] = None,
    execution_mode: str = "execute",
    runner: Optional[QueryRunner] = None,
    **kwargs: Any,
) -> PipelineResult:
    """Convenience public entry point to run a question via the unified QueryPilot interface.

    Defaults strictly to sequential execution.

    Args:
        question: Natural-language user question.
        mode: Execution mode ('sequential' or 'graph'). Defaults to 'sequential'.
        request_id: Optional correlation ID for the request.
        conversation_context: Optional caller-supplied prior-turn context.
        execution_mode: 'execute' (default) or 'sql_only'.
        runner: Optional pre-configured QueryRunner instance.
        **kwargs: Arguments passed to QueryRunner if runner is None.

    Returns:
        Structured PipelineResult.
    """
    if runner is not None:
        return runner.run(
            question,
            request_id=request_id,
            mode=mode,
            conversation_context=conversation_context,
            execution_mode=execution_mode,
        )

    active_runner = QueryRunner(mode=mode, **kwargs)
    return active_runner.run(
        question,
        request_id=request_id,
        conversation_context=conversation_context,
        execution_mode=execution_mode,
    )
