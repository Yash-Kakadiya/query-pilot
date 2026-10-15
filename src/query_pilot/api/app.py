"""FastAPI application factory and routing layer for QueryPilot (Task 1.32-C).

Exposes:
- GET  /health: Lightweight operational readiness check (non-sensitive).
- POST /query:  Executes natural-language database queries through the existing
                synchronous QueryRunner and returns the public QueryResponse contract.

Principles:
- Synchronous `def` route handlers allow FastAPI / Starlette to execute blocking
  model and database operations in worker threads without blocking the event loop.
- Client inputs are strictly validated via QueryRequest (extra="forbid", max 3 turns).
- Generated SQL is omitted by default from public responses.
- No client-supplied SQL, schema overrides, database configurations, or retry controls.
- Logs only operational metadata (request ID, lengths, status); never logs user questions,
  conversation contents, passwords, or query results.
"""

import logging
from typing import Optional
import uuid

from fastapi import Depends, FastAPI, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from query_pilot.api.models import HealthResponse, QueryRequest, QueryResponse
from query_pilot.config import Settings
from query_pilot.presentation.formatter import sanitize_message
from query_pilot.runner import QueryRunner
from query_pilot.tracing import sanitize_safe_identifier

logger = logging.getLogger("query_pilot.api")


def get_runner(request: Request) -> QueryRunner:
    """Dependency provider retrieving the QueryRunner from application state."""
    runner = getattr(request.app.state, "runner", None)
    if runner is None:
        runner = QueryRunner()
        request.app.state.runner = runner
    return runner


def create_app(
    runner: Optional[QueryRunner] = None,
    settings: Optional[Settings] = None,
) -> FastAPI:
    """Create and configure a FastAPI application instance for QueryPilot.

    Args:
        runner: Optional pre-configured QueryRunner instance (e.g. for mocked unit testing).
        settings: Optional application settings.

    Returns:
        Configured FastAPI application instance.
    """
    app = FastAPI(
        title="QueryPilot API",
        version="0.1.0",
        description="Autonomous data analyst API with safe, self-correcting text-to-SQL workflows.",
    )

    app.state.runner = runner
    app.state.settings = settings

    # --------------------------------------------------------------------------
    # Exception Handlers
    # --------------------------------------------------------------------------

    @app.exception_handler(RequestValidationError)
    def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Handle 422 Unprocessable Entity with sanitized validation errors."""
        logger.warning("Request validation failed: %d errors", len(exc.errors()))
        sanitized_errors = []
        for err in exc.errors():
            sanitized_errors.append(
                {
                    "loc": list(err.get("loc", ())),
                    "msg": err.get("msg", ""),
                    "type": err.get("type", ""),
                }
            )
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={"detail": sanitized_errors},
        )

    @app.exception_handler(Exception)
    def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        """Handle unexpected application errors without leaking stack traces or secrets."""
        logger.error("Unhandled error processing request: %s", type(exc).__name__)

        client_req_id = request.headers.get("X-Request-ID")
        req_id = sanitize_safe_identifier(client_req_id, default="")
        headers = {"X-Request-ID": req_id} if req_id else {}

        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "An internal server error occurred."},
            headers=headers,
        )

    # --------------------------------------------------------------------------
    # Endpoints
    # --------------------------------------------------------------------------

    @app.get(
        "/health",
        response_model=HealthResponse,
        summary="Health Check",
        description="Verify application liveness and configured dialect.",
    )
    def health_check(request: Request) -> HealthResponse:
        """Return non-sensitive application liveness and operational metadata.

        Verifies that the API server process is running and ready to handle requests.
        Note: This is an application liveness check and does NOT verify downstream
        database reachability or Gemini API connectivity.
        """
        app_settings = getattr(request.app.state, "settings", None)
        return HealthResponse.from_config(settings=app_settings)

    @app.post(
        "/query",
        response_model=QueryResponse,
        summary="Execute Natural Language Query",
        description="Accepts a natural-language question, executes the pipeline, and returns structured data.",
    )
    def execute_query(
        body: QueryRequest,
        request: Request,
        response: Response,
        runner_dep: QueryRunner = Depends(get_runner),
    ) -> QueryResponse:
        """Synchronously execute a natural language question via the QueryRunner."""
        # 1. Establish correlated request ID
        client_req_id = request.headers.get("X-Request-ID")
        req_id = sanitize_safe_identifier(client_req_id, default="")
        if not req_id:
            req_id = f"req-{uuid.uuid4().hex[:12]}"

        # 2. Log safe operational metrics (never log question text, turns, or credentials)
        logger.info(
            "Processing query request [request_id=%s, question_length=%d, context_turns=%d]",
            req_id,
            len(body.question),
            len(body.conversation_context) if body.conversation_context else 0,
        )

        # 3. Synchronous execution through established pipeline/runner
        pipeline_result = runner_dep.run(
            question=body.question,
            request_id=req_id,
            conversation_context=body.conversation_context,
        )

        # 4. Project into safe public response model (excludes SQL by default)
        query_response = QueryResponse.from_pipeline_result(
            pipeline_result, include_sql=False
        )

        final_req_id = query_response.request_id or req_id
        response.headers["X-Request-ID"] = final_req_id

        # 5. Log completion status
        logger.info(
            "Completed query request [request_id=%s, status=%s, row_count=%d]",
            final_req_id,
            query_response.status.value,
            query_response.row_count,
        )

        return query_response

    return app


# Default ASGI application instance for local uvicorn serving
# Example: uvicorn query_pilot.api.app:app --host 0.0.0.0 --port 8000
app: FastAPI = create_app()
