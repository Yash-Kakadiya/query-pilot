import type { ProfileSummary } from '../types/api.ts'

/**
 * Determine the database engine type for a connection profile.
 *
 * Precedence:
 * 1. Explicit `engine_type` metadata takes strict precedence. An MSSQL profile with
 *    schema 'main' is never misidentified as SQLite when engine_type is 'mssql'.
 * 2. Fallback heuristic solely for legacy profiles where `engine_type` metadata is missing.
 * 3. Default safe engine type is 'mssql'.
 */
export function getProfileEngine(profile: ProfileSummary | null | undefined): 'mssql' | 'sqlite' {
  if (!profile) {
    return 'mssql'
  }
  // 1. Explicit engine_type metadata takes strict precedence
  if (profile.engine_type) {
    return profile.engine_type === 'sqlite' ? 'sqlite' : 'mssql'
  }
  // 2. Legacy fallback only when engine_type is absent
  if (profile.schema_name?.toLowerCase() === 'main') {
    return 'sqlite'
  }
  return 'mssql'
}

