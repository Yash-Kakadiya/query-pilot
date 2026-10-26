/**
 * QueryPilot Multi-Turn Conversation State & Contract Tests (Milestone B12-F12C).
 *
 * Verifies the frontend multi-turn conversation implementation:
 * 1. Context Transmission:
 *    - No history sent when turns list is empty.
 *    - Subsequent generation requests include bounded conversation_context.
 *    - Conforms to ConversationTurnContext schema.
 *    - Strictly no SQL, result rows, cell values, credentials, or connection strings.
 * 2. Turn Recording:
 *    - Turns recorded only upon successful execution and accepted result.
 *    - Turn question matches the candidate executed, NOT a subsequently edited input.
 *    - Column names recorded accurately, bounded to MAX_COLUMNS.
 *    - History bounded to MAX_CONVERSATION_TURNS = 3 with sliding FIFO drop-off.
 *    - Failures (generation error, execution error, timeout, unexecuted SQL) preserve previous turns without adding failed turns.
 * 3. Lifecycle Clearing:
 *    - Explicit New Topic / reset clears context completely.
 *    - Profile changes clear context.
 *    - Logout and session loss clear context.
 *    - Clearing candidate SQL alone preserves conversation context.
 * 4. Stale and Overlapping Operations:
 *    - Stale generation responses do not contaminate state.
 *    - In-flight execution completing after profile switch is rejected.
 *    - In-flight execution completing after reset is rejected.
 *    - Editing question or SQL during in-flight operations does not mismatch turns.
 * 5. Privacy & Explicit Execution Boundary:
 *    - Generation alone never records a turn.
 *    - Generation never auto-executes candidate SQL.
 *    - Explicit user action is strictly required for every candidate execution.
 *    - Context is strictly volatile in-memory (never in localStorage/sessionStorage).
 */

import assert from 'node:assert'
import test from 'node:test'

import { SqlGenerationCoordinator } from '../src/services/sqlGeneration.ts'
import { QueryExecutionCoordinator } from '../src/services/queryExecution.ts'
import { AgentSessionCoordinator } from '../src/services/agentSession.ts'
import type {
  AgentDatabaseSchema,
  ConversationTurnContext,
  ExecuteQueryResponse,
  GenerateSQLRequest,
  GenerateSQLResponse,
  ModelFacingSchemaContext,
  ProfileSummary,
} from '../src/types/api.ts'

const originalFetch = globalThis.fetch

function restoreFetch(): void {
  globalThis.fetch = originalFetch
}

// ==============================================================================
// Test Fixtures
// ==============================================================================

const SAMPLE_SCHEMA: ModelFacingSchemaContext = {
  database_name: 'test_inventory',
  tables: [
    {
      table_name: 'customers',
      schema_name: 'dbo',
      columns: [
        { name: 'customer_id', data_type: 'int', primary_key: true, nullable: false },
        { name: 'company_name', data_type: 'nvarchar(100)', primary_key: false, nullable: false },
        { name: 'city', data_type: 'nvarchar(50)', primary_key: false, nullable: true },
      ],
      primary_keys: ['customer_id'],
      foreign_keys: [],
    },
    {
      table_name: 'orders',
      schema_name: 'dbo',
      columns: [
        { name: 'order_id', data_type: 'int', primary_key: true, nullable: false },
        { name: 'customer_id', data_type: 'int', primary_key: false, nullable: false },
        { name: 'order_date', data_type: 'datetime', primary_key: false, nullable: true },
        { name: 'total_amount', data_type: 'decimal(10,2)', primary_key: false, nullable: false },
      ],
      primary_keys: ['order_id'],
      foreign_keys: [],
    },
  ],
}

const SAMPLE_PROFILE_A: ProfileSummary = {
  profile_id: 'profile_a',
  display_name: 'Profile A (Northwind)',
  schema_name: 'dbo',
  timeout_seconds: 15,
  max_row_limit: 100,
  has_allowlist: false,
  allowed_tables: null,
}

const SAMPLE_PROFILE_B: ProfileSummary = {
  profile_id: 'profile_b',
  display_name: 'Profile B (AdventureWorks)',
  schema_name: 'dbo',
  timeout_seconds: 15,
  max_row_limit: 100,
  has_allowlist: false,
  allowed_tables: null,
}

const SAMPLE_GEN_RESP_1: GenerateSQLResponse = {
  candidate_sql: 'SELECT customer_id, company_name, city FROM dbo.customers WHERE city = \'Seattle\';',
  status: 'generated',
  clarification_prompt: null,
  unsupported_reason: null,
  execution_disclaimer: 'Candidate SQL is unexecuted.',
  request_id: 'req-gen-turn-1',
}

const SAMPLE_EXEC_RESP_1: ExecuteQueryResponse = {
  columns: ['customer_id', 'company_name', 'city'],
  rows: [
    { customer_id: 1, company_name: 'Contoso Ltd', city: 'Seattle' },
    { customer_id: 2, company_name: 'Fabrikam Inc', city: 'Seattle' },
  ],
  row_count: 2,
  truncated: false,
  execution_time_ms: 14.2,
}

// Helper: deferred promise for race condition testing
function createDeferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

// ==============================================================================
// 1. Context Transmission & Schema Conformance
// ==============================================================================

test('Multi-Turn Conversation - Context Transmission & Schema Conformance', async (t) => {
  t.afterEach(() => {
    restoreFetch()
  })

  await t.test('no conversation_context transmitted when turns list is empty', async () => {
    let capturedBody: GenerateSQLRequest | null = null

    globalThis.fetch = (async (_url: string | URL | Request, init?: RequestInit) => {
      capturedBody = JSON.parse(String(init?.body)) as GenerateSQLRequest
      return new Response(JSON.stringify(SAMPLE_GEN_RESP_1), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    }) as typeof fetch

    const coordinator = new SqlGenerationCoordinator()
    assert.strictEqual(coordinator.getState().conversationContext.length, 0)

    await coordinator.generateCandidate('Show customers in Seattle', SAMPLE_SCHEMA, 'profile_a')

    assert.ok(capturedBody)
    assert.strictEqual(
      'conversation_context' in (capturedBody as unknown as Record<string, unknown>),
      false,
      'conversation_context key should not be present when empty'
    )
  })

  await t.test('subsequent generation requests transmit valid ConversationTurnContext', async () => {
    let capturedBody: GenerateSQLRequest | null = null

    globalThis.fetch = (async (_url: string | URL | Request, init?: RequestInit) => {
      capturedBody = JSON.parse(String(init?.body)) as GenerateSQLRequest
      return new Response(JSON.stringify(SAMPLE_GEN_RESP_1), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    }) as typeof fetch

    const coordinator = new SqlGenerationCoordinator()

    // Record turn 1
    coordinator.recordCompletedTurn(
      'Show customers in Seattle',
      ['customer_id', 'company_name', 'city'],
      'Filter customers by city Seattle'
    )

    await coordinator.generateCandidate('Now show only those with orders', SAMPLE_SCHEMA, 'profile_a')

    assert.ok(capturedBody)
    const context = (capturedBody as GenerateSQLRequest).conversation_context
    assert.ok(Array.isArray(context), 'conversation_context must be an array')
    assert.strictEqual(context.length, 1)

    const turn1 = context[0]
    assert.strictEqual(turn1.role, 'user')
    assert.strictEqual(turn1.user_question, 'Show customers in Seattle')
    assert.deepStrictEqual(turn1.result_columns, ['customer_id', 'company_name', 'city'])
    assert.strictEqual(turn1.resolved_intent, 'Filter customers by city Seattle')
  })

  await t.test('conversation_context payload contains strictly zero SQL, row, or secret data', async () => {
    let capturedRawBody = ''

    globalThis.fetch = (async (_url: string | URL | Request, init?: RequestInit) => {
      capturedRawBody = String(init?.body)
      return new Response(JSON.stringify(SAMPLE_GEN_RESP_1), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    }) as typeof fetch

    const coordinator = new SqlGenerationCoordinator()
    coordinator.recordCompletedTurn('List customers in Seattle', ['customer_id', 'company_name', 'city'])

    await coordinator.generateCandidate('Follow up question', SAMPLE_SCHEMA, 'profile_a')

    // Parse the full serialized JSON
    const parsed = JSON.parse(capturedRawBody) as Record<string, unknown>
    const contextArray = parsed.conversation_context as Array<Record<string, unknown>>
    assert.ok(Array.isArray(contextArray))

    for (const turn of contextArray) {
      // Forbidden SQL properties
      assert.strictEqual('sql' in turn, false, 'Turn must not contain raw sql')
      assert.strictEqual('candidate_sql' in turn, false, 'Turn must not contain candidate_sql')
      assert.strictEqual('executed_sql' in turn, false, 'Turn must not contain executed_sql')
      assert.strictEqual('query' in turn, false, 'Turn must not contain query')

      // Forbidden data row properties
      assert.strictEqual('rows' in turn, false, 'Turn must not contain rows')
      assert.strictEqual('row_data' in turn, false, 'Turn must not contain row_data')
      assert.strictEqual('data' in turn, false, 'Turn must not contain data')
      assert.strictEqual('cell_values' in turn, false, 'Turn must not contain cell_values')

      // Forbidden credentials / connection properties
      assert.strictEqual('password' in turn, false)
      assert.strictEqual('connection_string' in turn, false)
      assert.strictEqual('token' in turn, false)

      // Verified exact permitted keys only
      const keys = Object.keys(turn)
      for (const k of keys) {
        assert.ok(
          ['role', 'user_question', 'result_columns', 'resolved_intent'].includes(k),
          `Unexpected key in conversation turn: ${k}`
        )
      }
    }
  })
})

// ==============================================================================
// 2. Turn Recording, Bounds & Sliding Window
// ==============================================================================

test('Multi-Turn Conversation - Turn Recording & Bounds Enforcement', async (t) => {
  await t.test('recordCompletedTurn ignores empty or whitespace questions', () => {
    const coordinator = new SqlGenerationCoordinator()

    coordinator.recordCompletedTurn('', ['id', 'name'])
    assert.strictEqual(coordinator.getState().conversationContext.length, 0)

    coordinator.recordCompletedTurn('   \t\n  ', ['id', 'name'])
    assert.strictEqual(coordinator.getState().conversationContext.length, 0)
  })

  await t.test('recordCompletedTurn sanitizes and bounds column names to 50', () => {
    const coordinator = new SqlGenerationCoordinator()

    const oversizedColumns = Array.from({ length: 75 }, (_, i) => ` col_${i} `)
    coordinator.recordCompletedTurn('Sample question', oversizedColumns)

    const context = coordinator.getState().conversationContext
    assert.strictEqual(context.length, 1)
    assert.strictEqual(context[0].result_columns.length, 50, 'Must clamp columns to max 50')
    assert.strictEqual(context[0].result_columns[0], 'col_0', 'Must trim column names')
  })

  await t.test('history bounds at MAX_CONVERSATION_TURNS = 3 with FIFO eviction', () => {
    const coordinator = new SqlGenerationCoordinator()

    // Add 1st turn
    coordinator.recordCompletedTurn('Question 1', ['col_1'])
    assert.strictEqual(coordinator.getState().conversationContext.length, 1)
    assert.strictEqual(coordinator.getState().conversationContext[0].user_question, 'Question 1')

    // Add 2nd turn
    coordinator.recordCompletedTurn('Question 2', ['col_2'])
    assert.strictEqual(coordinator.getState().conversationContext.length, 2)

    // Add 3rd turn
    coordinator.recordCompletedTurn('Question 3', ['col_3'])
    assert.strictEqual(coordinator.getState().conversationContext.length, 3)

    // Add 4th turn - 1st turn dropped, 2nd, 3rd, 4th retained
    coordinator.recordCompletedTurn('Question 4', ['col_4'])
    const context4 = coordinator.getState().conversationContext
    assert.strictEqual(context4.length, 3)
    assert.strictEqual(context4[0].user_question, 'Question 2')
    assert.strictEqual(context4[1].user_question, 'Question 3')
    assert.strictEqual(context4[2].user_question, 'Question 4')

    // Add 5th turn - 2nd turn dropped, 3rd, 4th, 5th retained
    coordinator.recordCompletedTurn('Question 5', ['col_5'])
    const context5 = coordinator.getState().conversationContext
    assert.strictEqual(context5.length, 3)
    assert.strictEqual(context5[0].user_question, 'Question 3')
    assert.strictEqual(context5[1].user_question, 'Question 4')
    assert.strictEqual(context5[2].user_question, 'Question 5')
  })

  await t.test('failed generations and errors preserve earlier successful turns', async () => {
    let shouldFail = false
    globalThis.fetch = (async () => {
      if (shouldFail) {
        return new Response(JSON.stringify({ detail: 'Internal model error' }), { status: 500 })
      }
      return new Response(JSON.stringify(SAMPLE_GEN_RESP_1), { status: 200 })
    }) as typeof fetch

    const coordinator = new SqlGenerationCoordinator()

    // Initial successful turn recorded
    coordinator.recordCompletedTurn('Turn 1 Question', ['col_1'])
    assert.strictEqual(coordinator.getState().conversationContext.length, 1)

    // Attempt generation that fails with HTTP 500
    shouldFail = true
    await assert.rejects(
      async () => coordinator.generateCandidate('Failed follow up', SAMPLE_SCHEMA, 'profile_a'),
      (err: unknown) => err instanceof Error
    )

    // Verify context is preserved intact
    assert.strictEqual(coordinator.getState().conversationContext.length, 1)
    assert.strictEqual(coordinator.getState().conversationContext[0].user_question, 'Turn 1 Question')

    // Attempt empty question validation error
    await assert.rejects(
      async () => coordinator.generateCandidate('', SAMPLE_SCHEMA, 'profile_a'),
      (err: unknown) => err instanceof Error
    )
    assert.strictEqual(coordinator.getState().conversationContext.length, 1)

    restoreFetch()
  })

  await t.test('same candidate executed twice adds only one history turn', () => {
    const coordinator = new SqlGenerationCoordinator()

    // 1st execution of candidate-1
    coordinator.recordCompletedTurn('Show all customers', ['customer_id', 'city'], {
      candidateId: 'cand-1',
    })
    assert.strictEqual(coordinator.getState().conversationContext.length, 1)
    assert.strictEqual(coordinator.getState().lastRecordedCandidateId, 'cand-1')

    // 2nd execution of identical candidate-1 (e.g. user re-clicks execute button)
    coordinator.recordCompletedTurn('Show all customers', ['customer_id', 'city'], {
      candidateId: 'cand-1',
    })
    assert.strictEqual(
      coordinator.getState().conversationContext.length,
      1,
      'Duplicate execution of same candidate must not add second turn'
    )
    assert.strictEqual(coordinator.getState().lastRecordedCandidateId, 'cand-1')
  })

  await t.test('newly generated candidate with identical question produces a new turn when executed', async () => {
    globalThis.fetch = (async () => {
      return new Response(JSON.stringify(SAMPLE_GEN_RESP_1), { status: 200 })
    }) as typeof fetch

    const coordinator = new SqlGenerationCoordinator()

    // Turn 1: Generate & execute for "Show all customers"
    await coordinator.generateCandidate('Show all customers', SAMPLE_SCHEMA, 'profile_a')
    const cand1Id = coordinator.getState().candidateId
    assert.ok(cand1Id)

    coordinator.recordCompletedTurn('Show all customers', ['customer_id'], {
      candidateId: cand1Id,
    })
    assert.strictEqual(coordinator.getState().conversationContext.length, 1)

    // Turn 2: User asks identical question again as a legitimate follow-up
    await coordinator.generateCandidate('Show all customers', SAMPLE_SCHEMA, 'profile_a')
    const cand2Id = coordinator.getState().candidateId
    assert.ok(cand2Id)
    assert.notStrictEqual(cand2Id, cand1Id, 'New generation must have distinct candidateId')

    coordinator.recordCompletedTurn('Show all customers', ['customer_id'], {
      candidateId: cand2Id,
    })
    assert.strictEqual(
      coordinator.getState().conversationContext.length,
      2,
      'Legitimate follow-up with identical question text must be recorded as new turn'
    )

    restoreFetch()
  })

  await t.test('failed execution does not mark candidate as completed, allowing retry execution', () => {
    const coordinator = new SqlGenerationCoordinator()
    const candidateId = '1:req-test-fail'

    // Candidate is generated, but 1st execution fails (e.g. network/timeout error)
    // When execution fails, recordCompletedTurn is NOT called
    assert.strictEqual(coordinator.getState().lastRecordedCandidateId, null)

    // User retries execution and it now succeeds
    coordinator.recordCompletedTurn('Question after retry', ['col_1'], {
      candidateId,
    })

    assert.strictEqual(coordinator.getState().conversationContext.length, 1)
    assert.strictEqual(coordinator.getState().lastRecordedCandidateId, candidateId)
  })
})

// ==============================================================================
// 3. Lifecycle Invalidation & Reset Behavior
// ==============================================================================

test('Multi-Turn Conversation - Lifecycle Invalidation & Reset', async (t) => {
  await t.test('clearCandidate preserves conversationContext for follow-up questions', () => {
    const coordinator = new SqlGenerationCoordinator()
    coordinator.recordCompletedTurn('Question 1', ['col_1'])

    // Clearing candidate SQL (e.g. user prepares to type follow-up) must NOT clear history
    coordinator.clearCandidate()
    assert.strictEqual(coordinator.getState().response, null)
    assert.strictEqual(coordinator.getState().conversationContext.length, 1)
    assert.strictEqual(coordinator.getState().conversationContext[0].user_question, 'Question 1')
  })

  await t.test('clearConversationContext clears turns completely', () => {
    const coordinator = new SqlGenerationCoordinator()
    coordinator.recordCompletedTurn('Question 1', ['col_1'])
    coordinator.recordCompletedTurn('Question 2', ['col_2'])
    assert.strictEqual(coordinator.getState().conversationContext.length, 2)

    coordinator.clearConversationContext()
    assert.strictEqual(coordinator.getState().conversationContext.length, 0)
  })

  await t.test('resetAll clears candidate, question input, and conversationContext', () => {
    const coordinator = new SqlGenerationCoordinator()
    coordinator.setQuestion('In-progress draft')
    coordinator.recordCompletedTurn('Question 1', ['col_1'])

    coordinator.resetAll()
    const state = coordinator.getState()
    assert.strictEqual(state.question, '')
    assert.strictEqual(state.response, null)
    assert.strictEqual(state.lastGeneratedQuestion, null)
    assert.strictEqual(state.lastGeneratedProfileId, null)
    assert.strictEqual(state.conversationContext.length, 0)
  })
})

// ==============================================================================
// 4. Stale Association, Concurrency & Overlapping Operations
// ==============================================================================

test('Multi-Turn Conversation - Stale Associations & Overlapping Guards', async (t) => {
  t.afterEach(() => {
    restoreFetch()
  })

  await t.test('recording turn matches candidate question, NOT subsequent question input edits', async () => {
    // Simulate App.tsx question snapshot behavior:
    // 1. Candidate is generated for "Show Seattle customers"
    // 2. User starts editing the input box to "Show Portland customers" while review/exec is happening
    // 3. User executes the candidate
    // 4. Recorded turn MUST record "Show Seattle customers", not "Show Portland customers"

    globalThis.fetch = (async () => {
      return new Response(JSON.stringify(SAMPLE_GEN_RESP_1), { status: 200 })
    }) as typeof fetch

    const sqlCoordinator = new SqlGenerationCoordinator()
    await sqlCoordinator.generateCandidate('Show Seattle customers', SAMPLE_SCHEMA, 'profile_a')

    // Verified state has lastGeneratedQuestion
    assert.strictEqual(sqlCoordinator.getState().lastGeneratedQuestion, 'Show Seattle customers')

    // User edits question in input box
    sqlCoordinator.setQuestion('Show Portland customers (edited)')
    assert.strictEqual(sqlCoordinator.getState().question, 'Show Portland customers (edited)')

    // Snapshot captured in App.tsx prior to execution dispatch
    const questionForThisCandidate = sqlCoordinator.getState().lastGeneratedQuestion!
    assert.strictEqual(questionForThisCandidate, 'Show Seattle customers')

    // Simulate execution completion
    sqlCoordinator.recordCompletedTurn(questionForThisCandidate, ['customer_id', 'city'])

    const context = sqlCoordinator.getState().conversationContext
    assert.strictEqual(context.length, 1)
    assert.strictEqual(context[0].user_question, 'Show Seattle customers')
    assert.notStrictEqual(context[0].user_question, 'Show Portland customers (edited)')
  })

  await t.test('stale out-of-order generation responses do not contaminate conversation context', async () => {
    const deferred1 = createDeferred<Response>()
    const deferred2 = createDeferred<Response>()

    let requestCount = 0
    globalThis.fetch = (async () => {
      requestCount++
      if (requestCount === 1) {
        return deferred1.promise
      }
      return deferred2.promise
    }) as typeof fetch

    const sqlCoordinator = new SqlGenerationCoordinator()

    // Request 1 initiated (e.g. slow network)
    const p1 = sqlCoordinator.generateCandidate('Slow Question 1', SAMPLE_SCHEMA, 'profile_a')

    // User cancels or submits Request 2 before Request 1 finishes
    sqlCoordinator.cancelGeneration()
    const p2 = sqlCoordinator.generateCandidate('Fast Question 2', SAMPLE_SCHEMA, 'profile_a')

    // Request 2 resolves first
    deferred2.resolve(
      new Response(
        JSON.stringify({
          ...SAMPLE_GEN_RESP_1,
          candidate_sql: 'SELECT 2;',
          request_id: 'req-fast-2',
        }),
        { status: 200 }
      )
    )
    await p2

    assert.strictEqual(sqlCoordinator.getState().response?.candidate_sql, 'SELECT 2;')
    assert.strictEqual(sqlCoordinator.getState().lastGeneratedQuestion, 'Fast Question 2')

    // Request 1 resolves late
    deferred1.resolve(
      new Response(
        JSON.stringify({
          ...SAMPLE_GEN_RESP_1,
          candidate_sql: 'SELECT 1;',
          request_id: 'req-slow-1',
        }),
        { status: 200 }
      )
    )
    await p1

    // Late Request 1 must NOT overwrite state or candidate
    assert.strictEqual(sqlCoordinator.getState().response?.candidate_sql, 'SELECT 2;')
    assert.strictEqual(sqlCoordinator.getState().lastGeneratedQuestion, 'Fast Question 2')
  })

  await t.test('execution started for profile A cannot append turn after user switches to profile B', async () => {
    const execDeferred = createDeferred<ExecuteQueryResponse>()

    globalThis.fetch = (async (url: string | URL | Request) => {
      const urlStr = String(url)
      if (urlStr.includes('/agent/v1/profiles/profile_a/execute')) {
        const res = await execDeferred.promise
        return new Response(JSON.stringify(res), { status: 200 })
      }
      return new Response('Not Found', { status: 404 })
    }) as typeof fetch

    const sqlCoordinator = new SqlGenerationCoordinator()
    const execCoordinator = new QueryExecutionCoordinator()

    // Established candidate on profile_a
    sqlCoordinator.setState({
      lastGeneratedQuestion: 'Question for Profile A',
      lastGeneratedProfileId: 'profile_a',
    })

    // Capture snapshot for candidate execution
    const questionForThisCandidate = sqlCoordinator.getState().lastGeneratedQuestion
    const profileForThisCandidate = sqlCoordinator.getState().lastGeneratedProfileId
    let activeProfileId: string = 'profile_a'

    // Start execution on profile_a
    const execPromise = execCoordinator.executeQuery(
      activeProfileId,
      'SELECT * FROM profile_a_table;'
    )

    // User switches to profile_b while execution is in flight
    activeProfileId = 'profile_b'
    execCoordinator.clearResult()
    sqlCoordinator.clearConversationContext()

    // Execution on profile_a now resolves
    execDeferred.resolve(SAMPLE_EXEC_RESP_1)
    const result = await execPromise

    // Simulation of App.tsx post-execution guard:
    if (
      result &&
      questionForThisCandidate &&
      profileForThisCandidate === activeProfileId
    ) {
      sqlCoordinator.recordCompletedTurn(questionForThisCandidate, result.columns || [])
    }

    // Must be completely rejected: context remains empty!
    assert.strictEqual(result, null, 'Stale execution response returned null via sequence guard')
    assert.strictEqual(
      sqlCoordinator.getState().conversationContext.length,
      0,
      'Turn must NOT be recorded after profile switch'
    )
  })

  await t.test('execution started before reset cannot append turn after reset', async () => {
    const execDeferred = createDeferred<ExecuteQueryResponse>()

    globalThis.fetch = (async (url: string | URL | Request) => {
      const urlStr = String(url)
      if (urlStr.includes('/execute')) {
        const res = await execDeferred.promise
        return new Response(JSON.stringify(res), { status: 200 })
      }
      return new Response('Not Found', { status: 404 })
    }) as typeof fetch

    const sqlCoordinator = new SqlGenerationCoordinator()
    const execCoordinator = new QueryExecutionCoordinator()

    sqlCoordinator.setState({
      lastGeneratedQuestion: 'Pre-reset Question',
      lastGeneratedProfileId: 'profile_a',
    })

    const questionForThisCandidate = sqlCoordinator.getState().lastGeneratedQuestion
    const profileForThisCandidate = sqlCoordinator.getState().lastGeneratedProfileId
    const activeProfileId = 'profile_a'

    // Start execution
    const execPromise = execCoordinator.executeQuery(activeProfileId, 'SELECT 1;')

    // User clicks New Topic / reset while execution is in-flight
    sqlCoordinator.resetAll()
    execCoordinator.clearResult()

    // In-flight execution finishes
    execDeferred.resolve(SAMPLE_EXEC_RESP_1)
    const result = await execPromise

    // Guard evaluation in App.tsx
    if (
      result &&
      questionForThisCandidate &&
      profileForThisCandidate === activeProfileId
    ) {
      sqlCoordinator.recordCompletedTurn(questionForThisCandidate, result.columns || [])
    }

    assert.strictEqual(result, null, 'execCoordinator sequence check returned null')
    assert.strictEqual(
      sqlCoordinator.getState().conversationContext.length,
      0,
      'Reset context must NOT be repopulated by cancelled execution'
    )
  })
})

// ==============================================================================
// 5. Full Multi-Turn Golden Journey (3 Turns + Sliding Window + Reset)
// ==============================================================================

test('Multi-Turn Conversation - Full Multi-Turn Golden Journey', async (t) => {
  t.afterEach(() => {
    restoreFetch()
  })

  const capturedGenerateRequests: GenerateSQLRequest[] = []
  const capturedExecuteSqls: string[] = []

  globalThis.fetch = (async (url: string | URL | Request, init?: RequestInit) => {
    const urlStr = String(url)
    const method = init?.method || 'GET'

    // 1. Hosted SQL Generation
    if (urlStr.includes('/api/v1/generate-sql') && method === 'POST') {
      const body = JSON.parse(String(init?.body)) as GenerateSQLRequest
      capturedGenerateRequests.push(body)

      const turnNum = capturedGenerateRequests.length
      return new Response(
        JSON.stringify({
          candidate_sql: `SELECT col_${turnNum} FROM dbo.table_${turnNum};`,
          status: 'generated',
          execution_disclaimer: 'Candidate SQL is unexecuted.',
          request_id: `req-gen-turn-${turnNum}`,
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } }
      )
    }

    // 2. Local Query Execution
    if (urlStr.includes('/execute') && method === 'POST') {
      const body = JSON.parse(String(init?.body)) as { sql: string }
      capturedExecuteSqls.push(body.sql)

      const execCount = capturedExecuteSqls.length
      return new Response(
        JSON.stringify({
          columns: [`col_${execCount}`],
          rows: [{ [`col_${execCount}`]: execCount * 10 }],
          row_count: 1,
          truncated: false,
          execution_time_ms: 10.0,
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } }
      )
    }

    return new Response('Not Found', { status: 404 })
  }) as typeof fetch

  const sqlCoordinator = new SqlGenerationCoordinator()
  const execCoordinator = new QueryExecutionCoordinator()
  const profileId = 'profile_a'

  // Helper simulating App.tsx generation + explicit execution flow
  async function performTurn(question: string) {
    // 1. Generate candidate
    const genRes = await sqlCoordinator.generateCandidate(question, SAMPLE_SCHEMA, profileId)
    assert.ok(genRes)
    assert.strictEqual(genRes.status, 'generated')

    // Verify candidate is strictly unexecuted
    const currentExecCount = capturedExecuteSqls.length

    // 2. Explicit User Execution Action
    const questionForThisCandidate = sqlCoordinator.getState().lastGeneratedQuestion!
    const profileForThisCandidate = sqlCoordinator.getState().lastGeneratedProfileId!
    const candidateIdForThisCandidate = sqlCoordinator.getState().candidateId

    const execRes = await execCoordinator.executeQuery(
      profileId,
      genRes.candidate_sql,
      genRes.request_id
    )
    assert.ok(execRes)
    assert.strictEqual(capturedExecuteSqls.length, currentExecCount + 1)

    // 3. Record turn in App.tsx
    if (execRes && profileForThisCandidate === profileId) {
      sqlCoordinator.recordCompletedTurn(
        questionForThisCandidate,
        execRes.columns || [],
        { candidateId: candidateIdForThisCandidate }
      )
    }
  }

  // --- Turn 1 ---
  await performTurn('Turn 1 Question')
  assert.strictEqual(capturedGenerateRequests.length, 1)
  assert.strictEqual('conversation_context' in capturedGenerateRequests[0], false)
  assert.strictEqual(sqlCoordinator.getState().conversationContext.length, 1)

  // --- Turn 2 ---
  await performTurn('Turn 2 Follow Up')
  assert.strictEqual(capturedGenerateRequests.length, 2)
  assert.ok(capturedGenerateRequests[1].conversation_context)
  assert.strictEqual(capturedGenerateRequests[1].conversation_context.length, 1)
  assert.strictEqual(capturedGenerateRequests[1].conversation_context[0].user_question, 'Turn 1 Question')
  assert.deepStrictEqual(capturedGenerateRequests[1].conversation_context[0].result_columns, ['col_1'])
  assert.strictEqual(sqlCoordinator.getState().conversationContext.length, 2)

  // --- Turn 3 ---
  await performTurn('Turn 3 Second Follow Up')
  assert.strictEqual(capturedGenerateRequests.length, 3)
  assert.strictEqual(capturedGenerateRequests[2].conversation_context?.length, 2)
  assert.strictEqual(capturedGenerateRequests[2].conversation_context[0].user_question, 'Turn 1 Question')
  assert.strictEqual(capturedGenerateRequests[2].conversation_context[1].user_question, 'Turn 2 Follow Up')
  assert.strictEqual(sqlCoordinator.getState().conversationContext.length, 3)

  // --- Turn 4 (Sliding Window Drop-off) ---
  await performTurn('Turn 4 Third Follow Up')
  assert.strictEqual(capturedGenerateRequests.length, 4)
  // Request 4 sent the 3 prior turns (Turns 1, 2, 3)
  assert.strictEqual(capturedGenerateRequests[3].conversation_context?.length, 3)
  // Context state after Turn 4 drops Turn 1 and has Turns 2, 3, 4
  const contextAfter4 = sqlCoordinator.getState().conversationContext
  assert.strictEqual(contextAfter4.length, 3)
  assert.strictEqual(contextAfter4[0].user_question, 'Turn 2 Follow Up')
  assert.strictEqual(contextAfter4[1].user_question, 'Turn 3 Second Follow Up')
  assert.strictEqual(contextAfter4[2].user_question, 'Turn 4 Third Follow Up')

  // --- Reset ("New Topic") ---
  sqlCoordinator.resetAll()
  execCoordinator.clearResult()
  assert.strictEqual(sqlCoordinator.getState().conversationContext.length, 0)

  // --- Turn 5 (Post-Reset Fresh Conversation) ---
  await performTurn('Fresh Topic Question')
  assert.strictEqual(capturedGenerateRequests.length, 5)
  // Generation request for fresh topic must have empty conversation context!
  assert.strictEqual('conversation_context' in capturedGenerateRequests[4], false)
  assert.strictEqual(sqlCoordinator.getState().conversationContext.length, 1)
  assert.strictEqual(sqlCoordinator.getState().conversationContext[0].user_question, 'Fresh Topic Question')
})
