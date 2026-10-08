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
from typing import Any, Dict, List, Optional
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
        return {
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
        """
        self._generator = generator
        self._engine = engine
        self._schema = schema
        self._introspector = introspector
        self.max_rows = max_rows
        self.timeout_seconds = timeout_seconds
        self.dialect = dialect
        self.additional_instructions = additional_instructions

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

    def run(self, question: str) -> PipelineResult:
        """Execute end-to-end question processing through strict pipeline stages.

        Stage A: Schema acquisition
        Stage B: SQL generation
        Stage C: Deterministic validation
        Stage D: Conditional execution (only if validated)
        Stage E: Structured pipeline result

        Args:
            question: The natural-language user question.

        Returns:
            PipelineResult representing the outcome.
        """
        # Stage A: Schema acquisition
        try:
            schema = self.get_schema()
        except Exception as exc:
            logger.error(f"Pipeline schema acquisition failed: {exc}")
            return PipelineResult(
                question=question,
                status=PipelineStatus.EXECUTION_FAILED,
                error_message=f"Schema acquisition failed: {exc}",
            )

        # Stage B: SQL generation
        try:
            request = SQLGenerationRequest(
                question=question,
                schema_context=schema,
                dialect=self.dialect,
                instructions=self.additional_instructions,
            )
            gen_resp = self.generator.generate(request)
        except SQLGenerationError as exc:
            logger.warning(f"SQL generation failed with controlled error: {exc}")
            return PipelineResult(
                question=question,
                status=PipelineStatus.GENERATION_FAILED,
                error_message=str(exc),
            )
        except Exception as exc:
            logger.error(f"Unexpected error during SQL generation: {exc}")
            return PipelineResult(
                question=question,
                status=PipelineStatus.GENERATION_FAILED,
                error_message=f"SQL generation error: {exc}",
            )

        candidate_sql = gen_resp.sql
        explanation = gen_resp.explanation
        assumptions = gen_resp.assumptions or []

        # Stage B.1: Check if question is unsupported by schema
        if gen_resp.status == SQLGenerationStatus.UNSUPPORTED:
            logger.info(f"Question is unsupported by database schema: {question}")
            return PipelineResult(
                question=question,
                status=PipelineStatus.UNSUPPORTED,
                generation_status=gen_resp.status,
                generated_sql=None,
                generation_explanation=explanation,
                generation_assumptions=assumptions,
                validation_result=None,
                execution_result=None,
            )

        # Stage C: Deterministic validation
        try:
            val_result = validate_sql(
                sql=candidate_sql,
                schema=schema,
                max_rows=self.max_rows,
            )
        except Exception as exc:
            logger.error(f"Unexpected error during SQL validation: {exc}")
            return PipelineResult(
                question=question,
                status=PipelineStatus.VALIDATION_REJECTED,
                generation_status=gen_resp.status,
                generated_sql=candidate_sql,
                generation_explanation=explanation,
                generation_assumptions=assumptions,
                error_message=f"Validation failed with unexpected error: {exc}",
            )

        if not val_result.allowed:
            reasons_str = "; ".join(f"{r.code.value}: {r.message}" for r in val_result.reasons)
            logger.info(f"SQL validation rejected candidate SQL: {reasons_str}")
            return PipelineResult(
                question=question,
                status=PipelineStatus.VALIDATION_REJECTED,
                generation_status=gen_resp.status,
                generated_sql=candidate_sql,
                generation_explanation=explanation,
                generation_assumptions=assumptions,
                validation_result=val_result,
                error_message=f"Validation rejected: {reasons_str}",
            )

        # Stage D: Conditional execution (ONLY if val_result.allowed is True)
        try:
            exec_result = execute_read_only(
                sql=candidate_sql,
                max_rows=self.max_rows,
                timeout_seconds=self.timeout_seconds,
                engine=self.engine,
            )
        except QueryExecutionError as exc:
            logger.warning(f"Read-only query execution failed: {exc}")
            return PipelineResult(
                question=question,
                status=PipelineStatus.EXECUTION_FAILED,
                generation_status=gen_resp.status,
                generated_sql=candidate_sql,
                generation_explanation=explanation,
                generation_assumptions=assumptions,
                validation_result=val_result,
                error_message=f"Execution error: {exc}",
            )
        except Exception as exc:
            logger.error(f"Unexpected error during query execution: {exc}")
            return PipelineResult(
                question=question,
                status=PipelineStatus.EXECUTION_FAILED,
                generation_status=gen_resp.status,
                generated_sql=candidate_sql,
                generation_explanation=explanation,
                generation_assumptions=assumptions,
                validation_result=val_result,
                error_message=f"Execution error: {exc}",
            )

        # Stage E: Structured pipeline result
        return PipelineResult(
            question=question,
            status=PipelineStatus.SUCCESS,
            generation_status=gen_resp.status,
            generated_sql=candidate_sql,
            generation_explanation=explanation,
            generation_assumptions=assumptions,
            validation_result=val_result,
            execution_result=exec_result,
        )


def run_question(
    question: str,
    pipeline: Optional[QueryPipeline] = None,
    **kwargs,
) -> PipelineResult:
    """Convenience helper to run a question through the QueryPilot pipeline.

    Args:
        question: User natural-language question.
        pipeline: Optional preconfigured QueryPipeline instance.
        **kwargs: Arguments passed to QueryPipeline constructor if pipeline is None.

    Returns:
        PipelineResult with structured execution data or controlled error.
    """
    pipe = pipeline or QueryPipeline(**kwargs)
    return pipe.run(question)
