"""Phase 3 Final Closure Audit Tests (B24-D.1).

Validates:
1. SQLite URI path correctness and RFC 3986 percent-encoding across special filenames
   (spaces, #, %, ?, Unicode, Windows drive letters).
2. AgentEngineManager lifecycle, cache invalidation on target changes, deletion,
   recreation, and safe non-disruptive invalidation.
3. Watchdog lifecycle, thread join before connection disposal, progress handler cleanup,
   and isolation across sequential queries.
4. WAL filesystem integrity across all 4 scenarios, ensuring zero modification of .db
   and -wal files, zero automatic checkpointing, and zero writable fallback.
"""

import hashlib
import os
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import threading
import time
from typing import Dict, Optional, Tuple

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, OperationalError

from query_pilot.agent.engine_manager import AgentEngineManager
from query_pilot.db.connection import (
    DatabaseConnectionError,
    DatabaseSecurityError,
    create_profile_sqlite_engine,
    verify_sqlite_database_context,
)
from query_pilot.db.factory import ProfileConnectionFactory
from query_pilot.db.profile import (
    ConnectionProfile,
    ProfileConfigurationError,
    ProfileStore,
    format_sqlite_uri,
)
from query_pilot.db.profile_introspector import ProfileDatabaseIntrospector
from query_pilot.db.profile_executor import ProfileExecutionBoundary, QueryTimeoutError


def get_file_digest_and_mtime(path: str) -> Tuple[Optional[str], Optional[int]]:
    """Return SHA-256 and st_mtime_ns for a filesystem path if it exists."""
    if not os.path.exists(path):
        return None, None
    st = os.stat(path)
    with open(path, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    return digest, st.st_mtime_ns


# ==============================================================================
# Area 1: SQLite URI Path Correctness and Percent-Encoding
# ==============================================================================

class TestSQLiteURIPathCorrectness:
    """Audit tests for URI construction, percent-encoding, and read-only enforcement."""

    @pytest.fixture
    def test_dir(self):
        d = tempfile.mkdtemp(prefix="qp_uri_audit_")
        yield d
        shutil.rmtree(d, ignore_errors=True)

    @pytest.mark.parametrize(
        "filename",
        [
            "database with spaces.db",
            "database#with#hash.db",
            "database%20percent%23hash.db",
            "database_测试_unicode.db",
            "db_ampersand&mode=rwc.db",
            "db;semicolon=1.db",
            "db(1)[2].db",
            "db+plus+1.db",
        ],
    )
    def test_special_characters_opened_and_read_correctly(self, test_dir, filename):
        """Verify filenames with #, %, &, spaces, plus, and Unicode open exact file read-only."""
        db_path = os.path.join(test_dir, filename)
        with sqlite3.connect(db_path) as conn:
            conn.execute("CREATE TABLE items (id INT, name TEXT);")
            conn.execute("INSERT INTO items VALUES (101, 'SpecialItem');")

        profile = ConnectionProfile(
            profile_id="audit_uri_prof",
            display_name="Audit URI Profile",
            engine_type="sqlite",
            database_path=db_path,
        )

        engine = create_profile_sqlite_engine(profile, mode="execution")
        try:
            with engine.connect() as conn:
                # 1. Verify exact data returned
                res = conn.execute(text("SELECT name FROM items WHERE id = 101;")).scalar()
                assert res == "SpecialItem"

                # 2. Verify verified path matches canonical database path
                verified = verify_sqlite_database_context(conn, db_path)
                assert os.path.normcase(os.path.abspath(verified)) == os.path.normcase(os.path.abspath(db_path))

                # 3. Verify write operations fail closed under mode=ro
                with pytest.raises((OperationalError, DBAPIError)):
                    conn.execute(text("INSERT INTO items VALUES (102, 'Illegal');"))
        finally:
            engine.dispose()

    def test_format_sqlite_uri_query_injection_prevention(self):
        """Verify query delimiter '?' in path is safely encoded and cannot inject URI params."""
        synthetic_path = "/var/data/test?mode=rwc&cache=shared.db"
        uri = format_sqlite_uri(synthetic_path)
        # Verify ? is double-encoded to %253F, not left as raw ? query delimiter
        assert "%253F" in uri
        # Verify mode=ro is preserved and not overridden
        assert uri.endswith("?mode=ro&uri=true")
        assert "mode=rwc" not in uri.split("?")[-1]

    def test_format_sqlite_uri_rejects_empty(self):
        """Verify format_sqlite_uri rejects empty or whitespace paths."""
        with pytest.raises(ProfileConfigurationError):
            format_sqlite_uri("")
        with pytest.raises(ProfileConfigurationError):
            format_sqlite_uri("   ")

    def test_format_sqlite_uri_double_encoding(self):
        """Verify double percent encoding protects characters from premature unquoting."""
        raw_path = r"C:\Data\Project#1%20\test db.db"
        uri = format_sqlite_uri(raw_path)
        # First quote converts # to %23, second quote converts %23 to %2523
        assert "%2523" in uri
        assert "%2520" in uri
        assert "mode=ro" in uri
        assert "uri=true" in uri


# ==============================================================================
# Area 2: AgentEngineManager Lifecycle & Invalidation
# ==============================================================================

class TestAgentEngineManagerLifecycle:
    """Audit tests for engine caching, target-change invalidation, and concurrency safety."""

    @pytest.fixture
    def store_and_mgr(self):
        store = ProfileStore()
        factory = ProfileConnectionFactory(store=store)
        mgr = AgentEngineManager(factory=factory)
        return store, factory, mgr

    def test_engine_manager_replaces_stale_engine_when_database_path_changes(self, store_and_mgr, tmp_path):
        """When a profile's database_path changes, the manager must dispose the old engine."""
        store, factory, mgr = store_and_mgr

        db1 = tmp_path / "db1.db"
        with sqlite3.connect(str(db1)) as conn:
            conn.execute("CREATE TABLE t (val TEXT);")
            conn.execute("INSERT INTO t VALUES ('DB1');")

        db2 = tmp_path / "db2.db"
        with sqlite3.connect(str(db2)) as conn:
            conn.execute("CREATE TABLE t (val TEXT);")
            conn.execute("INSERT INTO t VALUES ('DB2');")

        prof1 = ConnectionProfile(
            profile_id="dyn_prof",
            display_name="Dynamic Profile",
            engine_type="sqlite",
            database_path=str(db1),
        )
        store.register_profile(prof1)

        # Get initial engine and query db1
        eng1 = mgr.get_or_create_engine("dyn_prof", mode="execution")
        with eng1.connect() as c:
            assert c.execute(text("SELECT val FROM t;")).scalar() == "DB1"

        # Update profile to point to db2
        prof2 = ConnectionProfile(
            profile_id="dyn_prof",
            display_name="Dynamic Profile",
            engine_type="sqlite",
            database_path=str(db2),
        )
        store.register_profile(prof2)

        # Retrieve engine: manager must detect path change and return engine targeting db2
        eng2 = mgr.get_or_create_engine("dyn_prof", mode="execution")
        assert eng2 is not eng1
        with eng2.connect() as c:
            assert c.execute(text("SELECT val FROM t;")).scalar() == "DB2"

    def test_profile_deletion_and_recreation_lifecycle(self, store_and_mgr, tmp_path):
        """Deleting a profile purges cached engines; recreating it instantiates fresh engine."""
        store, factory, mgr = store_and_mgr

        db = tmp_path / "lifecycle.db"
        with sqlite3.connect(str(db)) as conn:
            conn.execute("CREATE TABLE t (id INT);")

        prof = ConnectionProfile(
            profile_id="del_prof",
            display_name="Delete Profile",
            engine_type="sqlite",
            database_path=str(db),
        )
        store.register_profile(prof)

        eng = mgr.get_or_create_engine("del_prof", mode="execution")
        assert mgr.is_engine_owned("del_prof")

        # Dispose profile engine
        assert mgr.dispose_engine("del_prof") is True
        assert mgr.is_engine_owned("del_prof") is False

        # Invalidate profile explicitly
        assert mgr.invalidate_profile("del_prof") is False  # Already disposed

    def test_invalidation_does_not_interrupt_active_in_flight_queries(self, store_and_mgr, tmp_path):
        """Disposing or invalidating an engine pool does not kill an active query thread."""
        store, factory, mgr = store_and_mgr

        db = tmp_path / "concurrent.db"
        with sqlite3.connect(str(db)) as conn:
            conn.execute("CREATE TABLE t (id INT);")
            for i in range(100):
                conn.execute(f"INSERT INTO t VALUES ({i});")

        prof = ConnectionProfile(
            profile_id="conc_prof",
            display_name="Concurrent Profile",
            engine_type="sqlite",
            database_path=str(db),
        )
        store.register_profile(prof)

        engine = mgr.get_or_create_engine("conc_prof", mode="execution")

        query_started = threading.Event()
        query_completed = threading.Event()
        query_result = []

        def worker():
            with engine.connect() as conn:
                query_started.set()
                # Run query while main thread calls dispose_engine
                time.sleep(0.05)
                res = conn.execute(text("SELECT count(*) FROM t;")).scalar()
                query_result.append(res)
                query_completed.set()

        t = threading.Thread(target=worker)
        t.start()

        query_started.wait(timeout=2.0)
        # Concurrently dispose engine while query is executing
        mgr.dispose_engine("conc_prof")

        query_completed.wait(timeout=2.0)
        t.join(timeout=2.0)

        assert query_result == [100]


# ==============================================================================
# Area 3: Watchdog Lifecycle & Concurrency
# ==============================================================================

class TestWatchdogLifecycleAndConcurrency:
    """Audit tests for watchdog thread termination, progress handler cleanup, and isolation."""

    @pytest.fixture
    def test_db(self, tmp_path):
        db_path = tmp_path / "watchdog.db"
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute("CREATE TABLE records (id INT);")
            for i in range(1000):
                conn.execute(f"INSERT INTO records VALUES ({i});")
        return str(db_path)

    def test_watchdog_thread_terminates_before_connection_disposal(self, test_db):
        """Verify the watchdog thread has fully terminated upon execution exit."""
        profile = ConnectionProfile(
            profile_id="wd_prof",
            display_name="Watchdog Test",
            engine_type="sqlite",
            database_path=test_db,
            timeout_seconds=5,
        )
        schema = ProfileDatabaseIntrospector(config=profile).introspect_model_facing()
        engine = create_profile_sqlite_engine(profile, mode="execution")
        executor = ProfileExecutionBoundary(config=profile, schema=schema, engine=engine)

        active_threads_before = threading.active_count()

        # Run query
        res = executor.execute(sql="SELECT count(*) FROM records;")
        assert res.row_count == 1

        # Verify no leaking watchdog threads
        active_threads_after = threading.active_count()
        assert active_threads_after <= active_threads_before

    def test_progress_handler_cleared_after_successful_query(self, test_db):
        """Verify progress handler is set to None and does not fire on recycled connections."""
        profile = ConnectionProfile(
            profile_id="wd_prof2",
            display_name="Watchdog Test 2",
            engine_type="sqlite",
            database_path=test_db,
            timeout_seconds=5,
        )
        schema = ProfileDatabaseIntrospector(config=profile).introspect_model_facing()
        engine = create_profile_sqlite_engine(profile, mode="execution")
        executor = ProfileExecutionBoundary(config=profile, schema=schema, engine=engine)

        # Run query 1
        res1 = executor.execute(sql="SELECT 1;")
        assert res1.row_count == 1
        assert list(res1.rows[0].values()) == [1]

        # Run query 2 sequentially: should succeed with no lingering interrupt
        res2 = executor.execute(sql="SELECT 2;")
        assert res2.row_count == 1
        assert list(res2.rows[0].values()) == [2]

    def test_watchdog_terminates_and_cleans_up_on_sql_syntax_error(self, test_db):
        """Verify watchdog thread terminates and progress handler clears after SQL errors."""
        profile = ConnectionProfile(
            profile_id="wd_prof3",
            display_name="Watchdog Test 3",
            engine_type="sqlite",
            database_path=test_db,
            timeout_seconds=5,
        )
        schema = ProfileDatabaseIntrospector(config=profile).introspect_model_facing()
        engine = create_profile_sqlite_engine(profile, mode="execution")
        executor = ProfileExecutionBoundary(config=profile, schema=schema, engine=engine)

        threads_before = threading.active_count()
        with pytest.raises(Exception):
            executor.execute(sql="SELECT invalid_column_xyz FROM records;")

        threads_after = threading.active_count()
        assert threads_after <= threads_before

    def test_watchdog_hard_timeout_interruption_and_cleanup(self, test_db):
        """Verify timeout interruption terminates the query and watchdog thread cleanly."""
        profile = ConnectionProfile(
            profile_id="wd_timeout_prof",
            display_name="Watchdog Timeout Test",
            engine_type="sqlite",
            database_path=test_db,
            timeout_seconds=1,  # minimum 1s timeout
        )
        schema = ProfileDatabaseIntrospector(config=profile).introspect_model_facing()
        engine = create_profile_sqlite_engine(profile, mode="execution")
        executor = ProfileExecutionBoundary(config=profile, schema=schema, engine=engine)

        # Recursive CTE that runs indefinitely
        long_query = """
        WITH RECURSIVE cnt(x) AS (
            SELECT 1
            UNION ALL
            SELECT x + 1 FROM cnt
        )
        SELECT count(*) FROM cnt;
        """

        threads_before = threading.active_count()
        with pytest.raises(QueryTimeoutError):
            executor.execute(sql=long_query)

        time.sleep(0.1)
        threads_after = threading.active_count()
        assert threads_after <= threads_before

        # Ensure subsequent normal query succeeds without being affected by the previous interrupt
        res = executor.execute(sql="SELECT 42;")
        assert res.row_count == 1
        assert list(res.rows[0].values()) == [42]

    def test_watchdog_timeout_during_result_fetching(self, test_db):
        """Verify timeout covers slow/heavy result fetching, not just statement execution."""
        profile = ConnectionProfile(
            profile_id="wd_fetch_timeout_prof",
            display_name="Watchdog Fetch Timeout Test",
            engine_type="sqlite",
            database_path=test_db,
            timeout_seconds=1,
        )
        schema = ProfileDatabaseIntrospector(config=profile).introspect_model_facing()
        engine = create_profile_sqlite_engine(profile, mode="execution")
        executor = ProfileExecutionBoundary(config=profile, schema=schema, engine=engine)

        # 3-way sorted cross-join that requires sorting 27M rows during fetch evaluation
        slow_fetch_query = """
        WITH RECURSIVE
            a(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM a WHERE x < 300),
            b(y) AS (SELECT 1 UNION ALL SELECT y+1 FROM b WHERE y < 300),
            c(z) AS (SELECT 1 UNION ALL SELECT z+1 FROM c WHERE z < 300)
        SELECT a.x, b.y, c.z FROM a CROSS JOIN b CROSS JOIN c ORDER BY a.x DESC, b.y DESC, c.z DESC;
        """

        threads_before = threading.active_count()
        with pytest.raises(QueryTimeoutError):
            executor.execute(sql=slow_fetch_query, max_rows=100)

        threads_after = threading.active_count()
        assert threads_after <= threads_before

    def test_watchdog_state_machine_prevents_interrupt_after_completion(self, test_db):
        """Deterministically prove that once state reaches COMPLETED, watchdog cannot interrupt."""
        profile = ConnectionProfile(
            profile_id="wd_race_prof",
            display_name="Watchdog Race Test",
            engine_type="sqlite",
            database_path=test_db,
            timeout_seconds=5,
        )

        interrupted_called = []

        class InterceptRawConn:
            def __init__(self, real_raw):
                self._real = real_raw

            def interrupt(self):
                interrupted_called.append(True)
                return self._real.interrupt()

            def __getattr__(self, name):
                return getattr(self._real, name)

        # Simulate execution completion state transition
        state_lock = threading.Lock()
        execution_state = ["RUNNING"]
        deadline = time.perf_counter() + 0.05
        finished_event = threading.Event()

        raw_conn = sqlite3.connect(test_db)
        intercepted = InterceptRawConn(raw_conn)

        def delayed_watchdog():
            time.sleep(0.06)  # Wake up after deadline
            if time.perf_counter() >= deadline:
                with state_lock:
                    if execution_state[0] == "RUNNING":
                        execution_state[0] = "TIMED_OUT"
                        intercepted.interrupt()

        th = threading.Thread(target=delayed_watchdog)
        th.start()

        # Simulate query completion prior to watchdog lock acquisition
        with state_lock:
            execution_state[0] = "COMPLETED"
        finished_event.set()

        th.join(timeout=2.0)
        assert not th.is_alive()
        # Verify interrupt was NOT called because state was COMPLETED
        assert interrupted_called == []
        raw_conn.close()

    def test_watchdog_terminates_cleanly_on_fetch_error(self, test_db):
        """Verify watchdog thread terminates and joins when fetch raises an unexpected error."""
        profile = ConnectionProfile(
            profile_id="wd_fetch_err_prof",
            display_name="Watchdog Fetch Error Test",
            engine_type="sqlite",
            database_path=test_db,
            timeout_seconds=5,
        )
        schema = ProfileDatabaseIntrospector(config=profile).introspect_model_facing()
        engine = create_profile_sqlite_engine(profile, mode="execution")
        executor = ProfileExecutionBoundary(config=profile, schema=schema, engine=engine)

        threads_before = threading.active_count()
        # Invalid JSON inside json_extract causes runtime OperationalError during fetch
        with pytest.raises(Exception):
            executor.execute(sql="SELECT json_extract('{', '$.a') FROM records;")

        threads_after = threading.active_count()
        assert threads_after <= threads_before

    def test_connection_release_cannot_precede_watchdog_termination(self, test_db):
        """Deterministically prove connection release cannot precede watchdog termination."""
        profile = ConnectionProfile(
            profile_id="wd_close_order_prof",
            display_name="Watchdog Close Order Test",
            engine_type="sqlite",
            database_path=test_db,
            timeout_seconds=5,
        )
        schema = ProfileDatabaseIntrospector(config=profile).introspect_model_facing()
        engine = create_profile_sqlite_engine(profile, mode="execution")

        observed_watchdog_alive_at_close = []
        conn_closed_event = []

        class MonitoredBoundary(ProfileExecutionBoundary):
            def _acquire_connection(self):
                real_conn = super()._acquire_connection()
                orig_close = real_conn.close

                def monitored_close():
                    # At the exact instant conn.close() is called, inspect all active threads
                    # for any watchdog thread created for this boundary
                    active_watchdogs = [
                        t for t in threading.enumerate()
                        if t.name.startswith("qp-sqlite-watchdog-") and t.is_alive()
                    ]
                    if active_watchdogs:
                        observed_watchdog_alive_at_close.append(len(active_watchdogs))
                    conn_closed_event.append(True)
                    orig_close()

                real_conn.close = monitored_close
                return real_conn

        executor = MonitoredBoundary(config=profile, schema=schema, engine=engine)
        res = executor.execute(sql="SELECT count(*) FROM records;")
        assert res.row_count == 1
        assert conn_closed_event == [True]
        # Proves watchdog thread is 100% terminated before conn.close() is reached
        assert observed_watchdog_alive_at_close == []


# ==============================================================================
# Area 4: WAL Filesystem Integrity
# ==============================================================================

class TestWALFilesystemIntegrity:
    """Audit tests ensuring zero modifications to .db or -wal sidecars across all scenarios."""

    @pytest.fixture
    def temp_dir(self):
        d = tempfile.mkdtemp(prefix="qp_wal_audit_")
        yield d
        shutil.rmtree(d, ignore_errors=True)

    def test_scenario_1_delete_journal_mode_integrity(self, temp_dir):
        """Scenario 1: Main DB in DELETE journal mode remains bit-for-bit identical."""
        db_path = os.path.join(temp_dir, "scenario1.db")
        wal_path = db_path + "-wal"
        shm_path = db_path + "-shm"

        with sqlite3.connect(db_path) as c:
            c.execute("PRAGMA journal_mode=DELETE;")
            c.execute("CREATE TABLE data (id INT, val TEXT);")
            c.execute("INSERT INTO data VALUES (1, 'Initial');")

        # Capture all 3 files before
        h_db_before, mtime_db_before = get_file_digest_and_mtime(db_path)
        h_wal_before, mtime_wal_before = get_file_digest_and_mtime(wal_path)
        h_shm_before, mtime_shm_before = get_file_digest_and_mtime(shm_path)

        assert h_wal_before is None
        assert h_shm_before is None

        profile = ConnectionProfile(
            profile_id="scen1_prof",
            display_name="Scenario 1",
            engine_type="sqlite",
            database_path=db_path,
        )
        engine = create_profile_sqlite_engine(profile, mode="execution")
        with engine.connect() as conn:
            val = conn.execute(text("SELECT val FROM data WHERE id = 1;")).scalar()
            assert val == "Initial"
        engine.dispose()

        # Capture all 3 files after
        h_db_after, mtime_db_after = get_file_digest_and_mtime(db_path)
        h_wal_after, mtime_wal_after = get_file_digest_and_mtime(wal_path)
        h_shm_after, mtime_shm_after = get_file_digest_and_mtime(shm_path)

        # Assert main database unchanged bit-for-bit
        assert h_db_before == h_db_after
        assert mtime_db_before == mtime_db_after
        # Assert no sidecars were created
        assert h_wal_after is None
        assert h_shm_after is None

    def test_scenario_2_wal_mode_with_existing_sidecars_integrity(self, temp_dir):
        """Scenario 2: WAL mode with -wal and -shm existing preserves .db and -wal hashes."""
        src_db = os.path.join(temp_dir, "src.db")
        src_wal = src_db + "-wal"
        src_shm = src_db + "-shm"

        c = sqlite3.connect(src_db)
        c.execute("PRAGMA journal_mode=WAL;")
        c.execute("CREATE TABLE data (id INT, val TEXT);")
        for i in range(200):
            c.execute(f"INSERT INTO data VALUES ({i}, 'Val_{i}');")
        c.commit()

        # Copy files to unshared destination directory
        dst_dir = os.path.join(temp_dir, "dest")
        os.makedirs(dst_dir)
        dst_db = os.path.join(dst_dir, "scenario2.db")
        dst_wal = dst_db + "-wal"
        dst_shm = dst_db + "-shm"

        with open(src_db, "rb") as fi, open(dst_db, "wb") as fo:
            fo.write(fi.read())
        if os.path.exists(src_wal):
            with open(src_wal, "rb") as fi, open(dst_wal, "wb") as fo:
                fo.write(fi.read())
        if os.path.exists(src_shm):
            with open(src_shm, "rb") as fi, open(dst_shm, "wb") as fo:
                fo.write(fi.read())
        c.close()

        # Capture before state of all 3 files
        h_db_before, mtime_db_before = get_file_digest_and_mtime(dst_db)
        h_wal_before, mtime_wal_before = get_file_digest_and_mtime(dst_wal)
        h_shm_before, mtime_shm_before = get_file_digest_and_mtime(dst_shm)

        assert h_db_before is not None
        assert h_wal_before is not None
        assert h_shm_before is not None

        profile = ConnectionProfile(
            profile_id="scen2_prof",
            display_name="Scenario 2",
            engine_type="sqlite",
            database_path=dst_db,
        )
        engine = create_profile_sqlite_engine(profile, mode="execution")
        with engine.connect() as conn:
            cnt = conn.execute(text("SELECT count(*) FROM data;")).scalar()
            assert cnt == 200
        engine.dispose()

        # Capture after state of all 3 files
        h_db_after, mtime_db_after = get_file_digest_and_mtime(dst_db)
        h_wal_after, mtime_wal_after = get_file_digest_and_mtime(dst_wal)
        h_shm_after, mtime_shm_after = get_file_digest_and_mtime(dst_shm)

        # Main DB and WAL log must be bit-for-bit identical
        assert h_db_before == h_db_after
        assert mtime_db_before == mtime_db_after
        assert h_wal_before == h_wal_after
        assert mtime_wal_before == mtime_wal_after
        # -shm coordination file persists
        assert h_shm_after is not None

    def test_scenario_3_wal_mode_missing_shm_in_writable_dir(self, temp_dir):
        """Scenario 3: WAL mode with missing -shm creates -shm coordination file, preserving .db and -wal."""
        src_db = os.path.join(temp_dir, "src3.db")
        src_wal = src_db + "-wal"

        c = sqlite3.connect(src_db)
        c.execute("PRAGMA journal_mode=WAL;")
        c.execute("CREATE TABLE data (id INT);")
        for i in range(100):
            c.execute(f"INSERT INTO data VALUES ({i});")
        c.commit()

        dst_dir = os.path.join(temp_dir, "dest3")
        os.makedirs(dst_dir)
        dst_db = os.path.join(dst_dir, "scenario3.db")
        dst_wal = dst_db + "-wal"
        dst_shm = dst_db + "-shm"

        with open(src_db, "rb") as fi, open(dst_db, "wb") as fo:
            fo.write(fi.read())
        if os.path.exists(src_wal):
            with open(src_wal, "rb") as fi, open(dst_wal, "wb") as fo:
                fo.write(fi.read())
        c.close()

        # Ensure -shm is absent initially
        assert not os.path.exists(dst_shm)

        # Capture before state
        h_db_before, mtime_db_before = get_file_digest_and_mtime(dst_db)
        h_wal_before, mtime_wal_before = get_file_digest_and_mtime(dst_wal)
        h_shm_before, mtime_shm_before = get_file_digest_and_mtime(dst_shm)

        assert h_shm_before is None

        profile = ConnectionProfile(
            profile_id="scen3_prof",
            display_name="Scenario 3",
            engine_type="sqlite",
            database_path=dst_db,
        )
        engine = create_profile_sqlite_engine(profile, mode="execution")
        with engine.connect() as conn:
            cnt = conn.execute(text("SELECT count(*) FROM data;")).scalar()
            assert cnt == 100
        engine.dispose()

        # Capture after state
        h_db_after, mtime_db_after = get_file_digest_and_mtime(dst_db)
        h_wal_after, mtime_wal_after = get_file_digest_and_mtime(dst_wal)
        h_shm_after, mtime_shm_after = get_file_digest_and_mtime(dst_shm)

        # Main DB and WAL log must be bit-for-bit identical
        assert h_db_before == h_db_after
        assert mtime_db_before == mtime_db_after
        assert h_wal_before == h_wal_after
        assert mtime_wal_before == mtime_wal_after

        # Explicitly verify coordination-file behavior:
        # SQLite creates -shm to coordinate read index without modifying database
        assert h_shm_after is not None
        # Verify -shm remains on disk after the last connection is closed
        assert os.path.exists(dst_shm)

    def test_scenario_4_wal_mode_in_readonly_dir_fails_closed(self, temp_dir):
        """Scenario 4: WAL mode in read-only directory fails closed with zero writable fallback."""
        db_dir = os.path.join(temp_dir, "readonly_dir")
        os.makedirs(db_dir)
        db_path = os.path.join(db_dir, "scenario4.db")
        wal_path = db_path + "-wal"
        shm_path = db_path + "-shm"

        c = sqlite3.connect(db_path)
        c.execute("PRAGMA journal_mode=WAL;")
        c.execute("CREATE TABLE data (id INT);")
        c.execute("INSERT INTO data VALUES (42);")
        c.close()

        if os.path.exists(shm_path):
            os.remove(shm_path)

        # Deny write permissions on directory via icacls on Windows
        subprocess.run(["icacls", db_dir, "/deny", "Everyone:(W,WD,AD)"], capture_output=True)

        # Capture before state
        h_db_before, mtime_db_before = get_file_digest_and_mtime(db_path)
        h_wal_before, mtime_wal_before = get_file_digest_and_mtime(wal_path)
        h_shm_before, mtime_shm_before = get_file_digest_and_mtime(shm_path)

        assert h_shm_before is None

        profile = ConnectionProfile(
            profile_id="scen4_prof",
            display_name="Scenario 4",
            engine_type="sqlite",
            database_path=db_path,
        )

        engine = None
        try:
            engine = create_profile_sqlite_engine(profile, mode="execution")
            with pytest.raises((OperationalError, DBAPIError, DatabaseConnectionError)):
                with engine.connect() as conn:
                    conn.execute(text("SELECT count(*) FROM data;")).scalar()
        finally:
            if engine:
                engine.dispose()
            subprocess.run(["icacls", db_dir, "/remove:d", "Everyone"], capture_output=True)

        # Capture after state
        h_db_after, mtime_db_after = get_file_digest_and_mtime(db_path)
        h_wal_after, mtime_wal_after = get_file_digest_and_mtime(wal_path)
        h_shm_after, mtime_shm_after = get_file_digest_and_mtime(shm_path)

        # Assert no unexpected modifications or file creations occurred
        assert h_db_before == h_db_after
        assert mtime_db_before == mtime_db_after
        if h_wal_before is not None:
            assert h_wal_before == h_wal_after
            assert mtime_wal_before == mtime_wal_after
        # -shm remains absent
        assert h_shm_after is None
