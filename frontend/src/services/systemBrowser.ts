/**
 * Helper to securely open external URLs in the user's default system browser.
 *
 * Dispatches via:
 * 1. Local Agent Daemon system endpoint (/agent/v1/system/open-browser) via OS webbrowser.open
 * 2. WebView2 / Tauri main-frame navigation (intercepted by Tauri ShellExecuteW)
 * 3. window.open fallback for standard browser environments
 */
import { getAgentBaseUrl } from './agentClient.ts'

export async function openExternalUrl(url: string): Promise<void> {
  const cleanUrl = url.trim()
  if (!cleanUrl) return

  // 1. Try local agent daemon endpoint (direct OS ShellExecute)
  try {
    const agentBase = getAgentBaseUrl()
    const openEndpoint = `${agentBase}/agent/v1/system/open-browser`
    const res = await fetch(openEndpoint, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-QueryPilot-Request': '1',
      },
      credentials: 'include',
      body: JSON.stringify({ url: cleanUrl }),
    })
    if (res.ok) {
      return
    }
  } catch {
    // If agent is not connected yet, proceed to browser/Tauri fallback
  }

  // 2. Try window.open fallback
  try {
    if (typeof window !== 'undefined') {
      const opened = window.open(cleanUrl, '_blank')
      if (opened) return
    }
  } catch {
    // Continue to location fallback
  }

  // 3. Main-frame navigation fallback (Tauri on_navigation intercepts this and opens system browser)
  try {
    if (typeof window !== 'undefined') {
      window.location.href = cleanUrl
    }
  } catch {
    // No-op
  }
}
