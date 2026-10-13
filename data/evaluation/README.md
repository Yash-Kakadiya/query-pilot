# QueryPilot Baseline Evaluation Dataset

## Overview

This directory contains the first reproducible baseline evaluation dataset for QueryPilot, designed to assess Text-to-SQL generation, deterministic validation, and execution accuracy against the local `GradeSense_Local` database.

The dataset serves as authoritative ground truth for evaluating pipeline capabilities before introducing autonomous recovery, LangGraph workflows, or answer synthesis.

## Dataset Structure

The dataset is located in [`baseline_questions.json`](baseline_questions.json) and comprises **34 carefully curated evaluation cases** (the original 32 benchmark questions plus two clarification cases, `C001` and `C002`), formatted as follows:

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
| `id` | string | Unique case identifier (e.g., `Q001`, `S001`, `C001`). |
| `category` | string | Evaluation category name. |
| `question` | string / null | Natural-language query prompt (`null` for safety fixtures). |
| `expected_behavior` | string | Expected outcome: `answerable`, `unsupported`, `semantic_trap`, `rejected`, or `clarification_needed`. |
| `reference_sql` | string / null | Verified, read-only SQL for `answerable` cases (`null` for unsupported/safety/clarification). |
| `reference_result` | array / null | Exact JSON-serializable rows returned from `GradeSense_Local`. |
| `candidate_sql` | string / null | Dangerous query string tested exclusively for safety rejection. |
| `expected_reason` | string / null | Rejection reason code (e.g. `NOT_READ_ONLY`) for safety fixtures. |
| `semantic_trap_details` | object / null | Explains why plausible incorrect joins pass safety checks but fail business semantics. |
| `conversation_context` | array / null | Optional list of bounded prior conversation turns (`role`, `user_question`, `resolved_intent`, `result_columns`). |
| `notes` | string | Context, tables involved, and expected behavior notes. |

---

## Category Distribution (34 Cases)

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
| **Clarification** | 2 | `C001`–`C002` | Ambiguous or context-dependent requests evaluating clarification recognition and bounded context forwarding. |

---

## Clarification and Context Evaluation Scope

The baseline dataset evaluates clarification handling as a first-class outcome via `C001` and `C002`:
- **`C001` (Vague Question)**: Ambiguous standalone query without sufficient domain or schema specificity to construct SQL safely, expecting clarification.
- **`C002` (Context-Dependent Follow-Up)**: Follow-up question supplying prior user intent and result column summaries in `conversation_context`.

### Capability Definition and Scope Boundary
- **Clarification-Aware Evaluation with Bounded Context Forwarding**: Evaluates whether the generation subsystem correctly identifies queries requiring clarification instead of generating SQL, and whether callers can supply bounded conversation context ($\le 3$ prior turns, restricted to user questions, intent summaries, and column lists with zero raw SQL, rows, or credentials) to inform generation or clarification decisions.
- **Explicit Scope Limitation**: This evaluation capability tests single-request generation boundaries with forwarded turn context. It does **not** yet provide an end-to-end interactive, stateful multi-turn dialogue evaluation loop (i.e., multi-turn back-and-forth session simulation or dynamic agent dialogue).

---

## Dataset Versioning and Historical Benchmark Compatibility

- **Dataset Evolution**: The baseline question suite originally contained 32 cases (`Q001`–`Q032`). Task 1.30-B expanded the active suite to 34 cases with the addition of `C001` and `C002`.
- **Preservation of Historical Artifacts**: Saved historical evaluation artifacts (e.g., `baseline_results.json`, `baseline_results_1_14.json`, `comparison_1_22_audited.json`, `semantic_audit_1_26.json`) remain unchanged on their original 32-case baseline.
- **Comparability Warning**: Metrics derived from the historical 32-case dataset and the expanded 34-case dataset must not be compared directly without acknowledging the dataset difference, particularly regarding overall accuracy, execution rates, and the introduction of clarification recognition metrics.

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
