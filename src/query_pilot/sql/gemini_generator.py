"""Gemini-backed SQL generation provider implementing the SQLGenerator protocol.

Uses the official google-genai SDK to request structured JSON output conforming to
the SQLGenerationResponse model. Strictly generates candidate SQL without executing it.
"""

import logging
from pathlib import Path
import re
import time
from typing import List, Optional, Union

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
from query_pilot.sql.prompt import SYSTEM_INSTRUCTION, build_sql_generation_prompt
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

        Raises:
            ConfigurationError: If no valid API key can be resolved.
            FileNotFoundError: If the specified catalog file cannot be found.
            ValueError: If the catalog file is malformed or invalid.
        """
        resolved_key: Optional[str] = None
        resolved_model: str = "gemini-3.8-flash"

        if api_key is not None:
            resolved_key = api_key.strip()
        else:
            cfg = settings
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

        if not resolved_key or resolved_key == "your_gemini_api_key_here":
            raise ConfigurationError(
                "GEMINI_API_KEY is not configured or contains a default placeholder."
            )

        self.model_name = model_name or resolved_model
        self.last_usage: Optional[LLMUsageMetadata] = None

        if not enable_retrieval:
            self.retriever: Optional[DomainContextRetriever] = None
        elif retriever is not None:
            self.retriever = retriever
        else:
            # Instantiate retriever using validated catalog
            self.retriever = DomainContextRetriever(catalog_path=catalog_path)

        try:
            # Note: Do NOT log the API key
            logger.debug(f"Initializing Gemini Client with model={self.model_name}")
            self.client = genai.Client(api_key=resolved_key)
        except Exception as exc:
            raise ConfigurationError(
                f"Failed to initialize google-genai Client: {exc}"
            ) from exc

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

        prompt = build_sql_generation_prompt(request, domain_context=domain_guidance)

        config_kwargs: dict = {
            "system_instruction": SYSTEM_INSTRUCTION,
            "response_mime_type": "application/json",
            "response_schema": _GeminiSQLPayloadSchema,
        }
        if should_include_sampling_parameters(self.model_name):
            config_kwargs["temperature"] = 0.0

        config = types.GenerateContentConfig(**config_kwargs)

        start_time = time.perf_counter()
        response = None
        try:
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=prompt,
                config=config,
            )
        except Exception as exc:
            self.last_usage = None
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
