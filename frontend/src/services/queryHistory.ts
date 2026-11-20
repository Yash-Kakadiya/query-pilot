/**
 * Query History Coordinator.
 *
 * Coordinates persistent query history and favorites across dual-layer storage:
 * 1. Local Cache (localStorage + in-memory): Immediate sub-millisecond retrieval and offline capability.
 * 2. Local Daemon Backend (/agent/v1/history): Canonical disk persistence in ~/.query_pilot/history.json.
 *
 * Provides optimistic UI updates for toggling favorites and deleting entries,
 * with graceful offline fallback when daemon is disconnected.
 */

import type {
  HistoryDeleteResponse,
  HistoryItem,
  HistoryItemCreate,
  HistoryListResponse,
  HistoryToggleStarResponse,
} from '../types/history.ts'
import {
  clearHistory as apiClearHistory,
  deleteHistoryEntry as apiDeleteHistoryEntry,
  getHistory as apiGetHistory,
  recordHistory as apiRecordHistory,
  toggleHistoryStar as apiToggleHistoryStar,
} from './agentClient.ts'

const STORAGE_KEY = 'querypilot_history_cache'
const MAX_LOCAL_ITEMS = 500

export type HistoryChangeListener = (items: HistoryItem[]) => void

export class QueryHistoryCoordinator {
  private items: HistoryItem[] = []
  private listeners: Set<HistoryChangeListener> = new Set()
  private isLoadedFromStorage = false

  public isLoaded(): boolean {
    return this.isLoadedFromStorage
  }

  constructor() {
    this.loadFromLocalStorage()
  }

  /**
   * Subscribe to history state updates.
   */
  public subscribe(listener: HistoryChangeListener): () => void {
    this.listeners.add(listener)
    listener(this.getItems())
    return () => {
      this.listeners.delete(listener)
    }
  }

  private notify(): void {
    const current = this.getItems()
    for (const listener of this.listeners) {
      try {
        listener(current)
      } catch (err) {
        console.error('[QueryHistory] Listener error:', err)
      }
    }
  }

  /**
   * Loads cached history items from localStorage.
   */
  private loadFromLocalStorage(): void {
    if (typeof window === 'undefined' || !window.localStorage) {
      return
    }
    try {
      const raw = window.localStorage.getItem(STORAGE_KEY)
      if (raw) {
        const parsed = JSON.parse(raw)
        if (Array.isArray(parsed)) {
          this.items = parsed
          this.isLoadedFromStorage = true
        }
      }
    } catch (err) {
      console.warn('[QueryHistory] Failed to read cached history from localStorage:', err)
    }
  }

  /**
   * Persists items to localStorage.
   */
  private saveToLocalStorage(): void {
    if (typeof window === 'undefined' || !window.localStorage) {
      return
    }
    try {
      const slice = this.items.slice(0, MAX_LOCAL_ITEMS)
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(slice))
    } catch (err) {
      console.warn('[QueryHistory] Failed to save history to localStorage:', err)
    }
  }

  /**
   * Returns current in-memory history items (most recent first).
   */
  public getItems(): HistoryItem[] {
    return [...this.items]
  }

  /**
   * Synchronizes history with the daemon backend.
   * If daemon is disconnected or unavailable, smoothly returns cached items.
   */
  public async syncWithDaemon(profileId?: string): Promise<HistoryItem[]> {
    try {
      const resp: HistoryListResponse = await apiGetHistory({
        profileId,
        limit: 500,
      })
      if (resp && Array.isArray(resp.entries)) {
        this.items = resp.entries
        this.saveToLocalStorage()
        this.notify()
        return this.getItems()
      }
    } catch (err) {
      // Daemon may be unauthenticated or starting up; fallback to local cache
      console.debug('[QueryHistory] Daemon sync unavailable, using local cache:', err)
    }
    return this.getItems()
  }

  /**
   * Records an executed query into history.
   * Optimistically updates local cache and broadcasts change, then posts to daemon.
   */
  public async recordQuery(payload: HistoryItemCreate): Promise<HistoryItem> {
    const tempId = payload.id || `hist_${Date.now()}_${Math.random().toString(36).substring(2, 9)}`
    const optimisticItem: HistoryItem = {
      id: tempId,
      timestamp: payload.timestamp || Date.now(),
      question: payload.question || null,
      sql: payload.sql.trim(),
      profile_id: payload.profile_id,
      profile_name: payload.profile_name,
      engine_type: payload.engine_type,
      status: payload.status || 'succeeded',
      row_count: payload.row_count ?? null,
      execution_time_ms: payload.execution_time_ms ?? null,
      error_message: payload.error_message || null,
      synthesized_answer: payload.synthesized_answer || null,
      result_columns: payload.result_columns || null,
      is_favorite: Boolean(payload.is_favorite),
      tags: Array.isArray(payload.tags) ? payload.tags : [],
      notes: payload.notes || null,
    }

    // Insert at front
    this.items = [optimisticItem, ...this.items.filter((i) => i.id !== tempId)].slice(0, MAX_LOCAL_ITEMS)
    this.saveToLocalStorage()
    this.notify()

    try {
      const persisted = await apiRecordHistory(payload)
      if (persisted && persisted.id) {
        // Update temp ID with daemon-assigned ID if different
        this.items = this.items.map((i) => (i.id === tempId ? persisted : i))
        this.saveToLocalStorage()
        this.notify()
        return persisted
      }
    } catch (err) {
      console.warn('[QueryHistory] Failed to persist query history to daemon, saved locally:', err)
    }

    return optimisticItem
  }

  /**
   * Toggles the favorite (starred) state of an entry with optimistic update.
   */
  public async toggleStar(id: string): Promise<boolean> {
    const item = this.items.find((i) => i.id === id)
    if (!item) return false

    const previousState = item.is_favorite
    const nextState = !previousState

    // Optimistic local update
    this.items = this.items.map((i) => (i.id === id ? { ...i, is_favorite: nextState } : i))
    this.saveToLocalStorage()
    this.notify()

    try {
      const resp: HistoryToggleStarResponse = await apiToggleHistoryStar(id)
      if (resp && resp.is_favorite !== nextState) {
        item.is_favorite = resp.is_favorite
        this.saveToLocalStorage()
        this.notify()
      }
      return nextState
    } catch (err) {
      console.warn('[QueryHistory] Failed to toggle star on daemon, keeping local state:', err)
      return nextState
    }
  }

  /**
   * Deletes a single history entry with optimistic update.
   */
  public async deleteEntry(id: string): Promise<boolean> {
    this.items = this.items.filter((i) => i.id !== id)
    this.saveToLocalStorage()
    this.notify()

    try {
      const resp: HistoryDeleteResponse = await apiDeleteHistoryEntry(id)
      return resp ? resp.success : true
    } catch (err) {
      console.warn('[QueryHistory] Failed to delete entry on daemon, keeping local deletion:', err)
      return true
    }
  }

  /**
   * Clears query history.
   * If clearFavorites is false, starred favorites are preserved.
   */
  public async clearHistory(clearFavorites: boolean = false, profileId?: string): Promise<number> {
    const previous = [...this.items]

    this.items = this.items.filter((i) => {
      if (profileId && i.profile_id !== profileId) return true
      if (!clearFavorites && i.is_favorite) return true
      return false
    })

    const deletedLocal = previous.length - this.items.length
    this.saveToLocalStorage()
    this.notify()

    try {
      const resp = await apiClearHistory(clearFavorites, profileId)
      return resp.deleted_count
    } catch (err) {
      console.warn('[QueryHistory] Daemon clearHistory call failed, updated locally:', err)
      return deletedLocal
    }
  }
}

export const queryHistoryCoordinator = new QueryHistoryCoordinator()
