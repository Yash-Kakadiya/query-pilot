import assert from 'node:assert'
import test from 'node:test'
import {
  AgentSessionCoordinator,
  type AgentSessionState,
} from '../src/services/agentSession.ts'

const originalFetch = globalThis.fetch

function restoreFetch(): void {
  globalThis.fetch = originalFetch
}

const VALID_TOKEN = 'bootstrap-token-test-1234567890'
const SAMPLE_PROFILES = [
  {
    profile_id: 'sqlite_demo',
    display_name: 'SQLite Demo Database',
    schema_name: 'main',
    timeout_seconds: 30,
    max_row_limit: 1000,
    has_allowlist: false,
    allowed_tables: null,
  },
]

// ==============================================================================
// 1. Session Probing & Discovery
// ==============================================================================

test('Agent Session Coordinator - Session Probing & State Classification', async (t) => {
  t.afterEach(() => {
    restoreFetch()
  })

  await t.test('Initial successful session probe establishes authenticated state with profiles in memory', async () => {
    globalThis.fetch = (async (url: string | URL | Request) => {
      const urlStr = String(url)
      if (urlStr.includes('/v1/profiles')) {
        return new Response(JSON.stringify({ profiles: SAMPLE_PROFILES }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      }
      return new Response('Not Found', { status: 404 })
    }) as typeof fetch

    const coordinator = new AgentSessionCoordinator()
    assert.strictEqual(coordinator.getState().status, 'probing')

    await coordinator.probeSession()

    const state = coordinator.getState()
    assert.strictEqual(state.status, 'authenticated')
    assert.strictEqual(state.profiles.length, 1)
    assert.strictEqual(state.profiles[0].profile_id, 'sqlite_demo')
    assert.strictEqual(state.isModalOpen, false)
    assert.strictEqual(state.errorMessage, undefined)
  })

  await t.test('Initial 401 returns auth_required and automatically opens the authentication dialog', async () => {
    globalThis.fetch = (async () => {
      return new Response(
        JSON.stringify({
          error: 'UNAUTHORIZED',
          detail: 'Authentication required. Please authenticate via /agent/v1/auth/bootstrap.',
        }),
        {
          status: 401,
          headers: { 'Content-Type': 'application/json' },
        }
      )
    }) as typeof fetch

    const coordinator = new AgentSessionCoordinator()
    await coordinator.probeSession()

    const state = coordinator.getState()
    assert.strictEqual(state.status, 'auth_required')
    assert.strictEqual(state.isModalOpen, true, 'Authentication dialog should open on 401')
    assert.strictEqual(state.profiles.length, 0)
    assert.strictEqual(state.errorMessage, undefined)
  })

  await t.test('Agent network failure shows a disconnected/retry state, not the bootstrap dialog', async () => {
    globalThis.fetch = (async () => {
      // Simulate daemon offline / loopback ECONNREFUSED
      throw new TypeError('Failed to fetch: Connection refused')
    }) as typeof fetch

    const coordinator = new AgentSessionCoordinator()
    await coordinator.probeSession()

    const state = coordinator.getState()
    assert.strictEqual(state.status, 'disconnected')
    assert.strictEqual(state.isModalOpen, false, 'Modal MUST remain closed on disconnected/network failure')
    assert.strictEqual(state.profiles.length, 0)
    assert.ok(state.errorMessage)
    assert.ok(
      state.errorMessage.includes('local agent is running on loopback') ||
      state.errorMessage.includes('Unable to connect'),
      `Unexpected error message: ${state.errorMessage}`
    )
  })

  await t.test('Daemon 500 error shows error state without opening auth modal', async () => {
    globalThis.fetch = (async () => {
      return new Response(
        JSON.stringify({
          error: 'INTERNAL_ERROR',
          detail: 'Internal server error in agent daemon.',
        }),
        {
          status: 500,
          headers: { 'Content-Type': 'application/json' },
        }
      )
    }) as typeof fetch

    const coordinator = new AgentSessionCoordinator()
    await coordinator.probeSession()

    const state = coordinator.getState()
    assert.strictEqual(state.status, 'error')
    assert.strictEqual(state.isModalOpen, false)
    assert.strictEqual(state.profiles.length, 0)
    assert.ok(state.errorMessage)
  })
})

// ==============================================================================
// 2. Bootstrap Authentication & Profile Loading
// ==============================================================================

test('Agent Session Coordinator - Bootstrap Token Submission & Profile Loading', async (t) => {
  t.afterEach(() => {
    restoreFetch()
  })

  await t.test('Successful bootstrap followed by immediate profile loading', async () => {
    const requestedEndpoints: string[] = []

    globalThis.fetch = (async (url: string | URL | Request) => {
      const urlStr = String(url)
      requestedEndpoints.push(urlStr)

      if (urlStr.includes('/v1/auth/bootstrap')) {
        return new Response(
          JSON.stringify({
            status: 'success',
            message: 'Bootstrap authentication successful. Ambient loopback session established.',
          }),
          {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          }
        )
      }

      if (urlStr.includes('/v1/profiles')) {
        return new Response(JSON.stringify({ profiles: SAMPLE_PROFILES }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      }

      return new Response('Not Found', { status: 404 })
    }) as typeof fetch

    const coordinator = new AgentSessionCoordinator({ status: 'auth_required', isModalOpen: true })

    const result = await coordinator.submitBootstrap(VALID_TOKEN)

    assert.strictEqual(result.success, true)
    assert.strictEqual(requestedEndpoints.length, 2)
    assert.ok(requestedEndpoints[0].includes('/v1/auth/bootstrap'))
    assert.ok(requestedEndpoints[1].includes('/v1/profiles'))

    const state = coordinator.getState()
    assert.strictEqual(state.status, 'authenticated')
    assert.strictEqual(state.profiles.length, 1)
    assert.strictEqual(state.profiles[0].profile_id, 'sqlite_demo')
    assert.strictEqual(state.isModalOpen, false)
    assert.strictEqual(state.isSubmitting, false)
    assert.strictEqual(state.authError, undefined)
  })

  await t.test('Client-side token validation rejects invalid token lengths before network dispatch', async () => {
    let fetchCount = 0
    globalThis.fetch = (async () => {
      fetchCount++
      return new Response('{}', { status: 200 })
    }) as typeof fetch

    const coordinator = new AgentSessionCoordinator({ status: 'auth_required', isModalOpen: true })

    // Empty
    const emptyResult = await coordinator.submitBootstrap('')
    assert.strictEqual(emptyResult.success, false)
    assert.ok(/cannot be empty/i.test(emptyResult.error || ''))
    assert.strictEqual(fetchCount, 0, 'Should not dispatch network request on empty token')

    // Whitespace only
    const whitespaceResult = await coordinator.submitBootstrap('   \t\n  ')
    assert.strictEqual(whitespaceResult.success, false)
    assert.ok(/cannot be empty/i.test(whitespaceResult.error || ''))
    assert.strictEqual(fetchCount, 0)

    // Too short (< 16 chars)
    const shortResult = await coordinator.submitBootstrap('too-short')
    assert.strictEqual(shortResult.success, false)
    assert.ok(/between 16 and 128/i.test(shortResult.error || ''))
    assert.strictEqual(fetchCount, 0)

    // Too long (> 128 chars)
    const longResult = await coordinator.submitBootstrap('x'.repeat(129))
    assert.strictEqual(longResult.success, false)
    assert.ok(/between 16 and 128/i.test(longResult.error || ''))
    assert.strictEqual(fetchCount, 0)
  })

  await t.test('Invalid or expired bootstrap token returns error and keeps modal open for retry', async () => {
    globalThis.fetch = (async (url: string | URL | Request) => {
      const urlStr = String(url)
      if (urlStr.includes('/v1/auth/bootstrap')) {
        return new Response(
          JSON.stringify({
            error: 'UNAUTHORIZED',
            detail: 'Invalid, expired, or already-consumed bootstrap token.',
          }),
          {
            status: 401,
            headers: { 'Content-Type': 'application/json' },
          }
        )
      }
      return new Response('Not Found', { status: 404 })
    }) as typeof fetch

    const coordinator = new AgentSessionCoordinator({ status: 'auth_required', isModalOpen: true })

    const result = await coordinator.submitBootstrap(VALID_TOKEN)

    assert.strictEqual(result.success, false)
    assert.ok(
      result.error?.includes('Invalid, expired, or already-consumed bootstrap token'),
      `Unexpected error: ${result.error}`
    )

    const state = coordinator.getState()
    assert.strictEqual(state.status, 'auth_required')
    assert.strictEqual(state.isModalOpen, true, 'Modal should remain open after failed authentication')
    assert.strictEqual(state.isSubmitting, false)
    assert.strictEqual(state.profiles.length, 0)
  })

  await t.test('Duplicate submit protection prevents concurrent bootstrap submissions', async () => {
    let bootstrapInvocations = 0

    globalThis.fetch = (async (url: string | URL | Request) => {
      const urlStr = String(url)
      if (urlStr.includes('/v1/auth/bootstrap')) {
        bootstrapInvocations++
        // Simulate in-flight delay
        await new Promise((resolve) => setTimeout(resolve, 30))
        return new Response(JSON.stringify({ status: 'success' }), { status: 200 })
      }
      if (urlStr.includes('/v1/profiles')) {
        return new Response(JSON.stringify({ profiles: SAMPLE_PROFILES }), { status: 200 })
      }
      return new Response('Not Found', { status: 404 })
    }) as typeof fetch

    const coordinator = new AgentSessionCoordinator({ status: 'auth_required', isModalOpen: true })

    // Launch first submission (takes 30ms)
    const firstPromise = coordinator.submitBootstrap(VALID_TOKEN)

    // Attempt second submission concurrently
    const secondResult = await coordinator.submitBootstrap(VALID_TOKEN)

    // Second submission must be rejected immediately
    assert.strictEqual(secondResult.success, false)
    assert.strictEqual(secondResult.error, 'Authentication already in progress.')

    // First submission finishes successfully
    const firstResult = await firstPromise
    assert.strictEqual(firstResult.success, true)
    assert.strictEqual(bootstrapInvocations, 1, 'Only one bootstrap request should have been dispatched')
  })
})

// ==============================================================================
// 3. Logout & State Clearance
// ==============================================================================

test('Agent Session Coordinator - Logout & State Clearance', async (t) => {
  t.afterEach(() => {
    restoreFetch()
  })

  await t.test('Logout clears session and profile state immediately', async () => {
    let logoutCalled = false
    globalThis.fetch = (async (url: string | URL | Request) => {
      const urlStr = String(url)
      if (urlStr.includes('/v1/auth/logout')) {
        logoutCalled = true
        return new Response(
          JSON.stringify({
            status: 'logged_out',
            message: 'Session terminated and cookie cleared.',
          }),
          { status: 200 }
        )
      }
      return new Response('Not Found', { status: 404 })
    }) as typeof fetch

    const coordinator = new AgentSessionCoordinator({
      status: 'authenticated',
      profiles: SAMPLE_PROFILES,
      isModalOpen: false,
    })

    assert.strictEqual(coordinator.getState().profiles.length, 1)

    await coordinator.logout()

    assert.strictEqual(logoutCalled, true)
    const state = coordinator.getState()
    assert.strictEqual(state.status, 'auth_required')
    assert.strictEqual(state.profiles.length, 0, 'Profiles MUST be cleared from memory on logout')
    assert.strictEqual(state.isModalOpen, false)
  })

  await t.test('Logout succeeds and clears local state even if daemon returns error or is unreachable', async () => {
    globalThis.fetch = (async () => {
      throw new TypeError('Failed to fetch')
    }) as typeof fetch

    const coordinator = new AgentSessionCoordinator({
      status: 'authenticated',
      profiles: SAMPLE_PROFILES,
    })

    await coordinator.logout()

    const state = coordinator.getState()
    assert.strictEqual(state.status, 'auth_required')
    assert.strictEqual(state.profiles.length, 0)
  })
})

// ==============================================================================
// 4. Stale-Response Protection & Credential Safeguards
// ==============================================================================

test('Agent Session Coordinator - Stale-Response & Credential Safeguards', async (t) => {
  t.afterEach(() => {
    restoreFetch()
  })

  await t.test('Stale profile responses cannot restore authenticated state after logout', async () => {
    let delayedResolve: (value: Response) => void
    const delayedPromise = new Promise<Response>((resolve) => {
      delayedResolve = resolve
    })

    globalThis.fetch = (async (url: string | URL | Request) => {
      const urlStr = String(url)
      if (urlStr.includes('/v1/profiles')) {
        // Return a promise that hangs until we resolve it after logout
        return delayedPromise
      }
      if (urlStr.includes('/v1/auth/logout')) {
        return new Response(JSON.stringify({ status: 'ok' }), { status: 200 })
      }
      return new Response('Not Found', { status: 404 })
    }) as typeof fetch

    const coordinator = new AgentSessionCoordinator()

    // 1. Initiate probe (will hang on delayedPromise)
    const probePromise = coordinator.probeSession()

    // 2. User logs out while probe is still in-flight
    await coordinator.logout()

    assert.strictEqual(coordinator.getState().status, 'auth_required')
    assert.strictEqual(coordinator.getState().profiles.length, 0)

    // 3. The delayed probe response finally arrives with profiles
    delayedResolve!(
      new Response(JSON.stringify({ profiles: SAMPLE_PROFILES }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    )

    await probePromise

    // 4. Verify stale response was ignored by generation counter
    const finalState = coordinator.getState()
    assert.strictEqual(finalState.status, 'auth_required', 'Stale response must not override auth_required')
    assert.strictEqual(finalState.profiles.length, 0, 'Stale profiles must not be applied to state')
  })

  await t.test('Bootstrap token is never persisted in storage or coordinator state', async () => {
    globalThis.fetch = (async (url: string | URL | Request) => {
      const urlStr = String(url)
      if (urlStr.includes('/v1/auth/bootstrap')) {
        return new Response(JSON.stringify({ status: 'success' }), { status: 200 })
      }
      if (urlStr.includes('/v1/profiles')) {
        return new Response(JSON.stringify({ profiles: SAMPLE_PROFILES }), { status: 200 })
      }
      return new Response('Not Found', { status: 404 })
    }) as typeof fetch

    const testToken = 'super-secret-bootstrap-token-12345'
    const coordinator = new AgentSessionCoordinator()

    await coordinator.submitBootstrap(testToken)

    // Inspect coordinator state
    const state = coordinator.getState() as AgentSessionState & Record<string, unknown>
    assert.strictEqual(state.status, 'authenticated')

    for (const [key, value] of Object.entries(state)) {
      assert.notStrictEqual(
        value,
        testToken,
        `Token was found persisted in state under key "${key}"`
      )
    }

    // Inspect browser storage mocks if present
    if (typeof localStorage !== 'undefined') {
      assert.strictEqual(localStorage.getItem('qpt_session'), null)
      assert.strictEqual(localStorage.getItem('qp_agent_session'), null)
      assert.strictEqual(localStorage.getItem('bootstrap_token'), null)
      assert.strictEqual(localStorage.getItem('token'), null)
    }
    if (typeof sessionStorage !== 'undefined') {
      assert.strictEqual(sessionStorage.getItem('qpt_session'), null)
      assert.strictEqual(sessionStorage.getItem('qp_agent_session'), null)
      assert.strictEqual(sessionStorage.getItem('bootstrap_token'), null)
      assert.strictEqual(sessionStorage.getItem('token'), null)
    }
  })

  await t.test('Modal open and close controls function as expected', () => {
    const coordinator = new AgentSessionCoordinator({ isModalOpen: false })

    coordinator.openModal()
    assert.strictEqual(coordinator.getState().isModalOpen, true)

    coordinator.closeModal()
    assert.strictEqual(coordinator.getState().isModalOpen, false)
  })

  await t.test('Listeners receive state updates on every transition', async () => {
    globalThis.fetch = (async () => {
      return new Response(JSON.stringify({ profiles: SAMPLE_PROFILES }), { status: 200 })
    }) as typeof fetch

    const coordinator = new AgentSessionCoordinator()
    const receivedStatuses: string[] = []

    const unsubscribe = coordinator.subscribe((state) => {
      receivedStatuses.push(state.status)
    })

    await coordinator.probeSession()
    unsubscribe()

    assert.ok(receivedStatuses.includes('probing'))
    assert.ok(receivedStatuses.includes('authenticated'))
  })
})
