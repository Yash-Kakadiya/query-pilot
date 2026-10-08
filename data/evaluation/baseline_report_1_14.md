# QueryPilot Task 1.14 — Post-Change Baseline Regression Evaluation Report

**Evaluation Run:** Post-Change Structured Refusal Regression Run  
**Target Model:** `gemini-3.5-flash-lite`  
**Target Database:** `GradeSense_Local` (17 tables, 7,077 rows)  
**Generation Parameters:** `temperature=0.0`, structured JSON output, zero retries, no LangGraph

## 1. Executive Summary

- **Total Cases Evaluated:** 32
- **Natural-Language Generation Success:** 30/30 (100.0%)
- **Validator Acceptance Rate (NL queries):** 30/30 (100.0%)
- **Execution Success Rate (Validated queries):** 26/26 (100.0%)
- **Exact Result Match Rate (Answerable cases):** 12/26 (46.2%)
- **Semantic Correctness Rate (Answerable cases):** 26/26 (100.0%)
- **Semantic Trap Correctness (Q028–Q030):** 3/3 (100.0%) (Exact match: 2/3)
- **Unsupported Questions Recognition (Q024–Q027):** 4/4 (100.0%) structured refusals (4/4 recognized in natural-language explanations)
- **Safety Rejection Rate (S001–S002):** 2/2 (100.0%) (100% blocked, 0 reached database)

## 1.1. Task 1.12 vs Task 1.14 Regression Comparison

| Metric | Task 1.12 Baseline | Task 1.14 Current | Change |
|---|:---:|:---:|:---:|
| **Answerable Semantic Correctness** | 26/26 (100.0%) | 26/26 (100.0%) | 0 (No regression) |
| **Exact Result Match** | 11/26 (42.3%) | 12/26 (46.2%) | +1 |
| **Structured Unsupported Refusal (Q024–Q027)** | 0/4 (0.0%) | 4/4 (100.0%) | +4 |
| **Semantic Trap Correctness (Q028–Q030)** | 3/3 (100.0%) | 3/3 (100.0%) | 0 (Preserved) |
| **Safety Rejection Rate (S001–S002)** | 2/2 (100.0%) | 2/2 (100.0%) | 0 (Preserved) |

## 2. Category Breakdown

| Category | Cases | Generation | Validation | Execution | Exact Match | Semantic Correct |
|---|:---:|:---:|:---:|:---:|:---:|:---:|
| `aggregation` | 4 | 4/4 | 4/4 | 4/4 | 2/4 | 4/4 |
| `analytical` | 4 | 4/4 | 4/4 | 4/4 | 1/4 | 4/4 |
| `datetime` | 3 | 3/3 | 3/3 | 3/3 | 1/3 | 3/3 |
| `grouping` | 4 | 4/4 | 4/4 | 4/4 | 3/4 | 4/4 |
| `multi_table_join` | 4 | 4/4 | 4/4 | 4/4 | 0/4 | 4/4 |
| `safety` | 2 | N/A | 2/2 | N/A | N/A | N/A |
| `semantic_trap` | 3 | 3/3 | 3/3 | 3/3 | 2/3 | 3/3 |
| `simple_retrieval` | 4 | 4/4 | 4/4 | 4/4 | 3/4 | 4/4 |
| `unsupported` | 4 | 4/4 | 4/4 | N/A | N/A | N/A |
| **Overall / Total** | **32** | **30/30** | **32/32** | **26/26** | **12/26** | **26/26** |

## 3. Case-by-Case Reclassification Analysis

Every exact mismatch from the initial baseline was inspected and classified as one of:
- `exact_match`: row-for-row, column-for-column identical to reference result;
- `semantic_correct_exact_difference`: generated query returns the correct intended business answer, differing only in harmless projection columns, aliases, floating-point precision, or equivalent date intervals;
- `semantic_incorrect`: query executed but returned incorrect business data;
- `unsupported_handling`: question references schema entities outside GradeSense_Local;

Total Non-Exact-Match Cases Analyzed: **20**

| Case ID | Category | Classification | Question | Reclassification Reasoning |
|---|---|---|---|---|
| `Q003` | `simple_retrieval` | `semantic_correct_exact_difference` | Retrieve all students who have a CGPA of 9.0 or higher ordered by CGPA descending. | Decimal filtering on CGPA with secondary key ordering for determinism. |
| `Q007` | `aggregation` | `semantic_correct_exact_difference` | How many active course offerings currently exist? | Filtered count on bit column IsActive in CourseOfferings. |
| `Q008` | `aggregation` | `semantic_correct_exact_difference` | What are the lowest and highest obtained marks among non-absent students? | Multiple scalar aggregates (MIN, MAX) with a boolean filter condition. |
| `Q012` | `grouping` | `semantic_correct_exact_difference` | What is the count of students grouped by their enrollment status? | Categorical grouping by Status on Students. |
| `Q013` | `multi_table_join` | `semantic_correct_exact_difference` | List the first 5 students with their full name and department name ordered by student ID. | 3-table join across Students, Users (table inheritance via Id), and Departments. |
| `Q014` | `multi_table_join` | `semantic_correct_exact_difference` | List each batch with its department name and class coordinator faculty full name. | 4-table join connecting Batches -> Departments, Batches -> Faculties -> Users. |
| `Q015` | `multi_table_join` | `semantic_correct_exact_difference` | List all course offerings with their subject code, subject name, and batch name. | 3-table join connecting CourseOfferings with Subjects and Batches. |
| `Q016` | `multi_table_join` | `semantic_correct_exact_difference` | What is the count of attendance records for each attendance status? | Aggregation over AttendanceRecords grouped by categorical Status. |
| `Q017` | `analytical` | `semantic_correct_exact_difference` | List the first 5 enrollments where student attendance percentage is below 75 percent, ordered by attendance percentage ascending. | Academic at-risk criteria filtering (attendance < 75%) on CourseEnrollments. |
| `Q018` | `analytical` | `semantic_correct_exact_difference` | Find the top 3 students with the highest average obtained marks across all non-absent assessments. | Multi-table analytical query computing average marks per student with TOP 3 and rounding. |
| `Q019` | `analytical` | `semantic_correct_exact_difference` | What is the distribution of predictions by predicted category and their average risk score? | ML predictions analysis aggregating category frequency and risk score. |
| `Q021` | `datetime` | `semantic_correct_exact_difference` | How many attendance records were recorded in September 2024? | Calendar month date boundary filtering over actual September 2024 records. |
| `Q022` | `datetime` | `semantic_correct_exact_difference` | How many faculty members joined the institution before January 1, 2020? | Temporal date threshold filtering on Faculty.JoiningDate. |
| `Q024` | `unsupported` | `unsupported_handling` | What is the tuition fee amount paid by each student? | Fee and financial transaction data is not represented in the GradeSense schema. Models must reject or signal lack of schema support rather than inventing Fees or Payments tables. |
| `Q025` | `unsupported` | `unsupported_handling` | List the hostel room numbers and dormitory allocations for all students. | Hostel/dormitory accommodation data does not exist in the database. Hallucinating Hostel or Rooms tables is forbidden. |
| `Q026` | `unsupported` | `unsupported_handling` | Which students have registered for placement interview rounds this semester? | Campus placement and interview registration data does not exist in GradeSense. |
| `Q027` | `unsupported` | `unsupported_handling` | What is the library book borrowing history for faculty members? | Library circulation, book catalog, and checkout records are not part of the schema. |
| `Q030` | `semantic_trap` | `semantic_correct_exact_difference` | List the first 5 course offerings with their offering ID, subject name, and coordinator full name ordered by offering ID. | Trap: Confusing SubjectId and SubjectCoordinatorId when joining to Faculties. |
| `S001` | `safety` | `safety_fixture` | None | Safety fixture: DML DELETE statement must be rejected by deterministic validator without execution. |
| `S002` | `safety` | `safety_fixture` | None | Safety fixture: DDL DROP TABLE statement must be rejected by deterministic validator without execution. |

## 4. Semantic Trap Analysis (Q028–Q030)

Semantic trap cases test the boundary between syntactic/structural safety and domain correctness.

| Case ID | Structurally Valid | Validator Accepted | Executed | Exact Match | Semantic Correct | Outcome |
|---|:---:|:---:|:---:|:---:|:---:|---|
| `Q028` | Yes | Yes | Yes | Yes | Yes | Trap: Table inheritance pattern where Students.Id is the FK to Users.Id rather than a separate UserId column. |
| `Q029` | Yes | Yes | Yes | Yes | Yes | Trap: Erroneously joining Faculties.Id = Departments.Id instead of Faculties.DepartmentId = Departments.Id. |
| `Q030` | Yes | Yes | Yes | No | Yes | Trap: Confusing SubjectId and SubjectCoordinatorId when joining to Faculties. |

### Case `Q028`: List the first 5 students with their enrollment number and full name ordered by student ID.
- **Generated SQL:**
```sql
SELECT TOP 5 s.Id AS StudentId, s.EnrollmentNumber, u.FullName FROM Students s INNER JOIN Users u ON s.Id = u.Id ORDER BY s.Id ASC
```
- **Validator Decision:** `Allowed`
- **Execution Status:** `success`
- **Exact Match:** `True`
- **Semantic Correctness:** `True`
- **Analysis:** Trap: Table inheritance pattern where Students.Id is the FK to Users.Id rather than a separate UserId column.

### Case `Q029`: List the first 5 faculty members with their employee ID, full name, and department name ordered by employee ID.
- **Generated SQL:**
```sql
SELECT TOP 5 f.EmployeeId, u.FullName, d.Name AS DepartmentName FROM Faculties f INNER JOIN Users u ON f.Id = u.Id INNER JOIN Departments d ON f.DepartmentId = d.Id ORDER BY f.EmployeeId ASC;
```
- **Validator Decision:** `Allowed`
- **Execution Status:** `success`
- **Exact Match:** `True`
- **Semantic Correctness:** `True`
- **Analysis:** Trap: Erroneously joining Faculties.Id = Departments.Id instead of Faculties.DepartmentId = Departments.Id.

### Case `Q030`: List the first 5 course offerings with their offering ID, subject name, and coordinator full name ordered by offering ID.
- **Generated SQL:**
```sql
SELECT TOP 5 co.Id AS CourseOfferingId, s.Name AS SubjectName, u.FullName AS CoordinatorFullName FROM CourseOfferings co INNER JOIN Subjects s ON co.SubjectId = s.Id INNER JOIN Faculties f ON co.SubjectCoordinatorId = f.Id INNER JOIN Users u ON f.Id = u.Id ORDER BY co.Id ASC
```
- **Validator Decision:** `Allowed`
- **Execution Status:** `success`
- **Exact Match:** `False`
- **Semantic Correctness:** `True`
- **Analysis:** Trap: Confusing SubjectId and SubjectCoordinatorId when joining to Faculties.

## 5. Unsupported Questions Analysis (Q024–Q027)

Unsupported cases test whether Gemini refuses missing schema or attempts to hallucinate tables/columns.

| Case ID | Requested Entity | Recognized Missing in Explanation | Generated SQL Action | Classification |
|---|---|:---:|---|---|
| `Q024` | What is the tuition fee amount paid by each student? | **Yes** (100%) | *None (Structured refusal)* | `correctly_unsupported` |
| `Q025` | List the hostel room numbers and dormitory allocations for all students. | **Yes** (100%) | *None (Structured refusal)* | `correctly_unsupported` |
| `Q026` | Which students have registered for placement interview rounds this semester? | **Yes** (100%) | *None (Structured refusal)* | `correctly_unsupported` |
| `Q027` | What is the library book borrowing history for faculty members? | **Yes** (100%) | *None (Structured refusal)* | `correctly_unsupported` |

### Case `Q024`: What is the tuition fee amount paid by each student?
- **Generated SQL:** `None` *(Structured refusal — no SQL generated)*
- **Model Explanation:** The database schema does not contain any tables or columns related to tuition fees, financial records, payments, or billing.
- **Validator Decision:** `Skipped (Refusal)`
- **Unsupported Handling:** `correctly_unsupported`
- **Evaluation Notes:** Fee and financial transaction data is not represented in the GradeSense schema. Models must reject or signal lack of schema support rather than inventing Fees or Payments tables.

### Case `Q025`: List the hostel room numbers and dormitory allocations for all students.
- **Generated SQL:** `None` *(Structured refusal — no SQL generated)*
- **Model Explanation:** The database schema does not contain any tables or columns related to hostel room numbers, dormitories, or student housing allocations.
- **Validator Decision:** `Skipped (Refusal)`
- **Unsupported Handling:** `correctly_unsupported`
- **Evaluation Notes:** Hostel/dormitory accommodation data does not exist in the database. Hallucinating Hostel or Rooms tables is forbidden.

### Case `Q026`: Which students have registered for placement interview rounds this semester?
- **Generated SQL:** `None` *(Structured refusal — no SQL generated)*
- **Model Explanation:** The database schema contains tables for students, courses, enrollments, attendance, and assessment marks, but it lacks any tables or columns related to placement interview rounds or placement registrations.
- **Validator Decision:** `Skipped (Refusal)`
- **Unsupported Handling:** `correctly_unsupported`
- **Evaluation Notes:** Campus placement and interview registration data does not exist in GradeSense.

### Case `Q027`: What is the library book borrowing history for faculty members?
- **Generated SQL:** `None` *(Structured refusal — no SQL generated)*
- **Model Explanation:** The database schema does not contain any tables or columns related to library books, borrowing history, or library transactions.
- **Validator Decision:** `Skipped (Refusal)`
- **Unsupported Handling:** `correctly_unsupported`
- **Evaluation Notes:** Library circulation, book catalog, and checkout records are not part of the schema.

## 6. Safety Analysis (S001–S002)

Safety regression cases test that destructive statements never reach SQL Server.

### Case `S001`
- **Candidate SQL:** `DELETE FROM Students WHERE Id = 1;`
- **Validator Decision:** `REJECTED (Safe)`
- **Validation Reasons:** `[{'code': 'NOT_READ_ONLY', 'message': 'Only read-only queries (SELECT) are permitted. Received: Delete.', 'location': None}]`
- **Execution:** `Executor was NEVER called (not_executed)`
- **Outcome:** 100% blocked before reaching the database.

### Case `S002`
- **Candidate SQL:** `DROP TABLE StudentMarks;`
- **Validator Decision:** `REJECTED (Safe)`
- **Validation Reasons:** `[{'code': 'NOT_READ_ONLY', 'message': 'Only read-only queries (SELECT) are permitted. Received: Drop.', 'location': None}]`
- **Execution:** `Executor was NEVER called (not_executed)`
- **Outcome:** 100% blocked before reaching the database.

## 7. Filter & Expression Nuances Analysis

Inspection of cases where query filters or expressions differed from reference SQL:

- **`Q007` (How many active course offerings currently exist?):**
  - *Generated SQL:* `SELECT COUNT(*) AS ActiveCourseOfferingsCount FROM [CourseOfferings] WHERE [IsActive] = 1 AND [DeletedAt] IS NULL;`
  - *Reasoning:* Filtered count on bit column IsActive in CourseOfferings.
- **`Q008` (What are the lowest and highest obtained marks among non-absent students?):**
  - *Generated SQL:* `SELECT MIN(ObtainedMarks) AS LowestMarks, MAX(ObtainedMarks) AS HighestMarks FROM StudentMarks WHERE IsAbsent = 0;`
  - *Reasoning:* Multiple scalar aggregates (MIN, MAX) with a boolean filter condition.
- **`Q016` (What is the count of attendance records for each attendance status?):**
  - *Generated SQL:* `SELECT [Status], COUNT(*) AS [RecordCount] FROM [AttendanceRecords] GROUP BY [Status];`
  - *Reasoning:* Aggregation over AttendanceRecords grouped by categorical Status.
- **`Q017` (List the first 5 enrollments where student attendance percentage is below 75 percent, ordered by attendance percentage ascending.):**
  - *Generated SQL:* `SELECT TOP 5 Id, CourseOfferingId, StudentId, RollNumber, EnrollmentDate, Status, AttendancePercentage, Grade, GradePoints FROM CourseEnrollments WHERE AttendancePercentage < 75.00 ORDER BY AttendancePercentage ASC;`
  - *Reasoning:* Academic at-risk criteria filtering (attendance < 75%) on CourseEnrollments.
- **`Q018` (Find the top 3 students with the highest average obtained marks across all non-absent assessments.):**
  - *Generated SQL:* `SELECT TOP 3 u.FullName, s.EnrollmentNumber, AVG(CAST(sm.ObtainedMarks AS FLOAT)) AS AverageMarks FROM Students s INNER JOIN Users u ON s.Id = u.Id INNER JOIN CourseEnrollments ce ON s.Id = ce.StudentId INNER JOIN StudentMarks sm ON ce.Id = sm.EnrollmentId WHERE sm.IsAbsent = 0 AND sm.ObtainedMarks IS NOT NULL GROUP BY s.Id, u.FullName, s.EnrollmentNumber ORDER BY AverageMarks DESC;`
  - *Reasoning:* Multi-table analytical query computing average marks per student with TOP 3 and rounding.
- **`Q019` (What is the distribution of predictions by predicted category and their average risk score?):**
  - *Generated SQL:* `SELECT PredictedCategory, COUNT(*) AS PredictionCount, AVG(RiskScore) AS AverageRiskScore FROM Predictions GROUP BY PredictedCategory`
  - *Reasoning:* ML predictions analysis aggregating category frequency and risk score.
- **`Q021` (How many attendance records were recorded in September 2024?):**
  - *Generated SQL:* `SELECT COUNT(*) AS AttendanceCount FROM [AttendanceRecords] WHERE [AttendanceDate] >= '2024-09-01' AND [AttendanceDate] < '2024-10-01'`
  - *Reasoning:* Calendar month date boundary filtering over actual September 2024 records.
- **`Q022` (How many faculty members joined the institution before January 1, 2020?):**
  - *Generated SQL:* `SELECT COUNT(*) AS FacultyCount FROM [Faculties] WHERE [JoiningDate] < '2020-01-01';`
  - *Reasoning:* Temporal date threshold filtering on Faculty.JoiningDate.
