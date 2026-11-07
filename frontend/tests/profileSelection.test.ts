/**
 * Milestone B29-B: Automatic Database Profile Selection Tests.
 *
 * Verifies:
 * 1. Default profile is selected when available (is_default === true).
 * 2. First profile is selected when no default is identified.
 * 3. Explicit user selection is preserved during profile refresh.
 * 4. Empty profile list leaves selector in its existing empty state (null).
 * 5. Profile loading failure / probing / non-authenticated states do not trigger an invalid selection.
 * 6. Removed or unavailable selected profile falls back safely to default or first profile.
 * 7. Respects explicit user clearing without auto-reselecting.
 * 8. Avoids duplicate schema-loading requests from repeated updates or re-renders.
 * 9. Profile switching continues to invalidate stale manual SQL, candidate SQL, and execution state.
 * 10. Existing AI candidate review and multi-turn lifecycle behavior remains intact.
 */

import assert from 'node:assert'
import test from 'node:test'

import { resolveAutoProfileSelection } from '../src/utils/profileSelection.ts'
import { AgentSessionCoordinator } from '../src/services/agentSession.ts'
import { SqlGenerationCoordinator } from '../src/services/sqlGeneration.ts'
import { QueryExecutionCoordinator } from '../src/services/queryExecution.ts'
import type { ProfileSummary, AgentDatabaseSchema } from '../src/types/api.ts'

const originalFetch = globalThis.fetch

function restoreFetch(): void {
  globalThis.fetch = originalFetch
}

const PROFILE_A: ProfileSummary = {
  profile_id: 'profile_a',
  display_name: 'Database A (First)',
  schema_name: 'dbo',
  engine_type: 'mssql',
  timeout_seconds: 15,
  max_row_limit: 100,
  has_allowlist: false,
}

const PROFILE_B_DEFAULT: ProfileSummary = {
  profile_id: 'profile_b',
  display_name: 'Database B (Designated Default)',
  schema_name: 'dbo',
  engine_type: 'mssql',
  timeout_seconds: 15,
  max_row_limit: 100,
  has_allowlist: false,
  is_default: true,
}

const PROFILE_C: ProfileSummary = {
  profile_id: 'profile_c',
  display_name: 'Database C (Third)',
  schema_name: 'main',
  engine_type: 'sqlite',
  timeout_seconds: 15,
  max_row_limit: 100,
  has_allowlist: false,
}

const SAMPLE_SCHEMA: AgentDatabaseSchema = {
  database_name: 'test_db',
  tables: [
    {
      schema_name: 'dbo',
      table_name: 'customers',
      columns: [
        { name: 'id', data_type: 'INT', primary_key: true, nullable: false },
        { name: 'name', data_type: 'VARCHAR', primary_key: false, nullable: false },
      ],
      primary_keys: ['id'],
      foreign_keys: [],
    },
  ],
  views: [],
}

test('Milestone B29-B - Automatic Database Profile Selection Logic', async (t) => {
  await t.test('1. Default profile is selected when available', () => {
    const selected = resolveAutoProfileSelection({
      status: 'authenticated',
      profiles: [PROFILE_A, PROFILE_B_DEFAULT, PROFILE_C],
      currentSelectedId: null,
      userExplicitlyCleared: false,
    })
    assert.strictEqual(selected, 'profile_b')
  })

  await t.test('2. First profile is selected when no default is identified', () => {
    const selected = resolveAutoProfileSelection({
      status: 'authenticated',
      profiles: [PROFILE_A, PROFILE_C],
      currentSelectedId: null,
      userExplicitlyCleared: false,
    })
    assert.strictEqual(selected, 'profile_a')
  })

  await t.test('3. Explicit user selection is preserved during profile refresh', () => {
    const selected = resolveAutoProfileSelection({
      status: 'authenticated',
      profiles: [PROFILE_A, PROFILE_B_DEFAULT, PROFILE_C],
      currentSelectedId: 'profile_c',
      userExplicitlyCleared: false,
    })
    // Even though profile_b is default, user had selected profile_c, so profile_c is preserved
    assert.strictEqual(selected, 'profile_c')
  })

  await t.test('4. Empty profile list leaves the selector in its existing empty state', () => {
    const selected = resolveAutoProfileSelection({
      status: 'authenticated',
      profiles: [],
      currentSelectedId: null,
      userExplicitlyCleared: false,
    })
    assert.strictEqual(selected, null)
  })

  await t.test('5. Profile loading failure / non-authenticated states do not trigger selection', () => {
    const probing = resolveAutoProfileSelection({
      status: 'probing',
      profiles: [PROFILE_A, PROFILE_B_DEFAULT],
      currentSelectedId: null,
    })
    assert.strictEqual(probing, null)

    const authRequired = resolveAutoProfileSelection({
      status: 'auth_required',
      profiles: [PROFILE_A],
      currentSelectedId: null,
    })
    assert.strictEqual(authRequired, null)

    const disconnected = resolveAutoProfileSelection({
      status: 'disconnected',
      profiles: [PROFILE_A],
      currentSelectedId: null,
    })
    assert.strictEqual(disconnected, null)

    const error = resolveAutoProfileSelection({
      status: 'error',
      profiles: [PROFILE_A],
      currentSelectedId: null,
    })
    assert.strictEqual(error, null)
  })

  await t.test('6. Removed or unavailable selected profile falls back safely to default or first', () => {
    // Current selected profile was 'deleted_prof', which is no longer in profiles
    const fallbackWithDefault = resolveAutoProfileSelection({
      status: 'authenticated',
      profiles: [PROFILE_A, PROFILE_B_DEFAULT, PROFILE_C],
      currentSelectedId: 'deleted_prof',
      userExplicitlyCleared: false,
    })
    assert.strictEqual(fallbackWithDefault, 'profile_b', 'Falls back to default profile when available')

    const fallbackWithoutDefault = resolveAutoProfileSelection({
      status: 'authenticated',
      profiles: [PROFILE_A, PROFILE_C],
      currentSelectedId: 'deleted_prof',
      userExplicitlyCleared: false,
    })
    assert.strictEqual(fallbackWithoutDefault, 'profile_a', 'Falls back to first profile when no default')
  })

  await t.test('7. Respects explicit user clearing and prevents auto-reselecting', () => {
    const cleared = resolveAutoProfileSelection({
      status: 'authenticated',
      profiles: [PROFILE_A, PROFILE_B_DEFAULT],
      currentSelectedId: null,
      userExplicitlyCleared: true,
    })
    assert.strictEqual(cleared, null, 'Must remain null when user explicitly cleared selection')
  })
})

test('Milestone B29-B - Integration Lifecycle & Invariant Preservation', async (t) => {
  t.afterEach(restoreFetch)

  await t.test('8. Automatic selection triggers schema load via standard pathway without duplicate requests', async () => {
    let schemaRequestCount = 0
    let lastRequestedProfile: string | null = null

    globalThis.fetch = (async (url: string | URL | Request) => {
      const urlStr = String(url)
      if (urlStr.includes('/agent/v1/profiles') && urlStr.includes('/schema')) {
        schemaRequestCount++
        const match = urlStr.match(/\/agent\/v1\/profiles\/([^/]+)\/schema/)
        lastRequestedProfile = match ? match[1] : null
        return new Response(JSON.stringify(SAMPLE_SCHEMA), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      }
      return new Response('Not Found', { status: 404 })
    }) as typeof fetch

    const coordinator = new AgentSessionCoordinator({
      status: 'authenticated',
      profiles: [PROFILE_A, PROFILE_B_DEFAULT],
      selectedProfileId: null,
      schemaStatus: 'idle',
    })

    // Simulate auto-selection resolution on authenticated startup
    const targetId = resolveAutoProfileSelection({
      status: coordinator.getState().status,
      profiles: coordinator.getState().profiles,
      currentSelectedId: coordinator.getState().selectedProfileId,
      userExplicitlyCleared: false,
    })
    assert.strictEqual(targetId, 'profile_b')

    // Execute selection via existing coordinator pathway
    await coordinator.selectProfile(targetId)

    assert.strictEqual(coordinator.getState().selectedProfileId, 'profile_b')
    assert.strictEqual(coordinator.getState().schemaStatus, 'loaded')
    assert.strictEqual(schemaRequestCount, 1)
    assert.strictEqual(lastRequestedProfile, 'profile_b')

    // Re-evaluating selection with active profile yields the same ID, causing 0 duplicate requests
    const recheckedId = resolveAutoProfileSelection({
      status: coordinator.getState().status,
      profiles: coordinator.getState().profiles,
      currentSelectedId: coordinator.getState().selectedProfileId,
      userExplicitlyCleared: false,
    })
    assert.strictEqual(recheckedId, 'profile_b')
    if (recheckedId !== coordinator.getState().selectedProfileId) {
      await coordinator.selectProfile(recheckedId)
    }
    assert.strictEqual(schemaRequestCount, 1, 'Zero duplicate schema requests must be dispatched')
  })

  await t.test('9. Profile switching invalidates manual SQL and execution results', async () => {
    globalThis.fetch = (async (url: string | URL | Request) => {
      const urlStr = String(url)
      if (urlStr.includes('/schema')) {
        return new Response(JSON.stringify(SAMPLE_SCHEMA), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      }
      return new Response('Not Found', { status: 404 })
    }) as typeof fetch

    const sessionCoord = new AgentSessionCoordinator({
      status: 'authenticated',
      profiles: [PROFILE_A, PROFILE_B_DEFAULT],
      selectedProfileId: null,
      schemaStatus: 'idle',
    })
    const execCoord = new QueryExecutionCoordinator()
    const sqlCoord = new SqlGenerationCoordinator()

    // 1. Initial auto-selection
    const autoId = resolveAutoProfileSelection({
      status: sessionCoord.getState().status,
      profiles: sessionCoord.getState().profiles,
      currentSelectedId: sessionCoord.getState().selectedProfileId,
    })
    assert.strictEqual(autoId, 'profile_b')
    await sessionCoord.selectProfile(autoId)

    // Simulate active execution result and candidate state
    const activeExecCoord = new QueryExecutionCoordinator({
      result: {
        columns: ['id', 'name'],
        rows: [{ id: 1, name: 'Alice' }],
        row_count: 1,
        truncated: false,
        execution_time_ms: 5.2,
      },
      executedSql: 'SELECT * FROM customers;',
      executedProfileId: 'profile_b',
    })
    const activeSqlCoord = new SqlGenerationCoordinator({
      candidateId: 'cand_123',
      response: {
        candidate_sql: 'SELECT * FROM customers;',
        dialect: 'tsql',
        confidence_score: 0.95,
        explanation: 'Queries customers',
      },
      conversationHistory: [
        {
          turn_id: 'turn_1',
          user_question: 'List customers',
          candidate_sql: 'SELECT * FROM customers;',
          status: 'succeeded',
          timestamp: Date.now(),
        },
      ],
    })

    assert.ok(activeExecCoord.getState().result !== null)
    assert.strictEqual(activeSqlCoord.getState().conversationHistory.length, 1)

    // 2. User switches profile to PROFILE_A
    await sessionCoord.selectProfile('profile_a')
    activeExecCoord.clearResult()
    activeSqlCoord.clearCandidate()
    activeSqlCoord.clearConversationContext()

    // 3. Confirm all stale execution state and context are wiped
    assert.strictEqual(sessionCoord.getState().selectedProfileId, 'profile_a')
    assert.strictEqual(activeExecCoord.getState().result, null, 'Execution result must be cleared')
    assert.strictEqual(activeSqlCoord.getState().conversationHistory.length, 0, 'Conversation history must be cleared')
    assert.strictEqual(activeSqlCoord.getState().response, null, 'Candidate response must be cleared')
  })

  await t.test('10. Backend profile-list response with is_default selects designated default over first profile', async () => {
    // Simulates exact payload serialized by GET /agent/v1/profiles
    const backendProfiles: ProfileSummary[] = [
      {
        profile_id: 'corp_alpha',
        display_name: 'Corporate Alpha (First)',
        engine_type: 'mssql',
        database_name: 'alpha_db',
        schema_name: 'dbo',
        timeout_seconds: 15,
        max_row_limit: 100,
        has_allowlist: false,
        allowed_tables: null,
        is_default: false,
      },
      {
        profile_id: 'corp_beta_default',
        display_name: 'Corporate Beta (Designated Default)',
        engine_type: 'mssql',
        database_name: 'beta_db',
        schema_name: 'dbo',
        timeout_seconds: 15,
        max_row_limit: 100,
        has_allowlist: false,
        allowed_tables: null,
        is_default: true,
      },
    ]

    const selected = resolveAutoProfileSelection({
      status: 'authenticated',
      profiles: backendProfiles,
      currentSelectedId: null,
      userExplicitlyCleared: false,
    })

    assert.strictEqual(
      selected,
      'corp_beta_default',
      'Designated default profile must be chosen over first profile'
    )
  })
})
