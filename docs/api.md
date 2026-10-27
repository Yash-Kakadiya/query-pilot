# QueryPilot API Guide

> [!WARNING]
> **Architecture & Scope Notice**
> QueryPilot is organized into a decoupled architecture separating text-to-SQL generation from local database execution:
> - **Local Agent Daemon (`querypilot-agent` — Port 8001)**: Binds strictly to loopback (`127.0.0.1`), serves the web UI at `/app/`, introspects schemas locally, and executes approved SQL queries locally under Windows Integrated Authentication.
> - **Generation API Service (`querypilot-api` — Port 8000)**: Generates unexecuted candidate SQL and provides optional metadata-only answer synthesis via Google Gemini (`gemini-3.8-flash` configured default; `gemini-3.5-flash-lite` validated in live milestone tests). It has **zero database access or credentials**.
> - **Controlled Preview Status**: QueryPilot is authorized strictly as a Controlled Preview for local development and invited evaluations. Public distribution remains **BLOCKED**; unverified live Gemini generation across external multi-tenant production workloads remains unverified for general public release.
> - **Milestone-Verified Evidence**: Milestone B12-F5D verified live Gemini API generation and synthesis payload contracts; Milestone B12-F5E2 verified the complete browser journey against `QueryPilot_TestDB` (1 live Gemini generation call, 0 DB calls during generation, explicit execution of read-only query returning 2 records, and deterministic local answer rendering without external synthesis); Milestone B12-F5C verified 32 live SQL Server integration tests; Milestone B12-F5G reconciled 1,217 passing offline unit/contract tests (62 live integration tests deselected).
> - **Hard Execution Ceilings**: Local query execution is strictly clamped to a **15-second statement timeout** (`GLOBAL_MAX_TIMEOUT_SECONDS = 15`) and a **100-row result limit** (`GLOBAL_MAX_ROW_LIMIT = 100`).
> - **Timeout Distinction**: Upstream HTTP proxy and transport timeouts (default **30.0 seconds**) are network transport timeouts, completely distinct from database query statement execution timeouts (hard-clamped to 15s).

---

## 1. Decoupled Architecture Endpoints (Current v1 Workflow)

### 1.1 `POST /api/v1/generate-sql` (Hosted Generation API)
Translates a natural-language question and model-facing schema context into a read-only candidate SQL query without executing it.

* **Method:** `POST`
* **Path:** `/api/v1/generate-sql` (also available via local agent reverse-proxy at `http://127.0.0.1:8001/api/v1/generate-sql`)
* **Service:** Generation API (`querypilot-api :8000`)
* **Database Boundary:** **Zero database access.** The service never connects to customer databases and does not execute SQL queries.
* **Authentication:** Optional `X-QueryPilot-Access-Key: <string>` when configured on backend. No session cookie required.
* **Proxy Exposure:** Proxied on local agent daemon at `/api/v1/generate-sql` via the agent's proxy allowlist (`/api/health`, `/api/v1/generate-sql`, `/api/v1/synthesize-answer`, `/api/query`). Upstream proxy forwards `Content-Type`, `Accept`, `X-Request-ID`, and `X-QueryPilot-Access-Key`, stripping cookies and agent-specific headers.
* **Headers:** `Content-Type: application/json`, optional `X-Request-ID: <string>`, optional `X-QueryPilot-Access-Key: <string>`.

#### Request Model (`GenerateSQLRequest` — `extra = "forbid"`)

| Field | Type | Required | Constraints & Description |
| :--- | :--- | :---: | :--- |
| `question` | `string` | **Yes** | Natural-language query question (1–1,000 characters). Transmitted to Google Gemini. |
| `schema_context` | `object` | **Yes** | Model-facing database schema (`database_name`, list of `tables` with normalized `columns`). Transmitted to Google Gemini. |
| `conversation_context` | `array[ConversationTurnContext]` | No | Optional list of prior user conversation turns (maximum 3 turns). Sub-model detailed below. |
| `request_id` | `string` | No | Optional client tracking correlation ID (maximum 128 characters). |

##### Turn Context Object (`ConversationTurnContext` — `extra = "forbid"`)

| Field | Type | Required | Constraints & Description |
| :--- | :--- | :---: | :--- |
| `role` | `string` | No | Must be `"user"` (default: `"user"`). Role `"assistant"` is strictly rejected with HTTP 422. |
| `user_question` | `string` | **Yes** | Prior user question text (1–1,000 characters). Automatically trimmed. Rejects empty or whitespace-only strings. Accepts legacy key `content`. |
| `result_columns` | `array[string]` | No | Optional list of column names returned by that turn's execution (maximum 50 columns, default: `[]`). Each item must be a non-empty string identifier. |
| `resolved_intent` | `string \| null` | No | Optional brief summary of the resolved domain intent (maximum 1,000 characters). Automatically trimmed; empty string normalized to `null`. |

> **Security & Validation Invariants:**
> - **Forbidden SQL Fields:** Fields named `executed_sql`, `sql`, `query`, or `candidate_sql` are strictly forbidden inside `ConversationTurnContext` and will cause validation rejection (HTTP 422).
> - **Zero Row/Cell Records:** Raw database rows, cell dictionaries, and database credentials are prohibited and rejected under `extra = "forbid"`.
> - **Turn Limit:** At most 3 turns (`MAX_CONVERSATION_TURNS = 3`). Exceeding 3 turns triggers HTTP 422.

#### Response Model (`GenerateSQLResponse` — `extra = "forbid"`)

| Field | Type | Description |
| :--- | :--- | :--- |
| `candidate_sql` | `string \| null` | Generated read-only T-SQL candidate query. Present only when `status == "generated"`. |
| `status` | `string` | Generation outcome (`"generated"`, `"clarification_needed"`, `"unsupported"`, `"generation_failed"`). |
| `clarification_prompt` | `string \| null` | Clarification question when status is `"clarification_needed"`. |
| `clarification_options` | `array[object]` | Structured candidate interpretation choices (each with `option_id`, `description`, and optional `suggested_question`). Default: `[]`. |
| `unsupported_reason` | `string \| null` | Refusal explanation when status is `"unsupported"`. |
| `execution_disclaimer` | `string` | Fixed disclaimer: `"Candidate SQL is unexecuted."` |
| `is_executed` | `boolean` | Fixed literal `false` confirming the query has **not** been executed. |
| `request_id` | `string` | Correlated request tracking ID. |

---

### 1.2 `POST /api/v1/synthesize-answer` (Hosted Metadata Answer Synthesis)
Generates an optional natural-language answer narrative from query metadata and aggregate dimensions without receiving database row records.

* **Method:** `POST`
* **Path:** `/api/v1/synthesize-answer` (also available via local agent reverse-proxy at `http://127.0.0.1:8001/api/v1/synthesize-answer`)
* **Service:** Generation API (`querypilot-api :8000`)
* **Authentication:** Optional `X-QueryPilot-Access-Key: <string>` when configured on backend. No session cookie required.
* **Proxy Exposure:** Proxied on local agent daemon at `/api/v1/synthesize-answer` via the agent's proxy allowlist. The proxy strips cookies and agent headers before forwarding.
* **Privacy Boundary:** The request schema declares aggregate metadata and execution metrics (`question`, `candidate_sql`, `columns`, `row_count`, `truncated`, `execution_status`, `execution_time_ms`, and `request_id`) and does not include raw result rows or cell records. Unknown fields are rejected by model validation (`extra = "forbid"`), and column items are validated as SQL identifiers. However, `extra = "forbid"` alone does not inspect or prevent row/cell values from being embedded within permitted text fields (such as within the user's question or SQL string literals). When hosted synthesis is invoked, the question, candidate SQL, and column names are transmitted to Google Gemini. Note that during the Milestone B12-F5E2 browser journey, the UI executed deterministic local answer rendering directly from client memory without invoking this hosted synthesis route.
* **Headers:** `Content-Type: application/json`, optional `X-Request-ID: <string>`, optional `X-QueryPilot-Access-Key: <string>`.

#### Request Model (`SynthesizeAnswerRequest` — `extra = "forbid"`)

| Field | Type | Required | Constraints & Description |
| :--- | :--- | :---: | :--- |
| `question` | `string` | **Yes** | Original natural-language question (1–1,000 characters). |
| `candidate_sql` | `string` | **Yes** | Validated SQL query executed locally (1–10,000 characters). |
| `columns` | `array[string]` | **Yes** | List of returned column identifiers (1–100 items; validated against SQL identifier regex `^[a-zA-Z0-9_#$]+$|\[[a-zA-Z0-9_#$ ]+\]$`). |
| `row_count` | `integer` | **Yes** | Total count of rows returned by query (range: `0`–`10000`). |
| `truncated` | `boolean` | No | `true` if results exceeded effective row limit and were truncated (default: `false`). |
| `execution_status` | `string` | **Yes** | Must be strictly `"succeeded"`. |
| `execution_time_ms` | `float` | **Yes** | Local query execution time in milliseconds (range: `0.0`–`60000.0`). |
| `request_id` | `string` | **Yes** | Correlated request tracking ID (1–128 characters). |

#### Response Model (`SynthesizeAnswerResponse` — `extra = "forbid"`)

| Field | Type | Description |
| :--- | :--- | :--- |
| `summary` | `string` | Grounded prose narrative explaining findings from aggregate metadata. |
| `status` | `string` | Grounding outcome (`"grounded"`, `"ungrounded"`, `"partially_grounded"`, `"unavailable"`). |
| `grounding_notes` | `string \| null` | Transparency notes on synthesis grounding. |
| `request_id` | `string` | Correlated request tracking ID. |

---

### 1.3 `POST /agent/v1/profiles/{profile_id}/execute` (Local Agent Execution Route)
Executes a human-reviewed, read-only SQL query locally against an approved database profile.

* **Method:** `POST`
* **Path:** `/agent/v1/profiles/{profile_id}/execute`
* **Service:** Local Agent Daemon (`querypilot-agent :8001` only — **strictly local; rejected by proxy allowlist and never forwarded upstream**)
* **Authentication:** Valid loopback `qpt_session` `HttpOnly` cookie (established via `/agent/v1/auth/bootstrap`) and mandatory `X-QueryPilot-Request: 1` header for CSRF protection on state-changing agent endpoints.
* **Execution Boundary:** Enforces read-only AST safety validation, statement timeout (profile timeout clamped $\le$ 15s), result row limit (profile limit clamped $\le$ 100 rows), and database context identity checks (`SELECT DB_NAME()`).
* **Data Flow:** Executes locally against SQL Server via `pyodbc` using Windows Integrated Authentication. Tabular row records are returned directly to the loopback browser client and retained in browser memory; they are never transmitted to the hosted API service.

#### Request Model (`ExecuteQueryRequest` — `extra = "forbid"`)

| Field | Type | Required | Constraints & Description |
| :--- | :--- | :---: | :--- |
| `sql` | `string` | **Yes** | Read-only SQL candidate query to validate and execute (1–10,000 characters). |
| `request_id` | `string` | No | Optional client tracking correlation ID (maximum 128 characters). |

*(Note: Clients cannot override database targets, timeouts, or row limits in the request payload; execution parameters are governed strictly by the verified local connection profile and global ceilings).*

#### Response Model (`ExecuteQueryResponse` — `extra = "forbid"`)

| Field | Type | Description |
| :--- | :--- | :--- |
| `columns` | `array[string]` | List of returned column names in SELECT order. |
| `rows` | `array[object]` | Serialized tabular row records as JSON objects (retained strictly in local browser memory). |
| `row_count` | `integer` | Number of rows returned in this batch. |
| `truncated` | `boolean` | `true` if results were truncated by the effective row ceiling. |
| `execution_time_ms` | `float` | Server-side execution duration in milliseconds. |
| `request_id` | `string \| null` | Echoed correlation tracking ID. |

---

### 1.4 `GET /agent/v1/profiles` (List Approved Profiles)
Retrieves safe, redacted summaries of all approved database connection profiles registered in the local agent daemon.

* **Method:** `GET`
* **Path:** `/agent/v1/profiles`
* **Service:** Local Agent Daemon (`querypilot-agent :8001` only — strictly loopback)
* **Authentication:** Valid `qpt_session` cookie.
* **Privacy Guarantee:** Returns zero passwords, connection strings, or server hostnames. Returns only human-readable metadata (`profile_id`, `display_name`, `schema_name`, `timeout_seconds`, `max_row_limit`).

#### Response Model (`ListProfilesResponse`)
```json
{
  "profiles": [
    {
      "profile_id": "corp_dw",
      "display_name": "Corporate Data Warehouse",
      "schema_name": "dbo",
      "timeout_seconds": 15,
      "max_row_limit": 100,
      "has_allowlist": false,
      "allowed_tables": null
    }
  ]
}
```

---

### 1.5 `GET /agent/v1/profiles/{profile_id}/schema` (Introspect Database Schema)
Introspects the target profile's database schema locally via SQLAlchemy, verifies runtime database identity (`SELECT DB_NAME()`), redacts sensitive columns, and returns model-facing schema metadata.

* **Method:** `GET`
* **Path:** `/agent/v1/profiles/{profile_id}/schema`
* **Service:** Local Agent Daemon (`querypilot-agent :8001` only)
* **Authentication:** Valid `qpt_session` cookie.
* **Security & Redaction:** Replaces unknown profiles with HTTP 404 (never falls back to default databases). Strips sensitive columns matching `SensitiveColumnPolicy` (e.g. `password_hash`, `ssn`, `api_key`).
* **Query Parameter:** `project=true` (optional, default: true) projects schema into bounded `ModelFacingSchemaContext`.

---

### 1.6 `POST /agent/v1/profiles/validate-config` (Offline Profile Validation)
Performs 100% offline syntax, boundary, and policy validation of a proposed local SQL Server profile configuration.

* **Method:** `POST`
* **Path:** `/agent/v1/profiles/validate-config`
* **Service:** Local Agent Daemon (`querypilot-agent :8001` only)
* **Authentication:** Valid `qpt_session` cookie and `X-QueryPilot-Request: 1` header.
* **Zero I/O Guarantee:** Absolute zero database connections, zero ODBC driver calls, zero engine creation, and zero disk writes.
* **Security Enforcement:**
  - `server` target must be an approved loopback host (`localhost`, `127.0.0.1`, `[::1]`, `.\INSTANCE`). Arbitrary remote IPs or hostnames are rejected with HTTP 422.
  - `database_name` cannot be a system database (`master`, `model`, `msdb`, `tempdb`).
  - Cleartext credentials (`password`, `user`, `secret`, `token`) are strictly forbidden (`extra = "forbid"`).
  - Validates `profile_id` uniqueness against the local profile store without altering stored profiles.

#### Request Model (`ValidateProfileConfigRequest` — `extra = "forbid"`)
```json
{
  "profile_id": "analytics_local",
  "display_name": "Local Sales Analytics",
  "server": "localhost\\SQLEXPRESS",
  "database_name": "SalesAnalytics",
  "schema_name": "dbo",
  "timeout_seconds": 15,
  "max_row_limit": 100,
  "set_as_default": false
}
```

#### Response Model (`ValidateProfileConfigResponse`)
```json
{
  "valid": true,
  "message": "Profile configuration is valid and complies with security policies."
}
```

---

### 1.7 `POST /agent/v1/profiles` (Create & Register Approved Profile)
Registers and persists an approved database connection profile into the local profile store (`~/.query_pilot/profiles.json`).

* **Method:** `POST`
* **Path:** `/agent/v1/profiles`
* **Service:** Local Agent Daemon (`querypilot-agent :8001` only)
* **Authentication:** Valid `qpt_session` cookie and `X-QueryPilot-Request: 1` header.
* **Atomicity & Locking:** Re-loads latest profiles from disk under an exclusive cross-process lock (`profiles.json.lock`), re-checks for duplicate profile IDs (HTTP 409 Conflict), writes atomically via temporary sibling replace, applies Windows `icacls` user-only permissions, and synchronizes the active daemon memory store.
* **Decoupling Notice:** Successfully creating a profile registers it in the local store. **It does NOT prove reachability or establish an active database connection.**

#### Request Model (`CreateProfileRequest` — `extra = "forbid"`)
Accepts all fields of `ValidateProfileConfigRequest` plus optional `set_as_default: boolean`.

#### Response Model (`CreateProfileResponse`)
```json
{
  "profile": {
    "profile_id": "analytics_local",
    "display_name": "Local Sales Analytics",
    "schema_name": "dbo",
    "timeout_seconds": 15,
    "max_row_limit": 100,
    "has_allowlist": false,
    "allowed_tables": null
  },
  "is_default": false,
  "message": "Database connection profile created and registered successfully."
}
```

---

### 1.8 `POST /agent/v1/profiles/{profile_id}/test-connection` (Isolated Connection Test)
Executes an explicit, bounded connection test against a registered profile using an isolated SQLAlchemy `NullPool` engine.

* **Method:** `POST`
* **Path:** `/agent/v1/profiles/{profile_id}/test-connection`
* **Service:** Local Agent Daemon (`querypilot-agent :8001` only)
* **Authentication:** Valid `qpt_session` cookie and `X-QueryPilot-Request: 1` header.
* **Empty Request Payload:** Accepts `{}`. Client-supplied connection parameters or query strings are strictly forbidden (`extra = "forbid"`). All parameters resolve from the trusted server-side profile store.
* **Execution Boundary:** Connects using an isolated temporary engine (`poolclass=NullPool`), issues a single probe query `SELECT DB_NAME()`, verifies database context identity, measures round-trip latency, and immediately disposes the engine (`engine.dispose()`).
* **Safe Failure Classification:** Returns HTTP 200 with sanitized diagnosis and `connected: false` if connection fails. **Failed connection tests never delete the saved profile.**

#### Response Model (`TestConnectionResponse`)
```json
{
  "connected": true,
  "database_name": "SalesAnalytics",
  "latency_ms": 14.8,
  "message": "Connection verified successfully. SELECT DB_NAME() returned SalesAnalytics.",
  "failure_code": null
}
```

#### Diagnostic Failure Codes (`failure_code`):
| Failure Code | Description | User Action |
| :--- | :--- | :--- |
| `AUTH_FAILED` | Windows Integrated Authentication failed. | Verify active Windows user has access to SQL Server. |
| `UNREACHABLE` | SQL Server instance cannot be reached. | Ensure SQL Server is running and TCP/IP or Named Pipes is enabled. |
| `TIMEOUT` | Connection timed out. | Check local host responsiveness and firewall rules. |
| `DATABASE_MISMATCH` | Connected database does not match profile name. | Ensure target database exists and is not failing over to `master`. |
| `DRIVER_ERROR` | Missing or incompatible ODBC driver. | Verify Microsoft ODBC Driver 17 or 18 is installed. |
| `CONNECTION_FAILED` | Generic connection error. | Inspect SQL Server local error log for details. |

---

## 2. Legacy Prototype API (Phase 1–6 Monolithic Prototype)

> [!NOTE]
> The endpoints below represent the Phase 1–6 monolithic prototype where `querypilot-api` connected directly to database engines server-side. They are maintained for regression evaluation and legacy evaluation harnesses, but are distinct from the supported decoupled agent workflow above.

### Prerequisites
* Python `>=3.12` (as declared in `pyproject.toml`)
* Microsoft SQL Server Express or LocalDB with ODBC Driver 17 for SQL Server
* Activated virtual environment with dependencies installed from `requirements.txt`

### Setup and Installation
```powershell
# Create and activate virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# Install required dependencies
pip install -r requirements.txt

# Configure environment variables
cp .env.example .env
# Edit .env with your GEMINI_API_KEY and database connection settings
```

### Starting the Server
Start the service with Uvicorn, bound to localhost (`127.0.0.1`) on port `8000`:

```powershell
uvicorn query_pilot.api.app:app --host 127.0.0.1 --port 8000
```

> **Note:** Alternatively, `uvicorn query_pilot.api:app --host 127.0.0.1 --port 8000` is supported because `query_pilot.api` exports `app`. For development, `--reload` may be added if auto-reloading on code changes is desired.

### Interactive API Documentation
Once the server is running, browser-based documentation is accessible at:
* **OpenAPI / Swagger UI:** `http://127.0.0.1:8000/docs`
* **ReDoc:** `http://127.0.0.1:8000/redoc`
* **Raw OpenAPI JSON Schema:** `http://127.0.0.1:8000/openapi.json`

---

## 2. Endpoint Reference

### `GET /health`
Operational readiness probe verifying application liveness and active configuration.

* **Method:** `GET`
* **Path:** `/health`
* **Authentication:** None
* **Liveness Semantics:** This endpoint verifies strictly that the FastAPI process is running and capable of servicing HTTP requests. It **does NOT** verify downstream database connectivity or Gemini API availability.

#### Response Model (`HealthResponse`)
`extra = "forbid"`

| Field | Type | Description |
| :--- | :--- | :--- |
| `status` | `string` | Service status (default: `"healthy"`). |
| `version` | `string` | Application version (default: `"0.1.0"`). |
| `dialect` | `string` | Target SQL dialect (default: `"T-SQL"`). |
| `tracing_enabled` | `boolean` | `true` if LangSmith tracing is active; `false` otherwise. |

#### Response Example (HTTP 200)
```json
{
  "status": "healthy",
  "version": "0.1.0",
  "dialect": "T-SQL",
  "tracing_enabled": false
}
```

---

### `POST /query`
Executes a natural-language query through the autonomous QueryPilot workflow.

* **Method:** `POST`
* **Path:** `/query`
* **Headers:**
  * `Content-Type: application/json` (required)
  * `X-Request-ID: <string>` (optional client-supplied tracking ID)
  * `X-QueryPilot-Access-Key: <string>` (required when QUERY_PILOT_ACCESS_KEY is configured on backend)

#### Request Model (`QueryRequest`)
`extra = "forbid"` (Unknown fields trigger HTTP 422)

| Field | Type | Required | Constraints & Description |
| :--- | :--- | :---: | :--- |
| `question` | `string` | **Yes** | Natural-language question. Automatically trimmed. Minimum 1 non-whitespace character, maximum 1,000 characters. |
| `conversation_context` | `array[object]` | No | Optional list of prior user conversation turns (maximum 3 turns). |
| `execution_mode` | `string` | No | `"execute"` (default) or `"sql_only"`. When `"sql_only"`, validates generated SQL against QueryPilot's checked-in GradeSense demo schema without executing against or inspecting a live database. (This is not arbitrary-schema support for user-owned databases.) |
| `include_sql` | `boolean` | No | `false` (default) or `true`. When `true`, returns validated SQL if approved (`SUCCESS`, `VALIDATED_ONLY`, or `EXECUTION_FAILED` after passing safety validation). Rejected candidate SQL is never disclosed. |

##### Turn Context Object (`ConversationTurnContext`)
`extra = "forbid"`

| Field | Type | Required | Constraints & Description |
| :--- | :--- | :---: | :--- |
| `role` | `string` | No | Must be `"user"` (default: `"user"`). Role `"assistant"` is strictly rejected. |
| `user_question` | `string` | **Yes** | Prior user question text (1–1,000 characters). Automatically trimmed. Accepts legacy key `content`. |
| `resolved_intent` | `string \| null` | No | Optional summary of the resolved domain intent (max 1,000 characters). Automatically trimmed. |
| `result_columns` | `array[string]` | No | Optional list of column names returned by that turn's query (maximum 50 columns, default: `[]`). Each item must be a non-empty string identifier. |

> **Security Rule:** Fields named `sql`, `executed_sql`, `candidate_sql`, or `query` are strictly forbidden inside `ConversationTurnContext` and trigger validation rejection (HTTP 422). Raw database rows, cell dictionaries, and database credentials are prohibited and rejected under `extra = "forbid"`.

#### Response Model (`QueryResponse`)
`extra = "forbid"`

| Field | Type | Description |
| :--- | :--- | :--- |
| `request_id` | `string \| null` | Correlated request ID. |
| `status` | `string` | Execution outcome status code (see Domain Statuses below). |
| `question` | `string` | The natural-language question processed. |
| `message` | `string \| null` | User-facing summary or sanitized error description. |
| `columns` | `array[string]` | Output column names in result order (default: `[]`). |
| `rows` | `array[object]` | Output row records as key-value objects (default: `[]`). Empty for `VALIDATED_ONLY`. |
| `row_count` | `integer` | Number of rows returned (default: `0`). Zero for `VALIDATED_ONLY`. |
| `truncated` | `boolean` | `true` if rows exceeded `DB_MAX_ROW_LIMIT` and were truncated. |
| `scalar_value` | `any \| null` | Scalar value if the result is 1 row and 1 column; `null` otherwise. |
| `markdown_table` | `string \| null` | Formatted Markdown table representation of tabular results. |
| `explanation` | `string \| null` | Generator reasoning or explanation (sanitized). |
| `clarification_prompt` | `string \| null` | Clarification question when status is `CLARIFICATION_NEEDED`. |
| `clarification_options` | `array[object]` | Structured disambiguation choices (each with `option_id`, `description`, and optional `suggested_question`). |
| `validation_reasons` | `array[object]` | Sanitized policy rejection reasons when status is `VALIDATION_REJECTED` (each with `code` and `message`). |
| `grounded_answer` | `object \| null` | Structured natural-language answer with `text`, `status`, `is_mechanically_verified`, and `verification_notes`. |
| `generated_sql` | `string \| null` | Approved SQL query if `include_sql=true` was requested and query passed validation; `null` otherwise. Rejected SQL is never disclosed. |

---

## 3. Domain Outcomes & HTTP Status Codes

### Handled Pipeline Outcomes (HTTP 200 OK)
All completed pipeline workflows return HTTP 200. The specific operational result is indicated by the `status` field:

| `status` | Meaning | Key Response Fields Populated |
| :--- | :--- | :--- |
| `SUCCESS` | SQL generated, AST validated, executed read-only, results structured. | `columns`, `rows`, `row_count`, `markdown_table`, `grounded_answer` |
| `VALIDATED_ONLY` | SQL generated and validated against checked-in GradeSense demo schema; execution was skipped. | `message`, `generated_sql` (if requested), empty `rows` |
| `UNSUPPORTED` | Question falls outside the database domain or schema. | `message`, `explanation` |
| `CLARIFICATION_NEEDED` | Question is ambiguous; options provided for disambiguation. | `clarification_prompt`, `clarification_options` |
| `GENERATION_FAILED` | Model failed to generate valid SQL or hit quota limits. | `message` (sanitized) |
| `VALIDATION_REJECTED` | Deterministic AST safety rules blocked the generated query (e.g. mutation forbidden, table not in schema). | `validation_reasons`, `message` |
| `EXECUTION_FAILED` | Database execution failed (e.g. query timeout, T-SQL syntax error). | `message` (sanitized) |

### HTTP Error Codes

#### HTTP 422 Unprocessable Content
Returned when the incoming request body violates schema constraints (e.g. empty question, question > 1,000 characters, > 3 conversation turns, or unexpected extra fields).

* Sanitized format: Exposes only field locations (`loc`), error messages (`msg`), and validation rule types (`type`).
* Never echoes raw client inputs back in error messages.

```json
{
  "detail": [
    {
      "loc": ["body", "question"],
      "msg": "Value error, Question cannot be empty or whitespace.",
      "type": "value_error"
    }
  ]
}
```

#### HTTP 500 Internal Server Error
Returned when an unhandled server exception occurs during request execution.

* Returns a strictly constant, fixed generic message:
  ```json
  {
    "detail": "An internal server error occurred."
  }
  ```
* Never includes stack traces, raw exception strings, database connection strings, file paths, hostnames, or credentials.
* Correlates the error with the `X-Request-ID` response header for operational log inspection.

---

## 4. Request ID Correlation

The API provides end-to-end request tracing and log correlation via `X-Request-ID`:

1. **Client Header:** Callers can provide an `X-Request-ID` header.
2. **Sanitization:** The inbound value is sanitized via `sanitize_safe_identifier()` to allow only safe alphanumeric characters, hyphens, and underscores (max 64 characters).
3. **Auto-Generation:** If the header is missing, empty, or invalid, the server generates a unique identifier formatted as `req-{12-hex-chars}` (e.g., `req-a1b2c3d4e5f6`).
4. **Propagation:** The correlated ID is:
   * Included in the HTTP response headers: `X-Request-ID: req-a1b2c3d4e5f6`.
   * Included in the JSON response body: `"request_id": "req-a1b2c3d4e5f6"`.
   * Included in structured operational logs (recording question length, context turn count, and status without logging user questions or sensitive data).
5. **Stateless Scope:** The request ID is strictly a per-request operational correlation tag. It **does NOT** create or reference a server-side session.

---

## 5. Security, Privacy, and Operational Boundaries

1. **SQL Omitted by Default:** `generated_sql` is `null` in public responses. Callers consume structured results (`rows`, `columns`, `markdown_table`, `grounded_answer`) without exposing schema internals.
2. **Strict Input Envelopes (`extra="forbid"`):** Callers cannot supply SQL statements, schema overrides, database server parameters, timeouts, or retry limits.
3. **Data Sanitization:** All user-facing text fields (`message`, `explanation`, `clarification_prompt`) pass through `sanitize_message()`, which redacts connection strings (`PWD=...`, `UID=...`), Bearer tokens, and tracebacks.
4. **Stateless API:** QueryPilot does not persist conversation state or user sessions on the server. Clients maintain dialogue history and forward at most 3 relevant prior turns via `conversation_context`.
5. **Optional LangSmith Tracing:** Tracing is off by default and controlled exclusively through server environment variables (`LANGCHAIN_TRACING_V2=true`, `LANGCHAIN_API_KEY=...`, `LANGCHAIN_PROJECT=query-pilot`). Client requests cannot enable, disable, or alter tracing settings.
6. **Read-Only Database Access & Hard Resource Ceilings:** When executed via the Local Agent (`querypilot-agent`), SQL queries are strictly read-only and hard-clamped to a 15-second statement timeout (`GLOBAL_MAX_TIMEOUT_SECONDS = 15`) and a 100-row result limit (`GLOBAL_MAX_ROW_LIMIT = 100`). In the standalone prototype backend fallback, settings are governed by `DB_QUERY_TIMEOUT_SECONDS` (default 15s) and `DB_MAX_ROW_LIMIT` (default 100 rows).
7. **Request Payload Size Limits:** The local agent limits request bodies to 65,536 bytes (64 KB) by default with a 1 MiB configurable ceiling. The hosted API enforces a 1 MiB streaming payload ceiling via `StreamingPayloadSizeMiddleware`.

---

## 6. Examples

> **Note:** The following examples use illustrative university data. Actual results depend on the configured local database.

### Example 1: Successful Query (`curl`)

#### Request
```bash
curl -X POST "http://127.0.0.1:8000/query" \
  -H "Content-Type: application/json" \
  -H "X-Request-ID: req-example-001" \
  -d '{
    "question": "What is the average GPA of all students?"
  }'
```

#### Response (HTTP 200 OK)
```json
{
  "request_id": "req-example-001",
  "status": "SUCCESS",
  "question": "What is the average GPA of all students?",
  "message": "Found 1 record.",
  "columns": [
    "AverageGPA"
  ],
  "rows": [
    {
      "AverageGPA": 3.42
    }
  ],
  "row_count": 1,
  "truncated": false,
  "scalar_value": 3.42,
  "markdown_table": "| AverageGPA |\n|---:|\n| 3.42 |",
  "explanation": "Calculated the average GPA across all active students.",
  "clarification_prompt": null,
  "clarification_options": [],
  "validation_reasons": [],
  "grounded_answer": {
    "text": "The average GPA across all students is 3.42.",
    "status": "grounded",
    "is_mechanically_verified": true,
    "verification_notes": []
  },
  "generated_sql": null
}
```

---

### Example 2: Clarification Response (HTTP 200 OK)

#### Request
```bash
curl -X POST "http://127.0.0.1:8000/query" \
  -H "Content-Type: application/json" \
  -d '{
    "question": "Show top students"
  }'
```

#### Response
```json
{
  "request_id": "req-7b3f91c02e18",
  "status": "CLARIFICATION_NEEDED",
  "question": "Show top students",
  "message": "Clarification needed to proceed.",
  "columns": [],
  "rows": [],
  "row_count": 0,
  "truncated": false,
  "scalar_value": null,
  "markdown_table": null,
  "explanation": null,
  "clarification_prompt": "How would you like to define 'top students'?",
  "clarification_options": [
    {
      "option_id": "opt_gpa",
      "description": "Top students ranked by cumulative GPA.",
      "suggested_question": "Show top 10 students by cumulative GPA."
    },
    {
      "option_id": "opt_credits",
      "description": "Top students by total completed credits.",
      "suggested_question": "Show top 10 students by completed credits."
    }
  ],
  "validation_reasons": [],
  "grounded_answer": null,
  "generated_sql": null
}
```

---

### Example 3: Schema Validation Error (HTTP 422 Unprocessable Content)

#### Request (Forbidden Client SQL Injection Attempt)
```bash
curl -X POST "http://127.0.0.1:8000/query" \
  -H "Content-Type: application/json" \
  -d '{
    "question": "What is the student count?",
    "sql": "SELECT COUNT(*) FROM Students"
  }'
```

#### Response
```json
{
  "detail": [
    {
      "loc": [
        "body",
        "sql"
      ],
      "msg": "Extra inputs are not permitted",
      "type": "extra_forbidden"
    }
  ]
}
```

---

### Example 4: Python Client (`httpx`)

```python
import httpx

client = httpx.Client(base_url="http://127.0.0.1:8000", timeout=30.0)  # HTTP transport timeout, distinct from the 15s database execution ceiling

# 1. Health check
health_resp = client.get("/health")
print("Health status:", health_resp.json()["status"])

# 2. Execute natural language query
query_payload = {
    "question": "How many courses are in the Computer Science department?",
    "conversation_context": [
        {
            "role": "user",
            "user_question": "List all academic departments.",
            "resolved_intent": "list department names",
            "result_columns": ["DepartmentID", "DepartmentName"],
        }
    ],
}

headers = {"X-Request-ID": "client-job-42"}
response = client.post("/query", json=query_payload, headers=headers)

if response.status_code == 200:
    data = response.json()
    print(f"Status: {data['status']}")
    print(f"Request ID: {data['request_id']}")
    if data["status"] == "SUCCESS":
        print(f"Answer: {data['grounded_answer']['text']}")
        print(f"Rows ({data['row_count']}):", data["rows"])
    elif data["status"] == "CLARIFICATION_NEEDED":
        print(f"Clarification: {data['clarification_prompt']}")
else:
    print(f"Error {response.status_code}:", response.json())
```

---

## 5. Deployment Connectivity & CORS Policy

QueryPilot supports two production deployment architectures:

### 1. Same-Origin Deployment (Unified Reverse Proxy)
In a unified deployment, the frontend static assets and the FastAPI backend are served behind a single reverse proxy (such as Nginx, Caddy, Cloudflare, or an API gateway):
* Browser requests hit `/api/*` on the same domain.
* The reverse proxy strips `/api` and routes directly to FastAPI's root endpoints (`/health`, `/query`).
* **CORS is not required**: Leave `CORS_ALLOWED_ORIGINS` unset or empty in the backend environment. No CORS headers are emitted.

### 2. Split-Origin Deployment (Separate Frontend & Backend Hosts)
When the frontend SPA (e.g. deployed on a CDN or Vercel at `https://querypilot.example.com`) communicates directly with a separate backend API host (e.g. `https://api.querypilot.example.com`):
* Configure the backend with `CORS_ALLOWED_ORIGINS`:
  ```ini
  CORS_ALLOWED_ORIGINS=https://querypilot.example.com
  ```
* Multiple origins may be supplied as a comma-separated list or JSON array:
  ```ini
  CORS_ALLOWED_ORIGINS=https://querypilot.example.com,https://preview.querypilot.example.com
  ```
* Configure the frontend build environment with `VITE_API_BASE_URL`:
  ```ini
  VITE_API_BASE_URL=https://api.querypilot.example.com
  ```

### Security & CORS Rules
* **Explicit Origins Only:** Wildcards (`*`) are strictly rejected. Each origin must specify an explicit `http://` or `https://` scheme and valid host/port without paths, query strings, or fragments.
* **Credentials Disabled:** `allow_credentials` is set to `False`. QueryPilot queries are uncredentialed and do not accept cross-origin cookies.
* **Minimal Methods & Headers:** Only `GET`, `POST`, and `OPTIONS` are permitted. Accepted request headers: `Content-Type`, `Accept`, `X-Request-ID`, and `X-QueryPilot-Access-Key`.
* **Header Exposure:** `X-Request-ID` is explicitly exposed in `Access-Control-Expose-Headers` so browser clients can track request correlation IDs.
* **Important Notice:** CORS is a browser cross-origin policy enforcement mechanism, **not** an authentication system. It restricts which web frontends can invoke the API from a browser, but does not substitute for network boundaries or API authorization.

---

## 6. Prototype Security & Abuse Protection

QueryPilot includes built-in protective controls intended for private evaluator deployments (such as Render prototypes):

### 1. Prototype Access Key (`X-QueryPilot-Access-Key`)
* When `QUERY_PILOT_ACCESS_KEY` is configured on the backend, every `/query` request must provide this value in the `X-QueryPilot-Access-Key` header.
* Missing or invalid keys immediately receive `HTTP 401 Unauthorized` (`{"detail": "Invalid or missing access key."}`).
* Constant-time comparison (`secrets.compare_digest`) prevents timing attacks.
* When `QUERY_PILOT_ACCESS_KEY` is unset or empty, access-key verification is inactive (allowing unauthenticated local development and testing).
* Setting `QUERY_PILOT_REQUIRE_ACCESS_KEY=true` causes the server to abort startup if `QUERY_PILOT_ACCESS_KEY` is missing or empty, enforcing a fail-closed deployment.

### 2. Server-Enforced SQL-Only Mode (`QUERY_PILOT_FORCE_SQL_ONLY`)
* When `QUERY_PILOT_FORCE_SQL_ONLY=true` is set on the backend, all queries are forced to `execution_mode="sql_only"` regardless of what `execution_mode` was requested in the request body.
* The request uses the in-memory GradeSense demo schema and bypasses database engine creation, live introspection, and SQL execution entirely, returning `status="VALIDATED_ONLY"`.

### 3. Request Payload Size Ceiling (`QUERY_PILOT_MAX_REQUEST_BYTES`)
* Enforces a hard ceiling on request body bytes (default: `65536` = 64 KB).
* Enforced while streaming ASGI request-body chunks: oversized requests are rejected with `HTTP 413 Content Too Large` as soon as accumulated bytes exceed the limit, without buffering the complete body in memory.
* Safely handles absent (chunked), invalid, or understated `Content-Length` headers, and rejects oversized requests before JSON parsing, Pydantic validation, pipeline execution, or runner invocation.

### 4. In-Memory Rate Limiting & Concurrency Guard
* **Rate Limit (`QUERY_PILOT_RATE_LIMIT_PER_MINUTE`):** Sliding 60-second window across the prototype (default: `20 RPM`). Excess requests receive `HTTP 429 Too Many Requests`.
* **Concurrency Guard (`QUERY_PILOT_MAX_CONCURRENT_QUERIES`):** Thread-safe semaphore capping active simultaneous queries (default: `3`). Prevents thread starvation and memory exhaustion on resource-constrained containers.

### 5. Architectural Limitations & Disclosures
* **Private Prototype Scope:** A shared access key is designed for invited reviewers and course evaluation. It is **not a substitute for user-level authentication** (e.g., Clerk, Supabase Auth) in a public SaaS product.
* **Process In-Memory State:** Rate limiter timestamps and concurrency semaphores are stored in memory and reset upon container restarts or spin-down.
* **LLM Context Transmission:** In SQL-only mode, user questions, conversation history, and schema definitions are still transmitted to Google Gemini (`gemini-3.8-flash`) for Text-to-SQL generation.
* **External Quota Limits:** Application-level rate limits do not guarantee complete protection against quota exhaustion or cost overruns. Hard spending limits and alert budgets must be configured in Google AI Studio / Google Cloud Console.
