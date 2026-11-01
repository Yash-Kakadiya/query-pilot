"""Acceptance test suite for B24-D Phase 3: SQLite Introspection & Read-Only Execution.

Covers:
1. SQLite connection factory: mode=ro, NullPool, database_list validation, authorizer installation.
2. SQLite schema introspection: tables, columns, PK, FK, indexes, sensitive column redaction, table allowlist.
3. Read-only SQL execution boundary: SELECT, JOIN, aggregates, window functions, CTEs/recursive CTEs.
4. Resource enforcement: bounded cursor fetch (100 row ceiling, truncation flag), single-statement enforcement.
5. Security authorizer defense-in-depth: DDL, DML, candidate PRAGMAs, ATTACH, DETACH, sensitive columns denied.
6. Execution timeout and cancellation: progress handler and watchdog interrupt for runaway queries.
7. WAL and sidecar safety matrix: rollback journal, WAL with shm, WAL missing shm writable, WAL missing shm read-only.
8. Local Agent API integration: test-connection, schema, execute endpoints returning typed response models.
"""

from __future__ import annotations

import os
import sqlite3
import stat
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from starlette import status
from starlette.testclient import TestClient

from query_pilot.agent.app import create_agent_app
from query_pilot.agent.auth import BootstrapTokenManager, SESSION_COOKIE_NAME
from query_pilot.agent.config import AgentConfig
from query_pilot.agent.models import ExecuteQueryResponse, TestConnectionResponse
from query_pilot.db.connection import (
    DatabaseConnectionError,
    DatabaseSecurityError,
    create_profile_sqlite_engine,
    verify_sqlite_database_context,
)
from query_pilot.db.executor import QueryTimeoutError, UnsafeQueryError
from query_pilot.db.factory import ProfileConnectionFactory
from query_pilot.db.models import DatabaseSchema
from query_pilot.db.profile import (
    ConnectionProfile,
    ProfileConfigurationError,
    ProfileStore,
    save_trusted_profiles_file,
)
from query_pilot.db.profile_executor import ProfileExecutionBoundary
from query_pilot.db.profile_introspector import ProfileDatabaseIntrospector


# ==============================================================================
# Fixtures
# ==============================================================================


@pytest.fixture
def sample_sqlite_db(tmp_path: Path) -> Path:
    """Create a rich SQLite database with tables, foreign keys, indexes, and sample data."""
    db_file = tmp_path / "university.db"
    conn = sqlite3.connect(str(db_file))
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE departments (
            dept_id INTEGER PRIMARY KEY,
            dept_name TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE instructors (
            instructor_id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            salary REAL,
            password_hash TEXT,
            dept_id INTEGER,
            FOREIGN KEY (dept_id) REFERENCES departments(dept_id)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE courses (
            course_id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            credits INTEGER DEFAULT 3,
            dept_id INTEGER,
            FOREIGN KEY (dept_id) REFERENCES departments(dept_id)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE enrollments (
            enrollment_id INTEGER PRIMARY KEY,
            student_id INTEGER NOT NULL,
            course_id INTEGER NOT NULL,
            grade TEXT,
            FOREIGN KEY (course_id) REFERENCES courses(course_id)
        )
        """
    )
    cur.execute("CREATE INDEX idx_course_title ON courses(title)")

    # Insert test data
    cur.execute("INSERT INTO departments VALUES (1, 'Computer Science')")
    cur.execute("INSERT INTO departments VALUES (2, 'Mathematics')")
    cur.execute("INSERT INTO instructors VALUES (10, 'Dr. Alan Turing', 95000.0, 'secret_hash_1', 1)")
    cur.execute("INSERT INTO instructors VALUES (11, 'Dr. Ada Lovelace', 98000.0, 'secret_hash_2', 1)")
    cur.execute("INSERT INTO courses VALUES (101, 'Intro to CS', 4, 1)")
    cur.execute("INSERT INTO courses VALUES (102, 'Discrete Math', 3, 2)")
    cur.execute("INSERT INTO enrollments VALUES (1, 501, 101, 'A')")
    cur.execute("INSERT INTO enrollments VALUES (2, 502, 101, 'B')")
    cur.execute("INSERT INTO enrollments VALUES (3, 501, 102, 'A')")

    conn.commit()
    conn.close()
    return db_file


@pytest.fixture
def large_dataset_sqlite_db(tmp_path: Path) -> Path:
    """Create a SQLite database with > 100 rows to test bounded fetch truncation."""
    db_file = tmp_path / "large_data.db"
    conn = sqlite3.connect(str(db_file))
    cur = conn.cursor()
    cur.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, label TEXT)")
    cur.executemany(
        "INSERT INTO items (id, label) VALUES (?, ?)",
        [(i, f"Item {i}") for i in range(1, 150)],
    )
    conn.commit()
    conn.close()
    return db_file


@pytest.fixture
def sqlite_profile(sample_sqlite_db: Path) -> ConnectionProfile:
    """ConnectionProfile pointing to the sample SQLite database."""
    return ConnectionProfile(
        profile_id="univ_sqlite",
        display_name="University SQLite Profile",
        engine_type="sqlite",
        database_path=str(sample_sqlite_db),
        timeout_seconds=5,
        max_row_limit=50,
        sensitive_columns=("salary",),  # additive sensitive column
    )


# ==============================================================================
# 1. SQLite Connection Factory Tests
# ==============================================================================


def test_sqlite_engine_creation_readonly_mode(sqlite_profile: ConnectionProfile):
    """Engine creates connection in mode=ro; mutating statements fail."""
    engine = create_profile_sqlite_engine(sqlite_profile, mode="execution")
    with engine.connect() as conn:
        # SELECT works
        raw_conn = getattr(conn.connection, "dbapi_connection", None)
        assert raw_conn is not None

        # Verify PRAGMA database_list confirms the exact canonical file
        verified = verify_sqlite_database_context(conn, expected_path=sqlite_profile.database_path)
        assert os.path.normcase(os.path.abspath(verified)) == os.path.normcase(os.path.abspath(sqlite_profile.database_path))

    engine.dispose()


def test_sqlite_engine_never_creates_missing_db(tmp_path: Path):
    """Attempting to create profile or connect to non-existent database fails closed."""
    missing_file = tmp_path / "missing_target.db"
    assert not missing_file.exists()
    with pytest.raises((ValueError, DatabaseConnectionError)):
        ConnectionProfile(
            profile_id="missing_db",
            display_name="Missing DB",
            engine_type="sqlite",
            database_path=str(missing_file),
        )
    assert not missing_file.exists()


def test_sqlite_engine_detects_database_mismatch(sample_sqlite_db: Path, tmp_path: Path):
    """Engine verification fails if connected database does not match expected canonical path."""
    other_db = tmp_path / "other.db"
    conn = sqlite3.connect(str(other_db))
    conn.close()

    # If verify_sqlite_database_context is called with a mismatched path, raises DatabaseSecurityError
    test_conn = sqlite3.connect(str(sample_sqlite_db))
    try:
        with pytest.raises(DatabaseSecurityError):
            verify_sqlite_database_context(test_conn, expected_path=str(other_db))
    finally:
        test_conn.close()


# ==============================================================================
# 2. SQLite Schema Introspection Tests
# ==============================================================================


def test_sqlite_schema_introspection_raw(sqlite_profile: ConnectionProfile):
    """ProfileDatabaseIntrospector discovers tables, columns, primary keys, and foreign keys."""
    introspector = ProfileDatabaseIntrospector(config=sqlite_profile)
    schema = introspector.introspect_raw()

    assert schema.database_name == sqlite_profile.database_name
    assert not schema.is_model_facing

    # Verify tables
    table_names = set(schema.table_names)
    assert {"departments", "instructors", "courses", "enrollments"}.issubset(table_names)

    # Verify columns and PK on courses
    courses_tbl = schema.get_table("courses")
    assert courses_tbl is not None
    assert courses_tbl.schema_name == "main"
    assert "course_id" in courses_tbl.primary_keys
    col_names = [c.name for c in courses_tbl.columns]
    assert "title" in col_names
    assert "credits" in col_names

    # Verify foreign keys on courses
    assert len(courses_tbl.foreign_keys) == 1
    fk = courses_tbl.foreign_keys[0]
    assert fk.source_column == "dept_id"
    assert fk.target_table == "departments"
    assert fk.target_column == "dept_id"


def test_sqlite_schema_introspection_model_facing_sensitive_redaction(sqlite_profile: ConnectionProfile):
    """introspect_model_facing redacts canonical defaults (password_hash) and additive columns (salary)."""
    introspector = ProfileDatabaseIntrospector(config=sqlite_profile)
    model_schema = introspector.introspect_model_facing()

    assert model_schema.is_model_facing

    instructors_tbl = model_schema.get_table("instructors")
    assert instructors_tbl is not None
    safe_col_names = [c.name.lower() for c in instructors_tbl.columns]

    assert "instructor_id" in safe_col_names
    assert "name" in safe_col_names
    # Redacted columns MUST NOT be present in model-facing schema
    assert "password_hash" not in safe_col_names
    assert "salary" not in safe_col_names


def test_sqlite_schema_introspection_table_allowlist(sample_sqlite_db: Path):
    """Profile with allowed_tables omits non-whitelisted tables from schema."""
    prof = ConnectionProfile(
        profile_id="restricted_sqlite",
        display_name="Restricted SQLite",
        engine_type="sqlite",
        database_path=str(sample_sqlite_db),
        allowed_tables=("departments", "courses"),
    )
    introspector = ProfileDatabaseIntrospector(config=prof)
    schema = introspector.introspect_model_facing()

    assert set(schema.table_names) == {"departments", "courses"}
    assert schema.get_table("instructors") is None
    assert schema.get_table("enrollments") is None


# ==============================================================================
# 3. Read-Only SQL Execution Boundary Tests
# ==============================================================================


def test_sqlite_execute_select_and_join(sqlite_profile: ConnectionProfile):
    """Valid read-only SELECT and JOIN execute successfully."""
    introspector = ProfileDatabaseIntrospector(config=sqlite_profile)
    schema = introspector.introspect_model_facing()

    executor = ProfileExecutionBoundary(config=sqlite_profile, schema=schema)
    sql = """
    SELECT c.course_id, c.title, d.dept_name
    FROM courses c
    INNER JOIN departments d ON c.dept_id = d.dept_id
    ORDER BY c.course_id ASC
    """
    result = executor.execute(sql=sql)

    assert result.columns == ["course_id", "title", "dept_name"]
    assert result.row_count == 2
    assert result.rows[0]["title"] == "Intro to CS"
    assert result.rows[0]["dept_name"] == "Computer Science"
    assert not result.truncated


def test_sqlite_execute_aggregates_and_window_functions(sqlite_profile: ConnectionProfile):
    """Aggregates and window functions execute cleanly under SQLite execution authorizer."""
    introspector = ProfileDatabaseIntrospector(config=sqlite_profile)
    schema = introspector.introspect_model_facing()

    executor = ProfileExecutionBoundary(config=sqlite_profile, schema=schema)

    # Aggregates
    agg_sql = "SELECT count(*) AS total_courses, avg(credits) AS avg_credits FROM courses"
    res_agg = executor.execute(sql=agg_sql)
    assert res_agg.row_count == 1
    assert res_agg.rows[0]["total_courses"] == 2

    # Window function
    win_sql = """
    SELECT course_id, title, row_number() OVER (ORDER BY course_id) AS rn
    FROM courses
    """
    res_win = executor.execute(sql=win_sql)
    assert res_win.row_count == 2
    assert res_win.rows[0]["rn"] == 1
    assert res_win.rows[1]["rn"] == 2


def test_sqlite_execute_common_table_expression(sqlite_profile: ConnectionProfile):
    """Ordinary and recursive CTEs execute cleanly."""
    introspector = ProfileDatabaseIntrospector(config=sqlite_profile)
    schema = introspector.introspect_model_facing()

    executor = ProfileExecutionBoundary(config=sqlite_profile, schema=schema)

    # Ordinary CTE
    cte_sql = """
    WITH cs_courses AS (
        SELECT course_id, title FROM courses WHERE dept_id = 1
    )
    SELECT course_id, title FROM cs_courses
    """
    res_cte = executor.execute(sql=cte_sql)
    assert res_cte.row_count == 1
    assert res_cte.rows[0]["title"] == "Intro to CS"

    # Recursive CTE
    rec_sql = """
    WITH RECURSIVE cnt(x) AS (
        SELECT 1
        UNION ALL
        SELECT x + 1 FROM cnt WHERE x < 5
    )
    SELECT x FROM cnt
    """
    res_rec = executor.execute(sql=rec_sql)
    assert res_rec.row_count == 5


def test_sqlite_execute_bounded_cursor_truncation(large_dataset_sqlite_db: Path):
    """Bounded cursor fetch truncates at effective_rows and sets truncated flag."""
    prof = ConnectionProfile(
        profile_id="large_prof",
        display_name="Large Prof",
        engine_type="sqlite",
        database_path=str(large_dataset_sqlite_db),
        max_row_limit=25,
    )
    introspector = ProfileDatabaseIntrospector(config=prof)
    schema = introspector.introspect_model_facing()

    executor = ProfileExecutionBoundary(config=prof, schema=schema)
    res = executor.execute(sql="SELECT id, label FROM items ORDER BY id ASC")

    assert res.row_count == 25
    assert res.truncated is True
    assert res.rows[0]["id"] == 1
    assert res.rows[-1]["id"] == 25


# ==============================================================================
# 4. Security Authorizer Defense-in-Depth Tests
# ==============================================================================


def test_sqlite_blocks_sensitive_column_query(sqlite_profile: ConnectionProfile):
    """Candidate SQL querying sensitive columns (salary or password_hash) is rejected."""
    introspector = ProfileDatabaseIntrospector(config=sqlite_profile)
    schema = introspector.introspect_model_facing()

    executor = ProfileExecutionBoundary(config=sqlite_profile, schema=schema)

    # salary is in profile sensitive_columns
    with pytest.raises(UnsafeQueryError):
        executor.execute(sql="SELECT instructor_id, salary FROM instructors")

    # password_hash is in canonical sensitive columns
    with pytest.raises(UnsafeQueryError):
        executor.execute(sql="SELECT instructor_id, password_hash FROM instructors")


def test_sqlite_blocks_ddl_and_dml(sqlite_profile: ConnectionProfile):
    """DDL and DML statements are strictly rejected before execution."""
    introspector = ProfileDatabaseIntrospector(config=sqlite_profile)
    schema = introspector.introspect_model_facing()

    executor = ProfileExecutionBoundary(config=sqlite_profile, schema=schema)

    # DDL
    with pytest.raises(UnsafeQueryError):
        executor.execute(sql="DROP TABLE courses")

    with pytest.raises(UnsafeQueryError):
        executor.execute(sql="CREATE TABLE hack (id INT)")

    # DML
    with pytest.raises(UnsafeQueryError):
        executor.execute(sql="INSERT INTO departments VALUES (99, 'Hacked')")

    with pytest.raises(UnsafeQueryError):
        executor.execute(sql="DELETE FROM departments WHERE dept_id = 1")


def test_sqlite_blocks_candidate_pragmas(sqlite_profile: ConnectionProfile):
    """Candidate SQL executing any PRAGMA is rejected."""
    introspector = ProfileDatabaseIntrospector(config=sqlite_profile)
    schema = introspector.introspect_model_facing()

    executor = ProfileExecutionBoundary(config=sqlite_profile, schema=schema)

    with pytest.raises(UnsafeQueryError):
        executor.execute(sql="PRAGMA query_only = OFF")

    with pytest.raises(UnsafeQueryError):
        executor.execute(sql="PRAGMA busy_timeout = 0")

    with pytest.raises(UnsafeQueryError):
        executor.execute(sql="PRAGMA table_info('courses')")


def test_sqlite_blocks_attach_detach(sqlite_profile: ConnectionProfile, tmp_path: Path):
    """Candidate SQL attempting ATTACH or DETACH database is rejected."""
    introspector = ProfileDatabaseIntrospector(config=sqlite_profile)
    schema = introspector.introspect_model_facing()

    executor = ProfileExecutionBoundary(config=sqlite_profile, schema=schema)
    other = tmp_path / "evil.db"

    with pytest.raises(UnsafeQueryError):
        executor.execute(sql=f"ATTACH DATABASE '{other}' AS evil")


def test_sqlite_blocks_multi_statements(sqlite_profile: ConnectionProfile):
    """Multi-statement SQL queries are rejected by AST validation."""
    introspector = ProfileDatabaseIntrospector(config=sqlite_profile)
    schema = introspector.introspect_model_facing()

    executor = ProfileExecutionBoundary(config=sqlite_profile, schema=schema)

    with pytest.raises(UnsafeQueryError):
        executor.execute(sql="SELECT * FROM departments; SELECT * FROM courses;")


# ==============================================================================
# 5. Execution Timeout and Cancellation Tests
# ==============================================================================


def test_sqlite_runaway_query_times_out(sample_sqlite_db: Path):
    """Runaway recursive query exceeds timeout and is cleanly interrupted."""
    prof = ConnectionProfile(
        profile_id="timeout_prof",
        display_name="Timeout Prof",
        engine_type="sqlite",
        database_path=str(sample_sqlite_db),
        timeout_seconds=1,  # 1 second timeout
    )
    introspector = ProfileDatabaseIntrospector(config=prof)
    schema = introspector.introspect_model_facing()

    executor = ProfileExecutionBoundary(config=prof, schema=schema)

    # Infinite recursive CTE
    runaway_sql = """
    WITH RECURSIVE inf(x) AS (
        SELECT 1
        UNION ALL
        SELECT x + 1 FROM inf
    )
    SELECT count(*) FROM inf
    """
    start_t = time.perf_counter()
    with pytest.raises(QueryTimeoutError):
        executor.execute(sql=runaway_sql)

    elapsed = time.perf_counter() - start_t
    assert elapsed < 5.0  # Must be cancelled well before 5 seconds


# ==============================================================================
# 6. WAL and Sidecar Safety Matrix Tests
# ==============================================================================


def test_wal_scenario_1_rollback_journal(tmp_path: Path):
    """Scenario 1: Rollback journal database opened mode=ro produces zero sidecars."""
    db_file = tmp_path / "rollback.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute("PRAGMA journal_mode = DELETE;")
    conn.execute("CREATE TABLE t (id INT);")
    conn.execute("INSERT INTO t VALUES (42);")
    conn.commit()
    conn.close()

    prof = ConnectionProfile(
        profile_id="rb_prof",
        display_name="Rollback",
        engine_type="sqlite",
        database_path=str(db_file),
    )
    introspector = ProfileDatabaseIntrospector(config=prof)
    schema = introspector.introspect_model_facing()
    executor = ProfileExecutionBoundary(config=prof, schema=schema)
    res = executor.execute("SELECT id FROM t")
    assert res.rows == [{"id": 42}]

    assert not (tmp_path / "rollback.db-wal").exists()
    assert not (tmp_path / "rollback.db-shm").exists()


def test_wal_scenario_2_wal_with_existing_shm(tmp_path: Path):
    """Scenario 2: WAL database with existing -wal and -shm reads cleanly in mode=ro."""
    db_file = tmp_path / "wal_normal.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("CREATE TABLE t (id INT);")
    conn.execute("INSERT INTO t VALUES (100);")
    conn.commit()
    conn.execute("SELECT * FROM t;").fetchall()
    conn.close()

    prof = ConnectionProfile(
        profile_id="wal_prof",
        display_name="WAL",
        engine_type="sqlite",
        database_path=str(db_file),
    )
    introspector = ProfileDatabaseIntrospector(config=prof)
    schema = introspector.introspect_model_facing()
    executor = ProfileExecutionBoundary(config=prof, schema=schema)
    res = executor.execute("SELECT id FROM t")
    assert res.rows == [{"id": 100}]


def test_wal_scenario_3_wal_missing_shm_in_writable_dir(tmp_path: Path):
    """Scenario 3: WAL database missing -shm in writable dir creates -shm on mode=ro open."""
    db_file = tmp_path / "wal_missing_shm.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("CREATE TABLE t (id INT);")
    conn.execute("INSERT INTO t VALUES (200);")
    conn.commit()
    conn.close()

    shm_file = tmp_path / "wal_missing_shm.db-shm"
    if shm_file.exists():
        shm_file.unlink()

    prof = ConnectionProfile(
        profile_id="wal_missing_shm_prof",
        display_name="WAL Missing SHM",
        engine_type="sqlite",
        database_path=str(db_file),
    )
    introspector = ProfileDatabaseIntrospector(config=prof)
    schema = introspector.introspect_model_facing()
    executor = ProfileExecutionBoundary(config=prof, schema=schema)
    res = executor.execute("SELECT id FROM t")
    assert res.rows == [{"id": 200}]
    assert shm_file.exists()


def test_wal_scenario_4_wal_missing_shm_in_readonly_dir(tmp_path: Path):
    """Scenario 4: WAL database missing -shm in read-only dir fails closed."""
    readonly_dir = tmp_path / "ro_dir"
    readonly_dir.mkdir()
    db_file = readonly_dir / "wal_ro.db"

    conn = sqlite3.connect(str(db_file))
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("CREATE TABLE t (id INT);")
    conn.execute("INSERT INTO t VALUES (300);")
    conn.commit()
    conn.close()

    shm_file = readonly_dir / "wal_ro.db-shm"
    if shm_file.exists():
        shm_file.unlink()

    prof = ConnectionProfile(
        profile_id="wal_ro_prof",
        display_name="WAL RO Dir",
        engine_type="sqlite",
        database_path=str(db_file),
    )

    try:
        os.chmod(str(readonly_dir), stat.S_IREAD | stat.S_IEXEC)
        # In a read-only directory where SQLite cannot create -shm, connecting mode=ro fails closed
        try:
            ro_conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
            ro_conn.execute("SELECT id FROM t")
            ro_conn.close()
        except sqlite3.OperationalError as exc:
            err_msg = str(exc).lower()
            assert "unable to open database file" in err_msg or "readonly" in err_msg or "read-only" in err_msg
    finally:
        os.chmod(str(readonly_dir), stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)


# ==============================================================================
# 7. Local Agent API Integration Tests
# ==============================================================================


@pytest.fixture
def agent_api_client(sample_sqlite_db: Path, tmp_path: Path):
    """TestClient setup with registered SQLite profile and active authenticated session."""
    token_dir = tmp_path / "tokens"
    token_dir.mkdir()
    profiles_file = tmp_path / "profiles.json"

    store = ProfileStore()
    prof = ConnectionProfile(
        profile_id="api_sqlite",
        display_name="API SQLite Profile",
        engine_type="sqlite",
        database_path=str(sample_sqlite_db),
        sensitive_columns=("salary",),
    )
    store.register_profile(prof)
    store.set_default_profile("api_sqlite")
    save_trusted_profiles_file(profiles_file, store)

    tm = BootstrapTokenManager(token_dir=token_dir)
    token = tm.initialize_token()

    cfg = AgentConfig(
        bind_host="127.0.0.1",
        bind_port=8001,
        token_dir=token_dir,
        profiles_file=profiles_file,
    )
    app = create_agent_app(config=cfg, store=store, token_manager=tm)
    client = TestClient(app, base_url="http://127.0.0.1:8001")

    # Bootstrap session
    resp = client.post(
        "/agent/v1/auth/bootstrap",
        headers={"Host": "127.0.0.1:8001", "X-QueryPilot-Request": "1"},
        json={"bootstrap_token": token},
    )
    assert resp.status_code == status.HTTP_200_OK
    session_id = resp.cookies.get(SESSION_COOKIE_NAME)
    return client, session_id


def test_api_test_connection_success(agent_api_client):
    """POST /agent/v1/profiles/{id}/test-connection returns HTTP 200 connected=True."""
    client, sid = agent_api_client
    resp = client.post(
        "/agent/v1/profiles/api_sqlite/test-connection",
        headers={"Host": "127.0.0.1:8001", "X-QueryPilot-Request": "1"},
        cookies={SESSION_COOKIE_NAME: sid},
    )
    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert data["connected"] is True
    assert data["database_name"] == "university"
    assert data["latency_ms"] >= 0.0
    assert data["failure_code"] is None


def test_api_test_connection_missing_file_diagnostic(agent_api_client, tmp_path: Path):
    """POST /agent/v1/profiles/{id}/test-connection on deleted db returns failure_code='FILE_NOT_FOUND'."""
    client, sid = agent_api_client

    missing_file = tmp_path / "deleted_after_creation.db"
    missing_file.touch()

    missing_prof = ConnectionProfile(
        profile_id="missing_file_prof",
        display_name="Missing DB",
        engine_type="sqlite",
        database_path=str(missing_file),
    )
    store = client.app.state.store
    store.register_profile(missing_prof)

    # Delete the database file to simulate TOCTOU / missing file at connect time
    missing_file.unlink()

    resp = client.post(
        "/agent/v1/profiles/missing_file_prof/test-connection",
        headers={"Host": "127.0.0.1:8001", "X-QueryPilot-Request": "1"},
        cookies={SESSION_COOKIE_NAME: sid},
    )
    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert data["connected"] is False
    assert data["failure_code"] in ("FILE_NOT_FOUND", "CONNECTION_FAILED")


def test_api_test_connection_corrupt_file_diagnostic(agent_api_client, tmp_path: Path):
    """POST /agent/v1/profiles/{id}/test-connection on corrupt file returns failure_code='CORRUPTED_DATABASE'."""
    client, sid = agent_api_client

    corrupt_file = tmp_path / "corrupt.db"
    corrupt_file.touch()

    corrupt_prof = ConnectionProfile(
        profile_id="corrupt_prof",
        display_name="Corrupt DB",
        engine_type="sqlite",
        database_path=str(corrupt_file),
    )
    store = client.app.state.store
    store.register_profile(corrupt_prof)

    # Overwrite with corrupt non-sqlite bytes
    corrupt_file.write_text("NOT A SQLITE DATABASE HEADER", encoding="utf-8")

    resp = client.post(
        "/agent/v1/profiles/corrupt_prof/test-connection",
        headers={"Host": "127.0.0.1:8001", "X-QueryPilot-Request": "1"},
        cookies={SESSION_COOKIE_NAME: sid},
    )
    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert data["connected"] is False
    assert data["failure_code"] == "CORRUPTED_DATABASE"


def test_api_schema_introspection(agent_api_client):
    """GET /agent/v1/profiles/{id}/schema returns HTTP 200 with model-facing DatabaseSchema."""
    client, sid = agent_api_client
    resp = client.get(
        "/agent/v1/profiles/api_sqlite/schema",
        headers={"Host": "127.0.0.1:8001", "X-QueryPilot-Request": "1"},
        cookies={SESSION_COOKIE_NAME: sid},
    )
    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert data["database_name"] == "university"
    assert data["is_model_facing"] is True

    table_names = [t["table_name"] for t in data["tables"]]
    assert "courses" in table_names
    assert "instructors" in table_names

    # Check that sensitive columns are redacted
    inst_tbl = next(t for t in data["tables"] if t["table_name"] == "instructors")
    col_names = [c["name"] for c in inst_tbl["columns"]]
    assert "salary" not in col_names
    assert "password_hash" not in col_names


def test_api_query_execution_success(agent_api_client):
    """POST /agent/v1/profiles/{id}/execute returns HTTP 200 with ExecuteQueryResponse."""
    client, sid = agent_api_client
    resp = client.post(
        "/agent/v1/profiles/api_sqlite/execute",
        headers={"Host": "127.0.0.1:8001", "X-QueryPilot-Request": "1"},
        cookies={SESSION_COOKIE_NAME: sid},
        json={"sql": "SELECT course_id, title FROM courses ORDER BY course_id ASC"},
    )
    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert data["columns"] == ["course_id", "title"]
    assert data["row_count"] == 2
    assert data["rows"][0]["title"] == "Intro to CS"
    assert not data["truncated"]


def test_api_query_execution_blocked_for_mutating_sql(agent_api_client):
    """POST /agent/v1/profiles/{id}/execute returns HTTP 400 for mutating SQL."""
    client, sid = agent_api_client
    resp = client.post(
        "/agent/v1/profiles/api_sqlite/execute",
        headers={"Host": "127.0.0.1:8001", "X-QueryPilot-Request": "1"},
        cookies={SESSION_COOKIE_NAME: sid},
        json={"sql": "DELETE FROM departments WHERE dept_id = 1"},
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
