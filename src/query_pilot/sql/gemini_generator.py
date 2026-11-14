"""Gemini-backed SQL generation provider implementing the SQLGenerator protocol.

Uses the official google-genai SDK to request structured JSON output conforming to
the SQLGenerationResponse model. Strictly generates candidate SQL without executing it.
"""

import logging
from pathlib import Path
import re
import time
from typing import Any, List, Optional, Union

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from query_pilot.config import Settings, get_settings
from query_pilot.observability import LLMUsageMetadata, extract_llm_usage
from query_pilot.rag.retriever import DomainContextRetriever
from query_pilot.sql.generation import (
    ClarificationOption,
    ConfigurationError,
    EmptySQLError,
    MalformedResponseError,
    ProviderAPIError,
    SQLGenerationRequest,
    SQLGenerationResponse,
    SQLGenerationStatus,
    SQLGenerator,
)
from query_pilot.sql.prompt import (
    SYSTEM_INSTRUCTION,
    build_sql_generation_prompt,
    get_system_instruction,
)
from query_pilot.tracing import trace_gemini_call

logger = logging.getLogger(__name__)


class _GeminiClarificationOptionSchema(BaseModel):
    """Internal Pydantic schema for clarification options in Gemini structured output.

    Omits extra='forbid' so that Pydantic does not inject 'additionalProperties: false',
    which the Gemini API backend rejects in response_schema.
    """

    option_id: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="Unique identifier for the clarification option.",
    )
    description: str = Field(
        ...,
        min_length=1,
        max_length=500,
        description="Concise description of this interpretation.",
    )
    suggested_question: Optional[str] = Field(
        default=None,
        max_length=500,
        description="Optional unambiguous reformulated question corresponding to this option.",
    )


class _GeminiSQLPayloadSchema(BaseModel):
    """Internal schema for Gemini structured output."""

    status: SQLGenerationStatus = SQLGenerationStatus.ANSWERABLE
    sql: Optional[str] = None
    explanation: Optional[str] = None
    assumptions: List[str] = Field(default_factory=list)
    clarification_prompt: Optional[str] = None
    clarification_options: List[_GeminiClarificationOptionSchema] = Field(default_factory=list)


def should_include_sampling_parameters(model_name: Optional[str]) -> bool:
    """Determine whether custom sampling parameters (temperature, top_p, top_k) should be sent.

    Aligned with Google guidance:
    - Gemini 3.x and Gemini 4.x: custom sampling parameters are omitted (Gemini 3.6+ and 4 ignore
      these parameters and return deprecation-warning headers; earlier Gemini 3 models perform
      optimally with provider defaults to prevent degraded reasoning).
    - Gemini 1.x and 2.x: preserve existing legacy sampling behavior (e.g. temperature=0.0).
    - Unknown or unrecognized model formats: conservatively omit custom sampling overrides.

    Returns True for Gemini 1.x and 2.x models, False for Gemini 3.x+, Gemini 4.x+, and unrecognized models.
    """
    if not isinstance(model_name, str):
        return False

    cleaned = model_name.strip().lower()
    match = re.search(r"gemini-(\d+)(?:\.(\d+))?", cleaned)
    if not match:
        # Conservative fallback: do not send custom sampling overrides for unknown model formats
        return False

    major = int(match.group(1))

    # Gemini 1.x and 2.x retain legacy sampling overrides
    if major in (1, 2):
        return True

    # Gemini 3.x, 4.x, and beyond omit custom sampling overrides
    return False


# ==============================================================================
# Transient Error Retry Configuration
# ==============================================================================

GEMINI_RETRY_ATTEMPTS: int = 3
GEMINI_RETRY_INITIAL_DELAY: float = 1.0
GEMINI_RETRY_MAX_DELAY: float = 8.0
GEMINI_RETRY_EXP_BASE: float = 2.0
GEMINI_RETRY_JITTER: float = 1.0
GEMINI_RETRY_HTTP_STATUS_CODES: list[int] = [408, 429, 500, 502, 503, 504]


def get_gemini_http_options(
    *,
    attempts: int = GEMINI_RETRY_ATTEMPTS,
    initial_delay: float = GEMINI_RETRY_INITIAL_DELAY,
    max_delay: float = GEMINI_RETRY_MAX_DELAY,
    exp_base: float = GEMINI_RETRY_EXP_BASE,
    jitter: float = GEMINI_RETRY_JITTER,
    http_status_codes: Optional[list[int]] = None,
) -> types.HttpOptions:
    """Construct shared types.HttpOptions with bounded retry settings for Google GenAI SDK.

    Configures bounded exponential backoff with full jitter for transient provider failures
    (408, 429, 500, 502, 503, 504) and transport errors (timeouts, connection drops).
    """
    retry_options = types.HttpRetryOptions(
        attempts=attempts,
        initial_delay=initial_delay,
        max_delay=max_delay,
        exp_base=exp_base,
        jitter=jitter,
        http_status_codes=http_status_codes or list(GEMINI_RETRY_HTTP_STATUS_CODES),
    )
    return types.HttpOptions(retry_options=retry_options)


class GeminiSQLGenerator(SQLGenerator):
    """Generates candidate read-only SQL queries using Google Gemini."""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        api_key: Optional[str] = None,
        model_name: Optional[str] = None,
        retriever: Optional[DomainContextRetriever] = None,
        catalog_path: Optional[Union[str, Path]] = None,
        enable_retrieval: bool = True,
        http_options: Optional[types.HttpOptions] = None,
        client: Optional[Any] = None,
        secondary_api_key: Optional[str] = None,
        secondary_client: Optional[Any] = None,
        fallback_on_any_error: bool = False,
        backup_keys: Optional[List[str]] = None,
    ):
        """Initialize Gemini SQL Generator.

        Args:
            settings: Optional Settings object containing GEMINI_API_KEY and GEMINI_MODEL.
            api_key: Optional explicit API key string (overrides settings).
            model_name: Optional explicit model name string (overrides settings).
            retriever: Optional explicit DomainContextRetriever instance. If omitted and
                enable_retrieval is True, loads the validated catalog at data/knowledge/catalog.json.
            catalog_path: Optional explicit path to catalog.json.
            enable_retrieval: Whether domain context retrieval is enabled (default: True).
            http_options: Optional custom types.HttpOptions (defaults to bounded retry settings).
            client: Optional pre-configured genai.Client instance (useful for unit testing).
            secondary_api_key: Optional explicit secondary API key for quota fallback.
            secondary_client: Optional pre-configured secondary genai.Client instance.

        Raises:
            ConfigurationError: If no valid API key can be resolved.
            FileNotFoundError: If the specified catalog file cannot be found.
            ValueError: If the catalog file is malformed or invalid.
        """
        resolved_key: Optional[str] = None
        resolved_secondary_key: Optional[str] = None
        resolved_model: str = "gemini-3.8-flash"
        cfg = settings

        if api_key is not None:
            resolved_key = api_key.strip()
            # If user provided an explicit custom key in production runtime, auto-resolve environment keys as backups
            if client is None and cfg is None:
                try:
                    cfg = get_settings()
                except Exception:
                    pass
            if client is None and cfg and cfg.GEMINI_API_KEY:
                env_k1 = cfg.GEMINI_API_KEY.get_secret_value().strip()
                if env_k1 and env_k1 != resolved_key and env_k1 != "your_gemini_api_key_here":
                    if secondary_api_key is None and resolved_secondary_key is None and secondary_client is None:
                        resolved_secondary_key = env_k1
                    else:
                        if backup_keys is None:
                            backup_keys = []
                        if env_k1 not in backup_keys:
                            backup_keys.append(env_k1)
            if client is None and cfg and getattr(cfg, "GEMINI_API_KEY_2", None):
                env_k2 = cfg.GEMINI_API_KEY_2.get_secret_value().strip()
                if env_k2 and env_k2 not in (resolved_key, resolved_secondary_key, "your_gemini_api_key_2_here"):
                    if backup_keys is None:
                        backup_keys = []
                    if env_k2 not in backup_keys:
                        backup_keys.append(env_k2)
        else:
            if cfg is None:
                try:
                    cfg = get_settings()
                except Exception as exc:
                    raise ConfigurationError(
                        f"Failed to load application settings for Gemini: {exc}"
                    ) from exc

            if cfg and cfg.GEMINI_API_KEY:
                resolved_key = cfg.GEMINI_API_KEY.get_secret_value().strip()
                resolved_model = cfg.GEMINI_MODEL

        if secondary_api_key is not None:
            resolved_secondary_key = secondary_api_key.strip()
        elif cfg and getattr(cfg, "GEMINI_API_KEY_2", None) and resolved_secondary_key is None:
            resolved_secondary_key = cfg.GEMINI_API_KEY_2.get_secret_value().strip()

        if not resolved_key or resolved_key == "your_gemini_api_key_here":
            raise ConfigurationError(
                "GEMINI_API_KEY is not configured or contains a default placeholder."
            )

        self.model_name = model_name or resolved_model
        self.last_usage: Optional[LLMUsageMetadata] = None
        self.fallback_on_any_error = fallback_on_any_error
        self.backup_clients: list[tuple[Any, str]] = []

        if not enable_retrieval:
            self.retriever: Optional[DomainContextRetriever] = None
        elif retriever is not None:
            self.retriever = retriever
        else:
            # Instantiate retriever using validated catalog
            self.retriever = DomainContextRetriever(catalog_path=catalog_path)

        opts = http_options if http_options is not None else get_gemini_http_options()

        if client is not None:
            self.client = client
        else:
            try:
                # Note: Do NOT log the API key
                logger.debug(f"Initializing Gemini Client with model={self.model_name}")
                self.client = genai.Client(api_key=resolved_key, http_options=opts)
            except Exception as exc:
                raise ConfigurationError(
                    f"Failed to initialize google-genai Client: {exc}"
                ) from exc

        if secondary_client is not None:
            self.secondary_client = secondary_client
            self.backup_clients.append((secondary_client, "secondary"))
        elif resolved_secondary_key:
            try:
                logger.debug("Initializing secondary Gemini Client for quota fallback")
                self.secondary_client = genai.Client(api_key=resolved_secondary_key, http_options=opts)
                self.backup_clients.append((self.secondary_client, "secondary"))
            except Exception as exc:
                logger.warning("Failed to initialize secondary Gemini Client: %s", type(exc).__name__)
                self.secondary_client = None
        else:
            self.secondary_client = None

        if backup_keys:
            for b_idx, b_key in enumerate(backup_keys, start=1):
                clean_b = b_key.strip() if isinstance(b_key, str) else None
                if clean_b and clean_b not in (resolved_key, resolved_secondary_key):
                    try:
                        b_cl = genai.Client(api_key=clean_b, http_options=opts)
                        self.backup_clients.append((b_cl, f"backup_{b_idx}"))
                    except Exception as b_exc:
                        logger.warning("Failed to initialize backup Gemini Client #%d: %s", b_idx, type(b_exc).__name__)

    @trace_gemini_call(name="gemini_generate_sql", run_type="llm")
    def generate(self, request: SQLGenerationRequest) -> SQLGenerationResponse:
        """Generate structured candidate SQL for a natural language question.

        Never executes SQL, never calls the database, and never calls the validator.

        Args:
            request: SQLGenerationRequest containing question and model-facing schema.

        Returns:
            SQLGenerationResponse with candidate SQL, optional explanations, and usage telemetry.

        Raises:
            ProviderAPIError: If the Gemini API request fails.
            MalformedResponseError: If the response cannot be parsed into structured format.
            EmptySQLError: If the returned SQL is empty or whitespace.
        """
        self.last_usage = None

        # Resolve domain guidance exclusively through the validated retriever
        domain_guidance = None
        if self.retriever is not None:
            intent = None
            if request.conversation_context:
                last_turn = request.conversation_context[-1]
                if last_turn.resolved_intent:
                    intent = last_turn.resolved_intent
            results = self.retriever.retrieve(request.question, intent=intent)
            if results:
                domain_guidance = self.retriever.render_context(results)

        # Resolve request-scoped business metrics if supplied in the request
        business_metrics_guidance = None
        if getattr(request, "business_metrics", None):
            from query_pilot.rag.metrics_matcher import (
                match_business_metrics,
                render_business_metrics_context,
            )

            intent = None
            if request.conversation_context:
                last_turn = request.conversation_context[-1]
                if last_turn.resolved_intent:
                    intent = last_turn.resolved_intent
            matched_metrics = match_business_metrics(
                metrics=request.business_metrics,
                question=request.question,
                schema=request.schema_context,
                resolved_intent=intent,
            )
            if matched_metrics:
                business_metrics_guidance = render_business_metrics_context(matched_metrics)

        prompt = build_sql_generation_prompt(
            request,
            domain_context=domain_guidance,
            business_metrics_context=business_metrics_guidance,
        )

        config_kwargs: dict = {
            "system_instruction": get_system_instruction(request.dialect),
            "response_mime_type": "application/json",
            "response_schema": _GeminiSQLPayloadSchema,
        }
        if should_include_sampling_parameters(self.model_name):
            config_kwargs["temperature"] = 0.0

        config = types.GenerateContentConfig(**config_kwargs)

        start_time = time.perf_counter()
        response = None
        candidate_models: list[str] = [self.model_name]

        def _call_client_with_models(cl, models: list[str]):
            m_idx = 0
            while m_idx < len(models):
                cand = models[m_idx]
                try:
                    res = cl.models.generate_content(
                        model=cand,
                        contents=prompt,
                        config=config,
                    )
                    if res and res.text:
                        self.model_name = cand
                    return res
                except Exception as exc:
                    status_code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
                    err_msg = str(exc).lower()
                    if (
                        status_code == 404
                        or "not found" in err_msg
                        or "no longer available" in err_msg
                    ) and m_idx == 0:
                        for fb in ("gemini-2.5-flash", "gemini-3.5-flash-lite", "gemini-3.8-flash"):
                            if fb not in models:
                                models.append(fb)
                        m_idx += 1
                        continue
                    raise exc

        try:
            response = _call_client_with_models(self.client, candidate_models)
        except Exception as exc:
            self.last_usage = None
            from query_pilot.sql.failure_classifier import classify_generation_failure
            from query_pilot.api.models import GenerationFailureClassification

            diagnostic = classify_generation_failure(exc)
            is_quota = diagnostic.classification == GenerationFailureClassification.QUOTA_EXHAUSTED
            should_fallback = (self.fallback_on_any_error or is_quota)

            if should_fallback and self.backup_clients:
                if is_quota:
                    logger.info(
                        "Primary Gemini key quota exhausted. Attempting bounded fallback using secondary key."
                    )
                else:
                    logger.info(
                        "Primary Gemini key failed (%s: %s). Attempting bounded fallback using backup keys.",
                        type(exc).__name__,
                        exc,
                    )
                fallback_success = False
                fallback_error = exc
                for b_client, b_label in self.backup_clients:
                    try:
                        response = _call_client_with_models(b_client, candidate_models)
                        fallback_success = True
                        break
                    except Exception as sec_exc:
                        fallback_error = sec_exc
                        logger.warning("%s Gemini key attempt also failed: %s", b_label, type(sec_exc).__name__)

                if not fallback_success:
                    raise ProviderAPIError(f"Gemini API request failed on fallback key: {fallback_error}") from fallback_error
            else:
                raise ProviderAPIError(
                    f"Gemini API request failed: {exc}"
                ) from exc
        finally:
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0

        usage_meta = extract_llm_usage(
            response=response,
            stage="sql_generation",
            model_name=self.model_name,
            latency_ms=elapsed_ms,
        )
        self.last_usage = usage_meta

        if not response or not response.text:
            err = MalformedResponseError(
                "Gemini API returned an empty or null content response."
            )
            err.usage_metadata = usage_meta
            raise err

        try:
            parsed = SQLGenerationResponse.model_validate_json(response.text)
        except Exception as exc:
            err = MalformedResponseError(
                f"Failed to parse Gemini response into SQLGenerationResponse: {exc}. "
                f"Raw output: {response.text[:200]}"
            )
            err.usage_metadata = usage_meta
            raise err from exc

        parsed.usage_metadata = usage_meta

        if parsed.status == SQLGenerationStatus.ANSWERABLE:
            if not parsed.sql or not parsed.sql.strip():
                err = EmptySQLError("Gemini marked question as answerable but returned an empty SQL string.")
                err.usage_metadata = usage_meta
                raise err

        return parsed
