# QueryPilot B13-EVAL3 — Deterministic Clarification Benchmark Report

- **Benchmark Run:** Structured Clarification & Disambiguation Evaluation
- **Environment:** 100% Offline (Deterministic Models & Mocked Database Dependencies)
- **Git Commit:** `881d97f`
- **Timestamp (UTC):** `2026-10-27T04:19:26.745118+00:00`

## 1. Executive Summary

- **Total Scenarios Evaluated:** 10
- **Scenarios Passed:** 10 (100.0%)
- **Scenarios Failed:** 0
- **Ambiguity Pattern Scenarios Verified:** 4 / 4
- **Negative Control Scenarios Verified:** 2 / 2
- **Question Reformulation & Resolution Workflows:** 2 / 2
- **Bounds & Status Invariants Verified:** 2 / 2

## 2. Category Summary

| Category | Scenarios | Passed | Status |
|---|:---:|:---:|:---:|
| `aggregation_ambiguity` | 1 | 1/1 | **PASS** |
| `direct_answerable_control` | 1 | 1/1 | **PASS** |
| `explicit_unsupported_control` | 1 | 1/1 | **PASS** |
| `fallback_resolution` | 1 | 1/1 | **PASS** |
| `id_uniqueness_and_status_invariants` | 1 | 1/1 | **PASS** |
| `lexical_ambiguity` | 1 | 1/1 | **PASS** |
| `option_bounds_validation` | 1 | 1/1 | **PASS** |
| `option_resolution_reformulation` | 1 | 1/1 | **PASS** |
| `scope_filter_ambiguity` | 1 | 1/1 | **PASS** |
| `temporal_ambiguity` | 1 | 1/1 | **PASS** |

## 3. Scenario Results & Verified Assertions

| ID | Status | Category | System Boundary | Components Exercised |
|---|:---:|---|---|---|
| `CLAR-01` | **PASS** | `lexical_ambiguity` | `production_generation_contract` | `SQLGenerationResponse`, `GenerateSQLResponse`, `ClarificationOption` |
| `CLAR-02` | **PASS** | `temporal_ambiguity` | `production_generation_contract` | `SQLGenerationResponse`, `ClarificationOption`, `validate_clarification_options` |
| `CLAR-03` | **PASS** | `aggregation_ambiguity` | `production_generation_contract` | `SQLGenerationResponse`, `ClarificationOption` |
| `CLAR-04` | **PASS** | `scope_filter_ambiguity` | `production_api_v1_contract` | `GenerateSQLResponse`, `ClarificationOption`, `validate_clarification_options` |
| `CLAR-05` | **PASS** | `direct_answerable_control` | `production_generation_contract` | `SQLGenerationResponse`, `GenerateSQLResponse`, `SQLValidator` |
| `CLAR-06` | **PASS** | `explicit_unsupported_control` | `production_generation_contract` | `SQLGenerationResponse`, `GenerateSQLResponse` |
| `CLAR-07` | **PASS** | `option_resolution_reformulation` | `reference_reformulation_workflow` | `derive_clarified_question`, `SQLValidator`, `ClarificationOption` |
| `CLAR-08` | **PASS** | `fallback_resolution` | `reference_reformulation_workflow` | `derive_clarified_question`, `ClarificationOption` |
| `CLAR-09` | **PASS** | `option_bounds_validation` | `production_validation_invariants` | `validate_clarification_options`, `ClarificationOption`, `SQLGenerationResponse` |
| `CLAR-10` | **PASS** | `id_uniqueness_and_status_invariants` | `production_validation_invariants` | `validate_clarification_options`, `SQLGenerationResponse`, `GenerateSQLResponse` |

### Detailed Scenario Breakdown

#### `CLAR-01`: Lexical Ambiguity (Domain Entities) — **PASS**
- **Category:** `lexical_ambiguity`
- **System Boundary:** `production_generation_contract`
- **Components Exercised:** `SQLGenerationResponse`, `GenerateSQLResponse`, `ClarificationOption`
- **Assertions Verified:**
  - SQLGenerationResponse accepted status='clarification_needed' with null SQL
  - clarification_options contains exactly 2 structured options
  - Each option has non-empty option_id, description, and suggested_question
  - GenerateSQLResponse accepted status='clarification_needed' with preserved options
  - GenerateSQLResponse execution_disclaimer is 'Candidate SQL is unexecuted.' and is_executed=False
- **Scope & Limitation Note:** Deterministic contract and workflow verification only. Does not measure live LLM natural-language ambiguity sensitivity.

#### `CLAR-02`: Temporal Ambiguity (Time Windows) — **PASS**
- **Category:** `temporal_ambiguity`
- **System Boundary:** `production_generation_contract`
- **Components Exercised:** `SQLGenerationResponse`, `ClarificationOption`, `validate_clarification_options`
- **Assertions Verified:**
  - Status is strictly CLARIFICATION_NEEDED
  - Options adhere to ClarificationOption schema with temporal interpretations
  - Benchmark policy bound satisfied (2 options present)
- **Scope & Limitation Note:** Deterministic contract and workflow verification only. Does not measure live LLM natural-language ambiguity sensitivity.

#### `CLAR-03`: Aggregation Ambiguity (Score vs GPA Mean) — **PASS**
- **Category:** `aggregation_ambiguity`
- **System Boundary:** `production_generation_contract`
- **Components Exercised:** `SQLGenerationResponse`, `ClarificationOption`
- **Assertions Verified:**
  - Status is CLARIFICATION_NEEDED with null candidate SQL
  - Aggregation options cleanly distinguish row-level average from student-level average
  - Both options supply distinct non-empty suggested_question reformulations
- **Scope & Limitation Note:** Deterministic contract and workflow verification only. Does not measure live LLM natural-language ambiguity sensitivity.

#### `CLAR-04`: Scope / Filter Ambiguity (Active vs All Registered) — **PASS**
- **Category:** `scope_filter_ambiguity`
- **System Boundary:** `production_api_v1_contract`
- **Components Exercised:** `GenerateSQLResponse`, `ClarificationOption`, `validate_clarification_options`
- **Assertions Verified:**
  - Scope filter options validated by validate_clarification_options
  - GenerateSQLResponse preserves clarification_options without corruption
  - status is CLARIFICATION_NEEDED and candidate_sql is strictly None
- **Scope & Limitation Note:** Deterministic contract and workflow verification only. Does not measure live LLM natural-language ambiguity sensitivity.

#### `CLAR-05`: Non-Ambiguous Negative Control (Answerable Query) — **PASS**
- **Category:** `direct_answerable_control`
- **System Boundary:** `production_generation_contract`
- **Components Exercised:** `SQLGenerationResponse`, `GenerateSQLResponse`, `SQLValidator`
- **Assertions Verified:**
  - Status is ANSWERABLE (GENERATED in API v1)
  - candidate_sql is non-empty and read-only
  - clarification_prompt is None and clarification_options is empty list
  - SQLValidator allowed candidate SQL (allowed=True)
- **Scope & Limitation Note:** Deterministic contract and workflow verification only. Does not measure live LLM natural-language ambiguity sensitivity.

#### `CLAR-06`: Non-Ambiguous Negative Control (Unsupported Query) — **PASS**
- **Category:** `explicit_unsupported_control`
- **System Boundary:** `production_generation_contract`
- **Components Exercised:** `SQLGenerationResponse`, `GenerateSQLResponse`
- **Assertions Verified:**
  - Status is UNSUPPORTED for out-of-scope query
  - candidate_sql is strictly None
  - clarification_prompt is None and clarification_options is empty list
  - unsupported_reason is populated with clear domain boundary explanation
- **Scope & Limitation Note:** Deterministic contract and workflow verification only. Does not measure live LLM natural-language ambiguity sensitivity.

#### `CLAR-07`: Option Resolution & Reformulation Workflow — **PASS**
- **Category:** `option_resolution_reformulation`
- **System Boundary:** `reference_reformulation_workflow`
- **Components Exercised:** `derive_clarified_question`, `SQLValidator`, `ClarificationOption`
- **Assertions Verified:**
  - derive_clarified_question adopted non-empty suggested_question
  - Derived question matches expected: 'Show average test scores for students in Semester 7 or 8.'
  - Reformulation does NOT cumulatively append option description
  - Resolved SQL passes SQLValidator AST checks (allowed=True)
- **Scope & Limitation Note:** Deterministic contract and workflow verification only. Does not measure live LLM natural-language ambiguity sensitivity.

#### `CLAR-08`: Fallback Resolution on Null / Empty Suggested Question — **PASS**
- **Category:** `fallback_resolution`
- **System Boundary:** `reference_reformulation_workflow`
- **Components Exercised:** `derive_clarified_question`, `ClarificationOption`
- **Assertions Verified:**
  - ClarificationOption validator normalizes whitespace suggested_question to None
  - Fallback derivation preserves original question intact when suggested_question is None
  - Fallback derivation does NOT append option description or invent text
- **Scope & Limitation Note:** Deterministic contract and workflow verification only. Does not measure live LLM natural-language ambiguity sensitivity.

#### `CLAR-09`: Option Bounds & Non-Empty Fields Enforcement — **PASS**
- **Category:** `option_bounds_validation`
- **System Boundary:** `production_validation_invariants`
- **Components Exercised:** `validate_clarification_options`, `ClarificationOption`, `SQLGenerationResponse`
- **Assertions Verified:**
  - validate_clarification_options strictly rejects > 5 options
  - ClarificationOption rejects empty or whitespace-only option_id
  - ClarificationOption rejects empty or whitespace-only description
  - SQLGenerationResponse rejects CLARIFICATION_NEEDED when sql is not None
- **Scope & Limitation Note:** Deterministic contract and workflow verification only. Does not measure live LLM natural-language ambiguity sensitivity.

#### `CLAR-10`: ID Uniqueness & Status Invariant Constraints — **PASS**
- **Category:** `id_uniqueness_and_status_invariants`
- **System Boundary:** `production_validation_invariants`
- **Components Exercised:** `validate_clarification_options`, `SQLGenerationResponse`, `GenerateSQLResponse`
- **Assertions Verified:**
  - validate_clarification_options strictly enforces case-insensitive option_id uniqueness
  - SQLGenerationResponse rejects ANSWERABLE status when clarification_options is present
  - SQLGenerationResponse rejects UNSUPPORTED status when clarification_options is present
  - SQLGenerationResponse rejects CLARIFICATION_NEEDED when prompt and explanation are both absent
  - GenerateSQLResponse rejects candidate_sql under CLARIFICATION_NEEDED status
- **Scope & Limitation Note:** Deterministic contract and workflow verification only. Does not measure live LLM natural-language ambiguity sensitivity.

## 4. Methodological Scope & Limitations Disclosure

> **CRITICAL METHODOLOGICAL DISTINCTION:**
> This benchmark deterministically verifies the runtime contracts, Pydantic validation boundaries,
> option bounds enforcement, question reformulation logic, and cross-field status invariants
> across QueryPilot's clarification subsystem. It explicitly establishes:
> 1. **Option Schema Adherence**: Structured options strictly conform to `ClarificationOption` with non-empty fields.
> 2. **Option Bounds & Uniqueness**: Option lists are bounded to `<= 5` options and enforce case-insensitive ID uniqueness.
> 3. **Cross-Field Status Invariants**: `CLARIFICATION_NEEDED` responses strictly forbid candidate SQL, and `ANSWERABLE`/`UNSUPPORTED` responses strictly forbid clarification options.
> 4. **Reformulation & Fallback Behavior**: Selecting an option derives the clarified question from `suggested_question` or safely preserves the original question intact without string concatenation drift.
> 5. **Negative Controls**: Explicit answerable and unsupported queries proceed directly without triggering erroneous clarification demands.
>
> **WHAT THIS BENCHMARK DOES NOT MEASURE:**
> - This benchmark uses deterministic fixtures and does **NOT** measure live Gemini LLM ambiguity classification accuracy.
> - Whether a live generative model reliably detects subtle natural-language ambiguity in unconstrained user queries
>   is a prompt-sensitivity and model-performance question that requires live evaluation datasets.
