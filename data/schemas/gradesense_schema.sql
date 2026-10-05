-- ============================================================================
-- Canonical DDL Schema for QueryPilot (GradeSense)
-- Source Origin: D:\YASH\GradeSense\DB\GradeSensev3.sql
-- Reference: D:\YASH\GradeSense\DB\tables.sql (StudentMarks constraint)
-- Target Database: GradeSense_Local ONLY
-- Description: 17 core relational tables, primary keys, foreign keys, 
--              check constraints, 60 performance indexes, and extended properties.
-- ============================================================================

-- SAFETY HALT: Guard against execution in unintended environments
DECLARE @CurrentDB sysname = DB_NAME();
IF @CurrentDB <> N'GradeSense_Local'
BEGIN
    RAISERROR(N'SAFETY HALT: This script must ONLY be executed against [GradeSense_Local]. Current database is [%s]. Aborting.', 16, 1, @CurrentDB);
    SET NOEXEC ON;
END
GO

SET ANSI_NULLS ON;
GO
SET QUOTED_IDENTIFIER ON;
GO

CREATE TABLE [Users] (
  [Id] int PRIMARY KEY IDENTITY(1, 1),
  [Email] varchar(255) UNIQUE NOT NULL,
  [PasswordHash] varchar(255) NOT NULL,
  [FullName] varchar(255) NOT NULL,
  [Role] nvarchar(255) NOT NULL CHECK ([Role] IN ('Student', 'Faculty', 'Admin')),
  [IsActive] bit NOT NULL DEFAULT (1),
  [CreatedAt] datetime2 DEFAULT (sysdatetime()),
  [UpdatedAt] datetime2,
  [DeletedAt] datetime2
)
GO

CREATE TABLE [Departments] (
  [Id] int PRIMARY KEY IDENTITY(1, 1),
  [Name] varchar(255) UNIQUE NOT NULL,
  [Code] varchar(50) UNIQUE,
  [HODUserId] int,
  [IsActive] bit NOT NULL DEFAULT (1),
  [CreatedAt] datetime2 DEFAULT (sysdatetime()),
  [UpdatedAt] datetime2,
  [DeletedAt] datetime2
)
GO

CREATE TABLE [Faculties] (
  [Id] int PRIMARY KEY,
  [EmployeeId] varchar(255) UNIQUE NOT NULL,
  [DepartmentId] int NOT NULL,
  [Designation] varchar(255),
  [JoiningDate] date,
  [Qualification] varchar(255),
  [Specialization] varchar(255),
  [CreatedAt] datetime2 DEFAULT (sysdatetime()),
  [UpdatedAt] datetime2,
  [DeletedAt] datetime2
)
GO

CREATE TABLE [Students] (
  [Id] int PRIMARY KEY,
  [EnrollmentNumber] varchar(255) UNIQUE NOT NULL,
  [AdmissionYear] int NOT NULL,
  [CurrentSemester] int NOT NULL,
  [DepartmentId] int NOT NULL,
  [Status] varchar(50) NOT NULL CHECK ([Status] IN ('Active', 'Suspended', 'Graduated', 'Dropped')) DEFAULT 'Active',
  [CGPA] decimal(4,2) CHECK ([CGPA] BETWEEN 0 AND 10),
  [CreatedAt] datetime2 DEFAULT (sysdatetime()),
  [UpdatedAt] datetime2,
  [DeletedAt] datetime2
)
GO

CREATE TABLE [Batches] (
  [Id] int PRIMARY KEY IDENTITY(1, 1),
  [Name] varchar(255) NOT NULL,
  [Semester] int NOT NULL,
  [AcademicYear] int NOT NULL,
  [DepartmentId] int NOT NULL,
  [ClassCoordinatorId] int,
  [Division] varchar(10),
  [IsActive] bit NOT NULL DEFAULT (1),
  [CreatedAt] datetime2 DEFAULT (sysdatetime()),
  [UpdatedAt] datetime2,
  [DeletedAt] datetime2
)
GO

CREATE TABLE [Subjects] (
  [Id] int PRIMARY KEY IDENTITY(1, 1),
  [Code] varchar(255) UNIQUE NOT NULL,
  [Name] varchar(255) NOT NULL,
  [Credit] decimal(3,1) NOT NULL,
  [DepartmentId] int NOT NULL,
  [Semester] int,
  [SubjectType] varchar(50),
  [IsElective] bit NOT NULL DEFAULT (0),
  [PrerequisiteSubjectId] int,
  [Description] nvarchar(MAX),
  [Syllabus] nvarchar(MAX),
  [IsActive] bit NOT NULL DEFAULT (1),
  [CreatedAt] datetime2 DEFAULT (sysdatetime()),
  [UpdatedAt] datetime2,
  [DeletedAt] datetime2
)
GO

CREATE TABLE [SubjectUnits] (
  [Id] int PRIMARY KEY IDENTITY(1, 1),
  [SubjectId] int NOT NULL,
  [UnitNumber] int NOT NULL,
  [TopicName] varchar(255) NOT NULL,
  [Description] varchar(MAX),
  [TeachingHours] int NOT NULL,
  [Weightage] decimal(5,2),
  [LearningOutcomes] varchar(MAX),
  [CreatedAt] datetime2 DEFAULT (sysdatetime()),
  [UpdatedAt] datetime2,
  [DeletedAt] datetime2
)
GO

CREATE TABLE [CourseOfferings] (
  [Id] int PRIMARY KEY IDENTITY(1, 1),
  [SubjectId] int NOT NULL,
  [BatchId] int NOT NULL,
  [SubjectCoordinatorId] int NOT NULL,
  [AcademicYear] int NOT NULL,
  [StartDate] date,
  [EndDate] date,
  [MaxEnrollment] int,
  [IsActive] bit NOT NULL DEFAULT (1),
  [CreatedAt] datetime2 DEFAULT (sysdatetime()),
  [UpdatedAt] datetime2,
  [DeletedAt] datetime2
)
GO

CREATE TABLE [CourseEnrollments] (
  [Id] int PRIMARY KEY IDENTITY(1, 1),
  [CourseOfferingId] int NOT NULL,
  [StudentId] int NOT NULL,
  [RollNumber] varchar(255),
  [EnrollmentDate] datetime2 DEFAULT (sysdatetime()),
  [Status] varchar(50) NOT NULL CHECK ([Status] IN ('Active', 'Completed', 'Dropped', 'Withdrawn')) DEFAULT 'Active',
  [AttendancePercentage] decimal(5,2),
  [Grade] varchar(5),
  [GradePoints] decimal(4,2) CHECK (GradePoints BETWEEN 0 AND 10),
  [CreatedAt] datetime2 DEFAULT (sysdatetime()),
  [UpdatedAt] datetime2,
  [DeletedAt] datetime2
)
GO

CREATE TABLE [EvaluationSchemes] (
  [Id] int PRIMARY KEY IDENTITY(1, 1),
  [CourseOfferingId] int NOT NULL,
  [Name] varchar(255) NOT NULL,
  [Description] varchar(MAX),
  [TotalMarks] decimal(6,2) NOT NULL,
  [PassingMarks] decimal(6,2) NOT NULL,
  [Weight] decimal(5,2) NOT NULL,
  [EvaluationType] varchar(50),
  [IsActive] bit NOT NULL DEFAULT (1),
  [CreatedAt] datetime2 DEFAULT (sysdatetime()),
  [UpdatedAt] datetime2,
  [DeletedAt] datetime2
)
GO

CREATE TABLE [AssessmentItems] (
  [Id] int PRIMARY KEY IDENTITY(1, 1),
  [EvaluationSchemeId] int NOT NULL,
  [SubjectUnitId] int,
  [Name] varchar(255) NOT NULL,
  [Description] varchar(MAX),
  [MaxMarks] decimal(6,2) NOT NULL,
  [CalculationType] nvarchar(255) NOT NULL CHECK ([CalculationType] IN ('Raw', 'Average', 'BestOf')) DEFAULT 'Raw',
  [Weight] decimal(5,2),
  [ScheduledDate] date,
  [DueDate] date,
  [IsActive] bit NOT NULL DEFAULT (1),
  [CreatedBy] int,
  [CreatedAt] datetime2 DEFAULT (sysdatetime()),
  [UpdatedAt] datetime2,
  [DeletedAt] datetime2
)
GO

CREATE TABLE [StudentMarks] (
  [Id] int PRIMARY KEY IDENTITY(1, 1),
  [EnrollmentId] int NOT NULL,
  [AssessmentItemId] int NOT NULL,
  [ObtainedMarks] decimal(6,2),
  [IsAbsent] bit NOT NULL DEFAULT (0),
  [Remarks] varchar(max),
  [GraderId] int NOT NULL,
  [GradedDate] datetime2,
  [SubmissionDate] datetime2,
  [CreatedAt] datetime2 NOT NULL DEFAULT (sysdatetime()),
  [UpdatedAt] datetime2,
  [DeletedAt] datetime2,

  CONSTRAINT CK_StudentMarks_AbsentMarks
  CHECK (
        IsAbsent = 0
     OR ObtainedMarks IS NULL
  )
)
GO

CREATE TABLE [FacultyAssignments] (
  [Id] int PRIMARY KEY IDENTITY(1, 1),
  [CourseOfferingId] int NOT NULL,
  [FacultyId] int NOT NULL,
  [Role] varchar(50),
  [AssignmentDate] datetime2 DEFAULT (sysdatetime()),
  [CreatedAt] datetime2 DEFAULT (sysdatetime()),
  [UpdatedAt] datetime2,
  [DeletedAt] datetime2
)
GO

CREATE TABLE [AttendanceRecords] (
  [Id] int PRIMARY KEY IDENTITY(1, 1),
  [EnrollmentId] int NOT NULL,
  [AttendanceDate] date NOT NULL,
  [Status] varchar(20) NOT NULL CHECK ([Status] IN ('Present', 'Absent', 'Excused', 'Late')),
  [RecordedBy] int,
  [Remarks] varchar(MAX),
  [CreatedAt] datetime2 DEFAULT (sysdatetime()),
  [UpdatedAt] datetime2,
  [DeletedAt] datetime2
)
GO

CREATE TABLE [UploadHistory] (
  [Id] varchar(36) PRIMARY KEY,
  [CourseOfferingId] int NOT NULL,
  [AssessmentItemId] int,
  [UploadedBy] int NOT NULL,
  [FileName] varchar(500) NOT NULL,
  [FileSize] bigint,
  [SuccessCount] int NOT NULL DEFAULT (0),
  [ErrorCount] int NOT NULL DEFAULT (0),
  [TotalCount] int NOT NULL,
  [ErrorDetails] varchar(MAX),
  [RowDataBlob] varchar(MAX),
  [Status] varchar(50) NOT NULL CHECK ([Status] IN ('Processing', 'Completed', 'Failed')) DEFAULT 'Processing',
  [UploadedAt] datetime2 DEFAULT (sysdatetime()),
  [CompletedAt] datetime2,
  [CreatedAt] datetime2 DEFAULT (sysdatetime()),
  [UpdatedAt] datetime2,
  [DeletedAt] datetime2
)
GO

CREATE TABLE [AuditLog] (
  [Id] bigint PRIMARY KEY IDENTITY(1, 1),
  [Action] varchar(50) NOT NULL,
  [ActorUserId] int NOT NULL,
  [EntityName] varchar(100) NOT NULL,
  [EntityId] varchar(100) NOT NULL,
  [OldValue] varchar(MAX),
  [NewValue] varchar(MAX),
  [ChangedFields] varchar(MAX),
  [OccurredAt] datetime2 DEFAULT (sysdatetime()),
  [IPAddress] varchar(45),
  [UserAgent] varchar(500),
  [SessionId] varchar(255),
  [Reason] varchar(MAX),
  [CreatedAt] datetime2 DEFAULT (sysdatetime()),
  [UpdatedAt] datetime2,
  [DeletedAt] datetime2
)
GO

CREATE TABLE [Predictions] (
  [Id] varchar(36) PRIMARY KEY,
  [CourseEnrollmentId] int NOT NULL,
  [PredictedCategory] varchar(50) NOT NULL CHECK ([PredictedCategory] IN ('At-Risk','Safe','High-Achiever','Needs-Attention')),
  [RiskScore] decimal(5,4) NOT NULL CHECK (RiskScore >= 0 AND RiskScore <= 1),
  [ConfidenceScore] decimal(5,4) CHECK (ConfidenceScore IS NULL OR (ConfidenceScore >= 0 AND ConfidenceScore <= 1)),
  [PredictedGrade] varchar(5),
  [PredictedMarks] decimal(6,2),
  [ModelVersion] varchar(50) NOT NULL,
  [ModelAccuracy] decimal(5,4),
  [FeatureImportance] varchar(MAX),
  [ExplanationJson] varchar(MAX),
  [RecommendedActions] varchar(MAX),
  [GeneratedAt] datetime2 DEFAULT (sysdatetime()),
  [ExpiresAt] datetime2,
  [IsActive] bit NOT NULL DEFAULT (1),
  [ReviewedBy] int,
  [ReviewedAt] datetime2,
  [ReviewNotes] varchar(MAX),
  [CreatedAt] datetime2 DEFAULT (sysdatetime()),
  [UpdatedAt] datetime2,
  [DeletedAt] datetime2
)
GO

CREATE UNIQUE INDEX [idx_users_email] ON [Users] ([Email])
GO

CREATE INDEX [idx_users_role] ON [Users] ([Role])
GO

CREATE INDEX [idx_users_active] ON [Users] ([IsActive])
GO

CREATE INDEX [idx_departments_hod] ON [Departments] ([HODUserId])
GO

CREATE UNIQUE INDEX [idx_departments_name] ON [Departments] ([Name])
GO

CREATE UNIQUE INDEX [idx_departments_code] ON [Departments] ([Code])
GO

CREATE UNIQUE INDEX [idx_faculties_employee] ON [Faculties] ([EmployeeId])
GO

CREATE INDEX [idx_faculties_department] ON [Faculties] ([DepartmentId])
GO

CREATE UNIQUE INDEX [idx_students_enrollment] ON [Students] ([EnrollmentNumber])
GO

CREATE INDEX [idx_students_department] ON [Students] ([DepartmentId])
GO

CREATE INDEX [idx_students_year_sem] ON [Students] ([AdmissionYear], [CurrentSemester])
GO

CREATE INDEX [idx_batches_coordinator] ON [Batches] ([ClassCoordinatorId])
GO

CREATE INDEX [idx_batches_dept_sem_year] ON [Batches] ([DepartmentId], [Semester], [AcademicYear])
GO

CREATE UNIQUE INDEX [idx_batches_name] ON [Batches] ([Name])
GO

CREATE UNIQUE INDEX [idx_subjects_code] ON [Subjects] ([Code])
GO

CREATE INDEX [idx_subjects_department] ON [Subjects] ([DepartmentId])
GO

CREATE INDEX [idx_subjects_dept_sem] ON [Subjects] ([DepartmentId], [Semester])
GO

CREATE INDEX [idx_subject_units_subject] ON [SubjectUnits] ([SubjectId])
GO

CREATE UNIQUE INDEX [idx_subject_units_subject_num] ON [SubjectUnits] ([SubjectId], [UnitNumber])
GO

CREATE INDEX [idx_offerings_subject] ON [CourseOfferings] ([SubjectId])
GO

CREATE INDEX [idx_offerings_batch] ON [CourseOfferings] ([BatchId])
GO

CREATE INDEX [idx_offerings_coordinator] ON [CourseOfferings] ([SubjectCoordinatorId])
GO

CREATE UNIQUE INDEX [idx_offerings_unique] ON [CourseOfferings] ([SubjectId], [BatchId], [AcademicYear])
GO

CREATE INDEX [idx_enrollments_offering] ON [CourseEnrollments] ([CourseOfferingId])
GO

CREATE INDEX [idx_enrollments_student] ON [CourseEnrollments] ([StudentId])
GO

CREATE UNIQUE INDEX [idx_enrollments_unique] ON [CourseEnrollments] ([CourseOfferingId], [StudentId])
GO

CREATE INDEX [idx_enrollments_roll] ON [CourseEnrollments] ([RollNumber])
GO

CREATE INDEX [idx_eval_schemes_offering] ON [EvaluationSchemes] ([CourseOfferingId])
GO

CREATE UNIQUE INDEX [idx_eval_schemes_unique] ON [EvaluationSchemes] ([CourseOfferingId], [Name])
GO

CREATE INDEX [idx_assessments_scheme] ON [AssessmentItems] ([EvaluationSchemeId])
GO

CREATE INDEX [idx_assessments_unit] ON [AssessmentItems] ([SubjectUnitId])
GO

CREATE INDEX [idx_assessments_date] ON [AssessmentItems] ([ScheduledDate])
GO

CREATE INDEX [idx_marks_enrollment] ON [StudentMarks] ([EnrollmentId])
GO

CREATE INDEX [idx_marks_assessment] ON [StudentMarks] ([AssessmentItemId])
GO

CREATE UNIQUE INDEX [idx_marks_unique] ON [StudentMarks] ([EnrollmentId], [AssessmentItemId])
GO

CREATE INDEX [idx_marks_grader] ON [StudentMarks] ([GraderId])
GO

CREATE INDEX [idx_faculty_assign_course] ON [FacultyAssignments] ([CourseOfferingId])
GO

CREATE INDEX [idx_faculty_assign_faculty] ON [FacultyAssignments] ([FacultyId])
GO

CREATE UNIQUE INDEX [idx_faculty_assign_unique] ON [FacultyAssignments] ([CourseOfferingId], [FacultyId])
GO

CREATE INDEX [idx_attendance_enrollment] ON [AttendanceRecords] ([EnrollmentId])
GO

CREATE INDEX [idx_attendance_date] ON [AttendanceRecords] ([AttendanceDate])
GO

CREATE UNIQUE INDEX [idx_attendance_unique] ON [AttendanceRecords] ([EnrollmentId], [AttendanceDate])
GO

CREATE INDEX [idx_upload_offering] ON [UploadHistory] ([CourseOfferingId])
GO

CREATE INDEX [idx_upload_assessment] ON [UploadHistory] ([AssessmentItemId])
GO

CREATE INDEX [idx_upload_user] ON [UploadHistory] ([UploadedBy])
GO

CREATE INDEX [idx_upload_date] ON [UploadHistory] ([UploadedAt])
GO

CREATE INDEX [idx_upload_status] ON [UploadHistory] ([Status])
GO

CREATE INDEX [idx_audit_actor] ON [AuditLog] ([ActorUserId])
GO

CREATE INDEX [idx_audit_entity] ON [AuditLog] ([EntityName])
GO

CREATE INDEX [idx_audit_entity_id] ON [AuditLog] ([EntityName], [EntityId])
GO

CREATE INDEX [idx_audit_occurredat] ON [AuditLog] ([OccurredAt])
GO

CREATE INDEX [idx_audit_action] ON [AuditLog] ([Action])
GO

CREATE INDEX [idx_audit_session] ON [AuditLog] ([SessionId])
GO

CREATE INDEX [idx_predictions_enrollment] ON [Predictions] ([CourseEnrollmentId])
GO

CREATE INDEX [idx_predictions_category] ON [Predictions] ([PredictedCategory])
GO

CREATE INDEX [idx_predictions_risk] ON [Predictions] ([RiskScore])
GO

CREATE INDEX [idx_predictions_date] ON [Predictions] ([GeneratedAt])
GO

CREATE INDEX [idx_predictions_active] ON [Predictions] ([CourseEnrollmentId], [IsActive])
GO

CREATE UNIQUE INDEX [idx_predictions_active_unique] ON [Predictions] ([CourseEnrollmentId])
GO

CREATE INDEX [idx_predictions_model] ON [Predictions] ([ModelVersion])
GO

EXEC sp_addextendedproperty
@name = N'Table_Description',
@value = 'Base table for all users in the system. Uses single-table inheritance pattern with role-based extensions.',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Users';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Unique identifier for the user',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Users',
@level2type = N'Column', @level2name = 'Id';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'User email address for authentication and communication',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Users',
@level2type = N'Column', @level2name = 'Email';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Securely hashed password using bcrypt or similar',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Users',
@level2type = N'Column', @level2name = 'PasswordHash';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Complete name of the user',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Users',
@level2type = N'Column', @level2name = 'FullName';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'User role determining access permissions',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Users',
@level2type = N'Column', @level2name = 'Role';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Flag indicating if user account is currently active',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Users',
@level2type = N'Column', @level2name = 'IsActive';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'datetime2 when user was created',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Users',
@level2type = N'Column', @level2name = 'CreatedAt';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'datetime2 of last user information update',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Users',
@level2type = N'Column', @level2name = 'UpdatedAt';
GO

EXEC sp_addextendedproperty
@name = N'Table_Description',
@value = 'Represents academic departments within the university. Each department has a Head of Department (HOD).',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Departments';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Unique identifier for the department',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Departments',
@level2type = N'Column', @level2name = 'Id';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Department name (e.g., Computer Engineering, Mechanical Engineering)',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Departments',
@level2type = N'Column', @level2name = 'Name';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Short department code (e.g., CE, ME, EE)',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Departments',
@level2type = N'Column', @level2name = 'Code';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Head of Department - must be a Faculty user',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Departments',
@level2type = N'Column', @level2name = 'HODUserId';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Flag indicating if department is currently operational',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Departments',
@level2type = N'Column', @level2name = 'IsActive';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'datetime2 when department was created',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Departments',
@level2type = N'Column', @level2name = 'CreatedAt';
GO

EXEC sp_addextendedproperty
@name = N'Table_Description',
@value = 'Extends Users table for faculty members. Contains faculty-specific information like employee ID and designation.',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Faculties';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Foreign key to Users.Id (one-to-one relationship)',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Faculties',
@level2type = N'Column', @level2name = 'Id';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Unique employee identifier for the faculty member',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Faculties',
@level2type = N'Column', @level2name = 'EmployeeId';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Department to which the faculty belongs',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Faculties',
@level2type = N'Column', @level2name = 'DepartmentId';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Faculty designation (e.g., Assistant Professor, Associate Professor, Professor, Dean)',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Faculties',
@level2type = N'Column', @level2name = 'Designation';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Date when faculty joined the institution',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Faculties',
@level2type = N'Column', @level2name = 'JoiningDate';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Highest qualification (e.g., Ph.D., M.Tech)',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Faculties',
@level2type = N'Column', @level2name = 'Qualification';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Area of specialization or expertise',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Faculties',
@level2type = N'Column', @level2name = 'Specialization';
GO

EXEC sp_addextendedproperty
@name = N'Table_Description',
@value = 'Extends Users table for students. Enrollment number is permanent and never changes throughout academic career.',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Students';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Foreign key to Users.Id (one-to-one relationship)',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Students',
@level2type = N'Column', @level2name = 'Id';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Permanent unique enrollment number assigned at admission',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Students',
@level2type = N'Column', @level2name = 'EnrollmentNumber';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Year when student was admitted (e.g., 2021, 2022)',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Students',
@level2type = N'Column', @level2name = 'AdmissionYear';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Current semester the student is enrolled in (1-8 for 4-year programs)',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Students',
@level2type = N'Column', @level2name = 'CurrentSemester';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Department in which student is enrolled',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Students',
@level2type = N'Column', @level2name = 'DepartmentId';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Student status (Active, Suspended, Graduated, Dropped)',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Students',
@level2type = N'Column', @level2name = 'Status';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Cumulative Grade Point Average',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Students',
@level2type = N'Column', @level2name = 'CGPA';
GO

EXEC sp_addextendedproperty
@name = N'Table_Description',
@value = 'Represents a group of students studying together (e.g., a class division). Used for course scheduling and attendance.',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Batches';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Unique identifier for the batch',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Batches',
@level2type = N'Column', @level2name = 'Id';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Descriptive batch name (e.g., 2025-CE-Sem5-DivA)',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Batches',
@level2type = N'Column', @level2name = 'Name';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Semester number for this batch (1-8)',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Batches',
@level2type = N'Column', @level2name = 'Semester';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Academic year (e.g., 2024 for 2024-25)',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Batches',
@level2type = N'Column', @level2name = 'AcademicYear';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Department this batch belongs to',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Batches',
@level2type = N'Column', @level2name = 'DepartmentId';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Faculty member coordinating this batch',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Batches',
@level2type = N'Column', @level2name = 'ClassCoordinatorId';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Division identifier (A, B, C, etc.)',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Batches',
@level2type = N'Column', @level2name = 'Division';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Flag indicating if batch is currently active',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Batches',
@level2type = N'Column', @level2name = 'IsActive';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'datetime2 when batch was created',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Batches',
@level2type = N'Column', @level2name = 'CreatedAt';
GO

EXEC sp_addextendedproperty
@name = N'Table_Description',
@value = 'Master definition of subjects/courses offered by the university. This is the catalog data.',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Subjects';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Unique identifier for the subject',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Subjects',
@level2type = N'Column', @level2name = 'Id';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Official subject code (e.g., 2180703, CS301)',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Subjects',
@level2type = N'Column', @level2name = 'Code';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Subject name (e.g., Machine Learning, Data Structures)',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Subjects',
@level2type = N'Column', @level2name = 'Name';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Credit hours for the subject (e.g., 3.0, 4.5)',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Subjects',
@level2type = N'Column', @level2name = 'Credit';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Department offering this subject',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Subjects',
@level2type = N'Column', @level2name = 'DepartmentId';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Recommended semester for this subject (1-8)',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Subjects',
@level2type = N'Column', @level2name = 'Semester';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Type of subject (Theory, Practical, Lab, Project)',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Subjects',
@level2type = N'Column', @level2name = 'SubjectType';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Indicates if subject is an elective',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Subjects',
@level2type = N'Column', @level2name = 'IsElective';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Subject that must be completed before this one',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Subjects',
@level2type = N'Column', @level2name = 'PrerequisiteSubjectId';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Detailed subject description and objectives',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Subjects',
@level2type = N'Column', @level2name = 'Description';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Complete syllabus content',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Subjects',
@level2type = N'Column', @level2name = 'Syllabus';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Flag indicating if subject is currently offered',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Subjects',
@level2type = N'Column', @level2name = 'IsActive';
GO

EXEC sp_addextendedproperty
@name = N'Table_Description',
@value = 'Breakdown of subjects into units/modules. Helps in organizing content and unit-wise assessment.',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'SubjectUnits';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Unique identifier for the subject unit',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'SubjectUnits',
@level2type = N'Column', @level2name = 'Id';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Subject to which this unit belongs',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'SubjectUnits',
@level2type = N'Column', @level2name = 'SubjectId';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Sequential unit number (1, 2, 3, etc.)',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'SubjectUnits',
@level2type = N'Column', @level2name = 'UnitNumber';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Main topic/theme of the unit (e.g., Supervised Learning, Sorting Algorithms)',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'SubjectUnits',
@level2type = N'Column', @level2name = 'TopicName';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Detailed description of topics covered in this unit',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'SubjectUnits',
@level2type = N'Column', @level2name = 'Description';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Number of teaching hours allocated for this unit',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'SubjectUnits',
@level2type = N'Column', @level2name = 'TeachingHours';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Weightage of this unit in final evaluation (percentage)',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'SubjectUnits',
@level2type = N'Column', @level2name = 'Weightage';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Expected learning outcomes after completing this unit',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'SubjectUnits',
@level2type = N'Column', @level2name = 'LearningOutcomes';
GO

EXEC sp_addextendedproperty
@name = N'Table_Description',
@value = 'Represents a specific instance of a subject being taught to a batch in an academic year. Links catalog to actual teaching.',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'CourseOfferings';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Unique identifier for the course offering',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'CourseOfferings',
@level2type = N'Column', @level2name = 'Id';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Subject being offered in this course',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'CourseOfferings',
@level2type = N'Column', @level2name = 'SubjectId';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Batch for which this course is offered',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'CourseOfferings',
@level2type = N'Column', @level2name = 'BatchId';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Faculty coordinating this course offering',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'CourseOfferings',
@level2type = N'Column', @level2name = 'SubjectCoordinatorId';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Academic year of the offering (e.g., 2024 for 2024-25)',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'CourseOfferings',
@level2type = N'Column', @level2name = 'AcademicYear';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Course start date',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'CourseOfferings',
@level2type = N'Column', @level2name = 'StartDate';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Course end date',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'CourseOfferings',
@level2type = N'Column', @level2name = 'EndDate';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Maximum number of students that can enroll',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'CourseOfferings',
@level2type = N'Column', @level2name = 'MaxEnrollment';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Flag indicating if offering is currently active',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'CourseOfferings',
@level2type = N'Column', @level2name = 'IsActive';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'datetime2 when course offering was created',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'CourseOfferings',
@level2type = N'Column', @level2name = 'CreatedAt';
GO

EXEC sp_addextendedproperty
@name = N'Table_Description',
@value = 'Links students to specific course offerings. Represents actual enrollment and tracks student progress in each course.',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'CourseEnrollments';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Unique identifier for the enrollment record',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'CourseEnrollments',
@level2type = N'Column', @level2name = 'Id';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Course offering in which student is enrolled',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'CourseEnrollments',
@level2type = N'Column', @level2name = 'CourseOfferingId';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Student who is enrolled in the course',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'CourseEnrollments',
@level2type = N'Column', @level2name = 'StudentId';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Roll number specific to this semester/class (can change per semester)',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'CourseEnrollments',
@level2type = N'Column', @level2name = 'RollNumber';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'When student enrolled in this course',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'CourseEnrollments',
@level2type = N'Column', @level2name = 'EnrollmentDate';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Enrollment status (Active, Dropped, Completed, Failed)',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'CourseEnrollments',
@level2type = N'Column', @level2name = 'Status';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Overall attendance percentage for this course',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'CourseEnrollments',
@level2type = N'Column', @level2name = 'AttendancePercentage';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Final grade received (A+, A, B+, B, etc.)',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'CourseEnrollments',
@level2type = N'Column', @level2name = 'Grade';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Grade points earned (10.0, 9.0, 8.0, etc.)',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'CourseEnrollments',
@level2type = N'Column', @level2name = 'GradePoints';
GO

EXEC sp_addextendedproperty
@name = N'Table_Description',
@value = 'Defines major evaluation buckets/components for a course (e.g., Internal, External, Lab). Sum of weights should ideally be 100.',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'EvaluationSchemes';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Unique identifier for the evaluation scheme',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'EvaluationSchemes',
@level2type = N'Column', @level2name = 'Id';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Course offering for which this scheme is defined',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'EvaluationSchemes',
@level2type = N'Column', @level2name = 'CourseOfferingId';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Name of the evaluation component (e.g., Internal Theory, External Practical, Lab Work)',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'EvaluationSchemes',
@level2type = N'Column', @level2name = 'Name';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Detailed description of this evaluation component',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'EvaluationSchemes',
@level2type = N'Column', @level2name = 'Description';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Total maximum marks for this evaluation scheme',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'EvaluationSchemes',
@level2type = N'Column', @level2name = 'TotalMarks';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Minimum marks required to pass this component',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'EvaluationSchemes',
@level2type = N'Column', @level2name = 'PassingMarks';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Weight/percentage contribution to final grade (e.g., 30 for 30%)',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'EvaluationSchemes',
@level2type = N'Column', @level2name = 'Weight';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Type of evaluation (Internal, External, Continuous)',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'EvaluationSchemes',
@level2type = N'Column', @level2name = 'EvaluationType';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Flag indicating if scheme is currently active',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'EvaluationSchemes',
@level2type = N'Column', @level2name = 'IsActive';
GO

EXEC sp_addextendedproperty
@name = N'Table_Description',
@value = 'Individual assessment events within an evaluation scheme. Examples: quizzes, tests, assignments, labs, projects.',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'AssessmentItems';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Unique identifier for the assessment item',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'AssessmentItems',
@level2type = N'Column', @level2name = 'Id';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Evaluation scheme this assessment belongs to',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'AssessmentItems',
@level2type = N'Column', @level2name = 'EvaluationSchemeId';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Optional reference to subject unit if assessment is unit-specific',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'AssessmentItems',
@level2type = N'Column', @level2name = 'SubjectUnitId';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Name of the assessment (e.g., Unit 1 Test, Midsem Exam, Assignment 1, Lab 3)',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'AssessmentItems',
@level2type = N'Column', @level2name = 'Name';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Detailed description of the assessment',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'AssessmentItems',
@level2type = N'Column', @level2name = 'Description';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Maximum marks for this assessment item',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'AssessmentItems',
@level2type = N'Column', @level2name = 'MaxMarks';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'How marks are calculated if multiple attempts exist',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'AssessmentItems',
@level2type = N'Column', @level2name = 'CalculationType';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Weight of this item within its evaluation scheme (percentage)',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'AssessmentItems',
@level2type = N'Column', @level2name = 'Weight';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Scheduled date for this assessment',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'AssessmentItems',
@level2type = N'Column', @level2name = 'ScheduledDate';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Due date for submission (for assignments/projects)',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'AssessmentItems',
@level2type = N'Column', @level2name = 'DueDate';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Flag indicating if assessment is currently active',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'AssessmentItems',
@level2type = N'Column', @level2name = 'IsActive';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Faculty who created this assessment',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'AssessmentItems',
@level2type = N'Column', @level2name = 'CreatedBy';
GO

EXEC sp_addextendedproperty
@name = N'Table_Description',
@value = 'Stores individual student marks for each assessment. Critical table for grade calculation and performance tracking.',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'StudentMarks';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Unique identifier for the marks record',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'StudentMarks',
@level2type = N'Column', @level2name = 'Id';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Enrollment record linking student to course offering',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'StudentMarks',
@level2type = N'Column', @level2name = 'EnrollmentId';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Assessment item for which marks are recorded',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'StudentMarks',
@level2type = N'Column', @level2name = 'AssessmentItemId';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Actual marks obtained by student (null if absent)',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'StudentMarks',
@level2type = N'Column', @level2name = 'ObtainedMarks';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Flag indicating if student was absent for this assessment',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'StudentMarks',
@level2type = N'Column', @level2name = 'IsAbsent';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Additional remarks or comments about performance',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'StudentMarks',
@level2type = N'Column', @level2name = 'Remarks';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Faculty who graded this assessment',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'StudentMarks',
@level2type = N'Column', @level2name = 'GraderId';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'When the marks were entered/graded',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'StudentMarks',
@level2type = N'Column', @level2name = 'GradedDate';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'When student submitted (for assignments/projects)',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'StudentMarks',
@level2type = N'Column', @level2name = 'SubmissionDate';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'datetime2 when record was created',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'StudentMarks',
@level2type = N'Column', @level2name = 'CreatedAt';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'datetime2 of last update to marks',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'StudentMarks',
@level2type = N'Column', @level2name = 'UpdatedAt';
GO

EXEC sp_addextendedproperty
@name = N'Table_Description',
@value = 'Tracks multiple faculty members teaching the same course (co-teachers, TAs, lab instructors).',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'FacultyAssignments';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Unique identifier for faculty assignment',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'FacultyAssignments',
@level2type = N'Column', @level2name = 'Id';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Course offering being taught',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'FacultyAssignments',
@level2type = N'Column', @level2name = 'CourseOfferingId';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Faculty member assigned to teach',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'FacultyAssignments',
@level2type = N'Column', @level2name = 'FacultyId';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Role in course (Coordinator, Co-Teacher, Lab Instructor, TA)',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'FacultyAssignments',
@level2type = N'Column', @level2name = 'Role';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'When faculty was assigned',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'FacultyAssignments',
@level2type = N'Column', @level2name = 'AssignmentDate';
GO

EXEC sp_addextendedproperty
@name = N'Table_Description',
@value = 'Daily attendance tracking for students in each course offering.',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'AttendanceRecords';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Unique identifier for attendance record',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'AttendanceRecords',
@level2type = N'Column', @level2name = 'Id';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Student enrollment record',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'AttendanceRecords',
@level2type = N'Column', @level2name = 'EnrollmentId';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Date of attendance',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'AttendanceRecords',
@level2type = N'Column', @level2name = 'AttendanceDate';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Attendance status (Present, Absent, Late, Excused)',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'AttendanceRecords',
@level2type = N'Column', @level2name = 'Status';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Faculty who recorded attendance',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'AttendanceRecords',
@level2type = N'Column', @level2name = 'RecordedBy';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Additional notes about attendance',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'AttendanceRecords',
@level2type = N'Column', @level2name = 'Remarks';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'When record was created',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'AttendanceRecords',
@level2type = N'Column', @level2name = 'CreatedAt';
GO

EXEC sp_addextendedproperty
@name = N'Table_Description',
@value = 'Tracks all CSV upload transactions for marks, attendance, or other bulk data imports. Critical for audit trail and rollback capability.',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'UploadHistory';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'GUID/UUID for unique identification of upload transaction',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'UploadHistory',
@level2type = N'Column', @level2name = 'Id';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Course offering for which data was uploaded',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'UploadHistory',
@level2type = N'Column', @level2name = 'CourseOfferingId';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Optional: Specific assessment if upload was for particular test/assignment',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'UploadHistory',
@level2type = N'Column', @level2name = 'AssessmentItemId';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Faculty member who performed the upload',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'UploadHistory',
@level2type = N'Column', @level2name = 'UploadedBy';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Original filename of the uploaded CSV file',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'UploadHistory',
@level2type = N'Column', @level2name = 'FileName';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Size of uploaded file in bytes',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'UploadHistory',
@level2type = N'Column', @level2name = 'FileSize';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Number of records successfully processed',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'UploadHistory',
@level2type = N'Column', @level2name = 'SuccessCount';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Number of records that failed to process',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'UploadHistory',
@level2type = N'Column', @level2name = 'ErrorCount';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Total number of records in the CSV file',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'UploadHistory',
@level2type = N'Column', @level2name = 'TotalCount';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'JSON array of error messages and row numbers for failed records',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'UploadHistory',
@level2type = N'Column', @level2name = 'ErrorDetails';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Complete raw CSV content stored for rollback/audit purposes',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'UploadHistory',
@level2type = N'Column', @level2name = 'RowDataBlob';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Upload status (Processing, Completed, Failed, Rolled Back)',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'UploadHistory',
@level2type = N'Column', @level2name = 'Status';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'datetime2 when upload was initiated',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'UploadHistory',
@level2type = N'Column', @level2name = 'UploadedAt';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'datetime2 when upload processing completed',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'UploadHistory',
@level2type = N'Column', @level2name = 'CompletedAt';
GO

EXEC sp_addextendedproperty
@name = N'Table_Description',
@value = 'Comprehensive audit trail for all critical operations. Essential for security, compliance, and debugging. Should be write-only and never deleted.',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'AuditLog';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Unique identifier for audit log entry (use bigint for high-volume logging)',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'AuditLog',
@level2type = N'Column', @level2name = 'Id';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Type of action performed (CREATE, UPDATE, DELETE, LOGIN, LOGOUT, EXPORT, IMPORT)',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'AuditLog',
@level2type = N'Column', @level2name = 'Action';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'User who performed the action',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'AuditLog',
@level2type = N'Column', @level2name = 'ActorUserId';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Name of the table/entity affected (e.g., StudentMarks, Users, CourseEnrollments)',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'AuditLog',
@level2type = N'Column', @level2name = 'EntityName';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Primary key value of the affected record',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'AuditLog',
@level2type = N'Column', @level2name = 'EntityId';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Previous state of the record in JSON format (for UPDATE/DELETE operations)',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'AuditLog',
@level2type = N'Column', @level2name = 'OldValue';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'New state of the record in JSON format (for CREATE/UPDATE operations)',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'AuditLog',
@level2type = N'Column', @level2name = 'NewValue';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Comma-separated list of field names that were modified',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'AuditLog',
@level2type = N'Column', @level2name = 'ChangedFields';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Exact datetime2 when action occurred',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'AuditLog',
@level2type = N'Column', @level2name = 'OccurredAt';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'IP address of the user (supports both IPv4 and IPv6)',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'AuditLog',
@level2type = N'Column', @level2name = 'IPAddress';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Browser/client user agent string',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'AuditLog',
@level2type = N'Column', @level2name = 'UserAgent';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Session identifier for grouping related actions',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'AuditLog',
@level2type = N'Column', @level2name = 'SessionId';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Optional: Reason provided by user for sensitive operations',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'AuditLog',
@level2type = N'Column', @level2name = 'Reason';
GO

EXEC sp_addextendedproperty
@name = N'Table_Description',
@value = 'Stores ML-based predictions for student performance and risk assessment. Enables early intervention for at-risk students.',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Predictions';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'GUID/UUID for unique identification of prediction record',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Predictions',
@level2type = N'Column', @level2name = 'Id';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Links prediction to specific student enrollment in a course offering',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Predictions',
@level2type = N'Column', @level2name = 'CourseEnrollmentId';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Risk category (e.g., At-Risk, Safe, High-Achiever, Needs-Attention)',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Predictions',
@level2type = N'Column', @level2name = 'PredictedCategory';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Numerical risk score between 0.0 and 1.0 (e.g., 0.8542 = 85.42% risk)',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Predictions',
@level2type = N'Column', @level2name = 'RiskScore';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Model confidence in prediction (0.0 to 1.0)',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Predictions',
@level2type = N'Column', @level2name = 'ConfidenceScore';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Projected final grade (e.g., B+, 72.5)',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Predictions',
@level2type = N'Column', @level2name = 'PredictedGrade';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Projected final marks out of total',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Predictions',
@level2type = N'Column', @level2name = 'PredictedMarks';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Version of ML model used (e.g., v1.0-LogisticReg, v2.1-RandomForest)',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Predictions',
@level2type = N'Column', @level2name = 'ModelVersion';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Historical accuracy of this model version',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Predictions',
@level2type = N'Column', @level2name = 'ModelAccuracy';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'JSON object showing which features most influenced prediction',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Predictions',
@level2type = N'Column', @level2name = 'FeatureImportance';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'JSON explanation for UI (e.g., {[low_attendance]: true, [failed_unit_1]: true, [missing_assignments]: 3})',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Predictions',
@level2type = N'Column', @level2name = 'ExplanationJson';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'JSON array of suggested interventions for at-risk students',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Predictions',
@level2type = N'Column', @level2name = 'RecommendedActions';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'datetime2 when prediction was generated',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Predictions',
@level2type = N'Column', @level2name = 'GeneratedAt';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'When prediction should be regenerated (predictions get stale)',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Predictions',
@level2type = N'Column', @level2name = 'ExpiresAt';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Whether this is the current active prediction (allows versioning)',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Predictions',
@level2type = N'Column', @level2name = 'IsActive';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Faculty who reviewed/acknowledged this prediction',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Predictions',
@level2type = N'Column', @level2name = 'ReviewedBy';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'When faculty reviewed the prediction',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Predictions',
@level2type = N'Column', @level2name = 'ReviewedAt';
GO

EXEC sp_addextendedproperty
@name = N'Column_Description',
@value = 'Faculty notes about intervention taken or prediction accuracy',
@level0type = N'Schema', @level0name = 'dbo',
@level1type = N'Table',  @level1name = 'Predictions',
@level2type = N'Column', @level2name = 'ReviewNotes';
GO

ALTER TABLE [Departments] ADD FOREIGN KEY ([HODUserId]) REFERENCES [Users] ([Id])
GO

ALTER TABLE [Faculties] ADD FOREIGN KEY ([Id]) REFERENCES [Users] ([Id])
GO

ALTER TABLE [Faculties] ADD FOREIGN KEY ([DepartmentId]) REFERENCES [Departments] ([Id])
GO

ALTER TABLE [Students] ADD FOREIGN KEY ([Id]) REFERENCES [Users] ([Id])
GO

ALTER TABLE [Students] ADD FOREIGN KEY ([DepartmentId]) REFERENCES [Departments] ([Id])
GO

ALTER TABLE [Batches] ADD FOREIGN KEY ([DepartmentId]) REFERENCES [Departments] ([Id])
GO

ALTER TABLE [Batches] ADD FOREIGN KEY ([ClassCoordinatorId]) REFERENCES [Faculties] ([Id])
GO

ALTER TABLE [Subjects] ADD FOREIGN KEY ([DepartmentId]) REFERENCES [Departments] ([Id])
GO

ALTER TABLE [Subjects] ADD FOREIGN KEY ([PrerequisiteSubjectId]) REFERENCES [Subjects] ([Id])
GO

ALTER TABLE [SubjectUnits] ADD FOREIGN KEY ([SubjectId]) REFERENCES [Subjects] ([Id])
GO

ALTER TABLE [CourseOfferings] ADD FOREIGN KEY ([SubjectId]) REFERENCES [Subjects] ([Id])
GO

ALTER TABLE [CourseOfferings] ADD FOREIGN KEY ([BatchId]) REFERENCES [Batches] ([Id])
GO

ALTER TABLE [CourseOfferings] ADD FOREIGN KEY ([SubjectCoordinatorId]) REFERENCES [Faculties] ([Id])
GO

ALTER TABLE [CourseEnrollments] ADD FOREIGN KEY ([CourseOfferingId]) REFERENCES [CourseOfferings] ([Id])
GO

ALTER TABLE [CourseEnrollments] ADD FOREIGN KEY ([StudentId]) REFERENCES [Students] ([Id])
GO

ALTER TABLE [EvaluationSchemes] ADD FOREIGN KEY ([CourseOfferingId]) REFERENCES [CourseOfferings] ([Id])
GO

ALTER TABLE [AssessmentItems] ADD FOREIGN KEY ([EvaluationSchemeId]) REFERENCES [EvaluationSchemes] ([Id])
GO

ALTER TABLE [AssessmentItems] ADD FOREIGN KEY ([SubjectUnitId]) REFERENCES [SubjectUnits] ([Id])
GO

ALTER TABLE [AssessmentItems] ADD FOREIGN KEY ([CreatedBy]) REFERENCES [Faculties] ([Id])
GO

ALTER TABLE [StudentMarks] ADD FOREIGN KEY ([EnrollmentId]) REFERENCES [CourseEnrollments] ([Id])
GO

ALTER TABLE [StudentMarks] ADD FOREIGN KEY ([AssessmentItemId]) REFERENCES [AssessmentItems] ([Id])
GO

ALTER TABLE [StudentMarks] ADD FOREIGN KEY ([GraderId]) REFERENCES [Faculties] ([Id])
GO

ALTER TABLE [FacultyAssignments] ADD FOREIGN KEY ([CourseOfferingId]) REFERENCES [CourseOfferings] ([Id])
GO

ALTER TABLE [FacultyAssignments] ADD FOREIGN KEY ([FacultyId]) REFERENCES [Faculties] ([Id])
GO

ALTER TABLE [AttendanceRecords] ADD FOREIGN KEY ([EnrollmentId]) REFERENCES [CourseEnrollments] ([Id])
GO

ALTER TABLE [AttendanceRecords] ADD FOREIGN KEY ([RecordedBy]) REFERENCES [Faculties] ([Id])
GO

ALTER TABLE [UploadHistory] ADD FOREIGN KEY ([CourseOfferingId]) REFERENCES [CourseOfferings] ([Id])
GO

ALTER TABLE [UploadHistory] ADD FOREIGN KEY ([AssessmentItemId]) REFERENCES [AssessmentItems] ([Id])
GO

ALTER TABLE [UploadHistory] ADD FOREIGN KEY ([UploadedBy]) REFERENCES [Faculties] ([Id])
GO

ALTER TABLE [AuditLog] ADD FOREIGN KEY ([ActorUserId]) REFERENCES [Users] ([Id])
GO

ALTER TABLE [Predictions] ADD FOREIGN KEY ([CourseEnrollmentId]) REFERENCES [CourseEnrollments] ([Id])
GO

ALTER TABLE [Predictions] ADD FOREIGN KEY ([ReviewedBy]) REFERENCES [Faculties] ([Id])
GO
