/**
 * QueryPilot Local Agent Session Coordinator.
 *
 * Coordinates authentication probe, bootstrap token submission, profile discovery,
 * profile selection, dynamic schema introspection, and explicit logout lifecycle
 * for the loopback QueryPilot agent daemon.
 *
 * Security & Architectural Guarantees:
 * - Ambient HttpOnly Cookie: relies strictly on `qpt_session` managed by the browser.
 * - Credential Protection: never stores bootstrap tokens, profiles, or schema objects
 *   in localStorage, sessionStorage, or persistent storage. Kept in memory only.
 * - Non-Persistence: tokens are treated as transient inputs and wiped immediately upon submission.
 * - Stale Response Protection: uses monotonic generation counters and AbortControllers so delayed
 *   responses from prior probes, bootstrap attempts, or previous profile schema requests cannot
 *   restore state after logout, profile switching, or re-authentication.
 * - Error Discrimination: cleanly distinguishes daemon disconnects (ECONNREFUSED / network errors),
 *   authentication challenges (HTTP 401), and schema introspection/validation failures.
 * - Model-Facing Projection: strictly applies projectToModelFacingSchema before passing schema
 *   context to consumers.
 */

import { useSyncExternalStore, useMemo } from 'react'
import {
  AgentClientError,
  MAX_BOOTSTRAP_TOKEN_LENGTH,
  MIN_BOOTSTRAP_TOKEN_LENGTH,
  bootstrapAgent,
  getProfileSchema,
  getProfiles,
  logoutAgent,
} from './agentClient.ts'
import { projectToModelFacingSchema } from './schemaProjection.ts'
import type {
  AgentDatabaseSchema,
  ModelFacingSchemaContext,
  ProfileSummary,
} from '../types/api.ts'

export type AgentSessionStatus =
  | 'probing'        // Startup probe in-flight
  | 'authenticated'  // Session active, approved profiles loaded in memory
  | 'auth_required'  // HTTP 401: requires bootstrap authentication
  | 'disconnected'   // Daemon unreachable on loopback (network failure)
  | 'error'          // Server or validation error

export type SchemaIntrospectionStatus =
  | 'idle'           // No profile selected or schema not requested
  | 'loading'        // Schema introspection currently in-flight
  | 'loaded'         // Schema successfully introspected and projected
  | 'error'          // Schema introspection or validation failed

export interface AgentSessionState {
  status: AgentSessionStatus
  profiles: ProfileSummary[]
  errorMessage?: string
  authError?: string
  isModalOpen: boolean
  isSubmitting: boolean

  // Profile Selection & Schema Introspection (Phase 7.2)
  selectedProfileId: string | null
  selectedProfile: ProfileSummary | null
  schemaStatus: SchemaIntrospectionStatus
  schemaContext: ModelFacingSchemaContext | null
  rawSchema: AgentDatabaseSchema | null
  schemaError?: string
}

export interface BootstrapResult {
  success: boolean
  error?: string
}

export class AgentSessionCoordinator {
  private state: AgentSessionState
  private listeners: Set<(state: AgentSessionState) => void> = new Set()
  private generation: number = 0
  private schemaGeneration: number = 0
  private currentAbortController: AbortController | null = null
  private currentSchemaAbortController: AbortController | null = null

  constructor(initialState?: Partial<AgentSessionState>) {
    this.state = {
      status: 'probing',
      profiles: [],
      isModalOpen: false,
      isSubmitting: false,
      selectedProfileId: null,
      selectedProfile: null,
      schemaStatus: 'idle',
      schemaContext: null,
      rawSchema: null,
      schemaError: undefined,
      ...initialState,
    }
  }

  /**
   * Returns a snapshot of the current session state.
   */
  getState(): AgentSessionState {
    return this.state
  }

  /**
   * Subscribes a listener to state changes (compatible with React useSyncExternalStore).
   */
  subscribe(listener: (state: AgentSessionState) => void): () => void {
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

  private setState(partial: Partial<AgentSessionState>): void {
    this.state = { ...this.state, ...partial }
    this.notify()
  }

  /**
   * Performs an initial or refresh session probe by requesting registered profiles.
   *
   * Transitions:
   * - Success -> 'authenticated' (retains profiles in memory, closes modal)
   * - 401 -> 'auth_required' (clears profiles and schema, opens modal)
   * - Disconnected -> 'disconnected' (clears profiles and schema, closes modal, shows retry status)
   * - Other error -> 'error'
   */
  async probeSession(): Promise<void> {
    const currentGen = ++this.generation

    if (this.currentAbortController) {
      this.currentAbortController.abort()
    }
    const controller = new AbortController()
    this.currentAbortController = controller

    this.setState({
      status: 'probing',
      errorMessage: undefined,
      authError: undefined,
    })

    try {
      const response = await getProfiles({ signal: controller.signal })
      if (this.generation !== currentGen) {
        // Stale response guard: superseded by a newer probe or logout
        return
      }

      // Re-evaluate selected profile if it still exists in refreshed profiles
      let newSelectedProfile: ProfileSummary | null = null
      let newSelectedId = this.state.selectedProfileId
      let newSchemaStatus = this.state.schemaStatus
      let newSchemaContext = this.state.schemaContext
      let newRawSchema = this.state.rawSchema
      let newSchemaError = this.state.schemaError

      if (newSelectedId) {
        const found = response.profiles.find((p) => p.profile_id === newSelectedId)
        if (found) {
          newSelectedProfile = found
        } else {
          // Previously selected profile no longer exists in agent profiles
          newSelectedId = null
          newSelectedProfile = null
          newSchemaStatus = 'idle'
          newSchemaContext = null
          newRawSchema = null
          newSchemaError = undefined
        }
      }

      this.setState({
        status: 'authenticated',
        profiles: response.profiles,
        selectedProfileId: newSelectedId,
        selectedProfile: newSelectedProfile,
        schemaStatus: newSchemaStatus,
        schemaContext: newSchemaContext,
        rawSchema: newRawSchema,
        schemaError: newSchemaError,
        errorMessage: undefined,
        authError: undefined,
        isModalOpen: false,
      })
    } catch (err: unknown) {
      if (this.generation !== currentGen) {
        // Stale response guard
        return
      }
      if (err instanceof DOMException && err.name === 'AbortError') {
        return
      }

      // Clear profile & schema selection on probe failure
      this.schemaGeneration++
      if (this.currentSchemaAbortController) {
        this.currentSchemaAbortController.abort()
        this.currentSchemaAbortController = null
      }

      if (err instanceof AgentClientError) {
        if (err.kind === 'authentication' || err.statusCode === 401) {
          this.setState({
            status: 'auth_required',
            profiles: [],
            selectedProfileId: null,
            selectedProfile: null,
            schemaStatus: 'idle',
            schemaContext: null,
            rawSchema: null,
            schemaError: undefined,
            isModalOpen: true,
            errorMessage: undefined,
          })
          return
        }

        if (err.kind === 'disconnected') {
          this.setState({
            status: 'disconnected',
            profiles: [],
            selectedProfileId: null,
            selectedProfile: null,
            schemaStatus: 'idle',
            schemaContext: null,
            rawSchema: null,
            schemaError: undefined,
            isModalOpen: false,
            errorMessage: err.message,
          })
          return
        }
      }

      this.setState({
        status: 'error',
        profiles: [],
        selectedProfileId: null,
        selectedProfile: null,
        schemaStatus: 'idle',
        schemaContext: null,
        rawSchema: null,
        schemaError: undefined,
        isModalOpen: false,
        errorMessage: err instanceof Error ? err.message : 'Failed to connect to local agent.',
      })
    } finally {
      if (this.currentAbortController === controller) {
        this.currentAbortController = null
      }
    }
  }

  /**
   * Submits a single-use bootstrap token to establish an ambient session cookie.
   *
   * Guarantees:
   * - Duplicate submission protection: rejects if another submission is in-flight.
   * - Client validation: rejects empty or out-of-range tokens before network dispatch.
   * - Immediate profile retrieval upon successful bootstrap.
   * - Zero credential persistence.
   */
  async submitBootstrap(token: string): Promise<BootstrapResult> {
    // 1. Duplicate submission guard
    if (this.state.isSubmitting) {
      return { success: false, error: 'Authentication already in progress.' }
    }

    // 2. Client-side input validation
    if (!token || typeof token !== 'string' || !token.trim()) {
      const errorMsg = 'Bootstrap token cannot be empty.'
      this.setState({ authError: errorMsg })
      return { success: false, error: errorMsg }
    }

    const trimmed = token.trim()
    if (trimmed.length < MIN_BOOTSTRAP_TOKEN_LENGTH || trimmed.length > MAX_BOOTSTRAP_TOKEN_LENGTH) {
      const errorMsg = `Bootstrap token must be between ${MIN_BOOTSTRAP_TOKEN_LENGTH} and ${MAX_BOOTSTRAP_TOKEN_LENGTH} characters.`
      this.setState({ authError: errorMsg })
      return { success: false, error: errorMsg }
    }

    // 3. Begin submission with monotonic generation guard
    const currentGen = ++this.generation
    if (this.currentAbortController) {
      this.currentAbortController.abort()
    }
    const controller = new AbortController()
    this.currentAbortController = controller

    this.setState({ isSubmitting: true, authError: undefined })

    try {
      await bootstrapAgent(trimmed, { signal: controller.signal })
      if (this.generation !== currentGen) {
        return { success: false, error: 'Operation cancelled.' }
      }

      // 4. Fetch profiles immediately under the newly established session
      const profileResponse = await getProfiles({ signal: controller.signal })
      if (this.generation !== currentGen) {
        return { success: false, error: 'Operation cancelled.' }
      }

      this.setState({
        status: 'authenticated',
        profiles: profileResponse.profiles,
        selectedProfileId: null,
        selectedProfile: null,
        schemaStatus: 'idle',
        schemaContext: null,
        rawSchema: null,
        schemaError: undefined,
        isModalOpen: false,
        isSubmitting: false,
        errorMessage: undefined,
        authError: undefined,
      })
      return { success: true }
    } catch (err: unknown) {
      if (this.generation !== currentGen) {
        return { success: false, error: 'Operation cancelled.' }
      }
      if (err instanceof DOMException && err.name === 'AbortError') {
        this.setState({ isSubmitting: false })
        return { success: false, error: 'Aborted' }
      }

      let errorMsg = 'Bootstrap authentication failed.'
      if (err instanceof AgentClientError) {
        if (err.kind === 'authentication' || err.statusCode === 401) {
          errorMsg = 'Invalid, expired, or already-consumed bootstrap token.'
        } else if (err.kind === 'disconnected') {
          errorMsg = err.message
          this.setState({
            status: 'disconnected',
            isModalOpen: false,
            errorMessage: err.message,
          })
        } else if (err.detail || err.message) {
          errorMsg = err.detail || err.message
        }
      } else if (err instanceof Error) {
        errorMsg = err.message
      }

      this.setState({
        isSubmitting: false,
        authError: errorMsg,
      })
      return { success: false, error: errorMsg }
    } finally {
      if (this.currentAbortController === controller) {
        this.currentAbortController = null
      }
      if (this.state.isSubmitting && this.generation === currentGen) {
        this.setState({ isSubmitting: false })
      }
    }
  }

  /**
   * Selects a database profile and introspects its schema from the local agent daemon.
   *
   * Guarantees:
   * - Immediately clears previous schema to prevent stale schema presentation.
   * - Advances schemaGeneration and aborts any active schema request so late responses are ignored.
   * - Introspects raw schema and projects it to ModelFacingSchemaContext.
   * - Retains schema in React memory only (zero localStorage / sessionStorage persistence).
   * - If session expires (HTTP 401), automatically transitions session to 'auth_required' and opens modal.
   * - Distinguishes daemon disconnects from schema validation failures.
   */
  async selectProfile(profileId: string | null): Promise<void> {
    if (!profileId || !profileId.trim()) {
      this.clearSelectedProfile()
      return
    }

    const cleanId = profileId.trim()
    const targetProfile = this.state.profiles.find((p) => p.profile_id === cleanId) || null

    // 1. Advance schema generation counter and abort any pending schema request
    const currentSchemaGen = ++this.schemaGeneration
    if (this.currentSchemaAbortController) {
      this.currentSchemaAbortController.abort()
      this.currentSchemaAbortController = null
    }

    const controller = new AbortController()
    this.currentSchemaAbortController = controller

    // 2. Clear previous schema immediately to guarantee instant UI isolation
    this.setState({
      selectedProfileId: cleanId,
      selectedProfile: targetProfile,
      schemaStatus: 'loading',
      schemaContext: null,
      rawSchema: null,
      schemaError: undefined,
    })

    try {
      // 3. Introspect raw schema from loopback daemon
      const raw = await getProfileSchema(cleanId, {
        project: false,
        signal: controller.signal,
      })

      if (this.schemaGeneration !== currentSchemaGen) {
        // Discard stale response
        return
      }

      // 4. Project into bounded ModelFacingSchemaContext
      const projected = projectToModelFacingSchema(raw)

      this.setState({
        schemaStatus: 'loaded',
        rawSchema: raw,
        schemaContext: projected,
        schemaError: undefined,
      })
    } catch (err: unknown) {
      if (this.schemaGeneration !== currentSchemaGen) {
        return
      }
      if (err instanceof DOMException && err.name === 'AbortError') {
        return
      }

      if (err instanceof AgentClientError) {
        if (err.kind === 'authentication' || err.statusCode === 401) {
          // Authentication expired while introspecting schema
          this.setState({
            status: 'auth_required',
            profiles: [],
            selectedProfileId: null,
            selectedProfile: null,
            schemaStatus: 'idle',
            schemaContext: null,
            rawSchema: null,
            isModalOpen: true,
            errorMessage: undefined,
            schemaError: undefined,
          })
          return
        }

        if (err.kind === 'disconnected') {
          this.setState({
            status: 'disconnected',
            schemaStatus: 'error',
            schemaError: err.message,
            errorMessage: err.message,
          })
          return
        }
      }

      this.setState({
        schemaStatus: 'error',
        schemaError: err instanceof Error ? err.message : 'Failed to introspect database schema.',
      })
    } finally {
      if (this.currentSchemaAbortController === controller) {
        this.currentSchemaAbortController = null
      }
    }
  }

  /**
   * Clears the currently selected profile and schema state.
   */
  clearSelectedProfile(): void {
    this.schemaGeneration++
    if (this.currentSchemaAbortController) {
      this.currentSchemaAbortController.abort()
      this.currentSchemaAbortController = null
    }

    this.setState({
      selectedProfileId: null,
      selectedProfile: null,
      schemaStatus: 'idle',
      schemaContext: null,
      rawSchema: null,
      schemaError: undefined,
    })
  }

  /**
   * Retries schema introspection for the currently selected profile.
   */
  async retryLoadSchema(): Promise<void> {
    if (this.state.selectedProfileId) {
      await this.selectProfile(this.state.selectedProfileId)
    }
  }

  /**
   * Explicitly logs out of the local agent daemon.
   *
   * Increments generation counters to immediately invalidate any in-flight requests,
   * cancels active controllers, clears in-memory profile summaries and schema, and
   * transitions status to 'auth_required'.
   */
  async logout(): Promise<void> {
    // 1. Advance generation counters to disregard any late asynchronous responses
    this.generation++
    this.schemaGeneration++

    if (this.currentAbortController) {
      this.currentAbortController.abort()
      this.currentAbortController = null
    }
    if (this.currentSchemaAbortController) {
      this.currentSchemaAbortController.abort()
      this.currentSchemaAbortController = null
    }

    // 2. Clear state in memory immediately
    this.setState({
      status: 'auth_required',
      profiles: [],
      selectedProfileId: null,
      selectedProfile: null,
      schemaStatus: 'idle',
      schemaContext: null,
      rawSchema: null,
      schemaError: undefined,
      isModalOpen: false,
      isSubmitting: false,
      errorMessage: undefined,
      authError: undefined,
    })

    // 3. Dispatch logout to daemon to clear the HttpOnly ambient cookie
    try {
      await logoutAgent()
    } catch {
      // Regardless of daemon response (e.g. if already expired), local session remains cleared
    }
  }

  /**
   * Manually opens the authentication modal.
   */
  openModal(): void {
    this.setState({ isModalOpen: true, authError: undefined })
  }

  /**
   * Manually closes the authentication modal (only if not currently submitting).
   */
  closeModal(): void {
    if (!this.state.isSubmitting) {
      this.setState({ isModalOpen: false, authError: undefined })
    }
  }

  /**
   * Retries session discovery (e.g. after daemon reconnect).
   */
  retry(): Promise<void> {
    return this.probeSession()
  }
}

/**
 * Default shared coordinator instance for application runtime.
 */
export const defaultAgentCoordinator = new AgentSessionCoordinator()

/**
 * React hook connecting components to an AgentSessionCoordinator instance.
 */
export function useAgentSession(customCoordinator?: AgentSessionCoordinator): {
  state: AgentSessionState
  coordinator: AgentSessionCoordinator
} {
  const coordinator = useMemo(
    () => customCoordinator || defaultAgentCoordinator,
    [customCoordinator]
  )

  const state = useSyncExternalStore(
    (onStoreChange) => coordinator.subscribe(onStoreChange),
    () => coordinator.getState()
  )

  return { state, coordinator }
}
