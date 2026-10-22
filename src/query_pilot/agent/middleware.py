"""Security middleware for QueryPilot Local Agent Daemon.

Enforces:
1. Strict Host-header validation (DNS rebinding defense).
2. Origin-header validation (browser cross-origin defense, packaged vs dev mode).
3. Custom request header validation (X-QueryPilot-Request: 1) on state-changing requests.
4. Hardened response security headers (CSP, Cache-Control, Referrer-Policy, nosniff, DENY).
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional, Set

from query_pilot.agent.config import AgentConfig


def _make_error_response_bytes(status_code: int, error_code: str, detail: str) -> tuple[int, list[tuple[bytes, bytes]], bytes]:
    """Helper creating raw ASGI response components for early middleware rejections."""
    payload = json.dumps({"error": error_code, "detail": detail}).encode("utf-8")
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(payload)).encode("ascii")),
        (b"cache-control", b"no-store, no-cache, must-revalidate, max-age=0"),
        (b"pragma", b"no-cache"),
        (b"referrer-policy", b"no-referrer"),
    ]
    return status_code, headers, payload


class HostOriginValidationMiddleware:
    """Pure ASGI middleware enforcing loopback Host and Origin validation."""

    def __init__(self, app: Any, config: AgentConfig):
        self.app = app
        self.config = config
        port = config.bind_port

        # Explicit loopback host allowlist
        self.allowed_hosts: Set[str] = {
            f"127.0.0.1:{port}",
            f"localhost:{port}",
            f"[::1]:{port}",
            "127.0.0.1",
            "localhost",
            "[::1]",
        }

        # Packaged mode: only agent's own loopback origin
        self.allowed_origins: Set[str] = {
            f"http://127.0.0.1:{port}",
            f"http://localhost:{port}",
        }

        # Development mode: optionally allow Vite dev server
        if config.allow_dev_origin:
            self.allowed_origins.add("http://127.0.0.1:5173")
            self.allowed_origins.add("http://localhost:5173")

    async def __call__(self, scope: Dict[str, Any], receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        method = scope.get("method", "GET").upper()

        # 1. Host Header Validation (DNS Rebinding Defense)
        raw_host = headers.get(b"host", b"").decode("latin-1").strip().lower()
        if not raw_host or raw_host not in self.allowed_hosts:
            status_code, resp_headers, payload = _make_error_response_bytes(
                400,
                "INVALID_HOST",
                f"Host '{raw_host}' is not permitted. Only loopback hosts are accepted.",
            )
            await send({"type": "http.response.start", "status": status_code, "headers": resp_headers})
            await send({"type": "http.response.body", "body": payload})
            return

        # 2. Origin Header Validation (Cross-Origin Defense)
        raw_origin = headers.get(b"origin", b"").decode("latin-1").strip().lower()
        if raw_origin:
            if raw_origin not in self.allowed_origins:
                status_code, resp_headers, payload = _make_error_response_bytes(
                    403,
                    "FORBIDDEN_ORIGIN",
                    f"Origin '{raw_origin}' is not permitted.",
                )
                await send({"type": "http.response.start", "status": status_code, "headers": resp_headers})
                await send({"type": "http.response.body", "body": payload})
                return

        # 3. Custom Header Requirement for State-Changing Requests (CSRF Defense on Agent Endpoints)
        path = scope.get("path", "")
        if method in {"POST", "PUT", "DELETE", "PATCH"} and path.startswith("/agent/"):
            raw_custom = headers.get(b"x-querypilot-request", b"").decode("latin-1").strip()
            if raw_custom != "1":
                status_code, resp_headers, payload = _make_error_response_bytes(
                    400,
                    "MISSING_CUSTOM_HEADER",
                    "State-changing requests require 'X-QueryPilot-Request: 1' header.",
                )
                await send({"type": "http.response.start", "status": status_code, "headers": resp_headers})
                await send({"type": "http.response.body", "body": payload})
                return

        await self.app(scope, receive, send)


class SecurityHeadersMiddleware:
    """Pure ASGI middleware appending mandatory security headers to every response."""

    def __init__(self, app: Any, config: Optional[AgentConfig] = None):
        self.app = app
        self.config = config or AgentConfig()

        connect_src = "'self' https:"
        if self.config.allow_dev_origin:
            connect_src = "'self' http://127.0.0.1:8000 http://localhost:8000 https:"

        csp_policy = (
            f"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            f"font-src 'self' data:; connect-src {connect_src}; img-src 'self' data:; frame-ancestors 'none';"
        )

        # Hardened headers matching architecture specification 6.1 and 6.3
        self.security_headers = [
            (b"cache-control", b"no-store, no-cache, must-revalidate, max-age=0"),
            (b"pragma", b"no-cache"),
            (b"referrer-policy", b"no-referrer"),
            (b"content-security-policy", csp_policy.encode("latin-1")),
            (b"x-content-type-options", b"nosniff"),
            (b"x-frame-options", b"DENY"),
        ]

    async def __call__(self, scope: Dict[str, Any], receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message: Dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                existing_headers: List[tuple[bytes, bytes]] = list(message.get("headers", []))
                existing_keys = {k.lower() for k, _ in existing_headers}

                for sec_key, sec_val in self.security_headers:
                    if sec_key not in existing_keys:
                        existing_headers.append((sec_key, sec_val))

                message["headers"] = existing_headers

            await send(message)

        await self.app(scope, receive, send_wrapper)
