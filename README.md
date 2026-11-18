<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/querypilot-banner.png">
    <img src="docs/assets/querypilot-banner.png" alt="QueryPilot Logo" width="760" />
  </picture>
</p>

<p align="center">
  <strong>Autonomous, On-Premise AI Data Analyst &amp; Natural Language SQL Copilot for Windows</strong><br />
  <em>100% Private Local Database Execution &bull; Multi-Cloud Connectivity &bull; Fail-Closed Safety Boundaries</em>
</p>

<p align="center">
  <a href="https://github.com/Yash-Kakadiya/query-pilot/releases"><img src="https://img.shields.io/github/v/release/Yash-Kakadiya/query-pilot?color=22c55e&label=Desktop%20Release" alt="Desktop Release" /></a>
  <a href="https://github.com/Yash-Kakadiya/query-pilot/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/Yash-Kakadiya/query-pilot/ci.yml?branch=main&label=CI%20Pipeline&color=brightgreen" alt="CI Status" /></a>
  <a href="data/evaluation/BENCHMARK_REPORT.md"><img src="https://img.shields.io/badge/AI%20Benchmark%20Accuracy-100%25%20EX-blueviolet.svg" alt="Benchmark" /></a>
  <a href="#test-coverage--verification"><img src="https://img.shields.io/badge/Tests-2%2C461%20Passing-brightgreen.svg" alt="Tests" /></a>
  <a href="src-tauri/"><img src="https://img.shields.io/badge/Desktop-Tauri%20v2%20%2B%20WebView2-blue.svg" alt="Desktop" /></a>
  <a href="frontend/"><img src="https://img.shields.io/badge/Frontend-React%2019%20%2B%20TypeScript-61dafb.svg" alt="Frontend" /></a>
  <a href="src/query_pilot/"><img src="https://img.shields.io/badge/Backend-Python%203.12%2B%20FastAPI-009688.svg" alt="Backend" /></a>
</p>

---

## ⬇️ Download QueryPilot for Windows (v0.2.0)

> [!TIP]
> **No installation or admin rights required!** QueryPilot is packaged as a standalone portable Windows application.

👉 **[Download QueryPilot v0.2.0 Portable (.zip)](https://github.com/Yash-Kakadiya/query-pilot/releases)**

1. Download **`QueryPilot-v0.2.0-windows-x64.zip`** from the [Releases page](https://github.com/Yash-Kakadiya/query-pilot/releases).
2. Extract the archive to any folder on your computer.
3. Double-click **`QueryPilot.exe`** to launch!
4. Select the included **`university_demo.db`** (or connect your cloud database) and start asking questions!

---

## 💡 Why QueryPilot?

Traditional web-based text-to-SQL tools require uploading database schemas, connection strings, and sensitive credentials to external multi-tenant cloud servers. 

**QueryPilot is built fundamentally different:**
- 🔒 **100% Local Privacy**: Your database credentials, connection strings, and introspected schemas **never leave your machine**.
- 🛡️ **Guarded Read-Only Execution**: Candidate SQL queries must be reviewed by the user. Destructive mutations (`DROP`, `DELETE`, `UPDATE`, `INSERT`) are deterministically blocked by AST inspection and physical connection authorizers.
- ⚡ **Zero-Latency Local Exploration**: Query execution runs directly over local loopback (`127.0.0.1`), delivering query response times in under **4 milliseconds**.

---

## ✨ Key Features

| Feature | Description |
| :--- | :--- |
| ☁️ **Multi-Cloud Database Profiles** | Connect seamlessly to local **SQLite** files, **AWS RDS**, **Google Cloud SQL**, **Azure SQL**, **MonsterASP**, or on-premise **Microsoft SQL Server** with SQL or Windows Authentication. |
| 🔑 **In-App Gemini Key Management** | Paste your free Google Gemini API key directly into the UI header (saved securely in your local browser storage) or use environment fallback keys. |
| 🛡️ **Fail-Closed Safety Engine** | Advanced AST parsing via SQLGlot blocks DDL/DML mutations, multi-statement injection, and hallucinated schema tables/columns before queries reach the database. |
| 📊 **Enterprise Data UX** | **1-Click Copy CSV**, **1-Click Download CSV**, sticky table headers, numeric right-alignment, starter exploration chips, and <kbd>Ctrl</kbd>+<kbd>Enter</kbd> execution. |
| 📈 **Interactive Visualizations** | Dynamic bar charts, line graphs, and scatter plots powered by Recharts rendered client-side directly from local query results. |
| 🎓 **Pre-Packaged Demo Database** | Comes bundled with a synthetic university database (departments, courses, students, enrollments) so you can test immediately with zero database setup. |
| 🎯 **Audited AI Benchmark Suite** | Verified across 28 canonical evaluation scenarios with **100.0% Execution Accuracy (EX)** and **100.0% Valid SQL Rate (VSR)**. |

---

## 🏗️ Architecture Overview

QueryPilot operates as a desktop-native application combining a lightweight **Tauri v2** shell with an isolated **Python daemon sidecar**:

```mermaid
flowchart TD
    subgraph Desktop_App ["QueryPilot Desktop Application (Windows)"]
        UI["Tauri v2 Desktop Window (WebView2)\n- React 19 + TypeScript\n- Centered Enterprise Header\n- 1-Click CSV Exports & Charts"]
        
        JobObj["Win32 Job Object Supervision\n(JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)"]
        
        Sidecar["Local Python Agent Sidecar (:8001 loopback)\n- FastAPI Server\n- Profile Manager (~/.query_pilot/profiles.json)\n- Dynamic Schema Introspector"]
        
        Validator["SQLValidator (AST Inspection)\n- Fail-Closed Read-Only Gate\n- Schema Grounding Verification"]
        
        Executor["Guarded Query Executor\n- SQLite PRAGMA query_only=ON\n- ODBC Connection Guard\n- Row Clamp (<= 100 rows)"]
        
        UI <-->|"Loopback HTTP /app/"| Sidecar
        JobObj -->|"Supervises PID & Lifecycle"| Sidecar
        Sidecar --> Validator
        Validator --> Executor
    end
    
    subgraph Upstream_AI ["AI Natural Language Translation"]
        Gemini["Google Gemini API (gemini-2.5-flash)\n- Natural Language -> Candidate SQL\n- Upstream synthesis\n- ZERO raw database rows sent"]
        Sidecar -.->|"Encrypted HTTPS Request"| Gemini
    end

    subgraph Target_Databases ["Connected Databases"]
        Executor -->|"Local Read-Only"| SQLite[("Local SQLite Database\n(university_demo.db)")]
        Executor -->|"Encrypted SQL Auth"| CloudDB[("Multi-Cloud Databases\nAWS RDS / Azure / GCP / MonsterASP")]
    end
```

---

## 🚀 Quickstart for End Users

### 1. Launch the Desktop App
Double-click `QueryPilot.exe`. The application automatically launches its local background sidecar and opens the desktop interface.

### 2. Connect a Database
- **SQLite**: Click **+ Add Profile** -> **Select SQLite File** -> choose `university_demo.db`.
- **Cloud Database**: Click **☁️ Cloud Database**, pick your provider (**AWS RDS**, **Azure SQL**, **Google Cloud SQL**, or **MonsterASP**), fill in your host and credentials, and click **Save Profile**.

### 3. Add Gemini API Key
Click **Show** on the API key card in the top header, paste your free Google Gemini API key, and click **Save Key**. *(Need a key? Click the **Get Free Gemini 🗝️** button in the app).*

### 4. Ask Questions!
Ask questions in plain English:
- *"Show all courses offering 4 credits ordered by course ID"*
- *"What is the average marks scored across all enrollments?"*
- *"Which courses currently have zero student enrollments?"*
- *"Identify the top 3 students with the highest average marks"*

Click **Run Query**, view the results in the table or interactive chart, and export with **Copy CSV** or **Download CSV**!

---

## 🛠️ Developer Quickstart & Contributing

### Prerequisites
- **Python 3.12+**
- **Node.js 24+** (LTS) & npm
- **Rust & Cargo** (for building the Tauri desktop shell)
- Windows 10/11 x64

### 1. Clone the Repository
```powershell
git clone https://github.com/Yash-Kakadiya/query-pilot.git
cd query-pilot
```

### 2. Set Up Python Virtual Environment
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip setuptools wheel
python -m pip install -e ".[test]"
```

### 3. Install Frontend Dependencies & Build
```powershell
npm ci --prefix frontend
npm run build --prefix frontend
```

### 4. Run in Development Mode
Launch both the Python backend and Vite frontend development server concurrently:
```powershell
powershell -ExecutionPolicy Bypass -File scripts\start_stack.ps1 -Dev
```
The app will open at `http://localhost:5173/app/`.

To shut down services cleanly:
```powershell
powershell -ExecutionPolicy Bypass -File scripts\stop_stack.ps1
```

---

## 🧪 Test Coverage & Verification

QueryPilot maintains a rigorous automated testing baseline across Python and Node.js:

```powershell
# Run all Python backend unit & integration tests (1,933 tests)
.venv\Scripts\pytest -o pythonpath="src ." -m "not integration"

# Run all Frontend tests (528 tests)
npm test --prefix frontend

# Run the Desktop Release Packaging test suite
.venv\Scripts\pytest tests/test_desktop_release_packaging.py -v

# Run the Desktop Tauri compilation check
cargo check --manifest-path src-tauri/Cargo.toml
```

| Subsystem | Test Framework | Passing Tests | Failures |
| :--- | :--- | :---: | :---: |
| **Python Backend** | `pytest 9.1` | **1,933** | 0 |
| **Frontend UI** | Node.js Test Runner | **528** | 0 |
| **Combined Baseline** | Full Suite | **2,461** | **0** |

---

## 📊 AI Evaluation Benchmark Scorecard (Pillar 4)

QueryPilot includes a reproducible benchmark runner ([run_evaluation_benchmark.py](scripts/run_evaluation_benchmark.py)) evaluating 28 canonical text-to-SQL scenarios against `university_demo.db`:

```powershell
.venv\Scripts\python scripts/run_evaluation_benchmark.py --mode offline
```

| Key Performance Indicator (KPI) | Measured Score | Target Benchmark | Audit Status |
| :--- | :---: | :---: | :---: |
| **Execution Accuracy (EX)** | **`100.0%`** | $\ge 90.0\%$ | ✅ PASSED |
| **Valid SQL Rate (VSR)** | **`100.0%`** | $100.0\%$ | ✅ PASSED |
| **Schema Grounding Score (SGS)** | **`100.0%`** | $100.0\%$ | ✅ PASSED |
| **Safety Mutation Rejection** | **`100.0%`** | $100.0\%$ | ✅ PASSED |
| **Unsupported & Clarification Gating** | **`100.0%`** | $100.0\%$ | ✅ PASSED |
| **Median Execution Latency ($p_{50}$)** | **`3.22 ms`** | $< 25.0\text{ ms}$ | ✅ EXCELLENT |
| **Tail Latency ($p_{95}$)** | **`3.97 ms`** | $< 50.0\text{ ms}$ | ✅ EXCELLENT |

*See full audit details in [BENCHMARK_REPORT.md](data/evaluation/BENCHMARK_REPORT.md).*

---

## 📦 Packaging Desktop Releases

To build a standalone portable release archive locally:
```powershell
.venv\Scripts\python scripts/package_desktop_release.py --version 0.2.0 --rebuild
```
This produces `release/QueryPilot-v0.2.0-windows-x64.zip` (46.3 MB) and generates cryptographic hashes in `release/SHA256SUMS.txt`.

### Automated GitHub Release Workflow
Releases are automatically built and published to GitHub Releases via GitHub Actions whenever a version tag is pushed:
```powershell
git tag v0.2.0
git push origin v0.2.0
```

---

## 📁 Repository Structure

```
query-pilot/
├── .github/workflows/         # CI/CD Workflows (Desktop CI & Release CD)
│   ├── ci.yml                 # Backend, frontend, benchmark & Tauri check
│   └── desktop-release.yml    # Automated Windows desktop release packaging
├── data/
│   ├── demo/                  # Packaged SQLite database (university_demo.db)
│   └── evaluation/            # Canonical AI benchmark dataset & reports
├── frontend/                  # React 19 + TypeScript single-page application
│   ├── src/components/        # UI components (Header, ReviewCard, Tables, Modals)
│   └── tests/                 # 528 automated frontend tests
├── src/query_pilot/           # Python backend core
│   ├── agent/                 # Local daemon, loopback auth & CLI
│   ├── db/                    # Multi-cloud profile managers, connection pools
│   ├── eval/                  # Semantic result comparison & benchmark runner
│   └── sql/                   # AST validator, prompt templates & Gemini generator
├── src-tauri/                 # Tauri v2 Windows desktop shell (Rust)
│   └── src/process_manager.rs # Win32 Job Object sidecar supervision
├── scripts/                   # Build, release packaging & evaluation scripts
├── tests/                     # 1,933 pytest backend and integration tests
└── README.md                  # Project overview & documentation
```

---

## 📄 License & Security

- **License**: MIT License. See [LICENSE](LICENSE) for details.
- **Privacy Notice**: QueryPilot never transmits database credentials, passwords, or connection strings externally. All database execution occurs strictly within local read-only connection boundaries.
- **Author & Maintainer**: Yash Kakadiya ([@Yash-Kakadiya](https://github.com/Yash-Kakadiya))
