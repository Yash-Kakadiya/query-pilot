/**
 * QueryPilot AI Availability Status Coordinator.
 *
 * Coordinates upstream AI backend liveness, error classification, authentication status,
 * recovery tracking, and conservative background polling for the QueryPilot interface.
 *
 * Status Semantics (Milestone B29-C):
 * 1. AI Available: Local agent is authenticated and upstream AI backend is verified reachable.
 * 2. AI Unavailable: Upstream backend is unreachable, connection refused, timed out, or unavailable.
 * 3. Authentication Required: Local agent session is unauthenticated or expired.
 * 4. Checking: Liveness probe or session verification currently in-flight.
 * 5. Unknown: Status cannot be determined reliably (unrecognized failure / initial unprobed state).
 *
 * Guarantees:
 * - Distinct Scope: AI availability does NOT imply database connection readiness; local database
 *   browsing and manual SQL execution remain fully operational even when AI is unavailable.
 * - Zero Side-Effects: Probing uses GET /health; NEVER triggers Gemini query generation or synthesis.
 * - Security: Never exposes API keys, tokens, connection strings, or internal gateway URLs.
 * - Concurrency & Stale Protection: Monotonic sequence tracking and AbortControllers discard delayed responses.
 */

import { useSyncExternalStore, useMemo } from 'react'
import { ApiClientError } from '../types/api.ts'
import { getHealth } from './apiClient.ts'
import type { AgentSessionStatus } from './agentSession.ts'

export type AiAvailabilityStatus =
  | 'ai_available'
  | 'ai_unavailable'
  | 'auth_required'
  | 'checking'
  | 'unknown'

export interface AiStatusState {
  status: AiAvailabilityStatus
  label: string
  description: string
  detail?: string
  isChecking: boolean
  lastChecked?: number
}

/**
 * Pure function resolving status information from current environmental signals.
 */
export function resolveAiStatus(options: {
  agentStatus: AgentSessionStatus
  isChecking: boolean
  upstreamOnline: boolean | null
  upstreamError?: Error | ApiClientError | null
  lastChecked?: number
}): AiStatusState {
  const { agentStatus, isChecking, upstreamOnline, upstreamError, lastChecked } = options

  // 1. Local Agent Authentication Required / Expired
  if (agentStatus === 'auth_required') {
    return {
      status: 'auth_required',
      label: 'Authentication Required',
      description: 'Local agent session is unauthenticated or expired. Enter bootstrap token to connect.',
      isChecking: false,
      lastChecked,
    }
  }

  // 2. Local Agent Daemon Disconnected
  if (agentStatus === 'disconnected') {
    return {
      status: 'ai_unavailable',
      label: 'AI Unavailable',
      description: 'Local agent daemon is unreachable on loopback. AI query generation is unavailable.',
      detail: 'Local agent daemon disconnected',
      isChecking: false,
      lastChecked,
    }
  }

  // 3. Status check / probe currently in progress
  if (isChecking || agentStatus === 'probing') {
    return {
      status: 'checking',
      label: 'Checking AI Status',
      description: 'Verifying upstream AI backend connectivity...',
      isChecking: true,
      lastChecked,
    }
  }

  // 4. Upstream Error occurred
  if (upstreamError) {
    if (upstreamError instanceof ApiClientError) {
      if (upstreamError.statusCode === 401) {
        return {
          status: 'ai_unavailable',
          label: 'AI Unavailable',
          description: 'Upstream AI backend requires an access key. Check your access key in settings.',
          detail: '401 Unauthorized / Access Key Required',
          isChecking: false,
          lastChecked,
        }
      }
      if (upstreamError.statusCode === 403) {
        return {
          status: 'ai_unavailable',
          label: 'AI Unavailable',
          description: 'Access denied by the upstream AI backend service.',
          detail: '403 Forbidden / Access Denied',
          isChecking: false,
          lastChecked,
        }
      }
      if (upstreamError.statusCode === 502 || upstreamError.statusCode === 503) {
        return {
          status: 'ai_unavailable',
          label: 'AI Unavailable',
          description: 'Upstream AI generation backend is unreachable or unhealthy. Manual SQL remains operational.',
          detail: `HTTP ${upstreamError.statusCode} Upstream Unavailable`,
          isChecking: false,
          lastChecked,
        }
      }
      if (upstreamError.statusCode === 504) {
        return {
          status: 'ai_unavailable',
          label: 'AI Unavailable',
          description: 'Upstream AI generation backend timed out. Manual SQL remains operational.',
          detail: '504 Gateway Timeout',
          isChecking: false,
          lastChecked,
        }
      }
      if (upstreamError.kind === 'network') {
        return {
          status: 'ai_unavailable',
          label: 'AI Unavailable',
          description: 'Cannot connect to upstream AI backend. Offline database browsing and manual SQL remain operational.',
          detail: 'Network Connection Refused / Backend Unreachable',
          isChecking: false,
          lastChecked,
        }
      }
    }

    // Unrecognized or generic failure
    return {
      status: 'unknown',
      label: 'AI Status Unknown',
      description: 'AI backend connectivity cannot be determined reliably.',
      detail: upstreamError.message || 'Unknown upstream failure',
      isChecking: false,
      lastChecked,
    }
  }

  // 5. Upstream verified online and session is authenticated
  if (agentStatus === 'authenticated' && upstreamOnline === true) {
    return {
      status: 'ai_available',
      label: 'AI Available',
      description: 'Upstream AI backend is reachable. Natural-language query generation is ready.',
      isChecking: false,
      lastChecked,
    }
  }

  // 6. Upstream explicitly reported offline
  if (upstreamOnline === false) {
    return {
      status: 'ai_unavailable',
      label: 'AI Unavailable',
      description: 'Upstream AI service reported offline. Manual SQL execution remains fully available.',
      detail: 'Upstream service reported offline',
      isChecking: false,
      lastChecked,
    }
  }

  // 7. Neutral default / unknown
  return {
    status: 'unknown',
    label: 'AI Status Unknown',
    description: 'AI service status has not been verified yet.',
    isChecking: false,
    lastChecked,
  }
}

export class AiStatusCoordinator {
  private state: AiStatusState
  private listeners: Set<(state: AiStatusState) => void> = new Set()
  private checkSeq: number = 0
  private activeAbortController: AbortController | null = null
  private pollIntervalId: ReturnType<typeof setInterval> | null = null
  private currentAgentStatus: AgentSessionStatus = 'probing'
  private lastUpstreamOnline: boolean | null = null
  private lastUpstreamError: Error | ApiClientError | null = null

  constructor(initialState?: Partial<AiStatusState>) {
    this.state = {
      status: 'checking',
      label: 'Checking AI Status',
      description: 'Verifying upstream AI backend connectivity...',
      isChecking: false,
      ...initialState,
    }
  }

  getState(): AiStatusState {
    return this.state
  }

  subscribe(listener: (state: AiStatusState) => void): () => void {
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

  private updateState(): void {
    const newState = resolveAiStatus({
      agentStatus: this.currentAgentStatus,
      isChecking: this.state.isChecking,
      upstreamOnline: this.lastUpstreamOnline,
      upstreamError: this.lastUpstreamError,
      lastChecked: this.state.lastChecked,
    })
    this.state = newState
    this.notify()
  }

  /**
   * Probes upstream backend availability using GET /health via apiClient.
   * Enforces monotonic sequence guard and request cancellation.
   */
  async checkStatus(signal?: AbortSignal): Promise<void> {
    // If agent is not authenticated, immediate status applies
    if (this.currentAgentStatus === 'auth_required' || this.currentAgentStatus === 'disconnected') {
      this.updateState()
      return
    }

    const currentSeq = ++this.checkSeq

    if (this.activeAbortController) {
      this.activeAbortController.abort()
      this.activeAbortController = null
    }

    const controller = new AbortController()
    this.activeAbortController = controller

    // Link optional parent cancellation signal
    if (signal) {
      signal.addEventListener('abort', () => controller.abort(), { once: true })
    }

    this.state = {
      ...this.state,
      isChecking: true,
      status: 'checking',
      label: 'Checking AI Status',
      description: 'Verifying upstream AI backend connectivity...',
    }
    this.notify()

    try {
      await getHealth(controller.signal)

      if (this.checkSeq !== currentSeq) {
        return // Stale check guard
      }

      this.lastUpstreamOnline = true
      this.lastUpstreamError = null
      this.state = {
        status: 'ai_available',
        label: 'AI Available',
        description: 'Upstream AI backend is reachable. Natural-language query generation is ready.',
        isChecking: false,
        lastChecked: Date.now(),
      }
      this.notify()
    } catch (err: unknown) {
      if (this.checkSeq !== currentSeq) {
        return
      }
      if (err instanceof DOMException && err.name === 'AbortError') {
        return
      }

      this.lastUpstreamOnline = false
      this.lastUpstreamError = err instanceof Error ? err : new Error(String(err))

      const resolved = resolveAiStatus({
        agentStatus: this.currentAgentStatus,
        isChecking: false,
        upstreamOnline: false,
        upstreamError: this.lastUpstreamError,
        lastChecked: Date.now(),
      })
      this.state = resolved
      this.notify()
    } finally {
      if (this.activeAbortController === controller) {
        this.activeAbortController = null
      }
    }
  }

  /**
   * Records a successful upstream candidate generation or synthesis turn,
   * immediately recovering availability state.
   */
  recordGenerationSuccess(): void {
    this.lastUpstreamOnline = true
    this.lastUpstreamError = null
    this.state = {
      status: 'ai_available',
      label: 'AI Available',
      description: 'Upstream AI backend is reachable. Natural-language query generation is ready.',
      isChecking: false,
      lastChecked: Date.now(),
    }
    this.notify()
  }

  /**
   * Records an upstream candidate generation failure, updating error classification.
   */
  recordGenerationFailure(error: unknown): void {
    this.lastUpstreamOnline = false
    this.lastUpstreamError = error instanceof Error ? error : new Error(String(error))
    this.updateState()
  }

  /**
   * Synchronizes local agent session status.
   */
  syncAgentStatus(newStatus: AgentSessionStatus): void {
    const prevStatus = this.currentAgentStatus
    this.currentAgentStatus = newStatus

    if (newStatus === 'auth_required' || newStatus === 'disconnected') {
      this.lastUpstreamOnline = null
      this.lastUpstreamError = null
      this.updateState()
    } else if (newStatus === 'authenticated' && prevStatus !== 'authenticated') {
      void this.checkStatus()
    } else {
      this.updateState()
    }
  }

  /**
   * Starts periodic polling with a conservative interval.
   * Returns a cleanup function that must be called on unmount.
   */
  startPolling(intervalMs: number = 60000): () => void {
    if (this.pollIntervalId) {
      clearInterval(this.pollIntervalId)
    }

    this.pollIntervalId = setInterval(() => {
      if (this.currentAgentStatus === 'authenticated' && !this.state.isChecking) {
        void this.checkStatus()
      }
    }, intervalMs)

    return () => {
      if (this.pollIntervalId) {
        clearInterval(this.pollIntervalId)
        this.pollIntervalId = null
      }
    }
  }

  /**
   * Disposes all active timers, in-flight controllers, and listeners.
   */
  dispose(): void {
    if (this.pollIntervalId) {
      clearInterval(this.pollIntervalId)
      this.pollIntervalId = null
    }
    if (this.activeAbortController) {
      this.activeAbortController.abort()
      this.activeAbortController = null
    }
    this.listeners.clear()
  }
}

export function useAiStatus(customCoordinator?: AiStatusCoordinator) {
  const coordinator = useMemo(
    () => customCoordinator || new AiStatusCoordinator(),
    [customCoordinator]
  )
  const state = useSyncExternalStore(
    (cb) => coordinator.subscribe(cb),
    () => coordinator.getState()
  )
  return { state, coordinator }
}
