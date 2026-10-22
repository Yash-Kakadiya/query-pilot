import assert from 'node:assert'
import test from 'node:test'
import {
  AgentSessionCoordinator,
} from '../src/services/agentSession.ts'
import type { AgentDatabaseSchema, ProfileSummary } from '../types/api.ts'

const originalFetch = globalThis.fetch

function restoreFetch(): void {
  globalThis.fetch = originalFetch
}

const SAMPLE_PROFILES: ProfileSummary[] = [
  {
    profile_id: 'sqlite_demo',
    display_name: 'SQLite Demo Database',
    schema_name: 'main',
    timeout_seconds: 30,
    max_row_limit: 1000,
    has_allowlist: false,
    allowed_tables: null,
  },
  {
    profile_id: 'postgres_analytics',
    display_name: 'PostgreSQL Analytics Warehouse',
    schema_name: 'analytics',
    timeout_seconds: 60,
    max_row_limit: 5000,
    has_allowlist: true,
    allowed_tables: ['monthly_sales', 'customers'],
  },
]

const SAMPLE_RAW_SCHEMA: AgentDatabaseSchema = {
  database_name: 'demo_db',
  tables: [
    {
      schema_name: 'main',
      table_name: 'courses',
      columns: [
        { name: 'course_id', data_type: 'INTEGER', primary_key: true, nullable: false },
        { name: 'title', data_type: 'TEXT', nullable: false },
        { name: 'department', data_type: 'TEXT', nullable: true },
      ],
      primary_keys: ['course_id'],
      foreign_keys: [],
    },
    {
      schema_name: 'main',
      table_name: 'students',
      columns: [
        { name: 'student_id', data_type: 'INTEGER', primary_key: true, nullable: false },
        { name: 'full_name', data_type: 'TEXT', nullable: false },
        { name: 'gpa', data_type: 'REAL', nullable: true },
      ],
      primary_keys: ['student_id'],
      foreign_keys: [],
    },
  ],
}

// ==============================================================================
// 1. Authenticated Profile List Loading & Empty Profile List
// ==============================================================================

test('Profile Workflow - Profile Discovery & Empty List Handling', async (t) => {
  t.afterEach(() => {
    restoreFetch()
  })

  await t.test('authenticated profile list loading populates profiles in memory with idle schema state', async () => {
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
    await coordinator.probeSession()

    const state = coordinator.getState()
    assert.strictEqual(state.status, 'authenticated')
    assert.strictEqual(state.profiles.length, 2)
    assert.strictEqual(state.profiles[0].profile_id, 'sqlite_demo')
    assert.strictEqual(state.profiles[1].profile_id, 'postgres_analytics')
    assert.strictEqual(state.selectedProfileId, null)
    assert.strictEqual(state.selectedProfile, null)
    assert.strictEqual(state.schemaStatus, 'idle')
    assert.strictEqual(state.schemaContext, null)
    assert.strictEqual(state.rawSchema, null)
  })

  await t.test('empty profile list maintains clean state without crashing', async () => {
    globalThis.fetch = (async (url: string | URL | Request) => {
      const urlStr = String(url)
      if (urlStr.includes('/v1/profiles')) {
        return new Response(JSON.stringify({ profiles: [] }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      }
      return new Response('Not Found', { status: 404 })
    }) as typeof fetch

    const coordinator = new AgentSessionCoordinator()
    await coordinator.probeSession()

    const state = coordinator.getState()
    assert.strictEqual(state.status, 'authenticated')
    assert.strictEqual(state.profiles.length, 0)
    assert.strictEqual(state.selectedProfileId, null)
    assert.strictEqual(state.selectedProfile, null)
    assert.strictEqual(state.schemaStatus, 'idle')
    assert.strictEqual(state.schemaContext, null)
  })
})

// ==============================================================================
// 2. Profile Selection & Schema Loading
// ==============================================================================

test('Profile Workflow - Profile Selection & Schema Introspection', async (t) => {
  t.afterEach(() => {
    restoreFetch()
  })

  await t.test('selecting a profile introspects and projects schema into ModelFacingSchemaContext', async () => {
    const requestedUrls: string[] = []

    globalThis.fetch = (async (url: string | URL | Request) => {
      const urlStr = String(url)
      requestedUrls.push(urlStr)

      if (urlStr.includes('/v1/profiles/sqlite_demo/schema')) {
        return new Response(JSON.stringify(SAMPLE_RAW_SCHEMA), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      }
      return new Response('Not Found', { status: 404 })
    }) as typeof fetch

    const coordinator = new AgentSessionCoordinator({
      status: 'authenticated',
      profiles: SAMPLE_PROFILES,
    })

    await coordinator.selectProfile('sqlite_demo')

    assert.ok(requestedUrls.some((u) => u.includes('/v1/profiles/sqlite_demo/schema')))

    const state = coordinator.getState()
    assert.strictEqual(state.selectedProfileId, 'sqlite_demo')
    assert.strictEqual(state.selectedProfile?.display_name, 'SQLite Demo Database')
    assert.strictEqual(state.schemaStatus, 'loaded')
    assert.strictEqual(state.schemaError, undefined)

    // Verify ModelFacingSchemaContext projection
    assert.ok(state.schemaContext)
    assert.strictEqual(state.schemaContext.database_name, 'demo_db')
    assert.strictEqual(state.schemaContext.tables.length, 2)
    assert.strictEqual(state.schemaContext.tables[0].table_name, 'courses')
    assert.strictEqual(state.schemaContext.tables[0].columns.length, 3)
    assert.strictEqual(state.schemaContext.tables[1].table_name, 'students')

    // Verify raw schema retained for metadata presentation
    assert.ok(state.rawSchema)
    assert.strictEqual(state.rawSchema.tables.length, 2)
  })

  await t.test('switching profiles clears the previous schema immediately', async () => {
    let schemaResolve: (res: Response) => void
    const schemaPromise = new Promise<Response>((resolve) => {
      schemaResolve = resolve
    })

    globalThis.fetch = (async (url: string | URL | Request) => {
      const urlStr = String(url)
      if (urlStr.includes('/v1/profiles/postgres_analytics/schema')) {
        return schemaPromise
      }
      if (urlStr.includes('/v1/profiles/sqlite_demo/schema')) {
        return new Response(JSON.stringify(SAMPLE_RAW_SCHEMA), { status: 200 })
      }
      return new Response('Not Found', { status: 404 })
    }) as typeof fetch

    const coordinator = new AgentSessionCoordinator({
      status: 'authenticated',
      profiles: SAMPLE_PROFILES,
    })

    // 1. Load initial profile
    await coordinator.selectProfile('sqlite_demo')
    assert.strictEqual(coordinator.getState().schemaStatus, 'loaded')
    assert.ok(coordinator.getState().schemaContext)

    // 2. Switch to second profile (in-flight)
    const switchPromise = coordinator.selectProfile('postgres_analytics')

    // Verify immediate state clearing before fetch completes
    const intermediateState = coordinator.getState()
    assert.strictEqual(intermediateState.selectedProfileId, 'postgres_analytics')
    assert.strictEqual(intermediateState.schemaStatus, 'loading')
    assert.strictEqual(intermediateState.schemaContext, null, 'Previous schema MUST be cleared immediately')
    assert.strictEqual(intermediateState.rawSchema, null, 'Previous raw schema MUST be cleared immediately')

    // 3. Resolve second profile fetch
    schemaResolve!(
      new Response(
        JSON.stringify({
          database_name: 'analytics_db',
          tables: [
            {
              schema_name: 'analytics',
              table_name: 'monthly_sales',
              columns: [{ name: 'sale_id', data_type: 'INT', primary_key: true }],
            },
          ],
        }),
        { status: 200 }
      )
    )

    await switchPromise

    const finalState = coordinator.getState()
    assert.strictEqual(finalState.selectedProfileId, 'postgres_analytics')
    assert.strictEqual(finalState.schemaStatus, 'loaded')
    assert.strictEqual(finalState.schemaContext?.database_name, 'analytics_db')
    assert.strictEqual(finalState.schemaContext?.tables[0].table_name, 'monthly_sales')
  })

  await t.test('clearing profile selection resets schema and profile state to idle', async () => {
    globalThis.fetch = (async () => {
      return new Response(JSON.stringify(SAMPLE_RAW_SCHEMA), { status: 200 })
    }) as typeof fetch

    const coordinator = new AgentSessionCoordinator({
      status: 'authenticated',
      profiles: SAMPLE_PROFILES,
    })

    await coordinator.selectProfile('sqlite_demo')
    assert.strictEqual(coordinator.getState().schemaStatus, 'loaded')

    // User clears selection (passes null or empty string)
    await coordinator.selectProfile(null)

    const state = coordinator.getState()
    assert.strictEqual(state.selectedProfileId, null)
    assert.strictEqual(state.selectedProfile, null)
    assert.strictEqual(state.schemaStatus, 'idle')
    assert.strictEqual(state.schemaContext, null)
    assert.strictEqual(state.rawSchema, null)
  })
})

// ==============================================================================
// 3. Error Handling & Retry Behavior
// ==============================================================================

test('Profile Workflow - Error Handling & Retry Behavior', async (t) => {
  t.afterEach(() => {
    restoreFetch()
  })

  await t.test('schema load error transitions to schema error state and supports retry', async () => {
    let attemptCount = 0

    globalThis.fetch = (async (url: string | URL | Request) => {
      const urlStr = String(url)
      if (urlStr.includes('/v1/profiles/sqlite_demo/schema')) {
        attemptCount++
        if (attemptCount === 1) {
          // First attempt fails with 500
          return new Response(
            JSON.stringify({ error: 'DB_ERROR', detail: 'Database connection failed during introspection.' }),
            { status: 500, headers: { 'Content-Type': 'application/json' } }
          )
        }
        // Second attempt succeeds
        return new Response(JSON.stringify(SAMPLE_RAW_SCHEMA), { status: 200 })
      }
      return new Response('Not Found', { status: 404 })
    }) as typeof fetch

    const coordinator = new AgentSessionCoordinator({
      status: 'authenticated',
      profiles: SAMPLE_PROFILES,
    })

    // First attempt
    await coordinator.selectProfile('sqlite_demo')

    const errorState = coordinator.getState()
    assert.strictEqual(errorState.selectedProfileId, 'sqlite_demo')
    assert.strictEqual(errorState.schemaStatus, 'error')
    assert.strictEqual(errorState.schemaContext, null)
    assert.ok(errorState.schemaError?.includes('Database connection failed'))

    // Retry
    await coordinator.retryLoadSchema()

    const retryState = coordinator.getState()
    assert.strictEqual(retryState.schemaStatus, 'loaded')
    assert.ok(retryState.schemaContext)
    assert.strictEqual(retryState.schemaError, undefined)
    assert.strictEqual(attemptCount, 2)
  })

  await t.test('authentication becoming invalid (401) during schema request transitions to auth_required', async () => {
    globalThis.fetch = (async (url: string | URL | Request) => {
      const urlStr = String(url)
      if (urlStr.includes('/v1/profiles/sqlite_demo/schema')) {
        return new Response(
          JSON.stringify({ error: 'UNAUTHORIZED', detail: 'Session has expired. Please re-authenticate.' }),
          { status: 401, headers: { 'Content-Type': 'application/json' } }
        )
      }
      return new Response('Not Found', { status: 404 })
    }) as typeof fetch

    const coordinator = new AgentSessionCoordinator({
      status: 'authenticated',
      profiles: SAMPLE_PROFILES,
    })

    await coordinator.selectProfile('sqlite_demo')

    const state = coordinator.getState()
    assert.strictEqual(state.status, 'auth_required', 'Session MUST transition to auth_required on 401')
    assert.strictEqual(state.isModalOpen, true, 'Auth modal MUST open when session expires during schema load')
    assert.strictEqual(state.profiles.length, 0)
    assert.strictEqual(state.selectedProfileId, null)
    assert.strictEqual(state.schemaStatus, 'idle')
    assert.strictEqual(state.schemaContext, null)
  })

  await t.test('daemon disconnect during schema request marks disconnected status', async () => {
    globalThis.fetch = (async () => {
      throw new TypeError('Failed to fetch: Connection refused')
    }) as typeof fetch

    const coordinator = new AgentSessionCoordinator({
      status: 'authenticated',
      profiles: SAMPLE_PROFILES,
    })

    await coordinator.selectProfile('sqlite_demo')

    const state = coordinator.getState()
    assert.strictEqual(state.status, 'disconnected')
    assert.strictEqual(state.schemaStatus, 'error')
    assert.ok(state.schemaError?.includes('Unable to connect') || state.schemaError?.includes('loopback'))
  })
})

// ==============================================================================
// 4. Stale Schema Response Protection & Non-Persistence
// ==============================================================================

test('Profile Workflow - Stale-Response Protection & Non-Persistence Safeguards', async (t) => {
  t.afterEach(() => {
    restoreFetch()
  })

  await t.test('stale schema responses after profile switching are dropped', async () => {
    let delayedResolve: (res: Response) => void
    const delayedPromise = new Promise<Response>((resolve) => {
      delayedResolve = resolve
    })

    globalThis.fetch = (async (url: string | URL | Request) => {
      const urlStr = String(url)
      if (urlStr.includes('/v1/profiles/sqlite_demo/schema')) {
        // Slow profile A
        return delayedPromise
      }
      if (urlStr.includes('/v1/profiles/postgres_analytics/schema')) {
        // Fast profile B
        return new Response(
          JSON.stringify({
            database_name: 'analytics_db',
            tables: [
              {
                schema_name: 'analytics',
                table_name: 'monthly_sales',
                columns: [{ name: 'id', data_type: 'INT' }],
              },
            ],
          }),
          { status: 200 }
        )
      }
      return new Response('Not Found', { status: 404 })
    }) as typeof fetch

    const coordinator = new AgentSessionCoordinator({
      status: 'authenticated',
      profiles: SAMPLE_PROFILES,
    })

    // 1. User selects Profile A (will hang on delayedPromise)
    const profileAPromise = coordinator.selectProfile('sqlite_demo')

    // 2. User quickly switches to Profile B before Profile A finishes
    await coordinator.selectProfile('postgres_analytics')

    assert.strictEqual(coordinator.getState().selectedProfileId, 'postgres_analytics')
    assert.strictEqual(coordinator.getState().schemaContext?.database_name, 'analytics_db')

    // 3. Profile A's response arrives late
    delayedResolve!(new Response(JSON.stringify(SAMPLE_RAW_SCHEMA), { status: 200 }))
    await profileAPromise

    // 4. State must STILL be Profile B, not overwritten by stale Profile A
    const finalState = coordinator.getState()
    assert.strictEqual(finalState.selectedProfileId, 'postgres_analytics')
    assert.strictEqual(finalState.schemaContext?.database_name, 'analytics_db')
    assert.strictEqual(finalState.schemaContext?.tables[0].table_name, 'monthly_sales')
  })

  await t.test('stale schema response after logout is dropped', async () => {
    let delayedResolve: (res: Response) => void
    const delayedPromise = new Promise<Response>((resolve) => {
      delayedResolve = resolve
    })

    globalThis.fetch = (async (url: string | URL | Request) => {
      const urlStr = String(url)
      if (urlStr.includes('/v1/profiles/sqlite_demo/schema')) {
        return delayedPromise
      }
      if (urlStr.includes('/v1/auth/logout')) {
        return new Response(JSON.stringify({ status: 'ok' }), { status: 200 })
      }
      return new Response('Not Found', { status: 404 })
    }) as typeof fetch

    const coordinator = new AgentSessionCoordinator({
      status: 'authenticated',
      profiles: SAMPLE_PROFILES,
    })

    // 1. Select profile (hangs)
    const schemaPromise = coordinator.selectProfile('sqlite_demo')

    // 2. User logs out
    await coordinator.logout()

    assert.strictEqual(coordinator.getState().status, 'auth_required')
    assert.strictEqual(coordinator.getState().selectedProfileId, null)
    assert.strictEqual(coordinator.getState().schemaStatus, 'idle')

    // 3. Stale schema arrives
    delayedResolve!(new Response(JSON.stringify(SAMPLE_RAW_SCHEMA), { status: 200 }))
    await schemaPromise

    // 4. State must remain logged out and empty
    const finalState = coordinator.getState()
    assert.strictEqual(finalState.status, 'auth_required')
    assert.strictEqual(finalState.selectedProfileId, null)
    assert.strictEqual(finalState.schemaContext, null)
    assert.strictEqual(finalState.rawSchema, null)
  })

  await t.test('profiles and schema are never persisted in localStorage or sessionStorage', async () => {
    globalThis.fetch = (async (url: string | URL | Request) => {
      const urlStr = String(url)
      if (urlStr.includes('/v1/profiles/sqlite_demo/schema')) {
        return new Response(JSON.stringify(SAMPLE_RAW_SCHEMA), { status: 200 })
      }
      return new Response('Not Found', { status: 404 })
    }) as typeof fetch

    const coordinator = new AgentSessionCoordinator({
      status: 'authenticated',
      profiles: SAMPLE_PROFILES,
    })

    await coordinator.selectProfile('sqlite_demo')

    assert.strictEqual(coordinator.getState().schemaStatus, 'loaded')

    if (typeof localStorage !== 'undefined') {
      assert.strictEqual(localStorage.getItem('selected_profile'), null)
      assert.strictEqual(localStorage.getItem('schema'), null)
      assert.strictEqual(localStorage.getItem('profiles'), null)
    }
    if (typeof sessionStorage !== 'undefined') {
      assert.strictEqual(sessionStorage.getItem('selected_profile'), null)
      assert.strictEqual(sessionStorage.getItem('schema'), null)
      assert.strictEqual(sessionStorage.getItem('profiles'), null)
    }
  })
})
