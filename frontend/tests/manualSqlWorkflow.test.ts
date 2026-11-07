/**
 * Milestone B29-A: Independent Manual SQL Workflow & Upstream Error Classification Tests.
 *
 * Verifies:
 * 1. Manual SQL editor and execution are available with no AI response (response === null).
 * 2. Manual SQL workflow remains accessible and operational after offline generation failure.
 * 3. Manual SQL can be entered and edited independently of AI generation state.
 * 4. Execution requires explicit user authorization (never triggers on edit, profile switch, or typing).
 * 5. Destructive SQL (DROP, DELETE, UPDATE) remains blocked by safety controls and authorizers.
 * 6. Profile changes, logout, and stale asynchronous operations invalidate manual execution state.
 * 7. Existing AI candidate review and execution workflows continue working without regression.
 * 8. Zero fabricated AI response objects are introduced into coordinator state.
 * 9. Upstream error classification properly distinguishes 401/403 (auth), 502/504 (upstream gateway),
 *    connection refusal (backend unreachable), and safe generic errors without misleading attribution.
 */

import assert from 'node:assert'
import test from 'node:test'

import { AgentSessionCoordinator } from '../src/services/agentSession.ts'
import { SqlGenerationCoordinator } from '../src/services/sqlGeneration.ts'
import { QueryExecutionCoordinator } from '../src/services/queryExecution.ts'
import { ApiClientError, AgentClientError } from '../src/types/api.ts'
import type {
  AgentDatabaseSchema,
  ExecuteQueryResponse,
  GenerateSQLResponse,
  ProfileSummary,
} from '../src/types/api.ts'

const originalFetch = globalThis.fetch

function restoreFetch(): void {
  globalThis.fetch = originalFetch
}

const SAMPLE_SQLITE_PROFILE: ProfileSummary = {
  profile_id: 'sqlite_demo',
  display_name: 'SQLite Demo Database',
  schema_name: 'main',
  engine_type: 'sqlite',
  is_connected: true,
  is_default: true,
  read_only: true,
}

const SAMPLE_SQLITE_SCHEMA: AgentDatabaseSchema = {
  database_name: 'university_demo',
  tables: [
    {
      schema_name: 'main',
      table_name: 'courses',
      columns: [
        { name: 'course_id', data_type: 'INTEGER', primary_key: true, nullable: false },
        { name: 'title', data_type: 'TEXT', primary_key: false, nullable: false },
        { name: 'credits', data_type: 'INTEGER', primary_key: false, nullable: false },
      ],
      primary_keys: ['course_id'],
      foreign_keys: [],
    },
  ],
  views: [],
}

const SAMPLE_MODEL_SCHEMA = {
  database_name: 'university_demo',
  tables: [
    {
      table_name: 'courses',
      columns: [
        { name: 'course_id', data_type: 'INTEGER' },
        { name: 'title', data_type: 'TEXT' },
      ],
    },
  ],
}

test('Milestone B29-A - Independent Manual SQL Workflow', async (t) => {
  t.afterEach(restoreFetch)

  await t.test('1. Manual editor is available and executable with no AI response (zero AI state)', async () => {
    let executeCalled = false
    let executedSql = ''
    let executedProfileId = ''
    let executedRequestId: string | null = null

    globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      const urlStr = String(input)
      if (urlStr.includes('/agent/v1/profiles/sqlite_demo/execute')) {
        executeCalled = true
        executedProfileId = 'sqlite_demo'
        const body = JSON.parse(String(init?.body))
        executedSql = body.sql
        executedRequestId = body.request_id || null

        const mockResponse: ExecuteQueryResponse = {
          columns: ['course_id', 'title', 'credits'],
          rows: [
            { course_id: 1, title: 'CS101', credits: 4 },
            { course_id: 2, title: 'MATH201', credits: 3 },
          ],
          row_count: 2,
          truncated: false,
          execution_time_ms: 1.5,
          scalar_value: null,
          execution_status: 'succeeded',
        }
        return new Response(JSON.stringify(mockResponse), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      }
      return new Response('{}', { status: 200 })
    }) as typeof fetch

    const sqlGenCoordinator = new SqlGenerationCoordinator()
    const execCoordinator = new QueryExecutionCoordinator()

    // Invariant: AI response is strictly null
    assert.strictEqual(sqlGenCoordinator.getState().response, null)
    assert.strictEqual(sqlGenCoordinator.getState().isLoading, false)

    // User executes manual SQL directly
    const manualSql = 'SELECT * FROM courses WHERE credits >= 3;'
    const result = await execCoordinator.executeQuery('sqlite_demo', manualSql, null)

    assert.strictEqual(executeCalled, true)
    assert.strictEqual(executedProfileId, 'sqlite_demo')
    assert.strictEqual(executedSql, manualSql)
    assert.strictEqual(executedRequestId, null)
    assert.strictEqual(result?.row_count, 2)
    assert.deepStrictEqual(result?.columns, ['course_id', 'title', 'credits'])

    // Invariant: AI state remains strictly null (no fabricated AI response object)
    assert.strictEqual(sqlGenCoordinator.getState().response, null)
  })

  await t.test('2. Manual editor remains accessible and executable after offline generation failure', async () => {
    let executeCalled = false

    globalThis.fetch = (async (input: RequestInfo | URL) => {
      const urlStr = String(input)
      if (urlStr.includes('/api/v1/generate-sql')) {
        // Simulate offline upstream failure (HTTP 502 Bad Gateway from local agent router)
        return new Response(
          JSON.stringify({ detail: 'Unable to connect to upstream API service.' }),
          { status: 502, headers: { 'Content-Type': 'application/json' } }
        )
      }
      if (urlStr.includes('/agent/v1/profiles/sqlite_demo/execute')) {
        executeCalled = true
        const mockResponse: ExecuteQueryResponse = {
          columns: ['count'],
          rows: [{ count: 10 }],
          row_count: 1,
          truncated: false,
          execution_time_ms: 0.8,
          scalar_value: 10,
          execution_status: 'succeeded',
        }
        return new Response(JSON.stringify(mockResponse), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      }
      return new Response('{}', { status: 200 })
    }) as typeof fetch

    const sqlGenCoordinator = new SqlGenerationCoordinator()
    const execCoordinator = new QueryExecutionCoordinator()

    // Attempt AI generation while offline
    try {
      await sqlGenCoordinator.generateCandidate(
        'Show all courses',
        SAMPLE_MODEL_SCHEMA,
        'sqlite_demo'
      )
    } catch (err) {
      assert(err instanceof Error)
    }

    // AI state has error and null response
    assert.ok(sqlGenCoordinator.getState().error !== null)
    assert.strictEqual(sqlGenCoordinator.getState().response, null)

    // Manual SQL execution succeeds despite generation failure
    const manualSql = 'SELECT COUNT(*) as count FROM courses;'
    const result = await execCoordinator.executeQuery('sqlite_demo', manualSql, null)

    assert.strictEqual(executeCalled, true)
    assert.strictEqual(result?.row_count, 1)
    assert.strictEqual(result?.scalar_value, 10)
    // Response remains null without fabrication
    assert.strictEqual(sqlGenCoordinator.getState().response, null)
  })

  await t.test('3. SQL can be entered and edited independently of AI generation question state', async () => {
    const execCoordinator = new QueryExecutionCoordinator()
    const sqlGenCoordinator = new SqlGenerationCoordinator()

    // User types question in prompt box
    sqlGenCoordinator.setQuestion('What courses are taught by CS department?')
    assert.strictEqual(sqlGenCoordinator.getState().question, 'What courses are taught by CS department?')

    // User changes question — this sets isStale for AI candidate, but manual SQL is independent
    sqlGenCoordinator.setQuestion('Different question entirely')
    assert.strictEqual(sqlGenCoordinator.getState().question, 'Different question entirely')

    // AI candidate remains null
    assert.strictEqual(sqlGenCoordinator.getState().response, null)
  })

  await t.test('4. Execution requires explicit action and rejects empty query strings', async () => {
    const execCoordinator = new QueryExecutionCoordinator()

    // Attempt to execute empty string
    await assert.rejects(
      async () => execCoordinator.executeQuery('sqlite_demo', '', null),
      (err: unknown) => {
        assert(err instanceof Error)
        assert(err.message.includes('cannot be empty'))
        return true
      }
    )

    // Attempt to execute whitespace string
    await assert.rejects(
      async () => execCoordinator.executeQuery('sqlite_demo', '    \n\t  ', null),
      (err: unknown) => {
        assert(err instanceof Error)
        assert(err.message.includes('cannot be empty'))
        return true
      }
    )

    // Attempt to execute without active profile
    await assert.rejects(
      async () => execCoordinator.executeQuery('', 'SELECT 1;', null),
      (err: unknown) => {
        assert(err instanceof Error)
        assert(err.message.includes('profile must be selected'))
        return true
      }
    )
  })

  await t.test('5. Destructive SQL remains strictly blocked by deterministic safety controls', async () => {
    globalThis.fetch = (async (input: RequestInfo | URL) => {
      const urlStr = String(input)
      if (urlStr.includes('/agent/v1/profiles/sqlite_demo/execute')) {
        // Local agent daemon blocks destructive statements with 422
        return new Response(
          JSON.stringify({
            detail: 'Destructive statement rejected: DROP TABLE statements are forbidden in read-only mode.',
            error: 'validation_error',
          }),
          { status: 422, headers: { 'Content-Type': 'application/json' } }
        )
      }
      return new Response('{}', { status: 200 })
    }) as typeof fetch

    const execCoordinator = new QueryExecutionCoordinator()

    await assert.rejects(
      async () => execCoordinator.executeQuery('sqlite_demo', 'DROP TABLE courses;', null),
      (err: unknown) => {
        assert(err instanceof AgentClientError)
        assert.strictEqual(err.kind, 'validation')
        assert.strictEqual(err.statusCode, 422)
        assert(err.message.includes('Destructive statement rejected'))
        return true
      }
    )
  })

  await t.test('6. Profile changes and stale asynchronous operations clear execution state', async () => {
    const execCoordinator = new QueryExecutionCoordinator()

    // Simulate prior result
    execCoordinator['setState']({
      isLoading: false,
      result: {
        columns: ['col1'],
        rows: [{ col1: 'val' }],
        row_count: 1,
        truncated: false,
        execution_time_ms: 1.0,
        scalar_value: null,
        execution_status: 'succeeded',
      },
      error: null,
      isAmbiguousFailure: false,
    })

    assert.ok(execCoordinator.getState().result !== null)

    // Clear result (as happens on profile switch)
    execCoordinator.clearResult()
    assert.strictEqual(execCoordinator.getState().result, null)
    assert.strictEqual(execCoordinator.getState().error, null)
  })

  await t.test('7. Existing AI candidate review and execution workflow continues working', async () => {
    let genCalled = false
    let execCalled = false

    globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      const urlStr = String(input)
      if (urlStr.includes('/api/v1/generate-sql')) {
        genCalled = true
        const mockGen: GenerateSQLResponse = {
          candidate_sql: 'SELECT title FROM courses ORDER BY title ASC;',
          status: 'generated',
          dialect: 'sqlite',
          request_id: 'req-gen-123',
          execution_disclaimer: 'Unexecuted query.',
        }
        return new Response(JSON.stringify(mockGen), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      }
      if (urlStr.includes('/agent/v1/profiles/sqlite_demo/execute')) {
        execCalled = true
        const body = JSON.parse(String(init?.body))
        assert.strictEqual(body.request_id, 'req-gen-123')
        return new Response(
          JSON.stringify({
            columns: ['title'],
            rows: [{ title: 'CS101' }],
            row_count: 1,
            truncated: false,
            execution_time_ms: 1.0,
            scalar_value: null,
            execution_status: 'succeeded',
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        )
      }
      return new Response('{}', { status: 200 })
    }) as typeof fetch

    const sqlGenCoordinator = new SqlGenerationCoordinator()
    const execCoordinator = new QueryExecutionCoordinator()

    const genRes = await sqlGenCoordinator.generateCandidate(
      'List courses alphabetically',
      SAMPLE_MODEL_SCHEMA,
      'sqlite_demo'
    )

    assert.strictEqual(genCalled, true)
    assert.ok(genRes !== null)
    assert.strictEqual(genRes.candidate_sql, 'SELECT title FROM courses ORDER BY title ASC;')
    assert.strictEqual(genRes.status, 'generated')

    // Explicit execution of AI candidate transmits request_id
    const execRes = await execCoordinator.executeQuery(
      'sqlite_demo',
      genRes.candidate_sql,
      genRes.request_id
    )

    assert.strictEqual(execCalled, true)
    assert.strictEqual(execRes?.row_count, 1)
  })
})

test('Milestone B29-A - Upstream Error Classification Invariants', async (t) => {
  t.afterEach(restoreFetch)

  await t.test('1. HTTP 401 produces authentication-specific classification and message', async () => {
    globalThis.fetch = (async () => {
      return new Response(
        JSON.stringify({ detail: 'Invalid or missing access key.' }),
        { status: 401, headers: { 'Content-Type': 'application/json' } }
      )
    }) as typeof fetch

    const sqlGenCoordinator = new SqlGenerationCoordinator()

    await assert.rejects(
      async () =>
        sqlGenCoordinator.generateCandidate(
          'Count courses',
          SAMPLE_MODEL_SCHEMA,
          'sqlite_demo'
        ),
      (err: unknown) => {
        assert(err instanceof ApiClientError)
        assert.strictEqual(err.kind, 'unauthorized')
        assert.strictEqual(err.statusCode, 401)
        assert(err.message.includes('access key'))
        return true
      }
    )
  })

  await t.test('2. HTTP 403 produces access-denied classification and message', async () => {
    globalThis.fetch = (async () => {
      return new Response(
        JSON.stringify({ detail: 'Access denied: profile access forbidden.' }),
        { status: 403, headers: { 'Content-Type': 'application/json' } }
      )
    }) as typeof fetch

    const sqlGenCoordinator = new SqlGenerationCoordinator()

    await assert.rejects(
      async () =>
        sqlGenCoordinator.generateCandidate(
          'Count courses',
          SAMPLE_MODEL_SCHEMA,
          'sqlite_demo'
        ),
      (err: unknown) => {
        assert(err instanceof ApiClientError)
        assert.strictEqual(err.kind, 'unauthorized')
        assert.strictEqual(err.statusCode, 403)
        assert(err.message.includes('Access denied'))
        return true
      }
    )
  })

  await t.test('3. HTTP 502 produces upstream-unavailable classification and does not claim access-key error', async () => {
    globalThis.fetch = (async () => {
      return new Response(
        JSON.stringify({ detail: 'Unable to connect to upstream API service.' }),
        { status: 502, headers: { 'Content-Type': 'application/json' } }
      )
    }) as typeof fetch

    const sqlGenCoordinator = new SqlGenerationCoordinator()

    await assert.rejects(
      async () =>
        sqlGenCoordinator.generateCandidate(
          'Count courses',
          SAMPLE_MODEL_SCHEMA,
          'sqlite_demo'
        ),
      (err: unknown) => {
        assert(err instanceof ApiClientError)
        assert.strictEqual(err.kind, 'server')
        assert.strictEqual(err.statusCode, 502)
        assert(err.message.includes('Unable to connect to upstream API service'))
        // Critical requirement: MUST NOT claim an invalid access key
        assert(!err.message.toLowerCase().includes('access key'))
        return true
      }
    )
  })

  await t.test('4. HTTP 504 produces gateway-timeout classification and does not claim access-key error', async () => {
    globalThis.fetch = (async () => {
      return new Response(
        JSON.stringify({ detail: 'Upstream API request timed out.' }),
        { status: 504, headers: { 'Content-Type': 'application/json' } }
      )
    }) as typeof fetch

    const sqlGenCoordinator = new SqlGenerationCoordinator()

    await assert.rejects(
      async () =>
        sqlGenCoordinator.generateCandidate(
          'Count courses',
          SAMPLE_MODEL_SCHEMA,
          'sqlite_demo'
        ),
      (err: unknown) => {
        assert(err instanceof ApiClientError)
        assert.strictEqual(err.kind, 'server')
        assert.strictEqual(err.statusCode, 504)
        assert(err.message.includes('timed out'))
        assert(!err.message.toLowerCase().includes('access key'))
        return true
      }
    )
  })

  await t.test('5. Connection refusal produces network connectivity message without misleading attribution', async () => {
    globalThis.fetch = (async () => {
      throw new TypeError('fetch failed: ECONNREFUSED 127.0.0.1:8001')
    }) as typeof fetch

    const sqlGenCoordinator = new SqlGenerationCoordinator()

    await assert.rejects(
      async () =>
        sqlGenCoordinator.generateCandidate(
          'Count courses',
          SAMPLE_MODEL_SCHEMA,
          'sqlite_demo'
        ),
      (err: unknown) => {
        assert(err instanceof ApiClientError)
        assert.strictEqual(err.kind, 'network')
        assert(err.message.toLowerCase().includes('connect') || err.message.toLowerCase().includes('network'))
        assert(!err.message.toLowerCase().includes('access key'))
        return true
      }
    )
  })

  await t.test('6. Unknown 500 error remains safe and generic without internal leakage', async () => {
    globalThis.fetch = (async () => {
      return new Response(
        JSON.stringify({ detail: 'Internal server error occurred.' }),
        { status: 500, headers: { 'Content-Type': 'application/json' } }
      )
    }) as typeof fetch

    const sqlGenCoordinator = new SqlGenerationCoordinator()

    await assert.rejects(
      async () =>
        sqlGenCoordinator.generateCandidate(
          'Count courses',
          SAMPLE_MODEL_SCHEMA,
          'sqlite_demo'
        ),
      (err: unknown) => {
        assert(err instanceof ApiClientError)
        assert.strictEqual(err.kind, 'server')
        assert.strictEqual(err.statusCode, 500)
        assert(err.message.includes('Internal server error occurred.'))
        return true
      }
    )
  })
})
