/**
 * Deterministic Local Answer Synthesis Service.
 *
 * Produces concise, truthful natural-language answers grounded strictly in the query
 * execution results already present in local browser memory.
 *
 * Security & Integrity Invariants:
 * - Local-Only: Operates strictly in-memory. Zero network dispatches, zero external APIs,
 *   zero LLM/cloud calls, zero browser storage persistence.
 * - Truthful Grounding: Strictly describes actual returned rows/columns. Never extrapolates,
 *   never invents reasons for empty sets, never infers business causes or missing data.
 * - Exact Preservation: Preserves numeric and decimal values exactly without silent rounding.
 *   Does not invent currency symbols ($/€/£) unless present in the data.
 * - Explicit Distinctions: Distinctly represents NULL, zero (0), false, empty string (""),
 *   and missing/undefined fields.
 * - Truncation Honesty: When results are truncated by the profile limit, prominently labels
 *   answers and derived statistics as partial (describing only the retrieved sample).
 * - Untrusted Display Data: Treats all database values as untrusted text. Never evaluates or
 *   executes cell contents as HTML, JavaScript, SQL, or spreadsheet formulas.
 * - Error Safety: Never produces an answer implying successful execution when an error or
 *   ambiguous outcome occurs.
 */

import type { ExecuteQueryResponse } from '../types/api.ts'

export type DeterministicAnswerKind =
  | 'zero_rows'
  | 'single_cell'
  | 'single_row'
  | 'multi_row'
  | 'inconsistent_metadata'
  | 'error'

export interface DeterministicAnswerField {
  label: string
  value: string
  isNull: boolean
  isUndefined: boolean
  rawValue: unknown
}

export interface ColumnStatistic {
  column: string
  nonNullCount: number
  nullCount: number
  min: number | string
  max: number | string
  sum: number | null
  avg: number | null
}

export interface LocalResultStats {
  isPartial: boolean
  sampleRowCount: number
  scopeNotice: string
  columns: ColumnStatistic[]
}

export interface DeterministicAnswerResult {
  kind: DeterministicAnswerKind
  text: string
  isTruncated: boolean
  fields?: DeterministicAnswerField[]
  stats?: LocalResultStats | null
}

export interface CellPresentation {
  kind: 'missing' | 'null' | 'bool' | 'json' | 'text'
  text: string
}

/**
 * Resolves cell value representation for table display, strictly distinguishing
 * absent properties (undefined) from explicit database NULLs, booleans, objects, and strings.
 */
export function resolveCellPresentation(value: unknown): CellPresentation {
  if (value === undefined) {
    return { kind: 'missing', text: '[missing]' }
  }
  if (value === null) {
    return { kind: 'null', text: 'NULL' }
  }
  if (typeof value === 'boolean') {
    return { kind: 'bool', text: value ? 'true' : 'false' }
  }
  if (typeof value === 'object') {
    try {
      return { kind: 'json', text: JSON.stringify(value) }
    } catch {
      return { kind: 'text', text: '[Complex Object]' }
    }
  }
  return { kind: 'text', text: String(value) }
}

/**
 * Deterministically formats any cell value into a clean, safe text string,
 * strictly distinguishing NULL, missing fields, empty strings, zero, and boolean false.
 */
export function formatDeterministicValue(value: unknown): string {
  if (value === null) {
    return 'NULL'
  }
  if (value === undefined) {
    return '[missing]'
  }
  if (typeof value === 'boolean') {
    return value ? 'true' : 'false'
  }
  if (typeof value === 'number') {
    if (Number.isNaN(value)) {
      return 'NaN'
    }
    if (!Number.isFinite(value)) {
      return value > 0 ? 'Infinity' : '-Infinity'
    }
    return String(value)
  }
  if (typeof value === 'string') {
    if (value === '') {
      return '""'
    }
    return value
  }
  if (typeof value === 'bigint') {
    return value.toString()
  }
  if (typeof value === 'object') {
    try {
      return JSON.stringify(value)
    } catch {
      return '[Complex Object]'
    }
  }
  return String(value)
}

/**
 * Computes strictly bounded, explicitly labeled column statistics from locally present rows.
 * Only processes columns where all non-null values are finite numbers.
 *
 * Arithmetic & Precision Policy:
 * - Employs native JavaScript IEEE-754 floating-point arithmetic.
 * - Minimum, maximum, and sum preserve exact evaluated numeric numbers.
 * - To prevent unbounded recurring fractional displays (e.g. 13.666666666666666),
 *   the computed average ('avg') is deterministically bounded to four decimal places (toFixed(4)).
 * - Non-numeric values (strings, booleans), non-finite values (NaN, Infinity), and all-null
 *   columns are strictly excluded and cause the statistic computation for that column to return null.
 * - Missing or invalid values are NEVER silently converted into zero.
 */
export function computeColumnStatistics(
  column: string,
  rows: Record<string, unknown>[]
): ColumnStatistic | null {
  if (!rows || rows.length === 0) {
    return null
  }

  let nonNullCount = 0
  let nullCount = 0
  let sum = 0
  let min: number | null = null
  let max: number | null = null
  let allNumeric = true

  for (const row of rows) {
    const val = row[column]
    if (val === null || val === undefined) {
      nullCount++
      continue
    }

    if (typeof val === 'number' && Number.isFinite(val)) {
      nonNullCount++
      sum += val
      if (min === null || val < min) {
        min = val
      }
      if (max === null || val > max) {
        max = val
      }
    } else {
      allNumeric = false
      break
    }
  }

  // Only produce statistics if there is at least one numeric value and no conflicting non-numeric types
  if (!allNumeric || nonNullCount === 0 || min === null || max === null) {
    return null
  }

  const avg = nonNullCount > 0 ? sum / nonNullCount : null

  return {
    column,
    nonNullCount,
    nullCount,
    min,
    max,
    sum,
    avg: avg !== null ? Number(avg.toFixed(4)) : null,
  }
}

/**
 * Pure, deterministic answer synthesis function.
 *
 * Operates purely on the in-memory ExecuteQueryResponse and optional error context.
 * Performs zero network requests and does not persist data to browser storage.
 */
export function synthesizeDeterministicAnswer(
  result: ExecuteQueryResponse | null,
  options?: {
    error?: Error | null
    isAmbiguousFailure?: boolean
    isConfirmedTimeout?: boolean
  }
): DeterministicAnswerResult | null {
  // 1. Error or ambiguous outcome guard: Never imply successful execution
  if (options?.error || options?.isAmbiguousFailure || options?.isConfirmedTimeout) {
    return {
      kind: 'error',
      text: 'Query execution did not complete successfully. No answer could be synthesized.',
      isTruncated: false,
    }
  }

  if (!result) {
    return null
  }

  const cols = result.columns || []
  const isTruncated = Boolean(result.truncated)
  const rowsArray = result.rows || []
  const hasExplicitRowCount = typeof result.row_count === 'number'

  // 2. Metadata consistency validation: Disagreement between reported row_count and actual rows array length
  if (hasExplicitRowCount && result.row_count !== rowsArray.length) {
    let inconsistencyText: string
    if (result.row_count > 0 && rowsArray.length === 0) {
      inconsistencyText = `Inconsistent result metadata: execution reported ${result.row_count} rows, but 0 row records were received. The result set cannot be verified as empty or complete.`
    } else if (result.row_count === 0 && rowsArray.length > 0) {
      const noun = rowsArray.length === 1 ? 'row record was' : 'row records were'
      inconsistencyText = `Inconsistent result metadata: execution reported 0 rows, but ${rowsArray.length} ${noun} received. Please verify directly in the results table below.`
    } else {
      inconsistencyText = `Inconsistent result metadata: execution reported ${result.row_count} rows, but ${rowsArray.length} row records were provided. The available data is incomplete or conflicting.`
    }

    return {
      kind: 'inconsistent_metadata',
      text: inconsistencyText,
      isTruncated,
    }
  }

  const rowCount = hasExplicitRowCount ? result.row_count : rowsArray.length

  // 3. Zero rows returned (both row_count and rows array are verified 0)
  if (rowCount === 0) {
    return {
      kind: 'zero_rows',
      text: 'Query executed successfully. No rows were returned.',
      isTruncated: false,
    }
  }

  const partialNotice = isTruncated ? ' (partial result set — truncated by profile limit)' : ''

  // 4. Exactly one cell (1 row, 1 column)
  if (rowCount === 1 && cols.length === 1) {
    const colName = cols[0]
    const row = rowsArray[0]
    const rawVal = row ? row[colName] : undefined
    const formattedVal = formatDeterministicValue(rawVal)

    return {
      kind: 'single_cell',
      text: `${colName}: ${formattedVal}${partialNotice}`,
      isTruncated,
      fields: [
        {
          label: colName,
          value: formattedVal,
          isNull: rawVal === null,
          isUndefined: rawVal === undefined,
          rawValue: rawVal,
        },
      ],
    }
  }

  // 5. Exactly one row with multiple columns
  if (rowCount === 1 && cols.length > 1) {
    const row = rowsArray[0] || {}
    const fields: DeterministicAnswerField[] = cols.map((colName) => {
      const rawVal = row[colName]
      return {
        label: colName,
        value: formatDeterministicValue(rawVal),
        isNull: rawVal === null,
        isUndefined: rawVal === undefined,
        rawValue: rawVal,
      }
    })

    const summaryPairs = fields.map((f) => `${f.label}: ${f.value}`).join('; ')

    return {
      kind: 'single_row',
      text: `1 row returned: ${summaryPairs}${partialNotice}`,
      isTruncated,
      fields,
    }
  }

  // 5. Multiple rows
  const rowNoun = rowCount === 1 ? 'row' : 'rows'
  const colNoun = cols.length === 1 ? 'column' : 'columns'

  let text: string
  if (isTruncated) {
    text = `Returned ${rowCount} ${rowNoun} across ${cols.length} ${colNoun} (partial result set — truncated by profile limit). See the results table below for the retrieved rows.`
  } else {
    text = `Returned ${rowCount} ${rowNoun} across ${cols.length} ${colNoun}. See the results table below for the complete row details.`
  }

  // 6. Optional local numeric statistics
  const statColumns: ColumnStatistic[] = []
  for (const col of cols) {
    const stat = computeColumnStatistics(col, result.rows)
    if (stat) {
      statColumns.push(stat)
    }
  }

  let stats: LocalResultStats | null = null
  if (statColumns.length > 0) {
    stats = {
      isPartial: isTruncated,
      sampleRowCount: result.rows.length,
      scopeNotice: isTruncated
        ? `Partial statistics — based only on the ${result.rows.length} retrieved rows (profile row limit applied).`
        : `Statistics based only on the ${result.rows.length} returned rows.`,
      columns: statColumns,
    }
  }

  return {
    kind: 'multi_row',
    text,
    isTruncated,
    stats,
  }
}
