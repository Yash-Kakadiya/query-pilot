# QueryPilot Task 1.12-R — Baseline Evaluation Report (Corrected Classification)

**Evaluation Run:** Baseline Single-Pass Run (Unmodified Gemini Outputs)  
**Target Model:** `gemini-3.5-flash-lite`  
**Target Database:** `GradeSense_Local` (17 tables, 7,077 rows)  
**Generation Parameters:** `temperature=0.0`, structured JSON output, zero retries, no LangGraph

## 1. Executive Summary

- **Total Cases Evaluated:** 32
- **Natural-Language Generation Success:** 30/30 (100.0%)
- **Validator Acceptance Rate (NL queries):** 30/30 (100.0%)
- **Execution Success Rate (Validated queries):** 26/26 (100.0%)
- **Exact Result Match Rate (Answerable cases):** 11/26 (42.3%)
- **Semantic Correctness Rate (Answerable cases):** 26/26 (100.0%)
- **Semantic Trap Correctness (Q028–Q030):** 3/3 (100.0%) (Exact match: 1/3)
- **Unsupported Questions Recognition (Q024–Q027):** 0/4 (0.0%) structured refusals (4/4 recognized in natural-language explanations)
- **Safety Rejection Rate (S001–S002):** 2/2 (100.0%) (100% blocked, 0 reached database)

## 2. Category Breakdown

| Category | Cases | Generation | Validation | Execution | Exact Match | Semantic Correct |
|---|:---:|:---:|:---:|:---:|:---:|:---:|
| `aggregation` | 4 | 4/4 | 4/4 | 4/4 | 2/4 | 4/4 |
| `analytical` | 4 | 4/4 | 4/4 | 4/4 | 1/4 | 4/4 |
| `datetime` | 3 | 3/3 | 3/3 | 3/3 | 1/3 | 3/3 |
| `grouping` | 4 | 4/4 | 4/4 | 4/4 | 3/4 | 4/4 |
| `multi_table_join` | 4 | 4/4 | 4/4 | 4/4 | 0/4 | 4/4 |
| `safety` | 2 | N/A | 2/2 | N/A | N/A | N/A |
| `semantic_trap` | 3 | 3/3 | 3/3 | 3/3 | 1/3 | 3/3 |
| `simple_retrieval` | 4 | 4/4 | 4/4 | 4/4 | 3/4 | 4/4 |
| `unsupported` | 4 | 4/4 | 4/4 | N/A | N/A | N/A |
| **Overall / Total** | **32** | **30/30** | **32/32** | **26/26** | **11/26** | **26/26** |

## 3. Case-by-Case Reclassification Analysis

Every exact mismatch from the initial baseline was inspected and classified as one of:
- `exact_match`: row-for-row, column-for-column identical to reference result;
- `semantic_correct_exact_difference`: generated query returns the correct intended business answer, differing only in harmless projection columns, aliases, floating-point precision, or equivalent date intervals;
- `semantic_incorrect`: query executed but returned incorrect business data;
- `unsupported_handling`: question references schema entities outside GradeSense_Local;

Total Non-Exact-Match Cases Analyzed: **21**

| Case ID | Category | Classification | Question | Reclassification Reasoning |
|---|---|---|---|---|
| `Q003` | `simple_retrieval` | `semantic_correct_exact_difference` | Retrieve all students who have a CGPA of 9.0 or higher ordered by CGPA descending. | Harmless extra projection columns (FullName, Email, AdmissionYear) and descending sort preserved. |
| `Q007` | `aggregation` | `semantic_correct_exact_difference` | How many active course offerings currently exist? | Column alias ActiveCourseOfferingsCount vs ActiveOfferingsCount; identical count (10). |
| `Q008` | `aggregation` | `semantic_correct_exact_difference` | What are the lowest and highest obtained marks among non-absent students? | Column aliases LowestMarks/HighestMarks vs MinMarks/MaxMarks; identical values (0, 9.64). |
| `Q012` | `grouping` | `semantic_correct_exact_difference` | What is the count of students grouped by their enrollment status? | Column alias StudentCount vs StatusCount; identical single group (Active: 40). |
| `Q013` | `multi_table_join` | `semantic_correct_exact_difference` | List the first 5 students with their full name and department name ordered by student ID. | Omitted s.Id from projection; full names and department names of top 5 students match exactly. |
| `Q014` | `multi_table_join` | `semantic_correct_exact_difference` | List each batch with its department name and class coordinator faculty full name. | Additional batch projection columns (Semester, Division, etc.) and alias ClassCoordinatorName vs CoordinatorName. |
| `Q015` | `multi_table_join` | `semantic_correct_exact_difference` | List all course offerings with their subject code, subject name, and batch name. | Column alias CourseOfferingId vs OfferingId; identical 10 offerings. |
| `Q016` | `multi_table_join` | `semantic_correct_exact_difference` | What is the count of attendance records for each attendance status? | Omitted ORDER BY clause; exact same 4 status counts returned. |
| `Q017` | `analytical` | `semantic_correct_exact_difference` | List the first 5 enrollments where student attendance percentage is below 75 percent, ordered by attendance percentage ascending. | Additional projection columns from CourseEnrollments; exact same 5 lowest attendance enrollments. |
| `Q018` | `analytical` | `semantic_correct_exact_difference` | Find the top 3 students with the highest average obtained marks across all non-absent assessments. | Floating-point precision instead of 2-decimal CAST and included student FullName; exact same top 3 students. |
| `Q019` | `analytical` | `semantic_correct_exact_difference` | What is the distribution of predictions by predicted category and their average risk score? | Float average risk score instead of rounded decimal and alias AverageRiskScore vs AvgRiskScore; exact same category count and score. |
| `Q021` | `datetime` | `semantic_correct_exact_difference` | How many attendance records were recorded in September 2024? | Date filter < '2024-10-01' vs <= '2024-09-30' and alias AttendanceCount vs SeptAttendanceCount; identical count (1400). |
| `Q022` | `datetime` | `semantic_correct_exact_difference` | How many faculty members joined the institution before January 1, 2020? | Column alias FacultyCount vs FacultyJoinedBefore2020; identical count (7). |
| `Q024` | `unsupported` | `unsupported_handling` | What is the tuition fee amount paid by each student? | Model recognized tuition fees are missing in explanation, but attempted fallback student list SQL instead of structured refusal. |
| `Q025` | `unsupported` | `unsupported_handling` | List the hostel room numbers and dormitory allocations for all students. | Model recognized hostel rooms are missing in explanation, but generated limitation string SQL query instead of structured refusal. |
| `Q026` | `unsupported` | `unsupported_handling` | Which students have registered for placement interview rounds this semester? | Model recognized placement rounds are missing in explanation, but generated WHERE 1=0 query instead of structured refusal. |
| `Q027` | `unsupported` | `unsupported_handling` | What is the library book borrowing history for faculty members? | Model recognized library history is missing in explanation, but generated SELECT NULL WHERE 1=0 query instead of structured refusal. |
| `Q028` | `semantic_trap` | `semantic_correct_exact_difference` | List the first 5 students with their enrollment number and full name ordered by student ID. | Correctly navigated Students.Id = Users.Id join (avoided trap); omitted s.Id in SELECT list. |
| `Q030` | `semantic_trap` | `semantic_correct_exact_difference` | List the first 5 course offerings with their offering ID, subject name, and coordinator full name ordered by offering ID. | Correctly navigated CourseOfferings -> Faculties -> Users join (avoided trap); aliases CourseOfferingId vs OfferingId, CoordinatorFullName vs CoordinatorName. |
| `S001` | `safety` | `safety_fixture` | None | DELETE statement rejected by AST validator. |
| `S002` | `safety` | `safety_fixture` | None | DROP TABLE statement rejected by AST validator. |

## 4. Semantic Trap Analysis (Q028–Q030)

Semantic trap cases test the boundary between syntactic/structural safety and domain correctness.

| Case ID | Structurally Valid | Validator Accepted | Executed | Exact Match | Semantic Correct | Outcome |
|---|:---:|:---:|:---:|:---:|:---:|---|
| `Q028` | Yes | Yes | Yes | No | Yes | Correctly navigated Students.Id = Users.Id join (avoided trap); omitted s.Id in SELECT list. |
| `Q029` | Yes | Yes | Yes | Yes | Yes | Exact match (avoided trap) |
| `Q030` | Yes | Yes | Yes | No | Yes | Correctly navigated CourseOfferings -> Faculties -> Users join (avoided trap); aliases CourseOfferingId vs OfferingId, CoordinatorFullName vs CoordinatorName. |

### Case `Q028`: List the first 5 students with their enrollment number and full name ordered by student ID.
- **Generated SQL:**
```sql
SELECT TOP 5 s.EnrollmentNumber, u.FullName FROM Students s INNER JOIN Users u ON s.Id = u.Id ORDER BY s.Id ASC
```
- **Validator Decision:** `Allowed`
- **Execution Status:** `success`
- **Exact Match:** `False`
- **Semantic Correctness:** `True`
- **Analysis:** Correctly navigated Students.Id = Users.Id join (avoided trap); omitted s.Id in SELECT list.

### Case `Q029`: List the first 5 faculty members with their employee ID, full name, and department name ordered by employee ID.
- **Generated SQL:**
```sql
SELECT TOP 5 f.EmployeeId, u.FullName, d.Name AS DepartmentName FROM Faculties f INNER JOIN Users u ON f.Id = u.Id INNER JOIN Departments d ON f.DepartmentId = d.Id ORDER BY f.EmployeeId;
```
- **Validator Decision:** `Allowed`
- **Execution Status:** `success`
- **Exact Match:** `True`
- **Semantic Correctness:** `True`
- **Analysis:** Exact match (avoided trap)

### Case `Q030`: List the first 5 course offerings with their offering ID, subject name, and coordinator full name ordered by offering ID.
- **Generated SQL:**
```sql
SELECT TOP 5 co.Id AS CourseOfferingId, s.Name AS SubjectName, u.FullName AS CoordinatorFullName FROM CourseOfferings co INNER JOIN Subjects s ON co.SubjectId = s.Id INNER JOIN Faculties f ON co.SubjectCoordinatorId = f.Id INNER JOIN Users u ON f.Id = u.Id ORDER BY co.Id ASC
```
- **Validator Decision:** `Allowed`
- **Execution Status:** `success`
- **Exact Match:** `False`
- **Semantic Correctness:** `True`
- **Analysis:** Correctly navigated CourseOfferings -> Faculties -> Users join (avoided trap); aliases CourseOfferingId vs OfferingId, CoordinatorFullName vs CoordinatorName.

## 5. Unsupported Questions Analysis (Q024–Q027)

Unsupported cases test whether Gemini refuses missing schema or attempts to hallucinate tables/columns.

| Case ID | Requested Entity | Recognized Missing in Explanation | Generated SQL Action | Classification |
|---|---|:---:|---|---|
| `Q024` | What is the tuition fee amount paid by each student? | **Yes** (100%) | `SELECT S.Id AS StudentId, U.FullName AS StudentNam...` | `incorrectly_attempted` |
| `Q025` | List the hostel room numbers and dormitory allocations for all students. | **Yes** (100%) | `SELECT 'This question cannot be answered using the...` | `incorrectly_attempted` |
| `Q026` | Which students have registered for placement interview rounds this semester? | **Yes** (100%) | `SELECT S.Id, U.FullName, S.EnrollmentNumber FROM S...` | `incorrectly_attempted` |
| `Q027` | What is the library book borrowing history for faculty members? | **Yes** (100%) | `SELECT NULL AS Message WHERE 1 = 0;...` | `incorrectly_attempted` |

### Case `Q024`: What is the tuition fee amount paid by each student?
- **Generated SQL:**
```sql
SELECT S.Id AS StudentId, U.FullName AS StudentName, S.EnrollmentNumber FROM Students S INNER JOIN Users U ON S.Id = U.Id
```
- **Model Explanation:** The database schema contains student demographic, academic, and enrollment information (such as CGPA, Department, Status), but it does not contain any financial tables, columns, or records relating to tuition fees or payments. Therefore, a list of students is returned instead, as the specific fee amount requested is unanswerable with the given schema.
- **Validator Decision:** `Allowed`
- **Unsupported Handling:** `incorrectly_attempted`
- **Evaluation Notes:** Model recognized tuition fees are missing in explanation, but attempted fallback student list SQL instead of structured refusal.

### Case `Q025`: List the hostel room numbers and dormitory allocations for all students.
- **Generated SQL:**
```sql
SELECT 'This question cannot be answered using the provided schema because hostel room numbers and dormitory allocations are not stored in the database.' AS [Limitation]
```
- **Model Explanation:** The provided database schema contains tables related to students, courses, attendance, marks, and departments, but does not include any information regarding hostel rooms or dormitory allocations.
- **Validator Decision:** `Allowed`
- **Unsupported Handling:** `incorrectly_attempted`
- **Evaluation Notes:** Model recognized hostel rooms are missing in explanation, but generated limitation string SQL query instead of structured refusal.

### Case `Q026`: Which students have registered for placement interview rounds this semester?
- **Generated SQL:**
```sql
SELECT S.Id, U.FullName, S.EnrollmentNumber FROM Students S INNER JOIN Users U ON S.Id = U.Id WHERE 1 = 0
```
- **Model Explanation:** The database schema does not contain any tables or columns related to placement interview rounds, career services, or job interviews. Therefore, an empty result set is returned accompanied by an explanatory assumption.
- **Validator Decision:** `Allowed`
- **Unsupported Handling:** `incorrectly_attempted`
- **Evaluation Notes:** Model recognized placement rounds are missing in explanation, but generated WHERE 1=0 query instead of structured refusal.

### Case `Q027`: What is the library book borrowing history for faculty members?
- **Generated SQL:**
```sql
SELECT NULL AS Message WHERE 1 = 0;
```
- **Model Explanation:** The database schema does not contain any tables or columns related to library books or borrowing history. Therefore, this question cannot be answered using the provided schema.
- **Validator Decision:** `Allowed`
- **Unsupported Handling:** `incorrectly_attempted`
- **Evaluation Notes:** Model recognized library history is missing in explanation, but generated SELECT NULL WHERE 1=0 query instead of structured refusal.

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
  - *Generated SQL:* `SELECT COUNT(*) AS ActiveCourseOfferingsCount FROM [CourseOfferings] WHERE [IsActive] = 1 AND [DeletedAt] IS NULL`
  - *Reasoning:* Column alias ActiveCourseOfferingsCount vs ActiveOfferingsCount; identical count (10).
- **`Q008` (What are the lowest and highest obtained marks among non-absent students?):**
  - *Generated SQL:* `SELECT MIN(ObtainedMarks) AS LowestMarks, MAX(ObtainedMarks) AS HighestMarks FROM StudentMarks WHERE IsAbsent = 0 AND ObtainedMarks IS NOT NULL`
  - *Reasoning:* Column aliases LowestMarks/HighestMarks vs MinMarks/MaxMarks; identical values (0, 9.64).
- **`Q016` (What is the count of attendance records for each attendance status?):**
  - *Generated SQL:* `SELECT Status, COUNT(*) AS RecordCount FROM AttendanceRecords GROUP BY Status`
  - *Reasoning:* Omitted ORDER BY clause; exact same 4 status counts returned.
- **`Q017` (List the first 5 enrollments where student attendance percentage is below 75 percent, ordered by attendance percentage ascending.):**
  - *Generated SQL:* `SELECT TOP 5 Id, CourseOfferingId, StudentId, RollNumber, EnrollmentDate, Status, AttendancePercentage, Grade, GradePoints FROM CourseEnrollments WHERE AttendancePercentage < 75.00 ORDER BY AttendancePercentage ASC;`
  - *Reasoning:* Additional projection columns from CourseEnrollments; exact same 5 lowest attendance enrollments.
- **`Q018` (Find the top 3 students with the highest average obtained marks across all non-absent assessments.):**
  - *Generated SQL:* `SELECT TOP 3 s.Id AS StudentId, u.FullName, AVG(CAST(sm.ObtainedMarks AS FLOAT)) AS AverageObtainedMarks FROM Students s JOIN Users u ON s.Id = u.Id JOIN CourseEnrollments ce ON s.Id = ce.StudentId JOIN StudentMarks sm ON ce.Id = sm.EnrollmentId WHERE sm.IsAbsent = 0 AND sm.ObtainedMarks IS NOT NULL GROUP BY s.Id, u.FullName ORDER BY AverageObtainedMarks DESC`
  - *Reasoning:* Floating-point precision instead of 2-decimal CAST and included student FullName; exact same top 3 students.
- **`Q019` (What is the distribution of predictions by predicted category and their average risk score?):**
  - *Generated SQL:* `SELECT PredictedCategory, COUNT(*) AS PredictionCount, AVG(RiskScore) AS AverageRiskScore FROM Predictions WHERE DeletedAt IS NULL GROUP BY PredictedCategory`
  - *Reasoning:* Float average risk score instead of rounded decimal and alias AverageRiskScore vs AvgRiskScore; exact same category count and score.
- **`Q021` (How many attendance records were recorded in September 2024?):**
  - *Generated SQL:* `SELECT COUNT(*) AS AttendanceCount FROM AttendanceRecords WHERE AttendanceDate >= '2024-09-01' AND AttendanceDate < '2024-10-01';`
  - *Reasoning:* Date filter < '2024-10-01' vs <= '2024-09-30' and alias AttendanceCount vs SeptAttendanceCount; identical count (1400).
- **`Q022` (How many faculty members joined the institution before January 1, 2020?):**
  - *Generated SQL:* `SELECT COUNT(*) AS FacultyCount FROM Faculties WHERE JoiningDate < '2020-01-01'`
  - *Reasoning:* Column alias FacultyCount vs FacultyJoinedBefore2020; identical count (7).
