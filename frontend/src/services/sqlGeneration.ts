/**
 * QueryPilot Decoupled SQL Generation Coordinator.
 *
 * Coordinates natural-language question submission against the active database profile's
 * projected ModelFacingSchemaContext via the hosted V1 API (POST /api/v1/generate-sql).
 *
 * Security & Execution Boundaries:
 * - Unexecuted Candidates: Generated SQL is strictly an unexecuted candidate. This module
 *   never executes queries against customer databases or local agent profiles.
 * - Model-Facing Schema Boundary: Transmits only bounded, projected ModelFacingSchemaContext.
 *   Raw driver metadata, rows, and cell values are never sent to the hosted generation endpoint.
 * - Stale-Response Protection: Uses monotonic generation counters and AbortControllers so late
 *   responses from prior requests, profile switches, or logouts cannot overwrite state.
 * - Invalidation On Input Drift: Automatically flags or clears candidate SQL when the input question,
 *   selected profile, or schema changes.
 * - Non-Persistence: Questions, schemas, and candidate SQL statements are held in memory only
 *   and never written to localStorage or sessionStorage.
 * - Duplicate Submission Defense: Guards against concurrent generation dispatches.
 */

import { useSyncExternalStore, useMemo } from 'react'
import { generateSql } from './v1ApiClient.ts'
import type {
  ConversationTurnContext,
  GenerateSQLRequest,
  GenerateSQLResponse,
  ModelFacingSchemaContext,
} from '../types/api.ts'
import type { RequestOptions } from './apiClient.ts'

export interface SqlGenerationState {
  question: string
  isLoading: boolean
  response: GenerateSQLResponse | null
  error: Error | null
  lastGeneratedQuestion: string | null
  lastGeneratedProfileId: string | null
  candidateId: string | null
  isStale: boolean
  conversationContext: ConversationTurnContext[]
  lastRecordedCandidateId: string | null
}

export class SqlGenerationCoordinator {
  static readonly MAX_CONVERSATION_TURNS = 3

  private state: SqlGenerationState
  private listeners: Set<(state: SqlGenerationState) => void> = new Set()
  private generation: number = 0
  private activeAbortController: AbortController | null = null

  constructor(initialState?: Partial<SqlGenerationState>) {
    this.state = {
      question: '',
      isLoading: false,
      response: null,
      error: null,
      lastGeneratedQuestion: null,
      lastGeneratedProfileId: null,
      candidateId: null,
      isStale: false,
      conversationContext: [],
      lastRecordedCandidateId: null,
      ...initialState,
    }
  }

  /**
   * Returns a snapshot of the current generation state.
   */
  getState(): SqlGenerationState {
    return this.state
  }

  /**
   * Subscribes a listener to state changes (compatible with React useSyncExternalStore).
   */
  subscribe(listener: (state: SqlGenerationState) => void): () => void {
    this.listeners.add(listener)
    return () => {
      this.listeners.delete(listener)
    }
  }

  private notify(): void {
    for (const listener of this.listeners) {
      listener(this.state)
    }
  }

  private setState(partial: Partial<SqlGenerationState>): void {
    this.state = { ...this.state, ...partial }
    this.notify()
  }

  /**
   * Updates the natural language question text and marks the candidate stale
   * if the question drifts from the one used for the last generation.
   */
  setQuestion(newQuestion: string): void {
    const isStale = Boolean(
      this.state.lastGeneratedQuestion &&
      newQuestion.trim() !== this.state.lastGeneratedQuestion.trim()
    )
    this.setState({
      question: newQuestion,
      isStale,
    })
  }

  /**
   * Dispatches a candidate SQL generation request to the hosted V1 API.
   *
   * Guarantees:
   * - Requires an active profile and loaded model-facing schema context.
   * - Duplicate submission prevention.
   * - Enforces question length constraints (1-1000 characters).
   * - Stale response protection via monotonic generation counter.
   * - Does NOT execute candidate SQL.
   */
  async generateCandidate(
    question: string,
    schemaContext: ModelFacingSchemaContext | null,
    profileId: string | null,
    options?: RequestOptions
  ): Promise<GenerateSQLResponse | null> {
    // 1. Guard against duplicate submission
    if (this.state.isLoading) {
      return null
    }

    // 2. Validate session and profile schema prerequisites
    if (!profileId || !schemaContext) {
      const err = new Error(
        'A database profile with loaded schema must be selected before generating candidate SQL.'
      )
      this.setState({ error: err })
      throw err
    }

    // 3. Client-side question validation
    const trimmedQuestion = question.trim()
    if (!trimmedQuestion) {
      const err = new Error('Question cannot be empty or whitespace.')
      this.setState({ error: err })
      throw err
    }

    if (trimmedQuestion.length > 1000) {
      const err = new Error(
        `Question exceeds maximum length of 1000 characters (${trimmedQuestion.length} chars).`
      )
      this.setState({ error: err })
      throw err
    }

    // 4. Begin generation with monotonic generation guard
    const currentGen = ++this.generation
    if (this.activeAbortController) {
      this.activeAbortController.abort()
      this.activeAbortController = null
    }

    const controller = new AbortController()
    this.activeAbortController = controller

    this.setState({
      isLoading: true,
      error: null,
    })

    try {
      const request: GenerateSQLRequest = {
        question: trimmedQuestion,
        schema_context: schemaContext,
        ...(this.state.conversationContext.length > 0
          ? { conversation_context: this.state.conversationContext }
          : {}),
      }

      const res = await generateSql(request, {
        ...options,
        signal: controller.signal,
      })

      if (this.generation !== currentGen) {
        // Stale response guard: superseded by a newer generation or cancellation
        return null
      }

      const candidateId = `${currentGen}:${res.request_id || 'candidate'}`

      this.setState({
        isLoading: false,
        response: res,
        error: null,
        lastGeneratedQuestion: trimmedQuestion,
        lastGeneratedProfileId: profileId,
        candidateId,
        isStale: false,
      })

      return res
    } catch (err: unknown) {
      if (this.generation !== currentGen) {
        return null
      }
      if (err instanceof DOMException && err.name === 'AbortError') {
        this.setState({ isLoading: false })
        return null
      }

      const effectiveError =
        err instanceof Error ? err : new Error('Candidate SQL generation failed.')
      this.setState({
        isLoading: false,
        error: effectiveError,
      })
      throw effectiveError
    } finally {
      if (this.activeAbortController === controller) {
        this.activeAbortController = null
      }
      if (this.state.isLoading && this.generation === currentGen) {
        this.setState({ isLoading: false })
      }
    }
  }

  /**
   * Cancels any in-flight SQL generation request.
   */
  cancelGeneration(): void {
    this.generation++
    if (this.activeAbortController) {
      this.activeAbortController.abort()
      this.activeAbortController = null
    }
    this.setState({ isLoading: false })
  }

  /**
   * Records a completed, executed conversation turn into the sliding context window.
   *
   * Security & Semantic Guarantees:
   * - Records strictly after successful local execution.
   * - Stores only user question, role 'user', returned column names, and optional resolved intent.
   * - Never stores raw SQL, result rows, cell values, or secrets.
   * - Bounded to at most 3 turns (oldest turns drop off).
   * - Completion guard: avoids duplicate turns from repeated execution of the exact same candidate.
   */
  recordCompletedTurn(
    question: string,
    resultColumns: string[],
    optionsOrIntent?: string | null | { candidateId?: string | null; resolvedIntent?: string | null },
    candidateId?: string | null
  ): void {
    let resolvedIntentStr: string | null = null
    let candidateIdStr: string | null = null

    if (optionsOrIntent && typeof optionsOrIntent === 'object') {
      resolvedIntentStr = optionsOrIntent.resolvedIntent ?? null
      candidateIdStr = optionsOrIntent.candidateId ?? null
    } else {
      resolvedIntentStr = optionsOrIntent ?? null
      candidateIdStr = candidateId ?? null
    }

    // Completion guard: avoid duplicate turns from repeated execution of the exact same candidate
    if (candidateIdStr && candidateIdStr === this.state.lastRecordedCandidateId) {
      return
    }

    const trimmedQuestion = question ? question.trim() : ''
    if (!trimmedQuestion) {
      return
    }

    const safeColumns = Array.isArray(resultColumns)
      ? resultColumns
          .map((col) => (typeof col === 'string' ? col.trim() : ''))
          .filter(Boolean)
          .slice(0, 50)
      : []

    const turn: ConversationTurnContext = {
      role: 'user',
      user_question: trimmedQuestion.slice(0, 1000),
      result_columns: safeColumns,
      ...(resolvedIntentStr && typeof resolvedIntentStr === 'string' && resolvedIntentStr.trim()
        ? { resolved_intent: resolvedIntentStr.trim().slice(0, 1000) }
        : {}),
    }

    const updated = [...this.state.conversationContext, turn].slice(
      -SqlGenerationCoordinator.MAX_CONVERSATION_TURNS
    )

    this.setState({
      conversationContext: updated,
      lastRecordedCandidateId: candidateIdStr || this.state.lastRecordedCandidateId,
    })
  }

  /**
   * Explicitly clears all prior conversation context.
   */
  clearConversationContext(): void {
    this.setState({
      conversationContext: [],
      lastRecordedCandidateId: null,
    })
  }

  /**
   * Clears candidate SQL response, error, and associated metadata.
   * Advances generation counter to discard any pending responses.
   * Note: conversationContext is preserved for subsequent follow-up queries.
   */
  clearCandidate(): void {
    this.generation++
    if (this.activeAbortController) {
      this.activeAbortController.abort()
      this.activeAbortController = null
    }
    this.setState({
      response: null,
      error: null,
      lastGeneratedQuestion: null,
      lastGeneratedProfileId: null,
      candidateId: null,
      isStale: false,
      isLoading: false,
    })
  }

  /**
   * Resets question input, candidate state, and conversation context completely.
   */
  resetAll(): void {
    this.generation++
    if (this.activeAbortController) {
      this.activeAbortController.abort()
      this.activeAbortController = null
    }
    this.setState({
      question: '',
      response: null,
      error: null,
      lastGeneratedQuestion: null,
      lastGeneratedProfileId: null,
      candidateId: null,
      isStale: false,
      isLoading: false,
      conversationContext: [],
      lastRecordedCandidateId: null,
    })
  }
}

/**
 * Default shared coordinator instance for runtime SQL generation state.
 */
export const defaultSqlGenerationCoordinator = new SqlGenerationCoordinator()

/**
 * React hook connecting components to a SqlGenerationCoordinator instance.
 */
export function useSqlGeneration(customCoordinator?: SqlGenerationCoordinator): {
  state: SqlGenerationState
  coordinator: SqlGenerationCoordinator
} {
  const coordinator = useMemo(
    () => customCoordinator || defaultSqlGenerationCoordinator,
    [customCoordinator]
  )

  const state = useSyncExternalStore(
    (onStoreChange) => coordinator.subscribe(onStoreChange),
    () => coordinator.getState()
  )

  return { state, coordinator }
}
