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

// =============================================================================
// Local Agent Daemon Contracts (Phase 7.1+)
// =============================================================================

export interface BootstrapRequest {
  /** Single-use bootstrap token read from the terminal or ~/.query_pilot/agent.token (16-128 chars). */
  bootstrap_token: string
}

export interface BootstrapResponse {
  status: string
  message: string
}

export interface LogoutResponse {
  status: string
  message: string
}

export interface RotateTokenResponse {
  status: string
  message: string
}

export interface ProfileSummary {
  profile_id: string
  display_name: string
  schema_name?: string | null
  timeout_seconds: number
  max_row_limit: number
  has_allowlist: boolean
  allowed_tables?: string[] | null
}

export interface ProfileListResponse {
  profiles: ProfileSummary[]
}

export interface AgentErrorResponse {
  error: string
  detail: string
}

export type AgentErrorKind =
  | 'authentication'
  | 'forbidden'
  | 'not_found'
  | 'conflict'
  | 'timeout'
  | 'disconnected'
  | 'validation'
  | 'rate_limit'
  | 'payload_limit'
  | 'server'
  | 'parse'

// =============================================================================
// Profile Onboarding & Explicit Connection Testing Contracts (B12-F8H)
// =============================================================================

export interface ValidateProfileConfigRequest {
  profile_id: string
  display_name: string
  server: string
  database_name: string
  schema_name?: string
  driver?: string
  engine_type?: 'mssql'
  use_trusted_connection?: boolean
  timeout_seconds?: number
  max_row_limit?: number
  allowed_tables?: string[] | null
  sensitive_columns?: string[] | null
  set_as_default?: boolean
}

export interface ValidateProfileConfigResponse {
  valid: boolean
  message: string
}

export interface CreateProfileRequest extends ValidateProfileConfigRequest {
  set_as_default?: boolean
}

export interface CreateProfileResponse {
  profile: ProfileSummary
  is_default: boolean
  message: string
}

export interface TestConnectionRequest {
  // Deliberately empty object to reject arbitrary client connection parameters
}

export type ConnectionFailureCode =
  | 'AUTH_FAILED'
  | 'UNREACHABLE'
  | 'TIMEOUT'
  | 'DATABASE_MISMATCH'
  | 'DRIVER_ERROR'
  | 'CONNECTION_FAILED'

export interface TestConnectionResponse {
  connected: boolean
  database_name?: string | null
  latency_ms?: number | null
  message: string
  failure_code?: ConnectionFailureCode | null
}


export class AgentClientError extends Error {
  readonly kind: AgentErrorKind
  readonly statusCode?: number
  readonly requestId?: string | null
  readonly errorCode?: string | null
  readonly detail?: string | null

  constructor(options: {
    message: string
    kind: AgentErrorKind
    statusCode?: number
    requestId?: string | null
    errorCode?: string | null
    detail?: string | null
  }) {
    super(options.message)
    this.name = 'AgentClientError'
    this.kind = options.kind
    this.statusCode = options.statusCode
    this.requestId = options.requestId
    this.errorCode = options.errorCode
    this.detail = options.detail
  }
}

export interface AgentColumnMetadata {
  name: string
  data_type: string
  nullable?: boolean
  primary_key?: boolean
  default?: string | null
  is_identity?: boolean
  comment?: string | null
}

export interface AgentForeignKeyMetadata {
  source_table: string
  source_column: string
  target_table: string
  target_column: string
  name?: string | null
}

export interface AgentIndexMetadata {
  name: string
  columns: string[]
  is_unique?: boolean
  filter_definition?: string | null
}

export interface AgentTableMetadata {
  schema_name: string
  table_name: string
  columns: AgentColumnMetadata[]
  primary_keys?: string[]
  foreign_keys?: AgentForeignKeyMetadata[]
  indexes?: AgentIndexMetadata[]
  unique_constraints?: Record<string, unknown>[]
}

export interface AgentDatabaseSchema {
  database_name: string
  is_model_facing?: boolean
  total_tables?: number
  total_columns?: number
  total_foreign_keys?: number
  tables: AgentTableMetadata[]
}

export interface ExecuteQueryRequest {
  /** Read-only SQL candidate query string (1-10000 characters). Note: uses 'sql', NOT 'candidate_sql'. */
  sql: string
  /** Optional client correlation or request ID. */
  request_id?: string | null
}

export interface ExecuteQueryResponse {
  columns: string[]
  rows: Record<string, unknown>[]
  row_count: number
  truncated: boolean
  execution_time_ms: number
  request_id?: string | null
}

// =============================================================================
// Hosted V1 API Models (Decoupled SQL Generation & Metadata Synthesis)
// =============================================================================

export interface ColumnSchemaContext {
  name: string
  data_type: string
  nullable?: boolean
  primary_key?: boolean
  comment?: string | null
}

export interface ForeignKeySchemaContext {
  source_table: string
  source_column: string
  target_table: string
  target_column: string
  name?: string | null
}

export interface TableSchemaContext {
  table_name: string
  schema_name?: string
  columns: ColumnSchemaContext[]
  primary_keys?: string[]
  foreign_keys?: ForeignKeySchemaContext[]
  description?: string | null
}

export interface ModelFacingSchemaContext {
  database_name: string
  tables: TableSchemaContext[]
}

export type GenerationStatus =
  | 'generated'
  | 'clarification_needed'
  | 'unsupported'
  | 'generation_failed'

export interface GenerateSQLRequest {
  question: string
  schema_context: ModelFacingSchemaContext
  conversation_context?: ConversationTurnContext[] | null
  request_id?: string | null
  diagnostic_feedback?: string | null
}

export interface GenerateSQLResponse {
  candidate_sql: string | null
  status: GenerationStatus
  clarification_prompt?: string | null
  clarification_options?: ClarificationOption[]
  unsupported_reason?: string | null
  execution_disclaimer: string
  request_id: string
}

export type AnswerGroundingStatus =
  | 'grounded_from_metadata'
  | 'requires_row_disclosure'
  | 'unavailable'

export interface SynthesizeAnswerRequest {
  question: string
  candidate_sql: string
  columns: string[]
  row_count: number
  truncated?: boolean
  /** Local execution status; must be strictly 'succeeded'. */
  execution_status: string
  execution_time_ms: number
  request_id: string
}

export interface SynthesizeAnswerResponse {
  summary: string
  status: AnswerGroundingStatus
  grounding_notes?: string | null
  request_id: string
}

// Re-export pure schema projection helper
export {
  projectToModelFacingSchema,
  SchemaProjectionError,
  MAX_SCHEMA_TABLES,
  MAX_COLUMNS_PER_TABLE,
  MAX_IDENTIFIER_LENGTH,
  MAX_DESCRIPTION_LENGTH,
} from '../services/schemaProjection.ts'


