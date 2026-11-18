# 🚀 QueryPilot v0.2.0 — Autonomous Desktop AI Data Analyst

Welcome to **QueryPilot v0.2.0**! This milestone release brings QueryPilot to your Windows desktop as a lightweight, secure, and privacy-first application powered by **Tauri v2** and an isolated Python database engine.

---

## 📦 Download & Installation

| Asset | Platform | Description |
| :--- | :--- | :--- |
| **`QueryPilot-v0.2.0-windows-x64.zip`** | Windows 10/11 (64-bit) | **Recommended**: Portable standalone release bundle (No installation required). Includes `QueryPilot.exe`, supervised Python sidecar, sample `university_demo.db`, and docs. |
| **`SHA256SUMS.txt`** | All | Cryptographic SHA-256 verification checksums for release integrity. |

### Quick Start (Under 1 Minute)
1. Download **`QueryPilot-v0.2.0-windows-x64.zip`** below.
2. Extract the archive to any folder (e.g. `C:\Users\YourName\QueryPilot`).
3. Double-click **`QueryPilot.exe`** to launch.
4. Click **+ Add Profile** -> **Select SQLite File** and choose the included **`university_demo.db`** (or connect to AWS RDS, Azure SQL, GCP Cloud SQL, or MonsterASP).
5. Paste your free Google Gemini API key into the top header (or click **Get Free Gemini 🗝️**).
6. Start asking questions in plain English or write SQL!

---

## ✨ What's New in v0.2.0

### 🖥️ Native Windows Desktop Application
- Built with **Tauri v2** and **WebView2** for lightning-fast performance, low memory footprint, and instant window rendering.
- Supervised Python agent sidecar daemon running under strict **Win32 Job Object isolation** (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`) ensuring zero orphaned background processes when closing the app.

### ☁️ Multi-Cloud Database Profile Support
- Seamless connection profiles for:
  - **SQLite**: Local file databases with native Windows file picker.
  - **AWS RDS**: Microsoft SQL Server on Amazon Web Services.
  - **Google Cloud SQL**: SQL Server instances on Google Cloud Platform.
  - **Azure SQL**: Managed Microsoft Azure SQL Database endpoints.
  - **MonsterASP & Other Cloud**: Custom hostnames, ports, and SQL Server authentication.
- In-app **"📋 How to Fill Form Fields"** setup guide with platform-specific instructions.

### 🔒 100% On-Premise Privacy Guarantee
- Database connection strings, passwords, and sensitive schema tables **never touch the cloud**.
- All SQL execution occurs locally on loopback (`127.0.0.1`) with fail-closed AST validation intercepting mutating operations (`DROP`, `DELETE`, `UPDATE`, `INSERT`).

### 💎 Enterprise User Experience
- **1-Click Export**: Instant **Copy CSV** and **Download CSV** buttons on all query results.
- **Sticky Column Headers**: Freeze headers during long scrolling tables with numeric right-alignment.
- **Starter Query Chips**: One-click exploratory query suggestions for immediate analysis.
- **Keyboard Shortcuts**: Press <kbd>Ctrl</kbd>+<kbd>Enter</kbd> (or <kbd>Cmd</kbd>+<kbd>Enter</kbd>) to instantly run queries.
- **Interactive Visualizations**: Dynamic bar, line, and scatter charts powered by Recharts.

### 📊 Audited AI Evaluation Benchmark (Pillar 4)
- 28 canonical evaluation scenarios tested against `university_demo.db`:
  - **Execution Accuracy (EX)**: **`100.0%`**
  - **Valid SQL Rate (VSR)**: **`100.0%`**
  - **Schema Grounding Score (SGS)**: **`100.0%`**
  - **Safety Rejection Rate**: **`100.0%`**
  - **Median Execution Latency**: **`3.22 ms`**

---

## 🛡️ Integrity & Verification

Verify downloaded archive checksum via PowerShell:
```powershell
Get-FileHash QueryPilot-v0.2.0-windows-x64.zip -Algorithm SHA256
```
Compare the output against the hash published in `SHA256SUMS.txt`.

---

## 👥 Feedback & Contributions
- GitHub Repository: [Yash-Kakadiya/query-pilot](https://github.com/Yash-Kakadiya/query-pilot)
- Report an Issue: [GitHub Issues](https://github.com/Yash-Kakadiya/query-pilot/issues)
