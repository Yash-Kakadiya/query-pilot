import { useEffect, useRef, useState } from 'react'
import { ErrorAlert } from './components/ErrorAlert'
import { QueryInput } from './components/QueryInput'
import { QueryResults } from './components/QueryResults'
import { getHealth, postQuery } from './services/apiClient'
import type { HealthResponse, QueryResponse } from './types/api'
import './App.css'

type HealthStatus = 'checking' | 'online' | 'unavailable'

interface HealthState {
  status: HealthStatus
  data?: HealthResponse
  errorMessage?: string
}

function App() {
  // 1. Health probe state (Task 1.33-B2)
  const [health, setHealth] = useState<HealthState>({
    status: 'checking',
  })

  // 2. Query execution state (Task 1.33-B4)
  const [isLoading, setIsLoading] = useState(false)
  const [queryResponse, setQueryResponse] = useState<QueryResponse | null>(null)
  const [queryError, setQueryError] = useState<Error | null>(null)

  const activeAbortController = useRef<AbortController | null>(null)

  // Health check on mount
  useEffect(() => {
    const controller = new AbortController()
    let isMounted = true

    getHealth(controller.signal)
      .then((data) => {
        if (isMounted) {
          setHealth({ status: 'online', data })
        }
      })
      .catch((err: unknown) => {
        if (isMounted && !(err instanceof DOMException && err.name === 'AbortError')) {
          const message =
            err instanceof Error ? err.message : 'Unable to connect to API service.'
          setHealth({
            status: 'unavailable',
            errorMessage: message,
          })
        }
      })

    return () => {
      isMounted = false
      controller.abort()
    }
  }, [])

  // In-flight query cancellation on unmount
  useEffect(() => {
    return () => {
      if (activeAbortController.current) {
        activeAbortController.current.abort()
        activeAbortController.current = null
      }
    }
  }, [])

  const handleRetryHealth = () => {
    setHealth({ status: 'checking' })
    getHealth()
      .then((data) => {
        setHealth({ status: 'online', data })
      })
      .catch((err: unknown) => {
        const message =
          err instanceof Error ? err.message : 'Unable to connect to API service.'
        setHealth({
          status: 'unavailable',
          errorMessage: message,
        })
      })
  }

  // Query execution handler with lifecycle & stale-response hardening
  const handleQuerySubmit = async (question: string) => {
    setQueryError(null)
    setIsLoading(true)

    // Abort any existing in-flight query before initiating a new one
    if (activeAbortController.current) {
      activeAbortController.current.abort()
    }

    const controller = new AbortController()
    activeAbortController.current = controller

    try {
      const response = await postQuery(
        { question },
        { signal: controller.signal }
      )
      // Only commit if this controller is still the active one (prevent race conditions)
      if (activeAbortController.current === controller) {
        setQueryResponse(response)
      }
    } catch (err: unknown) {
      if (err instanceof DOMException && err.name === 'AbortError') {
        // User/system aborted, do not treat as error
        return
      }
      if (activeAbortController.current === controller) {
        setQueryError(err instanceof Error ? err : new Error('Query execution failed.'))
      }
    } finally {
      if (activeAbortController.current === controller) {
        setIsLoading(false)
        activeAbortController.current = null
      }
    }
  }

  const handleQueryCancel = () => {
    if (activeAbortController.current) {
      activeAbortController.current.abort()
      activeAbortController.current = null
      setIsLoading(false)
    }
  }

  const handleClearResults = () => {
    setQueryResponse(null)
    setQueryError(null)
  }

  return (
    <div className="app-container">
      <header className="app-header">
        <div className="brand">
          <span className="brand-icon" aria-hidden="true">🧭</span>
          <h1 className="brand-title">QueryPilot</h1>
        </div>
        <p className="brand-subtitle">
          Ask questions about your data in natural language.
        </p>

        {/* Operational Liveness Indicator */}
        <div className="health-bar" role="status" aria-live="polite">
          <div className="health-indicator-group">
            <span
              className={`status-dot status-dot-${health.status}`}
              aria-hidden="true"
            />
            <span className="health-label">API Liveness:</span>
            {health.status === 'checking' && (
              <span className="health-status-text">Checking...</span>
            )}
            {health.status === 'online' && (
              <span className="health-status-text health-online">
                Online <span className="health-version">v{health.data?.version}</span>
              </span>
            )}
            {health.status === 'unavailable' && (
              <span className="health-status-text health-unavailable">
                Unavailable
              </span>
            )}
          </div>

          {health.status === 'unavailable' && (
            <div className="health-error-row">
              <span className="health-error-message">
                {health.errorMessage || 'Service unreachable.'}
              </span>
              <button
                type="button"
                className="retry-button"
                onClick={handleRetryHealth}
              >
                Retry
              </button>
            </div>
          )}

          <div className="health-caption">
            Application service liveness probe &bull; Does not verify database or Gemini readiness
          </div>
        </div>
      </header>

      <main className="app-main">
        <div className="workspace-container">
          {/* Query Input Section */}
          <section className="query-input-section" aria-label="Query Workspace">
            <QueryInput
              isLoading={isLoading}
              onSubmit={handleQuerySubmit}
              onCancel={handleQueryCancel}
              onClear={handleClearResults}
              hasResult={queryResponse !== null || queryError !== null}
            />
          </section>

          {/* Error Message */}
          {queryError && (
            <section className="query-error-section" aria-label="Error Notification">
              <ErrorAlert
                error={queryError}
                onDismiss={() => setQueryError(null)}
              />
            </section>
          )}

          {/* Results Presentation */}
          {queryResponse && (
            <section className="query-results-section" aria-label="Query Results">
              <QueryResults response={queryResponse} />
            </section>
          )}

          {/* Initial State Helper */}
          {!queryResponse && !queryError && !isLoading && (
            <div className="initial-state-card">
              <div className="initial-icon" aria-hidden="true">💡</div>
              <p className="initial-hint">
                Type a natural-language question above and click <strong>Run Query</strong> to execute.
              </p>
              <div className="example-chips">
                <span className="example-label">Examples:</span>
                <span className="example-pill">"How many courses are in Computer Science?"</span>
                <span className="example-pill">"What is the average GPA of all students?"</span>
              </div>
            </div>
          )}
        </div>
      </main>

      <footer className="app-footer">
        <p>QueryPilot &bull; Autonomous Data Analyst</p>
      </footer>
    </div>
  )
}

export default App
