import {
  ApiClientError,
  type HealthResponse,
  type QueryRequest,
  type QueryResponse,
  type ValidationErrorDetail,
} from '../types/api'

/**
 * Options for client requests.
 */
export interface RequestOptions {
  /** Optional AbortSignal for request cancellation. */
  signal?: AbortSignal
  /** Optional client-supplied request tracking ID. */
  requestId?: string
}

/**
 * Fetch application operational liveness status from the backend.
 *
 * Calls `GET /api/health` via the Vite development proxy.
 *
 * @param signal - Optional AbortSignal for request cancellation on component unmount.
 * @returns Promise resolving to the validated HealthResponse.
 * @throws ApiClientError on network or HTTP failure.
 */
export async function getHealth(signal?: AbortSignal): Promise<HealthResponse> {
  let response: Response
  try {
    response = await fetch('/api/health', {
      method: 'GET',
      headers: {
        Accept: 'application/json',
      },
      signal,
    })
  } catch (err: unknown) {
    if (err instanceof DOMException && err.name === 'AbortError') {
      throw err
    }
    throw new ApiClientError({
      message: 'Unable to connect to the QueryPilot API service.',
      kind: 'network',
    })
  }

  const requestId = response.headers.get('X-Request-ID')

  if (!response.ok) {
    throw new ApiClientError({
      message: `API health check failed (HTTP ${response.status}).`,
      kind: response.status >= 500 ? 'server' : 'network',
      statusCode: response.status,
      requestId,
    })
  }

  try {
    const data = (await response.json()) as HealthResponse
    if (typeof data?.status !== 'string' || typeof data?.version !== 'string') {
      throw new ApiClientError({
        message: 'Invalid health response format received from server.',
        kind: 'parse',
        statusCode: response.status,
        requestId,
      })
    }
    return data
  } catch (err: unknown) {
    if (err instanceof ApiClientError) {
      throw err
    }
    throw new ApiClientError({
      message: 'Failed to parse API health response.',
      kind: 'parse',
      statusCode: response.status,
      requestId,
    })
  }
}

/**
 * Execute a natural-language query through the QueryPilot pipeline.
 *
 * Calls `POST /api/query` via the Vite development proxy.
 * Note: The backend response projection excludes generated SQL by default.
 *
 * @param request - Validated QueryRequest containing question.
 * @param options - Optional request configuration (cancellation signal, custom tracking ID).
 * @returns Promise resolving to the validated and normalized QueryResponse.
 * @throws ApiClientError with kind 'validation' on 422, 'server' on 500, or 'network' on connectivity error.
 */
export async function postQuery(
  request: QueryRequest,
  options?: RequestOptions
): Promise<QueryResponse> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    Accept: 'application/json',
  }

  if (options?.requestId) {
    headers['X-Request-ID'] = options.requestId
  }

  let response: Response
  try {
    response = await fetch('/api/query', {
      method: 'POST',
      headers,
      body: JSON.stringify(request),
      signal: options?.signal,
    })
  } catch (err: unknown) {
    if (err instanceof DOMException && err.name === 'AbortError') {
      throw err
    }
    throw new ApiClientError({
      message: 'Unable to connect to the QueryPilot API service.',
      kind: 'network',
    })
  }

  const requestId = response.headers.get('X-Request-ID')

  // 1. Handle HTTP 422 Unprocessable Content (Schema validation error)
  if (response.status === 422) {
    let validationErrors: ValidationErrorDetail[] | undefined
    let message = 'Request validation failed.'

    try {
      const errorBody = (await response.json()) as { detail?: ValidationErrorDetail[] }
      if (Array.isArray(errorBody?.detail)) {
        validationErrors = errorBody.detail
        const first = errorBody.detail[0]
        if (first?.msg) {
          message = `Validation error: ${first.msg}`
        }
      }
    } catch {
      // Discard JSON parse failure on error body
    }

    throw new ApiClientError({
      message,
      kind: 'validation',
      statusCode: 422,
      requestId,
      validationErrors,
    })
  }

  // 2. Handle HTTP 500 or other non-success statuses
  if (!response.ok) {
    let message = `Server error (HTTP ${response.status}).`
    try {
      const errorBody = (await response.json()) as { detail?: string }
      if (typeof errorBody?.detail === 'string' && errorBody.detail.trim()) {
        message = errorBody.detail
      }
    } catch {
      // Discard JSON parse failure
    }

    throw new ApiClientError({
      message,
      kind: 'server',
      statusCode: response.status,
      requestId,
    })
  }

  // 3. Parse and validate successful response
  let data: QueryResponse
  try {
    data = (await response.json()) as QueryResponse
  } catch {
    throw new ApiClientError({
      message: 'Failed to parse query response as JSON.',
      kind: 'parse',
      statusCode: response.status,
      requestId,
    })
  }

  if (
    !data ||
    typeof data !== 'object' ||
    typeof data.status !== 'string' ||
    typeof data.question !== 'string'
  ) {
    throw new ApiClientError({
      message: 'Invalid query response format received from server.',
      kind: 'parse',
      statusCode: response.status,
      requestId: requestId || (data as unknown as Record<string, unknown>)?.request_id as string | undefined,
    })
  }

  // 4. Safely normalize optional and collection fields to prevent UI runtime crashes
  return {
    ...data,
    request_id: data.request_id || requestId || null,
    columns: Array.isArray(data.columns) ? data.columns : [],
    rows: Array.isArray(data.rows) ? data.rows : [],
    row_count:
      typeof data.row_count === 'number'
        ? data.row_count
        : Array.isArray(data.rows)
          ? data.rows.length
          : 0,
    truncated: Boolean(data.truncated),
    clarification_options: Array.isArray(data.clarification_options)
      ? data.clarification_options
      : [],
    validation_reasons: Array.isArray(data.validation_reasons)
      ? data.validation_reasons
      : [],
    grounded_answer:
      data.grounded_answer && typeof data.grounded_answer === 'object'
        ? {
            text:
              typeof data.grounded_answer.text === 'string'
                ? data.grounded_answer.text
                : '',
            status:
              typeof data.grounded_answer.status === 'string'
                ? data.grounded_answer.status
                : '',
            is_mechanically_verified: Boolean(
              data.grounded_answer.is_mechanically_verified
            ),
            verification_notes: Array.isArray(
              data.grounded_answer.verification_notes
            )
              ? data.grounded_answer.verification_notes
              : [],
          }
        : null,
    generated_sql:
      typeof data.generated_sql === 'string' ? data.generated_sql : null,
  }
}
