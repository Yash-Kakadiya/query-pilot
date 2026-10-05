"""Configuration management for QueryPilot using Pydantic Settings.

Cleanly separates database connection and safety settings from LLM credentials.
Ensures fail-fast environment variable validation and prevents secret leakage.
"""

from functools import lru_cache
from typing import Optional
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
        default="gemini-2.0-flash",
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
