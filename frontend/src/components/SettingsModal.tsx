import { useState, useEffect } from 'react'
import { getStoredGeminiKey, setStoredGeminiKey } from '../services/geminiKeyStorage.ts'
import { openExternalUrl } from '../services/systemBrowser.ts'
import './SettingsModal.css'

export interface SettingsModalProps {
  isOpen: boolean
  onClose: () => void
}

export function SettingsModal({
  isOpen,
  onClose,
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
                <span className="settings-info-val">QueryPilot v0.2.0 (Tauri 2.12 + WebView2)</span>
              </div>
              <div className="settings-info-row">
                <span className="settings-info-name">Database Engines:</span>
                <span className="settings-info-val">SQLite &bull; Microsoft SQL Server (ODBC)</span>
              </div>
            </div>
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
