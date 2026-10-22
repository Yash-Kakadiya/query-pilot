import React, { useMemo } from 'react'
import type { ExecuteQueryResponse } from '../types/api.ts'
import {
  synthesizeDeterministicAnswer,
  resolveCellPresentation,
} from '../services/answerSynthesis.ts'
import './LocalExecutionResults.css'

export interface LocalExecutionResultsProps {
  isLoading: boolean
  result: ExecuteQueryResponse | null
  error: Error | null
  isAmbiguousFailure?: boolean
  isConfirmedTimeout?: boolean
  activeProfileName?: string
  onClear: () => void
  onRetry?: () => void
}

/**
 * Renders serialized cell values safely into user-facing text.
 * Strictly distinguishes an absent field (undefined / missing property) from an explicit database NULL.
 */
function renderCellValue(value: unknown): React.ReactNode {
  const presentation = resolveCellPresentation(value)

  if (presentation.kind === 'missing') {
    return <span className="cell-missing" aria-label="Missing field">{presentation.text}</span>
  }
  if (presentation.kind === 'null') {
    return <span className="cell-null" aria-label="Null value">{presentation.text}</span>
  }
  if (presentation.kind === 'bool') {
    return <span className="cell-bool">{presentation.text}</span>
  }
  if (presentation.kind === 'json') {
    return <span className="cell-json">{presentation.text}</span>
  }
  return presentation.text
}

/**
 * Accessible Component displaying safe local database query execution outcomes.
 *
 * Security Guarantee:
 * - Result rows and cell values are rendered strictly inside the local browser DOM.
 * - Raw database records are NEVER forwarded to external APIs, telemetry, or model providers.
 */
export function LocalExecutionResults({
  isLoading,
  result,
  error,
  isAmbiguousFailure,
  isConfirmedTimeout,
  activeProfileName,
  onClear,
  onRetry,
}: LocalExecutionResultsProps) {
  const synthesizedAnswer = useMemo(() => {
    if (isLoading) {
      return null
    }
    return synthesizeDeterministicAnswer(result, {
      error,
      isAmbiguousFailure,
      isConfirmedTimeout,
    })
  }, [result, error, isLoading, isAmbiguousFailure, isConfirmedTimeout])

  if (!isLoading && !result && !error) {
    return null
  }

  return (
    <div
      className="local-execution-card"
      role="region"
      aria-label="Local Database Query Results"
    >
      <div className="local-results-header">
        <div className="local-results-title-group">
          <span className="local-results-icon" aria-hidden="true">📊</span>
          <div>
            <h3 className="local-results-title">Local Execution Results</h3>
            <p className="local-results-subtitle">
              Direct results from <strong>{activeProfileName || 'the selected profile database'}</strong>.
            </p>
          </div>
        </div>

        {(!isLoading && (result || error)) && (
          <button
            type="button"
            className="local-results-dismiss-btn"
            onClick={onClear}
            aria-label="Dismiss execution results"
          >
            Dismiss Results
          </button>
        )}
      </div>

      {/* Execution in progress */}
      {isLoading && (
        <div className="local-executing-indicator" role="status">
          <span className="spinner" aria-hidden="true" />
          <span>Executing read-only query on database...</span>
        </div>
      )}

      {/* Error state */}
      {error && !isLoading && (
        <div className="local-error-card" role="alert">
          <div className="local-error-header">
            <span aria-hidden="true">⚠️</span>
            <strong>Query Execution Failed</strong>
          </div>
          <p className="local-error-message">{error.message}</p>

          {isConfirmedTimeout && (
            <div className="local-timeout-notice" role="note">
              <span aria-hidden="true">⏱️</span>
              <span>
                <strong>Statement Timed Out:</strong> The database engine halted query execution because it exceeded
                the configured profile timeout limit. The connection was cleanly closed and discarded.
              </span>
            </div>
          )}

          {isAmbiguousFailure && (
            <div className="local-ambiguous-notice" role="note">
              <span aria-hidden="true">ℹ️</span>
              <span>
                <strong>Outcome Uncertain:</strong> A network disruption occurred before a response was received from the local daemon.
                Query outcome cannot be verified. Automatic re-execution was prevented.
              </span>
            </div>
          )}

          {onRetry && (
            <div className="local-error-actions">
              <button
                type="button"
                className="local-retry-btn"
                onClick={onRetry}
              >
                Retry Execution
              </button>
            </div>
          )}
        </div>
      )}

      {/* Success result presentation */}
      {result && !isLoading && (
        <div className="local-results-content">
          {/* Metadata badges and summary */}
          <div className="local-metadata-bar">
            <div className="local-badge-cluster">
              <span className="local-stat-chip local-chip-rows">
                <strong>{result.row_count}</strong> {result.row_count === 1 ? 'row' : 'rows'}
              </span>
              <span className="local-stat-chip local-chip-time">
                ⏱ {result.execution_time_ms.toFixed(1)} ms
              </span>
              {result.truncated && (
                <span className="local-stat-chip local-chip-truncated" role="status">
                  ⚠️ Result Truncated (Profile Limit)
                </span>
              )}
            </div>

            {result.request_id && (
              <span className="local-request-id">
                ID: <code>{result.request_id}</code>
              </span>
            )}
          </div>

          {/* Privacy Boundary Banner */}
          <div className="local-privacy-notice" role="note">
            <span className="local-privacy-icon" aria-hidden="true">🔒</span>
            <span>
              <strong>LOCAL RESULTS ONLY:</strong> Database records are stored only in your local browser memory
              and are never sent to hosted APIs, Gemini, or remote servers.
            </span>
          </div>

          {/* Deterministic Local Answer Presentation */}
          {synthesizedAnswer && synthesizedAnswer.kind !== 'error' && (
            <div
              className="local-synthesized-card"
              role="region"
              aria-label="Deterministic Answer Based on Local Results"
            >
              <div className="local-synthesized-header">
                <div className="local-synthesized-title-group">
                  <span className="local-synthesized-icon" aria-hidden="true">💡</span>
                  <span className="local-synthesized-badge">
                    Deterministic answer — based on local results
                  </span>
                </div>
                {synthesizedAnswer.isTruncated && (
                  <span className="local-synthesized-partial-chip" role="status">
                    Partial Result Set
                  </span>
                )}
              </div>

              <div className="local-synthesized-body">
                <p className="local-synthesized-text">{synthesizedAnswer.text}</p>

                {/* Display structured fields for single-row or single-cell results */}
                {synthesizedAnswer.fields && synthesizedAnswer.fields.length > 0 && (
                  <div className="local-synthesized-fields" aria-label="Returned Fields">
                    {synthesizedAnswer.fields.map((field, idx) => (
                      <div key={idx} className="local-synthesized-field-badge">
                        <span className="local-field-key">{field.label}:</span>
                        <span className={`local-field-val ${field.isNull ? 'cell-null' : ''}`}>
                          {field.value}
                        </span>
                      </div>
                    ))}
                  </div>
                )}

                {/* Derived local statistics for multi-row results */}
                {synthesizedAnswer.stats && synthesizedAnswer.stats.columns.length > 0 && (
                  <div className="local-synthesized-stats" aria-label="Local Column Statistics">
                    <div className="local-stats-notice" role="note">
                      <span aria-hidden="true">📊</span>
                      <span>{synthesizedAnswer.stats.scopeNotice}</span>
                    </div>
                    <div className="local-stats-grid">
                      {synthesizedAnswer.stats.columns.map((colStat, idx) => (
                        <div key={idx} className="local-stat-col-card">
                          <span className="local-stat-col-name">{colStat.column}</span>
                          <div className="local-stat-col-metrics">
                            <span>Min: <strong>{colStat.min}</strong></span>
                            <span>Max: <strong>{colStat.max}</strong></span>
                            {colStat.avg !== null && <span>Avg: <strong>{colStat.avg}</strong></span>}
                            {colStat.sum !== null && <span>Sum: <strong>{colStat.sum}</strong></span>}
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            </div>
          )}

          {/* Data Table */}
          {result.rows.length > 0 ? (
            <div className="local-table-wrapper" tabIndex={0} aria-label="Query result data table">
              <table className="local-data-table">
                <thead>
                  <tr>
                    {result.columns.map((col, colIdx) => (
                      <th key={colIdx} scope="col">
                        {col}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {result.rows.map((row, rowIdx) => (
                    <tr key={rowIdx}>
                      {result.columns.map((col, colIdx) => (
                        <td key={colIdx}>
                          {renderCellValue(row[col])}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <div className="local-empty-results">
              <span aria-hidden="true">🔍</span>
              <p>Query executed successfully, but returned 0 rows.</p>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

