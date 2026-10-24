# QueryPilot Configuration Guide

This guide documents the configuration contract, precedence rules, environment variables, CLI entry points, and operational boundaries for QueryPilot's decoupled local-agent architecture.

---

## 1. Architectural Distinction: Agent vs. Hosted API

QueryPilot separates concerns into two distinct services:

| Attribute | Local Agent Daemon (`querypilot-agent`) | Hosted/Local API Service (`querypilot-api`) |
| :--- | :--- | :--- |
| **Primary Role** | Local database execution, schema introspection, frontend serving (`/app/`), session authentication | Natural language to SQL generation, answer grounding, LLM orchestration |
| **Network Boundary** | Strict loopback (`127.0.0.1` / `localhost`) | Bound locally (`127.0.0.1`) or deployed in cloud container |
| **Default Port** | `8001` | `8000` |
| **Database Access** | **Direct**: Owns connection pool and executes SQL locally | **Zero**: No connection strings, no database credentials |
| **LLM Access** | **Zero**: Does not call Gemini directly; proxies allowed requests to API | **Direct**: Requires `GEMINI_API_KEY` for LLM generation |
| **Frontend Assets** | Serves compiled SPA from `frontend/dist` under `/app/` | Does not serve frontend static assets |

Both services are designed to run concurrently during interactive local usage.

---

## 2. Agent Configuration Precedence

The Local Agent daemon strictly enforces a 3-tier configuration precedence order:

1. **Explicit CLI Arguments**: Parameters explicitly supplied to `querypilot-agent` or `python -m query_pilot.agent.cli`.
2. **Supported Environment Variables**: Process-level environment variables (e.g., in Windows shell or container runtime).
3. **Code Defaults**: Hardened defaults defined in `query_pilot.agent.config.AgentConfig`.

No general configuration file precedence layer is introduced for agent runtime settings; configuration remains simple, explicit, and deterministic.

> [!NOTE]
> **Environment File Handling (`.env`)**:
> - The Hosted API service (`querypilot-api`) automatically loads supported configuration from a local `.env` file via its Pydantic Settings integration.
> - The Local Agent daemon (`querypilot-agent`) reads strictly from process-level environment variables (`os.getenv`). It does **not** automatically parse `.env` files. When launching the agent, ensure any required environment variables are set in the launching shell (e.g. via PowerShell `$env:VAR = "value"`) or loaded explicitly prior to execution.

---

## 3. Local Agent Configuration Inventory

| Setting | CLI Argument | Environment Variable | Default | Purpose / Constraints |
| :--- | :--- | :--- | :--- | :--- |
| `bind_host` | `--host` | `QUERYPILOT_AGENT_HOST` *(alias: `QUERYPILOT_AGENT_BIND_HOST`)* | `127.0.0.1` | Loopback socket address. Non-loopback addresses fail closed. |
| `bind_port` | `--port` | `QUERYPILOT_AGENT_PORT` | `8001` | TCP port (range: `1024`–`65535`). |
| `allow_dev_origin` | `--allow-dev-origin` | `QUERYPILOT_AGENT_ALLOW_DEV_ORIGIN` | `False` | Permits Vite dev server (`http://127.0.0.1:5173`). Must be `False` in production. |
| `token_dir` | `--token-dir` | `QUERYPILOT_TOKEN_DIR` *(alias: `QUERYPILOT_AGENT_TOKEN_DIR`)* | `~/.query_pilot` | Directory where single-use bootstrap token is provisioned. |
| `token_filename` | *N/A* | `QUERYPILOT_AGENT_TOKEN_FILENAME` | `agent.token` | Filename of the bootstrap token. |
| `profiles_file` | `--profiles-file` | `QUERYPILOT_PROFILES_FILE` | `~/.query_pilot/profiles.json` | Path to approved database connection profiles (loaded at agent startup; managed via `querypilot-agent profile`). |
| `session_idle_seconds` | *N/A* | `QUERYPILOT_AGENT_SESSION_IDLE_SECONDS` | `3600` (1 hr) | Sliding idle expiration window (range: `60`–`86400`s). |
| `session_max_lifetime_seconds` | *N/A* | `QUERYPILOT_AGENT_SESSION_MAX_LIFETIME_SECONDS` | `43200` (12 hrs) | Hard session expiration ceiling (range: `300`–`86400`s). |
| `max_request_bytes` | *N/A* | `QUERYPILOT_AGENT_MAX_REQUEST_BYTES` | `65536` (64 KB) | Maximum incoming HTTP request body size (range: `1024`–`1048576` bytes / 1 MiB ceiling). |
| `max_concurrent_requests` | *N/A* | `QUERYPILOT_AGENT_MAX_CONCURRENT_REQUESTS` | `2` | Maximum concurrent requests processed by agent. |
| `static_dir` | `--static-dir` | `QUERYPILOT_AGENT_STATIC_DIR` | Package `static/` or `frontend/dist` | Directory containing compiled frontend SPA assets. Resolves to package-bundled `query_pilot/agent/static` if present (wheel installation), falling back to repository `frontend/dist`. |
| `upstream_api_url` | `--upstream-api-url` | `QUERYPILOT_UPSTREAM_API_URL` *(alias: `QUERYPILOT_AGENT_UPSTREAM_API_URL`)* | `http://127.0.0.1:8000` | Upstream API URL. If HTTP, must be loopback. |
| `upstream_timeout_seconds` | *N/A* | `QUERYPILOT_UPSTREAM_TIMEOUT_SECONDS` *(alias: `QUERYPILOT_AGENT_UPSTREAM_TIMEOUT_SECONDS`)* | `30.0`s | Timeout for proxied upstream API requests (distinct from SQL statement execution timeout, which is hard-clamped to 15s). |

### 3.1 Supported Environment Variable Aliases

The Local Agent daemon supports backwards-compatible secondary aliases for key environment variables in `query_pilot.agent.config.AgentConfig`:

| Canonical Variable | Supported Alias | Default Value | Consuming Component |
| :--- | :--- | :--- | :--- |
| `QUERYPILOT_AGENT_HOST` | `QUERYPILOT_AGENT_BIND_HOST` | `127.0.0.1` | Local Agent Daemon (`AgentConfig.bind_host`, `agent/cli.py`) |
| `QUERYPILOT_TOKEN_DIR` | `QUERYPILOT_AGENT_TOKEN_DIR` | `~/.query_pilot` | Local Agent Daemon (`get_default_token_dir`, `agent/cli.py`) |
| `QUERYPILOT_UPSTREAM_API_URL` | `QUERYPILOT_AGENT_UPSTREAM_API_URL` | `http://127.0.0.1:8000` | Local Agent Daemon (`AgentConfig.upstream_api_url`, `agent/cli.py`) |
| `QUERYPILOT_UPSTREAM_TIMEOUT_SECONDS` | `QUERYPILOT_AGENT_UPSTREAM_TIMEOUT_SECONDS` | `30.0`s | Local Agent Daemon (`AgentConfig.upstream_timeout_seconds`) |

*Precedence*: If both the canonical variable and its alias are defined, the canonical variable takes precedence. Explicit CLI arguments always override both environment variables.

> [!IMPORTANT]
> **Frontend Build Requirement**:
> In a development repository checkout, running `pip install -e .` does **not** compile the frontend assets. The local agent requires pre-compiled static files in `frontend/dist/` (or the directory specified by `--static-dir` / `QUERYPILOT_AGENT_STATIC_DIR`) to serve the UI at `/app/`.
>
> To compile the frontend assets before launching `querypilot-agent`:
> ```powershell
> cd frontend
> npm install
> npm run build
> cd ..
> ```
> If `frontend/dist` or `frontend/dist/index.html` is absent when navigating to `http://127.0.0.1:8001/app/`, the agent will return an `HTTP 503 Service Unavailable` error page with instructions to run the build.

---

## 4. Database Profile Onboarding & Management

Starting in Milestone B12-C, the local agent automatically loads approved database connection profiles from `profiles_file` at startup. Profiles are safely created, inspected, and validated using the `querypilot-agent profile` CLI subcommands.

### 4.1 Supported Scope & Authentication Architecture

- **Supported Engine**: Microsoft SQL Server (`engine_type: "mssql"`).
- **Supported Authentication**: **Windows Integrated Authentication only** (`use_trusted_connection: true`).
- **Explicit Credential Limitation**: SQL username/password authentication is **strictly unsupported** in this milestone. Any profile attempting to provide a password, username, token, or `use_trusted_connection: false` is rejected immediately to prevent plaintext credential exposure.
- **Connection Boundary**: All database connection parameters remain strictly local to the agent process. Browser clients cannot provide arbitrary connection strings or target arbitrary database hosts.
- **CLI-Based Profile Onboarding**: Database profiles are registered strictly via the CLI (`querypilot-agent profile add`). There is **no browser-based profile creation wizard** in the web interface.
- **Hard Global Resource Ceilings**: SQL statement execution is hard-clamped to **15 seconds** (`GLOBAL_MAX_TIMEOUT_SECONDS = 15`), and query result sets are hard-clamped to **100 rows** (`GLOBAL_MAX_ROW_LIMIT = 100`). Profile configurations or query requests attempting to exceed these values are strictly clamped down.

### 4.2 Windows Integrated Authentication Prerequisites

1. **SQL Server Instance**: A local or network SQL Server instance reachable from the host machine (e.g., `localhost\SQLEXPRESS`).
2. **Windows Login & Permissions**: The active Windows user account must have login access and query permissions on the target database (e.g. `CONNECT`, `SELECT`).
3. **ODBC Driver**: An installed Microsoft ODBC Driver for SQL Server (e.g. `ODBC Driver 17 for SQL Server` or `ODBC Driver 18 for SQL Server`). Verify installed drivers in Windows PowerShell:
   ```powershell
   Get-OdbcDriver -Name "*SQL Server*"
   ```

### 4.3 Profile File Location and Precedence

The profile configuration file is located according to the standard agent configuration precedence:
1. `--profiles-file <path>` CLI option passed to `querypilot-agent`.
2. `QUERYPILOT_PROFILES_FILE` environment variable.
3. Default fallback: `~/.query_pilot/profiles.json` (resolves to `%USERPROFILE%\.query_pilot\profiles.json` on Windows).

### 4.4 Profile File Format

The profile file is formatted as JSON. It contains strictly non-secret metadata and connection parameters:

```json
{
  "profiles": {
    "local_testdb": {
      "profile_id": "local_testdb",
      "display_name": "Local Test Database",
      "engine_type": "mssql",
      "server": "localhost\\SQLEXPRESS",
      "database_name": "QueryPilot_TestDB",
      "driver": "ODBC Driver 17 for SQL Server",
      "use_trusted_connection": true,
      "timeout_seconds": 15,
      "max_row_limit": 100,
      "schema_name": "dbo",
      "allowed_tables": ["Customers", "Orders"]
    }
  },
  "default_profile_id": "local_testdb"
}
```

Key schema rules:
- `engine_type`: Must be `"mssql"`.
- `use_trusted_connection`: Must be `true`.
- `timeout_seconds`: Query execution timeout in seconds (default: `15`, range: `1`–`15`, hard-clamped to `15` by `GLOBAL_MAX_TIMEOUT_SECONDS`).
- `max_row_limit`: Result set truncation ceiling (default: `100`, range: `1`–`100`, hard-clamped to `100` by `GLOBAL_MAX_ROW_LIMIT`).
- **Forbidden keys**: Fields named `password`, `pwd`, `user`, `username`, `uid`, `secret`, `token`, or `api_key` are rejected with a security error.

### 4.5 File Permission Hardening, Windows ACL Enforcement, and Security Boundaries

- **Atomic Writes**: Profile files are always written to an exclusively created sibling temporary file and replaced atomically (`os.replace`) to prevent corrupted or partially written state.
- **Windows ACL Hardening**: When saving profiles on Windows, QueryPilot runs `icacls` to strip inheritance (`/inheritance:r`) and grant Full Control to the active domain-qualified trustee (`<USERDOMAIN>\<USERNAME>:(F)`). On POSIX systems, file permissions are restricted to `0600`.
- **Security Limitations & Operational Boundaries**:
  - **Windows Integrated Authentication**: All database connections run under the local security context of the active Windows user running `querypilot-agent`. No database passwords exist or are persisted.
  - **Plaintext Storage Limitation**: The profile file is stored as plaintext JSON and is **not encrypted at rest**. Only non-sensitive connection parameters (server name, database name, profile ID) are kept in the profile file; secret-bearing credentials are prohibited by design.
  - **ACL Verification Boundaries**: While `icacls` strips broad inherited groups (such as `Users`, `Authenticated Users`, or `Everyone`), this does not constitute a mathematically verified guarantee of exclusive access against every explicit ACE or every multi-user filesystem configuration, nor does it provide virtualized OS sandboxing.
  - **Risks of Custom Shared Directories**: Storing `profiles.json` in custom shared locations (e.g. network shares, world-readable directories, or filesystems lacking ACL support such as FAT32) can expose database metadata to other local users or fail security validation. Always maintain profile files in standard user-private directories such as `%USERPROFILE%\.query_pilot`.

### 4.6 CLI Profile Management Commands

The `querypilot-agent profile` command family provides profile onboarding and validation workflows:

#### Adding a Profile (`profile add`)

Non-interactive registration with explicit CLI options:
```powershell
querypilot-agent profile add `
  --id "local_testdb" `
  --name "Local Test Database" `
  --server "localhost\SQLEXPRESS" `
  --database "QueryPilot_TestDB" `
  --driver "ODBC Driver 17 for SQL Server" `
  --yes
```

Interactive registration (prompts for fields and asks for confirmation before writing):
```powershell
querypilot-agent profile add
```

Supported options for `profile add`:
- `--id` / `--profile-id`: Unique identifier (letters, numbers, underscores, hyphens).
- `--name` / `--display-name`: Human-readable label for UI selection.
- `--server`: SQL Server instance name (default: `localhost\SQLEXPRESS`).
- `--database` / `--database-name`: Target database name.
- `--driver`: ODBC driver name (default: `ODBC Driver 17 for SQL Server`).
- `--schema` / `--schema-name`: Default schema name (default: `dbo`).
- `--timeout` / `--timeout-seconds`: Query timeout in seconds (`1`–`15`, default: `15`, hard-clamped to 15s).
- `--max-rows` / `--max-row-limit`: Max row limit (`1`–`100`, default: `100`, hard-clamped to 100 rows).
- `--profiles-file`: Path to target custom profiles JSON file.
- `--yes` / `-y`: Skip interactive confirmation prompt.

*(Note: The first registered profile is automatically designated as the default active profile).*

#### Listing Profiles (`profile list`)

Lists all registered profiles in the configured store:
```powershell
querypilot-agent profile list
```

Outputs profile IDs, display names, database engines, target servers, databases, and default status. Sensitive credentials or raw connection strings are never stored or printed.

#### Validating Profiles (`profile validate`)

Validates the profile configuration file syntax, schema, and security rules without connecting to any database:
```powershell
querypilot-agent profile validate
```

Exits with code `0` if all profiles are valid and safe, or code `1` with actionable diagnostic messages if errors are detected.

### 4.7 Operational Troubleshooting Runbook

#### 1. Missing Frontend Build Assets (HTTP 503)
- **Symptom**: Opening `http://127.0.0.1:8001/app/` returns `HTTP 503 Service Unavailable` with a page titled *QueryPilot Frontend Build Not Found*.
- **Root Cause**: In a development checkout, `pip install -e .` does not build the frontend assets.
- **Resolution**:
  ```powershell
  cd frontend
  npm install
  npm run build
  cd ..
  ```
  Restart `querypilot-agent` after the build completes.

#### 2. Stale Profile Lockfile Recovery
- **Symptom**: Running `querypilot-agent profile add` fails after 5 seconds with:
  ```
  ERROR: Failed to save profile to '...': Timeout (5.0s) acquiring lock on '...profiles.json.lock'. Another profile operation may be in progress.
  ```
- **Root Cause**: A prior profile CLI invocation crashed or was forcibly terminated while holding the cross-process lock.
- **Safe Recovery Procedure**:
  1. Confirm that no other `querypilot-agent` process is actively running or writing profiles (check Task Manager or the PID written inside the `.lock` file).
  2. Only after verifying no profile-writing process is active, delete the stale lockfile:
     ```powershell
     Remove-Item "$HOME\.query_pilot\profiles.json.lock"
     ```
  3. Re-run `querypilot-agent profile add`.

#### 3. Port Conflicts (Address Already in Use)
- **Symptom**: `OSError: [Errno 10048] error while attempting to bind on address ('127.0.0.1', 8000)` or `8001`.
- **Root Cause**: Another instance or service is occupying port 8000 (API) or 8001 (Agent).
- **Resolution**:
  - For the agent: launch with `--port <PORT>` or set `$env:QUERYPILOT_AGENT_PORT = "<PORT>"`.
  - For the API: launch with `--port <PORT>` or set `$env:QUERYPILOT_API_PORT = "<PORT>"`. If changing the API port, update the agent's upstream URL: `--upstream-api-url "http://127.0.0.1:<PORT>"`.

#### 4. Missing or Invalid Gemini API Key
- **Symptom**: API startup fails or submitting questions returns a 500 error citing `RuntimeError: Application startup failed: unable to load or validate settings (ValidationError)` or `GEMINI_API_KEY not found in environment`.
- **Root Cause**: `GEMINI_API_KEY` is not set in the process environment running `querypilot-api`.
- **Resolution**:
  ```powershell
  $env:GEMINI_API_KEY = "your-actual-api-key"
  querypilot-api
  ```

#### 5. ODBC Driver Not Found
- **Symptom**: Introspection or query execution fails with `pyodbc.InterfaceError: ('IM002', '[IM002] [Microsoft][ODBC Driver Manager] Data source name not found and no default driver specified')`.
- **Root Cause**: Neither ODBC Driver 17 nor Driver 18 for SQL Server is installed on Windows.
- **Resolution**: Download and install Microsoft ODBC Driver 17 or 18 for SQL Server from Microsoft's download portal. Confirm installation using `Get-OdbcDriver -Name "*SQL Server*"`.

#### 6. SQL Server Login / Authorization Failure
- **Symptom**: Query execution fails with `OperationalError: 28000 [Microsoft][ODBC Driver 17 for SQL Server][SQL Server]Login failed for user '<DOMAIN>\<USER>'`.
- **Root Cause**: The active Windows user account lacks permission to connect to the SQL Server instance or target database.
- **Resolution**: In SQL Server Management Studio (SSMS), ensure the active Windows account is added under **Security -> Logins**, and map the login with `db_datareader` (or `SELECT`) permissions on the target database.

#### 7. No Profiles Configured on First Run
- **Behavior**: If `profiles.json` does not exist on initial agent startup, the agent starts cleanly with an empty profile store:
  ```
  No database profiles file found at '~/.query_pilot/profiles.json'. Starting agent with empty profile store. Register a profile using 'querypilot-agent profile add'.
  ```
  The UI displays an empty profile selector. Register a profile via the CLI to enable query execution.

---

## 5. Hosted API Configuration Inventory

Settings for the Hosted/Local API service are managed via `query_pilot.config.Settings`:

| Setting | CLI Argument / Flag | Environment Variable | Default | Required? | Purpose |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `GEMINI_API_KEY` | *N/A* | `GEMINI_API_KEY` | *None* | **Yes** | Google Gemini API key for LLM generation. |
| `GEMINI_MODEL` | *N/A* | `GEMINI_MODEL` | `"gemini-3.8-flash"` | No | LLM model identifier (configured default: `"gemini-3.8-flash"`; live milestone verification used `"gemini-3.5-flash-lite"`). |
| `LANGCHAIN_TRACING_V2`| *N/A* | `LANGCHAIN_TRACING_V2` | `False` | No | Enables LangSmith telemetry. |
| `LANGCHAIN_API_KEY` | *N/A* | `LANGCHAIN_API_KEY` | *None* | No | LangSmith API credential. |
| `LANGCHAIN_PROJECT` | *N/A* | `LANGCHAIN_PROJECT` | `"query-pilot"` | No | Project grouping name for LangSmith traces. |
| API Bind Host | `--host` | `QUERYPILOT_API_HOST` | `127.0.0.1` | No | Host socket address for API service. |
| API Bind Port | `--port` | `QUERYPILOT_API_PORT` | `8000` | No | TCP port for API service. |

---

## 6. Supported Launch Entry Points

QueryPilot provides both console script shortcuts and standard module invocations.

### Launching the Local Agent Daemon (Port 8001)

```powershell
# Using console script entry point (when package is installed)
querypilot-agent --host 127.0.0.1 --port 8001

# Or using direct Python module execution
python -m query_pilot.agent.cli --host 127.0.0.1 --port 8001

# With browser auto-launch on startup
python -m query_pilot.agent.cli --open-browser
```

### Launching the Hosted/Local API Service (Port 8000)

```powershell
# Using console script entry point (when package is installed)
querypilot-api --host 127.0.0.1 --port 8000

# Or using Uvicorn directly
uvicorn query_pilot.api.app:app --host 127.0.0.1 --port 8000

# Or using direct Python module execution
python -m query_pilot.api.cli --host 127.0.0.1 --port 8000
```

### Verifying Service Startup & Operational Health

Once both services are running, verify process liveness and connectivity from a PowerShell terminal:

```powershell
# 1. Verify upstream API liveness directly on port 8000
Invoke-RestMethod -Uri "http://127.0.0.1:8000/health" -Method GET
# Expected response: @{ status = "healthy"; ... }

# 2. Verify agent proxy routing to upstream API on port 8001
Invoke-RestMethod -Uri "http://127.0.0.1:8001/api/health" -Method GET
# Expected response: @{ status = "healthy"; ... }

# 3. Verify frontend UI availability
# Navigate to http://127.0.0.1:8001/app/ in your browser
```

---

## 7. Preserved Security Invariants

The configuration contract maintains all QueryPilot security controls:
1. **Loopback-Only Binding**: The agent rejects any non-loopback bind address (`0.0.0.0`, LAN IPs, public hostnames) and fails closed at startup.
2. **SSRF Defense**: Unencrypted HTTP upstream URLs (`upstream_api_url`) must point strictly to loopback (`127.0.0.1`, `localhost`, `::1`). Non-loopback upstreams must use `https://`.
3. **Single-Use Bootstrap Authentication**: Access to the agent web interface requires retrieving the ephemeral 32-byte bootstrap token from `<token_dir>/<token_filename>`, establishing a secure `qpt_session` cookie, and invalidating the token immediately.
4. **Credential & Data Boundaries**: SQL passwords and connection strings remain strictly local to the agent process and are never transmitted to the upstream API. Tabular query result rows are delivered exclusively to the loopback browser client and retained in browser memory.
5. **Hard Global Execution Ceilings**: Local query execution is strictly clamped to a 15-second statement timeout (`GLOBAL_MAX_TIMEOUT_SECONDS = 15`) and 100 returned rows (`GLOBAL_MAX_ROW_LIMIT = 100`). Profile configurations or query parameters attempting to exceed these bounds are clamped down.
6. **Payload Size Envelopes**: The agent limits incoming request bodies to 65,536 bytes (64 KB) by default with a 1 MiB hard ceiling. The hosted API enforces a 1 MiB streaming ceiling.
7. **Distinct Proxy vs. SQL Timeouts**: The agent's `upstream_timeout_seconds` (default 30.0s) governs HTTP reverse-proxy requests to the upstream generation API and is distinct from the 15-second SQL statement execution timeout.

---

## 8. Release Status & Evidence Alignment

- **Controlled Preview Only**: QueryPilot is currently authorized strictly as a Controlled Preview for local developer and invited evaluation environments. Public distribution (e.g. to PyPI) is **BLOCKED / NO-GO** until dependency-resolved clean install tests and multi-platform packaging pass.
- **Live Gemini Synthetic Verification (Milestone B12-F5D)**: Completed four bounded live Gemini requests (`gemini-3.5-flash-lite`, one attempt per request) using synthetic inputs: answerable read-only SQL generation, refusal of an unsupported request, clarification prompting for an ambiguous request, and metadata-only answer synthesis.
- **Controlled End-to-End Browser Journey (Milestone B12-F5E2)**: Verified the complete browser workflow against the dedicated `QueryPilot_TestDB`: live schema introspection and type normalization (`NVARCHAR(100)`), sensitive-column redaction (`password_hash` omitted), exactly one live Gemini candidate generation call, explicit human-in-the-loop authorization, and execution of a read-only query returning two records (`record_count: 2`).
- **Generation vs. Synthesis Execution Boundaries**: In Milestone B12-F5E2, zero database execution calls occurred during candidate SQL generation, and the browser UI workflow rendered a deterministic local answer directly from client memory without invoking Gemini for synthesis.
- **Dedicated SQL Server Integration Suite (Milestone B12-F5C)**: Reported 32 passing live integration tests against the dedicated test database.
- **Offline Test Reconciled Baseline (Milestone B12-F5G)**: The latest reconciled offline test suite execution reported **1,217 passed, 62 deselected, 5 warnings** (the 62 deselected tests are live integration tests excluded by the `-m 'not integration'` filter). This count represents milestone-specific evidence rather than a permanent guarantee.
- **CLI-Based Profile Management**: There is no browser-based profile onboarding wizard. All profile setup occurs via `querypilot-agent profile add`.

