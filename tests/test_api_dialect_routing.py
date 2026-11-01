"""Tests for QueryPilot B24-E: API Dialect Routing.

Verifies:
1. Dialect normalization and validation in request models (tsql, mssql, t-sql, sqlite, sqlite3, rejection of unsupported).
2. Authoritative local agent dialect resolution and forwarding (profile overrides frontend).
3. Stripping of local profile identifiers and secrets from upstream generation requests.
4. Gemini prompt generation and system instruction customization for SQLite vs T-SQL.
5. AST validator dialect enforcement (SQLite LIMIT vs T-SQL TOP, mutation blocking).
6. End-to-end hosted API generation endpoint behavior with dialect propagation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

from fastapi import status
from fastapi.testclient import TestClient
import httpx
import pytest
from pydantic import ValidationError

from query_pilot.agent import create_agent_app
from query_pilot.agent.config import AgentConfig
from query_pilot.api.app import create_app
from query_pilot.api.models import (
    ColumnSchemaContext,
    GenerateSQLRequest,
    GenerateSQLResponse,
    GenerationStatus,
    ModelFacingSchemaContext,
    TableSchemaContext,
)
from query_pilot.db.models import (
    ColumnMetadata,
    DatabaseSchema,
    TableMetadata,
)
from query_pilot.db.profile import ConnectionProfile, ProfileStore
from query_pilot.sql.generation import SQLGenerationRequest, SQLGenerationResponse, SQLGenerationStatus
from query_pilot.sql.prompt import (
    SYSTEM_INSTRUCTION_SQLITE,
    SYSTEM_INSTRUCTION_TSQL,
    build_sql_generation_prompt,
    get_system_instruction,
)
from query_pilot.sql.validator import SQLValidator, normalize_dialect


# ==============================================================================
# Fixtures
# ==============================================================================


@pytest.fixture
def sample_schema() -> DatabaseSchema:
    return DatabaseSchema(
        database_name="TestDB",
        tables=[
            TableMetadata(
                schema_name="main",
                table_name="students",
                columns=[
                    ColumnMetadata(name="id", data_type="int", nullable=False, primary_key=True),
                    ColumnMetadata(name="name", data_type="varchar", nullable=False),
                    ColumnMetadata(name="enrolled_date", data_type="date", nullable=True),
                ],
            ),
        ],
        is_model_facing=True,
    )


@pytest.fixture
def multi_engine_store(tmp_path: Path) -> ProfileStore:
    db_file = tmp_path / "valid.db"
    db_file.touch()

    mssql_prof = ConnectionProfile(
        profile_id="corp_mssql",
        display_name="Corporate MSSQL",
        engine_type="mssql",
        server="127.0.0.1",
        database_name="CorpDW",
        schema_name="dbo",
        use_trusted_connection=True,
    )
    sqlite_prof = ConnectionProfile(
        profile_id="local_sqlite",
        display_name="Local SQLite",
        engine_type="sqlite",
        database_path=str(db_file),
    )
    legacy_prof = ConnectionProfile(
        profile_id="legacy_prof",
        display_name="Legacy Omitted Engine",
        server="127.0.0.1",
        database_name="LegacyDB",
        schema_name="dbo",
        use_trusted_connection=True,
    )
    return ProfileStore(
        profiles={
            "corp_mssql": mssql_prof,
            "local_sqlite": sqlite_prof,
            "legacy_prof": legacy_prof,
        },
        default_profile_id="corp_mssql",
    )


class MockUpstreamDispatched:
    def __init__(self):
        self.dispatched_requests: List[httpx.Request] = []
        self.last_payload: Dict[str, Any] = {}

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.dispatched_requests.append(req)
        try:
            self.last_payload = json.loads(req.content.decode("utf-8"))
        except Exception:
            self.last_payload = {}
        return httpx.Response(
            200,
            json={
                "candidate_sql": "SELECT id, name FROM students LIMIT 10;",
                "is_executed": False,
                "status": "generated",
                "request_id": "req-test-123",
                "execution_disclaimer": "Candidate SQL is unexecuted.",
                "dialect": self.last_payload.get("dialect", "tsql"),
            },
        )


# ==============================================================================
# 1. Dialect Normalization & Request Model Validation
# ==============================================================================


def test_normalize_dialect():
    assert normalize_dialect(None) == "tsql"
    assert normalize_dialect("") == "tsql"
    assert normalize_dialect("tsql") == "tsql"
    assert normalize_dialect("TSQL") == "tsql"
    assert normalize_dialect("t-sql") == "tsql"
    assert normalize_dialect("T-SQL") == "tsql"
    assert normalize_dialect("mssql") == "tsql"
    assert normalize_dialect("MSSQL") == "tsql"
    assert normalize_dialect("sqlite") == "sqlite"
    assert normalize_dialect("SQLite") == "sqlite"
    assert normalize_dialect("sqlite3") == "sqlite"
    assert normalize_dialect("SQLITE3") == "sqlite"

    with pytest.raises(ValueError, match="Unsupported SQL dialect 'postgres'"):
        normalize_dialect("postgres")

    with pytest.raises(ValueError, match="Unsupported SQL dialect 'mysql'"):
        normalize_dialect("mysql")


@pytest.fixture
def model_schema_context() -> ModelFacingSchemaContext:
    return ModelFacingSchemaContext(
        database_name="TestDB",
        tables=[
            TableSchemaContext(
                table_name="students",
                columns=[ColumnSchemaContext(name="id", data_type="int")],
            )
        ],
    )


def test_generate_sql_request_model_dialect_default(model_schema_context):
    req = GenerateSQLRequest(
        question="How many students?",
        schema_context=model_schema_context,
    )
    assert req.dialect == "tsql"


def test_generate_sql_request_model_dialect_normalization(model_schema_context):
    for raw, expected in [
        ("mssql", "tsql"),
        ("t-sql", "tsql"),
        ("tsql", "tsql"),
        ("sqlite", "sqlite"),
        ("sqlite3", "sqlite"),
    ]:
        req = GenerateSQLRequest(
            question="How many students?",
            schema_context=model_schema_context,
            dialect=raw,
        )
        assert req.dialect == expected


def test_generate_sql_request_model_unsupported_dialect_rejected(model_schema_context):
    with pytest.raises(ValidationError) as exc_info:
        GenerateSQLRequest(
            question="How many students?",
            schema_context=model_schema_context,
            dialect="oracle",
        )
    assert "Unsupported SQL dialect 'oracle'" in str(exc_info.value)


# ==============================================================================
# 2. Local Agent Dialect Resolution & Upstream Proxying
# ==============================================================================


def test_agent_proxy_derives_sqlite_dialect_from_profile(multi_engine_store, tmp_path):
    mock = MockUpstreamDispatched()
    config = AgentConfig(
        bind_host="127.0.0.1",
        bind_port=8001,
        allow_dev_origin=False,
        token_dir=tmp_path / "token",
        upstream_api_url="http://127.0.0.1:8000",
    )
    transport = httpx.MockTransport(mock.handler)
    mock_client = httpx.AsyncClient(transport=transport, base_url=config.upstream_api_url)

    app = create_agent_app(config=config, store=multi_engine_store, upstream_client=mock_client)
    client = TestClient(app, base_url="http://127.0.0.1:8001")

    resp = client.post(
        "/api/v1/generate-sql",
        headers={"Host": "127.0.0.1:8001", "Content-Type": "application/json"},
        json={
            "profile_id": "local_sqlite",
            "question": "Show first 10 students",
            "schema_context": {"database_name": "TestDB", "tables": []},
            "database_path": "C:\\secret\\db.sqlite",  # sensitive path should be stripped
        },
    )
    assert resp.status_code == status.HTTP_200_OK
    assert len(mock.dispatched_requests) == 1
    assert mock.last_payload["dialect"] == "sqlite"
    assert "database_path" not in mock.last_payload
    assert "profile_id" not in mock.last_payload


def test_agent_proxy_derives_mssql_dialect_from_profile(multi_engine_store, tmp_path):
    mock = MockUpstreamDispatched()
    config = AgentConfig(
        bind_host="127.0.0.1",
        bind_port=8001,
        allow_dev_origin=False,
        token_dir=tmp_path / "token",
        upstream_api_url="http://127.0.0.1:8000",
    )
    transport = httpx.MockTransport(mock.handler)
    mock_client = httpx.AsyncClient(transport=transport, base_url=config.upstream_api_url)

    app = create_agent_app(config=config, store=multi_engine_store, upstream_client=mock_client)
    client = TestClient(app, base_url="http://127.0.0.1:8001")

    # Frontend claims sqlite, but targeted profile is corp_mssql -> authoritative profile wins
    resp = client.post(
        "/api/v1/generate-sql",
        headers={"Host": "127.0.0.1:8001", "Content-Type": "application/json", "X-Profile-ID": "corp_mssql"},
        json={
            "dialect": "sqlite",
            "question": "Show first 10 students",
            "schema_context": {"database_name": "TestDB", "tables": []},
        },
    )
    assert resp.status_code == status.HTTP_200_OK
    assert len(mock.dispatched_requests) == 1
    # Profile overrides frontend-supplied dialect
    assert mock.last_payload["dialect"] == "tsql"


def test_agent_proxy_legacy_omitted_engine_defaults_to_tsql(multi_engine_store, tmp_path):
    mock = MockUpstreamDispatched()
    config = AgentConfig(
        bind_host="127.0.0.1",
        bind_port=8001,
        allow_dev_origin=False,
        token_dir=tmp_path / "token",
        upstream_api_url="http://127.0.0.1:8000",
    )
    transport = httpx.MockTransport(mock.handler)
    mock_client = httpx.AsyncClient(transport=transport, base_url=config.upstream_api_url)

    app = create_agent_app(config=config, store=multi_engine_store, upstream_client=mock_client)
    client = TestClient(app, base_url="http://127.0.0.1:8001")

    resp = client.post(
        "/api/v1/generate-sql",
        headers={"Host": "127.0.0.1:8001", "Content-Type": "application/json"},
        json={
            "profile_id": "legacy_prof",
            "question": "List students",
            "schema_context": {"database_name": "TestDB", "tables": []},
        },
    )
    assert resp.status_code == status.HTTP_200_OK
    assert mock.last_payload["dialect"] == "tsql"


def test_agent_proxy_unknown_profile_returns_404(multi_engine_store, tmp_path):
    mock = MockUpstreamDispatched()
    config = AgentConfig(
        bind_host="127.0.0.1",
        bind_port=8001,
        allow_dev_origin=False,
        token_dir=tmp_path / "token",
        upstream_api_url="http://127.0.0.1:8000",
    )
    transport = httpx.MockTransport(mock.handler)
    mock_client = httpx.AsyncClient(transport=transport, base_url=config.upstream_api_url)

    app = create_agent_app(config=config, store=multi_engine_store, upstream_client=mock_client)
    client = TestClient(app, base_url="http://127.0.0.1:8001")

    resp = client.post(
        "/api/v1/generate-sql",
        headers={"Host": "127.0.0.1:8001", "Content-Type": "application/json"},
        json={
            "profile_id": "non_existent_profile",
            "question": "List students",
            "schema_context": {"database_name": "TestDB", "tables": []},
        },
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND
    assert len(mock.dispatched_requests) == 0


def test_agent_proxy_unsupported_dialect_returns_422(tmp_path):
    # Empty store, no profile specified
    store = ProfileStore()
    mock = MockUpstreamDispatched()
    config = AgentConfig(
        bind_host="127.0.0.1",
        bind_port=8001,
        allow_dev_origin=False,
        token_dir=tmp_path / "token",
        upstream_api_url="http://127.0.0.1:8000",
    )
    transport = httpx.MockTransport(mock.handler)
    mock_client = httpx.AsyncClient(transport=transport, base_url=config.upstream_api_url)

    app = create_agent_app(config=config, store=store, upstream_client=mock_client)
    client = TestClient(app, base_url="http://127.0.0.1:8001")

    resp = client.post(
        "/api/v1/generate-sql",
        headers={"Host": "127.0.0.1:8001", "Content-Type": "application/json"},
        json={
            "dialect": "cockroachdb",
            "question": "List students",
            "schema_context": {"database_name": "TestDB", "tables": []},
        },
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    assert len(mock.dispatched_requests) == 0


# ==============================================================================
# 3. Prompt Construction & System Instructions (T-SQL vs SQLite)
# ==============================================================================


def test_system_instruction_selection():
    tsql_inst = get_system_instruction("tsql")
    assert "Microsoft SQL Server" in tsql_inst
    assert "T-SQL" in tsql_inst

    sqlite_inst = get_system_instruction("sqlite")
    assert "SQLite" in sqlite_inst
    assert "LIMIT and OFFSET" in sqlite_inst
    assert "never use TOP" in sqlite_inst
    assert "strftime" in sqlite_inst


def test_prompt_construction_sqlite(sample_schema):
    req = SQLGenerationRequest(
        question="Show the first 5 enrolled students",
        schema_context=sample_schema,
        dialect="sqlite",
    )
    prompt = build_sql_generation_prompt(req)
    assert "DATABASE DIALECT: SQLite" in prompt
    assert "valid read-only SQLite query" in prompt


def test_prompt_construction_tsql(sample_schema):
    req = SQLGenerationRequest(
        question="Show the first 5 enrolled students",
        schema_context=sample_schema,
        dialect="tsql",
    )
    prompt = build_sql_generation_prompt(req)
    assert "DATABASE DIALECT: T-SQL" in prompt
    assert "valid read-only T-SQL query" in prompt


# ==============================================================================
# 4. AST Validator Dialect Enforcement
# ==============================================================================


def test_validator_sqlite_allows_limit_and_date_functions(sample_schema):
    validator = SQLValidator(schema=sample_schema, dialect="sqlite")

    # SQLite LIMIT syntax
    valid_sql = "SELECT id, name FROM students ORDER BY id LIMIT 10 OFFSET 5;"
    res = validator.validate(valid_sql)
    assert res.allowed is True
    assert res.normalized_sql is not None

    # SQLite date/strftime syntax
    valid_date_sql = "SELECT id, strftime('%Y', enrolled_date) AS enroll_year FROM students;"
    res_date = validator.validate(valid_date_sql)
    assert res_date.allowed is True


def test_validator_sqlite_rejects_tsql_top_syntax(sample_schema):
    validator = SQLValidator(schema=sample_schema, dialect="sqlite")

    # T-SQL TOP is invalid in SQLite
    tsql_sql = "SELECT TOP 10 id, name FROM students ORDER BY id;"
    res = validator.validate(tsql_sql)
    assert res.allowed is False


def test_validator_tsql_allows_top_syntax(sample_schema):
    validator = SQLValidator(schema=sample_schema, dialect="tsql")

    tsql_sql = "SELECT TOP 10 id, name FROM students ORDER BY id;"
    res = validator.validate(tsql_sql)
    assert res.allowed is True


def test_validator_mutation_blocked_on_both_dialects(sample_schema):
    sqlite_validator = SQLValidator(schema=sample_schema, dialect="sqlite")
    tsql_validator = SQLValidator(schema=sample_schema, dialect="tsql")

    mutations = [
        "DROP TABLE students;",
        "DELETE FROM students WHERE id = 1;",
        "UPDATE students SET name = 'Hacked';",
        "INSERT INTO students (id, name) VALUES (99, 'Bad');",
    ]

    for m in mutations:
        assert sqlite_validator.validate(m).allowed is False
        assert tsql_validator.validate(m).allowed is False


# ==============================================================================
# 5. End-to-End Hosted API Endpoint Dialect Flow
# ==============================================================================


def test_generate_sql_endpoint_sqlite_flow():
    mock_gen = MagicMock()
    mock_gen.generate.return_value = SQLGenerationResponse(
        sql="SELECT id, name FROM students ORDER BY id LIMIT 5;",
        status=SQLGenerationStatus.ANSWERABLE,
        explanation="Retrieves the first 5 students using SQLite LIMIT.",
    )

    hosted_app = create_app(
        runner=MagicMock(),
        sql_generator=mock_gen,
        synthesizer=MagicMock(),
    )
    client = TestClient(hosted_app)

    resp = client.post(
        "/api/v1/generate-sql",
        json={
            "question": "Show first 5 students",
            "schema_context": {
                "database_name": "TestDB",
                "tables": [
                    {
                        "table_name": "students",
                        "schema_name": "main",
                        "columns": [
                            {"name": "id", "data_type": "int"},
                            {"name": "name", "data_type": "varchar"},
                            {"name": "enrolled_date", "data_type": "date"},
                        ],
                    }
                ],
            },
            "dialect": "sqlite",
        },
    )
    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert data["status"] == "generated"
    assert data["dialect"] == "sqlite"
    assert "LIMIT" in data["candidate_sql"]

    # Verify SQLGenerationRequest passed to generator had dialect='sqlite'
    gen_req: SQLGenerationRequest = mock_gen.generate.call_args[0][0]
    assert gen_req.dialect == "sqlite"


def test_generate_sql_endpoint_sqlite_safety_rejection():
    mock_gen = MagicMock()
    mock_gen.generate.return_value = SQLGenerationResponse(
        sql="DROP TABLE students;",
        status=SQLGenerationStatus.ANSWERABLE,
    )

    hosted_app = create_app(
        runner=MagicMock(),
        sql_generator=mock_gen,
        synthesizer=MagicMock(),
    )
    client = TestClient(hosted_app)

    resp = client.post(
        "/api/v1/generate-sql",
        json={
            "question": "Delete table",
            "schema_context": {
                "database_name": "TestDB",
                "tables": [
                    {
                        "table_name": "students",
                        "schema_name": "main",
                        "columns": [
                            {"name": "id", "data_type": "int"},
                            {"name": "name", "data_type": "varchar"},
                        ],
                    }
                ],
            },
            "dialect": "sqlite",
        },
    )
    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert data["status"] == "unsupported"
    assert data["dialect"] == "sqlite"
    assert "Candidate SQL rejected by safety policy" in data["unsupported_reason"]
