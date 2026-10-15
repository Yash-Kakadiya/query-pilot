# QueryPilot Frontend 🧭

The QueryPilot frontend is a modern, single-page React workspace designed for natural-language database exploration, interactive query clarification, and safe presentation of grounded SQL analysis results.

---

## Technology Stack & Prerequisites

* **Framework:** React `19.2.8` + React DOM `19.2.8`
* **Language:** TypeScript `~6.0.2` (Strict Type Checking)
* **Build Tool:** Vite `8.3.0`
* **Linter:** Oxlint `1.81.0`
* **Node.js Requirement:** `^20.19.0 || >=22.12.0` (LTS Node 20.19+ or Node 22.12+)
* **Package Manager:** npm `>=10.0.0`

---

## Local Development & Setup

### 1. Install Dependencies

From the `frontend/` directory:

```bash
npm install
```

### 2. Start Development Server

```bash
npm run dev
```

The Vite development server runs at `http://127.0.0.1:5173` (or `http://localhost:5173`).

### 3. API Proxy & Base URL Configuration

The frontend communicates with the FastAPI backend through dynamic endpoint resolution in `src/services/apiClient.ts`:

* **Default (Local Development via Vite Proxy):**
  When `VITE_API_BASE_URL` is unset or empty, the frontend defaults to `/api`. Vite's development proxy ([vite.config.ts](vite.config.ts)) intercepts these requests and transparently forwards them to `http://127.0.0.1:8000`:
  ```typescript
  server: {
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
    },
  }
  ```
* **Split-Origin Production Deployment:**
  When deploying the frontend to a separate origin from the backend (e.g. static CDN/hosting), configure `VITE_API_BASE_URL` in your frontend build environment:
  ```bash
  # Example: direct backend host (trailing slash is automatically normalized)
  VITE_API_BASE_URL=https://api.querypilot.example.com
  ```
  > [!IMPORTANT]
  > `VITE_API_BASE_URL` is a **client-side, build-time setting** embedded directly into the frontend bundle. **NEVER** put API keys, secrets, or database credentials in frontend environment variables.

---

## Available Scripts

Verified scripts from [package.json](package.json):

* `npm run dev`: Starts the local Vite development server with Hot Module Replacement (HMR).
* `npm run build`: Type-checks the application via `tsc -b` and builds production-optimized static assets in `dist/`.
* `npm run lint`: Runs Oxlint over TypeScript and React source files for fast static analysis.
* `npm run preview`: Serves the local `dist/` production bundle for previewing.
* `npm test`: Runs unit tests for API client endpoint resolution using Node.js native test runner.

---

## Project Structure & Architecture

```text
frontend/
├── public/
│   ├── favicon.svg          # Application favicon
│   └── icons.svg            # SVG asset bundle
├── src/
│   ├── assets/              # Static styling assets
│   ├── components/
│   │   ├── QueryInput.tsx   # Search input, Submit/Cancel buttons, and keyboard controls
│   │   ├── QueryResults.tsx # Grounded answers, interactive clarification pills, and data table
│   │   └── ErrorAlert.tsx   # Sanitized error alert banner with correlated X-Request-ID
│   ├── services/
│   │   └── apiClient.ts     # Typed fetch client wrapping /api/health and /api/query
│   ├── types/
│   │   └── api.ts           # TypeScript interfaces matching backend Pydantic models
│   ├── App.tsx              # Root application shell, state management, and request lifecycle
│   ├── App.css              # Application layout, responsive grid, and component styles
│   ├── index.css            # Base CSS reset and typographic tokens
│   └── main.tsx             # React DOM entry point
├── index.html               # Single-page application entry HTML
├── package.json             # Dependencies and build scripts
├── tsconfig.json            # Root TypeScript project references
├── tsconfig.app.json        # Application TypeScript configuration
├── tsconfig.node.json       # Vite tooling TypeScript configuration
└── vite.config.ts           # Vite server, React plugin, and reverse-proxy setup
```

### Component Responsibilities

1. **`App.tsx`**:
   Manages the top-level state machine: liveness check on mount (`getHealth`), query submission lifecycle, active request aborting via `AbortController`, error state, and result presentation.
2. **`QueryInput.tsx`**:
   Provides an accessible query textarea, submission trigger, and real-time cancellation control.
3. **`QueryResults.tsx`**:
   Renders the pipeline outcome:
   - Grounded natural-language summary (scalar value or verified response).
   - Interactive clarification cards when ambiguous requests require user refinement.
   - Tabular grid displaying column names, rows, row counts, and truncation disclosures.
   - Execution performance indicators (latency, row count, read-only safety banner).
4. **`ErrorAlert.tsx`**:
   Displays sanitized user-friendly error banners alongside the tracking `X-Request-ID` for troubleshooting without leaking backend stack traces.
5. **`apiClient.ts`**:
   Encapsulates HTTP operations, translates network drops and 422/500 HTTP codes into typed `ApiClientError` models, and handles cancellation cleanly without false server error alarms.

---

## Known Limitations

1. **Production Hosting Configuration:**
   While local development defaults to the Vite reverse proxy (`/api/*`), production builds deployed on a distinct origin from the backend require setting `VITE_API_BASE_URL` at build time and configuring `CORS_ALLOWED_ORIGINS` on the FastAPI server. Unified deployments behind a reverse proxy (e.g. Nginx or Cloudflare) can continue using the default `/api` prefix without CORS.
2. **No Persistent Session Store:**
   Conversation turns and query history are kept in React memory for the active session and are not persisted to a database or local storage.
3. **Target Environment:**
   Configured for local paired development with the QueryPilot FastAPI server running on `127.0.0.1:8000`.
