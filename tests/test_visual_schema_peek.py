"""Offline test suite for Visual Schema Explorer: Table Peek and Data Dictionary (Phase 4).

Verifies:
1. Table peek endpoint authentication and authorization.
2. Table peek security: table allowlist enforcement, non-existent table 404, unknown profile 404.
3. Sensitive column redaction during table peek.
4. Hard row limit clamp (at most 5 rows).
5. Data dictionary generation: heuristic translation of identifiers, acronyms, PK/FK badges.
6. Data dictionary disk caching and retrieval.
7. AI dictionary generation fallback when offline or without Gemini key.
8. System diagnostics auditing dictionary store.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from fastapi import status
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, text

from query_pilot.agent.app import create_agent_app
from query_pilot.agent.auth import (
    SESSION_COOKIE_NAME,
    BootstrapTokenManager,
    SessionManager,
)
from query_pilot.agent.config import AgentConfig
from query_pilot.agent.dictionary_store import DictionaryStore, humanize_identifier
from query_pilot.agent.engine_manager import AgentEngineManager
from query_pilot.agent.history_store import HistoryStore
from query_pilot.db.factory import ProfileConnectionFactory
from query_pilot.db.models import ColumnMetadata, DatabaseSchema, ForeignKeyMetadata, TableMetadata
from query_pilot.db.profile import ConnectionProfile, ProfileStore


@pytest.fixture
def test_sqlite_engine(tmp_path: Path):
    """Provides a SQLite engine with sample tables for peek and dictionary tests."""
    db_file = tmp_path / "test_peek.db"
    engine = create_engine(f"sqlite:///{db_file.as_posix()}", echo=False)
    with engine.begin() as conn:
        conn.execute(
            text(
                """
            CREATE TABLE customers (
                customer_id INTEGER PRIMARY KEY,
                customer_name TEXT NOT NULL,
                secret_password TEXT,
                is_actv_flg INTEGER DEFAULT 1
            );
            """
            )
        )
        conn.execute(
            text(
                """
            CREATE TABLE orders (
                order_id INTEGER PRIMARY KEY,
                cst_fk INTEGER,
                tot_amt REAL,
                order_dt TEXT,
                FOREIGN KEY (cst_fk) REFERENCES customers(customer_id)
            );
            """
            )
        )
        conn.execute(
            text(
                """
            CREATE TABLE restricted_table (
                id INTEGER PRIMARY KEY,
                internal_notes TEXT
            );
            """
            )
        )
        # Insert 10 rows into customers to test the 5-row clamp
        for i in range(1, 11):
            conn.execute(
                text(
                    "INSERT INTO customers (customer_id, customer_name, secret_password, is_actv_flg) "
                    "VALUES (:id, :name, :pwd, 1)"
                ),
                {"id": i, "name": f"Customer {i}", "pwd": f"secret_{i}"},
            )
        for i in range(1, 6):
            conn.execute(
                text(
                    "INSERT INTO orders (order_id, cst_fk, tot_amt, order_dt) "
                    "VALUES (:id, :cid, :amt, '2026-11-20')"
                ),
                {"id": i, "cid": i, "amt": float(i * 100)},
            )
    return engine


@pytest.fixture
def peek_test_setup(tmp_path: Path, test_sqlite_engine):
    """Sets up an isolated FastAPI test client with SQLite profile and store."""
    token_dir = tmp_path / "tokens"
    token_dir.mkdir(parents=True, exist_ok=True)
    dict_dir = tmp_path / "dictionaries"
    dict_dir.mkdir(parents=True, exist_ok=True)
    history_file = tmp_path / "history.json"

    profile = ConnectionProfile(
        profile_id="test_ecommerce",
        display_name="Test E-Commerce Store",
        engine_type="sqlite",
        database_name="main",
        database_path=str(tmp_path / "test_peek.db"),
        allowed_tables=["customers", "orders", "ghost_table"],  # Note: restricted_table is excluded
        sensitive_columns=["secret_password"],
        timeout_seconds=5,
        max_row_limit=50,
    )
    store = ProfileStore(profiles={"test_ecommerce": profile})

    config = AgentConfig(
        token_dir=token_dir,
        dictionaries_dir=dict_dir,
        history_file=history_file,
    )
    token_mgr = BootstrapTokenManager(token_dir=token_dir)
    token = token_mgr.adopt_or_initialize_token()
    session_mgr = SessionManager()
    dict_store = DictionaryStore(dictionaries_dir=dict_dir)
    hist_store = HistoryStore(history_file=history_file)

    test_sqlite_engine.dispose()

    app = create_agent_app(
        config=config,
        store=store,
        token_manager=token_mgr,
        session_manager=session_mgr,
        dictionary_store=dict_store,
        history_store=hist_store,
    )
    client = TestClient(app, base_url="http://127.0.0.1:8001")

    # Bootstrap login to obtain session cookie
    resp = client.post(
        "/agent/v1/auth/bootstrap",
        headers={"X-QueryPilot-Request": "1"},
        json={"bootstrap_token": token},
    )
    assert resp.status_code == status.HTTP_200_OK
    session_id = resp.cookies.get(SESSION_COOKIE_NAME)
    assert session_id is not None

    return {
        "client": client,
        "session_id": session_id,
        "profile_id": "test_ecommerce",
        "dict_store": dict_store,
        "dict_dir": dict_dir,
    }


def test_humanize_identifier():
    """Verify acronym and camel/snake case translation."""
    assert humanize_identifier("cst_id") == "Customer Identifier"
    assert humanize_identifier("is_actv_flg") == "Is Active Flag"
    assert humanize_identifier("tot_amt") == "Total Amount"
    assert humanize_identifier("order_dt") == "Order Date"
    assert humanize_identifier("customerName") == "Customer Name"


def test_peek_table_requires_authentication(peek_test_setup):
    """GET /agent/v1/profiles/{id}/tables/{table}/peek requires ambient session."""
    client = peek_test_setup["client"]
    client.cookies.clear()
    resp = client.get("/agent/v1/profiles/test_ecommerce/tables/customers/peek")
    assert resp.status_code == status.HTTP_401_UNAUTHORIZED
    # Restore cookie for subsequent calls if needed
    client.cookies.set(SESSION_COOKIE_NAME, peek_test_setup["session_id"])


def test_peek_table_unknown_profile_returns_404(peek_test_setup):
    """Unknown profile fails closed with 404."""
    client = peek_test_setup["client"]
    cookies = {SESSION_COOKIE_NAME: peek_test_setup["session_id"]}
    resp = client.get("/agent/v1/profiles/non_existent_profile/tables/customers/peek", cookies=cookies)
    assert resp.status_code == status.HTTP_404_NOT_FOUND


def test_peek_table_allowlist_enforcement(peek_test_setup):
    """Accessing table not in profile allowlist fails closed with 403."""
    client = peek_test_setup["client"]
    cookies = {SESSION_COOKIE_NAME: peek_test_setup["session_id"]}
    resp = client.get("/agent/v1/profiles/test_ecommerce/tables/restricted_table/peek", cookies=cookies)
    assert resp.status_code == status.HTTP_403_FORBIDDEN
    assert "not in the approved table allowlist" in resp.json()["detail"]


def test_peek_table_non_existent_table_returns_404(peek_test_setup):
    """Accessing non-existent table returns 404."""
    client = peek_test_setup["client"]
    cookies = {SESSION_COOKIE_NAME: peek_test_setup["session_id"]}
    # 'ghost_table' is in allowlist but not present in database schema
    resp = client.get("/agent/v1/profiles/test_ecommerce/tables/ghost_table/peek", cookies=cookies)
    assert resp.status_code == status.HTTP_404_NOT_FOUND


def test_peek_table_success_clamped_to_5_rows_and_redacts_sensitive(peek_test_setup):
    """Verifies that peek returns at most 5 rows and redacts sensitive columns."""
    client = peek_test_setup["client"]
    cookies = {SESSION_COOKIE_NAME: peek_test_setup["session_id"]}
    resp = client.get(
        "/agent/v1/profiles/test_ecommerce/tables/customers/peek?limit=100",
        cookies=cookies,
    )
    if resp.status_code != 200:
        print("PEEK ERROR JSON:", resp.json())
    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert data["profile_id"] == "test_ecommerce"
    assert data["table_name"] == "customers"
    assert "columns" in data
    assert "rows" in data
    assert len(data["rows"]) == 5  # Strictly clamped to 5
    assert data["row_count"] == 5

    # Check sensitive column exclusion and redaction:
    # Denylisted sensitive column 'secret_password' was never exposed in columns or rows
    assert "secret_password" not in data["columns"]
    for row in data["rows"]:
        assert "secret_password" not in row


def test_get_data_dictionary_heuristic(peek_test_setup):
    """GET /agent/v1/profiles/{id}/data-dictionary derives and persists heuristic dictionary."""
    client = peek_test_setup["client"]
    cookies = {SESSION_COOKIE_NAME: peek_test_setup["session_id"]}

    resp = client.get("/agent/v1/profiles/test_ecommerce/data-dictionary", cookies=cookies)
    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert data["profile_id"] == "test_ecommerce"
    assert data["source"] == "deterministic_heuristic"
    assert "tables" in data
    assert "customers" in data["tables"]
    assert "orders" in data["tables"]

    # Verify table attributes
    cust_table = data["tables"]["customers"]
    assert cust_table["business_name"] == "Customers"
    assert len(cust_table["suggested_questions"]) >= 2
    assert "customer_id" in cust_table["columns"]
    assert cust_table["columns"]["customer_id"]["is_primary_key"] is True

    # Verify FK attribution on orders
    orders_table = data["tables"]["orders"]
    assert "cst_fk" in orders_table["columns"]
    # Check that disk cache was created
    dict_store: DictionaryStore = peek_test_setup["dict_store"]
    cached = dict_store.get_dictionary("test_ecommerce")
    assert cached is not None
    assert cached.profile_id == "test_ecommerce"


def test_generate_data_dictionary_offline_fallback(peek_test_setup):
    """POST /agent/v1/profiles/{id}/data-dictionary/generate returns fallback gracefully."""
    client = peek_test_setup["client"]
    cookies = {SESSION_COOKIE_NAME: peek_test_setup["session_id"]}

    resp = client.post(
        "/agent/v1/profiles/test_ecommerce/data-dictionary/generate",
        headers={"X-QueryPilot-Request": "1"},
        cookies=cookies,
    )
    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert data["profile_id"] == "test_ecommerce"
    assert "tables" in data


def test_diagnostics_audit_includes_dictionary_store(peek_test_setup):
    """GET /agent/v1/system-diagnostics audits dictionary store health."""
    client = peek_test_setup["client"]
    cookies = {SESSION_COOKIE_NAME: peek_test_setup["session_id"]}

    resp = client.get("/agent/v1/system-diagnostics", cookies=cookies)
    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert "dictionary_store" in data
    assert data["dictionary_store"]["healthy"] is True
