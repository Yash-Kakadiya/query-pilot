import type { ExecutionMode } from '../types/api.ts'

/**
 * Resolves the default query execution mode configured for the frontend.
 *
 * Reads from build-time VITE_DEFAULT_EXECUTION_MODE environment variable:
 * - 'sql_only': returns 'sql_only'
 * - 'execute' (or unset/empty/whitespace): returns 'execute'
 * - Any unrecognized or invalid value: safely falls back to 'execute'
 *
 * Value is normalized case-insensitively and trimmed.
 *
 * @param customEnvMode - Optional override for testing or dynamic configuration.
 */
export function getDefaultExecutionMode(customEnvMode?: string): ExecutionMode {
  const envMode =
    customEnvMode !== undefined
      ? customEnvMode
      : (import.meta as { env?: Record<string, string | undefined> })?.env?.VITE_DEFAULT_EXECUTION_MODE

  if (envMode === undefined || envMode === null) {
    return 'execute'
  }

  const normalized = envMode.trim().toLowerCase()
  if (normalized === 'sql_only') {
    return 'sql_only'
  }

  // Covers 'execute', empty strings, whitespace, and any invalid/unknown value
  return 'execute'
}

/**
 * Resolves the initial execution mode for the query input component on a fresh load.
 *
 * Honors explicit prop if provided; otherwise falls back to build-time environment configuration.
 *
 * @param explicitProp - Optional prop passed to the component.
 * @param customEnvMode - Optional environment mode override for testing.
 */
export function resolveInitialExecutionMode(
  explicitProp?: ExecutionMode,
  customEnvMode?: string
): ExecutionMode {
  return explicitProp ?? getDefaultExecutionMode(customEnvMode)
}
