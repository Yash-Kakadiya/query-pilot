import assert from 'node:assert'
import test from 'node:test'
import { extractUniqueTags, filterHistoryItems } from '../src/services/historyFilter.ts'
import type { HistoryItem } from '../src/types/history.ts'

const sampleItems: HistoryItem[] = [
  {
    id: 'h1',
    timestamp: 1700000001000,
    question: 'Find high value orders',
    sql: 'SELECT order_id, total FROM orders WHERE total > 1000;',
    profile_id: 'prod_pg',
    profile_name: 'Production PostgreSQL',
    engine_type: 'postgres',
    status: 'succeeded',
    row_count: 15,
    is_favorite: true,
    tags: ['sales', 'finance'],
    notes: 'Q4 VIP customers',
  },
  {
    id: 'h2',
    timestamp: 1700000002000,
    question: 'List active users',
    sql: 'SELECT id, email FROM users WHERE is_active = 1;',
    profile_id: 'sqlite_local',
    profile_name: 'Local Dev SQLite',
    engine_type: 'sqlite',
    status: 'succeeded',
    row_count: 50,
    is_favorite: false,
    tags: ['auth', 'users'],
  },
  {
    id: 'h3',
    timestamp: 1700000003000,
    question: null,
    sql: 'SELECT COUNT(*) FROM inventory;',
    profile_id: 'prod_pg',
    profile_name: 'Production PostgreSQL',
    engine_type: 'postgres',
    status: 'failed',
    error_message: 'Table inventory does not exist',
    is_favorite: false,
    tags: ['warehouse'],
  },
]

test('History Filter - Tab and Favorites Filtering', async (t) => {
  await t.test('returns all items on recent tab', () => {
    const results = filterHistoryItems(sampleItems, { tab: 'recent' })
    assert.strictEqual(results.length, 3)
  })

  await t.test('returns only favorites on favorites tab', () => {
    const results = filterHistoryItems(sampleItems, { tab: 'favorites' })
    assert.strictEqual(results.length, 1)
    assert.strictEqual(results[0].id, 'h1')
    assert.strictEqual(results[0].is_favorite, true)
  })
})

test('History Filter - Search Matching', async (t) => {
  await t.test('searches case-insensitively by SQL query', () => {
    const results = filterHistoryItems(sampleItems, { search: 'inventory' })
    assert.strictEqual(results.length, 1)
    assert.strictEqual(results[0].id, 'h3')
  })

  await t.test('searches case-insensitively by natural language question', () => {
    const results = filterHistoryItems(sampleItems, { search: 'active users' })
    assert.strictEqual(results.length, 1)
    assert.strictEqual(results[0].id, 'h2')
  })

  await t.test('searches by profile name', () => {
    const results = filterHistoryItems(sampleItems, { search: 'PostgreSQL' })
    assert.strictEqual(results.length, 2)
  })

  await t.test('searches by tag', () => {
    const results = filterHistoryItems(sampleItems, { search: 'finance' })
    assert.strictEqual(results.length, 1)
    assert.strictEqual(results[0].id, 'h1')
  })

  await t.test('searches by user notes', () => {
    const results = filterHistoryItems(sampleItems, { search: 'VIP customers' })
    assert.strictEqual(results.length, 1)
    assert.strictEqual(results[0].id, 'h1')
  })

  await t.test('returns empty array when search query matches nothing', () => {
    const results = filterHistoryItems(sampleItems, { search: 'nonexistent keyword' })
    assert.strictEqual(results.length, 0)
  })
})

test('History Filter - Profile and Tag Filters', async (t) => {
  await t.test('filters by profile ID strictly', () => {
    const results = filterHistoryItems(sampleItems, { profileId: 'sqlite_local' })
    assert.strictEqual(results.length, 1)
    assert.strictEqual(results[0].id, 'h2')
  })

  await t.test('filters by tag strictly', () => {
    const results = filterHistoryItems(sampleItems, { tag: 'sales' })
    assert.strictEqual(results.length, 1)
    assert.strictEqual(results[0].id, 'h1')
  })

  await t.test('combines tab, profile, and search filters correctly', () => {
    const results = filterHistoryItems(sampleItems, {
      tab: 'favorites',
      profileId: 'prod_pg',
      search: 'orders',
    })
    assert.strictEqual(results.length, 1)
    assert.strictEqual(results[0].id, 'h1')
  })
})

test('History Filter - Extract Unique Tags', () => {
  const tags = extractUniqueTags(sampleItems)
  assert.deepStrictEqual(tags, ['auth', 'finance', 'sales', 'users', 'warehouse'])
})
