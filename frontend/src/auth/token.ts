import { InteractionRequiredAuthError } from '@azure/msal-browser'
import { loginRequest, msalInstance } from './msalConfig'

// Renew the ID token when it expires within this many seconds.
const REFRESH_MARGIN_SECONDS = 300

function secondsUntilExpiry(idToken: string): number {
  try {
    const payload = idToken.split('.')[1].replace(/-/g, '+').replace(/_/g, '/')
    const padded = payload + '='.repeat((4 - (payload.length % 4)) % 4)
    const { exp } = JSON.parse(atob(padded)) as { exp?: number }
    return typeof exp === 'number' ? exp - Date.now() / 1000 : 0
  } catch {
    return 0
  }
}

/**
 * The signed-in user's Microsoft Entra ID token, used as `Authorization: Bearer <token>` for the backend.
 * Tokens are taken from the MSAL cache and renewed silently; if Microsoft requires interaction
 * (for example the session ended), the user is redirected to sign in again.
 */
export async function getIdToken(forceRefresh = false): Promise<string> {
  const account = msalInstance.getActiveAccount() ?? msalInstance.getAllAccounts()[0]
  if (!account) {
    throw new Error('You are not signed in.')
  }
  try {
    let result = await msalInstance.acquireTokenSilent({ ...loginRequest, account, forceRefresh })
    if (!forceRefresh && secondsUntilExpiry(result.idToken) < REFRESH_MARGIN_SECONDS) {
      result = await msalInstance.acquireTokenSilent({ ...loginRequest, account, forceRefresh: true })
    }
    return result.idToken
  } catch (error) {
    if (error instanceof InteractionRequiredAuthError) {
      await msalInstance.acquireTokenRedirect({ ...loginRequest, account })
    }
    throw error
  }
}
