import { PublicClientApplication } from '@azure/msal-browser'
import type { Configuration } from '@azure/msal-browser'

const clientId = import.meta.env.VITE_AZURE_CLIENT_ID?.trim() ?? ''
const tenantId = import.meta.env.VITE_AZURE_TENANT_ID?.trim() ?? ''

function resolveUris() {
  const configured = import.meta.env.VITE_AZURE_REDIRECT_URI?.trim()
  const fallbackOrigin =
    typeof window !== 'undefined' ? window.location.origin : 'http://localhost:5173'

  if (!configured) {
    return { redirectUri: fallbackOrigin, postLogoutRedirectUri: fallbackOrigin }
  }

  try {
    const url = new URL(configured)
    return {
      redirectUri: configured.replace(/\/$/, ''),
      postLogoutRedirectUri: url.origin,
    }
  } catch {
    return { redirectUri: fallbackOrigin, postLogoutRedirectUri: fallbackOrigin }
  }
}

const { redirectUri, postLogoutRedirectUri } = resolveUris()

export const isAuthConfigured = Boolean(clientId && tenantId)

const msalConfig: Configuration = {
  auth: {
    clientId: clientId || 'missing-client-id',
    authority: `https://login.microsoftonline.com/${tenantId || 'common'}`,
    redirectUri,
    postLogoutRedirectUri,
  },
  cache: {
    // localStorage keeps the sign-in across browser restarts (sessionStorage is wiped when the browser closes,
    // which forced a sign-in redirect - a full page reload - every time the browser was reopened).
    cacheLocation: 'localStorage',
  },
}

export const loginRequest = {
  scopes: ['openid', 'profile'],
}

export const msalInstance = new PublicClientApplication(msalConfig)
