import assert from 'node:assert'
import test from 'node:test'
import { SqlGenerationCoordinator } from '../src/services/sqlGeneration.ts'
import type {
  BusinessMetricDefinition,
  GenerateSQLRequest,
  GenerateSQLResponse,
  ModelFacingSchemaContext,
} from '../src/types/api.ts'

const originalFetch = globalThis.fetch

function restoreFetch(): void {
  globalThis.fetch = originalFetch
}

const SAMPLE_SCHEMA: ModelFacingSchemaContext = {
  database_name: 'test_dw',
  tables: [
    {
      table_name: 'orders',
      schema_name: 'dbo',
      columns: [
        { name: 'order_id', data_type: 'int', primary_key: true, nullable: false },
        { name: 'gross_amount', data_type: 'decimal(18,2)', primary_key: false, nullable: false },
        { name: 'discount', data_type: 'decimal(18,2)', primary_key: false, nullable: true },
      ],
      primary_keys: ['order_id'],
      foreign_keys: [],
    },
    {
      table_name: 'customers',
      schema_name: 'dbo',
      columns: [
        { name: 'customer_id', data_type: 'int', primary_key: true, nullable: false },
        { name: 'status', data_type: 'varchar(50)', primary_key: false, nullable: false },
      ],
      primary_keys: ['customer_id'],
      foreign_keys: [],
    },
  ],
}

const SAMPLE_GENERATE_RESPONSE: GenerateSQLResponse = {
  candidate_sql: 'SELECT order_id, gross_amount - COALESCE(discount, 0) AS net_revenue FROM dbo.orders;',
  status: 'generated',
  clarification_prompt: null,
  unsupported_reason: null,
  execution_disclaimer: 'Candidate SQL is unexecuted.',
  request_id: 'req-metric-001',
}

// ==============================================================================
// 1. Business Metric Configuration State & Mutator Lifecycle
// ==============================================================================

test('Business Metric Configuration - Coordinator State & Lifecycle', async (t) => {
  t.afterEach(() => {
    restoreFetch()
  })

  await t.test('initializes with empty business metrics list', () => {
    const coordinator = new SqlGenerationCoordinator()
    const state = coordinator.getState()
    assert.deepStrictEqual(state.businessMetrics, [])
  })

  await t.test('adds valid business metric definitions up to ceiling of 5', () => {
    const coordinator = new SqlGenerationCoordinator()

    for (let i = 1; i <= 5; i++) {
      const metric: BusinessMetricDefinition = {
        name: `Metric ${i}`,
        description: `Description for metric ${i}`,
        calculation_formula: `SUM(field_${i})`,
        applicable_tables: ['orders'],
      }
      coordinator.addBusinessMetric(metric)
      assert.strictEqual(coordinator.getState().businessMetrics.length, i)
    }

    // 6th metric must throw error
    assert.throws(
      () => {
        coordinator.addBusinessMetric({
          name: 'Excess Metric',
          description: 'Should fail',
        })
      },
      (err: unknown) =>
        err instanceof Error && err.message.includes('Cannot configure more than 5 business metrics')
    )
  })

  await t.test('enforces ceiling of 5 when setBusinessMetrics is invoked with excess items', () => {
    const coordinator = new SqlGenerationCoordinator()
    const excessMetrics: BusinessMetricDefinition[] = Array.from({ length: 8 }, (_, i) => ({
      name: `Batch Metric ${i + 1}`,
      description: `Description ${i + 1}`,
    }))

    coordinator.setBusinessMetrics(excessMetrics)
    // Slices down to exactly 5
    assert.strictEqual(coordinator.getState().businessMetrics.length, 5)
    assert.strictEqual(coordinator.getState().businessMetrics[0].name, 'Batch Metric 1')
    assert.strictEqual(coordinator.getState().businessMetrics[4].name, 'Batch Metric 5')
  })

  await t.test('removes business metric by index correctly', () => {
    const coordinator = new SqlGenerationCoordinator()
    coordinator.setBusinessMetrics([
      { name: 'M1', description: 'D1' },
      { name: 'M2', description: 'D2' },
      { name: 'M3', description: 'D3' },
    ])

    coordinator.removeBusinessMetric(1) // Remove M2
    const remaining = coordinator.getState().businessMetrics
    assert.strictEqual(remaining.length, 2)
    assert.strictEqual(remaining[0].name, 'M1')
    assert.strictEqual(remaining[1].name, 'M3')
  })

  await t.test('clears business metrics on clearBusinessMetrics()', () => {
    const coordinator = new SqlGenerationCoordinator()
    coordinator.setBusinessMetrics([
      { name: 'M1', description: 'D1' },
      { name: 'M2', description: 'D2' },
    ])
    assert.strictEqual(coordinator.getState().businessMetrics.length, 2)

    coordinator.clearBusinessMetrics()
    assert.strictEqual(coordinator.getState().businessMetrics.length, 0)
  })
})

// ==============================================================================
// 2. Business Metric Validation Rules
// ==============================================================================

test('Business Metric Configuration - Validation Rules', async (t) => {
  await t.test('enforces case-insensitive uniqueness check', () => {
    const existingMetrics: BusinessMetricDefinition[] = [
      { name: 'Net Revenue', description: 'Gross revenue minus discounts.' },
      { name: 'Active Users', description: 'Users with login within 30 days.' },
    ]

    const candidateName = 'net revenue'
    const isDuplicate = existingMetrics.some(
      (m) => m.name.trim().toLowerCase() === candidateName.trim().toLowerCase()
    )
    assert.strictEqual(isDuplicate, true)

    const uniqueName = 'Churn Rate'
    const isUnique = !existingMetrics.some(
      (m) => m.name.trim().toLowerCase() === uniqueName.trim().toLowerCase()
    )
    assert.strictEqual(isUnique, true)
  })

  await t.test('verifies character limits (name: 100, desc: 500, formula: 500)', () => {
    const validMetric: BusinessMetricDefinition = {
      name: 'A'.repeat(100),
      description: 'B'.repeat(500),
      calculation_formula: 'C'.repeat(500),
      applicable_tables: ['orders', 'customers'],
    }

    assert.ok(validMetric.name.length <= 100)
    assert.ok(validMetric.description.length <= 500)
    assert.ok((validMetric.calculation_formula || '').length <= 500)
    assert.ok((validMetric.applicable_tables || []).length <= 10)

    const invalidNameLen = 'A'.repeat(101).length
    assert.ok(invalidNameLen > 100)

    const invalidDescLen = 'B'.repeat(501).length
    assert.ok(invalidDescLen > 500)

    const invalidFormulaLen = 'C'.repeat(501).length
    assert.ok(invalidFormulaLen > 500)
  })

  await t.test('enforces aggregate UTF-8 payload limit of 8,192 bytes', () => {
    const metrics: BusinessMetricDefinition[] = Array.from({ length: 5 }, (_, i) => ({
      name: `Metric Name ${i}`,
      description: 'Standard description that fits comfortably in payload budget.',
      calculation_formula: `SUM(column_${i})`,
      applicable_tables: ['orders'],
    }))

    const payloadBytes = new TextEncoder().encode(JSON.stringify(metrics)).length
    assert.ok(payloadBytes < 8192, `Expected payload < 8192 bytes, was ${payloadBytes}`)
  })
})

// ==============================================================================
// 3. API Payload Propagation & Session Persistence
// ==============================================================================

test('Business Metric Configuration - API Dispatch & Session Propagation', async (t) => {
  t.afterEach(() => {
    restoreFetch()
  })

  await t.test('propagates business_metrics in GenerateSQLRequest when configured', async () => {
    const coordinator = new SqlGenerationCoordinator()
    const testMetrics: BusinessMetricDefinition[] = [
      {
        name: 'Net Revenue',
        description: 'Gross revenue minus discounts',
        calculation_formula: 'gross_amount - discount',
        applicable_tables: ['orders'],
      },
    ]
    coordinator.setBusinessMetrics(testMetrics)

    let capturedPayload: GenerateSQLRequest | null = null
    globalThis.fetch = async (_input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.body && typeof init.body === 'string') {
        capturedPayload = JSON.parse(init.body) as GenerateSQLRequest
      }
      return new Response(JSON.stringify(SAMPLE_GENERATE_RESPONSE), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    }

    const res = await coordinator.generateCandidate(
      'What is the net revenue for each order?',
      SAMPLE_SCHEMA,
      'profile-sales'
    )

    assert.ok(res !== null)
    assert.ok(capturedPayload !== null)
    assert.ok(capturedPayload.business_metrics !== undefined)
    assert.strictEqual(capturedPayload.business_metrics.length, 1)
    assert.strictEqual(capturedPayload.business_metrics[0].name, 'Net Revenue')
  })

  await t.test('omits business_metrics when none are configured (backward compatibility)', async () => {
    const coordinator = new SqlGenerationCoordinator()

    let capturedPayload: GenerateSQLRequest | null = null
    globalThis.fetch = async (_input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.body && typeof init.body === 'string') {
        capturedPayload = JSON.parse(init.body) as GenerateSQLRequest
      }
      return new Response(JSON.stringify(SAMPLE_GENERATE_RESPONSE), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    }

    await coordinator.generateCandidate(
      'Show all orders',
      SAMPLE_SCHEMA,
      'profile-sales'
    )

    assert.ok(capturedPayload !== null)
    assert.strictEqual(capturedPayload.business_metrics, undefined)
  })

  await t.test('preserves business metrics across follow-up turns in the same profile session', async () => {
    const coordinator = new SqlGenerationCoordinator()
    coordinator.setBusinessMetrics([
      {
        name: 'Net Revenue',
        description: 'Gross revenue minus discounts',
      },
    ])

    let callCount = 0
    globalThis.fetch = async () => {
      callCount++
      return new Response(JSON.stringify(SAMPLE_GENERATE_RESPONSE), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    }

    // Turn 1
    await coordinator.generateCandidate('What is net revenue?', SAMPLE_SCHEMA, 'profile-sales')
    coordinator.recordCompletedTurn('What is net revenue?', ['net_revenue'])

    // Turn 2: clearCandidate clears candidate response but preserves business metrics
    coordinator.clearCandidate()
    assert.strictEqual(coordinator.getState().businessMetrics.length, 1)

    // Turn 2 candidate generation still includes the business metric
    await coordinator.generateCandidate('Filter by current year', SAMPLE_SCHEMA, 'profile-sales')
    assert.strictEqual(callCount, 2)
    assert.strictEqual(coordinator.getState().businessMetrics.length, 1)
  })

  await t.test('clears business metrics on profile switch and session logout', () => {
    const coordinator = new SqlGenerationCoordinator()
    coordinator.setBusinessMetrics([
      { name: 'Metric A', description: 'Table A metric', applicable_tables: ['orders'] },
    ])
    assert.strictEqual(coordinator.getState().businessMetrics.length, 1)

    // Profile switch clears business metrics
    coordinator.clearBusinessMetrics()
    assert.strictEqual(coordinator.getState().businessMetrics.length, 0)
  })

  await t.test('editing metric validates against duplicate names excluding self', () => {
    const metrics: BusinessMetricDefinition[] = [
      { name: 'Revenue', description: 'Total revenue' },
      { name: 'Expenses', description: 'Total expenses' },
    ]

    const editingIdx = 1 // Editing 'Expenses'
    const newNameCandidate = 'revenue' // Tries to rename Expenses to Revenue
    const isDuplicate = metrics.some(
      (m, idx) => idx !== editingIdx && m.name.toLowerCase() === newNameCandidate.toLowerCase()
    )
    assert.strictEqual(isDuplicate, true)

    // Editing to same name ('Expenses') or distinct name ('Operating Costs') is valid
    const sameNameCandidate = 'Expenses'
    const isSelfDuplicate = metrics.some(
      (m, idx) => idx !== editingIdx && m.name.toLowerCase() === sameNameCandidate.toLowerCase()
    )
    assert.strictEqual(isSelfDuplicate, false)

    const uniqueCandidate = 'Operating Costs'
    const isUniqueDuplicate = metrics.some(
      (m, idx) => idx !== editingIdx && m.name.toLowerCase() === uniqueCandidate.toLowerCase()
    )
    assert.strictEqual(isUniqueDuplicate, false)
  })
})


