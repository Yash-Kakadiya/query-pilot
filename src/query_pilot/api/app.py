"""FastAPI application factory and routing layer for QueryPilot.

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

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from query_pilot.api.models import HealthResponse, QueryRequest, QueryResponse
from query_pilot.api.security import (
    ACCESS_KEY_HEADER,
    ConcurrencyGuard,
    ConcurrencyLimitExceeded,
    InMemoryRateLimiter,
    PayloadTooLargeError,
    StreamingPayloadSizeMiddleware,
    verify_prototype_access_key,
)
from query_pilot.api.v1 import router as v1_router
from query_pilot.config import Settings
from query_pilot.presentation.formatter import sanitize_message
from query_pilot.presentation.synthesizer import AnswerSynthesisService
from query_pilot.runner import QueryRunner
from query_pilot.sql.generation import SQLGenerator
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
    sql_generator: Optional[SQLGenerator] = None,
    synthesizer: Optional[AnswerSynthesisService] = None,
) -> FastAPI:
    """Create and configure a FastAPI application instance for QueryPilot.

    Args:
        runner: Optional pre-configured QueryRunner instance (e.g. for mocked unit testing).
        settings: Optional application settings.
        sql_generator: Optional pre-configured SQLGenerator instance for hosted v1 SQL generation.
        synthesizer: Optional pre-configured AnswerSynthesisService instance for hosted v1 synthesis.

    Returns:
        Configured FastAPI application instance.
    """
    app = FastAPI(
        title="QueryPilot API",
        version="0.1.0",
        description="Autonomous data analyst API with safe, self-correcting text-to-SQL workflows.",
    )

    # --------------------------------------------------------------------------
    # Security Infrastructure & Resource Limiting
    # --------------------------------------------------------------------------
    resolved_settings = settings
    if resolved_settings is None:
        try:
            from query_pilot.config import get_settings

            resolved_settings = get_settings()
        except Exception as exc:
            logger.error(
                "Application startup failed: unable to load or validate settings [%s]",
                type(exc).__name__,
            )
            raise RuntimeError(
                f"Application startup failed: unable to load or validate settings ({type(exc).__name__})."
            ) from None

    app.state.runner = runner
    app.state.settings = resolved_settings
    app.state.sql_generator = sql_generator
    app.state.synthesizer = synthesizer

    rate_limit_rpm = getattr(resolved_settings, "QUERY_PILOT_RATE_LIMIT_PER_MINUTE", 20) if resolved_settings else 20
    max_concurrent = getattr(resolved_settings, "QUERY_PILOT_MAX_CONCURRENT_QUERIES", 3) if resolved_settings else 3
    app.state.rate_limiter = InMemoryRateLimiter(rate_limit_rpm)
    app.state.concurrency_guard = ConcurrencyGuard(max_concurrent)

    # --------------------------------------------------------------------------
    # Streaming Request Payload Size Limit Middleware
    # Added BEFORE CORSMiddleware so CORSMiddleware wraps it as the outer layer.
    # --------------------------------------------------------------------------
    app.add_middleware(StreamingPayloadSizeMiddleware)

    # --------------------------------------------------------------------------
    # CORS Middleware (Conditional on explicit configuration)
    # --------------------------------------------------------------------------
    cors_origins = resolved_settings.CORS_ALLOWED_ORIGINS if resolved_settings else []
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Content-Type", "Accept", "X-Request-ID", ACCESS_KEY_HEADER],
            expose_headers=["X-Request-ID"],
            max_age=600,
        )

    # --------------------------------------------------------------------------
    # Exception Handlers
    # --------------------------------------------------------------------------

    @app.exception_handler(PayloadTooLargeError)
    def payload_too_large_exception_handler(
        request: Request, exc: PayloadTooLargeError
    ) -> JSONResponse:
        """Handle 413 Content Too Large when streaming request body exceeds configured limit."""
        client_req_id = request.headers.get("X-Request-ID")
        req_id = sanitize_safe_identifier(client_req_id, default="")
        headers = {"X-Request-ID": req_id} if req_id else {}
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
            headers=headers,
        )

    @app.exception_handler(HTTPException)
    def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        """Handle HTTP exceptions preserving status codes and sanitized messages."""
        client_req_id = request.headers.get("X-Request-ID")
        req_id = sanitize_safe_identifier(client_req_id, default="")
        headers = {"X-Request-ID": req_id} if req_id else {}
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
            headers=headers,
        )

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

        # 2. Authorization check: Validate prototype access key BEFORE any LLM or DB work
        app_settings = getattr(request.app.state, "settings", None)
        if app_settings is None:
            try:
                from query_pilot.config import get_settings

                app_settings = get_settings()
            except Exception as exc:
                logger.error(
                    "Request execution failed: unable to resolve application settings [%s]",
                    type(exc).__name__,
                )
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Server configuration unavailable.",
                ) from None
            request.app.state.settings = app_settings

        verify_prototype_access_key(request, app_settings)

        # 3. Rate limiting check (in-memory token bucket across prototype)
        rate_limiter = getattr(request.app.state, "rate_limiter", None)
        if rate_limiter and not rate_limiter.acquire():
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded. Please wait before submitting another query.",
            )

        # 4. Concurrency limiting and server-enforced execution mode
        concurrency_guard = getattr(request.app.state, "concurrency_guard", None)
        force_sql_only = bool(getattr(app_settings, "QUERY_PILOT_FORCE_SQL_ONLY", False))
        effective_mode = "sql_only" if force_sql_only else body.execution_mode

        def _run_pipeline():
            logger.info(
                "Processing query request [request_id=%s, question_length=%d, context_turns=%d, mode=%s]",
                req_id,
                len(body.question),
                len(body.conversation_context) if body.conversation_context else 0,
                effective_mode,
            )
            return runner_dep.run(
                question=body.question,
                request_id=req_id,
                conversation_context=body.conversation_context,
                execution_mode=effective_mode,
            )

        # 5. Synchronous execution through established pipeline/runner within concurrency limit
        if concurrency_guard:
            try:
                with concurrency_guard.limit(timeout=0.0):
                    pipeline_result = _run_pipeline()
            except ConcurrencyLimitExceeded:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Server is busy processing concurrent queries. Please retry shortly.",
                )
        else:
            pipeline_result = _run_pipeline()

        # 6. Project into safe public response model (honors include_sql contract)
        query_response = QueryResponse.from_pipeline_result(
            pipeline_result, include_sql=body.include_sql
        )

        final_req_id = query_response.request_id or req_id
        response.headers["X-Request-ID"] = final_req_id

        # 7. Log completion status
        logger.info(
            "Completed query request [request_id=%s, status=%s, row_count=%d]",
            final_req_id,
            query_response.status.value,
            query_response.row_count,
        )

        return query_response

    # --------------------------------------------------------------------------
    # Mount Hosted API V1 Routers
    # --------------------------------------------------------------------------
    app.include_router(v1_router, prefix="/api/v1")

    return app


# Default ASGI application instance for local uvicorn serving
# Example: uvicorn query_pilot.api.app:app --host 0.0.0.0 --port 8000
app: FastAPI = create_app()
