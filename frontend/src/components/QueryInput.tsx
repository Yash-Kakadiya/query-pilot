import { useState } from 'react'

export interface QueryInputSubmitOptions {
  executionMode?: 'execute' | 'sql_only'
  includeSql?: boolean
}

interface QueryInputProps {
  isLoading: boolean
  onSubmit: (question: string, options?: QueryInputSubmitOptions) => void
  onCancel?: () => void
  onClear?: () => void
  hasResult: boolean
}

const MAX_QUESTION_LENGTH = 1000

export function QueryInput({
  isLoading,
  onSubmit,
  onCancel,
  onClear,
  hasResult,
}: QueryInputProps) {
  const [question, setQuestion] = useState('')
  const [executionMode, setExecutionMode] = useState<'execute' | 'sql_only'>('execute')
  const [includeSql, setIncludeSql] = useState(false)

  const trimmed = question.trim()
  const charCount = question.length
  const isTooLong = charCount > MAX_QUESTION_LENGTH
  const isEmpty = trimmed.length === 0
  const canSubmit = !isEmpty && !isTooLong && !isLoading

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    if (canSubmit) {
      onSubmit(trimmed, { executionMode, includeSql })
    }
  }

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
      e.preventDefault()
      if (canSubmit) {
        onSubmit(trimmed, { executionMode, includeSql })
      }
    }
  }

  const handleReset = () => {
    setQuestion('')
    if (onClear) {
      onClear()
    }
  }

  return (
    <form className="query-form" onSubmit={handleSubmit} noValidate>
      <div className="input-group">
        <div className="input-label-row">
          <label htmlFor="query-question" className="input-label">
            Natural-Language Question
          </label>
          <span
            className={`char-counter ${isTooLong ? 'char-counter-error' : ''}`}
            aria-live="polite"
          >
            {charCount} / {MAX_QUESTION_LENGTH}
          </span>
        </div>

        <textarea
          id="query-question"
          className={`query-textarea ${isTooLong ? 'textarea-error' : ''}`}
          rows={3}
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="e.g., What is the average GPA of students in the Computer Science department?"
          disabled={isLoading}
          maxLength={MAX_QUESTION_LENGTH + 50} // slight buffer to show error state if exceeded
          aria-describedby="question-hint"
        />

        <div id="question-hint" className="input-hint">
          {isTooLong && (
            <span className="hint-error">
              Question exceeds maximum allowed limit of {MAX_QUESTION_LENGTH} characters.
            </span>
          )}
          {!isTooLong && (
            <span>Press <kbd>Ctrl</kbd> + <kbd>Enter</kbd> to submit</span>
          )}
        </div>
      </div>

      <div className="query-options-bar">
        <fieldset className="execution-mode-group">
          <legend className="options-legend">Execution Mode:</legend>
          <label className="radio-label">
            <input
              type="radio"
              name="execution_mode"
              value="execute"
              checked={executionMode === 'execute'}
              onChange={() => setExecutionMode('execute')}
              disabled={isLoading}
            />
            <span>Execute Query</span>
          </label>
          <label className="radio-label">
            <input
              type="radio"
              name="execution_mode"
              value="sql_only"
              checked={executionMode === 'sql_only'}
              onChange={() => setExecutionMode('sql_only')}
              disabled={isLoading}
            />
            <span>SQL Only (Validate, Skip Execution)</span>
          </label>
        </fieldset>

        <label className="checkbox-label">
          <input
            type="checkbox"
            name="include_sql"
            checked={includeSql}
            onChange={(e) => setIncludeSql(e.target.checked)}
            disabled={isLoading}
          />
          <span>Include SQL in response</span>
        </label>
      </div>

      {executionMode === 'sql_only' && (
        <div className="mode-explainer" role="note">
          ℹ️ <strong>SQL-Only Mode:</strong> Validates generated SQL against QueryPilot&apos;s checked-in GradeSense demo schema without executing against or inspecting a live database. (Arbitrary custom schemas for external databases are not yet supported.)
        </div>
      )}

      <div className="form-actions">
        {hasResult && !isLoading && (
          <button
            type="button"
            className="secondary-btn"
            onClick={handleReset}
          >
            Ask Another Question
          </button>
        )}

        {isLoading && onCancel && (
          <button
            type="button"
            className="cancel-btn"
            onClick={onCancel}
          >
            Cancel Request
          </button>
        )}

        <button
          type="submit"
          className="submit-btn"
          disabled={!canSubmit}
        >
          {isLoading ? (
            <span className="submit-loading">
              <span className="spinner" aria-hidden="true" />
              {executionMode === 'sql_only' ? 'Validating SQL...' : 'Running Query...'}
            </span>
          ) : (
            executionMode === 'sql_only' ? 'Validate SQL Only' : 'Run Query'
          )}
        </button>
      </div>
    </form>
  )
}
