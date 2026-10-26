/**
 * QueryPilot Clarification Workflow Tests (Milestone B13-F13B).
 *
 * Verifies the accessible, user-controlled clarification resolution workflow:
 * 1. Question derivation helper (deriveClarifiedQuestion) and intent preservation.
 * 2. Real API contract alignment (option_id, description, optional suggested_question, no phantom label).
 * 3. Keeping original question intact when suggested_question is absent (no intent fabrication).
 * 4. Preventing text accumulation when switching between clarification options.
 * 5. Defensive null/undefined safety on malformed runtime data.
 * 6. Graceful degradation when clarification_options is missing, undefined, or empty.
 * 7. User-controlled selection: updates question input without auto-generating SQL.
 * 8. Safety execution boundary: selecting an option never executes SQL or records a conversation turn.
 * 9. Explicit user submission required for candidate generation.
 * 10. Preservation of bounded multi-turn conversation context across clarification selection.
 */

import assert from 'node:assert'
import test from 'node:test'

import {
  deriveClarifiedQuestion,
  SqlGenerationCoordinator,
} from '../src/services/sqlGeneration.ts'
import { QueryExecutionCoordinator } from '../src/services/queryExecution.ts'
import type {
  ClarificationOption,
  GenerateSQLResponse,
  ModelFacingSchemaContext,
} from '../src/types/api.ts'

// ==============================================================================
// Test Fixtures (matching real backend contract: option_id, description, suggested_question)
// ==============================================================================

const SAMPLE_PROJECTED_SCHEMA: ModelFacingSchemaContext = {
  database_name: 'university_db',
  tables: [
    {
      schema_name: 'dbo',
      table_name: 'Students',
      columns: [
        { name: 'Id', data_type: 'int', nullable: false, primary_key: true },
        { name: 'FullName', data_type: 'nvarchar(100)', nullable: false },
        { name: 'DepartmentCode', data_type: 'nvarchar(10)', nullable: false },
        { name: 'CGPA', data_type: 'decimal(3,2)', nullable: false },
      ],
    },
    {
      schema_name: 'dbo',
      table_name: 'StudentMarks',
      columns: [
        { name: 'MarkId', data_type: 'int', nullable: false, primary_key: true },
        { name: 'StudentId', data_type: 'int', nullable: false },
        { name: 'SubjectCode', data_type: 'nvarchar(20)', nullable: false },
        { name: 'ObtainedMarks', data_type: 'decimal(5,2)', nullable: false },
        { name: 'AssessmentType', data_type: 'nvarchar(50)', nullable: false },
      ],
    },
  ],
}

const SAMPLE_CLARIFICATION_OPTIONS: ClarificationOption[] = [
  {
    option_id: 'opt_cgpa',
    description: 'Cumulative Grade Point Average (CGPA) from student records',
    suggested_question: 'Show the average CGPA of students by department',
  },
  {
    option_id: 'opt_marks',
    description: 'Coursework and exam assessment marks',
    suggested_question: 'Show the obtained assessment marks of students',
  },
  {
    option_id: 'opt_attendance',
    description: 'Classroom and laboratory attendance percentage',
    suggested_question: null,
  },
]

const SAMPLE_CLARIFICATION_RESPONSE: GenerateSQLResponse = {
  candidate_sql: null,
  status: 'clarification_needed',
  clarification_prompt: 'Multiple performance metrics exist (CGPA vs assessment marks vs attendance). Please clarify your intent.',
  clarification_options: SAMPLE_CLARIFICATION_OPTIONS,
  unsupported_reason: null,
  execution_disclaimer: 'Candidate SQL is unexecuted.',
  request_id: 'req-clar-001',
}

const originalFetch = globalThis.fetch

function restoreFetch(): void {
  globalThis.fetch = originalFetch
}

// ==============================================================================
// 1. Question Derivation & Intent Preservation Tests
// ==============================================================================

test('Clarification Workflow - deriveClarifiedQuestion', async (t) => {
  await t.test('prefers suggested_question when present and non-empty', () => {
    const derived = deriveClarifiedQuestion('Show student performance', {
      option_id: 'opt1',
      description: 'Average CGPA by department',
      suggested_question: 'Show the average CGPA of students by department',
    })
    assert.strictEqual(derived, 'Show the average CGPA of students by department')
  })

  await t.test('trims whitespace on suggested_question', () => {
    const derived = deriveClarifiedQuestion('Show student performance', {
      option_id: 'opt1',
      description: 'Average CGPA by department',
      suggested_question: '   Show CGPA   ',
    })
    assert.strictEqual(derived, 'Show CGPA')
  })

  await t.test('keeps original question intact when suggested_question is missing (does not fabricate intent)', () => {
    const derived = deriveClarifiedQuestion('Show student performance', {
      option_id: 'opt2',
      description: 'Coursework and exam marks',
    })
    // Invariant: original question is kept intact; description is NOT appended as parentheses
    assert.strictEqual(derived, 'Show student performance')
  })

  await t.test('keeps original question intact when suggested_question is null or whitespace', () => {
    const derived1 = deriveClarifiedQuestion('Show student performance', {
      option_id: 'opt2',
      description: 'Coursework marks',
      suggested_question: null,
    })
    assert.strictEqual(derived1, 'Show student performance')

    const derived2 = deriveClarifiedQuestion('Show student performance', {
      option_id: 'opt2',
      description: 'Coursework marks',
      suggested_question: '    ',
    })
    assert.strictEqual(derived2, 'Show student performance')
  })

  await t.test('switching options derives from base question without accumulating text', () => {
    const baseQuestion = 'Show student performance'

    // Option 1 has suggested_question
    const option1: ClarificationOption = {
      option_id: 'opt_cgpa',
      description: 'CGPA metrics',
      suggested_question: 'Show average CGPA by department',
    }
    const derived1 = deriveClarifiedQuestion(baseQuestion, option1)
    assert.strictEqual(derived1, 'Show average CGPA by department')

    // Option 2 has NO suggested_question -> derived from baseQuestion
    const option2: ClarificationOption = {
      option_id: 'opt_attendance',
      description: 'Attendance percentage',
      suggested_question: null,
    }
    const derived2 = deriveClarifiedQuestion(baseQuestion, option2)
    assert.strictEqual(derived2, 'Show student performance')
    assert.strictEqual(derived2.includes('CGPA'), false)
    assert.strictEqual(derived2.includes('('), false)

    // Option 3 has suggested_question -> derived from baseQuestion
    const option3: ClarificationOption = {
      option_id: 'opt_marks',
      description: 'Assessment marks',
      suggested_question: 'Show obtained marks by subject',
    }
    const derived3 = deriveClarifiedQuestion(baseQuestion, option3)
    assert.strictEqual(derived3, 'Show obtained marks by subject')
    assert.strictEqual(derived3.includes('attendance'), false)
  })

  await t.test('defensively handles null or undefined option without throwing', () => {
    assert.strictEqual(deriveClarifiedQuestion('Show metrics', null), 'Show metrics')
    assert.strictEqual(deriveClarifiedQuestion('Show metrics', undefined), 'Show metrics')
  })

  await t.test('defensively handles null, undefined, or empty question string without throwing', () => {
    const optionWithSuggested: ClarificationOption = {
      option_id: 'opt1',
      description: 'Desc',
      suggested_question: 'Show all students',
    }
    assert.strictEqual(deriveClarifiedQuestion(null, optionWithSuggested), 'Show all students')
    assert.strictEqual(deriveClarifiedQuestion(undefined, optionWithSuggested), 'Show all students')
    assert.strictEqual(deriveClarifiedQuestion('', optionWithSuggested), 'Show all students')

    const optionWithoutSuggested: ClarificationOption = {
      option_id: 'opt2',
      description: 'Desc',
    }
    assert.strictEqual(deriveClarifiedQuestion(null, optionWithoutSuggested), '')
    assert.strictEqual(deriveClarifiedQuestion(undefined, optionWithoutSuggested), '')
  })
})

// ==============================================================================
// 2. Coordinator & Response Contract Handling
// ==============================================================================

test('Clarification Workflow - Response Handling & State Updates', async (t) => {
  t.afterEach(() => {
    restoreFetch()
  })

  await t.test('handles clarification_needed response with structured clarification_options', async () => {
    globalThis.fetch = (async () => {
      return new Response(JSON.stringify(SAMPLE_CLARIFICATION_RESPONSE), { status: 200 })
    }) as typeof fetch

    const coordinator = new SqlGenerationCoordinator()
    const res = await coordinator.generateCandidate(
      'Show student performance',
      SAMPLE_PROJECTED_SCHEMA,
      'university_db'
    )

    assert.ok(res)
    assert.strictEqual(res.status, 'clarification_needed')
    assert.strictEqual(res.candidate_sql, null)
    assert.strictEqual(res.clarification_prompt, SAMPLE_CLARIFICATION_RESPONSE.clarification_prompt)
    assert.ok(Array.isArray(res.clarification_options))
    assert.strictEqual(res.clarification_options.length, 3)
    assert.strictEqual(res.clarification_options[0].option_id, 'opt_cgpa')
    assert.strictEqual(res.clarification_options[0].suggested_question, 'Show the average CGPA of students by department')
    assert.strictEqual(res.clarification_options[1].option_id, 'opt_marks')
    assert.strictEqual(res.clarification_options[2].option_id, 'opt_attendance')
    assert.strictEqual(res.clarification_options[2].suggested_question, null)

    const state = coordinator.getState()
    assert.strictEqual(state.isLoading, false)
    assert.strictEqual(state.error, null)
    assert.strictEqual(state.isStale, false)
    assert.deepStrictEqual(state.response?.clarification_options, SAMPLE_CLARIFICATION_OPTIONS)
  })

  await t.test('degrades gracefully when clarification_options is missing in response', async () => {
    const responseWithoutOptions: GenerateSQLResponse = {
      candidate_sql: null,
      status: 'clarification_needed',
      clarification_prompt: 'Please specify whether you want undergraduate or graduate records.',
      unsupported_reason: null,
      execution_disclaimer: 'Candidate SQL is unexecuted.',
      request_id: 'req-no-opts',
    }

    globalThis.fetch = (async () => {
      return new Response(JSON.stringify(responseWithoutOptions), { status: 200 })
    }) as typeof fetch

    const coordinator = new SqlGenerationCoordinator()
    const res = await coordinator.generateCandidate(
      'Show students',
      SAMPLE_PROJECTED_SCHEMA,
      'university_db'
    )

    assert.ok(res)
    assert.strictEqual(res.status, 'clarification_needed')
    assert.strictEqual(res.clarification_options, undefined)
    assert.strictEqual(coordinator.getState().response?.clarification_options, undefined)
  })

  await t.test('degrades gracefully when clarification_options is an empty array', async () => {
    const responseWithEmptyOptions: GenerateSQLResponse = {
      candidate_sql: null,
      status: 'clarification_needed',
      clarification_prompt: 'Please provide more details.',
      clarification_options: [],
      unsupported_reason: null,
      execution_disclaimer: 'Candidate SQL is unexecuted.',
      request_id: 'req-empty-opts',
    }

    globalThis.fetch = (async () => {
      return new Response(JSON.stringify(responseWithEmptyOptions), { status: 200 })
    }) as typeof fetch

    const coordinator = new SqlGenerationCoordinator()
    const res = await coordinator.generateCandidate(
      'Show metrics',
      SAMPLE_PROJECTED_SCHEMA,
      'university_db'
    )

    assert.ok(res)
    assert.strictEqual(res.status, 'clarification_needed')
    assert.deepStrictEqual(res.clarification_options, [])
  })
})

// ==============================================================================
// 3. User-Controlled Selection & State Invariant Tests
// ==============================================================================

test('Clarification Workflow - Selection & Control Invariants', async (t) => {
  t.afterEach(() => {
    restoreFetch()
  })

  await t.test('selecting an option populates question input without triggering generation', () => {
    const coordinator = new SqlGenerationCoordinator()
    coordinator.setQuestion('Show student performance')

    const option = SAMPLE_CLARIFICATION_OPTIONS[0]
    const clarifiedText = deriveClarifiedQuestion(coordinator.getState().question, option)

    // Option selection only updates the question text
    coordinator.setQuestion(clarifiedText)

    // 1. Question input was populated with unambiguous clarified question
    assert.strictEqual(
      coordinator.getState().question,
      'Show the average CGPA of students by department'
    )

    // 2. Generation state remains not loading, no candidate SQL generated
    assert.strictEqual(coordinator.getState().isLoading, false)
    assert.strictEqual(coordinator.getState().response, null)
  })

  await t.test('selecting an option marks question as stale when initial question had generated', () => {
    const coordinator = new SqlGenerationCoordinator()
    coordinator.setQuestion('Show student performance')

    // Simulate initial generation state update
    ;(coordinator as unknown as { setState: (p: unknown) => void }).setState({
      lastGeneratedQuestion: 'Show student performance',
      response: SAMPLE_CLARIFICATION_RESPONSE,
      isStale: false,
    })

    assert.strictEqual(coordinator.getState().isStale, false)

    // User selects clarification option
    const option = SAMPLE_CLARIFICATION_OPTIONS[0]
    const clarifiedText = deriveClarifiedQuestion(coordinator.getState().question, option)
    coordinator.setQuestion(clarifiedText)

    // Invariant: question is updated and marked stale because it differs from lastGeneratedQuestion
    assert.strictEqual(
      coordinator.getState().question,
      'Show the average CGPA of students by department'
    )
    assert.strictEqual(coordinator.getState().isStale, true)
  })

  await t.test('option selection does NOT execute SQL or create a conversation turn', () => {
    const sqlCoordinator = new SqlGenerationCoordinator()
    const execCoordinator = new QueryExecutionCoordinator()

    sqlCoordinator.setQuestion('Show student performance')

    const option = SAMPLE_CLARIFICATION_OPTIONS[0]
    const clarifiedText = deriveClarifiedQuestion(sqlCoordinator.getState().question, option)
    sqlCoordinator.setQuestion(clarifiedText)

    // Invariant: zero query executions dispatched
    assert.strictEqual(execCoordinator.getState().result, null)
    assert.strictEqual(execCoordinator.getState().isLoading, false)

    // Invariant: zero turns recorded
    assert.strictEqual(sqlCoordinator.getState().conversationContext.length, 0)
  })

  await t.test('preserves existing bounded conversation context when clarification option is selected', () => {
    const coordinator = new SqlGenerationCoordinator()

    // Pre-seed conversation with a prior successful turn
    coordinator.recordCompletedTurn(
      'List all departments',
      ['Code', 'Name'],
      'Retrieve all academic departments'
    )

    assert.strictEqual(coordinator.getState().conversationContext.length, 1)

    // Clarification happens on turn 2
    coordinator.setQuestion('Show student performance')
    const option = SAMPLE_CLARIFICATION_OPTIONS[0]
    const clarifiedText = deriveClarifiedQuestion(coordinator.getState().question, option)
    coordinator.setQuestion(clarifiedText)

    // Invariant: prior turn is completely preserved, not cleared or duplicated
    const context = coordinator.getState().conversationContext
    assert.strictEqual(context.length, 1)
    assert.strictEqual(context[0].user_question, 'List all departments')
    assert.strictEqual(context[0].resolved_intent, 'Retrieve all academic departments')
    assert.deepStrictEqual(context[0].result_columns, ['Code', 'Name'])
  })

  await t.test('explicit user action is required to submit the clarified question', async () => {
    let requestPayload: Record<string, unknown> | null = null

    globalThis.fetch = (async (url: string | URL | Request, init?: RequestInit) => {
      const urlStr = String(url)
      if (urlStr.includes('/api/v1/generate-sql')) {
        requestPayload = JSON.parse(String(init?.body)) as Record<string, unknown>
        return new Response(
          JSON.stringify({
            candidate_sql: 'SELECT DepartmentCode, AVG(CGPA) AS AvgCGPA FROM dbo.Students GROUP BY DepartmentCode;',
            status: 'generated',
            execution_disclaimer: 'Candidate SQL is unexecuted.',
            request_id: 'req-gen-after-clar',
          }),
          { status: 200 }
        )
      }
      return new Response('Not Found', { status: 404 })
    }) as typeof fetch

    const coordinator = new SqlGenerationCoordinator()
    coordinator.setQuestion('Show student performance')

    // 1. User selects option -> modifies question in editor
    const option = SAMPLE_CLARIFICATION_OPTIONS[0]
    const clarifiedText = deriveClarifiedQuestion(coordinator.getState().question, option)
    coordinator.setQuestion(clarifiedText)

    // Network has NOT been called yet
    assert.strictEqual(requestPayload, null)

    // 2. User explicitly clicks "Generate Candidate SQL"
    const res = await coordinator.generateCandidate(
      coordinator.getState().question,
      SAMPLE_PROJECTED_SCHEMA,
      'test_db'
    )

    // Network request was now dispatched with the clarified question
    assert.ok(requestPayload)
    assert.strictEqual(
      (requestPayload as Record<string, unknown>).question,
      'Show the average CGPA of students by department'
    )

    // State updated with candidate SQL
    assert.ok(res)
    assert.strictEqual(res.status, 'generated')
    assert.strictEqual(
      res.candidate_sql,
      'SELECT DepartmentCode, AVG(CGPA) AS AvgCGPA FROM dbo.Students GROUP BY DepartmentCode;'
    )
    assert.strictEqual(coordinator.getState().isStale, false)
  })
})
