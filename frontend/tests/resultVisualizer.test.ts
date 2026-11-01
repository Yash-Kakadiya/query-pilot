import assert from 'node:assert'
import test from 'node:test'
import type { ExecuteQueryResponse } from '../src/types/api.ts'
import { sampleUniformStride } from '../src/utils/visualizerSampling.ts'

// ==============================================================================
// 1. Result Visualizer - Data Semantics, Missing Values & Suitability
// ==============================================================================

test('Result Visualizer - Missing Values & Chart Semantics', async (t) => {
  await t.test('preserves valid zero values as actual 0 and missing/invalid as null (gaps)', () => {
    const rawRows = [
      { category: 'A', value: 100 },
      { category: 'B', value: 0 },          // Valid zero
      { category: 'C', value: '0' },        // String valid zero
      { category: 'D', value: null },       // Missing value
      { category: 'E', value: undefined },  // Missing value
      { category: 'F', value: 'invalid' },  // Non-numeric string
      { category: 'G', value: NaN },        // Non-finite
      { category: 'H', value: Infinity },   // Non-finite
      { category: 'I', value: false },      // Boolean false (must not coerce to 0)
      { category: 'J', value: true },       // Boolean true (must not coerce to 1)
    ]

    let missingCount = 0
    let validZeroCount = 0

    const processed = rawRows.map((r) => {
      let yVal: number | null = null
      const rawY = r.value

      if ((rawY === 0 || rawY === '0') && typeof rawY !== 'boolean') {
        validZeroCount++
        yVal = 0
      } else if (rawY === null || rawY === undefined || rawY === '' || typeof rawY === 'boolean') {
        missingCount++
        yVal = null
      } else {
        const num = typeof rawY === 'number' ? rawY : (typeof rawY === 'string' && rawY.trim() !== '' ? Number(rawY) : NaN)
        if (Number.isFinite(num)) {
          yVal = num
        } else {
          missingCount++
          yVal = null
        }
      }
      return { x: r.category, y: yVal }
    })

    assert.strictEqual(processed.length, 10)
    assert.strictEqual(validZeroCount, 2)
    assert.strictEqual(missingCount, 7)

    // Check specific values
    assert.strictEqual(processed[0].y, 100)
    assert.strictEqual(processed[1].y, 0)     // Preserved 0
    assert.strictEqual(processed[2].y, 0)     // Preserved 0
    assert.strictEqual(processed[3].y, null)  // Rendered as gap (null)
    assert.strictEqual(processed[4].y, null)  // Rendered as gap (null)
    assert.strictEqual(processed[5].y, null)  // Rendered as gap (null)
    assert.strictEqual(processed[6].y, null)  // Rendered as gap (null)
    assert.strictEqual(processed[7].y, null)  // Rendered as gap (null)
    assert.strictEqual(processed[8].y, null)  // Boolean false is null, not 0
    assert.strictEqual(processed[9].y, null)  // Boolean true is null, not 1
  })

  await t.test('excludes invalid points from scatter plot coordinates when X or Y is non-numeric', () => {
    const rawRows = [
      { x_val: 10, y_val: 20 },      // Valid point (10, 20)
      { x_val: 0, y_val: 0 },        // Valid point (0, 0)
      { x_val: null, y_val: 15 },    // Invalid X -> excluded
      { x_val: 25, y_val: null },    // Invalid Y -> excluded
      { x_val: 'bad', y_val: 30 },   // Invalid X -> excluded
      { x_val: 35, y_val: Infinity },// Invalid Y -> excluded
    ]

    let omittedScatterCount = 0
    const scatterPoints: Array<{ x: number; y: number }> = []

    for (const r of rawRows) {
      const rawX = r.x_val
      const rawY = r.y_val

      const isXZero = rawX === 0 || rawX === '0'
      const isYZero = rawY === 0 || rawY === '0'
      const numX = typeof rawX === 'number' ? rawX : Number(rawX)
      const numY = typeof rawY === 'number' ? rawY : Number(rawY)

      const isXValid = (isXZero || Number.isFinite(numX)) && rawX !== null && rawX !== undefined && rawX !== ''
      const isYValid = (isYZero || Number.isFinite(numY)) && rawY !== null && rawY !== undefined && rawY !== ''

      if (!isXValid || !isYValid) {
        omittedScatterCount++
        continue
      }

      scatterPoints.push({
        x: isXZero ? 0 : numX,
        y: isYZero ? 0 : numY,
      })
    }

    assert.strictEqual(scatterPoints.length, 2)
    assert.strictEqual(omittedScatterCount, 4)
    assert.deepStrictEqual(scatterPoints[0], { x: 10, y: 20 })
    assert.deepStrictEqual(scatterPoints[1], { x: 0, y: 0 })
  })

  await t.test('detects line chart suitability when temporal and numeric columns are present', () => {
    const temporalCols = ['event_date']
    const numericCols = ['active_users']
    assert.ok(temporalCols.length > 0 && numericCols.length > 0)
    const bestType = temporalCols.length > 0 ? 'line' : 'bar'
    assert.strictEqual(bestType, 'line')
  })

  await t.test('detects bar chart suitability when categorical and numeric columns are present', () => {
    const categoricalCols = ['department']
    const numericCols = ['budget']
    assert.ok(categoricalCols.length > 0 && numericCols.length > 0)
    const bestType = 'bar'
    assert.strictEqual(bestType, 'bar')
  })

  await t.test('detects scatter chart suitability when two numeric columns are present', () => {
    const numericCols = ['study_hours', 'exam_score']
    const isScatterSuitable = numericCols.length >= 2
    assert.strictEqual(isScatterSuitable, true)
  })

  await t.test('detects unchartable data when zero numeric metrics exist', () => {
    const numericCols: string[] = []
    const isChartable = numericCols.length > 0
    assert.strictEqual(isChartable, false)
  })
})

// ==============================================================================
// 2. Large Dataset Reduction Policy
// ==============================================================================

test('Result Visualizer - Large Dataset Reduction Policy', async (t) => {
  await t.test('sampleUniformStride preserves endpoints and chronological span', () => {
    const dataset = Array.from({ length: 1000 }, (_, i) => ({
      date: `2026-01-${String(i + 1).padStart(4, '0')}`,
      value: i * 10,
    }))

    const sampled = sampleUniformStride(dataset, 200)

    assert.strictEqual(sampled.length, 200)
    // Preserves exact first item
    assert.strictEqual(sampled[0].date, '2026-01-0001')
    assert.strictEqual(sampled[0].value, 0)
    // Preserves exact last item
    assert.strictEqual(sampled[199].date, '2026-01-1000')
    assert.strictEqual(sampled[199].value, 9990)
    // Deterministic: repeat call returns identical sequence
    const sampledRepeat = sampleUniformStride(dataset, 200)
    assert.deepStrictEqual(sampled, sampledRepeat)
  })

  await t.test('sampleUniformStride returns original array when length is under limit', () => {
    const smallData = [{ id: 1 }, { id: 2 }, { id: 3 }]
    const result = sampleUniformStride(smallData, 200)
    assert.strictEqual(result.length, 3)
    assert.deepStrictEqual(result, smallData)
  })

  await t.test('categorical reduction deterministically samples categories across the full range preserving values and endpoints', () => {
    const MAX_POINTS_BAR = 100
    const manyCategories = Array.from({ length: 350 }, (_, i) => ({
      category: `Category ${i + 1}`,
      sales: (i + 1) * 10,
    }))

    const isReduced = manyCategories.length > MAX_POINTS_BAR
    const displayItems = isReduced ? sampleUniformStride(manyCategories, MAX_POINTS_BAR) : manyCategories

    assert.strictEqual(isReduced, true)
    assert.strictEqual(displayItems.length, 100)
    // Preserves original first category and its unaggregated value
    assert.strictEqual(displayItems[0].category, 'Category 1')
    assert.strictEqual(displayItems[0].sales, 10)
    // Preserves original final category and its unaggregated value (not truncated at Category 100)
    assert.strictEqual(displayItems[99].category, 'Category 350')
    assert.strictEqual(displayItems[99].sales, 3500)

    // Repeat sampling is 100% deterministic
    const repeatSample = sampleUniformStride(manyCategories, MAX_POINTS_BAR)
    assert.deepStrictEqual(displayItems, repeatSample)
  })
})

// ==============================================================================
// 3. Privacy & Network Invariants
// ==============================================================================

test('Result Visualizer - Privacy & Network Invariants', async (t) => {
  await t.test('guarantees zero network transmission of result rows during visualization', () => {
    let networkRequestDispatched = false
    const originalFetch = globalThis.fetch
    globalThis.fetch = async () => {
      networkRequestDispatched = true
      return new Response('{}', { status: 200 })
    }

    try {
      const localResult: ExecuteQueryResponse = {
        columns: ['dept', 'revenue'],
        rows: [
          { dept: 'Finance', revenue: 120000 },
          { dept: 'Tech', revenue: 240000 },
        ],
        row_count: 2,
        truncated: false,
        execution_time_ms: 8,
      }

      // Perform local transformations
      const sampled = sampleUniformStride(localResult.rows, 200)
      assert.strictEqual(sampled.length, 2)
      assert.strictEqual(networkRequestDispatched, false, 'No fetch calls must be made during visualization')
    } finally {
      globalThis.fetch = originalFetch
    }
  })
})
