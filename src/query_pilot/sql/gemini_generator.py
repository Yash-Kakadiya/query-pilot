"""Gemini-backed SQL generation provider implementing the SQLGenerator protocol.

Uses the official google-genai SDK to request structured JSON output conforming to
the SQLGenerationResponse model. Strictly generates candidate SQL without executing it.
"""

import logging
from typing import Optional

from google import genai
from google.genai import types

from query_pilot.config import Settings, get_settings
from query_pilot.sql.generation import (
    ConfigurationError,
    EmptySQLError,
    MalformedResponseError,
    ProviderAPIError,
    SQLGenerationRequest,
    SQLGenerationResponse,
    SQLGenerator,
)
from query_pilot.sql.prompt import SYSTEM_INSTRUCTION, build_sql_generation_prompt

logger = logging.getLogger(__name__)


class GeminiSQLGenerator(SQLGenerator):
    """Generates candidate read-only SQL queries using Google Gemini."""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        api_key: Optional[str] = None,
        model_name: Optional[str] = None,
    ):
        """Initialize Gemini SQL Generator.

        Args:
            settings: Optional Settings object containing GEMINI_API_KEY and GEMINI_MODEL.
            api_key: Optional explicit API key string (overrides settings).
            model_name: Optional explicit model name string (overrides settings).

        Raises:
            ConfigurationError: If no valid API key can be resolved.
        """
        resolved_key: Optional[str] = None
        resolved_model: str = "gemini-2.0-flash"

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

        try:
            # Note: Do NOT log the API key
            logger.debug(f"Initializing Gemini Client with model={self.model_name}")
            self.client = genai.Client(api_key=resolved_key)
        except Exception as exc:
            raise ConfigurationError(
                f"Failed to initialize google-genai Client: {exc}"
            ) from exc

    def generate(self, request: SQLGenerationRequest) -> SQLGenerationResponse:
        """Generate structured candidate SQL for a natural language question.

        Never executes SQL, never calls the database, and never calls the validator.

        Args:
            request: SQLGenerationRequest containing question and model-facing schema.

        Returns:
            SQLGenerationResponse with candidate SQL and optional explanations.

        Raises:
            ProviderAPIError: If the Gemini API request fails.
            MalformedResponseError: If the response cannot be parsed into structured format.
            EmptySQLError: If the returned SQL is empty or whitespace.
        """
        prompt = build_sql_generation_prompt(request)

        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            response_mime_type="application/json",
            response_schema=SQLGenerationResponse,
            temperature=0.0,
        )

        try:
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=prompt,
                config=config,
            )
        except Exception as exc:
            raise ProviderAPIError(
                f"Gemini API request failed: {exc}"
            ) from exc

        if not response or not response.text:
            raise MalformedResponseError(
                "Gemini API returned an empty or null content response."
            )

        try:
            parsed = SQLGenerationResponse.model_validate_json(response.text)
        except Exception as exc:
            raise MalformedResponseError(
                f"Failed to parse Gemini response into SQLGenerationResponse: {exc}. "
                f"Raw output: {response.text[:200]}"
            ) from exc

        if not parsed.sql or not parsed.sql.strip():
            raise EmptySQLError("Gemini generated an empty SQL string.")

        return parsed
