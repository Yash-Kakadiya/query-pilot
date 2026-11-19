"""Tests for PostgreSQL and MySQL Connection Profiles and Read-Only Enforcement.

Validates:
1. Profile validation and default port/schema resolution for PostgreSQL (5432, public) and MySQL (3306).
2. Dialect derivation ("postgres" and "mysql") and driver URLs (psycopg, pymysql).
3. Cloud password enforcement and TLS/SSL parameter injection.
4. Credential masking in safe URLs and diagnostic strings.
5. Session-level read-only connection guard execution (SET default_transaction_read_only = ON, SET SESSION TRANSACTION READ ONLY).
6. Engine session context verification (SELECT current_database(), SELECT DATABASE()).
7. Fail-closed safety: mutation statements (DROP, UPDATE, INSERT) rejected for all dialects.
"""

from unittest.mock import MagicMock, call, patch
import pytest

from query_pilot.db.connection import (
    DatabaseSecurityError,
    verify_engine_database_context,
)
from query_pilot.db.profile import (
    ConnectionProfile,
    ProfileConfigurationError,
)
from query_pilot.db.profile_executor import ProfileQueryExecutor
from query_pilot.db.models import DatabaseSchema, TableMetadata, ColumnMetadata
from query_pilot.sql.models import ValidationReasonCode
from query_pilot.sql.validator import (
    SQLValidator,
    normalize_dialect,
    validate_sql,
)


# =============================================================================
# 1. Profile Validation & SQLAlchemy URL Generation
# =============================================================================


def test_postgresql_profile_defaults_and_url():
    """Verify PostgreSQL profile port, schema, driver, and SQLAlchemy URL."""
    profile = ConnectionProfile(
        profile_id="pg_local_test",
        display_name="Local PostgreSQL",
        engine_type="postgresql",
        server="localhost",
        database_name="app_db",
        user="postgres",
        password="secretpassword123",
        is_cloud=False,
    )

    assert profile.engine_type == "postgresql"
    assert profile.get_dialect() == "postgres"
    assert profile.schema_name == "public"

    # Raw URL contains credentials and psycopg driver
    raw_url = profile.get_sqlalchemy_url()
    assert "postgresql+psycopg://postgres:secretpassword123@localhost:5432/app_db" in raw_url

    # Safe URL redacts password
    safe_url = profile.get_sqlalchemy_url(hide_password=True)
    assert "secretpassword123" not in safe_url
    assert "postgres:********@localhost:5432/app_db" in safe_url


def test_mysql_profile_defaults_and_url():
    """Verify MySQL profile port, driver, and SQLAlchemy URL."""
    profile = ConnectionProfile(
        profile_id="mysql_local_test",
        display_name="Local MySQL",
        engine_type="mysql",
        server="127.0.0.1",
        database_name="store_db",
        user="root",
        password="mysql_secret_pass",
        is_cloud=False,
    )

    assert profile.engine_type == "mysql"
    assert profile.get_dialect() == "mysql"

    # Raw URL contains pymysql driver
    raw_url = profile.get_sqlalchemy_url()
    assert "mysql+pymysql://root:mysql_secret_pass@127.0.0.1:3306/store_db" in raw_url

    # Safe URL redacts password
    safe_url = profile.get_sqlalchemy_url(hide_password=True)
    assert "mysql_secret_pass" not in safe_url
    assert "root:********@127.0.0.1:3306/store_db" in safe_url


def test_cloud_postgresql_enforces_password_and_ssl():
    """Verify Cloud PostgreSQL requires password and enables sslmode=require."""
    # Missing password raises error for cloud
    with pytest.raises(Exception, match="password must be specified for cloud"):
        ConnectionProfile(
            profile_id="pg_cloud_nopass",
            display_name="Cloud PG No Pass",
            engine_type="postgresql",
            server="aws-0-us-east-1.pooler.supabase.com",
            database_name="postgres",
            user="postgres.project",
            is_cloud=True,
        )

    # Valid cloud PG includes sslmode=require in URL
    profile = ConnectionProfile(
        profile_id="supabase_prod",
        display_name="Supabase Prod",
        engine_type="postgresql",
        server="aws-0-us-east-1.pooler.supabase.com",
        database_name="postgres",
        user="postgres.project",
        password="MySupabasePassword!",
        encrypt=True,
        is_cloud=True,
    )

    url = profile.get_sqlalchemy_url()
    assert "sslmode=require" in url


def test_cloud_mysql_enforces_password_and_ssl():
    """Verify Cloud MySQL requires password and enables ssl_mode=REQUIRED."""
    with pytest.raises(Exception, match="password must be specified for cloud"):
        ConnectionProfile(
            profile_id="mysql_cloud_nopass",
            display_name="Cloud MySQL No Pass",
            engine_type="mysql",
            server="aws.connect.psdb.cloud",
            database_name="mydb",
            user="user123",
            is_cloud=True,
        )

    profile = ConnectionProfile(
        profile_id="planetscale_prod",
        display_name="PlanetScale Prod",
        engine_type="mysql",
        server="aws.connect.psdb.cloud",
        database_name="mydb",
        user="user123",
        password="CloudPassword456",
        encrypt=True,
        is_cloud=True,
    )

    url = profile.get_sqlalchemy_url()
    assert "ssl_mode=REQUIRED" in url


# =============================================================================
# 2. Database Identity Verification
# =============================================================================


def test_verify_engine_database_context_postgresql():
    """Verify PostgreSQL database context uses SELECT current_database()."""
    mock_conn = MagicMock()
    mock_conn.execute.return_value.scalar.return_value = "app_db"

    # Match succeeds
    verified = verify_engine_database_context(mock_conn, expected_db="app_db", engine_type="postgresql")
    assert verified == "app_db"
    assert "SELECT current_database()" in str(mock_conn.execute.call_args[0][0])

    # Mismatch raises DatabaseSecurityError
    mock_conn.execute.return_value.scalar.return_value = "unauthorized_db"
    with pytest.raises(DatabaseSecurityError, match="SECURITY VIOLATION: Connected to unauthorized database"):
        verify_engine_database_context(mock_conn, expected_db="app_db", engine_type="postgresql")


def test_verify_engine_database_context_mysql():
    """Verify MySQL database context uses SELECT DATABASE()."""
    mock_conn = MagicMock()
    mock_conn.execute.return_value.scalar.return_value = "store_db"

    # Match succeeds
    verified = verify_engine_database_context(mock_conn, expected_db="store_db", engine_type="mysql")
    assert verified == "store_db"
    assert "SELECT DATABASE()" in str(mock_conn.execute.call_args[0][0])

    # Mismatch raises DatabaseSecurityError
    mock_conn.execute.return_value.scalar.return_value = "production_db"
    with pytest.raises(DatabaseSecurityError, match="SECURITY VIOLATION: Connected to unauthorized database"):
        verify_engine_database_context(mock_conn, expected_db="store_db", engine_type="mysql")


# =============================================================================
# 3. Read-Only Transaction Guard Enforcement
# =============================================================================


def test_profile_executor_postgresql_read_only_guards():
    """Verify ProfileQueryExecutor configures PostgreSQL session read-only guards."""
    profile = ConnectionProfile(
        profile_id="pg_test",
        display_name="PG Test",
        engine_type="postgresql",
        server="localhost",
        database_name="testdb",
        user="postgres",
        password="pw",
        timeout_seconds=5,
    )

    mock_engine = MagicMock()
    mock_conn = MagicMock()
    mock_engine.connect.return_value = mock_conn

    executor = ProfileQueryExecutor(profile, engine=mock_engine)
    mock_conn_inst = MagicMock()

    executor._configure_connection_guards(mock_conn_inst, engine_type="postgresql", timeout_seconds=5)

    executed_statements = [str(call_args[0][0]) for call_args in mock_conn_inst.execute.call_args_list]
    assert any("SET default_transaction_read_only = ON" in stmt for stmt in executed_statements)
    assert any("SET statement_timeout = 5000" in stmt for stmt in executed_statements)


def test_profile_executor_mysql_read_only_guards():
    """Verify ProfileQueryExecutor configures MySQL session read-only guards."""
    profile = ConnectionProfile(
        profile_id="mysql_test",
        display_name="MySQL Test",
        engine_type="mysql",
        server="localhost",
        database_name="testdb",
        user="root",
        password="pw",
        timeout_seconds=10,
    )

    mock_engine = MagicMock()
    executor = ProfileQueryExecutor(profile, engine=mock_engine)
    mock_conn_inst = MagicMock()

    executor._configure_connection_guards(mock_conn_inst, engine_type="mysql", timeout_seconds=10)

    executed_statements = [str(call_args[0][0]) for call_args in mock_conn_inst.execute.call_args_list]
    assert any("SET SESSION TRANSACTION READ ONLY" in stmt for stmt in executed_statements)
    assert any("SET max_execution_time = 10000" in stmt for stmt in executed_statements)


# =============================================================================
# 4. Fail-Closed SQL Validation for PostgreSQL & MySQL Dialects
# =============================================================================


def test_normalize_dialect_supports_postgres_and_mysql():
    """Verify dialect canonicalization for postgres and mysql."""
    assert normalize_dialect("postgres") == "postgres"
    assert normalize_dialect("postgresql") == "postgres"
    assert normalize_dialect("pg") == "postgres"
    assert normalize_dialect("mysql") == "mysql"
    assert normalize_dialect("mariadb") == "mysql"
    assert normalize_dialect("tsql") == "tsql"
    assert normalize_dialect("sqlite") == "sqlite"


def test_postgres_read_only_sql_validation():
    """Verify PostgreSQL SELECT queries pass and mutations fail closed."""
    schema = DatabaseSchema(
        database_name="app_db",
        tables=[
            TableMetadata(
                schema_name="public",
                table_name="users",
                columns=[
                    ColumnMetadata(name="id", data_type="INTEGER", nullable=False, primary_key=True),
                    ColumnMetadata(name="name", data_type="VARCHAR", nullable=False),
                    ColumnMetadata(name="created_at", data_type="TIMESTAMP", nullable=False),
                    ColumnMetadata(name="active", data_type="BOOLEAN", nullable=False),
                ],
            )
        ],
    )
    validator = SQLValidator(schema=schema, dialect="postgres")

    # Valid SELECT
    result = validator.validate("SELECT id, name, created_at FROM users WHERE active = true LIMIT 10;")
    assert result.allowed is True

    # Mutations blocked
    res_del = validator.validate("DELETE FROM users WHERE id = 1;")
    assert res_del.allowed is False
    assert any(r.code == ValidationReasonCode.NOT_READ_ONLY for r in res_del.reasons)

    res_upd = validator.validate("UPDATE users SET active = false WHERE id = 1;")
    assert res_upd.allowed is False
    assert any(r.code == ValidationReasonCode.NOT_READ_ONLY for r in res_upd.reasons)

    res_drop = validator.validate("DROP TABLE users;")
    assert res_drop.allowed is False
    assert any(r.code == ValidationReasonCode.NOT_READ_ONLY for r in res_drop.reasons)


def test_mysql_read_only_sql_validation():
    """Verify MySQL SELECT queries pass and mutations fail closed."""
    schema = DatabaseSchema(
        database_name="store_db",
        tables=[
            TableMetadata(
                schema_name="store_db",
                table_name="students",
                columns=[
                    ColumnMetadata(name="id", data_type="INT", nullable=False, primary_key=True),
                    ColumnMetadata(name="name", data_type="VARCHAR", nullable=False),
                    ColumnMetadata(name="score", data_type="INT", nullable=False),
                ],
            )
        ],
    )
    validator = SQLValidator(schema=schema, dialect="mysql")

    # Valid SELECT with backticks and LIMIT
    result = validator.validate("SELECT `id`, `name`, `score` FROM `students` ORDER BY `score` DESC LIMIT 5;")
    assert result.allowed is True

    # Mutations blocked
    res_ins = validator.validate("INSERT INTO students (id, name, score) VALUES (1, 'Alice', 95);")
    assert res_ins.allowed is False
    assert any(r.code == ValidationReasonCode.NOT_READ_ONLY for r in res_ins.reasons)

    res_alt = validator.validate("ALTER TABLE students ADD COLUMN grade VARCHAR(2);")
    assert res_alt.allowed is False
    assert any(r.code == ValidationReasonCode.NOT_READ_ONLY for r in res_alt.reasons)

    res_trunc = validator.validate("TRUNCATE TABLE students;")
    assert res_trunc.allowed is False
    assert any(r.code == ValidationReasonCode.NOT_READ_ONLY for r in res_trunc.reasons)
