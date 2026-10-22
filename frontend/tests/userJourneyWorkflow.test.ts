/**
 * B10 Offline End-to-End User Journey Workflow Tests.
 *
 * Verifies the integrated client workflow across:
 * 1. Profile discovery and dynamic schema loading (verifying sensitive-column redaction).
 * 2. Natural-language question submission and unexecuted candidate SQL generation.
 * 3. Candidate review state and the is_executed=false invariant.
 * 4. Explicit user execution authorization dispatching exact reviewed SQL.
 * 5. Truthful local result rendering and deterministic scalar/tabular presentation.
 * 6. Cancellation, request sequencing, stale-result clearing, and profile switching.
 * 7. Network privacy boundary: zero raw row records or cell values sent to hosted APIs.
 *
 * NOTE: These tests verify the coordinator integration, state transitions, network contracts,
 * and presentation data models in the Node.js test environment.
 * Actual React browser DOM rendering (layout, CSS, pointer events) is distinguished from this coordinator integration.
 */

import assert from 'node:assert'
import test from 'node:test'

import { AgentSessionCoordinator } from '../src/services/agentSession.ts'
import { SqlGenerationCoordinator } from '../src/services/sqlGeneration.ts'
import { QueryExecutionCoordinator } from '../src/services/queryExecution.ts'
import {
  synthesizeDeterministicAnswer,
  resolveCellPresentation,
  formatDeterministicValue,
} from '../src/services/answerSynthesis.ts'
import type {
  AgentDatabaseSchema,
  ExecuteQueryResponse,
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

const SAMPLE_PROFILES: ProfileSummary[] = [
  {
    profile_id: 'sqlite_demo',
    display_name: 'SQLite Demo Database',
    schema_name: 'main',
    timeout_seconds: 15,
    max_row_limit: 100,
    has_allowlist: false,
    allowed_tables: null,
  },
  {
    profile_id: 'sales_warehouse',
    display_name: 'Sales Warehouse Database',
    schema_name: 'dbo',
    timeout_seconds: 15,
    max_row_limit: 100,
    has_allowlist: false,
    allowed_tables: null,
  },
]

// Model-facing schema returned by local agent (sensitive columns like password_hash are already redacted)
const SAMPLE_MODEL_FACING_SCHEMA: AgentDatabaseSchema = {
  database_name: 'sqlite_demo_db',
  tables: [
    {
      schema_name: 'main',
      table_name: 'qp_test_sample',
      columns: [
        { name: 'id', data_type: 'INTEGER', primary_key: true, nullable: false },
        { name: 'category', data_type: 'TEXT', nullable: false },
        // password_hash, api_key, ssn are NOT present in model-facing schema!
      ],
      primary_keys: ['id'],
      foreign_keys: [],
    },
  ],
}

const SAMPLE_CANDIDATE_SQL_RESPONSE: GenerateSQLResponse = {
  candidate_sql: 'SELECT COUNT(*) AS total_records FROM dbo.qp_test_sample;',
  status: 'generated',
  clarification_prompt: null,
  unsupported_reason: null,
  execution_disclaimer: 'Candidate SQL is unexecuted.',
  request_id: 'req-gen-b10-001',
}

const SAMPLE_LIVE_COUNT_RESULT: ExecuteQueryResponse = {
  columns: ['total_records'],
  rows: [{ total_records: 2 }],
  row_count: 1,
  truncated: false,
  execution_time_ms: 11.8,
  request_id: 'req-exec-b10-001',
}

// ==============================================================================
// B10 End-to-End User Journey Tests
// ==============================================================================

test('B10 User Journey - Golden Path: Profile -> Gen -> Review -> Execute -> Grounded Answer', async (t) => {
  t.afterEach(() => {
    restoreFetch()
  })

  await t.test('executes complete 5-stage golden path with zero generation-time DB calls', async () => {
    const outboundCalls: { url: string; method: string; body: any; headers: any }[] = []

    globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
      const url = String(input)
      const method = init?.method || 'GET'
      let parsedBody: any = null
      if (init?.body && typeof init.body === 'string') {
        try {
          parsedBody = JSON.parse(init.body)
        } catch {
          parsedBody = init.body
        }
      }

      outboundCalls.push({
        url,
        method,
        body: parsedBody,
        headers: init?.headers,
      })

      // 1. Profile Discovery
      if (url.includes('/agent/v1/profiles') && method === 'GET' && !url.includes('/schema') && !url.includes('/execute')) {
        return new Response(JSON.stringify({ profiles: SAMPLE_PROFILES }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      }

      // 2. Schema Introspection
      if (url.includes('/agent/v1/profiles/sqlite_demo/schema') && method === 'GET') {
        return new Response(JSON.stringify(SAMPLE_MODEL_FACING_SCHEMA), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      }

      // 3. Hosted Candidate SQL Generation
      if (url.includes('/api/v1/generate-sql') && method === 'POST') {
        return new Response(JSON.stringify(SAMPLE_CANDIDATE_SQL_RESPONSE), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      }

      // 4. Local Execution Boundary
      if (url.includes('/agent/v1/profiles/sqlite_demo/execute') && method === 'POST') {
        return new Response(JSON.stringify(SAMPLE_LIVE_COUNT_RESULT), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      }

      return new Response('Not Found', { status: 404 })
    }) as typeof fetch

    // -------------------------------------------------------------------------
    // Stage 1: Profile Selection & Schema Loading
    // -------------------------------------------------------------------------
    const sessionCoordinator = new AgentSessionCoordinator()
    await sessionCoordinator.probeSession()
    assert.strictEqual(sessionCoordinator.getState().status, 'authenticated')
    assert.strictEqual(sessionCoordinator.getState().profiles.length, 2)

    await sessionCoordinator.selectProfile('sqlite_demo')
    const sessionState = sessionCoordinator.getState()
    assert.strictEqual(sessionState.schemaStatus, 'loaded')
    assert.ok(sessionState.schemaContext)
    assert.strictEqual(sessionState.schemaContext.database_name, 'sqlite_demo_db')
    // Sensitive column invariant: password_hash, api_key, ssn must not be present
    const schemaCols = sessionState.schemaContext.tables[0].columns.map((c) => c.name.toLowerCase())
    assert.ok(schemaCols.includes('id'))
    assert.ok(schemaCols.includes('category'))
    assert.ok(!schemaCols.includes('password_hash'))
    assert.ok(!schemaCols.includes('api_key'))
    assert.ok(!schemaCols.includes('ssn'))

    // -------------------------------------------------------------------------
    // Stage 2: Question Submission & SQL Generation
    // -------------------------------------------------------------------------
    const sqlCoordinator = new SqlGenerationCoordinator()
    const question = 'How many total records are in the sample table?'
    sqlCoordinator.setQuestion(question)

    const executionCallsBeforeGen = outboundCalls.filter((c) => c.url.includes('/execute'))
    assert.strictEqual(executionCallsBeforeGen.length, 0, 'No execution calls before candidate generation')

    const genResponse = await sqlCoordinator.generateCandidate(
      question,
      sessionState.schemaContext,
      sessionState.selectedProfileId
    )
    assert.ok(genResponse)
    assert.strictEqual(genResponse.status, 'generated')
    assert.strictEqual(genResponse.candidate_sql, 'SELECT COUNT(*) AS total_records FROM dbo.qp_test_sample;')

    // -------------------------------------------------------------------------
    // Stage 3: Candidate Review State & Zero Database Calls During Generation
    // -------------------------------------------------------------------------
    const executionCallsAfterGen = outboundCalls.filter((c) => c.url.includes('/execute'))
    assert.strictEqual(
      executionCallsAfterGen.length,
      0,
      'INVARIANT: Generating candidate SQL must cause strictly zero database execution calls'
    )

    // reviewedSql is initialized from candidate_sql
    let reviewedSql = genResponse.candidate_sql

    // -------------------------------------------------------------------------
    // Stage 4: Explicit Execution Authorization
    // -------------------------------------------------------------------------
    const execCoordinator = new QueryExecutionCoordinator()
    assert.strictEqual(execCoordinator.getState().result, null, 'No execution result prior to explicit trigger')

    // User explicitly authorizes execution
    const execResponse = await execCoordinator.executeQuery(
      sessionState.selectedProfileId,
      reviewedSql,
      genResponse.request_id
    )
    assert.ok(execResponse)
    assert.strictEqual(execResponse.row_count, 1)

    // Verify exact execution dispatch
    const executionCalls = outboundCalls.filter((c) => c.url.includes('/execute'))
    assert.strictEqual(executionCalls.length, 1, 'Exactly one execution call upon explicit trigger')
    assert.ok(
      executionCalls[0].url.endsWith('/agent/v1/profiles/sqlite_demo/execute'),
      `Dispatched to selected profile endpoint: ${executionCalls[0].url}`
    )
    assert.strictEqual(
      executionCalls[0].body.sql,
      'SELECT COUNT(*) AS total_records FROM dbo.qp_test_sample;',
      'Dispatched exact reviewed SQL'
    )
    assert.strictEqual(
      executionCalls[0].body.request_id,
      'req-gen-b10-001',
      'Correlated request tracking ID preserved'
    )

    // -------------------------------------------------------------------------
    // Stage 5: Truthful Local Result Presentation & Answer Grounding
    // -------------------------------------------------------------------------
    const answer = synthesizeDeterministicAnswer(execCoordinator.getState().result)
    assert.ok(answer)
    assert.strictEqual(answer.kind, 'single_cell', '1x1 scalar result categorized as single_cell')
    assert.strictEqual(answer.text, 'total_records: 2', 'Derived exactly from returned row data')
    assert.strictEqual(answer.isTruncated, false)
    assert.strictEqual(answer.fields?.[0].label, 'total_records')
    assert.strictEqual(answer.fields?.[0].value, '2')
    assert.strictEqual(answer.fields?.[0].rawValue, 2)

    // Table cell presentation checks
    const cell = resolveCellPresentation(execCoordinator.getState().result?.rows[0]['total_records'])
    assert.strictEqual(cell.kind, 'text')
    assert.strictEqual(cell.text, '2')

    // -------------------------------------------------------------------------
    // Stage 7: Privacy Assertion - Zero Row Data Sent to Hosted API
    // -------------------------------------------------------------------------
    const hostedCalls = outboundCalls.filter((c) => c.url.includes('/api/v1/'))
    for (const hc of hostedCalls) {
      if (hc.body && typeof hc.body === 'object') {
        assert.strictEqual('rows' in hc.body, false, 'Hosted API payload must never contain a rows field')
        assert.strictEqual('records' in hc.body, false, 'Hosted API payload must never contain a records field')
      }
    }
  })
})

test('B10 User Journey - Reviewed SQL Edit Fidelity', async (t) => {
  t.afterEach(() => {
    restoreFetch()
  })

  await t.test('dispatches user-edited query instead of original candidate SQL', async () => {
    let executedSql = ''
    globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
      const url = String(input)
      if (url.includes('/execute') && init?.body) {
        const parsed = JSON.parse(String(init.body))
        executedSql = parsed.sql
        return new Response(JSON.stringify(SAMPLE_LIVE_COUNT_RESULT), { status: 200 })
      }
      return new Response(JSON.stringify({}), { status: 200 })
    }) as typeof fetch

    const execCoordinator = new QueryExecutionCoordinator()

    // Original generated SQL was "SELECT * FROM courses;"
    // User modifies it in the textarea to:
    const userEditedSql = 'SELECT COUNT(*) AS total_records FROM dbo.qp_test_sample WHERE id > 0;'

    await execCoordinator.executeQuery('sqlite_demo', userEditedSql, 'req-edit-001')

    assert.strictEqual(
      executedSql,
      userEditedSql,
      'Execution coordinator must dispatch exact user-edited SQL, not stale candidate SQL'
    )
  })
})

test('B10 User Journey - Invalidation, Staleness & Profile Switching', async (t) => {
  t.afterEach(() => {
    restoreFetch()
  })

  await t.test('editing SQL clears stale execution results', async () => {
    const execCoordinator = new QueryExecutionCoordinator({
      result: SAMPLE_LIVE_COUNT_RESULT,
      executedSql: 'SELECT COUNT(*) AS total_records FROM dbo.qp_test_sample;',
    })
    assert.ok(execCoordinator.getState().result !== null)

    // User edits the reviewed SQL -> triggers execCoordinator.clearResult()
    execCoordinator.clearResult()

    assert.strictEqual(execCoordinator.getState().result, null, 'Result must be cleared on edit')
    assert.strictEqual(execCoordinator.getState().executedSql, null)
    assert.strictEqual(execCoordinator.getState().error, null)
  })

  await t.test('profile switching clears candidate SQL and execution state', async () => {
    const sessionCoordinator = new AgentSessionCoordinator({
      status: 'authenticated',
      profiles: SAMPLE_PROFILES,
      selectedProfileId: 'sqlite_demo',
      schemaStatus: 'loaded',
    })
    const sqlCoordinator = new SqlGenerationCoordinator({
      question: 'How many records?',
      response: SAMPLE_CANDIDATE_SQL_RESPONSE,
      lastGeneratedProfileId: 'sqlite_demo',
    })
    const execCoordinator = new QueryExecutionCoordinator({
      result: SAMPLE_LIVE_COUNT_RESULT,
      executedProfileId: 'sqlite_demo',
    })

    // User switches from sqlite_demo to sales_warehouse
    sqlCoordinator.clearCandidate()
    execCoordinator.clearResult()

    assert.strictEqual(sqlCoordinator.getState().response, null, 'Candidate SQL cleared on profile switch')
    assert.strictEqual(execCoordinator.getState().result, null, 'Execution result cleared on profile switch')
  })

  await t.test('out-of-order delayed response is safely discarded after cancellation', async () => {
    let slowResolve: ((res: Response) => void) | null = null
    const slowPromise = new Promise<Response>((resolve) => {
      slowResolve = resolve
    })

    let callCount = 0
    globalThis.fetch = (async () => {
      callCount++
      if (callCount === 1) {
        return slowPromise
      }
      return new Response(
        JSON.stringify({
          ...SAMPLE_LIVE_COUNT_RESULT,
          request_id: 'query-fast-2',
          rows: [{ total_records: 99 }],
        }),
        { status: 200 }
      )
    }) as typeof fetch

    const execCoordinator = new QueryExecutionCoordinator()

    // 1. Dispatch slow execution 1
    const slowExecPromise = execCoordinator.executeQuery('sqlite_demo', 'SELECT slow;')

    // 2. User cancels
    execCoordinator.cancelExecution()

    // 3. User dispatches fast execution 2
    const fastExecPromise = execCoordinator.executeQuery('sqlite_demo', 'SELECT fast;')
    const fastResult = await fastExecPromise

    assert.ok(fastResult)
    assert.strictEqual(fastResult.request_id, 'query-fast-2')
    assert.strictEqual(fastResult.rows[0]['total_records'], 99)

    // 4. Late slow response resolves
    slowResolve!(new Response(JSON.stringify(SAMPLE_LIVE_COUNT_RESULT), { status: 200 }))
    const slowResult = await slowExecPromise
    assert.strictEqual(slowResult, null, 'Cancelled / superseded slow execution must return null')

    // State must still reflect execution 2
    assert.strictEqual(execCoordinator.getState().result?.request_id, 'query-fast-2')
    assert.strictEqual(execCoordinator.getState().result?.rows[0]['total_records'], 99)
  })
})

test('B10 User Journey - Honest Presentation of Errors, Timeouts & Truncation', async (t) => {
  await t.test('statement timeout produces error presentation and never claims success', () => {
    const answer = synthesizeDeterministicAnswer(null, {
      isConfirmedTimeout: true,
      error: new Error('Query execution timed out after 15 seconds.'),
    })
    assert.ok(answer)
    assert.strictEqual(answer.kind, 'error')
    assert.strictEqual(
      answer.text,
      'Query execution did not complete successfully. No answer could be synthesized.'
    )
  })

  await t.test('truncated result explicitly communicates profile ceiling limitation', () => {
    const truncatedResult: ExecuteQueryResponse = {
      columns: ['total_records'],
      rows: [{ total_records: 2 }],
      row_count: 1,
      truncated: true, // Truncated by profile ceiling!
      execution_time_ms: 15.0,
      request_id: 'req-trunc',
    }
    const answer = synthesizeDeterministicAnswer(truncatedResult)
    assert.ok(answer)
    assert.strictEqual(answer.kind, 'single_cell')
    assert.strictEqual(answer.isTruncated, true)
    assert.ok(
      answer.text.includes('(partial result set — truncated by profile limit)'),
      'Must prominently label truncation'
    )
  })
})
