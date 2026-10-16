"""Tests for QueryPilot prototype security safeguards (Task 1.34-B1-F14).

Verifies:
1. Missing and incorrect prototype access keys are rejected with HTTP 401.
2. Correct prototype access key succeeds with HTTP 200.
3. Authorization occurs strictly before any runner, pipeline, LLM, or database execution.
4. Server-enforced SQL-only mode (QUERY_PILOT_FORCE_SQL_ONLY=True) forces sql_only execution
   even when the client sends execution_mode="execute", preventing live database execution.
5. Oversized request bodies exceeding QUERY_PILOT_MAX_REQUEST_BYTES are rejected with HTTP 413
   before pipeline or model invocation.
6. In-memory sliding-window rate limiting safely rejects excess queries with HTTP 429.
7. Concurrency guard safely rejects concurrent overload with HTTP 429 without thread starvation.
8. CORS preflight permits X-QueryPilot-Access-Key for configured origins.
9. Configuration fails startup when QUERY_PILOT_REQUIRE_ACCESS_KEY=True without a valid key.
"""

import asyncio
import json
from unittest.mock import MagicMock
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from query_pilot.api.app import create_app
from query_pilot.api.security import ACCESS_KEY_HEADER, ConcurrencyGuard, InMemoryRateLimiter
from query_pilot.config import Settings
from query_pilot.pipeline import PipelineResult, PipelineStatus


# ==============================================================================
# Fixtures
# ==============================================================================

@pytest.fixture
def mock_success_runner():
    """Create a mock QueryRunner returning a successful result."""
    runner = MagicMock()
    runner.run.return_value = PipelineResult(
        question="What is the average student GPA?",
        status=PipelineStatus.SUCCESS,
        generated_sql="SELECT AVG(GPA) FROM Students;",
        generation_explanation="Calculates average GPA.",
        request_id="req-sec-test-01",
    )
    return runner


@pytest.fixture
def base_settings():
    """Settings with a configured prototype access key."""
    return Settings(
        _env_file=None,
        GEMINI_API_KEY="test_gemini_key",
        QUERY_PILOT_ACCESS_KEY=SecretStr("prototype_secret_passcode_2026"),
        QUERY_PILOT_FORCE_SQL_ONLY=False,
        QUERY_PILOT_MAX_REQUEST_BYTES=1024,  # Small 1KB limit for testing
        QUERY_PILOT_RATE_LIMIT_PER_MINUTE=5,
        QUERY_PILOT_MAX_CONCURRENT_QUERIES=2,
    )


# ==============================================================================
# 1. Access Key Authorization Tests
# ==============================================================================

def test_missing_access_key_rejected(mock_success_runner, base_settings):
    """Verify request without X-QueryPilot-Access-Key receives 401 Unauthorized."""
    app = create_app(runner=mock_success_runner, settings=base_settings)
    client = TestClient(app)

    response = client.post("/query", json={"question": "What is the average student GPA?"})

    assert response.status_code == 401
    data = response.json()
    assert data["detail"] == "Invalid or missing access key."

    # Invariant: Pipeline and Gemini must NEVER be invoked on authorization failure
    mock_success_runner.run.assert_not_called()


def test_incorrect_access_key_rejected(mock_success_runner, base_settings):
    """Verify request with wrong access key receives 401 Unauthorized."""
    app = create_app(runner=mock_success_runner, settings=base_settings)
    client = TestClient(app)

    headers = {ACCESS_KEY_HEADER: "wrong_guess_key"}
    response = client.post(
        "/query",
        json={"question": "What is the average student GPA?"},
        headers=headers,
    )

    assert response.status_code == 401
    data = response.json()
    assert data["detail"] == "Invalid or missing access key."
    mock_success_runner.run.assert_not_called()


def test_correct_access_key_succeeds(mock_success_runner, base_settings):
    """Verify request with valid access key succeeds and executes pipeline."""
    app = create_app(runner=mock_success_runner, settings=base_settings)
    client = TestClient(app)

    headers = {ACCESS_KEY_HEADER: "prototype_secret_passcode_2026"}
    response = client.post(
        "/query",
        json={"question": "What is the average student GPA?"},
        headers=headers,
    )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "SUCCESS"
    assert mock_success_runner.run.call_count == 1


def test_unauthenticated_mode_when_key_unset(mock_success_runner):
    """Verify local development / test harness without access key works without header."""
    settings = Settings(
        _env_file=None,
        GEMINI_API_KEY="test_gemini_key",
        QUERY_PILOT_ACCESS_KEY=None,
    )
    app = create_app(runner=mock_success_runner, settings=settings)
    client = TestClient(app)

    response = client.post("/query", json={"question": "What is the average student GPA?"})
    assert response.status_code == 200
    assert mock_success_runner.run.call_count == 1


# ==============================================================================
# 2. Server-Enforced SQL-Only Mode Tests
# ==============================================================================

def test_server_enforced_sql_only_overrides_client_execute(mock_success_runner):
    """Verify server-enforced SQL-only mode forces sql_only even if client requests execute."""
    settings = Settings(
        _env_file=None,
        GEMINI_API_KEY="test_gemini_key",
        QUERY_PILOT_FORCE_SQL_ONLY=True,
    )
    app = create_app(runner=mock_success_runner, settings=settings)
    client = TestClient(app)

    # Client explicitly requests live execution
    payload = {
        "question": "Show all enrolled students",
        "execution_mode": "execute",
    }
    response = client.post("/query", json=payload)

    assert response.status_code == 200
    # Crucial assertion: runner must receive execution_mode="sql_only", overriding client request
    mock_success_runner.run.assert_called_once()
    _, kwargs = mock_success_runner.run.call_args
    assert kwargs.get("execution_mode") == "sql_only"


def test_server_enforced_sql_only_with_real_pipeline():
    """Verify end-to-end with real QueryRunner that forced SQL-only mode avoids DB connection."""
    from query_pilot.runner import QueryRunner
    from query_pilot.sql.generation import SQLGenerationStatus

    # Provide a mock generator that returns valid SQL conforming to GradeSense demo schema
    mock_gen = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status = SQLGenerationStatus.ANSWERABLE
    mock_resp.sql = "SELECT COUNT(*) AS total_students FROM Students"
    mock_resp.explanation = "Counts total students."
    mock_resp.assumptions = []
    mock_resp.clarification_prompt = None
    mock_resp.clarification_options = []
    mock_gen.generate.return_value = mock_resp

    # Provide a mock engine that asserts it is NEVER connected to
    mock_engine = MagicMock()
    mock_engine.connect.side_effect = AssertionError("Database connection attempted in SQL-only mode!")

    runner = QueryRunner(generator=mock_gen, engine=mock_engine)
    settings = Settings(
        _env_file=None,
        GEMINI_API_KEY="test_gemini_key",
        QUERY_PILOT_FORCE_SQL_ONLY=True,
    )

    app = create_app(runner=runner, settings=settings)
    client = TestClient(app)

    payload = {"question": "List department GPAs", "execution_mode": "execute"}
    response = client.post("/query", json=payload)

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "VALIDATED_ONLY"
    mock_engine.connect.assert_not_called()


# ==============================================================================
# 3. Request Payload Size Limit Tests (Streaming & ASGI Lifecycle)
# ==============================================================================

async def _invoke_raw_asgi(app, method: str, path: str, headers: list, chunks: list):
    """Execute a raw ASGI request with controlled chunks and track consumption."""
    sent = []
    chunk_idx = 0
    recv_calls = 0

    async def mock_receive():
        nonlocal chunk_idx, recv_calls
        recv_calls += 1
        if chunk_idx < len(chunks):
            c = chunks[chunk_idx]
            chunk_idx += 1
            return {"type": "http.request", "body": c, "more_body": chunk_idx < len(chunks)}
        return {"type": "http.request", "body": b"", "more_body": False}

    async def mock_send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": headers,
        "query_string": b"",
        "app": app,
    }

    await app(scope, mock_receive, mock_send)
    start_msg = next((m for m in sent if m["type"] == "http.response.start"), None)
    body_msgs = [m.get("body", b"") for m in sent if m["type"] == "http.response.body"]
    status_code = start_msg["status"] if start_msg else None
    resp_headers = dict(start_msg.get("headers", [])) if start_msg else {}
    body_bytes = b"".join(body_msgs)
    return status_code, resp_headers, body_bytes, recv_calls, chunk_idx


def test_valid_payload_at_or_below_limit(mock_success_runner, base_settings):
    """Verify a valid request at or below max_bytes proceeds normally and remains readable."""
    app = create_app(runner=mock_success_runner, settings=base_settings)
    client = TestClient(app)

    # Payload well within 1024-byte limit
    payload = {"question": "What is the average student GPA?"}
    headers = {
        ACCESS_KEY_HEADER: "prototype_secret_passcode_2026",
        "X-Request-ID": "req-valid-size-01",
    }
    response = client.post("/query", json=payload, headers=headers)

    assert response.status_code == 200
    assert response.headers.get("X-Request-ID") == "req-sec-test-01"
    mock_success_runner.run.assert_called_once()
    _, kwargs = mock_success_runner.run.call_args
    assert kwargs.get("question") == "What is the average student GPA?"
    assert kwargs.get("request_id") == "req-valid-size-01"


def test_oversized_payload_rejected_by_content_length(mock_success_runner, base_settings):
    """Verify request with Content-Length exceeding QUERY_PILOT_MAX_REQUEST_BYTES returns 413."""
    app = create_app(runner=mock_success_runner, settings=base_settings)
    client = TestClient(app)

    # base_settings sets max_bytes = 1024
    huge_question = "What is the status? " + ("A" * 1500)
    response = client.post(
        "/query",
        json={"question": huge_question},
        headers={
            ACCESS_KEY_HEADER: "prototype_secret_passcode_2026",
            "X-Request-ID": "req-oversized-cl-01",
        },
    )

    assert response.status_code == 413
    assert response.headers.get("X-Request-ID") == "req-oversized-cl-01"
    data = response.json()
    assert data["detail"] == "Request payload exceeds maximum allowed size of 1024 bytes."
    mock_success_runner.run.assert_not_called()


def test_early_rejection_by_content_length_does_not_consume_body(mock_success_runner, base_settings):
    """Verify Content-Length exceeding max_bytes rejects immediately without reading body chunks."""
    app = create_app(runner=mock_success_runner, settings=base_settings)

    headers = [
        (b"host", b"testserver"),
        (b"content-type", b"application/json"),
        (b"content-length", b"5000"),
        (b"x-request-id", b"req-early-cl-reject"),
        (ACCESS_KEY_HEADER.lower().encode("latin1"), b"prototype_secret_passcode_2026"),
    ]
    chunks = [b"x" * 1000 for _ in range(5)]

    status_code, resp_headers, body_bytes, recv_calls, chunks_consumed = asyncio.run(
        _invoke_raw_asgi(app, "POST", "/query", headers, chunks)
    )

    assert status_code == 413
    assert resp_headers.get(b"x-request-id") == b"req-early-cl-reject"
    body_data = json.loads(body_bytes.decode("utf-8"))
    assert "exceeds maximum allowed size of 1024 bytes" in body_data["detail"]

    # Crucial assertion: Not a single chunk was consumed from the body stream
    assert chunks_consumed == 0
    assert recv_calls == 0
    mock_success_runner.run.assert_not_called()


def test_missing_content_length_chunked_rejected_while_streaming(mock_success_runner, base_settings):
    """Verify chunked transfer with no Content-Length accumulates bytes and aborts early."""
    app = create_app(runner=mock_success_runner, settings=base_settings)

    # 5 chunks of 300 bytes each = 1500 bytes (limit is 1024)
    # The limit is exceeded during chunk 4 (4 * 300 = 1200 > 1024)
    chunks = [
        b'{"question": "',
        b"A" * 300,
        b"B" * 300,
        b"C" * 300,
        b"D" * 300,
        b'"}',
    ]
    headers = [
        (b"host", b"testserver"),
        (b"content-type", b"application/json"),
        (b"x-request-id", b"req-chunked-stream-01"),
        (ACCESS_KEY_HEADER.lower().encode("latin1"), b"prototype_secret_passcode_2026"),
    ]

    status_code, resp_headers, body_bytes, recv_calls, chunks_consumed = asyncio.run(
        _invoke_raw_asgi(app, "POST", "/query", headers, chunks)
    )

    assert status_code == 413
    assert resp_headers.get(b"x-request-id") == b"req-chunked-stream-01"
    body_data = json.loads(body_bytes.decode("utf-8"))
    assert "exceeds maximum allowed size of 1024 bytes" in body_data["detail"]

    # Crucial assertion: Stream reading was aborted before the final chunks were read
    assert chunks_consumed < len(chunks)
    mock_success_runner.run.assert_not_called()


def test_understated_content_length_does_not_bypass_actual_counting(mock_success_runner, base_settings):
    """Verify misleading smaller Content-Length (e.g. 50) cannot bypass streaming accumulation."""
    app = create_app(runner=mock_success_runner, settings=base_settings)

    # Header falsely claims 50 bytes, but stream delivers 1500 bytes
    headers = [
        (b"host", b"testserver"),
        (b"content-type", b"application/json"),
        (b"content-length", b"50"),
        (b"x-request-id", b"req-understated-cl-01"),
        (ACCESS_KEY_HEADER.lower().encode("latin1"), b"prototype_secret_passcode_2026"),
    ]
    chunks = [
        b'{"question": "',
        b"A" * 400,
        b"B" * 400,
        b"C" * 400,
        b'"}',
    ]

    status_code, resp_headers, body_bytes, recv_calls, chunks_consumed = asyncio.run(
        _invoke_raw_asgi(app, "POST", "/query", headers, chunks)
    )

    assert status_code == 413
    assert resp_headers.get(b"x-request-id") == b"req-understated-cl-01"
    body_data = json.loads(body_bytes.decode("utf-8"))
    assert "exceeds maximum allowed size of 1024 bytes" in body_data["detail"]

    assert chunks_consumed < len(chunks)
    mock_success_runner.run.assert_not_called()


def test_invalid_content_length_string_falls_back_to_streaming_limit(mock_success_runner, base_settings):
    """Verify non-integer or malformed Content-Length falls back to streaming chunk counter."""
    app = create_app(runner=mock_success_runner, settings=base_settings)

    headers = [
        (b"host", b"testserver"),
        (b"content-type", b"application/json"),
        (b"content-length", b"not-a-number"),
        (b"x-request-id", b"req-invalid-cl-01"),
        (ACCESS_KEY_HEADER.lower().encode("latin1"), b"prototype_secret_passcode_2026"),
    ]
    chunks = [
        b'{"question": "',
        b"A" * 600,
        b"B" * 600,
        b'"}',
    ]

    status_code, resp_headers, body_bytes, recv_calls, chunks_consumed = asyncio.run(
        _invoke_raw_asgi(app, "POST", "/query", headers, chunks)
    )

    assert status_code == 413
    assert resp_headers.get(b"x-request-id") == b"req-invalid-cl-01"
    mock_success_runner.run.assert_not_called()


def test_valid_chunked_streaming_request_succeeds(mock_success_runner, base_settings):
    """Verify valid chunked streaming payload below limit is fully parsed and passed to endpoint."""
    app = create_app(runner=mock_success_runner, settings=base_settings)

    valid_question = "What is the average student GPA?"
    chunks = [
        b'{"question": "',
        b"What is the average",
        b" student GPA?\"}",
    ]
    headers = [
        (b"host", b"testserver"),
        (b"content-type", b"application/json"),
        (b"x-request-id", b"req-valid-chunked-01"),
        (ACCESS_KEY_HEADER.lower().encode("latin1"), b"prototype_secret_passcode_2026"),
    ]

    status_code, resp_headers, body_bytes, recv_calls, chunks_consumed = asyncio.run(
        _invoke_raw_asgi(app, "POST", "/query", headers, chunks)
    )

    assert status_code == 200
    assert resp_headers.get(b"x-request-id") == b"req-sec-test-01"
    assert chunks_consumed == len(chunks)
    mock_success_runner.run.assert_called_once()
    _, kwargs = mock_success_runner.run.call_args
    assert kwargs.get("question") == valid_question
    assert kwargs.get("request_id") == "req-valid-chunked-01"


def test_streaming_rejection_preserves_cors_headers(mock_success_runner):
    """Verify 413 responses emitted during streaming or early rejection retain CORS headers."""
    settings = Settings(
        _env_file=None,
        GEMINI_API_KEY="test_gemini_key",
        QUERY_PILOT_ACCESS_KEY=SecretStr("prototype_secret_passcode_2026"),
        QUERY_PILOT_MAX_REQUEST_BYTES=1024,
        CORS_ALLOWED_ORIGINS=["https://dashboard.example.com"],
    )
    app = create_app(runner=mock_success_runner, settings=settings)

    headers = [
        (b"host", b"testserver"),
        (b"origin", b"https://dashboard.example.com"),
        (b"content-type", b"application/json"),
        (b"content-length", b"2000"),
        (b"x-request-id", b"req-cors-413-test"),
        (ACCESS_KEY_HEADER.lower().encode("latin1"), b"prototype_secret_passcode_2026"),
    ]

    status_code, resp_headers, body_bytes, _, _ = asyncio.run(
        _invoke_raw_asgi(app, "POST", "/query", headers, [b"x" * 2000])
    )

    assert status_code == 413
    assert resp_headers.get(b"access-control-allow-origin") == b"https://dashboard.example.com"
    assert resp_headers.get(b"x-request-id") == b"req-cors-413-test"


# ==============================================================================
# 4. Rate Limiting Tests
# ==============================================================================

def test_rate_limiting_enforced(mock_success_runner, base_settings):
    """Verify rate limiter rejects requests when limit is exceeded within 60s."""
    # base_settings sets rate_limit_per_minute = 5
    app = create_app(runner=mock_success_runner, settings=base_settings)
    client = TestClient(app)

    headers = {ACCESS_KEY_HEADER: "prototype_secret_passcode_2026"}

    # 5 requests should succeed
    for _ in range(5):
        resp = client.post("/query", json={"question": "Test query"}, headers=headers)
        assert resp.status_code == 200

    # 6th request should fail with 429
    resp_rate_limited = client.post("/query", json={"question": "Test query"}, headers=headers)
    assert resp_rate_limited.status_code == 429
    data = resp_rate_limited.json()
    assert "Rate limit exceeded" in data["detail"]

    # Invariant: runner called exactly 5 times, NOT 6 times
    assert mock_success_runner.run.call_count == 5


# ==============================================================================
# 5. Concurrency Limiting Tests
# ==============================================================================

def test_concurrency_guard_rejects_busy(mock_success_runner, base_settings):
    """Verify concurrency guard returns 429 when concurrency slots are saturated."""
    # base_settings sets max_concurrent = 2
    app = create_app(runner=mock_success_runner, settings=base_settings)
    client = TestClient(app)

    headers = {ACCESS_KEY_HEADER: "prototype_secret_passcode_2026"}

    # Simulate saturation of the 2 concurrency slots
    guard: ConcurrencyGuard = app.state.concurrency_guard
    assert guard.acquire(timeout=0.0) is True
    assert guard.acquire(timeout=0.0) is True

    try:
        # A new request should now be rejected immediately
        response = client.post("/query", json={"question": "Test query"}, headers=headers)
        assert response.status_code == 429
        data = response.json()
        assert "Server is busy processing concurrent queries" in data["detail"]
        mock_success_runner.run.assert_not_called()
    finally:
        guard.release()
        guard.release()

    # Now that slots are released, a subsequent request must succeed
    response_after = client.post("/query", json={"question": "Test query"}, headers=headers)
    assert response_after.status_code == 200
    assert mock_success_runner.run.call_count == 1


# ==============================================================================
# 6. CORS and Access Key Header Policy
# ==============================================================================

def test_cors_allows_access_key_header_for_whitelisted_origin(mock_success_runner):
    """Verify CORS preflight permits X-QueryPilot-Access-Key for configured origins."""
    settings = Settings(
        _env_file=None,
        GEMINI_API_KEY="test_key",
        CORS_ALLOWED_ORIGINS=["https://prototype.onrender.com"],
        QUERY_PILOT_ACCESS_KEY=SecretStr("test_secret"),
    )
    app = create_app(runner=mock_success_runner, settings=settings)
    client = TestClient(app)

    # Preflight request from approved origin requesting X-QueryPilot-Access-Key
    preflight = client.options(
        "/query",
        headers={
            "Origin": "https://prototype.onrender.com",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": f"content-type, {ACCESS_KEY_HEADER.lower()}",
        },
    )

    assert preflight.status_code == 200
    allow_headers = preflight.headers.get("access-control-allow-headers", "").lower()
    assert ACCESS_KEY_HEADER.lower() in allow_headers


def test_cors_rejects_unwhitelisted_origin(mock_success_runner):
    """Verify CORS preflight does not grant access headers to unauthorized origins."""
    settings = Settings(
        _env_file=None,
        GEMINI_API_KEY="test_key",
        CORS_ALLOWED_ORIGINS=["https://prototype.onrender.com"],
    )
    app = create_app(runner=mock_success_runner, settings=settings)
    client = TestClient(app)

    preflight = client.options(
        "/query",
        headers={
            "Origin": "https://malicious-site.com",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )

    assert "access-control-allow-origin" not in preflight.headers


# ==============================================================================
# 7. Fail-Closed Startup Configuration Tests
# ==============================================================================

def test_fail_closed_when_access_key_required_but_missing():
    """Verify startup validation fails if QUERY_PILOT_REQUIRE_ACCESS_KEY is True but key is missing."""
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            _env_file=None,
            GEMINI_API_KEY="test_key",
            QUERY_PILOT_REQUIRE_ACCESS_KEY=True,
            QUERY_PILOT_ACCESS_KEY=None,
        )
    assert "QUERY_PILOT_REQUIRE_ACCESS_KEY is enabled, but QUERY_PILOT_ACCESS_KEY is empty" in str(exc_info.value)


def test_fail_closed_when_access_key_required_but_empty_whitespace():
    """Verify startup validation fails if access key is empty whitespace."""
    with pytest.raises(ValidationError) as exc_info:
        Settings(
            _env_file=None,
            GEMINI_API_KEY="test_key",
            QUERY_PILOT_REQUIRE_ACCESS_KEY=True,
            QUERY_PILOT_ACCESS_KEY=SecretStr("   "),
        )
    assert "QUERY_PILOT_REQUIRE_ACCESS_KEY is enabled, but QUERY_PILOT_ACCESS_KEY is empty" in str(exc_info.value)


def test_startup_succeeds_when_access_key_required_and_provided():
    """Verify startup succeeds when QUERY_PILOT_REQUIRE_ACCESS_KEY=True and key is set."""
    settings = Settings(
        _env_file=None,
        GEMINI_API_KEY="test_key",
        QUERY_PILOT_REQUIRE_ACCESS_KEY=True,
        QUERY_PILOT_ACCESS_KEY=SecretStr("valid_secret_code_123"),
    )
    assert settings.QUERY_PILOT_ACCESS_KEY.get_secret_value() == "valid_secret_code_123"
