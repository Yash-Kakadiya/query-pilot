import assert from 'node:assert'
import test from 'node:test'
import { AgentSessionCoordinator } from '../src/services/agentSession.ts'
import { AgentClientError } from '../src/services/agentClient.ts'
import type {
  AgentDatabaseSchema,
  CreateProfileRequest,
  ProfileSummary,
  TestConnectionResponse,
} from '../src/types/api.ts'

const originalFetch = globalThis.fetch

function restoreFetch(): void {
  globalThis.fetch = originalFetch
}

const SAMPLE_EXISTING_PROFILES: ProfileSummary[] = [
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

const SAMPLE_NEW_PROFILE_CONFIG: CreateProfileRequest = {
  profile_id: 'mssql_analytics',
  display_name: 'Local SQL Server Analytics',
  server: 'localhost\\SQLEXPRESS',
  database_name: 'AnalyticsDB',
  schema_name: 'dbo',
  driver: 'ODBC Driver 17 for SQL Server',
  engine_type: 'mssql',
  use_trusted_connection: true,
  timeout_seconds: 15,
  max_row_limit: 100,
  set_as_default: false,
}

const SAMPLE_NEW_PROFILE_SUMMARY: ProfileSummary = {
  profile_id: 'mssql_analytics',
  display_name: 'Local SQL Server Analytics',
  schema_name: 'dbo',
  timeout_seconds: 15,
  max_row_limit: 100,
  has_allowlist: false,
  allowed_tables: null,
}

const SAMPLE_RAW_SCHEMA: AgentDatabaseSchema = {
  database_name: 'AnalyticsDB',
  tables: [
    {
      schema_name: 'dbo',
      table_name: 'monthly_sales',
      columns: [
        { name: 'sale_id', data_type: 'INTEGER', primary_key: true, nullable: false },
        { name: 'amount', data_type: 'DECIMAL', primary_key: false, nullable: false },
      ],
      primary_keys: ['sale_id'],
      foreign_keys: [],
    },
  ],
}

// ==============================================================================
// 1. Profile Onboarding & Persistence Lifecycle
// ==============================================================================

test('Profile Onboarding - Validation and Creation Lifecycle', async (t) => {
  t.afterEach(() => {
    restoreFetch()
  })

  await t.test('rejects client-side invalid configuration without sending network request', async () => {
    let fetchCalled = false
    globalThis.fetch = (async () => {
      fetchCalled = true
      return new Response(JSON.stringify({}), { status: 200 })
    }) as typeof fetch

    const coordinator = new AgentSessionCoordinator({
      status: 'authenticated',
      profiles: SAMPLE_EXISTING_PROFILES,
    })

    await assert.rejects(
      async () =>
        coordinator.validateConfig({
          ...SAMPLE_NEW_PROFILE_CONFIG,
          profile_id: 'invalid id with spaces!',
        }),
      (err: unknown) => {
        assert(err instanceof AgentClientError)
        assert.strictEqual(err.kind, 'validation')
        assert(err.message.includes('Profile ID must be 1-64 characters'))
        return true
      }
    )

    assert.strictEqual(fetchCalled, false)
  })

  await t.test('handles backend validation failure (HTTP 422)', async () => {
    globalThis.fetch = (async (url: string | URL | Request) => {
      const urlStr = String(url)
      if (urlStr.includes('/v1/profiles/validate-config')) {
        return new Response(
          JSON.stringify({
            detail: [{ loc: ['body', 'server'], msg: 'Remote database targets are forbidden by local security policy.' }],
          }),
          { status: 422, headers: { 'Content-Type': 'application/json' } }
        )
      }
      return new Response('Not Found', { status: 404 })
    }) as typeof fetch

    const coordinator = new AgentSessionCoordinator({
      status: 'authenticated',
      profiles: SAMPLE_EXISTING_PROFILES,
    })

    await assert.rejects(
      async () => coordinator.validateConfig(SAMPLE_NEW_PROFILE_CONFIG),
      (err: unknown) => {
        assert(err instanceof AgentClientError)
        assert.strictEqual(err.kind, 'validation')
        assert.strictEqual(err.statusCode, 422)
        assert(err.message.includes('Remote database targets are forbidden'))
        return true
      }
    )
  })

  await t.test('handles duplicate profile ID conflict (HTTP 409)', async () => {
    globalThis.fetch = (async (url: string | URL | Request) => {
      const urlStr = String(url)
      if (urlStr.includes('/v1/profiles')) {
        return new Response(
          JSON.stringify({
            error: 'conflict',
            detail: "Profile 'mssql_analytics' already exists in the profile store.",
          }),
          { status: 409, headers: { 'Content-Type': 'application/json' } }
        )
      }
      return new Response('Not Found', { status: 404 })
    }) as typeof fetch

    const coordinator = new AgentSessionCoordinator({
      status: 'authenticated',
      profiles: SAMPLE_EXISTING_PROFILES,
    })

    await assert.rejects(
      async () => coordinator.addProfile(SAMPLE_NEW_PROFILE_CONFIG),
      (err: unknown) => {
        assert(err instanceof AgentClientError)
        assert.strictEqual(err.kind, 'conflict')
        assert.strictEqual(err.statusCode, 409)
        assert(err.message.includes('already exists in the profile store'))
        return true
      }
    )
  })

  await t.test('successful creation refreshes profile list without automatically selecting or loading schema', async () => {
    let schemaRequested = false
    let testConnectionRequested = false

    globalThis.fetch = (async (url: string | URL | Request, options?: RequestInit) => {
      const urlStr = String(url)
      const method = options?.method || 'GET'

      if (urlStr.includes('/v1/profiles') && method === 'POST') {
        return new Response(
          JSON.stringify({
            profile: SAMPLE_NEW_PROFILE_SUMMARY,
            is_default: false,
            message: 'Database connection profile created and registered successfully.',
          }),
          { status: 201, headers: { 'Content-Type': 'application/json' } }
        )
      }

      if (urlStr.includes('/v1/profiles') && method === 'GET') {
        return new Response(
          JSON.stringify({
            profiles: [...SAMPLE_EXISTING_PROFILES, SAMPLE_NEW_PROFILE_SUMMARY],
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        )
      }

      if (urlStr.includes('/schema')) {
        schemaRequested = true
        return new Response(JSON.stringify(SAMPLE_RAW_SCHEMA), { status: 200 })
      }

      if (urlStr.includes('/test-connection')) {
        testConnectionRequested = true
        return new Response(JSON.stringify({ connected: true }), { status: 200 })
      }

      return new Response('Not Found', { status: 404 })
    }) as typeof fetch

    const coordinator = new AgentSessionCoordinator({
      status: 'authenticated',
      profiles: SAMPLE_EXISTING_PROFILES,
      selectedProfileId: null,
      selectedProfile: null,
      schemaStatus: 'idle',
    })

    const res = await coordinator.addProfile(SAMPLE_NEW_PROFILE_CONFIG)
    assert.strictEqual(res.profile.profile_id, 'mssql_analytics')

    const state = coordinator.getState()
    // Invariant: Profile list now contains newly created profile
    assert.strictEqual(state.profiles.length, 2)
    assert.ok(state.profiles.some((p) => p.profile_id === 'mssql_analytics'))

    // Invariant: Did NOT automatically select profile or load schema
    assert.strictEqual(state.selectedProfileId, null)
    assert.strictEqual(state.selectedProfile, null)
    assert.strictEqual(state.schemaStatus, 'idle')
    assert.strictEqual(schemaRequested, false)
    assert.strictEqual(testConnectionRequested, false)
  })
})

// ==============================================================================
// 2. Explicit Bounded Connection Testing Lifecycle
// ==============================================================================

test('Profile Onboarding - Explicit Connection Testing Lifecycle', async (t) => {
  t.afterEach(() => {
    restoreFetch()
  })

  await t.test('explicit testConnection() updates status to success on connected: true', async () => {
    globalThis.fetch = (async (url: string | URL | Request, options?: RequestInit) => {
      const urlStr = String(url)
      if (urlStr.includes('/v1/profiles/mssql_analytics/test-connection')) {
        assert.strictEqual(options?.method, 'POST')
        assert.strictEqual(options?.body, '{}')
        return new Response(
          JSON.stringify({
            connected: true,
            database_name: 'AnalyticsDB',
            latency_ms: 18.2,
            message: 'Connection verified successfully. SELECT DB_NAME() returned AnalyticsDB.',
            failure_code: null,
          } satisfies TestConnectionResponse),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        )
      }
      return new Response('Not Found', { status: 404 })
    }) as typeof fetch

    const coordinator = new AgentSessionCoordinator({
      status: 'authenticated',
      profiles: [...SAMPLE_EXISTING_PROFILES, SAMPLE_NEW_PROFILE_SUMMARY],
    })

    const testRes = await coordinator.testConnection('mssql_analytics')
    assert.strictEqual(testRes.connected, true)
    assert.strictEqual(testRes.database_name, 'AnalyticsDB')
    assert.strictEqual(testRes.latency_ms, 18.2)

    const state = coordinator.getState()
    assert.strictEqual(state.connectionTestStatus, 'success')
    assert.strictEqual(state.connectionTestResult?.connected, true)
    assert.strictEqual(state.connectionTestResult?.database_name, 'AnalyticsDB')
  })

  await t.test('typed connection failure with HTTP 200 and connected: false preserves saved profile', async () => {
    globalThis.fetch = (async (url: string | URL | Request) => {
      const urlStr = String(url)
      if (urlStr.includes('/test-connection')) {
        return new Response(
          JSON.stringify({
            connected: false,
            database_name: null,
            latency_ms: null,
            message: 'Windows Integrated Authentication failed for target SQL Server.',
            failure_code: 'AUTH_FAILED',
          } satisfies TestConnectionResponse),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        )
      }
      return new Response('Not Found', { status: 404 })
    }) as typeof fetch

    const coordinator = new AgentSessionCoordinator({
      status: 'authenticated',
      profiles: [...SAMPLE_EXISTING_PROFILES, SAMPLE_NEW_PROFILE_SUMMARY],
    })

    const testRes = await coordinator.testConnection('mssql_analytics')
    assert.strictEqual(testRes.connected, false)
    assert.strictEqual(testRes.failure_code, 'AUTH_FAILED')

    const state = coordinator.getState()
    assert.strictEqual(state.connectionTestStatus, 'failed')
    assert.strictEqual(state.connectionTestResult?.connected, false)
    assert.strictEqual(state.connectionTestResult?.failure_code, 'AUTH_FAILED')

    // Invariant: Failed connection test does NOT remove saved profile from profile list
    assert.strictEqual(state.profiles.length, 2)
    assert.ok(state.profiles.some((p) => p.profile_id === 'mssql_analytics'))
  })

  await t.test('user can retry connection test explicitly after failure', async () => {
    let attempt = 0
    globalThis.fetch = (async (url: string | URL | Request) => {
      const urlStr = String(url)
      if (urlStr.includes('/test-connection')) {
        attempt++
        if (attempt === 1) {
          return new Response(
            JSON.stringify({
              connected: false,
              database_name: null,
              latency_ms: null,
              message: 'SQL Server service is unreachable.',
              failure_code: 'UNREACHABLE',
            } satisfies TestConnectionResponse),
            { status: 200, headers: { 'Content-Type': 'application/json' } }
          )
        }
        // Attempt 2 succeeds
        return new Response(
          JSON.stringify({
            connected: true,
            database_name: 'AnalyticsDB',
            latency_ms: 12.0,
            message: 'Connection verified successfully.',
            failure_code: null,
          } satisfies TestConnectionResponse),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        )
      }
      return new Response('Not Found', { status: 404 })
    }) as typeof fetch

    const coordinator = new AgentSessionCoordinator({
      status: 'authenticated',
      profiles: [...SAMPLE_EXISTING_PROFILES, SAMPLE_NEW_PROFILE_SUMMARY],
    })

    // First attempt fails
    const res1 = await coordinator.testConnection('mssql_analytics')
    assert.strictEqual(res1.connected, false)
    assert.strictEqual(coordinator.getState().connectionTestStatus, 'failed')

    // Second attempt succeeds
    const res2 = await coordinator.testConnection('mssql_analytics')
    assert.strictEqual(res2.connected, true)
    assert.strictEqual(coordinator.getState().connectionTestStatus, 'success')
    assert.strictEqual(attempt, 2)
  })
})

// ==============================================================================
// 3. Selection, Schema Loading & Decoupled Execution
// ==============================================================================

test('Profile Onboarding - Explicit Selection & Decoupled Workflow', async (t) => {
  t.afterEach(() => {
    restoreFetch()
  })

  await t.test('explicit selectProfile() resets test banner and invokes schema loading', async () => {
    globalThis.fetch = (async (url: string | URL | Request) => {
      const urlStr = String(url)
      if (urlStr.includes('/v1/profiles/mssql_analytics/schema')) {
        return new Response(JSON.stringify(SAMPLE_RAW_SCHEMA), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      }
      return new Response('Not Found', { status: 404 })
    }) as typeof fetch

    const coordinator = new AgentSessionCoordinator({
      status: 'authenticated',
      profiles: [...SAMPLE_EXISTING_PROFILES, SAMPLE_NEW_PROFILE_SUMMARY],
      connectionTestStatus: 'success',
      connectionTestResult: {
        connected: true,
        database_name: 'AnalyticsDB',
        latency_ms: 15.0,
        message: 'Verified',
      },
    })

    // User explicitly clicks "Use Profile"
    await coordinator.selectProfile('mssql_analytics')

    const state = coordinator.getState()
    assert.strictEqual(state.selectedProfileId, 'mssql_analytics')
    assert.strictEqual(state.schemaStatus, 'loaded')
    assert.ok(state.schemaContext)
    assert.strictEqual(state.schemaContext.database_name, 'AnalyticsDB')

    // Invariant: connectionTestStatus is reset so test state does not persist onto active query views
    assert.strictEqual(state.connectionTestStatus, 'idle')
    assert.strictEqual(state.connectionTestResult, null)
  })

  await t.test('switching profiles resets connection test status so test results never leak', async () => {
    globalThis.fetch = (async (url: string | URL | Request) => {
      const urlStr = String(url)
      if (urlStr.includes('/schema')) {
        return new Response(JSON.stringify(SAMPLE_RAW_SCHEMA), { status: 200 })
      }
      return new Response('Not Found', { status: 404 })
    }) as typeof fetch

    const coordinator = new AgentSessionCoordinator({
      status: 'authenticated',
      profiles: [...SAMPLE_EXISTING_PROFILES, SAMPLE_NEW_PROFILE_SUMMARY],
      selectedProfileId: 'mssql_analytics',
      connectionTestStatus: 'success',
      connectionTestResult: {
        connected: true,
        database_name: 'AnalyticsDB',
        latency_ms: 14.2,
        message: 'Verified',
      },
    })

    // Switch to another profile
    await coordinator.selectProfile('sqlite_demo')

    const state = coordinator.getState()
    assert.strictEqual(state.selectedProfileId, 'sqlite_demo')
    assert.strictEqual(state.connectionTestStatus, 'idle')
    assert.strictEqual(state.connectionTestResult, null)
  })

  await t.test('session expiration (HTTP 401) during connection test transitions to auth_required', async () => {
    globalThis.fetch = (async () => {
      return new Response(
        JSON.stringify({ error: 'unauthorized', detail: 'Agent session expired.' }),
        { status: 401, headers: { 'Content-Type': 'application/json' } }
      )
    }) as typeof fetch

    const coordinator = new AgentSessionCoordinator({
      status: 'authenticated',
      profiles: [...SAMPLE_EXISTING_PROFILES, SAMPLE_NEW_PROFILE_SUMMARY],
    })

    await assert.rejects(
      async () => coordinator.testConnection('mssql_analytics'),
      (err: unknown) => {
        assert(err instanceof AgentClientError)
        assert.strictEqual(err.kind, 'authentication')
        assert.strictEqual(err.statusCode, 401)
        return true
      }
    )

    const state = coordinator.getState()
    assert.strictEqual(state.status, 'auth_required')
    assert.strictEqual(state.isModalOpen, true)
  })

  await t.test(
    'delayed connection test resolving after profile switch does not overwrite new profile connection test state',
    async () => {
      let resolveSlowTest: (res: Response) => void = () => {}
      const slowPromise = new Promise<Response>((resolve) => {
        resolveSlowTest = resolve
      })

      globalThis.fetch = (async (url: string | URL | Request) => {
        const urlStr = String(url)
        if (urlStr.includes('/test-connection')) {
          return slowPromise
        }
        if (urlStr.includes('/schema')) {
          return new Response(JSON.stringify(SAMPLE_RAW_SCHEMA), { status: 200 })
        }
        return new Response('Not Found', { status: 404 })
      }) as typeof fetch

      const coordinator = new AgentSessionCoordinator({
        status: 'authenticated',
        profiles: [...SAMPLE_EXISTING_PROFILES, SAMPLE_NEW_PROFILE_SUMMARY],
        selectedProfileId: 'mssql_analytics',
      })

      // Dispatch connection test for mssql_analytics (starts in-flight)
      const testPromise = coordinator.testConnection('mssql_analytics')
      assert.strictEqual(coordinator.getState().connectionTestStatus, 'testing')

      // User switches to sqlite_demo while mssql_analytics test is still pending
      await coordinator.selectProfile('sqlite_demo')
      assert.strictEqual(coordinator.getState().selectedProfileId, 'sqlite_demo')
      assert.strictEqual(coordinator.getState().connectionTestStatus, 'idle')

      // Now the slow mssql_analytics connection test resolves
      resolveSlowTest(
        new Response(
          JSON.stringify({
            connected: true,
            database_name: 'AnalyticsDB',
            latency_ms: 12.5,
            message: 'Connected',
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        )
      )

      const result = await testPromise
      assert.strictEqual(result.connected, true)

      // Guard verification: coordinator state must NOT be overwritten by the delayed test response
      const state = coordinator.getState()
      assert.strictEqual(state.selectedProfileId, 'sqlite_demo')
      assert.strictEqual(state.connectionTestStatus, 'idle')
      assert.strictEqual(state.connectionTestResult, null)
    }
  )

  await t.test(
    'connection test error resolving after profile switch does not overwrite new profile state with failed',
    async () => {
      let rejectSlowTest: (err: Error) => void = () => {}
      const slowPromise = new Promise<Response>((_, reject) => {
        rejectSlowTest = reject
      })

      globalThis.fetch = (async (url: string | URL | Request) => {
        const urlStr = String(url)
        if (urlStr.includes('/test-connection')) {
          return slowPromise
        }
        if (urlStr.includes('/schema')) {
          return new Response(JSON.stringify(SAMPLE_RAW_SCHEMA), { status: 200 })
        }
        return new Response('Not Found', { status: 404 })
      }) as typeof fetch

      const coordinator = new AgentSessionCoordinator({
        status: 'authenticated',
        profiles: [...SAMPLE_EXISTING_PROFILES, SAMPLE_NEW_PROFILE_SUMMARY],
        selectedProfileId: 'mssql_analytics',
      })

      const testPromise = coordinator.testConnection('mssql_analytics')
      assert.strictEqual(coordinator.getState().connectionTestStatus, 'testing')

      // Profile is changed before slow connection test fails
      await coordinator.selectProfile('sqlite_demo')
      assert.strictEqual(coordinator.getState().selectedProfileId, 'sqlite_demo')
      assert.strictEqual(coordinator.getState().connectionTestStatus, 'idle')

      // Now the network connection drops or errors
      rejectSlowTest(new Error('Network connection timeout'))

      await assert.rejects(async () => testPromise, (err: unknown) => {
        assert(err instanceof AgentClientError)
        assert.strictEqual(err.kind, 'disconnected')
        return true
      })

      // Guard verification: coordinator state remains untouched on sqlite_demo
      const state = coordinator.getState()
      assert.strictEqual(state.selectedProfileId, 'sqlite_demo')
      assert.strictEqual(state.connectionTestStatus, 'idle')
      assert.strictEqual(state.connectionTestResult, null)
    }
  )

  await t.test(
    'overlapping connection tests discard older response and preserve newer test result and loading state',
    async () => {
      let resolveTest1: (res: Response) => void = () => {}
      const promise1 = new Promise<Response>((resolve) => {
        resolveTest1 = resolve
      })

      let resolveTest2: (res: Response) => void = () => {}
      const promise2 = new Promise<Response>((resolve) => {
        resolveTest2 = resolve
      })

      let callCount = 0
      globalThis.fetch = (async (url: string | URL | Request) => {
        const urlStr = String(url)
        if (urlStr.includes('/test-connection')) {
          callCount++
          if (callCount === 1) return promise1
          return promise2
        }
        return new Response('Not Found', { status: 404 })
      }) as typeof fetch

      const coordinator = new AgentSessionCoordinator({
        status: 'authenticated',
        profiles: [...SAMPLE_EXISTING_PROFILES, SAMPLE_NEW_PROFILE_SUMMARY],
        selectedProfileId: 'mssql_analytics',
      })

      // Dispatch first test
      const testPromise1 = coordinator.testConnection('mssql_analytics')
      assert.strictEqual(coordinator.getState().connectionTestStatus, 'testing')

      // Dispatch second test before first test resolves
      const testPromise2 = coordinator.testConnection('mssql_analytics')
      assert.strictEqual(coordinator.getState().connectionTestStatus, 'testing')

      // Test 2 finishes first with failure
      resolveTest2(
        new Response(
          JSON.stringify({
            connected: false,
            database_name: 'AnalyticsDB',
            latency_ms: 10.0,
            message: 'Test 2 connection refused',
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        )
      )

      const result2 = await testPromise2
      assert.strictEqual(result2.connected, false)
      assert.strictEqual(coordinator.getState().connectionTestStatus, 'failed')
      assert.strictEqual(coordinator.getState().connectionTestResult?.message, 'Test 2 connection refused')

      // Test 1 finishes later with success
      resolveTest1(
        new Response(
          JSON.stringify({
            connected: true,
            database_name: 'AnalyticsDB',
            latency_ms: 25.0,
            message: 'Test 1 late success',
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        )
      )

      const result1 = await testPromise1
      assert.strictEqual(result1.connected, true)

      // Guard verification: newer Test 2 state is NOT overwritten by late Test 1 response
      const state = coordinator.getState()
      assert.strictEqual(state.connectionTestStatus, 'failed')
      assert.strictEqual(state.connectionTestResult?.message, 'Test 2 connection refused')
    }
  )
})

// ==============================================================================
// 4. Strict Request Sequence, Failure Gating & Session Invalidation
// ==============================================================================

test('Profile Onboarding - Strict Sequence, Failure Gating & Session Invalidation', async (t) => {
  t.afterEach(() => {
    restoreFetch()
  })

  await t.test(
    'complete onboarding workflow executes requests in strict sequential order without premature testing or execution',
    async () => {
      const dispatchedRequests: Array<{ url: string; method: string }> = []

      globalThis.fetch = (async (url: string | URL | Request, options?: RequestInit) => {
        const urlStr = String(url)
        const method = options?.method || 'GET'
        dispatchedRequests.push({ url: urlStr, method })

        if (urlStr.includes('/validate-config') && method === 'POST') {
          return new Response(
            JSON.stringify({ valid: true, message: 'Configuration valid.' }),
            { status: 200, headers: { 'Content-Type': 'application/json' } }
          )
        }

        if (urlStr.includes('/test-connection') && method === 'POST') {
          return new Response(
            JSON.stringify({
              connected: true,
              database_name: 'AnalyticsDB',
              latency_ms: 15.4,
              message: 'Verified',
              failure_code: null,
            } satisfies TestConnectionResponse),
            { status: 200, headers: { 'Content-Type': 'application/json' } }
          )
        }

        if (urlStr.includes('/schema') && method === 'GET') {
          return new Response(JSON.stringify(SAMPLE_RAW_SCHEMA), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          })
        }

        if (urlStr.includes('/v1/profiles') && method === 'POST') {
          return new Response(
            JSON.stringify({
              profile: SAMPLE_NEW_PROFILE_SUMMARY,
              is_default: false,
              message: 'Profile created.',
            }),
            { status: 201, headers: { 'Content-Type': 'application/json' } }
          )
        }

        if (urlStr.includes('/v1/profiles') && method === 'GET') {
          return new Response(
            JSON.stringify({
              profiles: [...SAMPLE_EXISTING_PROFILES, SAMPLE_NEW_PROFILE_SUMMARY],
            }),
            { status: 200, headers: { 'Content-Type': 'application/json' } }
          )
        }

        return new Response('Not Found', { status: 404 })
      }) as typeof fetch

      const coordinator = new AgentSessionCoordinator({
        status: 'authenticated',
        profiles: SAMPLE_EXISTING_PROFILES,
        selectedProfileId: null,
      })

      // Step 1: Validate config
      const valRes = await coordinator.validateConfig(SAMPLE_NEW_PROFILE_CONFIG)
      assert.strictEqual(valRes.valid, true)
      assert.strictEqual(dispatchedRequests.length, 1)
      assert.ok(dispatchedRequests[0].url.includes('/v1/profiles/validate-config'))
      assert.strictEqual(dispatchedRequests[0].method, 'POST')

      // Step 2: Add profile (create + refresh)
      const createRes = await coordinator.addProfile(SAMPLE_NEW_PROFILE_CONFIG)
      assert.strictEqual(createRes.profile.profile_id, 'mssql_analytics')
      assert.strictEqual(dispatchedRequests.length, 3)
      assert.ok(dispatchedRequests[1].url.includes('/v1/profiles'))
      assert.strictEqual(dispatchedRequests[1].method, 'POST')
      assert.ok(dispatchedRequests[2].url.includes('/v1/profiles'))
      assert.strictEqual(dispatchedRequests[2].method, 'GET')

      // Invariant: No connection test or schema loading initiated during creation
      assert.strictEqual(coordinator.getState().selectedProfileId, null)
      assert.strictEqual(coordinator.getState().schemaStatus, 'idle')
      assert.strictEqual(coordinator.getState().connectionTestStatus, 'idle')

      // Step 3: Explicit connection test
      const testRes = await coordinator.testConnection('mssql_analytics')
      assert.strictEqual(testRes.connected, true)
      assert.strictEqual(dispatchedRequests.length, 4)
      assert.ok(dispatchedRequests[3].url.includes('/test-connection'))
      assert.strictEqual(dispatchedRequests[3].method, 'POST')

      // Invariant: Testing connection does NOT select profile or load schema
      assert.strictEqual(coordinator.getState().selectedProfileId, null)
      assert.strictEqual(coordinator.getState().schemaStatus, 'idle')
      assert.strictEqual(coordinator.getState().connectionTestStatus, 'success')

      // Step 4: Explicit profile selection
      await coordinator.selectProfile('mssql_analytics')
      assert.strictEqual(dispatchedRequests.length, 5)
      assert.ok(dispatchedRequests[4].url.includes('/schema'))
      assert.strictEqual(dispatchedRequests[4].method, 'GET')

      // Invariant: Selection loads schema and resets connection test status to idle
      const finalState = coordinator.getState()
      assert.strictEqual(finalState.selectedProfileId, 'mssql_analytics')
      assert.strictEqual(finalState.schemaStatus, 'loaded')
      assert.strictEqual(finalState.connectionTestStatus, 'idle')
      assert.strictEqual(finalState.connectionTestResult, null)
    }
  )

  await t.test(
    'failed connection test preserves profile and isolates subsequent schema introspection failure',
    async () => {
      globalThis.fetch = (async (url: string | URL | Request) => {
        const urlStr = String(url)
        if (urlStr.includes('/test-connection')) {
          return new Response(
            JSON.stringify({
              connected: false,
              database_name: null,
              latency_ms: null,
              message: 'Database does not exist or target is unreachable.',
              failure_code: 'DATABASE_MISMATCH',
            } satisfies TestConnectionResponse),
            { status: 200, headers: { 'Content-Type': 'application/json' } }
          )
        }
        if (urlStr.includes('/schema')) {
          return new Response(
            JSON.stringify({
              error: 'schema_error',
              detail: 'Cannot introspect schema on unreachable database.',
            }),
            { status: 500, headers: { 'Content-Type': 'application/json' } }
          )
        }
        return new Response('Not Found', { status: 404 })
      }) as typeof fetch

      const coordinator = new AgentSessionCoordinator({
        status: 'authenticated',
        profiles: [...SAMPLE_EXISTING_PROFILES, SAMPLE_NEW_PROFILE_SUMMARY],
      })

      // 1. Connection test fails
      const testRes = await coordinator.testConnection('mssql_analytics')
      assert.strictEqual(testRes.connected, false)
      assert.strictEqual(testRes.failure_code, 'DATABASE_MISMATCH')
      assert.strictEqual(coordinator.getState().connectionTestStatus, 'failed')

      // 2. Profile is selected despite failed test
      await coordinator.selectProfile('mssql_analytics')

      // Invariant: Selecting profile clears connectionTestStatus
      const state = coordinator.getState()
      assert.strictEqual(state.connectionTestStatus, 'idle')
      assert.strictEqual(state.connectionTestResult, null)

      // Invariant: Schema status fails closed
      assert.strictEqual(state.schemaStatus, 'error')
      assert.ok(state.schemaError?.includes('Cannot introspect schema'))
      assert.strictEqual(state.schemaContext, null)
    }
  )

  await t.test(
    'clearSelectedProfile and clearConnectionTest clear connection test status and results',
    async () => {
      const coordinator = new AgentSessionCoordinator({
        status: 'authenticated',
        profiles: [...SAMPLE_EXISTING_PROFILES, SAMPLE_NEW_PROFILE_SUMMARY],
        selectedProfileId: 'mssql_analytics',
        connectionTestStatus: 'success',
        connectionTestResult: {
          connected: true,
          database_name: 'AnalyticsDB',
          latency_ms: 12.0,
        },
      })

      coordinator.clearSelectedProfile()
      const state1 = coordinator.getState()
      assert.strictEqual(state1.selectedProfileId, null)
      assert.strictEqual(state1.connectionTestStatus, 'idle')
      assert.strictEqual(state1.connectionTestResult, null)

      // Setting connection test manually and clearing via clearConnectionTest()
      coordinator.clearConnectionTest()
      const state2 = coordinator.getState()
      assert.strictEqual(state2.connectionTestStatus, 'idle')
      assert.strictEqual(state2.connectionTestResult, null)
    }
  )
})
