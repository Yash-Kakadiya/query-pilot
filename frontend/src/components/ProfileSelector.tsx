import type {
  AgentDatabaseSchema,
  ModelFacingSchemaContext,
  ProfileSummary,
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
}

/**
 * Accessible database connection profile selector and schema introspection component.
 *
 * Exposes approved loopback connection profiles discovered from the local agent daemon,
 * manages schema introspection state transitions, displays bounded schema statistics,
 * and handles retry on schema introspection failure.
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
}: ProfileSelectorProps) {
  const hasProfiles = profiles.length > 0
  const isSchemaLoading = schemaStatus === 'loading'

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
        <p className="profile-selector-desc">
          Select an approved local database profile to introspect its schema metadata.
        </p>
      </div>

      {!hasProfiles ? (
        <div className="profile-empty-alert" role="status">
          <span aria-hidden="true">ℹ️</span>
          <span>
            No database profiles configured in local agent. Define profiles in{' '}
            <code>~/.query_pilot/profiles.yaml</code> to begin.
          </span>
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
            disabled={isSchemaLoading}
            aria-describedby="profile-meta-info"
          >
            <option value="">-- Choose a database profile --</option>
            {profiles.map((p) => (
              <option key={p.profile_id} value={p.profile_id}>
                {p.display_name} ({p.profile_id})
              </option>
            ))}
          </select>

          {selectedProfileId && (
            <button
              type="button"
              className="profile-clear-btn"
              onClick={() => onSelectProfile(null)}
              disabled={isSchemaLoading}
              aria-label="Clear profile selection"
            >
              Clear
            </button>
          )}
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
