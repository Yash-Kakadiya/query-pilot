/**
 * QueryPilot Local Agent Daemon Client.
 *
 * Implements typed HTTP communication and robust error classification for
 * communicating with the authenticated loopback QueryPilot Local Agent daemon.
 *
 * Security & Protocol Guarantees:
 * - Credentials: uses `credentials: 'include'` for all requests so the browser
 *   automatically sends and receives the HttpOnly SameSite=Strict ambient session cookie.
 * - CSRF Defense: attaches mandatory `X-QueryPilot-Request: 1` header to every state-changing POST request.
 * - Credential Protection: never stores bootstrap tokens or session credentials in localStorage or sessionStorage.
 * - Path Encoding: safely encodes all dynamic profile identifiers via encodeURIComponent().
 * - Error Classification: classifies errors into typed kinds (authentication, forbidden, not_found,
 *   timeout, disconnected, validation, rate_limit, payload_limit, server, parse) so future UI can cleanly
 *   react to session expiration, daemon disconnects, timeouts, and authorization barriers.
 * - Zero Secrets Leakage: prevents leakage of raw database connection strings, passwords, or internal driver traces.
 * - Base URL: defaults to `/agent` for seamless local development via Vite reverse proxy and split-origin configurations.
 */

import {
  AgentClientError,
  type AgentDatabaseSchema,
  type AgentErrorKind,
  type BootstrapResponse,
  type ExecuteQueryResponse,
  type LogoutResponse,
  type ModelFacingSchemaContext,
  type ProfileListResponse,
  type RotateTokenResponse,
} from '../types/api.ts'
import { projectToModelFacingSchema } from './schemaProjection.ts'

export { AgentClientError, type AgentErrorKind }

export const AGENT_CUSTOM_HEADER = 'X-QueryPilot-Request'
export const AGENT_CUSTOM_HEADER_VALUE = '1'
export const DEFAULT_AGENT_BASE_PATH = '/agent'
export const MIN_BOOTSTRAP_TOKEN_LENGTH = 16
export const MAX_BOOTSTRAP_TOKEN_LENGTH = 128
export const MAX_SQL_QUERY_LENGTH = 10000

export interface AgentRequestOptions {
  /** Optional AbortSignal for request cancellation. */
  signal?: AbortSignal
  /** Optional client-supplied correlation or request ID. */
  requestId?: string | null
}

export interface GetProfileSchemaOptions extends AgentRequestOptions {
  /** If true, transforms the raw agent schema into bounded ModelFacingSchemaContext. */
  project?: boolean
}

/**
 * Resolves the configured Agent base URL.
 *
 * Defaults to '/agent' for development via the Vite reverse proxy and packaged deployments.
 * If explicitly configured via VITE_AGENT_BASE_URL (e.g. 'http://127.0.0.1:8765'),
 * normalizes trailing slashes.
 *
 * @throws AgentClientError if VITE_AGENT_BASE_URL is explicitly configured with an invalid URL.
 */
export function getAgentBaseUrl(customEnvUrl?: string): string {
  const envUrl =
    customEnvUrl !== undefined
      ? customEnvUrl
      : ((globalThis as unknown as { __TEST_VITE_AGENT_BASE_URL__?: string }).__TEST_VITE_AGENT_BASE_URL__ !== undefined
          ? (globalThis as unknown as { __TEST_VITE_AGENT_BASE_URL__?: string }).__TEST_VITE_AGENT_BASE_URL__
          : (import.meta as { env?: Record<string, string | undefined> })?.env?.VITE_AGENT_BASE_URL)

  if (envUrl === undefined || envUrl === null) {
    return DEFAULT_AGENT_BASE_PATH
  }

  const trimmed = envUrl.trim()
  if (trimmed === '') {
    return DEFAULT_AGENT_BASE_PATH
  }

  // Handle relative root or proxy path
  if (trimmed.startsWith('/')) {
    const cleanRelative = trimmed.replace(/\/+$/, '')
    return cleanRelative === '' ? '' : cleanRelative
  }

  // Handle absolute HTTP/HTTPS URL
  if (trimmed.startsWith('http://') || trimmed.startsWith('https://')) {
    try {
      const parsed = new URL(trimmed)
      if (!parsed.hostname) {
        throw new Error('Missing hostname')
      }
      return trimmed.replace(/\/+$/, '')
    } catch {
      throw new AgentClientError({
        message: `Invalid VITE_AGENT_BASE_URL configured: "${envUrl}". Must be a valid absolute HTTP or HTTPS URL.`,
        kind: 'validation',
      })
    }
  }

  throw new AgentClientError({
    message: `Invalid VITE_AGENT_BASE_URL configured: "${envUrl}". Expected an absolute HTTP/HTTPS URL or a relative path starting with "/".`,
    kind: 'validation',
  })
}

/**
 * Resolves the full URL for a given local-agent endpoint path.
 *
 * Handles both relative proxy prefixes (e.g. '/agent') and full base URLs.
 * Avoids accidental duplication if both base and path contain '/agent'.
 *
 * @param path - Target endpoint path (e.g. '/v1/profiles' or '/agent/v1/profiles').
 * @param customEnvUrl - Optional override for testing or dynamic configuration.
 */
export function resolveAgentEndpoint(path: string, customEnvUrl?: string): string {
  const base = getAgentBaseUrl(customEnvUrl)
  let cleanPath = path.startsWith('/') ? path : `/${path}`

  // If base already ends with /agent and path starts with /agent/, remove the prefix to avoid /agent/agent
  if (base.endsWith('/agent') && cleanPath.startsWith('/agent/')) {
    cleanPath = cleanPath.slice('/agent'.length)
  }

  return `${base}${cleanPath}`
}

/**
 * Sanitizes potentially sensitive details (passwords, tokens, connection strings) from error messages.
 */
function sanitizeErrorMessage(rawMessage: string): string {
  if (!rawMessage) {
    return ''
  }
  return rawMessage
    .replace(/(?:password|pwd|secret|token)\s*=\s*[^;\s&]+/gi, '$1=[REDACTED]')
    .replace(/(?:Bearer\s+)[A-Za-z0-9._~+/-]+=*/gi, 'Bearer [REDACTED]')
    .replace(/\r?\n+/g, ' ')
    .trim()
}

/**
 * Maps HTTP status codes to standardized AgentErrorKind.
 */
function mapHttpStatusToErrorKind(status: number): AgentErrorKind {
  switch (status) {
    case 401:
      return 'authentication'
    case 403:
      return 'forbidden'
    case 404:
      return 'not_found'
    case 504:
      return 'timeout'
    case 429:
      return 'rate_limit'
    case 413:
      return 'payload_limit'
    case 400:
    case 422:
      return 'validation'
    case 500:
    case 502:
    case 503:
    default:
      return 'server'
  }
}

/**
 * Internal helper executing fetch with standardized headers, credentials, and error classification.
 */
async function sendAgentRequest<T>(
  url: string,
  init: RequestInit,
  options?: AgentRequestOptions
): Promise<T> {
  const method = (init.method || 'GET').toUpperCase()
  const headers: Record<string, string> = {
    Accept: 'application/json',
    ...(init.headers as Record<string, string> | undefined),
  }

  // Require state-changing CSRF header for mutating operations
  if (['POST', 'PUT', 'DELETE', 'PATCH'].includes(method)) {
    headers[AGENT_CUSTOM_HEADER] = AGENT_CUSTOM_HEADER_VALUE
  }

  // Attach optional request tracking ID
  const effectiveRequestId = options?.requestId?.trim()
  if (effectiveRequestId) {
    headers['X-Request-ID'] = effectiveRequestId
  }

  let response: Response
  try {
    response = await fetch(url, {
      ...init,
      method,
      headers,
      credentials: 'include', // Always include ambient session cookies
      signal: options?.signal,
    })
  } catch (err: unknown) {
    if (err instanceof DOMException && err.name === 'AbortError') {
      throw err
    }
    // Network errors (e.g. ECONNREFUSED, fetch failed, DNS failure) indicate the local daemon is unreachable
    throw new AgentClientError({
      message: 'Unable to connect to QueryPilot Local Agent daemon. Please ensure the local agent is running on loopback.',
      kind: 'disconnected',
      requestId: effectiveRequestId || null,
    })
  }

  const responseRequestId = response.headers.get('x-request-id') || effectiveRequestId || null

  // Read raw text first to handle both JSON and non-JSON error pages safely
  let responseText = ''
  try {
    responseText = await response.text()
  } catch {
    // If reading body fails, proceed with empty string
    responseText = ''
  }

  let parsedJson: Record<string, unknown> | null = null
  if (responseText.trim()) {
    try {
      parsedJson = JSON.parse(responseText) as Record<string, unknown>
    } catch {
      parsedJson = null
    }
  }

  if (!response.ok) {
    const kind = mapHttpStatusToErrorKind(response.status)
    let errorMessage = ''
    let errorCode: string | null = null
    let detailStr: string | null = null

    if (parsedJson && typeof parsedJson === 'object') {
      if (typeof parsedJson.error === 'string' && parsedJson.error.trim()) {
        errorCode = parsedJson.error.trim()
      }
      if (typeof parsedJson.detail === 'string' && parsedJson.detail.trim()) {
        detailStr = sanitizeErrorMessage(parsedJson.detail.trim())
      } else if (Array.isArray(parsedJson.detail)) {
        // Pydantic validation error details array
        detailStr = sanitizeErrorMessage(
          parsedJson.detail
            .map((d: unknown) => {
              if (d && typeof d === 'object' && 'msg' in d) {
                return String((d as { msg: unknown }).msg)
              }
              return 'Invalid parameter'
            })
            .join('; ')
        )
      }
    }

    if (detailStr) {
      errorMessage = detailStr
    } else {
      // Provide clean, informative fallback messages based on status kind
      switch (kind) {
        case 'authentication':
          errorMessage = 'Authentication required. Local agent session is missing or expired.'
          break
        case 'forbidden':
          errorMessage = 'Access denied by local agent.'
          break
        case 'not_found':
          errorMessage = 'The requested profile or agent resource was not found.'
          break
        case 'timeout':
          errorMessage = 'Query execution timed out on the local database agent.'
          break
        case 'rate_limit':
          errorMessage = 'Agent concurrency limit exceeded. Please wait and retry.'
          break
        case 'payload_limit':
          errorMessage = 'Request payload exceeded maximum size (64 KB).'
          break
        case 'validation':
          errorMessage = 'Invalid request to local database agent.'
          break
        default:
          errorMessage = `Local database agent error (HTTP ${response.status}).`
      }
    }

    throw new AgentClientError({
      message: errorMessage,
      kind,
      statusCode: response.status,
      requestId: responseRequestId,
      errorCode,
      detail: detailStr,
    })
  }

  if (parsedJson === null) {
    throw new AgentClientError({
      message: `Invalid JSON response received from local agent (HTTP ${response.status}).`,
      kind: 'parse',
      statusCode: response.status,
      requestId: responseRequestId,
    })
  }

  return parsedJson as T
}

/**
 * Exchanges a single-use bootstrap token for an ambient HttpOnly loopback session cookie.
 *
 * Security:
 * - The bootstrap token is never stored in browser localStorage or sessionStorage.
 * - Callers should discard their local token reference immediately upon completion.
 * - Ambient session cookie (qpt_session) is managed automatically by the browser with credentials: 'include'.
 *
 * @param bootstrapToken - Single-use 16-128 character token read from daemon terminal or ~/.query_pilot/agent.token.
 * @param options - Optional cancellation signal or correlation ID.
 * @returns Promise resolving to BootstrapResponse.
 * @throws AgentClientError on invalid/expired token (401), network failure, or malformed input.
 */
export async function bootstrapAgent(
  bootstrapToken: string,
  options?: AgentRequestOptions
): Promise<BootstrapResponse> {
  if (typeof bootstrapToken !== 'string' || !bootstrapToken.trim()) {
    throw new AgentClientError({
      message: 'Bootstrap token must be a non-empty string.',
      kind: 'validation',
    })
  }

  const trimmedToken = bootstrapToken.trim()
  if (trimmedToken.length < MIN_BOOTSTRAP_TOKEN_LENGTH || trimmedToken.length > MAX_BOOTSTRAP_TOKEN_LENGTH) {
    throw new AgentClientError({
      message: `Bootstrap token must be between ${MIN_BOOTSTRAP_TOKEN_LENGTH} and ${MAX_BOOTSTRAP_TOKEN_LENGTH} characters.`,
      kind: 'validation',
    })
  }

  const url = resolveAgentEndpoint('/v1/auth/bootstrap')
  return sendAgentRequest<BootstrapResponse>(
    url,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ bootstrap_token: trimmedToken }),
    },
    options
  )
}

/**
 * Explicitly terminates the active loopback agent session and instructs the browser to clear the session cookie.
 *
 * @param options - Optional cancellation signal or correlation ID.
 * @returns Promise resolving to LogoutResponse.
 * @throws AgentClientError on network failure or unauthenticated session.
 */
export async function logoutAgent(options?: AgentRequestOptions): Promise<LogoutResponse> {
  const url = resolveAgentEndpoint('/v1/auth/logout')
  return sendAgentRequest<LogoutResponse>(
    url,
    {
      method: 'POST',
    },
    options
  )
}

/**
 * Invalidates all active daemon sessions and generates a fresh bootstrap token on the local machine.
 *
 * Security Notice:
 * Calling this function invalidates ALL existing browser sessions daemon-wide.
 * A new bootstrap token will be written to the daemon terminal and ~/.query_pilot/agent.token,
 * requiring manual operator retrieval to establish a subsequent session.
 *
 * @param options - Optional cancellation signal or correlation ID.
 * @returns Promise resolving to RotateTokenResponse.
 * @throws AgentClientError on network or daemon failure.
 */
export async function rotateAgentToken(options?: AgentRequestOptions): Promise<RotateTokenResponse> {
  const url = resolveAgentEndpoint('/v1/auth/rotate-token')
  return sendAgentRequest<RotateTokenResponse>(
    url,
    {
      method: 'POST',
    },
    options
  )
}

/**
 * Retrieves the list of approved database connection profiles registered in the local agent.
 *
 * Profiles return safe summaries (display name, schema, timeouts, row limits) with zero credentials,
 * passwords, or connection strings.
 *
 * @param options - Optional cancellation signal or correlation ID.
 * @returns Promise resolving to ProfileListResponse.
 * @throws AgentClientError on 401 (unauthenticated), network failure, or daemon error.
 */
export async function getProfiles(options?: AgentRequestOptions): Promise<ProfileListResponse> {
  const url = resolveAgentEndpoint('/v1/profiles')
  return sendAgentRequest<ProfileListResponse>(
    url,
    {
      method: 'GET',
    },
    options
  )
}

/**
 * Introspects dynamic database schema metadata for an approved profile.
 *
 * Returns raw introspected schema by default. When options.project is true,
 * transforms the output into the strict ModelFacingSchemaContext required by hosted SQL generation.
 *
 * @param profileId - Profile identifier registered in local agent.
 * @param options - Optional GetProfileSchemaOptions.
 */
export async function getProfileSchema(
  profileId: string,
  options?: GetProfileSchemaOptions & { project?: false }
): Promise<AgentDatabaseSchema>
export async function getProfileSchema(
  profileId: string,
  options: GetProfileSchemaOptions & { project: true }
): Promise<ModelFacingSchemaContext>
export async function getProfileSchema(
  profileId: string,
  options?: GetProfileSchemaOptions
): Promise<AgentDatabaseSchema | ModelFacingSchemaContext> {
  if (typeof profileId !== 'string' || !profileId.trim()) {
    throw new AgentClientError({
      message: 'Profile ID must be a non-empty string.',
      kind: 'validation',
    })
  }

  const encodedId = encodeURIComponent(profileId.trim())
  const url = resolveAgentEndpoint(`/v1/profiles/${encodedId}/schema`)

  const rawSchema = await sendAgentRequest<AgentDatabaseSchema>(
    url,
    {
      method: 'GET',
    },
    options
  )

  if (options?.project === true) {
    return projectToModelFacingSchema(rawSchema)
  }

  return rawSchema
}

/**
 * Convenience helper fetching profile schema and projecting it directly into ModelFacingSchemaContext.
 *
 * @param profileId - Profile identifier registered in local agent.
 * @param options - Optional request options.
 */
export async function getProjectedProfileSchema(
  profileId: string,
  options?: AgentRequestOptions
): Promise<ModelFacingSchemaContext> {
  return getProfileSchema(profileId, { ...options, project: true })
}

/**
 * Submits a validated read-only SQL query to the local agent for execution against an approved connection profile.
 *
 * Enforces:
 * - Exact wire request payload shape `{ sql, request_id }` matching ExecuteQueryRequest.
 * - Safely encodes profileId in URL path.
 * - Preserves execution result rows, column order, execution time, and truncation flags.
 * - Does not expose database credentials or internal driver details on error.
 *
 * @param profileId - Target approved profile ID.
 * @param sql - Candidate read-only SQL statement string (1-10000 characters).
 * @param requestId - Optional correlation tracking ID.
 * @param options - Optional request options (e.g. AbortSignal).
 * @returns Promise resolving to ExecuteQueryResponse.
 * @throws AgentClientError on execution timeout (504), safety rejection (400), unknown profile (404), or auth failure (401).
 */
export async function executeProfileQuery(
  profileId: string,
  sql: string,
  requestId?: string | null,
  options?: AgentRequestOptions
): Promise<ExecuteQueryResponse> {
  if (typeof profileId !== 'string' || !profileId.trim()) {
    throw new AgentClientError({
      message: 'Profile ID must be a non-empty string.',
      kind: 'validation',
    })
  }

  if (typeof sql !== 'string' || !sql.trim()) {
    throw new AgentClientError({
      message: 'SQL candidate query cannot be empty or whitespace only.',
      kind: 'validation',
    })
  }

  const trimmedSql = sql.trim()
  if (trimmedSql.length > MAX_SQL_QUERY_LENGTH) {
    throw new AgentClientError({
      message: `SQL query exceeds maximum allowed length of ${MAX_SQL_QUERY_LENGTH} characters.`,
      kind: 'validation',
    })
  }

  const encodedId = encodeURIComponent(profileId.trim())
  const url = resolveAgentEndpoint(`/v1/profiles/${encodedId}/execute`)

  const effectiveRequestId = (requestId !== undefined && requestId !== null && requestId.trim())
    ? requestId.trim()
    : (options?.requestId?.trim() || null)

  const requestBody: { sql: string; request_id?: string } = {
    sql: trimmedSql,
    ...(effectiveRequestId ? { request_id: effectiveRequestId } : {}),
  }

  return sendAgentRequest<ExecuteQueryResponse>(
    url,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(requestBody),
    },
    {
      ...options,
      requestId: effectiveRequestId,
    }
  )
}
