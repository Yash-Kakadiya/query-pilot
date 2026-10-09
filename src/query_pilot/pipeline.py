"""Controlled end-to-end Text-to-SQL pipeline for QueryPilot.

Orchestrates the deterministic boundary:
    Natural-language question
            ↓
    SQL generation (SQLGenerator)
            ↓
    Deterministic SQL validation (validate_sql)
            ↓
    Conditional safe read-only execution (execute_read_only)
            ↓
    Structured pipeline result (PipelineResult)

Strict safety principles:
- Generator never executes SQL.
- Validation ALWAYS happens before execution.
- Invalid or unsafe SQL is stopped immediately without touching the database.
- Read-only executor operates strictly under session context against GradeSense_Local.
- No autonomous retries, conversational memory, or answer generation in this layer.
"""

from enum import Enum
import logging
from typing import Any, Callable, Dict, List, Optional
from pydantic import BaseModel, Field
from sqlalchemy.engine import Engine

from query_pilot.db.connection import create_db_engine
from query_pilot.db.executor import (
    QueryExecutionError,
    QueryResult,
    execute_read_only,
)
from query_pilot.db.introspection import DatabaseIntrospector
from query_pilot.db.models import DatabaseSchema
from query_pilot.observability import (
    ErrorCategory,
    PipelineObserver,
    PipelineStage,
    StageStatus,
    classify_error,
)
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


class PipelineStatus(str, Enum):
    """Execution status codes for the QueryPilot pipeline."""

    SUCCESS = "SUCCESS"
    UNSUPPORTED = "UNSUPPORTED"
    GENERATION_FAILED = "GENERATION_FAILED"
    VALIDATION_REJECTED = "VALIDATION_REJECTED"
    EXECUTION_FAILED = "EXECUTION_FAILED"


class PipelineResult(BaseModel):
    """Structured, typed result returned by the Text-to-SQL pipeline."""

    model_config = {"arbitrary_types_allowed": True}

    question: str = Field(
        ...,
        description="The original user natural-language question.",
    )
    status: PipelineStatus = Field(
        ...,
        description="The terminal status code of the pipeline.",
    )
    generation_status: Optional[SQLGenerationStatus] = Field(
        default=None,
        description="The generation status from the model ('answerable' or 'unsupported').",
    )
    generated_sql: Optional[str] = Field(
        default=None,
        description="The candidate SQL produced by the generator, if generation succeeded and was answerable.",
    )
    generation_explanation: Optional[str] = Field(
        default=None,
        description="Optional natural-language explanation from the generator.",
    )
    generation_assumptions: List[str] = Field(
        default_factory=list,
        description="Optional assumptions noted by the generator during query formation.",
    )
    validation_result: Optional[ValidationResult] = Field(
        default=None,
        description="Deterministic validation result from the SQL policy layer.",
    )
    execution_result: Optional[QueryResult] = Field(
        default=None,
        description="Safe read-only execution result, populated only if query was executed.",
    )
    error_message: Optional[str] = Field(
        default=None,
        description="Controlled error details if the pipeline encountered a failure.",
    )
    request_id: Optional[str] = Field(
        default=None,
        description="Correlated unique request ID.",
    )
    duration_ms: Optional[float] = Field(
        default=None,
        description="Total pipeline duration in milliseconds.",
    )
    stage_durations: Dict[str, float] = Field(
        default_factory=dict,
        description="Stage-level durations in milliseconds.",
    )
    presentation: Optional[Any] = Field(
        default=None,
        description="Deterministic presentation result.",
    )

    @property
    def is_success(self) -> bool:
        """Return True if pipeline successfully completed generation, validation, and execution."""
        return self.status == PipelineStatus.SUCCESS

    @property
    def is_unsupported(self) -> bool:
        """Return True if question was identified as unsupported by the database schema."""
        return self.status == PipelineStatus.UNSUPPORTED

    def to_dict(self) -> Dict[str, Any]:
        """Convert pipeline result to a serializable dictionary."""
        d = {
            "question": self.question,
            "status": self.status.value,
            "generation_status": self.generation_status.value if self.generation_status else None,
            "generated_sql": self.generated_sql,
            "generation_explanation": self.generation_explanation,
            "generation_assumptions": self.generation_assumptions,
            "validation_result": self.validation_result.to_dict() if self.validation_result else None,
            "execution_result": self.execution_result.to_dict() if self.execution_result else None,
            "error_message": self.error_message,
        }
        if self.request_id is not None:
            d["request_id"] = self.request_id
        if self.duration_ms is not None:
            d["duration_ms"] = self.duration_ms
        if self.stage_durations:
            d["stage_durations"] = self.stage_durations
        return d

    def present(self) -> Any:
        """Convert this pipeline result into a deterministic presentation representation."""
        if self.presentation is not None:
            return self.presentation
        from query_pilot.presentation.formatter import format_pipeline_result

        return format_pipeline_result(self)




class QueryPipeline:
    """Thin orchestration pipeline coordinating SQL generation, validation, and execution.

    Enforces the strict security boundary:
        generate -> validate -> if allowed: execute else: stop
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
        enable_observability: bool = True,
        observer_factory: Optional[Callable[..., PipelineObserver]] = None,
    ):
        """Initialize the pipeline with configurable components and dependency injection.

        Args:
            generator: SQLGenerator implementation (defaults to GeminiSQLGenerator).
            engine: SQLAlchemy Engine connected to GradeSense_Local.
            schema: Optional pre-loaded model-facing DatabaseSchema.
            introspector: Optional DatabaseIntrospector for schema discovery.
            max_rows: Optional row limit for validation and execution.
            timeout_seconds: Optional execution timeout in seconds.
            dialect: SQL dialect for generation (default: 'T-SQL').
            additional_instructions: Optional context passed to generator prompt.
            enable_observability: Whether structured lifecycle observability is enabled.
            observer_factory: Optional custom factory for creating PipelineObserver instances.
        """
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

    @property
    def generator(self) -> SQLGenerator:
        """Lazily initialize default Gemini SQL generator if none was injected."""
        if self._generator is None:
            from query_pilot.sql.gemini_generator import GeminiSQLGenerator
            self._generator = GeminiSQLGenerator()
        return self._generator

    @property
    def engine(self) -> Engine:
        """Lazily initialize default database engine if none was injected."""
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

    def run(self, question: str, request_id: Optional[str] = None) -> PipelineResult:
        """Execute end-to-end question processing through strict pipeline stages.

        Stage A: Schema acquisition
        Stage B: SQL generation
        Stage C: Deterministic validation
        Stage D: Conditional execution (only if validated)
        Stage E: Result presentation
        Stage F: Pipeline completion

        Args:
            question: The natural-language user question.
            request_id: Optional correlation ID for the request.

        Returns:
            PipelineResult representing the outcome.
        """
        observer = (
            self.observer_factory(request_id=request_id, enabled=self.enable_observability)
            if self.observer_factory
            else PipelineObserver(request_id=request_id, enabled=self.enable_observability)
        )
        observer.start_pipeline()

        def _finalize(
            pipe_res: PipelineResult,
            terminal_status: str,
            stage_status: Optional[str] = None,
        ) -> PipelineResult:
            # Stage: Result presentation
            observer.start_stage(PipelineStage.RESULT_PRESENTATION)
            try:
                from query_pilot.presentation.formatter import format_pipeline_result

                pipe_res.request_id = observer.request_id
                pres = format_pipeline_result(pipe_res)
                observer.end_stage(PipelineStage.RESULT_PRESENTATION, StageStatus.SUCCEEDED)
            except Exception as pres_exc:
                pres_code, pres_type = classify_error(pres_exc, PipelineStage.RESULT_PRESENTATION)
                observer.end_stage(
                    PipelineStage.RESULT_PRESENTATION,
                    StageStatus.FAILED,
                    error_code=pres_code,
                    error_type=pres_type,
                )
                pres = None

            # Stage: Pipeline completion
            observer.end_pipeline(final_status=terminal_status, status=stage_status)
            pipe_res.request_id = observer.request_id
            pipe_res.duration_ms = observer.total_duration_ms
            pipe_res.stage_durations = dict(observer.stage_durations)
            pipe_res.presentation = pres
            return pipe_res

        # Stage A: Schema acquisition
        observer.start_stage(PipelineStage.SCHEMA_ACQUISITION)
        try:
            schema = self.get_schema()
            observer.end_stage(PipelineStage.SCHEMA_ACQUISITION, StageStatus.SUCCEEDED)
        except Exception as exc:
            err_code, err_type = classify_error(exc, PipelineStage.SCHEMA_ACQUISITION)
            observer.end_stage(
                PipelineStage.SCHEMA_ACQUISITION,
                StageStatus.FAILED,
                error_code=err_code,
                error_type=err_type,
            )
            logger.error(f"Pipeline schema acquisition failed: {exc}")
            return _finalize(
                PipelineResult(
                    question=question,
                    status=PipelineStatus.EXECUTION_FAILED,
                    error_message=f"Schema acquisition failed: {exc}",
                ),
                PipelineStatus.EXECUTION_FAILED.value,
                StageStatus.FAILED,
            )

        # Stage B: SQL generation
        observer.start_stage(PipelineStage.SQL_GENERATION)
        try:
            request = SQLGenerationRequest(
                question=question,
                schema_context=schema,
                dialect=self.dialect,
                instructions=self.additional_instructions,
            )
            gen_resp = self.generator.generate(request)
        except SQLGenerationError as exc:
            err_code, err_type = classify_error(exc, PipelineStage.SQL_GENERATION)
            observer.end_stage(
                PipelineStage.SQL_GENERATION,
                StageStatus.FAILED,
                error_code=err_code,
                error_type=err_type,
            )
            logger.warning(f"SQL generation failed with controlled error: {exc}")
            return _finalize(
                PipelineResult(
                    question=question,
                    status=PipelineStatus.GENERATION_FAILED,
                    error_message=str(exc),
                ),
                PipelineStatus.GENERATION_FAILED.value,
                StageStatus.FAILED,
            )
        except Exception as exc:
            err_code, err_type = classify_error(exc, PipelineStage.SQL_GENERATION)
            observer.end_stage(
                PipelineStage.SQL_GENERATION,
                StageStatus.FAILED,
                error_code=err_code,
                error_type=err_type,
            )
            logger.error(f"Unexpected error during SQL generation: {exc}")
            return _finalize(
                PipelineResult(
                    question=question,
                    status=PipelineStatus.GENERATION_FAILED,
                    error_message=f"SQL generation error: {exc}",
                ),
                PipelineStatus.GENERATION_FAILED.value,
                StageStatus.FAILED,
            )

        candidate_sql = gen_resp.sql
        explanation = gen_resp.explanation
        assumptions = gen_resp.assumptions or []

        # Stage B.1: Check if question is unsupported by schema
        if gen_resp.status == SQLGenerationStatus.UNSUPPORTED:
            observer.end_stage(
                PipelineStage.SQL_GENERATION,
                StageStatus.UNSUPPORTED,
                error_code=ErrorCategory.UNSUPPORTED_QUESTION.value,
            )
            logger.info(f"Question is unsupported by database schema: {question}")
            return _finalize(
                PipelineResult(
                    question=question,
                    status=PipelineStatus.UNSUPPORTED,
                    generation_status=gen_resp.status,
                    generated_sql=None,
                    generation_explanation=explanation,
                    generation_assumptions=assumptions,
                    validation_result=None,
                    execution_result=None,
                ),
                PipelineStatus.UNSUPPORTED.value,
                StageStatus.UNSUPPORTED,
            )

        observer.end_stage(PipelineStage.SQL_GENERATION, StageStatus.SUCCEEDED)

        # Stage C: Deterministic validation
        observer.start_stage(PipelineStage.SQL_VALIDATION)
        try:
            val_result = validate_sql(
                sql=candidate_sql,
                schema=schema,
                max_rows=self.max_rows,
            )
        except Exception as exc:
            err_code, err_type = classify_error(exc, PipelineStage.SQL_VALIDATION)
            observer.end_stage(
                PipelineStage.SQL_VALIDATION,
                StageStatus.FAILED,
                error_code=err_code,
                error_type=err_type,
            )
            logger.error(f"Unexpected error during SQL validation: {exc}")
            return _finalize(
                PipelineResult(
                    question=question,
                    status=PipelineStatus.VALIDATION_REJECTED,
                    generation_status=gen_resp.status,
                    generated_sql=candidate_sql,
                    generation_explanation=explanation,
                    generation_assumptions=assumptions,
                    error_message=f"Validation failed with unexpected error: {exc}",
                ),
                PipelineStatus.VALIDATION_REJECTED.value,
                StageStatus.FAILED,
            )

        if not val_result.allowed:
            reasons_str = "; ".join(f"{r.code.value}: {r.message}" for r in val_result.reasons)
            observer.end_stage(
                PipelineStage.SQL_VALIDATION,
                StageStatus.REJECTED,
                error_code=ErrorCategory.VALIDATION_REJECTED.value,
                rejection_codes=val_result.reason_codes,
            )
            logger.info(f"SQL validation rejected candidate SQL: {reasons_str}")
            return _finalize(
                PipelineResult(
                    question=question,
                    status=PipelineStatus.VALIDATION_REJECTED,
                    generation_status=gen_resp.status,
                    generated_sql=candidate_sql,
                    generation_explanation=explanation,
                    generation_assumptions=assumptions,
                    validation_result=val_result,
                    error_message=f"Validation rejected: {reasons_str}",
                ),
                PipelineStatus.VALIDATION_REJECTED.value,
                StageStatus.REJECTED,
            )

        observer.end_stage(PipelineStage.SQL_VALIDATION, StageStatus.SUCCEEDED)

        # Stage D: Conditional execution (ONLY if val_result.allowed is True)
        observer.start_stage(PipelineStage.READ_ONLY_EXECUTION)
        try:
            exec_result = execute_read_only(
                sql=candidate_sql,
                max_rows=self.max_rows,
                timeout_seconds=self.timeout_seconds,
                engine=self.engine,
            )
            observer.end_stage(
                PipelineStage.READ_ONLY_EXECUTION,
                StageStatus.SUCCEEDED,
                row_count=exec_result.row_count,
                truncated=exec_result.truncated,
            )
        except QueryExecutionError as exc:
            err_code, err_type = classify_error(exc, PipelineStage.READ_ONLY_EXECUTION)
            observer.end_stage(
                PipelineStage.READ_ONLY_EXECUTION,
                StageStatus.FAILED,
                error_code=err_code,
                error_type=err_type,
            )
            logger.warning(f"Read-only query execution failed: {exc}")
            return _finalize(
                PipelineResult(
                    question=question,
                    status=PipelineStatus.EXECUTION_FAILED,
                    generation_status=gen_resp.status,
                    generated_sql=candidate_sql,
                    generation_explanation=explanation,
                    generation_assumptions=assumptions,
                    validation_result=val_result,
                    error_message=f"Execution error: {exc}",
                ),
                PipelineStatus.EXECUTION_FAILED.value,
                StageStatus.FAILED,
            )
        except Exception as exc:
            err_code, err_type = classify_error(exc, PipelineStage.READ_ONLY_EXECUTION)
            observer.end_stage(
                PipelineStage.READ_ONLY_EXECUTION,
                StageStatus.FAILED,
                error_code=err_code,
                error_type=err_type,
            )
            logger.error(f"Unexpected error during query execution: {exc}")
            return _finalize(
                PipelineResult(
                    question=question,
                    status=PipelineStatus.EXECUTION_FAILED,
                    generation_status=gen_resp.status,
                    generated_sql=candidate_sql,
                    generation_explanation=explanation,
                    generation_assumptions=assumptions,
                    validation_result=val_result,
                    error_message=f"Execution error: {exc}",
                ),
                PipelineStatus.EXECUTION_FAILED.value,
                StageStatus.FAILED,
            )

        # Stage E: Structured pipeline result
        return _finalize(
            PipelineResult(
                question=question,
                status=PipelineStatus.SUCCESS,
                generation_status=gen_resp.status,
                generated_sql=candidate_sql,
                generation_explanation=explanation,
                generation_assumptions=assumptions,
                validation_result=val_result,
                execution_result=exec_result,
            ),
            PipelineStatus.SUCCESS.value,
            StageStatus.SUCCEEDED,
        )


def run_question(
    question: str,
    pipeline: Optional[QueryPipeline] = None,
    request_id: Optional[str] = None,
    **kwargs,
) -> PipelineResult:
    """Convenience helper to run a question through the QueryPilot pipeline.

    Args:
        question: User natural-language question.
        pipeline: Optional preconfigured QueryPipeline instance.
        request_id: Optional correlation ID for the request.
        **kwargs: Arguments passed to QueryPipeline constructor if pipeline is None.

    Returns:
        PipelineResult with structured execution data or controlled error.
    """
    pipe = pipeline or QueryPipeline(**kwargs)
    return pipe.run(question, request_id=request_id)

