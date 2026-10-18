/**
 * TypeScript API contracts matching QueryPilot FastAPI models.
 */

// =============================================================================
// Health Contract
// =============================================================================

export interface HealthResponse {
  status: string
  version: string
  dialect: string
  tracing_enabled: boolean
}

// =============================================================================
// Pipeline Statuses
// =============================================================================

export type KnownPipelineStatus =
  | 'SUCCESS'
  | 'VALIDATED_ONLY'
  | 'UNSUPPORTED'
  | 'CLARIFICATION_NEEDED'
  | 'GENERATION_FAILED'
  | 'VALIDATION_REJECTED'
  | 'EXECUTION_FAILED'

/**
 * High-level pipeline execution outcome.
 * Preserves the 7 known statuses while safely allowing unrecognized status strings.
 */
export type PipelineStatus = KnownPipelineStatus | (string & {})

// =============================================================================
// Query Request & Sub-Models
// =============================================================================

export interface ConversationTurnContext {
  /** Participant role (strictly 'user'). */
  role?: 'user'
  /** User question asked in prior turn (1-1000 characters). */
  user_question: string
  /** Optional resolved domain intent or summary (max 1000 characters). */
  resolved_intent?: string | null
  /** Names of columns returned by that turn's query. */
  result_columns?: string[]
}

export type ExecutionMode = 'execute' | 'sql_only'

export interface QueryRequest {
  /** Natural-language question (non-empty, max 1000 characters). */
  question: string
  /** Optional prior turns context (max 3 turns). */
  conversation_context?: ConversationTurnContext[] | null
  /** Execution mode: 'execute' (default) or 'sql_only'. */
  execution_mode?: ExecutionMode
  /** Whether to return eligible validated SQL in response (default: false). */
  include_sql?: boolean
}

// =============================================================================
// Query Response & Sub-Models
// =============================================================================

export interface ClarificationOption {
  option_id: string
  description: string
  suggested_question?: string | null
}

export interface ValidationReasonResponse {
  code: string
  message: string
}

export interface GroundedAnswerResponse {
  text: string
  status: string
  is_mechanically_verified: boolean
  verification_notes: string[]
}

export interface QueryResponse {
  request_id: string | null
  status: PipelineStatus
  question: string
  message: string | null
  columns: string[]
  rows: Record<string, unknown>[]
  row_count: number
  truncated: boolean
  scalar_value: unknown | null
  markdown_table: string | null
  explanation: string | null
  clarification_prompt: string | null
  clarification_options: ClarificationOption[]
  validation_reasons: ValidationReasonResponse[]
  grounded_answer: GroundedAnswerResponse | null
  generated_sql: string | null
}

// =============================================================================
// Error Handling Contracts
// =============================================================================

export type ApiErrorKind = 'validation' | 'server' | 'network' | 'parse' | 'unauthorized'

export interface ValidationErrorDetail {
  loc: (string | number)[]
  msg: string
  type: string
}

export class ApiClientError extends Error {
  readonly kind: ApiErrorKind
  readonly statusCode?: number
  readonly requestId?: string | null
  readonly validationErrors?: ValidationErrorDetail[]

  constructor(options: {
    message: string
    kind: ApiErrorKind
    statusCode?: number
    requestId?: string | null
    validationErrors?: ValidationErrorDetail[]
  }) {
    super(options.message)
    this.name = 'ApiClientError'
    this.kind = options.kind
    this.statusCode = options.statusCode
    this.requestId = options.requestId
    this.validationErrors = options.validationErrors
  }
}
