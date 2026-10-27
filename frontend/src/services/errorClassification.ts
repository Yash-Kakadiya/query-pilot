/**
 * QueryPilot Error Classification Utility
 *
 * Classifies query execution and communication errors into typed categories:
 * - Identifies potentially recoverable query errors (e.g. invalid column name,
 *   invalid table name, GROUP BY mismatch, syntax error, type conversion).
 * - Identifies non-recoverable terminal failures (e.g. policy rejections,
 *   timeouts, 401 authentication, 403 authorization, connection drops, cancellations).
 * - Generates safe, generic, allowlisted diagnostic hints without ever leaking
 *   raw driver messages, connection strings, server names, credentials, tokens,
 *   raw SQL text, or stack traces.
 *
 * Pure classification: Strictly zero network dispatch, side effects, or mutations.
 */

import { AgentClientError } from '../types/api.ts'

export type ErrorClassificationCategory =
  | 'recoverable_query_error'
  | 'policy_rejection'
  | 'timeout'
  | 'authentication'
  | 'authorization'
  | 'connection'
  | 'cancellation'
  | 'unknown'

export interface ErrorClassificationResult {
  category: ErrorClassificationCategory
  recoverable: boolean
  diagnosticFeedback: string | null
  userMessage: string
}

// Safe SQL identifier pattern (standard T-SQL alphanumeric and underscore identifiers)
const SAFE_IDENTIFIER_REGEX = /^[A-Za-z0-9_#$@.]{1,128}$/

/**
 * Validates whether an extracted database identifier is safe to include in diagnostic feedback.
 * Rejects strings containing spaces, quotes, brackets, semicolons, comments, or suspicious payload chars.
 */
function isSafeIdentifier(ident: string): boolean {
  return SAFE_IDENTIFIER_REGEX.test(ident.trim())
}

/**
 * Classifies an error resulting from local query execution, API communication, or validation.
 *
 * @param error - The raw caught error (AgentClientError, DOMException, Error, or unknown).
 * @returns Structured, typed ErrorClassificationResult.
 */
export function classifyExecutionError(error: unknown): ErrorClassificationResult {
  // 1. Cancellation / Aborted requests (e.g. user navigation, explicit abort)
  if (
    (error instanceof DOMException && error.name === 'AbortError') ||
    (error instanceof Error && error.name === 'AbortError') ||
    (typeof error === 'object' && error !== null && 'name' in error && (error as { name: unknown }).name === 'AbortError')
  ) {
    return {
      category: 'cancellation',
      recoverable: false,
      diagnosticFeedback: null,
      userMessage: 'Query execution was cancelled.',
    }
  }

  // 2. Local Agent Client Errors
  if (error instanceof AgentClientError) {
    const status = error.statusCode
    const kind = error.kind
    const rawMessage = (error.message || '').trim()

    // 2a. Timeouts (HTTP 504 or timeout kind)
    if (kind === 'timeout' || status === 504) {
      return {
        category: 'timeout',
        recoverable: false,
        diagnosticFeedback: null,
        userMessage: 'Query execution timed out.',
      }
    }

    // 2b. Authentication failure (HTTP 401 or authentication kind)
    if (kind === 'authentication' || status === 401) {
      return {
        category: 'authentication',
        recoverable: false,
        diagnosticFeedback: null,
        userMessage: 'Authentication failed. Please verify your local agent session.',
      }
    }

    // 2c. Authorization failure (HTTP 403 or forbidden kind)
    if (kind === 'forbidden' || status === 403) {
      return {
        category: 'authorization',
        recoverable: false,
        diagnosticFeedback: null,
        userMessage: 'Access forbidden. Query or target is not permitted by profile policy.',
      }
    }

    // 2d. Connection failure (daemon unreachable, loopback failure, disconnected kind)
    if (kind === 'disconnected') {
      return {
        category: 'connection',
        recoverable: false,
        diagnosticFeedback: null,
        userMessage: 'Unable to connect to QueryPilot Local Agent daemon.',
      }
    }

    // 2e. Validation / Safety Policy rejections (HTTP 400 or validation kind)
    if (status === 400 || kind === 'validation') {
      // Check for AST safety policy rejection codes
      const isPolicyRejection =
        /NOT_READ_ONLY|FORBIDDEN_OPERATION|SENSITIVE_COLUMN|FORBIDDEN_TABLE|MULTI_STATEMENT|EXCEEDS_MAX_ROWS|UNSUPPORTED_QUERY_SHAPE|EMPTY_SQL/i.test(
          rawMessage
        ) || /forbidden by profile policy/i.test(rawMessage)

      if (isPolicyRejection) {
        return {
          category: 'policy_rejection',
          recoverable: false,
          diagnosticFeedback: null,
          userMessage: 'Query rejected by security policy or read-only validation.',
        }
      }

      // Check for AST syntax parse error (recoverable query shape error)
      if (/PARSE_ERROR/i.test(rawMessage)) {
        return {
          category: 'recoverable_query_error',
          recoverable: true,
          diagnosticFeedback:
            'T-SQL syntax error: The proposed query could not be parsed as valid SQL. Please generate valid T-SQL.',
          userMessage: 'Query syntax error detected during validation.',
        }
      }

      // Other 400 errors default safely to unknown/non-repairable
      return {
        category: 'unknown',
        recoverable: false,
        diagnosticFeedback: null,
        userMessage: 'Invalid query execution request.',
      }
    }

    // 2f. Database execution errors (HTTP 500 or server kind)
    if (status === 500 || kind === 'server') {
      return classifyDatabaseErrorMessage(rawMessage)
    }
  }

  // 3. Generic Error objects (e.g. network fetch errors or unhandled exceptions)
  if (error instanceof Error) {
    const msg = error.message || ''

    // Network / fetch failure
    if (/failed to fetch|networkerror|econnrefused|econnreset/i.test(msg)) {
      return {
        category: 'connection',
        recoverable: false,
        diagnosticFeedback: null,
        userMessage: 'Network connection failure communicating with agent.',
      }
    }

    // Attempt allowlist match on error message if it resembles a database error
    return classifyDatabaseErrorMessage(msg)
  }

  // 4. Fallback for untyped or unclassified errors
  return {
    category: 'unknown',
    recoverable: false,
    diagnosticFeedback: null,
    userMessage: 'An unexpected or unclassified execution error occurred.',
  }
}

/**
 * Checks a database error message against a strict allowlist of recoverable SQL errors.
 * Never exposes raw error text, connection details, credentials, or arbitrary substrings.
 */
function classifyDatabaseErrorMessage(rawMessage: string): ErrorClassificationResult {
  const msg = rawMessage.trim()
  const hasSuspiciousContent = /;|--|\/\*|\*\/|\b(drop|alter|create|insert|update|delete|exec|grant|revoke)\b/i.test(msg)

  // 1. Invalid column name (e.g. MSSQL Error 207)
  const colMatch = msg.match(/invalid column name\s+'([^']+)'/i)
  if (colMatch) {
    const rawCol = colMatch[1]?.trim() || ''
    const safeCol = !hasSuspiciousContent && isSafeIdentifier(rawCol) ? rawCol : null
    const hint = safeCol
      ? `Invalid column name: '${safeCol}'. Please query only valid columns defined in the schema.`
      : 'Invalid column name referenced. Please query only valid columns defined in the schema.'
    return {
      category: 'recoverable_query_error',
      recoverable: true,
      diagnosticFeedback: hint,
      userMessage: safeCol ? `Invalid column name: "${safeCol}".` : 'Invalid column name referenced in query.',
    }
  }
  if (/invalid column name/i.test(msg)) {
    return {
      category: 'recoverable_query_error',
      recoverable: true,
      diagnosticFeedback: 'Invalid column name referenced. Please query only valid columns defined in the schema.',
      userMessage: 'Invalid column name referenced in query.',
    }
  }

  // 2. Invalid object / table name (e.g. MSSQL Error 208)
  const tableMatch = msg.match(/invalid object name\s+'([^']+)'/i)
  if (tableMatch) {
    const rawTable = tableMatch[1]?.trim() || ''
    const safeTable = !hasSuspiciousContent && isSafeIdentifier(rawTable) ? rawTable : null
    const hint = safeTable
      ? `Invalid table or object name: '${safeTable}'. Please query only tables defined in the schema.`
      : 'Invalid table or object name. Please query only tables defined in the schema.'
    return {
      category: 'recoverable_query_error',
      recoverable: true,
      diagnosticFeedback: hint,
      userMessage: safeTable ? `Invalid table name: "${safeTable}".` : 'Invalid table name referenced in query.',
    }
  }
  if (/invalid object name/i.test(msg)) {
    return {
      category: 'recoverable_query_error',
      recoverable: true,
      diagnosticFeedback: 'Invalid table or object name. Please query only tables defined in the schema.',
      userMessage: 'Invalid table name referenced in query.',
    }
  }

  // 3. GROUP BY / Aggregate mismatch (e.g. MSSQL Error 8120)
  if (
    /not contained in either an aggregate function or the GROUP BY/i.test(msg) ||
    /invalid in the select list because it is not contained/i.test(msg)
  ) {
    return {
      category: 'recoverable_query_error',
      recoverable: true,
      diagnosticFeedback: 'Selected column must be contained in an aggregate function or the GROUP BY clause.',
      userMessage: 'Selected column must be contained in an aggregate function or the GROUP BY clause.',
    }
  }

  // 4. Ambiguous column name (e.g. MSSQL Error 209)
  const ambigMatch = msg.match(/ambiguous column name\s+'([^']+)'/i)
  if (ambigMatch) {
    const rawCol = ambigMatch[1]?.trim() || ''
    const safeCol = !hasSuspiciousContent && isSafeIdentifier(rawCol) ? rawCol : null
    const hint = safeCol
      ? `Ambiguous column name: '${safeCol}'. Please qualify column names with table aliases.`
      : 'Ambiguous column name. Please qualify column names with table aliases.'
    return {
      category: 'recoverable_query_error',
      recoverable: true,
      diagnosticFeedback: hint,
      userMessage: safeCol ? `Ambiguous column name: "${safeCol}".` : 'Ambiguous column name referenced in query.',
    }
  }
  if (/ambiguous column name/i.test(msg)) {
    return {
      category: 'recoverable_query_error',
      recoverable: true,
      diagnosticFeedback: 'Ambiguous column name. Please qualify column names with table aliases.',
      userMessage: 'Ambiguous column name referenced in query.',
    }
  }

  // 5. T-SQL Syntax error (e.g. MSSQL Error 102 / 156)
  if (/incorrect syntax near/i.test(msg) || /syntax error/i.test(msg)) {
    return {
      category: 'recoverable_query_error',
      recoverable: true,
      diagnosticFeedback: 'T-SQL syntax error. Please ensure valid Microsoft SQL Server syntax.',
      userMessage: 'Database reported a syntax error.',
    }
  }

  // 6. Data type conversion failure (e.g. MSSQL Error 245)
  if (/conversion failed when converting/i.test(msg)) {
    return {
      category: 'recoverable_query_error',
      recoverable: true,
      diagnosticFeedback: 'Type conversion failed. Please verify column data types in expressions, joins, or filters.',
      userMessage: 'Data type conversion failure during query execution.',
    }
  }

  // 7. Non-recoverable or unclassified 500 error (connection drop, server crash, unknown driver error)
  return {
    category: 'unknown',
    recoverable: false,
    diagnosticFeedback: null,
    userMessage: 'An unexpected database execution error occurred.',
  }
}
