"""Deterministic presentation layer for QueryPilot results."""

from query_pilot.presentation.formatter import (
    PresentationResult,
    escape_markdown_cell,
    extract_scalar_value,
    format_cell_value,
    format_markdown_table,
    format_pipeline_result,
    format_query_result,
    sanitize_message,
)

__all__ = [
    "PresentationResult",
    "escape_markdown_cell",
    "extract_scalar_value",
    "format_cell_value",
    "format_markdown_table",
    "format_pipeline_result",
    "format_query_result",
    "sanitize_message",
]
