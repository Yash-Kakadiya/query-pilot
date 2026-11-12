import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { AgentAuthModal } from './components/AgentAuthModal'
import { CandidateSqlReview } from './components/CandidateSqlReview'
import { ProfileOnboardingModal } from './components/ProfileOnboardingModal'
import { ProfileSelector } from './components/ProfileSelector'
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
import { MetricConfigModal } from './components/MetricConfigModal'
import { ConversationHistoryDrawer } from './components/ConversationHistoryDrawer'
import { synthesizeDeterministicAnswer } from './services/answerSynthesis'
import { classifyExecutionError, canAttemptRepair } from './services/errorClassification'
import { getProfileEngine } from './utils/profileUtils'
import { resolveAutoProfileSelection } from './utils/profileSelection'
import {
  AiStatusCoordinator,
  useAiStatus,
} from './services/aiStatus'
import { AiStatusIndicator } from './components/AiStatusIndicator'
import { getPairingToken } from './services/agentClient'
import './App.css'

export interface AppProps {
  agentCoordinator?: AgentSessionCoordinator
  sqlCoordinator?: SqlGenerationCoordinator
  queryExecCoordinator?: QueryExecutionCoordinator
  aiCoordinator?: AiStatusCoordinator
}

export function App({ agentCoordinator, sqlCoordinator, queryExecCoordinator, aiCoordinator }: AppProps = {}) {
  // 1. AI Status Coordinator (Milestone B29-C)
  const { state: aiStatus, coordinator: aiStatusCoordinator } = useAiStatus(aiCoordinator)

  // 2. Local Agent session coordinator
  const { state: agentSession, coordinator } = useAgentSession(agentCoordinator)
  const [isOnboardingOpen, setIsOnboardingOpen] = useState<boolean>(false)
  const [isMetricModalOpen, setIsMetricModalOpen] = useState<boolean>(false)
  const [isHistoryDrawerOpen, setIsHistoryDrawerOpen] = useState<boolean>(false)

  // 3. Decoupled SQL Generation coordinator (Phase 7.3 / F28-A9-B6)
  const { state: sqlGenState, coordinator: sqlGenCoordinator } = useSqlGeneration(sqlCoordinator)

  // 4. Safe Query Execution coordinator (Phase 7.4 / F28-A9-B7)
  const { state: queryExecState, coordinator: execCoordinator } = useQueryExecution(queryExecCoordinator)
  const [prevCandidateResponse, setPrevCandidateResponse] = useState(sqlGenState.response)
  const [reviewedSql, setReviewedSql] = useState<string>(sqlGenState.response?.candidate_sql || '')

  // 5. Execution Recovery state (Milestone B14-F14)
  const [repairedCandidateIds, setRepairedCandidateIds] = useState<Record<string, boolean>>({})
  const [isRepairing, setIsRepairing] = useState<boolean>(false)
  const [repairError, setRepairError] = useState<string | null>(null)

  // Synchronous in-flight guard and lifecycle tracking for repair requests
  const activeRepairTokenRef = useRef<number | null>(null)
  const repairSeqRef = useRef<number>(0)
  const isMountedRef = useRef<boolean>(true)

  useEffect(() => {
    isMountedRef.current = true
    return () => {
      isMountedRef.current = false
      activeRepairTokenRef.current = null
    }
  }, [])

  // Reset or initialize reviewed SQL when candidate response updates
  if (sqlGenState.response !== prevCandidateResponse) {
    setPrevCandidateResponse(sqlGenState.response)
    setReviewedSql(sqlGenState.response?.candidate_sql || '')
  }

  // Agent session probe on application startup
  useEffect(() => {
    coordinator.probeSession()
  }, [coordinator])

  // User explicit profile selection tracking (Milestone B29-B)
  const userClearedProfileRef = useRef<boolean>(false)
  const autoSelectedProfileIdRef = useRef<string | null>(null)

  const handleSelectProfile = useCallback(
    (profileId: string | null) => {
      if (!profileId) {
        userClearedProfileRef.current = true
        autoSelectedProfileIdRef.current = null
        void coordinator.selectProfile(null)
      } else {
        userClearedProfileRef.current = false
        autoSelectedProfileIdRef.current = profileId
        void coordinator.selectProfile(profileId)
      }
    },
    [coordinator]
  )

  // Automatic database profile selection (Milestone B29-B)
  useEffect(() => {
    if (agentSession.status !== 'authenticated') {
      autoSelectedProfileIdRef.current = null
      return
    }

    const targetProfileId = resolveAutoProfileSelection({
      status: agentSession.status,
      profiles: agentSession.profiles,
      currentSelectedId: agentSession.selectedProfileId,
      userExplicitlyCleared: userClearedProfileRef.current,
    })

    if (targetProfileId && targetProfileId !== agentSession.selectedProfileId) {
      if (autoSelectedProfileIdRef.current === targetProfileId) {
        return
      }
      autoSelectedProfileIdRef.current = targetProfileId
      void coordinator.selectProfile(targetProfileId)
    } else if (!targetProfileId && agentSession.selectedProfileId) {
      const isCurrentValid = agentSession.profiles.some(
        (p) => p.profile_id === agentSession.selectedProfileId
      )
      if (!isCurrentValid) {
        userClearedProfileRef.current = false
        autoSelectedProfileIdRef.current = null
        void coordinator.selectProfile(null)
      }
    }
  }, [
    agentSession.status,
    agentSession.profiles,
    agentSession.selectedProfileId,
    coordinator,
  ])

  // Clear candidate SQL, query execution results, and conversation context when profile changes or session is invalidated
  const prevProfileIdRef = useRef<string | null>(agentSession.selectedProfileId)
  useEffect(() => {
    if (prevProfileIdRef.current !== agentSession.selectedProfileId) {
      prevProfileIdRef.current = agentSession.selectedProfileId
      setReviewedSql('')
      execCoordinator.clearResult()
    }
  }, [agentSession.selectedProfileId, execCoordinator])

  useEffect(() => {
    if (
      agentSession.status !== 'authenticated' ||
      !agentSession.selectedProfileId ||
      agentSession.schemaStatus !== 'loaded'
    ) {
      activeRepairTokenRef.current = null
      sqlGenCoordinator.clearCandidate()
      sqlGenCoordinator.clearConversationContext()
      sqlGenCoordinator.clearBusinessMetrics()
      execCoordinator.clearResult()
      setReviewedSql('')
      setRepairedCandidateIds({})
      setIsRepairing(false)
      setRepairError(null)
    } else if (
      sqlGenState.lastGeneratedProfileId &&
      sqlGenState.lastGeneratedProfileId !== agentSession.selectedProfileId
    ) {
      activeRepairTokenRef.current = null
      sqlGenCoordinator.clearCandidate()
      sqlGenCoordinator.clearConversationContext()
      sqlGenCoordinator.clearBusinessMetrics()
      execCoordinator.clearResult()
      setReviewedSql('')
      setRepairedCandidateIds({})
      setIsRepairing(false)
      setRepairError(null)
    }
  }, [
    agentSession.status,
    agentSession.selectedProfileId,
    agentSession.schemaStatus,
    sqlGenState.lastGeneratedProfileId,
    sqlGenCoordinator,
    execCoordinator,
  ])

  // Synchronize agent session status with AI status coordinator (Milestone B29-C)
  useEffect(() => {
    aiStatusCoordinator.syncAgentStatus(agentSession.status)
  }, [agentSession.status, aiStatusCoordinator])

  // Periodic background check with cleanup on unmount
  useEffect(() => {
    const cleanup = aiStatusCoordinator.startPolling(60000)
    return () => cleanup()
  }, [aiStatusCoordinator])

  const handleAgentBootstrap = async (token: string) => {
    return coordinator.submitBootstrap(token)
  }

  const [copiedToken, setCopiedToken] = useState<boolean>(false)

  const handleCopyPairingKey = async () => {
    try {
      const res = await getPairingToken()
      if (res.pairing_token) {
        await navigator.clipboard.writeText(res.pairing_token)
        setCopiedToken(true)
        setTimeout(() => setCopiedToken(false), 3000)
      }
    } catch {
      // Ignore or fall back gracefully
    }
  }

  const handleAgentLogout = async () => {
    sqlGenCoordinator.clearConversationContext()
    await coordinator.logout()
  }

  const handleResetConversation = () => {
    activeRepairTokenRef.current = null
    sqlGenCoordinator.resetAll()
    execCoordinator.clearResult()
    setReviewedSql('')
    setRepairedCandidateIds({})
    setIsRepairing(false)
    setRepairError(null)
  }

  const handleGenerateCandidateSql = async () => {
    execCoordinator.clearResult()
    setRepairError(null)
    try {
      const res = await sqlGenCoordinator.generateCandidate(
        sqlGenState.question,
        agentSession.schemaContext,
        agentSession.selectedProfileId
      )
      if (res?.candidate_sql) {
        setReviewedSql(res.candidate_sql)
      }
      aiStatusCoordinator.recordGenerationSuccess()
    } catch (err: unknown) {
      aiStatusCoordinator.recordGenerationFailure(err)
    }
  }

  // Memoized error classification and repair eligibility
  const errorClassification = useMemo(() => {
    if (!queryExecState.error) {
      return null
    }
    return classifyExecutionError(queryExecState.error)
  }, [queryExecState.error])

  const canRepair = useMemo(
    () =>
      canAttemptRepair({
        executionError: queryExecState.error,
        isExecutionLoading: queryExecState.isLoading,
        isSqlGenLoading: sqlGenState.isLoading,
        isRepairing,
        errorClassification,
        activeCandidateId: sqlGenState.candidateId,
        repairedCandidateIds,
        sessionStatus: agentSession.status,
        selectedProfileId: agentSession.selectedProfileId,
        schemaStatus: agentSession.schemaStatus,
        lastGeneratedQuestion: sqlGenState.lastGeneratedQuestion,
        isStale: sqlGenState.isStale,
      }),
    [
      queryExecState.error,
      queryExecState.isLoading,
      sqlGenState.isLoading,
      isRepairing,
      errorClassification,
      sqlGenState.candidateId,
      repairedCandidateIds,
      agentSession.status,
      agentSession.selectedProfileId,
      agentSession.schemaStatus,
      sqlGenState.lastGeneratedQuestion,
      sqlGenState.isStale,
    ]
  )

  const handleRepairCandidateSql = async () => {
    // 1. Synchronous in-flight guard check: if repair is already active, return immediately
    if (activeRepairTokenRef.current !== null) {
      return
    }

    // 2. Direct coordinator check to guard against stale closure if coordinator is loading
    if (sqlGenCoordinator.getState().isLoading) {
      return
    }

    const activeCandidateId = sqlGenCoordinator.getState().candidateId || sqlGenState.candidateId
    const activeQuestion =
      sqlGenCoordinator.getState().lastGeneratedQuestion || sqlGenState.lastGeneratedQuestion
    if (
      !canRepair ||
      !activeCandidateId ||
      !errorClassification?.diagnosticFeedback ||
      !activeQuestion
    ) {
      return
    }

    // 3. Acquire ownership token synchronously BEFORE dispatching or scheduling state changes
    const token = ++repairSeqRef.current
    activeRepairTokenRef.current = token

    const initiatingCandidateId = activeCandidateId
    const initiatingProfileId = agentSession.selectedProfileId
    const initiatingQuestion = SqlGenerationCoordinator.normalizeQuestion(activeQuestion)
    const diagnosticFeedback = errorClassification.diagnosticFeedback

    // Immediately record attempt to block duplicate rapid clicks
    setRepairedCandidateIds((prev) => ({
      ...prev,
      [initiatingCandidateId]: true,
    }))
    setIsRepairing(true)
    setRepairError(null)

    try {
      const res = await sqlGenCoordinator.generateCandidate(
        initiatingQuestion,
        agentSession.schemaContext,
        initiatingProfileId,
        { diagnosticFeedback }
      )

      // Guard check on resolution:
      // - Must still be the active invocation token
      // - Component must still be mounted
      // - Response must be non-null
      // - Session and profile must match initiating context
      // - Schema status must still be loaded
      // - Current coordinator question must still match initiating question (question-drift protection)
      // - Candidate produced by this generation must still be the current active candidate in coordinator
      if (
        activeRepairTokenRef.current === token &&
        isMountedRef.current &&
        res &&
        agentSession.status === 'authenticated' &&
        agentSession.selectedProfileId === initiatingProfileId &&
        agentSession.schemaStatus === 'loaded' &&
        sqlGenCoordinator.matchesCurrentQuestion(initiatingQuestion) &&
        sqlGenCoordinator.isCurrentCandidateId(res.candidateId)
      ) {
        execCoordinator.clearResult()
        if (res.candidate_sql) {
          setReviewedSql(res.candidate_sql)
        }
      }
    } catch {
      // Only set error if this invocation still owns the active token and component is mounted
      if (activeRepairTokenRef.current === token && isMountedRef.current) {
        setRepairError(
          'Unable to generate repair suggestion. You can modify the query manually or retry execution.'
        )
      }
    } finally {
      // Release guard ONLY if this invocation still owns the active token
      if (activeRepairTokenRef.current === token) {
        activeRepairTokenRef.current = null
        if (isMountedRef.current) {
          setIsRepairing(false)
        }
      }
    }
  }

  const handleReviewedSqlChange = (newSql: string) => {
    setReviewedSql(newSql)
    execCoordinator.clearResult()
  }

  const handleExecuteCandidateSql = async (sqlToExecute: string, isManual: boolean = false) => {
    // Capture exact candidate metadata prior to async execution dispatch
    const questionForThisCandidate = isManual ? null : sqlGenState.lastGeneratedQuestion
    const profileForThisCandidate = isManual ? null : sqlGenState.lastGeneratedProfileId
    const candidateIdForThisCandidate = isManual ? null : sqlGenState.candidateId

    try {
      const result = await execCoordinator.executeQuery(
        agentSession.selectedProfileId,
        sqlToExecute,
        isManual ? null : sqlGenState.response?.request_id
      )

      // Record completed turn only when execution succeeded, query was an AI candidate, and active profile remains matched
      if (
        !isManual &&
        result &&
        questionForThisCandidate &&
        profileForThisCandidate === agentSession.selectedProfileId &&
        agentSession.status === 'authenticated'
      ) {
        let safeSummary: string | null = null
        try {
          const synthesized = synthesizeDeterministicAnswer(result)
          safeSummary = synthesized?.text || null
        } catch {
          safeSummary = null
        }

        sqlGenCoordinator.recordCompletedTurn(
          questionForThisCandidate,
          result.columns || [],
          {
            candidateId: candidateIdForThisCandidate,
            synthesizedSummary: safeSummary,
            rowCount: result.row_count,
            executionStatus: 'succeeded',
          }
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
          {/* AI Availability Status Indicator (Milestone B29-C) */}
          <AiStatusIndicator
            status={aiStatus.status}
            label={aiStatus.label}
            description={aiStatus.description}
            detail={aiStatus.detail}
            isChecking={aiStatus.isChecking}
            onCheckAgain={() => {
              void aiStatusCoordinator.checkStatus()
            }}
          />

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
                <>
                  <button
                    type="button"
                    className="agent-action-btn agent-copy-token-btn"
                    onClick={handleCopyPairingKey}
                    title="Copy pairing token to connect web app hosted on Render"
                  >
                    {copiedToken ? '✓ Copied Pairing Key!' : '🔗 Copy Web Pairing Key'}
                  </button>
                  <button
                    type="button"
                    className="agent-action-btn agent-logout-btn"
                    onClick={handleAgentLogout}
                  >
                    Disconnect / Logout
                  </button>
                </>
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
                onSelectProfile={handleSelectProfile}
                onRetrySchema={() => {
                  void coordinator.retryLoadSchema()
                }}
                onOpenOnboarding={() => setIsOnboardingOpen(true)}
                onTestConnection={(profileId) => {
                  void coordinator.testConnection(profileId)
                }}
                onDeleteProfile={(profileId) => {
                  return coordinator.deleteProfile(profileId).then(() => {})
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
                <div className="metric-config-bar">
                  <button
                    type="button"
                    className="metric-config-trigger-btn"
                    onClick={() => setIsMetricModalOpen(true)}
                    aria-label="Configure business metrics"
                  >
                    📐 Business Metrics {sqlGenState.businessMetrics.length > 0 ? `(${sqlGenState.businessMetrics.length})` : ''}
                  </button>

                  <button
                    type="button"
                    className="metric-config-trigger-btn"
                    onClick={() => setIsHistoryDrawerOpen(true)}
                    aria-label="View conversation history drawer"
                  >
                    📜 Conversation History {sqlGenState.conversationHistory.length > 0 ? `(${sqlGenState.conversationHistory.length})` : ''}
                  </button>

                  {sqlGenState.conversationHistory.length > 0 && (
                    <button
                      type="button"
                      className="history-quick-reset-btn"
                      onClick={handleResetConversation}
                      title="Clear conversation context and start new topic"
                    >
                      + New Topic
                    </button>
                  )}

                  {sqlGenState.businessMetrics.length > 0 && (
                    <span className="metric-config-active-hint">
                      {sqlGenState.businessMetrics.length} active metric definition{sqlGenState.businessMetrics.length === 1 ? '' : 's'} attached
                    </span>
                  )}
                </div>

                <CandidateSqlReview
                  question={sqlGenState.question}
                  isLoading={sqlGenState.isLoading}
                  response={sqlGenState.response}
                  error={sqlGenState.error}
                  isStale={sqlGenState.isStale}
                  activeProfileName={agentSession.selectedProfile?.display_name}
                  engineType={getProfileEngine(agentSession.selectedProfile)}
                  dialect={sqlGenState.response?.dialect}
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
                  onClear={() => {
                    execCoordinator.clearResult()
                    setRepairError(null)
                  }}
                  onRetry={() => {
                    if (reviewedSql.trim()) {
                      void handleExecuteCandidateSql(reviewedSql.trim())
                    }
                  }}
                  canRepair={canRepair}
                  isRepairing={isRepairing}
                  onRepair={handleRepairCandidateSql}
                  repairHint={errorClassification?.diagnosticFeedback}
                  repairError={repairError}
                />
              </section>
            )}

          {/* Authenticated Mode: If no profile selected, display clean getting-started guide */}
          {agentSession.status === 'authenticated' && (!agentSession.selectedProfileId || agentSession.schemaStatus !== 'loaded') && (
            <div className="profile-guide-card" role="region" aria-label="Getting Started">
              <div className="profile-guide-icon" aria-hidden="true">💡</div>
              <div className="profile-guide-body">
                <h3 className="profile-guide-title">
                  {agentSession.schemaStatus === 'loading'
                    ? 'Loading Database Schema...'
                    : 'Select a Database Profile to Begin'}
                </h3>
                <p className="profile-guide-desc">
                  {agentSession.schemaStatus === 'loading'
                    ? 'Introspecting metadata from your local database agent...'
                    : 'QueryPilot generates safe read-only SQL queries tailored directly to your local database schema. Select an approved profile from the dropdown above or click + Add Profile to connect.'}
                </p>
                {agentSession.schemaStatus !== 'loading' && (
                  <div className="profile-guide-steps">
                    <div className="guide-step">
                      <span className="guide-step-num">1</span>
                      <span className="guide-step-text">Choose or configure your local database profile</span>
                    </div>
                    <div className="guide-step">
                      <span className="guide-step-num">2</span>
                      <span className="guide-step-text">Ask business questions in natural English</span>
                    </div>
                    <div className="guide-step">
                      <span className="guide-step-num">3</span>
                      <span className="guide-step-text">Review and safely execute candidate SQL queries</span>
                    </div>
                  </div>
                )}
              </div>
            </div>
          )}

          {/* Unauthenticated / Disconnected States */}
          {agentSession.status === 'probing' && (
            <div className="profile-guide-card" role="region" aria-label="Connecting to Local Agent">
              <div className="profile-guide-icon" aria-hidden="true">⏳</div>
              <div className="profile-guide-body">
                <h3 className="profile-guide-title">Connecting to Local Agent...</h3>
                <p className="profile-guide-desc">Establishing secure loopback connection with your QueryPilot local agent.</p>
              </div>
            </div>
          )}

          {agentSession.status === 'auth_required' && (
            <div className="profile-guide-card" role="region" aria-label="Authentication Required">
              <div className="profile-guide-icon" aria-hidden="true">🔒</div>
              <div className="profile-guide-body">
                <h3 className="profile-guide-title">Local Agent Authentication Required</h3>
                <p className="profile-guide-desc">
                  Authentication is required to inspect database schemas and run queries.
                  Please enter your pairing token to unlock query features.
                </p>
                <button
                  type="button"
                  className="agent-action-btn agent-auth-btn"
                  style={{ marginTop: '0.75rem' }}
                  onClick={() => coordinator.openModal()}
                >
                  Enter Pairing Key
                </button>
              </div>
            </div>
          )}

          {(agentSession.status === 'disconnected' || agentSession.status === 'error') && (
            <div className="profile-guide-card agent-disconnected-guide" role="region" aria-label="Local Agent Connection Guidance">
              <div className="profile-guide-icon" aria-hidden="true">🔌</div>
              <div className="profile-guide-body">
                <h3 className="profile-guide-title">
                  Local Agent Not Connected
                </h3>
                <p className="profile-guide-desc">
                  QueryPilot is designed so database credentials never leave your machine.
                  To inspect your local database schemas and run queries, ensure the QueryPilot daemon is running on <code>127.0.0.1:8001</code>.
                </p>
                <div style={{ marginTop: '1rem', display: 'flex', gap: '0.75rem', alignItems: 'center' }}>
                  <button
                    type="button"
                    className="agent-action-btn agent-retry-btn"
                    onClick={() => coordinator.retry()}
                  >
                    🔄 Retry Connection
                  </button>
                </div>
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
          handleSelectProfile(profileId)
        }}
      />

      {/* Business Metric Configuration Dialog (Milestone B22-C) */}
      <MetricConfigModal
        isOpen={isMetricModalOpen}
        onClose={() => setIsMetricModalOpen(false)}
        metrics={sqlGenState.businessMetrics}
        availableTables={
          agentSession.rawSchema?.tables?.map((t) => t.table_name) ||
          agentSession.schemaContext?.tables?.map((t) => t.table_name) ||
          []
        }
        onSaveMetrics={(updatedMetrics) => {
          sqlGenCoordinator.setBusinessMetrics(updatedMetrics)
        }}
      />

      {/* Conversation History Drawer (Milestone B22-B) */}
      <ConversationHistoryDrawer
        isOpen={isHistoryDrawerOpen}
        onClose={() => setIsHistoryDrawerOpen(false)}
        history={sqlGenState.conversationHistory}
        onRestoreTurn={(turnIdx) => {
          sqlGenCoordinator.restoreTurn(turnIdx)
          execCoordinator.clearResult()
          setReviewedSql('')
        }}
        onNewTopic={handleResetConversation}
      />
    </div>
  )
}

export default App
