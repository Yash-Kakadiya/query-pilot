# Deploying QueryPilot (Render SQL-Only Prototype)

This guide details how to deploy a zero-cost, public **SQL-Only prototype** of QueryPilot on [Render](https://render.com) using a split-origin architecture:
- **Frontend**: Render Static Site (React SPA served globally via CDN).
- **Backend**: Render Web Service (Docker container running FastAPI and Uvicorn).

> [!NOTE]
> This deployment guide provides an architectural blueprint and configuration reference. This deployment configuration has **not been tested live** on Render infrastructure.

---

## Architecture Overview

```mermaid
flowchart LR
    User([Analyst / Browser])

    subgraph Render ["Render Cloud Platform"]
        Frontend["Render Static Site\n(React 19 SPA / Global CDN)\nhttps://<frontend-site>.onrender.com"]
        Backend["Render Web Service\n(Docker: Dockerfile.backend :8000)\nhttps://<backend-service>.onrender.com"]
    end

    Gemini["Google Gemini API\n(gemini-3.8-flash)"]

    User -->|"1. HTTPS /"| Frontend
    Frontend -->|"2. Cross-Origin API Calls\nPOST /query & GET /health"| Backend
    Backend -->|"3. Text-to-SQL Reasoning"| Gemini
    Backend -.->|"4. SQL-Only: Zero DB Calls\n(Uses in-memory GradeSense demo schema)"| DB[(No Database Connected)]
```

---

## Prerequisites

1. A [GitHub](https://github.com) account containing your fork or clone of the QueryPilot repository.
2. A [Render](https://render.com) account (the free tier does not require a credit card for initial setup).
3. A [Google Gemini API Key](https://aistudio.google.com/) for natural-language query reasoning.

---

## Step 1: Deploy Backend Web Service

Deploy the backend as a Docker Web Service on Render:

1. Log in to the [Render Dashboard](https://dashboard.render.com/) and click **New +** $\rightarrow$ **Web Service**.
2. Connect your GitHub repository.
3. Configure the service settings:
   - **Name**: `querypilot-backend` (or a name of your choice, yielding `https://<backend-service>.onrender.com`).
   - **Region**: Choose the region closest to you (e.g., `Oregon (US West)` or `Frankfurt (EU Central)`).
   - **Branch**: `main`.
   - **Runtime**: **Docker**.
   - **Dockerfile Path**: `Dockerfile.backend` (in repository root).
   - **Docker Context**: `.` (repository root).
   - **Instance Type**: **Free** (512 MB RAM, 0.1 vCPU).
4. Configure **Environment Variables**:
   | Variable | Value | Purpose |
   | :--- | :--- | :--- |
   | `GEMINI_API_KEY` | `AIzaSy...` (your secret key) | Required for Gemini model reasoning and SQL generation. |
   | `PORT` | `8000` | The backend container dynamically binds to `${PORT:-8000}`. Configuring `PORT=8000` (matching `EXPOSE 8000`) directs Render to route traffic to container port 8000. |
   | `CORS_ALLOWED_ORIGINS` | `https://<frontend-site>.onrender.com` | Whitelists your frontend domain for Cross-Origin Resource Sharing (CORS). *(Update with your exact frontend URL after Step 2).* |
5. Click **Create Web Service**. Wait for the Docker image to build and deploy.
6. Note your backend URL: `https://<backend-service>.onrender.com`.

---

## Step 2: Deploy Frontend Static Site

Deploy the React SPA as a Render Static Site:

1. In the Render Dashboard, click **New +** $\rightarrow$ **Static Site**.
2. Connect the same GitHub repository.
3. Configure the static site settings:
   - **Name**: `querypilot-frontend` (yielding `https://<frontend-site>.onrender.com`).
   - **Branch**: `main`.
   - **Root Directory**: `frontend`.
   - **Build Command**: `npm run build`.
   - **Publish Directory**: `dist`.
4. Configure **Build-Time Environment Variables**:
   | Variable | Value | Purpose |
   | :--- | :--- | :--- |
   | `VITE_API_BASE_URL` | `https://<backend-service>.onrender.com` | Base URL used by the frontend API client to reach the backend service directly. |
   | `VITE_DEFAULT_EXECUTION_MODE` | `sql_only` | Sets the initial execution mode selector to **SQL Only** on every fresh page load. |
5. Click **Create Static Site**. Render will run `npm install`, compile the Vite production bundle, and deploy it to their global CDN.
6. Copy your frontend URL: `https://<frontend-site>.onrender.com`.

---

## Step 3: Align Backend CORS Configuration

Now that you have your assigned frontend URL:

1. Return to the **querypilot-backend** service in the Render Dashboard.
2. Navigate to **Environment**.
3. Update `CORS_ALLOWED_ORIGINS` to match your exact frontend site URL:
   ```text
   CORS_ALLOWED_ORIGINS=https://<frontend-site>.onrender.com
   ```
   *(Or a JSON array: `["https://<frontend-site>.onrender.com"]`)*
4. Save changes. Render will automatically redeploy the backend service with the updated CORS policy.

---

## Operational Details & Constraints

### 1. Build-Time vs. Runtime Variables
- Variables prefixed with `VITE_*` (such as `VITE_API_BASE_URL` and `VITE_DEFAULT_EXECUTION_MODE`) are **baked into the client-side JavaScript bundle during the build step** (`npm run build`).
- Modifying a `VITE_*` variable in the Render Dashboard will **not** take effect on running clients until a **Manual Deploy $\rightarrow$ Clear build cache & deploy** is triggered.
- Conversely, backend variables like `GEMINI_API_KEY` and `CORS_ALLOWED_ORIGINS` are runtime variables read by Python on service startup.

### 2. Operational Health Check Semantics
- The `GET /health` endpoint ([src/query_pilot/api/app.py](file:///d:/01.%20Github/query-pilot/src/query_pilot/api/app.py)) is an **application process liveness check**.
- It verifies that the FastAPI server is running and ready to handle incoming HTTP requests.
- It **does not** test downstream Gemini API connectivity or SQL Server database availability. An "Online" status badge indicates the API worker is live, not that downstream external services are reachable.

### 3. Inactivity Sleep & Cold Starts
- Free tier Web Services on Render automatically spin down (sleep) after **15 minutes of inactivity** (no incoming HTTP traffic).
- When a user visits the frontend and sends the first query or liveness check, Render spins up the backend container.
- This "cold start" wake-up typically adds a **30 to 60 second delay** on the first request. Subsequent requests will execute with normal latency.

### 4. SQL-Only Mode Privacy & LLM Usage
- In **SQL-Only mode**, QueryPilot relies entirely on an in-memory representation of the 17 relational tables in the GradeSense demo schema ([src/query_pilot/db/demo_schema.py](file:///d:/01.%20Github/query-pilot/src/query_pilot/db/demo_schema.py)).
- **Zero database connections** are established, and zero queries are executed against a live database.
- **LLM Reasoning Still Occurs**: The user's question, bounded conversation context, and schema metadata are sent to the configured Google Gemini API (`gemini-3.8-flash`) to generate and explain the candidate SQL. Do not submit sensitive personal information in query text.

### 5. Manual Mode Override
- While `VITE_DEFAULT_EXECUTION_MODE=sql_only` initializes the interface to SQL-Only mode on fresh page loads, users can still manually switch the radio button to "Execute Query" if an external database has been configured.

### 6. Public Deployment Safety & Abuse Considerations
- **Gemini Context Transmission**: Although SQL-Only mode eliminates live database access, questions, conversation history, and schema definitions are transmitted to Google Gemini (`gemini-3.8-flash`). Sensitive PII must never be submitted.
- **CORS Is Not Authentication**: CORS configuration only restricts web browsers from invoking the API across origins. It provides **zero protection** against direct non-browser HTTP requests (such as `curl`, Postman, or automated scripts).
- **No Incoming User Authentication**: The backend API currently exposes no authentication, user session tokens, or API keys on the `/query` endpoint. Anyone who obtains or inspects the backend service URL can send requests directly.
- **API Quota & Denial of Wallet/Service**: Unrestricted public access can be abused to consume your configured Gemini API quota, trigger rate limits (HTTP 429), or cause Render bandwidth/pipeline exhaustion.
- **Render Free-Tier Account Risks**: If inbound/outbound limits (such as Render's 5 GB monthly bandwidth) are exceeded, services may be suspended or incur charges depending on workspace billing settings.
- **Recommended Next Step**: Before exposing this prototype beyond trusted evaluators, implement lightweight incoming bearer token authentication or API key header validation middleware on the FastAPI backend.
