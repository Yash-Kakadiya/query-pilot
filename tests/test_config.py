"""Unit tests for query_pilot.config Settings and DatabaseSettings."""

import pytest
from pydantic import ValidationError
from query_pilot.config import DatabaseSettings, Settings, get_db_settings


def test_database_settings_without_llm_key(monkeypatch):
    """Verify that DatabaseSettings can be instantiated without GEMINI_API_KEY."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("DB_USER", raising=False)
    monkeypatch.delenv("DB_PASSWORD", raising=False)

    db_settings = DatabaseSettings(_env_file=None)

    assert db_settings.DB_SERVER == r"localhost\SQLEXPRESS"
    assert db_settings.DB_NAME == "GradeSense_Local"
    assert db_settings.DB_DRIVER == "ODBC Driver 17 for SQL Server"
    assert db_settings.DB_USE_TRUSTED_CONNECTION is True
    assert db_settings.DB_QUERY_TIMEOUT_SECONDS == 15
    assert db_settings.DB_MAX_ROW_LIMIT == 100
    assert not hasattr(db_settings, "GEMINI_API_KEY")

    # Connection strings still function correctly
    conn_str = db_settings.get_odbc_connection_string()
    assert "Trusted_Connection=yes" in conn_str
    sa_url = db_settings.get_sqlalchemy_url()
    assert sa_url.startswith("mssql+pyodbc:///?odbc_connect=")


def test_database_settings_secret_obfuscation(monkeypatch):
    """Verify that sensitive database password is never exposed in string representation."""
    monkeypatch.setenv("DB_PASSWORD", "secret_db_pass_123")
    monkeypatch.setenv("DB_USER", "db_reader")
    monkeypatch.setenv("DB_USE_TRUSTED_CONNECTION", "false")

    db_settings = DatabaseSettings(_env_file=None)

    assert "secret_db_pass_123" not in repr(db_settings)
    assert "secret_db_pass_123" not in str(db_settings)
    assert db_settings.DB_PASSWORD.get_secret_value() == "secret_db_pass_123"


def test_settings_valid_defaults(monkeypatch):
    """Test that application Settings load valid defaults when GEMINI_API_KEY is supplied."""
    monkeypatch.setenv("GEMINI_API_KEY", "valid_test_gemini_api_key")
    monkeypatch.delenv("LANGCHAIN_API_KEY", raising=False)
    monkeypatch.delenv("DB_USER", raising=False)
    monkeypatch.delenv("DB_PASSWORD", raising=False)

    settings = Settings(_env_file=None)

    assert settings.GEMINI_API_KEY.get_secret_value() == "valid_test_gemini_api_key"
    assert settings.GEMINI_MODEL == "gemini-2.0-flash"
    assert settings.LANGCHAIN_TRACING_V2 is False
    assert settings.LANGCHAIN_API_KEY is None
    assert settings.LANGCHAIN_PROJECT == "query-pilot"
    assert settings.DB_SERVER == r"localhost\SQLEXPRESS"
    assert settings.DB_NAME == "GradeSense_Local"
    assert settings.DB_DRIVER == "ODBC Driver 17 for SQL Server"
    assert settings.DB_USE_TRUSTED_CONNECTION is True
    assert settings.DB_QUERY_TIMEOUT_SECONDS == 15
    assert settings.DB_MAX_ROW_LIMIT == 100


def test_settings_still_requires_gemini_api_key(monkeypatch):
    """Verify that full application Settings still strictly rejects missing GEMINI_API_KEY."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None)
    assert "GEMINI_API_KEY" in str(exc_info.value)


def test_settings_placeholder_gemini_api_key(monkeypatch):
    """Test that placeholder GEMINI_API_KEY is rejected."""
    monkeypatch.setenv("GEMINI_API_KEY", "your_gemini_api_key_here")
    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None)
    assert "cannot be empty or the default placeholder" in str(exc_info.value)


def test_settings_secret_obfuscation(monkeypatch):
    """Verify that sensitive fields are never exposed in string representations."""
    secret_key = "super_secret_production_key"
    secret_db_pass = "super_secret_db_password"
    monkeypatch.setenv("GEMINI_API_KEY", secret_key)
    monkeypatch.setenv("DB_PASSWORD", secret_db_pass)

    settings = Settings(_env_file=None)

    repr_str = repr(settings)
    str_val = str(settings)

    assert secret_key not in repr_str
    assert secret_key not in str_val
    assert secret_db_pass not in repr_str
    assert secret_db_pass not in str_val

    assert settings.GEMINI_API_KEY.get_secret_value() == secret_key
    assert settings.DB_PASSWORD.get_secret_value() == secret_db_pass


def test_odbc_connection_string_trusted(monkeypatch):
    """Verify ODBC string generation with Windows Integrated Authentication."""
    monkeypatch.setenv("GEMINI_API_KEY", "test_key")
    settings = Settings(_env_file=None)

    conn_str = settings.get_odbc_connection_string()
    assert "DRIVER={ODBC Driver 17 for SQL Server}" in conn_str
    assert r"SERVER=localhost\SQLEXPRESS" in conn_str
    assert "DATABASE=GradeSense_Local" in conn_str
    assert "Trusted_Connection=yes" in conn_str
    assert "UID=" not in conn_str
    assert "PWD=" not in conn_str


def test_odbc_connection_string_sql_auth(monkeypatch):
    """Verify ODBC string generation with SQL Authentication credentials."""
    monkeypatch.setenv("GEMINI_API_KEY", "test_key")
    monkeypatch.setenv("DB_USE_TRUSTED_CONNECTION", "false")
    monkeypatch.setenv("DB_USER", "querypilot_reader")
    monkeypatch.setenv("DB_PASSWORD", "reader_pass_999")

    settings = Settings(_env_file=None)

    conn_str = settings.get_odbc_connection_string()
    assert "Trusted_Connection=yes" not in conn_str
    assert "UID=querypilot_reader" in conn_str
    assert "PWD=reader_pass_999" in conn_str


def test_sqlalchemy_url_generation(monkeypatch):
    """Verify that SQLAlchemy mssql+pyodbc connection URL is properly encoded."""
    monkeypatch.setenv("GEMINI_API_KEY", "test_key")
    settings = Settings(_env_file=None)

    sa_url = settings.get_sqlalchemy_url()
    assert sa_url.startswith("mssql+pyodbc:///?odbc_connect=")


def test_guardrails_validation_bounds(monkeypatch):
    """Verify that invalid timeout and row limit bounds trigger validation errors."""
    monkeypatch.setenv("GEMINI_API_KEY", "test_key")

    with pytest.raises(ValidationError):
        Settings(_env_file=None, DB_QUERY_TIMEOUT_SECONDS=0)

    with pytest.raises(ValidationError):
        Settings(_env_file=None, DB_QUERY_TIMEOUT_SECONDS=300)

    with pytest.raises(ValidationError):
        Settings(_env_file=None, DB_MAX_ROW_LIMIT=0)

    with pytest.raises(ValidationError):
        Settings(_env_file=None, DB_MAX_ROW_LIMIT=5000)
