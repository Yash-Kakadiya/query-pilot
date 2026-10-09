# QueryPilot — Evaluation Reliability & Regression Report

## Provenance & Evaluation Context

- **Source Artifact:** `baseline_results_1_14.json`
- **Evaluation Dataset Version:** Task 1.14
- **Target Model:** `gemini-3.5-flash-lite`
- **Model Temperature:** `0.0`
- **Evaluation Run Duration:** 204.14s
- **Report Generated:** 2026-10-09T13:40:09Z
- **Evaluation Mode:** Saved Artifact Replay (Offline Deterministic Analysis)

## 1. Executive Summary & Overall Metrics

- **Total Test Cases:** 32
- **Natural-Language Generation Success:** 30/30 (100.0%)
- **Validator Acceptance (NL Queries):** 30/30 (100.0%)
- **Query Execution Success (Validated Queries):** 26/26 (100.0%)
- **Semantic Correctness Rate (Answerable Cases):** 26/26 (100.0%)
- **Exact Result Match Rate (Answerable Cases):** 12/26 (46.2%)
- **Semantic Trap Correctness (Q028–Q030):** 3/3 (100.0%) (Exact match: 2/3 (66.7%))
- **Structured Unsupported Refusal (Q024–Q027):** 4/4 (100.0%)
- **Safety Policy Enforcement (S001–S002):** 2/2 (100.0%) rejected before database execution

> [!NOTE]
> **Measurement Boundary Distinction:**
> - **Exact Result Match** requires literal equality of returned tabular result sets against the stored reference output.
> - **Semantic Correctness** evaluates whether the generated SQL accurately satisfies the user's business question without hallucinations, allowing safe differences in column aliasing, extra projections, or equivalent date bounds.
> - Syntactic SQL differences are NOT classified as pipeline failures when semantic correctness holds.

## 2. Category Breakdown

| Category | Total Cases | Generation | Validation | Execution | Exact Match | Semantic Correct |
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
| **Overall Total** | **32** | **30/30** | **32/32** | **26/26** | **12/26** | **26/26** |

## 3. Failure & Rejection Analysis

- **Total Pipeline Failures:** 0
- **Total Semantic Errors:** 0
- **Status:** All answerable questions executed successfully with verified semantic correctness.
- **Refusals:** All unsupported questions were recognized with structured refusals without attempting database execution.
- **Safety Fixtures:** All safety violations were blocked deterministically by the SQL policy validator.

## 4. Operational Metrics & Timing

### Database Query Execution Latency (ms)

- **Executed Queries Measured:** 26
- **Minimum Latency:** 5.67 ms
- **Median Latency:** 10.86 ms
- **Mean Latency:** 12.48 ms
- **Maximum Latency:** 30.13 ms

### Metric Availability in Source Artifact

| Operational Metric | Status | Note |
|---|:---:|---|
| Database Execution Latency | Available | Captured via executor `execution_time_ms` |
| Total Benchmark Duration | Available | Captured in artifact metadata (`execution_time_seconds`) |
| Per-Stage Pipeline Durations | Unavailable | Stage-level durations were not recorded in historical baseline JSON |
| Correlated Request IDs | Unavailable | Request IDs were introduced in Task 1.16 observability and are absent from historical data |
| Query Result Truncation Flags | Unavailable | Truncation flags were not stored in historical CaseEvaluationResult models |
