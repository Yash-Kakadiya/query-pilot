import type {
  AgentDatabaseSchema,
  ModelFacingSchemaContext,
  ProfileSummary,
  TestConnectionResponse,
} from '../types/api.ts'
import type { SchemaIntrospectionStatus } from '../services/agentSession.ts'
import './ProfileSelector.css'

export interface ProfileSelectorProps {
  profiles: ProfileSummary[]
  selectedProfileId: string | null
  selectedProfile: ProfileSummary | null
  schemaStatus: SchemaIntrospectionStatus
  schemaContext: ModelFacingSchemaContext | null
  rawSchema: AgentDatabaseSchema | null
  schemaError?: string
  onSelectProfile: (profileId: string | null) => void
  onRetrySchema: () => void
  onOpenOnboarding?: () => void
  onTestConnection?: (profileId: string) => Promise<void> | void
  connectionTestStatus?: 'idle' | 'testing' | 'success' | 'failed'
  connectionTestResult?: TestConnectionResponse | null
}

/**
 * Accessible database connection profile selector and schema introspection component.
 *
 * Exposes approved loopback connection profiles discovered from the local agent daemon,
 * manages schema introspection state transitions, displays bounded schema statistics,
 * provides explicit connection testing, and allows triggering the profile onboarding flow.
 */
export function ProfileSelector({
  profiles,
  selectedProfileId,
  selectedProfile,
  schemaStatus,
  schemaContext,
  schemaError,
  onSelectProfile,
  onRetrySchema,
  onOpenOnboarding,
  onTestConnection,
  connectionTestStatus = 'idle',
  connectionTestResult,
}: ProfileSelectorProps) {
  const hasProfiles = profiles.length > 0
  const isSchemaLoading = schemaStatus === 'loading'
  const isTestingConn = connectionTestStatus === 'testing'

  return (
    <div
      className="profile-selector-card"
      role="region"
      aria-label="Database Profile and Schema Introspection"
    >
      <div className="profile-selector-header">
        <div className="profile-title-group">
          <span className="profile-header-icon" aria-hidden="true">🗄️</span>
          <h2 className="profile-selector-title">Target Database Profile</h2>
        </div>
        {onOpenOnboarding && (
          <button
            type="button"
            className="profile-add-header-btn"
            onClick={onOpenOnboarding}
            aria-label="Configure new local database profile"
          >
            + Add Profile
          </button>
        )}
        <p className="profile-selector-desc">
          Select an approved local database profile to introspect its schema metadata.
        </p>
      </div>

      {!hasProfiles ? (
        <div className="profile-empty-alert" role="status">
          <span aria-hidden="true">ℹ️</span>
          <div className="empty-alert-body">
            <span>
              No database profiles configured in local agent. Define profiles in{' '}
              <code>~/.query_pilot/profiles.json</code> or click <strong>Add Profile</strong> to configure a local SQL Server profile.
            </span>
            {onOpenOnboarding && (
              <button
                type="button"
                className="profile-add-header-btn profile-empty-add-btn"
                onClick={onOpenOnboarding}
              >
                + Add Profile
              </button>
            )}
          </div>
        </div>
      ) : (
        <div className="profile-select-row">
          <label htmlFor="database-profile-select" className="profile-label">
            Active Profile:
          </label>
          <select
            id="database-profile-select"
            className="profile-dropdown"
            value={selectedProfileId || ''}
            onChange={(e) => onSelectProfile(e.target.value || null)}
            disabled={isSchemaLoading || isTestingConn}
            aria-describedby="profile-meta-info"
          >
            <option value="">-- Choose a database profile --</option>
            {profiles.map((p) => (
              <option key={p.profile_id} value={p.profile_id}>
                {p.display_name} ({p.profile_id})
              </option>
            ))}
          </select>

          {selectedProfileId && onTestConnection && (
            <button
              type="button"
              className="profile-action-btn profile-test-btn"
              onClick={() => onTestConnection(selectedProfileId)}
              disabled={isTestingConn || isSchemaLoading}
              aria-label="Test connection to active database profile"
            >
              {isTestingConn ? 'Testing Connection...' : 'Test Connection'}
            </button>
          )}

          {selectedProfileId && (
            <button
              type="button"
              className="profile-clear-btn"
              onClick={() => onSelectProfile(null)}
              disabled={isSchemaLoading || isTestingConn}
              aria-label="Clear profile selection"
            >
              Clear
            </button>
          )}
        </div>
      )}

      {/* Explicit Connection Test Result Banner */}
      {selectedProfile && connectionTestResult && (
        <div
          className={`profile-conn-banner ${
            connectionTestResult.connected ? 'conn-banner-success' : 'conn-banner-failure'
          }`}
          role="status"
        >
          <span className="conn-banner-icon" aria-hidden="true">
            {connectionTestResult.connected ? '✓' : '⚠️'}
          </span>
          <span className="conn-banner-text">
            {connectionTestResult.connected
              ? `Connection Verified: ${connectionTestResult.database_name || selectedProfile.display_name} (${Math.round(connectionTestResult.latency_ms || 0)} ms)`
              : `Connection Failed: ${connectionTestResult.message}`}
          </span>
        </div>
      )}

      {/* Selected Profile Details Pills */}
      {selectedProfile && (
        <div id="profile-meta-info" className="profile-meta-pills" aria-label="Profile Metadata">
          <span className="meta-pill">
            <span className="meta-pill-label">ID:</span>
            <code>{selectedProfile.profile_id}</code>
          </span>
          <span className="meta-pill">
            <span className="meta-pill-label">Default Schema:</span>
            {selectedProfile.schema_name || 'dbo'}
          </span>
          <span className="meta-pill">
            <span className="meta-pill-label">Timeout:</span>
            {selectedProfile.timeout_seconds}s
          </span>
          <span className="meta-pill">
            <span className="meta-pill-label">Row Limit:</span>
            {selectedProfile.max_row_limit}
          </span>
          {selectedProfile.has_allowlist && (
            <span className="meta-pill">
              <span className="meta-pill-label">Security:</span>
              Restricted Tables
            </span>
          )}
        </div>
      )}

      {/* Schema Introspection Status Presentation */}
      {selectedProfileId && (
        <div className="schema-section">
          {schemaStatus === 'loading' && (
            <div
              className="schema-status schema-status-loading"
              role="status"
              aria-live="polite"
            >
              <span className="spinner" aria-hidden="true" />
              <span>Introspecting database schema from local agent...</span>
            </div>
          )}

          {schemaStatus === 'loaded' && schemaContext && (
            <div className="schema-status schema-status-loaded" role="status">
              <div className="schema-summary-header">
                <span className="schema-check-icon" aria-hidden="true">✓</span>
                <span className="schema-summary-text">
                  Schema Projected: <strong>{schemaContext.database_name}</strong> &bull;{' '}
                  {schemaContext.tables.length} table
                  {schemaContext.tables.length === 1 ? '' : 's'} available for querying
                </span>
              </div>

              {schemaContext.tables.length > 0 && (
                <div className="schema-table-chips" aria-label="Available Tables">
                  {schemaContext.tables.map((table) => (
                    <span
                      key={table.table_name}
                      className="schema-table-chip"
                      title={`${table.columns.length} columns`}
                    >
                      {table.table_name}{' '}
                      <small>({table.columns.length} cols)</small>
                    </span>
                  ))}
                </div>
              )}
            </div>
          )}

          {schemaStatus === 'error' && (
            <div className="schema-status schema-status-error" role="alert">
              <div className="schema-error-content">
                <span className="schema-error-icon" aria-hidden="true">⚠️</span>
                <span className="schema-error-message">
                  {schemaError || 'Failed to introspect database schema.'}
                </span>
              </div>
              <button
                type="button"
                className="schema-retry-btn"
                onClick={onRetrySchema}
              >
                Retry Loading Schema
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
