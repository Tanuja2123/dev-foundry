import { InteractionStatus } from '@azure/msal-browser'
import { useMsal } from '@azure/msal-react'
import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'

export function AuthCallbackPage() {
  const { instance, inProgress, accounts } = useMsal()
  const navigate = useNavigate()
  const [error, setError] = useState<string | null>(() => {
    const params = new URLSearchParams(window.location.search)
    return params.get('error_description') ?? params.get('error')
  })

  useEffect(() => {
    const params = new URLSearchParams(window.location.search)
    const azureError = params.get('error_description') ?? params.get('error')
    if (azureError) {
      return
    }

    if (
      inProgress === InteractionStatus.Startup ||
      inProgress === InteractionStatus.HandleRedirect
    ) {
      return
    }

    if (accounts.length > 0) {
      navigate('/', { replace: true })
      return
    }

    void instance
      .handleRedirectPromise()
      .then((result) => {
        const account = result?.account ?? instance.getAllAccounts()[0]
        if (account) {
          instance.setActiveAccount(account)
          navigate('/', { replace: true })
          return
        }
        setError(
          'Sign-in did not complete. In Azure, register this exact SPA redirect URI: ' +
            (import.meta.env.VITE_AZURE_REDIRECT_URI || window.location.origin),
        )
      })
      .catch((reason: unknown) => {
        setError(reason instanceof Error ? reason.message : 'Sign-in failed.')
      })
  }, [accounts.length, inProgress, instance, navigate])

  return (
    <div className="auth-screen">
      <div className="auth-card">
        <h1>{error ? 'Sign-in failed' : 'Signing you in'}</h1>
        <p>{error ?? 'Please wait while we finish Microsoft authentication.'}</p>
        {error && (
          <button type="button" onClick={() => navigate('/', { replace: true })}>
            Back to sign in
          </button>
        )}
      </div>
    </div>
  )
}
