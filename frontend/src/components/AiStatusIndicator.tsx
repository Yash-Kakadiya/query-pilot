import { useState, useEffect } from 'react'
import type { AiAvailabilityStatus } from '../services/aiStatus.ts'
import { getStoredGeminiKey, setStoredGeminiKey } from '../services/geminiKeyStorage.ts'
import './AiStatusIndicator.css'

export interface AiStatusIndicatorProps {
  status: AiAvailabilityStatus
  label: string
  description: string
  detail?: string
  isChecking?: boolean
  onCheckAgain?: () => void
}

/**
 * Accessible persistent indicator displaying upstream AI assistant availability
 * with an integrated Gemini API Key manager and Google AI Studio direct link.
 *
 * Requirements:
 * - Field for Gemini key: when non-empty, used as Primary Key.
 * - If primary key fails or is empty, system automatically uses backup keys.
 * - Dedicated "Get Free Gemini 🗝️" button linking to https://aistudio.google.com/api-keys.
 */
export function AiStatusIndicator({
  status,
  label,
  description,
  detail,
  isChecking = false,
  onCheckAgain,
}: AiStatusIndicatorProps) {
  const [apiKey, setApiKey] = useState<string>(() => getStoredGeminiKey())
  const [showKey, setShowKey] = useState<boolean>(false)

  useEffect(() => {
    const handleKeyChange = () => {
      setApiKey(getStoredGeminiKey())
    }
    window.addEventListener('querypilot_gemini_key_changed', handleKeyChange)
    return () => {
      window.removeEventListener('querypilot_gemini_key_changed', handleKeyChange)
    }
  }, [])

  const handleKeyInputChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const val = e.target.value
    setApiKey(val)
    setStoredGeminiKey(val)
    if (onCheckAgain) {
      setTimeout(() => onCheckAgain(), 100)
    }
  }

  const handleClearKey = () => {
    setApiKey('')
    setStoredGeminiKey('')
    if (onCheckAgain) {
      setTimeout(() => onCheckAgain(), 100)
    }
  }

  const dotModifier =
    status === 'ai_available'
      ? 'available'
      : status === 'ai_unavailable'
      ? 'unavailable'
      : status === 'auth_required'
      ? 'auth'
      : status === 'checking'
      ? 'checking'
      : 'unknown'

  const hasCustomKey = Boolean(apiKey.trim())

  return (
    <div
      className={`ai-status-card ai-status-card-${status}`}
      role="status"
      aria-live="polite"
      title={`${label}: ${description}${detail ? ` (${detail})` : ''}`}
    >
      <div className="ai-status-row">
        <div className="ai-status-header-group">
          <span
            className={`ai-status-dot ai-dot-${dotModifier}`}
            aria-hidden="true"
          />
          <span className="ai-status-prefix">AI Service:</span>
          <strong className="ai-status-label">{label}</strong>
        </div>

        {onCheckAgain && (status === 'ai_unavailable' || status === 'unknown') && (
          <button
            type="button"
            className="ai-status-retry-button"
            onClick={onCheckAgain}
            disabled={isChecking}
            aria-label="Re-check upstream AI service connectivity"
          >
            {isChecking ? 'Checking...' : 'Check Status'}
          </button>
        )}
      </div>

      <p className="ai-status-description">{description}</p>

      {detail && <div className="ai-status-detail-tag">{detail}</div>}

      {/* Gemini API Key Configuration Controls */}
      <div className="ai-gemini-key-section">
        <div className="ai-gemini-key-input-row">
          <div className="ai-gemini-key-input-wrapper">
            <span className="ai-gemini-key-icon" aria-hidden="true">🔑</span>
            <input
              type={showKey ? 'text' : 'password'}
              className="ai-gemini-key-input"
              value={apiKey}
              onChange={handleKeyInputChange}
              placeholder="Paste Gemini API Key (optional primary key)"
              aria-label="Gemini API Key"
              autoComplete="off"
              spellCheck="false"
            />
            {hasCustomKey && (
              <button
                type="button"
                className="ai-gemini-key-clear-btn"
                onClick={handleClearKey}
                title="Clear custom key (revert to backup keys)"
                aria-label="Clear custom Gemini key"
              >
                ✕
              </button>
            )}
            <button
              type="button"
              className="ai-gemini-key-toggle-btn"
              onClick={() => setShowKey(!showKey)}
              title={showKey ? 'Hide key' : 'Show key'}
              aria-label={showKey ? 'Hide key' : 'Show key'}
            >
              {showKey ? 'Hide' : 'Show'}
            </button>
          </div>

          <a
            href="https://aistudio.google.com/api-keys"
            target="_blank"
            rel="noopener noreferrer"
            className="ai-gemini-get-key-btn"
            title="Open Google AI Studio to generate a free Gemini API key"
          >
            Get Free Gemini 🗝️
          </a>
        </div>

        <div className="ai-gemini-key-status-indicator">
          {hasCustomKey ? (
            <span className="ai-gemini-badge-custom">
              ✓ Custom Primary Key Active &bull; Automatic fallback to backup keys enabled
            </span>
          ) : (
            <span className="ai-gemini-badge-default">
              ℹ️ Using configured backup keys from environment &bull; Enter key above to set primary key
            </span>
          )}
        </div>
      </div>

      <div className="ai-status-scope-notice" aria-hidden="true">
        AI connectivity applies to natural-language features • Database browsing & manual SQL remain independent.
      </div>
    </div>
  )
}
