import { useState } from 'react'
import type { ConversationTurnContext, GenerateSQLResponse } from '../types/api.ts'
import './CandidateSqlReview.css'

export interface CandidateSqlReviewProps {
  question: string
  isLoading: boolean
  response: GenerateSQLResponse | null
  error: Error | null
  isStale: boolean
  activeProfileName?: string
  onQuestionChange: (newQuestion: string) => void
  onSubmit: () => void
  onCancel: () => void
  onClear: () => void

  // Execution additions (Task 1.34-B1-F28-A9-B7)
  reviewedSql?: string
  onReviewedSqlChange?: (newSql: string) => void
  onExecuteSql?: (sql: string) => void
  onCancelExecute?: () => void
  isExecuting?: boolean
  canExecute?: boolean

  // Multi-turn additions (Milestone B12-F12B)
  conversationContext?: ConversationTurnContext[]
  onResetConversation?: () => void
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
}: CandidateSqlReviewProps) {
  const [copied, setCopied] = useState<boolean>(false)
  const [isEditing, setIsEditing] = useState<boolean>(false)
  const [prevCandidateSql, setPrevCandidateSql] = useState<string | null | undefined>(response?.candidate_sql)
  const [localSql, setLocalSql] = useState<string>(response?.candidate_sql || '')

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
    if (onExecuteSql && currentSql.trim() && !isStale && !isExecuting) {
      onExecuteSql(currentSql.trim())
    }
  }

  const charCount = question.length
  const isOverLimit = charCount > 1000

  // Execution enablement invariants
  const canExecuteQuery =
    canExecute &&
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
            <span aria-hidden="true">⚠️</span>
            <span>
              Question was modified since candidate SQL was generated. Generate again to refresh the candidate before executing.
            </span>
          </div>
        )}

        <div className="candidate-form-actions">
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

      {/* Generation Error Alert with Retry */}
      {error && !isLoading && (
        <div className="candidate-error-alert" role="alert">
          <div className="candidate-error-text">
            <span aria-hidden="true">⚠️</span>
            <span>{error.message || 'Failed to generate candidate SQL.'}</span>
          </div>
          <button
            type="button"
            className="candidate-retry-btn"
            onClick={onSubmit}
          >
            Retry Generation
          </button>
        </div>
      )}

      {/* Candidate SQL Presentation Panel */}
      {response && !isLoading && (
        <div className="candidate-review-panel">
          <div className="candidate-review-header">
            <div className="candidate-badge-group">
              {response.status === 'generated' && (
                <span className="candidate-status-badge candidate-badge-generated">
                  Candidate Generated
                </span>
              )}
              {isSqlEdited && (
                <span className="candidate-status-badge candidate-badge-edited">
                  Edited SQL (User Modified)
                </span>
              )}
              {response.status === 'clarification_needed' && (
                <span className="candidate-status-badge candidate-badge-clarification">
                  Clarification Needed
                </span>
              )}
              {response.status === 'unsupported' && (
                <span className="candidate-status-badge candidate-badge-unsupported">
                  Unsupported Question
                </span>
              )}
              {response.status === 'generation_failed' && (
                <span className="candidate-status-badge candidate-badge-failed">
                  Generation Failed
                </span>
              )}
            </div>

            {response.request_id && (
              <span className="candidate-request-id">
                Request ID: <code>{response.request_id}</code>
              </span>
            )}
          </div>

          {/* Prominent Safety Execution Boundary Disclaimer */}
          <div className="candidate-disclaimer-box" role="note">
            <span className="candidate-disclaimer-icon" aria-hidden="true">⚠️</span>
            <div className="candidate-disclaimer-text">
              <strong>STRICT EXECUTION BOUNDARY &bull; NOT YET EXECUTED</strong>
              {isSqlEdited
                ? 'This SQL has been modified by the user. It will be independently validated and executed in read-only mode by the local agent daemon upon your explicit request.'
                : 'This candidate SQL statement was generated by AI and has NOT been executed against your database. Review the query carefully before executing.'}
            </div>
          </div>

          {/* Generated or Edited SQL Code Block */}
          {response.status === 'generated' && currentSql && (
            <div className="candidate-code-container">
              <div className="candidate-code-toolbar">
                <span className="candidate-code-label">
                  {isSqlEdited ? 'Edited SQL Statement:' : 'Candidate T-SQL Statement:'}
                </span>
                <div className="candidate-code-actions">
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
                </div>
              </div>

              {isEditing ? (
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

                      {isStale && (
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

          {/* Clarification Needed Message */}
          {response.status === 'clarification_needed' && response.clarification_prompt && (
            <div className="candidate-outcome-card candidate-outcome-clarification">
              <strong>Clarification Requested:</strong>
              <p>{response.clarification_prompt}</p>
            </div>
          )}

          {/* Unsupported Question Message */}
          {response.status === 'unsupported' && response.unsupported_reason && (
            <div className="candidate-outcome-card candidate-outcome-unsupported">
              <strong>Unsupported Query:</strong>
              <p>{response.unsupported_reason}</p>
            </div>
          )}

          <p className="candidate-footer-disclaimer">
            {response.execution_disclaimer || 'Candidate SQL is unexecuted.'}
          </p>
        </div>
      )}
    </div>
  )
}
