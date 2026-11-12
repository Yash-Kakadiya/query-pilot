from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi import status
from fastapi.testclient import TestClient
import pytest
from sqlalchemy.engine import Engine

from query_pilot.agent.app import create_agent_app
from query_pilot.agent.auth import BootstrapTokenManager, SessionManager
from query_pilot.agent.config import AgentConfig
from query_pilot.agent.engine_manager import AgentEngineManager
from query_pilot.db.factory import ProfileConnectionFactory
from query_pilot.db.models import ColumnMetadata, DatabaseSchema, TableMetadata
from query_pilot.db.profile import (
    ConnectionProfile,
    ProfileStore,
    save_trusted_profiles_file,
)
from query_pilot.db.profile_executor import QueryResult


@pytest.fixture
def test_setup(tmp_path: Path):
    profiles_file = tmp_path / "profiles.json"
    token_dir = tmp_path / "tokens"
    token_dir.mkdir(parents=True, exist_ok=True)
    db_file = tmp_path / "cached.db"
    db_file.touch()

    store = ProfileStore()
    profile = ConnectionProfile(
        profile_id="cached-profile",
        display_name="Cached Profile",
        engine_type="sqlite",
        database_path=str(db_file),
        database_name="cached_db",
        schema_name="main",
        timeout_seconds=5,
        max_row_limit=50,
    )
    store.register_profile(profile)
    store.set_default_profile("cached-profile")
    save_trusted_profiles_file(profiles_file, store)

    config = AgentConfig(
        bind_host="127.0.0.1",
        bind_port=8001,
        token_dir=token_dir,
        profiles_file=profiles_file,
    )
    token_mgr = BootstrapTokenManager(token_dir=token_dir)
    token_str = token_mgr.adopt_or_initialize_token()
    session_mgr = SessionManager()
    session = session_mgr.create_session()

    factory = ProfileConnectionFactory(store=store)
    engine_mgr = AgentEngineManager(factory=factory)

    app = create_agent_app(
        config=config,
        store=store,
        factory=factory,
        engine_manager=engine_mgr,
        token_manager=token_mgr,
        session_manager=session_mgr,
    )
    client = TestClient(
        app,
        base_url="http://127.0.0.1:8001",
        cookies={"qpt_session": session.session_id},
    )

    return {
        "client": client,
        "app": app,
        "profile": profile,
        "store": store,
        "session": session,
    }


def test_schema_cache_hit_and_execution_reuse(test_setup):
    client = test_setup["client"]
    app = test_setup["app"]

    mock_schema = DatabaseSchema(
        database_name="cached_db",
        tables=[
            TableMetadata(
                table_name="users",
                schema_name="main",
                columns=[
                    ColumnMetadata(name="id", data_type="INTEGER", primary_key=True),
                    ColumnMetadata(name="username", data_type="TEXT"),
                ],
            )
        ],
        is_model_facing=True,
    )

    with patch(
        "query_pilot.agent.app.ProfileDatabaseIntrospector.introspect_model_facing",
        return_value=mock_schema,
    ) as mock_introspect:
        # First request: should call introspect
        res1 = client.get("/agent/v1/profiles/cached-profile/schema")
        assert res1.status_code == status.HTTP_200_OK
        assert mock_introspect.call_count == 1
        assert "cached-profile" in app.state.schema_cache

        # Second request: should hit schema cache, not calling introspect again
        res2 = client.get("/agent/v1/profiles/cached-profile/schema")
        assert res2.status_code == status.HTTP_200_OK
        assert mock_introspect.call_count == 1  # Still 1, served from cache!

        # Third request with refresh=true: should bypass cache
        res3 = client.get("/agent/v1/profiles/cached-profile/schema?refresh=true")
        assert res3.status_code == status.HTTP_200_OK
        assert mock_introspect.call_count == 2

    # Now test query execution: execute should reuse cached schema without calling introspect
    with patch(
        "query_pilot.agent.app.ProfileDatabaseIntrospector.introspect_model_facing"
    ) as mock_introspect_exec, patch(
        "query_pilot.agent.app.ProfileExecutionBoundary.execute"
    ) as mock_execute:
        mock_execute.return_value = QueryResult(
            columns=["id", "username"],
            rows=[{"id": 1, "username": "alice"}],
            row_count=1,
            truncated=False,
            execution_time_ms=10.5,
        )

        res4 = client.post(
            "/agent/v1/profiles/cached-profile/execute",
            json={"sql": "SELECT id, username FROM users;"},
            headers={"X-QueryPilot-Request": "1"},
        )
        assert res4.status_code == status.HTTP_200_OK
        assert res4.json()["row_count"] == 1
        # Introspect should NEVER be called because schema was cached!
        assert mock_introspect_exec.call_count == 0


def test_schema_cache_invalidation_on_profile_delete(test_setup):
    client = test_setup["client"]
    app = test_setup["app"]

    mock_schema = DatabaseSchema(
        database_name="cached_db",
        tables=[],
        is_model_facing=True,
    )
    app.state.schema_cache["cached-profile"] = mock_schema

    # Delete profile
    res = client.delete(
        "/agent/v1/profiles/cached-profile",
        headers={"X-QueryPilot-Request": "1"},
    )
    assert res.status_code == status.HTTP_200_OK

    # Verify cache was invalidated
    assert "cached-profile" not in app.state.schema_cache
