import { EventType } from '@azure/msal-browser'
import type { AuthenticationResult } from '@azure/msal-browser'
import { MsalProvider } from '@azure/msal-react'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import App from './App.tsx'
import { isAuthConfigured, msalInstance } from './auth/msalConfig'
import './index.css'

async function bootstrap() {
  const root = createRoot(document.getElementById('root')!)

  if (!isAuthConfigured) {
    root.render(
      <StrictMode>
        <App />
      </StrictMode>,
    )
    return
  }

  await msalInstance.initialize()
  await msalInstance.handleRedirectPromise().catch(() => undefined)

  const existing =
    msalInstance.getActiveAccount() ?? msalInstance.getAllAccounts()[0]
  if (existing) {
    msalInstance.setActiveAccount(existing)
  }

  msalInstance.addEventCallback((event) => {
    if (event.eventType === EventType.LOGIN_SUCCESS && event.payload) {
      const result = event.payload as AuthenticationResult
      if (result.account) {
        msalInstance.setActiveAccount(result.account)
      }
    }
  })

  root.render(
    <StrictMode>
      <MsalProvider instance={msalInstance}>
        <BrowserRouter>
          <App />
        </BrowserRouter>
      </MsalProvider>
    </StrictMode>,
  )
}

void bootstrap()
