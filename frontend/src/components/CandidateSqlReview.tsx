import { useEffect, useState } from 'react'
import type { ClarificationOption, ConversationTurnContext, GenerateSQLResponse } from '../types/api.ts'
import './CandidateSqlReview.css'

import { deriveClarifiedQuestion } from '../services/sqlGeneration.ts'
export { deriveClarifiedQuestion }

export interface CandidateSqlReviewProps {
  question: string
  isLoading: boolean
  response: GenerateSQLResponse | null
  error: Error | null
  isStale: boolean
  activeProfileName?: string
  engineType?: 'mssql' | 'sqlite'
  dialect?: string | null
  onQuestionChange: (newQuestion: string) => void
  onSubmit: () => void
  onCancel: () => void
  onClear: () => void

  // Execution additions (Task 1.34-B1-F28-A9-B7)
  reviewedSql?: string
  onReviewedSqlChange?: (newSql: string) => void
  onExecuteSql?: (sql: string, isManual?: boolean) => void
  onCancelExecute?: () => void
  isExecuting?: boolean
  canExecute?: boolean

  // Multi-turn additions (Milestone B12-F12B)
  conversationContext?: ConversationTurnContext[]
  onResetConversation?: () => void

  // Clarification additions (Milestone B13-F13A)
  onSelectClarificationOption?: (option: ClarificationOption, clarifiedQuestion: string) => void

  // Manual SQL workflow additions (Milestone B29-A)
  isManualMode?: boolean
  onManualModeChange?: (active: boolean) => void
}

/**
 * Classifies generation errors for actionable user feedback.
 */
function getErrorClassificationInfo(err: Error | null): {
  title: string
  isAuth: boolean
  isGateway: boolean
  isNetwork: boolean
} {
  if (!err) {
    return { title: 'Generation Failed', isAuth: false, isGateway: false, isNetwork: false }
  }
  const isApiErr = 'kind' in err
  const statusCode = isApiErr ? (err as { statusCode?: number }).statusCode : undefined
  const kind = isApiErr ? (err as { kind?: string }).kind : undefined

  if (kind === 'unauthorized' || statusCode === 401 || statusCode === 403) {
    return {
      title: statusCode === 403 ? 'Access Denied (HTTP 403)' : 'Access Key Required (HTTP 401)',
      isAuth: true,
      isGateway: false,
      isNetwork: false,
    }
  }
  if (statusCode === 502) {
    return {
      title: 'Upstream Backend Unavailable (HTTP 502)',
      isAuth: false,
      isGateway: true,
      isNetwork: false,
    }
  }
  if (statusCode === 504) {
    return {
      title: 'Upstream Gateway Timeout (HTTP 504)',
      isAuth: false,
      isGateway: true,
      isNetwork: false,
    }
  }
  if (
    kind === 'network' ||
    err.message?.toLowerCase().includes('network') ||
    err.message?.toLowerCase().includes('connect')
  ) {
    return {
      title: 'Network Connection Error',
      isAuth: false,
      isGateway: false,
      isNetwork: true,
    }
  }
  if (statusCode) {
    return {
      title: `Server Error (HTTP ${statusCode})`,
      isAuth: false,
      isGateway: false,
      isNetwork: false,
    }
  }
  return {
    title: 'Failed to generate candidate SQL',
    isAuth: false,
    isGateway: false,
    isNetwork: false,
  }
}

/**
 * Resolve display label for SQL dialect based strictly on authoritative profile/response context.
 */
function resolveDialectLabel(
  responseDialect?: string | null,
  engineType?: 'mssql' | 'sqlite',
): string {
  // 1. Authoritative response dialect metadata
  if (responseDialect) {
    const d = responseDialect.trim().toLowerCase()
    if (d === 'sqlite') {
      return 'SQLite'
    }
    if (d === 'tsql' || d === 'mssql') {
      return 'T-SQL'
    }
  }

  // 2. Authoritative profile engine type
  if (engineType === 'sqlite') {
    return 'SQLite'
  }

  // 3. Fallback to T-SQL (preserving MSSQL and legacy profiles)
  return 'T-SQL'
}

/**
 * Accessible Decoupled Candidate SQL Generation, Review & Safe Execution Component.
 *
 * Provides:
 * 1. Natural-language question submission against the active database profile's schema.
 * 2. Visual presentation of candidate SQL labeled strictly as unexecuted.
 * 3. Optional SQL inspection and editing with clear differentiation of user-edited SQL.
 * 4. Explicit user action required to execute candidate queries against the database profile.
 * 5. Invalidation notice when question changes, blocking execution of stale candidates.
 */
export function CandidateSqlReview({
  question,
  isLoading,
  response,
  error,
  isStale,
  activeProfileName,
  engineType,
  dialect,
  onQuestionChange,
  onSubmit,
  onCancel,
  onClear,
  reviewedSql,
  onReviewedSqlChange,
  onExecuteSql,
  onCancelExecute,
  isExecuting = false,
  canExecute = true,
  conversationContext = [],
  onResetConversation,
  onSelectClarificationOption,
  isManualMode: propsIsManualMode,
  onManualModeChange,
}: CandidateSqlReviewProps) {
  const [copied, setCopied] = useState<boolean>(false)
  const [isEditing, setIsEditing] = useState<boolean>(false)
  const [prevCandidateSql, setPrevCandidateSql] = useState<string | null | undefined>(response?.candidate_sql)
  const [localSql, setLocalSql] = useState<string>(response?.candidate_sql || '')
  const [clarificationBaseQuestion, setClarificationBaseQuestion] = useState<string>(question)
  const [prevResponseRequestId, setPrevResponseRequestId] = useState<string | null | undefined>(response?.request_id)
  const [selectedOptionId, setSelectedOptionId] = useState<string | null>(null)
  const [isColdStartPending, setIsColdStartPending] = useState<boolean>(false)
  const [manualMode, setManualMode] = useState<boolean>(false)

  const isManualModeActive = propsIsManualMode !== undefined ? propsIsManualMode : manualMode

  const setIsManualModeActive = (active: boolean) => {
    setManualMode(active)
    if (onManualModeChange) {
      onManualModeChange(active)
    }
  }

  // Cold-start detection for slow upstream hosted API (e.g. Render spin-up)
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | null = null
    if (isLoading) {
      setIsColdStartPending(false)
      timer = setTimeout(() => {
        setIsColdStartPending(true)
      }, 5000)
    } else {
      setIsColdStartPending(false)
    }
    return () => {
      if (timer) {
        clearTimeout(timer)
      }
    }
  }, [isLoading])

  const dialectLabel = resolveDialectLabel(response?.dialect || dialect, engineType)

  // Track response updates to capture original question baseline for clarification
  if (response?.request_id !== prevResponseRequestId) {
    setPrevResponseRequestId(response?.request_id)
    if (response?.status === 'clarification_needed') {
      setClarificationBaseQuestion(question)
      setSelectedOptionId(null)
    }
  }

  const handleSelectClarificationOption = (option: ClarificationOption) => {
    setSelectedOptionId(option.option_id)
    const clarified = deriveClarifiedQuestion(clarificationBaseQuestion, option)
    onQuestionChange(clarified)
    if (onSelectClarificationOption) {
      onSelectClarificationOption(option, clarified)
    }
  }

  // Sync local SQL during render when response.candidate_sql changes
  if (response?.candidate_sql !== prevCandidateSql) {
    setPrevCandidateSql(response?.candidate_sql)
    setLocalSql(response?.candidate_sql || '')
    setIsEditing(false)
  }

  // Resolve current active SQL text (controlled or local)
  const currentSql = reviewedSql !== undefined ? reviewedSql : localSql
  const isSqlEdited = Boolean(
    response?.candidate_sql &&
    currentSql.trim() !== response.candidate_sql.trim()
  )

  const handleSqlTextChange = (newVal: string) => {
    setLocalSql(newVal)
    if (onReviewedSqlChange) {
      onReviewedSqlChange(newVal)
    }
  }

  const handleResetSql = () => {
    if (response?.candidate_sql) {
      handleSqlTextChange(response.candidate_sql)
      setIsEditing(false)
    }
  }

  const handleFormSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    if (isLoading || isExecuting || !question.trim()) {
      return
    }
    onSubmit()
  }

  const handleCopySql = () => {
    if (currentSql) {
      void navigator.clipboard.writeText(currentSql)
      setCopied(true)
      setTimeout(() => {
        setCopied(false)
      }, 2000)
    }
  }

  const handleExecuteClick = () => {
    if (onExecuteSql && currentSql.trim() && !isExecuting) {
      if (isManualModeActive || (!isStale && response?.status === 'generated')) {
        onExecuteSql(currentSql.trim(), isManualModeActive)
      }
    }
  }

  const charCount = question.length
  const isOverLimit = charCount > 1000

  // Execution enablement invariants:
  // In manual mode, execution requires valid SQL, connected profile, and no in-flight execution.
  // In AI candidate mode, it additionally requires response.status === 'generated' and non-stale question.
  const canExecuteQuery = isManualModeActive
    ? canExecute && !isExecuting && currentSql.trim().length > 0
    : canExecute &&
      response?.status === 'generated' &&
      !isStale &&
      !isLoading &&
      !isExecuting &&
      currentSql.trim().length > 0

  return (
    <div
      className="candidate-sql-card"
      role="region"
      aria-label="Candidate SQL Generation, Review and Execution"
    >
      <div className="candidate-sql-header">
        <span className="candidate-header-icon" aria-hidden="true">📝</span>
        <div className="candidate-title-group">
          <h2 className="candidate-sql-title">Candidate SQL Generation &amp; Local Review</h2>
          <p className="candidate-sql-subtitle">
            Generate and inspect read-only SQL queries against{' '}
            <strong>{activeProfileName || 'the active database profile'}</strong> before execution.
          </p>
        </div>
      </div>

      {/* Active Conversation Context Notice */}
      {conversationContext && conversationContext.length > 0 && (
        <div className="candidate-conversation-banner" role="status" aria-label="Conversation Context">
          <div className="candidate-conversation-info">
            <span className="candidate-conversation-icon" aria-hidden="true">💬</span>
            <span className="candidate-conversation-text">
              Follow-up mode: <strong>{conversationContext.length}</strong> prior turn{conversationContext.length === 1 ? '' : 's'} in memory
            </span>
          </div>
          {onResetConversation && (
            <button
              type="button"
              className="candidate-reset-conversation-btn"
              onClick={onResetConversation}
              disabled={isLoading || isExecuting}
              aria-label="Start a new conversation and clear prior turn memory"
            >
              New Topic
            </button>
          )}
        </div>
      )}

      {/* Natural Language Question Form */}
      <form className="candidate-question-form" onSubmit={handleFormSubmit}>
        <div className="candidate-textarea-wrapper">
          <div className="candidate-label-row">
            <label htmlFor="candidate-question-input" className="candidate-input-label">
              Natural Language Question:
            </label>
            <span
              className={`candidate-char-counter ${
                isOverLimit ? 'candidate-char-counter-error' : ''
              }`}
            >
              {charCount}/1000
            </span>
          </div>

          <textarea
            id="candidate-question-input"
            className="candidate-textarea"
            placeholder={
              conversationContext && conversationContext.length > 0
                ? "Ask a follow-up question (e.g. 'Now show only 2024') or continue query thread..."
                : "Ask a question about your database (e.g. 'Show total courses offered by department')..."
            }
            value={question}
            onChange={(e) => onQuestionChange(e.target.value)}
            disabled={isLoading || isExecuting}
            rows={3}
            aria-required="true"
            aria-invalid={isOverLimit}
          />
        </div>

        {/* Question Invalidation / Stale Notice */}
        {isStale && response && (
          <div className="candidate-stale-banner" role="status">
            <span aria-hidden="true">{response.status === 'clarification_needed' ? 'ℹ️' : '⚠️'}</span>
            <span>
              {response.status === 'clarification_needed'
                ? 'Question updated from clarification. Review or edit your question above, then click "Generate Candidate SQL".'
                : 'Question was modified since candidate SQL was generated. Generate again to refresh the candidate before executing.'}
            </span>
          </div>
        )}

        <div className="candidate-form-actions">
          <button
            type="button"
            className={`candidate-manual-toggle-btn ${isManualModeActive ? 'active' : ''}`}
            onClick={() => setIsManualModeActive(!isManualModeActive)}
            disabled={isLoading || isExecuting}
            aria-pressed={isManualModeActive}
          >
            {isManualModeActive ? 'Close Custom SQL' : 'Write Custom SQL'}
          </button>

          {question.trim().length > 0 && !isLoading && !isExecuting && (
            <button
              type="button"
              className="candidate-clear-btn"
              onClick={onClear}
            >
              Clear
            </button>
          )}

          {isLoading && (
            <button
              type="button"
              className="candidate-cancel-btn"
              onClick={onCancel}
            >
              Cancel Generation
            </button>
          )}

          <button
            type="submit"
            className="candidate-submit-btn"
            disabled={isLoading || isExecuting || !question.trim() || isOverLimit}
          >
            {isLoading ? (
              <>
                <span className="spinner" aria-hidden="true" />
                <span>Generating Candidate SQL...</span>
              </>
            ) : (
              'Generate Candidate SQL'
            )}
          </button>
        </div>
      </form>

      {/* Cold-Start / Slow Upstream Feedback Notice */}
      {isLoading && isColdStartPending && (
        <div className="candidate-coldstart-banner" role="status">
          <span className="coldstart-spinner" aria-hidden="true">⏳</span>
          <div className="coldstart-content">
            <strong>Connecting to hosted backend service...</strong>
            <span>
              The hosted API service may be waking from an idle state (cold start typically takes 15–30 seconds). Request is active and still pending.
            </span>
          </div>
        </div>
      )}

      {/* Generation Error Alert with Retry & Manual Fallback */}
      {error && !isLoading && (
        <div className="candidate-error-alert" role="alert">
          <div className="candidate-error-content">
            <div className="candidate-error-header">
              <span className="candidate-error-icon" aria-hidden="true">⚠️</span>
              <strong className="candidate-error-title">{getErrorClassificationInfo(error).title}</strong>
            </div>
            <p className="candidate-error-text">{error.message || 'Failed to generate candidate SQL.'}</p>
          </div>
          <div className="candidate-error-actions">
            <button
              type="button"
              className="candidate-retry-btn"
              onClick={onSubmit}
            >
              Retry Generation
            </button>
            <button
              type="button"
              className="candidate-manual-entry-btn"
              onClick={() => setIsManualModeActive(true)}
              disabled={isExecuting}
            >
              Write Custom SQL
            </button>
          </div>
        </div>
      )}

      {/* Candidate SQL / Independent Manual SQL Presentation Panel */}
      {(Boolean(response) || isManualModeActive) && !isLoading && (
        <div className="candidate-review-panel">
          <div className="candidate-review-header">
            <div className="candidate-badge-group">
              {isManualModeActive ? (
                <>
                  <span className="candidate-status-badge candidate-badge-manual">
                    Manual SQL Statement
                  </span>
                  <span className="candidate-status-badge candidate-badge-unexecuted">
                    Not Yet Executed
                  </span>
                </>
              ) : (
                <>
                  {response?.status === 'generated' && (
                    <span className="candidate-status-badge candidate-badge-generated">
                      Candidate Generated
                    </span>
                  )}
                  {isSqlEdited && (
                    <span className="candidate-status-badge candidate-badge-edited">
                      Edited SQL (User Modified)
                    </span>
                  )}
                  {response?.status === 'clarification_needed' && (
                    <span className="candidate-status-badge candidate-badge-clarification">
                      Clarification Needed
                    </span>
                  )}
                  {response?.status === 'unsupported' && (
                    <span className="candidate-status-badge candidate-badge-unsupported">
                      Unsupported Question
                    </span>
                  )}
                  {response?.status === 'generation_failed' && (
                    <span
                      className={`candidate-status-badge ${
                        (response.failure_diagnostic?.classification || response.failure_classification) === 'quota_exhausted'
                          ? 'candidate-badge-quota'
                          : (response.failure_diagnostic?.classification || response.failure_classification) === 'authentication_failed'
                          ? 'candidate-badge-auth'
                          : (response.failure_diagnostic?.classification || response.failure_classification) === 'provider_unavailable'
                          ? 'candidate-badge-unavailable'
                          : (response.failure_diagnostic?.classification || response.failure_classification) === 'network_error'
                          ? 'candidate-badge-network'
                          : 'candidate-badge-failed'
                      }`}
                    >
                      {(response.failure_diagnostic?.classification || response.failure_classification) === 'quota_exhausted'
                        ? 'Quota Exhausted'
                        : (response.failure_diagnostic?.classification || response.failure_classification) === 'authentication_failed'
                        ? 'Authentication Failed'
                        : (response.failure_diagnostic?.classification || response.failure_classification) === 'provider_unavailable'
                        ? 'Provider Unavailable'
                        : (response.failure_diagnostic?.classification || response.failure_classification) === 'network_error'
                        ? 'Network Error'
                        : 'Generation Failed'}
                    </span>
                  )}
                </>
              )}
            </div>

            {response?.request_id && !isManualModeActive && (
              <span className="candidate-request-id">
                Request ID: <code>{response.request_id}</code>
              </span>
            )}
          </div>

          {/* Prominent Safety Execution Boundary Disclaimer */}
          {isManualModeActive ? (
            <div className="candidate-disclaimer-box candidate-disclaimer-manual" role="note">
              <span className="candidate-disclaimer-icon" aria-hidden="true">⚠️</span>
              <div className="candidate-disclaimer-text">
                <strong>STRICT EXECUTION BOUNDARY &bull; NOT YET EXECUTED</strong>
                This SQL statement was entered manually and has NOT been executed against your database. Review the query carefully before executing.
              </div>
            </div>
          ) : response?.status === 'generated' && Boolean(currentSql) ? (
            <div className="candidate-disclaimer-box" role="note">
              <span className="candidate-disclaimer-icon" aria-hidden="true">⚠️</span>
              <div className="candidate-disclaimer-text">
                <strong>STRICT EXECUTION BOUNDARY &bull; NOT YET EXECUTED</strong>
                {isSqlEdited
                  ? 'This SQL has been modified by the user. It will be independently validated and executed in read-only mode by the local agent daemon upon your explicit request.'
                  : 'This candidate SQL statement was generated by AI and has NOT been executed against your database. Review the query carefully before executing.'}
              </div>
            </div>
          ) : null}

          {/* Generated, Edited, or Manual SQL Code Block */}
          {(isManualModeActive || (response?.status === 'generated' && Boolean(currentSql))) && (
            <div className="candidate-code-container">
              <div className="candidate-code-toolbar">
                <span className="candidate-code-label">
                  {isManualModeActive
                    ? `Manual ${dialectLabel} Statement:`
                    : isSqlEdited
                    ? `Edited ${dialectLabel} Statement:`
                    : `Candidate ${dialectLabel} Statement:`}
                </span>
                <div className="candidate-code-actions">
                  {isManualModeActive ? (
                    <>
                      {response?.candidate_sql && (
                        <button
                          type="button"
                          className="candidate-reset-sql-btn"
                          onClick={() => setIsManualModeActive(false)}
                          disabled={isExecuting}
                        >
                          Switch to AI Candidate
                        </button>
                      )}
                      <button
                        type="button"
                        className="candidate-clear-manual-btn"
                        onClick={() => handleSqlTextChange('')}
                        disabled={isExecuting || !currentSql}
                      >
                        Clear SQL
                      </button>
                      <button
                        type="button"
                        className="candidate-copy-btn"
                        onClick={handleCopySql}
                        aria-label="Copy manual SQL to clipboard"
                      >
                        {copied ? '✓ Copied!' : 'Copy SQL'}
                      </button>
                      <button
                        type="button"
                        className="candidate-close-manual-btn"
                        onClick={() => setIsManualModeActive(false)}
                        disabled={isExecuting}
                      >
                        Close Custom SQL
                      </button>
                    </>
                  ) : (
                    <>
                      {isSqlEdited && (
                        <button
                          type="button"
                          className="candidate-reset-sql-btn"
                          onClick={handleResetSql}
                          disabled={isExecuting}
                        >
                          Reset to AI Candidate
                        </button>
                      )}
                      <button
                        type="button"
                        className="candidate-edit-toggle-btn"
                        onClick={() => setIsEditing(!isEditing)}
                        disabled={isExecuting}
                      >
                        {isEditing ? 'Done Editing' : 'Edit SQL'}
                      </button>
                      <button
                        type="button"
                        className="candidate-copy-btn"
                        onClick={handleCopySql}
                        aria-label="Copy candidate SQL to clipboard"
                      >
                        {copied ? '✓ Copied!' : 'Copy SQL'}
                      </button>
                    </>
                  )}
                </div>
              </div>

              {isManualModeActive ? (
                <div className="candidate-edit-area-wrapper">
                  <textarea
                    id="manual-sql-input"
                    className="candidate-edit-textarea candidate-manual-textarea"
                    value={currentSql}
                    onChange={(e) => handleSqlTextChange(e.target.value)}
                    disabled={isExecuting}
                    rows={6}
                    placeholder={`Enter a read-only ${dialectLabel} query (e.g. SELECT * FROM ...) ...`}
                    aria-label={`Manual ${dialectLabel} SQL query`}
                  />
                  <span className="candidate-edit-hint">
                    Manually entered queries will be validated for read-only safety by the local agent daemon before execution.
                  </span>
                </div>
              ) : isEditing ? (
                <div className="candidate-edit-area-wrapper">
                  <textarea
                    className="candidate-edit-textarea"
                    value={currentSql}
                    onChange={(e) => handleSqlTextChange(e.target.value)}
                    disabled={isExecuting}
                    rows={5}
                    aria-label="Edit candidate SQL statement"
                  />
                  <span className="candidate-edit-hint">
                    Editing SQL clears prior execution results and triggers full server-side revalidation upon execution.
                  </span>
                </div>
              ) : (
                <pre className="candidate-code-block" tabIndex={0}>
                  <code>{currentSql}</code>
                </pre>
              )}

              {/* Explicit Execution Action Bar */}
              <div className="candidate-execution-bar">
                <div className="candidate-execution-actions">
                  {isExecuting ? (
                    <div className="candidate-executing-state">
                      <span className="spinner" aria-hidden="true" />
                      <span>Executing read-only query on {activeProfileName || 'database'}...</span>
                      {onCancelExecute && (
                        <button
                          type="button"
                          className="candidate-cancel-btn"
                          onClick={onCancelExecute}
                        >
                          Cancel Execution
                        </button>
                      )}
                    </div>
                  ) : (
                    <>
                      <button
                        type="button"
                        className="candidate-execute-btn"
                        disabled={!canExecuteQuery}
                        onClick={handleExecuteClick}
                        aria-label="Execute SQL query on database"
                      >
                        <span aria-hidden="true">▶</span>
                        <span>Execute Query on Database</span>
                      </button>

                      {!isManualModeActive && isStale && (
                        <span className="candidate-execute-blocked-hint">
                          ⚠️ Execution blocked: question modified. Generate a new candidate first.
                        </span>
                      )}
                    </>
                  )}
                </div>

                <p className="candidate-execute-security-note">
                  🛡️ <strong>Safety Guarantee:</strong> Local agent daemon independently validates AST, enforces read-only mode, checks allowlists, and applies row/time bounds.
                </p>
              </div>
            </div>
          )}

          {/* Clarification Needed Message and Interactive Disambiguation Options */}
          {!isManualModeActive && response && response.status === 'clarification_needed' && (
            <div
              className="candidate-outcome-card candidate-outcome-clarification"
              role="region"
              aria-label="Clarification Request"
            >
              <div className="candidate-clarification-header">
                <strong>Clarification Requested:</strong>
                <p className="candidate-clarification-prompt">
                  {response.clarification_prompt || 'Clarification is needed to answer this question accurately.'}
                </p>
              </div>

              {Array.isArray(response.clarification_options) && response.clarification_options.length > 0 && (
                <div className="candidate-clarification-options-section">
                  <h4 className="candidate-clarification-options-title">
                    Select an option to clarify your question:
                  </h4>
                  <div
                    className="candidate-clarification-options-list"
                    role="group"
                    aria-label="Clarification Options"
                  >
                    {response.clarification_options.map((option) => {
                      const optionId = option.option_id || 'option'
                      const isSelected = selectedOptionId === option.option_id
                      return (
                        <button
                          key={optionId}
                          type="button"
                          className={`candidate-clarification-option-btn ${
                            isSelected ? 'candidate-clarification-option-selected' : ''
                          }`}
                          onClick={() => handleSelectClarificationOption(option)}
                          disabled={isLoading || isExecuting}
                          aria-label={`Select option ${optionId}: ${option.description || ''}`}
                          aria-pressed={isSelected}
                        >
                          <span className="candidate-clarification-option-badge">{optionId}</span>
                          <div className="candidate-clarification-option-details">
                            <span className="candidate-clarification-option-desc">{option.description || ''}</span>
                            {option.suggested_question ? (
                              <span className="candidate-clarification-option-suggested">
                                Suggested query: <em>&ldquo;{option.suggested_question}&rdquo;</em>
                              </span>
                            ) : (
                              <span className="candidate-clarification-guidance-note">
                                Guidance: Keep current question and review/refine with this interpretation.
                              </span>
                            )}
                          </div>
                          <span className="candidate-clarification-option-arrow" aria-hidden="true">&rarr;</span>
                        </button>
                      )
                    })}
                  </div>
                  <p className="candidate-clarification-help-text">
                    Selecting an option updates the question above for your review. You can edit it before clicking &quot;Generate Candidate SQL&quot;.
                  </p>
                </div>
              )}
            </div>
          )}

          {/* Unsupported Question Message */}
          {!isManualModeActive && response && response.status === 'unsupported' && response.unsupported_reason && (
            <div className="candidate-outcome-card candidate-outcome-unsupported">
              <strong>Unsupported Query:</strong>
              <p>{response.unsupported_reason}</p>
            </div>
          )}

          {/* Structured Generation Failure Outcome Card */}
          {!isManualModeActive && response && response.status === 'generation_failed' && (
            <div
              className={`candidate-outcome-card candidate-outcome-failed ${
                (response.failure_diagnostic?.classification || response.failure_classification) === 'quota_exhausted'
                  ? 'candidate-outcome-quota'
                  : ''
              }`}
              role="alert"
              aria-label="Generation Failure Notice"
            >
              <div className="candidate-failure-header">
                <span className="candidate-failure-icon" aria-hidden="true">
                  {(response.failure_diagnostic?.classification || response.failure_classification) === 'quota_exhausted'
                    ? '⏳'
                    : '⚠️'}
                </span>
                <strong className="candidate-failure-title">
                  {(response.failure_diagnostic?.classification || response.failure_classification) === 'quota_exhausted'
                    ? 'AI Provider Quota Exhausted'
                    : (response.failure_diagnostic?.classification || response.failure_classification) === 'authentication_failed'
                    ? 'AI Provider Authentication Failed'
                    : (response.failure_diagnostic?.classification || response.failure_classification) === 'provider_unavailable'
                    ? 'AI Provider Temporarily Unavailable'
                    : (response.failure_diagnostic?.classification || response.failure_classification) === 'network_error'
                    ? 'Network Connection Error'
                    : 'Candidate SQL Generation Failed'}
                </strong>
              </div>

              <p className="candidate-failure-message">
                {response.failure_diagnostic?.message ||
                  response.failure_message ||
                  'Candidate SQL generation could not be completed.'}
              </p>

              {(response.failure_diagnostic?.retry_guidance || response.retry_guidance) && (
                <div className="candidate-retry-guidance" role="note">
                  <span className="candidate-guidance-icon" aria-hidden="true">💡</span>
                  <span className="candidate-guidance-text">
                    <strong>Guidance:</strong>{' '}
                    {response.failure_diagnostic?.retry_guidance || response.retry_guidance}
                  </span>
                </div>
              )}

              <div className="candidate-failure-actions">
                <button
                  type="button"
                  className="candidate-retry-btn"
                  onClick={onSubmit}
                  disabled={isLoading || isExecuting}
                  aria-label="Retry candidate SQL generation"
                >
                  {isLoading ? 'Generating Candidate SQL...' : 'Retry Generation'}
                </button>
              </div>
            </div>
          )}

          {!isManualModeActive && response && response.status === 'generated' && response.execution_disclaimer && (
            <p className="candidate-footer-disclaimer">
              {response.execution_disclaimer}
            </p>
          )}
        </div>
      )}
    </div>
  )
}
