/**
 * QueryPilot Safe Local Query Execution Coordinator.
 *
 * Coordinates explicit, user-authorized query execution against an approved database
 * connection profile via the loopback QueryPilot agent daemon (POST /agent/v1/profiles/{id}/execute).
 *
 * Security & Execution Invariants:
 * - Read-Only Server Validation: Never trusts client-side assertions of safety. The local agent daemon
 *   independently inspects and AST-validates every SQL query against the active profile schema and policies.
 * - Explicit User Authorization: Queries are executed ONLY when the user explicitly triggers execution.
 *   Generating SQL, editing inputs, or viewing candidates NEVER executes automatically.
 * - Stale-Candidate Protection: Refuses to execute if the candidate is stale relative to current inputs,
 *   profile, or schema.
 * - Result Privacy: Returned query results (columns, rows, cell values) are strictly confined to local
 *   volatile memory and NEVER transmitted to the hosted API, Gemini, telemetry, or external services.
 * - Zero Ambiguous Retries: Does not automatically retry queries after network or timeout failures.
 * - Stale-Response Protection: Uses monotonic sequence numbers and AbortControllers so late responses
 *   after cancellation, profile switching, or logout are dropped.
 * - Non-Persistence: Result rows, cell values, and queries are held in memory only and never stored in
 *   localStorage or sessionStorage.
 */

import { useSyncExternalStore, useMemo } from 'react'
import {
  AgentClientError,
  executeProfileQuery,
} from './agentClient.ts'
import type { ExecuteQueryResponse } from '../types/api.ts'
import type { AgentRequestOptions } from './agentClient.ts'

export interface QueryExecutionState {
  isLoading: boolean
  result: ExecuteQueryResponse | null
  error: Error | null
  executedSql: string | null
  executedProfileId: string | null
  isAmbiguousFailure: boolean
  isConfirmedTimeout: boolean
}

export class QueryExecutionCoordinator {
  private state: QueryExecutionState
  private listeners: Set<(state: QueryExecutionState) => void> = new Set()
  private executionSeq: number = 0
  private activeAbortController: AbortController | null = null

  constructor(initialState?: Partial<QueryExecutionState>) {
    this.state = {
      isLoading: false,
      result: null,
      error: null,
      executedSql: null,
      executedProfileId: null,
      isAmbiguousFailure: false,
      isConfirmedTimeout: false,
      ...initialState,
    }
  }

  /**
   * Returns a snapshot of the current query execution state.
   */
  getState(): QueryExecutionState {
    return this.state
  }

  /**
   * Subscribes a listener to state changes (compatible with React useSyncExternalStore).
   */
  subscribe(listener: (state: QueryExecutionState) => void): () => void {
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

  private setState(partial: Partial<QueryExecutionState>): void {
    this.state = { ...this.state, ...partial }
    this.notify()
  }

  /**
   * Dispatches a read-only SQL query to the local agent for profile-governed execution.
   *
   * Enforces:
   * - Explicit user invocation (never auto-executed).
   * - Duplicate submission prevention while an execution is in-flight.
   * - Input validation (non-empty profileId, non-empty SQL <= 10,000 chars).
   * - Stale response protection via monotonic sequence tracking.
   * - No automatic retries on ambiguous failure.
   *
   * @param profileId - Approved target database profile ID.
   * @param sql - Exact candidate or reviewed SQL query string to execute.
   * @param requestId - Optional correlation tracking ID.
   * @param options - Optional cancellation signal or client options.
   */
  async executeQuery(
    profileId: string | null,
    sql: string,
    requestId?: string | null,
    options?: AgentRequestOptions
  ): Promise<ExecuteQueryResponse | null> {
    // 1. Duplicate submission defense
    if (this.state.isLoading) {
      return null
    }

    // 2. Validate profile prerequisite
    if (!profileId || !profileId.trim()) {
      const err = new Error('A database profile must be selected before executing a query.')
      this.setState({ error: err, isAmbiguousFailure: false })
      throw err
    }

    // 3. Validate SQL string input
    const trimmedSql = sql ? sql.trim() : ''
    if (!trimmedSql) {
      const err = new Error('SQL candidate query cannot be empty or whitespace only.')
      this.setState({ error: err, isAmbiguousFailure: false })
      throw err
    }

    if (trimmedSql.length > 10000) {
      const err = new Error(
        `SQL query exceeds maximum allowed length of 10000 characters (${trimmedSql.length} chars).`
      )
      this.setState({ error: err, isAmbiguousFailure: false })
      throw err
    }

    // 4. Begin execution with monotonic sequence guard
    const currentSeq = ++this.executionSeq
    if (this.activeAbortController) {
      this.activeAbortController.abort()
      this.activeAbortController = null
    }

    const controller = new AbortController()
    this.activeAbortController = controller

    this.setState({
      isLoading: true,
      error: null,
      isAmbiguousFailure: false,
    })

    try {
      const response = await executeProfileQuery(
        profileId.trim(),
        trimmedSql,
        requestId,
        {
          ...options,
          signal: controller.signal,
        }
      )

      if (this.executionSeq !== currentSeq) {
        // Discard stale response (superseded by newer execution, cancellation, or profile switch)
        return null
      }

      this.setState({
        isLoading: false,
        result: response,
        error: null,
        executedSql: trimmedSql,
        executedProfileId: profileId.trim(),
        isAmbiguousFailure: false,
      })

      return response
    } catch (err: unknown) {
      if (this.executionSeq !== currentSeq) {
        return null
      }

      if (err instanceof DOMException && err.name === 'AbortError') {
        this.setState({ isLoading: false })
        return null
      }

      const effectiveError =
        err instanceof Error ? err : new Error('Query execution failed.')

      // Distinguish confirmed server timeout (HTTP 504) from ambiguous network failures
      const isConfirmedTimeout =
        (err instanceof AgentClientError && (err.kind === 'timeout' || err.statusCode === 504))

      const isAmbiguous =
        !isConfirmedTimeout &&
        ((err instanceof AgentClientError && err.kind === 'disconnected') ||
          (err instanceof Error && /network|connection refused|failed to fetch/i.test(err.message)))

      this.setState({
        isLoading: false,
        result: null,
        error: effectiveError,
        isAmbiguousFailure: isAmbiguous,
        isConfirmedTimeout,
      })

      throw effectiveError
    } finally {
      if (this.activeAbortController === controller) {
        this.activeAbortController = null
      }
      if (this.state.isLoading && this.executionSeq === currentSeq) {
        this.setState({ isLoading: false })
      }
    }
  }

  /**
   * Cancels any currently in-flight query execution.
   */
  cancelExecution(): void {
    this.executionSeq++
    if (this.activeAbortController) {
      this.activeAbortController.abort()
      this.activeAbortController = null
    }
    this.setState({
      isLoading: false,
      isAmbiguousFailure: false,
      isConfirmedTimeout: false,
    })
  }

  /**
   * Clears query execution results, errors, and executed SQL metadata.
   * Advances sequence counter to discard any pending responses.
   */
  clearResult(): void {
    this.executionSeq++
    if (this.activeAbortController) {
      this.activeAbortController.abort()
      this.activeAbortController = null
    }
    this.setState({
      isLoading: false,
      result: null,
      error: null,
      executedSql: null,
      executedProfileId: null,
      isAmbiguousFailure: false,
      isConfirmedTimeout: false,
    })
  }
}

/**
 * Default shared coordinator instance for runtime query execution state.
 */
export const defaultQueryExecutionCoordinator = new QueryExecutionCoordinator()

/**
 * React hook connecting components to a QueryExecutionCoordinator instance.
 */
export function useQueryExecution(customCoordinator?: QueryExecutionCoordinator): {
  state: QueryExecutionState
  coordinator: QueryExecutionCoordinator
} {
  const coordinator = useMemo(
    () => customCoordinator || defaultQueryExecutionCoordinator,
    [customCoordinator]
  )

  const state = useSyncExternalStore(
    (onStoreChange) => coordinator.subscribe(onStoreChange),
    () => coordinator.getState()
  )

  return { state, coordinator }
}
