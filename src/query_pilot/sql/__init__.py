"""SQL validation, generation contract, and policy module for QueryPilot."""

from query_pilot.sql.gemini_generator import GeminiSQLGenerator
from query_pilot.sql.generation import (
    ConfigurationError,
    EmptySQLError,
    MalformedResponseError,
    ProviderAPIError,
    SQLGenerationError,
    SQLGenerationRequest,
    SQLGenerationResponse,
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
]
