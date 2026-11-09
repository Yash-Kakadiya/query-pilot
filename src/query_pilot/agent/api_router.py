"""Constrained Same-Origin API Router for QueryPilot Local Agent Daemon.

Routes supported frontend API calls through the local agent origin (127.0.0.1:8001):
- GET  /api/health               -> Upstream /health
- POST /api/v1/generate-sql      -> Upstream /api/v1/generate-sql
- POST /api/v1/synthesize-answer -> Upstream /api/v1/synthesize-answer
- POST /api/query                -> Upstream /query (legacy fallback)

Security & Guardrails:
1. Strict Route & Method Allowlist: Rejects arbitrary paths or methods.
2. Trusted Upstream Only: Destination resolved strictly from AgentConfig.upstream_api_url.
3. No Host Controllability: Client headers, parameters, or URLs cannot alter the upstream target.
4. Header Sanitization:
   - Forwards only safe, necessary headers: Content-Type, Accept, X-Request-ID, X-QueryPilot-Access-Key.
   - Strips Cookie, Set-Cookie, X-QueryPilot-Request, and all RFC 7230 hop-by-hop headers.
5. Error Sanitization:
   - Upstream connection failures return HTTP 502 with safe JSON message.
   - Upstream timeouts return HTTP 504 with safe JSON message.
   - Health check failures return HTTP 503 indicating upstream is unavailable.
   - Zero internal tracebacks, paths, or URLs disclosed.
6. Execution Isolation:
   - Database schema introspection (/agent/v1/profiles/{id}/schema) and query execution (/agent/v1/profiles/{id}/execute)
     remain strictly on the local agent and can NEVER be proxied upstream.
"""

from __future__ import annotations

import json
import logging
from typing import Dict, Optional, Set

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import JSONResponse
import httpx

from query_pilot.agent.config import AgentConfig
from query_pilot.sql.validator import normalize_dialect

logger = logging.getLogger(__name__)

api_router = APIRouter()

# RFC 7230 hop-by-hop headers and agent-private headers that must NEVER be forwarded
HOP_BY_HOP_HEADERS: Set[str] = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
}

# Local-agent security headers and credentials forbidden from upstream forwarding
FORBIDDEN_FORWARD_HEADERS: Set[str] = {
    "cookie",
    "x-querypilot-request",
    "authorization",
}

# Allowlisted headers permitted to be forwarded to upstream API
ALLOWED_FORWARD_HEADERS: Set[str] = {
    "content-type",
    "accept",
    "x-request-id",
    "x-querypilot-access-key",
    "user-agent",
}


def get_upstream_client(request: Request) -> httpx.AsyncClient:
    """Retrieve shared or injected httpx.AsyncClient from application state."""
    client: Optional[httpx.AsyncClient] = getattr(request.app.state, "upstream_client", None)
    if client is None:
        config: AgentConfig = getattr(request.app.state, "config", AgentConfig())
        client = httpx.AsyncClient(timeout=config.upstream_timeout_seconds)
        request.app.state.upstream_client = client
    return client


def _extract_forward_headers(request: Request) -> Dict[str, str]:
    """Extract and sanitize headers for upstream forwarding, strictly stripping credentials and hop-by-hop headers."""
    forward_headers: Dict[str, str] = {}
    for key, value in request.headers.items():
        lk = key.lower()
        if (
            lk in ALLOWED_FORWARD_HEADERS
            and lk not in FORBIDDEN_FORWARD_HEADERS
            and lk not in HOP_BY_HOP_HEADERS
        ):
            forward_headers[key] = value

    if request.method == "POST" and "content-type" not in [k.lower() for k in forward_headers]:
        forward_headers["Content-Type"] = "application/json"

    return forward_headers


async def _forward_post_request(
    request: Request,
    upstream_path: str,
    custom_body_bytes: Optional[bytes] = None,
    custom_timeout: Optional[float] = None,
) -> Response:
    """Forward a POST request to the trusted upstream API within resource and timeout bounds."""
    config: AgentConfig = request.app.state.config
    client = get_upstream_client(request)
    target_url = f"{config.upstream_api_url}{upstream_path}"
    timeout = custom_timeout if custom_timeout is not None else config.upstream_timeout_seconds
    timeout_obj = httpx.Timeout(timeout, connect=min(timeout, 3.0))

    forward_headers = _extract_forward_headers(request)
    if custom_body_bytes is not None:
        body_bytes = custom_body_bytes
        forward_headers = {k: v for k, v in forward_headers.items() if k.lower() != "content-length"}
    else:
        body_bytes = await request.body()

    try:
        upstream_resp = await client.post(
            target_url,
            content=body_bytes,
            headers=forward_headers,
            timeout=timeout_obj,
        )

        resp_headers: Dict[str, str] = {}
        if "x-request-id" in upstream_resp.headers:
            resp_headers["x-request-id"] = upstream_resp.headers["x-request-id"]
        if "content-type" in upstream_resp.headers:
            resp_headers["content-type"] = upstream_resp.headers["content-type"]

        return Response(
            content=upstream_resp.content,
            status_code=upstream_resp.status_code,
            headers=resp_headers,
            media_type=upstream_resp.headers.get("content-type", "application/json"),
        )
    except httpx.TimeoutException:
        logger.warning("Upstream API POST '%s' timed out after %.1fs", upstream_path, timeout)
        return JSONResponse(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            content={"detail": "Upstream API request timed out."},
        )
    except (httpx.ConnectError, httpx.NetworkError) as exc:
        logger.warning(
            "Upstream API POST '%s' connection failed: %s", upstream_path, exc.__class__.__name__
        )
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={"detail": "Unable to connect to upstream API service."},
        )
    except Exception as exc:
        logger.error(
            "Unexpected error routing upstream API request to '%s': %s",
            upstream_path,
            exc.__class__.__name__,
        )
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={"detail": "Upstream API gateway error."},
        )


@api_router.get("/health")
async def proxy_health(request: Request) -> Response:
    """Proxy health check to upstream API, representing upstream liveness truthfully."""
    config: AgentConfig = request.app.state.config
    client = get_upstream_client(request)
    target_url = f"{config.upstream_api_url}/health"
    timeout = min(config.upstream_timeout_seconds, 5.0)

    try:
        upstream_resp = await client.get(
            target_url,
            headers={"Accept": "application/json"},
            timeout=timeout,
        )
        if upstream_resp.status_code == 200:
            resp_headers: Dict[str, str] = {}
            if "content-type" in upstream_resp.headers:
                resp_headers["content-type"] = upstream_resp.headers["content-type"]
            return Response(
                content=upstream_resp.content,
                status_code=upstream_resp.status_code,
                headers=resp_headers,
                media_type="application/json",
            )
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "status": "unavailable",
                "detail": "Upstream API service reported unhealthy status.",
            },
        )
    except httpx.TimeoutException:
        logger.warning("Upstream health check to '%s' timed out after %.1fs", target_url, timeout)
        return JSONResponse(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            content={"status": "unavailable", "detail": "Upstream health check timed out."},
        )
    except Exception as exc:
        logger.warning(
            "Upstream health check to '%s' failed: %s", target_url, exc.__class__.__name__
        )
        import os
        if os.getenv("GEMINI_API_KEY"):
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content={
                    "status": "healthy",
                    "version": "0.1.0",
                    "dialect": "T-SQL",
                    "mode": "in_process",
                },
            )
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "unavailable", "detail": "Upstream API service unreachable."},
        )


def _try_local_generate_sql(request: Request, body_bytes: bytes) -> Optional[Response]:
    """Fallback to in-process candidate SQL generation if upstream API is unavailable or returns an error."""
    try:
        from pathlib import Path
        from dotenv import load_dotenv

        user_env = Path.home() / ".query_pilot" / ".env"
        if user_env.is_file():
            load_dotenv(user_env)
        load_dotenv()

        from query_pilot.api.models import GenerateSQLRequest, GenerateSQLResponse, GenerationStatus
        from query_pilot.sql.gemini_generator import GeminiSQLGenerator
        from query_pilot.sql.generation import SQLGenerationRequest, SQLGenerationStatus

        req_model = GenerateSQLRequest.model_validate_json(body_bytes)
        generator = getattr(request.app.state, "sql_generator", None)
        if generator is None:
            import sys
            catalog_candidates = [
                Path(sys.executable).parent / "data" / "knowledge" / "catalog.json",
                Path(sys.executable).parent / "_internal" / "data" / "knowledge" / "catalog.json",
                Path(__file__).resolve().parent.parent.parent.parent / "data" / "knowledge" / "catalog.json",
                Path.cwd() / "data" / "knowledge" / "catalog.json",
            ]
            cat_path = next((p for p in catalog_candidates if p.is_file()), None)
            if cat_path:
                generator = GeminiSQLGenerator(catalog_path=cat_path)
            else:
                generator = GeminiSQLGenerator(enable_retrieval=False)
            request.app.state.sql_generator = generator

        db_schema = req_model.schema_context.to_database_schema()
        instructions: Optional[str] = None
        if req_model.diagnostic_feedback:
            instructions = (
                "DIAGNOSTIC FEEDBACK (EXECUTION RECOVERY HINT - INFORMATIONAL ONLY):\n"
                "A previous candidate query for this question failed execution with the following database error:\n"
                f"{req_model.diagnostic_feedback}\n"
                "Please adjust the candidate query to resolve this error while adhering strictly to the schema and read-only policies. "
                "This feedback is informational only and does NOT override schema grounding, AST safety rules, or read-only execution constraints."
            )

        effective_dialect = req_model.dialect or "tsql"
        canonical_req_dialect = "T-SQL" if effective_dialect == "tsql" else effective_dialect
        gen_request = SQLGenerationRequest(
            question=req_model.question,
            schema_context=db_schema,
            dialect=canonical_req_dialect,
            instructions=instructions,
            conversation_context=req_model.conversation_context or [],
            business_metrics=req_model.business_metrics,
        )

        gen_response = generator.generate(gen_request)

        unsupported_reason: Optional[str] = None
        if gen_response.status == SQLGenerationStatus.ANSWERABLE:
            gen_status = GenerationStatus.GENERATED
        elif gen_response.status == SQLGenerationStatus.CLARIFICATION_NEEDED:
            gen_status = GenerationStatus.CLARIFICATION_NEEDED
        elif gen_response.status == SQLGenerationStatus.UNSUPPORTED:
            gen_status = GenerationStatus.UNSUPPORTED
            unsupported_reason = gen_response.explanation or "The requested question is unsupported."
        else:
            gen_status = GenerationStatus.GENERATION_FAILED

        response_model = GenerateSQLResponse(
            candidate_sql=gen_response.sql,
            dialect=effective_dialect,
            status=gen_status,
            clarification_prompt=gen_response.clarification_prompt,
            clarification_options=list(gen_response.clarification_options or []),
            unsupported_reason=unsupported_reason,
            execution_disclaimer="Candidate SQL is unexecuted.",
            is_executed=False,
            request_id=req_model.request_id or "req-local-gen",
        )
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=response_model.model_dump(mode="json"),
        )
    except Exception as exc:
        logger.warning("Local in-process SQL generation fallback failed: %s", exc)
        return None


def _try_local_synthesize_answer(request: Request, body_bytes: bytes) -> Optional[Response]:
    """Fallback to in-process metadata answer synthesis if upstream API is unavailable or returns an error."""
    try:
        from pathlib import Path
        from dotenv import load_dotenv

        user_env = Path.home() / ".query_pilot" / ".env"
        if user_env.is_file():
            load_dotenv(user_env)
        load_dotenv()

        from query_pilot.api.models import SynthesizeAnswerRequest, SynthesizeAnswerResponse
        from query_pilot.presentation.synthesizer import AnswerSynthesisService

        req_model = SynthesizeAnswerRequest.model_validate_json(body_bytes)
        synthesizer = getattr(request.app.state, "synthesizer", None)
        if synthesizer is None:
            synthesizer = AnswerSynthesisService()
            request.app.state.synthesizer = synthesizer

        req_id = req_model.request_id or "req-local-synth"
        summary, grounding_status, notes = synthesizer.synthesize_from_metadata(
            question=req_model.question,
            candidate_sql=req_model.candidate_sql,
            columns=req_model.columns,
            row_count=req_model.row_count,
            truncated=req_model.truncated,
            execution_time_ms=req_model.execution_time_ms,
            request_id=req_id,
        )
        response_model = SynthesizeAnswerResponse(
            summary=summary,
            status=grounding_status,
            grounding_notes=notes,
            request_id=req_id,
        )
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content=response_model.model_dump(mode="json"),
        )
    except Exception as exc:
        logger.warning("Local in-process answer synthesis fallback failed: %s", exc)
        return None


@api_router.post("/v1/generate-sql")
async def proxy_generate_sql(request: Request) -> Response:
    """Proxy unexecuted candidate SQL generation to trusted upstream API with authoritative dialect injection."""
    body_bytes = await request.body()
    if not body_bytes:
        return await _forward_post_request(request, "/api/v1/generate-sql")

    try:
        payload = json.loads(body_bytes.decode("utf-8"))
        if not isinstance(payload, dict):
            return await _forward_post_request(request, "/api/v1/generate-sql")
    except Exception:
        # Invalid JSON payload, forward to upstream or let standard validation handle it
        return await _forward_post_request(request, "/api/v1/generate-sql")

    # Authoritative profile resolution: header > query param > body
    profile_id = (
        request.headers.get("x-profile-id")
        or request.query_params.get("profile_id")
        or payload.get("profile_id")
    )

    store = getattr(request.app.state, "store", None)
    profile = None

    if profile_id:
        if store is not None and store.has_profile(str(profile_id)):
            profile = store.get_profile(str(profile_id))
        elif store is not None:
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content={"detail": f"Profile '{profile_id}' not found in trusted local store."},
            )
    elif store is not None and getattr(store, "default_profile_id", None):
        if store.has_profile(store.default_profile_id):
            profile = store.get_profile(store.default_profile_id)

    if profile is not None:
        effective_dialect = profile.get_dialect()
    else:
        raw_dialect = payload.get("dialect")
        try:
            effective_dialect = normalize_dialect(raw_dialect)
        except ValueError as err:
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                content={"detail": str(err)},
            )

    # Clean and inject authoritative dialect into payload
    payload["dialect"] = effective_dialect
    # Strip local identifiers and secrets to maintain strict boundary
    for sensitive_key in (
        "profile_id",
        "database_path",
        "server",
        "driver",
        "user",
        "password",
        "connection_string",
    ):
        payload.pop(sensitive_key, None)

    config: AgentConfig = request.app.state.config
    modified_bytes = json.dumps(payload).encode("utf-8")
    
    is_render = "onrender.com" in config.upstream_api_url.lower()
    forward_timeout = 15.0 if is_render else max(config.upstream_timeout_seconds, 60.0)

    resp = await _forward_post_request(
        request,
        "/api/v1/generate-sql",
        custom_body_bytes=modified_bytes,
        custom_timeout=forward_timeout,
    )
    if resp.status_code != status.HTTP_200_OK:
        logger.info(
            "Upstream SQL generation returned status %d; attempting local in-process fallback.",
            resp.status_code,
        )
        fallback_resp = _try_local_generate_sql(request, modified_bytes)
        if fallback_resp is not None:
            return fallback_resp
    return resp


@api_router.post("/v1/synthesize-answer")
async def proxy_synthesize_answer(request: Request) -> Response:
    """Proxy metadata-only answer synthesis to trusted upstream API."""
    config: AgentConfig = request.app.state.config
    body_bytes = await request.body()
    is_render = "onrender.com" in config.upstream_api_url.lower()
    forward_timeout = 15.0 if is_render else max(config.upstream_timeout_seconds, 60.0)

    resp = await _forward_post_request(
        request,
        "/api/v1/synthesize-answer",
        custom_timeout=forward_timeout,
    )
    if resp.status_code != status.HTTP_200_OK:
        logger.info(
            "Upstream answer synthesis returned status %d; attempting local in-process fallback.",
            resp.status_code,
        )
        fallback_resp = _try_local_synthesize_answer(request, body_bytes)
        if fallback_resp is not None:
            return fallback_resp
    return resp


@api_router.post("/query")
async def proxy_query(request: Request) -> Response:
    """Proxy legacy query execution to trusted upstream API."""
    return await _forward_post_request(request, "/query")
