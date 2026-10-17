"""SQL validation, generation contract, and policy module for QueryPilot."""

from query_pilot.sql.gemini_generator import (
    GEMINI_RETRY_ATTEMPTS,
    GEMINI_RETRY_EXP_BASE,
    GEMINI_RETRY_HTTP_STATUS_CODES,
    GEMINI_RETRY_INITIAL_DELAY,
    GEMINI_RETRY_JITTER,
    GEMINI_RETRY_MAX_DELAY,
    GeminiSQLGenerator,
    get_gemini_http_options,
)
from query_pilot.sql.generation import (
    ConfigurationError,
    EmptySQLError,
    MalformedResponseError,
    ProviderAPIError,
    SQLGenerationError,
    SQLGenerationRequest,
    SQLGenerationResponse,
    SQLGenerationStatus,
    SQLGenerator,
    format_schema_context,
)
from query_pilot.sql.models import ValidationReason, ValidationReasonCode, ValidationResult
from query_pilot.sql.prompt import SYSTEM_INSTRUCTION, build_sql_generation_prompt
from query_pilot.sql.validator import SQLValidator, validate_sql

__all__ = [
    # Validation
    "SQLValidator",
    "ValidationReason",
    "ValidationReasonCode",
    "ValidationResult",
    "validate_sql",
    # Generation Contract
    "SQLGenerationRequest",
    "SQLGenerationResponse",
    "SQLGenerationStatus",
    "SQLGenerator",
    "SQLGenerationError",
    "ConfigurationError",
    "ProviderAPIError",
    "MalformedResponseError",
    "EmptySQLError",
    "format_schema_context",
    "SYSTEM_INSTRUCTION",
    "build_sql_generation_prompt",
    "GeminiSQLGenerator",
    "get_gemini_http_options",
    "GEMINI_RETRY_ATTEMPTS",
    "GEMINI_RETRY_INITIAL_DELAY",
    "GEMINI_RETRY_MAX_DELAY",
    "GEMINI_RETRY_EXP_BASE",
    "GEMINI_RETRY_JITTER",
    "GEMINI_RETRY_HTTP_STATUS_CODES",
]
