import { useEffect, useRef, useState } from 'react'
import {
  MAX_BOOTSTRAP_TOKEN_LENGTH,
  MIN_BOOTSTRAP_TOKEN_LENGTH,
} from '../services/agentClient.ts'
import type { BootstrapResult } from '../services/agentSession.ts'
import './AgentAuthModal.css'

export interface AgentAuthModalProps {
  isOpen: boolean
  isSubmitting: boolean
  authError?: string
  onSubmit: (token: string) => Promise<BootstrapResult>
  onClose: () => void
}

/**
 * Accessible authentication dialog for exchanging single-use bootstrap tokens
 * with the QueryPilot local-agent daemon.
 *
 * Security & Ergonomics:
 * - Password-style masked input with optional visibility toggle.
 * - Transient input: token is wiped from React state immediately upon submission,
 *   regardless of success or failure.
 * - Never logs, serializes, or echoes the bootstrap token into error alerts or analytics.
 * - Duplicate submission prevention.
 * - Keyboard navigation (Escape to dismiss, Enter to submit, autofocus on open).
 */
export function AgentAuthModal({
  isOpen,
  isSubmitting,
  authError,
  onSubmit,
  onClose,
}: AgentAuthModalProps) {
  const [inputToken, setInputToken] = useState<string>('')
  const [showToken, setShowToken] = useState<boolean>(false)
  const [localError, setLocalError] = useState<string | null>(null)
  const [successMessage, setSuccessMessage] = useState<string | null>(null)

  const inputRef = useRef<HTMLInputElement>(null)

  // Focus input automatically upon modal appearance
  useEffect(() => {
    if (isOpen) {
      inputRef.current?.focus()
    }
  }, [isOpen])


  if (!isOpen) {
    return null
  }

  const effectiveError = localError || authError || null

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()

    // Prevent duplicate concurrent submission
    if (isSubmitting) {
      return
    }

    const candidate = inputToken.trim()

    // Immediately clear transient token from input state
    setInputToken('')
    setLocalError(null)

    // Client-side length and presence verification
    if (!candidate) {
      setLocalError('Bootstrap token cannot be empty.')
      return
    }

    if (
      candidate.length < MIN_BOOTSTRAP_TOKEN_LENGTH ||
      candidate.length > MAX_BOOTSTRAP_TOKEN_LENGTH
    ) {
      setLocalError(
        `Bootstrap token must be between ${MIN_BOOTSTRAP_TOKEN_LENGTH} and ${MAX_BOOTSTRAP_TOKEN_LENGTH} characters.`
      )
      return
    }

    try {
      const result = await onSubmit(candidate)
      if (result.success) {
        setSuccessMessage('Authenticated successfully. Establishing ambient session...')
        setTimeout(() => {
          setSuccessMessage(null)
          onClose()
        }, 500)
      } else if (result.error) {
        setLocalError(result.error)
      }
    } catch (err: unknown) {
      setLocalError(
        err instanceof Error ? err.message : 'Bootstrap authentication failed.'
      )
    } finally {
      // Ensure input is empty on both success and failure
      setInputToken('')
    }
  }

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Escape' && !isSubmitting) {
      onClose()
    }
  }

  return (
    <div
      className="agent-auth-backdrop"
      role="dialog"
      aria-modal="true"
      aria-labelledby="agent-auth-title"
      aria-describedby="agent-auth-desc"
      onKeyDown={handleKeyDown}
    >
      <div className="agent-auth-dialog">
        <div className="agent-auth-header">
          <div className="agent-auth-header-title">
            <span className="agent-auth-icon" aria-hidden="true">🔒</span>
            <h2 id="agent-auth-title" className="agent-auth-title">
              Authenticate Local Agent
            </h2>
          </div>
          <button
            type="button"
            className="agent-auth-close-btn"
            onClick={onClose}
            disabled={isSubmitting}
            aria-label="Close authentication dialog"
          >
            &times;
          </button>
        </div>

        <p id="agent-auth-desc" className="agent-auth-desc">
          The QueryPilot local agent requires authentication to inspect and execute queries against approved local database profiles.
          Read your single-use bootstrap token from the agent terminal output or from{' '}
          <code>~/.query_pilot/agent.token</code>.
        </p>

        {effectiveError && (
          <div className="agent-auth-alert agent-auth-alert-error" role="alert">
            <span className="agent-auth-alert-icon" aria-hidden="true">⚠️</span>
            <span className="agent-auth-alert-text">{effectiveError}</span>
          </div>
        )}

        {successMessage && (
          <div
            className="agent-auth-alert agent-auth-alert-success"
            role="status"
            aria-live="polite"
          >
            <span className="agent-auth-alert-icon" aria-hidden="true">✅</span>
            <span className="agent-auth-alert-text">{successMessage}</span>
          </div>
        )}

        <form className="agent-auth-form" onSubmit={handleSubmit}>
          <div className="agent-auth-field">
            <label htmlFor="bootstrap-token-input" className="agent-auth-label">
              Bootstrap Token
            </label>
            <div className="agent-auth-input-group">
              <input
                ref={inputRef}
                id="bootstrap-token-input"
                type={showToken ? 'text' : 'password'}
                className="agent-auth-input"
                placeholder="Paste 16-128 character bootstrap token..."
                value={inputToken}
                onChange={(e) => {
                  setInputToken(e.target.value)
                  if (localError) setLocalError(null)
                }}
                disabled={isSubmitting}
                autoComplete="off"
                spellCheck={false}
                aria-required="true"
              />
              <button
                type="button"
                className="agent-auth-toggle-btn"
                onClick={() => setShowToken(!showToken)}
                disabled={isSubmitting}
                aria-label={showToken ? 'Hide token' : 'Show token'}
              >
                {showToken ? 'Hide' : 'Show'}
              </button>
            </div>
          </div>

          <div className="agent-auth-actions">
            <button
              type="button"
              className="agent-auth-cancel-btn"
              onClick={onClose}
              disabled={isSubmitting}
            >
              Cancel
            </button>
            <button
              type="submit"
              className="agent-auth-submit-btn"
              disabled={isSubmitting || !inputToken.trim()}
            >
              {isSubmitting ? (
                <span className="agent-auth-submitting">
                  <span className="spinner" aria-hidden="true" />
                  Authenticating...
                </span>
              ) : (
                'Authenticate'
              )}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
