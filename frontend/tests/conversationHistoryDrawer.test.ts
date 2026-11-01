/**
 * QueryPilot Multi-Turn Conversation History Drawer & State Tests (Milestone B22-B).
 *
 * Verifies:
 * 1. Safe Display Model:
 *    - Stores only safe metadata (turnId, turnNumber, question, resolvedIntent, synthesizedSummary, resultColumns, rowCount, executionStatus, timestamp).
 *    - Strictly NO SQL query text.
 *    - Strictly NO cell data or row contents.
 *    - Strictly NO credentials, tokens, or connection strings.
 * 2. Bounded Capacity (Max 3 Turns):
 *    - Maintains maximum 3 turns in chronological order.
 *    - Sliding FIFO drop-off when new turns are recorded beyond 3.
 * 3. Active Turn and State Visibility:
 *    - Turns are chronologically numbered.
 * 4. Turn Restoration & Branching:
 *    - restoreTurn(index) restores question to editor.
 *    - Truncates conversationContext and conversationHistory so subsequent turns do not leak future context.
 * 5. Lifecycle Resets:
 *    - clearConversationContext() empties history.
 *    - resetAll() empties history.
 */

import assert from 'node:assert'
import test from 'node:test'

import { SqlGenerationCoordinator } from '../src/services/sqlGeneration.ts'

test('Conversation History — Safe display model enforces privacy boundaries', () => {
  const coordinator = new SqlGenerationCoordinator()

  const sensitiveSql = 'SELECT * FROM secrets WHERE token = "sk-secret-12345"'
  const sensitiveRows = [['admin', 'password_hash_xyz', 'super_secret_cell_data']]

  coordinator.recordCompletedTurn(
    'List administrative accounts',
    ['username', 'password_hash', 'extra'],
    {
      resolvedIntent: 'Find admin credentials',
      synthesizedSummary: 'Found 1 admin account.',
      rowCount: sensitiveRows.length,
      executionStatus: 'succeeded',
    },
    'cand-test-1'
  )

  const state = coordinator.getState()
  assert.strictEqual(state.conversationHistory.length, 1)

  const turn = state.conversationHistory[0]
  assert.strictEqual(turn.turnNumber, 1)
  assert.strictEqual(turn.question, 'List administrative accounts')
  assert.strictEqual(turn.resolvedIntent, 'Find admin credentials')
  assert.strictEqual(turn.synthesizedSummary, 'Found 1 admin account.')
  assert.deepStrictEqual(turn.resultColumns, ['username', 'password_hash', 'extra'])
  assert.strictEqual(turn.rowCount, 1)
  assert.strictEqual(turn.executionStatus, 'succeeded')
  assert.ok(typeof turn.timestamp === 'string')

  // Inspect the raw object keys of ConversationHistoryTurn
  const turnKeys = Object.keys(turn)
  assert.ok(!turnKeys.includes('generatedSql'))
  assert.ok(!turnKeys.includes('sql'))
  assert.ok(!turnKeys.includes('rows'))
  assert.ok(!turnKeys.includes('data'))

  // Verify full stringified JSON contains zero sensitive cell data or SQL
  const serialized = JSON.stringify(turn)
  assert.ok(!serialized.includes(sensitiveSql), 'History turn must not contain sensitive SQL')
  assert.ok(!serialized.includes('password_hash_xyz'), 'History turn must not contain sensitive cell values')
  assert.ok(!serialized.includes('super_secret_cell_data'), 'History turn must not contain raw row values')
})

test('Conversation History — Enforces maximum 3 turns with chronological FIFO drop-off', () => {
  const coordinator = new SqlGenerationCoordinator()

  for (let i = 1; i <= 5; i++) {
    coordinator.recordCompletedTurn(
      `Question ${i}`,
      ['id'],
      {
        resolvedIntent: `Intent ${i}`,
        synthesizedSummary: `Summary ${i}`,
        rowCount: i,
        executionStatus: 'succeeded',
      },
      `cand-${i}`
    )
  }

  const history = coordinator.getState().conversationHistory
  assert.strictEqual(history.length, 3, 'Must maintain at most 3 conversation turns')

  // FIFO drop-off: Questions 1 and 2 dropped, 3, 4, 5 retained
  assert.strictEqual(history[0].question, 'Question 3')
  assert.strictEqual(history[0].turnNumber, 3)
  assert.strictEqual(history[1].question, 'Question 4')
  assert.strictEqual(history[1].turnNumber, 4)
  assert.strictEqual(history[2].question, 'Question 5')
  assert.strictEqual(history[2].turnNumber, 5)
})

test('Conversation History — restoreTurn() branches conversation context and history correctly', () => {
  const coordinator = new SqlGenerationCoordinator()

  // Add 3 turns
  for (let i = 1; i <= 3; i++) {
    coordinator.recordCompletedTurn(
      `Question ${i}`,
      ['col'],
      {
        resolvedIntent: `Intent ${i}`,
        synthesizedSummary: `Summary ${i}`,
        rowCount: 1,
        executionStatus: 'succeeded',
      },
      `cand-${i}`
    )
  }

  assert.strictEqual(coordinator.getState().conversationHistory.length, 3)
  assert.strictEqual(coordinator.getState().conversationContext.length, 3)

  // Restore turn at index 1 (Question 2)
  coordinator.restoreTurn(1)

  const stateAfterRestore = coordinator.getState()
  // Active question input is restored
  assert.strictEqual(stateAfterRestore.question, 'Question 2')
  // Candidate response is cleared
  assert.strictEqual(stateAfterRestore.response, null)

  // conversationHistory must be truncated to turns before and including index 1 (i.e. Question 1 and 2 remain, Question 3 pruned)
  assert.strictEqual(stateAfterRestore.conversationHistory.length, 2)
  assert.strictEqual(stateAfterRestore.conversationHistory[0].question, 'Question 1')
  assert.strictEqual(stateAfterRestore.conversationHistory[1].question, 'Question 2')

  // conversationContext must be truncated to strictly prior context (only Question 1 context remains)
  assert.strictEqual(stateAfterRestore.conversationContext.length, 1)
  assert.strictEqual(stateAfterRestore.conversationContext[0].user_question, 'Question 1')
})

test('Conversation History — restoreTurn(0) at the first turn clears prior context and prunes subsequent history', () => {
  const coordinator = new SqlGenerationCoordinator()

  for (let i = 1; i <= 2; i++) {
    coordinator.recordCompletedTurn(
      `Question ${i}`,
      ['col'],
      {
        resolvedIntent: `Intent ${i}`,
        synthesizedSummary: `Summary ${i}`,
        rowCount: 1,
        executionStatus: 'succeeded',
      },
      `cand-${i}`
    )
  }

  // Restore first turn (index 0)
  coordinator.restoreTurn(0)

  const state = coordinator.getState()
  assert.strictEqual(state.question, 'Question 1')
  assert.strictEqual(state.response, null)
  assert.strictEqual(state.conversationHistory.length, 1)
  assert.strictEqual(state.conversationContext.length, 0)
})

test('Conversation History — clearConversationContext() and resetAll() completely purge history', () => {
  const coordinator = new SqlGenerationCoordinator()

  coordinator.recordCompletedTurn(
    'Test query',
    ['col'],
    {
      resolvedIntent: 'Test intent',
      synthesizedSummary: 'Summary',
      rowCount: 1,
      executionStatus: 'succeeded',
    },
    'cand-reset-1'
  )

  assert.strictEqual(coordinator.getState().conversationHistory.length, 1)

  coordinator.clearConversationContext()
  assert.strictEqual(coordinator.getState().conversationHistory.length, 0)
  assert.strictEqual(coordinator.getState().conversationContext.length, 0)

  // Re-add turn and test resetAll()
  coordinator.recordCompletedTurn(
    'Test query 2',
    ['col'],
    {
      resolvedIntent: 'Test intent 2',
      synthesizedSummary: 'Summary 2',
      rowCount: 1,
      executionStatus: 'succeeded',
    },
    'cand-reset-2'
  )
  assert.strictEqual(coordinator.getState().conversationHistory.length, 1)

  coordinator.resetAll()
  assert.strictEqual(coordinator.getState().conversationHistory.length, 0)
  assert.strictEqual(coordinator.getState().conversationContext.length, 0)
  assert.strictEqual(coordinator.getState().question, '')
})

test('Conversation History — Subsequent generation transmits only context valid at restored branch point', async () => {
  const originalFetch = globalThis.fetch
  let capturedContext: unknown = null

  globalThis.fetch = async (_url: string | URL | Request, init?: RequestInit) => {
    const body = JSON.parse(String(init?.body))
    capturedContext = body.conversation_context
    return new Response(
      JSON.stringify({
        candidate_sql: 'SELECT 1;',
        status: 'generated',
        clarification_prompt: null,
        unsupported_reason: null,
        execution_disclaimer: 'Unexecuted.',
        request_id: 'req-branch-1',
      }),
      { status: 200, headers: { 'Content-Type': 'application/json' } }
    )
  }

  try {
    const coordinator = new SqlGenerationCoordinator()

    // Record turns 1, 2, 3
    for (let i = 1; i <= 3; i++) {
      coordinator.recordCompletedTurn(
        `Question ${i}`,
        [`col_${i}`],
        {
          resolvedIntent: `Intent ${i}`,
          synthesizedSummary: `Summary ${i}`,
          rowCount: i,
          executionStatus: 'succeeded',
        },
        `cand-${i}`
      )
    }

    assert.strictEqual(coordinator.getState().conversationContext.length, 3)

    // Restore Turn 2 (index 1)
    coordinator.restoreTurn(1)
    assert.strictEqual(coordinator.getState().conversationContext.length, 1)

    const sampleSchema = {
      database_name: 'test_db',
      tables: [{ table_name: 't1', schema_name: 'dbo', columns: [{ name: 'col_1', data_type: 'int', primary_key: true, nullable: false }], primary_keys: ['col_1'] }]
    }

    // Subsequent generation from restored turn
    await coordinator.generateCandidate('Question 2 modified', sampleSchema, 'profile_1')

    assert.ok(Array.isArray(capturedContext))
    assert.strictEqual(capturedContext.length, 1)
    assert.strictEqual(capturedContext[0].user_question, 'Question 1')
    // Must NOT contain Question 2 or Question 3
    const contextJson = JSON.stringify(capturedContext)
    assert.ok(!contextJson.includes('Question 2'))
    assert.ok(!contextJson.includes('Question 3'))
  } finally {
    globalThis.fetch = originalFetch
  }
})

test('Conversation History — Failed generation or execution preserves prior history without creating false entries', async () => {
  const originalFetch = globalThis.fetch

  // Simulate failed generation (HTTP 500)
  globalThis.fetch = async () => {
    return new Response(JSON.stringify({ detail: 'Internal model error' }), {
      status: 500,
      headers: { 'Content-Type': 'application/json' },
    })
  }

  try {
    const coordinator = new SqlGenerationCoordinator()

    // Successful Turn 1
    coordinator.recordCompletedTurn(
      'Turn 1 Question',
      ['id'],
      {
        resolvedIntent: 'Intent 1',
        synthesizedSummary: 'Summary 1',
        rowCount: 1,
        executionStatus: 'succeeded',
      },
      'cand-success-1'
    )

    assert.strictEqual(coordinator.getState().conversationHistory.length, 1)

    const sampleSchema = {
      database_name: 'test_db',
      tables: [{ table_name: 't1', schema_name: 'dbo', columns: [{ name: 'id', data_type: 'int', primary_key: true, nullable: false }], primary_keys: ['id'] }]
    }

    // Attempt generation that fails
    try {
      await coordinator.generateCandidate('Turn 2 Question', sampleSchema, 'profile_1')
    } catch {
      // Expected failure
    }

    // History must NOT record a failed turn
    assert.strictEqual(coordinator.getState().conversationHistory.length, 1)
    assert.strictEqual(coordinator.getState().conversationHistory[0].question, 'Turn 1 Question')
  } finally {
    globalThis.fetch = originalFetch
  }
})
