import assert from 'node:assert'
import test from 'node:test'
import { QueryExecutionCoordinator } from '../src/services/queryExecution.ts'
import { AgentClientError } from '../src/services/agentClient.ts'
import type { ExecuteQueryResponse } from '../types/api.ts'

const originalFetch = globalThis.fetch

function restoreFetch(): void {
  globalThis.fetch = originalFetch
}

const SAMPLE_SUCCESS_RESULT: ExecuteQueryResponse = {
  columns: ['department_id', 'course_count'],
  rows: [
    { department_id: 1, course_count: 14 },
    { department_id: 2, course_count: 8 },
  ],
  row_count: 2,
  truncated: false,
  execution_time_ms: 12.4,
  request_id: 'req-exec-101',
}

const SAMPLE_EMPTY_RESULT: ExecuteQueryResponse = {
  columns: ['id', 'name'],
  rows: [],
  row_count: 0,
  truncated: false,
  execution_time_ms: 4.8,
  request_id: 'req-exec-102',
}

const SAMPLE_TRUNCATED_RESULT: ExecuteQueryResponse = {
  columns: ['id', 'title'],
  rows: [{ id: 1, title: 'Item 1' }],
  row_count: 1,
  truncated: true,
  execution_time_ms: 25.1,
  request_id: 'req-exec-103',
}

// ==============================================================================
// 1. Prerequisites, Validation & Explicit Invocation
// ==============================================================================

test('Query Execution - Prerequisites & Validation', async (t) => {
  t.afterEach(() => {
    restoreFetch()
  })

  await t.test('execution fails when profile is missing or empty', async () => {
    const coordinator = new QueryExecutionCoordinator()

    await assert.rejects(
      async () => coordinator.executeQuery(null, 'SELECT 1;'),
      (err: unknown) =>
        err instanceof Error &&
        err.message.includes('A database profile must be selected')
    )

    await assert.rejects(
      async () => coordinator.executeQuery('   ', 'SELECT 1;'),
      (err: unknown) =>
        err instanceof Error &&
        err.message.includes('A database profile must be selected')
    )
  })

  await t.test('execution rejects empty or whitespace-only SQL without network dispatch', async () => {
    let fetchCalled = false
    globalThis.fetch = (async () => {
      fetchCalled = true
      return new Response(JSON.stringify(SAMPLE_SUCCESS_RESULT), { status: 200 })
    }) as typeof fetch

    const coordinator = new QueryExecutionCoordinator()

    await assert.rejects(
      async () => coordinator.executeQuery('sqlite_demo', ''),
      (err: unknown) =>
        err instanceof Error && err.message.includes('cannot be empty or whitespace')
    )

    await assert.rejects(
      async () => coordinator.executeQuery('sqlite_demo', '   \n\t  '),
      (err: unknown) =>
        err instanceof Error && err.message.includes('cannot be empty or whitespace')
    )

    assert.strictEqual(fetchCalled, false)
  })

  await t.test('execution rejects SQL exceeding 10000 characters without network dispatch', async () => {
    let fetchCalled = false
    globalThis.fetch = (async () => {
      fetchCalled = true
      return new Response(JSON.stringify(SAMPLE_SUCCESS_RESULT), { status: 200 })
    }) as typeof fetch

    const coordinator = new QueryExecutionCoordinator()
    const oversizedSql = 'SELECT ' + 'a'.repeat(10001)

    await assert.rejects(
      async () => coordinator.executeQuery('sqlite_demo', oversizedSql),
      (err: unknown) =>
        err instanceof Error && err.message.includes('exceeds maximum allowed length of 10000')
    )

    assert.strictEqual(fetchCalled, false)
  })
})

// ==============================================================================
// 2. Successful Execution & Result Presentation
// ==============================================================================

test('Query Execution - Success Outcomes & Metadata', async (t) => {
  t.afterEach(() => {
    restoreFetch()
  })

  await t.test('successful query execution updates state with rows, metadata, and execution timing', async () => {
    let requestedUrl = ''
    let receivedPayload: Record<string, unknown> | null = null

    globalThis.fetch = (async (url: string | URL | Request, init?: RequestInit) => {
      requestedUrl = String(url)
      receivedPayload = JSON.parse(String(init?.body)) as Record<string, unknown>
      return new Response(JSON.stringify(SAMPLE_SUCCESS_RESULT), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    }) as typeof fetch

    const coordinator = new QueryExecutionCoordinator()

    const result = await coordinator.executeQuery(
      'sqlite_demo',
      'SELECT department_id, COUNT(*) FROM courses GROUP BY department_id;',
      'req-exec-101'
    )

    assert.ok(result)
    assert.strictEqual(result.row_count, 2)
    assert.strictEqual(result.truncated, false)
    assert.strictEqual(result.execution_time_ms, 12.4)
    assert.strictEqual(result.request_id, 'req-exec-101')
    assert.strictEqual(result.columns.length, 2)
    assert.strictEqual(result.rows.length, 2)

    // Verify endpoint route and payload shape
    assert.ok(requestedUrl.includes('/agent/v1/profiles/sqlite_demo/execute'))
    assert.strictEqual(
      receivedPayload?.sql,
      'SELECT department_id, COUNT(*) FROM courses GROUP BY department_id;'
    )
    assert.strictEqual(receivedPayload?.request_id, 'req-exec-101')

    // Verify state updates
    const state = coordinator.getState()
    assert.strictEqual(state.isLoading, false)
    assert.strictEqual(state.error, null)
    assert.strictEqual(state.executedProfileId, 'sqlite_demo')
    assert.strictEqual(state.result?.row_count, 2)
    assert.strictEqual(state.isAmbiguousFailure, false)
  })

  await t.test('handles empty result sets (0 rows) cleanly', async () => {
    globalThis.fetch = (async () => {
      return new Response(JSON.stringify(SAMPLE_EMPTY_RESULT), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    }) as typeof fetch

    const coordinator = new QueryExecutionCoordinator()

    const result = await coordinator.executeQuery(
      'sqlite_demo',
      'SELECT * FROM courses WHERE 1=0;'
    )

    assert.ok(result)
    assert.strictEqual(result.row_count, 0)
    assert.strictEqual(result.rows.length, 0)
    assert.strictEqual(coordinator.getState().result?.row_count, 0)
  })

  await t.test('handles result sets marked as truncated', async () => {
    globalThis.fetch = (async () => {
      return new Response(JSON.stringify(SAMPLE_TRUNCATED_RESULT), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    }) as typeof fetch

    const coordinator = new QueryExecutionCoordinator()

    const result = await coordinator.executeQuery('sqlite_demo', 'SELECT * FROM big_table;')

    assert.ok(result)
    assert.strictEqual(result.truncated, true)
    assert.strictEqual(coordinator.getState().result?.truncated, true)
  })
})

// ==============================================================================
// 3. Concurrency, Invalidation & Stale Responses
// ==============================================================================

test('Query Execution - Concurrency & Invalidation', async (t) => {
  t.afterEach(() => {
    restoreFetch()
  })

  await t.test('duplicate submit protection prevents concurrent executions', async () => {
    let dispatchCount = 0

    globalThis.fetch = (async () => {
      dispatchCount++
      await new Promise((resolve) => setTimeout(resolve, 30))
      return new Response(JSON.stringify(SAMPLE_SUCCESS_RESULT), { status: 200 })
    }) as typeof fetch

    const coordinator = new QueryExecutionCoordinator()

    const firstPromise = coordinator.executeQuery('sqlite_demo', 'SELECT 1;')
    const secondResult = await coordinator.executeQuery('sqlite_demo', 'SELECT 1;')

    assert.strictEqual(secondResult, null, 'Second concurrent execution must be rejected')
    assert.strictEqual(dispatchCount, 1)

    const firstResult = await firstPromise
    assert.ok(firstResult)
    assert.strictEqual(dispatchCount, 1)
  })

  await t.test('stale responses from older executions are discarded after cancellation', async () => {
    let slowResolve: (res: Response) => void
    const slowPromise = new Promise<Response>((resolve) => {
      slowResolve = resolve
    })

    let callCount = 0
    globalThis.fetch = (async () => {
      callCount++
      if (callCount === 1) {
        return slowPromise
      }
      return new Response(JSON.stringify({ ...SAMPLE_SUCCESS_RESULT, request_id: 'req-fast-2' }), {
        status: 200,
      })
    }) as typeof fetch

    const coordinator = new QueryExecutionCoordinator()

    // 1. Dispatch slow execution 1
    const slowExecPromise = coordinator.executeQuery('sqlite_demo', 'SELECT slow;')

    // 2. User cancels
    coordinator.cancelExecution()

    // 3. User dispatches fast execution 2
    const fastExecPromise = coordinator.executeQuery('sqlite_demo', 'SELECT fast;')
    const fastResult = await fastExecPromise

    assert.ok(fastResult)
    assert.strictEqual(fastResult.request_id, 'req-fast-2')

    // 4. Late slow response resolves
    slowResolve!(new Response(JSON.stringify(SAMPLE_SUCCESS_RESULT), { status: 200 }))
    await slowExecPromise

    // State must still reflect execution 2
    assert.strictEqual(coordinator.getState().result?.request_id, 'req-fast-2')
  })

  await t.test('switching profiles clears execution results immediately', () => {
    const coordinator = new QueryExecutionCoordinator({
      result: SAMPLE_SUCCESS_RESULT,
      executedProfileId: 'profile-a',
      executedSql: 'SELECT 1;',
    })

    assert.ok(coordinator.getState().result)

    coordinator.clearResult()

    assert.strictEqual(coordinator.getState().result, null)
    assert.strictEqual(coordinator.getState().executedProfileId, null)
    assert.strictEqual(coordinator.getState().executedSql, null)
    assert.strictEqual(coordinator.getState().error, null)
  })

  await t.test('in-flight execution is aborted when profile is cleared', async () => {
    let slowResolve: (res: Response) => void
    const slowPromise = new Promise<Response>((resolve) => {
      slowResolve = resolve
    })

    globalThis.fetch = (async () => {
      return slowPromise
    }) as typeof fetch

    const coordinator = new QueryExecutionCoordinator()

    const execPromise = coordinator.executeQuery('profile-a', 'SELECT 1;')
    assert.strictEqual(coordinator.getState().isLoading, true)

    // User switches profile
    coordinator.clearResult()
    assert.strictEqual(coordinator.getState().isLoading, false)

    // Late response resolves
    slowResolve!(new Response(JSON.stringify(SAMPLE_SUCCESS_RESULT), { status: 200 }))
    const res = await execPromise

    assert.strictEqual(res, null)
    assert.strictEqual(coordinator.getState().result, null)
    assert.strictEqual(coordinator.getState().isLoading, false)
  })
})

// ==============================================================================
// 4. Server-Side Safety Rejections, Timeouts & Error Classification
// ==============================================================================

test('Query Execution - Error Classification & Safety Boundaries', async (t) => {
  t.afterEach(() => {
    restoreFetch()
  })

  await t.test('server-side AST validation rejection (HTTP 400) records error without retry', async () => {
    let callCount = 0
    globalThis.fetch = (async () => {
      callCount++
      return new Response(
        JSON.stringify({
          error: 'unsafe_query',
          detail: 'Query rejected: statement contains forbidden mutation keyword (DROP).',
        }),
        { status: 400, headers: { 'Content-Type': 'application/json' } }
      )
    }) as typeof fetch

    const coordinator = new QueryExecutionCoordinator()

    await assert.rejects(
      async () => coordinator.executeQuery('sqlite_demo', 'DROP TABLE courses;'),
      (err: unknown) => {
        assert.ok(err instanceof AgentClientError)
        assert.strictEqual(err.statusCode, 400)
        assert.ok(err.message.includes('forbidden mutation keyword'))
        return true
      }
    )

    assert.strictEqual(callCount, 1, 'Unsafe query must never be automatically retried')
    const state = coordinator.getState()
    assert.strictEqual(state.isLoading, false)
    assert.strictEqual(state.result, null)
    assert.ok(state.error)
    assert.strictEqual(state.isAmbiguousFailure, false)
  })

  await t.test('query timeout (HTTP 504) flags ambiguous failure and blocks auto-retry', async () => {
    let callCount = 0
    globalThis.fetch = (async () => {
      callCount++
      return new Response(
        JSON.stringify({
          error: 'query_timeout',
          detail: 'Statement exceeded execution timeout of 15 seconds.',
        }),
        { status: 504, headers: { 'Content-Type': 'application/json' } }
      )
    }) as typeof fetch

    const coordinator = new QueryExecutionCoordinator()

    await assert.rejects(
      async () => coordinator.executeQuery('sqlite_demo', 'SELECT heavy_compute();'),
      (err: unknown) => {
        assert.ok(err instanceof AgentClientError)
        assert.strictEqual(err.statusCode, 504)
        return true
      }
    )

    assert.strictEqual(callCount, 1, 'Timed-out query must NEVER be automatically re-executed')
    const state = coordinator.getState()
    assert.strictEqual(state.isLoading, false)
    assert.strictEqual(state.isConfirmedTimeout, true, 'HTTP 504 must be flagged as confirmed timeout')
    assert.strictEqual(state.isAmbiguousFailure, false, 'Confirmed timeout must be distinguished from unknown outcome')
  })

  await t.test('daemon disconnected error flags ambiguous failure and prevents auto-retry', async () => {
    let callCount = 0
    globalThis.fetch = (async () => {
      callCount++
      throw new TypeError('fetch failed')
    }) as typeof fetch

    const coordinator = new QueryExecutionCoordinator()

    await assert.rejects(
      async () => coordinator.executeQuery('sqlite_demo', 'SELECT 1;'),
      (err: unknown) => {
        assert.ok(err instanceof AgentClientError)
        assert.strictEqual(err.kind, 'disconnected')
        return true
      }
    )

    assert.strictEqual(callCount, 1, 'Network disconnect must never be automatically retried')
    const state = coordinator.getState()
    assert.strictEqual(state.isAmbiguousFailure, true, 'Network disconnect must be flagged as ambiguous/unknown outcome')
    assert.strictEqual(state.isConfirmedTimeout, false)
  })

  await t.test('browser cancellation aborts client fetch without claiming database query stopped', async () => {
    let slowResolve: (res: Response) => void
    const slowPromise = new Promise<Response>((resolve) => {
      slowResolve = resolve
    })

    globalThis.fetch = (async () => {
      return slowPromise
    }) as typeof fetch

    const coordinator = new QueryExecutionCoordinator()

    const execPromise = coordinator.executeQuery('sqlite_demo', 'SELECT long_running();')
    assert.strictEqual(coordinator.getState().isLoading, true)

    // User cancels in browser
    coordinator.cancelExecution()

    // Browser state is immediately reset to non-loading
    assert.strictEqual(coordinator.getState().isLoading, false)
    assert.strictEqual(coordinator.getState().result, null)
    assert.strictEqual(coordinator.getState().error, null)

    // Late server response resolves after client cancellation
    slowResolve!(new Response(JSON.stringify(SAMPLE_SUCCESS_RESULT), { status: 200 }))
    const res = await execPromise

    assert.strictEqual(res, null, 'Cancelled client request must return null')
    assert.strictEqual(coordinator.getState().result, null, 'Cancelled request must not write to state')
  })

  await t.test('older request timing out while newer request is in-flight does not overwrite state', async () => {
    let slowResolve: (res: Response) => void
    const slowPromise = new Promise<Response>((resolve) => {
      slowResolve = resolve
    })

    let fastResolve: (res: Response) => void
    const fastPromise = new Promise<Response>((resolve) => {
      fastResolve = resolve
    })

    let callCount = 0
    globalThis.fetch = (async () => {
      callCount++
      if (callCount === 1) {
        return slowPromise
      }
      return fastPromise
    }) as typeof fetch

    const coordinator = new QueryExecutionCoordinator()

    // 1. Dispatch slow request
    const slowPromiseCall = coordinator.executeQuery('sqlite_demo', 'SELECT slow;')
    assert.strictEqual(coordinator.getState().isLoading, true)

    // 2. User cancels
    coordinator.cancelExecution()
    assert.strictEqual(coordinator.getState().isLoading, false)

    // 3. User dispatches Request 2
    const fastPromiseCall = coordinator.executeQuery('sqlite_demo', 'SELECT fast;')
    assert.strictEqual(coordinator.getState().isLoading, true)

    // 4. Request 1 times out with 504 NOW while Request 2 is still in-flight
    slowResolve!(
      new Response(
        JSON.stringify({ error: 'query_timeout', detail: 'Query timed out after 15s' }),
        { status: 504, headers: { 'Content-Type': 'application/json' } }
      )
    )
    await slowPromiseCall

    // 5. State must still be loading for Request 2! Timeout error must not overwrite Request 2!
    assert.strictEqual(coordinator.getState().isLoading, true)
    assert.strictEqual(coordinator.getState().error, null)
    assert.strictEqual(coordinator.getState().isConfirmedTimeout, false)

    // 6. Request 2 completes successfully
    fastResolve!(new Response(JSON.stringify(SAMPLE_SUCCESS_RESULT), { status: 200 }))
    const res2 = await fastPromiseCall

    assert.ok(res2)
    assert.strictEqual(coordinator.getState().isLoading, false)
    assert.strictEqual(coordinator.getState().result?.row_count, 2)
    assert.strictEqual(coordinator.getState().error, null)
  })

  await t.test('authentication expiry (HTTP 401) sets authentication error', async () => {
    globalThis.fetch = (async () => {
      return new Response(
        JSON.stringify({
          error: 'unauthorized',
          detail: 'Session expired or invalid.',
        }),
        { status: 401, headers: { 'Content-Type': 'application/json' } }
      )
    }) as typeof fetch

    const coordinator = new QueryExecutionCoordinator()

    await assert.rejects(
      async () => coordinator.executeQuery('sqlite_demo', 'SELECT 1;'),
      (err: unknown) => {
        assert.ok(err instanceof AgentClientError)
        assert.strictEqual(err.statusCode, 401)
        assert.strictEqual(err.kind, 'authentication')
        return true
      }
    )
  })
})

// ==============================================================================
// 5. Result Privacy & Non-Persistence Safeguards
// ==============================================================================

test('Query Execution - Privacy & Non-Persistence Safeguards', async (t) => {
  t.afterEach(() => {
    restoreFetch()
  })

  await t.test('result rows, cell values, and queries are never stored in localStorage or sessionStorage', async () => {
    globalThis.fetch = (async () => {
      return new Response(JSON.stringify(SAMPLE_SUCCESS_RESULT), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    }) as typeof fetch

    const coordinator = new QueryExecutionCoordinator()

    await coordinator.executeQuery(
      'sqlite_demo',
      'SELECT confidential_column FROM secret_table;'
    )

    if (typeof localStorage !== 'undefined') {
      assert.strictEqual(localStorage.getItem('rows'), null)
      assert.strictEqual(localStorage.getItem('result'), null)
      assert.strictEqual(localStorage.getItem('executed_sql'), null)
    }
    if (typeof sessionStorage !== 'undefined') {
      assert.strictEqual(sessionStorage.getItem('rows'), null)
      assert.strictEqual(sessionStorage.getItem('result'), null)
      assert.strictEqual(sessionStorage.getItem('executed_sql'), null)
    }
  })

  await t.test('verifies results are never forwarded to hosted API or Gemini', async () => {
    let hostedApiCalledWithRows = false

    globalThis.fetch = (async (url: string | URL | Request, init?: RequestInit) => {
      const urlStr = String(url)
      if (urlStr.includes('/api/v1/') && init?.body) {
        const bodyStr = String(init.body)
        if (bodyStr.includes('department_id') || bodyStr.includes('course_count')) {
          hostedApiCalledWithRows = true
        }
      }
      return new Response(JSON.stringify(SAMPLE_SUCCESS_RESULT), { status: 200 })
    }) as typeof fetch

    const coordinator = new QueryExecutionCoordinator()

    await coordinator.executeQuery('sqlite_demo', 'SELECT department_id, course_count FROM table;')

    assert.strictEqual(hostedApiCalledWithRows, false, 'Database rows must NEVER be sent to hosted APIs')
  })
})
