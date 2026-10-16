"""Configuration management for QueryPilot using Pydantic Settings.

Cleanly separates database connection and safety settings from LLM credentials.
Ensures fail-fast environment variable validation and prevents secret leakage.
"""

from functools import lru_cache
import json
from typing import Any, List, Optional, Set
import urllib.parse
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseSettings(BaseSettings):
    """Database connection and execution guardrails configuration.
    
    Can be used by standalone database tooling without requiring LLM API credentials.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --------------------------------------------------------------------------
    # Database Settings (MSSQL / T-SQL)
    # --------------------------------------------------------------------------
    DB_SERVER: str = Field(
        default=r"localhost\SQLEXPRESS",
        description="Database server host/instance name.",
    )
    DB_NAME: str = Field(
        default="GradeSense_Local",
        description="Database name for local testing.",
    )
    DB_DRIVER: str = Field(
        default="ODBC Driver 17 for SQL Server",
        description="ODBC driver name installed on the system.",
    )
    DB_USE_TRUSTED_CONNECTION: bool = Field(
        default=True,
        description="Use Windows Integrated Authentication if True.",
    )
    DB_USER: Optional[str] = Field(
        default=None,
        description="SQL Authentication username (if not using trusted connection).",
    )
    DB_PASSWORD: Optional[SecretStr] = Field(
        default=None,
        description="SQL Authentication password (if not using trusted connection).",
    )

    # --------------------------------------------------------------------------
    # Safety & Execution Guardrails
    # --------------------------------------------------------------------------
    DB_QUERY_TIMEOUT_SECONDS: int = Field(
        default=15,
        ge=1,
        le=120,
        description="Maximum allowed query execution time in seconds.",
    )
    DB_MAX_ROW_LIMIT: int = Field(
        default=100,
        ge=1,
        le=1000,
        description="Maximum row limit enforced on queries.",
    )

    def get_odbc_connection_string(self) -> str:
        """Construct the raw ODBC connection string for SQL Server."""
        parts = [
            f"DRIVER={{{self.DB_DRIVER}}}",
            f"SERVER={self.DB_SERVER}",
            f"DATABASE={self.DB_NAME}",
        ]
        if self.DB_USE_TRUSTED_CONNECTION:
            parts.append("Trusted_Connection=yes")
        elif self.DB_USER and self.DB_PASSWORD:
            parts.append(f"UID={self.DB_USER}")
            parts.append(f"PWD={self.DB_PASSWORD.get_secret_value()}")
        return ";".join(parts) + ";"

    def get_sqlalchemy_url(self) -> str:
        """Construct a SQLAlchemy URL using the mssql+pyodbc dialect."""
        odbc_str = self.get_odbc_connection_string()
        params = urllib.parse.quote_plus(odbc_str)
        return f"mssql+pyodbc:///?odbc_connect={params}"


class Settings(DatabaseSettings):
    """Application-wide settings extending DatabaseSettings with LLM and Observability credentials."""

    # --------------------------------------------------------------------------
    # LLM Settings (Google Gemini)
    # --------------------------------------------------------------------------
    GEMINI_API_KEY: SecretStr = Field(
        ...,
        description="Google Gemini API Key for LLM reasoning and SQL generation.",
    )
    GEMINI_MODEL: str = Field(
        default="gemini-3.8-flash",
        description="Gemini model identifier to use.",
    )

    # --------------------------------------------------------------------------
    # Observability (LangSmith)
    # --------------------------------------------------------------------------
    LANGCHAIN_TRACING_V2: bool = Field(
        default=False,
        description="Enable LangSmith tracing for LangChain/LangGraph calls.",
    )
    LANGCHAIN_API_KEY: Optional[SecretStr] = Field(
        default=None,
        description="API key for LangSmith observability.",
    )
    LANGCHAIN_PROJECT: str = Field(
        default="query-pilot",
        description="LangSmith project name for traces.",
    )

    # --------------------------------------------------------------------------
    # API & CORS Settings
    # --------------------------------------------------------------------------
    CORS_ALLOWED_ORIGINS: List[str] = Field(
        default_factory=list,
        description="Allowed origins for Cross-Origin Resource Sharing (CORS). Empty disables CORS headers.",
    )

    @field_validator("CORS_ALLOWED_ORIGINS", mode="before")
    @classmethod
    def validate_and_normalize_cors_origins(cls, v: Any) -> List[str]:
        """Validate and normalize allowed CORS origins.

        Supports:
        - List of string origins.
        - Comma-separated string (e.g. 'http://localhost:5173,https://querypilot.com').
        - JSON array string (e.g. '["http://localhost:5173"]').

        Guarantees:
        - Strips leading/trailing whitespace.
        - Strips trailing slashes from valid origins.
        - Rejects wildcards ('*').
        - Rejects paths, queries, fragments, credentials, or non-http(s) schemes.
        - Deduplicates while preserving order.
        """
        if v is None:
            return []

        if isinstance(v, str):
            trimmed = v.strip()
            if not trimmed:
                return []
            if trimmed.startswith("[") and trimmed.endswith("]"):
                try:
                    parsed_json = json.loads(trimmed)
                except Exception as exc:
                    raise ValueError(
                        f"Failed to parse CORS_ALLOWED_ORIGINS as JSON array: {exc}"
                    ) from exc
                if not isinstance(parsed_json, list):
                    raise ValueError("CORS_ALLOWED_ORIGINS JSON must be a list.")
                raw_items = parsed_json
            else:
                raw_items = [item.strip() for item in trimmed.split(",") if item.strip()]
        elif isinstance(v, (list, tuple, set)):
            raw_items = list(v)
        else:
            raise ValueError(
                f"Invalid type for CORS_ALLOWED_ORIGINS: {type(v).__name__}. Must be a list or string."
            )

        validated: List[str] = []
        seen: Set[str] = set()

        for item in raw_items:
            if not isinstance(item, str):
                raise ValueError(f"Origin item must be a string, got {type(item).__name__}.")
            s = item.strip()
            if not s:
                continue

            if "*" in s:
                raise ValueError(
                    f"Wildcard CORS origin '{s}' is strictly disallowed for production safety."
                )

            s_clean = s.rstrip("/")

            parsed = urllib.parse.urlsplit(s_clean)
            if parsed.scheme not in ("http", "https"):
                raise ValueError(
                    f"Invalid CORS origin scheme '{parsed.scheme or s}': origin must start with 'http://' or 'https://'."
                )
            if not parsed.netloc:
                raise ValueError(f"Invalid CORS origin '{s}': missing host/network location.")
            if "@" in parsed.netloc:
                raise ValueError(f"Invalid CORS origin '{s}': user credentials are not permitted in origins.")
            if parsed.path and parsed.path != "":
                raise ValueError(
                    f"Invalid CORS origin '{s}': path components are not permitted in origins."
                )
            if parsed.query:
                raise ValueError(
                    f"Invalid CORS origin '{s}': query parameters are not permitted in origins."
                )
            if parsed.fragment:
                raise ValueError(
                    f"Invalid CORS origin '{s}': fragments are not permitted in origins."
                )

            canonical = f"{parsed.scheme}://{parsed.netloc}"
            if canonical not in seen:
                seen.add(canonical)
                validated.append(canonical)

        return validated

    @field_validator("GEMINI_API_KEY")
    @classmethod
    def validate_api_key_not_empty(cls, v: SecretStr) -> SecretStr:
        """Ensure the API key is not empty or a default placeholder."""
        raw_val = v.get_secret_value().strip()
        if not raw_val or raw_val == "your_gemini_api_key_here":
            raise ValueError(
                "GEMINI_API_KEY cannot be empty or the default placeholder."
            )
        return v


@lru_cache(maxsize=1)
def get_db_settings() -> DatabaseSettings:
    """Retrieve cached database-only settings instance."""
    return DatabaseSettings()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Retrieve cached full application settings instance."""
    return Settings()
