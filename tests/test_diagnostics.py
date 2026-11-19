"""Unit tests for Self-Healing Diagnostics Engine (Milestone 1).

Verifies:
1. Accurate categorization across API keys, models, connections, SSL, and safety rejections.
2. Sensitive password and token redaction invariant.
3. Appropriate 1-click action recommendations.
"""

from query_pilot.agent.diagnostics import (
    DiagnosticCategory,
    classify_diagnostic,
    sanitize_error_string,
)


def test_sanitize_error_string_redacts_credentials():
    raw_error = "Failed to connect: server='sql.example.com' user='admin' password='SuperSecretPassword123' db='corp'"
    sanitized = sanitize_error_string(raw_error)
    assert "SuperSecretPassword123" not in sanitized
    assert "[REDACTED]" in sanitized

    # Check Gemini key pattern redaction
    dummy_key = "AIzaSy" + "A" * 33
    key_err = f"Google API error using key {dummy_key}"
    sanitized_key = sanitize_error_string(key_err)
    assert dummy_key not in sanitized_key
    assert "AIzaSy[REDACTED]" in sanitized_key


def test_classify_gemini_api_key_missing_or_invalid():
    # Missing key in config
    diag = classify_diagnostic("ConfigurationError: GEMINI_API_KEY is not configured or contains a default placeholder.")
    assert diag.category == DiagnosticCategory.API_KEY
    assert "Key Required" in diag.title
    assert diag.primary_action is not None
    assert diag.primary_action.action_type == "open_external_url"
    assert "aistudio.google.com" in (diag.primary_action.payload or "")

    # Google 400 API_KEY_INVALID
    diag2 = classify_diagnostic("API error: 400 API_KEY_INVALID", status_code=400)
    assert diag2.category == DiagnosticCategory.API_KEY
    assert diag2.secondary_action is not None
    assert diag2.secondary_action.action_type == "focus_api_key_input"


def test_classify_gemini_rate_limit_429():
    diag = classify_diagnostic("429 Resource has been exhausted (e.g. check quota).", status_code=429)
    assert diag.category == DiagnosticCategory.MODEL_QUOTA
    assert "Rate Limit" in diag.title
    assert diag.primary_action is not None
    assert diag.primary_action.action_type == "retry"
    assert diag.secondary_action is not None
    assert diag.secondary_action.action_type == "switch_model"
    assert diag.secondary_action.payload == "gemini-3.5-flash-lite"


def test_classify_gemini_model_not_found():
    diag = classify_diagnostic("models/gemini-preview is not found", status_code=404)
    assert diag.category == DiagnosticCategory.MODEL_UNAVAILABLE
    assert "Model Unavailable" in diag.title
    assert diag.primary_action is not None
    assert diag.primary_action.action_type == "switch_model"
    assert diag.primary_action.payload == "gemini-3.8-flash"


def test_classify_db_connection_refused():
    diag = classify_diagnostic(
        "psycopg.OperationalError: connection to server at '127.0.0.1', port 5432 failed: Connection refused (0x0000274D/10061)",
        context={"port": 5432, "server": "127.0.0.1"},
    )
    assert diag.category == DiagnosticCategory.DB_CONNECTION_REFUSED
    assert "Connection Refused" in diag.title
    assert "5432" in diag.summary
    assert diag.primary_action is not None
    assert diag.primary_action.action_type == "open_profile_modal"


def test_classify_db_auth_failed():
    diag = classify_diagnostic(
        'psycopg.OperationalError: password authentication failed for user "postgres"',
    )
    assert diag.category == DiagnosticCategory.DB_AUTH_FAILED
    assert "Authentication Failed" in diag.title
    assert diag.primary_action is not None
    assert diag.primary_action.action_type == "open_profile_modal"


def test_classify_db_ssl_required_cloud():
    diag = classify_diagnostic(
        'psycopg.OperationalError: no pg_hba.conf entry for host "1.2.3.4", user "postgres", database "postgres", no encryption, SSL off'
    )
    assert diag.category == DiagnosticCategory.DB_SSL_REQUIRED
    assert "SSL Encryption Required" in diag.title
    assert diag.primary_action is not None
    assert diag.primary_action.action_type == "open_profile_modal"
    assert diag.primary_action.payload == "ssl_enable"


def test_classify_db_safety_mutation_rejection():
    diag = classify_diagnostic("SQLMutationError: Operation 'DELETE' is prohibited in read-only mode.")
    assert diag.category == DiagnosticCategory.DB_SAFETY_MUTATION
    assert "Modification Blocked" in diag.title
    assert "read-only" in diag.summary.lower()


def test_classify_db_schema_not_found():
    diag = classify_diagnostic("psycopg.errors.UndefinedTable: relation 'users_tbl' does not exist")
    assert diag.category == DiagnosticCategory.DB_SCHEMA_NOT_FOUND
    assert "Table or Column Not Found" in diag.title


def test_classify_db_timeout_ceiling():
    diag = classify_diagnostic("Statement execution exceeded maximum timeout ceiling (15s)")
    assert diag.category == DiagnosticCategory.DB_TIMEOUT
    assert "Timed Out" in diag.title
    assert "15s Ceiling" in diag.title
