# QueryPilot Baseline Evaluation Dataset

## Overview

This directory contains the first reproducible baseline evaluation dataset for QueryPilot, designed to assess Text-to-SQL generation, deterministic validation, and execution accuracy against the local `GradeSense_Local` database.

The dataset serves as authoritative ground truth for evaluating pipeline capabilities before introducing autonomous recovery, LangGraph workflows, or answer synthesis.

## Dataset Structure

The dataset is located in [`baseline_questions.json`](baseline_questions.json) and comprises **32 carefully curated evaluation cases** formatted as follows:

```json
{
  "id": "Q001",
  "category": "simple_retrieval",
  "question": "List all academic departments with their code and name ordered by department code.",
  "expected_behavior": "answerable",
  "reference_sql": "SELECT Code, Name FROM Departments ORDER BY Code ASC;",
  "reference_result": [
    {"Code": "CE", "Name": "Computer Engineering"},
    {"Code": "ME", "Name": "Mechanical Engineering"}
  ],
  "notes": "Basic projection and ordering on the Departments table."
}
```

### Case Schema Fields

| Field | Type | Description |
|---|---|---|
| `id` | string | Unique case identifier (e.g., `Q001`, `S001`). |
| `category` | string | Evaluation category name. |
| `question` | string / null | Natural-language query prompt (`null` for safety fixtures). |
| `expected_behavior` | string | Expected outcome: `answerable`, `unsupported`, `semantic_trap`, or `rejected`. |
| `reference_sql` | string / null | Verified, read-only SQL for `answerable` cases (`null` for unsupported/safety). |
| `reference_result` | array / null | Exact JSON-serializable rows returned from `GradeSense_Local`. |
| `candidate_sql` | string / null | Dangerous query string tested exclusively for safety rejection. |
| `expected_reason` | string / null | Rejection reason code (e.g. `NOT_READ_ONLY`) for safety fixtures. |
| `semantic_trap_details` | object / null | Explains why plausible incorrect joins pass safety checks but fail business semantics. |
| `notes` | string | Context, tables involved, and expected behavior notes. |

---

## Category Distribution (32 Cases)

| Category | Count | IDs | Description |
|---|:---:|---|---|
| **Simple Retrieval** | 4 | `Q001`–`Q004` | Single-table projection, filtering, and ordering. |
| **Aggregation** | 4 | `Q005`–`Q008` | Scalar aggregations (`COUNT`, `AVG`, `MIN`, `MAX`) with rounding. |
| **Grouping / Comparison** | 4 | `Q009`–`Q012` | Categorical aggregations with `GROUP BY` and `ORDER BY`. |
| **Multi-Table Joins** | 4 | `Q013`–`Q016` | Relational queries spanning 2 to 4 normalized tables. |
| **Analytical** | 4 | `Q017`–`Q020` | Academic domain analytics (at-risk attendance, ML predictions, marks). |
| **Date / Time** | 3 | `Q021`–`Q023` | Bounded calendar filtering and temporal cohort grouping. |
| **Unsupported** | 4 | `Q024`–`Q027` | Questions outside database scope (fees, hostels, placements, library). |
| **Semantic Trap** | 3 | `Q028`–`Q030` | Valid syntax/schema queries that produce wrong semantics if misjoined. |
| **Safety Regressions** | 2 | `S001`–`S002` | Destructive DML (`DELETE`) and DDL (`DROP`) validation fixtures. |

---

## Semantic Trap Analysis

A critical principle established in QueryPilot Tasks 1.9 & 1.10 is:

$$\text{Syntax Validity} \neq \text{Safety Validation} \neq \text{Semantic Correctness}$$

Cases `Q028`, `Q029`, and `Q030` test this boundary:
1. **`Q028` (Table Inheritance)**: `Students.Id` inherits from `Users.Id`. If an LLM erroneously joins `Students.DepartmentId = Users.Id`, both columns are valid `INT` types in the model-facing schema and pass Task 1.8 deterministic validation, but semantically pair students with HODs/Deans.
2. **`Q029` (Primary vs Foreign Keys)**: `Faculties` has `Faculties.Id` (User inheritance) and `Faculties.DepartmentId` (FK to `Departments.Id`). Joining `Faculties.Id = Departments.Id` passes safety validation but joins user IDs to department IDs.
3. **`Q030` (Multi-Hop Join Disambiguation)**: `CourseOfferings` contains `SubjectId` and `SubjectCoordinatorId`. Equating `SubjectId = Faculties.Id` passes validation but conflates subjects with faculty members.

---

## Safety Fixtures

Cases `S001` and `S002` are **validation regression fixtures only**:
- They test that destructive DML (`DELETE`) and DDL (`DROP TABLE`) statements are deterministically caught by the Task 1.8 AST policy layer before execution.
- They are **never executed** against any database.

---

## Ground-Truth Verification

All 26 `answerable` reference SQL statements:
1. Were verified against Task 1.8 `validate_sql()` (100% approved, 0 policy violations).
2. Were executed directly on `GradeSense_Local` using `execute_read_only()`.
3. Have their actual, non-fabricated results stored in `reference_result`.
4. Include explicit `ORDER BY` clauses to ensure deterministic row comparisons.
