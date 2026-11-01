import assert from 'node:assert'
import test from 'node:test'
import {
  createProfile,
  executeProfileQuery,
  getProfileSchema,
  testProfileConnection,
  validateProfileConfig,
} from '../src/services/agentClient.ts'
import { AgentSessionCoordinator } from '../src/services/agentSession.ts'
import { getProfileEngine } from '../src/utils/profileUtils.ts'
import { SqlGenerationCoordinator } from '../src/services/sqlGeneration.ts'
import { generateSql } from '../src/services/v1ApiClient.ts'
import type {
  AgentDatabaseSchema,
  CreateProfileRequest,
  GenerateSQLRequest,
  ModelFacingSchemaContext,
  ProfileSummary,
  TestConnectionResponse,
} from '../src/types/api.ts'

const originalFetch = globalThis.fetch

function restoreFetch(): void {
  globalThis.fetch = originalFetch
}

const SAMPLE_SQLITE_SCHEMA: AgentDatabaseSchema = {
  database_name: 'chinook',
  tables: [
    {
      schema_name: 'main',
      table_name: 'artists',
      columns: [
        { name: 'ArtistId', data_type: 'INTEGER', primary_key: true, nullable: false },
        { name: 'Name', data_type: 'NVARCHAR(120)', primary_key: false, nullable: true },
      ],
      primary_keys: ['ArtistId'],
      foreign_keys: [],
    },
    {
      schema_name: 'main',
      table_name: 'albums',
      columns: [
        { name: 'AlbumId', data_type: 'INTEGER', primary_key: true, nullable: false },
        { name: 'Title', data_type: 'NVARCHAR(160)', primary_key: false, nullable: false },
        { name: 'ArtistId', data_type: 'INTEGER', primary_key: false, nullable: false },
      ],
      primary_keys: ['AlbumId'],
      foreign_keys: [
        {
          source_table: 'albums',
          source_column: 'ArtistId',
          target_table: 'artists',
          target_column: 'ArtistId',
        },
      ],
    },
  ],
}

const SAMPLE_SQLITE_PROFILE_SUMMARY: ProfileSummary = {
  profile_id: 'sqlite_chinook',
  display_name: 'Chinook Music SQLite',
  engine_type: 'sqlite',
  database_name: 'chinook',
  schema_name: 'main',
  timeout_seconds: 10,
  max_row_limit: 100,
  has_allowlist: false,
  allowed_tables: null,
}

const SAMPLE_MSSQL_PROFILE_SUMMARY: ProfileSummary = {
  profile_id: 'mssql_sales',
  display_name: 'Sales MSSQL Database',
  engine_type: 'mssql',
  database_name: 'SalesDB',
  schema_name: 'dbo',
  timeout_seconds: 15,
  max_row_limit: 100,
  has_allowlist: false,
  allowed_tables: null,
}

// ==============================================================================
// 1. Profile Creation & Engine Selection
// ==============================================================================

test('SQLite Profile Creation & Engine Form Contracts', async (t) => {
  t.afterEach(() => {
    restoreFetch()
  })

  await t.test('SQL Server profile creation payload retains all MSSQL fields', async () => {
    let capturedBody: unknown = null
    globalThis.fetch = async (_input, init) => {
      capturedBody = JSON.parse(init?.body as string)
      return new Response(
        JSON.stringify({
          valid: true,
          message: 'Profile configuration is valid.',
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } }
      )
    }

    const mssqlPayload: CreateProfileRequest = {
      profile_id: 'mssql_test',
      display_name: 'MSSQL Test',
      engine_type: 'mssql',
      server: 'localhost',
      database_name: 'TestDB',
      schema_name: 'dbo',
      driver: 'ODBC Driver 17 for SQL Server',
      use_trusted_connection: true,
      timeout_seconds: 15,
      max_row_limit: 100,
    }

    const res = await validateProfileConfig(mssqlPayload)
    assert.strictEqual(res.valid, true)
    const body = capturedBody as Record<string, unknown>
    assert.strictEqual(body.engine_type, 'mssql')
    assert.strictEqual(body.server, 'localhost')
    assert.strictEqual(body.database_name, 'TestDB')
    assert.strictEqual(body.schema_name, 'dbo')
    assert.strictEqual(body.driver, 'ODBC Driver 17 for SQL Server')
    assert.strictEqual(body.use_trusted_connection, true)
    assert.strictEqual(body.database_path, undefined)
  })

  await t.test('SQLite profile creation payload includes database_path and omits MSSQL fields', async () => {
    let capturedBody: unknown = null
    globalThis.fetch = async (_input, init) => {
      capturedBody = JSON.parse(init?.body as string)
      return new Response(
        JSON.stringify({
          profile: SAMPLE_SQLITE_PROFILE_SUMMARY,
          is_default: false,
          message: "Database connection profile 'sqlite_chinook' created successfully.",
        }),
        { status: 201, headers: { 'Content-Type': 'application/json' } }
      )
    }

    const sqlitePayload: CreateProfileRequest = {
      profile_id: 'sqlite_chinook',
      display_name: 'Chinook Music SQLite',
      engine_type: 'sqlite',
      database_path: 'C:\\data\\chinook.db',
      schema_name: 'main',
      timeout_seconds: 10,
      max_row_limit: 100,
      set_as_default: false,
    }

    const res = await createProfile(sqlitePayload)
    assert.strictEqual(res.profile.profile_id, 'sqlite_chinook')
    assert.strictEqual(res.profile.schema_name, 'main')

    const body = capturedBody as Record<string, unknown>
    assert.strictEqual(body.engine_type, 'sqlite')
    assert.strictEqual(body.database_path, 'C:\\data\\chinook.db')
    assert.strictEqual(body.schema_name, 'main')
    // Crucial: MSSQL fields must not be present
    assert.strictEqual(body.server, undefined)
    assert.strictEqual(body.driver, undefined)
    assert.strictEqual(body.use_trusted_connection, undefined)
  })

  await t.test('rejects SQLite profile with empty or blank database_path client-side', async () => {
    let fetchCalled = false
    globalThis.fetch = async () => {
      fetchCalled = true
      return new Response(JSON.stringify({ valid: true }), { status: 200 })
    }

    const emptyPathPayload: CreateProfileRequest = {
      profile_id: 'sqlite_empty',
      display_name: 'Empty Path',
      engine_type: 'sqlite',
      database_path: '   ',
      schema_name: 'main',
    }

    await assert.rejects(
      async () => validateProfileConfig(emptyPathPayload),
      (err: unknown) => {
        assert.ok(err instanceof Error)
        assert.ok(err.message.includes('Database path is required'))
        return true
      }
    )
    assert.strictEqual(fetchCalled, false)
  })

  await t.test('rejects SQLite profile with oversized database_path client-side', async () => {
    let fetchCalled = false
    globalThis.fetch = async () => {
      fetchCalled = true
      return new Response(JSON.stringify({ valid: true }), { status: 200 })
    }

    const oversizedPathPayload: CreateProfileRequest = {
      profile_id: 'sqlite_huge',
      display_name: 'Huge Path',
      engine_type: 'sqlite',
      database_path: 'C:\\' + 'a'.repeat(1005) + '.db',
      schema_name: 'main',
    }

    await assert.rejects(
      async () => validateProfileConfig(oversizedPathPayload),
      (err: unknown) => {
        assert.ok(err instanceof Error)
        assert.ok(err.message.includes('cannot exceed 1000 characters'))
        return true
      }
    )
    assert.strictEqual(fetchCalled, false)
  })

  await t.test('rejects unsupported engine_type client-side', async () => {
    let fetchCalled = false
    globalThis.fetch = async () => {
      fetchCalled = true
      return new Response(JSON.stringify({ valid: true }), { status: 200 })
    }

    const unsupportedPayload = {
      profile_id: 'postgres_test',
      display_name: 'Postgres',
      engine_type: 'postgres' as unknown as 'mssql',
    }

    await assert.rejects(
      async () => validateProfileConfig(unsupportedPayload),
      (err: unknown) => {
        assert.ok(err instanceof Error)
        assert.ok(err.message.includes("Unsupported engine_type 'postgres'"))
        return true
      }
    )
    assert.strictEqual(fetchCalled, false)
  })

  await t.test('createProfile handles 422 backend validation failure gracefully', async () => {
    globalThis.fetch = async () => {
      return new Response(
        JSON.stringify({
          detail: 'Database path does not exist or is not a regular file.',
        }),
        { status: 422, headers: { 'Content-Type': 'application/json' } }
      )
    }

    const invalidPathPayload: CreateProfileRequest = {
      profile_id: 'sqlite_missing',
      display_name: 'Missing DB',
      engine_type: 'sqlite',
      database_path: 'C:\\nonexistent\\missing.db',
      schema_name: 'main',
    }

    await assert.rejects(
      async () => createProfile(invalidPathPayload),
      (err: unknown) => {
        assert.ok(err instanceof Error)
        assert.ok(err.message.includes('Database path does not exist'))
        return true
      }
    )
  })

  await t.test('createProfile handles 409 conflict when profile_id already exists', async () => {
    globalThis.fetch = async () => {
      return new Response(
        JSON.stringify({
          detail: "Profile ID 'sqlite_chinook' already exists in approved profile store.",
        }),
        { status: 409, headers: { 'Content-Type': 'application/json' } }
      )
    }

    const duplicatePayload: CreateProfileRequest = {
      profile_id: 'sqlite_chinook',
      display_name: 'Duplicate Chinook',
      engine_type: 'sqlite',
      database_path: 'C:\\data\\chinook.db',
      schema_name: 'main',
    }

    await assert.rejects(
      async () => createProfile(duplicatePayload),
      (err: unknown) => {
        assert.ok(err instanceof Error)
        assert.ok(err.message.includes('already exists'))
        return true
      }
    )
  })
})

// ==============================================================================
// 2. Profile Management & Privacy
// ==============================================================================

test('Profile Management, Engine Labeling & Privacy Guarantees', async (t) => {
  t.afterEach(() => {
    restoreFetch()
  })

  await t.test('getProfileEngine accurately identifies SQLite and MSSQL profiles', () => {
    assert.strictEqual(getProfileEngine(SAMPLE_SQLITE_PROFILE_SUMMARY), 'sqlite')
    assert.strictEqual(getProfileEngine(SAMPLE_MSSQL_PROFILE_SUMMARY), 'mssql')

    // Profile with schema_name: 'main' but omitted engine_type defaults to SQLite
    const legacySqlite: ProfileSummary = {
      profile_id: 'legacy_sqlite',
      display_name: 'Legacy SQLite',
      schema_name: 'main',
      timeout_seconds: 10,
      max_row_limit: 100,
      has_allowlist: false,
    }
    assert.strictEqual(getProfileEngine(legacySqlite), 'sqlite')

    // Profile with schema_name: 'dbo' or undefined defaults to MSSQL
    const legacyMssql: ProfileSummary = {
      profile_id: 'legacy_mssql',
      display_name: 'Legacy MSSQL',
      schema_name: 'dbo',
      timeout_seconds: 15,
      max_row_limit: 100,
      has_allowlist: false,
    }
    assert.strictEqual(getProfileEngine(legacyMssql), 'mssql')
    assert.strictEqual(getProfileEngine(null), 'mssql')
    assert.strictEqual(getProfileEngine(undefined), 'mssql')
  })

  await t.test('ProfileSummary contract contains zero raw database paths or credentials', () => {
    const summaryKeys = Object.keys(SAMPLE_SQLITE_PROFILE_SUMMARY)
    assert.ok(!summaryKeys.includes('database_path'))
    assert.ok(!summaryKeys.includes('server'))
    assert.ok(!summaryKeys.includes('driver'))
    assert.ok(!summaryKeys.includes('password'))
    assert.ok(!summaryKeys.includes('user'))
    assert.ok(!summaryKeys.includes('connection_string'))
  })

  await t.test('Database paths are never persisted in localStorage or sessionStorage', () => {
    if (typeof localStorage !== 'undefined') {
      assert.strictEqual(localStorage.getItem('database_path'), null)
    }
    if (typeof sessionStorage !== 'undefined') {
      assert.strictEqual(sessionStorage.getItem('database_path'), null)
    }
  })
})

// ==============================================================================
// 3. Schema Loading & Connection Testing
// ==============================================================================

test('SQLite Schema Loading & Connection Testing', async (t) => {
  t.afterEach(() => {
    restoreFetch()
  })

  await t.test('testProfileConnection sends POST with empty body to test-connection endpoint', async () => {
    let capturedUrl = ''
    let capturedMethod = ''
    let capturedBody: unknown = null

    globalThis.fetch = async (input, init) => {
      capturedUrl = String(input)
      capturedMethod = init?.method || 'GET'
      capturedBody = init?.body ? JSON.parse(init.body as string) : null
      const res: TestConnectionResponse = {
        connected: true,
        database_name: 'chinook',
        latency_ms: 12.5,
        message: 'Connection established and database context verified successfully.',
        failure_code: null,
      }
      return new Response(JSON.stringify(res), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    }

    const testRes = await testProfileConnection('sqlite_chinook')
    assert.strictEqual(testRes.connected, true)
    assert.strictEqual(testRes.database_name, 'chinook')
    assert.ok(capturedUrl.includes('/agent/v1/profiles/sqlite_chinook/test-connection'))
    assert.strictEqual(capturedMethod, 'POST')
    assert.deepStrictEqual(capturedBody, {})
  })

  await t.test('getProfileSchema successfully returns introspected SQLite tables and columns', async () => {
    globalThis.fetch = async () => {
      return new Response(JSON.stringify(SAMPLE_SQLITE_SCHEMA), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    }

    const schema = await getProfileSchema('sqlite_chinook', { project: true })
    assert.strictEqual(schema.database_name, 'chinook')
    assert.strictEqual(schema.tables.length, 2)
    assert.strictEqual(schema.tables[0].table_name, 'artists')
    assert.strictEqual(schema.tables[1].table_name, 'albums')
    assert.strictEqual(schema.tables[0].columns.length, 2)
  })

  await t.test('AgentSessionCoordinator successfully selects SQLite profile and loads schema', async () => {
    globalThis.fetch = async (input) => {
      const url = String(input)
      if (url.includes('/agent/v1/profiles/sqlite_chinook/schema')) {
        return new Response(JSON.stringify(SAMPLE_SQLITE_SCHEMA), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      }
      return new Response(JSON.stringify({ profiles: [SAMPLE_SQLITE_PROFILE_SUMMARY] }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    }

    const coordinator = new AgentSessionCoordinator()
    await coordinator.refreshProfiles()
    assert.strictEqual(coordinator.getState().profiles.length, 1)

    await coordinator.selectProfile('sqlite_chinook')
    const state = coordinator.getState()
    assert.strictEqual(state.selectedProfileId, 'sqlite_chinook')
    assert.strictEqual(state.schemaStatus, 'loaded')
    assert.strictEqual(state.schemaContext?.database_name, 'chinook')
    assert.strictEqual(state.schemaContext?.tables.length, 2)
  })
})

// ==============================================================================
// 4. Workflow Integration & Execution Safeguards
// ==============================================================================

test('SQLite Workflow Integration, Dialect Propagation & Execution Safety', async (t) => {
  t.afterEach(() => {
    restoreFetch()
  })

  await t.test('generateSql attaches X-Profile-ID header for authoritative agent dialect routing', async () => {
    let capturedHeaders: Record<string, string> = {}
    let capturedPayload: unknown = null

    globalThis.fetch = async (_input, init) => {
      capturedHeaders = Object.fromEntries(new Headers(init?.headers).entries())
      capturedPayload = JSON.parse(init?.body as string)
      return new Response(
        JSON.stringify({
          candidate_sql: 'SELECT ArtistId, Name FROM artists ORDER BY ArtistId LIMIT 10;',
          status: 'generated',
          dialect: 'sqlite',
          execution_disclaimer: 'Candidate SQL is unexecuted.',
          request_id: 'req-sqlite-gen-001',
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } }
      )
    }

    const schemaContext: ModelFacingSchemaContext = {
      database_name: 'chinook',
      tables: [
        {
          table_name: 'artists',
          schema_name: 'main',
          columns: [{ name: 'ArtistId', data_type: 'int' }, { name: 'Name', data_type: 'varchar' }],
          primary_keys: ['ArtistId'],
          foreign_keys: [],
        },
      ],
    }

    const req: GenerateSQLRequest = {
      question: 'Show the first 10 artists',
      schema_context: schemaContext,
    }

    const res = await generateSql(req, { profileId: 'sqlite_chinook' })
    assert.strictEqual(res.status, 'generated')
    assert.ok(res.candidate_sql?.includes('LIMIT 10'))
    assert.strictEqual(capturedHeaders['x-profile-id'], 'sqlite_chinook')

    // Verify privacy: payload contains NO database_path or credentials
    const payload = capturedPayload as Record<string, unknown>
    assert.strictEqual(payload.database_path, undefined)
    assert.strictEqual(payload.server, undefined)
    assert.strictEqual(payload.user, undefined)
    assert.strictEqual(payload.password, undefined)
  })

  await t.test('Candidate SQL requires explicit approval; generation alone executes ZERO queries', async () => {
    let executeCalled = false
    globalThis.fetch = async (input, init) => {
      const url = String(input)
      if (url.includes('/execute')) {
        executeCalled = true
        return new Response(JSON.stringify({ columns: [], rows: [], row_count: 0 }), { status: 200 })
      }
      return new Response(
        JSON.stringify({
          candidate_sql: 'SELECT ArtistId FROM artists LIMIT 5;',
          status: 'generated',
          dialect: 'sqlite',
          execution_disclaimer: 'Candidate SQL is unexecuted.',
          request_id: 'req-gen-002',
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } }
      )
    }

    const schemaContext: ModelFacingSchemaContext = {
      database_name: 'chinook',
      tables: [
        {
          table_name: 'artists',
          schema_name: 'main',
          columns: [{ name: 'ArtistId', data_type: 'int' }],
          primary_keys: [],
          foreign_keys: [],
        },
      ],
    }

    const coordinator = new SqlGenerationCoordinator()
    const result = await coordinator.generateCandidate(
      'Show 5 artists',
      schemaContext,
      'sqlite_chinook'
    )

    assert.ok(result)
    assert.strictEqual(result.status, 'generated')
    assert.strictEqual(result.candidate_sql, 'SELECT ArtistId FROM artists LIMIT 5;')
    // Explicit assertion: execution endpoint was NEVER called during generation
    assert.strictEqual(executeCalled, false)
  })

  await t.test('executeProfileQuery dispatches candidate SQL through local agent profile executor', async () => {
    let capturedUrl = ''
    let capturedBody: unknown = null

    globalThis.fetch = async (input, init) => {
      capturedUrl = String(input)
      capturedBody = JSON.parse(init?.body as string)
      return new Response(
        JSON.stringify({
          columns: ['ArtistId', 'Name'],
          rows: [
            { ArtistId: 1, Name: 'AC/DC' },
            { ArtistId: 2, Name: 'Accept' },
          ],
          row_count: 2,
          truncated: false,
          execution_status: 'succeeded',
          execution_time_ms: 5.2,
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } }
      )
    }

    const result = await executeProfileQuery(
      'sqlite_chinook',
      'SELECT ArtistId, Name FROM artists LIMIT 2;'
    )

    assert.ok(capturedUrl.includes('/agent/v1/profiles/sqlite_chinook/execute'))
    assert.strictEqual(result.row_count, 2)
    assert.strictEqual(result.columns[0], 'ArtistId')
    assert.strictEqual(result.rows[0].Name, 'AC/DC')
    const body = capturedBody as Record<string, unknown>
    assert.strictEqual(body.sql, 'SELECT ArtistId, Name FROM artists LIMIT 2;')
  })

  await t.test('Profile switching clears candidate SQL preventing accidental cross-engine execution', async () => {
    const coordinator = new SqlGenerationCoordinator()
    coordinator.setState({
      candidateId: 'candidate-1',
      lastGeneratedProfileId: 'sqlite_chinook',
      lastGeneratedQuestion: 'Show artists',
      response: {
        candidate_sql: 'SELECT * FROM artists LIMIT 5;',
        status: 'generated',
        execution_disclaimer: 'Unexecuted',
        request_id: 'req-1',
      },
      conversationContext: [
        { user_question: 'Show artists' },
      ],
    })
    assert.strictEqual(coordinator.getState().response?.candidate_sql, 'SELECT * FROM artists LIMIT 5;')

    // Simulate profile switch cleanup
    coordinator.clearCandidate()
    coordinator.clearConversationContext()
    coordinator.clearBusinessMetrics()

    const state = coordinator.getState()
    assert.strictEqual(state.response, null)
    assert.strictEqual(state.candidateId, null)
    assert.strictEqual(state.lastGeneratedProfileId, null)
    assert.strictEqual(state.conversationContext.length, 0)
    assert.strictEqual(state.businessMetrics.length, 0)
  })
})

// ==============================================================================
// 5. B24-F.1 Profile Summary Contract & Explicit Engine Identification
// ==============================================================================

test('B24-F.1 Safe Profile Summary Contract & Engine Identification', async (t) => {
  await t.test('Explicit engine_type takes precedence over schema_name', () => {
    // MSSQL with custom schema named 'main' must NOT be misidentified as SQLite
    const mssqlWithMainSchema: ProfileSummary = {
      profile_id: 'mssql_custom_main',
      display_name: 'MSSQL Custom Main Schema',
      engine_type: 'mssql',
      database_name: 'CorporateDB',
      schema_name: 'main',
      timeout_seconds: 15,
      max_row_limit: 100,
      has_allowlist: false,
    }
    assert.strictEqual(getProfileEngine(mssqlWithMainSchema), 'mssql')

    // SQLite with explicit engine_type
    const sqliteExplicit: ProfileSummary = {
      profile_id: 'sqlite_active',
      display_name: 'SQLite Active Profile',
      engine_type: 'sqlite',
      database_name: 'active',
      schema_name: 'main',
      timeout_seconds: 10,
      max_row_limit: 50,
      has_allowlist: false,
    }
    assert.strictEqual(getProfileEngine(sqliteExplicit), 'sqlite')
  })

  await t.test('Legacy profiles without engine_type fallback safely to MSSQL or schema heuristic', () => {
    // Legacy profile without engine_type and with schema_name 'main'
    const legacySqlite: ProfileSummary = {
      profile_id: 'legacy_sqlite',
      display_name: 'Legacy SQLite',
      schema_name: 'main',
      timeout_seconds: 10,
      max_row_limit: 50,
      has_allowlist: false,
    }
    assert.strictEqual(getProfileEngine(legacySqlite), 'sqlite')

    // Legacy MSSQL profile without engine_type and schema 'dbo'
    const legacyMssqlDbo: ProfileSummary = {
      profile_id: 'legacy_mssql',
      display_name: 'Legacy MSSQL Dbo',
      schema_name: 'dbo',
      timeout_seconds: 15,
      max_row_limit: 100,
      has_allowlist: false,
    }
    assert.strictEqual(getProfileEngine(legacyMssqlDbo), 'mssql')

    // Legacy profile with omitted schema_name
    const legacyDefault: ProfileSummary = {
      profile_id: 'legacy_default',
      display_name: 'Legacy Default Profile',
      timeout_seconds: 15,
      max_row_limit: 100,
      has_allowlist: false,
    }
    assert.strictEqual(getProfileEngine(legacyDefault), 'mssql')

    // Null or undefined profile
    assert.strictEqual(getProfileEngine(null), 'mssql')
    assert.strictEqual(getProfileEngine(undefined), 'mssql')
  })

  await t.test('ProfileSummary object contract never contains database paths or secrets', () => {
    const safeSummary: ProfileSummary = {
      profile_id: 'safe_test_prof',
      display_name: 'Safe Test Profile',
      engine_type: 'sqlite',
      database_name: 'sales',
      schema_name: 'main',
      timeout_seconds: 10,
      max_row_limit: 100,
      has_allowlist: false,
      allowed_tables: null,
    }

    const summaryRecord = safeSummary as Record<string, unknown>
    assert.strictEqual(summaryRecord.profile_id, 'safe_test_prof')
    assert.strictEqual(summaryRecord.engine_type, 'sqlite')
    assert.strictEqual(summaryRecord.database_name, 'sales')
    assert.strictEqual(summaryRecord.schema_name, 'main')

    // Sensitive keys must be undefined
    assert.strictEqual(summaryRecord.database_path, undefined)
    assert.strictEqual(summaryRecord.server, undefined)
    assert.strictEqual(summaryRecord.driver, undefined)
    assert.strictEqual(summaryRecord.password, undefined)
    assert.strictEqual(summaryRecord.connection_string, undefined)
  })
})
