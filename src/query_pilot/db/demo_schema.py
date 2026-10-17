"""Checked-in, trusted model-facing demo schema provider for QueryPilot.

Provides an in-memory, deterministic model-facing DatabaseSchema for GradeSense_Local,
derived directly from the canonical DDL definition in data/schemas/gradesense_schema.sql.

Security & Safety Guarantees:
- Zero database connections or live network access required.
- PasswordHash and sensitive columns are redacted per SensitiveColumnPolicy.
- All 17 relational tables, primary keys, and foreign-key constraints are faithfully represented.
- is_model_facing is strictly True.
- Client cannot provide arbitrary schema content to bypass validation boundaries.
"""

from functools import lru_cache
from typing import List

from query_pilot.db.models import (
    ColumnMetadata,
    DatabaseSchema,
    ForeignKeyMetadata,
    TableMetadata,
)


@lru_cache(maxsize=1)
def get_demo_schema() -> DatabaseSchema:
    """Return the trusted, model-facing GradeSense_Local demo schema.

    Contains all 17 canonical relational tables from data/schemas/gradesense_schema.sql
    with sensitive columns redacted.
    """
    tables: List[TableMetadata] = []

    # 1. Users (PasswordHash redacted)
    tables.append(
        TableMetadata(
            schema_name="dbo",
            table_name="Users",
            columns=[
                ColumnMetadata(name="Id", data_type="int", nullable=False, primary_key=True, is_identity=True),
                ColumnMetadata(name="Email", data_type="varchar(255)", nullable=False),
                ColumnMetadata(name="FullName", data_type="varchar(255)", nullable=False),
                ColumnMetadata(name="Role", data_type="nvarchar(255)", nullable=False),
                ColumnMetadata(name="IsActive", data_type="bit", nullable=False, default="1"),
                ColumnMetadata(name="CreatedAt", data_type="datetime2", nullable=True),
                ColumnMetadata(name="UpdatedAt", data_type="datetime2", nullable=True),
                ColumnMetadata(name="DeletedAt", data_type="datetime2", nullable=True),
            ],
            primary_keys=["Id"],
        )
    )

    # 2. Departments
    tables.append(
        TableMetadata(
            schema_name="dbo",
            table_name="Departments",
            columns=[
                ColumnMetadata(name="Id", data_type="int", nullable=False, primary_key=True, is_identity=True),
                ColumnMetadata(name="Name", data_type="varchar(255)", nullable=False),
                ColumnMetadata(name="Code", data_type="varchar(50)", nullable=True),
                ColumnMetadata(name="HODUserId", data_type="int", nullable=True),
                ColumnMetadata(name="IsActive", data_type="bit", nullable=False, default="1"),
                ColumnMetadata(name="CreatedAt", data_type="datetime2", nullable=True),
                ColumnMetadata(name="UpdatedAt", data_type="datetime2", nullable=True),
                ColumnMetadata(name="DeletedAt", data_type="datetime2", nullable=True),
            ],
            primary_keys=["Id"],
            foreign_keys=[
                ForeignKeyMetadata(
                    name="FK_Departments_Users",
                    source_table="Departments",
                    source_column="HODUserId",
                    target_table="Users",
                    target_column="Id",
                ),
            ],
        )
    )

    # 3. Faculties
    tables.append(
        TableMetadata(
            schema_name="dbo",
            table_name="Faculties",
            columns=[
                ColumnMetadata(name="Id", data_type="int", nullable=False, primary_key=True),
                ColumnMetadata(name="EmployeeId", data_type="varchar(255)", nullable=False),
                ColumnMetadata(name="DepartmentId", data_type="int", nullable=False),
                ColumnMetadata(name="Designation", data_type="varchar(255)", nullable=True),
                ColumnMetadata(name="JoiningDate", data_type="date", nullable=True),
                ColumnMetadata(name="Qualification", data_type="varchar(255)", nullable=True),
                ColumnMetadata(name="Specialization", data_type="varchar(255)", nullable=True),
                ColumnMetadata(name="CreatedAt", data_type="datetime2", nullable=True),
                ColumnMetadata(name="UpdatedAt", data_type="datetime2", nullable=True),
                ColumnMetadata(name="DeletedAt", data_type="datetime2", nullable=True),
            ],
            primary_keys=["Id"],
            foreign_keys=[
                ForeignKeyMetadata(
                    name="FK_Faculties_Users",
                    source_table="Faculties",
                    source_column="Id",
                    target_table="Users",
                    target_column="Id",
                ),
                ForeignKeyMetadata(
                    name="FK_Faculties_Departments",
                    source_table="Faculties",
                    source_column="DepartmentId",
                    target_table="Departments",
                    target_column="Id",
                ),
            ],
        )
    )

    # 4. Students
    tables.append(
        TableMetadata(
            schema_name="dbo",
            table_name="Students",
            columns=[
                ColumnMetadata(name="Id", data_type="int", nullable=False, primary_key=True),
                ColumnMetadata(name="EnrollmentNumber", data_type="varchar(255)", nullable=False),
                ColumnMetadata(name="AdmissionYear", data_type="int", nullable=False),
                ColumnMetadata(name="CurrentSemester", data_type="int", nullable=False),
                ColumnMetadata(name="DepartmentId", data_type="int", nullable=False),
                ColumnMetadata(name="Status", data_type="varchar(50)", nullable=False, default="'Active'"),
                ColumnMetadata(name="CGPA", data_type="decimal(4,2)", nullable=True),
                ColumnMetadata(name="CreatedAt", data_type="datetime2", nullable=True),
                ColumnMetadata(name="UpdatedAt", data_type="datetime2", nullable=True),
                ColumnMetadata(name="DeletedAt", data_type="datetime2", nullable=True),
            ],
            primary_keys=["Id"],
            foreign_keys=[
                ForeignKeyMetadata(
                    name="FK_Students_Users",
                    source_table="Students",
                    source_column="Id",
                    target_table="Users",
                    target_column="Id",
                ),
                ForeignKeyMetadata(
                    name="FK_Students_Departments",
                    source_table="Students",
                    source_column="DepartmentId",
                    target_table="Departments",
                    target_column="Id",
                ),
            ],
        )
    )

    # 5. Batches
    tables.append(
        TableMetadata(
            schema_name="dbo",
            table_name="Batches",
            columns=[
                ColumnMetadata(name="Id", data_type="int", nullable=False, primary_key=True, is_identity=True),
                ColumnMetadata(name="Name", data_type="varchar(255)", nullable=False),
                ColumnMetadata(name="Semester", data_type="int", nullable=False),
                ColumnMetadata(name="AcademicYear", data_type="int", nullable=False),
                ColumnMetadata(name="DepartmentId", data_type="int", nullable=False),
                ColumnMetadata(name="ClassCoordinatorId", data_type="int", nullable=True),
                ColumnMetadata(name="Division", data_type="varchar(10)", nullable=True),
                ColumnMetadata(name="IsActive", data_type="bit", nullable=False, default="1"),
                ColumnMetadata(name="CreatedAt", data_type="datetime2", nullable=True),
                ColumnMetadata(name="UpdatedAt", data_type="datetime2", nullable=True),
                ColumnMetadata(name="DeletedAt", data_type="datetime2", nullable=True),
            ],
            primary_keys=["Id"],
            foreign_keys=[
                ForeignKeyMetadata(
                    name="FK_Batches_Departments",
                    source_table="Batches",
                    source_column="DepartmentId",
                    target_table="Departments",
                    target_column="Id",
                ),
                ForeignKeyMetadata(
                    name="FK_Batches_Faculties",
                    source_table="Batches",
                    source_column="ClassCoordinatorId",
                    target_table="Faculties",
                    target_column="Id",
                ),
            ],
        )
    )

    # 6. Subjects
    tables.append(
        TableMetadata(
            schema_name="dbo",
            table_name="Subjects",
            columns=[
                ColumnMetadata(name="Id", data_type="int", nullable=False, primary_key=True, is_identity=True),
                ColumnMetadata(name="Code", data_type="varchar(255)", nullable=False),
                ColumnMetadata(name="Name", data_type="varchar(255)", nullable=False),
                ColumnMetadata(name="Credit", data_type="decimal(3,1)", nullable=False),
                ColumnMetadata(name="DepartmentId", data_type="int", nullable=False),
                ColumnMetadata(name="Semester", data_type="int", nullable=True),
                ColumnMetadata(name="SubjectType", data_type="varchar(50)", nullable=True),
                ColumnMetadata(name="IsElective", data_type="bit", nullable=False, default="0"),
                ColumnMetadata(name="PrerequisiteSubjectId", data_type="int", nullable=True),
                ColumnMetadata(name="Description", data_type="nvarchar(MAX)", nullable=True),
                ColumnMetadata(name="Syllabus", data_type="nvarchar(MAX)", nullable=True),
                ColumnMetadata(name="IsActive", data_type="bit", nullable=False, default="1"),
                ColumnMetadata(name="CreatedAt", data_type="datetime2", nullable=True),
                ColumnMetadata(name="UpdatedAt", data_type="datetime2", nullable=True),
                ColumnMetadata(name="DeletedAt", data_type="datetime2", nullable=True),
            ],
            primary_keys=["Id"],
            foreign_keys=[
                ForeignKeyMetadata(
                    name="FK_Subjects_Departments",
                    source_table="Subjects",
                    source_column="DepartmentId",
                    target_table="Departments",
                    target_column="Id",
                ),
                ForeignKeyMetadata(
                    name="FK_Subjects_Subjects",
                    source_table="Subjects",
                    source_column="PrerequisiteSubjectId",
                    target_table="Subjects",
                    target_column="Id",
                ),
            ],
        )
    )

    # 7. SubjectUnits
    tables.append(
        TableMetadata(
            schema_name="dbo",
            table_name="SubjectUnits",
            columns=[
                ColumnMetadata(name="Id", data_type="int", nullable=False, primary_key=True, is_identity=True),
                ColumnMetadata(name="SubjectId", data_type="int", nullable=False),
                ColumnMetadata(name="UnitNumber", data_type="int", nullable=False),
                ColumnMetadata(name="TopicName", data_type="varchar(255)", nullable=False),
                ColumnMetadata(name="Description", data_type="varchar(MAX)", nullable=True),
                ColumnMetadata(name="TeachingHours", data_type="int", nullable=False),
                ColumnMetadata(name="Weightage", data_type="decimal(5,2)", nullable=True),
                ColumnMetadata(name="LearningOutcomes", data_type="varchar(MAX)", nullable=True),
                ColumnMetadata(name="CreatedAt", data_type="datetime2", nullable=True),
                ColumnMetadata(name="UpdatedAt", data_type="datetime2", nullable=True),
                ColumnMetadata(name="DeletedAt", data_type="datetime2", nullable=True),
            ],
            primary_keys=["Id"],
            foreign_keys=[
                ForeignKeyMetadata(
                    name="FK_SubjectUnits_Subjects",
                    source_table="SubjectUnits",
                    source_column="SubjectId",
                    target_table="Subjects",
                    target_column="Id",
                ),
            ],
        )
    )

    # 8. CourseOfferings
    tables.append(
        TableMetadata(
            schema_name="dbo",
            table_name="CourseOfferings",
            columns=[
                ColumnMetadata(name="Id", data_type="int", nullable=False, primary_key=True, is_identity=True),
                ColumnMetadata(name="SubjectId", data_type="int", nullable=False),
                ColumnMetadata(name="BatchId", data_type="int", nullable=False),
                ColumnMetadata(name="SubjectCoordinatorId", data_type="int", nullable=False),
                ColumnMetadata(name="AcademicYear", data_type="int", nullable=False),
                ColumnMetadata(name="StartDate", data_type="date", nullable=True),
                ColumnMetadata(name="EndDate", data_type="date", nullable=True),
                ColumnMetadata(name="MaxEnrollment", data_type="int", nullable=True),
                ColumnMetadata(name="IsActive", data_type="bit", nullable=False, default="1"),
                ColumnMetadata(name="CreatedAt", data_type="datetime2", nullable=True),
                ColumnMetadata(name="UpdatedAt", data_type="datetime2", nullable=True),
                ColumnMetadata(name="DeletedAt", data_type="datetime2", nullable=True),
            ],
            primary_keys=["Id"],
            foreign_keys=[
                ForeignKeyMetadata(
                    name="FK_CourseOfferings_Subjects",
                    source_table="CourseOfferings",
                    source_column="SubjectId",
                    target_table="Subjects",
                    target_column="Id",
                ),
                ForeignKeyMetadata(
                    name="FK_CourseOfferings_Batches",
                    source_table="CourseOfferings",
                    source_column="BatchId",
                    target_table="Batches",
                    target_column="Id",
                ),
                ForeignKeyMetadata(
                    name="FK_CourseOfferings_Faculties",
                    source_table="CourseOfferings",
                    source_column="SubjectCoordinatorId",
                    target_table="Faculties",
                    target_column="Id",
                ),
            ],
        )
    )

    # 9. CourseEnrollments
    tables.append(
        TableMetadata(
            schema_name="dbo",
            table_name="CourseEnrollments",
            columns=[
                ColumnMetadata(name="Id", data_type="int", nullable=False, primary_key=True, is_identity=True),
                ColumnMetadata(name="CourseOfferingId", data_type="int", nullable=False),
                ColumnMetadata(name="StudentId", data_type="int", nullable=False),
                ColumnMetadata(name="RollNumber", data_type="varchar(255)", nullable=True),
                ColumnMetadata(name="EnrollmentDate", data_type="datetime2", nullable=True),
                ColumnMetadata(name="Status", data_type="varchar(50)", nullable=False, default="'Active'"),
                ColumnMetadata(name="AttendancePercentage", data_type="decimal(5,2)", nullable=True),
                ColumnMetadata(name="Grade", data_type="varchar(5)", nullable=True),
                ColumnMetadata(name="GradePoints", data_type="decimal(4,2)", nullable=True),
                ColumnMetadata(name="CreatedAt", data_type="datetime2", nullable=True),
                ColumnMetadata(name="UpdatedAt", data_type="datetime2", nullable=True),
                ColumnMetadata(name="DeletedAt", data_type="datetime2", nullable=True),
            ],
            primary_keys=["Id"],
            foreign_keys=[
                ForeignKeyMetadata(
                    name="FK_CourseEnrollments_CourseOfferings",
                    source_table="CourseEnrollments",
                    source_column="CourseOfferingId",
                    target_table="CourseOfferings",
                    target_column="Id",
                ),
                ForeignKeyMetadata(
                    name="FK_CourseEnrollments_Students",
                    source_table="CourseEnrollments",
                    source_column="StudentId",
                    target_table="Students",
                    target_column="Id",
                ),
            ],
        )
    )

    # 10. EvaluationSchemes
    tables.append(
        TableMetadata(
            schema_name="dbo",
            table_name="EvaluationSchemes",
            columns=[
                ColumnMetadata(name="Id", data_type="int", nullable=False, primary_key=True, is_identity=True),
                ColumnMetadata(name="CourseOfferingId", data_type="int", nullable=False),
                ColumnMetadata(name="Name", data_type="varchar(255)", nullable=False),
                ColumnMetadata(name="Description", data_type="varchar(MAX)", nullable=True),
                ColumnMetadata(name="TotalMarks", data_type="decimal(6,2)", nullable=False),
                ColumnMetadata(name="PassingMarks", data_type="decimal(6,2)", nullable=False),
                ColumnMetadata(name="Weight", data_type="decimal(5,2)", nullable=False),
                ColumnMetadata(name="EvaluationType", data_type="varchar(50)", nullable=True),
                ColumnMetadata(name="IsActive", data_type="bit", nullable=False, default="1"),
                ColumnMetadata(name="CreatedAt", data_type="datetime2", nullable=True),
                ColumnMetadata(name="UpdatedAt", data_type="datetime2", nullable=True),
                ColumnMetadata(name="DeletedAt", data_type="datetime2", nullable=True),
            ],
            primary_keys=["Id"],
            foreign_keys=[
                ForeignKeyMetadata(
                    name="FK_EvaluationSchemes_CourseOfferings",
                    source_table="EvaluationSchemes",
                    source_column="CourseOfferingId",
                    target_table="CourseOfferings",
                    target_column="Id",
                ),
            ],
        )
    )

    # 11. AssessmentItems
    tables.append(
        TableMetadata(
            schema_name="dbo",
            table_name="AssessmentItems",
            columns=[
                ColumnMetadata(name="Id", data_type="int", nullable=False, primary_key=True, is_identity=True),
                ColumnMetadata(name="EvaluationSchemeId", data_type="int", nullable=False),
                ColumnMetadata(name="SubjectUnitId", data_type="int", nullable=True),
                ColumnMetadata(name="Name", data_type="varchar(255)", nullable=False),
                ColumnMetadata(name="Description", data_type="varchar(MAX)", nullable=True),
                ColumnMetadata(name="MaxMarks", data_type="decimal(6,2)", nullable=False),
                ColumnMetadata(name="CalculationType", data_type="nvarchar(255)", nullable=False, default="'Raw'"),
                ColumnMetadata(name="Weight", data_type="decimal(5,2)", nullable=True),
                ColumnMetadata(name="ScheduledDate", data_type="date", nullable=True),
                ColumnMetadata(name="DueDate", data_type="date", nullable=True),
                ColumnMetadata(name="IsActive", data_type="bit", nullable=False, default="1"),
                ColumnMetadata(name="CreatedBy", data_type="int", nullable=True),
                ColumnMetadata(name="CreatedAt", data_type="datetime2", nullable=True),
                ColumnMetadata(name="UpdatedAt", data_type="datetime2", nullable=True),
                ColumnMetadata(name="DeletedAt", data_type="datetime2", nullable=True),
            ],
            primary_keys=["Id"],
            foreign_keys=[
                ForeignKeyMetadata(
                    name="FK_AssessmentItems_EvaluationSchemes",
                    source_table="AssessmentItems",
                    source_column="EvaluationSchemeId",
                    target_table="EvaluationSchemes",
                    target_column="Id",
                ),
                ForeignKeyMetadata(
                    name="FK_AssessmentItems_SubjectUnits",
                    source_table="AssessmentItems",
                    source_column="SubjectUnitId",
                    target_table="SubjectUnits",
                    target_column="Id",
                ),
                ForeignKeyMetadata(
                    name="FK_AssessmentItems_Faculties",
                    source_table="AssessmentItems",
                    source_column="CreatedBy",
                    target_table="Faculties",
                    target_column="Id",
                ),
            ],
        )
    )

    # 12. StudentMarks
    tables.append(
        TableMetadata(
            schema_name="dbo",
            table_name="StudentMarks",
            columns=[
                ColumnMetadata(name="Id", data_type="int", nullable=False, primary_key=True, is_identity=True),
                ColumnMetadata(name="EnrollmentId", data_type="int", nullable=False),
                ColumnMetadata(name="AssessmentItemId", data_type="int", nullable=False),
                ColumnMetadata(name="ObtainedMarks", data_type="decimal(6,2)", nullable=True),
                ColumnMetadata(name="IsAbsent", data_type="bit", nullable=False, default="0"),
                ColumnMetadata(name="Remarks", data_type="varchar(max)", nullable=True),
                ColumnMetadata(name="GraderId", data_type="int", nullable=False),
                ColumnMetadata(name="GradedDate", data_type="datetime2", nullable=True),
                ColumnMetadata(name="SubmissionDate", data_type="datetime2", nullable=True),
                ColumnMetadata(name="CreatedAt", data_type="datetime2", nullable=False),
                ColumnMetadata(name="UpdatedAt", data_type="datetime2", nullable=True),
                ColumnMetadata(name="DeletedAt", data_type="datetime2", nullable=True),
            ],
            primary_keys=["Id"],
            foreign_keys=[
                ForeignKeyMetadata(
                    name="FK_StudentMarks_CourseEnrollments",
                    source_table="StudentMarks",
                    source_column="EnrollmentId",
                    target_table="CourseEnrollments",
                    target_column="Id",
                ),
                ForeignKeyMetadata(
                    name="FK_StudentMarks_AssessmentItems",
                    source_table="StudentMarks",
                    source_column="AssessmentItemId",
                    target_table="AssessmentItems",
                    target_column="Id",
                ),
                ForeignKeyMetadata(
                    name="FK_StudentMarks_Faculties",
                    source_table="StudentMarks",
                    source_column="GraderId",
                    target_table="Faculties",
                    target_column="Id",
                ),
            ],
        )
    )

    # 13. FacultyAssignments
    tables.append(
        TableMetadata(
            schema_name="dbo",
            table_name="FacultyAssignments",
            columns=[
                ColumnMetadata(name="Id", data_type="int", nullable=False, primary_key=True, is_identity=True),
                ColumnMetadata(name="CourseOfferingId", data_type="int", nullable=False),
                ColumnMetadata(name="FacultyId", data_type="int", nullable=False),
                ColumnMetadata(name="Role", data_type="varchar(50)", nullable=True),
                ColumnMetadata(name="AssignmentDate", data_type="datetime2", nullable=True),
                ColumnMetadata(name="CreatedAt", data_type="datetime2", nullable=True),
                ColumnMetadata(name="UpdatedAt", data_type="datetime2", nullable=True),
                ColumnMetadata(name="DeletedAt", data_type="datetime2", nullable=True),
            ],
            primary_keys=["Id"],
            foreign_keys=[
                ForeignKeyMetadata(
                    name="FK_FacultyAssignments_CourseOfferings",
                    source_table="FacultyAssignments",
                    source_column="CourseOfferingId",
                    target_table="CourseOfferings",
                    target_column="Id",
                ),
                ForeignKeyMetadata(
                    name="FK_FacultyAssignments_Faculties",
                    source_table="FacultyAssignments",
                    source_column="FacultyId",
                    target_table="Faculties",
                    target_column="Id",
                ),
            ],
        )
    )

    # 14. AttendanceRecords
    tables.append(
        TableMetadata(
            schema_name="dbo",
            table_name="AttendanceRecords",
            columns=[
                ColumnMetadata(name="Id", data_type="int", nullable=False, primary_key=True, is_identity=True),
                ColumnMetadata(name="EnrollmentId", data_type="int", nullable=False),
                ColumnMetadata(name="AttendanceDate", data_type="date", nullable=False),
                ColumnMetadata(name="Status", data_type="varchar(20)", nullable=False),
                ColumnMetadata(name="RecordedBy", data_type="int", nullable=True),
                ColumnMetadata(name="Remarks", data_type="varchar(MAX)", nullable=True),
                ColumnMetadata(name="CreatedAt", data_type="datetime2", nullable=True),
                ColumnMetadata(name="UpdatedAt", data_type="datetime2", nullable=True),
                ColumnMetadata(name="DeletedAt", data_type="datetime2", nullable=True),
            ],
            primary_keys=["Id"],
            foreign_keys=[
                ForeignKeyMetadata(
                    name="FK_AttendanceRecords_CourseEnrollments",
                    source_table="AttendanceRecords",
                    source_column="EnrollmentId",
                    target_table="CourseEnrollments",
                    target_column="Id",
                ),
                ForeignKeyMetadata(
                    name="FK_AttendanceRecords_Faculties",
                    source_table="AttendanceRecords",
                    source_column="RecordedBy",
                    target_table="Faculties",
                    target_column="Id",
                ),
            ],
        )
    )

    # 15. UploadHistory
    tables.append(
        TableMetadata(
            schema_name="dbo",
            table_name="UploadHistory",
            columns=[
                ColumnMetadata(name="Id", data_type="varchar(36)", nullable=False, primary_key=True),
                ColumnMetadata(name="CourseOfferingId", data_type="int", nullable=False),
                ColumnMetadata(name="AssessmentItemId", data_type="int", nullable=True),
                ColumnMetadata(name="UploadedBy", data_type="int", nullable=False),
                ColumnMetadata(name="FileName", data_type="varchar(500)", nullable=False),
                ColumnMetadata(name="FileSize", data_type="bigint", nullable=True),
                ColumnMetadata(name="SuccessCount", data_type="int", nullable=False, default="0"),
                ColumnMetadata(name="ErrorCount", data_type="int", nullable=False, default="0"),
                ColumnMetadata(name="TotalCount", data_type="int", nullable=False),
                ColumnMetadata(name="ErrorDetails", data_type="varchar(MAX)", nullable=True),
                ColumnMetadata(name="RowDataBlob", data_type="varchar(MAX)", nullable=True),
                ColumnMetadata(name="Status", data_type="varchar(50)", nullable=False, default="'Processing'"),
                ColumnMetadata(name="UploadedAt", data_type="datetime2", nullable=True),
                ColumnMetadata(name="CompletedAt", data_type="datetime2", nullable=True),
                ColumnMetadata(name="CreatedAt", data_type="datetime2", nullable=True),
                ColumnMetadata(name="UpdatedAt", data_type="datetime2", nullable=True),
                ColumnMetadata(name="DeletedAt", data_type="datetime2", nullable=True),
            ],
            primary_keys=["Id"],
            foreign_keys=[
                ForeignKeyMetadata(
                    name="FK_UploadHistory_CourseOfferings",
                    source_table="UploadHistory",
                    source_column="CourseOfferingId",
                    target_table="CourseOfferings",
                    target_column="Id",
                ),
                ForeignKeyMetadata(
                    name="FK_UploadHistory_AssessmentItems",
                    source_table="UploadHistory",
                    source_column="AssessmentItemId",
                    target_table="AssessmentItems",
                    target_column="Id",
                ),
                ForeignKeyMetadata(
                    name="FK_UploadHistory_Faculties",
                    source_table="UploadHistory",
                    source_column="UploadedBy",
                    target_table="Faculties",
                    target_column="Id",
                ),
            ],
        )
    )

    # 16. AuditLog
    tables.append(
        TableMetadata(
            schema_name="dbo",
            table_name="AuditLog",
            columns=[
                ColumnMetadata(name="Id", data_type="bigint", nullable=False, primary_key=True, is_identity=True),
                ColumnMetadata(name="Action", data_type="varchar(50)", nullable=False),
                ColumnMetadata(name="ActorUserId", data_type="int", nullable=False),
                ColumnMetadata(name="EntityName", data_type="varchar(100)", nullable=False),
                ColumnMetadata(name="EntityId", data_type="varchar(100)", nullable=False),
                ColumnMetadata(name="OldValue", data_type="varchar(MAX)", nullable=True),
                ColumnMetadata(name="NewValue", data_type="varchar(MAX)", nullable=True),
                ColumnMetadata(name="ChangedFields", data_type="varchar(MAX)", nullable=True),
                ColumnMetadata(name="OccurredAt", data_type="datetime2", nullable=True),
                ColumnMetadata(name="IPAddress", data_type="varchar(45)", nullable=True),
                ColumnMetadata(name="UserAgent", data_type="varchar(500)", nullable=True),
                ColumnMetadata(name="SessionId", data_type="varchar(255)", nullable=True),
                ColumnMetadata(name="Reason", data_type="varchar(MAX)", nullable=True),
                ColumnMetadata(name="CreatedAt", data_type="datetime2", nullable=True),
                ColumnMetadata(name="UpdatedAt", data_type="datetime2", nullable=True),
                ColumnMetadata(name="DeletedAt", data_type="datetime2", nullable=True),
            ],
            primary_keys=["Id"],
            foreign_keys=[
                ForeignKeyMetadata(
                    name="FK_AuditLog_Users",
                    source_table="AuditLog",
                    source_column="ActorUserId",
                    target_table="Users",
                    target_column="Id",
                ),
            ],
        )
    )

    # 17. Predictions
    tables.append(
        TableMetadata(
            schema_name="dbo",
            table_name="Predictions",
            columns=[
                ColumnMetadata(name="Id", data_type="varchar(36)", nullable=False, primary_key=True),
                ColumnMetadata(name="CourseEnrollmentId", data_type="int", nullable=False),
                ColumnMetadata(name="PredictedCategory", data_type="varchar(50)", nullable=False),
                ColumnMetadata(name="RiskScore", data_type="decimal(5,4)", nullable=False),
                ColumnMetadata(name="ConfidenceScore", data_type="decimal(5,4)", nullable=True),
                ColumnMetadata(name="PredictedGrade", data_type="varchar(5)", nullable=True),
                ColumnMetadata(name="PredictedMarks", data_type="decimal(6,2)", nullable=True),
                ColumnMetadata(name="ModelVersion", data_type="varchar(50)", nullable=False),
                ColumnMetadata(name="ModelAccuracy", data_type="decimal(5,4)", nullable=True),
                ColumnMetadata(name="FeatureImportance", data_type="varchar(MAX)", nullable=True),
                ColumnMetadata(name="ExplanationJson", data_type="varchar(MAX)", nullable=True),
                ColumnMetadata(name="RecommendedActions", data_type="varchar(MAX)", nullable=True),
                ColumnMetadata(name="GeneratedAt", data_type="datetime2", nullable=True),
                ColumnMetadata(name="ExpiresAt", data_type="datetime2", nullable=True),
                ColumnMetadata(name="IsActive", data_type="bit", nullable=False, default="1"),
                ColumnMetadata(name="ReviewedBy", data_type="int", nullable=True),
                ColumnMetadata(name="ReviewedAt", data_type="datetime2", nullable=True),
                ColumnMetadata(name="ReviewNotes", data_type="varchar(MAX)", nullable=True),
                ColumnMetadata(name="CreatedAt", data_type="datetime2", nullable=True),
                ColumnMetadata(name="UpdatedAt", data_type="datetime2", nullable=True),
                ColumnMetadata(name="DeletedAt", data_type="datetime2", nullable=True),
            ],
            primary_keys=["Id"],
            foreign_keys=[
                ForeignKeyMetadata(
                    name="FK_Predictions_CourseEnrollments",
                    source_table="Predictions",
                    source_column="CourseEnrollmentId",
                    target_table="CourseEnrollments",
                    target_column="Id",
                ),
                ForeignKeyMetadata(
                    name="FK_Predictions_Faculties",
                    source_table="Predictions",
                    source_column="ReviewedBy",
                    target_table="Faculties",
                    target_column="Id",
                ),
            ],
        )
    )

    return DatabaseSchema(
        database_name="GradeSense_Local",
        tables=tables,
        is_model_facing=True,
    )
