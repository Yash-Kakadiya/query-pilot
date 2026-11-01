"""Tests for SQLite dialect validation and authorizer security infrastructure."""

import sqlite3
import pytest
import sqlalchemy as sa

from query_pilot.db.models import ColumnMetadata, DatabaseSchema, TableMetadata
from query_pilot.db.sqlite_security import (
    ALLOWED_EXECUTION_FUNCTIONS,
    ALLOWED_INTROSPECTION_PRAGMAS,
    SQLiteSecurityError,
    configure_sqlite_connection,
    create_execution_authorizer,
    create_introspection_authorizer,
    get_connection_security_mode,
    reset_connection_security_registry,
)
from query_pilot.sql.validator import SQLValidator, validate_sql
from query_pilot.sql.models import ValidationReasonCode


@pytest.fixture(autouse=True)
def clean_registry():
    """Ensure connection security registry is clean before and after each test."""
    reset_connection_security_registry()
    yield
    reset_connection_security_registry()


@pytest.fixture
def sample_schema() -> DatabaseSchema:
    """Fixture providing a sample model-facing schema."""
    return DatabaseSchema(
        database_name="test_db",
        tables=[
            TableMetadata(
                schema_name="main",
                table_name="students",
                columns=[
                    ColumnMetadata(name="id", data_type="int", nullable=False),
                    ColumnMetadata(name="name", data_type="varchar", nullable=False),
                    ColumnMetadata(name="gpa", data_type="decimal", nullable=True),
                    ColumnMetadata(name="department_id", data_type="int", nullable=True),
                ],
            ),
            TableMetadata(
                schema_name="main",
                table_name="departments",
                columns=[
                    ColumnMetadata(name="id", data_type="int", nullable=False),
                    ColumnMetadata(name="name", data_type="varchar", nullable=False),
                ],
            ),
            TableMetadata(
                schema_name="main",
                table_name="accounts",
                columns=[
                    ColumnMetadata(name="id", data_type="int", nullable=False),
                    ColumnMetadata(name="username", data_type="varchar", nullable=False),
                    # Sensitive column (redacted from model-facing schema, but present in DB)
                ],
            ),
        ]
    )


# ---------------------------------------------------------------------------
# 1. SQL Validator Dialect Parameterization Tests
# ---------------------------------------------------------------------------

class TestSQLValidatorDialect:
    """Tests verifying SQL validation parameterization for MSSQL and SQLite."""

    def test_omitted_dialect_defaults_to_tsql(self, sample_schema: DatabaseSchema):
        v_omitted = SQLValidator(schema=sample_schema)
        assert v_omitted.dialect == "tsql"

        v_none = SQLValidator(schema=sample_schema, dialect=None)
        assert v_none.dialect == "tsql"

        v_empty = SQLValidator(schema=sample_schema, dialect="")
        assert v_empty.dialect == "tsql"

        v_spaces = SQLValidator(schema=sample_schema, dialect="   ")
        assert v_spaces.dialect == "tsql"

    def test_supported_dialects(self, sample_schema: DatabaseSchema):
        v_tsql = SQLValidator(schema=sample_schema, dialect="tsql")
        assert v_tsql.dialect == "tsql"

        v_sqlite = SQLValidator(schema=sample_schema, dialect="sqlite")
        assert v_sqlite.dialect == "sqlite"

    def test_aliased_dialects_normalization(self, sample_schema: DatabaseSchema):
        v_mssql = SQLValidator(schema=sample_schema, dialect="mssql")
        assert v_mssql.dialect == "tsql"

        v_mssql_caps = SQLValidator(schema=sample_schema, dialect="MSSQL")
        assert v_mssql_caps.dialect == "tsql"

        v_sqlite3 = SQLValidator(schema=sample_schema, dialect="sqlite3")
        assert v_sqlite3.dialect == "sqlite"

        v_sqlite3_caps = SQLValidator(schema=sample_schema, dialect="SQLite3")
        assert v_sqlite3_caps.dialect == "sqlite"

    def test_unsupported_dialects_rejected(self, sample_schema: DatabaseSchema):
        for unsupported in ["postgres", "postgresql", "mysql", "oracle", "duckdb", "unknown", "invalid"]:
            with pytest.raises(ValueError, match="Unsupported SQL dialect"):
                SQLValidator(schema=sample_schema, dialect=unsupported)

            with pytest.raises(ValueError, match="Unsupported SQL dialect"):
                validate_sql("SELECT 1", schema=sample_schema, dialect=unsupported)

    def test_tsql_top_syntax_default(self, sample_schema: DatabaseSchema):
        # T-SQL TOP syntax with default dialect
        sql = "SELECT TOP 10 id, name FROM students"
        result = validate_sql(sql, schema=sample_schema)
        assert result.allowed is True
        assert "TOP 10" in result.normalized_sql

    def test_sqlite_limit_syntax(self, sample_schema: DatabaseSchema):
        # SQLite LIMIT syntax with dialect="sqlite"
        sql = "SELECT id, name FROM students LIMIT 10"
        result = validate_sql(sql, schema=sample_schema, dialect="sqlite")
        assert result.allowed is True
        assert "LIMIT 10" in result.normalized_sql

    def test_sqlite_recursive_cte(self, sample_schema: DatabaseSchema):
        # Recursive CTE validation
        sql = """
        WITH RECURSIVE cte(n) AS (
            SELECT 1
            UNION ALL
            SELECT n + 1 FROM cte WHERE n < 10
        )
        SELECT n FROM cte
        """
        result = validate_sql(sql, schema=sample_schema, dialect="sqlite")
        assert result.allowed is True

    def test_sqlite_window_functions(self, sample_schema: DatabaseSchema):
        # Window functions in SQLite dialect
        sql = "SELECT id, name, ROW_NUMBER() OVER (ORDER BY gpa DESC) AS rank FROM students"
        result = validate_sql(sql, schema=sample_schema, dialect="sqlite")
        assert result.allowed is True

    def test_mutation_blocked_in_both_dialects(self, sample_schema: DatabaseSchema):
        sql = "DROP TABLE students"
        res_tsql = validate_sql(sql, schema=sample_schema, dialect="tsql")
        assert res_tsql.allowed is False
        assert any(r.code == ValidationReasonCode.NOT_READ_ONLY for r in res_tsql.reasons)

        res_sqlite = validate_sql(sql, schema=sample_schema, dialect="sqlite")
        assert res_sqlite.allowed is False
        assert any(r.code == ValidationReasonCode.NOT_READ_ONLY for r in res_sqlite.reasons)


# ---------------------------------------------------------------------------
# 2. SQLite Candidate Execution Authorizer Tests
# ---------------------------------------------------------------------------

class TestExecutionAuthorizer:
    """Tests verifying candidate SQL authorizer defense-in-depth."""

    @pytest.fixture
    def execution_db(self):
        """In-memory SQLite database populated with test schema."""
        conn = sqlite3.connect(":memory:")
        cur = conn.cursor()
        cur.execute("CREATE TABLE students (id INT, name TEXT, gpa REAL, department_id INT)")
        cur.execute("CREATE TABLE departments (id INT, name TEXT)")
        cur.execute("CREATE TABLE accounts (id INT, username TEXT, passwordhash TEXT)")

        cur.execute("INSERT INTO students VALUES (1, 'Alice', 3.9, 10), (2, 'Bob', 3.4, 20)")
        cur.execute("INSERT INTO departments VALUES (10, 'Computer Science'), (20, 'Mathematics')")
        cur.execute("INSERT INTO accounts VALUES (1, 'alice', 'secret_hash_123')")
        conn.commit()

        # Attach execution authorizer
        auth = create_execution_authorizer(
            allowed_tables={"students", "departments", "accounts"},
            sensitive_columns={"passwordhash", "accounts.passwordhash"},
            deny_sensitive=True,
        )
        conn.set_authorizer(auth)
        yield conn
        conn.close()

    def test_basic_select_allowed(self, execution_db: sqlite3.Connection):
        cur = execution_db.cursor()
        cur.execute("SELECT id, name, gpa FROM students")
        rows = cur.fetchall()
        assert len(rows) == 2

    def test_count_star_allowed(self, execution_db: sqlite3.Connection):
        cur = execution_db.cursor()
        cur.execute("SELECT COUNT(*) FROM students")
        res = cur.fetchone()
        assert res[0] == 2

    def test_aggregates_and_math_allowed(self, execution_db: sqlite3.Connection):
        cur = execution_db.cursor()
        cur.execute("SELECT SUM(gpa), AVG(gpa), MIN(gpa), MAX(gpa), ROUND(AVG(gpa), 2) FROM students")
        res = cur.fetchone()
        assert res[0] is not None

    def test_joins_allowed(self, execution_db: sqlite3.Connection):
        cur = execution_db.cursor()
        cur.execute("""
            SELECT s.name, d.name
            FROM students s
            JOIN departments d ON s.department_id = d.id
        """)
        rows = cur.fetchall()
        assert len(rows) == 2

    def test_ordinary_cte_allowed(self, execution_db: sqlite3.Connection):
        cur = execution_db.cursor()
        cur.execute("""
            WITH high_achievers AS (
                SELECT id, name, gpa FROM students WHERE gpa > 3.5
            )
            SELECT name FROM high_achievers
        """)
        rows = cur.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "Alice"

    def test_recursive_cte_allowed(self, execution_db: sqlite3.Connection):
        cur = execution_db.cursor()
        cur.execute("""
            WITH RECURSIVE numbers(n) AS (
                SELECT 1
                UNION ALL
                SELECT n + 1 FROM numbers WHERE n < 5
            )
            SELECT n FROM numbers
        """)
        rows = cur.fetchall()
        assert [r[0] for r in rows] == [1, 2, 3, 4, 5]

    def test_window_functions_allowed(self, execution_db: sqlite3.Connection):
        cur = execution_db.cursor()
        cur.execute("""
            SELECT id, name,
                   ROW_NUMBER() OVER (ORDER BY gpa DESC) as row_num,
                   RANK() OVER (ORDER BY gpa DESC) as rnk
            FROM students
        """)
        rows = cur.fetchall()
        assert len(rows) == 2

    def test_sensitive_column_denied(self, execution_db: sqlite3.Connection):
        cur = execution_db.cursor()
        # Querying username is fine
        cur.execute("SELECT id, username FROM accounts")
        assert len(cur.fetchall()) == 1

        # Querying passwordhash is denied
        with pytest.raises(sqlite3.DatabaseError, match=r"(prohibited|not authorized)"):
            cur.execute("SELECT passwordhash FROM accounts")

        with pytest.raises(sqlite3.DatabaseError, match=r"(prohibited|not authorized)"):
            cur.execute("SELECT * FROM accounts")

    def test_sensitive_column_ignore_policy(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE accounts (id INT, username TEXT, passwordhash TEXT)")
        conn.execute("INSERT INTO accounts VALUES (1, 'alice', 'hash')")

        auth = create_execution_authorizer(
            allowed_tables={"accounts"},
            sensitive_columns={"passwordhash"},
            deny_sensitive=False,  # Return SQLITE_IGNORE (NULL)
        )
        conn.set_authorizer(auth)
        cur = conn.cursor()
        cur.execute("SELECT username, passwordhash FROM accounts")
        row = cur.fetchone()
        assert row[0] == "alice"
        assert row[1] is None  # Evaluated to NULL by SQLite
        conn.close()

    def test_unapproved_table_denied(self, execution_db: sqlite3.Connection):
        # Create an unapproved table outside the authorizer's allowed_tables
        conn2 = sqlite3.connect(":memory:")
        conn2.execute("CREATE TABLE secret_table (x INT)")
        conn2.execute("INSERT INTO secret_table VALUES (42)")
        auth = create_execution_authorizer(allowed_tables={"students"})
        conn2.set_authorizer(auth)

        cur = conn2.cursor()
        with pytest.raises(sqlite3.DatabaseError, match=r"(prohibited|not authorized)"):
            cur.execute("SELECT * FROM secret_table")
        conn2.close()

    def test_candidate_pragmas_denied(self, execution_db: sqlite3.Connection):
        cur = execution_db.cursor()
        # All PRAGMAs must be denied for candidate execution
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            cur.execute("PRAGMA query_only = OFF;")

        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            cur.execute("PRAGMA table_info('students');")

        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            cur.execute("PRAGMA busy_timeout = 0;")

    def test_candidate_transactions_and_savepoints_denied(self, execution_db: sqlite3.Connection):
        cur = execution_db.cursor()
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            cur.execute("BEGIN TRANSACTION;")

        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            cur.execute("SAVEPOINT sp1;")

        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            cur.execute("RELEASE SAVEPOINT sp1;")

    def test_candidate_ddl_and_dml_denied(self, execution_db: sqlite3.Connection):
        cur = execution_db.cursor()
        # DDL
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            cur.execute("DROP TABLE students;")

        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            cur.execute("CREATE TABLE evil (id INT);")

        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            cur.execute("ALTER TABLE students ADD COLUMN evil_col INT;")

        # DML
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            cur.execute("INSERT INTO students VALUES (3, 'Charlie', 3.0, 10);")

        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            cur.execute("UPDATE students SET gpa = 4.0;")

        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            cur.execute("DELETE FROM students;")

    def test_attach_and_detach_denied(self, execution_db: sqlite3.Connection):
        cur = execution_db.cursor()
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            cur.execute("ATTACH DATABASE ':memory:' AS attached_db;")

        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            cur.execute("DETACH DATABASE attached_db;")

    def test_unapproved_functions_denied(self, execution_db: sqlite3.Connection):
        cur = execution_db.cursor()
        # load_extension must be denied
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            cur.execute("SELECT load_extension('foo.dll');")

        # sqlite_version and non-whitelisted functions must be denied
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            cur.execute("SELECT sqlite_version();")

    def test_statement_repreparation_authorizer_enforcement(self, tmp_path):
        """Verify authorizer is re-invoked on statement re-preparation after schema changes."""
        db_file = str(tmp_path / "test_reprep.db")

        # 1. Setup DB
        c1 = sqlite3.connect(db_file)
        c1.execute("CREATE TABLE t (id INT, val TEXT)")
        c1.execute("INSERT INTO t VALUES (1, 'hello')")
        c1.commit()

        # 2. Attach authorizer
        auth_calls = []
        def auth_wrapper(action, p1, p2, db, trigger):
            auth_calls.append((action, p1, p2))
            return sqlite3.SQLITE_OK

        c1.set_authorizer(auth_wrapper)
        cur = c1.cursor()
        cur.execute("SELECT id FROM t")
        cur.fetchall()  # Exhaust cursor to release statement lock
        initial_calls = len(auth_calls)
        assert initial_calls > 0

        # 3. Trigger schema modification via secondary connection
        c2 = sqlite3.connect(db_file)
        c2.execute("ALTER TABLE t ADD COLUMN extra INT")
        c2.commit()
        c2.close()

        # 4. Re-execute cached statement on c1: SQLite must re-prepare and invoke authorizer
        cur.execute("SELECT id FROM t")
        cur.fetchall()
        assert len(auth_calls) > initial_calls
        c1.close()


# ---------------------------------------------------------------------------
# 3. Trusted Introspection Authorizer Tests
# ---------------------------------------------------------------------------

class TestIntrospectionAuthorizer:
    """Tests verifying trusted schema introspection authorizer policy."""

    @pytest.fixture
    def intro_db(self):
        conn = sqlite3.connect(":memory:")
        cur = conn.cursor()
        cur.execute("CREATE TABLE students (id INTEGER PRIMARY KEY, name TEXT, gpa REAL)")
        cur.execute("CREATE TABLE courses (id INTEGER PRIMARY KEY, student_id INTEGER, FOREIGN KEY(student_id) REFERENCES students(id))")
        cur.execute("CREATE INDEX idx_students_name ON students(name)")
        conn.commit()

        auth = create_introspection_authorizer(allowed_tables={"students", "courses"})
        conn.set_authorizer(auth)
        yield conn
        conn.close()

    def test_schema_pragmas_allowed(self, intro_db: sqlite3.Connection):
        cur = intro_db.cursor()
        # table_info
        cur.execute("PRAGMA table_info('students')")
        cols = cur.fetchall()
        assert len(cols) == 3

        # foreign_key_list
        cur.execute("PRAGMA foreign_key_list('courses')")
        fks = cur.fetchall()
        assert len(fks) == 1

        # index_list
        cur.execute("PRAGMA index_list('students')")
        idxs = cur.fetchall()
        assert len(idxs) >= 1

        # database_list
        cur.execute("PRAGMA database_list")
        dbs = cur.fetchall()
        assert len(dbs) >= 1

    def test_mutating_pragmas_denied(self, intro_db: sqlite3.Connection):
        cur = intro_db.cursor()
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            cur.execute("PRAGMA query_only = OFF;")

        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            cur.execute("PRAGMA busy_timeout = 0;")

        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            cur.execute("PRAGMA writable_schema = ON;")

    def test_unapproved_table_pragma_denied(self, intro_db: sqlite3.Connection):
        cur = intro_db.cursor()
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            cur.execute("PRAGMA table_info('secret_table')")

    def test_sqlalchemy_reflection_with_introspection_authorizer(self):
        """Verify SQLAlchemy inspect() succeeds cleanly under the introspection authorizer."""
        engine = sa.create_engine("sqlite:///:memory:")
        with engine.connect() as conn:
            conn.execute(sa.text("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT)"))
            conn.execute(sa.text("CREATE TABLE posts (id INTEGER PRIMARY KEY, user_id INTEGER, title TEXT, FOREIGN KEY(user_id) REFERENCES users(id))"))
            conn.commit()

        auth = create_introspection_authorizer()
        with engine.connect() as conn:
            raw_conn = conn.connection.dbapi_connection
            raw_conn.set_authorizer(auth)

            insp = sa.inspect(conn)
            tables = sorted(insp.get_table_names())
            assert "posts" in tables
            assert "users" in tables

            user_cols = [c["name"] for c in insp.get_columns("users")]
            assert "id" in user_cols
            assert "username" in user_cols

            fks = insp.get_foreign_keys("posts")
            assert len(fks) == 1
            assert fks[0]["referred_table"] == "users"


# ---------------------------------------------------------------------------
# 4. Physical Connection Separation & Configuration Tests
# ---------------------------------------------------------------------------

class TestConnectionSeparation:
    """Tests verifying connection separation and configuration safeguards."""

    def test_configure_execution_connection(self):
        conn = sqlite3.connect(":memory:")
        configure_sqlite_connection(conn, mode="execution", allowed_tables=["t1"])

        cur = conn.cursor()
        # Check candidate execution authorizer is active and blocks all PRAGMAs
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            cur.execute("PRAGMA query_only")

        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            cur.execute("PRAGMA table_info('t1')")

        # Temporarily detach authorizer to inspect underlying PRAGMA state
        conn.set_authorizer(None)
        cur.execute("PRAGMA query_only")
        row = cur.fetchone()
        assert row[0] == 1  # query_only was applied as ON
        conn.close()

    def test_configure_introspection_connection(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE t1 (id INT)")
        configure_sqlite_connection(conn, mode="introspection", allowed_tables=["t1"])

        cur = conn.cursor()
        # Schema pragma allowed
        cur.execute("PRAGMA table_info('t1')")
        assert len(cur.fetchall()) == 1

        # Mutating pragma denied
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            cur.execute("PRAGMA query_only = OFF")
        conn.close()

    def test_switching_connection_mode_is_blocked(self):
        conn = sqlite3.connect(":memory:")
        configure_sqlite_connection(conn, mode="introspection")

        # Attempting to switch connection to execution must fail
        with pytest.raises(SQLiteSecurityError, match="Physical connection separation violation"):
            configure_sqlite_connection(conn, mode="execution")

        conn.close()

    def test_invalid_mode_raises(self):
        conn = sqlite3.connect(":memory:")
        with pytest.raises(SQLiteSecurityError, match="Invalid SQLite security mode"):
            configure_sqlite_connection(conn, mode="super_admin")
        conn.close()

    def test_connection_mode_lifecycle_and_cleanup(self):
        conn = sqlite3.connect(":memory:")
        # Fresh connection has no security mode
        assert get_connection_security_mode(conn) is None

        # Configured connection returns mode
        configure_sqlite_connection(conn, mode="execution")
        assert get_connection_security_mode(conn) == "execution"

        # Idempotent reconfiguration with same mode succeeds
        configure_sqlite_connection(conn, mode="execution")
        assert get_connection_security_mode(conn) == "execution"

        # Closed connection returns None
        conn.close()
        assert get_connection_security_mode(conn) is None

    def test_repeated_connection_creation_and_disposal_no_stale_state(self):
        """Verify repeated allocation/disposal cannot leak stale security-mode associations."""
        for i in range(50):
            target_mode = "execution" if i % 2 == 0 else "introspection"
            conn = sqlite3.connect(":memory:")
            try:
                # Every freshly opened connection MUST start unconfigured
                assert get_connection_security_mode(conn) is None

                # Must configure cleanly without false-positive separation violation
                configure_sqlite_connection(conn, mode=target_mode)
                assert get_connection_security_mode(conn) == target_mode
            finally:
                conn.close()
                assert get_connection_security_mode(conn) is None

    def test_failed_configuration_rolls_back_security_tag(self, monkeypatch):
        """Verify that configuration exceptions cleanly roll back registered mode state."""
        conn = sqlite3.connect(":memory:")
        assert get_connection_security_mode(conn) is None

        # Simulate unexpected failure during authorizer attachment
        def failing_authorizer(*args, **kwargs):
            raise RuntimeError("Simulated authorizer attachment failure")

        monkeypatch.setattr(
            "query_pilot.db.sqlite_security.create_execution_authorizer",
            failing_authorizer,
        )

        with pytest.raises(RuntimeError, match="Simulated authorizer attachment failure"):
            configure_sqlite_connection(conn, mode="execution")

        # Security mode must NOT linger after failure
        assert get_connection_security_mode(conn) is None
        conn.close()
