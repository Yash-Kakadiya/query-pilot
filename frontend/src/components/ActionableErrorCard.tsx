import { useState } from 'react'
import type { DiagnosticSolution, DiagnosticAction } from '../services/errorDiagnostics.ts'
import { openExternalUrl } from '../services/systemBrowser.ts'
import './ActionableErrorCard.css'

export interface ActionableErrorCardProps {
  diagnostic: DiagnosticSolution
  onRetry?: () => void
  onOpenProfileModal?: () => void
  onFocusApiKeyInput?: () => void
  onSwitchModel?: (modelName: string) => void
  className?: string
}

export function ActionableErrorCard({
  diagnostic,
  onRetry,
  onOpenProfileModal,
  onFocusApiKeyInput,
  onSwitchModel,
  className = '',
}: ActionableErrorCardProps) {
  const [showTechnicalDetails, setShowTechnicalDetails] = useState(false)

  const getCategoryIcon = (category: string): string => {
    switch (category) {
      case 'api_key':
        return '🔑'
      case 'model_quota':
        return '⏳'
      case 'model_unavailable':
        return '🤖'
      case 'db_connection_refused':
        return '🔌'
      case 'db_host_unreachable':
        return '🌐'
      case 'db_auth_failed':
        return '🔒'
      case 'db_ssl_required':
      case 'db_safety_mutation':
        return '🛡️'
      case 'db_schema_not_found':
        return '❓'
      case 'db_timeout':
        return '⏱️'
      default:
        return '⚠️'
    }
  }

  const handleActionClick = (action: DiagnosticAction) => {
    switch (action.action_type) {
      case 'open_external_url':
        if (action.payload) {
          void openExternalUrl(action.payload)
        }
        break
      case 'focus_api_key_input':
        if (onFocusApiKeyInput) {
          onFocusApiKeyInput()
        } else {
          // Fallback: try focusing the header API key input directly
          const input = document.querySelector<HTMLInputElement>('.ai-gemini-key-input')
          if (input) {
            input.focus()
            input.scrollIntoView({ behavior: 'smooth', block: 'center' })
          }
        }
        break
      case 'open_profile_modal':
        if (onOpenProfileModal) {
          onOpenProfileModal()
        }
        break
      case 'switch_model':
        if (action.payload) {
          if (onSwitchModel) {
            onSwitchModel(action.payload)
          } else {
            try {
              localStorage.setItem('querypilot_gemini_model', action.payload)
              window.dispatchEvent(new Event('querypilot_model_changed'))
            } catch {
              // Ignore storage errors
            }
          }
        }
        break
      case 'retry':
        if (onRetry) {
          onRetry()
        }
        break
    }
  }

  return (
    <div className={`actionable-error-card actionable-error-card-${diagnostic.category} ${className}`} role="alert">
      {/* Header */}
      <div className="actionable-error-header">
        <span className="actionable-error-icon" aria-hidden="true">
          {getCategoryIcon(diagnostic.category)}
        </span>
        <div className="actionable-error-title-group">
          <span className="actionable-error-badge">{diagnostic.category.replace(/_/g, ' ').toUpperCase()}</span>
          <h3 className="actionable-error-title">{diagnostic.title}</h3>
        </div>
      </div>

      {/* Summary */}
      <p className="actionable-error-summary">{diagnostic.summary}</p>

      {/* Root Cause Analysis */}
      <div className="actionable-error-section">
        <span className="actionable-error-section-label">💡 Why this happened:</span>
        <p className="actionable-error-root-cause">{diagnostic.root_cause}</p>
      </div>

      {/* Actionable Steps Checklist */}
      {diagnostic.actionable_steps && diagnostic.actionable_steps.length > 0 && (
        <div className="actionable-error-section">
          <span className="actionable-error-section-label">🛠️ How to solve it:</span>
          <ul className="actionable-error-steps-list">
            {diagnostic.actionable_steps.map((step, idx) => (
              <li key={idx} className="actionable-error-step-item">
                <span className="actionable-step-bullet">{idx + 1}</span>
                <span>{step}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* 1-Click Action Buttons */}
      {(diagnostic.primary_action || diagnostic.secondary_action) && (
        <div className="actionable-error-actions-row">
          {diagnostic.primary_action && (
            <button
              type="button"
              className="actionable-btn actionable-btn-primary"
              onClick={() => handleActionClick(diagnostic.primary_action!)}
            >
              {diagnostic.primary_action.label}
            </button>
          )}

          {diagnostic.secondary_action && (
            <button
              type="button"
              className="actionable-btn actionable-btn-secondary"
              onClick={() => handleActionClick(diagnostic.secondary_action!)}
            >
              {diagnostic.secondary_action.label}
            </button>
          )}
        </div>
      )}

      {/* Technical Details Accordion */}
      {diagnostic.technical_details && (
        <div className="actionable-error-technical-section">
          <button
            type="button"
            className="actionable-toggle-technical-btn"
            onClick={() => setShowTechnicalDetails(!showTechnicalDetails)}
            aria-expanded={showTechnicalDetails}
          >
            {showTechnicalDetails ? '▲ Hide Technical Details' : '▼ Show Technical Details (for debugging)'}
          </button>
          {showTechnicalDetails && (
            <pre className="actionable-technical-pre">
              <code>{diagnostic.technical_details}</code>
            </pre>
          )}
        </div>
      )}
    </div>
  )
}
