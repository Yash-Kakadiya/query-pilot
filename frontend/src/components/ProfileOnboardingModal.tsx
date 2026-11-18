import { useEffect, useRef, useState } from 'react'
import { open as openTauriDialog } from '@tauri-apps/plugin-dialog'
import {
  AgentClientError,
  createProfile,
  testProfileConnection,
  validateProfileConfig,
} from '../services/agentClient.ts'
import type {
  CreateProfileRequest,
  ProfileSummary,
  TestConnectionResponse,
} from '../types/api.ts'
import './ProfileOnboardingModal.css'
import {
  CLOUD_PROVIDER_CONFIGS,
  mapFailureCodeToFriendlyMessage,
  sanitizeServerInput,
  type CloudProvider,
} from '../utils/cloudProviders.ts'

export interface ProfileOnboardingModalProps {
  isOpen: boolean
  onClose: () => void
  onProfileCreated: (profile: ProfileSummary) => Promise<void> | void
  onUseProfile: (profileId: string) => void
}

export { CLOUD_PROVIDER_CONFIGS, sanitizeServerInput }
export type { CloudProvider }

interface FormFields {
  engineCategory: 'local' | 'cloud'
  engineType: 'mssql' | 'sqlite'
  cloudProvider: CloudProvider
  profileId: string
  displayName: string
  server: string
  port: string
  databaseName: string
  databasePath: string
  schemaName: string
  user: string
  password: string
  encrypt: boolean
  trustServerCertificate: boolean
  timeoutSeconds: number
  maxRowLimit: number
  setAsDefault: boolean
}

const INITIAL_FIELDS: FormFields = {
  engineCategory: 'local',
  engineType: 'mssql',
  cloudProvider: 'monsterasp',
  profileId: '',
  displayName: '',
  server: 'localhost',
  port: '1433',
  databaseName: '',
  databasePath: '',
  schemaName: 'dbo',
  user: '',
  password: '',
  encrypt: true,
  trustServerCertificate: true,
  timeoutSeconds: 15,
  maxRowLimit: 100,
  setAsDefault: false,
}

const PROFILE_ID_REGEX = /^[a-zA-Z0-9_-]+$/

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
  const [showPassword, setShowPassword] = useState(false)
  const [showGuide, setShowGuide] = useState(false)
  const [showLocalGuide, setShowLocalGuide] = useState(false)
  const [copiedKey, setCopiedKey] = useState<string | null>(null)

  const handleCopyExample = (exampleText: string, cardKey: string) => {
    if (!exampleText) return
    try {
      if (navigator?.clipboard?.writeText) {
        navigator.clipboard.writeText(exampleText).catch(() => {})
      }
    } catch {
      // ignore clipboard error
    }
    setCopiedKey(cardKey)
    setTimeout(() => {
      setCopiedKey((curr) => (curr === cardKey ? null : curr))
    }, 1500)
  }

  // Post-creation saved state and explicit connection testing
  const [savedProfile, setSavedProfile] = useState<ProfileSummary | null>(null)
  const [isTestingConnection, setIsTestingConnection] = useState(false)
  const [connectionTestResult, setConnectionTestResult] = useState<TestConnectionResponse | null>(null)

  const profileIdInputRef = useRef<HTMLInputElement>(null)
  const testConnBtnRef = useRef<HTMLButtonElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)

  const handleBrowseFileClick = async () => {
    // 1. Try official Tauri v2 native dialog plugin first
    try {
      const selected = await openTauriDialog({
        title: 'Select SQLite Database File',
        multiple: false,
        directory: false,
        filters: [
          { name: 'SQLite Databases (*.db, *.sqlite, *.sqlite3)', extensions: ['db', 'sqlite', 'sqlite3'] },
          { name: 'All Files (*.*)', extensions: ['*'] },
        ],
      })
      if (typeof selected === 'string' && selected.trim()) {
        handleFieldChange('databasePath', selected.trim())
      }
      return
    } catch {
      // Not running in Tauri desktop shell or IPC unavailable: fallback to browser handling
    }

    // 2. Browser fallback: trigger native HTML file picker
    if (fileInputRef.current) {
      fileInputRef.current.value = ''
      fileInputRef.current.click()
    }
  }

  const handleNativeFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files
    if (!files || files.length === 0) {
      // Graceful cancellation: no file chosen, preserve existing state
      return
    }

    const selectedFile = files[0]
    // In desktop webview runtimes (e.g. Electron / patched WebView), file.path contains the local filesystem path.
    // In standard web browsers, file.path is undefined for sandbox security reasons.
    const rawPath = (selectedFile as unknown as { path?: string }).path
    if (rawPath && /^(?:[a-zA-Z]:[\\/]|\\\\|\/)/.test(rawPath.trim())) {
      handleFieldChange('databasePath', rawPath.trim())
    } else {
      // Do NOT populate a relative filename or fake path. Inform user to enter absolute path manually.
      setFieldErrors((prev) => ({
        ...prev,
        databasePath: `Browser security sandbox does not expose the full filesystem path for "${selectedFile.name}". Please enter or paste the full absolute path manually.`,
      }))
    }
  }

  const handleClose = () => {
    setFields(INITIAL_FIELDS)
    setFieldErrors({})
    setGeneralError(null)
    setIsSubmitting(false)
    setSubmissionStep('idle')
    setSavedProfile(null)
    setIsTestingConnection(false)
    setConnectionTestResult(null)
    setShowGuide(false)
    setShowLocalGuide(false)
    setCopiedKey(null)
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
    if (key === 'server' && typeof value === 'string' && fields.engineCategory === 'cloud') {
      const { cleanServer, extractedPort } = sanitizeServerInput(value)
      setFields((prev) => ({
        ...prev,
        server: cleanServer,
        ...(extractedPort ? { port: extractedPort } : {}),
      }))
    } else if (key === 'databaseName' && typeof value === 'string' && fields.cloudProvider === 'monsterasp') {
      let cleanDb = value.trim()
      if (cleanDb.endsWith('.databaseasp.net')) {
        cleanDb = cleanDb.replace('.databaseasp.net', '')
      }
      setFields((prev) => ({ ...prev, databaseName: cleanDb }))
    } else {
      setFields((prev) => ({ ...prev, [key]: value }))
    }
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

  const handleCategoryChange = (category: 'local' | 'cloud') => {
    if (category === fields.engineCategory) {
      return
    }
    setFields((prev) => ({
      ...prev,
      engineCategory: category,
      engineType: 'mssql',
      cloudProvider: 'monsterasp',
      server: category === 'cloud' ? '' : 'localhost',
      port: '1433',
      databaseName: '',
      schemaName: 'dbo',
      databasePath: '',
      user: '',
      password: '',
      encrypt: true,
      trustServerCertificate: true,
    }))
    setFieldErrors({})
    setGeneralError(null)
    setShowGuide(false)
  }

  const handleCloudProviderChange = (provider: CloudProvider) => {
    if (provider === fields.cloudProvider) {
      return
    }
    setFields((prev) => ({
      ...prev,
      cloudProvider: provider,
      // Retain all user-entered credentials and inputs without resetting or overriding them
    }))
    setGeneralError(null)
  }

  const handleEngineTypeChange = (newEngine: 'mssql' | 'sqlite') => {
    if (newEngine === fields.engineType) {
      return
    }
    setFields((prev) => ({
      ...prev,
      engineType: newEngine,
      schemaName: newEngine === 'sqlite' ? 'main' : 'dbo',
      // Clear sensitive databasePath when switching away from SQLite
      databasePath: newEngine === 'sqlite' ? prev.databasePath : '',
    }))
    // Clear engine-specific validation errors upon engine switch
    setFieldErrors((prev) => {
      const next = { ...prev }
      delete next.server
      delete next.port
      delete next.databaseName
      delete next.databasePath
      delete next.schemaName
      delete next.user
      delete next.password
      return next
    })
    setGeneralError(null)
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

    if (fields.displayName.trim().length > 128) {
      errors.displayName = 'Display name cannot exceed 128 characters.'
    }

    if (fields.engineCategory === 'cloud') {
      const srv = fields.server.trim()
      if (!srv) {
        errors.server = 'Cloud database server host is required (e.g. sqlXX.monsterasp.net).'
      } else if (srv.length > 255) {
        errors.server = 'Server target cannot exceed 255 characters.'
      }

      const p = fields.port.trim()
      if (p) {
        const portNum = Number(p)
        if (isNaN(portNum) || portNum < 1 || portNum > 65535) {
          errors.port = 'Port must be a valid number (1-65535).'
        }
      }

      const db = fields.databaseName.trim()
      if (!db) {
        errors.databaseName = 'Database name is required.'
      } else if (db.length > 128) {
        errors.databaseName = 'Database name cannot exceed 128 characters.'
      }

      const u = fields.user.trim()
      if (!u) {
        errors.user = 'Database login username is required.'
      } else if (u.length > 128) {
        errors.user = 'Username cannot exceed 128 characters.'
      }

      if (!fields.password) {
        errors.password = 'Database password is required.'
      }
    } else if (fields.engineType === 'mssql') {
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

      if (fields.schemaName.trim().length > 128) {
        errors.schemaName = 'Schema name cannot exceed 128 characters.'
      }
    } else {
      // SQLite validation
      const dbPath = fields.databasePath.trim()
      if (!dbPath) {
        errors.databasePath = 'Database file path is required.'
      } else if (dbPath.length > 1000) {
        errors.databasePath = 'Database path exceeds maximum allowed length.'
      } else if (!/^(?:[a-zA-Z]:[\\/]|\\\\|\/)/.test(dbPath)) {
        errors.databasePath = 'Please provide an absolute path to the SQLite database file (e.g. C:\\... or /...).'
      }
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
    let payload: CreateProfileRequest

    if (fields.engineCategory === 'cloud') {
      let srv = fields.server.trim()
      const port = fields.port.trim()
      if (port && !srv.includes(',') && !srv.includes(':')) {
        srv = `${srv},${port}`
      }
      payload = {
        profile_id: fields.profileId.trim(),
        display_name: effectiveDisplayName,
        engine_type: 'mssql',
        is_cloud: true,
        server: srv,
        database_name: fields.databaseName.trim(),
        schema_name: fields.schemaName.trim() || 'dbo',
        driver: 'ODBC Driver 17 for SQL Server',
        use_trusted_connection: false,
        user: fields.user.trim(),
        password: fields.password,
        encrypt: fields.encrypt,
        trust_server_certificate: fields.trustServerCertificate,
        timeout_seconds: fields.timeoutSeconds,
        max_row_limit: fields.maxRowLimit,
        set_as_default: fields.setAsDefault,
      }
    } else if (fields.engineType === 'sqlite') {
      payload = {
        profile_id: fields.profileId.trim(),
        display_name: effectiveDisplayName,
        engine_type: 'sqlite',
        database_path: fields.databasePath.trim(),
        schema_name: 'main',
        timeout_seconds: fields.timeoutSeconds,
        max_row_limit: fields.maxRowLimit,
        set_as_default: fields.setAsDefault,
      }
    } else {
      payload = {
        profile_id: fields.profileId.trim(),
        display_name: effectiveDisplayName,
        engine_type: 'mssql',
        is_cloud: false,
        server: fields.server.trim(),
        database_name: fields.databaseName.trim(),
        schema_name: fields.schemaName.trim() || 'dbo',
        driver: 'ODBC Driver 17 for SQL Server',
        use_trusted_connection: true,
        timeout_seconds: fields.timeoutSeconds,
        max_row_limit: fields.maxRowLimit,
        set_as_default: fields.setAsDefault,
      }
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
                {savedProfile
                  ? 'Profile Created Successfully'
                  : fields.engineCategory === 'cloud'
                  ? 'Add Cloud Database Profile'
                  : 'Add Local Database Profile'}
              </h2>
              <p className="profile-modal-subtitle">
                {savedProfile
                  ? 'Verify connectivity or proceed to use this database profile.'
                  : fields.engineCategory === 'cloud'
                  ? 'Configure an approved cloud database profile (AWS RDS, Google Cloud SQL, Azure SQL, or MonsterASP.net) with SQL Authentication.'
                  : fields.engineType === 'sqlite'
                  ? 'Configure an approved local SQLite database profile for natural-language queries.'
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

            {/* Category Tabs: Local vs Cloud */}
            <div className="profile-category-tabs" role="tablist" aria-label="Database Location">
              <button
                type="button"
                role="tab"
                aria-selected={fields.engineCategory === 'local'}
                className={`profile-category-tab ${fields.engineCategory === 'local' ? 'tab-active' : ''}`}
                onClick={() => handleCategoryChange('local')}
                disabled={isSubmitting}
              >
                <span className="category-tab-icon" aria-hidden="true">💻</span>
                <span>Local Database</span>
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={fields.engineCategory === 'cloud'}
                className={`profile-category-tab ${fields.engineCategory === 'cloud' ? 'tab-active' : ''}`}
                onClick={() => handleCategoryChange('cloud')}
                disabled={isSubmitting}
              >
                <span className="category-tab-icon" aria-hidden="true">☁️</span>
                <span>Cloud Database</span>
              </button>
            </div>

            {fields.engineCategory === 'local' ? (
              <>
                {/* Database Engine Selector */}
                <div className="profile-engine-selector-group" role="radiogroup" aria-label="Database Engine">
                  <span className="profile-form-label">Select Database Engine <span className="required-star">*</span></span>
                  <div className="engine-toggle-buttons">
                    <button
                      type="button"
                      role="radio"
                      aria-checked={fields.engineType === 'mssql'}
                      className={`engine-toggle-btn ${fields.engineType === 'mssql' ? 'engine-active' : ''}`}
                      onClick={() => handleEngineTypeChange('mssql')}
                      disabled={isSubmitting}
                    >
                      <span className="engine-icon" aria-hidden="true">🏢</span>
                      <span className="engine-label">Microsoft SQL Server</span>
                    </button>
                    <button
                      type="button"
                      role="radio"
                      aria-checked={fields.engineType === 'sqlite'}
                      className={`engine-toggle-btn ${fields.engineType === 'sqlite' ? 'engine-active' : ''}`}
                      onClick={() => handleEngineTypeChange('sqlite')}
                      disabled={isSubmitting}
                    >
                      <span className="engine-icon" aria-hidden="true">🗃️</span>
                      <span className="engine-label">SQLite</span>
                    </button>
                  </div>
                </div>

                {/* Security Guarantee Notice */}
                <div className="profile-security-banner">
                  <span className="security-icon" aria-hidden="true">🔒</span>
                  <div className="security-text">
                    {fields.engineType === 'sqlite' ? (
                      <>
                        <strong>Local Security Policy:</strong> SQLite local file engine. Read-only authorizer and watchdog timeout enforced. No database file upload or remote queries permitted.
                      </>
                    ) : (
                      <>
                        <strong>Local Security Policy:</strong> Fixed to <strong>Microsoft SQL Server</strong> and{' '}
                        <strong>Windows Integrated Authentication</strong>. No SQL passwords or arbitrary connection strings permitted.
                      </>
                    )}
                  </div>
                </div>

                {/* Collapsible Local Guide Card for Beginners */}
                <div className="platform-guide-card local-guide-card">
                  <div className="guide-card-header">
                    <div className="guide-header-title">
                      <span className="guide-header-icon" aria-hidden="true">
                        {fields.engineType === 'sqlite' ? '🗃️' : '🏢'}
                      </span>
                      <strong>
                        {fields.engineType === 'sqlite'
                          ? 'SQLite Local Database Quick Guide'
                          : 'Local SQL Server Quick Guide'}
                      </strong>
                    </div>
                    <button
                      type="button"
                      className="guide-toggle-btn"
                      onClick={() => setShowLocalGuide(!showLocalGuide)}
                      aria-expanded={showLocalGuide}
                    >
                      {showLocalGuide ? 'Hide Guide ▲' : 'Quick Guide ▼'}
                    </button>
                  </div>
                  {showLocalGuide && (
                    <div className="guide-card-content">
                      {fields.engineType === 'sqlite' ? (
                        <div className="guide-steps-list">
                          <div className="guide-step-item">
                            <div className="guide-step-header">
                              <span className="guide-step-number">1</span>
                              <span className="guide-step-badge">Choose SQLite File</span>
                            </div>
                            <p className="guide-step-text">
                              Click the <strong>Browse...</strong> button next to Database File Path to select any <code>.db</code>, <code>.sqlite</code>, or <code>.sqlite3</code> file on your computer.
                            </p>
                          </div>
                          <div className="guide-step-item">
                            <div className="guide-step-header">
                              <span className="guide-step-number">2</span>
                              <span className="guide-step-badge">Read-Only Safety</span>
                            </div>
                            <p className="guide-step-text">
                              QueryPilot queries locally in read-only mode with strict execution limits. Your database file is never modified or uploaded to the cloud.
                            </p>
                          </div>
                        </div>
                      ) : (
                        <div className="guide-steps-list">
                          <div className="guide-step-item">
                            <div className="guide-step-header">
                              <span className="guide-step-number">1</span>
                              <span className="guide-step-badge">Verify SQL Server Service</span>
                            </div>
                            <p className="guide-step-text">
                              Press <kbd>Win + R</kbd>, type <code>services.msc</code>, and verify that <strong>SQL Server (MSSQLSERVER)</strong> or <strong>SQL Server (SQLEXPRESS)</strong> is <em>Running</em>.
                            </p>
                          </div>
                          <div className="guide-step-item">
                            <div className="guide-step-header">
                              <span className="guide-step-number">2</span>
                              <span className="guide-step-badge">Server Host / Instance</span>
                            </div>
                            <p className="guide-step-text">
                              For default instances, use <code>localhost</code> or <code>127.0.0.1</code>. For named instances, use <code>localhost\SQLEXPRESS</code> or <code>.\SQLEXPRESS</code>.
                            </p>
                          </div>
                          <div className="guide-step-item">
                            <div className="guide-step-header">
                              <span className="guide-step-number">3</span>
                              <span className="guide-step-badge">Integrated Security</span>
                            </div>
                            <p className="guide-step-text">
                              QueryPilot automatically signs in using your active Windows account identity (<code>use_trusted_connection=true</code>). No database password is required.
                            </p>
                          </div>
                        </div>
                      )}
                    </div>
                  )}
                </div>
              </>
            ) : (
              <>
                {/* Cloud Platform Selector Chips */}
                <div className="profile-cloud-provider-selector" role="radiogroup" aria-label="Cloud Database Provider">
                  <div className="cloud-provider-selector-header">
                    <span className="profile-form-label">
                      Select Cloud Platform <span className="required-star">*</span>
                    </span>
                    <span className="cloud-provider-active-badge">
                      Active Guide: <strong>{CLOUD_PROVIDER_CONFIGS[fields.cloudProvider]?.badge || 'Cloud'}</strong>
                    </span>
                  </div>
                  <div className="cloud-provider-chips">
                    {(Object.keys(CLOUD_PROVIDER_CONFIGS) as CloudProvider[]).map((provKey) => {
                      const prov = CLOUD_PROVIDER_CONFIGS[provKey]
                      const isActive = fields.cloudProvider === provKey
                      return (
                        <button
                          key={provKey}
                          type="button"
                          role="radio"
                          aria-checked={isActive}
                          className={`cloud-provider-chip chip-${provKey} ${isActive ? 'provider-active' : ''}`}
                          onClick={() => handleCloudProviderChange(provKey)}
                          disabled={isSubmitting}
                        >
                          <span className="provider-chip-icon" aria-hidden="true">{prov.icon}</span>
                          <span className="provider-chip-label">{prov.badge}</span>
                          {isActive && <span className="provider-chip-check" aria-hidden="true">✓</span>}
                        </button>
                      )
                    })}
                  </div>
                </div>

                {/* Collapsible Step-by-Step Connection Guide */}
                {(() => {
                  const currentCloudConfig = CLOUD_PROVIDER_CONFIGS[fields.cloudProvider]
                  return (
                    <div className="platform-guide-card">
                      <div className="guide-card-header">
                        <div className="guide-header-title">
                          <span className="guide-header-icon" aria-hidden="true">{currentCloudConfig.icon}</span>
                          <div className="guide-header-text-group">
                            <strong className="guide-header-heading">{currentCloudConfig.name} Setup Guide</strong>
                            <span className="guide-header-subtext">Click to view where to get each field from {currentCloudConfig.badge} & firewall instructions</span>
                          </div>
                        </div>
                        <button
                          type="button"
                          className="guide-toggle-btn"
                          onClick={() => setShowGuide(!showGuide)}
                          aria-expanded={showGuide}
                        >
                          {showGuide ? 'Hide Guide ▲' : 'Show Guide ▼'}
                        </button>
                      </div>
                      {showGuide && (
                        <div className="guide-card-content">
                          <p className="guide-desc">{currentCloudConfig.desc}</p>

                          {/* How to fill each form field for this provider */}
                          {currentCloudConfig.fieldGuides && currentCloudConfig.fieldGuides.length > 0 && (
                            <div className="guide-field-guides-section">
                              <div className="guide-section-subtitle">
                                <span className="guide-section-icon" aria-hidden="true">📋</span>
                                <strong>How to Fill Form Fields for {currentCloudConfig.name}:</strong>
                              </div>
                              <div className="guide-field-cards-grid">
                                {currentCloudConfig.fieldGuides.map((fg, idx) => {
                                  const cardKey = `${fields.cloudProvider}-${idx}`
                                  const isCopied = copiedKey === cardKey
                                  return (
                                    <div key={idx} className="guide-field-card">
                                      <div className="guide-field-card-header">
                                        <span className="guide-field-name">{fg.fieldName}</span>
                                        {fg.example && (
                                          <button
                                            type="button"
                                            className={`guide-copy-example-btn ${isCopied ? 'copied' : ''}`}
                                            onClick={() => handleCopyExample(fg.example, cardKey)}
                                            title={`Copy example "${fg.example}" to clipboard`}
                                          >
                                            {isCopied ? '✓ Copied' : 'Copy'}
                                          </button>
                                        )}
                                      </div>
                                      <div className="guide-field-detail">
                                        <span className="guide-field-label">Where to get it:</span> {fg.howToFind}
                                      </div>
                                      <div className="guide-field-example-row">
                                        <span className="guide-field-label">Example:</span> <code>{fg.example}</code>
                                      </div>
                                      {fg.note && (
                                        <div className="guide-field-note">
                                          <span className="field-note-icon">💡</span> {fg.note}
                                        </div>
                                      )}
                                    </div>
                                  )
                                })}
                              </div>
                            </div>
                          )}

                          {/* Network & Platform Configuration Steps */}
                          {currentCloudConfig.steps && currentCloudConfig.steps.length > 0 && (
                            <div className="guide-platform-steps-section">
                              <div className="guide-section-subtitle">
                                <span className="guide-section-icon" aria-hidden="true">⚙️</span>
                                <strong>Network & Firewall Access:</strong>
                              </div>
                              <div className="guide-steps-list">
                                {currentCloudConfig.steps.map((st, sIdx) => (
                                  <div key={sIdx} className="guide-step-item">
                                    <div className="guide-step-header">
                                      <span className="guide-step-number">{sIdx + 1}</span>
                                      <span className="guide-step-badge">{st.title}</span>
                                    </div>
                                    <p className="guide-step-text">{st.detail}</p>
                                    {st.substeps && st.substeps.length > 0 && (
                                      <ul className="guide-substeps-list">
                                        {st.substeps.map((sub, subIdx) => (
                                          <li key={subIdx} className="guide-substep-item">
                                            {sub}
                                          </li>
                                        ))}
                                      </ul>
                                    )}
                                  </div>
                                ))}
                              </div>
                            </div>
                          )}

                          {currentCloudConfig.firewallTip && (
                            <div className="guide-firewall-tip" role="note">
                              <span className="firewall-tip-icon" aria-hidden="true">🛡️</span>
                              <span>{currentCloudConfig.firewallTip}</span>
                            </div>
                          )}
                        </div>
                      )}
                    </div>
                  )
                })()}
              </>
            )}

            {/* SECTION 1: Profile Identity */}
            <div className="profile-form-section">
              <div className="form-section-header">
                <span className="form-section-icon" aria-hidden="true">🏷️</span>
                <span className="form-section-title">Profile Identity</span>
                <span className="form-section-sub">Required identifiers for QueryPilot</span>
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
                    placeholder={
                      fields.engineCategory === 'cloud'
                        ? 'e.g. cloud_db_prod'
                        : fields.engineType === 'sqlite'
                        ? 'e.g. sales_sqlite_local'
                        : 'e.g. sales_db_local'
                    }
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
                    placeholder={
                      fields.engineCategory === 'cloud'
                        ? 'e.g. Production Cloud DB'
                        : fields.engineType === 'sqlite'
                        ? 'e.g. Local SQLite Store'
                        : 'e.g. Local Sales Warehouse'
                    }
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
              </div>
            </div>

            {/* SECTION 2: Server & Connection */}
            <div className="profile-form-section">
              <div className="form-section-header">
                <span className="form-section-icon" aria-hidden="true">
                  {fields.engineCategory === 'cloud' ? '🌐' : fields.engineType === 'sqlite' ? '📁' : '🖥️'}
                </span>
                <span className="form-section-title">
                  {fields.engineCategory === 'cloud'
                    ? 'Server Endpoint & Database'
                    : fields.engineType === 'sqlite'
                    ? 'SQLite Database File'
                    : 'Local Server & Database'}
                </span>
                <span className="form-section-sub">
                  {fields.engineCategory === 'cloud'
                    ? 'Host, port and target catalog'
                    : fields.engineType === 'sqlite'
                    ? 'Local database path'
                    : 'Local instance and catalog'}
                </span>
              </div>
              <div className="profile-form-grid">
                {fields.engineCategory === 'cloud' ? (
                  <>
                    {/* Cloud Host */}
                    <div className="profile-form-group">
                      <label htmlFor="onboarding-server" className="profile-form-label">
                        Server Host / Endpoint <span className="required-star">*</span>
                      </label>
                      <input
                        id="onboarding-server"
                        type="text"
                        className={`profile-form-input ${fieldErrors.server ? 'input-error' : ''}`}
                        placeholder="e.g. db52589.public.databaseasp.net or mydb.rds.amazonaws.com"
                        value={fields.server}
                        onChange={(e) => handleFieldChange('server', e.target.value)}
                        disabled={isSubmitting}
                        maxLength={255}
                        required
                      />
                      {fieldErrors.server && (
                        <span className="field-error-text" role="alert">{fieldErrors.server}</span>
                      )}
                      <span className="field-help-text">
                        Remote server hostname, endpoint, or public IPv4 address.
                      </span>
                    </div>

                    {/* Port */}
                    <div className="profile-form-group">
                      <label htmlFor="onboarding-port" className="profile-form-label">
                        TCP Port
                      </label>
                      <input
                        id="onboarding-port"
                        type="text"
                        className={`profile-form-input ${fieldErrors.port ? 'input-error' : ''}`}
                        placeholder="1433"
                        value={fields.port}
                        onChange={(e) => handleFieldChange('port', e.target.value)}
                        disabled={isSubmitting}
                        maxLength={5}
                      />
                      {fieldErrors.port && (
                        <span className="field-error-text" role="alert">{fieldErrors.port}</span>
                      )}
                      <span className="field-help-text">Standard SQL Server TCP port (default: 1433).</span>
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
                        placeholder="e.g. db52589 or sales_db"
                        value={fields.databaseName}
                        onChange={(e) => handleFieldChange('databaseName', e.target.value)}
                        disabled={isSubmitting}
                        maxLength={128}
                        required
                      />
                      {fieldErrors.databaseName && (
                        <span className="field-error-text" role="alert">{fieldErrors.databaseName}</span>
                      )}
                      {fields.cloudProvider === 'monsterasp' && fields.databaseName.trim().endsWith('.databaseasp.net') && (
                        <span className="field-help-text" style={{ color: '#fbbf24', marginTop: '2px' }}>
                          💡 Tip: On MonsterASP.net, your Database Name is usually just <code>{fields.databaseName.trim().split('.')[0]}</code> (without <code>.databaseasp.net</code>).
                        </span>
                      )}
                      <span className="field-help-text">
                        Target database name on the remote SQL Server.
                      </span>
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
                      <span className="field-help-text">Default relational schema (typically dbo).</span>
                    </div>
                  </>
                ) : fields.engineType === 'mssql' ? (
                  <>
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
                    <div className="profile-form-group profile-form-full-width">
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
                  </>
                ) : (
                  /* SQLite Database File Path */
                  <div className="profile-form-group profile-form-full-width">
                    <label htmlFor="onboarding-database-path" className="profile-form-label">
                      Database File Path <span className="required-star">*</span>
                    </label>
                    <div className="profile-input-with-button">
                      <input
                        id="onboarding-database-path"
                        type="text"
                        className={`profile-form-input ${fieldErrors.databasePath ? 'input-error' : ''}`}
                        placeholder="e.g. C:\sqlite\data\chinook.db or /var/data/chinook.db"
                        value={fields.databasePath}
                        onChange={(e) => handleFieldChange('databasePath', e.target.value)}
                        disabled={isSubmitting}
                        maxLength={1000}
                        required
                      />
                      <input
                        ref={fileInputRef}
                        type="file"
                        id="onboarding-file-picker"
                        accept=".db,.sqlite,.sqlite3,*"
                        style={{ display: 'none' }}
                        onChange={handleNativeFileSelect}
                        tabIndex={-1}
                        aria-hidden="true"
                      />
                      <button
                        type="button"
                        className="profile-browse-btn"
                        onClick={handleBrowseFileClick}
                        disabled={isSubmitting}
                        title="Select SQLite database file from disk"
                        aria-label="Browse for SQLite database file"
                      >
                        Browse...
                      </button>
                    </div>
                    {fieldErrors.databasePath && (
                      <span className="field-error-text" role="alert">{fieldErrors.databasePath}</span>
                    )}
                    <span className="field-help-text">
                      Absolute local filesystem path to an existing SQLite database file. No files are uploaded to the browser or cloud.
                    </span>
                  </div>
                )}
              </div>
            </div>

            {/* SECTION 3: SQL Authentication & Credentials (Cloud Only) */}
            {fields.engineCategory === 'cloud' && (
              <div className="profile-form-section">
                <div className="form-section-header">
                  <span className="form-section-icon" aria-hidden="true">🔐</span>
                  <span className="form-section-title">SQL Authentication & TLS</span>
                  <span className="form-section-sub">Encrypted remote credentials</span>
                </div>
                <div className="profile-form-grid">
                  {/* Username */}
                  <div className="profile-form-group">
                    <label htmlFor="onboarding-user" className="profile-form-label">
                      SQL Username <span className="required-star">*</span>
                    </label>
                    <input
                      id="onboarding-user"
                      type="text"
                      className={`profile-form-input ${fieldErrors.user ? 'input-error' : ''}`}
                      placeholder="e.g. db52589, admin, or sqlserver"
                      value={fields.user}
                      onChange={(e) => handleFieldChange('user', e.target.value)}
                      disabled={isSubmitting}
                      maxLength={128}
                      required
                      autoComplete="off"
                    />
                    {fieldErrors.user && (
                      <span className="field-error-text" role="alert">{fieldErrors.user}</span>
                    )}
                    <span className="field-help-text">
                      SQL Server login username.
                    </span>
                  </div>

                  {/* Password with Eye Toggle */}
                  <div className="profile-form-group">
                    <label htmlFor="onboarding-password" className="profile-form-label">
                      SQL Password <span className="required-star">*</span>
                    </label>
                    <div className="profile-password-wrapper">
                      <input
                        id="onboarding-password"
                        type={showPassword ? 'text' : 'password'}
                        className={`profile-form-input ${fieldErrors.password ? 'input-error' : ''}`}
                        placeholder="••••••••••••"
                        value={fields.password}
                        onChange={(e) => handleFieldChange('password', e.target.value)}
                        disabled={isSubmitting}
                        required
                        autoComplete="new-password"
                      />
                      <button
                        type="button"
                        className="profile-pwd-toggle-btn"
                        onClick={() => setShowPassword(!showPassword)}
                        title={showPassword ? 'Hide password' : 'Show password'}
                        aria-label={showPassword ? 'Hide password' : 'Show password'}
                        tabIndex={-1}
                      >
                        {showPassword ? '🙈' : '👁️'}
                      </button>
                    </div>
                    {fieldErrors.password && (
                      <span className="field-error-text" role="alert">{fieldErrors.password}</span>
                    )}
                    <span className="field-help-text">Stored securely with local OS-level ACL protection.</span>
                  </div>

                  {/* TLS & Certificate Options */}
                  <div className="profile-form-group profile-form-full-width profile-checkbox-row">
                    <label className="profile-checkbox-label">
                      <input
                        type="checkbox"
                        checked={fields.encrypt}
                        onChange={(e) => handleFieldChange('encrypt', e.target.checked)}
                        disabled={isSubmitting}
                      />
                      <span>Enforce TLS Transport Encryption (<code>Encrypt=yes</code>)</span>
                    </label>
                    <label className="profile-checkbox-label">
                      <input
                        type="checkbox"
                        checked={fields.trustServerCertificate}
                        onChange={(e) => handleFieldChange('trustServerCertificate', e.target.checked)}
                        disabled={isSubmitting}
                      />
                      <span>Trust Server Certificate (<code>TrustServerCertificate=yes</code>)</span>
                    </label>
                  </div>
                </div>
              </div>
            )}

            {/* SECTION 4: Execution Guardrails & Preferences */}
            <div className="profile-form-section">
              <div className="form-section-header">
                <span className="form-section-icon" aria-hidden="true">⚡</span>
                <span className="form-section-title">Execution Guardrails</span>
                <span className="form-section-sub">Query safety ceilings</span>
              </div>
              <div className="profile-form-grid">
                {/* Timeout */}
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

                {/* Max Row Limit */}
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

              {/* Set as Default Profile Checkbox */}
              <div className="profile-checkbox-row profile-default-row">
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
            </div>

            {/* Locked security attributes summary */}
            <div className="profile-locked-attributes" aria-label="Fixed Security Configuration">
              {fields.engineCategory === 'cloud' ? (
                <>
                  <span className="locked-badge">
                    <span className="locked-icon" aria-hidden="true">{CLOUD_PROVIDER_CONFIGS[fields.cloudProvider]?.icon || '☁️'}</span>
                    Platform: <strong>{CLOUD_PROVIDER_CONFIGS[fields.cloudProvider]?.badge || 'Cloud MSSQL'}</strong>
                  </span>
                  <span className="locked-badge">
                    <span className="locked-icon" aria-hidden="true">🔒</span>
                    Auth: <strong>SQL Authentication</strong>
                  </span>
                  <span className="locked-badge">
                    <span className="locked-icon" aria-hidden="true">🛡️</span>
                    TLS: <strong>Encrypted</strong>
                  </span>
                </>
              ) : fields.engineType === 'sqlite' ? (
                <>
                  <span className="locked-badge">
                    <span className="locked-icon" aria-hidden="true">🔒</span>
                    Engine: <strong>SQLite</strong>
                  </span>
                  <span className="locked-badge">
                    <span className="locked-icon" aria-hidden="true">🔒</span>
                    Mode: <strong>Read-Only Authorizer</strong>
                  </span>
                  <span className="locked-badge">
                    <span className="locked-icon" aria-hidden="true">🔒</span>
                    Schema: <strong>main</strong>
                  </span>
                </>
              ) : (
                <>
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
                </>
              )}
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
                <span className="meta-row-label">Engine:</span>
                <span className="meta-row-value">{fields.engineType === 'sqlite' ? 'SQLite' : 'Microsoft SQL Server'}</span>
              </div>
              {fields.engineType === 'mssql' ? (
                <>
                  <div className="meta-row">
                    <span className="meta-row-label">Platform:</span>
                    <span className="meta-row-value">
                      {savedProfile.is_cloud
                        ? `${CLOUD_PROVIDER_CONFIGS[fields.cloudProvider]?.name || 'Cloud MSSQL'}`
                        : 'Local SQL Server'}
                    </span>
                  </div>
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
                    <span className="meta-row-value">
                      {savedProfile.is_cloud
                        ? `SQL Server Authentication (${fields.user})`
                        : 'Windows Integrated (Trusted Connection)'}
                    </span>
                  </div>
                </>
              ) : (
                <>
                  <div className="meta-row">
                    <span className="meta-row-label">Schema:</span>
                    <span className="meta-row-value">{savedProfile.schema_name || 'main'}</span>
                  </div>
                  <div className="meta-row">
                    <span className="meta-row-label">Security Policy:</span>
                    <span className="meta-row-value">Read-Only Authorizer & Watchdog Timer</span>
                  </div>
                </>
              )}
            </div>

            <div className="connection-test-section">
              <h4 className="conn-test-title">Explicit Database Connectivity Test</h4>
              <p className="conn-test-desc">
                {fields.engineType === 'sqlite'
                  ? 'Execute an isolated, bounded probe to verify that QueryPilot can access and query the local SQLite database file.'
                  : 'Execute an isolated, bounded query (SELECT DB_NAME()) to verify that QueryPilot can communicate with your local SQL Server database.'}
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
                      ? `Verified database: ${connectionTestResult.database_name || fields.displayName || savedProfile.profile_id}`
                      : mapFailureCodeToFriendlyMessage(
                          connectionTestResult.failure_code,
                          connectionTestResult.message,
                          savedProfile?.is_cloud,
                          fields.cloudProvider
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
                      <span>{fields.engineType === 'sqlite' ? 'Testing SQLite Connection...' : 'Testing SQL Server Connection...'}</span>
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
