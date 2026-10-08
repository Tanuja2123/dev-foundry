import { EventType, InteractionStatus } from '@azure/msal-browser'
import {
  AuthenticatedTemplate,
  UnauthenticatedTemplate,
  useMsal,
} from '@azure/msal-react'
import { useEffect, useState } from 'react'
import { Route, Routes } from 'react-router-dom'
import { isAuthConfigured, loginRequest } from './auth/msalConfig'
import { AuthCallbackPage } from './pages/AuthCallbackPage'
import { HomePage } from './pages/HomePage'

function LoginScreen() {
  const { instance, inProgress } = useMsal()
  const [error, setError] = useState<string | null>(null)
  const busy =
    inProgress === InteractionStatus.AcquireToken ||
    inProgress === InteractionStatus.HandleRedirect

  useEffect(() => {
    const callbackId = instance.addEventCallback((message) => {
      if (message.eventType === EventType.ACQUIRE_TOKEN_FAILURE && message.error) {
        setError(message.error.message)
      }
    })
    return () => {
      if (callbackId) instance.removeEventCallback(callbackId)
    }
  }, [instance])

  async function signIn() {
    setError(null)
    try {
      await instance.handleRedirectPromise()
      await instance.loginRedirect(loginRequest)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Sign-in failed.')
    }
  }

  return (
    <div className="auth-screen">
      <div className="auth-card">
        <div className="ms-brand-logo" aria-hidden="true">
          <div className="ms-tile-red" />
          <div className="ms-tile-green" />
          <div className="ms-tile-blue" />
          <div className="ms-tile-yellow" />
        </div>
        <h1>DevFoundry</h1>
        <p>Sign in with your Microsoft account to continue to DevFoundry Copilot.</p>
        {error && <p className="auth-error">{error}</p>}
        <button
          type="button"
          className="ms-signin-button"
          disabled={busy}
          onClick={() => void signIn()}
        >
          <span>❖</span>
          <span>{busy ? 'Redirecting to Microsoft…' : 'Sign in with Microsoft'}</span>
        </button>
      </div>
    </div>
  )
}

function SetupScreen() {
  return (
    <div className="auth-screen">
      <div className="auth-card">
        <h1>Configure authentication</h1>
        <p>
          Copy <code>.env.example</code> to <code>.env</code> and add your Azure
          AD client ID and tenant ID, then restart the dev server.
        </p>
      </div>
    </div>
  )
}

function LoadingScreen() {
  return (
    <div className="auth-screen">
      <div className="auth-card">
        <h1>Loading</h1>
        <p>Checking your Microsoft session…</p>
      </div>
    </div>
  )
}

function SignedInApp() {
  const { inProgress } = useMsal()

  if (
    inProgress === InteractionStatus.Startup ||
    inProgress === InteractionStatus.HandleRedirect
  ) {
    return <LoadingScreen />
  }

  return (
    <>
      <AuthenticatedTemplate>
        <HomePage />
      </AuthenticatedTemplate>
      <UnauthenticatedTemplate>
        <LoginScreen />
      </UnauthenticatedTemplate>
    </>
  )
}

export default function App() {
  if (!isAuthConfigured) {
    return <SetupScreen />
  }

  return (
    <Routes>
      <Route path="/auth-callback" element={<AuthCallbackPage />} />
      <Route path="*" element={<SignedInApp />} />
    </Routes>
  )
}
