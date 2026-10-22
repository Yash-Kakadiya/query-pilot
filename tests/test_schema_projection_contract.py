"""Offline schema contract verification tests between Frontend projection and Python Pydantic models.

Task 1.34-B1-F28-A9-B1-R1 — Schema Contract Verification.

This test suite executes the actual TypeScript projection function
(frontend/src/services/schemaProjection.ts:projectToModelFacingSchema) via Node.js
and validates the resulting JSON payload through the backend Pydantic models:
- ModelFacingSchemaContext
- TableSchemaContext
- ColumnSchemaContext
- ForeignKeySchemaContext
- GenerateSQLRequest

No live services, databases, Gemini, LangSmith, or Render are invoked.
"""

import json
from pathlib import Path
import subprocess
from typing import Any, Dict

import pytest
from pydantic import ValidationError

from query_pilot.api.models import (
    ColumnSchemaContext,
    ForeignKeySchemaContext,
    GenerateSQLRequest,
    ModelFacingSchemaContext,
    TableSchemaContext,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_SERVICE_PATH = REPO_ROOT / "frontend" / "src" / "services" / "schemaProjection.ts"


def run_ts_projection(raw_data: Any) -> Dict[str, Any]:
    """Execute the pure TypeScript projectToModelFacingSchema function using Node.js."""
    script = f"""
    import {{ projectToModelFacingSchema }} from '{FRONTEND_SERVICE_PATH.as_uri()}';
    const input = {json.dumps(raw_data)};
    const projected = projectToModelFacingSchema(input);
    console.log(JSON.stringify(projected));
    """
    proc = subprocess.run(
        ["node", "--experimental-strip-types", "-e", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"TypeScript projection failed: {proc.stderr.strip()}")
    return json.loads(proc.stdout)


def assert_ts_projection_error(raw_data: Any, expected_match: str) -> None:
    """Verify that TypeScript projection throws SchemaProjectionError with expected message."""
    script = f"""
    import {{ projectToModelFacingSchema }} from '{FRONTEND_SERVICE_PATH.as_uri()}';
    const input = {json.dumps(raw_data)};
    projectToModelFacingSchema(input);
    """
    proc = subprocess.run(
        ["node", "--experimental-strip-types", "-e", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0, "Expected TypeScript projection to fail, but it succeeded."
    assert "SchemaProjectionError" in proc.stderr
    assert expected_match.lower() in proc.stderr.lower()


# ==============================================================================
# 1. Valid Comprehensive Schema Test
# ==============================================================================

def test_contract_valid_comprehensive_schema():
    """Verify a complete raw introspected schema is projected and accepted by Pydantic."""
    raw_schema = {
        "database_name": "EnterpriseDW",
        "is_model_facing": True,
        "total_tables": 2,
        "total_columns": 5,
        "total_foreign_keys": 1,
        "tables": [
            {
                "schema_name": "dbo",
                "table_name": "Departments",
                "description": "Department roster\r\nand organizational hierarchy",
                "columns": [
                    {
                        "name": "DepartmentID",
                        "data_type": "int",
                        "nullable": False,
                        "primary_key": True,
                        "default": None,
                        "is_identity": True,
                        "comment": "Primary key for department",
                    },
                    {
                        "name": "DepartmentName",
                        "data_type": "varchar(100)",
                        "nullable": False,
                        "primary_key": False,
                        "default": "''",
                        "is_identity": False,
                        "comment": None,
                    },
                ],
                "primary_keys": ["DepartmentID"],
                "foreign_keys": [],
                "indexes": [{"name": "IX_Dept", "columns": ["DepartmentName"]}],
                "unique_constraints": [{"name": "UQ_Dept", "columns": ["DepartmentName"]}],
            },
            {
                "schema_name": "hr",
                "table_name": "Employees",
                "columns": [
                    {"name": "EmployeeID", "data_type": "int", "nullable": False, "primary_key": True},
                    {"name": "DepartmentID", "data_type": "int", "nullable": True},
                    {"name": "Title", "data_type": "varchar(50)", "nullable": False},
                ],
                "primary_keys": ["EmployeeID"],
                "foreign_keys": [
                    {
                        "source_table": "Employees",
                        "source_column": "DepartmentID",
                        "target_table": "Departments",
                        "target_column": "DepartmentID",
                        "name": "FK_Employees_Departments",
                    }
                ],
            },
        ],
    }

    projected_json = run_ts_projection(raw_schema)

    # Validate directly into Python Pydantic ModelFacingSchemaContext
    model_schema = ModelFacingSchemaContext.model_validate(projected_json)
    assert model_schema.database_name == "EnterpriseDW"
    assert len(model_schema.tables) == 2

    dept_tbl = model_schema.tables[0]
    assert dept_tbl.schema_name == "dbo"
    assert dept_tbl.table_name == "Departments"
    assert dept_tbl.description == "Department roster and organizational hierarchy"
    assert dept_tbl.primary_keys == ["DepartmentID"]
    assert len(dept_tbl.columns) == 2
    assert dept_tbl.columns[0].name == "DepartmentID"
    assert dept_tbl.columns[0].data_type == "int"
    assert dept_tbl.columns[0].nullable is False
    assert dept_tbl.columns[0].primary_key is True
    assert dept_tbl.columns[0].comment == "Primary key for department"
    assert dept_tbl.columns[1].comment is None

    emp_tbl = model_schema.tables[1]
    assert emp_tbl.schema_name == "hr"
    assert emp_tbl.table_name == "Employees"
    assert len(emp_tbl.foreign_keys) == 1
    fk = emp_tbl.foreign_keys[0]
    assert fk.source_table == "Employees"
    assert fk.source_column == "DepartmentID"
    assert fk.target_table == "Departments"
    assert fk.target_column == "DepartmentID"
    assert fk.name == "FK_Employees_Departments"

    # Verify integration inside GenerateSQLRequest
    req = GenerateSQLRequest(
        question="How many employees are in each department?",
        schema_context=model_schema,
    )
    assert req.question == "How many employees are in each department?"
    assert req.schema_context.database_name == "EnterpriseDW"

    # Verify internal DatabaseSchema conversion
    internal_db_schema = model_schema.to_database_schema()
    assert internal_db_schema.database_name == "EnterpriseDW"
    assert internal_db_schema.is_model_facing is True
    assert len(internal_db_schema.tables) == 2


# ==============================================================================
# 2. Extra Forbidden Fields Rejection & Stripping Test
# ==============================================================================

def test_contract_extra_forbidden_fields_stripped():
    """Verify raw introspected metadata with extra fields is rejected by Pydantic if unprojected,
    but passes cleanly after projection.
    """
    raw_with_extras = {
        "database_name": "SalesDB",
        "is_model_facing": True,
        "total_tables": 1,
        "total_columns": 2,
        "total_foreign_keys": 0,
        "unrecognized_root_field": "disallowed",
        "tables": [
            {
                "schema_name": "dbo",
                "table_name": "Orders",
                "indexes": [{"name": "IX_OrderDate", "columns": ["OrderDate"]}],
                "unique_constraints": [{"name": "UQ_OrderNum", "columns": ["OrderNum"]}],
                "unrecognized_table_field": 12345,
                "columns": [
                    {
                        "name": "OrderID",
                        "data_type": "int",
                        "nullable": False,
                        "primary_key": True,
                        "default": None,
                        "is_identity": True,
                        "unrecognized_col_field": True,
                    },
                    {
                        "name": "OrderNum",
                        "data_type": "varchar(50)",
                        "nullable": False,
                        "primary_key": False,
                    },
                ],
                "primary_keys": ["OrderID"],
                "foreign_keys": [],
            }
        ],
    }

    # 1. Unprojected raw schema MUST be rejected by Pydantic due to extra="forbid"
    with pytest.raises(ValidationError) as exc_info:
        ModelFacingSchemaContext.model_validate(raw_with_extras)
    errors = exc_info.value.errors()
    assert any(err["type"] == "extra_forbidden" for err in errors)

    # 2. Projected JSON strips all extras and succeeds with 0 errors
    projected = run_ts_projection(raw_with_extras)
    assert "is_model_facing" not in projected
    assert "total_tables" not in projected
    assert "unrecognized_root_field" not in projected
    assert "indexes" not in projected["tables"][0]
    assert "unique_constraints" not in projected["tables"][0]
    assert "unrecognized_table_field" not in projected["tables"][0]
    assert "default" not in projected["tables"][0]["columns"][0]
    assert "is_identity" not in projected["tables"][0]["columns"][0]
    assert "unrecognized_col_field" not in projected["tables"][0]["columns"][0]

    model = ModelFacingSchemaContext.model_validate(projected)
    assert model.database_name == "SalesDB"
    assert len(model.tables) == 1


# ==============================================================================
# 3. Missing Required Fields Test
# ==============================================================================

def test_contract_missing_required_fields():
    """Verify missing required fields fail in TypeScript projection and Pydantic."""
    # 3a. Missing database_name
    assert_ts_projection_error({"tables": []}, "database_name must be a non-empty string")
    with pytest.raises(ValidationError):
        ModelFacingSchemaContext.model_validate({"tables": []})

    # 3b. Missing table_name
    assert_ts_projection_error(
        {"database_name": "DB", "tables": [{"columns": [{"name": "id", "data_type": "int"}]}]},
        "table_name must be a non-empty string",
    )
    with pytest.raises(ValidationError):
        TableSchemaContext.model_validate({"columns": [{"name": "id", "data_type": "int"}]})

    # 3c. Missing columns
    assert_ts_projection_error(
        {"database_name": "DB", "tables": [{"table_name": "Users"}]},
        "must contain at least 1 column",
    )
    with pytest.raises(ValidationError):
        TableSchemaContext.model_validate({"table_name": "Users"})

    # 3d. Missing column name
    assert_ts_projection_error(
        {"database_name": "DB", "tables": [{"table_name": "Users", "columns": [{"data_type": "int"}]}]},
        "must have a non-empty string name",
    )
    with pytest.raises(ValidationError):
        ColumnSchemaContext.model_validate({"data_type": "int"})

    # 3e. Missing column data_type
    assert_ts_projection_error(
        {"database_name": "DB", "tables": [{"table_name": "Users", "columns": [{"name": "id"}]}]},
        "must have a non-empty string data_type",
    )
    with pytest.raises(ValidationError):
        ColumnSchemaContext.model_validate({"name": "id"})


# ==============================================================================
# 4. Optional Fields Omitted Test
# ==============================================================================

def test_contract_optional_fields_omitted():
    """Verify minimal payload with optional fields omitted resolves to compatible defaults."""
    minimal_raw = {
        "database_name": "MinimalDB",
        "tables": [
            {
                "table_name": "Items",
                "columns": [
                    {"name": "ItemID", "data_type": "int"},
                ],
            }
        ],
    }

    projected = run_ts_projection(minimal_raw)
    model = ModelFacingSchemaContext.model_validate(projected)

    # Defaults verified
    assert model.tables[0].schema_name == "dbo"
    assert model.tables[0].description is None
    assert model.tables[0].primary_keys == []
    assert model.tables[0].foreign_keys == []
    assert model.tables[0].columns[0].nullable is True
    assert model.tables[0].columns[0].primary_key is False
    assert model.tables[0].columns[0].comment is None


# ==============================================================================
# 5. Explicit Null Values Handling Test
# ==============================================================================

def test_contract_explicit_null_values():
    """Verify explicit nulls for optional fields are sanitized and pass Pydantic."""
    raw_with_nulls = {
        "database_name": "NullTestDB",
        "tables": [
            {
                "schema_name": "dbo",
                "table_name": "Customers",
                "description": None,
                "comment": None,
                "primary_keys": None,
                "foreign_keys": [
                    {
                        "source_table": "Customers",
                        "source_column": "RegionID",
                        "target_table": "Regions",
                        "target_column": "RegionID",
                        "name": None,
                    }
                ],
                "columns": [
                    {
                        "name": "CustomerID",
                        "data_type": "int",
                        "nullable": False,
                        "primary_key": True,
                        "comment": None,
                    },
                    {
                        "name": "RegionID",
                        "data_type": "int",
                        "nullable": True,
                        "primary_key": False,
                        "comment": None,
                    },
                ],
            }
        ],
    }

    projected = run_ts_projection(raw_with_nulls)
    model = ModelFacingSchemaContext.model_validate(projected)

    assert model.tables[0].description is None
    assert model.tables[0].columns[0].comment is None
    assert model.tables[0].columns[1].comment is None
    assert model.tables[0].foreign_keys[0].name is None


# ==============================================================================
# 6. Empty Collections Rejection Test
# ==============================================================================

def test_contract_empty_collections():
    """Verify empty tables and empty columns collections are strictly rejected."""
    # 6a. Empty tables collection
    assert_ts_projection_error(
        {"database_name": "EmptyDB", "tables": []},
        "tables array must contain at least 1 table",
    )
    with pytest.raises(ValidationError):
        ModelFacingSchemaContext.model_validate({"database_name": "EmptyDB", "tables": []})

    # 6b. Empty columns collection
    assert_ts_projection_error(
        {"database_name": "EmptyDB", "tables": [{"table_name": "EmptyTable", "columns": []}]},
        "must contain at least 1 column",
    )
    with pytest.raises(ValidationError):
        TableSchemaContext.model_validate({"table_name": "EmptyTable", "columns": []})


# ==============================================================================
# 7. Structural Fidelity & Hosted SQL-Generation Invariants Test
# ==============================================================================

def test_contract_structural_fidelity_and_integrity():
    """Confirm normalization preserves all structural information required by hosted SQL-generation."""
    raw_schema = {
        "database_name": "Analytics_Prod#1",
        "tables": [
            {
                "schema_name": "reporting",
                "table_name": "Monthly_Sales",
                "description": "Aggregated sales facts\r\nupdated at 00:00 UTC",
                "columns": [
                    {"name": "Report_Month", "data_type": "varchar(7)", "nullable": False, "primary_key": True},
                    {"name": "Product_SKU", "data_type": "varchar(50)", "nullable": False, "primary_key": True},
                    {"name": "Gross_Revenue", "data_type": "decimal(18, 2)", "nullable": False, "primary_key": False, "comment": "Pre-tax total"},
                    {"name": "Units_Sold", "data_type": "int", "nullable": True, "primary_key": False},
                ],
                "primary_keys": ["Report_Month", "Product_SKU", "NonExistentColumnPruned"],
                "foreign_keys": [
                    {
                        "source_table": "Monthly_Sales",
                        "source_column": "Product_SKU",
                        "target_table": "Products_Catalog",
                        "target_column": "SKU",
                        "name": "FK_MonthlySales_Products",
                    }
                ],
            }
        ],
    }

    projected = run_ts_projection(raw_schema)
    model = ModelFacingSchemaContext.model_validate(projected)

    # 1. Database name intact
    assert model.database_name == "Analytics_Prod#1"

    # 2. Table and schema intact
    tbl = model.tables[0]
    assert tbl.schema_name == "reporting"
    assert tbl.table_name == "Monthly_Sales"
    assert tbl.description == "Aggregated sales facts updated at 00:00 UTC"

    # 3. Columns exact data types and nullability intact
    assert len(tbl.columns) == 4
    col_map = {c.name: c for c in tbl.columns}
    assert col_map["Gross_Revenue"].data_type == "decimal(18, 2)"
    assert col_map["Gross_Revenue"].nullable is False
    assert col_map["Gross_Revenue"].comment == "Pre-tax total"
    assert col_map["Units_Sold"].data_type == "int"
    assert col_map["Units_Sold"].nullable is True

    # 4. Primary keys preserved and orphaned key pruned without breaking table integrity
    assert tbl.primary_keys == ["Report_Month", "Product_SKU"]

    # 5. Foreign keys preserved
    assert len(tbl.foreign_keys) == 1
    assert tbl.foreign_keys[0].source_table == "Monthly_Sales"
    assert tbl.foreign_keys[0].target_table == "Products_Catalog"
    assert tbl.foreign_keys[0].target_column == "SKU"

    # 6. Pydantic to internal DatabaseSchema retains complete model-facing metadata
    db_schema = model.to_database_schema()
    assert db_schema.is_model_facing is True
    assert db_schema.tables[0].primary_keys == ["Report_Month", "Product_SKU"]
    assert len(db_schema.tables[0].foreign_keys) == 1
