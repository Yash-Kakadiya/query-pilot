"""Privacy-safe, opt-in LangSmith tracing instrumentation for QueryPilot.

Provides:
- Fully opt-in, disabled-by-default execution.
- Strict payload redaction preventing raw questions, SQL, schema metadata, row contents,
  or credentials from being transmitted in traces.
- Unified SDK-level error anonymization replacing exception messages and tracebacks with
  a fixed safe marker ('[REDACTED_ERROR]').
- Identifier, tag, and run-name sanitization preventing metadata exfiltration.
- Process-race-free explicit callback and client propagation for per-request execution.
- Seamless correlation across LangGraph runs, node transitions, and direct Gemini calls.
- Fail-open execution: tracing errors or missing credentials never crash the pipeline.
- Zero network activity and zero background threads in default offline mode.
"""

from contextlib import contextmanager
from contextvars import ContextVar
import functools
import logging
import os
import re
from typing import Any, Callable, Dict, Generator, List, Optional, Set

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.tracers.langchain import LangChainTracer
from langsmith import traceable, Client

from query_pilot.config import Settings, get_settings

logger = logging.getLogger("query_pilot.tracing")

# Sensitive substrings triggering redaction in dictionary keys
SENSITIVE_KEY_PATTERNS = (
    "password",
    "secret",
    "token",
    "hash",
    "sql",
    "query",
    "schema",
    "question",
    "prompt",
    "row",
    "column",
    "answer",
    "result",
    "sample",
    "feedback",
    "context",
    "email",
    "content",
    "explanation",
    "assumptions",
    "instructions",
)

# Safe operational metadata keys permitted in trace payloads
SAFE_METADATA_KEYS: Set[str] = {
    "request_id",
    "status",
    "stage",
    "event",
    "dialect",
    "generation_attempts",
    "retry_count",
    "duration_ms",
    "error_category",
    "row_count",
    "truncated",
    "is_mechanically_verified",
    "model_name",
    "total_tokens",
    "prompt_tokens",
    "completion_tokens",
    "latency_ms",
}

SAFE_IDENTIFIER_PATTERN = re.compile(r"^[a-zA-Z0-9_\-\.:]+$")


def sanitize_safe_identifier(
    val: Optional[str], default: str = "anonymous", max_len: int = 64
) -> str:
    """Validate and constrain identifier strings (request IDs, dialects, tag components).

    Rejects arbitrary strings, SQL statements, spaces, or secrets, falling back to a safe default.
    """
    if not val or not isinstance(val, str):
        return default
    cleaned = val.strip()
    if not (1 <= len(cleaned) <= max_len):
        return default
    if not SAFE_IDENTIFIER_PATTERN.match(cleaned):
        return default
    return cleaned


def redact_sensitive_payload(data: Any, max_depth: int = 5) -> Any:
    """Recursively redact sensitive text, queries, schemas, and credentials from payloads.

    Preserves safe operational metadata (request_id, status, error_category, token counts).
    Replaces all raw text, questions, SQL queries, schema metadata, and result rows with '[REDACTED]'.
    Replaces exception/error keys with '[REDACTED_ERROR]'.
    """
    if max_depth <= 0:
        return "[REDACTED]"

    if isinstance(data, dict):
        sanitized: Dict[str, Any] = {}
        for k, v in data.items():
            k_lower = str(k).lower()
            if k in SAFE_METADATA_KEYS:
                if k in ("request_id", "dialect"):
                    sanitized[str(k)] = sanitize_safe_identifier(str(v), default="[REDACTED]", max_len=64)
                elif isinstance(v, (int, float, bool)) or v is None:
                    sanitized[str(k)] = v
                elif isinstance(v, str) and len(v) <= 100:
                    sanitized[str(k)] = v
                else:
                    sanitized[str(k)] = "[REDACTED]"
            elif k_lower == "error":
                sanitized[str(k)] = "[REDACTED_ERROR]"
            elif any(pattern in k_lower for pattern in SENSITIVE_KEY_PATTERNS):
                sanitized[str(k)] = "[REDACTED]"
            elif isinstance(v, dict):
                sanitized[str(k)] = redact_sensitive_payload(v, max_depth - 1)
            elif isinstance(v, (list, tuple)):
                sanitized[str(k)] = [redact_sensitive_payload(item, max_depth - 1) for item in v]
            elif isinstance(v, (int, float, bool)) or v is None:
                sanitized[str(k)] = v
            else:
                sanitized[str(k)] = "[REDACTED]"
        return sanitized

    if isinstance(data, (list, tuple)):
        return [redact_sensitive_payload(item, max_depth - 1) for item in data]

    if isinstance(data, (int, float, bool)) or data is None:
        return data

    return "[REDACTED]"


def anonymize_trace_payload(data: Any) -> Any:
    """Unified SDK-level anonymizer conforming strictly to Callable[[dict], dict].

    SDK contract: Client._hide_run_error wraps error strings as {'error': ...}.
    If the returned dict is missing 'error', or is not a dict, Client._hide_run_error
    falls back to returning the original sensitive error string!
    Therefore, this function guarantees:
    1. For dict payloads containing 'error', always returns a dictionary with 'error': '[REDACTED_ERROR]'.
    2. For standard dict payloads (inputs, outputs, metadata), returns a recursively redacted dict.
    3. For non-dict payloads, falls back safely to redact_sensitive_payload.
    4. Handles unexpected structures and exceptions fail-closed.
    """
    try:
        if isinstance(data, dict):
            res = redact_sensitive_payload(data)
            if not isinstance(res, dict):
                res = {"payload": "[REDACTED]"}
            if "error" in data or "error" in res:
                res["error"] = "[REDACTED_ERROR]"
            return res
        return redact_sensitive_payload(data)
    except Exception:
        if isinstance(data, dict) and "error" in data:
            return {"error": "[REDACTED_ERROR]"}
        return {"error": "[REDACTED_ERROR]"} if isinstance(data, dict) else "[REDACTED]"


_QUERY_PILOT_CLIENT_OWNER_ID = "query_pilot_privacy_safe_client_v1"
_ACTIVE_QP_CLIENT: ContextVar[Optional[Client]] = ContextVar("qp_active_client", default=None)


class QueryPilotClient(Client):
    """Explicit privacy-safe LangSmith Client subclass owned by QueryPilot.

    Enables unambiguous ownership verification without relying on mutable tags or heuristic checks.
    """
    __slots__ = ("_query_pilot_owner",)


def get_active_query_pilot_client() -> Optional[Client]:
    """Retrieve the explicitly active QueryPilot tracing client from context."""
    return _ACTIVE_QP_CLIENT.get()


def is_query_pilot_client(client: Any) -> bool:
    """Validate that a client instance is an explicitly configured QueryPilot privacy-safe client."""
    if client is None or not isinstance(client, QueryPilotClient):
        return False
    if getattr(client, "_query_pilot_owner", None) != _QUERY_PILOT_CLIENT_OWNER_ID:
        return False
    if getattr(client, "_anonymizer", None) is not anonymize_trace_payload:
        return False
    return True


@contextmanager
def query_pilot_tracing_context(client: Optional[Client]) -> Generator[Optional[Client], None, None]:
    """Context manager setting the active QueryPilot tracing client for a request or block."""
    token = _ACTIVE_QP_CLIENT.set(client)
    try:
        yield client
    finally:
        _ACTIVE_QP_CLIENT.reset(token)


class PrivacySafeLangChainTracer(LangChainTracer):
    """LangChainTracer enforcing strict input and output payload redaction before run emission."""

    def _get_chain_inputs(self, inputs: Any) -> Any:
        raw_inputs = super()._get_chain_inputs(inputs)
        return anonymize_trace_payload(raw_inputs)

    def _get_chain_outputs(self, outputs: Any) -> Any:
        raw_outputs = super()._get_chain_outputs(outputs)
        return anonymize_trace_payload(raw_outputs)


def is_tracing_enabled(settings: Optional[Settings] = None) -> bool:
    """Check whether LangSmith tracing is explicitly enabled in context, settings, or environment."""
    if settings is not None:
        return bool(settings.LANGCHAIN_TRACING_V2)

    # 1. Active RunTree context (e.g. running inside an explicitly traced QueryPilot LangGraph node)
    try:
        from langsmith.run_helpers import get_current_run_tree
        tree = get_current_run_tree()
        if tree is not None:
            tree_client = getattr(tree, "client", None)
            if is_query_pilot_client(tree_client):
                return True
            # Ambient run from external application -> do not treat as QueryPilot tracing enabled
            return False
    except Exception:
        pass

    # 2. Active QueryPilot context variable
    if is_query_pilot_client(get_active_query_pilot_client()):
        return True

    # 3. Process environment variables
    env_v2 = os.getenv("LANGCHAIN_TRACING_V2", "").strip().lower()
    if env_v2 in ("true", "1"):
        return True
    if env_v2 in ("false", "0"):
        return False

    env_ls = os.getenv("LANGSMITH_TRACING", "").strip().lower()
    if env_ls in ("true", "1"):
        return True
    if env_ls in ("false", "0"):
        return False

    try:
        current_settings = get_settings()
        return bool(current_settings.LANGCHAIN_TRACING_V2)
    except Exception:
        return False


def configure_tracing(settings: Optional[Settings] = None) -> bool:
    """Synchronize opt-in tracing configuration to process environment.

    Intended for optional, one-time application startup initialization.
    Per-request execution paths rely on explicit RunnableConfig and Client propagation
    without mutating process-wide environment variables.
    Never logs credentials or secret values.
    Returns True if tracing was successfully activated; False if disabled or unconfigured.
    """
    target_settings = settings
    if target_settings is None:
        try:
            target_settings = get_settings()
        except Exception as exc:
            logger.debug("Failed to load settings during tracing configuration: %s", type(exc).__name__)
            return False

    if not target_settings.LANGCHAIN_TRACING_V2:
        return False

    api_key_secret = target_settings.LANGCHAIN_API_KEY
    if api_key_secret is None:
        logger.warning(
            "LangSmith tracing requested (LANGCHAIN_TRACING_V2=True) but LANGCHAIN_API_KEY is not configured. Tracing disabled."
        )
        return False

    raw_key = api_key_secret.get_secret_value().strip()
    if not raw_key or raw_key == "your_langsmith_api_key_here":
        logger.warning(
            "LangSmith tracing requested with placeholder/empty API key. Tracing disabled."
        )
        return False

    # Synchronize only necessary environment variables for LangSmith SDK
    os.environ["LANGCHAIN_TRACING_V2"] = "true"
    os.environ["LANGCHAIN_PROJECT"] = target_settings.LANGCHAIN_PROJECT
    os.environ["LANGCHAIN_API_KEY"] = raw_key
    try:
        from langsmith.utils import get_env_var
        get_env_var.cache_clear()
    except Exception:
        pass
    try:
        get_settings.cache_clear()
    except Exception:
        pass
    return True


@contextmanager
def temporary_tracing_context(settings: Settings) -> Generator[bool, None, None]:
    """Context manager for temporary, isolated tracing execution without permanent env mutation."""
    saved_env = {
        "LANGCHAIN_TRACING_V2": os.environ.get("LANGCHAIN_TRACING_V2"),
        "LANGCHAIN_PROJECT": os.environ.get("LANGCHAIN_PROJECT"),
        "LANGCHAIN_API_KEY": os.environ.get("LANGCHAIN_API_KEY"),
    }
    client = create_privacy_safe_client(settings)
    token = _ACTIVE_QP_CLIENT.set(client)
    try:
        active = configure_tracing(settings)
        yield active
    finally:
        _ACTIVE_QP_CLIENT.reset(token)
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        try:
            from langsmith.utils import get_env_var
            get_env_var.cache_clear()
        except Exception:
            pass
        try:
            get_settings.cache_clear()
        except Exception:
            pass


def create_privacy_safe_client(settings: Optional[Settings] = None) -> Optional[Client]:
    """Instantiate a LangSmith Client configured with SDK-level unified anonymization and redaction.

    Applies anonymizer across inputs, outputs, metadata, and error strings.
    Fails open by returning None if configuration is missing or invalid.
    """
    target_settings = settings
    if target_settings is None:
        try:
            target_settings = get_settings()
        except Exception:
            return None

    if not target_settings.LANGCHAIN_TRACING_V2:
        return None

    api_key_secret = target_settings.LANGCHAIN_API_KEY
    if api_key_secret is None:
        return None

    raw_key = api_key_secret.get_secret_value().strip()
    if not raw_key or raw_key == "your_langsmith_api_key_here":
        return None

    def _tracing_error_handler(exc: Exception) -> None:
        logger.warning("LangSmith tracing error caught (failing open): %s", type(exc).__name__)

    try:
        client = QueryPilotClient(
            api_key=raw_key,
            anonymizer=anonymize_trace_payload,
            hide_inputs=anonymize_trace_payload,
            hide_outputs=anonymize_trace_payload,
            hide_metadata=anonymize_trace_payload,
            tracing_error_callback=_tracing_error_handler,
        )
        client._query_pilot_owner = _QUERY_PILOT_CLIENT_OWNER_ID
        return client
    except Exception as exc:
        logger.warning("Failed to initialize LangSmith Client (failing open): %s", type(exc).__name__)
        return None


def get_tracing_callbacks(
    settings: Optional[Settings] = None,
    request_id: Optional[str] = None,
    dialect: str = "T-SQL",
) -> List[BaseCallbackHandler]:
    """Retrieve configured privacy-safe tracing callback handlers if tracing is enabled.

    Explicitly wires the client and sanitizes tags and metadata without relying on process env.
    Returns an empty list if tracing is disabled or unconfigured.
    """
    if not is_tracing_enabled(settings):
        return []

    client = create_privacy_safe_client(settings)
    if client is None:
        return []

    safe_req_id = sanitize_safe_identifier(request_id, default="anonymous", max_len=64)
    safe_dialect = sanitize_safe_identifier(dialect, default="T-SQL", max_len=32)

    project_name = settings.LANGCHAIN_PROJECT if settings else "query-pilot"
    tracer = PrivacySafeLangChainTracer(
        project_name=project_name,
        client=client,
        tags=["query-pilot", safe_dialect],
        metadata={
            "request_id": safe_req_id,
            "dialect": safe_dialect,
        },
    )
    return [tracer]


def get_runnable_config(
    request_id: Optional[str] = None,
    dialect: str = "T-SQL",
    settings: Optional[Settings] = None,
) -> Dict[str, Any]:
    """Construct a safe LangGraph RunnableConfig dictionary with validated metadata, tags, and tracer."""
    safe_req_id = sanitize_safe_identifier(request_id, default="anonymous", max_len=64)
    safe_dialect = sanitize_safe_identifier(dialect, default="T-SQL", max_len=32)

    config: Dict[str, Any] = {
        "run_name": f"query_pilot:{safe_req_id}",
        "tags": ["query-pilot", safe_dialect],
        "metadata": {
            "request_id": safe_req_id,
            "dialect": safe_dialect,
        },
    }
    callbacks = get_tracing_callbacks(settings=settings, request_id=safe_req_id, dialect=safe_dialect)
    if callbacks:
        config["callbacks"] = callbacks
    return config


def trace_gemini_call(name: str, run_type: str = "llm") -> Callable:
    """Decorator to trace direct Gemini calls under active LangSmith tracing.

    Strictly isolates spans:
    - Never emits spans to an ambient external run context.
    - When running within a QueryPilot graph execution, attaches as child of the active node using the explicit client.
    - When running standalone, requires explicit QueryPilot configuration and privacy-safe client.
    - When tracing is disabled or unconfigured, executes directly with zero overhead.
    """

    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            tree = None
            try:
                from langsmith.run_helpers import get_current_run_tree
                tree = get_current_run_tree()
            except Exception:
                pass

            client: Optional[Client] = None
            if tree is not None:
                tree_client = getattr(tree, "client", None)
                if is_query_pilot_client(tree_client):
                    client = tree_client
                else:
                    # Ambient run from external application -> strict isolation: do NOT trace
                    return fn(*args, **kwargs)

            if client is None:
                # Outside a LangGraph run tree; check explicit QueryPilot tracing context
                active_client = get_active_query_pilot_client()
                if is_query_pilot_client(active_client):
                    client = active_client
                else:
                    # No active QueryPilot-owned trace context -> do NOT trace
                    return fn(*args, **kwargs)

            def _redact_inputs(call_inputs: Dict[str, Any]) -> Dict[str, Any]:
                return anonymize_trace_payload(call_inputs)

            def _redact_outputs(call_output: Any) -> Dict[str, Any]:
                # Extract safe operational status and token telemetry, redact text/SQL
                res: Dict[str, Any] = {"payload": "[REDACTED]"}
                status = getattr(call_output, "status", None)
                if status is not None:
                    res["status"] = getattr(status, "value", str(status))
                usage = getattr(call_output, "usage_metadata", None)
                if usage is not None:
                    res["total_tokens"] = getattr(usage, "total_tokens", None)
                    res["prompt_tokens"] = getattr(usage, "prompt_tokens", None)
                    res["completion_tokens"] = getattr(usage, "completion_tokens", None)
                return res

            try:
                traced_fn = traceable(
                    name=name,
                    run_type=run_type,
                    client=client,
                    enabled=True,
                    process_inputs=_redact_inputs,
                    process_outputs=_redact_outputs,
                )(fn)
                return traced_fn(*args, **kwargs)
            except Exception as exc:
                logger.warning("Error in traceable wrapper for %s (failing open): %s", name, type(exc).__name__)
                return fn(*args, **kwargs)

        return wrapper

    return decorator
