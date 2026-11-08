# QueryPilot Desktop Python Sidecar Packaging Guide

This guide details the packaging, runtime architecture, verification procedures, and database driver requirements for the standalone QueryPilot Python local agent sidecar (`querypilot-agent.exe`), targeted for Windows x64.

---

## 1. Overview

QueryPilot Desktop uses a Tauri 2 host that supervises a frozen Python FastAPI daemon as a child sidecar process. To ensure a zero-friction desktop experience, the sidecar is packaged as a standalone Windows directory distribution (`--onedir`) using PyInstaller:

- **No Python runtime required**: Runs completely independently without requiring a system Python installation or virtual environment.
- **Embedded Frontend UI**: Statically serves the pre-compiled React 19 SPA from `/app/` directly out of the bundled `_internal/query_pilot/agent/static` assets.
- **Dual Database Dialect Support**:
  - **SQLite**: Built-in native support with C `sqlite3.dll` bundled for immediate out-of-the-box analytical querying.
  - **Microsoft SQL Server**: Bundled native C extension `pyodbc` and SQLAlchemy MSSQL dialect, utilizing Windows Integrated Authentication (`trusted_connection=True`).
- **Desktop Ephemeral Binding & Supervision**: Supports `--desktop` mode, binding dynamically to loopback port `0`, emitting a single machine-readable `QUERYPILOT_READY:{"status": "ready", "port": <port>, "pid": <pid>}` JSON handshake to `stdout`, and routing all diagnostic logging to `stderr`.
- **Hardened Security Middleware**: Enforces DNS rebinding protection (Host header allowlisting), Origin validation, state-changing CSRF token header (`X-QueryPilot-Request: 1`), single-use bootstrap token authentication, and strict read-only AST/authorizer query validation.

---

## 2. Packaging Prerequisites

To build the sidecar, the following development prerequisites must be met:

1. **Python 3.12 64-bit** with project virtual environment activated (`.venv`).
2. **PyInstaller** (`>=6.10`):
   ```bash
   pip install pyinstaller
   ```
3. **Compiled Frontend Build**: The React production build must exist at `frontend/dist/index.html`. If not present, compile it via:
   ```bash
   cd frontend
   npm run build
   cd ..
   ```

---

## 3. Packaging Procedure

The packaging process is automated by `scripts/package_sidecar.py` which wraps `querypilot-agent.spec`.

### Command

```powershell
python scripts/package_sidecar.py
```

### Build Artifact Layout

Upon completion, PyInstaller creates the distribution in `dist/querypilot-agent/`:

```
dist/
└── querypilot-agent/
    ├── querypilot-agent.exe             # Main executable binary (~18.8 MB)
    └── _internal/                       # Dependent binaries, shared libraries, and assets
        ├── python312.dll                # Python core runtime DLL
        ├── sqlite3.dll                  # Native SQLite C library
        ├── _sqlite3.pyd                 # Python SQLite extension module
        ├── pyodbc.cp312-win_amd64.pyd   # Native ODBC driver interface for SQL Server
        ├── certifi/cacert.pem           # Root CA certificates bundle
        └── query_pilot/
            └── agent/
                └── static/              # Embedded React frontend SPA assets
                    ├── index.html
                    ├── vite.svg
                    └── assets/
                        ├── index-*.js
                        └── index-*.css
```

### Bundle Size Pruning

To keep the frozen sidecar lightweight and fast to launch, unnecessary packages and testing frameworks are explicitly excluded in `querypilot-agent.spec`:
- `tkinter`, `matplotlib`, `scipy`, `pandas`
- `IPython`, `notebook`
- `pytest`, `tests`

---

## 4. Tauri 2 Sidecar Location & Process Supervision

In the desktop application architecture, Tauri manages the sidecar lifecycle:

1. **Location**: Tauri identifies the path to `querypilot-agent.exe` relative to the application resource directory:
   - Development: `dist/querypilot-agent/querypilot-agent.exe`
   - Production / Installed: `<resource_dir>/bin/querypilot-agent/querypilot-agent.exe`
2. **Process Creation**:
   - The Tauri host invokes `CreateProcessW` with `CREATE_SUSPENDED` flag.
   - Assigns the process to a Win32 Job Object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`.
   - Resumes the primary thread via `ResumeThread`.
   - Guarantees zero orphaned daemon processes when the Tauri window is closed or crashes.
3. **Execution Flags**:
   ```powershell
   querypilot-agent.exe --desktop --token-dir "<local_app_data>/QueryPilot" --profiles-file "<local_app_data>/QueryPilot/profiles.json"
   ```
4. **Handshake & Webview Redirection**:
   - The Tauri host reads `stdout` line-by-line until receiving:
     ```
     QUERYPILOT_READY:{"status": "ready", "port": <bound_port>, "pid": <assigned_pid>}
     ```
   - All HTTP access logs and diagnostic logs are emitted to `stderr`.
   - The desktop webview navigates to `http://127.0.0.1:<bound_port>/app/` and bootstraps authentication using the provisioned single-use token in `<local_app_data>/QueryPilot/agent.token`.

---

## 5. Database Prerequisites & Error Handling

### SQLite
SQLite works out of the box with zero external installation. Introspection and read-only execution are strictly governed by QueryPilot's SQLite authorizer hook, blocking all DDL, DML, ATTACH, and PRAGMA mutations.

### Microsoft SQL Server & ODBC Driver
The sidecar includes `pyodbc` and SQLAlchemy MSSQL dialects. However, connecting to SQL Server requires a system-level ODBC driver.

- **Prerequisite**: The user or machine must have **Microsoft ODBC Driver 18 (or 17) for SQL Server** installed.
- **Graceful Failure**: If the ODBC driver is missing when connecting to or testing a SQL Server profile, QueryPilot intercepts the ODBC `IM002` error ("Data source name not found and no default driver specified") and emits a clear, actionable error:
  ```
  ODBC DRIVER MISSING: Microsoft ODBC Driver 18 for SQL Server is not installed.
  Please download and install the official Microsoft ODBC Driver from:
  https://learn.microsoft.com/en-us/sql/connect/odbc/download-odbc-driver-for-sql-server
  ```
  The sidecar binary itself will **never crash** during startup or import if the driver is absent.

---

## 6. Verification and Automated Validation Suite

An end-to-end automated verification harness is provided in `scripts/test_frozen_sidecar.py`. It tests the packaged sidecar binary without using `python` or virtual environment dependencies.

### Running Validation

```powershell
python scripts/test_frozen_sidecar.py
```

### Validated Test Cases

1. **CLI Help**: Verifies `querypilot-agent.exe --help` displays clean usage and exits 0.
2. **Profile Registration**: Registers an isolated SQLite profile and verifies JSON store persistence.
3. **Desktop Mode Launch**: Starts `querypilot-agent.exe --desktop`, captures ephemeral port and PID handshake from `stdout`.
4. **Embedded Static Asset Serving**: Requests `GET /app/` on the ephemeral port and verifies HTML/SPA contents.
5. **Authenticated Analytical Query**: Exchanges `agent.token` via POST `/agent/v1/auth/bootstrap`, introspects schema, executes analytical aggregation query on `university_demo.db`, and verifies byte-for-byte database immutability.
6. **Mutation Query Rejection**: Attempts `DROP TABLE students` and verifies strict HTTP 400 rejection by the read-only authorizer.
7. **Clean Socket Release**: Gracefully terminates sidecar and verifies immediate release of the OS loopback socket.

