"""Deterministic SQL validation and policy enforcement layer.

Parses SQL statements using sqlglot, verifies single-statement read-only execution,
enforces table and column access against model-facing schema metadata, blocks
sensitive credentials (PasswordHash), and checks row limits. Never executes SQL.
"""

import logging
from typing import Dict, List, Optional, Set
import sqlglot
from sqlglot import exp

from query_pilot.config import DatabaseSettings, get_db_settings
from query_pilot.db.introspection import SensitiveColumnPolicy
from query_pilot.db.models import DatabaseSchema, TableMetadata
from query_pilot.sql.models import (
    ValidationReason,
    ValidationReasonCode,
    ValidationResult,
)

logger = logging.getLogger(__name__)

# Forbidden AST node types that indicate data/schema mutations or privileged commands
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


class SQLValidator:
    """Deterministic validator enforcing security and schema policy on SQL queries."""

    def __init__(
        self,
        schema: DatabaseSchema,
        settings: Optional[DatabaseSettings] = None,
        policy: Optional[SensitiveColumnPolicy] = None,
        max_rows: Optional[int] = None,
    ):
        """Initialize the validator with model-facing schema and configuration.

        Args:
            schema: Model-facing DatabaseSchema (must redact sensitive columns).
            settings: DatabaseSettings for defaults (defaults to cached settings).
            policy: SensitiveColumnPolicy for credential detection.
            max_rows: Maximum allowed rows for explicit LIMIT/TOP checks.
        """
        self.schema = schema
        self.settings = settings or get_db_settings()
        self.policy = policy or SensitiveColumnPolicy()
        self.max_rows = max_rows if max_rows is not None else self.settings.DB_MAX_ROW_LIMIT

    def validate(self, sql: str) -> ValidationResult:
        """Inspect and validate a SQL query deterministically without executing it.

        Args:
            sql: SQL statement string to evaluate.

        Returns:
            ValidationResult containing allowed status, reasons, and referenced identifiers.
        """
        # 1. Empty SQL Check
        if not sql or not sql.strip():
            return ValidationResult(
                allowed=False,
                reasons=[
                    ValidationReason(
                        code=ValidationReasonCode.EMPTY_SQL,
                        message="SQL query cannot be empty or whitespace.",
                    )
                ],
            )

        # 2. Syntax & Parsing via sqlglot
        try:
            parsed = sqlglot.parse(sql, read="tsql")
        except Exception as exc:
            return ValidationResult(
                allowed=False,
                reasons=[
                    ValidationReason(
                        code=ValidationReasonCode.PARSE_ERROR,
                        message=f"SQL syntax parsing failed: {exc}",
                    )
                ],
            )

        # 3. Single-Statement Enforcement
        statements = [s for s in parsed if s is not None and not isinstance(s, exp.Semicolon)]
        if len(statements) == 0:
            return ValidationResult(
                allowed=False,
                reasons=[
                    ValidationReason(
                        code=ValidationReasonCode.EMPTY_SQL,
                        message="No executable statement found in input.",
                    )
                ],
            )
        if len(statements) > 1:
            return ValidationResult(
                allowed=False,
                reasons=[
                    ValidationReason(
                        code=ValidationReasonCode.MULTI_STATEMENT,
                        message=f"Multi-statement queries are forbidden. Found {len(statements)} statements.",
                    )
                ],
            )

        stmt = statements[0]

        # 4. Read-Only Query Type Verification
        if not isinstance(stmt, exp.Query):
            return ValidationResult(
                allowed=False,
                reasons=[
                    ValidationReason(
                        code=ValidationReasonCode.NOT_READ_ONLY,
                        message=f"Only read-only queries (SELECT) are permitted. Received: {type(stmt).__name__}.",
                    )
                ],
            )

        # Check for any forbidden mutating AST nodes anywhere in the tree
        for node_type in FORBIDDEN_AST_NODES:
            found = stmt.find(node_type)
            if found is not None:
                return ValidationResult(
                    allowed=False,
                    reasons=[
                        ValidationReason(
                            code=ValidationReasonCode.FORBIDDEN_OPERATION,
                            message=f"Forbidden mutating operation detected: {node_type.__name__}.",
                        )
                    ],
                )

        # 5. Resource Policy / Explicit Row Limit Checks
        reasons: List[ValidationReason] = []
        warnings: List[str] = []

        # Check exp.Limit nodes (covers TOP and LIMIT in T-SQL)
        for limit_node in stmt.find_all(exp.Limit):
            opts = limit_node.args.get("limit_options")
            if opts and getattr(opts, "args", {}).get("percent"):
                reasons.append(
                    ValidationReason(
                        code=ValidationReasonCode.UNSUPPORTED_QUERY_SHAPE,
                        message="Percentage-based row limits (TOP ... PERCENT) are not permitted.",
                    )
                )
                break

            expr = limit_node.expression
            if expr is not None and hasattr(expr, "this"):
                try:
                    val = int(str(expr.this))
                    if val > self.max_rows:
                        reasons.append(
                            ValidationReason(
                                code=ValidationReasonCode.EXCEEDS_MAX_ROWS,
                                message=f"Explicit row limit ({val}) exceeds configured maximum allowed rows ({self.max_rows}).",
                            )
                        )
                        break
                except (ValueError, TypeError):
                    pass

        # Check exp.Fetch nodes (OFFSET ... FETCH NEXT N ROWS ONLY)
        for fetch_node in stmt.find_all(exp.Fetch):
            count_node = fetch_node.args.get("count")
            if count_node is not None and hasattr(count_node, "this"):
                try:
                    val = int(str(count_node.this))
                    if val > self.max_rows:
                        reasons.append(
                            ValidationReason(
                                code=ValidationReasonCode.EXCEEDS_MAX_ROWS,
                                message=f"Explicit row limit ({val}) exceeds configured maximum allowed rows ({self.max_rows}).",
                            )
                        )
                        break
                except (ValueError, TypeError):
                    pass

        if reasons:
            return ValidationResult(allowed=False, reasons=reasons)

        # 6. Table Resolution against Model-Facing Schema
        cte_names = {cte.alias.lower() for cte in stmt.find_all(exp.CTE) if cte.alias}
        known_tables = {t.table_name.lower(): t for t in self.schema.tables}

        referenced_tables: Set[str] = set()
        alias_to_table: Dict[str, Optional[TableMetadata]] = {}

        # Derived table / subquery aliases
        for subq in stmt.find_all(exp.Subquery):
            if subq.alias:
                alias_to_table[subq.alias.lower()] = None

        for table in stmt.find_all(exp.Table):
            tbl_name = table.name
            if not tbl_name:
                continue
            tbl_lower = tbl_name.lower()

            # Skip CTE references
            if tbl_lower in cte_names:
                if table.alias:
                    alias_to_table[table.alias.lower()] = None
                alias_to_table[tbl_lower] = None
                continue

            # Reject unauthorized foreign catalogs
            if table.catalog:
                if table.catalog.lower() != self.schema.database_name.lower():
                    reasons.append(
                        ValidationReason(
                            code=ValidationReasonCode.FORBIDDEN_TABLE,
                            message=f"Access to external catalog '{table.catalog}' is prohibited.",
                        )
                    )
                    continue

            # Match against known schema tables
            matched_table: Optional[TableMetadata] = None
            if table.db:
                # Schema-qualified: e.g. dbo.Students
                for t in self.schema.tables:
                    if t.schema_name.lower() == table.db.lower() and t.table_name.lower() == tbl_lower:
                        matched_table = t
                        break
            else:
                matched_table = known_tables.get(tbl_lower)

            if not matched_table:
                reasons.append(
                    ValidationReason(
                        code=ValidationReasonCode.UNKNOWN_TABLE,
                        message=f"Table '{tbl_name}' does not exist in the database schema.",
                    )
                )
                continue

            referenced_tables.add(matched_table.table_name)
            alias_to_table[matched_table.table_name.lower()] = matched_table
            if table.alias:
                alias_to_table[table.alias.lower()] = matched_table

        if reasons:
            return ValidationResult(
                allowed=False,
                reasons=reasons,
                referenced_tables=sorted(list(referenced_tables)),
            )

        # 7. Column Resolution & Sensitive Column Policy
        select_aliases = {
            alias_node.alias.lower()
            for alias_node in stmt.find_all(exp.Alias)
            if alias_node.alias
        }
        referenced_columns: Set[str] = set()

        for col in stmt.find_all(exp.Column):
            # Skip wildcards: e.g. s.* or *
            if isinstance(col.this, exp.Star):
                continue

            col_name = col.name
            if not col_name:
                continue
            col_lower = col_name.lower()

            # Sensitive Column Check
            if self.policy.is_sensitive(col_name):
                reasons.append(
                    ValidationReason(
                        code=ValidationReasonCode.SENSITIVE_COLUMN,
                        message=f"Access to sensitive column '{col_name}' is strictly prohibited.",
                    )
                )
                continue

            # Output column alias reference (e.g. in ORDER BY, HAVING)
            if not col.table and col_lower in select_aliases:
                continue

            # Qualified Column Reference: e.g. s.EnrollmentNumber
            if col.table:
                table_ref = col.table.lower()
                if table_ref in alias_to_table:
                    target_tbl = alias_to_table[table_ref]
                    if target_tbl is None:
                        # Column references a CTE or derived subquery
                        referenced_columns.add(col_name)
                        continue

                    # Verify column exists on matched physical table
                    if not target_tbl.get_column(col_name):
                        reasons.append(
                            ValidationReason(
                                code=ValidationReasonCode.UNKNOWN_COLUMN,
                                message=f"Column '{col_name}' does not exist on table '{target_tbl.table_name}'.",
                            )
                        )
                        continue
                    referenced_columns.add(f"{target_tbl.table_name}.{col_name}")
                else:
                    reasons.append(
                        ValidationReason(
                            code=ValidationReasonCode.UNKNOWN_TABLE,
                            message=f"Unknown table alias or identifier '{col.table}' for column '{col_name}'.",
                        )
                    )
                    continue
            else:
                # Unqualified Column Reference: e.g. EnrollmentNumber
                has_derived_source = None in alias_to_table.values()
                physical_tables = [t for t in alias_to_table.values() if t is not None]

                matched_any = False
                for tbl in physical_tables:
                    if tbl.get_column(col_name):
                        matched_any = True
                        referenced_columns.add(f"{tbl.table_name}.{col_name}")

                if not matched_any and not has_derived_source and physical_tables:
                    reasons.append(
                        ValidationReason(
                            code=ValidationReasonCode.UNKNOWN_COLUMN,
                            message=f"Column '{col_name}' does not exist in any referenced table.",
                        )
                    )
                elif matched_any:
                    pass
                else:
                    referenced_columns.add(col_name)

        if reasons:
            return ValidationResult(
                allowed=False,
                reasons=reasons,
                referenced_tables=sorted(list(referenced_tables)),
                referenced_columns=sorted(list(referenced_columns)),
                warnings=warnings,
            )

        # 8. Deterministic Normalization
        try:
            normalized_sql = stmt.sql(dialect="tsql")
        except Exception:
            normalized_sql = sql.strip()

        return ValidationResult(
            allowed=True,
            normalized_sql=normalized_sql,
            referenced_tables=sorted(list(referenced_tables)),
            referenced_columns=sorted(list(referenced_columns)),
            warnings=warnings,
        )


def validate_sql(
    sql: str,
    schema: DatabaseSchema,
    settings: Optional[DatabaseSettings] = None,
    policy: Optional[SensitiveColumnPolicy] = None,
    max_rows: Optional[int] = None,
) -> ValidationResult:
    """Validate a SQL query against the model-facing schema and security policy.

    Args:
        sql: The SQL string to validate.
        schema: Model-facing DatabaseSchema.
        settings: Optional DatabaseSettings.
        policy: Optional SensitiveColumnPolicy.
        max_rows: Optional row limit override.

    Returns:
        ValidationResult with allowed status, reasons, and referenced objects.
    """
    validator = SQLValidator(
        schema=schema,
        settings=settings,
        policy=policy,
        max_rows=max_rows,
    )
    return validator.validate(sql)
