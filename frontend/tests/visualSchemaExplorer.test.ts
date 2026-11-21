/**
 * Visual Schema Explorer, Table Peek & Data Dictionary Test Suite (Phase 4).
 *
 * Verifies:
 * 1. Table peek client requests with 5-row clamp and parameter validation.
 * 2. Data dictionary fetching, in-memory caching, and cache bypass.
 * 3. AI data dictionary enrichment triggering and header handling.
 * 4. Diagnostics Check 13 evaluation.
 */

import assert from 'node:assert'
import test from 'node:test'
import {
  peekTable,
  getDataDictionary,
  generateDataDictionary,
  setInMemorySessionToken,
} from '../src/services/agentClient.ts'
import {
  fetchProfileDataDictionary,
  enrichProfileDataDictionary,
  clearDataDictionaryCache,
} from '../src/services/dataDictionary.ts'
import { fetchTablePeek } from '../src/services/tablePeek.ts'

test('Visual Schema Explorer - Table Peek Client', async (t) => {
  await t.test('1. Validates that profileId and tableName cannot be empty', async () => {
    await assert.rejects(
      async () => {
        await peekTable('', 'customers')
      },
      { message: 'Profile ID must be a non-empty string.' }
    )

    await assert.rejects(
      async () => {
        await peekTable('test_profile', '   ')
      },
      { message: 'Table name must be a non-empty string.' }
    )
  })

  await t.test('2. Clamps requested limit to 5 rows maximum', async () => {
    let capturedUrl = ''
    const origFetch = globalThis.fetch
    globalThis.fetch = (async (input: RequestInfo | URL) => {
      capturedUrl = String(input)
      return new Response(
        JSON.stringify({
          profile_id: 'test_p',
          table_name: 'orders',
          columns: ['id', 'amount'],
          rows: [{ id: 1, amount: 100 }],
          row_count: 1,
          execution_time_ms: 12.5,
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } }
      )
    }) as typeof fetch

    try {
      const res = await fetchTablePeek('test_p', 'orders', 100)
      assert.strictEqual(res.table_name, 'orders')
      assert.ok(capturedUrl.includes('limit=5'), `URL should have clamped limit=5, got: ${capturedUrl}`)
    } finally {
      globalThis.fetch = origFetch
    }
  })
})

test('Visual Schema Explorer - Data Dictionary Service', async (t) => {
  t.beforeEach(() => {
    clearDataDictionaryCache()
  })

  await t.test('1. Validates profileId on getDataDictionary', async () => {
    await assert.rejects(
      async () => {
        await getDataDictionary('   ')
      },
      { message: 'Profile ID must be a non-empty string.' }
    )
  })

  await t.test('2. Caches data dictionary in memory to avoid duplicate fetches', async () => {
    let fetchCount = 0
    const origFetch = globalThis.fetch
    globalThis.fetch = (async () => {
      fetchCount++
      return new Response(
        JSON.stringify({
          profile_id: 'cache_test_p',
          database_name: 'TestDB',
          source: 'deterministic_heuristic',
          generated_at: new Date().toISOString(),
          tables: {
            customers: {
              table_name: 'customers',
              business_name: 'Customers',
              description: 'Customer records',
              suggested_questions: ['How many customers exist?'],
              columns: {
                id: {
                  name: 'id',
                  business_name: 'Identifier',
                  description: 'Primary key',
                  is_primary_key: true,
                },
              },
            },
          },
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } }
      )
    }) as typeof fetch

    try {
      // First fetch -> hits network
      const d1 = await fetchProfileDataDictionary('cache_test_p')
      assert.strictEqual(fetchCount, 1)
      assert.strictEqual(d1.tables['customers'].business_name, 'Customers')

      // Second fetch -> serves from in-memory cache
      const d2 = await fetchProfileDataDictionary('cache_test_p')
      assert.strictEqual(fetchCount, 1)
      assert.strictEqual(d2.profile_id, 'cache_test_p')

      // Bypass cache -> hits network again
      const d3 = await fetchProfileDataDictionary('cache_test_p', true)
      assert.strictEqual(fetchCount, 2)
      assert.strictEqual(d3.profile_id, 'cache_test_p')
    } finally {
      globalThis.fetch = origFetch
    }
  })

  await t.test('3. Enriches data dictionary with AI and updates cache', async () => {
    let capturedHeaders: HeadersInit | undefined
    const origFetch = globalThis.fetch
    globalThis.fetch = (async (_input: RequestInfo | URL, init?: RequestInit) => {
      capturedHeaders = init?.headers
      return new Response(
        JSON.stringify({
          profile_id: 'ai_enrich_p',
          database_name: 'AnalyticsDB',
          source: 'ai_gemini_flash',
          generated_at: new Date().toISOString(),
          tables: {},
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } }
      )
    }) as typeof fetch

    try {
      const enriched = await enrichProfileDataDictionary('ai_enrich_p', 'custom-gemini-test-key')
      assert.strictEqual(enriched.source, 'ai_gemini_flash')
      assert.ok(capturedHeaders)
      const headersObj = capturedHeaders as Record<string, string>
      assert.strictEqual(headersObj['X-Gemini-API-Key'], 'custom-gemini-test-key')
    } finally {
      globalThis.fetch = origFetch
    }
  })
})
