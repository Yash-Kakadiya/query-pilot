/**
 * QueryPilot System Health & Full App Diagnostic Types.
 *
 * Defines contracts for running comprehensive subsystem diagnostics across the desktop application:
 * - Local agent daemon loopback connectivity and version alignment.
 * - Deep backend subsystems and database driver availability.
 * - Ambient authentication and credential isolation.
 * - Active database profile selection and physical connection handshake.
 * - Read-only transaction boundaries and execution safety ceilings.
 * - Information schema dynamic introspection and table grounding.
 * - AST SQL safety validator (fail-closed mutation blockades).
 * - Gemini AI credentials and model configuration.
 * - Browser local storage and runtime persistence.
 * - Data export engine and formula injection protection.
 */

import type { ProfileSummary } from './api.ts'

export type DiagnosticCheckId =
  | 'daemon_connectivity'
  | 'system_subsystems'
  | 'session_auth'
  | 'active_profile'
  | 'db_connection'
  | 'read_only_boundary'
  | 'schema_introspection'
  | 'sql_safety_engine'
  | 'gemini_credentials'
  | 'storage_integrity'
  | 'data_export_engine'
  | 'history_persistence'
  | 'visual_schema_and_dictionary'

export type DiagnosticStatus =
  | 'pending'
  | 'running'
  | 'passed'
  | 'failed'
  | 'warning'
  | 'skipped'

export type DiagnosticCategory =
  | 'core_daemon'
  | 'database_profile'
  | 'safety_guard'
  | 'ai_gemini'
  | 'client_runtime'

export type DiagnosticOverallStatus =
  | 'healthy'
  | 'warning'
  | 'critical'
  | 'idle'

export interface DiagnosticCheck {
  id: DiagnosticCheckId
  name: string
  category: DiagnosticCategory
  description: string
  status: DiagnosticStatus
  durationMs?: number
  message?: string
  details?: string
  solution?: string
  actionText?: string
  actionType?: 'open_key_settings' | 'select_profile' | 'create_profile' | 'retest_connection'
}

export interface DiagnosticReport {
  timestamp: number
  total: number
  passed: number
  failed: number
  warnings: number
  skipped: number
  successRate: number // 0 to 100 percentage
  overallStatus: DiagnosticOverallStatus
  summary: string
  checks: DiagnosticCheck[]
}

export interface DiagnosticRunOptions {
  activeProfile?: ProfileSummary | null
  profiles?: ProfileSummary[]
  apiKey?: string
  model?: string
  maxRows?: string
  queryTimeout?: string
  onProgress?: (updatedReport: DiagnosticReport) => void
  fetchFn?: typeof fetch
}
