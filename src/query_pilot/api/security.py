"""Security, authorization, and resource limiting utilities for QueryPilot API.

Provides:
- Constant-time verification of runtime prototype access codes via X-QueryPilot-Access-Key.
- In-memory sliding-window request rate limiter across the prototype (prevents quota exhaustion).
- Bounded concurrency guard for active query execution (prevents container memory/thread exhaustion).
- Request payload size validation independent of upstream reverse proxies.
"""

from contextlib import contextmanager
import json
import logging
import secrets
import threading
import time
from typing import Any, Callable, List, Optional

from fastapi import HTTPException, Request, status

logger = logging.getLogger("query_pilot.api.security")

# Canonical request header for the shared prototype access key
ACCESS_KEY_HEADER = "X-QueryPilot-Access-Key"


class ConcurrencyLimitExceeded(Exception):
    """Raised when active concurrent requests exceed the configured ceiling."""


class RateLimitExceeded(Exception):
    """Raised when requests within the sliding window exceed the configured rate."""


class PayloadTooLargeError(HTTPException):
    """Raised when an incoming request body exceeds the configured byte ceiling."""

    def __init__(self, max_bytes: int):
        super().__init__(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"Request payload exceeds maximum allowed size of {max_bytes} bytes.",
        )
        self.max_bytes = max_bytes


class StreamingPayloadSizeMiddleware:
    """Pure ASGI middleware enforcing bounded request payload sizes while streaming.

    Protects memory-constrained deployments by counting incoming request-body bytes
    as chunks arrive over ASGI, rather than buffering the entire payload into memory.

    Enforcement:
    1. Early Rejection: If the Content-Length header is present, valid, and exceeds max_bytes,
       immediately returns HTTP 413 without reading the body.
    2. Streaming Count: If Content-Length is absent (chunked), invalid, or understated,
       wraps the ASGI receive callable to count bytes chunk-by-chunk. As soon as
       accumulated bytes exceed max_bytes, raises PayloadTooLargeError to abort
       body streaming without buffering subsequent chunks.
    3. Lifecycle Safety: Tracks response emission to prevent illegal ASGI duplicate
       response starts, and preserves sanitized X-Request-ID headers.
    """

    def __init__(
        self,
        app: Any,
        max_bytes: Optional[int] = None,
        get_max_bytes: Optional[Callable[[], int]] = None,
    ):
        self.app = app
        self._max_bytes = max_bytes
        self._get_max_bytes = get_max_bytes

    def _resolve_max_bytes(self, scope: dict) -> int:
        if self._max_bytes is not None:
            return self._max_bytes
        if self._get_max_bytes is not None:
            try:
                return self._get_max_bytes()
            except Exception:
                pass
        app_instance = scope.get("app")
        if app_instance is not None:
            settings = getattr(getattr(app_instance, "state", None), "settings", None)
            if settings is not None:
                return getattr(settings, "QUERY_PILOT_MAX_REQUEST_BYTES", 65536)
        try:
            from query_pilot.config import get_settings

            return get_settings().QUERY_PILOT_MAX_REQUEST_BYTES
        except Exception:
            return 65536

    async def _send_413(self, send: Any, max_bytes: int, req_id: str = "") -> None:
        body = json.dumps(
            {"detail": f"Request payload exceeds maximum allowed size of {max_bytes} bytes."}
        ).encode("utf-8")
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("latin1")),
        ]
        if req_id:
            headers.append((b"x-request-id", req_id.encode("latin1")))

        await send({
            "type": "http.response.start",
            "status": status.HTTP_413_CONTENT_TOO_LARGE,
            "headers": headers,
        })
        await send({
            "type": "http.response.body",
            "body": body,
            "more_body": False,
        })

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        # Skip body checks on OPTIONS preflights
        if scope.get("method") == "OPTIONS":
            await self.app(scope, receive, send)
            return

        max_bytes = self._resolve_max_bytes(scope)

        # Extract Content-Length and X-Request-ID from ASGI headers
        content_length_val: Optional[int] = None
        req_id: str = ""

        for header_name, header_val in scope.get("headers", []):
            name_lower = header_name.lower()
            if name_lower == b"content-length":
                try:
                    val_str = header_val.decode("latin1").strip()
                    parsed_len = int(val_str)
                    if parsed_len >= 0:
                        content_length_val = parsed_len
                except (ValueError, UnicodeDecodeError):
                    content_length_val = None
            elif name_lower == b"x-request-id":
                try:
                    req_id_str = header_val.decode("latin1").strip()
                    from query_pilot.tracing import sanitize_safe_identifier

                    req_id = sanitize_safe_identifier(req_id_str, default="")
                except Exception:
                    req_id = ""

        # 1. Early rejection if Content-Length header is definitively oversized
        if content_length_val is not None and content_length_val > max_bytes:
            logger.warning(
                "Request rejected by Content-Length limit [declared=%d, max=%d, path=%s]",
                content_length_val,
                max_bytes,
                scope.get("path", ""),
            )
            await self._send_413(send, max_bytes=max_bytes, req_id=req_id)
            return

        # 2. Streaming chunk count: enforce max_bytes as ASGI chunks arrive
        accumulated_bytes = 0
        response_started = False

        async def custom_send(message: dict) -> None:
            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        async def custom_receive() -> dict:
            nonlocal accumulated_bytes
            message = await receive()
            if message.get("type") == "http.request":
                chunk = message.get("body", b"")
                accumulated_bytes += len(chunk)
                if accumulated_bytes > max_bytes:
                    logger.warning(
                        "Request rejected while streaming: accumulated bytes exceeded limit [accumulated=%d, max=%d, path=%s]",
                        accumulated_bytes,
                        max_bytes,
                        scope.get("path", ""),
                    )
                    raise PayloadTooLargeError(max_bytes=max_bytes)
            return message

        try:
            await self.app(scope, custom_receive, custom_send)
        except PayloadTooLargeError:
            if not response_started:
                await self._send_413(send, max_bytes=max_bytes, req_id=req_id)


class InMemoryRateLimiter:
    """Thread-safe, sliding-window in-memory rate limiter for the prototype.

    Tracks request timestamps within a 60-second window. Does not depend on external
    services (such as Redis) and operates safely within a single process.
    Note: In-memory state resets when the host container spins down or restarts.
    """

    def __init__(self, requests_per_minute: int = 20):
        self.requests_per_minute = max(1, requests_per_minute)
        self._timestamps: List[float] = []
        self._lock = threading.Lock()

    def acquire(self) -> bool:
        """Attempt to acquire a request token within the 60-second sliding window.

        Returns:
            True if within rate limit; False if limit exceeded.
        """
        now = time.monotonic()
        window_start = now - 60.0

        with self._lock:
            # Prune timestamps outside the 60-second window
            self._timestamps = [t for t in self._timestamps if t > window_start]
            if len(self._timestamps) >= self.requests_per_minute:
                return False
            self._timestamps.append(now)
            return True

    def reset(self) -> None:
        """Clear all recorded timestamps (useful for isolated unit testing)."""
        with self._lock:
            self._timestamps.clear()


class ConcurrencyGuard:
    """Thread-safe concurrency limiter bounding simultaneous query executions.

    Protects memory-constrained environments (e.g. Render 512MB free-tier containers)
    from thread and connection exhaustion during long-running LLM and database calls.
    """

    def __init__(self, max_concurrent: int = 3):
        self.max_concurrent = max(1, max_concurrent)
        self._semaphore = threading.BoundedSemaphore(self.max_concurrent)

    def acquire(self, timeout: float = 0.0) -> bool:
        """Attempt to acquire execution permission.

        Args:
            timeout: Maximum seconds to block waiting for a slot (default 0.0: non-blocking).

        Returns:
            True if acquired; False if busy.
        """
        return self._semaphore.acquire(blocking=True, timeout=timeout)

    def release(self) -> None:
        """Release an acquired execution slot."""
        try:
            self._semaphore.release()
        except ValueError:
            pass

    @contextmanager
    def limit(self, timeout: float = 0.0):
        """Context manager for acquiring and safely releasing concurrency slot."""
        acquired = self.acquire(timeout=timeout)
        if not acquired:
            raise ConcurrencyLimitExceeded(
                "Server is busy processing concurrent queries. Please retry shortly."
            )
        try:
            yield
        finally:
            self.release()


def verify_prototype_access_key(request: Request, settings: Optional[Any] = None) -> None:
    """Verify the shared prototype access key using constant-time comparison.

    If QUERY_PILOT_ACCESS_KEY is configured on settings, checks for the presence
    and validity of the X-QueryPilot-Access-Key request header.

    If the access key is missing or invalid, raises HTTP 401 Unauthorized immediately
    before any LLM, pipeline, or database work occurs.

    If QUERY_PILOT_ACCESS_KEY is None or empty, authorization is considered inactive
    (e.g. for unauthenticated local development or test harnesses).
    """
    app_settings = settings
    if app_settings is None:
        app_settings = getattr(request.app.state, "settings", None)
    if app_settings is None:
        try:
            from query_pilot.config import get_settings
            app_settings = get_settings()
        except Exception as exc:
            logger.error(
                "Security check failed: unable to resolve application settings [%s]",
                type(exc).__name__,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Security configuration unavailable.",
            ) from None

    if not app_settings:
        logger.error("Security check failed: application settings are unavailable.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Security configuration unavailable.",
        )

    expected_secret = getattr(app_settings, "QUERY_PILOT_ACCESS_KEY", None)
    expected_val = (
        expected_secret.get_secret_value()
        if hasattr(expected_secret, "get_secret_value")
        else (str(expected_secret) if expected_secret is not None else "")
    )
    expected_clean = expected_val.strip()

    require_key = bool(getattr(app_settings, "QUERY_PILOT_REQUIRE_ACCESS_KEY", False))
    if require_key and not expected_clean:
        logger.error(
            "Security check failed: QUERY_PILOT_REQUIRE_ACCESS_KEY is enabled but key is missing."
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Security configuration unavailable.",
        )

    if not expected_clean:
        return

    provided_val = request.headers.get(ACCESS_KEY_HEADER)
    if not provided_val or not secrets.compare_digest(provided_val.strip(), expected_clean):
        logger.warning(
            "Unauthorized request rejected: missing or invalid access key [path=%s]",
            request.url.path,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing access key.",
        )
