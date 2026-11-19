import { test } from 'node:test'
import assert from 'node:assert/strict'
import { resolveDiagnostic } from '../src/services/errorDiagnostics.ts'

test('Error Diagnostics - Gemini API Key missing or rejected', () => {
  const diag = resolveDiagnostic('Error: API_KEY_INVALID 400')
  assert.equal(diag.category, 'api_key')
  assert.ok(diag.title.includes('Key'))
  assert.equal(diag.primary_action?.action_type, 'open_external_url')
  assert.ok(diag.primary_action?.payload?.includes('aistudio.google.com'))
})

test('Error Diagnostics - Gemini Rate Limit (429)', () => {
  const diag = resolveDiagnostic('RESOURCE_EXHAUSTED: 429 Rate limit exceeded')
  assert.equal(diag.category, 'model_quota')
  assert.ok(diag.title.includes('Rate Limit'))
  assert.equal(diag.primary_action?.action_type, 'retry')
  assert.equal(diag.secondary_action?.action_type, 'switch_model')
})

test('Error Diagnostics - Database Connection Refused', () => {
  const diag = resolveDiagnostic('Connection refused (0x0000274D/10061)', { server: '127.0.0.1', port: 5432 })
  assert.equal(diag.category, 'db_connection_refused')
  assert.ok(diag.summary.includes('5432'))
  assert.equal(diag.primary_action?.action_type, 'open_profile_modal')
})

test('Error Diagnostics - Database Authentication Failed', () => {
  const diag = resolveDiagnostic('password authentication failed for user "postgres"')
  assert.equal(diag.category, 'db_auth_failed')
  assert.ok(diag.title.includes('Authentication Failed'))
  assert.equal(diag.primary_action?.action_type, 'open_profile_modal')
})

test('Error Diagnostics - Cloud SSL Required', () => {
  const diag = resolveDiagnostic('no pg_hba.conf entry for host, no encryption, SSL off')
  assert.equal(diag.category, 'db_ssl_required')
  assert.ok(diag.title.includes('SSL'))
  assert.equal(diag.primary_action?.payload, 'ssl_enable')
})

test('Error Diagnostics - Read-Only Safety Violation', () => {
  const diag = resolveDiagnostic("SQLMutationError: Operation 'DELETE' is prohibited in read-only mode.")
  assert.equal(diag.category, 'db_safety_mutation')
  assert.ok(diag.title.includes('Modification Blocked'))
})

test('Error Diagnostics - Respects Server-Provided Diagnostic', () => {
  const serverDiagnostic = {
    category: 'api_key' as const,
    title: 'Server Classified Key Error',
    summary: 'From backend',
    root_cause: 'Explicit backend diagnosis',
    actionable_steps: ['Step 1'],
  }
  const diag = resolveDiagnostic({ diagnostic: serverDiagnostic })
  assert.equal(diag.title, 'Server Classified Key Error')
  assert.equal(diag.summary, 'From backend')
})
