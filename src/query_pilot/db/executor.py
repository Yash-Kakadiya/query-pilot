"""Safe, read-only SQL query execution layer for QueryPilot.

Enforces baseline read-only safety via AST inspection with sqlglot, strict
single-statement execution, bounded row fetching, query timeouts, and
session-level database context validation against GradeSense_Local.
"""

from dataclasses import asdict, dataclass
import datetime
import decimal
import logging
import time
from typing import Any, Dict, List, Optional
import uuid

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError, OperationalError, SQLAlchemyError
import sqlglot
from sqlglot import exp

from query_pilot.config import DatabaseSettings, get_db_settings
from query_pilot.db.connection import (
    ALLOWED_TARGET_DATABASE,
    create_db_engine,
    get_connection,
)

logger = logging.getLogger(__name__)

# AST node types that indicate data/schema mutation or privileged command execution
FORBIDDEN_AST_NODES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Drop,
    exp.Alter,
    exp.Create,
    exp.TruncateTable,
    exp.Execute,
    exp.Command,
    exp.Grant,
    exp.Revoke,
    exp.Into,
    exp.Set,
    exp.Transaction,
)


class QueryExecutionError(Exception):
    """Raised when query execution fails in the database."""
    pass


class UnsafeQueryError(QueryExecutionError):
    """Raised when a query fails baseline read-only validation or is multi-statement."""
    pass


class QueryTimeoutError(QueryExecutionError):
    """Raised when a query exceeds the configured execution timeout."""
    pass


@dataclass(frozen=True)
class QueryResult:
    """Structured, JSON-serializable representation of query results."""

    columns: List[str]
    rows: List[Dict[str, Any]]
    row_count: int
    truncated: bool
    execution_time_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        """Convert QueryResult to a dictionary."""
        return asdict(self)


def serialize_value(val: Any) -> Any:
    """Convert database values into JSON-friendly native types."""
    if val is None:
        return None
    if isinstance(val, (datetime.datetime, datetime.date, datetime.time)):
        return val.isoformat()
    if isinstance(val, decimal.Decimal):
        # Return int if exact whole number, otherwise float
        return int(val) if val % 1 == 0 else float(val)
    if isinstance(val, (bytes, bytearray)):
        return val.hex()
    if isinstance(val, uuid.UUID):
        return str(val)
    return val


def validate_read_only_sql(sql: str) -> exp.Query:
    """Validate that SQL is a single, strictly read-only query statement.

    Uses sqlglot to parse the statement into an Abstract Syntax Tree (AST),
    ensuring that keywords inside string literals and comments are not falsely
    flagged while verifying that no mutating operations exist anywhere in the AST.

    Args:
        sql: The SQL statement string to inspect.

    Returns:
        The validated sqlglot exp.Query AST node.

    Raises:
        UnsafeQueryError: If the statement is empty, contains multiple statements,
                          cannot be parsed, is not a Query, or contains mutations.
    """
    if not sql or not sql.strip():
        raise UnsafeQueryError("Query string cannot be empty.")

    try:
        parsed = sqlglot.parse(sql, read="tsql")
    except Exception as exc:
        raise UnsafeQueryError(f"SQL parsing failed: {exc}") from exc

    # Filter out empty statements or trailing semicolons that sqlglot emits as Semicolon nodes
    statements = [s for s in parsed if s is not None and not isinstance(s, exp.Semicolon)]

    if len(statements) == 0:
        raise UnsafeQueryError("No valid SQL statement found.")

    if len(statements) > 1:
        raise UnsafeQueryError(
            f"Multi-statement execution is strictly forbidden. Found {len(statements)} statements."
        )

    stmt = statements[0]

    if not isinstance(stmt, exp.Query):
        raise UnsafeQueryError(
            f"Only read-only queries (SELECT) are permitted. Received: {type(stmt).__name__}."
        )

    for node_type in FORBIDDEN_AST_NODES:
        if stmt.find(node_type) is not None:
            raise UnsafeQueryError(
                f"Query contains forbidden operation: {node_type.__name__}."
            )

    return stmt


class ReadOnlyQueryExecutor:
    """Executes validated read-only SQL queries against GradeSense_Local."""

    def __init__(
        self,
        engine: Optional[Engine] = None,
        settings: Optional[DatabaseSettings] = None,
        default_max_rows: Optional[int] = None,
        default_timeout_seconds: Optional[int] = None,
    ):
        """Initialize executor with database settings and optional custom engine.

        Args:
            engine: SQLAlchemy Engine (creates one from settings if None).
            settings: DatabaseSettings configuration (defaults to cached settings).
            default_max_rows: Default row limit (defaults to settings.DB_MAX_ROW_LIMIT).
            default_timeout_seconds: Default timeout in seconds (defaults to DB_QUERY_TIMEOUT_SECONDS).
        """
        self.settings = settings or get_db_settings()
        self.engine = engine or create_db_engine(self.settings)
        self.default_max_rows = default_max_rows or self.settings.DB_MAX_ROW_LIMIT
        self.default_timeout_seconds = (
            default_timeout_seconds or self.settings.DB_QUERY_TIMEOUT_SECONDS
        )

    def execute(
        self,
        sql: str,
        params: Optional[Dict[str, Any]] = None,
        max_rows: Optional[int] = None,
        timeout_seconds: Optional[int] = None,
    ) -> QueryResult:
        """Execute a read-only SQL query and return structured, sanitized results.

        1. Validates that the query is strictly read-only and single-statement via AST.
        2. Validates session database context (verifies GradeSense_Local).
        3. Enforces execution timeout at both connection and driver levels.
        4. Bounded row fetching: retrieves at most (max_rows + 1) rows over the wire,
           closing the cursor immediately to avoid unbounded memory consumption.
        5. Formats results into JSON-friendly native types preserving column order.

        Args:
            sql: SQL statement to execute.
            params: Optional query parameters for parameterized queries.
            max_rows: Optional override for max result rows (clamped to 1-1000).
            timeout_seconds: Optional override for query timeout in seconds (1-120).

        Returns:
            QueryResult containing columns, JSON-serializable rows, count, and truncation flag.

        Raises:
            UnsafeQueryError: If query fails AST read-only validation or is multi-statement.
            QueryTimeoutError: If execution exceeds the specified timeout.
            QueryExecutionError: If database execution fails.
        """
        # Step 1: Baseline AST read-only verification (fails closed)
        validate_read_only_sql(sql)

        # Step 2: Determine effective limits
        limit = max_rows if max_rows is not None else self.default_max_rows
        # Enforce configuration bounds [1, 1000]
        limit = max(1, min(limit, 1000))

        timeout = timeout_seconds if timeout_seconds is not None else self.default_timeout_seconds
        timeout = max(1, min(timeout, 120))

        # Step 3: Execute within verified connection context
        with get_connection(self.engine, settings=self.settings, expected_db=ALLOWED_TARGET_DATABASE) as conn:
            # Set timeout on underlying DBAPI connection (pyodbc Connection.timeout)
            raw_conn = getattr(conn.connection, "dbapi_connection", None)
            if raw_conn and hasattr(raw_conn, "timeout"):
                raw_conn.timeout = timeout

            start_time = time.perf_counter()

            try:
                cursor_result = conn.execution_options(
                    timeout=timeout,
                    yield_per=limit,
                ).execute(text(sql), params or {})

                columns = list(cursor_result.keys())

                # Bounded fetch: only fetch up to limit + 1 rows to detect truncation
                # without buffering entire unbounded result sets in Python memory
                raw_rows = cursor_result.fetchmany(limit + 1)
                truncated = len(raw_rows) > limit
                if truncated:
                    raw_rows = raw_rows[:limit]

                cursor_result.close()
                elapsed_ms = (time.perf_counter() - start_time) * 1000.0

            except OperationalError as exc:
                elapsed_ms = (time.perf_counter() - start_time) * 1000.0
                err_msg = str(exc).lower()
                if "timeout" in err_msg or "hyt00" in err_msg:
                    raise QueryTimeoutError(
                        f"Query execution timed out after {timeout} seconds."
                    ) from exc
                raise QueryExecutionError(f"Database operational error: {exc}") from exc

            except (SQLAlchemyError, DBAPIError) as exc:
                elapsed_ms = (time.perf_counter() - start_time) * 1000.0
                # Do NOT log secrets or parameters
                raise QueryExecutionError(f"Database query execution failed: {exc}") from exc

        # Step 4: Serialize rows into JSON-safe dictionaries preserving column order
        formatted_rows: List[Dict[str, Any]] = []
        for row in raw_rows:
            formatted_rows.append(
                {col: serialize_value(val) for col, val in zip(columns, row)}
            )

        return QueryResult(
            columns=columns,
            rows=formatted_rows,
            row_count=len(formatted_rows),
            truncated=truncated,
            execution_time_ms=round(elapsed_ms, 2),
        )


def execute_read_only(
    sql: str,
    params: Optional[Dict[str, Any]] = None,
    max_rows: Optional[int] = None,
    timeout_seconds: Optional[int] = None,
    engine: Optional[Engine] = None,
    settings: Optional[DatabaseSettings] = None,
) -> QueryResult:
    """Public convenience function to execute a read-only query against GradeSense_Local.

    Args:
        sql: The read-only SQL query to execute.
        params: Optional query parameters.
        max_rows: Maximum rows to return (default: 100).
        timeout_seconds: Maximum execution time in seconds (default: 15).
        engine: Optional SQLAlchemy engine.
        settings: Optional DatabaseSettings.

    Returns:
        QueryResult dataclass.
    """
    executor = ReadOnlyQueryExecutor(
        engine=engine,
        settings=settings,
        default_max_rows=max_rows,
        default_timeout_seconds=timeout_seconds,
    )
    return executor.execute(
        sql=sql,
        params=params,
        max_rows=max_rows,
        timeout_seconds=timeout_seconds,
    )
