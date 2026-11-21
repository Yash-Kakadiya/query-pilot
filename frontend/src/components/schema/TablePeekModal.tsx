import { useState, useEffect } from 'react'
import { fetchTablePeek } from '../../services/tablePeek.ts'
import type { TablePeekResponse } from '../../types/api.ts'
import './TablePeekModal.css'

export interface TablePeekModalProps {
  isOpen: boolean
  onClose: () => void
  profileId: string
  tableName: string
  schemaName?: string
  onAskQuestion?: (prompt: string) => void
}

export function TablePeekModal({
  isOpen,
  onClose,
  profileId,
  tableName,
  schemaName,
  onAskQuestion,
}: TablePeekModalProps) {
  const [loading, setLoading] = useState<boolean>(true)
  const [error, setError] = useState<string | null>(null)
  const [data, setData] = useState<TablePeekResponse | null>(null)
  const [copiedFormat, setCopiedFormat] = useState<'md' | 'json' | null>(null)

  useEffect(() => {
    if (!isOpen || !profileId || !tableName) return

    let isMounted = true
    setLoading(true)
    setError(null)
    setData(null)

    fetchTablePeek(profileId, tableName, 5)
      .then((res) => {
        if (isMounted) {
          setData(res)
          setLoading(false)
        }
      })
      .catch((err: unknown) => {
        if (isMounted) {
          setError(err instanceof Error ? err.message : 'Failed to peek table data.')
          setLoading(false)
        }
      })

    return () => {
      isMounted = false
    }
  }, [isOpen, profileId, tableName])

  if (!isOpen) return null

  const handleCopyMarkdown = () => {
    if (!data || !data.columns.length) return
    const headerLine = `| ${data.columns.join(' | ')} |`
    const sepLine = `| ${data.columns.map(() => '---').join(' | ')} |`
    const rowsLines = data.rows.map((row) => {
      const vals = data.columns.map((c) => {
        const v = row[c]
        return v === null || v === undefined ? 'NULL' : String(v).replace(/\|/g, '\\|')
      })
      return `| ${vals.join(' | ')} |`
    })
    const md = [headerLine, sepLine, ...rowsLines].join('\n')
    navigator.clipboard.writeText(md)
    setCopiedFormat('md')
    setTimeout(() => setCopiedFormat(null), 2000)
  }

  const handleCopyJson = () => {
    if (!data) return
    navigator.clipboard.writeText(JSON.stringify(data.rows, null, 2))
    setCopiedFormat('json')
    setTimeout(() => setCopiedFormat(null), 2000)
  }

  const handleAsk = () => {
    if (onAskQuestion) {
      onAskQuestion(`Show me the recent records and distribution of data in the ${tableName} table.`)
      onClose()
    }
  }

  return (
    <div
      className="table-peek-overlay"
      role="dialog"
      aria-modal="true"
      aria-labelledby="peek-modal-title"
      onClick={onClose}
    >
      <div className="table-peek-card" onClick={(e) => e.stopPropagation()}>
        {/* Header */}
        <div className="table-peek-header">
          <div className="peek-title-group">
            <span className="peek-icon" aria-hidden="true">👁️</span>
            <div>
              <h3 id="peek-modal-title" className="peek-title">
                Table Preview: <code>{tableName}</code>
              </h3>
              <div className="peek-meta-tags">
                {schemaName && <span className="peek-tag schema-tag">Schema: {schemaName}</span>}
                {data && (
                  <>
                    <span className="peek-tag count-tag">{data.row_count} rows (first 5 limit)</span>
                    <span className="peek-tag latency-tag">{Math.round(data.execution_time_ms)}ms</span>
                  </>
                )}
              </div>
            </div>
          </div>
          <button
            type="button"
            className="peek-close-btn"
            onClick={onClose}
            aria-label="Close table preview"
          >
            ✕
          </button>
        </div>

        {/* Content Area */}
        <div className="table-peek-body">
          {loading && (
            <div className="peek-status-state" role="status">
              <span className="peek-spinner" aria-hidden="true" />
              <span>Fetching first 5 rows with read-only safeguards...</span>
            </div>
          )}

          {error && (
            <div className="peek-status-state peek-error" role="alert">
              <span aria-hidden="true">⚠️</span>
              <span>{error}</span>
            </div>
          )}

          {!loading && !error && data && data.rows.length === 0 && (
            <div className="peek-status-state peek-empty" role="status">
              <span aria-hidden="true">📭</span>
              <span>This table is currently empty (0 records found).</span>
            </div>
          )}

          {!loading && !error && data && data.rows.length > 0 && (
            <div className="peek-table-container">
              <table className="peek-data-table">
                <thead>
                  <tr>
                    {data.columns.map((col) => (
                      <th key={col} scope="col">
                        <span className="peek-col-name">{col}</span>
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {data.rows.map((row, rIdx) => (
                    <tr key={rIdx}>
                      {data.columns.map((col) => {
                        const val = row[col]
                        const isRedacted = val === '[REDACTED]'
                        const isNull = val === null || val === undefined
                        return (
                          <td
                            key={col}
                            className={`peek-cell ${isRedacted ? 'cell-redacted' : ''} ${isNull ? 'cell-null' : ''}`}
                          >
                            {isRedacted ? (
                              <span className="redacted-badge">🔒 Redacted</span>
                            ) : isNull ? (
                              <span className="null-badge">NULL</span>
                            ) : typeof val === 'object' ? (
                              JSON.stringify(val)
                            ) : (
                              String(val)
                            )}
                          </td>
                        )
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>

        {/* Actions Footer */}
        <div className="table-peek-footer">
          <div className="peek-footer-left">
            <button
              type="button"
              className="peek-action-btn"
              onClick={handleCopyMarkdown}
              disabled={loading || !data || data.rows.length === 0}
              title="Copy as Markdown table"
            >
              {copiedFormat === 'md' ? '✓ Copied Markdown' : '📋 Copy Markdown'}
            </button>
            <button
              type="button"
              className="peek-action-btn"
              onClick={handleCopyJson}
              disabled={loading || !data || data.rows.length === 0}
              title="Copy rows as JSON"
            >
              {copiedFormat === 'json' ? '✓ Copied JSON' : '📋 Copy JSON'}
            </button>
          </div>
          <div className="peek-footer-right">
            {onAskQuestion && (
              <button
                type="button"
                className="peek-ask-btn"
                onClick={handleAsk}
                disabled={loading}
              >
                💬 Ask Query About Table
              </button>
            )}
            <button type="button" className="peek-done-btn" onClick={onClose}>
              Done
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
