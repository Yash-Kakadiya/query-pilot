"""QueryPilot API layer package."""

from query_pilot.api.cli import build_parser as build_api_parser, main as api_main, run_api_cli
from query_pilot.api.models import (
    AnswerGroundingStatus,
    ColumnSchemaContext,
    ForeignKeySchemaContext,
    GenerateSQLRequest,
    GenerateSQLResponse,
    GenerationStatus,
    GroundedAnswerResponse,
    HealthResponse,
    ModelFacingSchemaContext,
    QueryRequest,
    QueryResponse,
    SynthesizeAnswerRequest,
    SynthesizeAnswerResponse,
    TableSchemaContext,
    ValidationReasonResponse,
)

__all__ = [
    "AnswerGroundingStatus",
    "ColumnSchemaContext",
    "ForeignKeySchemaContext",
    "GenerateSQLRequest",
    "GenerateSQLResponse",
    "GenerationStatus",
    "GroundedAnswerResponse",
    "HealthResponse",
    "ModelFacingSchemaContext",
    "QueryRequest",
    "QueryResponse",
    "SynthesizeAnswerRequest",
    "SynthesizeAnswerResponse",
    "TableSchemaContext",
    "ValidationReasonResponse",
    "api_main",
    "app",
    "build_api_parser",
    "create_app",
    "run_api_cli",
]


def __getattr__(name: str):
    """Lazily load app or create_app on demand without eager settings validation."""
    if name in {"app", "create_app"}:
        from query_pilot.api.app import app as _app, create_app as _create_app

        return _app if name == "app" else _create_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

