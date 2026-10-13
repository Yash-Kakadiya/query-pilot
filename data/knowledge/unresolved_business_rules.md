# Unresolved Business Rules & Domain Questions

> **Status:** Pending Human Confirmation<br>
> **Notice:** The items in this document are **NOT** active business definitions in the knowledge catalog. They represent open business-logic ambiguities identified during Task 1.31-A that must receive authoritative human/stakeholder confirmation before being added to the knowledge catalog or used in RAG.

---

## 1. Dynamic vs. Stored CGPA
- **Question:** Should cumulative grade point average queries retrieve the stored snapshot in `Students.CGPA`, or calculate it dynamically from `CourseEnrollments.GradePoints` weighted by `Subjects.Credit`?
- **Current Observation:** Baseline questions (e.g. `Q003`) query `Students.CGPA` directly.
- **Confirmation Needed:** Is `Students.CGPA` the official institutional record, or is dynamic GPA recalculation required for semester-specific views?

---

## 2. Official "At-Risk" Student Definition
- **Question:** What is the authoritative institutional formula for identifying an "at-risk" student?
- **Current Observation:** Evaluation question `Q017` checks `CourseEnrollments.AttendancePercentage < 75.0`. Separately, the `Predictions` table provides an ML-based `RiskScore` (0.0 to 1.0) and `PredictedCategory = 'At-Risk'`.
- **Confirmation Needed:** Does "at-risk" refer strictly to attendance ($< 75\%$), ML risk predictions, low marks ($< \text{PassingMarks}$), or a composite criterion? When the user asks "Who is at risk?", should the system ask for clarification between attendance risk and ML risk?

---

## 3. Attendance Calculation Methodology
- **Question:** Is `CourseEnrollments.AttendancePercentage` an authoritative pre-aggregated value, or should queries aggregate granular records from `AttendanceRecords`?
- **Current Observation:** `AttendanceRecords` contains session-level status (`'Present'`, `'Absent'`, `'Excused'`, `'Late'`). `CourseEnrollments` contains `AttendancePercentage`.
- **Confirmation Needed:** How are `'Excused'` and `'Late'` statuses weighted in attendance calculations if computed from session records?

---

## 4. Default Enrollment Status Filtering
- **Question:** When queries request student enrollments, course counts, or rosters, should queries automatically filter to `CourseEnrollments.Status = 'Active'`?
- **Current Observation:** `CourseEnrollments.Status` has values `'Active'`, `'Completed'`, `'Dropped'`, `'Withdrawn'`.
- **Confirmation Needed:** What is the default filtering policy when an analytical prompt does not specify whether past or dropped enrollments should be included?

---

## 5. Passing Criteria and Grade Thresholds
- **Question:** What constitutes a passing grade or passing marks for an assessment vs. an overall course offering?
- **Current Observation:** `EvaluationSchemes` defines `PassingMarks` and `TotalMarks`. `StudentMarks` records `ObtainedMarks`.
- **Confirmation Needed:** Is passing evaluated per individual `AssessmentItem`, or exclusively at the aggregated `EvaluationScheme` level?
