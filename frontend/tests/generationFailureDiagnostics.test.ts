import assert from 'node:assert'
import test from 'node:test'
import { generateSql } from '../src/services/v1ApiClient.ts'
import { SqlGenerationCoordinator } from '../src/services/sqlGeneration.ts'
import type { ModelFacingSchemaContext, GenerateSQLResponse } from '../types/api.ts'

const originalFetch = globalThis.fetch

function restoreFetch(): void {
  globalThis.fetch = originalFetch
}

const SAMPLE_SCHEMA: ModelFacingSchemaContext = {
  database_name: 'university_demo',
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

// ==============================================================================
// 1. Structured Failure Diagnostics Parsing & Contract Preservation
// ==============================================================================

test('Generation Failure Diagnostics - V1 API Contract', async (t) => {
  t.afterEach(() => {
    restoreFetch()
  })

  await t.test('parses structured quota_exhausted failure diagnostic with retry guidance', async () => {
    const quotaExhaustedResponse: GenerateSQLResponse = {
      candidate_sql: null,
      status: 'generation_failed',
      clarification_prompt: null,
      unsupported_reason: null,
      execution_disclaimer: 'Candidate SQL is unexecuted.',
      request_id: 'req-quota-429',
      failure_classification: 'quota_exhausted',
      failure_message: 'The AI model quota has been temporarily exhausted. Please retry later.',
      retry_guidance: 'Quota limit reached. Retry after 12070 seconds.',
      failure_diagnostic: {
        classification: 'quota_exhausted',
        message: 'The AI model quota has been temporarily exhausted. Please retry later.',
        retry_guidance: 'Quota limit reached. Retry after 12070 seconds.',
      },
    }

    globalThis.fetch = (async () => {
      return new Response(JSON.stringify(quotaExhaustedResponse), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    }) as typeof fetch

    const res = await generateSql({
      question: 'Show all courses with credits',
      schema_context: SAMPLE_SCHEMA,
    })

    assert.strictEqual(res.status, 'generation_failed')
    assert.strictEqual(res.candidate_sql, null, 'Candidate SQL must be strictly null on failure')
    assert.strictEqual(res.failure_classification, 'quota_exhausted')
    assert.strictEqual(res.failure_diagnostic?.classification, 'quota_exhausted')
    assert.strictEqual(res.retry_guidance, 'Quota limit reached. Retry after 12070 seconds.')
    assert.strictEqual(res.failure_diagnostic?.retry_guidance, 'Quota limit reached. Retry after 12070 seconds.')
    assert.ok(res.failure_message?.includes('quota has been temporarily exhausted'))
  })

  await t.test('parses authentication_failed diagnostic without retry guidance', async () => {
    const authFailedResponse: GenerateSQLResponse = {
      candidate_sql: null,
      status: 'generation_failed',
      clarification_prompt: null,
      unsupported_reason: null,
      execution_disclaimer: 'Candidate SQL is unexecuted.',
      request_id: 'req-auth-401',
      failure_classification: 'authentication_failed',
      failure_message: 'Authentication with the AI provider failed. Verify the API key configuration.',
      retry_guidance: null,
      failure_diagnostic: {
        classification: 'authentication_failed',
        message: 'Authentication with the AI provider failed. Verify the API key configuration.',
        retry_guidance: null,
      },
    }

    globalThis.fetch = (async () => {
      return new Response(JSON.stringify(authFailedResponse), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    }) as typeof fetch

    const res = await generateSql({
      question: 'Show all courses',
      schema_context: SAMPLE_SCHEMA,
    })

    assert.strictEqual(res.status, 'generation_failed')
    assert.strictEqual(res.candidate_sql, null)
    assert.strictEqual(res.failure_classification, 'authentication_failed')
    assert.strictEqual(res.failure_diagnostic?.classification, 'authentication_failed')
    assert.strictEqual(res.retry_guidance, null)
    assert.strictEqual(res.failure_diagnostic?.retry_guidance, null)
  })

  await t.test('preserves backward compatibility when failure fields are absent in legacy response', async () => {
    const legacyResponse = {
      candidate_sql: null,
      status: 'generation_failed',
      clarification_prompt: null,
      unsupported_reason: null,
      execution_disclaimer: 'Candidate SQL is unexecuted.',
      request_id: 'req-legacy-001',
    }

    globalThis.fetch = (async () => {
      return new Response(JSON.stringify(legacyResponse), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    }) as typeof fetch

    const res = await generateSql({
      question: 'Show courses',
      schema_context: SAMPLE_SCHEMA,
    })

    assert.strictEqual(res.status, 'generation_failed')
    assert.strictEqual(res.candidate_sql, null)
    assert.strictEqual(res.failure_classification, undefined)
    assert.strictEqual(res.failure_diagnostic, undefined)
  })
})

// ==============================================================================
// 2. Coordinator & Execution Boundary Safeguards
// ==============================================================================

test('Generation Failure Diagnostics - Coordinator Invariants & Retry', async (t) => {
  t.afterEach(() => {
    restoreFetch()
  })

  await t.test('generation failure stores diagnostic response and prevents execution', async () => {
    const quotaExhaustedResponse: GenerateSQLResponse = {
      candidate_sql: null,
      status: 'generation_failed',
      clarification_prompt: null,
      unsupported_reason: null,
      execution_disclaimer: 'Candidate SQL is unexecuted.',
      request_id: 'req-quota-1',
      failure_classification: 'quota_exhausted',
      failure_message: 'The AI model quota has been temporarily exhausted.',
      retry_guidance: 'Quota limit reached. Retry after 12070 seconds.',
    }

    globalThis.fetch = (async () => {
      return new Response(JSON.stringify(quotaExhaustedResponse), { status: 200 })
    }) as typeof fetch

    const coordinator = new SqlGenerationCoordinator()
    const res = await coordinator.generateCandidate('Count courses', SAMPLE_SCHEMA, 'sqlite_demo')

    assert.ok(res)
    assert.strictEqual(res.status, 'generation_failed')
    assert.strictEqual(res.candidate_sql, null)
    assert.strictEqual(coordinator.getState().isLoading, false)
    assert.strictEqual(coordinator.getState().response?.status, 'generation_failed')
    assert.strictEqual(coordinator.getState().response?.candidate_sql, null)
    assert.strictEqual(coordinator.getState().response?.failure_classification, 'quota_exhausted')
  })

  await t.test('manual retry allows re-dispatching candidate generation while preventing concurrent duplicates', async () => {
    let callCount = 0

    const quotaResponse: GenerateSQLResponse = {
      candidate_sql: null,
      status: 'generation_failed',
      clarification_prompt: null,
      unsupported_reason: null,
      execution_disclaimer: 'Candidate SQL is unexecuted.',
      request_id: 'req-q-1',
      failure_classification: 'quota_exhausted',
      failure_message: 'Quota exhausted',
    }

    const successResponse: GenerateSQLResponse = {
      candidate_sql: 'SELECT COUNT(*) FROM courses;',
      status: 'generated',
      clarification_prompt: null,
      unsupported_reason: null,
      execution_disclaimer: 'Candidate SQL is unexecuted.',
      request_id: 'req-q-2',
    }

    globalThis.fetch = (async () => {
      callCount++
      if (callCount === 1) {
        return new Response(JSON.stringify(quotaResponse), { status: 200 })
      }
      await new Promise((resolve) => setTimeout(resolve, 20))
      return new Response(JSON.stringify(successResponse), { status: 200 })
    }) as typeof fetch

    const coordinator = new SqlGenerationCoordinator()

    // First attempt fails with quota exhaustion
    const res1 = await coordinator.generateCandidate('Count courses', SAMPLE_SCHEMA, 'sqlite_demo')
    assert.strictEqual(res1?.status, 'generation_failed')
    assert.strictEqual(callCount, 1)

    // Manual retry initiated by user
    const retryPromise = coordinator.generateCandidate('Count courses', SAMPLE_SCHEMA, 'sqlite_demo')
    assert.strictEqual(coordinator.getState().isLoading, true)

    // Concurrent duplicate submission while retry is in flight is rejected
    const dupRes = await coordinator.generateCandidate('Count courses', SAMPLE_SCHEMA, 'sqlite_demo')
    assert.strictEqual(dupRes, null, 'Duplicate in-flight retry request must be blocked')

    // Retry finishes successfully
    const res2 = await retryPromise
    assert.strictEqual(res2?.status, 'generated')
    assert.strictEqual(res2?.candidate_sql, 'SELECT COUNT(*) FROM courses;')
    assert.strictEqual(callCount, 2)
  })
})

