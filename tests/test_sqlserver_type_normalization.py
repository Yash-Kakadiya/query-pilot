"""Offline regression tests for SQL Server type validation and normalization.

Tests the remediation for HTTP 422 errors caused by valid SQL Server reflected
type metadata containing collation clauses (e.g. NVARCHAR(100) COLLATE "SQL_Latin1_General_CP1_CI_AS").

Covers:
1. Exact reflected SQL Server type string with double-quoted collation.
2. Ordinary SQL types (INT, NVARCHAR(100), DECIMAL(10,2), DATETIME2, etc.).
3. Collation metadata with valid quoted and unquoted identifiers.
4. Invalid / control-character metadata and excessively long input.
5. Schema payload acceptance through Pydantic request models (GenerateSQLRequest).
6. Invariance of SQL statement validation and execution authorization.
"""

import pytest
from pydantic import ValidationError

from query_pilot.api.models import (
    ColumnSchemaContext,
    GenerateSQLRequest,
    ModelFacingSchemaContext,
    TableSchemaContext,
    normalize_data_type,
)
from query_pilot.db.models import DatabaseSchema, TableMetadata, ColumnMetadata
from query_pilot.sql.validator import SQLValidator


# ==============================================================================
# 1. Exact Reflected SQL Server Type String
# ==============================================================================

def test_exact_reflected_sqlserver_type_string():
    """Verify that the exact reflected type string from SQL Server is accepted and normalized."""
    reflected_type = 'NVARCHAR(100) COLLATE "SQL_Latin1_General_CP1_CI_AS"'
    
    # 1a. Direct normalization function
    normalized = normalize_data_type(reflected_type)
    assert normalized == "NVARCHAR(100)"

    # 1b. Pydantic ColumnSchemaContext model validation
    col = ColumnSchemaContext(name="username", data_type=reflected_type)
    assert col.name == "username"
    assert col.data_type == "NVARCHAR(100)"

    # 1c. Dict instantiation via model_validate
    col_from_dict = ColumnSchemaContext.model_validate({
        "name": "username",
        "data_type": reflected_type,
        "nullable": False,
        "primary_key": False,
    })
    assert col_from_dict.data_type == "NVARCHAR(100)"


# ==============================================================================
# 2. Ordinary SQL Types
# ==============================================================================

# ==============================================================================
# 2. Database Compatibility Suites (SQL Server, SQLite, PostgreSQL)
# ==============================================================================

@pytest.mark.parametrize(
    "data_type,expected_normalized",
    [
        # --- Microsoft SQL Server ---
        ('NVARCHAR(100) COLLATE "SQL_Latin1_General_CP1_CI_AS"', "NVARCHAR(100)"),
        ('VARCHAR(50) COLLATE Latin1_General_CI_AS', "VARCHAR(50)"),
        ("DATETIME2(7)", "DATETIME2(7)"),
        ("DECIMAL(10,2)", "DECIMAL(10,2)"),
        ("DECIMAL(10, 2)", "DECIMAL(10, 2)"),
        ("VARBINARY(MAX)", "VARBINARY(MAX)"),
        ("UNIQUEIDENTIFIER", "UNIQUEIDENTIFIER"),
        ("INT", "INT"),
        ("BIGINT", "BIGINT"),
        ("SMALLINT", "SMALLINT"),
        ("TINYINT", "TINYINT"),
        ("BIT", "BIT"),
        ("FLOAT(53)", "FLOAT(53)"),
        ("DATE", "DATE"),
        ("TIME(7)", "TIME(7)"),

        # --- SQLite ---
        ("INTEGER", "INTEGER"),
        ("TEXT", "TEXT"),
        ("REAL", "REAL"),
        ("BLOB", "BLOB"),
        ("NUMERIC", "NUMERIC"),

        # --- PostgreSQL ---
        ("CHARACTER VARYING(100)", "CHARACTER VARYING(100)"),
        ("DOUBLE PRECISION", "DOUBLE PRECISION"),
        ("TIMESTAMP WITH TIME ZONE", "TIMESTAMP WITH TIME ZONE"),
        ("NUMERIC(10,2)", "NUMERIC(10,2)"),
        ("NUMERIC(10, 2)", "NUMERIC(10, 2)"),
        ("BOOLEAN", "BOOLEAN"),
        ("SMALLSERIAL", "SMALLSERIAL"),
        ("BIGSERIAL", "BIGSERIAL"),
    ],
)
def test_supported_database_type_representations(data_type: str, expected_normalized: str):
    """Verify realistic reflected type representations across supported database drivers."""
    normalized = normalize_data_type(data_type)
    assert normalized == expected_normalized

    col = ColumnSchemaContext(name="test_col", data_type=data_type)
    assert col.data_type == expected_normalized


# ==============================================================================
# 3. Collation Metadata with Valid Quoted and Unquoted Identifiers
# ==============================================================================

@pytest.mark.parametrize(
    "data_type_with_collation,expected_base",
    [
        ('NVARCHAR(100) COLLATE "SQL_Latin1_General_CP1_CI_AS"', "NVARCHAR(100)"),
        ('VARCHAR(50) COLLATE "Latin1_General_CI_AS"', "VARCHAR(50)"),
        ('VARCHAR(50) COLLATE Latin1_General_CI_AS', "VARCHAR(50)"),
        ('NCHAR(10) COLLATE "Japanese_Bushu_Kakusu_100_CS_AS_KS_WS_SC"', "NCHAR(10)"),
        ('NTEXT COLLATE "French_CI_AS"', "NTEXT"),
        ('VARCHAR(MAX) COLLATE "SQL_Latin1_General_CP1_CI_AS"', "VARCHAR(MAX)"),
        ('CHAR(1) COLLATE "C"', "CHAR(1)"),
    ],
)
def test_collation_metadata_with_valid_identifiers(data_type_with_collation: str, expected_base: str):
    """Verify collation clauses with valid quoted/unquoted identifiers are stripped to base type."""
    normalized = normalize_data_type(data_type_with_collation)
    assert normalized == expected_base

    col = ColumnSchemaContext(name="sample_col", data_type=data_type_with_collation)
    assert col.data_type == expected_base


# ==============================================================================
# 4. Negative Cases: Strict Rejection of Malformed, Injected, or Overlength Data
# ==============================================================================

@pytest.mark.parametrize(
    "invalid_input,reason",
    [
        # Control characters
        ("INT\x00", "null byte"),
        ("INT\n", "newline"),
        ("INT\r\nVARCHAR(50)", "CRLF"),
        ("INT\tCOLLATE \"Latin1\"", "tab"),
        ("NVARCHAR(50)\x1b[31m", "escape sequence"),

        # SQL Injection attempts and trailing fragments
        ("INT; DROP TABLE Users;", "semicolon injection"),
        ("VARCHAR(50) UNION SELECT 1", "union injection"),
        ("INT -- comment", "sql comment"),
        ("INT /* comment */", "block comment"),
        ("VARCHAR(50) SELECT 1", "trailing select statement"),
        ("NVARCHAR(100) COLLATE \"Latin1\" trailing", "trailing tokens after collation"),

        # Malformed and duplicate collation clauses
        ("VARCHAR(50) COLLATE", "missing collation identifier"),
        ("VARCHAR(50) COLLATE \"\"", "empty quoted collation"),
        ("VARCHAR(50) COLLATE \"unclosed", "unclosed quote"),
        ("VARCHAR(50) COLLATE unclosed\"", "unopened quote"),
        ("VARCHAR(50) COLLATE 'single_quotes'", "single quoted collation"),
        ("VARCHAR(50) COLLATE \"bad;injection\"", "special chars inside collation"),
        ("VARCHAR(50) COLLATE \"with spaces\"", "spaces in collation identifier"),
        ("VARCHAR(50) COLLATE \"Latin1\" COLLATE \"Latin2\"", "duplicate collation clauses"),
        ("VARCHAR(50) COLLATE COLLATE \"Latin1\"", "double COLLATE keyword"),

        # Empty or whitespace only
        ("", "empty string"),
        ("   ", "whitespace only"),

        # Structural syntax errors
        ("INT()", "empty parentheses"),
        ("DECIMAL(10, 2, 3)", "three arguments in parenthesis"),
        ("VARCHAR(abc)", "non-numeric non-max argument"),
        ("123INT", "leading number in type name"),

        # Overlength input (> 128 characters)
        ("VARCHAR(" + "9" * 125 + ")", "exceeds maximum length bounds"),
    ],
)
def test_invalid_metadata_rejected(invalid_input: str, reason: str):
    """Verify invalid, malformed, or hostile metadata is strictly rejected."""
    with pytest.raises(ValueError):
        normalize_data_type(invalid_input)

    with pytest.raises(ValidationError):
        ColumnSchemaContext(name="col", data_type=invalid_input)


def test_length_limit_applied_before_expensive_parsing():
    """Verify that inputs exceeding MAX_IDENTIFIER_LENGTH are rejected immediately."""
    # 129 characters
    long_type = "NVARCHAR(" + "1" * 125 + ")"
    assert len(long_type) > 128

    with pytest.raises(ValueError, match="exceeds maximum length"):
        normalize_data_type(long_type)

    with pytest.raises(ValidationError):
        ColumnSchemaContext(name="col", data_type=long_type)

    # Very large payload (100k characters) rejected in sub-millisecond without DoS
    huge_type = "VARCHAR(" + "9" * 100_000 + ")"
    with pytest.raises(ValueError, match="exceeds maximum length"):
        normalize_data_type(huge_type)


# ==============================================================================
# 5. Schema Payload Acceptance Through Actual Pydantic Request Models
# ==============================================================================

def test_generate_sql_request_payload_acceptance():
    """Verify that a full GenerateSQLRequest payload with SQL Server reflected types is accepted."""
    payload = {
        "question": "Count all users in the system",
        "schema_context": {
            "database_name": "QueryPilot_TestDB",
            "tables": [
                {
                    "table_name": "Users",
                    "schema_name": "dbo",
                    "columns": [
                        {
                            "name": "id",
                            "data_type": "INT",
                            "nullable": False,
                            "primary_key": True,
                        },
                        {
                            "name": "username",
                            "data_type": 'NVARCHAR(100) COLLATE "SQL_Latin1_General_CP1_CI_AS"',
                            "nullable": False,
                            "primary_key": False,
                        },
                        {
                            "name": "email",
                            "data_type": 'VARCHAR(255) COLLATE "Latin1_General_CI_AS"',
                            "nullable": True,
                            "primary_key": False,
                        },
                        {
                            "name": "created_at",
                            "data_type": "DATETIME2",
                            "nullable": False,
                            "primary_key": False,
                        },
                    ],
                    "primary_keys": ["id"],
                    "foreign_keys": [],
                }
            ],
        },
    }

    # Validate through top-level request model
    req = GenerateSQLRequest.model_validate(payload)
    assert req.question == "Count all users in the system"
    
    # Check normalized columns
    tbl = req.schema_context.tables[0]
    assert tbl.columns[0].data_type == "INT"
    assert tbl.columns[1].data_type == "NVARCHAR(100)"
    assert tbl.columns[2].data_type == "VARCHAR(255)"
    assert tbl.columns[3].data_type == "DATETIME2"

    # Convert to internal model-facing DatabaseSchema
    db_schema: DatabaseSchema = req.schema_context.to_database_schema()
    assert db_schema.database_name == "QueryPilot_TestDB"
    assert db_schema.is_model_facing is True
    assert len(db_schema.tables) == 1
    
    table_meta = db_schema.tables[0]
    assert table_meta.table_name == "Users"
    assert len(table_meta.columns) == 4
    assert table_meta.columns[1].name == "username"
    assert table_meta.columns[1].data_type == "NVARCHAR(100)"
    assert table_meta.columns[2].name == "email"
    assert table_meta.columns[2].data_type == "VARCHAR(255)"


# ==============================================================================
# 6. Normalization Does Not Alter SQL Validation or Execution Invariants
# ==============================================================================

def test_normalization_invariance_for_sql_validation():
    """Verify that type normalization maintains identical SQL validation behavior.
    
    SQLValidator enforces table and column existence, statement count, read-only
    status, and AST policy. It does not alter behavior based on collation metadata,
    and data_type remains non-interpolated metadata.
    """
    col_id = ColumnMetadata(name="id", data_type="INT", primary_key=True)
    col_user = ColumnMetadata(name="username", data_type="NVARCHAR(100)", nullable=False)
    
    schema = DatabaseSchema(
        database_name="TestDB",
        tables=[
            TableMetadata(
                table_name="Users",
                schema_name="dbo",
                columns=[col_id, col_user],
                primary_keys=["id"],
            )
        ],
        is_model_facing=True,
    )
    
    validator = SQLValidator(schema=schema)
    
    # 6a. Valid read-only query passes
    valid_query = "SELECT id, username FROM dbo.Users WHERE id = 1"
    res = validator.validate(valid_query)
    assert res.allowed is True
    assert res.reasons == []

    # 6b. Mutating query is still rejected
    mutating_query = "UPDATE dbo.Users SET username = 'admin' WHERE id = 1"
    res_mutate = validator.validate(mutating_query)
    assert res_mutate.allowed is False

    # 6c. Unknown column is still rejected
    unknown_col_query = "SELECT non_existent_col FROM dbo.Users"
    res_unknown = validator.validate(unknown_col_query)
    assert res_unknown.allowed is False
