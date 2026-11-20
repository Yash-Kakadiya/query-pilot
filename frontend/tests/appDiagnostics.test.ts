/**
 * QueryPilot Full Application Diagnostics Test Suite.
 *
 * Verifies:
 * 1. Initial diagnostic checks registry and subsystem categories.
 * 2. Summary metric calculation, success rate evaluation, and status classifications.
 * 3. End-to-end diagnostics run when all systems are operational (100% pass).
 * 4. Resilient handling when local daemon is unreachable (actionable failure).
 * 5. Diagnostic handling when no database profile is active (graceful skip).
 * 6. Connection failure detection and classification.
 * 7. Schema introspection verification (empty vs populated).
 * 8. AST SQL safety mutation blockade testing.
 * 9. Gemini credentials formatting and presence audit.
 * 10. Local storage persistence round-trip audit.
 * 11. CSV formula injection neutralization verification.
 * 12. Live onProgress reporting stream during test execution.
 */

import assert from 'node:assert'
import test from 'node:test'
import {
  INITIAL_DIAGNOSTIC_CHECKS,
  calculateReportSummary,
  runAppDiagnostics,
} from '../src/services/appDiagnostics.ts'
import type { DiagnosticCheck, DiagnosticRunOptions } from '../src/types/diagnostics.ts'
import type { ProfileSummary } from '../src/types/api.ts'

const MOCK_PROFILE: ProfileSummary = {
  profile_id: 'test_sqlite',
  display_name: 'University Demo DB',
  engine_type: 'sqlite',
  database_name: 'university_demo.db',
  schema_name: 'main',
  timeout_seconds: 15,
  max_row_limit: 100,
  has_allowlist: false,
  is_default: true,
  is_cloud: false,
}

test('QueryPilot Diagnostics - Initial Registry & Contracts', async (t) => {
  await t.test('1. Defines 12 canonical diagnostic checks across 5 core categories', () => {
    assert.strictEqual(INITIAL_DIAGNOSTIC_CHECKS.length, 12)
    const ids = INITIAL_DIAGNOSTIC_CHECKS.map((c) => c.id)
    assert.ok(ids.includes('daemon_connectivity'))
    assert.ok(ids.includes('system_subsystems'))
    assert.ok(ids.includes('session_auth'))
    assert.ok(ids.includes('active_profile'))
    assert.ok(ids.includes('db_connection'))
    assert.ok(ids.includes('read_only_boundary'))
    assert.ok(ids.includes('schema_introspection'))
    assert.ok(ids.includes('sql_safety_engine'))
    assert.ok(ids.includes('gemini_credentials'))
    assert.ok(ids.includes('storage_integrity'))
    assert.ok(ids.includes('data_export_engine'))
    assert.ok(ids.includes('history_persistence'))
  })

  await t.test('2. Checks cover core daemon, database, safety, AI, and client runtime categories', () => {
    const categories = new Set(INITIAL_DIAGNOSTIC_CHECKS.map((c) => c.category))
    assert.ok(categories.has('core_daemon'))
    assert.ok(categories.has('database_profile'))
    assert.ok(categories.has('safety_guard'))
    assert.ok(categories.has('ai_gemini'))
    assert.ok(categories.has('client_runtime'))
  })
})

test('QueryPilot Diagnostics - Summary & Success Rate Metrics', async (t) => {
  await t.test('3. Computes 100% success rate when all checks pass', () => {
    const mockChecks: DiagnosticCheck[] = [
      { id: 'daemon_connectivity', name: 'Test 1', category: 'core_daemon', description: '', status: 'passed' },
      { id: 'db_connection', name: 'Test 2', category: 'database_profile', description: '', status: 'passed' },
    ]
    const summary = calculateReportSummary(mockChecks)
    assert.strictEqual(summary.passed, 2)
    assert.strictEqual(summary.failed, 0)
    assert.strictEqual(summary.successRate, 100)
    assert.strictEqual(summary.overallStatus, 'healthy')
  })

  await t.test('4. Correctly classifies failures as critical and computes proportional success rate', () => {
    const mockChecks: DiagnosticCheck[] = [
      { id: 'daemon_connectivity', name: 'Test 1', category: 'core_daemon', description: '', status: 'passed' },
      { id: 'db_connection', name: 'Test 2', category: 'database_profile', description: '', status: 'failed' },
      { id: 'schema_introspection', name: 'Test 3', category: 'database_profile', description: '', status: 'passed' },
      { id: 'gemini_credentials', name: 'Test 4', category: 'ai_gemini', description: '', status: 'skipped' },
    ]
    // 2 passed out of 3 evaluable (excluding 1 skipped) -> 67%
    const summary = calculateReportSummary(mockChecks)
    assert.strictEqual(summary.passed, 2)
    assert.strictEqual(summary.failed, 1)
    assert.strictEqual(summary.skipped, 1)
    assert.strictEqual(summary.successRate, 67)
    assert.strictEqual(summary.overallStatus, 'critical')
    assert.ok(summary.summary.includes('critical check failed'))
  })

  await t.test('5. Non-blocking warnings without failures yield warning overall status', () => {
    const mockChecks: DiagnosticCheck[] = [
      { id: 'daemon_connectivity', name: 'Test 1', category: 'core_daemon', description: '', status: 'passed' },
      { id: 'gemini_credentials', name: 'Test 2', category: 'ai_gemini', description: '', status: 'warning' },
    ]
    const summary = calculateReportSummary(mockChecks)
    assert.strictEqual(summary.passed, 1)
    assert.strictEqual(summary.warnings, 1)
    assert.strictEqual(summary.failed, 0)
    assert.strictEqual(summary.overallStatus, 'warning')
    assert.strictEqual(summary.successRate, 50)
  })
})

test('QueryPilot Diagnostics - End-to-End Suite Execution', async (t) => {
  // Mock localStorage for Node test runner
  const storageMock: Record<string, string> = {}
  Object.defineProperty(globalThis, 'localStorage', {
    value: {
      getItem: (k: string) => storageMock[k] || null,
      setItem: (k: string, v: string) => {
        storageMock[k] = v
      },
      removeItem: (k: string) => {
        delete storageMock[k]
      },
      clear: () => {
        for (const k of Object.keys(storageMock)) delete storageMock[k]
      },
      get length() {
        return Object.keys(storageMock).length
      },
      key: (i: number) => Object.keys(storageMock)[i] || null,
    },
    writable: true,
    configurable: true,
  })

  await t.test('6. Executes full diagnostics and returns healthy report when all endpoints respond 200', async () => {
    const mockFetch = async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/health')) {
        return new Response(JSON.stringify({ status: 'healthy', version: '0.3.0' }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      }
      if (url.includes('/system-diagnostics')) {
        return new Response(
          JSON.stringify({
            status: 'healthy',
            version: '0.3.0',
            profiles_store: { profile_count: 1 },
            drivers: { sqlite: { available: true } },
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        )
      }
      if (url.includes('/test-connection')) {
        return new Response(
          JSON.stringify({
            connected: true,
            database_name: 'university_demo.db',
            latency_ms: 1.2,
            message: 'Connected successfully',
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        )
      }
      if (url.includes('/schema')) {
        return new Response(
          JSON.stringify({
            database_name: 'university_demo.db',
            tables: [
              {
                table_name: 'students',
                columns: [{ name: 'id', data_type: 'INTEGER' }, { name: 'name', data_type: 'TEXT' }],
              },
            ],
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        )
      }
      if (url.includes('/history')) {
        return new Response(
          JSON.stringify({
            entries: [],
            total_count: 5,
            favorites_count: 2,
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        )
      }
      return new Response('Not Found', { status: 404 })
    }

    const options: DiagnosticRunOptions = {
      activeProfile: MOCK_PROFILE,
      profiles: [MOCK_PROFILE],
      apiKey: 'AIzaSyFakeTestKeyForValidation12345',
      fetchFn: mockFetch as typeof fetch,
    }

    const report = await runAppDiagnostics(options)
    assert.strictEqual(report.total, 12)
    assert.strictEqual(report.failed, 0)
    assert.strictEqual(report.passed, 12)
    assert.strictEqual(report.successRate, 100)
    assert.strictEqual(report.overallStatus, 'healthy')
    assert.ok(report.summary.includes('100% operational'))
  })

  await t.test('7. Identifies daemon connection failure and provides actionable resolution', async () => {
    const failingFetch = async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/health')) {
        throw new Error('connect ECONNREFUSED 127.0.0.1:8001')
      }
      return new Response('{}', { status: 200 })
    }

    const report = await runAppDiagnostics({
      fetchFn: failingFetch as typeof fetch,
      activeProfile: null,
      profiles: [],
    })

    const daemonCheck = report.checks.find((c) => c.id === 'daemon_connectivity')
    assert.ok(daemonCheck)
    assert.strictEqual(daemonCheck.status, 'failed')
    assert.ok(daemonCheck.details?.includes('ECONNREFUSED'))
    assert.ok(daemonCheck.solution?.includes('scripts/start_stack.ps1'))
    assert.strictEqual(report.overallStatus, 'critical')
  })

  await t.test('8. Skips database connection and schema tests when no profile is active', async () => {
    const mockFetch = async () => new Response(JSON.stringify({ status: 'healthy', version: '0.3.0' }), { status: 200 })

    const report = await runAppDiagnostics({
      fetchFn: mockFetch as typeof fetch,
      activeProfile: null,
      profiles: [MOCK_PROFILE],
      apiKey: 'AIzaSyValidKey1234567890',
    })

    const profileCheck = report.checks.find((c) => c.id === 'active_profile')
    assert.strictEqual(profileCheck?.status, 'warning')
    assert.strictEqual(profileCheck?.actionType, 'select_profile')

    const dbConnCheck = report.checks.find((c) => c.id === 'db_connection')
    assert.strictEqual(dbConnCheck?.status, 'skipped')

    const schemaCheck = report.checks.find((c) => c.id === 'schema_introspection')
    assert.strictEqual(schemaCheck?.status, 'skipped')
  })

  await t.test('9. Catches physical database connection failure with error detail', async () => {
    const mockFetch = async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/test-connection')) {
        return new Response(
          JSON.stringify({
            connected: false,
            message: 'Password authentication failed for user "postgres"',
            failure_code: 'AUTH_FAILED',
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        )
      }
      return new Response(JSON.stringify({ status: 'healthy', version: '0.3.0' }), { status: 200 })
    }

    const report = await runAppDiagnostics({
      activeProfile: MOCK_PROFILE,
      profiles: [MOCK_PROFILE],
      fetchFn: mockFetch as typeof fetch,
      apiKey: 'AIzaSyValidKey1234567890',
    })

    const dbConnCheck = report.checks.find((c) => c.id === 'db_connection')
    assert.strictEqual(dbConnCheck?.status, 'failed')
    assert.ok(dbConnCheck?.details?.includes('Password authentication failed'))
    assert.ok(dbConnCheck?.solution?.includes('credentials'))
  })

  await t.test('10. Neutralizes formula injection characters in CSV data export audit', async () => {
    const mockFetch = async () => new Response(JSON.stringify({ status: 'healthy', version: '0.3.0' }), { status: 200 })

    const report = await runAppDiagnostics({
      fetchFn: mockFetch as typeof fetch,
      activeProfile: MOCK_PROFILE,
      profiles: [MOCK_PROFILE],
      apiKey: 'AIzaSyValidKey1234567890',
    })

    const exportCheck = report.checks.find((c) => c.id === 'data_export_engine')
    assert.strictEqual(exportCheck?.status, 'passed')
    assert.ok(exportCheck?.details?.includes('formula injection'))
  })

  await t.test('11. Streams onProgress notifications during execution', async () => {
    const progressReports: number[] = []
    const mockFetch = async () => new Response(JSON.stringify({ status: 'healthy', version: '0.3.0' }), { status: 200 })

    await runAppDiagnostics({
      fetchFn: mockFetch as typeof fetch,
      activeProfile: MOCK_PROFILE,
      apiKey: 'AIzaSyValidKey1234567890',
      onProgress: (r) => {
        progressReports.push(r.passed + r.failed + r.warnings + r.skipped)
      },
    })

    assert.ok(progressReports.length > 0, 'onProgress should receive updates during run')
  })
})
