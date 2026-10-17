"""Structured Pipeline Observability for QueryPilot.

Provides lightweight, machine-readable, correlated lifecycle tracking for
text-to-SQL requests without external dependencies, telemetry services,
or sensitive data exposure.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
from enum import Enum
import json
import logging
import time
from typing import Any, Dict, List, Optional
import uuid

from pydantic import BaseModel, Field

# Dedicated logger for structured pipeline lifecycle events
logger = logging.getLogger("query_pilot.observability")


class PipelineStage(str, Enum):
    """Documented lifecycle stages for the QueryPilot pipeline."""

    PIPELINE = "pipeline"
    SCHEMA_ACQUISITION = "schema_acquisition"
    SQL_GENERATION = "sql_generation"
    SQL_VALIDATION = "sql_validation"
    READ_ONLY_EXECUTION = "read_only_execution"
    RESULT_PRESENTATION = "result_presentation"


class StageStatus(str, Enum):
    """Machine-readable status outcomes for pipeline stages."""

    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"
    UNSUPPORTED = "unsupported"
    SKIPPED = "skipped"


class ObservabilityEvent(str, Enum):
    """Stable event names for pipeline lifecycle transitions."""

    PIPELINE_START = "pipeline.start"
    STAGE_START = "stage.start"
    STAGE_END = "stage.end"
    PIPELINE_END = "pipeline.end"


class ErrorCategory(str, Enum):
    """Sanitized, stable error categories preventing sensitive message leakage."""

    SCHEMA_ACQUISITION_ERROR = "SCHEMA_ACQUISITION_ERROR"
    SQL_GENERATION_FAILED = "SQL_GENERATION_FAILED"
    UNSUPPORTED_QUESTION = "UNSUPPORTED_QUESTION"
    VALIDATION_REJECTED = "VALIDATION_REJECTED"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    QUERY_TIMEOUT = "QUERY_TIMEOUT"
    UNSAFE_QUERY = "UNSAFE_QUERY"
    PRESENTATION_ERROR = "PRESENTATION_ERROR"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class PipelineEvent(BaseModel):
    """Structured, machine-readable event record.

    SECURITY GUARANTEE:
    This model explicitly contains NO fields for natural-language questions,
    generated SQL, row data, table contents, database credentials, or raw
    stack traces.
    """

    event: str = Field(..., description="Stable event name.")
    request_id: str = Field(..., description="Unique correlation ID for the request.")
    stage: str = Field(..., description="Pipeline stage name.")
    status: str = Field(..., description="Stage or request status.")
    timestamp: str = Field(..., description="UTC ISO-8601 timestamp.")
    duration_ms: Optional[float] = Field(default=None, description="Elapsed time in milliseconds.")
    error_code: Optional[str] = Field(default=None, description="Sanitized stable error category.")
    error_type: Optional[str] = Field(default=None, description="Exception class name if applicable.")
    row_count: Optional[int] = Field(default=None, description="Number of returned rows if executed.")
    truncated: Optional[bool] = Field(default=None, description="Whether execution was truncated.")
    rejection_codes: Optional[List[str]] = Field(default=None, description="Validation rejection reason codes.")
    final_status: Optional[str] = Field(default=None, description="Final terminal pipeline status.")

    def to_json_dict(self) -> Dict[str, Any]:
        """Convert event to dictionary omitting unset optional fields."""
        return self.model_dump(exclude_none=True)


def classify_error(exc: Exception, stage: PipelineStage | str) -> tuple[str, str]:
    """Map an exception to a safe, stable error category and class name.

    Never extracts exception message strings or arguments.

    Args:
        exc: The raised Exception.
        stage: The pipeline stage during which the exception occurred.

    Returns:
        Tuple of (error_code, error_type).
    """
    error_type = exc.__class__.__name__

    # Specific known execution exceptions
    if error_type == "QueryTimeoutError":
        return ErrorCategory.QUERY_TIMEOUT.value, error_type
    if error_type == "UnsafeQueryError":
        return ErrorCategory.UNSAFE_QUERY.value, error_type
    if error_type == "QueryExecutionError":
        return ErrorCategory.EXECUTION_FAILED.value, error_type

    # Specific known generation exceptions
    if error_type in ("SQLGenerationError", "ConfigurationError", "ProviderAPIError", "MalformedResponseError", "EmptySQLError"):
        return ErrorCategory.SQL_GENERATION_FAILED.value, error_type

    # Stage-based fallback
    stage_str = stage.value if isinstance(stage, Enum) else str(stage)
    if stage_str == PipelineStage.SCHEMA_ACQUISITION.value:
        return ErrorCategory.SCHEMA_ACQUISITION_ERROR.value, error_type
    if stage_str == PipelineStage.SQL_VALIDATION.value:
        return ErrorCategory.VALIDATION_ERROR.value, error_type
    if stage_str == PipelineStage.READ_ONLY_EXECUTION.value:
        return ErrorCategory.EXECUTION_FAILED.value, error_type
    if stage_str == PipelineStage.RESULT_PRESENTATION.value:
        return ErrorCategory.PRESENTATION_ERROR.value, error_type

    return ErrorCategory.INTERNAL_ERROR.value, error_type


class PipelineObserver:
    """Observability coordinator tracking a single pipeline request lifecycle.

    Maintains monotonic timing, correlates events via request_id, and safely
    emits structured logs without failing the underlying pipeline on logging errors.
    """

    def __init__(
        self,
        request_id: Optional[str] = None,
        event_logger: Optional[logging.Logger] = None,
        enabled: bool = True,
    ):
        """Initialize the observer for a pipeline invocation.

        Args:
            request_id: Unique request identifier (generates UUID if omitted).
            event_logger: Logger instance to emit events to.
            enabled: Whether event logging is active.
        """
        self.request_id: str = request_id or f"req_{uuid.uuid4().hex[:12]}"
        self.logger: logging.Logger = event_logger or logger
        self.enabled: bool = enabled
        self._start_perf: float = time.perf_counter()
        self._stage_perf: Dict[str, float] = {}
        self.stage_durations: Dict[str, float] = {}
        self.events: List[PipelineEvent] = []
        self.total_duration_ms: Optional[float] = None

    def _utc_now_iso(self) -> str:
        """Return current UTC time in ISO-8601 format."""
        return datetime.now(timezone.utc).isoformat()

    def _emit(self, event: PipelineEvent) -> None:
        """Safely record and log a structured event without raising errors."""
        try:
            self.events.append(event)
            if not self.enabled:
                return

            event_dict = event.to_json_dict()
            json_msg = json.dumps(event_dict)

            # Determine appropriate log level
            level = logging.INFO
            if event.status == StageStatus.REJECTED.value:
                level = logging.WARNING
            elif event.status == StageStatus.FAILED.value:
                level = logging.ERROR

            self.logger.log(level, json_msg, extra={"structured_event": event_dict})
        except Exception:
            # Failure isolation: logging issues must never disrupt the pipeline
            pass

    def start_pipeline(self) -> None:
        """Emit pipeline start event."""
        try:
            self._start_perf = time.perf_counter()
            event = PipelineEvent(
                event=ObservabilityEvent.PIPELINE_START.value,
                request_id=self.request_id,
                stage=PipelineStage.PIPELINE.value,
                status=StageStatus.STARTED.value,
                timestamp=self._utc_now_iso(),
            )
            self._emit(event)
        except Exception:
            pass

    def start_stage(self, stage: PipelineStage | str) -> None:
        """Record the start of a lifecycle stage.

        Args:
            stage: PipelineStage enum or string.
        """
        try:
            stage_str = stage.value if isinstance(stage, Enum) else str(stage)
            self._stage_perf[stage_str] = time.perf_counter()
            event = PipelineEvent(
                event=ObservabilityEvent.STAGE_START.value,
                request_id=self.request_id,
                stage=stage_str,
                status=StageStatus.STARTED.value,
                timestamp=self._utc_now_iso(),
            )
            self._emit(event)
        except Exception:
            pass

    def end_stage(
        self,
        stage: PipelineStage | str,
        status: StageStatus | str,
        error_code: Optional[str] = None,
        error_type: Optional[str] = None,
        row_count: Optional[int] = None,
        truncated: Optional[bool] = None,
        rejection_codes: Optional[List[str]] = None,
    ) -> float:
        """Record the completion of a lifecycle stage.

        Args:
            stage: PipelineStage enum or string.
            status: StageStatus outcome.
            error_code: Sanitized error code if failed or rejected.
            error_type: Exception class name if applicable.
            row_count: Query row count if executed.
            truncated: Query truncation flag if executed.
            rejection_codes: Validation rejection codes if rejected.

        Returns:
            Elapsed stage duration in milliseconds.
        """
        stage_str = stage.value if isinstance(stage, Enum) else str(stage)
        status_str = status.value if isinstance(status, Enum) else str(status)
        duration_ms = 0.0

        try:
            start_time = self._stage_perf.get(stage_str)
            if start_time is not None:
                duration_ms = max(0.0, round((time.perf_counter() - start_time) * 1000, 2))
                self.stage_durations[stage_str] = duration_ms

            event = PipelineEvent(
                event=ObservabilityEvent.STAGE_END.value,
                request_id=self.request_id,
                stage=stage_str,
                status=status_str,
                timestamp=self._utc_now_iso(),
                duration_ms=duration_ms,
                error_code=error_code,
                error_type=error_type,
                row_count=row_count,
                truncated=truncated,
                rejection_codes=rejection_codes,
            )
            self._emit(event)
        except Exception:
            pass

        return duration_ms

    def end_pipeline(
        self,
        final_status: str,
        status: Optional[StageStatus | str] = None,
    ) -> float:
        """Record the overall completion of the pipeline invocation.

        Args:
            final_status: PipelineStatus value string (e.g. 'SUCCESS', 'UNSUPPORTED').
            status: StageStatus string (defaults to succeeded/failed based on final_status).

        Returns:
            Total pipeline duration in milliseconds.
        """
        duration_ms = 0.0
        try:
            duration_ms = max(0.0, round((time.perf_counter() - self._start_perf) * 1000, 2))
            self.total_duration_ms = duration_ms

            if status is None:
                if final_status == "SUCCESS":
                    status_str = StageStatus.SUCCEEDED.value
                elif final_status == "UNSUPPORTED":
                    status_str = StageStatus.UNSUPPORTED.value
                elif final_status == "VALIDATION_REJECTED":
                    status_str = StageStatus.REJECTED.value
                else:
                    status_str = StageStatus.FAILED.value
            else:
                status_str = status.value if isinstance(status, Enum) else str(status)

            event = PipelineEvent(
                event=ObservabilityEvent.PIPELINE_END.value,
                request_id=self.request_id,
                stage=PipelineStage.PIPELINE.value,
                status=status_str,
                timestamp=self._utc_now_iso(),
                duration_ms=duration_ms,
                final_status=final_status,
            )
            self._emit(event)
        except Exception:
            pass

        return duration_ms


# ==============================================================================
# Model Invocation & Token Usage Telemetry
# ==============================================================================

class LLMUsageMetadata(BaseModel):
    """Structured telemetry metadata for an individual LLM invocation.

    Invariants:
    - prompt_tokens, completion_tokens, and total_tokens are Optional[int].
      Missing values remain None and are never converted to zero.
    - estimated_cost_usd is strictly None in this task (unverified pricing).
    - Contains strictly telemetry: zero prompts, SQL, schema text, rows, or secrets.
    """

    stage: str = Field(..., description="Stage name: 'sql_generation' or 'answer_synthesis'.")
    model_name: str = Field(..., description="Configured model identifier.")
    model_version: Optional[str] = Field(default=None, description="Provider-reported model version string.")
    prompt_tokens: Optional[int] = Field(default=None, description="Input/prompt token count.")
    completion_tokens: Optional[int] = Field(default=None, description="Candidate/output token count.")
    total_tokens: Optional[int] = Field(default=None, description="Total token count reported by provider.")
    latency_ms: float = Field(..., description="Monotonic elapsed time in milliseconds.")
    estimated_cost_usd: Optional[float] = Field(default=None, description="Estimated cost in USD (strictly None if unverified).")


class PipelineRunTelemetry(BaseModel):
    """Aggregate telemetry container for an end-to-end pipeline run.

    SECURITY GUARANTEE:
    Explicitly contains zero natural-language questions, SQL queries, database rows,
    table schemas, or authentication secrets.
    """

    request_id: Optional[str] = Field(default=None, description="Correlated request identifier.")
    invocations: List[LLMUsageMetadata] = Field(default_factory=list, description="Per-invocation usage records.")
    total_prompt_tokens: Optional[int] = Field(default=None, description="Sum of prompt tokens if all invocations are known.")
    total_completion_tokens: Optional[int] = Field(default=None, description="Sum of completion tokens if all invocations are known.")
    total_tokens: Optional[int] = Field(default=None, description="Sum of total tokens if all invocations are known.")
    total_llm_latency_ms: float = Field(default=0.0, description="Sum of all monotonic LLM call latencies.")
    total_estimated_cost_usd: Optional[float] = Field(default=None, description="Total estimated cost (None if any invocation is None or unverified).")

    def add_invocation(self, usage: Optional[LLMUsageMetadata]) -> None:
        """Add an invocation record and update aggregates with strict null-safety."""
        if usage is None:
            return
        self.invocations.append(usage)
        self._recompute_aggregates()

    def _recompute_aggregates(self) -> None:
        """Recompute aggregate token counts and latency."""
        self.total_llm_latency_ms = round(sum(inv.latency_ms for inv in self.invocations), 2)

        # Prompt tokens: sum only if all invocations have known non-None integer counts
        if self.invocations and all(inv.prompt_tokens is not None for inv in self.invocations):
            self.total_prompt_tokens = sum(inv.prompt_tokens for inv in self.invocations if inv.prompt_tokens is not None)
        else:
            self.total_prompt_tokens = None

        # Completion tokens: sum only if all invocations have known non-None integer counts
        if self.invocations and all(inv.completion_tokens is not None for inv in self.invocations):
            self.total_completion_tokens = sum(inv.completion_tokens for inv in self.invocations if inv.completion_tokens is not None)
        else:
            self.total_completion_tokens = None

        # Total tokens: sum only if all invocations have known non-None integer counts
        if self.invocations and all(inv.total_tokens is not None for inv in self.invocations):
            self.total_tokens = sum(inv.total_tokens for inv in self.invocations if inv.total_tokens is not None)
        else:
            self.total_tokens = None

        # Cost calculation is currently not tracked (remains None)
        self.total_estimated_cost_usd = None

    @property
    def total_latency_ms(self) -> float:
        """Alias for total_llm_latency_ms."""
        return self.total_llm_latency_ms

    @property
    def estimated_cost_usd(self) -> Optional[float]:
        """Alias for total_estimated_cost_usd."""
        return self.total_estimated_cost_usd

    def to_dict(self) -> Dict[str, Any]:
        """Convert telemetry to a clean dictionary guaranteed free of prompts, queries, or rows."""
        data = self.model_dump()
        data["total_latency_ms"] = self.total_llm_latency_ms
        data["estimated_cost_usd"] = self.total_estimated_cost_usd
        return data


def extract_llm_usage(
    response: Any,
    stage: str = "sql_generation",
    model_name: str = "gemini-3.8-flash",
    latency_ms: float = 0.0,
) -> LLMUsageMetadata:
    """Safely extract token usage and metadata from provider response.

    Fails open on missing, partial, or malformed usage metadata without raising exceptions.
    Never converts unknown token counts to zero.
    """
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    model_version: Optional[str] = None

    if response is not None:
        mv = getattr(response, "model_version", None)
        if isinstance(mv, str):
            model_version = mv
        usage = getattr(response, "usage_metadata", None)
        if usage is not None:
            # Handle object attributes (e.g. google.genai types)
            if hasattr(usage, "prompt_token_count"):
                pt = getattr(usage, "prompt_token_count", None)
                if isinstance(pt, int) and pt >= 0:
                    prompt_tokens = pt
            elif isinstance(usage, dict):
                pt = usage.get("prompt_token_count")
                if isinstance(pt, int) and pt >= 0:
                    prompt_tokens = pt

            if hasattr(usage, "candidates_token_count"):
                ct = getattr(usage, "candidates_token_count", None)
                if isinstance(ct, int) and ct >= 0:
                    completion_tokens = ct
            elif isinstance(usage, dict):
                ct = usage.get("candidates_token_count")
                if isinstance(ct, int) and ct >= 0:
                    completion_tokens = ct

            if hasattr(usage, "total_token_count"):
                tt = getattr(usage, "total_token_count", None)
                if isinstance(tt, int) and tt >= 0:
                    total_tokens = tt
            elif isinstance(usage, dict):
                tt = usage.get("total_token_count")
                if isinstance(tt, int) and tt >= 0:
                    total_tokens = tt

    return LLMUsageMetadata(
        stage=stage,
        model_name=model_name,
        model_version=model_version,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        latency_ms=round(latency_ms, 2),
        estimated_cost_usd=None,
    )
