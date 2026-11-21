import { useState, useEffect, useMemo } from 'react'
import {
  fetchProfileDataDictionary,
  enrichProfileDataDictionary,
} from '../../services/dataDictionary.ts'
import { getStoredGeminiKey } from '../../services/geminiKeyStorage.ts'
import type { DataDictionaryResponse } from '../../types/api.ts'
import './DataDictionaryDrawer.css'

export interface DataDictionaryDrawerProps {
  isOpen: boolean
  onClose: () => void
  profileId: string
  databaseName?: string
  selectedTable?: string
  onAskQuestion?: (prompt: string) => void
}

export function DataDictionaryDrawer({
  isOpen,
  onClose,
  profileId,
  databaseName = 'Database',
  selectedTable,
  onAskQuestion,
}: DataDictionaryDrawerProps) {
  const [loading, setLoading] = useState<boolean>(true)
  const [enriching, setEnriching] = useState<boolean>(false)
  const [error, setError] = useState<string | null>(null)
  const [dictionary, setDictionary] = useState<DataDictionaryResponse | null>(null)
  const [activeTable, setActiveTable] = useState<string | null>(selectedTable || null)
  const [searchQuery, setSearchQuery] = useState<string>('')

  // Load dictionary on open
  useEffect(() => {
    if (!isOpen || !profileId) return

    let isMounted = true
    setLoading(true)
    setError(null)

    fetchProfileDataDictionary(profileId)
      .then((res) => {
        if (isMounted) {
          setDictionary(res)
          setLoading(false)
          if (!activeTable && Object.keys(res.tables).length > 0) {
            setActiveTable(selectedTable || Object.keys(res.tables)[0])
          }
        }
      })
      .catch((err: unknown) => {
        if (isMounted) {
          setError(err instanceof Error ? err.message : 'Failed to load data dictionary.')
          setLoading(false)
        }
      })

    return () => {
      isMounted = false
    }
  }, [isOpen, profileId, selectedTable, activeTable])

  // Sync selectedTable when passed explicitly
  useEffect(() => {
    if (selectedTable) {
      setActiveTable(selectedTable)
    }
  }, [selectedTable])

  const handleEnrichAI = async () => {
    if (!profileId) return
    try {
      setEnriching(true)
      setError(null)
      const storedKey = getStoredGeminiKey()
      const enriched = await enrichProfileDataDictionary(profileId, storedKey)
      setDictionary(enriched)
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'AI enrichment failed.')
    } finally {
      setEnriching(false)
    }
  }

  // Filtered tables list
  const filteredTableNames = useMemo<string[]>(() => {
    if (!dictionary) return []
    const all = Object.keys(dictionary.tables)
    if (!searchQuery.trim()) return all

    const q = searchQuery.toLowerCase().trim()
    return all.filter((tName) => {
      const entry = dictionary.tables[tName]
      if (tName.toLowerCase().includes(q)) return true
      if (entry.business_name.toLowerCase().includes(q)) return true
      if (entry.description.toLowerCase().includes(q)) return true
      return Object.keys(entry.columns).some(
        (c) =>
          c.toLowerCase().includes(q) ||
          entry.columns[c].business_name.toLowerCase().includes(q)
      )
    })
  }, [dictionary, searchQuery])

  if (!isOpen) return null

  const currentTableEntry =
    activeTable && dictionary?.tables[activeTable]
      ? dictionary.tables[activeTable]
      : null

  const isAiGenerated = dictionary?.source === 'ai_gemini_flash'

  return (
    <div
      className="dict-drawer-overlay"
      role="dialog"
      aria-modal="true"
      aria-labelledby="dict-drawer-title"
      onClick={onClose}
    >
      <div className="dict-drawer-card" onClick={(e) => e.stopPropagation()}>
        {/* Header */}
        <div className="dict-drawer-header">
          <div className="dict-header-left">
            <span className="dict-header-icon" aria-hidden="true">📖</span>
            <div>
              <h3 id="dict-drawer-title" className="dict-drawer-title">
                AI Data Dictionary &bull; {databaseName}
              </h3>
              <div className="dict-source-badge-group">
                <span
                  className={`dict-badge ${isAiGenerated ? 'badge-ai' : 'badge-heuristic'}`}
                >
                  {isAiGenerated ? '✨ AI Synthesized (Gemini Flash)' : '⚙️ Deterministic Heuristic'}
                </span>
                {dictionary && (
                  <span className="dict-timestamp">
                    {new Date(dictionary.generated_at).toLocaleTimeString([], {
                      hour: '2-digit',
                      minute: '2-digit',
                    })}
                  </span>
                )}
              </div>
            </div>
          </div>

          <div className="dict-header-actions">
            <button
              type="button"
              className="dict-enrich-btn"
              onClick={handleEnrichAI}
              disabled={loading || enriching}
              title="Generate comprehensive plain-English descriptions using Google Gemini"
            >
              {enriching ? '✨ Enriching with AI...' : '✨ Enrich with AI'}
            </button>
            <button
              type="button"
              className="dict-close-btn"
              onClick={onClose}
              aria-label="Close data dictionary"
            >
              ✕
            </button>
          </div>
        </div>

        {/* Content Body: Two-pane Master-Detail layout */}
        <div className="dict-drawer-body">
          {loading && (
            <div className="dict-loading-state" role="status">
              <span className="dict-spinner" aria-hidden="true" />
              <span>Analyzing database semantics and building dictionary...</span>
            </div>
          )}

          {enriching && (
            <div className="dict-enriching-banner" role="status">
              <span className="dict-spinner" aria-hidden="true" />
              <span>
                ✨ Synthesizing plain-English business definitions and suggested questions with Google Gemini Flash... (this takes ~5-15s)
              </span>
            </div>
          )}

          {error && (
            <div className="dict-error-alert" role="alert">
              <span aria-hidden="true">⚠️</span>
              <span>{error}</span>
            </div>
          )}

          {!loading && dictionary && (
            <div className="dict-split-pane">
              {/* Left sidebar: Tables list */}
              <div className="dict-sidebar">
                <div className="dict-search-box">
                  <span aria-hidden="true">🔍</span>
                  <input
                    type="text"
                    placeholder="Filter dictionary..."
                    value={searchQuery}
                    onChange={(e) => setSearchQuery(e.target.value)}
                    className="dict-search-input"
                  />
                  {searchQuery && (
                    <button
                      type="button"
                      className="dict-search-clear"
                      onClick={() => setSearchQuery('')}
                    >
                      ✕
                    </button>
                  )}
                </div>

                <div className="dict-tables-list" role="tablist">
                  {filteredTableNames.map((tName) => {
                    const entry = dictionary.tables[tName]
                    const isActive = activeTable === tName
                    return (
                      <button
                        key={tName}
                        type="button"
                        role="tab"
                        aria-selected={isActive}
                        className={`dict-table-item ${isActive ? 'item-active' : ''}`}
                        onClick={() => setActiveTable(tName)}
                      >
                        <div className="dict-table-item-header">
                          <strong className="dict-item-bizname">{entry.business_name}</strong>
                          <span className="dict-item-colcount">
                            {Object.keys(entry.columns).length} cols
                          </span>
                        </div>
                        <code className="dict-item-rawname">{tName}</code>
                      </button>
                    )
                  })}
                  {filteredTableNames.length === 0 && (
                    <div className="dict-empty-search">No matching tables found.</div>
                  )}
                </div>
              </div>

              {/* Right main area: Table details & columns */}
              <div className="dict-details-pane">
                {currentTableEntry ? (
                  <div className="dict-table-content">
                    {/* Table overview banner */}
                    <div className="dict-overview-card">
                      <div className="dict-overview-top">
                        <h4 className="dict-table-title">{currentTableEntry.business_name}</h4>
                        <code className="dict-table-code">{currentTableEntry.table_name}</code>
                      </div>
                      <p className="dict-table-desc">{currentTableEntry.description}</p>

                      {/* Suggested analytical questions */}
                      {currentTableEntry.suggested_questions &&
                        currentTableEntry.suggested_questions.length > 0 && (
                          <div className="dict-suggested-section">
                            <span className="dict-suggested-label">💡 Suggested Questions:</span>
                            <div className="dict-question-chips">
                              {currentTableEntry.suggested_questions.map((q, qIdx) => (
                                <button
                                  key={qIdx}
                                  type="button"
                                  className="dict-q-chip"
                                  onClick={() => {
                                    if (onAskQuestion) {
                                      onAskQuestion(q)
                                      onClose()
                                    }
                                  }}
                                  title="Click to ask this question"
                                >
                                  {q} &rarr;
                                </button>
                              ))}
                            </div>
                          </div>
                        )}
                    </div>

                    {/* Columns breakdown */}
                    <div className="dict-columns-section">
                      <h5 className="dict-cols-heading">
                        Columns Data Dictionary ({Object.keys(currentTableEntry.columns).length})
                      </h5>
                      <div className="dict-cols-grid">
                        {Object.values(currentTableEntry.columns).map((col) => (
                          <div key={col.name} className="dict-col-card">
                            <div className="dict-col-header">
                              <div className="dict-col-names">
                                <strong className="dict-col-bizname">{col.business_name}</strong>
                                <code className="dict-col-rawname">{col.name}</code>
                              </div>
                              <div className="dict-col-badges">
                                {col.is_primary_key && (
                                  <span className="dict-col-badge pk" title="Primary Key">PK 🔑</span>
                                )}
                                {col.is_foreign_key && (
                                  <span
                                    className="dict-col-badge fk"
                                    title={`References ${col.foreign_key_target || ''}`}
                                  >
                                    FK 🔗
                                  </span>
                                )}
                                <span className="dict-col-badge type">{col.data_type || 'TEXT'}</span>
                              </div>
                            </div>
                            <p className="dict-col-desc">{col.description}</p>
                            {col.foreign_key_target && (
                              <div className="dict-col-rel">
                                <span aria-hidden="true">↳ Links to:</span>{' '}
                                <code>{col.foreign_key_target}</code>
                              </div>
                            )}
                          </div>
                        ))}
                      </div>
                    </div>
                  </div>
                ) : (
                  <div className="dict-select-prompt">Select a table on the left to view its data dictionary.</div>
                )}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
