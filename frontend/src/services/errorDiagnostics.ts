/**
 * Client-Side Self-Healing Error Diagnostics Service for QueryPilot.
 *
 * Classifies raw network rejections, database errors, Gemini API errors, and safety
 * rejections into clear, plain-English solutions with 1-click action buttons.
 */

export type DiagnosticCategory =
  | 'api_key'
  | 'model_quota'
  | 'model_unavailable'
  | 'db_connection_refused'
  | 'db_host_unreachable'
  | 'db_auth_failed'
  | 'db_ssl_required'
  | 'db_safety_mutation'
  | 'db_schema_not_found'
  | 'db_timeout'
  | 'network_disconnect'
  | 'unknown'

export interface DiagnosticAction {
  label: string
  action_type: 'open_external_url' | 'focus_api_key_input' | 'open_profile_modal' | 'switch_model' | 'retry'
  payload?: string | null
}

export interface DiagnosticSolution {
  category: DiagnosticCategory
  title: string
  summary: string
  root_cause: string
  actionable_steps: string[]
  primary_action?: DiagnosticAction | null
  secondary_action?: DiagnosticAction | null
  technical_details?: string | null
}

/**
 * Resolves a structured DiagnosticSolution from an error object or string.
 * Uses server-provided diagnostic if present, or falls back to client-side classification.
 */
export function resolveDiagnostic(
  error: unknown,
  context?: { server?: string; port?: number }
): DiagnosticSolution {
  // 1. Check if error object already contains a server-classified diagnostic
  if (error && typeof error === 'object') {
    const obj = error as Record<string, unknown>
    if (obj.diagnostic && typeof obj.diagnostic === 'object') {
      return obj.diagnostic as DiagnosticSolution
    }
    if (obj.diagnostic_solution && typeof obj.diagnostic_solution === 'object') {
      return obj.diagnostic_solution as DiagnosticSolution
    }
  }

  // 2. Client-side heuristic classification
  const msg = error instanceof Error ? error.message : String(error || '')
  const lower = msg.toLowerCase()
  const portStr = context?.port ? String(context.port) : '5432 / 3306 / 1433'
  const serverStr = context?.server || 'specified server'

  // A. Gemini API Key missing or invalid
  if (
    lower.includes('api_key') ||
    lower.includes('gemini_api_key') ||
    lower.includes('unauthenticated') ||
    lower.includes('permission_denied') ||
    lower.includes('api key not valid') ||
    lower.includes('invalid api key') ||
    lower.includes('default placeholder')
  ) {
    return {
      category: 'api_key',
      title: 'Gemini API Key Required or Invalid',
      summary: 'QueryPilot requires a valid Google Gemini API key to translate your natural language questions into SQL.',
      root_cause: 'The provided API key was rejected by Google (invalid, expired, or missing in settings).',
      actionable_steps: [
        "Click 'Get Free Gemini 🗝️' to generate a free personal API key from Google AI Studio.",
        'Paste the key into the API Key input in the top application header.',
        'Ensure your computer can reach generativelanguage.googleapis.com over the internet.',
      ],
      primary_action: {
        label: 'Get Free Gemini Key 🗝️',
        action_type: 'open_external_url',
        payload: 'https://aistudio.google.com/api-keys',
      },
      secondary_action: {
        label: 'Enter Key in Header',
        action_type: 'focus_api_key_input',
      },
      technical_details: msg,
    }
  }

  // B. Rate limit / Quota exceeded (429)
  if (
    lower.includes('429') ||
    lower.includes('resource_exhausted') ||
    lower.includes('quota exceeded') ||
    lower.includes('rate limit') ||
    lower.includes('too many requests')
  ) {
    return {
      category: 'model_quota',
      title: 'Google Gemini Rate Limit Reached',
      summary: "You have temporarily reached Google AI's per-minute request limit for free-tier models.",
      root_cause: 'Google Gemini free tier enforces request-per-minute (RPM) limits.',
      actionable_steps: [
        'Wait 20-30 seconds for the free-tier rate window to reset, then retry.',
        "Switch to a lower-latency model like 'gemini-3.5-flash-lite' in Settings.",
      ],
      primary_action: {
        label: 'Retry Query',
        action_type: 'retry',
      },
      secondary_action: {
        label: 'Switch to Flash-Lite',
        action_type: 'switch_model',
        payload: 'gemini-3.5-flash-lite',
      },
      technical_details: msg,
    }
  }

  // C. Model unavailable (404)
  if (lower.includes('model_not_found') || (lower.includes('models/') && lower.includes('not found'))) {
    return {
      category: 'model_unavailable',
      title: 'AI Model Unavailable',
      summary: 'The selected AI model is not available for your Google AI account tier or region.',
      root_cause: 'Google preview models frequently change. The requested model was not found.',
      actionable_steps: [
        "Switch to the recommended stable model 'gemini-3.8-flash' or 'gemini-2.5-flash' in Settings.",
      ],
      primary_action: {
        label: 'Switch to gemini-3.8-flash',
        action_type: 'switch_model',
        payload: 'gemini-3.8-flash',
      },
      technical_details: msg,
    }
  }

  // D. Read-Only safety mutation blocked
  if (
    lower.includes('mutation') ||
    lower.includes('read-only') ||
    lower.includes('prohibited in read-only mode') ||
    lower.includes('drop table') ||
    lower.includes('delete from')
  ) {
    return {
      category: 'db_safety_mutation',
      title: 'Data Modification Blocked by Safety Guard',
      summary: 'QueryPilot operates strictly as a read-only analyst. Modifying or deleting data is prohibited.',
      root_cause: 'An AST safety inspection blocked an INSERT, UPDATE, DELETE, or DDL statement.',
      actionable_steps: [
        "Rephrase your question to ask for data retrieval (e.g. 'Show me...' or 'List all...') instead of modifications.",
        'QueryPilot never modifies, deletes, or drops database records.',
      ],
      technical_details: msg,
    }
  }

  // E. Database connection refused / service down
  if (
    lower.includes('connection refused') ||
    lower.includes('10061') ||
    lower.includes('econnrefused') ||
    lower.includes("can't connect to mysql server") ||
    lower.includes('could not connect to server')
  ) {
    return {
      category: 'db_connection_refused',
      title: 'Database Connection Refused',
      summary: `Could not connect to database on ${serverStr} (port ${portStr}).`,
      root_cause: 'The target machine actively refused the connection. The database service may be stopped or blocked.',
      actionable_steps: [
        'Verify your database service is running (e.g., PostgreSQL, MySQL, SQL Server, or Docker container).',
        'Confirm the host address (127.0.0.1 or localhost) and TCP port are correct.',
        'If using Docker, check that the port is published (e.g. -p 5432:5432).',
      ],
      primary_action: {
        label: 'Edit Database Profile',
        action_type: 'open_profile_modal',
      },
      technical_details: msg,
    }
  }

  // F. Database authentication failed
  if (
    lower.includes('password authentication failed') ||
    lower.includes('access denied for user') ||
    lower.includes('login failed for user') ||
    lower.includes('authentication failed')
  ) {
    return {
      category: 'db_auth_failed',
      title: 'Database Authentication Failed',
      summary: 'The database server rejected the provided username or password.',
      root_cause: 'Invalid database credentials in your profile configuration.',
      actionable_steps: [
        'Double check the username and password in your database profile settings.',
        'Verify that the user has permission to connect and read tables from the database.',
      ],
      primary_action: {
        label: 'Update Database Credentials',
        action_type: 'open_profile_modal',
      },
      technical_details: msg,
    }
  }

  // G. SSL required (cloud providers)
  if (
    lower.includes('ssl off') ||
    lower.includes('no pg_hba.conf entry') ||
    lower.includes('ssl is required') ||
    lower.includes('ssl connection is required')
  ) {
    return {
      category: 'db_ssl_required',
      title: 'SSL Encryption Required by Cloud Database',
      summary: 'Your cloud database provider (Supabase, Neon, AWS RDS, or GCP) mandates encrypted SSL connections.',
      root_cause: 'Connection was attempted without SSL encryption.',
      actionable_steps: [
        "Edit your database profile and set SSL Mode to 'require'.",
        'For Supabase or Neon, verify that your connection string enables SSL.',
      ],
      primary_action: {
        label: 'Enable SSL in Profile',
        action_type: 'open_profile_modal',
        payload: 'ssl_enable',
      },
      technical_details: msg,
    }
  }

  // H. Database timeout (15-second ceiling)
  if (lower.includes('timeout') && (lower.includes('statement') || lower.includes('ceiling') || lower.includes('504'))) {
    return {
      category: 'db_timeout',
      title: 'Query Statement Timed Out (15s Ceiling)',
      summary: 'Query execution was safely halted because it exceeded the 15-second execution limit.',
      root_cause: 'The database query took too long to complete (e.g. scanning unindexed tables).',
      actionable_steps: [
        "Ask for a more specific subset of data with filters (e.g. 'for year 2024' or 'top 10').",
        'Ensure target foreign keys and search columns are indexed in your database.',
      ],
      primary_action: {
        label: 'Ask More Specific Question',
        action_type: 'retry',
      },
      technical_details: msg,
    }
  }

  // I. Schema table or column missing
  if (
    lower.includes('no such table') ||
    lower.includes('no such column') ||
    lower.includes('undefined_table') ||
    lower.includes('relation') && lower.includes('does not exist') ||
    lower.includes("table doesn't exist") ||
    lower.includes('unknown column') ||
    lower.includes('invalid column name')
  ) {
    return {
      category: 'db_schema_not_found',
      title: 'Database Table or Column Not Found',
      summary: 'The query referenced a table or column name that does not exist in your database.',
      root_cause: 'The AI model hallucinated a table or column name, or your schema was updated.',
      actionable_steps: [
        'Re-sync your database schema by refreshing the profile.',
        'Rephrase your question to include the exact table or column name.',
      ],
      primary_action: {
        label: 'Retry with Rephrased Question',
        action_type: 'retry',
      },
      technical_details: msg,
    }
  }

  // J. Generic Fallback
  return {
    category: 'unknown',
    title: 'Unexpected Error',
    summary: msg.length < 160 ? msg : `${msg.slice(0, 157)}...`,
    root_cause: 'An unexpected system or database error occurred.',
    actionable_steps: [
      'Review the technical details below to diagnose the issue.',
      'Retry the operation or check local database logs.',
    ],
    primary_action: {
      label: 'Retry Operation',
      action_type: 'retry',
    },
    technical_details: msg,
  }
}
