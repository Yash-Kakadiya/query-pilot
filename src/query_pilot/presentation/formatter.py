"""Deterministic Result Presentation Layer for QueryPilot.

Converts QueryResult and PipelineResult into clean, typed, user-facing
representations without natural-language interpretation, business inferences,
or secondary LLM calls.
"""

from decimal import Decimal
import datetime
import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional
import uuid

from pydantic import BaseModel, Field

from query_pilot.db.executor import QueryResult

if TYPE_CHECKING:
    from query_pilot.pipeline import PipelineResult, PipelineStatus


# Regex patterns for sanitizing sensitive credentials or tokens from messages
SENSITIVE_PATTERNS = [
    re.compile(r"password\s*=\s*[^;]+", re.IGNORECASE),
    re.compile(r"pwd\s*=\s*[^;]+", re.IGNORECASE),
    re.compile(r"uid\s*=\s*[^;]+", re.IGNORECASE),
    re.compile(r"user\s*id\s*=\s*[^;]+", re.IGNORECASE),
    re.compile(r"server\s*=\s*[^;]+", re.IGNORECASE),
    re.compile(r"data\s*source\s*=\s*[^;]+", re.IGNORECASE),
    re.compile(r"driver\s*=\s*\{?[^;\}]+\}?", re.IGNORECASE),
    re.compile(r"mssql\+pyodbc://[^\s'\"]+", re.IGNORECASE),
    re.compile(r"[a-zA-Z0-9_\-\.]+\.(?:internal|local|corp|com|net|org|edu|gov|io)(?::\d+)?", re.IGNORECASE),
    re.compile(r"api[_-]?key\s*[:=]\s*[^\s,]+", re.IGNORECASE),
    re.compile(r"bearer\s+[a-zA-Z0-9_\-\.]+", re.IGNORECASE),
    re.compile(r"aq\.[a-zA-Z0-9_\-\.]+", re.IGNORECASE),
    re.compile(r"aiza[0-9a-za-z-_]{35}", re.IGNORECASE),
]


def sanitize_message(msg: Optional[str]) -> Optional[str]:
    """Sanitize message strings to prevent credential or secret leaks."""
    if not msg:
        return None

    # Strip full Python exception stack traces to prevent internal exposure
    if "Traceback (most recent call last):" in msg:
        lines = msg.strip().splitlines()
        # Keep only the final exception summary line
        msg = lines[-1] if lines else "An internal error occurred."

    cleaned = msg
    for pattern in SENSITIVE_PATTERNS:
        cleaned = pattern.sub("[REDACTED]", cleaned)

    return cleaned


def format_cell_value(val: Any) -> str:
    """Deterministically convert a database cell value to a string representation.

    Rules:
    - None -> "NULL"
    - bool -> "true" / "false"
    - int, float, Decimal -> exact string representation
    - date, datetime, time -> ISO-8601 string
    - bytes, bytearray -> hexadecimal string prefixed with "0x"
    - UUID -> canonical string representation
    - other -> str(val)
    """
    if val is None:
        return "NULL"
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, (int, float, Decimal)):
        return str(val)
    if isinstance(val, (datetime.datetime, datetime.date, datetime.time)):
        return val.isoformat()
    if isinstance(val, (bytes, bytearray)):
        return "0x" + val.hex()
    if isinstance(val, uuid.UUID):
        return str(val)
    return str(val)


def escape_markdown_cell(val: Any) -> str:
    """Format and escape a cell value for inclusion in a Markdown table row."""
    text = format_cell_value(val)
    # Replace newlines with <br/> to keep Markdown table row intact
    escaped = text.replace("\r\n", "<br/>").replace("\n", "<br/>")
    # Escape pipe characters
    escaped = escaped.replace("|", "\\|")
    return escaped


def is_numeric_column(col_name: str, rows: List[Dict[str, Any]]) -> bool:
    """Check if all non-null values in a column are numeric."""
    has_values = False
    for row in rows:
        val = row.get(col_name)
        if val is not None:
            has_values = True
            if not isinstance(val, (int, float, Decimal)) or isinstance(val, bool):
                return False
    return has_values


def format_markdown_table(columns: List[str], rows: List[Dict[str, Any]]) -> str:
    """Render columns and rows into a formatted Markdown table.

    Preserves exact column order and row order without reordering.
    Escapes Markdown-sensitive cell characters (pipes, newlines).
    Aligns numeric columns to the right (---:) and text columns to the left (---).
    """
    if not columns:
        return ""

    # Build header
    header_cells = [col.replace("|", "\\|") for col in columns]
    header_line = "| " + " | ".join(header_cells) + " |"

    # Build separator with alignments
    separator_cells = []
    for col in columns:
        if is_numeric_column(col, rows):
            separator_cells.append("---:")
        else:
            separator_cells.append("---")
    separator_line = "|" + "|".join(separator_cells) + "|"

    # Build data rows
    data_lines = []
    for row in rows:
        cells = [escape_markdown_cell(row.get(col)) for col in columns]
        data_lines.append("| " + " | ".join(cells) + " |")

    return "\n".join([header_line, separator_line] + data_lines)


def extract_scalar_value(columns: List[str], rows: List[Dict[str, Any]]) -> Optional[Any]:
    """Extract scalar value if query returned exactly 1 row and 1 column."""
    if len(columns) == 1 and len(rows) == 1:
        col = columns[0]
        return rows[0].get(col)
    return None


class PresentationResult(BaseModel):
    """Structured, typed presentation model for user-facing QueryPilot output."""

    status: str = Field(
        ...,
        description="Presentation status: 'success', 'unsupported', 'validation_rejected', 'generation_failed', 'execution_failed'.",
    )
    request_id: Optional[str] = Field(
        default=None,
        description="Correlated unique pipeline request ID.",
    )
    title: Optional[str] = Field(
        default=None,
        description="User question or presentation title.",
    )
    message: Optional[str] = Field(
        default=None,
        description="Deterministic status message.",
    )
    columns: List[str] = Field(
        default_factory=list,
        description="Preserved column names in query result order.",
    )
    rows: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Preserved row records in query result order.",
    )
    row_count: int = Field(
        default=0,
        description="Total number of returned rows.",
    )
    truncated: bool = Field(
        default=False,
        description="Whether the result set was truncated by max_rows limit.",
    )
    execution_time_ms: Optional[float] = Field(
        default=None,
        description="Execution duration in milliseconds.",
    )
    scalar_value: Optional[Any] = Field(
        default=None,
        description="Deterministic scalar value if the result is exactly 1 row and 1 column.",
    )
    markdown_table: Optional[str] = Field(
        default=None,
        description="Deterministic Markdown table representation.",
    )
    explanation: Optional[str] = Field(
        default=None,
        description="Preserved generator explanation if available.",
    )
    generated_sql: Optional[str] = Field(
        default=None,
        description="Preserved candidate SQL query if available.",
    )
    validation_reasons: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Structured validation rejection reasons if rejected.",
    )
    clarification_prompt: Optional[str] = Field(
        default=None,
        description="Clarifying question or guidance if clarification is requested.",
    )
    clarification_options: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Structured candidate interpretation options for clarification.",
    )
    grounded_answer: Optional[Any] = Field(
        default=None,
        description="Structured natural-language answer if synthesized or templated.",
    )
    telemetry: Optional[Any] = Field(
        default=None,
        description="Request-level LLM token usage and latency telemetry.",
    )

    def to_dict(self) -> Dict[str, Any]:
        """Convert presentation result to JSON-compatible dictionary."""
        return self.model_dump()


def format_query_result(
    query_result: QueryResult,
    question: Optional[str] = None,
    explanation: Optional[str] = None,
    generated_sql: Optional[str] = None,
    request_id: Optional[str] = None,
    grounded_answer: Optional[Any] = None,
    telemetry: Optional[Any] = None,
) -> PresentationResult:
    """Format an executed QueryResult into a PresentationResult.

    Args:
        query_result: The raw database execution result.
        question: Optional natural-language question.
        explanation: Optional generator explanation.
        generated_sql: Optional candidate SQL statement.
        request_id: Optional unique request correlation ID.
        grounded_answer: Optional structured GroundedAnswer.
        telemetry: Optional PipelineRunTelemetry.

    Returns:
        A structured PresentationResult.
    """
    cols = list(query_result.columns)
    rows = list(query_result.rows)
    cnt = query_result.row_count
    trunc = query_result.truncated
    exec_time = query_result.execution_time_ms

    if cnt == 0:
        msg = "Query executed successfully. No rows returned."
    elif trunc:
        msg = f"Query executed successfully. Showing the first {cnt} rows; additional rows were not returned."
    else:
        msg = f"Query executed successfully. {cnt} row{'s' if cnt != 1 else ''} returned."

    scalar = extract_scalar_value(cols, rows)
    md_table = format_markdown_table(cols, rows)

    return PresentationResult(
        status="success",
        request_id=request_id,
        title=question,
        message=msg,
        columns=cols,
        rows=rows,
        row_count=cnt,
        truncated=trunc,
        execution_time_ms=exec_time,
        scalar_value=scalar,
        markdown_table=md_table,
        explanation=explanation,
        generated_sql=generated_sql,
        validation_reasons=[],
        grounded_answer=grounded_answer,
        telemetry=telemetry,
    )



def format_pipeline_result(pipeline_result: "PipelineResult") -> PresentationResult:
    """Format an end-to-end PipelineResult into a clean PresentationResult.

    Deterministic handling across all terminal pipeline states:
    - SUCCESS: formats rows, columns, scalar value, Markdown table.
    - UNSUPPORTED: structured refusal message and preserved explanation.
    - VALIDATION_REJECTED: policy rejection message and structured reasons.
    - GENERATION_FAILED: sanitized failure message without credential leaks.
    - EXECUTION_FAILED: sanitized database execution failure message.

    Args:
        pipeline_result: The PipelineResult to present.

    Returns:
        Structured PresentationResult.
    """
    from query_pilot.pipeline import PipelineStatus

    status = pipeline_result.status
    question = pipeline_result.question
    explanation = getattr(pipeline_result, "generation_explanation", None) or getattr(pipeline_result, "explanation", None)
    sql = pipeline_result.generated_sql
    req_id = getattr(pipeline_result, "request_id", None)
    answer = getattr(pipeline_result, "grounded_answer", None)
    telemetry = getattr(pipeline_result, "telemetry", None)

    if status == PipelineStatus.SUCCESS:
        if pipeline_result.execution_result is not None:
            return format_query_result(
                query_result=pipeline_result.execution_result,
                question=question,
                explanation=explanation,
                generated_sql=sql,
                request_id=req_id,
                grounded_answer=answer,
                telemetry=telemetry,
            )
        return PresentationResult(
            status="success",
            request_id=req_id,
            title=question,
            message="Query executed successfully. No rows returned.",
            columns=[],
            rows=[],
            row_count=0,
            truncated=False,
            explanation=explanation,
            generated_sql=sql,
            grounded_answer=answer,
            telemetry=telemetry,
        )

    if status == PipelineStatus.UNSUPPORTED:
        return PresentationResult(
            status="unsupported",
            request_id=req_id,
            title=question,
            message="The database schema does not contain the information required to answer this question.",
            columns=[],
            rows=[],
            row_count=0,
            truncated=False,
            explanation=explanation,
            generated_sql=None,
            grounded_answer=answer,
            telemetry=telemetry,
        )

    if status == PipelineStatus.VALIDATED_ONLY:
        return PresentationResult(
            status="validated_only",
            request_id=req_id,
            title=question,
            message="Query validated successfully. Execution was skipped.",
            columns=[],
            rows=[],
            row_count=0,
            truncated=False,
            explanation=explanation,
            generated_sql=sql,
            grounded_answer=answer,
            telemetry=telemetry,
        )

    if status == PipelineStatus.CLARIFICATION_NEEDED:
        prompt_text = (
            pipeline_result.clarification_prompt.strip()
            if pipeline_result.clarification_prompt and pipeline_result.clarification_prompt.strip()
            else None
        )
        clarification_text = prompt_text or (
            explanation.strip() if explanation and explanation.strip() else "Additional information is required to answer this question."
        )

        options_raw = getattr(pipeline_result, "clarification_options", []) or []
        options_dicts = [
            opt.model_dump() if hasattr(opt, "model_dump") else dict(opt)
            for opt in options_raw
        ]

        msg_lines = ["Additional information is required to process this question.", "", clarification_text]
        if options_dicts:
            msg_lines.append("")
            msg_lines.append("Available options:")
            for opt in options_dicts:
                opt_id = opt.get("option_id", "")
                opt_desc = opt.get("description", "")
                suggested = opt.get("suggested_question")
                if suggested:
                    msg_lines.append(f"- [{opt_id}] {opt_desc} (Suggested: {suggested})")
                else:
                    msg_lines.append(f"- [{opt_id}] {opt_desc}")

        full_message = "\n".join(msg_lines)

        return PresentationResult(
            status="clarification_needed",
            request_id=req_id,
            title=question,
            message=full_message,
            columns=[],
            rows=[],
            row_count=0,
            truncated=False,
            explanation=explanation,
            generated_sql=None,
            clarification_prompt=prompt_text,
            clarification_options=options_dicts,
            grounded_answer=answer,
            telemetry=telemetry,
        )

    if status == PipelineStatus.VALIDATION_REJECTED:
        reasons = (
            [r.to_dict() for r in pipeline_result.validation_result.reasons]
            if pipeline_result.validation_result
            else []
        )
        return PresentationResult(
            status="validation_rejected",
            request_id=req_id,
            title=question,
            message="Query rejected by the SQL safety/policy validator.",
            columns=[],
            rows=[],
            row_count=0,
            truncated=False,
            explanation=explanation,
            generated_sql=sql,
            validation_reasons=reasons,
            grounded_answer=answer,
            telemetry=telemetry,
        )

    if status == PipelineStatus.GENERATION_FAILED:
        safe_msg = sanitize_message(pipeline_result.error_message) or "Failed to generate SQL query for the question."
        return PresentationResult(
            status="generation_failed",
            request_id=req_id,
            title=question,
            message=safe_msg,
            columns=[],
            rows=[],
            row_count=0,
            truncated=False,
            explanation=explanation,
            generated_sql=None,
            grounded_answer=answer,
            telemetry=telemetry,
        )

    if status == PipelineStatus.EXECUTION_FAILED:
        safe_msg = sanitize_message(pipeline_result.error_message) or "Query execution failed in the database."
        return PresentationResult(
            status="execution_failed",
            request_id=req_id,
            title=question,
            message=safe_msg,
            columns=[],
            rows=[],
            row_count=0,
            truncated=False,
            explanation=explanation,
            generated_sql=sql,
            grounded_answer=answer,
            telemetry=telemetry,
        )

    # Fallback for unexpected status codes
    return PresentationResult(
        status=str(status).lower(),
        request_id=req_id,
        title=question,
        message=sanitize_message(pipeline_result.error_message) or "Pipeline processing encountered an unhandled state.",
        columns=[],
        rows=[],
        row_count=0,
        truncated=False,
        explanation=explanation,
        generated_sql=sql,
        grounded_answer=answer,
        telemetry=telemetry,
    )
