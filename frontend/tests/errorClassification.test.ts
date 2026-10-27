import assert from 'node:assert'
import test from 'node:test'
import { classifyExecutionError } from '../src/services/errorClassification.ts'
import { AgentClientError } from '../src/types/api.ts'

// ==============================================================================
// 1. Recoverable Database Execution Errors
// ==============================================================================

test('Error Classification - Recoverable: Invalid Column Name', () => {
  const err = new AgentClientError({
    message: "Database execution error: (pyodbc.ProgrammingError) ('42S22', \"[42S22] [Microsoft][ODBC Driver 18 for SQL Server][SQL Server]Invalid column name 'Salary'. (207) (SQLExecDirectW)\")",
    kind: 'server',
    statusCode: 500,
  })

  const res = classifyExecutionError(err)
  assert.strictEqual(res.category, 'recoverable_query_error')
  assert.strictEqual(res.recoverable, true)
  assert.strictEqual(
    res.diagnosticFeedback,
    "Invalid column name: 'Salary'. Please query only valid columns defined in the schema."
  )
  assert.strictEqual(res.userMessage, 'Invalid column name: "Salary".')
})

test('Error Classification - Recoverable: Invalid Column Name without specific identifier', () => {
  const err = new AgentClientError({
    message: 'Database execution error: Invalid column name in projection',
    kind: 'server',
    statusCode: 500,
  })

  const res = classifyExecutionError(err)
  assert.strictEqual(res.category, 'recoverable_query_error')
  assert.strictEqual(res.recoverable, true)
  assert.strictEqual(
    res.diagnosticFeedback,
    'Invalid column name referenced. Please query only valid columns defined in the schema.'
  )
})

test('Error Classification - Recoverable: Invalid Object / Table Name', () => {
  const err = new AgentClientError({
    message: "Database execution error: (pyodbc.ProgrammingError) ('42S02', \"[42S02] [Microsoft][ODBC Driver 18 for SQL Server][SQL Server]Invalid object name 'dbo.Staff'. (208) (SQLExecDirectW)\")",
    kind: 'server',
    statusCode: 500,
  })

  const res = classifyExecutionError(err)
  assert.strictEqual(res.category, 'recoverable_query_error')
  assert.strictEqual(res.recoverable, true)
  assert.strictEqual(
    res.diagnosticFeedback,
    "Invalid table or object name: 'dbo.Staff'. Please query only tables defined in the schema."
  )
  assert.strictEqual(res.userMessage, 'Invalid table name: "dbo.Staff".')
})

test('Error Classification - Recoverable: GROUP BY / Aggregation Error', () => {
  const err = new AgentClientError({
    message: "Database execution error: Column 'Employees.JobTitle' is invalid in the select list because it is not contained in either an aggregate function or the GROUP BY clause.",
    kind: 'server',
    statusCode: 500,
  })

  const res = classifyExecutionError(err)
  assert.strictEqual(res.category, 'recoverable_query_error')
  assert.strictEqual(res.recoverable, true)
  assert.strictEqual(
    res.diagnosticFeedback,
    'Selected column must be contained in an aggregate function or the GROUP BY clause.'
  )
})

test('Error Classification - Recoverable: Ambiguous Column Name', () => {
  const err = new AgentClientError({
    message: "Database execution error: Ambiguous column name 'DepartmentID'.",
    kind: 'server',
    statusCode: 500,
  })

  const res = classifyExecutionError(err)
  assert.strictEqual(res.category, 'recoverable_query_error')
  assert.strictEqual(res.recoverable, true)
  assert.strictEqual(
    res.diagnosticFeedback,
    "Ambiguous column name: 'DepartmentID'. Please qualify column names with table aliases."
  )
})

test('Error Classification - Recoverable: T-SQL Syntax Error', () => {
  const err = new AgentClientError({
    message: "Database execution error: Incorrect syntax near the keyword 'WHERE'.",
    kind: 'server',
    statusCode: 500,
  })

  const res = classifyExecutionError(err)
  assert.strictEqual(res.category, 'recoverable_query_error')
  assert.strictEqual(res.recoverable, true)
  assert.strictEqual(
    res.diagnosticFeedback,
    'T-SQL syntax error. Please ensure valid Microsoft SQL Server syntax.'
  )
  assert.strictEqual(res.userMessage, 'Database reported a syntax error.')
})

test('Error Classification - Recoverable: Data Type Conversion Failure', () => {
  const err = new AgentClientError({
    message: "Database execution error: Conversion failed when converting the varchar value 'Active' to data type int.",
    kind: 'server',
    statusCode: 500,
  })

  const res = classifyExecutionError(err)
  assert.strictEqual(res.category, 'recoverable_query_error')
  assert.strictEqual(res.recoverable, true)
  assert.strictEqual(
    res.diagnosticFeedback,
    'Type conversion failed. Please verify column data types in expressions, joins, or filters.'
  )
})

test('Error Classification - Recoverable: AST Parse Error', () => {
  const err = new AgentClientError({
    message: 'SQL validation rejected query: PARSE_ERROR: Failed to parse SQL statement near line 1',
    kind: 'validation',
    statusCode: 400,
  })

  const res = classifyExecutionError(err)
  assert.strictEqual(res.category, 'recoverable_query_error')
  assert.strictEqual(res.recoverable, true)
  assert.strictEqual(
    res.diagnosticFeedback,
    'T-SQL syntax error: The proposed query could not be parsed as valid SQL. Please generate valid T-SQL.'
  )
})

// ==============================================================================
// 2. Terminal Non-Recoverable Errors
// ==============================================================================

test('Error Classification - Non-Recoverable: Cancellation (DOMException AbortError)', () => {
  const err = new DOMException('The user aborted a request.', 'AbortError')

  const res = classifyExecutionError(err)
  assert.strictEqual(res.category, 'cancellation')
  assert.strictEqual(res.recoverable, false)
  assert.strictEqual(res.diagnosticFeedback, null)
})

test('Error Classification - Non-Recoverable: Timeout (HTTP 504 / kind timeout)', () => {
  const err = new AgentClientError({
    message: 'Query execution timed out after 15 seconds.',
    kind: 'timeout',
    statusCode: 504,
  })

  const res = classifyExecutionError(err)
  assert.strictEqual(res.category, 'timeout')
  assert.strictEqual(res.recoverable, false)
  assert.strictEqual(res.diagnosticFeedback, null)
})

test('Error Classification - Non-Recoverable: Authentication (HTTP 401)', () => {
  const err = new AgentClientError({
    message: 'Agent session invalid or expired.',
    kind: 'authentication',
    statusCode: 401,
  })

  const res = classifyExecutionError(err)
  assert.strictEqual(res.category, 'authentication')
  assert.strictEqual(res.recoverable, false)
  assert.strictEqual(res.diagnosticFeedback, null)
})

test('Error Classification - Non-Recoverable: Authorization (HTTP 403)', () => {
  const err = new AgentClientError({
    message: 'Access forbidden: profile database mismatch.',
    kind: 'forbidden',
    statusCode: 403,
  })

  const res = classifyExecutionError(err)
  assert.strictEqual(res.category, 'authorization')
  assert.strictEqual(res.recoverable, false)
  assert.strictEqual(res.diagnosticFeedback, null)
})

test('Error Classification - Non-Recoverable: Disconnected / Network Error', () => {
  const err = new AgentClientError({
    message: 'Unable to connect to QueryPilot Local Agent daemon on loopback.',
    kind: 'disconnected',
  })

  const res = classifyExecutionError(err)
  assert.strictEqual(res.category, 'connection')
  assert.strictEqual(res.recoverable, false)
  assert.strictEqual(res.diagnosticFeedback, null)
})

test('Error Classification - Non-Recoverable: Generic Fetch Network Failure', () => {
  const err = new TypeError('Failed to fetch')

  const res = classifyExecutionError(err)
  assert.strictEqual(res.category, 'connection')
  assert.strictEqual(res.recoverable, false)
  assert.strictEqual(res.diagnosticFeedback, null)
})

test('Error Classification - Non-Recoverable: Safety Policy Rejection (NOT_READ_ONLY)', () => {
  const err = new AgentClientError({
    message: 'SQL validation rejected query: NOT_READ_ONLY: Data modification statements are strictly forbidden.',
    kind: 'validation',
    statusCode: 400,
  })

  const res = classifyExecutionError(err)
  assert.strictEqual(res.category, 'policy_rejection')
  assert.strictEqual(res.recoverable, false)
  assert.strictEqual(res.diagnosticFeedback, null)
})

test('Error Classification - Non-Recoverable: Safety Policy Rejection (SENSITIVE_COLUMN)', () => {
  const err = new AgentClientError({
    message: 'SQL validation rejected query: SENSITIVE_COLUMN: Query attempts to access forbidden credential column PasswordHash.',
    kind: 'validation',
    statusCode: 400,
  })

  const res = classifyExecutionError(err)
  assert.strictEqual(res.category, 'policy_rejection')
  assert.strictEqual(res.recoverable, false)
  assert.strictEqual(res.diagnosticFeedback, null)
})

test('Error Classification - Non-Recoverable: Forbidden Table by Profile Policy', () => {
  const err = new AgentClientError({
    message: "Access to table 'SecretAudits' is forbidden by profile policy.",
    kind: 'validation',
    statusCode: 400,
  })

  const res = classifyExecutionError(err)
  assert.strictEqual(res.category, 'policy_rejection')
  assert.strictEqual(res.recoverable, false)
  assert.strictEqual(res.diagnosticFeedback, null)
})

// ==============================================================================
// 3. Fallback and Unknown Error Handling
// ==============================================================================

test('Error Classification - Unknown Error Defaults to Non-Repairable', () => {
  const err = new AgentClientError({
    message: 'Internal server error: Driver memory allocation failed.',
    kind: 'server',
    statusCode: 500,
  })

  const res = classifyExecutionError(err)
  assert.strictEqual(res.category, 'unknown')
  assert.strictEqual(res.recoverable, false)
  assert.strictEqual(res.diagnosticFeedback, null)
})

test('Error Classification - Arbitrary primitive or null error defaults safely', () => {
  assert.deepStrictEqual(classifyExecutionError(null), {
    category: 'unknown',
    recoverable: false,
    diagnosticFeedback: null,
    userMessage: 'An unexpected or unclassified execution error occurred.',
  })

  assert.deepStrictEqual(classifyExecutionError('unexpected string failure'), {
    category: 'unknown',
    recoverable: false,
    diagnosticFeedback: null,
    userMessage: 'An unexpected or unclassified execution error occurred.',
  })
})

// ==============================================================================
// 4. Privacy & Leak Prevention
// ==============================================================================

test('Error Classification - No raw driver details, tokens, or connection strings in feedback', () => {
  const driverNoiseErr = new AgentClientError({
    message: "Database execution error: (pyodbc.ProgrammingError) ('42S22', \"[42S22] [Microsoft][ODBC Driver 18 for SQL Server][SQL Server]Invalid column name 'Bonus'. (207) (SQLExecDirectW)\")",
    kind: 'server',
    statusCode: 500,
  })

  const res1 = classifyExecutionError(driverNoiseErr)
  assert.strictEqual(res1.category, 'recoverable_query_error')
  assert.strictEqual(res1.recoverable, true)
  assert.strictEqual(
    res1.diagnosticFeedback,
    "Invalid column name: 'Bonus'. Please query only valid columns defined in the schema."
  )
  assert.ok(!res1.diagnosticFeedback.includes('pyodbc'))
  assert.ok(!res1.diagnosticFeedback.includes('ODBC Driver 18'))
  assert.ok(!res1.diagnosticFeedback.includes('SQLExecDirectW'))

  // Semicolons / credential noise in raw message triggers safe generic hint
  const sensitiveErr = new AgentClientError({
    message: "Database execution error: Server=prod-sql.internal,1433;UID=admin;PWD=SecretPass123; Invalid column name 'Bonus'. (207)",
    kind: 'server',
    statusCode: 500,
  })

  const res2 = classifyExecutionError(sensitiveErr)
  assert.strictEqual(res2.category, 'recoverable_query_error')
  assert.strictEqual(res2.recoverable, true)
  assert.strictEqual(
    res2.diagnosticFeedback,
    'Invalid column name referenced. Please query only valid columns defined in the schema.'
  )
  assert.ok(!res2.diagnosticFeedback.includes('SecretPass123'))
  assert.ok(!res2.diagnosticFeedback.includes('prod-sql.internal'))
  assert.ok(!res2.userMessage.includes('SecretPass123'))
  assert.ok(!res2.userMessage.includes('prod-sql.internal'))
})

test('Error Classification - Rejects suspicious identifier containing SQL injection', () => {
  const maliciousErr = new AgentClientError({
    message: "Database execution error: Invalid column name 'Col'; DROP TABLE Students;--'. (207)",
    kind: 'server',
    statusCode: 500,
  })

  const res = classifyExecutionError(maliciousErr)
  assert.strictEqual(res.category, 'recoverable_query_error')
  assert.strictEqual(res.recoverable, true)
  // Suspicious identifier containing quotes or semicolons must NOT be embedded in diagnostic feedback
  assert.strictEqual(
    res.diagnosticFeedback,
    'Invalid column name referenced. Please query only valid columns defined in the schema.'
  )
  assert.ok(!res.diagnosticFeedback.includes('DROP TABLE'))
})

test('Error Classification - Pure function without network side effects', () => {
  const fetchCalls: unknown[] = []
  const originalFetch = globalThis.fetch
  globalThis.fetch = ((...args: unknown[]) => {
    fetchCalls.push(args)
    return Promise.reject(new Error('fetch should not be called'))
  }) as unknown as typeof fetch

  try {
    const err = new AgentClientError({
      message: "Database execution error: Invalid column name 'Age'.",
      kind: 'server',
      statusCode: 500,
    })
    const res = classifyExecutionError(err)
    assert.strictEqual(res.recoverable, true)
    assert.strictEqual(fetchCalls.length, 0, 'classifyExecutionError must not invoke fetch')
  } finally {
    globalThis.fetch = originalFetch
  }
})
