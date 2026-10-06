"""QueryPilot: Autonomous AI Data Analyst with Safe Text-to-SQL Workflow."""

from query_pilot.pipeline import (
    PipelineResult,
    PipelineStatus,
    QueryPipeline,
    run_question,
)

__version__ = "0.1.0"

__all__ = [
    "QueryPipeline",
    "PipelineResult",
    "PipelineStatus",
    "run_question",
]
