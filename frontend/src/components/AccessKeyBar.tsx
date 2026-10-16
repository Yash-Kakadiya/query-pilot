import { useState } from 'react'
import { getSessionAccessKey, setSessionAccessKey } from '../services/apiClient'
import './AccessKeyBar.css'

interface AccessKeyBarProps {
  /** Notified when the access key changes in session storage. */
  onKeyChange?: (hasKey: boolean) => void
  /** Indicates whether the last query failed due to authorization (HTTP 401). */
  authRequired?: boolean
}

/**
 * Access Key management bar for private evaluation prototypes (Task 1.34-B1-F14).
 *
 * Allows evaluators to enter and persist a shared prototype access key in browser
 * sessionStorage without baking secrets into client bundles.
 */
export function AccessKeyBar({ onKeyChange, authRequired }: AccessKeyBarProps) {
  const [currentKey, setCurrentKey] = useState<string>(() => getSessionAccessKey())
  const [inputVal, setInputVal] = useState<string>('')
  const [isExpanded, setIsExpanded] = useState<boolean>(Boolean(authRequired && !currentKey))
  const [saveMessage, setSaveMessage] = useState<string | null>(null)

  const hasKey = Boolean(currentKey.trim())

  const handleSave = (e: React.FormEvent) => {
    e.preventDefault()
    const trimmed = inputVal.trim()
    if (!trimmed) {
      return
    }

    setSessionAccessKey(trimmed)
    setCurrentKey(trimmed)
    setInputVal('')
    setSaveMessage('Access key saved for this session.')
    onKeyChange?.(true)

    setTimeout(() => {
      setSaveMessage(null)
    }, 3000)
  }

  const handleClear = () => {
    setSessionAccessKey(null)
    setCurrentKey('')
    setInputVal('')
    setSaveMessage('Access key cleared.')
    onKeyChange?.(false)

    setTimeout(() => {
      setSaveMessage(null)
    }, 3000)
  }

  return (
    <div
      className={`access-key-bar ${authRequired && !hasKey ? 'access-key-bar-alert' : ''}`}
      role="region"
      aria-label="Prototype Access Authorization"
    >
      <div className="access-key-summary">
        <span className="access-key-icon" aria-hidden="true">🔑</span>
        <span className="access-key-label">Evaluation Access Key:</span>
        <span
          className={`access-key-badge ${hasKey ? 'badge-active' : 'badge-inactive'}`}
        >
          {hasKey ? 'Active (Session)' : 'Not Set'}
        </span>

        {authRequired && !hasKey && (
          <span className="access-key-warning">
            Access key required for query execution
          </span>
        )}

        <button
          type="button"
          className="access-key-toggle-btn"
          onClick={() => setIsExpanded(!isExpanded)}
          aria-expanded={isExpanded}
        >
          {isExpanded ? 'Hide' : hasKey ? 'Change Key' : 'Enter Key'}
        </button>
      </div>

      {isExpanded && (
        <form className="access-key-form" onSubmit={handleSave}>
          <div className="access-key-input-row">
            <input
              type="password"
              className="access-key-input"
              placeholder="Enter prototype access key..."
              value={inputVal}
              onChange={(e) => setInputVal(e.target.value)}
              aria-label="Private Prototype Access Key"
              autoComplete="off"
            />
            <button
              type="submit"
              className="access-key-save-btn"
              disabled={!inputVal.trim()}
            >
              Save Key
            </button>
            {hasKey && (
              <button
                type="button"
                className="access-key-clear-btn"
                onClick={handleClear}
              >
                Clear
              </button>
            )}
          </div>
          <p className="access-key-helptext">
            Stored in browser session storage only. Sent via <code>X-QueryPilot-Access-Key</code> header.
          </p>
        </form>
      )}

      {saveMessage && (
        <div className="access-key-feedback" role="status" aria-live="polite">
          {saveMessage}
        </div>
      )}
    </div>
  )
}
