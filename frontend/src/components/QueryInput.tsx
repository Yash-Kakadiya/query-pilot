import { useState } from 'react'

interface QueryInputProps {
  isLoading: boolean
  onSubmit: (question: string) => void
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

  const trimmed = question.trim()
  const charCount = question.length
  const isTooLong = charCount > MAX_QUESTION_LENGTH
  const isEmpty = trimmed.length === 0
  const canSubmit = !isEmpty && !isTooLong && !isLoading

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    if (canSubmit) {
      onSubmit(trimmed)
    }
  }

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
      e.preventDefault()
      if (canSubmit) {
        onSubmit(trimmed)
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
              Running Query...
            </span>
          ) : (
            'Run Query'
          )}
        </button>
      </div>
    </form>
  )
}
