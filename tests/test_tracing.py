"""Offline tests for privacy-safe opt-in LangSmith tracing (Task 1.31-I-R3).

Verifies:
1. Tracing is disabled by default.
2. Disabled tracing causes zero network activity and instantiates no LangSmith Client.
3. Graph invocation receives a safe, correlated RunnableConfig with sanitized request_id, dialect, and tags.
4. Metadata and naming safety neutralizes injection attempts in request_id, dialect, run names, and tags.
5. Unified SDK-level anonymizer scrubs exception strings and tracebacks to '[REDACTED_ERROR]'.
6. Outbound transport boundary (Client._create_run and Client._update_run) intercepts payloads and confirms
   deliberate sentinels (questions, SQL, schema, rows, errors) never reach the transport layer.
7. Direct Gemini generator calls correlate as child spans and are sanitized at the transport boundary.
8. Explicit per-request configuration passes callbacks and clients without mutating process-wide environment.
9. Missing credentials and simulated tracing failures fail open cleanly without breaking the pipeline.
10. Existing graph, validation, recovery, and observability paths remain completely intact.
"""

from typing import Any, Dict, List, Optional
import os
import pytest
import re
from pydantic import SecretStr

from langchain_core.tracers.run_collector import RunCollectorCallbackHandler
from query_pilot.config import Settings
from query_pilot.db.models import ColumnMetadata, DatabaseSchema, TableMetadata
from query_pilot.graph.workflow import QueryPilotWorkflow, create_query_graph, run_query_graph
from query_pilot.pipeline import PipelineStatus
from query_pilot.presentation.synthesizer import AnswerSynthesisService, GroundedAnswerStatus
from query_pilot.sql.generation import (
    SQLGenerationRequest,
    SQLGenerationResponse,
    SQLGenerationStatus,
    SQLGenerator,
)
from query_pilot.tracing import (
    PrivacySafeLangChainTracer,
    QueryPilotClient,
    anonymize_trace_payload,
    configure_tracing,
    create_privacy_safe_client,
    get_runnable_config,
    get_tracing_callbacks,
    is_query_pilot_client,
    is_tracing_enabled,
    redact_sensitive_payload,
    sanitize_safe_identifier,
    temporary_tracing_context,
    trace_gemini_call,
)


# ==============================================================================
# In-Memory Test Fixtures & Fakes
# ==============================================================================

class FakeTracedGenerator(SQLGenerator):
    """Deterministic in-memory SQLGenerator simulating Gemini generation."""

    def __init__(self, response_sql: str = "SELECT Id FROM Students;"):
        self.response_sql = response_sql
        self.call_count = 0

    @trace_gemini_call(name="gemini_generate_sql", run_type="llm")
    def generate(self, request: SQLGenerationRequest) -> SQLGenerationResponse:
        self.call_count += 1
        return SQLGenerationResponse(
            status=SQLGenerationStatus.ANSWERABLE,
            sql=self.response_sql,
            explanation="Deterministic test explanation",
        )


@pytest.fixture
def offline_test_schema() -> DatabaseSchema:
    """Deterministic in-memory schema for offline tracing tests."""
    return DatabaseSchema(
        database_name="GradeSense",
        is_model_facing=True,
        tables={
            "Students": TableMetadata(
                schema_name="dbo",
                table_name="Students",
                columns=[
                    ColumnMetadata(name="Id", data_type="int", nullable=False, primary_key=True),
                    ColumnMetadata(name="DepartmentId", data_type="int", nullable=False),
                ],
                primary_keys=["Id"],
            )
        },
    )


@pytest.fixture(autouse=True)
def disable_langsmith_background_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure LangSmith Client never spawns background threads during offline unit tests."""
    from langsmith.client import Client

    orig_init = Client.__init__

    def no_thread_init(self: Any, *args: Any, **kwargs: Any) -> None:
        kwargs["auto_batch_tracing"] = False
        orig_init(self, *args, **kwargs)

    monkeypatch.setattr("langsmith.client.Client.__init__", no_thread_init)


@pytest.fixture
def enabled_tracing_settings() -> Settings:
    """Settings instance configured with opt-in tracing enabled."""
    return Settings(
        GEMINI_API_KEY=SecretStr("mock_gemini_key_for_testing"),
        LANGCHAIN_TRACING_V2=True,
        LANGCHAIN_API_KEY=SecretStr("ls__test_mock_api_key_valid"),
        LANGCHAIN_PROJECT="query-pilot-test",
        _env_file=None,
    )


# ==============================================================================
# Test Suite 1: Disabled-by-Default & Configuration Invariants
# ==============================================================================

class TestTracingConfigurationDefaults:
    """Verifies that tracing is disabled by default and handles configuration cleanly."""

    def test_tracing_disabled_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify tracing is strictly inactive when default settings are loaded."""
        monkeypatch.delenv("LANGCHAIN_TRACING_V2", raising=False)
        monkeypatch.delenv("LANGCHAIN_API_KEY", raising=False)

        settings = Settings(
            GEMINI_API_KEY=SecretStr("mock_gemini_key_for_testing"),
            _env_file=None,
        )

        assert settings.LANGCHAIN_TRACING_V2 is False
        assert is_tracing_enabled(settings) is False
        assert configure_tracing(settings) is False
        assert create_privacy_safe_client(settings) is None
        assert get_tracing_callbacks(settings) == []

    def test_runnable_config_structure_when_disabled(self) -> None:
        """Verify RunnableConfig includes safe metadata and tags, but no callbacks when disabled."""
        config = get_runnable_config(request_id="req-offline-100", dialect="T-SQL")

        assert config["run_name"] == "query_pilot:req-offline-100"
        assert config["tags"] == ["query-pilot", "T-SQL"]
        assert config["metadata"]["request_id"] == "req-offline-100"
        assert config["metadata"]["dialect"] == "T-SQL"
        assert "callbacks" not in config

    def test_missing_or_placeholder_api_key_disables_tracing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify that enabling tracing without a valid API key fails closed for external tracing."""
        monkeypatch.delenv("LANGCHAIN_API_KEY", raising=False)

        # Tracing True, but API key is default placeholder
        settings = Settings(
            GEMINI_API_KEY=SecretStr("mock_gemini_key_for_testing"),
            LANGCHAIN_TRACING_V2=True,
            LANGCHAIN_API_KEY=SecretStr("your_langsmith_api_key_here"),
            _env_file=None,
        )

        assert configure_tracing(settings) is False
        assert create_privacy_safe_client(settings) is None
        assert get_tracing_callbacks(settings) == []

    def test_temporary_tracing_context_restores_environment(
        self, enabled_tracing_settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify temporary_tracing_context sets env vars during context and cleanly restores after."""
        monkeypatch.delenv("LANGCHAIN_TRACING_V2", raising=False)
        monkeypatch.delenv("LANGCHAIN_PROJECT", raising=False)
        monkeypatch.delenv("LANGCHAIN_API_KEY", raising=False)

        with temporary_tracing_context(enabled_tracing_settings) as active:
            assert active is True
            assert os.environ.get("LANGCHAIN_TRACING_V2") == "true"
            assert os.environ.get("LANGCHAIN_PROJECT") == "query-pilot-test"
            assert os.environ.get("LANGCHAIN_API_KEY") == "ls__test_mock_api_key_valid"

        # Confirms restoration
        assert os.environ.get("LANGCHAIN_TRACING_V2") is None
        assert os.environ.get("LANGCHAIN_PROJECT") is None
        assert os.environ.get("LANGCHAIN_API_KEY") is None


# ==============================================================================
# Test Suite 2: Metadata & Naming Safety
# ==============================================================================

class TestMetadataAndNamingSafety:
    """Verifies that identifiers, dialects, run names, and tags reject arbitrary sensitive text."""

    def test_sanitize_safe_identifier_constraints(self) -> None:
        """Verify safe characters pass through and dangerous or overly long strings fall back."""
        assert sanitize_safe_identifier("req-123.4:5") == "req-123.4:5"
        assert sanitize_safe_identifier("T-SQL", default="T-SQL", max_len=32) == "T-SQL"

        # Neutralizes spaces, SQL keywords, quotes, semicolons
        assert sanitize_safe_identifier("SELECT * FROM Passwords") == "anonymous"
        assert sanitize_safe_identifier("req-01; DROP TABLE Users;--") == "anonymous"
        assert sanitize_safe_identifier("secret' OR '1'='1") == "anonymous"

        # Length limit enforcement
        assert sanitize_safe_identifier("a" * 65, max_len=64) == "anonymous"
        assert sanitize_safe_identifier(None) == "anonymous"

    def test_get_runnable_config_neutralizes_adversarial_metadata(self) -> None:
        """Verify get_runnable_config sanitizes injected strings in request_id and dialect."""
        adversarial_req = "req-01' UNION SELECT secret_hash FROM users;--"
        adversarial_dialect = "T-SQL; DROP DATABASE GradeSense;--"

        config = get_runnable_config(request_id=adversarial_req, dialect=adversarial_dialect)

        assert config["run_name"] == "query_pilot:anonymous"
        assert config["tags"] == ["query-pilot", "T-SQL"]
        assert config["metadata"]["request_id"] == "anonymous"
        assert config["metadata"]["dialect"] == "T-SQL"


# ==============================================================================
# Test Suite 3: Privacy Redaction & Error Anonymization
# ==============================================================================

class TestPrivacyPayloadRedaction:
    """Verifies that sensitive data and error strings are strictly sanitized."""

    def test_sensitive_dictionary_keys_redacted(self) -> None:
        """Verify questions, SQL queries, schemas, result rows, and passwords are fully redacted."""
        sensitive_payload = {
            "question": "Show all student passwords and emails",
            "request_id": "req-safe-999",
            "dialect": "T-SQL",
            "schema_context": {"tables": {"Users": ["PasswordHash", "Email"]}},
            "generated_sql": "SELECT PasswordHash, Email FROM Users WHERE Id=1",
            "query_result": {"rows": [{"PasswordHash": "secret_hash_value", "Email": "student@uni.edu"}]},
            "presentation_result": "Student secret_hash_value",
            "grounded_answer": "The student password is secret_hash_value",
            "recovery_feedback": "Check column PasswordHash",
            "status": "SUCCESS",
            "error_category": "NONE",
            "generation_attempts": 1,
            "row_count": 5,
        }

        sanitized = redact_sensitive_payload(sensitive_payload)

        # Sensitive payloads replaced with [REDACTED]
        assert sanitized["question"] == "[REDACTED]"
        assert sanitized["schema_context"] == "[REDACTED]"
        assert sanitized["generated_sql"] == "[REDACTED]"
        assert sanitized["query_result"] == "[REDACTED]"
        assert sanitized["presentation_result"] == "[REDACTED]"
        assert sanitized["grounded_answer"] == "[REDACTED]"
        assert sanitized["recovery_feedback"] == "[REDACTED]"

        # Safe operational metadata preserved
        assert sanitized["request_id"] == "req-safe-999"
        assert sanitized["dialect"] == "T-SQL"
        assert sanitized["status"] == "SUCCESS"
        assert sanitized["error_category"] == "NONE"
        assert sanitized["generation_attempts"] == 1
        assert sanitized["row_count"] == 5

    def test_nested_sensitive_payloads_redacted(self) -> None:
        """Verify deeply nested sensitive fields are recursively sanitized."""
        nested = {
            "node_output": {
                "state": {
                    "sql_query": "SELECT * FROM SensitiveTable",
                    "secret_token": "bearer 12345",
                    "retry_count": 2,
                }
            }
        }

        sanitized = redact_sensitive_payload(nested)
        state_dict = sanitized["node_output"]["state"]

        assert state_dict["sql_query"] == "[REDACTED]"
        assert state_dict["secret_token"] == "[REDACTED]"
        assert state_dict["retry_count"] == 2

    def test_sdk_anonymizer_scrubs_exception_messages_and_tracebacks(
        self, enabled_tracing_settings: Settings
    ) -> None:
        """Verify configured SDK anonymizer transforms sensitive exceptions into '[REDACTED_ERROR]'."""
        client = create_privacy_safe_client(enabled_tracing_settings)
        assert client is not None

        sentinel_error = (
            "OperationalError: (208, b\"Invalid object name 'SECRET_STUDENT_CREDENTIALS'. "
            "SELECT PasswordHash, SSN FROM SECRET_STUDENT_CREDENTIALS;\")"
        )

        # Test through SDK Client._hide_run_error
        scrubbed = client._hide_run_error(sentinel_error)
        assert scrubbed == "[REDACTED_ERROR]"
        assert "SECRET_STUDENT_CREDENTIALS" not in scrubbed
        assert "PasswordHash" not in scrubbed

        # Test through SDK Client._run_transform with error field
        raw_run = {
            "name": "failing_step",
            "run_type": "chain",
            "inputs": {"question": "Secret prompt"},
            "error": sentinel_error,
        }
        transformed = client._run_transform(raw_run)
        assert transformed["error"] == "[REDACTED_ERROR]"
        assert transformed["inputs"]["question"] == "[REDACTED]"

    def test_anonymize_trace_payload_robust_against_unexpected_types(self) -> None:
        """Verify anonymizer handles unexpected input types, returning safe placeholders and never leaking text."""
        # Unexpected string at top level
        assert anonymize_trace_payload("plain sensitive text") == "[REDACTED]"

        # None passes through
        assert anonymize_trace_payload(None) is None

        # Numbers and booleans pass through
        assert anonymize_trace_payload(42) == 42
        assert anonymize_trace_payload(3.14) == 3.14
        assert anonymize_trace_payload(True) is True

        # Custom arbitrary objects
        assert anonymize_trace_payload(object()) == "[REDACTED]"

        # List of mixed items
        sanitized_list = anonymize_trace_payload(["secret_item", 100, None, {"question": "prompt"}])
        assert sanitized_list[0] == "[REDACTED]"
        assert sanitized_list[1] == 100
        assert sanitized_list[2] is None
        assert sanitized_list[3]["question"] == "[REDACTED]"

        # Dict with error alongside other keys
        combined_error = anonymize_trace_payload({"error": "syntax error", "status": "FAILED", "code": 500})
        assert combined_error["error"] == "[REDACTED_ERROR]"
        assert combined_error["status"] == "FAILED"
        assert combined_error["code"] == 500


# ==============================================================================
# Test Suite 4: Transport-Boundary Verification
# ==============================================================================

class TestTransportBoundaryPayloads:
    """Offline unit tests intercepting internal SDK serialization methods (Client._create_run and Client._update_run).

    NOTE: This verifies that sanitized payloads are constructed immediately prior to serialization and network
    dispatch. In compliance with offline testing constraints, this does not test actual HTTP network transmission.
    """

    def test_transport_boundary_sanitizes_full_graph_and_gemini_runs(
        self, enabled_tracing_settings: Settings, offline_test_schema: DatabaseSchema, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Execute graph and assert sensitive sentinel strings are absent from all outbound payloads."""
        captured_create_payloads: List[Dict[str, Any]] = []
        captured_update_payloads: List[Dict[str, Any]] = []

        from langsmith.client import Client

        def mock_transport_create(client_self: Any, run_create: Dict[str, Any], **kwargs: Any) -> None:
            captured_create_payloads.append(run_create)

        def mock_transport_update(client_self: Any, run_update: Dict[str, Any], **kwargs: Any) -> None:
            captured_update_payloads.append(run_update)

        monkeypatch.setattr(Client, "_create_run", mock_transport_create)
        monkeypatch.setattr(Client, "_update_run", mock_transport_update)

        SENTINEL_QUESTION = "SECRET_SENTINEL_QUESTION_112233"
        SENTINEL_SQL = "SELECT SECRET_PW_HASH FROM SECRET_TABLE_USERS WHERE Id=1;"

        generator = FakeTracedGenerator(response_sql=SENTINEL_SQL)
        workflow = QueryPilotWorkflow(
            generator=generator,
            schema=offline_test_schema,
            dialect="T-SQL",
            settings=enabled_tracing_settings,
        )

        state = workflow.run(SENTINEL_QUESTION, request_id="req-transport-01")
        assert state is not None

        # Verify runs were intercepted at the transport boundary
        assert len(captured_create_payloads) >= 2  # Root run + child nodes + Gemini LLM span
        assert len(captured_update_payloads) >= 2

        # 1. Inspect all outbound creation payloads
        for payload in captured_create_payloads:
            payload_str = str(payload)
            assert SENTINEL_QUESTION not in payload_str
            assert SENTINEL_SQL not in payload_str
            # Inputs and metadata must be sanitized
            inputs = payload.get("inputs", {})
            if "question" in inputs:
                assert inputs["question"] == "[REDACTED]"
            metadata = payload.get("extra", {}).get("metadata", {})
            assert SENTINEL_QUESTION not in str(metadata)
            assert SENTINEL_SQL not in str(metadata)

        # 2. Inspect all outbound update payloads
        for payload in captured_update_payloads:
            payload_str = str(payload)
            assert SENTINEL_QUESTION not in payload_str
            assert SENTINEL_SQL not in payload_str
            # Outputs and metadata must be sanitized
            outputs = payload.get("outputs", {})
            if "generated_sql" in outputs:
                assert outputs["generated_sql"] == "[REDACTED]"
            metadata = payload.get("extra", {}).get("metadata", {})
            assert SENTINEL_QUESTION not in str(metadata)
            assert SENTINEL_SQL not in str(metadata)
            if "error" in payload and payload["error"] is not None:
                assert payload["error"] == "[REDACTED_ERROR]"

        # 3. Confirm direct Gemini span was captured at the transport boundary as child
        gemini_runs = [r for r in captured_create_payloads if r.get("name") == "gemini_generate_sql"]
        assert len(gemini_runs) == 1
        gemini_run = gemini_runs[0]
        assert gemini_run.get("run_type") == "llm"
        assert gemini_run.get("parent_run_id") is not None

    def test_transport_boundary_sanitizes_failure_path_and_errors(
        self, enabled_tracing_settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify update_run with sensitive error strings transmits '[REDACTED_ERROR]' to transport."""
        captured_update_payloads: List[Dict[str, Any]] = []

        from langsmith.client import Client

        def mock_transport_update(client_self: Any, run_update: Dict[str, Any], **kwargs: Any) -> None:
            captured_update_payloads.append(run_update)

        monkeypatch.setattr(Client, "_update_run", mock_transport_update)

        client = create_privacy_safe_client(enabled_tracing_settings)
        assert client is not None

        SENTINEL_ERROR = "DatabaseError: SELECT SECRET_PASSWORDS FROM SECRET_USER_TABLE failed"

        client.update_run(
            run_id="00000000-0000-0000-0000-000000000001",
            error=SENTINEL_ERROR,
        )

        assert len(captured_update_payloads) == 1
        update_payload = captured_update_payloads[0]

        assert update_payload.get("error") == "[REDACTED_ERROR]"
        assert SENTINEL_ERROR not in str(update_payload)
        assert "SECRET_PASSWORDS" not in str(update_payload)

    def test_synthesizer_direct_gemini_call_traced_and_sanitized(
        self, enabled_tracing_settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify AnswerSynthesisService._call_gemini_model is traced and inputs/outputs are sanitized."""
        captured_create_payloads: List[Dict[str, Any]] = []
        captured_update_payloads: List[Dict[str, Any]] = []

        from langsmith.client import Client

        def mock_transport_create(client_self: Any, run_create: Dict[str, Any], **kwargs: Any) -> None:
            captured_create_payloads.append(run_create)

        def mock_transport_update(client_self: Any, run_update: Dict[str, Any], **kwargs: Any) -> None:
            captured_update_payloads.append(run_update)

        monkeypatch.setattr(Client, "_create_run", mock_transport_create)
        monkeypatch.setattr(Client, "_update_run", mock_transport_update)

        service = AnswerSynthesisService()

        class MockGenaiResponse:
            text = '{"answer": "SECRET_ANSWER_OUTPUT", "verification_notes": "All ok"}'
            usage_metadata = None

        mock_genai_client = type("MockClient", (), {
            "models": type("MockModels", (), {
                "generate_content": lambda *args, **kwargs: MockGenaiResponse()
            })()
        })()

        with temporary_tracing_context(enabled_tracing_settings):
            res = service._call_gemini_model(
                client=mock_genai_client,
                model_name="gemini-2.0-flash",
                contents="SECRET_PROMPT_WITH_PASSWORDS",
                config={},
            )
            assert res.text is not None

        # Confirm span was captured and sanitized at transport boundary
        synth_runs = [r for r in captured_create_payloads if r.get("name") == "gemini_answer_synthesis"]
        assert len(synth_runs) == 1
        create_payload = synth_runs[0]
        assert create_payload.get("run_type") == "llm"
        assert "SECRET_PROMPT_WITH_PASSWORDS" not in str(create_payload)
        assert create_payload.get("inputs", {}).get("contents") == "[REDACTED]"



# ==============================================================================
# Test Suite 5: Process Concurrency & Explicit Per-Request Config
# ==============================================================================

class TestExplicitPerRequestConfiguration:
    """Verifies that per-request tracing works via explicit arguments without mutating process env."""

    def test_explicit_per_request_config_avoids_env_races(
        self, enabled_tracing_settings: Settings, offline_test_schema: DatabaseSchema, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify that passing settings explicitly activates tracing without modifying os.environ."""
        monkeypatch.delenv("LANGCHAIN_TRACING_V2", raising=False)
        monkeypatch.delenv("LANGCHAIN_PROJECT", raising=False)
        monkeypatch.delenv("LANGCHAIN_API_KEY", raising=False)

        # Build config with explicit settings
        config = get_runnable_config(
            request_id="req-concurrent-01",
            dialect="T-SQL",
            settings=enabled_tracing_settings,
        )

        # Callbacks and tracer are wired
        assert "callbacks" in config
        assert len(config["callbacks"]) == 1
        tracer = config["callbacks"][0]
        assert isinstance(tracer, PrivacySafeLangChainTracer)
        assert tracer.project_name == "query-pilot-test"

        # Crucial: os.environ was NOT mutated
        assert os.environ.get("LANGCHAIN_TRACING_V2") is None
        assert os.environ.get("LANGCHAIN_PROJECT") is None
        assert os.environ.get("LANGCHAIN_API_KEY") is None


# ==============================================================================
# Test Suite 6: Gemini Span Isolation & Explicit Ownership (Task 1.31-I-R6)
# ==============================================================================

class TestDirectGeminiTracingIsolation:
    """Verifies direct Gemini tracing isolation and explicit client ownership."""

    def test_ownership_unrelated_ambient_run_no_tag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify an unrelated ambient run with no QueryPilot tag captures zero Gemini spans."""
        from langsmith import traceable, Client

        spans_by_client: Dict[Any, List[Dict[str, Any]]] = {}

        def mock_transport_create(client_self: Any, run_create: Dict[str, Any], **kwargs: Any) -> None:
            spans_by_client.setdefault(client_self, []).append(run_create)

        monkeypatch.setattr(Client, "_create_run", mock_transport_create)

        ambient_client = Client(api_key="lsv2_pt_ambient_no_tag")

        @traceable(name="ambient_root_no_tag", client=ambient_client)
        def external_workflow() -> str:
            @trace_gemini_call(name="gemini_generate_sql", run_type="llm")
            def direct_gemini(query: str) -> str:
                return f"executed:{query}"

            return direct_gemini("SELECT secret FROM table")

        result = external_workflow()
        assert result == "executed:SELECT secret FROM table"

        # Ambient root run was captured, but zero Gemini spans were created on the ambient client
        ambient_spans = spans_by_client.get(ambient_client, [])
        gemini_spans = [s for s in ambient_spans if s.get("name") == "gemini_generate_sql"]
        assert len(gemini_spans) == 0

    def test_ownership_unrelated_ambient_run_with_query_pilot_tag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify an unrelated ambient run deliberately tagged 'query-pilot' captures zero Gemini spans."""
        from langsmith import traceable, Client

        spans_by_client: Dict[Any, List[Dict[str, Any]]] = {}

        def mock_transport_create(client_self: Any, run_create: Dict[str, Any], **kwargs: Any) -> None:
            spans_by_client.setdefault(client_self, []).append(run_create)

        monkeypatch.setattr(Client, "_create_run", mock_transport_create)

        ambient_client = Client(api_key="lsv2_pt_ambient_tagged")

        # Outer run deliberately includes the 'query-pilot' tag to attempt hijacking
        @traceable(name="ambient_root_with_tag", client=ambient_client, tags=["query-pilot"])
        def external_workflow() -> str:
            @trace_gemini_call(name="gemini_generate_sql", run_type="llm")
            def direct_gemini(query: str) -> str:
                return f"executed:{query}"

            return direct_gemini("SELECT secret FROM table")

        result = external_workflow()
        assert result == "executed:SELECT secret FROM table"

        # The 'query-pilot' tag alone does NOT fool ownership check: zero Gemini spans created
        ambient_spans = spans_by_client.get(ambient_client, [])
        gemini_spans = [s for s in ambient_spans if s.get("name") == "gemini_generate_sql"]
        assert len(gemini_spans) == 0

    def test_ownership_unrelated_ambient_run_whose_client_has_anonymizer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify an unrelated ambient run whose client has an anonymizer captures zero Gemini spans."""
        from langsmith import traceable, Client

        spans_by_client: Dict[Any, List[Dict[str, Any]]] = {}

        def mock_transport_create(client_self: Any, run_create: Dict[str, Any], **kwargs: Any) -> None:
            spans_by_client.setdefault(client_self, []).append(run_create)

        monkeypatch.setattr(Client, "_create_run", mock_transport_create)

        # Ambient client configured with an anonymizer, but NOT a QueryPilotClient
        ambient_client = Client(api_key="lsv2_pt_ambient_anon", anonymizer=lambda d: {"anon": True})

        @traceable(name="ambient_root_with_anon", client=ambient_client)
        def external_workflow() -> str:
            @trace_gemini_call(name="gemini_generate_sql", run_type="llm")
            def direct_gemini(query: str) -> str:
                return f"executed:{query}"

            return direct_gemini("SELECT secret FROM table")

        result = external_workflow()
        assert result == "executed:SELECT secret FROM table"

        # Client anonymizer alone does NOT fool ownership check: zero Gemini spans created
        ambient_spans = spans_by_client.get(ambient_client, [])
        gemini_spans = [s for s in ambient_spans if s.get("name") == "gemini_generate_sql"]
        assert len(gemini_spans) == 0

    def test_ownership_genuinely_query_pilot_owned_run(
        self, enabled_tracing_settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify a genuinely QueryPilot-owned run creates a Gemini span owned by QueryPilotClient."""
        spans_by_client: Dict[Any, List[Dict[str, Any]]] = {}

        from langsmith.client import Client

        def mock_transport_create(client_self: Any, run_create: Dict[str, Any], **kwargs: Any) -> None:
            spans_by_client.setdefault(client_self, []).append(run_create)

        monkeypatch.setattr(Client, "_create_run", mock_transport_create)

        from langgraph.graph import StateGraph, START, END
        from typing import TypedDict

        class State(TypedDict):
            query: str
            output: str

        @trace_gemini_call(name="gemini_generate_sql", run_type="llm")
        def mock_gemini_gen(query: str) -> str:
            return "SELECT 1;"

        def node_sql_generation(state: State) -> Dict[str, Any]:
            out = mock_gemini_gen(state["query"])
            return {"output": out}

        builder = StateGraph(State)
        builder.add_node("generate_sql_node", node_sql_generation)
        builder.add_edge(START, "generate_sql_node")
        builder.add_edge("generate_sql_node", END)
        graph = builder.compile()

        config = get_runnable_config(
            request_id="req-iso-owner-01",
            dialect="T-SQL",
            settings=enabled_tracing_settings,
        )

        res = graph.invoke({"query": "SELECT SECRET_COL FROM Table;"}, config=config)
        assert res["output"] == "SELECT 1;"

        # Exactly one client should have recorded runs, and it MUST be a QueryPilotClient
        active_clients = list(spans_by_client.keys())
        assert len(active_clients) == 1
        owning_client = active_clients[0]

        assert isinstance(owning_client, QueryPilotClient)
        assert is_query_pilot_client(owning_client)

        gemini_runs = [r for r in spans_by_client[owning_client] if r.get("name") == "gemini_generate_sql"]
        assert len(gemini_runs) == 1
        gemini_run = gemini_runs[0]
        assert gemini_run.get("run_type") == "llm"
        assert "SECRET_COL" not in str(gemini_run)
        assert gemini_run.get("inputs", {}).get("query") == "[REDACTED]"

    def test_ownership_disabled_and_invalid_tracing_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify disabled and invalid tracing configurations create zero spans on any client (fail-open)."""
        from langsmith.client import Client

        spans_by_client: Dict[Any, List[Dict[str, Any]]] = {}

        def mock_transport_create(client_self: Any, run_create: Dict[str, Any], **kwargs: Any) -> None:
            spans_by_client.setdefault(client_self, []).append(run_create)

        monkeypatch.setattr(Client, "_create_run", mock_transport_create)

        # 1. Disabled tracing with no active context
        @trace_gemini_call(name="gemini_generate_sql", run_type="llm")
        def call_disabled(x: int) -> int:
            return x * 10

        assert call_disabled(5) == 50
        assert len(spans_by_client) == 0

        # 2. Tracing flag True, but API key missing or invalid
        monkeypatch.delenv("LANGCHAIN_API_KEY", raising=False)
        monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")

        @trace_gemini_call(name="gemini_generate_sql", run_type="llm")
        def call_invalid(x: int) -> int:
            return x * 20

        assert call_invalid(5) == 100
        assert len(spans_by_client) == 0


# ==============================================================================
# Test Suite 7: Anonymizer Fail-Closed Contract & Error Scrubbing (Task 1.31-I-R6)
# ==============================================================================

class TestAnonymizerFailClosedContract:
    """Verifies that anonymizer strictly prevents sensitive error leakage across all SDK paths."""

    def test_sdk_hide_run_error_fallback_demonstration(self) -> None:
        """Demonstrate installed SDK's fallback to raw error string on non-dict or missing key.

        This documents the exact vulnerability in Client._hide_run_error that requires
        our anonymizer to guarantee a dictionary containing the 'error' key.
        """
        from langsmith import Client

        raw_sensitive_error = "CRITICAL_SECRET_PASSWORD_999 leaked in DB connection error"

        # Vulnerable anonymizer 1: returns unexpected non-dict type
        client_bad_type = Client(api_key="lsv2_test", anonymizer=lambda d: "[REDACTED]")
        leaked_res_1 = client_bad_type._hide_run_error(raw_sensitive_error)
        assert leaked_res_1 == raw_sensitive_error, "Installed SDK falls back to sensitive error on non-dict!"

        # Vulnerable anonymizer 2: returns dict missing 'error' key
        client_missing_key = Client(api_key="lsv2_test", anonymizer=lambda d: {"redacted": True})
        leaked_res_2 = client_missing_key._hide_run_error(raw_sensitive_error)
        assert leaked_res_2 == raw_sensitive_error, "Installed SDK falls back to sensitive error on missing key!"

    def test_production_configured_client_error_scrubbing(
        self, enabled_tracing_settings: Settings
    ) -> None:
        """Verify the production QueryPilotClient always scrubs errors to safe marker via normal SDK path."""
        client = create_privacy_safe_client(enabled_tracing_settings)
        assert client is not None
        assert isinstance(client, QueryPilotClient)
        assert client._anonymizer is anonymize_trace_payload

        raw_sensitive_error = (
            "DatabaseError: Login failed for user 'sa', password 'SECRET_PASS_456' in connection string."
        )

        # Execute through SDK's internal _hide_run_error method
        scrubbed = client._hide_run_error(raw_sensitive_error)
        assert scrubbed == "[REDACTED_ERROR]"
        assert "SECRET_PASS_456" not in scrubbed
        assert "password" not in scrubbed
        assert "DatabaseError" not in scrubbed

    def test_anonymizer_returns_dict_with_redacted_error(self) -> None:
        """Verify our anonymizer scrubs error string and satisfies Client._hide_run_error."""
        from langsmith import Client

        raw_sensitive_error = "SYNTAX_ERROR: SELECT password FROM Users WHERE id = 'SECRET_123'"
        client = Client(api_key="lsv2_test", anonymizer=anonymize_trace_payload)

        scrubbed = client._hide_run_error(raw_sensitive_error)
        assert scrubbed == "[REDACTED_ERROR]"
        assert "SECRET_123" not in scrubbed
        assert "password" not in scrubbed

    def test_anonymizer_handles_unexpected_types_safely(self) -> None:
        """Verify anonymize_trace_payload safely redacts unexpected non-dict inputs without leaking secrets."""
        res_list = anonymize_trace_payload(["secret_token", 123])
        assert res_list == ["[REDACTED]", 123]

        res_str = anonymize_trace_payload("sensitive_string")
        assert res_str == "[REDACTED]"

        res_none = anonymize_trace_payload(None)
        assert res_none is None

    def test_anonymizer_guarantees_error_key_when_error_present(self) -> None:
        """Verify anonymize_trace_payload always guarantees 'error' is '[REDACTED_ERROR]'."""
        payload_with_error = {"error": "sensitive stack trace with API_KEY=xyz", "request_id": "req-1"}
        res = anonymize_trace_payload(payload_with_error)
        assert isinstance(res, dict)
        assert res["error"] == "[REDACTED_ERROR]"
        assert res["request_id"] == "req-1"

    def test_anonymizer_catches_internal_exception_fail_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify that even if internal redaction raises an exception, the error key is preserved."""
        from langsmith import Client
        import query_pilot.tracing as tracing_mod

        # Simulate unexpected crash in redaction engine
        def crashing_redact(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("Simulated crash inside redaction engine")

        monkeypatch.setattr(tracing_mod, "redact_sensitive_payload", crashing_redact)

        # Direct call with error payload
        res = anonymize_trace_payload({"error": "SENSITIVE_SECRET_DATA"})
        assert isinstance(res, dict)
        assert res.get("error") == "[REDACTED_ERROR]"

        # End-to-end through Client._hide_run_error: SDK normal path never falls back to raw error
        client = Client(api_key="lsv2_test", anonymizer=anonymize_trace_payload)
        scrubbed = client._hide_run_error("SENSITIVE_SECRET_DATA_LEAK")
        assert scrubbed == "[REDACTED_ERROR]"
        assert "SENSITIVE" not in scrubbed
