import type { AiAvailabilityStatus } from '../services/aiStatus.ts'
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
 * Accessible persistent indicator displaying upstream AI assistant availability.
 *
 * Distinct from database profile status:
 * - Local database profile browsing and manual SQL execution remain functional
 *   even when the AI status is 'ai_unavailable'.
 * - Uses accessible text, high-contrast badges, explicit tooltips, and state descriptions.
 */
export function AiStatusIndicator({
  status,
  label,
  description,
  detail,
  isChecking = false,
  onCheckAgain,
}: AiStatusIndicatorProps) {
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

      <div className="ai-status-scope-notice" aria-hidden="true">
        AI connectivity applies to natural-language features • Database browsing & manual SQL remain independent.
      </div>
    </div>
  )
}
