import type { ApiClientError } from '../types/api'

interface ErrorAlertProps {
  error: ApiClientError | Error
  onDismiss?: () => void
}

export function ErrorAlert({ error, onDismiss }: ErrorAlertProps) {
  const isApiClientError = 'kind' in error
  const statusCode = isApiClientError ? error.statusCode : undefined
  const requestId = isApiClientError ? error.requestId : undefined
  const validationErrors = isApiClientError ? error.validationErrors : undefined

  let title = 'An error occurred'
  if (isApiClientError) {
    switch (error.kind) {
      case 'unauthorized':
        title = 'Access Key Required (HTTP 401)'
        break
      case 'validation':
        title = statusCode === 413 ? 'Payload Too Large (HTTP 413)' : 'Validation Error (HTTP 422)'
        break
      case 'server':
        title = statusCode === 429 ? 'Rate Limit Exceeded (HTTP 429)' : (statusCode ? `Server Error (HTTP ${statusCode})` : 'Server Error')
        break
      case 'network':
        title = 'Network Connection Error'
        break
      case 'parse':
        title = 'Response Parsing Error'
        break
    }
  }

  return (
    <div className="error-alert" role="alert">
      <div className="error-alert-header">
        <span className="error-icon" aria-hidden="true">⚠️</span>
        <h3 className="error-title">{title}</h3>
        {onDismiss && (
          <button
            type="button"
            className="error-dismiss-btn"
            onClick={onDismiss}
            aria-label="Dismiss error"
          >
            &times;
          </button>
        )}
      </div>

      <p className="error-message">{error.message}</p>

      {validationErrors && validationErrors.length > 0 && (
        <ul className="validation-error-list">
          {validationErrors.map((vErr, idx) => (
            <li key={idx} className="validation-error-item">
              <span className="validation-loc">
                {vErr.loc.join(' &rarr; ')}:
              </span>{' '}
              {vErr.msg}
            </li>
          ))}
        </ul>
      )}

      {requestId && (
        <div className="error-request-id">
          <span>Request ID:</span> <code>{requestId}</code>
        </div>
      )}
    </div>
  )
}
