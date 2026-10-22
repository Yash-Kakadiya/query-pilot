import assert from 'node:assert'
import test from 'node:test'
import { SqlGenerationCoordinator } from '../src/services/sqlGeneration.ts'
import type { ModelFacingSchemaContext, GenerateSQLResponse } from '../types/api.ts'

const originalFetch = globalThis.fetch

function restoreFetch(): void {
  globalThis.fetch = originalFetch
}

const SAMPLE_PROJECTED_SCHEMA: ModelFacingSchemaContext = {
  database_name: 'test_db',
  tables: [
    {
      table_name: 'courses',
      schema_name: 'dbo',
      columns: [
        { name: 'course_id', data_type: 'int', primary_key: true, nullable: false },
        { name: 'title', data_type: 'varchar', primary_key: false, nullable: false },
      ],
      primary_keys: ['course_id'],
      foreign_keys: [],
    },
  ],
}

const SAMPLE_SUCCESS_RESPONSE: GenerateSQLResponse = {
  candidate_sql: 'SELECT course_id, title FROM dbo.courses ORDER BY title;',
  status: 'generated',
  clarification_prompt: null,
  unsupported_reason: null,
  execution_disclaimer: 'Candidate SQL is unexecuted.',
  request_id: 'req-gen-123',
}

// ==============================================================================
// 1. Generation Prerequisites & State Validation
// ==============================================================================

test('SQL Generation - Prerequisites & Input Validation', async (t) => {
  t.afterEach(() => {
    restoreFetch()
  })

  await t.test('generation fails when profile or schema is missing', async () => {
    const coordinator = new SqlGenerationCoordinator()

    // Missing profileId and schema
    await assert.rejects(
      async () => coordinator.generateCandidate('Show all courses', null, null),
      (err: unknown) =>
        err instanceof Error &&
        err.message.includes('A database profile with loaded schema must be selected')
    )

    // Missing schema
    await assert.rejects(
      async () => coordinator.generateCandidate('Show all courses', null, 'profile-1'),
      (err: unknown) =>
        err instanceof Error &&
        err.message.includes('A database profile with loaded schema must be selected')
    )
  })

  await t.test('generation rejects empty or whitespace question without network dispatch', async () => {
    let fetchCalled = false
    globalThis.fetch = (async () => {
      fetchCalled = true
      return new Response(JSON.stringify(SAMPLE_SUCCESS_RESPONSE), { status: 200 })
    }) as typeof fetch

    const coordinator = new SqlGenerationCoordinator()

    await assert.rejects(
      async () =>
        coordinator.generateCandidate('', SAMPLE_PROJECTED_SCHEMA, 'profile-1'),
      (err: unknown) =>
        err instanceof Error && err.message.includes('Question cannot be empty')
    )

    await assert.rejects(
      async () =>
        coordinator.generateCandidate('   \t\n  ', SAMPLE_PROJECTED_SCHEMA, 'profile-1'),
      (err: unknown) =>
        err instanceof Error && err.message.includes('Question cannot be empty')
    )

    assert.strictEqual(fetchCalled, false, 'No network call should be dispatched for empty questions')
  })

  await t.test('generation rejects question exceeding 1000 characters without network dispatch', async () => {
    let fetchCalled = false
    globalThis.fetch = (async () => {
      fetchCalled = true
      return new Response(JSON.stringify(SAMPLE_SUCCESS_RESPONSE), { status: 200 })
    }) as typeof fetch

    const coordinator = new SqlGenerationCoordinator()
    const longQuestion = 'a'.repeat(1001)

    await assert.rejects(
      async () =>
        coordinator.generateCandidate(longQuestion, SAMPLE_PROJECTED_SCHEMA, 'profile-1'),
      (err: unknown) =>
        err instanceof Error && err.message.includes('exceeds maximum length of 1000 characters')
    )

    assert.strictEqual(fetchCalled, false)
  })
})

// ==============================================================================
// 2. Successful Generation & Candidate Review Presentation
// ==============================================================================

test('SQL Generation - Candidate Generation & Display State Updates', async (t) => {
  t.afterEach(() => {
    restoreFetch()
  })

  await t.test('successful candidate generation updates state with unexecuted SQL and metadata', async () => {
    let receivedPayload: Record<string, unknown> | null = null

    globalThis.fetch = (async (url: string | URL | Request, init?: RequestInit) => {
      const urlStr = String(url)
      if (urlStr.includes('/api/v1/generate-sql')) {
        receivedPayload = JSON.parse(String(init?.body)) as Record<string, unknown>
        return new Response(JSON.stringify(SAMPLE_SUCCESS_RESPONSE), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      }
      return new Response('Not Found', { status: 404 })
    }) as typeof fetch

    const coordinator = new SqlGenerationCoordinator()

    const res = await coordinator.generateCandidate(
      'List all available courses',
      SAMPLE_PROJECTED_SCHEMA,
      'sqlite_demo'
    )

    assert.ok(res)
    assert.strictEqual(res.status, 'generated')
    assert.strictEqual(res.candidate_sql, SAMPLE_SUCCESS_RESPONSE.candidate_sql)
    assert.strictEqual(res.execution_disclaimer, 'Candidate SQL is unexecuted.')
    assert.strictEqual(res.request_id, 'req-gen-123')

    // Verify request payload shape sent to /api/v1/generate-sql
    assert.ok(receivedPayload)
    assert.strictEqual((receivedPayload as Record<string, unknown>).question, 'List all available courses')
    assert.deepStrictEqual(
      (receivedPayload as Record<string, unknown>).schema_context,
      SAMPLE_PROJECTED_SCHEMA
    )

    // Verify state updates
    const state = coordinator.getState()
    assert.strictEqual(state.isLoading, false)
    assert.strictEqual(state.error, null)
    assert.strictEqual(state.lastGeneratedQuestion, 'List all available courses')
    assert.strictEqual(state.lastGeneratedProfileId, 'sqlite_demo')
    assert.strictEqual(state.isStale, false)
    assert.strictEqual(state.response?.candidate_sql, SAMPLE_SUCCESS_RESPONSE.candidate_sql)
  })

  await t.test('handles status="clarification_needed" response cleanly', async () => {
    const clarificationResponse: GenerateSQLResponse = {
      candidate_sql: null,
      status: 'clarification_needed',
      clarification_prompt: 'Did you mean undergraduate or graduate courses?',
      unsupported_reason: null,
      execution_disclaimer: 'Candidate SQL is unexecuted.',
      request_id: 'req-clar-456',
    }

    globalThis.fetch = (async () => {
      return new Response(JSON.stringify(clarificationResponse), { status: 200 })
    }) as typeof fetch

    const coordinator = new SqlGenerationCoordinator()

    const res = await coordinator.generateCandidate(
      'Show courses',
      SAMPLE_PROJECTED_SCHEMA,
      'sqlite_demo'
    )

    assert.ok(res)
    assert.strictEqual(res.status, 'clarification_needed')
    assert.strictEqual(res.candidate_sql, null)
    assert.strictEqual(res.clarification_prompt, 'Did you mean undergraduate or graduate courses?')
  })

  await t.test('handles status="unsupported" response cleanly', async () => {
    const unsupportedResponse: GenerateSQLResponse = {
      candidate_sql: null,
      status: 'unsupported',
      clarification_prompt: null,
      unsupported_reason: 'Natural language questions requesting data mutation (DROP, DELETE) are not supported.',
      execution_disclaimer: 'Candidate SQL is unexecuted.',
      request_id: 'req-unsup-789',
    }

    globalThis.fetch = (async () => {
      return new Response(JSON.stringify(unsupportedResponse), { status: 200 })
    }) as typeof fetch

    const coordinator = new SqlGenerationCoordinator()

    const res = await coordinator.generateCandidate(
      'Delete all courses',
      SAMPLE_PROJECTED_SCHEMA,
      'sqlite_demo'
    )

    assert.ok(res)
    assert.strictEqual(res.status, 'unsupported')
    assert.strictEqual(res.unsupported_reason?.includes('not supported'), true)
  })
})

// ==============================================================================
// 3. Concurrency, Invalidation, and Stale Response Protection
// ==============================================================================

test('SQL Generation - Concurrency, Invalidation & Stale Responses', async (t) => {
  t.afterEach(() => {
    restoreFetch()
  })

  await t.test('duplicate submit protection prevents concurrent generation dispatches', async () => {
    let dispatchCount = 0

    globalThis.fetch = (async () => {
      dispatchCount++
      await new Promise((resolve) => setTimeout(resolve, 30))
      return new Response(JSON.stringify(SAMPLE_SUCCESS_RESPONSE), { status: 200 })
    }) as typeof fetch

    const coordinator = new SqlGenerationCoordinator()

    const firstPromise = coordinator.generateCandidate(
      'Show courses',
      SAMPLE_PROJECTED_SCHEMA,
      'sqlite_demo'
    )

    // Attempt second submission concurrently
    const secondResult = await coordinator.generateCandidate(
      'Show courses',
      SAMPLE_PROJECTED_SCHEMA,
      'sqlite_demo'
    )

    assert.strictEqual(secondResult, null, 'Second concurrent submission should be rejected')
    assert.strictEqual(dispatchCount, 1)

    const firstResult = await firstPromise
    assert.ok(firstResult)
    assert.strictEqual(dispatchCount, 1)
  })

  await t.test('editing question marks existing candidate as stale', async () => {
    globalThis.fetch = (async () => {
      return new Response(JSON.stringify(SAMPLE_SUCCESS_RESPONSE), { status: 200 })
    }) as typeof fetch

    const coordinator = new SqlGenerationCoordinator()

    await coordinator.generateCandidate(
      'Show all courses',
      SAMPLE_PROJECTED_SCHEMA,
      'sqlite_demo'
    )

    assert.strictEqual(coordinator.getState().isStale, false)

    // User types in question box
    coordinator.setQuestion('Show all courses in department CS')

    assert.strictEqual(coordinator.getState().isStale, true, 'Candidate should be flagged as stale after edit')
    assert.strictEqual(coordinator.getState().question, 'Show all courses in department CS')
  })

  await t.test('stale responses from older generation requests are discarded', async () => {
    let slowResolve: (res: Response) => void
    const slowPromise = new Promise<Response>((resolve) => {
      slowResolve = resolve
    })

    let callIndex = 0
    globalThis.fetch = (async () => {
      callIndex++
      if (callIndex === 1) {
        // Slow first call
        return slowPromise
      }
      // Fast second call
      const fastResponse: GenerateSQLResponse = {
        ...SAMPLE_SUCCESS_RESPONSE,
        candidate_sql: 'SELECT * FROM dbo.fast_result;',
        request_id: 'req-fast-999',
      }
      return new Response(JSON.stringify(fastResponse), { status: 200 })
    }) as typeof fetch

    const coordinator = new SqlGenerationCoordinator()

    // 1. Dispatch slow request
    const slowGenPromise = coordinator.generateCandidate(
      'Slow query',
      SAMPLE_PROJECTED_SCHEMA,
      'sqlite_demo'
    )

    // User cancels or aborts slow request before dispatching a second request
    coordinator.cancelGeneration()

    // 2. Dispatch fast request (supersedes slow one)
    const fastGenPromise = coordinator.generateCandidate(
      'Fast query',
      SAMPLE_PROJECTED_SCHEMA,
      'sqlite_demo'
    )

    const fastResult = await fastGenPromise
    assert.ok(fastResult)
    assert.strictEqual(fastResult.candidate_sql, 'SELECT * FROM dbo.fast_result;')

    // 3. Late slow response resolves
    slowResolve!(new Response(JSON.stringify(SAMPLE_SUCCESS_RESPONSE), { status: 200 }))
    await slowGenPromise

    // 4. State must STILL be fast response
    assert.strictEqual(
      coordinator.getState().response?.candidate_sql,
      'SELECT * FROM dbo.fast_result;'
    )
    assert.strictEqual(coordinator.getState().response?.request_id, 'req-fast-999')
  })

  await t.test('cancelled older request resolving while newer request is in-flight does not reset isLoading', async () => {
    let slowResolve: (res: Response) => void
    const slowPromise = new Promise<Response>((resolve) => {
      slowResolve = resolve
    })

    let fastResolve: (res: Response) => void
    const fastPromise = new Promise<Response>((resolve) => {
      fastResolve = resolve
    })

    let callIndex = 0
    globalThis.fetch = (async () => {
      callIndex++
      if (callIndex === 1) {
        return slowPromise
      }
      return fastPromise
    }) as typeof fetch

    const coordinator = new SqlGenerationCoordinator()

    // 1. Dispatch slow request
    const slowPromiseCall = coordinator.generateCandidate(
      'Slow query 1',
      SAMPLE_PROJECTED_SCHEMA,
      'profile-1'
    )
    assert.strictEqual(coordinator.getState().isLoading, true)

    // 2. User cancels
    coordinator.cancelGeneration()
    assert.strictEqual(coordinator.getState().isLoading, false)

    // 3. User dispatches Request 2
    const fastPromiseCall = coordinator.generateCandidate(
      'Fast query 2',
      SAMPLE_PROJECTED_SCHEMA,
      'profile-1'
    )
    assert.strictEqual(coordinator.getState().isLoading, true)

    // 4. Request 1 resolves NOW, while Request 2 is still in-flight
    slowResolve!(new Response(JSON.stringify(SAMPLE_SUCCESS_RESPONSE), { status: 200 }))
    await slowPromiseCall

    // 5. State MUST STILL be loading for Request 2!
    assert.strictEqual(coordinator.getState().isLoading, true, 'Request 1 must not terminate Request 2 isLoading')
    assert.strictEqual(coordinator.getState().response, null)

    // 6. Request 2 resolves
    const response2: GenerateSQLResponse = {
      ...SAMPLE_SUCCESS_RESPONSE,
      candidate_sql: 'SELECT 2;',
      request_id: 'req-2',
    }
    fastResolve!(new Response(JSON.stringify(response2), { status: 200 }))
    const result2 = await fastPromiseCall

    assert.ok(result2)
    assert.strictEqual(coordinator.getState().isLoading, false)
    assert.strictEqual(coordinator.getState().response?.candidate_sql, 'SELECT 2;')
    assert.strictEqual(coordinator.getState().response?.request_id, 'req-2')
  })

  await t.test('editing question back to original question restores isStale to false', async () => {
    globalThis.fetch = (async () => {
      return new Response(JSON.stringify(SAMPLE_SUCCESS_RESPONSE), { status: 200 })
    }) as typeof fetch

    const coordinator = new SqlGenerationCoordinator()

    await coordinator.generateCandidate(
      'Show all courses',
      SAMPLE_PROJECTED_SCHEMA,
      'sqlite_demo'
    )

    assert.strictEqual(coordinator.getState().isStale, false)

    // Edit question
    coordinator.setQuestion('Show all courses in department CS')
    assert.strictEqual(coordinator.getState().isStale, true)

    // Edit back to original
    coordinator.setQuestion('Show all courses')
    assert.strictEqual(coordinator.getState().isStale, false)
  })

  await t.test('switching profiles clears candidate SQL immediately', () => {
    const coordinator = new SqlGenerationCoordinator({
      response: SAMPLE_SUCCESS_RESPONSE,
      lastGeneratedProfileId: 'profile-a',
      lastGeneratedQuestion: 'Q1',
    })

    assert.ok(coordinator.getState().response)

    // Simulated profile change clear
    coordinator.clearCandidate()

    assert.strictEqual(coordinator.getState().response, null)
    assert.strictEqual(coordinator.getState().lastGeneratedProfileId, null)
    assert.strictEqual(coordinator.getState().lastGeneratedQuestion, null)
    assert.strictEqual(coordinator.getState().isStale, false)
  })

  await t.test('profile change during in-flight generation aborts request and drops late response', async () => {
    let slowResolve: (res: Response) => void
    const slowPromise = new Promise<Response>((resolve) => {
      slowResolve = resolve
    })

    globalThis.fetch = (async () => {
      return slowPromise
    }) as typeof fetch

    const coordinator = new SqlGenerationCoordinator()

    const inFlightPromise = coordinator.generateCandidate(
      'Query on profile A',
      SAMPLE_PROJECTED_SCHEMA,
      'profile-a'
    )
    assert.strictEqual(coordinator.getState().isLoading, true)

    // Profile switch occurs while request is in-flight
    coordinator.clearCandidate()

    assert.strictEqual(coordinator.getState().isLoading, false)
    assert.strictEqual(coordinator.getState().response, null)
    assert.strictEqual(coordinator.getState().lastGeneratedProfileId, null)

    // Late response arrives from profile A
    slowResolve!(new Response(JSON.stringify(SAMPLE_SUCCESS_RESPONSE), { status: 200 }))
    const result = await inFlightPromise

    assert.strictEqual(result, null)
    assert.strictEqual(coordinator.getState().response, null)
    assert.strictEqual(coordinator.getState().isLoading, false)
  })

  await t.test('cancellation aborts in-flight request and resets loading state', () => {
    const coordinator = new SqlGenerationCoordinator({ isLoading: true })

    coordinator.cancelGeneration()

    assert.strictEqual(coordinator.getState().isLoading, false)
  })
})

// ==============================================================================
// 4. Hosted API Failures, Security Safeguards & Non-Persistence
// ==============================================================================

test('SQL Generation - Error Handling & Security Boundaries', async (t) => {
  t.afterEach(() => {
    restoreFetch()
  })

  await t.test('hosted API 500 error records error in state and supports retry', async () => {
    let callCount = 0

    globalThis.fetch = (async () => {
      callCount++
      if (callCount === 1) {
        return new Response(
          JSON.stringify({ detail: 'LLM service timeout during SQL generation.' }),
          { status: 500, headers: { 'Content-Type': 'application/json' } }
        )
      }
      return new Response(JSON.stringify(SAMPLE_SUCCESS_RESPONSE), { status: 200 })
    }) as typeof fetch

    const coordinator = new SqlGenerationCoordinator()

    // First attempt fails
    await assert.rejects(
      async () =>
        coordinator.generateCandidate(
          'Show courses',
          SAMPLE_PROJECTED_SCHEMA,
          'sqlite_demo'
        ),
      (err: unknown) =>
        err instanceof Error && err.message.includes('LLM service timeout')
    )

    const errorState = coordinator.getState()
    assert.strictEqual(errorState.isLoading, false)
    assert.ok(errorState.error)
    assert.strictEqual(errorState.response, null)

    // Retry succeeds
    const retryRes = await coordinator.generateCandidate(
      'Show courses',
      SAMPLE_PROJECTED_SCHEMA,
      'sqlite_demo'
    )

    assert.ok(retryRes)
    assert.strictEqual(retryRes.status, 'generated')
    assert.strictEqual(coordinator.getState().error, null)
    assert.strictEqual(callCount, 2)
  })

  await t.test('verifies candidate SQL is never automatically executed', async () => {
    let executedSql = false

    globalThis.fetch = (async (url: string | URL | Request) => {
      const urlStr = String(url)
      if (urlStr.includes('/execute')) {
        executedSql = true
        return new Response('{}', { status: 200 })
      }
      if (urlStr.includes('/generate-sql')) {
        return new Response(JSON.stringify(SAMPLE_SUCCESS_RESPONSE), { status: 200 })
      }
      return new Response('Not Found', { status: 404 })
    }) as typeof fetch

    const coordinator = new SqlGenerationCoordinator()

    await coordinator.generateCandidate(
      'Show all courses',
      SAMPLE_PROJECTED_SCHEMA,
      'sqlite_demo'
    )

    assert.strictEqual(executedSql, false, 'Candidate SQL MUST NOT be executed')
    assert.strictEqual(coordinator.getState().response?.execution_disclaimer, 'Candidate SQL is unexecuted.')
  })

  await t.test('verifies candidate SQL, questions, and schemas are never stored in localStorage or sessionStorage', async () => {
    globalThis.fetch = (async () => {
      return new Response(JSON.stringify(SAMPLE_SUCCESS_RESPONSE), { status: 200 })
    }) as typeof fetch

    const coordinator = new SqlGenerationCoordinator()

    await coordinator.generateCandidate(
      'Confidential salary question',
      SAMPLE_PROJECTED_SCHEMA,
      'sqlite_demo'
    )

    if (typeof localStorage !== 'undefined') {
      assert.strictEqual(localStorage.getItem('candidate_sql'), null)
      assert.strictEqual(localStorage.getItem('question'), null)
      assert.strictEqual(localStorage.getItem('generated_sql'), null)
    }
    if (typeof sessionStorage !== 'undefined') {
      assert.strictEqual(sessionStorage.getItem('candidate_sql'), null)
      assert.strictEqual(sessionStorage.getItem('question'), null)
      assert.strictEqual(sessionStorage.getItem('generated_sql'), null)
    }
  })
})
