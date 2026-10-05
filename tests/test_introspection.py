"""Unit and integration tests for schema introspection and sensitive column redaction."""

import pytest
from query_pilot.db.connection import create_db_engine
from query_pilot.db.introspection import (
    DEFAULT_SENSITIVE_COLUMNS,
    DatabaseIntrospector,
    SensitiveColumnPolicy,
)
from query_pilot.db.models import (
    ColumnMetadata,
    DatabaseSchema,
    ForeignKeyMetadata,
    IndexMetadata,
    TableMetadata,
)


# ==============================================================================
# SensitiveColumnPolicy Unit Tests
# ==============================================================================

def test_sensitive_column_policy_default_denylist():
    """Verify default sensitive column redaction behavior."""
    policy = SensitiveColumnPolicy()

    # Case-insensitive checks for default sensitive fields
    assert policy.is_sensitive("PasswordHash") is True
    assert policy.is_sensitive("passwordhash") is True
    assert policy.is_sensitive("PASSWORDHASH") is True
    assert policy.is_sensitive("password") is True
    assert policy.is_sensitive("secret") is True
    assert policy.is_sensitive("token") is True

    # Standard, non-sensitive columns must NOT be redacted
    assert policy.is_sensitive("Id") is False
    assert policy.is_sensitive("Email") is False
    assert policy.is_sensitive("FullName") is False
    assert policy.is_sensitive("Role") is False
    assert policy.is_sensitive("IsActive") is False
    assert policy.is_sensitive("CreatedAt") is False
    assert policy.is_sensitive("") is False


def test_sensitive_column_policy_custom_denylist():
    """Verify custom sensitive column denylist configuration."""
    policy = SensitiveColumnPolicy(denylist=["internal_notes", "salary", "ssn"])
    assert policy.is_sensitive("Salary") is True
    assert policy.is_sensitive("INTERNAL_NOTES") is True
    assert policy.is_sensitive("ssn") is True
    assert policy.is_sensitive("PasswordHash") is False  # not in custom list


# ==============================================================================
# Structured Models Unit Tests
# ==============================================================================

def test_column_and_table_metadata():
    """Verify metadata model dataclasses and query helpers."""
    col1 = ColumnMetadata(name="Id", data_type="INTEGER", nullable=False, primary_key=True)
    col2 = ColumnMetadata(name="PasswordHash", data_type="VARCHAR(255)", nullable=False)
    fk = ForeignKeyMetadata(
        name="FK_Test",
        source_table="Child",
        source_column="ParentId",
        target_table="Parent",
        target_column="Id",
    )
    idx = IndexMetadata(name="idx_test", columns=["Id"], is_unique=True)

    table = TableMetadata(
        schema_name="dbo",
        table_name="Users",
        columns=[col1, col2],
        primary_keys=["Id"],
        foreign_keys=[fk],
        indexes=[idx],
    )

    assert table.column_names == ["Id", "PasswordHash"]
    assert table.get_column("id") == col1
    assert table.get_column("PASSWORDHASH") == col2
    assert table.get_column("nonexistent") is None

    schema = DatabaseSchema(database_name="TestDB", tables=[table], is_model_facing=False)
    assert schema.table_names == ["Users"]
    assert schema.get_table("users") == table
    assert schema.total_columns_count == 2
    assert schema.total_foreign_keys_count == 1

    d = schema.to_dict()
    assert d["database_name"] == "TestDB"
    assert d["total_tables"] == 1
    assert d["tables"][0]["table_name"] == "Users"


# ==============================================================================
# Live Introspection Integration Tests (GradeSense_Local)
# ==============================================================================

@pytest.fixture(scope="module")
def introspector():
    """Fixture providing DatabaseIntrospector connected to GradeSense_Local."""
    engine = create_db_engine()
    return DatabaseIntrospector(engine)


@pytest.fixture(scope="module")
def raw_schema(introspector):
    """Fixture providing cached raw DatabaseSchema for testing."""
    return introspector.introspect_raw()


@pytest.fixture(scope="module")
def model_facing_schema(introspector, raw_schema):
    """Fixture providing cached model-facing DatabaseSchema for testing."""
    return introspector.filter_model_facing(raw_schema)


def test_live_introspection_tables_count(raw_schema):
    """Verify exactly 17 tables are discovered in GradeSense_Local."""
    assert raw_schema.database_name == "GradeSense_Local"
    assert len(raw_schema.tables) == 17


def test_live_introspection_expected_tables(raw_schema):
    """Verify all expected core tables exist in the introspected schema."""
    table_names = raw_schema.table_names

    expected_tables = [
        "AssessmentItems",
        "AttendanceRecords",
        "AuditLog",
        "Batches",
        "CourseEnrollments",
        "CourseOfferings",
        "Departments",
        "EvaluationSchemes",
        "Faculties",
        "FacultyAssignments",
        "Predictions",
        "StudentMarks",
        "Students",
        "Subjects",
        "SubjectUnits",
        "UploadHistory",
        "Users",
    ]
    for tbl in expected_tables:
        assert tbl in table_names, f"Expected table '{tbl}' not found in schema"


def test_live_introspection_raw_vs_model_facing_sensitive_columns(raw_schema, model_facing_schema):
    """Verify Users.PasswordHash is present in raw metadata and absent in model-facing metadata."""
    users_raw = raw_schema.get_table("Users")
    assert users_raw is not None
    assert "PasswordHash" in users_raw.column_names
    assert raw_schema.total_columns_count == 208
    assert raw_schema.is_model_facing is False

    # Model-facing schema must redact PasswordHash
    users_mf = model_facing_schema.get_table("Users")
    assert users_mf is not None
    assert "PasswordHash" not in users_mf.column_names
    assert model_facing_schema.total_columns_count == 207  # Exactly 1 sensitive column removed
    assert model_facing_schema.is_model_facing is True

    # Other columns in Users must remain intact
    expected_safe_cols = ["Id", "Email", "FullName", "Role", "IsActive", "CreatedAt", "UpdatedAt", "DeletedAt"]
    for col in expected_safe_cols:
        assert col in users_mf.column_names


def test_live_introspection_foreign_keys(raw_schema):
    """Verify all 32 foreign-key relationships are discovered."""
    assert raw_schema.total_foreign_keys_count == 32

    # Check CourseEnrollments foreign keys
    ce_table = raw_schema.get_table("CourseEnrollments")
    assert ce_table is not None

    ce_fks = [(fk.source_column, fk.target_table, fk.target_column) for fk in ce_table.foreign_keys]
    assert ("CourseOfferingId", "CourseOfferings", "Id") in ce_fks
    assert ("StudentId", "Students", "Id") in ce_fks

    # Check Predictions foreign key
    pred_table = raw_schema.get_table("Predictions")
    assert pred_table is not None
    pred_fks = [(fk.source_column, fk.target_table, fk.target_column) for fk in pred_table.foreign_keys]
    assert ("CourseEnrollmentId", "CourseEnrollments", "Id") in pred_fks


def test_live_introspection_filtered_index(raw_schema):
    """Verify Predictions filtered unique index is captured in index metadata."""
    pred_table = raw_schema.get_table("Predictions")
    assert pred_table is not None

    active_unique_idx = next(
        (idx for idx in pred_table.indexes if idx.name == "idx_predictions_active_unique"),
        None,
    )
    assert active_unique_idx is not None
    assert active_unique_idx.is_unique is True
    assert active_unique_idx.columns == ["CourseEnrollmentId"]
    assert active_unique_idx.filter_definition == "([IsActive]=(1))"


def test_live_introspection_determinism(introspector):
    """Verify that multiple introspection runs produce identical schema representations."""
    schema1 = introspector.introspect_model_facing()
    schema2 = introspector.introspect_model_facing()

    assert schema1.table_names == schema2.table_names
    for t1, t2 in zip(schema1.tables, schema2.tables):
        assert t1.table_name == t2.table_name
        assert t1.column_names == t2.column_names
        assert [c.data_type for c in t1.columns] == [c.data_type for c in t2.columns]
        assert [fk.to_dict() for fk in t1.foreign_keys] == [fk.to_dict() for fk in t2.foreign_keys]
        assert [idx.to_dict() for idx in t1.indexes] == [idx.to_dict() for idx in t2.indexes]
