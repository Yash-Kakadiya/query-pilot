# QueryPilot — Bounded Recovery Mechanics Evaluation Report

> **Evaluation Type:** Deterministic Offline State Machine Verification  
> **Timestamp:** `2026-10-12T04:25:01.041534+00:00`  
> **Git Commit:** `6d734c2`  
> **Service Isolation:** 100% Offline (Zero Gemini API requests, Zero database connections)

---

## 1. Executive Summary

This evaluation verifies the mechanical correctness, attempt limits, safety boundaries, and call counts
of the LangGraph bounded recovery workflow. It exercises the actual compiled graph nodes and routing
functions against deterministic offline fixtures.

| Metric | Result | Denominator / Basis | Interpretation |
| :--- | :---: | :--- | :--- |
| **Scenario Pass Rate** | **15/15** (100.0%) | Total scenarios evaluated | All scenario call counts and assertions satisfied |
| **Recovery Success Rate** | **83.3%** | `5` successes / `6` attempts | Effective correction on eligible failures |
| **Recovery Exhaustion** | **1** | Reached 2-attempt limit | Terminated with `VALIDATION_REJECTED` |
| **Terminal Protection** | **6/6** (100.0%) | Terminal safety & infrastructure scenarios | Unsafe queries terminated at Attempt 1 |
| **Safety Violations** | **0** | Unvalidated exec, unexpected retry, unsafe runs | Zero safety compromises across suite |
| **Total Generation Calls** | **21** | Across 15 scenarios (1.40 calls/scenario) | Strict 1 or 2 calls per scenario |
| **Recovery Call Overhead** | **2.00x** | `12` actual / `6` baseline | Exactly 1 extra generation call per retry |

---

## 2. Recovery Mechanics vs. Live Empirical Gain

> [!IMPORTANT]
> **Evaluation Methodology Note:**
> A 100% recovery rate in offline scenarios verifies that the **workflow state machine, routing edges,
> error classifiers, attempt counters, and diagnostic feedback loops operate strictly as designed**.
> It does NOT claim that a live LLM will achieve a 100% empirical recovery rate in production.
> Live model correction efficacy depends on model capability and will be measured in separate empirical evaluations.

---

## 3. Scenario-by-Scenario Evaluation Results

| ID | Scenario Name | Category | Calls (Gen/Val/Exec) | Expected Status | Actual Status | Recovery | Result |
| :--- | :--- | :--- | :---: | :--- | :--- | :---: | :---: |
| **REC-01** | Recoverable Unknown Column Validation Failure | `recoverable_validation` | `2 / 2 / 1` | `SUCCESS` | `SUCCESS` | Succeeded | **PASS** |
| **REC-02** | Recoverable T-SQL Syntax Parse Failure | `recoverable_validation` | `2 / 2 / 1` | `SUCCESS` | `SUCCESS` | Succeeded | **PASS** |
| **REC-03** | Recoverable Row Limit Validation Failure | `recoverable_validation` | `2 / 2 / 1` | `SUCCESS` | `SUCCESS` | Succeeded | **PASS** |
| **REC-04** | Recovery Exhaustion After Two Validation Failures | `recovery_exhaustion` | `2 / 2 / 0` | `VALIDATION_REJECTED` | `VALIDATION_REJECTED` | Exhausted | **PASS** |
| **REC-05** | Recoverable Malformed Generation JSON | `recoverable_generation` | `2 / 1 / 1` | `SUCCESS` | `SUCCESS` | Succeeded | **PASS** |
| **REC-06** | Recoverable Database Execution Error | `recoverable_execution` | `2 / 2 / 2` | `SUCCESS` | `SUCCESS` | Succeeded | **PASS** |
| **REC-07** | Terminal Mutating SQL Rejection | `terminal_safety` | `1 / 1 / 0` | `VALIDATION_REJECTED` | `VALIDATION_REJECTED` | N/A | **PASS** |
| **REC-08** | Terminal Multi-Statement Query Rejection | `terminal_safety` | `1 / 1 / 0` | `VALIDATION_REJECTED` | `VALIDATION_REJECTED` | N/A | **PASS** |
| **REC-09** | Terminal Sensitive Column Rejection | `terminal_safety` | `1 / 1 / 0` | `VALIDATION_REJECTED` | `VALIDATION_REJECTED` | N/A | **PASS** |
| **REC-10** | Terminal Database Timeout Failure | `terminal_infrastructure` | `1 / 1 / 1` | `EXECUTION_FAILED` | `EXECUTION_FAILED` | N/A | **PASS** |
| **REC-11** | Terminal Database Operational Failure | `terminal_infrastructure` | `1 / 1 / 1` | `EXECUTION_FAILED` | `EXECUTION_FAILED` | N/A | **PASS** |
| **REC-12** | Terminal Provider Configuration Failure | `terminal_provider` | `1 / 0 / 0` | `GENERATION_FAILED` | `GENERATION_FAILED` | N/A | **PASS** |
| **REC-13** | Baseline Single-Pass Success | `baseline_success` | `1 / 1 / 1` | `SUCCESS` | `SUCCESS` | N/A | **PASS** |
| **REC-14** | Baseline Unsupported Question Refusal | `baseline_unsupported` | `1 / 0 / 0` | `UNSUPPORTED` | `UNSUPPORTED` | N/A | **PASS** |
| **REC-15** | Baseline Empty Result Set Execution | `baseline_empty` | `1 / 1 / 1` | `SUCCESS` | `SUCCESS` | N/A | **PASS** |

---

## 4. Call Overhead Breakdown

| Execution Stage | Actual Calls Across Suite | Baseline (Single-Pass Equivalent) | Additional Calls Attributable to Recovery |
| :--- | :---: | :---: | :---: |
| **SQL Generation** | `21` | `15` | `+6` |
| **SQL Validation** | `18` | `13` | `+5` |
| **Read-Only Execution** | `10` | `9` | `+1` |

In recovery-eligible scenarios:
- Baseline generation calls (1 call per scenario): `6`
- Actual generation calls (with retry): `12`
- Overhead multiplier: `2.00x`

---

## 5. Safety Invariant Audit Summary

| Invariant | Enforcing Mechanism | Verified Status |
| :--- | :--- | :---: |
| **Attempt Ceiling (Max 2)** | `MAX_GENERATION_ATTEMPTS = 2` hard ceiling in workflow routing | **VERIFIED** |
| **No Execution Without Validation** | Graph topology routes candidates exclusively through `validate_sql` before `execute_sql` | **VERIFIED** |
| **Redaction of Prior SQL** | Recovery instructions contain stable reason codes; candidate SQL strings are strictly excluded | **VERIFIED** |
| **Mutations / Injections Blocked** | `NOT_READ_ONLY`, `MULTI_STATEMENT`, `FORBIDDEN_OPERATION` terminate at Attempt 1 without retry | **VERIFIED** |
| **Sensitive Access Blocked** | `SENSITIVE_COLUMN` terminates at Attempt 1 with zero hints leaked | **VERIFIED** |
| **Infrastructure Faults Terminal** | `QueryTimeoutError` and `OperationalError` terminate at Attempt 1 to protect database | **VERIFIED** |
| **Empty Results Successful** | 0 rows returned yields `PipelineStatus.SUCCESS` with zero retry overhead | **VERIFIED** |

---

## 6. Assertion Logs & Detail Notes

- **REC-01 (Recoverable Unknown Column Validation Failure):** Passed. Invariant: _Replacement candidate revalidated before execution; prior SQL redacted from instructions._
- **REC-02 (Recoverable T-SQL Syntax Parse Failure):** Passed. Invariant: _Syntax failure sanitized; recovery feedback provides valid T-SQL guidance; revalidated._
- **REC-03 (Recoverable Row Limit Validation Failure):** Passed. Invariant: _Row limit constraint enforced; second candidate adds TOP and passes validation before execution._
- **REC-04 (Recovery Exhaustion After Two Validation Failures):** Passed. Invariant: _Maximum generation attempts (2) strictly enforced; execution never called on exhaustion._
- **REC-05 (Recoverable Malformed Generation JSON):** Passed. Invariant: _Malformed LLM output consumes an attempt; structured output instructions provided on retry._
- **REC-06 (Recoverable Database Execution Error):** Passed. Invariant: _Execution error classified safely; generic error guidance supplied without raw DB traces; revalidated._
- **REC-07 (Terminal Mutating SQL Rejection):** Passed. Invariant: _Mutating SQL is strictly terminal; zero recovery retries; zero database execution._
- **REC-08 (Terminal Multi-Statement Query Rejection):** Passed. Invariant: _Multi-statement stacking is strictly terminal; zero retry fuzzing; execution blocked._
- **REC-09 (Terminal Sensitive Column Rejection):** Passed. Invariant: _Sensitive column access is strictly terminal; zero feedback hints leaked._
- **REC-10 (Terminal Database Timeout Failure):** Passed. Invariant: _Timeouts terminate immediately to prevent cascading database contention._
- **REC-11 (Terminal Database Operational Failure):** Passed. Invariant: _Connection/operational drop is terminal; infrastructure faults are never retried._
- **REC-12 (Terminal Provider Configuration Failure):** Passed. Invariant: _Provider configuration failures terminate immediately without validation or retries._
- **REC-13 (Baseline Single-Pass Success):** Passed. Invariant: _First-attempt success requires exactly 1 generation and 1 execution call; no retry overhead._
- **REC-14 (Baseline Unsupported Question Refusal):** Passed. Invariant: _Out-of-scope question terminates cleanly on refusal without validation or execution._
- **REC-15 (Baseline Empty Result Set Execution):** Passed. Invariant: _Empty result set is a valid read-only execution outcome; zero retries triggered._
