"""SQLite security enforcement and authorizer infrastructure.

Provides defense-in-depth security policies for SQLite database connections
in QueryPilot:
1. Candidate execution authorizer: Enforces read-only SELECT execution, denies
   all PRAGMAs, transactions, savepoints, DDL, DML, ATTACH, DETACH, extension loading,
   and restricts access to allowed tables and sensitive columns.
2. Trusted introspection authorizer: Permits read-only schema reflection
   (e.g., SQLAlchemy reflection, PRAGMA table_info, foreign_key_list, index_list)
   while denying mutating PRAGMAs (query_only=OFF, busy_timeout alterations) and
   all DDL/DML.
3. Physical connection separation: Guarantees introspection and execution operate
   on distinct connections and prevents policy-switching on shared connections.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Callable, Iterable, Optional, Set

logger = logging.getLogger(__name__)


class SQLiteSecurityError(Exception):
    """Raised when SQLite connection security configuration or policy is violated."""
    pass


# Allowed scalar, aggregate, window, math, string, and json functions for candidate SQL
ALLOWED_EXECUTION_FUNCTIONS: frozenset[str] = frozenset({
    # Aggregate functions
    "count",
    "sum",
    "total",
    "avg",
    "min",
    "max",
    "group_concat",
    "string_agg",
    # Window functions
    "row_number",
    "rank",
    "dense_rank",
    "percent_rank",
    "cume_dist",
    "ntile",
    "lead",
    "lag",
    "first_value",
    "last_value",
    "nth_value",
    # Scalar math functions
    "abs",
    "round",
    "random",
    "sign",
    "ceil",
    "ceiling",
    "floor",
    "trunc",
    "power",
    "sqrt",
    "mod",
    "ln",
    "log",
    "log2",
    "log10",
    "exp",
    "pow",
    "pi",
    "cos",
    "sin",
    "tan",
    "acos",
    "asin",
    "atan",
    "atan2",
    "radians",
    "degrees",
    # String functions
    "lower",
    "upper",
    "length",
    "trim",
    "ltrim",
    "rtrim",
    "substr",
    "substring",
    "replace",
    "instr",
    "printf",
    "format",
    "concat",
    "concat_ws",
    "quote",
    "glob",
    "like",
    "soundex",
    "hex",
    "unhex",
    "unicode",
    "char",
    # Conditional & Type checking
    "coalesce",
    "ifnull",
    "nullif",
    "iif",
    "typeof",
    # Date & time functions
    "date",
    "time",
    "datetime",
    "julianday",
    "strftime",
    "timediff",
    "unixepoch",
    # JSON functions
    "json",
    "json_array",
    "json_array_length",
    "json_extract",
    "json_insert",
    "json_replace",
    "json_set",
    "json_object",
    "json_type",
    "json_valid",
    "json_quote",
    "json_group_array",
    "json_group_object",
    "json_each",
    "json_tree",
})

# Whitelisted PRAGMAs permitted solely for trusted schema introspection
ALLOWED_INTROSPECTION_PRAGMAS: frozenset[str] = frozenset({
    "table_info",
    "table_xinfo",
    "database_list",
    "foreign_key_list",
    "index_list",
    "index_info",
    "index_xinfo",
    "collation_list",
})

# Explicitly prohibited mutating / privileged action codes
FORBIDDEN_MUTATING_ACTIONS: frozenset[int] = frozenset({
    sqlite3.SQLITE_CREATE_INDEX,
    sqlite3.SQLITE_CREATE_TABLE,
    sqlite3.SQLITE_CREATE_TEMP_INDEX,
    sqlite3.SQLITE_CREATE_TEMP_TABLE,
    sqlite3.SQLITE_CREATE_TEMP_TRIGGER,
    sqlite3.SQLITE_CREATE_TEMP_VIEW,
    sqlite3.SQLITE_CREATE_TRIGGER,
    sqlite3.SQLITE_CREATE_VIEW,
    sqlite3.SQLITE_CREATE_VTABLE,
    sqlite3.SQLITE_DELETE,
    sqlite3.SQLITE_DROP_INDEX,
    sqlite3.SQLITE_DROP_TABLE,
    sqlite3.SQLITE_DROP_TEMP_INDEX,
    sqlite3.SQLITE_DROP_TEMP_TABLE,
    sqlite3.SQLITE_DROP_TEMP_TRIGGER,
    sqlite3.SQLITE_DROP_TEMP_VIEW,
    sqlite3.SQLITE_DROP_TRIGGER,
    sqlite3.SQLITE_DROP_VIEW,
    sqlite3.SQLITE_DROP_VTABLE,
    sqlite3.SQLITE_INSERT,
    sqlite3.SQLITE_UPDATE,
    sqlite3.SQLITE_ALTER_TABLE,
    sqlite3.SQLITE_REINDEX,
    sqlite3.SQLITE_ANALYZE,
    sqlite3.SQLITE_ATTACH,
    sqlite3.SQLITE_DETACH,
})

# Connection-scoped security tag function name bound to the underlying SQLite C handle
SECURITY_TAG_FUNCTION = "__query_pilot_security_mode__"
VERIFIED_PATH_FUNCTION = "__query_pilot_verified_path__"


def reset_connection_security_registry() -> None:
    """No-op cleanup utility maintained for test fixture compatibility.

    Physical connection security modes are bound directly to the live SQLite C connection handle
    via connection-scoped functions, eliminating memory leaks and stale identity reuse in Python dicts.
    """
    pass


def get_connection_security_mode(conn: sqlite3.Connection) -> Optional[str]:
    """Retrieve the currently configured security mode for a connection, if any.

    Queries the connection-scoped security function bound to the underlying C handle.
    Returns None if the connection is unconfigured or closed.
    """
    try:
        cursor = conn.cursor()
        try:
            cursor.execute(f"SELECT {SECURITY_TAG_FUNCTION}()")
            row = cursor.fetchone()
            return row[0] if row else None
        finally:
            cursor.close()
    except (sqlite3.OperationalError, sqlite3.ProgrammingError, sqlite3.DatabaseError):
        return None


def create_execution_authorizer(
    allowed_tables: Optional[Iterable[str]] = None,
    sensitive_columns: Optional[Iterable[str]] = None,
    deny_sensitive: bool = True,
) -> Callable[[int, Optional[str], Optional[str], Optional[str], Optional[str]], int]:
    """Create an authorizer callback enforcing strict read-only execution policy for candidate SQL.

    Policy:
    - Allows `SQLITE_SELECT` and `SQLITE_RECURSIVE`.
    - Allows `SQLITE_READ` on unredacted columns of allowed tables.
    - Allows `SQLITE_FUNCTION` only for functions in `ALLOWED_EXECUTION_FUNCTIONS` (checked via `p2`).
    - Denies all `SQLITE_PRAGMA`.
    - Denies `SQLITE_TRANSACTION` and `SQLITE_SAVEPOINT`.
    - Denies all DDL, DML, ATTACH, DETACH, REINDEX, ANALYZE.
    - Denies sensitive columns (e.g., PasswordHash, tokens).
    - Default-deny on all unrecognized action codes.

    Args:
        allowed_tables: Optional set of allowed table names (case-insensitive).
        sensitive_columns: Optional set of sensitive column names or 'table.column' (case-insensitive).
        deny_sensitive: If True, returns SQLITE_DENY on sensitive column access. If False, SQLITE_IGNORE.

    Returns:
        Authorizer callback suitable for `sqlite3.Connection.set_authorizer`.
    """
    allowed_tables_set: Optional[frozenset[str]] = (
        frozenset(t.strip().lower() for t in allowed_tables) if allowed_tables is not None else None
    )
    sensitive_columns_set: frozenset[str] = (
        frozenset(c.strip().lower() for c in sensitive_columns) if sensitive_columns is not None else frozenset()
    )

    sensitive_response = sqlite3.SQLITE_DENY if deny_sensitive else sqlite3.SQLITE_IGNORE

    def authorizer(
        action: int,
        p1: Optional[str],
        p2: Optional[str],
        dbname: Optional[str],
        source: Optional[str],
    ) -> int:
        # 1. SELECT and RECURSIVE (CTEs)
        if action == sqlite3.SQLITE_SELECT or action == sqlite3.SQLITE_RECURSIVE:
            return sqlite3.SQLITE_OK

        # 2. Column / Table Reading
        if action == sqlite3.SQLITE_READ:
            # p1 = table name, p2 = column name (or empty string for table-level read / COUNT(*))
            tbl_name = (p1 or "").strip().lower()
            col_name = (p2 or "").strip().lower()

            # Check sensitive column access
            if col_name and sensitive_columns_set:
                qualified_col = f"{tbl_name}.{col_name}" if tbl_name else col_name
                if col_name in sensitive_columns_set or qualified_col in sensitive_columns_set:
                    logger.warning("Denied sensitive column access: table='%s', column='%s'", p1, p2)
                    return sensitive_response

            # Check allowed tables access
            if allowed_tables_set is not None and tbl_name:
                # Allow internal sqlite CTEs/aliases and schema if not blocked
                if tbl_name not in allowed_tables_set and not tbl_name.startswith("sqlite_"):
                    logger.warning("Denied unapproved table access: table='%s'", p1)
                    return sqlite3.SQLITE_DENY

            return sqlite3.SQLITE_OK

        # 3. Whitelisted SQL Functions
        if action == sqlite3.SQLITE_FUNCTION:
            # In Python's sqlite3 driver, p2 contains the function name
            func_name = (p2 or "").strip().lower()
            if func_name in (SECURITY_TAG_FUNCTION, VERIFIED_PATH_FUNCTION) or func_name in ALLOWED_EXECUTION_FUNCTIONS:
                return sqlite3.SQLITE_OK
            logger.warning("Denied unauthorized SQLite function: '%s'", p2)
            return sqlite3.SQLITE_DENY

        # 4. Strictly Prohibited Operations (Candidate PRAGMAs, Transactions, Savepoints, DDL, DML)
        if action == sqlite3.SQLITE_PRAGMA:
            logger.warning("Denied candidate PRAGMA execution: pragma='%s', arg='%s'", p1, p2)
            return sqlite3.SQLITE_DENY

        if action in (sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT):
            logger.warning("Denied candidate transaction/savepoint control: action=%d, p1='%s'", action, p1)
            return sqlite3.SQLITE_DENY

        if action in FORBIDDEN_MUTATING_ACTIONS:
            logger.warning("Denied candidate mutating action: action=%d, p1='%s'", action, p1)
            return sqlite3.SQLITE_DENY

        # 5. Default-Deny on any other / unrecognized action codes
        logger.warning("Denied unrecognized SQLite authorizer action: action=%d, p1='%s', p2='%s'", action, p1, p2)
        return sqlite3.SQLITE_DENY

    return authorizer


def create_introspection_authorizer(
    allowed_tables: Optional[Iterable[str]] = None,
) -> Callable[[int, Optional[str], Optional[str], Optional[str], Optional[str]], int]:
    """Create an authorizer callback for trusted schema introspection.

    Policy:
    - Allows `SQLITE_SELECT` and `SQLITE_READ` for querying metadata catalogs.
    - Allows whitelisted schema PRAGMAs (`ALLOWED_INTROSPECTION_PRAGMAS`) with validated arguments.
    - Allows safe helper functions needed by SQLAlchemy inspection (`like`, `glob`, etc.).
    - Denies all modifying PRAGMAs (e.g. `query_only=OFF`, `busy_timeout` alterations, `writable_schema`).
    - Denies candidate PRAGMAs, transactions, savepoints, DDL, DML, ATTACH, DETACH.
    - Default-deny on all unrecognized action codes.

    Args:
        allowed_tables: Optional set of allowed table names (case-insensitive).

    Returns:
        Authorizer callback suitable for `sqlite3.Connection.set_authorizer`.
    """
    allowed_tables_set: Optional[frozenset[str]] = (
        frozenset(t.strip().lower() for t in allowed_tables) if allowed_tables is not None else None
    )

    def authorizer(
        action: int,
        p1: Optional[str],
        p2: Optional[str],
        dbname: Optional[str],
        source: Optional[str],
    ) -> int:
        # 1. Catalog SELECT
        if action == sqlite3.SQLITE_SELECT:
            return sqlite3.SQLITE_OK

        # 2. Reading Catalog & Tables
        if action == sqlite3.SQLITE_READ:
            # Metadata inspection reads sqlite_master/sqlite_temp_master/sqlite_schema
            tbl_name = (p1 or "").strip().lower()
            if allowed_tables_set is not None and tbl_name:
                if tbl_name not in allowed_tables_set and not tbl_name.startswith("sqlite_"):
                    return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        # 3. Helper Functions for Inspection
        if action == sqlite3.SQLITE_FUNCTION:
            func_name = (p2 or "").strip().lower()
            if func_name in (SECURITY_TAG_FUNCTION, VERIFIED_PATH_FUNCTION) or func_name in ALLOWED_EXECUTION_FUNCTIONS or func_name in {"like", "glob", "lower", "upper", "length"}:
                return sqlite3.SQLITE_OK
            logger.warning("Denied unauthorized introspection function: '%s'", p2)
            return sqlite3.SQLITE_DENY

        # 4. Whitelisted Schema PRAGMAs
        if action == sqlite3.SQLITE_PRAGMA:
            pragma_name = (p1 or "").strip().lower()
            if pragma_name in ALLOWED_INTROSPECTION_PRAGMAS:
                # If an argument is provided (e.g., table_info('table_name')), validate it
                if p2 is not None:
                    arg_clean = p2.strip()
                    # Validate identifier safety: must not contain quotes, semicolons, or control chars
                    if not (arg_clean.isidentifier() or arg_clean.replace(" ", "_").isidentifier()):
                        logger.warning("Denied introspection PRAGMA with invalid argument: '%s'", p2)
                        return sqlite3.SQLITE_DENY

                    if allowed_tables_set is not None and pragma_name in (
                        "table_info",
                        "table_xinfo",
                        "foreign_key_list",
                        "index_list",
                    ):
                        arg_lower = arg_clean.lower()
                        if arg_lower not in allowed_tables_set and not arg_lower.startswith("sqlite_"):
                            logger.warning("Denied introspection PRAGMA for unapproved table: '%s'", p2)
                            return sqlite3.SQLITE_DENY

                return sqlite3.SQLITE_OK

            # Any other PRAGMA (query_only, busy_timeout, writable_schema, etc.) is denied
            logger.warning("Denied unauthorized introspection PRAGMA: '%s'", p1)
            return sqlite3.SQLITE_DENY

        # 5. Transactions, Savepoints, Mutations, and Unknown Actions are Denied
        if action in (sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT):
            return sqlite3.SQLITE_DENY

        if action in FORBIDDEN_MUTATING_ACTIONS:
            return sqlite3.SQLITE_DENY

        logger.warning("Denied unrecognized introspection authorizer action: action=%d, p1='%s'", action, p1)
        return sqlite3.SQLITE_DENY

    return authorizer


def configure_sqlite_connection(
    conn: sqlite3.Connection,
    mode: str = "execution",
    allowed_tables: Optional[Iterable[str]] = None,
    sensitive_columns: Optional[Iterable[str]] = None,
    deny_sensitive: bool = True,
    busy_timeout_ms: int = 5000,
    expected_canonical_path: Optional[str] = None,
) -> sqlite3.Connection:
    """Configure defense-in-depth security pragmas and authorizers on a SQLite connection.

    Mandates physical connection separation: raises SQLiteSecurityError if a connection
    previously configured for one mode is reconfigured for another mode.

    Args:
        conn: The raw sqlite3.Connection to configure.
        mode: Connection security mode: 'execution' (candidate SQL) or 'introspection' (schema discovery).
        allowed_tables: Optional set of allowed table names.
        sensitive_columns: Optional set of sensitive columns to deny/ignore (execution mode).
        deny_sensitive: If True, sensitive columns raise SQLITE_DENY; if False, SQLITE_IGNORE.
        busy_timeout_ms: Timeout in milliseconds for busy database locks (default 5000ms).
        expected_canonical_path: Optional expected canonical filesystem path to verify via PRAGMA database_list.

    Returns:
        The configured sqlite3.Connection.

    Raises:
        SQLiteSecurityError: If mode is invalid, if unexpected attached databases exist,
                             or if attempting to switch policies on a shared connection.
    """
    normalized_mode = (mode or "").strip().lower()
    if normalized_mode not in ("execution", "introspection"):
        raise SQLiteSecurityError(
            f"Invalid SQLite security mode '{mode}'. Must be 'execution' or 'introspection'."
        )

    # Physical connection separation enforcement
    existing_mode = get_connection_security_mode(conn)
    if existing_mode is not None:
        if existing_mode != normalized_mode:
            raise SQLiteSecurityError(
                f"Physical connection separation violation: connection already configured for '{existing_mode}', "
                f"cannot reconfigure for '{normalized_mode}'. Introspection and execution must use separate physical connections."
            )
        # Connection is already configured for this exact security mode
        return conn

    # Register the immutable security tag function on the physical connection handle
    conn.create_function(SECURITY_TAG_FUNCTION, 0, lambda: normalized_mode)

    try:
        # 1. Apply core safety PRAGMAs BEFORE attaching authorizer
        cursor = conn.cursor()
        try:
            cursor.execute("PRAGMA query_only = ON;")
            cursor.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)};")

            # Verify database identity and ensure no attached external databases
            if expected_canonical_path:
                import os
                cursor.execute("PRAGMA database_list;")
                rows = cursor.fetchall()
                if len(rows) != 1 or rows[0][1] != "main":
                    raise SQLiteSecurityError(
                        f"SECURITY VIOLATION: Unexpected SQLite attached databases: {rows}. "
                        "QueryPilot requires strictly a single main database."
                    )
                actual_file = rows[0][2]
                if not actual_file or os.path.normcase(os.path.abspath(actual_file)) != os.path.normcase(os.path.abspath(expected_canonical_path)):
                    raise SQLiteSecurityError(
                        f"SECURITY VIOLATION: Connected SQLite database '{actual_file}' does not match "
                        f"expected canonical path '{expected_canonical_path}'."
                    )
                actual_abs = os.path.abspath(actual_file)
                conn.create_function(VERIFIED_PATH_FUNCTION, 0, lambda: actual_abs)
        finally:
            cursor.close()

        # 2. Attach specialized authorizer
        if normalized_mode == "execution":
            authorizer = create_execution_authorizer(
                allowed_tables=allowed_tables,
                sensitive_columns=sensitive_columns,
                deny_sensitive=deny_sensitive,
            )
        else:
            authorizer = create_introspection_authorizer(
                allowed_tables=allowed_tables,
            )

        conn.set_authorizer(authorizer)
    except Exception:
        # Clean up registered tag on failure
        try:
            conn.create_function(SECURITY_TAG_FUNCTION, 0, None)
            conn.set_authorizer(None)
        except Exception:
            pass
        raise

    return conn
