import assert from 'node:assert'
import test from 'node:test'
import {
  formatDeterministicValue,
  resolveCellPresentation,
  computeColumnStatistics,
  synthesizeDeterministicAnswer,
} from '../src/services/answerSynthesis.ts'
import { QueryExecutionCoordinator } from '../src/services/queryExecution.ts'
import type { ExecuteQueryResponse } from '../src/types/api.ts'

// ==============================================================================
// 1. Value Formatting & Distinct Representation Tests
// ==============================================================================

test('Answer Synthesis - Distinct Value Representation', async (t) => {
  await t.test('NULL, missing fields, empty strings, zero, and false are strictly distinct', () => {
    const formattedNull = formatDeterministicValue(null)
    const formattedUndefined = formatDeterministicValue(undefined)
    const formattedEmpty = formatDeterministicValue('')
    const formattedZero = formatDeterministicValue(0)
    const formattedFalse = formatDeterministicValue(false)

    assert.strictEqual(formattedNull, 'NULL')
    assert.strictEqual(formattedUndefined, '[missing]')
    assert.strictEqual(formattedEmpty, '""')
    assert.strictEqual(formattedZero, '0')
    assert.strictEqual(formattedFalse, 'false')

    // Verify all 5 are distinct from one another
    const values = [
      formattedNull,
      formattedUndefined,
      formattedEmpty,
      formattedZero,
      formattedFalse,
    ]
    const uniqueValues = new Set(values)
    assert.strictEqual(uniqueValues.size, 5, 'All five representations must be mutually distinct')
  })

  await t.test('preserves exact numbers and decimals without rounding or currency inference', () => {
    assert.strictEqual(formatDeterministicValue(100), '100')
    assert.strictEqual(formatDeterministicValue(-42.75), '-42.75')
    assert.strictEqual(formatDeterministicValue(0.00012345), '0.00012345')
    assert.strictEqual(formatDeterministicValue(999999999.99), '999999999.99')

    // Confirm no currency symbol was injected
    assert.ok(!formatDeterministicValue(100).includes('$'))
    assert.ok(!formatDeterministicValue(100).includes('€'))
    assert.ok(!formatDeterministicValue(100).includes('£'))
  })

  await t.test('handles BigInt, NaN, and infinities safely', () => {
    assert.strictEqual(formatDeterministicValue(BigInt(9007199254740991)), '9007199254740991')
    assert.strictEqual(formatDeterministicValue(NaN), 'NaN')
    assert.strictEqual(formatDeterministicValue(Infinity), 'Infinity')
    assert.strictEqual(formatDeterministicValue(-Infinity), '-Infinity')
  })

  await t.test('formats complex objects as serialized JSON without throwing', () => {
    const obj = { id: 1, role: 'admin' }
    assert.strictEqual(formatDeterministicValue(obj), '{"id":1,"role":"admin"}')
  })
})

// ==============================================================================
// 2. Untrusted Display Data & Injection Immunity
// ==============================================================================

test('Answer Synthesis - Untrusted Display Data & Injection Immunity', async (t) => {
  await t.test('treats HTML-like text as literal display text', () => {
    const htmlPayload = '<script>alert("xss")</script><img src=x onerror=alert(1)>'
    const formatted = formatDeterministicValue(htmlPayload)
    assert.strictEqual(formatted, htmlPayload)

    const result: ExecuteQueryResponse = {
      columns: ['content'],
      rows: [{ content: htmlPayload }],
      row_count: 1,
      truncated: false,
      execution_time_ms: 5.0,
    }
    const answer = synthesizeDeterministicAnswer(result)
    assert.ok(answer)
    assert.strictEqual(answer.text, `content: ${htmlPayload}`)
    assert.strictEqual(answer.fields?.[0].value, htmlPayload)
  })

  await t.test('treats SQL-like injection strings as literal display text', () => {
    const sqlPayload = "'; DROP TABLE users; -- SELECT * FROM secrets"
    const formatted = formatDeterministicValue(sqlPayload)
    assert.strictEqual(formatted, sqlPayload)

    const result: ExecuteQueryResponse = {
      columns: ['user_input'],
      rows: [{ user_input: sqlPayload }],
      row_count: 1,
      truncated: false,
      execution_time_ms: 4.2,
    }
    const answer = synthesizeDeterministicAnswer(result)
    assert.ok(answer)
    assert.strictEqual(answer.text, `user_input: ${sqlPayload}`)
  })

  await t.test('treats spreadsheet formulas as literal display text without executing', () => {
    const formulaPayload = '=cmd|\'/C calc\'!A0'
    const formatted = formatDeterministicValue(formulaPayload)
    assert.strictEqual(formatted, formulaPayload)

    const result: ExecuteQueryResponse = {
      columns: ['formula'],
      rows: [{ formula: formulaPayload }],
      row_count: 1,
      truncated: false,
      execution_time_ms: 3.1,
    }
    const answer = synthesizeDeterministicAnswer(result)
    assert.ok(answer)
    assert.strictEqual(answer.text, `formula: ${formulaPayload}`)
  })
})

// ==============================================================================
// 3. Shape Handling: Zero Rows, Single Cell, Single Row, Multiple Rows
// ==============================================================================

test('Answer Synthesis - Shape Handling & Truthful Grounding', async (t) => {
  await t.test('zero rows: states no rows returned without inventing reasons', () => {
    const result: ExecuteQueryResponse = {
      columns: ['id', 'name'],
      rows: [],
      row_count: 0,
      truncated: false,
      execution_time_ms: 8.5,
    }
    const answer = synthesizeDeterministicAnswer(result)
    assert.ok(answer)
    assert.strictEqual(answer.kind, 'zero_rows')
    assert.strictEqual(answer.text, 'Query executed successfully. No rows were returned.')
    assert.strictEqual(answer.isTruncated, false)
  })

  await t.test('single cell (1 row, 1 column): shows column name and exact returned value', () => {
    // Numeric scalar
    const numResult: ExecuteQueryResponse = {
      columns: ['total_students'],
      rows: [{ total_students: 450 }],
      row_count: 1,
      truncated: false,
      execution_time_ms: 10.0,
    }
    const numAnswer = synthesizeDeterministicAnswer(numResult)
    assert.ok(numAnswer)
    assert.strictEqual(numAnswer.kind, 'single_cell')
    assert.strictEqual(numAnswer.text, 'total_students: 450')
    assert.strictEqual(numAnswer.fields?.[0].label, 'total_students')
    assert.strictEqual(numAnswer.fields?.[0].value, '450')
    assert.strictEqual(numAnswer.fields?.[0].isNull, false)

    // NULL scalar
    const nullResult: ExecuteQueryResponse = {
      columns: ['notes'],
      rows: [{ notes: null }],
      row_count: 1,
      truncated: false,
      execution_time_ms: 7.2,
    }
    const nullAnswer = synthesizeDeterministicAnswer(nullResult)
    assert.ok(nullAnswer)
    assert.strictEqual(nullAnswer.kind, 'single_cell')
    assert.strictEqual(nullAnswer.text, 'notes: NULL')
    assert.strictEqual(nullAnswer.fields?.[0].isNull, true)

    // Decimal scalar
    const decResult: ExecuteQueryResponse = {
      columns: ['gpa_average'],
      rows: [{ gpa_average: 3.7842 }],
      row_count: 1,
      truncated: false,
      execution_time_ms: 9.1,
    }
    const decAnswer = synthesizeDeterministicAnswer(decResult)
    assert.ok(decAnswer)
    assert.strictEqual(decAnswer.text, 'gpa_average: 3.7842')
  })

  await t.test('one row with multiple columns: presents each returned field and value', () => {
    const result: ExecuteQueryResponse = {
      columns: ['student_id', 'first_name', 'last_name', 'gpa', 'advisor_notes'],
      rows: [
        {
          student_id: 101,
          first_name: 'Alice',
          last_name: 'Smith',
          gpa: 3.92,
          advisor_notes: null,
        },
      ],
      row_count: 1,
      truncated: false,
      execution_time_ms: 14.2,
    }
    const answer = synthesizeDeterministicAnswer(result)
    assert.ok(answer)
    assert.strictEqual(answer.kind, 'single_row')
    assert.strictEqual(
      answer.text,
      '1 row returned: student_id: 101; first_name: Alice; last_name: Smith; gpa: 3.92; advisor_notes: NULL'
    )
    assert.strictEqual(answer.fields?.length, 5)
    assert.strictEqual(answer.fields?.[0].label, 'student_id')
    assert.strictEqual(answer.fields?.[0].value, '101')
    assert.strictEqual(answer.fields?.[4].label, 'advisor_notes')
    assert.strictEqual(answer.fields?.[4].value, 'NULL')
    assert.strictEqual(answer.fields?.[4].isNull, true)
  })

  await t.test('multiple rows: states row count and directs user to table', () => {
    const result: ExecuteQueryResponse = {
      columns: ['department', 'course_count'],
      rows: [
        { department: 'Computer Science', course_count: 24 },
        { department: 'Mathematics', course_count: 18 },
        { department: 'Physics', course_count: 12 },
      ],
      row_count: 3,
      truncated: false,
      execution_time_ms: 18.0,
    }
    const answer = synthesizeDeterministicAnswer(result)
    assert.ok(answer)
    assert.strictEqual(answer.kind, 'multi_row')
    assert.strictEqual(
      answer.text,
      'Returned 3 rows across 2 columns. See the results table below for the complete row details.'
    )
    assert.strictEqual(answer.isTruncated, false)
  })
})

// ==============================================================================
// 4. Truncation & Local Column Statistics
// ==============================================================================

test('Answer Synthesis - Truncation & Local Statistics', async (t) => {
  await t.test('truncated results state partialness and do not claim full population statistics', () => {
    const result: ExecuteQueryResponse = {
      columns: ['id', 'score'],
      rows: [
        { id: 1, score: 90 },
        { id: 2, score: 85 },
      ],
      row_count: 2,
      truncated: true,
      execution_time_ms: 22.1,
    }
    const answer = synthesizeDeterministicAnswer(result)
    assert.ok(answer)
    assert.strictEqual(answer.kind, 'multi_row')
    assert.strictEqual(answer.isTruncated, true)
    assert.ok(answer.text.includes('(partial result set — truncated by profile limit)'))
    assert.ok(answer.text.includes('See the results table below for the retrieved rows.'))

    // Check stats labeling
    assert.ok(answer.stats)
    assert.strictEqual(answer.stats.isPartial, true)
    assert.ok(
      answer.stats.scopeNotice.includes('Partial statistics — based only on the 2 retrieved rows')
    )
  })

  await t.test('computes exact column statistics on numeric columns without inferring totals', () => {
    const rows = [
      { item: 'A', quantity: 10, price: 5.5 },
      { item: 'B', quantity: 20, price: 15.0 },
      { item: 'C', quantity: null, price: 20.5 },
    ]

    const qtyStats = computeColumnStatistics('quantity', rows)
    assert.ok(qtyStats)
    assert.strictEqual(qtyStats.column, 'quantity')
    assert.strictEqual(qtyStats.nonNullCount, 2)
    assert.strictEqual(qtyStats.nullCount, 1)
    assert.strictEqual(qtyStats.min, 10)
    assert.strictEqual(qtyStats.max, 20)
    assert.strictEqual(qtyStats.sum, 30)
    assert.strictEqual(qtyStats.avg, 15)

    const priceStats = computeColumnStatistics('price', rows)
    assert.ok(priceStats)
    assert.strictEqual(priceStats.min, 5.5)
    assert.strictEqual(priceStats.max, 20.5)
    assert.strictEqual(priceStats.sum, 41)
    assert.strictEqual(priceStats.avg, 13.6667)

    // Non-numeric column produces null statistics
    const itemStats = computeColumnStatistics('item', rows)
    assert.strictEqual(itemStats, null)
  })
})

// ==============================================================================
// 5. Error & Unknown Outcome Safety
// ==============================================================================

test('Answer Synthesis - Error & Unknown Outcome Safety', async (t) => {
  await t.test('does not synthesize a success answer when an error is present', () => {
    const errorAnswer = synthesizeDeterministicAnswer(null, {
      error: new Error('Database connection failed'),
    })
    assert.ok(errorAnswer)
    assert.strictEqual(errorAnswer.kind, 'error')
    assert.strictEqual(
      errorAnswer.text,
      'Query execution did not complete successfully. No answer could be synthesized.'
    )
  })

  await t.test('does not imply success when ambiguous failure or timeout occurs', () => {
    const ambiguousAnswer = synthesizeDeterministicAnswer(null, {
      isAmbiguousFailure: true,
    })
    assert.ok(ambiguousAnswer)
    assert.strictEqual(ambiguousAnswer.kind, 'error')

    const timeoutAnswer = synthesizeDeterministicAnswer(null, {
      isConfirmedTimeout: true,
    })
    assert.ok(timeoutAnswer)
    assert.strictEqual(timeoutAnswer.kind, 'error')
  })

  await t.test('returns null when result is null and no error is flagged', () => {
    const answer = synthesizeDeterministicAnswer(null)
    assert.strictEqual(answer, null)
  })
})

// ==============================================================================
// 6. Security Invariants: No Network Calls & No Browser Storage Persistence
// ==============================================================================

test('Answer Synthesis - Zero Network & Zero Storage Invariants', async (t) => {
  await t.test('synthesis executes purely in-memory with zero network calls', () => {
    const originalFetch = globalThis.fetch
    let fetchCalled = false

    globalThis.fetch = (() => {
      fetchCalled = true
      throw new Error('Network call attempted during local answer synthesis')
    }) as unknown as typeof fetch

    try {
      const result: ExecuteQueryResponse = {
        columns: ['metric', 'val'],
        rows: [
          { metric: 'a', val: 1 },
          { metric: 'b', val: 2 },
        ],
        row_count: 2,
        truncated: false,
        execution_time_ms: 12.0,
      }
      const answer = synthesizeDeterministicAnswer(result)
      assert.ok(answer)
      assert.strictEqual(fetchCalled, false, 'Fetch must never be called during answer synthesis')
    } finally {
      globalThis.fetch = originalFetch
    }
  })

  await t.test('synthesis never touches localStorage or sessionStorage', () => {
    let storageAccessed = false

    const mockStorage = {
      getItem: () => {
        storageAccessed = true
        return null
      },
      setItem: () => {
        storageAccessed = true
      },
      removeItem: () => {
        storageAccessed = true
      },
      clear: () => {
        storageAccessed = true
      },
      key: () => null,
      length: 0,
    }

    const originalLocalStorage = globalThis.localStorage
    const originalSessionStorage = globalThis.sessionStorage

    Object.defineProperty(globalThis, 'localStorage', {
      value: mockStorage,
      configurable: true,
      writable: true,
    })
    Object.defineProperty(globalThis, 'sessionStorage', {
      value: mockStorage,
      configurable: true,
      writable: true,
    })

    try {
      const result: ExecuteQueryResponse = {
        columns: ['col1'],
        rows: [{ col1: 'sensitive_val' }],
        row_count: 1,
        truncated: false,
        execution_time_ms: 5.0,
      }
      const answer = synthesizeDeterministicAnswer(result)
      assert.ok(answer)
      assert.strictEqual(storageAccessed, false, 'Web Storage must never be accessed')
    } finally {
      Object.defineProperty(globalThis, 'localStorage', {
        value: originalLocalStorage,
        configurable: true,
        writable: true,
      })
      Object.defineProperty(globalThis, 'sessionStorage', {
        value: originalSessionStorage,
        configurable: true,
        writable: true,
      })
    }
  })
})

// ==============================================================================
// 7. Answer Invalidation & Coordination Lifecycle
// ==============================================================================

test('Answer Synthesis - Invalidation & Lifecycle Alignment', async (t) => {
  await t.test('clearing query execution coordinator immediately clears synthesized answer', () => {
    const coordinator = new QueryExecutionCoordinator()
    const sampleResult: ExecuteQueryResponse = {
      columns: ['status'],
      rows: [{ status: 'success' }],
      row_count: 1,
      truncated: false,
      execution_time_ms: 11.2,
    }

    // Set initial executed state
    coordinator['setState']({
      isLoading: false,
      result: sampleResult,
      executedSql: 'SELECT status FROM test;',
      executedProfileId: 'prof-1',
    })

    let state = coordinator.getState()
    let answer = synthesizeDeterministicAnswer(state.result)
    assert.ok(answer)
    assert.strictEqual(answer.text, 'status: success')

    // Invalidate results via clearResult()
    coordinator.clearResult()

    state = coordinator.getState()
    assert.strictEqual(state.result, null)
    answer = synthesizeDeterministicAnswer(state.result)
    assert.strictEqual(answer, null, 'Synthesized answer must be null after result clearance')
  })
})

// ==============================================================================
// 8. Metadata Inconsistency & Disagreement Hardening (B8-B-R2)
// ==============================================================================

test('Answer Synthesis - Metadata Inconsistency Hardening', async (t) => {
  await t.test('handles row_count > 0 with empty rows array without claiming empty or complete', () => {
    const inconsistentResult: ExecuteQueryResponse = {
      columns: ['id', 'name'],
      rows: [],
      row_count: 10,
      truncated: false,
      execution_time_ms: 12.0,
    }
    const answer = synthesizeDeterministicAnswer(inconsistentResult)
    assert.ok(answer)
    assert.strictEqual(answer.kind, 'inconsistent_metadata')
    assert.ok(
      answer.text.includes('reported 10 rows, but 0 row records were received'),
      'Must explicitly state reported count and received count mismatch'
    )
    assert.ok(
      answer.text.includes('cannot be verified as empty or complete'),
      'Must not definitively state result is empty'
    )
  })

  await t.test('handles row_count = 0 with non-empty rows array without ignoring actual rows', () => {
    const inconsistentResult: ExecuteQueryResponse = {
      columns: ['id'],
      rows: [{ id: 1 }],
      row_count: 0,
      truncated: false,
      execution_time_ms: 8.0,
    }
    const answer = synthesizeDeterministicAnswer(inconsistentResult)
    assert.ok(answer)
    assert.strictEqual(answer.kind, 'inconsistent_metadata')
    assert.ok(
      answer.text.includes('reported 0 rows, but 1 row record was received')
    )
  })

  await t.test('handles partial rows array length mismatching reported row_count', () => {
    const inconsistentResult: ExecuteQueryResponse = {
      columns: ['id'],
      rows: [{ id: 1 }, { id: 2 }],
      row_count: 5,
      truncated: false,
      execution_time_ms: 15.0,
    }
    const answer = synthesizeDeterministicAnswer(inconsistentResult)
    assert.ok(answer)
    assert.strictEqual(answer.kind, 'inconsistent_metadata')
    assert.ok(
      answer.text.includes('reported 5 rows, but 2 row records were provided')
    )
  })
})

// ==============================================================================
// 9. Table Cell Renderer: Missing Property versus Explicit Database NULL (B8-B-R2)
// ==============================================================================

test('Table Renderer - Missing versus NULL Distinctness', async (t) => {
  await t.test('distinguishes absent undefined property from explicit database NULL', () => {
    const renderedNull = resolveCellPresentation(null)
    const renderedMissing = resolveCellPresentation(undefined)

    assert.strictEqual(renderedNull.kind, 'null')
    assert.strictEqual(renderedNull.text, 'NULL')

    assert.strictEqual(renderedMissing.kind, 'missing')
    assert.strictEqual(renderedMissing.text, '[missing]')
  })

  await t.test('preserves correct table rendering for empty strings, zero, and booleans', () => {
    const renderedEmpty = resolveCellPresentation('')
    const renderedZero = resolveCellPresentation(0)
    const renderedFalse = resolveCellPresentation(false)
    const renderedTrue = resolveCellPresentation(true)

    assert.strictEqual(renderedEmpty.kind, 'text')
    assert.strictEqual(renderedEmpty.text, '')

    assert.strictEqual(renderedZero.kind, 'text')
    assert.strictEqual(renderedZero.text, '0')

    assert.strictEqual(renderedFalse.kind, 'bool')
    assert.strictEqual(renderedFalse.text, 'false')

    assert.strictEqual(renderedTrue.kind, 'bool')
    assert.strictEqual(renderedTrue.text, 'true')
  })
})

// ==============================================================================
// 10. Truncated Single-Row Results (B8-B-R2)
// ==============================================================================

test('Answer Synthesis - Truncated Single-Row Results', async (t) => {
  await t.test('single-cell answer explicitly communicates partialness when truncated is true', () => {
    const result: ExecuteQueryResponse = {
      columns: ['status'],
      rows: [{ status: 'active' }],
      row_count: 1,
      truncated: true,
      execution_time_ms: 18.0,
    }
    const answer = synthesizeDeterministicAnswer(result)
    assert.ok(answer)
    assert.strictEqual(answer.kind, 'single_cell')
    assert.strictEqual(answer.isTruncated, true)
    assert.strictEqual(
      answer.text,
      'status: active (partial result set — truncated by profile limit)'
    )
  })

  await t.test('single-row multi-column answer explicitly communicates partialness when truncated is true', () => {
    const result: ExecuteQueryResponse = {
      columns: ['id', 'name'],
      rows: [{ id: 1, name: 'Alpha' }],
      row_count: 1,
      truncated: true,
      execution_time_ms: 14.5,
    }
    const answer = synthesizeDeterministicAnswer(result)
    assert.ok(answer)
    assert.strictEqual(answer.kind, 'single_row')
    assert.strictEqual(answer.isTruncated, true)
    assert.strictEqual(
      answer.text,
      '1 row returned: id: 1; name: Alpha (partial result set — truncated by profile limit)'
    )
  })
})

// ==============================================================================
// 11. Numeric Statistics Edge Cases & Non-Finite Safeguards (B8-B-R2)
// ==============================================================================

test('Column Statistics - Edge Cases & Non-Finite Safeguards', async (t) => {
  await t.test('returns null for an all-null column', () => {
    const rows = [{ metric: null }, { metric: null }, { metric: null }]
    const stats = computeColumnStatistics('metric', rows)
    assert.strictEqual(stats, null, 'All-null column must produce null statistics')
  })

  await t.test('returns null for an all-undefined column', () => {
    const rows = [{ other: 1 }, { other: 2 }]
    const stats = computeColumnStatistics('metric', rows)
    assert.strictEqual(stats, null, 'All-undefined column must produce null statistics')
  })

  await t.test('returns null for mixed-type column containing numbers and strings', () => {
    const rows = [{ val: 10 }, { val: '20' }]
    const stats = computeColumnStatistics('val', rows)
    assert.strictEqual(stats, null, 'Mixed-type column must not silently convert strings or compute stats')
  })

  await t.test('returns null for column containing non-finite NaN or Infinity', () => {
    const nanRows = [{ val: 10 }, { val: NaN }]
    assert.strictEqual(computeColumnStatistics('val', nanRows), null)

    const infRows = [{ val: 10 }, { val: Infinity }]
    assert.strictEqual(computeColumnStatistics('val', infRows), null)

    const negInfRows = [{ val: 10 }, { val: -Infinity }]
    assert.strictEqual(computeColumnStatistics('val', negInfRows), null)
  })

  await t.test('preserves exact decimals and computes average rounded to four decimal places', () => {
    const rows = [
      { val: 1.11111 },
      { val: 2.22222 },
    ]
    const stats = computeColumnStatistics('val', rows)
    assert.ok(stats)
    assert.strictEqual(stats.min, 1.11111)
    assert.strictEqual(stats.max, 2.22222)
    assert.strictEqual(stats.sum, 3.33333)
    // Average is (1.11111 + 2.22222) / 2 = 1.666665 -> rounded to 4 decimals = 1.6667
    assert.strictEqual(stats.avg, 1.6667)
  })
})

// ==============================================================================
// 12. B9-C3 Local Deterministic Presentation Contract
// ==============================================================================

test('B9-C3 - Local Deterministic Scalar Presentation Contract', async (t) => {
  await t.test('faithfully presents actual returned scalar count (B9-C2 shape: total_records = 2)', () => {
    const liveWorkflowResult: ExecuteQueryResponse = {
      columns: ['total_records'],
      rows: [{ total_records: 2 }],
      row_count: 1,
      truncated: false,
      execution_time_ms: 12.4,
    }

    const answer = synthesizeDeterministicAnswer(liveWorkflowResult)
    assert.ok(answer, 'Answer must not be null for valid execution result')
    assert.strictEqual(answer.kind, 'single_cell', '1 row x 1 col must format as single_cell')
    assert.strictEqual(answer.text, 'total_records: 2', 'Exact scalar formatting must match column: value')
    assert.strictEqual(answer.isTruncated, false, 'Non-truncated result must have isTruncated=false')
    assert.ok(answer.fields && answer.fields.length === 1, 'Must contain exactly 1 field')
    assert.strictEqual(answer.fields[0].label, 'total_records')
    assert.strictEqual(answer.fields[0].value, '2')
    assert.strictEqual(answer.fields[0].rawValue, 2)
    assert.strictEqual(answer.fields[0].isNull, false)
    assert.strictEqual(answer.fields[0].isUndefined, false)
  })
})


