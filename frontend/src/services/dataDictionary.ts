/**
 * Data Dictionary Service.
 *
 * Manages retrieval, generation, and in-memory caching of plain-English
 * business metadata for database tables and columns.
 */

import {
  getDataDictionary as apiGetDataDictionary,
  generateDataDictionary as apiGenerateDataDictionary,
} from './agentClient.ts'
import type { DataDictionaryResponse } from '../types/api.ts'

// In-memory cache for instant switching between tables
const dictionaryCache: Map<string, DataDictionaryResponse> = new Map()

export async function fetchProfileDataDictionary(
  profileId: string,
  bypassCache: boolean = false
): Promise<DataDictionaryResponse> {
  if (!bypassCache && dictionaryCache.has(profileId)) {
    return dictionaryCache.get(profileId)!
  }

  const response = await apiGetDataDictionary(profileId)
  dictionaryCache.set(profileId, response)
  return response
}

export async function enrichProfileDataDictionary(
  profileId: string,
  customGeminiKey?: string | null
): Promise<DataDictionaryResponse> {
  const response = await apiGenerateDataDictionary(profileId, customGeminiKey)
  dictionaryCache.set(profileId, response)
  return response
}

export function clearDataDictionaryCache(profileId?: string): void {
  if (profileId) {
    dictionaryCache.delete(profileId)
  } else {
    dictionaryCache.clear()
  }
}
