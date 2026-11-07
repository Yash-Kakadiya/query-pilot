"""
Milestone B29-B.2: Backend Default-Profile Exposure API Tests.

Verifies:
1. Designated default profile is returned with `is_default: True`.
2. Other profiles return `is_default: False`.
3. Designation survives profile-store reload / persistence round-trip.
4. When no default is configured, every profile returns `is_default: False`.
5. Removing the designated default does not produce an incorrect default marker.
6. Existing authentication and safe redacted profile-list behavior remain intact.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict
from unittest.mock import MagicMock

from fastapi import status
from fastapi.testclient import TestClient
import pytest
from sqlalchemy.engine import Engine

from query_pilot.agent.app import create_agent_app
from query_pilot.agent.auth import (
    SESSION_COOKIE_NAME,
    BootstrapTokenManager,
    SessionManager,
)
from query_pilot.agent.config import AgentConfig
from query_pilot.agent.engine_manager import AgentEngineManager
from query_pilot.db.factory import ProfileConnectionFactory
from query_pilot.db.profile import (
    ConnectionProfile,
    ProfileStore,
    load_trusted_profiles_file,
    save_trusted_profiles_file,
)


def _make_profile(pid: str, name: str) -> ConnectionProfile:
    return ConnectionProfile(
        profile_id=pid,
        display_name=name,
        server="localhost",
        database_name=f"{pid}_db",
        schema_name="dbo",
        driver="ODBC Driver 17 for SQL Server",
        engine_type="mssql",
        use_trusted_connection=True,
        timeout_seconds=15,
        max_row_limit=100,
    )


def _create_test_client(store: ProfileStore, token_dir: Path):
    config = AgentConfig(
        bind_host="127.0.0.1",
        bind_port=8001,
        allow_dev_origin=False,
        token_dir=token_dir,
        token_filename="agent.token",
    )
    factory = ProfileConnectionFactory(store=store)
    engine_manager = AgentEngineManager(factory=factory)
    token_manager = BootstrapTokenManager(token_dir=token_dir, token_filename="agent.token")
    session_manager = SessionManager(idle_timeout_seconds=3600, max_lifetime_seconds=43200)

    bootstrap_token = token_manager.initialize_token()

    app = create_agent_app(
        config=config,
        store=store,
        factory=factory,
        engine_manager=engine_manager,
        token_manager=token_manager,
        session_manager=session_manager,
        caller_engine=MagicMock(spec=Engine),
    )

    client = TestClient(
        app,
        base_url="http://127.0.0.1:8001",
        headers={"Host": "127.0.0.1:8001"},
    )

    # Perform bootstrap authentication
    login_resp = client.post(
        "/agent/v1/auth/bootstrap",
        headers={"X-QueryPilot-Request": "1"},
        json={"bootstrap_token": bootstrap_token},
    )
    assert login_resp.status_code == status.HTTP_200_OK
    session_id = login_resp.cookies.get(SESSION_COOKIE_NAME)
    assert session_id is not None

    return client, session_id


def test_designated_default_profile_exposed_as_true(tmp_path: Path):
    """GET /agent/v1/profiles returns is_default=True for designated profile and False for others."""
    p1 = _make_profile("profile_1", "Profile 1 (First)")
    p2 = _make_profile("profile_2", "Profile 2 (Default)")
    p3 = _make_profile("profile_3", "Profile 3 (Third)")

    store = ProfileStore(
        profiles={"profile_1": p1, "profile_2": p2, "profile_3": p3},
        default_profile_id="profile_2",
    )

    client, session_id = _create_test_client(store, tmp_path)

    resp = client.get(
        "/agent/v1/profiles",
        cookies={SESSION_COOKIE_NAME: session_id},
    )
    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert "profiles" in data
    profiles = data["profiles"]
    assert len(profiles) == 3

    # Exactly profile_2 must report is_default=True
    by_id = {p["profile_id"]: p for p in profiles}
    assert by_id["profile_2"]["is_default"] is True
    assert by_id["profile_1"]["is_default"] is False
    assert by_id["profile_3"]["is_default"] is False


def test_no_configured_default_all_profiles_return_false(tmp_path: Path):
    """When no default profile is configured in the store, all profiles report is_default=False."""
    p1 = _make_profile("prof_a", "Profile A")
    p2 = _make_profile("prof_b", "Profile B")

    store = ProfileStore(
        profiles={"prof_a": p1, "prof_b": p2},
        default_profile_id=None,
    )

    client, session_id = _create_test_client(store, tmp_path)

    resp = client.get(
        "/agent/v1/profiles",
        cookies={SESSION_COOKIE_NAME: session_id},
    )
    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert len(data["profiles"]) == 2
    for p in data["profiles"]:
        assert p["is_default"] is False


def test_default_designation_survives_store_reload(tmp_path: Path):
    """The default profile designation persists through save and reload to disk."""
    profiles_file = tmp_path / "profiles.json"
    p1 = _make_profile("profile_x", "Profile X")
    p2 = _make_profile("profile_y", "Profile Y (Persisted Default)")

    initial_store = ProfileStore(
        profiles={"profile_x": p1, "profile_y": p2},
        default_profile_id="profile_y",
    )
    save_trusted_profiles_file(profiles_file, initial_store)

    # Reload store from disk
    reloaded_store = load_trusted_profiles_file(profiles_file)
    assert reloaded_store.default_profile_id == "profile_y"

    client, session_id = _create_test_client(reloaded_store, tmp_path)

    resp = client.get(
        "/agent/v1/profiles",
        cookies={SESSION_COOKIE_NAME: session_id},
    )
    assert resp.status_code == status.HTTP_200_OK
    profiles = resp.json()["profiles"]
    by_id = {p["profile_id"]: p for p in profiles}
    assert by_id["profile_y"]["is_default"] is True
    assert by_id["profile_x"]["is_default"] is False


def test_removed_default_profile_does_not_mark_spurious_default(tmp_path: Path):
    """If the default profile is deleted or not in profiles, no profile is marked default."""
    p1 = _make_profile("profile_remain", "Remaining Profile")

    # Store has default_profile_id set to a deleted profile ID
    store = ProfileStore(
        profiles={"profile_remain": p1},
    )
    # Manually point default to a nonexistent profile
    store._default_profile_id = "deleted_profile_id"

    client, session_id = _create_test_client(store, tmp_path)

    resp = client.get(
        "/agent/v1/profiles",
        cookies={SESSION_COOKIE_NAME: session_id},
    )
    assert resp.status_code == status.HTTP_200_OK
    profiles = resp.json()["profiles"]
    assert len(profiles) == 1
    assert profiles[0]["is_default"] is False


def test_unauthenticated_request_fails_with_401(tmp_path: Path):
    """Unauthenticated GET /agent/v1/profiles fails closed with HTTP 401."""
    p1 = _make_profile("p1", "Profile 1")
    store = ProfileStore(profiles={"p1": p1}, default_profile_id="p1")

    client, _ = _create_test_client(store, tmp_path)
    client.cookies.clear()
    resp = client.get("/agent/v1/profiles")  # no session cookie
    assert resp.status_code == status.HTTP_401_UNAUTHORIZED
