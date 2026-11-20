# 🚀 QueryPilot v0.3.0 — PostgreSQL, MySQL & Actionable Diagnostics

Welcome to **QueryPilot v0.3.0**! This major release significantly expands QueryPilot's database ecosystem with full native support for **PostgreSQL** and **MySQL**, introduces a **self-healing actionable error diagnostics engine** with plain-English solutions, and delivers verified Windows installers and portable distributions.

---

## 📦 Download & Installation

| Asset | Platform | Description |
| :--- | :--- | :--- |
| 🪟 **`QueryPilot-v0.3.0-Setup.exe`** | Windows 10/11 (64-bit) | **Recommended (Installer)**: Standalone Windows installer wizard with desktop and Start Menu shortcuts, bundled sidecar engine, and demo database. |
| 📦 **`QueryPilot-v0.3.0-windows-x64.zip`** | Windows 10/11 (64-bit) | **Portable**: Standalone portable zip archive (No installation or admin rights required). Extract and double-click `QueryPilot.exe`! |
| 🔒 **`SHA256SUMS.txt`** | All | Cryptographic SHA-256 verification checksums for release integrity. |

### Quick Start (Under 1 Minute)
1. **Using the Installer**: Download and run **`QueryPilot-v0.3.0-Setup.exe`**, then launch from your Desktop or Start Menu.
2. **Using the Portable Zip**: Download **`QueryPilot-v0.3.0-windows-x64.zip`**, extract to any folder, and double-click **`QueryPilot.exe`**.
3. **Connect a Database**: Click **+ Add Profile** and select:
   - **SQLite**: Local file database (bundles `university_demo.db`).
   - 🐘 **PostgreSQL**: Local or Cloud (Supabase, Neon, AWS RDS, GCP Cloud SQL).
   - 🐬 **MySQL**: Local or Cloud (PlanetScale, AWS RDS, GCP Cloud SQL).
   - 🏢 **Microsoft SQL Server**: AWS RDS, Azure SQL, MonsterASP, or On-Premise.
4. **Configure Gemini API Key**: Paste your free Google Gemini API key into the top header (or click **Get Free Gemini 🗝️**). Keys are saved strictly in your private local browser storage and never touch the cloud.
5. Start asking questions in plain English or write SQL!

---

## ✨ What's New in v0.3.0

### 🐘 Native PostgreSQL Engine Support
- Connect seamlessly to local and cloud-hosted PostgreSQL instances:
  - **Local PostgreSQL** (`localhost:5432`).
  - **Supabase**, **Neon Serverless Postgres**, **AWS RDS PostgreSQL**, **Google Cloud SQL for PostgreSQL**.
- Full information schema introspection: introspects tables, views, columns, types, primary keys, and foreign keys.
- **Fail-Closed Read-Only Execution**: Enforces `SET default_transaction_read_only = ON;` on every connection to physically block mutations even if AST validation is bypassed.

### 🐬 Native MySQL Engine Support
- Connect directly to local and cloud-hosted MySQL databases:
  - **Local MySQL** (`localhost:3306`).
  - **PlanetScale**, **AWS RDS MySQL**, **Google Cloud SQL for MySQL**.
- Introspects MySQL schema definitions, table catalogs, indexes, and relationship constraints.
- **Session-Level Read-Only Boundaries**: Enforces `SET SESSION TRANSACTION READ ONLY;` for all query operations.

### 🛡️ Self-Healing Actionable Error Diagnostics Engine
- No more cryptic stack traces! When an error occurs, QueryPilot classifies the root cause and renders an intuitive, plain-English card with:
  - 🔍 **Root Cause Explanation**: Plain-English diagnosis of why the operation failed.
  - 💡 **Actionable Solution**: Clear, bulleted steps to fix the issue.
  - ⚡ **1-Click Quick Action**: Contextual buttons like **Open Key Settings**, **Edit Profile**, or **Refresh Schema**.
- Built-in diagnostic patterns:
  - `INVALID_API_KEY`: API key invalid, revoked, or formatted incorrectly.
  - `MODEL_UNAVAILABLE_OR_QUOTA`: Rate limits or model quota exceeded with retry countdown.
  - `DB_CONNECTION_REFUSED`: Host, port, or firewall blocked with network troubleshooting tips.
  - `DB_AUTH_FAILED`: Username or password incorrect.
  - `DB_SSL_REQUIRED`: Cloud providers requiring TLS/SSL mode (Supabase, Neon, AWS RDS).
  - `SCHEMA_NOT_FOUND`: Database catalog, schema, or search path mismatch.
- **Zero Credential Exposure**: Sensitive passwords, keys, and connection tokens are rigorously redacted (`[REDACTED]`) before rendering or logging.

### 🎨 Revamped Multi-Database Onboarding UI
- Clean database selector with engine-specific badges (SQLite, PostgreSQL, MySQL, SQL Server).
- Default port auto-population (5432 for Postgres, 3306 for MySQL, 1433 for MSSQL).
- Inline SSL mode toggles (`prefer`, `require`, `disable`) for effortless cloud database setup.
- Interactive connection test button with immediate diagnostic feedback before saving profiles.

---

## 🛡️ Integrity & Verification

Verify downloaded archive checksum via PowerShell:
```powershell
Get-FileHash QueryPilot-v0.3.0-windows-x64.zip -Algorithm SHA256
Get-FileHash QueryPilot-v0.3.0-Setup.exe -Algorithm SHA256
```
Compare the output against the hash published in `SHA256SUMS.txt`.

---

## 👥 Feedback & Contributions
- GitHub Repository: [Yash-Kakadiya/query-pilot](https://github.com/Yash-Kakadiya/query-pilot)
- Report an Issue: [GitHub Issues](https://github.com/Yash-Kakadiya/query-pilot/issues)
