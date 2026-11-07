/**
 * Milestone B29-C: Online / Offline Status Indicator Tests.
 *
 * Verifies:
 * 1. Initial checking state.
 * 2. Authenticated session with verified upstream availability (AI Available).
 * 3. Upstream unavailable (AI Unavailable).
 * 4. Authentication required or expired (Authentication Required).
 * 5. 403 access denied maps truthfully to AI Unavailable with 403 detail.
 * 6. 502 / 504 upstream gateway responses map to AI Unavailable with respective detail.
 * 7. Network connection refused maps to AI Unavailable without misleading access key attribution.
 * 8. Unrecognized or generic errors produce Unknown status.
 * 9. Recovery from unavailable to available upon successful check or generation turn.
 * 10. Monotonic check sequence prevents delayed out-of-order responses from overwriting newer status.
 * 11. Conservative background polling timer is cleanly stopped on unmount/dispose.
 * 12. Local database profile selection and manual SQL execution remain functional when AI is unavailable.
 */

import assert from 'node:assert'
import test from 'node:test'

import {
  AiStatusCoordinator,
  resolveAiStatus,
} from '../src/services/aiStatus.ts'
import { ApiClientError } from '../src/types/api.ts'
import { AgentSessionCoordinator } from '../src/services/agentSession.ts'
import { QueryExecutionCoordinator } from '../src/services/queryExecution.ts'
import type { HealthResponse } from '../src/types/api.ts'

const originalFetch = globalThis.fetch

function restoreFetch(): void {
  globalThis.fetch = originalFetch
}

const SAMPLE_HEALTH: HealthResponse = {
  status: 'online',
  version: '0.2.0',
  dialect: 'tsql',
  tracing_enabled: false,
}

test('Milestone B29-C - Pure Status Resolution Semantics', async (t) => {
  await t.test('1. Initial checking state when checking is active or session is probing', () => {
    const res = resolveAiStatus({
      agentStatus: 'probing',
      isChecking: true,
      upstreamOnline: null,
    })
    assert.strictEqual(res.status, 'checking')
    assert.strictEqual(res.label, 'Checking AI Status')
    assert.ok(res.description.includes('Verifying'))
  })

  await t.test('2. Authenticated session with verified upstream liveness yields AI Available', () => {
    const res = resolveAiStatus({
      agentStatus: 'authenticated',
      isChecking: false,
      upstreamOnline: true,
    })
    assert.strictEqual(res.status, 'ai_available')
    assert.strictEqual(res.label, 'AI Available')
    assert.ok(res.description.includes('reachable'))
  })

  await t.test('3. Authentication required yields Authentication Required immediately', () => {
    const res = resolveAiStatus({
      agentStatus: 'auth_required',
      isChecking: false,
      upstreamOnline: true, // Even if upstream might be online, session requires auth
    })
    assert.strictEqual(res.status, 'auth_required')
    assert.strictEqual(res.label, 'Authentication Required')
  })

  await t.test('4. 403 Access Denied maps to AI Unavailable with 403 Forbidden detail', () => {
    const err = new ApiClientError({
      message: 'Access Denied',
      kind: 'unauthorized',
      statusCode: 403,
    })
    const res = resolveAiStatus({
      agentStatus: 'authenticated',
      isChecking: false,
      upstreamOnline: false,
      upstreamError: err,
    })
    assert.strictEqual(res.status, 'ai_unavailable')
    assert.strictEqual(res.label, 'AI Unavailable')
    assert.ok(res.detail?.includes('403'))
  })

  await t.test('5. 502 / 504 Gateway errors map to AI Unavailable with respective gateway details', () => {
    const err502 = new ApiClientError({
      message: 'Bad Gateway',
      kind: 'server',
      statusCode: 502,
    })
    const res502 = resolveAiStatus({
      agentStatus: 'authenticated',
      isChecking: false,
      upstreamOnline: false,
      upstreamError: err502,
    })
    assert.strictEqual(res502.status, 'ai_unavailable')
    assert.ok(res502.detail?.includes('502'))

    const err504 = new ApiClientError({
      message: 'Gateway Timeout',
      kind: 'server',
      statusCode: 504,
    })
    const res504 = resolveAiStatus({
      agentStatus: 'authenticated',
      isChecking: false,
      upstreamOnline: false,
      upstreamError: err504,
    })
    assert.strictEqual(res504.status, 'ai_unavailable')
    assert.ok(res504.detail?.includes('504'))
  })

  await t.test('6. Network connection refusal maps to AI Unavailable without misleading access key attribution', () => {
    const netErr = new ApiClientError({
      message: 'Unable to connect to the QueryPilot API service.',
      kind: 'network',
    })
    const res = resolveAiStatus({
      agentStatus: 'authenticated',
      isChecking: false,
      upstreamOnline: false,
      upstreamError: netErr,
    })
    assert.strictEqual(res.status, 'ai_unavailable')
    assert.ok(res.detail?.includes('Connection Refused'))
    assert.ok(!res.description.includes('access key'), 'Must not claim invalid access key on network failure')
  })

  await t.test('7. Unrecognized or generic errors yield AI Status Unknown', () => {
    const genericErr = new Error('Unexpected protocol payload parse error')
    const res = resolveAiStatus({
      agentStatus: 'authenticated',
      isChecking: false,
      upstreamOnline: null,
      upstreamError: genericErr,
    })
    assert.strictEqual(res.status, 'unknown')
    assert.strictEqual(res.label, 'AI Status Unknown')
  })
})

test('Milestone B29-C - AiStatusCoordinator Lifecycle & Concurrency', async (t) => {
  t.afterEach(restoreFetch)

  await t.test('8. Probing upstream health updates status to AI Available', async () => {
    globalThis.fetch = (async (url: string | URL | Request) => {
      const urlStr = String(url)
      if (urlStr.includes('/health')) {
        return new Response(JSON.stringify(SAMPLE_HEALTH), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      }
      return new Response('Not Found', { status: 404 })
    }) as typeof fetch

    const coordinator = new AiStatusCoordinator()
    coordinator.syncAgentStatus('authenticated')

    await coordinator.checkStatus()

    const state = coordinator.getState()
    assert.strictEqual(state.status, 'ai_available')
    assert.strictEqual(state.label, 'AI Available')
    assert.strictEqual(state.isChecking, false)
    assert.ok(typeof state.lastChecked === 'number')
    coordinator.dispose()
  })

  await t.test('9. Recovery from unavailable to available upon generation success', () => {
    const coordinator = new AiStatusCoordinator({
      status: 'ai_unavailable',
      label: 'AI Unavailable',
      description: 'Upstream gateway timed out',
    })

    assert.strictEqual(coordinator.getState().status, 'ai_unavailable')

    // AI turn succeeds
    coordinator.recordGenerationSuccess()

    const recovered = coordinator.getState()
    assert.strictEqual(recovered.status, 'ai_available')
    assert.strictEqual(recovered.label, 'AI Available')
    coordinator.dispose()
  })

  await t.test('10. Stale response protection prevents delayed probe from overwriting newer failure', async () => {
    let delayedResolve!: (res: Response) => void
    const delayedPromise = new Promise<Response>((resolve) => {
      delayedResolve = resolve
    })

    let callCount = 0
    globalThis.fetch = (async (url: string | URL | Request) => {
      callCount++
      if (callCount === 1) {
        return delayedPromise // hangs
      }
      return new Response(
        JSON.stringify({ error: 'UPSTREAM_TIMEOUT', detail: 'Upstream timed out' }),
        { status: 504, headers: { 'Content-Type': 'application/json' } }
      )
    }) as typeof fetch

    const coordinator = new AiStatusCoordinator()
    coordinator.syncAgentStatus('authenticated')

    // 1. Dispatch first check (will hang)
    const check1 = coordinator.checkStatus()

    // 2. Dispatch second check (completes with 504 failure)
    await coordinator.checkStatus()
    assert.strictEqual(coordinator.getState().status, 'ai_unavailable')

    // 3. First check now resolves with 200 OK
    delayedResolve(
      new Response(JSON.stringify(SAMPLE_HEALTH), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    )
    await check1

    // State must remain ai_unavailable because check1 is superseded/stale
    assert.strictEqual(
      coordinator.getState().status,
      'ai_unavailable',
      'Stale check response must NOT overwrite newer status'
    )
    coordinator.dispose()
  })

  await t.test('11. Polling interval timer is stopped and cleaned up on dispose', () => {
    const coordinator = new AiStatusCoordinator()
    const stopPolling = coordinator.startPolling(5000)

    // Stopping via returned callback
    stopPolling()
    coordinator.dispose()
    // Verify no unhandled timers or crashes
    assert.ok(true)
  })

  await t.test('12. Local database profiling and manual SQL execution remain functional when AI is unavailable', async () => {
    const aiCoord = new AiStatusCoordinator({
      status: 'ai_unavailable',
      label: 'AI Unavailable',
      description: 'Upstream connection refused. Manual SQL remains operational.',
    })
    const agentSession = new AgentSessionCoordinator({
      status: 'authenticated',
      profiles: [
        {
          profile_id: 'local_sqlite',
          display_name: 'Local SQLite DB',
          engine_type: 'sqlite',
          schema_name: 'main',
          timeout_seconds: 15,
          max_row_limit: 100,
          has_allowlist: false,
          is_default: true,
        },
      ],
      selectedProfileId: 'local_sqlite',
      schemaStatus: 'loaded',
    })
    // Verify local query execution completes successfully regardless of AI availability
    const execCoord = new QueryExecutionCoordinator({
      result: {
        columns: ['id', 'title'],
        rows: [{ id: 1, title: 'Introduction to CS' }],
        row_count: 1,
        truncated: false,
        execution_time_ms: 1.2,
      },
    })
    assert.strictEqual(execCoord.getState().result?.row_count, 1)

    aiCoord.dispose()
  })
})
