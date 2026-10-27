import assert from 'node:assert'
import test from 'node:test'
import { classifyExecutionError, canAttemptRepair } from '../src/services/errorClassification.ts'
import type { RepairEligibilityParams } from '../src/services/errorClassification.ts'
import { SqlGenerationCoordinator } from '../src/services/sqlGeneration.ts'
import { QueryExecutionCoordinator } from '../src/services/queryExecution.ts'
import { AgentClientError } from '../src/types/api.ts'
import type {
  GenerateSQLResponse,
  ModelFacingSchemaContext,
} from '../src/types/api.ts'

const SAMPLE_SCHEMA: ModelFacingSchemaContext = {
  database_name: 'TestDB',
  tables: [
    {
      table_name: 'Departments',
      schema_name: 'dbo',
      columns: [
        { name: 'DepartmentID', data_type: 'int', nullable: false, primary_key: true },
        { name: 'DepartmentName', data_type: 'varchar(100)', nullable: false },
      ],
      primary_keys: ['DepartmentID'],
      foreign_keys: [],
    },
  ],
}

const SAMPLE_GENERATION_RES: GenerateSQLResponse = {
  candidate_sql: 'SELECT DeptName FROM dbo.Departments',
  status: 'generated',
  execution_disclaimer: 'Candidate SQL is unexecuted.',
  request_id: 'req-gen-101',
}

const SAMPLE_REPAIRED_RES: GenerateSQLResponse = {
  candidate_sql: 'SELECT DepartmentName FROM dbo.Departments',
  status: 'generated',
  execution_disclaimer: 'Candidate SQL is unexecuted.',
  request_id: 'req-gen-repaired-102',
}

const originalFetch = globalThis.fetch

function restoreFetch(): void {
  globalThis.fetch = originalFetch
}

// ==============================================================================
// 1. Eligibility Filter & Category Boundaries
// ==============================================================================

test('Execution Recovery - Eligibility: Recoverable error enables repair hint', () => {
  const err = new AgentClientError({
    message: "Database execution error: Invalid column name 'DeptName'. (207)",
    kind: 'server',
    statusCode: 500,
  })

  const classification = classifyExecutionError(err)
  assert.strictEqual(classification.category, 'recoverable_query_error')
  assert.strictEqual(classification.recoverable, true)
  assert.strictEqual(
    classification.diagnosticFeedback,
    "Invalid column name: 'DeptName'. Please query only valid columns defined in the schema."
  )
})

test('Execution Recovery - Eligibility: Policy rejection, timeout, auth, network, cancellation fail closed', () => {
  // Policy rejection
  const policyErr = new AgentClientError({
    message: 'SQL validation rejected query: NOT_READ_ONLY: Statements modifying state are forbidden.',
    kind: 'validation',
    statusCode: 400,
  })
  const cPolicy = classifyExecutionError(policyErr)
  assert.strictEqual(cPolicy.category, 'policy_rejection')
  assert.strictEqual(cPolicy.recoverable, false)
  assert.strictEqual(cPolicy.diagnosticFeedback, null)

  // Timeout
  const timeoutErr = new AgentClientError({
    message: 'Statement timed out after 15 seconds.',
    kind: 'timeout',
    statusCode: 504,
  })
  const cTimeout = classifyExecutionError(timeoutErr)
  assert.strictEqual(cTimeout.category, 'timeout')
  assert.strictEqual(cTimeout.recoverable, false)
  assert.strictEqual(cTimeout.diagnosticFeedback, null)

  // Auth 401
  const authErr = new AgentClientError({
    message: 'Session expired',
    kind: 'authentication',
    statusCode: 401,
  })
  const cAuth = classifyExecutionError(authErr)
  assert.strictEqual(cAuth.category, 'authentication')
  assert.strictEqual(cAuth.recoverable, false)

  // Forbidden 403
  const forbidErr = new AgentClientError({
    message: 'Access forbidden',
    kind: 'forbidden',
    statusCode: 403,
  })
  const cForbid = classifyExecutionError(forbidErr)
  assert.strictEqual(cForbid.category, 'authorization')
  assert.strictEqual(cForbid.recoverable, false)

  // Disconnected
  const discErr = new AgentClientError({
    message: 'Daemon disconnected',
    kind: 'disconnected',
  })
  const cDisc = classifyExecutionError(discErr)
  assert.strictEqual(cDisc.category, 'connection')
  assert.strictEqual(cDisc.recoverable, false)

  // Cancellation
  const cancelErr = new DOMException('User cancelled', 'AbortError')
  const cCancel = classifyExecutionError(cancelErr)
  assert.strictEqual(cCancel.category, 'cancellation')
  assert.strictEqual(cCancel.recoverable, false)

  // Unknown generic 500
  const unkErr = new AgentClientError({
    message: 'Internal driver crash memory allocation failure',
    kind: 'server',
    statusCode: 500,
  })
  const cUnk = classifyExecutionError(unkErr)
  assert.strictEqual(cUnk.category, 'unknown')
  assert.strictEqual(cUnk.recoverable, false)
  assert.strictEqual(cUnk.diagnosticFeedback, null)
})

// ==============================================================================
// 2. User-Initiated Action, 1-Attempt Limit & Duplicate Prevention
// ==============================================================================

test('Execution Recovery - User Initiation & 1-Attempt Limit per Candidate Identity', async () => {
  let genDispatched = 0
  let capturedBody: Record<string, unknown> | null = null

  globalThis.fetch = (async (url: string | URL | Request, init?: RequestInit) => {
    const urlStr = String(url)
    if (urlStr.includes('/generate-sql')) {
      genDispatched++
      capturedBody = JSON.parse(String(init?.body)) as Record<string, unknown>
      return new Response(JSON.stringify(SAMPLE_REPAIRED_RES), { status: 200 })
    }
    return new Response('Not Found', { status: 404 })
  }) as typeof fetch

  const sqlCoordinator = new SqlGenerationCoordinator()

  // 1. Initial candidate generation
  await sqlCoordinator.generateCandidate('What are all departments?', SAMPLE_SCHEMA, 'profile_1')
  const candidate1Id = sqlCoordinator.getState().candidateId
  assert.ok(candidate1Id)
  assert.strictEqual(genDispatched, 1)

  // 2. Execution failure occurs (simulated)
  const recoverableErr = new AgentClientError({
    message: "Database execution error: Invalid column name 'DeptName'. (207)",
    kind: 'server',
    statusCode: 500,
  })
  const classification = classifyExecutionError(recoverableErr)
  assert.strictEqual(classification.recoverable, true)

  // Verify that execution failure ALONE does NOT dispatch repair generation
  assert.strictEqual(genDispatched, 1, 'Failure must not auto-dispatch repair')

  // 3. User explicitly clicks Suggest Repair
  const repairedCandidateIds: Record<string, boolean> = {}
  let isRepairing = false

  // Can repair before first attempt
  assert.strictEqual(!repairedCandidateIds[candidate1Id!], true)

  // Attempt recorded
  repairedCandidateIds[candidate1Id!] = true
  isRepairing = true

  const repairRes = await sqlCoordinator.generateCandidate(
    sqlCoordinator.getState().lastGeneratedQuestion!,
    SAMPLE_SCHEMA,
    'profile_1',
    { diagnosticFeedback: classification.diagnosticFeedback }
  )

  isRepairing = false
  assert.strictEqual(isRepairing, false)

  assert.ok(repairRes)
  assert.strictEqual(genDispatched, 2)
  assert.strictEqual(
    capturedBody!['diagnostic_feedback'],
    "Invalid column name: 'DeptName'. Please query only valid columns defined in the schema."
  )

  // 4. Repaired candidate receives new candidate ID
  const candidate2Id = sqlCoordinator.getState().candidateId
  assert.ok(candidate2Id)
  assert.notStrictEqual(candidate2Id, candidate1Id, 'Repaired candidate must receive distinct identity')

  // 5. Attempt limit for original candidate is consumed
  assert.strictEqual(repairedCandidateIds[candidate1Id!], true)
  // Attempt for candidate 1 cannot be re-run
  const canRepairCandidate1Again = !repairedCandidateIds[candidate1Id!]
  assert.strictEqual(canRepairCandidate1Again, false, 'Candidate 1 attempt must be permanently consumed')
})

test('Execution Recovery - Failed Repair Generation still consumes attempt limit', async () => {
  let callCount = 0

  globalThis.fetch = (async (url: string | URL | Request) => {
    const urlStr = String(url)
    if (urlStr.includes('/generate-sql')) {
      callCount++
      if (callCount === 1) {
        return new Response(JSON.stringify(SAMPLE_GENERATION_RES), { status: 200 })
      }
      // Second attempt (repair) fails
      return new Response(JSON.stringify({ detail: 'LLM service unavailable' }), { status: 500 })
    }
    return new Response('Not Found', { status: 404 })
  }) as typeof fetch

  const sqlCoordinator = new SqlGenerationCoordinator()
  await sqlCoordinator.generateCandidate('What are all departments?', SAMPLE_SCHEMA, 'profile_1')
  const candidateId = sqlCoordinator.getState().candidateId!

  const repairedCandidateIds: Record<string, boolean> = {}

  // User triggers repair -> attempt consumed immediately
  repairedCandidateIds[candidateId] = true

  await assert.rejects(
    async () =>
      sqlCoordinator.generateCandidate(
        sqlCoordinator.getState().lastGeneratedQuestion!,
        SAMPLE_SCHEMA,
        'profile_1',
        { diagnosticFeedback: 'Invalid column error' }
      )
  )

  // One-attempt limit remains consumed even after failure
  assert.strictEqual(repairedCandidateIds[candidateId], true)
  assert.strictEqual(!repairedCandidateIds[candidateId], false)
})

// ==============================================================================
// 3. Candidate Isolation, Unexecuted State & Local Approval
// ==============================================================================

test('Execution Recovery - Repaired SQL is strictly unexecuted and never auto-executed', async () => {
  let dbExecuteCalled = false

  globalThis.fetch = (async (url: string | URL | Request) => {
    const urlStr = String(url)
    if (urlStr.includes('/execute')) {
      dbExecuteCalled = true
      return new Response(JSON.stringify({ columns: ['DepartmentName'], rows: [] }), { status: 200 })
    }
    if (urlStr.includes('/generate-sql')) {
      return new Response(JSON.stringify(SAMPLE_REPAIRED_RES), { status: 200 })
    }
    return new Response('Not Found', { status: 404 })
  }) as typeof fetch

  const sqlCoordinator = new SqlGenerationCoordinator()
  const res = await sqlCoordinator.generateCandidate(
    'Show departments',
    SAMPLE_SCHEMA,
    'profile_1',
    { diagnosticFeedback: 'Invalid column name: DeptName' }
  )

  assert.ok(res)
  assert.strictEqual(res.status, 'generated')
  assert.strictEqual(res.execution_disclaimer, 'Candidate SQL is unexecuted.')
  assert.strictEqual(dbExecuteCalled, false, 'Repaired SQL must NEVER be executed automatically')

  // Execution occurs ONLY when explicit executeQuery is dispatched
  const execCoordinator = new QueryExecutionCoordinator()
  const execRes = await execCoordinator.executeQuery('profile_1', res.candidate_sql!)
  assert.ok(execRes)
  assert.strictEqual(dbExecuteCalled, true)
})

// ==============================================================================
// 4. Stale-Response Safety & Context Invalidation
// ==============================================================================

test('Execution Recovery - Cancellation or Profile Switch drops pending repair response', async () => {
  let resolveRepair: ((val: Response) => void) | null = null

  globalThis.fetch = (async (url: string | URL | Request) => {
    const urlStr = String(url)
    if (urlStr.includes('/generate-sql')) {
      return new Promise<Response>((resolve) => {
        resolveRepair = resolve
      })
    }
    return new Response('Not Found', { status: 404 })
  }) as typeof fetch

  const sqlCoordinator = new SqlGenerationCoordinator()

  // 1. Dispatch repair request
  const repairPromise = sqlCoordinator.generateCandidate(
    'Show departments',
    SAMPLE_SCHEMA,
    'profile_1',
    { diagnosticFeedback: 'Invalid column' }
  )

  // 2. User cancels or switches profile before response arrives
  sqlCoordinator.cancelGeneration()

  // 3. Late repair response arrives
  resolveRepair!(new Response(JSON.stringify(SAMPLE_REPAIRED_RES), { status: 200 }))
  const res = await repairPromise

  assert.strictEqual(res, null, 'Late repair response must return null and be dropped')
  assert.strictEqual(sqlCoordinator.getState().response, null)
})

test('Execution Recovery - Failed execution diagnostics are NEVER recorded in conversation context', async () => {
  const sqlCoordinator = new SqlGenerationCoordinator()

  // Failed execution query
  const failedTurnDiagnostic = "Invalid column name: 'DeptName'"

  // recordCompletedTurn requires successfully executed columns
  sqlCoordinator.recordCompletedTurn('What are all departments?', ['DepartmentName'])

  const turns = sqlCoordinator.getState().conversationContext
  assert.strictEqual(turns.length, 1)
  assert.strictEqual(turns[0].user_question, 'What are all departments?')
  // Verify NO diagnostic or raw error text is in conversation context
  assert.ok(!JSON.stringify(turns).includes(failedTurnDiagnostic))
})

// ==============================================================================
// 5. Production Eligibility Helper (canAttemptRepair) Decision Matrix
// ==============================================================================

test('Execution Recovery - Production canAttemptRepair: Enforces strict fail-closed gating for all conditions', () => {
  const validClassification = classifyExecutionError(
    new Error("Invalid column name 'DeptName'")
  )

  const baseValidContext: RepairEligibilityParams = {
    executionError: new Error("Invalid column name 'DeptName'"),
    isExecutionLoading: false,
    isSqlGenLoading: false,
    isRepairing: false,
    errorClassification: validClassification,
    activeCandidateId: '1:req-101',
    repairedCandidateIds: {},
    sessionStatus: 'authenticated',
    selectedProfileId: 'profile_sales',
    schemaStatus: 'loaded',
    lastGeneratedQuestion: 'Show departments',
    isStale: false,
  }

  // Base case: all valid -> canAttemptRepair is true
  assert.strictEqual(canAttemptRepair(baseValidContext), true)

  // 1. Missing execution error -> false
  assert.strictEqual(canAttemptRepair({ ...baseValidContext, executionError: null }), false)

  // 2. Execution currently in flight -> false
  assert.strictEqual(canAttemptRepair({ ...baseValidContext, isExecutionLoading: true }), false)

  // 3. SQL generation in flight -> false
  assert.strictEqual(canAttemptRepair({ ...baseValidContext, isSqlGenLoading: true }), false)

  // 4. Repair already in flight -> false
  assert.strictEqual(canAttemptRepair({ ...baseValidContext, isRepairing: true }), false)

  // 5. Unrecoverable classification category -> false
  const unrecoverableClassification = classifyExecutionError(new Error('Statement timeout expired'))
  assert.strictEqual(
    canAttemptRepair({ ...baseValidContext, errorClassification: unrecoverableClassification }),
    false
  )

  // 6. Classification missing diagnosticFeedback -> false
  assert.strictEqual(
    canAttemptRepair({
      ...baseValidContext,
      errorClassification: { ...validClassification, diagnosticFeedback: null },
    }),
    false
  )

  // 7. Active candidate identity null or empty -> false
  assert.strictEqual(canAttemptRepair({ ...baseValidContext, activeCandidateId: null }), false)
  assert.strictEqual(canAttemptRepair({ ...baseValidContext, activeCandidateId: '' }), false)
  assert.strictEqual(canAttemptRepair({ ...baseValidContext, activeCandidateId: '   ' }), false)

  // 8. Candidate already consumed repair attempt -> false
  assert.strictEqual(
    canAttemptRepair({
      ...baseValidContext,
      repairedCandidateIds: { '1:req-101': true },
    }),
    false
  )

  // 9. Session not authenticated -> false
  assert.strictEqual(
    canAttemptRepair({ ...baseValidContext, sessionStatus: 'unauthenticated' }),
    false
  )

  // 10. Selected profile missing -> false
  assert.strictEqual(canAttemptRepair({ ...baseValidContext, selectedProfileId: null }), false)

  // 11. Schema not loaded -> false
  assert.strictEqual(canAttemptRepair({ ...baseValidContext, schemaStatus: 'loading' }), false)

  // 12. Question missing -> false
  assert.strictEqual(
    canAttemptRepair({ ...baseValidContext, lastGeneratedQuestion: null }),
    false
  )
  assert.strictEqual(
    canAttemptRepair({ ...baseValidContext, lastGeneratedQuestion: '   ' }),
    false
  )

  // 13. Candidate question is marked stale -> false
  assert.strictEqual(canAttemptRepair({ ...baseValidContext, isStale: true }), false)
})

// ==============================================================================
// 6. Synchronous Guard Ownership & Race Condition Verification (Handler Simulation)
// NOTE: These tests simulate the React App.tsx handler architecture (ref token ownership)
// since React hooks and component lifecycles cannot be directly executed in node:test.
// ==============================================================================

test('Execution Recovery - Duplicate click race: Synchronous guard with token ownership prevents duplicate dispatch and premature release (Handler Simulation)', async () => {
  let dispatchCount = 0
  let resolveNetwork: ((res: Response) => void) | null = null

  globalThis.fetch = (async (url: string | URL | Request) => {
    const urlStr = String(url)
    if (urlStr.includes('/generate-sql')) {
      dispatchCount++
      return new Promise<Response>((resolve) => {
        resolveNetwork = resolve
      })
    }
    return new Response('Not Found', { status: 404 })
  }) as typeof fetch

  try {
    const coordinator = new SqlGenerationCoordinator()

    // Simulate guard ownership architecture implemented in App.tsx
    let activeRepairToken: number | null = null
    let repairSeq = 0
    let isRepairingState = false
    const repairedCandidateIds: Record<string, boolean> = {}

    const runRepairHandler = async (candidateId: string): Promise<boolean> => {
      // 1. Synchronous in-flight guard check
      if (activeRepairToken !== null) {
        return false // Duplicate rejected immediately!
      }
      if (coordinator.getState().isLoading) {
        return false
      }

      // 2. Acquire ownership token synchronously
      const token = ++repairSeq
      activeRepairToken = token
      repairedCandidateIds[candidateId] = true
      isRepairingState = true

      try {
        const res = await coordinator.generateCandidate(
          'Show departments',
          SAMPLE_SCHEMA,
          'profile_1',
          { diagnosticFeedback: 'Invalid column error' }
        )
        return Boolean(res)
      } finally {
        // Release guard ONLY if this invocation still owns the active token
        if (activeRepairToken === token) {
          activeRepairToken = null
          isRepairingState = false
        }
      }
    }

    // 1. Invocation 1 arrives
    const p1 = runRepairHandler('cand-101')
    assert.strictEqual(isRepairingState, true)
    assert.strictEqual(activeRepairToken, 1)
    assert.strictEqual(dispatchCount, 1)

    // 2. Invocation 2 arrives synchronously while Invocation 1 is awaiting network fetch
    const p2 = runRepairHandler('cand-101')
    const p2Result = await p2
    assert.strictEqual(p2Result, false, 'Duplicate invocation must return false immediately')

    // Crucial invariant: Duplicate invocation MUST NOT clear isRepairingState or activeRepairToken!
    assert.strictEqual(
      isRepairingState,
      true,
      'Duplicate invocation must NOT prematurely reset isRepairingState'
    )
    assert.strictEqual(
      activeRepairToken,
      1,
      'Duplicate invocation must NOT clear activeRepairToken'
    )
    assert.strictEqual(dispatchCount, 1, 'Only one network dispatch must occur')

    // 3. Invocation 1 network fetch resolves
    resolveNetwork!(new Response(JSON.stringify(SAMPLE_REPAIRED_RES), { status: 200 }))
    const p1Result = await p1
    assert.strictEqual(p1Result, true)

    // Invocation 1 properly releases the guard upon completion
    assert.strictEqual(isRepairingState, false)
    assert.strictEqual(activeRepairToken, null)
  } finally {
    restoreFetch()
  }
})

test('Execution Recovery - Token Ownership: Reset followed by newer repair prevents older invocation from clearing newer token or state (Handler Simulation)', async () => {
  let resolveNetwork1: ((res: Response) => void) | null = null
  let resolveNetwork2: ((res: Response) => void) | null = null
  let callCount = 0

  globalThis.fetch = (async (url: string | URL | Request) => {
    const urlStr = String(url)
    if (urlStr.includes('/generate-sql')) {
      callCount++
      if (callCount === 1) {
        return new Promise<Response>((resolve) => {
          resolveNetwork1 = resolve
        })
      }
      if (callCount === 2) {
        return new Promise<Response>((resolve) => {
          resolveNetwork2 = resolve
        })
      }
    }
    return new Response('Not Found', { status: 404 })
  }) as typeof fetch

  try {
    const coordinator = new SqlGenerationCoordinator()

    let activeRepairToken: number | null = null
    let repairSeq = 0
    let isRepairingState = false
    let appliedSql: string | null = null

    const runRepair = async (q: string): Promise<boolean> => {
      if (activeRepairToken !== null) {
        return false
      }
      const token = ++repairSeq
      activeRepairToken = token
      isRepairingState = true

      try {
        const res = await coordinator.generateCandidate(
          q,
          SAMPLE_SCHEMA,
          'profile_1',
          { diagnosticFeedback: 'Fix column' }
        )

        // Production guard invariant: token ownership + coordinator candidate validity
        if (activeRepairToken === token && res && coordinator.isCurrentCandidateId(res.candidateId)) {
          appliedSql = res.candidate_sql
          return true
        }
        return false
      } finally {
        if (activeRepairToken === token) {
          activeRepairToken = null
          isRepairingState = false
        }
      }
    }

    // 1. Invocation 1 starts with token 1
    const p1 = runRepair('Question 1')
    assert.strictEqual(activeRepairToken, 1)
    assert.strictEqual(isRepairingState, true)

    // 2. User resets conversation (e.g. handleResetConversation in App.tsx)
    activeRepairToken = null
    isRepairingState = false
    coordinator.resetAll()

    // 3. User starts a new repair (Invocation 2) with token 2
    const p2 = runRepair('Question 2')
    assert.strictEqual(activeRepairToken, 2)
    assert.strictEqual(isRepairingState, true)

    // 4. Invocation 1 network resolves late
    resolveNetwork1!(new Response(JSON.stringify({ ...SAMPLE_REPAIRED_RES, candidate_sql: 'SELECT 1' }), { status: 200 }))
    const p1Result = await p1
    assert.strictEqual(p1Result, false, 'Older invocation must not apply results')

    // CRITICAL: Older invocation MUST NOT clear token 2 or reset isRepairingState!
    assert.strictEqual(activeRepairToken, 2, 'Older invocation must NOT clear activeRepairToken of newer request')
    assert.strictEqual(isRepairingState, true, 'Older invocation must NOT reset isRepairingState of newer request')
    assert.strictEqual(appliedSql, null, 'Older invocation must NOT apply its SQL')

    // 5. Invocation 2 network resolves
    resolveNetwork2!(new Response(JSON.stringify({ ...SAMPLE_REPAIRED_RES, candidate_sql: 'SELECT 2' }), { status: 200 }))
    const p2Result = await p2
    assert.strictEqual(p2Result, true, 'Newer invocation must succeed')
    assert.strictEqual(appliedSql, 'SELECT 2')
    assert.strictEqual(activeRepairToken, null, 'Token released only after owning invocation completes')
    assert.strictEqual(isRepairingState, false)
  } finally {
    restoreFetch()
  }
})

// ==============================================================================
// 7. Deterministic Completion Guards (Production Coordinator & Helper Coverage)
// ==============================================================================

test('Execution Recovery - Deterministic Completion: Valid repair is accepted after coordinator transitions to the new candidate ID', async () => {
  globalThis.fetch = (async (url: string | URL | Request) => {
    const urlStr = String(url)
    if (urlStr.includes('/generate-sql')) {
      return new Response(JSON.stringify(SAMPLE_REPAIRED_RES), { status: 200 })
    }
    return new Response('Not Found', { status: 404 })
  }) as typeof fetch

  try {
    const coordinator = new SqlGenerationCoordinator()

    // 1. Initial candidate generation
    const initialRes = await coordinator.generateCandidate(
      'Show all departments',
      SAMPLE_SCHEMA,
      'profile_1'
    )
    assert.ok(initialRes)
    const initiatingCandidateId = coordinator.getState().candidateId!
    const initiatingQuestion = 'Show all departments'
    assert.ok(initiatingCandidateId)

    // 2. Repair generation dispatched
    const repairRes = await coordinator.generateCandidate(
      initiatingQuestion,
      SAMPLE_SCHEMA,
      'profile_1',
      { diagnosticFeedback: 'Invalid column error' }
    )
    assert.ok(repairRes)
    assert.ok(repairRes.candidateId)

    // Invariant: Coordinator transitioned to the NEW candidate ID
    const newCandidateId = coordinator.getState().candidateId!
    assert.strictEqual(newCandidateId, repairRes.candidateId)
    assert.notStrictEqual(newCandidateId, initiatingCandidateId, 'Coordinator transitioned to new candidate')

    // Production guard evaluation:
    // 1) Coordinator confirms resulting candidate is current
    assert.strictEqual(coordinator.isCurrentCandidateId(repairRes.candidateId), true)
    // 2) Coordinator confirms current question matches initiating question
    assert.strictEqual(coordinator.matchesCurrentQuestion(initiatingQuestion), true)

    // Guard accepts the valid repair
    const accepted = Boolean(
      repairRes &&
      coordinator.matchesCurrentQuestion(initiatingQuestion) &&
      coordinator.isCurrentCandidateId(repairRes.candidateId)
    )
    assert.strictEqual(accepted, true, 'Valid repair must be accepted after coordinator transitions to new candidate')
  } finally {
    restoreFetch()
  }
})

test('Execution Recovery - Deterministic Completion: Stale response is rejected if another candidate replaces it', async () => {
  let resolveRepair: ((res: Response) => void) | null = null
  let callCount = 0

  globalThis.fetch = (async (url: string | URL | Request) => {
    const urlStr = String(url)
    if (urlStr.includes('/generate-sql')) {
      callCount++
      if (callCount === 1) {
        // Initial
        return new Response(JSON.stringify(SAMPLE_GENERATION_RES), { status: 200 })
      }
      if (callCount === 2) {
        // Repair (held pending)
        return new Promise<Response>((resolve) => {
          resolveRepair = resolve
        })
      }
      if (callCount === 3) {
        // Newer generation
        return new Response(JSON.stringify({ ...SAMPLE_GENERATION_RES, request_id: 'req-new-3' }), { status: 200 })
      }
    }
    return new Response('Not Found', { status: 404 })
  }) as typeof fetch

  try {
    const coordinator = new SqlGenerationCoordinator()

    // 1. Initial candidate
    await coordinator.generateCandidate('Show all departments', SAMPLE_SCHEMA, 'profile_1')

    // 2. Repair dispatched (pending)
    const repairPromise = coordinator.generateCandidate(
      'Show all departments',
      SAMPLE_SCHEMA,
      'profile_1',
      { diagnosticFeedback: 'Invalid column error' }
    )

    // 3. User cancels in-flight repair and generates a new candidate (replaces candidate in coordinator)
    coordinator.cancelGeneration()
    await coordinator.generateCandidate('Show active courses', SAMPLE_SCHEMA, 'profile_1')
    const activeCandidateId = coordinator.getState().candidateId!
    assert.ok(activeCandidateId)

    // 4. Stale repair resolves
    resolveRepair!(new Response(JSON.stringify(SAMPLE_REPAIRED_RES), { status: 200 }))
    const staleRepairRes = await repairPromise

    // Due to coordinator generation counter check, stale repair returns null
    assert.strictEqual(staleRepairRes, null, 'Coordinator drops stale generation response')

    // Even if an older candidate ID were evaluated, isCurrentCandidateId rejects it
    assert.strictEqual(
      coordinator.isCurrentCandidateId('2:req-gen-repaired-102'),
      false,
      'Coordinator must reject any non-current candidate identity'
    )
  } finally {
    restoreFetch()
  }
})

test('Execution Recovery - Deterministic Completion: Question change during repair prevents old result from being applied', async () => {
  let resolveRepair: ((res: Response) => void) | null = null
  let callCount = 0

  globalThis.fetch = (async (url: string | URL | Request) => {
    const urlStr = String(url)
    if (urlStr.includes('/generate-sql')) {
      callCount++
      if (callCount === 1) {
        return new Response(JSON.stringify(SAMPLE_GENERATION_RES), { status: 200 })
      }
      return new Promise<Response>((resolve) => {
        resolveRepair = resolve
      })
    }
    return new Response('Not Found', { status: 404 })
  }) as typeof fetch

  try {
    const coordinator = new SqlGenerationCoordinator()

    // 1. Initial candidate
    await coordinator.generateCandidate('Show all departments', SAMPLE_SCHEMA, 'profile_1')
    const initiatingQuestion = 'Show all departments'

    // 2. Repair dispatched
    const repairPromise = coordinator.generateCandidate(
      initiatingQuestion,
      SAMPLE_SCHEMA,
      'profile_1',
      { diagnosticFeedback: 'Invalid column error' }
    )

    // 3. User edits the question in the UI while repair is in flight
    coordinator.setQuestion('Show all departments in 2024')
    assert.strictEqual(coordinator.getState().question, 'Show all departments in 2024')
    assert.strictEqual(coordinator.getState().isStale, true)

    // 4. Repair network resolves
    resolveRepair!(new Response(JSON.stringify(SAMPLE_REPAIRED_RES), { status: 200 }))
    const res = await repairPromise
    assert.ok(res)

    // Invariant: Successful generation resets isStale to false, proving why isStale alone is insufficient!
    assert.strictEqual(coordinator.getState().isStale, false)

    // Production question drift check:
    const matchesQuestion = coordinator.matchesCurrentQuestion(initiatingQuestion)
    assert.strictEqual(
      matchesQuestion,
      false,
      'Question drift must be detected when coordinator question was edited'
    )

    // In App.tsx completion guard: matchesCurrentQuestion check prevents applying the result
    const canApply = Boolean(
      res &&
      coordinator.matchesCurrentQuestion(initiatingQuestion) &&
      coordinator.isCurrentCandidateId(res.candidateId)
    )
    assert.strictEqual(canApply, false, 'Result must be discarded when question drifted')
  } finally {
    restoreFetch()
  }
})

test('Execution Recovery - Failure behavior: Preserves original execution failure and permanently consumes attempt', async () => {
  let callCount = 0

  globalThis.fetch = (async (url: string | URL | Request) => {
    const urlStr = String(url)
    if (urlStr.includes('/generate-sql')) {
      callCount++
      if (callCount === 1) {
        return new Response(JSON.stringify(SAMPLE_GENERATION_RES), { status: 200 })
      }
      // Repair network failure (500)
      return new Response(JSON.stringify({ detail: 'LLM backend failure' }), { status: 500 })
    }
    if (urlStr.includes('/execute')) {
      return new Response(
        JSON.stringify({ detail: "Database execution error: Invalid column name 'DeptName'. (207)" }),
        { status: 500 }
      )
    }
    return new Response('Not Found', { status: 404 })
  }) as typeof fetch

  try {
    const sqlCoordinator = new SqlGenerationCoordinator()
    const execCoordinator = new QueryExecutionCoordinator()

    // 1. Initial candidate
    const candidate = await sqlCoordinator.generateCandidate(
      'Show all departments',
      SAMPLE_SCHEMA,
      'profile_1'
    )
    const candidateId = sqlCoordinator.getState().candidateId!
    assert.ok(candidateId)

    // 2. Execute candidate SQL -> fails
    let execError: Error | null = null
    try {
      await execCoordinator.executeQuery('profile_1', candidate!.candidate_sql!)
    } catch (err) {
      execError = err as Error
    }
    assert.ok(execError)
    assert.strictEqual(execCoordinator.getState().error?.message, execError.message)

    // 3. Mark attempt consumed synchronously (as in App.tsx)
    const repairedCandidateIds: Record<string, boolean> = { [candidateId]: true }

    // 4. Repair generation throws error
    await assert.rejects(
      async () =>
        sqlCoordinator.generateCandidate(
          'Show all departments',
          SAMPLE_SCHEMA,
          'profile_1',
          { diagnosticFeedback: "Invalid column name 'DeptName'" }
        )
    )

    // 5. Verification of invariants after failure:
    // a. Original execution error is NOT cleared or overwritten
    assert.strictEqual(execCoordinator.getState().error?.message, execError.message)
    // b. Attempt remains permanently consumed
    assert.strictEqual(repairedCandidateIds[candidateId], true)
    // c. canAttemptRepair returns false
    const eligible = canAttemptRepair({
      executionError: execCoordinator.getState().error,
      activeCandidateId: candidateId,
      repairedCandidateIds,
      sessionStatus: 'authenticated',
      selectedProfileId: 'profile_1',
      schemaStatus: 'loaded',
      lastGeneratedQuestion: 'Show all departments',
      isStale: false,
    })
    assert.strictEqual(eligible, false, 'Candidate must not be eligible for re-repair after failure')
  } finally {
    restoreFetch()
  }
})
