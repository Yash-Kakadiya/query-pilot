# QueryPilot — Security, Error Handling & Adversarial Reliability Audit (Milestone B27-E Closeout)

**Audit Milestone:** B27-E.1 / B27-E.2 / B27-E.3  
**Verdict:** **PASS WITH LIMITATIONS**  
**Execution Mode:** Deterministic Offline Verification (Zero external provider calls, zero database mutations)  
**Evaluated Environment:** Windows 11, SQLite Engine (`data/demo/university_demo.db`), Local Agent Daemon (`127.0.0.1`), Desktop UI (Vite / React / Tauri)  

---

## 1. Executive Summary

Milestone B27-E conducted an adversarial evaluation and independent verification of QueryPilot's local-first security boundaries, deterministic SQL safety guarantees, execution authorization controls, error classifications, and frontend invariants.

The dedicated security audit runner (`scripts/verify_b27_e1_security_audit.py`) executed **56 automated checks**, of which **53 exercise production logic and enforcement boundaries** and **3 are static frontend source inspections**. All 56 checks passed deterministically. Complementing the audit runner, QueryPilot's dynamic frontend suite executed **455 tests with 0 failures**.

The overall audit verdict is recorded as **PASS WITH LIMITATIONS**. QueryPilot enforces strong, deterministic defenses against SQL injection, unauthorized execution, session hijacking, runaway queries, and credential disclosure at the local machine and application layers. However, cloud infrastructure controls (including TLS termination, production reverse proxies, and cloud IAM) were outside the scope of this local-first offline audit and remain unverified in this suite.

> [!IMPORTANT]
> **Audit Qualification and Scope Notice:**  
> This evaluation constitutes a milestone verification of specific local safety boundaries, input validators, authorizers, and error paths. It does **not** constitute a formal third-party certification, complete attack surface enumeration, or a universal security guarantee.

---

## 2. Dedicated Audit Runner Structure (56 Checks)

The audit suite `scripts/verify_b27_e1_security_audit.py` comprises 56 distinct deterministic assertions organized across six operational parts:

| Section | Check Count | Enforcement Type | Core Boundary Tested |
| :--- | :--- | :--- | :--- |
| **Part A: SQL Safety & Adversarial Inputs** | 20 | Production Code | AST parsing, token-level keyword prohibition, SQLite read-only authorizer, schema validation |
| **Part B: Execution Authorization & Integrity** | 7 | Production Code | Local agent route guards, session cookie validation, CSRF headers, non-empty candidate checks |
| **Part C: Authentication & Local Agent Security** | 7 | Production Code | Single-use bootstrap token unlinking, Host/Origin loopback filtering, 64KB request ceiling, session expiry |
| **Part D: Database Failure Handling** | 4 | Production Code | Missing file path rejection (0 files touched), corrupt header handling, lock timeouts, watchdog thread termination |
| **Part E: Provider & Network Failure Handling** | 10 | Offline Contract Mocks | Status code classification, Gemini quota fallback, retry guidance synthesis, sensitive error sanitization |
| **Part F: Frontend Reliability & Safety Contracts** | 8 (5 runner + 3 static) | Production / Static | Invalidation on edit, stale candidate execution block, contract response schemas, credential scrubbing |
| **Total** | **56** | **53 Production / 3 Static** | **100% Passed** |

### Static Source Inspections vs. Dynamic Tests
The audit runner incorporates 3 static source inspections in Part F that inspect `frontend/src/components/CandidateSqlReview.tsx` and `frontend/src/types/api.ts` to verify the presence of mandatory safety attributes:
1. `candidate-stale-banner` presence when questions are modified.
2. `candidate-execute-blocked-hint` presence to prevent stale execution.
3. Strict separation of `candidate_sql` and failure diagnostics in TypeScript interfaces.

These static inspections confirm the existence of UI safety elements in source code, while dynamic behavior (user interactions, state transitions, API calls, and edge cases) is comprehensively covered by the **455 dynamic Node/Vite test suites** (`frontend/tests/*.test.ts`).

---

## 3. Verified Boundaries vs. Mocked Contracts vs. Unverified Controls

### A. Locally Verified Production Boundaries
The following security controls were verified directly against production Python and TypeScript code without mocking:
- **Destructive SQL Rejection:** DROP TABLE, DELETE, UPDATE, INSERT, ALTER TABLE, and TRUNCATE are blocked before execution by both AST validation and low-level SQLite authorizer callbacks.
- **Statement Chaining & Injection:** Multiple statements (semicolon-separated), comment-based chain attacks, and subquery access to unapproved schemas are rejected.
- **SQLite Authorizer Restraints:** Disallowed actions including `ATTACH DATABASE`, `DETACH DATABASE`, `PRAGMA query_only = OFF`, `PRAGMA table_info`, `BEGIN TRANSACTION`, `SAVEPOINT`, and extensions (`load_extension`) are denied by the active SQLite authorizer.
- **Execution Authorization:** Candidate SQL cannot execute without explicit user invocation. Empty payloads, missing CSRF headers, unauthenticated sessions, and nonexistent profiles return HTTP 400/401/404.
- **Local Daemon Isolation:** The local agent rejects non-loopback bind hosts, non-loopback `Origin` headers, and mismatched `Host` headers. Bootstrap tokens are unlinked from disk immediately upon first read and cannot be replayed.
- **Watchdog Protection:** Long-running queries are interrupted by a dedicated background watchdog thread before the driver timeout ceiling.

### B. Mock-Based Offline Contract Verification
- **Provider Failure Classification:** Error mapping for Gemini HTTP 401, 403, 429 (quota exhaustion), and 503 is verified using offline mock HTTP responses. No live calls to Google Gemini or external networks were made.
- **Dual-Key Quota Fallback:** The bounded fallback mechanism that attempts a secondary key only upon provider quota exhaustion was tested with synthetic mock responses to verify fallback activation and prevention of key cycling on unrelated errors.
- **Retry Guidance Synthesis:** Extraction of retry delays from Google error metadata (and safe fallback when absent) was verified with offline contract fixtures.

### C. Skipped Tests & Platform Limitations
- **Windows Symlink Rejection:** Dynamic verification of symlink path traversal attacks against SQLite database files is marked as **SKIPPED** in standard Windows environments. Under Windows, `os.symlink` requires elevated administrative privileges or Developer Mode. While QueryPilot checks path resolution and restricts files to canonical paths, dynamic execution of the symlink creation test was skipped to avoid requiring administrator privileges during normal audit runs.

### D. Unverified Cloud Infrastructure Controls
The following enterprise and deployment controls were not part of this local offline audit and remain unverified by local test suites:
- **Cloud TLS Termination & Edge Routing:** HTTPS termination, certificate validity, and edge-proxy routing provided by Render or cloud gateways.
- **Cloud IAM & Production Key Management:** Cloud secret vaults, backend environment variable injection, and upstream rate limiting in deployed staging/production.
- **Live Upstream Gemini Quota Recovery:** Actual live quota recovery on Google Gemini infrastructure (verified offline via contract mocks only).

---

## 4. Database Integrity Verification

The SQLite demo database (`data/demo/university_demo.db`) was audited for unauthorized mutations throughout all test runs:

- **Pre-Audit SHA-256:** `384c847a3e5c5b3e1961c96dd851785afbbf121cda5fdea599879127758f9540`
- **Post-Audit SHA-256:** `384c847a3e5c5b3e1961c96dd851785afbbf121cda5fdea599879127758f9540`
- **Integrity Status:** `PRAGMA integrity_check` returned `ok`. 100% byte-identical, zero modifications.

---

## 5. Audit Closeout Conclusion

The B27-E security audit successfully validates that QueryPilot maintains a strict local-first security posture. All core local boundaries—AST validation, authorizer hooks, loopback guards, single-use bootstrap tokens, and candidate execution decoupling—function as intended with zero regressions. With the documentation of scope limits and platform qualifications, Milestone B27-E is formally closed out with a rating of **PASS WITH LIMITATIONS**.

