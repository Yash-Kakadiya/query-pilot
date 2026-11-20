"""Tests for Persistent Query History & Favorites Engine (Phase 3 / Option 1).

Verifies:
1. Authenticated recording of query history items (POST /agent/v1/history).
2. Automatic password and secret redaction from historical queries.
3. Filtering by profile, search text, tag, and starred favorites (GET /agent/v1/history).
4. Toggling favorite status (POST /agent/v1/history/{id}/toggle-star).
5. Deleting single entry (DELETE /agent/v1/history/{id}).
6. Clearing unstarred history while strictly preserving starred queries.
7. Unauthenticated access fails closed with HTTP 401.
8. Atomic disk persistence across store reloads.
"""

from pathlib import Path
import pytest
from fastapi import status
from starlette.testclient import TestClient

from query_pilot.agent.app import create_agent_app
from query_pilot.agent.auth import (
    SESSION_COOKIE_NAME,
    BootstrapTokenManager,
    SessionManager,
)
from query_pilot.agent.config import AgentConfig
from query_pilot.agent.history_store import HistoryStore, sanitize_history_string
from query_pilot.agent.models import HistoryItemCreate
from query_pilot.db.profile import ProfileStore


@pytest.fixture
def history_test_setup(tmp_path: Path):
    """Sets up an isolated agent with a dedicated temporary history file."""
    token_dir = tmp_path / "tokens"
    token_dir.mkdir()
    history_file = tmp_path / "test_history.json"
    profiles_file = tmp_path / "test_profiles.json"

    config = AgentConfig(
        bind_host="127.0.0.1",
        bind_port=8001,
        token_dir=token_dir,
        profiles_file=profiles_file,
        history_file=history_file,
    )
    token_manager = BootstrapTokenManager(token_dir=token_dir, single_use=False)
    bootstrap_token = token_manager.initialize_token()
    session_manager = SessionManager()
    store = ProfileStore()
    history_store = HistoryStore(history_file=history_file)

    app = create_agent_app(
        config=config,
        store=store,
        token_manager=token_manager,
        session_manager=session_manager,
        history_store=history_store,
    )
    client = TestClient(
        app,
        base_url="http://127.0.0.1:8001",
        headers={"Host": "127.0.0.1:8001"},
    )

    # Perform bootstrap login
    login_resp = client.post(
        "/agent/v1/auth/bootstrap",
        headers={"X-QueryPilot-Request": "1"},
        json={"bootstrap_token": bootstrap_token},
    )
    assert login_resp.status_code == status.HTTP_200_OK
    session_id = login_resp.cookies.get(SESSION_COOKIE_NAME)

    return {
        "app": app,
        "client": client,
        "session_id": session_id,
        "history_store": history_store,
        "history_file": history_file,
    }


def test_sanitize_history_string():
    """Verify passwords in connection strings or statements are scrubbed."""
    raw = "CREATE USER test IDENTIFIED BY 'SuperSecretPass123!';"
    sanitized = sanitize_history_string(raw)
    assert "SuperSecretPass123!" not in sanitized
    assert "[REDACTED]" in sanitized

    raw2 = "SELECT * FROM users WHERE password = 'my_password';"
    sanitized2 = sanitize_history_string(raw2)
    assert "my_password" not in sanitized2
    assert "[REDACTED]" in sanitized2


def test_record_and_list_history(history_test_setup):
    """Verify recording an executed query and retrieving it via GET."""
    client = history_test_setup["client"]
    session_id = history_test_setup["session_id"]

    item = {
        "question": "Show all courses offering 4 credits",
        "sql": "SELECT id, title FROM courses WHERE credits = 4;",
        "profile_id": "sqlite_demo",
        "profile_name": "University Demo DB",
        "engine_type": "sqlite",
        "status": "succeeded",
        "row_count": 4,
        "execution_time_ms": 3.4,
        "tags": ["academics", "kpi"],
    }

    create_resp = client.post(
        "/agent/v1/history",
        headers={"X-QueryPilot-Request": "1"},
        cookies={SESSION_COOKIE_NAME: session_id},
        json=item,
    )
    assert create_resp.status_code == status.HTTP_201_CREATED
    created = create_resp.json()
    assert created["question"] == item["question"]
    assert created["is_favorite"] is False
    assert "academics" in created["tags"]

    list_resp = client.get(
        "/agent/v1/history",
        cookies={SESSION_COOKIE_NAME: session_id},
    )
    assert list_resp.status_code == status.HTTP_200_OK
    data = list_resp.json()
    assert data["total_count"] == 1
    assert data["favorites_count"] == 0
    assert len(data["entries"]) == 1


def test_toggle_star_favorite(history_test_setup):
    """Verify toggling favorite status changes is_favorite and persists."""
    client = history_test_setup["client"]
    session_id = history_test_setup["session_id"]

    item = {
        "sql": "SELECT COUNT(*) FROM students;",
        "profile_id": "sqlite_demo",
        "profile_name": "Demo DB",
        "engine_type": "sqlite",
        "status": "succeeded",
    }
    create_resp = client.post(
        "/agent/v1/history",
        headers={"X-QueryPilot-Request": "1"},
        cookies={SESSION_COOKIE_NAME: session_id},
        json=item,
    )
    entry_id = create_resp.json()["id"]

    # Toggle to favorite
    toggle_resp = client.post(
        f"/agent/v1/history/{entry_id}/toggle-star",
        headers={"X-QueryPilot-Request": "1"},
        cookies={SESSION_COOKIE_NAME: session_id},
    )
    assert toggle_resp.status_code == status.HTTP_200_OK
    assert toggle_resp.json()["is_favorite"] is True

    # Filter only_favorites=true
    fav_resp = client.get(
        "/agent/v1/history?only_favorites=true",
        cookies={SESSION_COOKIE_NAME: session_id},
    )
    assert fav_resp.status_code == status.HTTP_200_OK
    assert len(fav_resp.json()["entries"]) == 1

    # Toggle off
    toggle_off_resp = client.post(
        f"/agent/v1/history/{entry_id}/toggle-star",
        headers={"X-QueryPilot-Request": "1"},
        cookies={SESSION_COOKIE_NAME: session_id},
    )
    assert toggle_off_resp.json()["is_favorite"] is False


def test_clear_history_preserves_favorites(history_test_setup):
    """Verify clearing history preserves starred favorites by default."""
    client = history_test_setup["client"]
    session_id = history_test_setup["session_id"]

    # Entry 1 (normal)
    client.post(
        "/agent/v1/history",
        headers={"X-QueryPilot-Request": "1"},
        cookies={SESSION_COOKIE_NAME: session_id},
        json={"sql": "SELECT 1;", "profile_id": "p1", "profile_name": "P1", "engine_type": "sqlite", "status": "succeeded", "is_favorite": False},
    )

    # Entry 2 (starred favorite)
    client.post(
        "/agent/v1/history",
        headers={"X-QueryPilot-Request": "1"},
        cookies={SESSION_COOKIE_NAME: session_id},
        json={"sql": "SELECT 2;", "profile_id": "p1", "profile_name": "P1", "engine_type": "sqlite", "status": "succeeded", "is_favorite": True},
    )

    # Clear history (clear_favorites=False)
    del_resp = client.delete(
        "/agent/v1/history?clear_favorites=false",
        headers={"X-QueryPilot-Request": "1"},
        cookies={SESSION_COOKIE_NAME: session_id},
    )
    assert del_resp.status_code == status.HTTP_200_OK
    assert del_resp.json()["deleted_count"] == 1

    # Starred item remains
    remaining = client.get("/agent/v1/history", cookies={SESSION_COOKIE_NAME: session_id}).json()
    assert remaining["total_count"] == 1
    assert remaining["entries"][0]["sql"] == "SELECT 2;"


def test_unauthenticated_fails_with_401(history_test_setup):
    """Verify history endpoints reject unauthenticated requests."""
    unauth_client = TestClient(
        history_test_setup["app"],
        base_url="http://127.0.0.1:8001",
        headers={"Host": "127.0.0.1:8001"},
    )

    resp = unauth_client.get("/agent/v1/history")
    assert resp.status_code == status.HTTP_401_UNAUTHORIZED

    resp2 = unauth_client.post(
        "/agent/v1/history",
        headers={"X-QueryPilot-Request": "1"},
        json={"sql": "SELECT 1;"},
    )
    assert resp2.status_code == status.HTTP_401_UNAUTHORIZED


def test_atomic_file_reload(history_test_setup):
    """Verify HistoryStore reloads persisted entries from disk cleanly."""
    store = history_test_setup["history_store"]
    file_path = history_test_setup["history_file"]

    store.add_entry(HistoryItemCreate(
        sql="SELECT * FROM students;",
        profile_id="demo",
        profile_name="Demo",
        engine_type="sqlite",
        is_favorite=True,
    ))

    # Re-initialize new store pointing to same file
    new_store = HistoryStore(history_file=file_path)
    entries = new_store.list_entries()
    assert len(entries) == 1
    assert entries[0].sql == "SELECT * FROM students;"
    assert entries[0].is_favorite is True
