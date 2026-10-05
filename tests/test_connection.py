"""Unit and integration tests for database connection and security safeguards."""

from unittest.mock import MagicMock
import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection

from query_pilot.config import DatabaseSettings
from query_pilot.db.connection import (
    ALLOWED_TARGET_DATABASE,
    DatabaseConnectionError,
    DatabaseSecurityError,
    create_db_engine,
    get_connection,
    verify_database_context,
)


def test_verify_database_context_success():
    """Verify that an active connection to GradeSense_Local succeeds."""
    engine = create_db_engine()
    with engine.connect() as conn:
        db_name = verify_database_context(conn, expected_db=ALLOWED_TARGET_DATABASE)
        assert db_name == ALLOWED_TARGET_DATABASE


@pytest.mark.parametrize(
    "unauthorized_db",
    ["master", "GradeSense", "GradeSenseCodeFirst", "TempDB", "model", "", None],
)
def test_verify_database_context_rejection(unauthorized_db):
    """Verify that any database other than GradeSense_Local raises DatabaseSecurityError."""
    mock_conn = MagicMock(spec=Connection)
    mock_result = MagicMock()
    mock_result.scalar.return_value = unauthorized_db
    mock_conn.execute.return_value = mock_result

    with pytest.raises(DatabaseSecurityError) as exc_info:
        verify_database_context(mock_conn, expected_db=ALLOWED_TARGET_DATABASE)

    assert "SECURITY VIOLATION" in str(exc_info.value)
    assert ALLOWED_TARGET_DATABASE in str(exc_info.value)


def test_verify_database_context_query_failure():
    """Verify that execution failure during DB_NAME query raises DatabaseConnectionError."""
    from sqlalchemy.exc import OperationalError

    mock_conn = MagicMock(spec=Connection)
    mock_conn.execute.side_effect = OperationalError("SELECT DB_NAME()", {}, Exception("Connection lost"))

    with pytest.raises(DatabaseConnectionError) as exc_info:
        verify_database_context(mock_conn, expected_db=ALLOWED_TARGET_DATABASE)

    assert "Failed to query SQL Server session context" in str(exc_info.value)


def test_get_connection_context_manager():
    """Verify that get_connection yields a verified connection and closes it."""
    engine = create_db_engine()
    with get_connection(engine) as conn:
        assert conn is not None
        assert not conn.closed
        result = conn.execute(text("SELECT 1;")).scalar()
        assert result == 1

    assert conn.closed


def test_get_connection_rejects_unauthorized_database():
    """Verify that get_connection enforces security boundary."""
    mock_conn = MagicMock(spec=Connection)
    mock_conn.closed = False
    mock_result = MagicMock()
    mock_result.scalar.return_value = "GradeSense"
    mock_conn.execute.return_value = mock_result

    mock_engine = MagicMock()
    mock_engine.connect.return_value = mock_conn

    with pytest.raises(DatabaseSecurityError):
        with get_connection(mock_engine, expected_db=ALLOWED_TARGET_DATABASE):
            pass

    mock_conn.close.assert_called_once()


def test_create_db_engine_invalid_driver():
    """Verify that invalid connection settings trigger DatabaseConnectionError on connect."""
    bad_settings = DatabaseSettings(
        DB_DRIVER="NonExistentDriverName_12345",
        DB_NAME="GradeSense_Local",
    )
    engine = create_db_engine(bad_settings)
    with pytest.raises(DatabaseConnectionError) as exc_info:
        with get_connection(engine):
            pass
    assert "Failed to establish connection" in str(exc_info.value)
