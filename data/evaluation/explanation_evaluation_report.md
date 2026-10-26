# QueryPilot B13-EVAL2 — Deterministic Result Explanation & Faithfulness Benchmark Report

- **Benchmark Run:** Result Presentation & Answer Synthesis Offline Evaluation
- **Environment:** 100% Offline (Mocked Gemini & Zero Database Calls)
- **Git Commit:** `6c0bb2f`
- **Target Module:** `src/query_pilot/presentation/synthesizer.py`
- **Timestamp (UTC):** `2026-10-26T13:19:11.225561+00:00`

## 1. Executive Summary

- **Total Scenarios Evaluated:** 12
- **Scenarios Passed:** 12 (100.0%)
- **Scenarios Failed:** 0
- **Scalar Template Formatting Checks:** 3
- **Hallucination Detection Checks:** 2
- **Privacy & Zero-Disclosure Boundaries:** 3
- **Truncation & Failure Fallbacks:** 2

## 2. Category Summary

| Category | Scenarios | Passed | Status |
|---|:---:|:---:|:---:|
| `empty_result` | 1 | 1/1 | **PASS** |
| `failure_fallback` | 1 | 1/1 | **PASS** |
| `hallucination_detection` | 2 | 2/2 | **PASS** |
| `narrative_classification` | 1 | 1/1 | **PASS** |
| `privacy_boundary` | 2 | 2/2 | **PASS** |
| `scalar_formatting` | 3 | 3/3 | **PASS** |
| `sql_literal_masking` | 1 | 1/1 | **PASS** |
| `truncation_disclosure` | 1 | 1/1 | **PASS** |

## 3. Scenario Results & Assertions

| ID | Status | Category | System Boundary | Functions Exercised |
|---|:---:|---|---|---|
| `EXP-01` | **PASS** | `scalar_formatting` | `production_scalar_template` | `format_scalar_template`, `AnswerSynthesisService.synthesize` |
| `EXP-02` | **PASS** | `scalar_formatting` | `production_scalar_template` | `format_scalar_template` |
| `EXP-03` | **PASS** | `scalar_formatting` | `production_scalar_template` | `format_scalar_template`, `AnswerSynthesisService.synthesize` |
| `EXP-04` | **PASS** | `empty_result` | `production_synthesizer_service` | `AnswerSynthesisService.synthesize`, `AnswerSynthesisService.synthesize_from_metadata` |
| `EXP-05` | **PASS** | `hallucination_detection` | `production_hallucination_heuristic` | `_detect_hallucinated_values`, `AnswerSynthesisService.synthesize_from_metadata` |
| `EXP-06` | **PASS** | `hallucination_detection` | `production_hallucination_heuristic` | `_detect_hallucinated_values` |
| `EXP-07` | **PASS** | `narrative_classification` | `production_synthesizer_service` | `AnswerSynthesisService.synthesize` |
| `EXP-08` | **PASS** | `privacy_boundary` | `production_metadata_synthesis` | `_question_requires_row_disclosure`, `AnswerSynthesisService.synthesize_from_metadata` |
| `EXP-09` | **PASS** | `privacy_boundary` | `production_metadata_synthesis` | `_question_requires_row_disclosure`, `AnswerSynthesisService.synthesize_from_metadata` |
| `EXP-10` | **PASS** | `sql_literal_masking` | `production_sql_literal_masking` | `mask_sql_literals`, `AnswerSynthesisService.synthesize_from_metadata` |
| `EXP-11` | **PASS** | `truncation_disclosure` | `production_synthesizer_service` | `AnswerSynthesisService.synthesize`, `AnswerSynthesisService.synthesize_from_metadata` |
| `EXP-12` | **PASS** | `failure_fallback` | `production_synthesizer_service` | `AnswerSynthesisService.synthesize`, `AnswerSynthesisService.synthesize_from_metadata` |

### Detailed Scenario Verification Details

#### `EXP-01`: Unambiguous Count Scalar Template — **PASS**
- **Category:** `scalar_formatting`
- **Boundary:** `production_scalar_template`
- **Functions Exercised:** `format_scalar_template`, `AnswerSynthesisService.synthesize`
- **Assertions Verified:**
  - format_scalar_template('StudentCount', 42) == 'Count: 42' [got 'Count: 42']
  - AnswerSynthesisService produced TEMPLATE_DETERMINISTIC with is_mechanically_verified=True
- **Heuristic Boundary & Limitation:** Token-based template heuristic formats unambiguous aggregate column names; does not inspect query AST.

#### `EXP-02`: Unambiguous Average, Min, and Max Templates — **PASS**
- **Category:** `scalar_formatting`
- **Boundary:** `production_scalar_template`
- **Functions Exercised:** `format_scalar_template`
- **Assertions Verified:**
  - Verified unambiguous average prefix ('Average: 87.5')
  - Verified unambiguous minimum prefix ('Minimum: 35')
  - Verified unambiguous maximum prefix ('Maximum: 3.95')
- **Heuristic Boundary & Limitation:** Recognizes tokens 'avg', 'average', 'mean', 'min', 'lowest', 'max', 'highest'; relies on non-conflicting naming.

#### `EXP-03`: Neutral Formatting for Ambiguous / Non-Count Columns — **PASS**
- **Category:** `scalar_formatting`
- **Boundary:** `production_scalar_template`
- **Functions Exercised:** `format_scalar_template`, `AnswerSynthesisService.synthesize`
- **Assertions Verified:**
  - Column 'total_revenue' formatted neutrally without assuming Count
  - Column 'discount_pct' formatted neutrally
  - Conflicting aggregate column 'min_max_spread' fell back neutrally
  - Synthesizer appended neutral-formatting disclosure note
- **Heuristic Boundary & Limitation:** Does not guess semantic meaning when column names contain ambiguous or non-count keywords.

#### `EXP-04`: Deterministic Empty Result Set Handling — **PASS**
- **Category:** `empty_result`
- **Boundary:** `production_synthesizer_service`
- **Functions Exercised:** `AnswerSynthesisService.synthesize`, `AnswerSynthesisService.synthesize_from_metadata`
- **Assertions Verified:**
  - synthesize() returns EMPTY_RESULT with is_mechanically_verified=True on 0 rows
  - synthesize_from_metadata() grounds 0-row outcome directly from metadata
- **Heuristic Boundary & Limitation:** Verifies empty-result deterministic short-circuit; assumes upstream execution correctly reported 0 rows.

#### `EXP-05`: Heuristic Detection of Fabricated Currency Claims — **PASS**
- **Category:** `hallucination_detection`
- **Boundary:** `production_hallucination_heuristic`
- **Functions Exercised:** `_detect_hallucinated_values`, `AnswerSynthesisService.synthesize_from_metadata`
- **Assertions Verified:**
  - Detected fabricated dollar symbol '$' in text
  - Detected euro (€), pound (£), rupee (₹), and yen (¥) symbols
  - synthesize_from_metadata safely downgraded currency hallucination to REQUIRES_ROW_DISCLOSURE
- **Heuristic Boundary & Limitation:** Heuristic regex check flags currency symbols; cannot detect non-currency financial claims (e.g. '500 dollars').

#### `EXP-06`: Heuristic Detection of Unsupported Numeric Constants — **PASS**
- **Category:** `hallucination_detection`
- **Boundary:** `production_hallucination_heuristic`
- **Functions Exercised:** `_detect_hallucinated_values`
- **Assertions Verified:**
  - Detected ungrounded numeric constant '99.5' outside allowed metadata bounds
  - Permitted grounded numbers matching metadata, SQL, and latency without false alarm
- **Heuristic Boundary & Limitation:** Bounds check checks whether numbers exist in {row_count, column count, latency, question, SQL}; does not verify semantic truth.

#### `EXP-07`: Faithful Multi-Row Narrative Classification as Unverified — **PASS**
- **Category:** `narrative_classification`
- **Boundary:** `production_synthesizer_service`
- **Functions Exercised:** `AnswerSynthesisService.synthesize`
- **Assertions Verified:**
  - Multi-row LLM output classified strictly as UNVERIFIED_NARRATIVE with is_mechanically_verified=False
- **Heuristic Boundary & Limitation:** Enforces the policy that LLM prose is never marked mechanically verified; does not verify semantic completeness.

#### `EXP-08`: Entity Query Pre-screening for Required Row Disclosure — **PASS**
- **Category:** `privacy_boundary`
- **Boundary:** `production_metadata_synthesis`
- **Functions Exercised:** `_question_requires_row_disclosure`, `AnswerSynthesisService.synthesize_from_metadata`
- **Assertions Verified:**
  - Correctly flagged all 5 entity queries as requiring row disclosure
  - synthesize_from_metadata safely refused cloud synthesis under zero-disclosure policy
- **Heuristic Boundary & Limitation:** Pattern-based heuristic looks for question phrasing (who, which, what is, list); may not catch novel paraphrases.

#### `EXP-09`: Metadata-Answerable Query Classification — **PASS**
- **Category:** `privacy_boundary`
- **Boundary:** `production_metadata_synthesis`
- **Functions Exercised:** `_question_requires_row_disclosure`, `AnswerSynthesisService.synthesize_from_metadata`
- **Assertions Verified:**
  - Non-entity question passed pre-screening without requiring row disclosure
  - Produced GROUNDED_FROM_METADATA response for metadata-only query
- **Heuristic Boundary & Limitation:** Demonstrates that metadata-only queries can proceed when no cell disclosure is required.

#### `EXP-10`: AST SQL Literal Masking & Fail-Closed Protection — **PASS**
- **Category:** `sql_literal_masking`
- **Boundary:** `production_sql_literal_masking`
- **Functions Exercised:** `mask_sql_literals`, `AnswerSynthesisService.synthesize_from_metadata`
- **Assertions Verified:**
  - AST parser masked string and numeric literals while preserving tables, columns, and operators
  - mask_sql_literals returned None on unparseable SQL AST (fail-closed)
- **Heuristic Boundary & Limitation:** Relies on sqlglot AST parsing; dialect-specific syntax errors fall back closed to None.

#### `EXP-11`: Truncation Disclosures on Sampled Results — **PASS**
- **Category:** `truncation_disclosure`
- **Boundary:** `production_synthesizer_service`
- **Functions Exercised:** `AnswerSynthesisService.synthesize`, `AnswerSynthesisService.synthesize_from_metadata`
- **Assertions Verified:**
  - synthesize() attached truncation disclosure note to GroundedAnswer
  - synthesize_from_metadata attached truncation caveat note
- **Heuristic Boundary & Limitation:** Verifies that truncation metadata is attached; does not evaluate whether prose itself discusses truncation.

#### `EXP-12`: Provider Failure and Timeout Safe Fallback — **PASS**
- **Category:** `failure_fallback`
- **Boundary:** `production_synthesizer_service`
- **Functions Exercised:** `AnswerSynthesisService.synthesize`, `AnswerSynthesisService.synthesize_from_metadata`
- **Assertions Verified:**
  - synthesize() caught TimeoutError and safely fell back to SYNTHESIS_FAILED
  - synthesize_from_metadata caught provider error and returned UNAVAILABLE
- **Heuristic Boundary & Limitation:** Tests safe error handling and graceful degradation; does not retry transient provider failures.

## 4. Heuristic Boundaries & Semantic Limitations Disclosure

> **CRITICAL METHODOLOGICAL DISTINCTION:**
> This benchmark deterministically verifies the runtime guardrails, classification logic, and heuristic checks
> implemented in `src/query_pilot/presentation/synthesizer.py`. It specifically establishes:
> 1. **Scalar Formatting vs. Neutral Fallback**: Verified that column token heuristics prevent count over-generalization.
> 2. **Heuristic Hallucination Screening**: Verified that `_detect_hallucinated_values` flags currency symbols and numbers outside metadata bounds.
> 3. **Unverified Narrative Policy**: Verified that multi-row LLM prose is classified as `UNVERIFIED_NARRATIVE` with `is_mechanically_verified=False`.
> 4. **Privacy Boundaries**: Verified that entity queries trigger `REQUIRES_ROW_DISCLOSURE` and that SQL literals are masked.
> 5. **Safe Failure Degradation**: Verified that provider errors and timeouts fall back cleanly without unhandled exceptions.
>
> **WHAT THIS BENCHMARK DOES NOT ESTABLISH:**
> - It does NOT claim to provide an exhaustive semantic fact-checker. Non-numeric or qualitative hallucinations (e.g. inventing dates or false business logic)
>   cannot be detected by regex heuristics.
> - A passing status on `EXP-07` confirms that output is classified as `UNVERIFIED_NARRATIVE`; it does NOT prove the narrative is semantically faithful to the sample rows.
> - Offline evaluation with mocked clients does not evaluate live Gemini model latency or cloud generation quality.
