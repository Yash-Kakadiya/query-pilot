# QueryPilot Evaluation & Benchmark Framework

## 1. Overview and Purpose

QueryPilot employs an offline-first, evidence-based evaluation architecture to assess natural-language-to-SQL generation, conversational multi-turn workflows, result presentation, and structured clarification resolution.

The evaluation framework is designed around strict separation between:
1. **Deterministic Contract & Workflow Verification**: 100% reproducible offline suites that execute zero live Gemini API calls and zero database queries. These suites verify Pydantic schema validation boundaries, AST security policies, status state machines, context window eviction invariants, and question reformulation logic.
2. **Live LLM Model-Quality Evaluation**: Online benchmarks evaluating actual generative accuracy, semantic intent extraction, and dialect compliance across real database instances.

---

## 2. Deterministic Clarification Benchmark (B13-EVAL3)

The Clarification Evaluation Benchmark ([`tests/eval_clarification.py`](../tests/eval_clarification.py)) provides comprehensive, offline regression testing for QueryPilot's structured clarification contracts, option validation boundaries, and resolution workflows introduced across milestones `B13-F13B` and `B13-F14`.

### Invocation Commands

```powershell
# Run the standalone CLI evaluation runner
python scripts/run_clarification_evaluation.py

# Or run the pytest regression test suite
pytest -v tests/eval_clarification.py
```

### Generated Artifacts

Executing the runner generates two persistent evaluation artifacts under `data/evaluation/`:
- **JSON Evidence Artifact**: [`data/evaluation/clarification_evaluation.json`](../data/evaluation/clarification_evaluation.json) — Complete machine-readable record including execution metadata, scenario traces, and dynamically derived secondary metrics.
- **Auditable Markdown Report**: [`data/evaluation/clarification_evaluation_report.md`](../data/evaluation/clarification_evaluation_report.md) — Human-readable scenario breakdown, category aggregations, and methodology disclosures.

### Canonical Scenarios (CLAR-01 to CLAR-10)

| Scenario ID | Category | System Boundary | Objective & Verified Invariant |
|---|---|---|---|
| **CLAR-01** | `lexical_ambiguity` | Production Generation Contract | Ambiguous domain entity ("senior students" -> cohort vs marks) produces `status="clarification_needed"` with 2 structured options. |
| **CLAR-02** | `temporal_ambiguity` | Production Generation Contract | Ambiguous time window ("recent enrollments" -> semester vs 30 days) adheres strictly to `ClarificationOption` schema. |
| **CLAR-03** | `aggregation_ambiguity` | Production Generation Contract | Ambiguous aggregation metric ("average student score" -> exam score mean vs student GPA mean) with null SQL. |
| **CLAR-04** | `scope_filter_ambiguity` | Production API v1 Contract | Scope filter ambiguity ("faculties in Mathematics" -> teaching vs registered) preserved in `GenerateSQLResponse`. |
| **CLAR-05** | `direct_answerable_control` | Production Generation Contract | Negative control on explicit baseline query (`Q001`): returns direct `status="answerable"`, candidate SQL allowed by `SQLValidator`, and zero clarification options. |
| **CLAR-06** | `explicit_unsupported_control` | Production Generation Contract | Negative control on out-of-scope query (`Q024`): returns `status="unsupported"` with populated `unsupported_reason` and zero clarification options. |
| **CLAR-07** | `option_resolution_reformulation` | Reference Reformulation Workflow | Selecting option with `suggested_question` derives clarified question and generates AST-valid SQL without text concatenation drift. |
| **CLAR-08** | `fallback_resolution` | Reference Reformulation Workflow | Selecting option with `suggested_question=None` defensively preserves original user question intact. |
| **CLAR-09** | `option_bounds_validation` | Production Validation Invariants | Enforces `MAX_CLARIFICATION_OPTIONS = 5` and rejects empty or whitespace-only option IDs and descriptions. |
| **CLAR-10** | `id_uniqueness_and_status_invariants` | Production Validation Invariants | Enforces case-insensitive option ID uniqueness and cross-field status constraints (e.g. forbidding SQL in clarification status). |

### Metrics and Pass/Fail Invariants

All secondary metrics in `ClarificationEvaluationMetrics` are dynamically computed from executed scenario results:
- **`total_scenarios`**: Total scenarios in suite (10).
- **`passed_scenarios`**: Count of passing scenarios (10 on baseline).
- **`pass_rate`**: Proportion of passing scenarios (1.0).
- **`ambiguity_types_verified`**: Dynamic count of passing ambiguity scenarios (`CLAR-01`–`CLAR-04`).
- **`negative_controls_verified`**: Dynamic count of passing negative control scenarios (`CLAR-05`, `CLAR-06`).
- **`resolution_workflows_verified`**: Dynamic count of passing reformulation scenarios (`CLAR-07`, `CLAR-08`).
- **`validation_invariants_verified`**: Dynamic count of passing schema validation scenarios (`CLAR-09`, `CLAR-10`).

If any scenario fails, the CLI runner prints a failure diagnostic to `stderr` and exits with **code 1**.

### Critical Methodological Distinction: Offline Contracts vs. Live LLM Ambiguity Sensitivity

> **WHAT THIS BENCHMARK ESTABLISHES:**
> - Proves that QueryPilot's Pydantic models (`ClarificationOption`, `SQLGenerationResponse`, `GenerateSQLResponse`) strictly enforce schema constraints, field boundaries, and cross-field invariants.
> - Proves that selecting a clarification option safely reformulates the user question using `suggested_question` or falls back to preserving the original question without text corruption.
> - Proves that direct answerable and unsupported queries proceed without generating erroneous clarification demands.
>
> **WHAT THIS BENCHMARK DOES NOT MEASURE:**
> - It does **NOT** measure live Gemini LLM ambiguity classification accuracy. Whether the live model consistently identifies natural-language ambiguity in unconstrained user queries is a prompt-sensitivity and model-quality question evaluated separately under live benchmarks.

---

## 3. Related Deterministic Benchmarks

1. **Multi-Turn Conversational Benchmark (`B13-M1`)**:
   - Location: [`tests/eval_multi_turn.py`](../tests/eval_multi_turn.py), [`scripts/run_multi_turn_evaluation.py`](../scripts/run_multi_turn_evaluation.py).
   - Artifacts: [`data/evaluation/multi_turn_evaluation.json`](../data/evaluation/multi_turn_evaluation.json), [`data/evaluation/multi_turn_evaluation_report.md`](../data/evaluation/multi_turn_evaluation_report.md).
   - Scope: Evaluates conversational context bounding ($\le 3$ turns), FIFO sliding-window eviction across 5 turns, topic reset boundaries, and prohibited data exclusion across 10 scenarios (`MT-01` to `MT-10`).

2. **Result Explanation & Faithfulness Benchmark (`B13-EVAL2`)**:
   - Location: [`tests/eval_explanation.py`](../tests/eval_explanation.py), [`scripts/run_explanation_evaluation.py`](../scripts/run_explanation_evaluation.py).
   - Artifacts: [`data/evaluation/explanation_evaluation.json`](../data/evaluation/explanation_evaluation.json), [`data/evaluation/explanation_evaluation_report.md`](../data/evaluation/explanation_evaluation_report.md).
   - Scope: Evaluates deterministic scalar formatting, empty result short-circuiting, currency and numeric hallucination screening, AST SQL literal masking, and provider error fallbacks across 12 scenarios (`EXP-01` to `EXP-12`).

3. **Baseline Text-to-SQL Benchmark Dataset**:
   - Location: [`data/evaluation/baseline_questions.json`](../data/evaluation/baseline_questions.json), [`data/evaluation/README.md`](../data/evaluation/README.md).
   - Scope: 34 curated test cases covering single-table projections, multi-table joins, aggregations, semantic traps (`Q028`–`Q030`), safety validation fixtures (`S001`, `S002`), and single-turn clarification prompts (`C001`, `C002`).
