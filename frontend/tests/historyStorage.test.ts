import assert from 'node:assert'
import test from 'node:test'
import { QueryHistoryCoordinator } from '../src/services/queryHistory.ts'

test('QueryHistoryCoordinator - In-Memory State and Subscription', async (t) => {
  await t.test('records query and notifies subscribers in descending chronological order', async () => {
    const coordinator = new QueryHistoryCoordinator()
    const receivedSnapshots: number[] = []

    const unsubscribe = coordinator.subscribe((items) => {
      receivedSnapshots.push(items.length)
    })

    assert.strictEqual(receivedSnapshots[0], 0, 'Initial subscription receives empty items')

    await coordinator.recordQuery({
      sql: 'SELECT 1;',
      profile_id: 'p1',
      profile_name: 'Profile 1',
      engine_type: 'sqlite',
      status: 'succeeded',
    })

    assert.strictEqual(coordinator.getItems().length, 1)
    assert.strictEqual(coordinator.getItems()[0].sql, 'SELECT 1;')

    await coordinator.recordQuery({
      sql: 'SELECT 2;',
      profile_id: 'p2',
      profile_name: 'Profile 2',
      engine_type: 'postgres',
      status: 'succeeded',
    })

    const items = coordinator.getItems()
    assert.strictEqual(items.length, 2)
    assert.strictEqual(items[0].sql, 'SELECT 2;', 'Newest item should be at index 0')
    assert.strictEqual(items[1].sql, 'SELECT 1;')

    unsubscribe()
  })

  await t.test('toggles favorite star optimistically', async () => {
    const coordinator = new QueryHistoryCoordinator()

    const item = await coordinator.recordQuery({
      sql: 'SELECT 42;',
      profile_id: 'p1',
      profile_name: 'Profile 1',
      engine_type: 'sqlite',
      is_favorite: false,
    })

    assert.strictEqual(item.is_favorite, false)
    const isNowFav = await coordinator.toggleStar(item.id)
    assert.strictEqual(isNowFav, true)
    assert.strictEqual(coordinator.getItems()[0].is_favorite, true)

    const isNowUnfav = await coordinator.toggleStar(item.id)
    assert.strictEqual(isNowUnfav, false)
    assert.strictEqual(coordinator.getItems()[0].is_favorite, false)
  })

  await t.test('deletes a single entry correctly', async () => {
    const coordinator = new QueryHistoryCoordinator()

    const item1 = await coordinator.recordQuery({
      sql: 'SELECT 100;',
      profile_id: 'p1',
      profile_name: 'Profile 1',
      engine_type: 'sqlite',
    })
    await coordinator.recordQuery({
      sql: 'SELECT 200;',
      profile_id: 'p1',
      profile_name: 'Profile 1',
      engine_type: 'sqlite',
    })

    assert.strictEqual(coordinator.getItems().length, 2)
    const deleted = await coordinator.deleteEntry(item1.id)
    assert.strictEqual(deleted, true)

    const remaining = coordinator.getItems()
    assert.strictEqual(remaining.length, 1)
    assert.strictEqual(remaining[0].sql, 'SELECT 200;')
  })

  await t.test('clearHistory strictly preserves favorites by default', async () => {
    const coordinator = new QueryHistoryCoordinator()

    await coordinator.recordQuery({
      sql: 'SELECT regular_query;',
      profile_id: 'p1',
      profile_name: 'Profile 1',
      engine_type: 'sqlite',
      is_favorite: false,
    })

    await coordinator.recordQuery({
      sql: 'SELECT starred_query;',
      profile_id: 'p1',
      profile_name: 'Profile 1',
      engine_type: 'sqlite',
      is_favorite: true,
    })

    assert.strictEqual(coordinator.getItems().length, 2)

    // Clear without clearFavorites
    const deletedCount = await coordinator.clearHistory(false)
    assert.strictEqual(deletedCount, 1)

    const remaining = coordinator.getItems()
    assert.strictEqual(remaining.length, 1)
    assert.strictEqual(remaining[0].sql, 'SELECT starred_query;')
    assert.strictEqual(remaining[0].is_favorite, true)
  })
})
