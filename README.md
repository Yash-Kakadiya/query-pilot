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

### 4. Start the API Server
Launch the local FastAPI service using Uvicorn bound to localhost:
```powershell
uvicorn query_pilot.api.app:app --host 127.0.0.1 --port 8000
```
Once started, the API is accessible at `http://127.0.0.1:8000`.
* Interactive OpenAPI documentation (Swagger UI): `http://127.0.0.1:8000/docs`
* Alternative documentation (ReDoc): `http://127.0.0.1:8000/redoc`

For detailed endpoint contracts, request/response schemas, error handling, request ID correlation, and curl examples, see the [API Guide](docs/api.md).

### 5. Run Test Suite
Run the test suites using the project virtual environment:

* **API Integration & Model Tests:**
  ```powershell
  pytest tests/test_api.py tests/test_api_models.py -v
  ```
* **Full Offline Suite (live database and external API calls excluded):**
  ```powershell
  pytest -k "not live" --tb=short
  ```
* **Full Test Suite:**
  ```powershell
  pytest
  ```

---

## Evaluation Benchmarks

QueryPilot includes an offline baseline evaluation harness (`data/evaluation/baseline_questions.json`) comprising **34 curated cases**: the original 32 benchmark questions plus two clarification cases (`C001` and `C002`).

* **Capability**: Clarification-aware evaluation with bounded conversation-context forwarding. It verifies that query generation correctly requests clarification when questions are ambiguous and respects caller-supplied prior-turn intent.
* **Scope Boundary**: This evaluates single-request generation boundaries with forwarded turn context; it does not provide an end-to-end interactive, stateful multi-turn dialogue evaluation loop.
* **Historical Compatibility**: Historical 32-case evaluation artifacts remain preserved. Metrics from 32-case and 34-case datasets should not be compared directly without acknowledging the dataset difference.

For complete benchmark schemas and category distributions, see [`data/evaluation/README.md`](data/evaluation/README.md).
