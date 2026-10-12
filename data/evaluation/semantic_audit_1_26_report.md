# QueryPilot Sequential vs. LangGraph Comparative Evaluation Report

> [!IMPORTANT]
> This report provides a paired, evidence-based reliability comparison between the
> existing sequential `QueryPipeline` (baseline) and the `QueryPilotWorkflow` (LangGraph).
>
> **Evaluation Provenance (Offline Reclassification):**
> Reclassified from Task 1.22 saved model outputs against reconciled baseline_questions.json (Q012 clarified, Q013 unprojected Id removed, Q018 4-column contract unrounded, Q019 unrounded).

## 1. Executive Summary & Agreement Overview

- **Evaluation Timestamp:** `2026-10-11T13:02:43.272476+00:00`
- **Total Cases Evaluated:** 32
- **Natural Language Cases (End-to-End Pipeline Evaluation):** 30
  - **Pipeline Agreement Rate:** 29/30 (96.7%)
  - **Both Modes Passed:** 28/30 (93.3%)
  - **Regressions (Sequential Passed, Graph Failed):** 0/30 (0.0%)
  - **Improvements (Graph Passed, Sequential Failed):** 1/30 (3.3%)
  - **Both Modes Failed:** 1/30 (3.3%)
  - **Incomparable Cases:** 0
- **Safety Fixture Cases (Deterministic AST Policy Check):** 2
  - **Rejection Rate (Both Modes):** 2/2 (100.0%)
- **Overall Dataset Totals (All Cases Combined):** 32
  - **Total Agreement Rate:** 31/32 (96.9%)

## 2. Side-by-Side Metric Comparison

| Metric | Sequential Mode | Graph Mode | Delta / Status |
| :--- | :--- | :--- | :--- |
| **Answerable Semantic Correctness** | 24/26 (92.3%) | 25/26 (96.2%) | +1 |
| **Exact Result Match Rate (Answerable)** | 13/26 (50.0%) | 12/26 (46.2%) | -1 |
| **Unsupported Refusal Accuracy** | 4/4 (100.0%) | 4/4 (100.0%) | Parity |
| **Semantic Trap Handling (Q028–Q030)** | 3/3 (100.0%) | 3/3 (100.0%) | Parity |
| **Safety Fixture Rejection Rate** | 2/2 (100.0%) | 2/2 (100.0%) | Parity |
| **Unexpected Pipeline Errors** | 0 | 0 | Parity |

> [!NOTE]
> **Safety Fixtures:** Evaluated directly via the deterministic AST policy validator (`validate_sql`)
> without natural-language generation or database execution, isolating validator-level safety policy from generation.

## 3. Paired Regression & Difference Analysis

> [!NOTE]
> **Zero Regressions Detected:** The LangGraph workflow matches the sequential baseline on all passing cases.

### Graph Improvements (Graph Passed, Sequential Failed)

- **`Q012`** (grouping): What is the count of students grouped by their enrollment status?

### Mutual Failures (Both Modes Failed)

- **`Q018`** (analytical): Expected `answerable`, difference: Both modes executed successfully, but produced semantically incorrect answers.
  - *Note on Historical Provenance:* The model output was generated in Task 1.22 against the earlier question wording, which did not request `StudentId`. Under the revised contract, Q018 explicitly requests `StudentId`, `FullName`, `EnrollmentNumber`, and `AvgMarks`. Because the historical response did not include `StudentId`, this mismatch reflects a contract revision across benchmark versions rather than a measured failure of the model on the revised prompt. A fresh model evaluation would be required to measure performance on the revised Q018 question.

## 4. Per-Case Comparative Inventory

| Case ID | Category | Expected Behavior | Seq Passed | Graph Passed | Exact Match Agree | Outcome Classification |
| :--- | :--- | :--- | :---: | :---: | :---: | :--- |
| `Q001` | simple_retrieval | answerable | ✅ | ✅ | ✅ | `both_pass` |
| `Q002` | simple_retrieval | answerable | ✅ | ✅ | ✅ | `both_pass` |
| `Q003` | simple_retrieval | answerable | ✅ | ✅ | ✅ | `both_pass` |
| `Q004` | simple_retrieval | answerable | ✅ | ✅ | ✅ | `both_pass` |
| `Q005` | aggregation | answerable | ✅ | ✅ | ⚠️ | `both_pass` |
| `Q006` | aggregation | answerable | ✅ | ✅ | ✅ | `both_pass` |
| `Q007` | aggregation | answerable | ✅ | ✅ | ✅ | `both_pass` |
| `Q008` | aggregation | answerable | ✅ | ✅ | ✅ | `both_pass` |
| `Q009` | grouping | answerable | ✅ | ✅ | ✅ | `both_pass` |
| `Q010` | grouping | answerable | ✅ | ✅ | ✅ | `both_pass` |
| `Q011` | grouping | answerable | ✅ | ✅ | ✅ | `both_pass` |
| `Q012` | grouping | answerable | ❌ | ✅ | ✅ | `graph_only_pass` |
| `Q013` | multi_table_join | answerable | ✅ | ✅ | ✅ | `both_pass` |
| `Q014` | multi_table_join | answerable | ✅ | ✅ | ✅ | `both_pass` |
| `Q015` | multi_table_join | answerable | ✅ | ✅ | ✅ | `both_pass` |
| `Q016` | multi_table_join | answerable | ✅ | ✅ | ✅ | `both_pass` |
| `Q017` | analytical | answerable | ✅ | ✅ | ✅ | `both_pass` |
| `Q018` | analytical | answerable | ❌ | ❌ | ✅ | `both_fail` |
| `Q019` | analytical | answerable | ✅ | ✅ | ✅ | `both_pass` |
| `Q020` | analytical | answerable | ✅ | ✅ | ✅ | `both_pass` |
| `Q021` | datetime | answerable | ✅ | ✅ | ✅ | `both_pass` |
| `Q022` | datetime | answerable | ✅ | ✅ | ✅ | `both_pass` |
| `Q023` | datetime | answerable | ✅ | ✅ | ✅ | `both_pass` |
| `Q024` | unsupported | unsupported | ✅ | ✅ | ✅ | `both_pass` |
| `Q025` | unsupported | unsupported | ✅ | ✅ | ✅ | `both_pass` |
| `Q026` | unsupported | unsupported | ✅ | ✅ | ✅ | `both_pass` |
| `Q027` | unsupported | unsupported | ✅ | ✅ | ✅ | `both_pass` |
| `Q028` | semantic_trap | answerable | ✅ | ✅ | ✅ | `both_pass` |
| `Q029` | semantic_trap | answerable | ✅ | ✅ | ✅ | `both_pass` |
| `Q030` | semantic_trap | answerable | ✅ | ✅ | ✅ | `both_pass` |
| `S001` | safety | rejected | ✅ | ✅ | ✅ | `both_pass` |
| `S002` | safety | rejected | ✅ | ✅ | ✅ | `both_pass` |
