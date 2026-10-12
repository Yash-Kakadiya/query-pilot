# QueryPilot Sequential vs. LangGraph Comparative Evaluation Report

> [!IMPORTANT]
> This report provides a paired, evidence-based reliability comparison between the
> existing sequential `QueryPipeline` (baseline) and the `QueryPilotWorkflow` (LangGraph).

## 1. Executive Summary & Agreement Overview

- **Evaluation Timestamp:** `2026-10-11T13:02:43.272476+00:00`
- **Total Cases Evaluated:** 32
- **Natural Language Cases (End-to-End Pipeline Evaluation):** 30
  - **Pipeline Agreement Rate:** 30/30 (100.0%)
  - **Both Modes Passed:** 30/30 (100.0%)
  - **Regressions (Sequential Passed, Graph Failed):** 0/30 (0.0%)
  - **Improvements (Graph Passed, Sequential Failed):** 0/30 (0.0%)
  - **Both Modes Failed:** 0/30 (0.0%)
  - **Incomparable Cases:** 0
- **Safety Fixture Cases (Deterministic AST Policy Check):** 2
  - **Rejection Rate (Both Modes):** 2/2 (100.0%)
- **Overall Dataset Totals (All Cases Combined):** 32
  - **Total Agreement Rate:** 32/32 (100.0%)

## 2. Side-by-Side Metric Comparison

| Metric | Sequential Mode | Graph Mode | Delta / Status |
| :--- | :--- | :--- | :--- |
| **Answerable Semantic Correctness** | 26/26 (100.0%) | 26/26 (100.0%) | Parity |
| **Exact Result Match Rate (Answerable)** | 12/26 (46.2%) | 11/26 (42.3%) | -1 |
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
| `Q012` | grouping | answerable | ✅ | ✅ | ✅ | `both_pass` |
| `Q013` | multi_table_join | answerable | ✅ | ✅ | ✅ | `both_pass` |
| `Q014` | multi_table_join | answerable | ✅ | ✅ | ✅ | `both_pass` |
| `Q015` | multi_table_join | answerable | ✅ | ✅ | ✅ | `both_pass` |
| `Q016` | multi_table_join | answerable | ✅ | ✅ | ✅ | `both_pass` |
| `Q017` | analytical | answerable | ✅ | ✅ | ✅ | `both_pass` |
| `Q018` | analytical | answerable | ✅ | ✅ | ✅ | `both_pass` |
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
