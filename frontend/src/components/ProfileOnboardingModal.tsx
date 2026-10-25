import { useEffect, useRef, useState } from 'react'
import {
  AgentClientError,
  createProfile,
  testProfileConnection,
  validateProfileConfig,
} from '../services/agentClient.ts'
import type {
  ConnectionFailureCode,
  CreateProfileRequest,
  ProfileSummary,
  TestConnectionResponse,
} from '../types/api.ts'
import './ProfileOnboardingModal.css'

export interface ProfileOnboardingModalProps {
  isOpen: boolean
  onClose: () => void
  onProfileCreated: (profile: ProfileSummary) => Promise<void> | void
  onUseProfile: (profileId: string) => void
}

interface FormFields {
  profileId: string
  displayName: string
  server: string
  databaseName: string
  schemaName: string
  timeoutSeconds: number
  maxRowLimit: number
  setAsDefault: boolean
}

const INITIAL_FIELDS: FormFields = {
  profileId: '',
  displayName: '',
  server: 'localhost',
  databaseName: '',
  schemaName: 'dbo',
  timeoutSeconds: 15,
  maxRowLimit: 100,
  setAsDefault: false,
}

const PROFILE_ID_REGEX = /^[a-zA-Z0-9_-]+$/

function mapFailureCodeToFriendlyMessage(code?: ConnectionFailureCode | null, fallbackMessage?: string): string {
  switch (code) {
    case 'AUTH_FAILED':
      return 'Windows Integrated Authentication failed. Please verify that your current Windows user account has access permissions to this SQL Server instance.'
    case 'UNREACHABLE':
      return 'SQL Server instance is unreachable. Please verify that SQL Server is running, the host/instance name is correct, and TCP/IP or Named Pipes protocols are enabled.'
    case 'TIMEOUT':
      return 'Connection timed out. The local database did not respond within the configured timeout window.'
    case 'DATABASE_MISMATCH':
      return 'Database mismatch. Successfully contacted SQL Server, but the specified database does not exist or cannot be accessed.'
    case 'DRIVER_ERROR':
      return 'ODBC Driver error. Ensure that "ODBC Driver 17 for SQL Server" is installed on this machine.'
    case 'CONNECTION_FAILED':
    default:
      return fallbackMessage || 'Failed to establish connection to the specified database.'
  }
}

/**
 * Accessible database profile onboarding modal dialog.
 *
 * Enforces security invariants:
 * - Engine is locked to Microsoft SQL Server ('mssql').
 * - Authentication is locked to Windows Integrated Authentication (use_trusted_connection=true).
 * - No SQL username, password, or arbitrary connection string fields.
 * - Validation is executed offline via the local agent authoritative endpoint before saving.
 * - Profile creation saves the profile under a cross-process lock and refreshes profiles.
 * - Connection testing is strictly explicit and decoupled from saving and schema loading.
 */
export function ProfileOnboardingModal({
  isOpen,
  onClose,
  onProfileCreated,
  onUseProfile,
}: ProfileOnboardingModalProps) {
  const [fields, setFields] = useState<FormFields>(INITIAL_FIELDS)
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({})
  const [generalError, setGeneralError] = useState<string | null>(null)
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [submissionStep, setSubmissionStep] = useState<'idle' | 'validating' | 'saving'>('idle')

  // Post-creation saved state and explicit connection testing
  const [savedProfile, setSavedProfile] = useState<ProfileSummary | null>(null)
  const [isTestingConnection, setIsTestingConnection] = useState(false)
  const [connectionTestResult, setConnectionTestResult] = useState<TestConnectionResponse | null>(null)

  const profileIdInputRef = useRef<HTMLInputElement>(null)
  const testConnBtnRef = useRef<HTMLButtonElement>(null)

  const handleClose = () => {
    setFields(INITIAL_FIELDS)
    setFieldErrors({})
    setGeneralError(null)
    setIsSubmitting(false)
    setSubmissionStep('idle')
    setSavedProfile(null)
    setIsTestingConnection(false)
    setConnectionTestResult(null)
    onClose()
  }

  // Focus input automatically upon modal appearance
  useEffect(() => {
    if (isOpen) {
      const timer = setTimeout(() => {
        profileIdInputRef.current?.focus()
      }, 50)
      return () => clearTimeout(timer)
    }
  }, [isOpen])

  // Focus test button once profile is saved
  useEffect(() => {
    if (savedProfile) {
      setTimeout(() => {
        testConnBtnRef.current?.focus()
      }, 50)
    }
  }, [savedProfile])

  if (!isOpen) {
    return null
  }

  const handleFieldChange = (key: keyof FormFields, value: string | number | boolean) => {
    setFields((prev) => ({ ...prev, [key]: value }))
    if (fieldErrors[key]) {
      setFieldErrors((prev) => {
        const next = { ...prev }
        delete next[key]
        return next
      })
    }
    if (generalError) {
      setGeneralError(null)
    }
  }

  const validateClientFields = (): boolean => {
    const errors: Record<string, string> = {}
    const pid = fields.profileId.trim()
    if (!pid) {
      errors.profileId = 'Profile ID is required.'
    } else if (pid.length > 64) {
      errors.profileId = 'Profile ID cannot exceed 64 characters.'
    } else if (!PROFILE_ID_REGEX.test(pid)) {
      errors.profileId = 'Profile ID may only contain letters, numbers, underscores, and hyphens.'
    }

    const srv = fields.server.trim()
    if (!srv) {
      errors.server = 'SQL Server host or instance is required.'
    } else if (srv.length > 255) {
      errors.server = 'Server target cannot exceed 255 characters.'
    }

    const db = fields.databaseName.trim()
    if (!db) {
      errors.databaseName = 'Database name is required.'
    } else if (db.length > 128) {
      errors.databaseName = 'Database name cannot exceed 128 characters.'
    }

    if (fields.displayName.trim().length > 128) {
      errors.displayName = 'Display name cannot exceed 128 characters.'
    }

    if (fields.schemaName.trim().length > 128) {
      errors.schemaName = 'Schema name cannot exceed 128 characters.'
    }

    if (fields.timeoutSeconds < 1 || fields.timeoutSeconds > 15) {
      errors.timeoutSeconds = 'Timeout must be between 1 and 15 seconds.'
    }

    if (fields.maxRowLimit < 1 || fields.maxRowLimit > 100) {
      errors.maxRowLimit = 'Max row limit must be between 1 and 100 rows.'
    }

    setFieldErrors(errors)
    return Object.keys(errors).length === 0
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (isSubmitting) {
      return
    }

    setGeneralError(null)

    if (!validateClientFields()) {
      return
    }

    setIsSubmitting(true)
    setSubmissionStep('validating')

    const effectiveDisplayName = fields.displayName.trim() || fields.profileId.trim()
    const payload: CreateProfileRequest = {
      profile_id: fields.profileId.trim(),
      display_name: effectiveDisplayName,
      server: fields.server.trim(),
      database_name: fields.databaseName.trim(),
      schema_name: fields.schemaName.trim() || 'dbo',
      driver: 'ODBC Driver 17 for SQL Server',
      engine_type: 'mssql',
      use_trusted_connection: true,
      timeout_seconds: fields.timeoutSeconds,
      max_row_limit: fields.maxRowLimit,
      set_as_default: fields.setAsDefault,
    }

    try {
      // 1. Authoritative local agent offline validation
      await validateProfileConfig(payload)

      // 2. Authoritative local agent profile persistence
      setSubmissionStep('saving')
      const createResponse = await createProfile(payload)

      // 3. Notify parent to refresh profile list without auto-selecting
      await onProfileCreated(createResponse.profile)

      // 4. Transition to saved state for explicit connection testing
      setSavedProfile(createResponse.profile)
      setSubmissionStep('idle')
      setIsSubmitting(false)
    } catch (err: unknown) {
      setIsSubmitting(false)
      setSubmissionStep('idle')

      if (err instanceof AgentClientError) {
        if (err.statusCode === 409 || err.kind === 'conflict') {
          setFieldErrors((prev) => ({
            ...prev,
            profileId: `Profile ID "${fields.profileId.trim()}" is already registered. Please choose a unique ID.`,
          }))
          return
        }
        if (err.statusCode === 422 || err.kind === 'validation') {
          setGeneralError(err.message || 'Validation failed. Please verify configuration parameters.')
          return
        }
        if (err.statusCode === 401 || err.kind === 'authentication') {
          setGeneralError('Local agent session has expired. Please re-authenticate the agent.')
          return
        }
        setGeneralError(err.message)
        return
      }

      setGeneralError(
        err instanceof Error ? err.message : 'An unexpected error occurred while saving profile.'
      )
    }
  }

  const handleTestConnection = async () => {
    if (!savedProfile || isTestingConnection) {
      return
    }

    setIsTestingConnection(true)
    setConnectionTestResult(null)

    try {
      const result = await testProfileConnection(savedProfile.profile_id)
      setConnectionTestResult(result)
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Connection test request failed.'
      setConnectionTestResult({
        connected: false,
        message,
        failure_code: 'CONNECTION_FAILED',
      })
    } finally {
      setIsTestingConnection(false)
    }
  }

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Escape' && !isSubmitting && !isTestingConnection) {
      handleClose()
    }
  }

  return (
    <div
      className="profile-modal-backdrop"
      role="dialog"
      aria-modal="true"
      aria-labelledby="profile-modal-title"
      onKeyDown={handleKeyDown}
    >
      <div className="profile-modal-dialog">
        <header className="profile-modal-header">
          <div className="profile-modal-title-group">
            <span className="profile-modal-icon" aria-hidden="true">🗄️</span>
            <div>
              <h2 id="profile-modal-title" className="profile-modal-title">
                {savedProfile ? 'Profile Created Successfully' : 'Add Local Database Profile'}
              </h2>
              <p className="profile-modal-subtitle">
                {savedProfile
                  ? 'Verify connectivity or proceed to use this database profile.'
                  : 'Configure an approved local SQL Server database profile for natural-language queries.'}
              </p>
            </div>
          </div>
          <button
            type="button"
            className="profile-modal-close-btn"
            onClick={handleClose}
            disabled={isSubmitting || isTestingConnection}
            aria-label="Close profile onboarding dialog"
          >
            &times;
          </button>
        </header>

        {/* VIEW 1: FORM INPUT VIEW */}
        {!savedProfile && (
          <form className="profile-modal-form" onSubmit={handleSubmit} noValidate>
            {generalError && (
              <div className="profile-modal-alert profile-modal-alert-error" role="alert">
                <span aria-hidden="true">⚠️</span>
                <span>{generalError}</span>
              </div>
            )}

            {/* Security Guarantee Notice */}
            <div className="profile-security-banner">
              <span className="security-icon" aria-hidden="true">🔒</span>
              <div className="security-text">
                <strong>Local Security Policy:</strong> Fixed to <strong>Microsoft SQL Server</strong> and{' '}
                <strong>Windows Integrated Authentication</strong>. No SQL passwords or arbitrary connection strings permitted.
              </div>
            </div>

            <div className="profile-form-grid">
              {/* Profile ID */}
              <div className="profile-form-group">
                <label htmlFor="onboarding-profile-id" className="profile-form-label">
                  Profile ID <span className="required-star">*</span>
                </label>
                <input
                  ref={profileIdInputRef}
                  id="onboarding-profile-id"
                  type="text"
                  className={`profile-form-input ${fieldErrors.profileId ? 'input-error' : ''}`}
                  placeholder="e.g. sales_db_local"
                  value={fields.profileId}
                  onChange={(e) => handleFieldChange('profileId', e.target.value)}
                  disabled={isSubmitting}
                  maxLength={64}
                  required
                />
                {fieldErrors.profileId && (
                  <span className="field-error-text" role="alert">{fieldErrors.profileId}</span>
                )}
                <span className="field-help-text">Unique identifier (letters, numbers, _, -).</span>
              </div>

              {/* Display Name */}
              <div className="profile-form-group">
                <label htmlFor="onboarding-display-name" className="profile-form-label">
                  Display Name
                </label>
                <input
                  id="onboarding-display-name"
                  type="text"
                  className={`profile-form-input ${fieldErrors.displayName ? 'input-error' : ''}`}
                  placeholder="e.g. Local Sales Warehouse"
                  value={fields.displayName}
                  onChange={(e) => handleFieldChange('displayName', e.target.value)}
                  disabled={isSubmitting}
                  maxLength={128}
                />
                {fieldErrors.displayName && (
                  <span className="field-error-text" role="alert">{fieldErrors.displayName}</span>
                )}
                <span className="field-help-text">Human-readable label for UI selection (optional).</span>
              </div>

              {/* Server Host/Instance */}
              <div className="profile-form-group">
                <label htmlFor="onboarding-server" className="profile-form-label">
                  SQL Server Host / Instance <span className="required-star">*</span>
                </label>
                <input
                  id="onboarding-server"
                  type="text"
                  className={`profile-form-input ${fieldErrors.server ? 'input-error' : ''}`}
                  placeholder="e.g. localhost or localhost\SQLEXPRESS"
                  value={fields.server}
                  onChange={(e) => handleFieldChange('server', e.target.value)}
                  disabled={isSubmitting}
                  maxLength={255}
                  required
                />
                {fieldErrors.server && (
                  <span className="field-error-text" role="alert">{fieldErrors.server}</span>
                )}
                <span className="field-help-text">Must target local machine (localhost, 127.0.0.1, .\instance).</span>
              </div>

              {/* Database Name */}
              <div className="profile-form-group">
                <label htmlFor="onboarding-database" className="profile-form-label">
                  Database Name <span className="required-star">*</span>
                </label>
                <input
                  id="onboarding-database"
                  type="text"
                  className={`profile-form-input ${fieldErrors.databaseName ? 'input-error' : ''}`}
                  placeholder="e.g. SalesAnalytics"
                  value={fields.databaseName}
                  onChange={(e) => handleFieldChange('databaseName', e.target.value)}
                  disabled={isSubmitting}
                  maxLength={128}
                  required
                />
                {fieldErrors.databaseName && (
                  <span className="field-error-text" role="alert">{fieldErrors.databaseName}</span>
                )}
                <span className="field-help-text">Target SQL Server database name.</span>
              </div>

              {/* Schema Name */}
              <div className="profile-form-group">
                <label htmlFor="onboarding-schema" className="profile-form-label">
                  Default Schema
                </label>
                <input
                  id="onboarding-schema"
                  type="text"
                  className={`profile-form-input ${fieldErrors.schemaName ? 'input-error' : ''}`}
                  placeholder="dbo"
                  value={fields.schemaName}
                  onChange={(e) => handleFieldChange('schemaName', e.target.value)}
                  disabled={isSubmitting}
                  maxLength={128}
                />
                {fieldErrors.schemaName && (
                  <span className="field-error-text" role="alert">{fieldErrors.schemaName}</span>
                )}
                <span className="field-help-text">Relational schema namespace (defaults to dbo).</span>
              </div>

              {/* Timeout & Max Row Limit row */}
              <div className="profile-form-group">
                <label htmlFor="onboarding-timeout" className="profile-form-label">
                  Timeout (seconds)
                </label>
                <input
                  id="onboarding-timeout"
                  type="number"
                  className={`profile-form-input ${fieldErrors.timeoutSeconds ? 'input-error' : ''}`}
                  value={fields.timeoutSeconds}
                  onChange={(e) => handleFieldChange('timeoutSeconds', Number(e.target.value))}
                  disabled={isSubmitting}
                  min={1}
                  max={15}
                />
                {fieldErrors.timeoutSeconds && (
                  <span className="field-error-text" role="alert">{fieldErrors.timeoutSeconds}</span>
                )}
                <span className="field-help-text">Bounded execution ceiling (1-15s).</span>
              </div>

              <div className="profile-form-group">
                <label htmlFor="onboarding-rowlimit" className="profile-form-label">
                  Max Row Limit
                </label>
                <input
                  id="onboarding-rowlimit"
                  type="number"
                  className={`profile-form-input ${fieldErrors.maxRowLimit ? 'input-error' : ''}`}
                  value={fields.maxRowLimit}
                  onChange={(e) => handleFieldChange('maxRowLimit', Number(e.target.value))}
                  disabled={isSubmitting}
                  min={1}
                  max={100}
                />
                {fieldErrors.maxRowLimit && (
                  <span className="field-error-text" role="alert">{fieldErrors.maxRowLimit}</span>
                )}
                <span className="field-help-text">Maximum returned rows ceiling (1-100).</span>
              </div>
            </div>

            {/* Locked security attributes summary */}
            <div className="profile-locked-attributes" aria-label="Fixed Security Configuration">
              <span className="locked-badge">
                <span className="locked-icon" aria-hidden="true">🔒</span>
                Engine: <strong>MSSQL</strong>
              </span>
              <span className="locked-badge">
                <span className="locked-icon" aria-hidden="true">🔒</span>
                Auth: <strong>Windows Integrated</strong>
              </span>
              <span className="locked-badge">
                <span className="locked-icon" aria-hidden="true">🔒</span>
                Driver: <strong>ODBC Driver 17 for SQL Server</strong>
              </span>
            </div>

            {/* Set as Default Profile Checkbox */}
            <div className="profile-checkbox-row">
              <label className="profile-checkbox-label">
                <input
                  type="checkbox"
                  checked={fields.setAsDefault}
                  onChange={(e) => handleFieldChange('setAsDefault', e.target.checked)}
                  disabled={isSubmitting}
                />
                <span>Set as default profile in local agent</span>
              </label>
            </div>

            <footer className="profile-modal-footer">
              <button
                type="button"
                className="profile-modal-btn profile-modal-cancel-btn"
                onClick={handleClose}
                disabled={isSubmitting}
              >
                Cancel
              </button>
              <button
                type="submit"
                className="profile-modal-btn profile-modal-submit-btn"
                disabled={isSubmitting}
              >
                {isSubmitting ? (
                  <>
                    <span className="spinner-inline" aria-hidden="true" />
                    <span>
                      {submissionStep === 'validating'
                        ? 'Validating Config...'
                        : 'Saving Profile...'}
                    </span>
                  </>
                ) : (
                  'Validate & Save Profile'
                )}
              </button>
            </footer>
          </form>
        )}

        {/* VIEW 2: SAVED CONFIRMATION & EXPLICIT CONNECTION TESTING VIEW */}
        {savedProfile && (
          <div className="profile-modal-saved-view">
            <div className="saved-confirmation-card" role="status">
              <span className="saved-check-icon" aria-hidden="true">✓</span>
              <div>
                <h3 className="saved-card-title">Profile Registered in Local Store</h3>
                <p className="saved-card-desc">
                  Profile <strong>{savedProfile.display_name}</strong> (<code>{savedProfile.profile_id}</code>) has been
                  saved to your trusted local profiles file.
                </p>
              </div>
            </div>

            <div className="saved-meta-summary">
              <div className="meta-row">
                <span className="meta-row-label">Server Target:</span>
                <span className="meta-row-value">{fields.server}</span>
              </div>
              <div className="meta-row">
                <span className="meta-row-label">Target Database:</span>
                <span className="meta-row-value">{fields.databaseName}</span>
              </div>
              <div className="meta-row">
                <span className="meta-row-label">Schema:</span>
                <span className="meta-row-value">{savedProfile.schema_name || 'dbo'}</span>
              </div>
              <div className="meta-row">
                <span className="meta-row-label">Authentication:</span>
                <span className="meta-row-value">Windows Integrated (Trusted Connection)</span>
              </div>
            </div>

            <div className="connection-test-section">
              <h4 className="conn-test-title">Explicit Database Connectivity Test</h4>
              <p className="conn-test-desc">
                Execute an isolated, bounded query (<code>SELECT DB_NAME()</code>) to verify that QueryPilot
                can communicate with your local SQL Server database.
              </p>

              {connectionTestResult && (
                <div
                  className={`test-result-box ${
                    connectionTestResult.connected ? 'test-result-success' : 'test-result-failure'
                  }`}
                  role="alert"
                >
                  <div className="test-result-header">
                    <span className="test-result-icon" aria-hidden="true">
                      {connectionTestResult.connected ? '✓' : '⚠️'}
                    </span>
                    <strong className="test-result-title">
                      {connectionTestResult.connected
                        ? 'Connection Succeeded'
                        : 'Connection Test Failed'}
                    </strong>
                  </div>
                  <p className="test-result-message">
                    {connectionTestResult.connected
                      ? `Verified database: ${connectionTestResult.database_name || fields.databaseName}`
                      : mapFailureCodeToFriendlyMessage(
                          connectionTestResult.failure_code,
                          connectionTestResult.message
                        )}
                  </p>
                  {connectionTestResult.connected && typeof connectionTestResult.latency_ms === 'number' && (
                    <span className="test-latency-pill">
                      Roundtrip Latency: {Math.round(connectionTestResult.latency_ms)} ms
                    </span>
                  )}
                  {!connectionTestResult.connected && (
                    <p className="test-failure-note">
                      The profile remains safely saved. You can retry the connection test after verifying your database service is running.
                    </p>
                  )}
                </div>
              )}

              <div className="conn-actions-row">
                <button
                  ref={testConnBtnRef}
                  type="button"
                  className="profile-modal-btn profile-test-btn"
                  onClick={handleTestConnection}
                  disabled={isTestingConnection}
                >
                  {isTestingConnection ? (
                    <>
                      <span className="spinner-inline" aria-hidden="true" />
                      <span>Testing SQL Server Connection...</span>
                    </>
                  ) : connectionTestResult ? (
                    'Re-test Connection'
                  ) : (
                    'Test Connection'
                  )}
                </button>
              </div>
            </div>

            <footer className="profile-modal-footer">
              <button
                type="button"
                className="profile-modal-btn profile-modal-cancel-btn"
                onClick={handleClose}
                disabled={isTestingConnection}
              >
                Close
              </button>

              <button
                type="button"
                className="profile-modal-btn profile-modal-submit-btn profile-use-btn"
                onClick={() => {
                  onUseProfile(savedProfile.profile_id)
                  handleClose()
                }}
                disabled={isTestingConnection}
              >
                Use Profile & Load Schema
              </button>
            </footer>
          </div>
        )}
      </div>
    </div>
  )
}
