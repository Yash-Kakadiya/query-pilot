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

import logging
from typing import Dict, Optional, Set

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import JSONResponse
import httpx

from query_pilot.agent.config import AgentConfig

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


async def _forward_post_request(request: Request, upstream_path: str) -> Response:
    """Forward a POST request to the trusted upstream API within resource and timeout bounds."""
    config: AgentConfig = request.app.state.config
    client = get_upstream_client(request)
    target_url = f"{config.upstream_api_url}{upstream_path}"
    timeout = config.upstream_timeout_seconds

    forward_headers = _extract_forward_headers(request)
    body_bytes = await request.body()

    try:
        upstream_resp = await client.post(
            target_url,
            content=body_bytes,
            headers=forward_headers,
            timeout=timeout,
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
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "unavailable", "detail": "Upstream API service unreachable."},
        )


@api_router.post("/v1/generate-sql")
async def proxy_generate_sql(request: Request) -> Response:
    """Proxy unexecuted candidate SQL generation to trusted upstream API."""
    return await _forward_post_request(request, "/api/v1/generate-sql")


@api_router.post("/v1/synthesize-answer")
async def proxy_synthesize_answer(request: Request) -> Response:
    """Proxy metadata-only answer synthesis to trusted upstream API."""
    return await _forward_post_request(request, "/api/v1/synthesize-answer")


@api_router.post("/query")
async def proxy_query(request: Request) -> Response:
    """Proxy legacy query execution to trusted upstream API."""
    return await _forward_post_request(request, "/query")
