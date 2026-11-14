import assert from 'node:assert'
import test from 'node:test'
import { getStoredGeminiKey, setStoredGeminiKey } from '../src/services/geminiKeyStorage.ts'
import { getHealth } from '../src/services/apiClient.ts'
import { generateSql } from '../src/services/v1ApiClient.ts'
import type { GenerateSQLRequest } from '../src/types/api.ts'

test('Gemini Key Storage & Header Propagation', async (t) => {
  const originalFetch = globalThis.fetch
  const mockLocalStorage: Record<string, string> = {}

  // Mock localStorage for Node test runner
  Object.defineProperty(globalThis, 'localStorage', {
    value: {
      getItem: (k: string) => mockLocalStorage[k] || null,
      setItem: (k: string, v: string) => {
        mockLocalStorage[k] = v
      },
      removeItem: (k: string) => {
        delete mockLocalStorage[k]
      },
      clear: () => {
        for (const k of Object.keys(mockLocalStorage)) {
          delete mockLocalStorage[k]
        }
      },
    },
    writable: true,
    configurable: true,
  })

  t.afterEach(() => {
    globalThis.fetch = originalFetch
    setStoredGeminiKey('')
  })

  await t.test('1. Reads empty string when no key is configured', () => {
    assert.strictEqual(getStoredGeminiKey(), '')
  })

  await t.test('2. Saves and retrieves custom primary Gemini key', () => {
    setStoredGeminiKey('AIzaSyCustomKey123')
    assert.strictEqual(getStoredGeminiKey(), 'AIzaSyCustomKey123')
  })

  await t.test('3. Clearing key removes it from storage', () => {
    setStoredGeminiKey('AIzaSyCustomKey123')
    assert.strictEqual(getStoredGeminiKey(), 'AIzaSyCustomKey123')
    setStoredGeminiKey('')
    assert.strictEqual(getStoredGeminiKey(), '')
  })

  await t.test('4. getHealth() propagates X-Gemini-API-Key when custom key configured', async () => {
    setStoredGeminiKey('AIzaSyHealthKey')
    let capturedHeaders: Record<string, string> = {}

    globalThis.fetch = async (_input: RequestInfo | URL, init?: RequestInit) => {
      capturedHeaders = (init?.headers as Record<string, string>) || {}
      return new Response(JSON.stringify({ status: 'healthy', version: '0.2.0' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    }

    const health = await getHealth()
    assert.strictEqual(health.status, 'healthy')
    assert.strictEqual(capturedHeaders['X-Gemini-API-Key'], 'AIzaSyHealthKey')
  })

  await t.test('5. generateSql() propagates X-Gemini-API-Key when custom key configured', async () => {
    setStoredGeminiKey('AIzaSyGenSqlKey')
    let capturedHeaders: Record<string, string> = {}

    globalThis.fetch = async (_input: RequestInfo | URL, init?: RequestInit) => {
      capturedHeaders = (init?.headers as Record<string, string>) || {}
      return new Response(
        JSON.stringify({
          candidate_sql: 'SELECT 1;',
          status: 'generated',
          dialect: 'sqlite',
        }),
        {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }
      )
    }

    const req: GenerateSQLRequest = {
      question: 'Show all students',
      schema_context: {
        database_name: 'test_db',
        tables: [{ schema_name: 'main', table_name: 'students', columns: [{ name: 'id', data_type: 'int' }] }],
      },
      dialect: 'sqlite',
    }

    const res = await generateSql(req)
    assert.strictEqual(res.candidate_sql, 'SELECT 1;')
    assert.strictEqual(capturedHeaders['X-Gemini-API-Key'], 'AIzaSyGenSqlKey')
  })
})
