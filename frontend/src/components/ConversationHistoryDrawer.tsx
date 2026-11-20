import { useCallback, useEffect, useMemo, useState } from 'react'
import { extractUniqueTags, filterHistoryItems } from '../services/historyFilter'
import { queryHistoryCoordinator } from '../services/queryHistory'
import type { ConversationHistoryTurn } from '../services/sqlGeneration'
import type { HistoryItem } from '../types/history'
import './ConversationHistoryDrawer.css'

export type HistoryTab = 'recent' | 'favorites' | 'turns'

export interface ConversationHistoryDrawerProps {
  isOpen: boolean
  onClose: () => void
  history?: ConversationHistoryTurn[]
  onRestoreTurn?: (index: number) => void
  onNewTopic?: () => void
  onSelectQuery?: (sql: string, profileId?: string) => void
  onRunQuery?: (sql: string, profileId?: string) => void
  activeProfileId?: string | null
  activeProfileName?: string | null
}

function formatTimestamp(timestamp: number | string): string {
  if (typeof timestamp === 'string') return timestamp
  try {
    const d = new Date(timestamp)
    return `${d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })} • ${d.toLocaleDateString([], { month: 'short', day: 'numeric' })}`
  } catch {
    return ''
  }
}

export function ConversationHistoryDrawer({
  isOpen,
  onClose,
  history = [],
  onRestoreTurn,
  onNewTopic,
  onSelectQuery,
  onRunQuery,
  activeProfileId,
  activeProfileName,
}: ConversationHistoryDrawerProps) {
  const [activeTab, setActiveTab] = useState<HistoryTab>('recent')
  const [historyItems, setHistoryItems] = useState<HistoryItem[]>([])
  const [search, setSearch] = useState<string>('')
  const [selectedTag, setSelectedTag] = useState<string | null>(null)
  const [filterActiveProfile, setFilterActiveProfile] = useState<boolean>(false)
  const [copiedId, setCopiedId] = useState<string | null>(null)
  const [showClearConfirm, setShowClearConfirm] = useState<boolean>(false)

  // Subscribe to coordinator items & sync with daemon on open
  useEffect(() => {
    if (!isOpen) return

    const unsubscribe = queryHistoryCoordinator.subscribe((items) => {
      setHistoryItems(items)
    })
    void queryHistoryCoordinator.syncWithDaemon()

    return () => {
      unsubscribe()
    }
  }, [isOpen])

  // Extract unique tags from current history items
  const uniqueTags = useMemo(() => extractUniqueTags(historyItems), [historyItems])

  // Filter items based on active tab, search, profile, and tag
  const filteredItems = useMemo(() => {
    return filterHistoryItems(historyItems, {
      tab: activeTab === 'favorites' ? 'favorites' : 'recent',
      search,
      profileId: filterActiveProfile && activeProfileId ? activeProfileId : undefined,
      tag: selectedTag || undefined,
    })
  }, [historyItems, activeTab, search, filterActiveProfile, activeProfileId, selectedTag])

  const favoritesCount = useMemo(() => {
    return historyItems.filter((i) => i.is_favorite).length
  }, [historyItems])

  const handleCopySql = useCallback((item: HistoryItem) => {
    if (navigator?.clipboard?.writeText) {
      void navigator.clipboard.writeText(item.sql)
      setCopiedId(item.id)
      setTimeout(() => setCopiedId(null), 1800)
    }
  }, [])

  const handleToggleStar = useCallback(async (id: string, e: React.MouseEvent) => {
    e.stopPropagation()
    await queryHistoryCoordinator.toggleStar(id)
  }, [])

  const handleDeleteItem = useCallback(async (id: string, e: React.MouseEvent) => {
    e.stopPropagation()
    await queryHistoryCoordinator.deleteEntry(id)
  }, [])

  const handleClearHistory = useCallback(async () => {
    await queryHistoryCoordinator.clearHistory(false)
    setShowClearConfirm(false)
  }, [])

  const handleExecute = useCallback((item: HistoryItem) => {
    if (onRunQuery) {
      onRunQuery(item.sql, item.profile_id)
    } else if (onSelectQuery) {
      onSelectQuery(item.sql, item.profile_id)
    }
    onClose()
  }, [onRunQuery, onSelectQuery, onClose])

  if (!isOpen) {
    return null
  }

  const maxTurns = 3

  return (
    <div
      className="history-drawer-backdrop"
      role="dialog"
      aria-modal="true"
      aria-labelledby="history-drawer-title"
      onClick={onClose}
    >
      <div
        className="history-drawer-container"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="history-drawer-header">
          <div className="history-header-title-group">
            <span className="history-header-icon" aria-hidden="true">📜</span>
            <div>
              <h2 id="history-drawer-title" className="history-drawer-title">
                Query History & Favorites
              </h2>
              <span className="history-turns-counter">
                {historyItems.length} stored • {favoritesCount} starred ⭐
              </span>
            </div>
          </div>

          <div className="history-header-actions">
            {activeTab === 'recent' && historyItems.length > 0 && (
              <button
                type="button"
                className="history-clear-btn"
                onClick={() => setShowClearConfirm(true)}
                title="Clear unstarred history entries (preserves favorites)"
              >
                Clear Unstarred
              </button>
            )}
            {activeTab === 'turns' && history.length > 0 && onNewTopic && (
              <button
                type="button"
                className="history-new-topic-btn"
                onClick={() => {
                  onNewTopic()
                  onClose()
                }}
                aria-label="Start new topic and reset conversation context"
              >
                + New Topic
              </button>
            )}
            <button
              type="button"
              className="history-close-btn"
              onClick={onClose}
              aria-label="Close conversation history drawer"
            >
              ✕
            </button>
          </div>
        </div>

        {/* Clear Confirmation Banner */}
        {showClearConfirm && (
          <div className="history-clear-confirm-banner" role="alert">
            <div className="history-clear-confirm-text">
              <strong>Clear Query History?</strong>
              <p>Unstarred history entries will be cleared. Starred favorites are strictly preserved.</p>
            </div>
            <div className="history-clear-confirm-buttons">
              <button
                type="button"
                className="history-confirm-btn danger"
                onClick={handleClearHistory}
              >
                Clear History
              </button>
              <button
                type="button"
                className="history-confirm-btn cancel"
                onClick={() => setShowClearConfirm(false)}
              >
                Cancel
              </button>
            </div>
          </div>
        )}

        {/* Navigation Tabs */}
        <div className="history-tabs-bar" role="tablist">
          <button
            type="button"
            role="tab"
            aria-selected={activeTab === 'recent'}
            className={`history-tab-btn ${activeTab === 'recent' ? 'active' : ''}`}
            onClick={() => setActiveTab('recent')}
          >
            🕒 Recent ({historyItems.length})
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={activeTab === 'favorites'}
            className={`history-tab-btn ${activeTab === 'favorites' ? 'active' : ''}`}
            onClick={() => setActiveTab('favorites')}
          >
            ⭐ Starred Favorites ({favoritesCount})
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={activeTab === 'turns'}
            className={`history-tab-btn ${activeTab === 'turns' ? 'active' : ''}`}
            onClick={() => setActiveTab('turns')}
          >
            💬 Active Turns ({history.length}/{maxTurns})
          </button>
        </div>

        {/* Search & Filter Bar (shown on Recent and Favorites tabs) */}
        {activeTab !== 'turns' && (
          <div className="history-filter-section">
            <div className="history-search-wrapper">
              <span className="history-search-icon" aria-hidden="true">🔍</span>
              <input
                type="text"
                className="history-search-input"
                placeholder="Search SQL, question, profile, or tag..."
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                aria-label="Search query history"
              />
              {search && (
                <button
                  type="button"
                  className="history-search-clear"
                  onClick={() => setSearch('')}
                  aria-label="Clear search text"
                >
                  ✕
                </button>
              )}
            </div>

            {/* Profile & Tag Filters */}
            <div className="history-filter-chips">
              {activeProfileId && (
                <button
                  type="button"
                  className={`history-filter-chip ${filterActiveProfile ? 'active' : ''}`}
                  onClick={() => setFilterActiveProfile(!filterActiveProfile)}
                  title={`Filter to current profile: ${activeProfileName || activeProfileId}`}
                >
                  {filterActiveProfile ? '✓ ' : ''}{activeProfileName || activeProfileId} Only
                </button>
              )}
              {uniqueTags.map((tag) => (
                <button
                  key={tag}
                  type="button"
                  className={`history-filter-chip tag-chip ${selectedTag === tag ? 'active' : ''}`}
                  onClick={() => setSelectedTag(selectedTag === tag ? null : tag)}
                >
                  #{tag}
                </button>
              ))}
            </div>
          </div>
        )}

        {/* Drawer Body */}
        <div className="history-drawer-body">
          {activeTab === 'turns' ? (
            /* Multi-turn conversation context view */
            history.length === 0 ? (
              <div className="history-empty-state" role="status">
                <span className="history-empty-icon" aria-hidden="true">💬</span>
                <p className="history-empty-title">No Active Conversation Turns</p>
                <p className="history-empty-desc">
                  Ask a question or execute a query against the selected profile to start a multi-turn conversation. Up to 3 follow-up turns are bounded.
                </p>
              </div>
            ) : (
              <div className="history-turns-list" role="feed" aria-label="Prior conversation turns">
                {history.map((turn, idx) => {
                  const isLatest = idx === history.length - 1
                  return (
                    <article
                      key={turn.turnId || idx}
                      className={`history-turn-card ${isLatest ? 'history-turn-active' : ''}`}
                      aria-label={`Conversation Turn ${idx + 1}`}
                    >
                      <div className="history-turn-header">
                        <div className="history-turn-meta">
                          <span className="history-turn-badge">
                            Turn {idx + 1}
                          </span>
                          {isLatest && (
                            <span className="history-active-chip" role="status">
                              Active Context
                            </span>
                          )}
                          <span className="history-turn-timestamp">
                            {turn.timestamp}
                          </span>
                        </div>

                        {onRestoreTurn && (
                          <button
                            type="button"
                            className="history-restore-btn"
                            onClick={() => {
                              onRestoreTurn(idx)
                              onClose()
                            }}
                            aria-label={`Restore question from Turn ${idx + 1}`}
                          >
                            Restore Question
                          </button>
                        )}
                      </div>

                      <div className="history-turn-question">
                        <p className="history-question-text">
                          "{turn.question}"
                        </p>
                      </div>

                      {turn.resolvedIntent && (
                        <div className="history-turn-intent">
                          <span className="history-intent-label">Resolved Intent:</span>
                          <span className="history-intent-value">{turn.resolvedIntent}</span>
                        </div>
                      )}

                      {turn.synthesizedSummary && (
                        <div className="history-turn-summary" role="note">
                          <span className="history-summary-icon" aria-hidden="true">💡</span>
                          <span className="history-summary-text">{turn.synthesizedSummary}</span>
                        </div>
                      )}

                      <div className="history-turn-footer">
                        {turn.rowCount !== undefined && (
                          <span className="history-meta-pill">
                            {turn.rowCount} row{turn.rowCount === 1 ? '' : 's'} returned
                          </span>
                        )}
                        {turn.resultColumns && turn.resultColumns.length > 0 && (
                          <span className="history-meta-pill history-cols-pill">
                            {turn.resultColumns.length} col{turn.resultColumns.length === 1 ? '' : 's'}: {turn.resultColumns.slice(0, 3).join(', ')}{turn.resultColumns.length > 3 ? '...' : ''}
                          </span>
                        )}
                      </div>
                    </article>
                  )
                })}
              </div>
            )
          ) : (
            /* Persistent Query History & Starred Favorites view */
            filteredItems.length === 0 ? (
              <div className="history-empty-state" role="status">
                <span className="history-empty-icon" aria-hidden="true">
                  {activeTab === 'favorites' ? '⭐' : '🕒'}
                </span>
                <p className="history-empty-title">
                  {activeTab === 'favorites'
                    ? 'No Starred Favorites Yet'
                    : search
                    ? 'No Matching Queries Found'
                    : 'No Query History Yet'}
                </p>
                <p className="history-empty-desc">
                  {activeTab === 'favorites'
                    ? 'Click the ⭐ star button on any executed query in history to save it here for instant 1-click re-runs.'
                    : search
                    ? `No query matched "${search}". Try searching another keyword or clearing filters.`
                    : 'Queries you execute against any database profile are automatically recorded here and preserved across app restarts.'}
                </p>
              </div>
            ) : (
              <div className="history-persistent-list" role="feed" aria-label="Query history list">
                {filteredItems.map((item) => {
                  const isCopied = copiedId === item.id
                  return (
                    <article
                      key={item.id}
                      className={`history-query-card ${item.is_favorite ? 'favorite-border' : ''}`}
                      aria-label={`Query executed on ${item.profile_name}`}
                    >
                      {/* Card Header */}
                      <div className="history-query-card-header">
                        <div className="history-card-meta-left">
                          <span className={`history-status-badge ${item.status}`}>
                            {item.status === 'succeeded' ? '✓' : item.status === 'failed' ? '✕' : '⊘'} {item.status}
                          </span>
                          <span className="history-profile-badge" title={item.profile_name}>
                            {item.profile_name}
                          </span>
                          <span className="history-engine-badge">
                            {item.engine_type.toUpperCase()}
                          </span>
                        </div>

                        <div className="history-card-meta-right">
                          <span className="history-card-timestamp">
                            {formatTimestamp(item.timestamp)}
                          </span>
                          <button
                            type="button"
                            className={`history-star-btn ${item.is_favorite ? 'starred' : ''}`}
                            onClick={(e) => handleToggleStar(item.id, e)}
                            title={item.is_favorite ? 'Starred favorite (click to unstar)' : 'Star as favorite'}
                            aria-label={item.is_favorite ? 'Starred favorite' : 'Star as favorite'}
                          >
                            {item.is_favorite ? '⭐' : '☆'}
                          </button>
                        </div>
                      </div>

                      {/* Natural Language Question (if present) */}
                      {item.question && (
                        <div className="history-query-question">
                          <span className="history-question-icon" aria-hidden="true">💬</span>
                          <span className="history-question-body">"{item.question}"</span>
                        </div>
                      )}

                      {/* SQL Code Block */}
                      <div className="history-sql-code-box">
                        <pre className="history-sql-code">
                          <code>{item.sql}</code>
                        </pre>
                      </div>

                      {/* Metrics Pill Row */}
                      <div className="history-metrics-row">
                        {item.row_count !== null && item.row_count !== undefined && (
                          <span className="history-stat-pill">
                            📊 {item.row_count} row{item.row_count === 1 ? '' : 's'}
                          </span>
                        )}
                        {item.execution_time_ms !== null && item.execution_time_ms !== undefined && (
                          <span className="history-stat-pill">
                            ⚡ {item.execution_time_ms.toFixed(1)} ms
                          </span>
                        )}
                        {item.tags && item.tags.length > 0 && (
                          <div className="history-card-tags">
                            {item.tags.map((t) => (
                              <span key={t} className="history-item-tag">
                                #{t}
                              </span>
                            ))}
                          </div>
                        )}
                      </div>

                      {/* Error Message if failed */}
                      {item.error_message && (
                        <div className="history-error-banner" role="alert">
                          <span className="history-error-icon">⚠️</span>
                          <span className="history-error-text">{item.error_message}</span>
                        </div>
                      )}

                      {/* Card Action Footer */}
                      <div className="history-card-actions-footer">
                        <button
                          type="button"
                          className="history-action-btn run-btn"
                          onClick={() => handleExecute(item)}
                          title="Run query against database profile"
                        >
                          ⚡ Run Query
                        </button>
                        <button
                          type="button"
                          className={`history-action-btn copy-btn ${isCopied ? 'copied' : ''}`}
                          onClick={() => handleCopySql(item)}
                          title="Copy SQL to clipboard"
                        >
                          {isCopied ? '✓ Copied!' : '📋 Copy SQL'}
                        </button>
                        <button
                          type="button"
                          className="history-action-btn delete-btn"
                          onClick={(e) => handleDeleteItem(item.id, e)}
                          title="Delete from history"
                          aria-label="Delete entry from history"
                        >
                          🗑️
                        </button>
                      </div>
                    </article>
                  )
                })}
              </div>
            )
          )}
        </div>

        {/* Drawer Footer */}
        <div className="history-drawer-footer">
          <p className="history-privacy-notice">
            🔒 <strong>Fail-Closed Security:</strong> Sensitive passwords and credentials are automatically scrubbed before saving to disk. Starred favorites ⭐ are strictly preserved across clears.
          </p>
        </div>
      </div>
    </div>
  )
}
