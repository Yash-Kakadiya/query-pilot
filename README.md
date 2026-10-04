# QueryPilot 🧭

**QueryPilot** is an AI-powered autonomous data analyst that converts natural-language questions into SQL, safely validates queries, explores schemas, recovers from execution errors, and explains results using a stateful LangGraph workflow.

The core differentiator of QueryPilot is its **self-correcting, measurable agent workflow** backed by deterministic safety envelopes and strict database guardrails.

---

## Architecture Overview

* **Orchestration:** LangGraph stateful graph with bounded recovery loops
* **LLM Provider:** Google Gemini API
* **Database Engine:** Microsoft SQL Server (T-SQL dialect) with SQLAlchemy & ODBC
* **Deterministic Safety:** Abstract Syntax Tree (AST) query validation via `sqlglot`
* **Observability:** LangSmith tracing and run evaluation
* **Backend:** FastAPI (Python 3.12)
* **Frontend:** React + TypeScript

---

## Engineering Guardrails

1. **Safety First:** Read-only database access, zero unvalidated SQL execution, and deterministic AST verification before running queries.
2. **Separation of Concerns:** Distinct boundaries between schema introspection, query generation, safety validation, query execution, and result interpretation.
3. **Bounded Recovery:** Strictly limited correction attempts to avoid runaway execution loops.
4. **Data Privacy:** Sensitive columns (e.g., credentials, password hashes) are masked at the schema introspection layer and blocked at the AST validation layer.
5. **No Hallucinated Insights:** Grounded answers backed directly by execution results.

---

## Getting Started (Local Development)

### 1. Prerequisites
* Python 3.12+
* Microsoft SQL Server Express / LocalDB with ODBC Driver 17 for SQL Server
* Git

### 2. Setup Virtual Environment
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 3. Configure Environment Variables
```powershell
cp .env.example .env
# Edit .env and supply your GEMINI_API_KEY and database configuration
```

### 4. Run Test Suite
```powershell
pytest
```
