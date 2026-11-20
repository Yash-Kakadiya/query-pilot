/**
 * History Filter and Search Service.
 *
 * Provides instantaneous, sub-millisecond client-side filtering and multi-attribute search
 * across question, SQL, profile name, tags, and notes for QueryPilot Persistent History.
 */

import type { HistoryFilterOptions, HistoryItem } from '../types/history.ts'

/**
 * Filters and searches history items based on active tab, profile, tag, and query text.
 */
export function filterHistoryItems(
  items: HistoryItem[],
  options: HistoryFilterOptions
): HistoryItem[] {
  const { tab = 'recent', search = '', profileId, tag } = options
  const normalizedSearch = search.trim().toLowerCase()

  return items.filter((item) => {
    // 1. Tab filter
    if (tab === 'favorites' && !item.is_favorite) {
      return false
    }

    // 2. Profile filter
    if (profileId && item.profile_id !== profileId) {
      return false
    }

    // 3. Tag filter
    if (tag && (!item.tags || !item.tags.includes(tag))) {
      return false
    }

    // 4. Search query (matches question, SQL, profile name, notes, or tags)
    if (normalizedSearch) {
      const matchQuestion = item.question?.toLowerCase().includes(normalizedSearch) ?? false
      const matchSql = item.sql.toLowerCase().includes(normalizedSearch)
      const matchProfile = item.profile_name.toLowerCase().includes(normalizedSearch)
      const matchNotes = item.notes?.toLowerCase().includes(normalizedSearch) ?? false
      const matchTags = item.tags ? item.tags.some((t) => t.toLowerCase().includes(normalizedSearch)) : false

      if (!matchQuestion && !matchSql && !matchProfile && !matchNotes && !matchTags) {
        return false
      }
    }

    return true
  })
}

/**
 * Extracts a sorted, unique list of tags across all history items.
 */
export function extractUniqueTags(items: HistoryItem[]): string[] {
  const set = new Set<string>()
  for (const item of items) {
    if (Array.isArray(item.tags)) {
      for (const t of item.tags) {
        if (t && t.trim()) {
          set.add(t.trim())
        }
      }
    }
  }
  return Array.from(set).sort((a, b) => a.localeCompare(b))
}
