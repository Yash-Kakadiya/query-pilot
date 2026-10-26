# QueryPilot Multi-Turn Conversational Benchmark & Context Evaluation Report

- **Evaluation Timestamp**: `2026-10-26T06:17:48.010209+00:00`
- **Git Commit**: `b0261aa`
- **Evaluation Mode**: `100% Deterministic Offline Benchmark`
- **Benchmark Pass Rate**: `100.0% (10/10 passed)`

---

## Executive Summary

This evaluation establishes the deterministic, offline benchmark for QueryPilot multi-turn conversational SQL workflows.
It provides evidence-based mechanical verification across production backend boundaries and client reference-simulator transitions:
- **Production Backend Boundaries**: Evaluates Pydantic schema validation (`ConversationTurnContext`, `SQLGenerationRequest`), prompt assembly (`build_sql_generation_prompt`), baseline dataset integration (`C002`), and real autonomous recovery isolation (`REC-01`).
- **Reference-Simulator Boundaries**: Evaluates client state machine invariants (FIFO sliding-window eviction, failure preservation, topic reset, duplicate execution idempotency, and stale async rejection) in a deterministic Python simulation.
- **Evidence-Based Metrics**: All counters are derived strictly from passed assertions during execution (zero hardcoded constants).

---

## Scenario Results (MT-01 to MT-10)

| ID | Status | Category | System Boundary | Description |
|---|:---:|---|---|---|
| `MT-01` | PASSED | `dependent_follow_up` | `production_prompt_and_schema_boundary` | Dependent Follow-Up Context Grounding |
| `MT-02` | PASSED | `fifo_eviction` | `reference_simulator_fifo_boundary` | FIFO Eviction on 4+ Sequential Turns |
| `MT-03` | PASSED | `failure_preservation` | `reference_simulator_state_boundary` | Failure Context Preservation |
| `MT-04` | PASSED | `boundary_clearing` | `reference_simulator_state_boundary` | Context Clearing Boundaries (Topic & Profile) |
| `MT-05` | PASSED | `prohibited_data_exclusion` | `production_security_validator_boundary` | Prohibited Sensitive Data Exclusion |
| `MT-06` | PASSED | `duplicate_execution` | `reference_simulator_state_boundary` | Duplicate Execution Idempotency |
| `MT-07` | PASSED | `stale_async_guard` | `reference_simulator_state_boundary` | Stale Asynchronous Operation Guard |
| `MT-08` | PASSED | `unapproved_candidate` | `reference_simulator_state_boundary` | Unapproved Candidate Non-Inclusion |
| `MT-09` | PASSED | `baseline_dataset_replay` | `production_baseline_dataset_boundary` | Baseline Dataset Replay (Case C002) |
| `MT-10` | PASSED | `recovery_workflow_isolation` | `production_recovery_harness_boundary` | Recovery Workflow Context Isolation |

---

## Mechanical Metrics Summary

| Metric | Derived Value | Exact Derivation Basis |
|---|:---:|---|
| **Total Scenarios** | `10` | Canonical 10-scenario suite |
| **Passed Scenarios** | `10` | Dynamically counted passing scenarios |
| **Failed Scenarios** | `0` | Total minus passed |
| **Overall Pass Rate** | `100.0%` | Passed / Total scenarios |
| **Context Violations Prevented** | `22` | Dynamically summed across MT-02, MT-03, MT-04, MT-05, MT-06, MT-07, MT-08, MT-10 |
| **FIFO Evictions Verified** | `2` | Dynamically counted FIFO queue drop-offs in MT-02 |
| **Data Leak Attempts Blocked** | `14` | Dynamically counted validation rejections in MT-05 |

---

## Boundary Classification & Implementation Scope

| Scenario Group | Boundary Classification | Implementation Under Test | Production Coverage Note |
|---|---|---|---|
| **MT-01, MT-05, MT-09** | Production Backend | `query_pilot.sql.generation`, `query_pilot.sql.prompt`, `baseline_questions.json` | Real production code and baseline dataset directly exercised. |
| **MT-10** | Production Recovery Harness | `query_pilot.eval.recovery.execute_scenario(REC-01)` | Genuinely runs real offline recovery pipeline and proves context isolation. |
| **MT-02..MT-04, MT-06..MT-08** | Reference Simulator | `DeterministicConversationSimulator` | Python behavioral simulation of client coordinator invariants. |

> [!NOTE]
> **Production Client State Verification**
> Actual production client state machine transitions (`SqlGenerationCoordinator`) are implemented in TypeScript and verified by `frontend/tests/multiTurnConversation.test.ts` (328 tests) and end-to-end browser tests (`tests/test_browser_multi_turn_journey.py`).
> This Python benchmark verifies backend boundaries, dataset fidelity, and provides an offline reference simulation.
