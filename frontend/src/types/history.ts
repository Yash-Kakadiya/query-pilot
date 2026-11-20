/**
 * QueryPilot Persistent Query History & Favorites Types (Phase 3 / Option 1).
 *
 * Types for persisted user query executions, starred favorites, full-text search,
 * profile tagging, and offline caching.
 */

export interface HistoryItem {
  id: string
  timestamp: number
  question?: string | null
  sql: string
  profile_id: string
  profile_name: string
  engine_type: string
  status: 'succeeded' | 'failed' | 'rejected'
  row_count?: number | null
  execution_time_ms?: number | null
  error_message?: string | null
  synthesized_answer?: string | null
  result_columns?: string[] | null
  is_favorite: boolean
  tags: string[]
  notes?: string | null
}

export interface HistoryItemCreate {
  id?: string
  timestamp?: number
  question?: string | null
  sql: string
  profile_id: string
  profile_name: string
  engine_type: string
  status?: 'succeeded' | 'failed' | 'rejected'
  row_count?: number | null
  execution_time_ms?: number | null
  error_message?: string | null
  synthesized_answer?: string | null
  result_columns?: string[] | null
  is_favorite?: boolean
  tags?: string[]
  notes?: string | null
}

export interface HistoryListResponse {
  entries: HistoryItem[]
  total_count: number
  favorites_count: number
}

export interface HistoryToggleStarResponse {
  id: string
  is_favorite: boolean
  message: string
}

export interface HistoryDeleteResponse {
  success: boolean
  deleted_count: number
  message: string
}

export interface HistoryFilterOptions {
  tab?: 'recent' | 'favorites'
  search?: string
  profileId?: string
  tag?: string
}
