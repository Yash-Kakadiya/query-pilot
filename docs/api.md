# QueryPilot API Guide

This guide documents the QueryPilot FastAPI service, including local setup, endpoint contracts, error handling, request ID correlation, security guardrails, and usage examples.

---

## 1. Running Locally

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
| `user_question` | `string` | **Yes** | Prior user question text (1–1,000 characters). Accepts legacy key `content`. |
| `resolved_intent` | `string \| null` | No | Optional summary of the resolved domain intent (max 1,000 characters). |
| `result_columns` | `array[string]` | No | Optional list of column names returned by that turn's query (default: `[]`). |

> **Security Rule:** Fields named `sql`, `executed_sql`, `candidate_sql`, or `query` are strictly forbidden inside `ConversationTurnContext` and will cause validation rejection.

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
6. **Read-Only Database Access:** SQL execution is strictly read-only and governed by `DB_QUERY_TIMEOUT_SECONDS` (default 15s) and `DB_MAX_ROW_LIMIT` (default 100 rows).

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

client = httpx.Client(base_url="http://127.0.0.1:8000", timeout=30.0)

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
