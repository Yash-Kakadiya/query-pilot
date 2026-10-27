"""Decoupled Hosted API V1 routes for SQL generation and metadata answer synthesis.

Exposes:
- POST /api/v1/generate-sql: Bounded text-to-SQL generation from client-supplied schema context.
  Never connects to customer databases or executes queries.
- POST /api/v1/synthesize-answer: Privacy-preserving metadata-only answer synthesis.
  Never accepts or transmits row records or individual cell values.

Security & Architecture Principles:
- Client-supplied schema context and conversation history are untrusted data.
- Zero customer database access, zero execution capabilities on hosted endpoints.
- Strict Pydantic models with extra="forbid" and bounded string/list limits.
- Candidate SQL is strictly labeled as unexecuted.
- Correlation IDs (X-Request-ID) are preserved and propagated.
- Model outputs are untrusted and application-validated before returning.
"""

import logging
from typing import Optional
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from query_pilot.api.models import (
    GenerateSQLRequest,
    GenerateSQLResponse,
    GenerationStatus,
    SynthesizeAnswerRequest,
    SynthesizeAnswerResponse,
)
from query_pilot.api.security import (
    ConcurrencyGuard,
    ConcurrencyLimitExceeded,
    InMemoryRateLimiter,
    verify_prototype_access_key,
)
from query_pilot.presentation.formatter import sanitize_message
from query_pilot.presentation.synthesizer import AnswerSynthesisService
from query_pilot.sql.generation import (
    SQLGenerationError,
    SQLGenerationRequest,
    SQLGenerationStatus,
    SQLGenerator,
)
from query_pilot.sql.validator import SQLValidator
from query_pilot.tracing import sanitize_safe_identifier

logger = logging.getLogger("query_pilot.api.v1")

router = APIRouter()


def get_sql_generator(request: Request) -> SQLGenerator:
    """Dependency provider resolving the SQLGenerator from application state."""
    generator = getattr(request.app.state, "sql_generator", None)
    if generator is None:
        from query_pilot.sql.gemini_generator import GeminiSQLGenerator

        generator = GeminiSQLGenerator(enable_retrieval=False)
        request.app.state.sql_generator = generator
    return generator


def get_synthesizer(request: Request) -> AnswerSynthesisService:
    """Dependency provider resolving the AnswerSynthesisService from application state."""
    synthesizer = getattr(request.app.state, "synthesizer", None)
    if synthesizer is None:
        synthesizer = AnswerSynthesisService()
        request.app.state.synthesizer = synthesizer
    return synthesizer


@router.post(
    "/generate-sql",
    response_model=GenerateSQLResponse,
    summary="Generate SQL Candidate",
    description="Generate an unexecuted candidate SQL query from untrusted schema context without connecting to any database.",
)
def generate_sql_endpoint(
    body: GenerateSQLRequest,
    request: Request,
    response: Response,
    generator_dep: SQLGenerator = Depends(get_sql_generator),
) -> GenerateSQLResponse:
    """Generate read-only candidate T-SQL from client question and model-facing schema.

    Invariants:
    - Never opens a database connection, executes SQL, or calls the legacy QueryRunner.
    - Treats schema context, identifiers, descriptions, and user question as untrusted data.
    - Validates cross-field response consistency before returning.
    - Candidate SQL is strictly labeled as unexecuted.
    """
    # 1. Establish correlated request ID
    client_req_id = request.headers.get("X-Request-ID")
    req_id = sanitize_safe_identifier(client_req_id, default="", max_len=128)
    if not req_id and body.request_id:
        req_id = sanitize_safe_identifier(body.request_id, default="", max_len=128)
    if not req_id:
        req_id = f"req-{uuid.uuid4().hex[:12]}"

    # 2. Authorization: Verify prototype access key
    app_settings = getattr(request.app.state, "settings", None)
    if app_settings is None:
        try:
            from query_pilot.config import get_settings

            app_settings = get_settings()
        except Exception as exc:
            logger.error("Unable to resolve application settings [%s]", type(exc).__name__)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Server configuration unavailable.",
            ) from None
        request.app.state.settings = app_settings

    verify_prototype_access_key(request, app_settings)

    # 3. Rate limiting check
    rate_limiter = getattr(request.app.state, "rate_limiter", None)
    if rate_limiter and not rate_limiter.acquire():
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded. Please wait before submitting another query.",
        )

    # 4. Concurrency limiting
    concurrency_guard = getattr(request.app.state, "concurrency_guard", None)

    def _execute_generation() -> GenerateSQLResponse:
        logger.info(
            "Processing SQL generation request [request_id=%s, question_length=%d, table_count=%d]",
            req_id,
            len(body.question),
            len(body.schema_context.tables),
        )
        db_schema = body.schema_context.to_database_schema()

        gen_request = SQLGenerationRequest(
            question=body.question,
            schema_context=db_schema,
            dialect="T-SQL",
            conversation_context=body.conversation_context or [],
        )

        try:
            gen_response = generator_dep.generate(gen_request)
        except SQLGenerationError as exc:
            logger.warning("Controlled SQL generation error [%s]: %s", type(exc).__name__, exc)
            return GenerateSQLResponse(
                candidate_sql=None,
                status=GenerationStatus.GENERATION_FAILED,
                request_id=req_id,
            )
        except Exception as exc:
            logger.error("Unexpected error during SQL generation [%s]", type(exc).__name__)
            return GenerateSQLResponse(
                candidate_sql=None,
                status=GenerationStatus.GENERATION_FAILED,
                request_id=req_id,
            )

        # Cross-field application-level validation
        if gen_response.status == SQLGenerationStatus.ANSWERABLE:
            candidate = gen_response.sql.strip() if gen_response.sql else None
            if not candidate:
                return GenerateSQLResponse(
                    candidate_sql=None,
                    status=GenerationStatus.GENERATION_FAILED,
                    request_id=req_id,
                )
            if len(candidate) > 10000:
                return GenerateSQLResponse(
                    candidate_sql=None,
                    status=GenerationStatus.GENERATION_FAILED,
                    request_id=req_id,
                )

            # Primary AST and policy validation against client schema
            validator = SQLValidator(schema=db_schema)
            val_result = validator.validate(candidate)
            if not val_result.allowed:
                reason_code = val_result.reasons[0].code.value if val_result.reasons else "VALIDATION_FAILED"
                raw_msg = val_result.reasons[0].message if val_result.reasons else "Candidate SQL failed safety validation."
                safe_reason = sanitize_message(raw_msg) or "Candidate SQL failed safety validation."
                bounded_reason = f"Candidate SQL rejected by safety policy [{reason_code}]: {safe_reason[:250]}"
                return GenerateSQLResponse(
                    candidate_sql=None,
                    status=GenerationStatus.UNSUPPORTED,
                    unsupported_reason=bounded_reason,
                    request_id=req_id,
                )

            return GenerateSQLResponse(
                candidate_sql=candidate,
                status=GenerationStatus.GENERATED,
                request_id=req_id,
            )
        elif gen_response.status == SQLGenerationStatus.CLARIFICATION_NEEDED:
            prompt_text = (
                gen_response.clarification_prompt
                or gen_response.explanation
                or "Clarification is needed to answer this question."
            )
            return GenerateSQLResponse(
                candidate_sql=None,
                status=GenerationStatus.CLARIFICATION_NEEDED,
                clarification_prompt=prompt_text,
                clarification_options=gen_response.clarification_options or [],
                request_id=req_id,
            )
        elif gen_response.status == SQLGenerationStatus.UNSUPPORTED:
            reason_text = (
                gen_response.explanation
                or "The requested question is not supported by the available schema."
            )
            return GenerateSQLResponse(
                candidate_sql=None,
                status=GenerationStatus.UNSUPPORTED,
                unsupported_reason=reason_text,
                request_id=req_id,
            )
        else:
            return GenerateSQLResponse(
                candidate_sql=None,
                status=GenerationStatus.GENERATION_FAILED,
                request_id=req_id,
            )

    if concurrency_guard:
        try:
            with concurrency_guard.limit(timeout=0.0):
                result = _execute_generation()
        except ConcurrencyLimitExceeded:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Server is busy processing concurrent queries. Please retry shortly.",
            )
    else:
        result = _execute_generation()

    response.headers["X-Request-ID"] = req_id
    return result


@router.post(
    "/synthesize-answer",
    response_model=SynthesizeAnswerResponse,
    summary="Synthesize Answer From Metadata",
    description="Synthesize an answer using strictly approved metadata without row records or cell values.",
)
def synthesize_answer_endpoint(
    body: SynthesizeAnswerRequest,
    request: Request,
    response: Response,
    synthesizer_dep: AnswerSynthesisService = Depends(get_synthesizer),
) -> SynthesizeAnswerResponse:
    """Synthesize a natural-language answer using strictly approved query metadata.

    Invariants:
    - Never accepts, processes, or transmits database rows or individual cell values.
    - Rejects invalid execution statuses; synthesis only runs for successful local executions.
    - Application-level policy overrides model hallucinations to requires_row_disclosure.
    - Synthesis failures do not invalidate successful local query executions.
    """
    # 1. Establish correlated request ID
    client_req_id = request.headers.get("X-Request-ID")
    req_id = sanitize_safe_identifier(client_req_id, default="", max_len=128)
    if not req_id and body.request_id:
        req_id = sanitize_safe_identifier(body.request_id, default="", max_len=128)
    if not req_id:
        req_id = f"req-{uuid.uuid4().hex[:12]}"

    # 2. Authorization: Verify prototype access key
    app_settings = getattr(request.app.state, "settings", None)
    if app_settings is None:
        try:
            from query_pilot.config import get_settings

            app_settings = get_settings()
        except Exception as exc:
            logger.error("Unable to resolve application settings [%s]", type(exc).__name__)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Server configuration unavailable.",
            ) from None
        request.app.state.settings = app_settings

    verify_prototype_access_key(request, app_settings)

    # 3. Rate limiting check
    rate_limiter = getattr(request.app.state, "rate_limiter", None)
    if rate_limiter and not rate_limiter.acquire():
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded. Please wait before submitting another query.",
        )

    # 4. Strict execution_status invariant
    if body.execution_status.lower() != "succeeded":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Answer synthesis requires execution_status to be 'succeeded'.",
        )

    # 5. Concurrency limiting
    concurrency_guard = getattr(request.app.state, "concurrency_guard", None)

    def _execute_synthesis() -> SynthesizeAnswerResponse:
        logger.info(
            "Processing answer synthesis request [request_id=%s, columns_count=%d, row_count=%d]",
            req_id,
            len(body.columns),
            body.row_count,
        )
        summary, grounding_status, notes = synthesizer_dep.synthesize_from_metadata(
            question=body.question,
            candidate_sql=body.candidate_sql,
            columns=body.columns,
            row_count=body.row_count,
            truncated=body.truncated,
            execution_time_ms=body.execution_time_ms,
            request_id=req_id,
        )
        return SynthesizeAnswerResponse(
            summary=summary,
            status=grounding_status,
            grounding_notes=notes,
            request_id=req_id,
        )

    if concurrency_guard:
        try:
            with concurrency_guard.limit(timeout=0.0):
                result = _execute_synthesis()
        except ConcurrencyLimitExceeded:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Server is busy processing concurrent queries. Please retry shortly.",
            )
    else:
        result = _execute_synthesis()

    response.headers["X-Request-ID"] = req_id
    return result
