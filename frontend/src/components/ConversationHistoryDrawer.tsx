import type { ConversationHistoryTurn } from '../services/sqlGeneration'
import './ConversationHistoryDrawer.css'

export interface ConversationHistoryDrawerProps {
  isOpen: boolean
  onClose: () => void
  history: ConversationHistoryTurn[]
  onRestoreTurn: (index: number) => void
  onNewTopic: () => void
}

export function ConversationHistoryDrawer({
  isOpen,
  onClose,
  history,
  onRestoreTurn,
  onNewTopic,
}: ConversationHistoryDrawerProps) {
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
        <div className="history-drawer-header">
          <div className="history-header-title-group">
            <span className="history-header-icon" aria-hidden="true">📜</span>
            <div>
              <h2 id="history-drawer-title" className="history-drawer-title">
                Conversation History
              </h2>
              <span className="history-turns-counter">
                {history.length} of {maxTurns} bounded turns
              </span>
            </div>
          </div>

          <div className="history-header-actions">
            {history.length > 0 && (
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

        <div className="history-drawer-body">
          {history.length === 0 ? (
            <div className="history-empty-state" role="status">
              <span className="history-empty-icon" aria-hidden="true">💬</span>
              <p className="history-empty-title">No Conversation History</p>
              <p className="history-empty-desc">
                Execute a query against the selected profile database to record conversational turns. Context is preserved across up to 3 follow-up turns.
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
                          {turn.resultColumns.length} column{turn.resultColumns.length === 1 ? '' : 's'}: {turn.resultColumns.slice(0, 3).join(', ')}{turn.resultColumns.length > 3 ? '...' : ''}
                        </span>
                      )}
                    </div>
                  </article>
                )
              })}
            </div>
          )}
        </div>

        <div className="history-drawer-footer">
          <p className="history-privacy-notice">
            🔒 <strong>Safe Display Boundary:</strong> Contains natural-language questions, summaries, and column metadata only. SQL queries, database rows, and credentials are never stored in conversation history.
          </p>
        </div>
      </div>
    </div>
  )
}
