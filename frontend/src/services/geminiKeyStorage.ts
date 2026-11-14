/**
 * Local storage manager for user-configured Gemini API Key.
 *
 * Guarantees:
 * - Persisted in browser/WebView2 localStorage under 'querypilot_gemini_api_key'.
 * - Custom key is treated as the primary key for all LLM reasoning tasks.
 * - When empty or invalid, the system automatically falls back to environment backup keys.
 */

const STORAGE_KEY = 'querypilot_gemini_api_key'

let inMemoryKey = ''

function resolveStorage(): Storage | null {
  try {
    if (typeof window !== 'undefined' && window.localStorage) {
      return window.localStorage
    }
    if (typeof globalThis !== 'undefined' && (globalThis as unknown as { localStorage?: Storage }).localStorage) {
      return (globalThis as unknown as { localStorage: Storage }).localStorage
    }
  } catch {
    // Storage access restricted
  }
  return null
}

export function getStoredGeminiKey(): string {
  try {
    const storage = resolveStorage()
    if (storage) {
      const val = storage.getItem(STORAGE_KEY)
      if (val !== null && val !== undefined) {
        return val.trim()
      }
    }
  } catch {
    // Gracefully handle restricted storage environments
  }
  return inMemoryKey
}

export function setStoredGeminiKey(key: string): void {
  const trimmed = key.trim()
  inMemoryKey = trimmed
  try {
    const storage = resolveStorage()
    if (storage) {
      if (trimmed) {
        storage.setItem(STORAGE_KEY, trimmed)
      } else {
        storage.removeItem(STORAGE_KEY)
      }
    }
    if (typeof window !== 'undefined' && typeof window.dispatchEvent === 'function') {
      window.dispatchEvent(new Event('querypilot_gemini_key_changed'))
    }
  } catch {
    // Gracefully handle restricted storage environments
  }
}
