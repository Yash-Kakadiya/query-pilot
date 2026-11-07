import type { ProfileSummary } from '../types/api.ts'
import type { AgentSessionStatus } from '../services/agentSession.ts'

export interface ResolveAutoProfileSelectionParams {
  status: AgentSessionStatus
  profiles: ProfileSummary[]
  currentSelectedId: string | null
  userExplicitlyCleared?: boolean
}

/**
 * Deterministically resolves the target database profile selection according to B29-B priority:
 *
 * 1. If the user has already explicitly selected a valid profile, preserve that selection.
 * 2. Otherwise, if the available profile list identifies a valid default profile (is_default === true), select it.
 * 3. Otherwise, select the first available profile in the existing deterministic order.
 * 4. If no profiles are available, retain the existing empty state (null).
 *
 * Additional requirements & guards:
 * - Do not select a profile until profile loading has completed successfully (status === 'authenticated').
 * - Do not overwrite a user's explicit selection when profiles refresh or reload.
 * - Handle loading, empty, and error states without selecting an invalid or stale profile.
 * - If the selected profile is deleted or becomes unavailable, clear the invalid selection and apply normal fallback priority.
 * - Respects explicit user clearing (userExplicitlyCleared === true) without auto-reselecting.
 */
export function resolveAutoProfileSelection(
  params: ResolveAutoProfileSelectionParams
): string | null {
  const { status, profiles, currentSelectedId, userExplicitlyCleared } = params

  // Do not select a profile until profile loading has completed successfully
  if (status !== 'authenticated') {
    return null
  }

  // If no profiles are available, retain the existing empty state
  if (!profiles || profiles.length === 0) {
    return null
  }

  // If the user explicitly cleared the selection, preserve the cleared state
  if (userExplicitlyCleared) {
    return null
  }

  // 1. If the user has already selected a valid profile, preserve that selection
  if (currentSelectedId) {
    const isCurrentValid = profiles.some((p) => p.profile_id === currentSelectedId)
    if (isCurrentValid) {
      return currentSelectedId
    }
    // If the selected profile was deleted or became unavailable, fall through to fallback priority (Step 2/3)
  }

  // 2. Otherwise, if the available profile list identifies a valid default profile, select it
  const defaultProfile = profiles.find((p) => Boolean(p.is_default))
  if (defaultProfile) {
    return defaultProfile.profile_id
  }

  // 3. Otherwise, select the first available profile in the existing deterministic order
  return profiles[0]?.profile_id || null
}
