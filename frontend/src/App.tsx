import { useEffect, useRef, useState } from 'react'
import { AccessKeyBar } from './components/AccessKeyBar'
import { AgentAuthModal } from './components/AgentAuthModal'
import { CandidateSqlReview } from './components/CandidateSqlReview'
import { ErrorAlert } from './components/ErrorAlert'
import { ProfileOnboardingModal } from './components/ProfileOnboardingModal'
import { ProfileSelector } from './components/ProfileSelector'
import { QueryInput } from './components/QueryInput'
import { QueryResults } from './components/QueryResults'
import {
  AgentSessionCoordinator,
  useAgentSession,
} from './services/agentSession'
import {
  SqlGenerationCoordinator,
  useSqlGeneration,
} from './services/sqlGeneration'
import {
  QueryExecutionCoordinator,
  useQueryExecution,
} from './services/queryExecution'
import { LocalExecutionResults } from './components/LocalExecutionResults'
import { getHealth, postQuery } from './services/apiClient'
import type { ExecutionMode, HealthResponse, QueryResponse } from './types/api'
import './App.css'

type HealthStatus = 'checking' | 'online' | 'unavailable'

interface HealthState {
  status: HealthStatus
  data?: HealthResponse
  errorMessage?: string
}

export interface AppProps {
  agentCoordinator?: AgentSessionCoordinator
  sqlCoordinator?: SqlGenerationCoordinator
  queryExecCoordinator?: QueryExecutionCoordinator
}

export function App({ agentCoordinator, sqlCoordinator, queryExecCoordinator }: AppProps = {}) {
  // 1. Health probe state
  const [health, setHealth] = useState<HealthState>({
    status: 'checking',
  })

  // 2. Local Agent session coordinator
  const { state: agentSession, coordinator } = useAgentSession(agentCoordinator)
  const [isOnboardingOpen, setIsOnboardingOpen] = useState<boolean>(false)

  // 3. Decoupled SQL Generation coordinator (Phase 7.3 / F28-A9-B6)
  const { state: sqlGenState, coordinator: sqlGenCoordinator } = useSqlGeneration(sqlCoordinator)

  // 4. Safe Query Execution coordinator (Phase 7.4 / F28-A9-B7)
  const { state: queryExecState, coordinator: execCoordinator } = useQueryExecution(queryExecCoordinator)
  const [prevCandidateResponse, setPrevCandidateResponse] = useState(sqlGenState.response)
  const [reviewedSql, setReviewedSql] = useState<string>(sqlGenState.response?.candidate_sql || '')

  // Reset or initialize reviewed SQL when candidate response updates
  if (sqlGenState.response !== prevCandidateResponse) {
    setPrevCandidateResponse(sqlGenState.response)
    setReviewedSql(sqlGenState.response?.candidate_sql || '')
  }

  // 5. Query execution state (legacy)
  const [isLoading, setIsLoading] = useState(false)
  const [queryResponse, setQueryResponse] = useState<QueryResponse | null>(null)
  const [queryError, setQueryError] = useState<Error | null>(null)

  const activeAbortController = useRef<AbortController | null>(null)

  // Agent session probe on application startup
  useEffect(() => {
    coordinator.probeSession()
  }, [coordinator])

  // Clear candidate SQL, query execution results, and conversation context when profile changes or session is invalidated
  useEffect(() => {
    if (
      agentSession.status !== 'authenticated' ||
      !agentSession.selectedProfileId ||
      agentSession.schemaStatus !== 'loaded'
    ) {
      sqlGenCoordinator.clearCandidate()
      sqlGenCoordinator.clearConversationContext()
      execCoordinator.clearResult()
    } else if (
      sqlGenState.lastGeneratedProfileId &&
      sqlGenState.lastGeneratedProfileId !== agentSession.selectedProfileId
    ) {
      sqlGenCoordinator.clearCandidate()
      sqlGenCoordinator.clearConversationContext()
      execCoordinator.clearResult()
    }
  }, [
    agentSession.status,
    agentSession.selectedProfileId,
    agentSession.schemaStatus,
    sqlGenState.lastGeneratedProfileId,
    sqlGenCoordinator,
    execCoordinator,
  ])

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
  const handleQuerySubmit = async (
    question: string,
    options?: { executionMode?: ExecutionMode; includeSql?: boolean }
  ) => {
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
        {
          question,
          execution_mode: options?.executionMode,
          include_sql: options?.includeSql,
        },
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

  const handleAgentBootstrap = async (token: string) => {
    return coordinator.submitBootstrap(token)
  }

  const handleAgentLogout = async () => {
    sqlGenCoordinator.clearConversationContext()
    await coordinator.logout()
  }

  const handleResetConversation = () => {
    sqlGenCoordinator.resetAll()
    execCoordinator.clearResult()
    setReviewedSql('')
  }

  const handleGenerateCandidateSql = async () => {
    execCoordinator.clearResult()
    try {
      const res = await sqlGenCoordinator.generateCandidate(
        sqlGenState.question,
        agentSession.schemaContext,
        agentSession.selectedProfileId
      )
      if (res?.candidate_sql) {
        setReviewedSql(res.candidate_sql)
      }
    } catch {
      // Error handled in sqlGenState.error
    }
  }

  const handleReviewedSqlChange = (newSql: string) => {
    setReviewedSql(newSql)
    execCoordinator.clearResult()
  }

  const handleExecuteCandidateSql = async (sqlToExecute: string) => {
    // Capture exact candidate metadata prior to async execution dispatch
    const questionForThisCandidate = sqlGenState.lastGeneratedQuestion
    const profileForThisCandidate = sqlGenState.lastGeneratedProfileId
    const candidateIdForThisCandidate = sqlGenState.candidateId

    try {
      const result = await execCoordinator.executeQuery(
        agentSession.selectedProfileId,
        sqlToExecute,
        sqlGenState.response?.request_id
      )

      // Record completed turn only when execution succeeded and active profile remains matched
      if (
        result &&
        questionForThisCandidate &&
        profileForThisCandidate === agentSession.selectedProfileId &&
        agentSession.status === 'authenticated'
      ) {
        sqlGenCoordinator.recordCompletedTurn(
          questionForThisCandidate,
          result.columns || [],
          { candidateId: candidateIdForThisCandidate }
        )
      }
    } catch {
      // Error handled in queryExecState.error
    }
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

        <div className="status-bars-container">
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

          {/* Local Agent Session Status Bar */}
          <div className="agent-session-bar" role="region" aria-label="Local Agent Session">
            <div className="agent-session-group">
              <span
                className={`status-dot status-dot-${
                  agentSession.status === 'authenticated'
                    ? 'online'
                    : agentSession.status === 'probing'
                    ? 'checking'
                    : 'unavailable'
                }`}
                aria-hidden="true"
              />
              <span className="agent-session-label">Local Agent:</span>
              {agentSession.status === 'probing' && (
                <span className="agent-session-text">Checking session...</span>
              )}
              {agentSession.status === 'authenticated' && (
                <span className="agent-session-text agent-authenticated">
                  Connected &bull; {agentSession.profiles.length} profile{agentSession.profiles.length === 1 ? '' : 's'} available
                </span>
              )}
              {agentSession.status === 'auth_required' && (
                <span className="agent-session-text agent-auth-needed">
                  Authentication Required
                </span>
              )}
              {agentSession.status === 'disconnected' && (
                <span className="agent-session-text agent-disconnected">
                  Disconnected (Daemon unreachable on 127.0.0.1:8001)
                </span>
              )}
              {agentSession.status === 'error' && (
                <span className="agent-session-text agent-error">
                  {agentSession.errorMessage || 'Agent error'}
                </span>
              )}
            </div>

            <div className="agent-session-actions">
              {agentSession.status === 'authenticated' && (
                <button
                  type="button"
                  className="agent-action-btn agent-logout-btn"
                  onClick={handleAgentLogout}
                >
                  Disconnect / Logout
                </button>
              )}
              {agentSession.status === 'auth_required' && (
                <button
                  type="button"
                  className="agent-action-btn agent-auth-btn"
                  onClick={() => coordinator.openModal()}
                >
                  Authenticate Agent
                </button>
              )}
              {(agentSession.status === 'disconnected' || agentSession.status === 'error') && (
                <button
                  type="button"
                  className="agent-action-btn agent-retry-btn"
                  onClick={() => coordinator.retry()}
                >
                  Retry Agent
                </button>
              )}
            </div>
          </div>
        </div>
      </header>

      <main className="app-main">
        <div className="workspace-container">
          {/* Prototype Access Key Authorization */}
          <section className="access-key-section" aria-label="Access Key Configuration">
            <AccessKeyBar
              authRequired={queryError !== null && 'kind' in queryError && queryError.kind === 'unauthorized'}
            />
          </section>

          {/* Database Profile & Schema Selection (Phase 7.2 / F28-A9-B5) */}
          {agentSession.status === 'authenticated' && (
            <section className="profile-selector-section" aria-label="Database Profile Configuration">
              <ProfileSelector
                profiles={agentSession.profiles}
                selectedProfileId={agentSession.selectedProfileId}
                selectedProfile={agentSession.selectedProfile}
                schemaStatus={agentSession.schemaStatus}
                schemaContext={agentSession.schemaContext}
                rawSchema={agentSession.rawSchema}
                schemaError={agentSession.schemaError}
                onSelectProfile={(profileId) => {
                  void coordinator.selectProfile(profileId)
                }}
                onRetrySchema={() => {
                  void coordinator.retryLoadSchema()
                }}
                onOpenOnboarding={() => setIsOnboardingOpen(true)}
                onTestConnection={(profileId) => {
                  void coordinator.testConnection(profileId)
                }}
                connectionTestStatus={agentSession.connectionTestStatus}
                connectionTestResult={agentSession.connectionTestResult}
              />
            </section>
          )}

          {/* Decoupled Candidate SQL Generation, Review & Execution (Phase 7.3+7.4 / F28-A9-B6+B7) */}
          {agentSession.status === 'authenticated' &&
            agentSession.selectedProfileId &&
            agentSession.schemaStatus === 'loaded' && (
              <section
                className="candidate-sql-section"
                aria-label="Candidate SQL Generation, Review and Execution"
              >
                <CandidateSqlReview
                  question={sqlGenState.question}
                  isLoading={sqlGenState.isLoading}
                  response={sqlGenState.response}
                  error={sqlGenState.error}
                  isStale={sqlGenState.isStale}
                  activeProfileName={agentSession.selectedProfile?.display_name}
                  onQuestionChange={(q) => {
                    sqlGenCoordinator.setQuestion(q)
                    execCoordinator.clearResult()
                  }}
                  onSubmit={handleGenerateCandidateSql}
                  onCancel={() => sqlGenCoordinator.cancelGeneration()}
                  onClear={() => {
                    sqlGenCoordinator.setQuestion('')
                    execCoordinator.clearResult()
                    setReviewedSql('')
                  }}
                  reviewedSql={reviewedSql}
                  onReviewedSqlChange={handleReviewedSqlChange}
                  onExecuteSql={handleExecuteCandidateSql}
                  onCancelExecute={() => execCoordinator.cancelExecution()}
                  isExecuting={queryExecState.isLoading}
                  canExecute={agentSession.status === 'authenticated' && agentSession.schemaStatus === 'loaded'}
                  conversationContext={sqlGenState.conversationContext}
                  onResetConversation={handleResetConversation}
                  onSelectClarificationOption={(_opt, _clarified) => {
                    execCoordinator.clearResult()
                  }}
                />

                <LocalExecutionResults
                  isLoading={queryExecState.isLoading}
                  result={queryExecState.result}
                  error={queryExecState.error}
                  isAmbiguousFailure={queryExecState.isAmbiguousFailure}
                  isConfirmedTimeout={queryExecState.isConfirmedTimeout}
                  activeProfileName={agentSession.selectedProfile?.display_name}
                  onClear={() => execCoordinator.clearResult()}
                  onRetry={() => {
                    if (reviewedSql.trim()) {
                      void handleExecuteCandidateSql(reviewedSql.trim())
                    }
                  }}
                />
              </section>
            )}

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

      {/* Local Agent Authentication Dialog */}
      <AgentAuthModal
        isOpen={agentSession.isModalOpen}
        isSubmitting={agentSession.isSubmitting}
        authError={agentSession.authError}
        onSubmit={handleAgentBootstrap}
        onClose={() => coordinator.closeModal()}
      />

      {/* Local Database Profile Onboarding & Connection Test Dialog (B12-F8H) */}
      <ProfileOnboardingModal
        isOpen={isOnboardingOpen}
        onClose={() => setIsOnboardingOpen(false)}
        onProfileCreated={async () => {
          await coordinator.refreshProfiles()
        }}
        onUseProfile={(profileId) => {
          void coordinator.selectProfile(profileId)
        }}
      />
    </div>
  )
}

export default App
