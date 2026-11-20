import { useState, useEffect } from 'react'
import { getStoredGeminiKey, setStoredGeminiKey } from '../services/geminiKeyStorage.ts'
import { openExternalUrl } from '../services/systemBrowser.ts'
import { runAppDiagnostics } from '../services/appDiagnostics.ts'
import type { ProfileSummary } from '../types/api.ts'
import type { DiagnosticReport } from '../types/diagnostics.ts'
import './SettingsModal.css'

export interface SettingsModalProps {
  isOpen: boolean
  onClose: () => void
  activeProfile?: ProfileSummary | null
  profiles?: ProfileSummary[]
}

export function SettingsModal({
  isOpen,
  onClose,
  activeProfile,
  profiles,
}: SettingsModalProps) {
  const [apiKey, setApiKey] = useState<string>(() => getStoredGeminiKey())
  const [showKey, setShowKey] = useState<boolean>(false)
  const [selectedModel, setSelectedModel] = useState<string>(() => {
    try {
      return localStorage.getItem('querypilot_gemini_model') || 'gemini-3.8-flash'
    } catch {
      return 'gemini-3.8-flash'
    }
  })
  const [maxRows, setMaxRows] = useState<string>(() => {
    try {
      return localStorage.getItem('querypilot_max_rows') || '100'
    } catch {
      return '100'
    }
  })
  const [queryTimeout, setQueryTimeout] = useState<string>(() => {
    try {
      return localStorage.getItem('querypilot_query_timeout') || '15'
    } catch {
      return '15'
    }
  })
  const [savedNotification, setSavedNotification] = useState<boolean>(false)

  // System Diagnostics state
  const [diagReport, setDiagReport] = useState<DiagnosticReport | null>(null)
  const [isRunningDiag, setIsRunningDiag] = useState<boolean>(false)

  useEffect(() => {
    if (isOpen) {
      setApiKey(getStoredGeminiKey())
    }
  }, [isOpen])

  // Handle escape key to dismiss
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && isOpen) {
        onClose()
      }
    }
    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [isOpen, onClose])

  if (!isOpen) return null

  const handleSave = () => {
    setStoredGeminiKey(apiKey)
    try {
      localStorage.setItem('querypilot_gemini_model', selectedModel)
      localStorage.setItem('querypilot_max_rows', maxRows)
      localStorage.setItem('querypilot_query_timeout', queryTimeout)
    } catch {
      // Ignore storage restrictions
    }
    setSavedNotification(true)
    setTimeout(() => {
      setSavedNotification(false)
      onClose()
    }, 600)
  }

  const handleClearKey = () => {
    setApiKey('')
    setStoredGeminiKey('')
  }

  const handleRunDiagnostics = async () => {
    setIsRunningDiag(true)
    try {
      const report = await runAppDiagnostics({
        activeProfile,
        profiles,
        apiKey,
        model: selectedModel,
        maxRows,
        queryTimeout,
        onProgress: (live) => setDiagReport({ ...live }),
      })
      setDiagReport(report)
    } catch {
      // Diagnostic errors are captured within report checks
    } finally {
      setIsRunningDiag(false)
    }
  }

  const hasCustomKey = Boolean(apiKey.trim())

  return (
    <div className="settings-backdrop" onClick={onClose} role="dialog" aria-modal="true" aria-label="QueryPilot Settings">
      <div className="settings-dialog" onClick={(e) => e.stopPropagation()}>
        <div className="settings-header">
          <div className="settings-header-title">
            <span className="settings-header-icon" aria-hidden="true">⚙️</span>
            <h2 className="settings-title">Application Settings</h2>
          </div>
          <button
            type="button"
            className="settings-close-btn"
            onClick={onClose}
            aria-label="Close Settings"
          >
            ✕
          </button>
        </div>

        <div className="settings-body">
          {/* Section 1: AI / Gemini Configuration */}
          <div className="settings-section">
            <h3 className="settings-section-title">
              <span className="settings-section-icon">🤖</span> AI Model & Gemini Credentials
            </h3>
            <p className="settings-section-desc">
              Configure your primary Gemini API key. If empty or invalid, the system automatically falls back to environment backup keys.
            </p>

            <div className="settings-field">
              <label htmlFor="settings-gemini-key" className="settings-label">
                Primary Gemini API Key
              </label>
              <div className="settings-input-group">
                <input
                  id="settings-gemini-key"
                  type={showKey ? 'text' : 'password'}
                  className="settings-input"
                  value={apiKey}
                  onChange={(e) => setApiKey(e.target.value)}
                  placeholder="Paste Gemini API key (e.g. AIzaSy...)"
                  autoComplete="off"
                  spellCheck="false"
                />
                {hasCustomKey && (
                  <button
                    type="button"
                    className="settings-btn-inline"
                    onClick={handleClearKey}
                    title="Clear key and revert to environment backup keys"
                  >
                    Clear
                  </button>
                )}
                <button
                  type="button"
                  className="settings-btn-inline"
                  onClick={() => setShowKey(!showKey)}
                >
                  {showKey ? 'Hide' : 'Show'}
                </button>
              </div>

              <div className="settings-key-actions">
                <button
                  type="button"
                  className="settings-get-key-btn"
                  onClick={() => {
                    void openExternalUrl('https://aistudio.google.com/api-keys')
                  }}
                  title="Open Google AI Studio in system browser"
                >
                  Get Free Gemini 🗝️
                </button>

                <span className={hasCustomKey ? 'settings-badge-active' : 'settings-badge-muted'}>
                  {hasCustomKey
                    ? '✓ Custom Gemini Key Active (Stored locally on device)'
                    : '⚠️ No Key Set (Paste your Gemini API key above)'}
                </span>
              </div>
            </div>

            <div className="settings-field">
              <label htmlFor="settings-model-select" className="settings-label">
                Gemini Model
              </label>
              <select
                id="settings-model-select"
                className="settings-select"
                value={selectedModel}
                onChange={(e) => setSelectedModel(e.target.value)}
              >
                <option value="gemini-3.8-flash">gemini-3.8-flash (Recommended &bull; Fast &bull; Newest)</option>
                <option value="gemini-2.5-flash">gemini-2.5-flash (Balanced)</option>
                <option value="gemini-3.5-flash-lite">gemini-3.5-flash-lite (Lowest Latency)</option>
                <option value="gemini-2.5-pro">gemini-2.5-pro (High Reasoning Capacity)</option>
              </select>
            </div>
          </div>

          {/* Section 2: Execution Safety Limits */}
          <div className="settings-section">
            <h3 className="settings-section-title">
              <span className="settings-section-icon">🛡️</span> Query Execution & Safety Guardrails
            </h3>

            <div className="settings-grid-two">
              <div className="settings-field">
                <label htmlFor="settings-max-rows" className="settings-label">
                  Max Row Ceiling
                </label>
                <select
                  id="settings-max-rows"
                  className="settings-select"
                  value={maxRows}
                  onChange={(e) => setMaxRows(e.target.value)}
                >
                  <option value="50">50 rows (Fastest preview)</option>
                  <option value="100">100 rows (Standard default)</option>
                  <option value="250">250 rows (Moderate)</option>
                  <option value="500">500 rows (Large preview)</option>
                  <option value="1000">1000 rows (Maximum limit)</option>
                </select>
              </div>

              <div className="settings-field">
                <label htmlFor="settings-timeout" className="settings-label">
                  Query Execution Timeout
                </label>
                <select
                  id="settings-timeout"
                  className="settings-select"
                  value={queryTimeout}
                  onChange={(e) => setQueryTimeout(e.target.value)}
                >
                  <option value="10">10 seconds</option>
                  <option value="15">15 seconds (Default)</option>
                  <option value="30">30 seconds (Complex analytical)</option>
                  <option value="60">60 seconds (Heavy joins)</option>
                </select>
              </div>
            </div>
          </div>

          {/* Section 3: App Information & Session Reset */}
          <div className="settings-section">
            <h3 className="settings-section-title">
              <span className="settings-section-icon">💻</span> Desktop App Environment
            </h3>
            <div className="settings-info-box">
              <div className="settings-info-row">
                <span className="settings-info-name">Runtime Mode:</span>
                <span className="settings-info-val">Desktop Standalone (Local Agent Loopback)</span>
              </div>
              <div className="settings-info-row">
                <span className="settings-info-name">Version:</span>
                <span className="settings-info-val">QueryPilot v0.3.0 (Tauri 2.12 + WebView2)</span>
              </div>
              <div className="settings-info-row">
                <span className="settings-info-name">Database Engines:</span>
                <span className="settings-info-val">SQLite &bull; PostgreSQL &bull; MySQL &bull; SQL Server</span>
              </div>
            </div>
          </div>

          {/* Section 4: System Health & Diagnostic Suite */}
          <div className="settings-section settings-diag-section">
            <div className="settings-diag-header">
              <div>
                <h3 className="settings-section-title">
                  <span className="settings-section-icon">🩺</span> System Health & App Diagnostics
                </h3>
                <p className="settings-section-desc">
                  Run comprehensive self-diagnostics across daemon connectivity, active database connections, schema discovery, SQL AST guardrails, and Gemini credentials.
                </p>
              </div>
              <button
                type="button"
                className={`settings-diag-run-btn ${isRunningDiag ? 'is-running' : ''}`}
                onClick={handleRunDiagnostics}
                disabled={isRunningDiag}
              >
                {isRunningDiag ? (
                  <>
                    <span className="diag-spinner" aria-hidden="true" />
                    <span>Running Audit...</span>
                  </>
                ) : (
                  <>
                    <span>🩺</span>
                    <span>Diagnose Entire App</span>
                  </>
                )}
              </button>
            </div>

            {diagReport && (
              <div className="diag-report-container">
                {/* Overall Scorecard */}
                <div className={`diag-score-card diag-score-${diagReport.overallStatus}`}>
                  <div className="diag-score-header">
                    <div className="diag-score-badge">
                      {diagReport.overallStatus === 'healthy' && '🟢 100% Fully Operational'}
                      {diagReport.overallStatus === 'warning' && `🟡 ${diagReport.successRate}% Partially Operational`}
                      {diagReport.overallStatus === 'critical' && `🔴 ${diagReport.successRate}% Issues Detected`}
                    </div>
                    <div className="diag-score-stats">
                      <span className="diag-stat-pill stat-pass">✓ {diagReport.passed} Passed</span>
                      {diagReport.warnings > 0 && (
                        <span className="diag-stat-pill stat-warn">⚠️ {diagReport.warnings} Warning{diagReport.warnings > 1 ? 's' : ''}</span>
                      )}
                      {diagReport.failed > 0 && (
                        <span className="diag-stat-pill stat-fail">✕ {diagReport.failed} Failed</span>
                      )}
                      {diagReport.skipped > 0 && (
                        <span className="diag-stat-pill stat-skip">⏭ {diagReport.skipped} Skipped</span>
                      )}
                    </div>
                  </div>

                  {/* Progress Bar */}
                  <div className="diag-progress-bar-track">
                    <div
                      className={`diag-progress-bar-fill fill-${diagReport.overallStatus}`}
                      style={{ width: `${diagReport.successRate}%` }}
                    />
                  </div>

                  <p className="diag-summary-text">{diagReport.summary}</p>
                </div>

                {/* Subsystem Test Checklist */}
                <div className="diag-checks-list">
                  {diagReport.checks.map((check) => (
                    <div key={check.id} className={`diag-check-row diag-check-${check.status}`}>
                      <div className="diag-check-main">
                        <div className="diag-check-status-col">
                          {check.status === 'passed' && <span className="diag-icon-pass">✓</span>}
                          {check.status === 'failed' && <span className="diag-icon-fail">✕</span>}
                          {check.status === 'warning' && <span className="diag-icon-warn">⚠️</span>}
                          {check.status === 'skipped' && <span className="diag-icon-skip">⏭</span>}
                          {check.status === 'running' && <span className="diag-spinner-sm" />}
                          {check.status === 'pending' && <span className="diag-icon-pending">○</span>}
                        </div>
                        <div className="diag-check-content">
                          <div className="diag-check-top">
                            <span className="diag-check-name">{check.name}</span>
                            <div className="diag-check-meta">
                              {check.durationMs !== undefined && (
                                <span className="diag-latency">{check.durationMs}ms</span>
                              )}
                              <span className={`diag-badge diag-badge-${check.status}`}>
                                {check.status.toUpperCase()}
                              </span>
                            </div>
                          </div>
                          {check.details && <p className="diag-check-details">{check.details}</p>}
                          {check.solution && (
                            <div className="diag-check-solution">
                              <strong>Recommended Solution:</strong> {check.solution}
                            </div>
                          )}
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        </div>

        <div className="settings-footer">
          {savedNotification && (
            <span className="settings-save-success">✓ Settings Saved!</span>
          )}
          <button type="button" className="settings-cancel-btn" onClick={onClose}>
            Cancel
          </button>
          <button type="button" className="settings-save-btn" onClick={handleSave}>
            Save Changes
          </button>
        </div>
      </div>
    </div>
  )
}
