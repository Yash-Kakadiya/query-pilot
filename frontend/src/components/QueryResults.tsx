import type { KnownPipelineStatus, QueryResponse } from '../types/api'

interface QueryResultsProps {
  response: QueryResponse
}

export function QueryResults({ response }: QueryResultsProps) {
  const status = response.status

  const isKnownStatus = (s: string): s is KnownPipelineStatus => {
    return [
      'SUCCESS',
      'UNSUPPORTED',
      'CLARIFICATION_NEEDED',
      'VALIDATION_REJECTED',
      'GENERATION_FAILED',
      'EXECUTION_FAILED',
    ].includes(s)
  }

  const getStatusBadgeClass = (s: string) => {
    switch (s) {
      case 'SUCCESS':
        return 'badge-success'
      case 'UNSUPPORTED':
        return 'badge-info'
      case 'CLARIFICATION_NEEDED':
        return 'badge-warning'
      case 'VALIDATION_REJECTED':
      case 'GENERATION_FAILED':
      case 'EXECUTION_FAILED':
        return 'badge-danger'
      default:
        return 'badge-neutral'
    }
  }

  const formatCellValue = (val: unknown): string => {
    if (val === null || val === undefined) {
      return '—'
    }
    if (typeof val === 'boolean') {
      return val ? 'true' : 'false'
    }
    if (typeof val === 'bigint') {
      return val.toString()
    }
    if (typeof val === 'object') {
      try {
        return JSON.stringify(val)
      } catch {
        return String(val)
      }
    }
    return String(val)
  }

  // Safe collections
  const columns = Array.isArray(response.columns) ? response.columns : []
  const rows = Array.isArray(response.rows) ? response.rows : []
  const clarificationOptions = Array.isArray(response.clarification_options)
    ? response.clarification_options
    : []
  const validationReasons = Array.isArray(response.validation_reasons)
    ? response.validation_reasons
    : []
  const verificationNotes = Array.isArray(response.grounded_answer?.verification_notes)
    ? response.grounded_answer.verification_notes
    : []

  const hasScalar =
    response.scalar_value !== null && response.scalar_value !== undefined

  return (
    <div className="query-results" role="region" aria-label="Query Results">
      {/* Result Metadata Header */}
      <div className="results-header">
        <div className="status-pill-group">
          <span className={`status-badge ${getStatusBadgeClass(status)}`}>
            {status}
          </span>
          {response.request_id && (
            <span className="request-id-badge">
              Request ID: <code>{response.request_id}</code>
            </span>
          )}
        </div>

        {status === 'SUCCESS' && rows.length > 0 && (
          <span className="row-count-summary">
            {response.row_count} {response.row_count === 1 ? 'row' : 'rows'}
          </span>
        )}
      </div>

      {/* 1. SUCCESS Status */}
      {status === 'SUCCESS' && (
        <div className="success-outcome">
          {/* Grounded Answer Card */}
          {response.grounded_answer && (
            <div className="grounded-answer-card">
              <div className="grounded-header">
                <span className="grounded-title">Synthesized Answer</span>
                <span
                  className={`verification-tag ${
                    response.grounded_answer.is_mechanically_verified
                      ? 'verified-deterministic'
                      : 'verified-llm'
                  }`}
                >
                  {response.grounded_answer.is_mechanically_verified
                    ? 'Deterministic Template'
                    : 'LLM Synthesized'}
                </span>
              </div>
              <p className="grounded-text">{response.grounded_answer.text}</p>
              {verificationNotes.length > 0 && (
                <ul className="verification-notes-list">
                  {verificationNotes.map((note, idx) => (
                    <li key={idx} className="verification-note-item">
                      {note}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}

          {/* Scalar Value Card */}
          {hasScalar && (
            <div className="scalar-card">
              <div className="scalar-label">
                {columns[0] || 'Result Value'}
              </div>
              <div className="scalar-value">
                {formatCellValue(response.scalar_value)}
              </div>
            </div>
          )}

          {/* Tabular Data */}
          {columns.length > 0 && (
            <div className="table-wrapper">
              {response.truncated && (
                <div className="truncation-warning" role="alert">
                  ⚠️ Results truncated: maximum row limit reached.
                </div>
              )}

              {rows.length === 0 ? (
                <div className="empty-results-box">
                  No matching records returned.
                </div>
              ) : (
                <table className="results-table">
                  <caption className="sr-only">Query output tabular results</caption>
                  <thead>
                    <tr>
                      {columns.map((col) => (
                        <th key={col} scope="col">
                          {col}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((row, rowIdx) => (
                      <tr key={rowIdx}>
                        {columns.map((col) => (
                          <td key={`${rowIdx}-${col}`}>
                            {formatCellValue(row[col])}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          )}

          {/* Empty Outcome Fallback if all data fields are empty */}
          {!response.grounded_answer && !hasScalar && rows.length === 0 && (
            <div className="empty-results-box">
              {response.message || 'Query completed successfully. No matching records found.'}
            </div>
          )}
        </div>
      )}

      {/* 2. UNSUPPORTED Status */}
      {status === 'UNSUPPORTED' && (
        <div className="outcome-card outcome-unsupported">
          <div className="outcome-icon" aria-hidden="true">ℹ️</div>
          <div className="outcome-content">
            <h4 className="outcome-title">Question Outside Database Scope</h4>
            <p className="outcome-message">
              {response.explanation ||
                response.message ||
                'This question cannot be answered using the available academic database schema.'}
            </p>
          </div>
        </div>
      )}

      {/* 3. CLARIFICATION_NEEDED Status */}
      {status === 'CLARIFICATION_NEEDED' && (
        <div className="outcome-card outcome-clarification">
          <div className="outcome-icon" aria-hidden="true">💬</div>
          <div className="outcome-content">
            <h4 className="outcome-title">Clarification Needed</h4>
            <p className="clarification-prompt-text">
              {response.clarification_prompt ||
                response.message ||
                'Clarification is needed to answer this question accurately.'}
            </p>

            {clarificationOptions.length > 0 ? (
              <div className="clarification-options-section">
                <p className="options-subtitle">
                  Available disambiguation choices (read-only):
                </p>
                <ul className="clarification-options-list">
                  {clarificationOptions.map((opt) => (
                    <li key={opt.option_id} className="clarification-option-item">
                      <div className="option-desc">{opt.description}</div>
                      {opt.suggested_question && (
                        <div className="option-suggested">
                          Suggested query: <em>"{opt.suggested_question}"</em>
                        </div>
                      )}
                    </li>
                  ))}
                </ul>
              </div>
            ) : (
              <p className="options-subtitle">
                No specific choices were generated. Please rephrase your question with additional detail.
              </p>
            )}
          </div>
        </div>
      )}

      {/* 4. VALIDATION_REJECTED Status */}
      {status === 'VALIDATION_REJECTED' && (
        <div className="outcome-card outcome-rejected">
          <div className="outcome-icon" aria-hidden="true">🛡️</div>
          <div className="outcome-content">
            <h4 className="outcome-title">Query Safety Validation Rejected</h4>
            <p className="outcome-message">
              {response.message ||
                'The generated query was rejected by database security and validation guardrails.'}
            </p>

            {validationReasons.length > 0 && (
              <ul className="validation-reasons-list">
                {validationReasons.map((reason, idx) => (
                  <li key={idx} className="validation-reason-item">
                    <span className="reason-code">{reason.code}</span>
                    <span className="reason-msg">{reason.message}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      )}

      {/* 5. GENERATION_FAILED & EXECUTION_FAILED */}
      {(status === 'GENERATION_FAILED' || status === 'EXECUTION_FAILED') && (
        <div className="outcome-card outcome-failure">
          <div className="outcome-icon" aria-hidden="true">⚠️</div>
          <div className="outcome-content">
            <h4 className="outcome-title">
              {status === 'GENERATION_FAILED'
                ? 'Query Generation Failed'
                : 'Query Execution Failed'}
            </h4>
            <p className="outcome-message">
              {response.message ||
                response.explanation ||
                'The query pipeline encountered an unexpected processing failure.'}
            </p>
          </div>
        </div>
      )}

      {/* 6. Unknown Status Fallback */}
      {!isKnownStatus(status) && (
        <div className="outcome-card outcome-neutral">
          <div className="outcome-icon" aria-hidden="true">❓</div>
          <div className="outcome-content">
            <h4 className="outcome-title">Unrecognized Outcome Status</h4>
            <p className="outcome-message">
              The server returned an unrecognized status: <code>{status}</code>.
            </p>
          </div>
        </div>
      )}

      {/* Generated SQL Disclosure */}
      <details className="sql-disclosure">
        <summary className="sql-disclosure-summary">
          Generated SQL Query
        </summary>
        <div className="sql-disclosure-body">
          {response.generated_sql ? (
            <pre className="sql-code-block">
              <code>{response.generated_sql}</code>
            </pre>
          ) : (
            <p className="sql-policy-notice">
              Generated SQL is excluded from public responses by server safety policy.
            </p>
          )}
        </div>
      </details>
    </div>
  )
}
