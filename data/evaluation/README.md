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
- **Single-Request vs. Multi-Turn Harness Scope**: The 34-case dataset evaluates single-request generation boundaries with forwarded turn context. Multi-turn sequential state transitions and sliding-window invariants are evaluated separately by the dedicated 10-scenario offline benchmark harness documented below.

---

## Deterministic Multi-Turn Conversational Benchmark Harness (MT-01 to MT-10)

Milestone B13-M1 introduced a dedicated offline evaluation suite ([`tests/eval_multi_turn.py`](../../tests/eval_multi_turn.py)) designed to evaluate multi-turn conversational SQL workflows, context grounding, security isolation, and client state machine invariants under 100% deterministic, offline conditions.

### Benchmark Runner & Artifacts

Run the evaluation benchmark directly via the standalone CLI runner or the pytest regression test suite:

```powershell
# Run the standalone CLI evaluation runner
python scripts/run_multi_turn_evaluation.py

# Or run the comprehensive unit and scenario test suite
pytest -o pythonpath=. -v tests/test_multi_turn_evaluation.py
```

The runner executes all 10 scenarios and writes two persistent evaluation artifacts:
- **JSON Evidence Artifact**: [`data/evaluation/multi_turn_evaluation.json`](multi_turn_evaluation.json) — Structured scenario results, turn execution traces, and dynamically derived secondary metrics.
- **Auditable Markdown Report**: [`data/evaluation/multi_turn_evaluation_report.md`](multi_turn_evaluation_report.md) — Comprehensive scenario breakdown, boundary classifications, and execution evidence.

### Scenario Distribution & System Boundaries

The 10 benchmark scenarios evaluate both production backend contracts and reference-simulator client state transitions:

| Scenario ID | Category | System Boundary | Objective & Verified Invariant |
|---|---|---|---|
| **MT-01** | `dependent_follow_up` | Production Backend | Follow-up context grounding in `build_sql_generation_prompt`. |
| **MT-02** | `fifo_eviction` | Reference Simulator | FIFO sliding-window eviction strictly capping history to 3 turns across 5 sequential turns. |
| **MT-03** | `failure_preservation` | Reference Simulator | Preserving prior turn context across query execution failures. |
| **MT-04** | `boundary_clearing` | Reference Simulator | Context clearing boundaries across topic resets (`New Topic`) and profile switches. |
| **MT-05** | `prohibited_data_exclusion` | Production Backend | Pydantic schema rejection of 14 forbidden SQL fields, raw rows, and invalid roles. |
| **MT-06** | `duplicate_execution` | Reference Simulator | Idempotent execution preventing duplicate turn recording for the same query. |
| **MT-07** | `stale_async_guard` | Reference Simulator | Discarding stale or out-of-order asynchronous query responses. |
| **MT-08** | `unapproved_candidate` | Reference Simulator | Ensuring unexecuted candidate SQL is never recorded as an executed turn. |
| **MT-09** | `baseline_dataset_replay` | Production Backend | Validating baseline dataset clarification case `C002` through prompt assembly. |
| **MT-10** | `recovery_workflow_isolation` | Production Backend | Offline execution of recovery scenario `REC-01` and verifying recovery retry fields do not leak into turn context. |

### Known Metric Limitations & Methodology Notes

1. **`total_turns_executed` (13 turns)**: Counts declarative turns defined in `scenario.turns` across scenarios `MT-01` through `MT-04`. It does not include turns evaluated procedurally within custom scenario evaluators (`MT-06`, `MT-08`, `MT-09`, `MT-10`).
2. **`context_violations_prevented` (22) vs `data_leak_attempts_blocked` (14)**: `context_violations_prevented` functions as an aggregate umbrella metric that includes all 14 Pydantic schema validation rejections from `MT-05` plus 8 lifecycle and state boundary violations (1 from MT-02, 1 from MT-03, 2 from MT-04, 1 from MT-06, 1 from MT-07, 1 from MT-08, and 1 from MT-10).
3. **Recovery Workflow Isolation (`MT-10`)**: Genuinely executes recovery scenario `REC-01` to verify autonomous repair and separately verifies schema rejection of retry metadata; it does not evaluate an automated server-side adapter connecting recovery outputs to conversation turns, because in QueryPilot's decoupled architecture, turn recording is managed on the client upon successful execution.
4. **Offline Deterministic Scope**: The benchmark evaluates mechanical contracts, Pydantic schemas, prompt formatting, and state invariants offline with mocked dependencies. It does not measure live LLM semantic accuracy on follow-up questions or live database execution correctness.

---

## Result Explanation & Faithfulness Benchmark (Milestone B13-EVAL2)

The explanation evaluation harness ([`tests/eval_explanation.py`](file:///d:/01.%20Github/query-pilot/tests/eval_explanation.py)) provides a 100% offline, deterministic benchmark for QueryPilot's result presentation and answer synthesis layer ([`src/query_pilot/presentation/synthesizer.py`](file:///d:/01.%20Github/query-pilot/src/query_pilot/presentation/synthesizer.py)).

### Benchmark Invocation

Run the evaluation benchmark directly via the standalone CLI runner or the pytest test suite:

```powershell
# Run the standalone CLI evaluation runner
python scripts/run_explanation_evaluation.py

# Or run the automated test suite
pytest -o pythonpath=. -v tests/test_explanation_evaluation.py
```

The runner executes all 12 scenarios and generates two persistent evaluation artifacts:
- **JSON Evidence Artifact**: [`data/evaluation/explanation_evaluation.json`](explanation_evaluation.json) — Structured scenario results, evidence payloads, and dynamic metrics.
- **Auditable Markdown Report**: [`data/evaluation/explanation_evaluation_report.md`](explanation_evaluation_report.md) — Comprehensive scenario breakdown, production functions exercised, and heuristic boundary disclosures.

### Scenario Distribution & System Boundaries (12 Scenarios)

| Scenario ID | Category | System Boundary | Objective & Verified Invariant |
|---|---|---|---|
| **EXP-01** | `scalar_formatting` | Production Scalar Template | Unambiguous count projection formatted strictly as `Count: <val>` (`TEMPLATE_DETERMINISTIC`). |
| **EXP-02** | `scalar_formatting` | Production Scalar Template | Unambiguous average, min, and max projections mapped to exact aggregate prefixes. |
| **EXP-03** | `scalar_formatting` | Production Scalar Template | Ambiguous/competing scalar columns (`revenue`, `discount_pct`) falling back neutrally to `<Column>: <val>`. |
| **EXP-04** | `empty_result` | Production Synthesizer Service | 0-row results deterministically producing `EMPTY_RESULT` with `is_mechanically_verified=True` without LLM calls. |
| **EXP-05** | `hallucination_detection` | Production Hallucination Heuristic | Heuristic regex detection flagging unsupported currency symbols (`$`, `€`, `£`, `₹`, `¥`). |
| **EXP-06** | `hallucination_detection` | Production Hallucination Heuristic | Heuristic out-of-bounds detection flagging numeric values not present in metadata, SQL, or latency. |
| **EXP-07** | `narrative_classification` | Production Synthesizer Service | Multi-row LLM summaries strictly classified as `UNVERIFIED_NARRATIVE` with `is_mechanically_verified=False`. |
| **EXP-08** | `privacy_boundary` | Production Metadata Synthesis | Entity queries (`who`, `which`, `list`) safely identified as `REQUIRES_ROW_DISCLOSURE` under zero-disclosure policy. |
| **EXP-09** | `privacy_boundary` | Production Metadata Synthesis | Non-entity execution queries proceeding to `GROUNDED_FROM_METADATA` without disclosing cell records. |
| **EXP-10** | `sql_literal_masking` | Production SQL Literal Masking | AST parser masking string and numeric literals while preserving structure, with fail-closed protection. |
| **EXP-11** | `truncation_disclosure` | Production Synthesizer Service | Truncated result sets attaching explicit truncation caveats in verification notes and prompt notices. |
| **EXP-12** | `failure_fallback` | Production Synthesizer Service | Provider exceptions (e.g. `TimeoutError`) safely caught and degraded to `SYNTHESIS_FAILED` without crashing. |

### Crucial Methodology Distinction: Heuristic Screening vs. Semantic Faithfulness

1. **Heuristic Detection vs. Semantic Fact-Checking**: The production detection function (`_detect_hallucinated_values`) is a bounded heuristic checking for unsupported currency symbols and numbers outside metadata boundaries. It does **not** evaluate qualitative claims, entity name hallucination, or historical facts.
2. **`UNVERIFIED_NARRATIVE` Classification**: A passing status on `EXP-07` confirms that output is classified as unverified prose; it does **not** prove semantic alignment between prose and underlying data rows.
3. **Offline Deterministic Scope**: Evaluates presentation decision trees, formatting templates, AST masking, and fallback degradation offline with zero live model calls or database connections.

---

## Dataset Versioning and Historical Benchmark Compatibility

- **Dataset Evolution**: The baseline question suite originally contained 32 cases (`Q001`–`Q032`). The active suite was expanded to 34 cases with the addition of `C001` and `C002`.
- **Preservation of Historical Artifacts**: Saved historical evaluation artifacts (e.g., `baseline_results.json`, `baseline_results_1_14.json`, `comparison_1_22_audited.json`, `semantic_audit_1_26.json`) remain unchanged on their original 32-case baseline.
- **Comparability Warning**: Metrics derived from the historical 32-case dataset and the expanded 34-case dataset must not be compared directly without acknowledging the dataset difference, particularly regarding overall accuracy, execution rates, and the introduction of clarification recognition metrics.

---

## Semantic Trap Analysis

A critical principle established in QueryPilot is:

$$\text{Syntax Validity} \neq \text{Safety Validation} \neq \text{Semantic Correctness}$$

Cases `Q028`, `Q029`, and `Q030` test this boundary:
1. **`Q028` (Table Inheritance)**: `Students.Id` inherits from `Users.Id`. If an LLM erroneously joins `Students.DepartmentId = Users.Id`, both columns are valid `INT` types in the model-facing schema and pass deterministic AST validation, but semantically pair students with HODs/Deans.
2. **`Q029` (Primary vs Foreign Keys)**: `Faculties` has `Faculties.Id` (User inheritance) and `Faculties.DepartmentId` (FK to `Departments.Id`). Joining `Faculties.Id = Departments.Id` passes safety validation but joins user IDs to department IDs.
3. **`Q030` (Multi-Hop Join Disambiguation)**: `CourseOfferings` contains `SubjectId` and `SubjectCoordinatorId`. Equating `SubjectId = Faculties.Id` passes validation but conflates subjects with faculty members.

---

## Safety Fixtures

Cases `S001` and `S002` are **validation regression fixtures only**:
- They test that destructive DML (`DELETE`) and DDL (`DROP TABLE`) statements are deterministically caught by the deterministic AST policy layer before execution.
- They are **never executed** against any database.

---

## Ground-Truth Verification

All 26 `answerable` reference SQL statements:
1. Were verified against `validate_sql()` (100% approved, 0 policy violations).
2. Were executed directly on `GradeSense_Local` using `execute_read_only()`.
3. Have their actual, non-fabricated results stored in `reference_result`.
4. Include explicit `ORDER BY` clauses to ensure deterministic row comparisons.
