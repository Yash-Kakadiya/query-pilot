"""FastAPI application factory for the QueryPilot Local Agent Daemon.

Exposes:
- POST /agent/v1/auth/bootstrap: Single-use bootstrap token exchange establishing session cookie.
- POST /agent/v1/auth/logout: Explicit session destruction and cookie clearing.
- POST /agent/v1/auth/rotate-token: Token rotation invalidating all active sessions.
- GET  /agent/v1/profiles: Redacted list of approved connection profiles (zero credentials).
- GET  /agent/v1/profiles/{profile_id}/schema: Model-facing dynamic schema introspection.

Guarantees:
- Runs strictly on loopback (127.0.0.1 / ::1); non-loopback bind hosts fail closed.
- DNS rebinding defense via strict Host-header validation.
- Origin validation (packaged mode by default, dev-origin opt-in).
- SameSite=Strict, HttpOnly session cookie with 1-hr idle and 12-hr absolute limits.
- Mandatory X-QueryPilot-Request: 1 header for state-changing requests.
- Concurrency limit (2 simultaneous requests ceiling).
- 64 KB request body ceiling.
- Sanitized errors; zero credentials, connection strings, or raw tracebacks leaked.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
import logging
import mimetypes
import threading
from typing import Any, Callable, Dict, Optional, Union

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
import httpx
from sqlalchemy.engine import Engine

from query_pilot.agent.api_router import api_router
from query_pilot.agent.auth import (
    SESSION_COOKIE_NAME,
    BootstrapTokenManager,
    Session,
    SessionManager,
)
from query_pilot.agent.config import AgentConfig
from query_pilot.agent.engine_manager import AgentEngineManager
from query_pilot.agent.middleware import (
    HostOriginValidationMiddleware,
    SecurityHeadersMiddleware,
    _make_error_response_bytes,
)
from query_pilot.agent.models import (
    AgentErrorResponse,
    BootstrapRequest,
    BootstrapResponse,
    ExecuteQueryRequest,
    ExecuteQueryResponse,
    LogoutResponse,
    ProfileListResponse,
    ProfileSummary,
    RotateTokenResponse,
)
from query_pilot.api.security import StreamingPayloadSizeMiddleware
from query_pilot.db.connection import DatabaseConnectionError, DatabaseSecurityError
from query_pilot.db.executor import QueryTimeoutError, UnsafeQueryError
from query_pilot.db.factory import ProfileConnectionFactory
from query_pilot.db.models import DatabaseSchema
from query_pilot.db.profile import (
    ConnectionTargetOverrideError,
    ProfileStore,
    UnknownProfileError,
    load_trusted_profiles_file,
)
from query_pilot.db.profile_executor import (
    ProfileExecutionBoundary,
    ProfileExecutionError,
)
from query_pilot.db.profile_introspector import (
    ProfileDatabaseIntrospector,
    ProfileIntrospectionError,
)
from query_pilot.presentation.formatter import sanitize_message

logger = logging.getLogger(__name__)


class ConcurrencyGuardMiddleware:
    """Pure ASGI middleware enforcing concurrency ceilings across local agent requests."""

    def __init__(self, app: Any, max_concurrent: int = 2):
        self.app = app
        self._max = max_concurrent
        self._current = 0
        self._lock = threading.Lock()

    async def __call__(self, scope: Dict[str, Any], receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        with self._lock:
            if self._current >= self._max:
                status_code, resp_headers, payload = _make_error_response_bytes(
                    429,
                    "CONCURRENCY_LIMIT_EXCEEDED",
                    f"Agent concurrency limit exceeded ({self._max} active requests).",
                )
                await send({"type": "http.response.start", "status": status_code, "headers": resp_headers})
                await send({"type": "http.response.body", "body": payload})
                return
            self._current += 1

        try:
            await self.app(scope, receive, send)
        finally:
            with self._lock:
                self._current -= 1


def create_agent_app(
    config: Optional[AgentConfig] = None,
    store: Optional[ProfileStore] = None,
    factory: Optional[ProfileConnectionFactory] = None,
    engine_manager: Optional[AgentEngineManager] = None,
    token_manager: Optional[BootstrapTokenManager] = None,
    session_manager: Optional[SessionManager] = None,
    caller_engine: Optional[Engine] = None,
    caller_schema: Optional[Union[DatabaseSchema, Dict[str, DatabaseSchema]]] = None,
    upstream_client: Optional[httpx.AsyncClient] = None,
) -> FastAPI:
    """Instantiate and configure the QueryPilot Local Agent FastAPI application.

    Args:
        config: Agent configuration specifying bind host, port, timeouts, and origins.
        store: Trusted ProfileStore managing local database connection profiles.
        factory: ProfileConnectionFactory creating engine configurations.
        engine_manager: Manager tracking engine pools and deterministic disposal.
        token_manager: Manager handling single-use bootstrap tokens.
        session_manager: Manager handling active authenticated loopback sessions.
        caller_engine: Optional pre-configured Engine (e.g. mock for offline testing).
        caller_schema: Optional pre-configured DatabaseSchema or map of profile_id to DatabaseSchema.
        upstream_client: Optional pre-configured httpx.AsyncClient (e.g. mock for offline routing tests).
    """
    resolved_config = config or AgentConfig()

    # Fail closed if bind_host is not strictly loopback
    resolved_config.validate_bind_host(resolved_config.bind_host)

    if store is not None:
        resolved_store = store
    else:
        profiles_path = resolved_config.resolved_profiles_file
        if profiles_path.exists():
            resolved_store = load_trusted_profiles_file(profiles_path)
            logger.info("Loaded %d database profile(s) from '%s'", len(resolved_store), profiles_path)
        else:
            logger.info(
                "No database profiles file found at '%s'. Starting agent with empty profile store. "
                "Register a profile using 'querypilot-agent profile add'.",
                profiles_path,
            )
            resolved_store = ProfileStore()
    resolved_factory = factory or ProfileConnectionFactory(store=resolved_store)
    resolved_engine_mgr = engine_manager or AgentEngineManager(factory=resolved_factory)
    resolved_token_mgr = token_manager or BootstrapTokenManager(
        token_dir=resolved_config.token_dir,
        token_filename=resolved_config.token_filename,
    )
    resolved_session_mgr = session_manager or SessionManager(
        idle_timeout_seconds=resolved_config.session_idle_seconds,
        max_lifetime_seconds=resolved_config.session_max_lifetime_seconds,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Startup: provision bootstrap token if not present
        if not resolved_token_mgr.has_active_token:
            resolved_token_mgr.initialize_token()
        # Initialize owned upstream client if not injected
        owned_client: Optional[httpx.AsyncClient] = None
        if app.state.upstream_client is None:
            owned_client = httpx.AsyncClient(timeout=resolved_config.upstream_timeout_seconds)
            app.state.upstream_client = owned_client
        yield
        # Shutdown: deterministic cleanup of daemon-owned client, engines and sessions
        if owned_client is not None:
            await owned_client.aclose()
        resolved_engine_mgr.dispose_all()
        resolved_session_mgr.invalidate_all()

    app = FastAPI(
        title="QueryPilot Local Agent",
        version="0.1.0",
        description="Local-agent daemon for authenticated profile discovery and dynamic schema introspection.",
        lifespan=lifespan,
    )

    # --------------------------------------------------------------------------
    # Store instances in app.state for access and testing
    # --------------------------------------------------------------------------
    app.state.config = resolved_config
    app.state.store = resolved_store
    app.state.factory = resolved_factory
    app.state.engine_manager = resolved_engine_mgr
    app.state.token_manager = resolved_token_mgr
    app.state.session_manager = resolved_session_mgr
    app.state.caller_engine = caller_engine
    app.state.caller_schema = caller_schema
    app.state.upstream_client = upstream_client

    # --------------------------------------------------------------------------
    # Security Middleware Stack (Evaluated in reverse addition order)
    # --------------------------------------------------------------------------
    # 1. Concurrency Ceiling Guard (innermost)
    app.add_middleware(
        ConcurrencyGuardMiddleware,
        max_concurrent=resolved_config.max_concurrent_requests,
    )
    # 2. Hardened Response Security Headers
    app.add_middleware(
        SecurityHeadersMiddleware,
        config=resolved_config,
    )
    # 3. Request Payload Size Ceiling (64 KB)
    app.add_middleware(
        StreamingPayloadSizeMiddleware,
        max_bytes=resolved_config.max_request_bytes,
    )
    # 4. Host and Origin Validation (outermost)
    app.add_middleware(
        HostOriginValidationMiddleware,
        config=resolved_config,
    )

    # --------------------------------------------------------------------------
    # Authentication Dependency
    # --------------------------------------------------------------------------
    def get_current_session(request: Request) -> Session:
        """Authenticate request via ambient loopback session cookie."""
        cookie_val = request.cookies.get(SESSION_COOKIE_NAME)
        if not cookie_val:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required. Please authenticate via /agent/v1/auth/bootstrap.",
            )

        session = resolved_session_mgr.get_valid_session(cookie_val)
        if session is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Session has expired or is invalid. Please re-authenticate.",
            )

        return session

    # --------------------------------------------------------------------------
    # Endpoints
    # --------------------------------------------------------------------------

    @app.post(
        "/agent/v1/auth/bootstrap",
        response_model=BootstrapResponse,
        responses={
            400: {"model": AgentErrorResponse},
            401: {"model": AgentErrorResponse},
        },
    )
    def bootstrap_auth(request: Request, body: BootstrapRequest, response: Response):
        """Exchange single-use bootstrap token for an ambient HttpOnly loopback session cookie."""
        is_valid = resolved_token_mgr.verify_and_consume(body.bootstrap_token)
        if not is_valid:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid, expired, or already-consumed bootstrap token.",
            )

        session = resolved_session_mgr.create_session()
        response.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=session.session_id,
            httponly=True,
            samesite="strict",
            path="/",
            secure=False,  # Loopback HTTP does not require TLS
        )
        return BootstrapResponse()

    @app.post(
        "/agent/v1/auth/logout",
        response_model=LogoutResponse,
        responses={401: {"model": AgentErrorResponse}},
    )
    def logout(response: Response, session: Session = Depends(get_current_session)):
        """Explicitly destroy active session and clear ambient session cookie."""
        resolved_session_mgr.invalidate_session(session.session_id)
        response.delete_cookie(
            key=SESSION_COOKIE_NAME,
            path="/",
            httponly=True,
            samesite="strict",
        )
        return LogoutResponse()

    @app.post(
        "/agent/v1/auth/rotate-token",
        response_model=RotateTokenResponse,
    )
    def rotate_token(response: Response):
        """Invalidate all active sessions and generate a fresh bootstrap token."""
        resolved_session_mgr.invalidate_all()
        resolved_token_mgr.rotate_token()
        response.delete_cookie(
            key=SESSION_COOKIE_NAME,
            path="/",
            httponly=True,
            samesite="strict",
        )
        return RotateTokenResponse()

    @app.get(
        "/agent/v1/profiles",
        response_model=ProfileListResponse,
        responses={401: {"model": AgentErrorResponse}},
    )
    def list_profiles(session: Session = Depends(get_current_session)):
        """Return safe, redacted summaries of approved local connection profiles.

        Never exposes credentials, passwords, raw connection strings, or server hostnames.
        """
        summaries = []
        for profile in resolved_store.profiles.values():
            summaries.append(
                ProfileSummary(
                    profile_id=profile.profile_id,
                    display_name=profile.display_name,
                    schema_name=profile.schema_name or "dbo",
                    timeout_seconds=profile.timeout_seconds,
                    max_row_limit=profile.max_row_limit,
                    has_allowlist=profile.allowed_tables is not None,
                    allowed_tables=list(profile.allowed_tables) if profile.allowed_tables is not None else None,
                )
            )
        return ProfileListResponse(profiles=summaries)

    @app.get(
        "/agent/v1/profiles/{profile_id}/schema",
        responses={
            400: {"model": AgentErrorResponse},
            401: {"model": AgentErrorResponse},
            403: {"model": AgentErrorResponse},
            404: {"model": AgentErrorResponse},
            500: {"model": AgentErrorResponse},
        },
    )
    def get_profile_schema(
        profile_id: str,
        session: Session = Depends(get_current_session),
    ):
        """Introspect database schema for an approved profile and return model-facing metadata.

        Enforces:
        - Unknown profiles fail closed (404); never falls back to GradeSense_Local.
        - Runtime session verification (SELECT DB_NAME() == profile.database_name).
        - Sensitive-column redaction (canonical defaults + additive profile denylist).
        - Profile table allowlist enforcement.
        - Sanitized error reporting with zero credential or connection-string leakage.
        """
        try:
            profile = resolved_store.get_profile(profile_id)
        except UnknownProfileError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Profile '{profile_id}' not found.",
            )

        # Resolve engine (uses caller_engine if injected for offline testing)
        engine = resolved_engine_mgr.get_or_create_engine(
            profile_id=profile_id,
            caller_engine=caller_engine,
        )

        introspector = ProfileDatabaseIntrospector(
            config=profile,
            engine=engine,
            factory=resolved_factory,
        )

        try:
            model_schema = introspector.introspect_model_facing()
            return model_schema.to_dict()
        except DatabaseSecurityError as exc:
            sanitized = sanitize_message(str(exc))
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=sanitized,
            )
        except (DatabaseConnectionError, ProfileIntrospectionError) as exc:
            sanitized = sanitize_message(str(exc))
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=sanitized,
            )
        except ConnectionTargetOverrideError as exc:
            sanitized = sanitize_message(str(exc))
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=sanitized,
            )
        except Exception as exc:
            sanitized = sanitize_message(str(exc))
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Schema introspection failed.",
            )

    @app.post(
        "/agent/v1/profiles/{profile_id}/execute",
        response_model=ExecuteQueryResponse,
        responses={
            400: {"model": AgentErrorResponse},
            401: {"model": AgentErrorResponse},
            403: {"model": AgentErrorResponse},
            404: {"model": AgentErrorResponse},
            422: {"model": AgentErrorResponse},
            500: {"model": AgentErrorResponse},
            504: {"model": AgentErrorResponse},
        },
    )
    def execute_profile_query(
        profile_id: str,
        body: ExecuteQueryRequest,
        session: Session = Depends(get_current_session),
    ):
        """Validate and execute a read-only SQL query against an approved connection profile.

        Enforces strict execution sequence & security boundaries:
        1. Authenticates request via ambient loopback session cookie (HTTP 401).
        2. Resolves profile strictly via ProfileStore; unknown profiles fail closed (HTTP 404).
        3. Obtains or verifies active profile schema; rejects cross-profile schema mismatches.
        4. Validates SQL AST against profile schema, table allowlist, and sensitive columns (HTTP 400).
        5. Acquires managed connection from AgentEngineManager.
        6. Verifies runtime database identity (SELECT DB_NAME() == profile.database_name) (HTTP 403).
        7. Enforces driver-level query timeout and bounded fetch (effective_rows + 1 <= 101).
        8. Executes read-only query and formats rows into JSON-safe serialized dictionaries.
        9. Cleans up cursor and connection resources deterministically in finally blocks.
        """
        try:
            profile = resolved_store.get_profile(profile_id)
        except UnknownProfileError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Profile '{profile_id}' not found.",
            )

        # Resolve engine (uses caller_engine if injected for offline testing)
        engine = resolved_engine_mgr.get_or_create_engine(
            profile_id=profile_id,
            caller_engine=caller_engine,
        )

        # Resolve active schema strictly bound to this profile
        effective_caller_schema = getattr(app.state, "caller_schema", caller_schema)
        active_schema: Optional[DatabaseSchema] = None
        if effective_caller_schema is not None:
            if isinstance(effective_caller_schema, DatabaseSchema):
                if effective_caller_schema.database_name.lower() != profile.database_name.lower():
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Provided schema database '{effective_caller_schema.database_name}' does not match profile expected database '{profile.database_name}'.",
                    )
                active_schema = effective_caller_schema
            elif isinstance(effective_caller_schema, dict):
                cand = effective_caller_schema.get(profile_id)
                if cand is not None:
                    if cand.database_name.lower() != profile.database_name.lower():
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"Provided schema database '{cand.database_name}' does not match profile expected database '{profile.database_name}'.",
                        )
                    active_schema = cand

        if active_schema is None:
            try:
                introspector = ProfileDatabaseIntrospector(
                    config=profile,
                    engine=engine,
                    factory=resolved_factory,
                )
                active_schema = introspector.introspect_model_facing()
            except DatabaseSecurityError as exc:
                sanitized = sanitize_message(str(exc))
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=sanitized,
                )
            except (DatabaseConnectionError, ProfileIntrospectionError) as exc:
                sanitized = sanitize_message(str(exc))
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=sanitized,
                )
            except Exception as exc:
                sanitized = sanitize_message(str(exc))
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Schema introspection failed.",
                )

        # Instantiate execution boundary contract
        try:
            boundary = ProfileExecutionBoundary(
                config=profile,
                schema=active_schema,
                engine=engine,
                factory=resolved_factory,
            )
        except Exception as exc:
            sanitized = sanitize_message(str(exc))
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=sanitized,
            )

        # Execute query within resource bounds
        try:
            result = boundary.execute(
                sql=body.sql,
            )
            return ExecuteQueryResponse(
                columns=result.columns,
                rows=result.rows,
                row_count=result.row_count,
                truncated=result.truncated,
                execution_time_ms=result.execution_time_ms,
                request_id=body.request_id,
            )
        except UnsafeQueryError as exc:
            sanitized = sanitize_message(str(exc))
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=sanitized,
            ) from None
        except DatabaseSecurityError as exc:
            sanitized = sanitize_message(str(exc))
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=sanitized,
            ) from None
        except QueryTimeoutError as exc:
            sanitized = sanitize_message(str(exc))
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail=sanitized,
            ) from None
        except (DatabaseConnectionError, ProfileExecutionError) as exc:
            sanitized = sanitize_message(str(exc))
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=sanitized,
            ) from None
        except ConnectionTargetOverrideError as exc:
            sanitized = sanitize_message(str(exc))
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=sanitized,
            ) from None
        except Exception as exc:
            sanitized = sanitize_message(str(exc))
            logger.error("Unexpected query execution error for profile '%s': %s", profile_id, sanitized)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Query execution failed.",
            ) from None

    # --------------------------------------------------------------------------
    # Production Frontend Static Serving & SPA Fallback
    # --------------------------------------------------------------------------

    @app.get("/", include_in_schema=False)
    @app.get("/app", include_in_schema=False)
    def redirect_to_app():
        """Redirect bare root and /app to /app/."""
        return RedirectResponse(url="/app/", status_code=status.HTTP_307_TEMPORARY_REDIRECT)

    @app.get("/app/", include_in_schema=False)
    @app.get("/app/{subpath:path}", include_in_schema=False)
    def serve_frontend(subpath: str = ""):
        """Serve production frontend assets and SPA routes under /app/.

        Behaviors:
        - If the configured static_dir or index.html is missing, returns actionable HTTP 503 HTML.
        - Verifies that target files resolve strictly within static_dir to prevent path traversal (HTTP 403).
        - If subpath points to an existing file, serves it with appropriate media type.
        - If subpath has a file extension or is within assets/ but does not exist, returns HTTP 404 (does not swallow missing assets).
        - If subpath is a client-side route (no extension), serves index.html for SPA routing.
        """
        static_dir = resolved_config.resolved_static_dir

        if not static_dir.exists() or not static_dir.is_dir():
            return HTMLResponse(
                content=(
                    "<!DOCTYPE html>\n"
                    "<html>\n"
                    "<head><title>Frontend Build Not Found - QueryPilot</title></head>\n"
                    "<body style=\"font-family: sans-serif; padding: 2rem; max-width: 600px; margin: auto; line-height: 1.5;\">\n"
                    "  <h2>QueryPilot Frontend Build Not Found</h2>\n"
                    "  <p>The production frontend assets were not found in the static assets directory (e.g. <code>frontend/dist</code>).</p>\n"
                    "  <p>To compile the frontend, run:</p>\n"
                    "  <pre style=\"background: #f4f4f4; padding: 1rem; border-radius: 4px;\">cd frontend && npm run build</pre>\n"
                    "</body>\n"
                    "</html>\n"
                ),
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        index_file = static_dir / "index.html"
        if not index_file.is_file():
            return HTMLResponse(
                content=(
                    "<!DOCTYPE html>\n"
                    "<html>\n"
                    "<head><title>Frontend Build Incomplete - QueryPilot</title></head>\n"
                    "<body style=\"font-family: sans-serif; padding: 2rem; max-width: 600px; margin: auto; line-height: 1.5;\">\n"
                    "  <h2>QueryPilot Frontend Build Incomplete</h2>\n"
                    "  <p><code>index.html</code> was not found in the static assets directory.</p>\n"
                    "  <p>To compile the frontend, run:</p>\n"
                    "  <pre style=\"background: #f4f4f4; padding: 1rem; border-radius: 4px;\">cd frontend && npm run build</pre>\n"
                    "</body>\n"
                    "</html>\n"
                ),
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        clean_subpath = subpath.strip("/")
        if not clean_subpath:
            return FileResponse(path=index_file, media_type="text/html")

        # Resolve requested file path securely
        try:
            target_path = (static_dir / clean_subpath).resolve()
            # Path traversal check: must be strictly inside static_dir
            target_path.relative_to(static_dir.resolve())
        except (ValueError, RuntimeError):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access forbidden: Path traversal outside static directory is not permitted.",
            )

        if target_path.is_file():
            media_type, _ = mimetypes.guess_type(str(target_path))
            return FileResponse(path=target_path, media_type=media_type)

        # If it has an extension or starts with 'assets/', treat as a missing asset (404)
        if target_path.suffix or clean_subpath.startswith("assets/"):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Asset '{clean_subpath}' not found.",
            )

        # Otherwise, client-side route fallback to index.html
        return FileResponse(path=index_file, media_type="text/html")

    # --------------------------------------------------------------------------
    # Mount Constrained Same-Origin Upstream API Routes
    # --------------------------------------------------------------------------
    app.include_router(api_router, prefix="/api")

    return app
