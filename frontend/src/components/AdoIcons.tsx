export function AdoLogo({ size = 20, className = '' }: { size?: number; className?: string }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      className={className}
      aria-label="Azure DevOps Logo"
    >
      {/* Azure DevOps signature folded ribbon */}
      <path
        d="M22 6.4L15.3 2 9.4 6.8 3.5 9.4 2 15.6l5.2 6.4 12.3-4.2V6.4zm-3.5 8.9l-9.3 3.2-3.1-3.8.9-3.7 3.5-1.5 5.9 3.4 2.1 2.4z"
        fill="#0078D4"
      />
      <path
        d="M22 6.4v11.4l-12.3 4.2-5.2-6.4 1.5-6.2 3.4 2.8 9.3-3.2 3.3-2.6z"
        fill="#2B88D8"
        fillOpacity="0.4"
      />
    </svg>
  )
}

export function WaffleIcon({ size = 18, className = '' }: { size?: number; className?: string }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 20 20"
      fill="currentColor"
      className={className}
      aria-label="App Launcher"
    >
      <circle cx="3.5" cy="3.5" r="1.8" />
      <circle cx="10" cy="3.5" r="1.8" />
      <circle cx="16.5" cy="3.5" r="1.8" />
      <circle cx="3.5" cy="10" r="1.8" />
      <circle cx="10" cy="10" r="1.8" />
      <circle cx="16.5" cy="10" r="1.8" />
      <circle cx="3.5" cy="16.5" r="1.8" />
      <circle cx="10" cy="16.5" r="1.8" />
      <circle cx="16.5" cy="16.5" r="1.8" />
    </svg>
  )
}

export function BoardsIcon({ size = 16 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="currentColor">
      <path d="M1 2.5A1.5 1.5 0 0 1 2.5 1h11A1.5 1.5 0 0 1 15 2.5v11a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 1 13.5v-11zM2.5 2a.5.5 0 0 0-.5.5V5h12V2.5a.5.5 0 0 0-.5-.5h-11zM2 6v7.5a.5.5 0 0 0 .5.5H6V6H2zm5 8h2.5V6H7v8zm3.5 0h3a.5.5 0 0 0 .5-.5V6h-3.5v8z" />
    </svg>
  )
}

export function ReposIcon({ size = 16 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="currentColor">
      <path d="M11.75 2.5a2.25 2.25 0 1 0-3.32 1.97v4.06a2.25 2.25 0 1 0 1.5 0V4.47a2.25 2.25 0 0 0 1.82-1.97zM10.25 3.25a.75.75 0 1 1 0-1.5.75.75 0 0 1 0 1.5zm-5 5a2.25 2.25 0 1 0-1.5 2.13v1.87a.75.75 0 0 0 1.5 0v-1.87a2.25 2.25 0 0 0 0-2.13zm-.75.75a.75.75 0 1 1 0-1.5.75.75 0 0 1 0 1.5z" />
    </svg>
  )
}

export function PipelinesIcon({ size = 16 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="currentColor">
      <path d="M9.5 1.5a.5.5 0 0 1 .5.5v1.293l2.854 2.853a.5.5 0 0 1 0 .708l-2 2a.5.5 0 0 1-.708 0L9 7.707V12a1 1 0 0 1-1 1H4.414l-1.707 1.707A.5.5 0 0 1 2 14.354V8.5a.5.5 0 0 1 .5-.5h5.293l1.146-1.146a.5.5 0 0 1 .708 0l.853.853V2a.5.5 0 0 1 .5-.5z" />
    </svg>
  )
}

export function TestPlansIcon({ size = 16 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="currentColor">
      <path d="M6 1.5a.5.5 0 0 1 .5-.5h3a.5.5 0 0 1 0 1H9v2.586l4.707 6.59A1.5 1.5 0 0 1 12.485 13H3.515a1.5 1.5 0 0 1-1.222-2.324L7 4.586V2h-.5a.5.5 0 0 1-.5-.5z" />
    </svg>
  )
}

export function ArtifactsIcon({ size = 16 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="currentColor">
      <path d="M8.186 1.113a.5.5 0 0 0-.372 0L1.814 3.447A.5.5 0 0 0 1.5 3.91v8.18a.5.5 0 0 0 .314.463l6 2.333a.5.5 0 0 0 .372 0l6-2.333a.5.5 0 0 0 .314-.463V3.91a.5.5 0 0 0-.314-.463l-6-2.334zM8 2.21l5.05 1.964L8 6.138 2.95 4.174 8 2.21zm-5.5 3.03l5 1.944v6.606l-5-1.944V5.24zm6 8.55V7.184l5-1.944v6.606l-5 1.944z" />
    </svg>
  )
}

export function CopilotSparkleIcon({ size = 16 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="currentColor">
      <path d="M8 0a1 1 0 0 1 1 1c0 3.314 2.686 6 6 6a1 1 0 0 1 0 2c-3.314 0-6 2.686-6 6a1 1 0 0 1-2 0c0-3.314-2.686-6-6-6a1 1 0 0 1 0-2c3.314 0 6-2.686 6-6a1 1 0 0 1 1-1z" />
    </svg>
  )
}

export function SettingsGearIcon({ size = 16 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="currentColor">
      <path d="M8 4.75a3.25 3.25 0 1 0 0 6.5 3.25 3.25 0 0 0 0-6.5zM6.25 8a1.75 1.75 0 1 1 3.5 0 1.75 1.75 0 0 1-3.5 0z" />
      <path d="M9.775 1.25a1.25 1.25 0 0 0-1.768 0l-.53.53a.75.75 0 0 1-.722.193l-.727-.242a1.25 1.25 0 0 0-1.572.786l-.282.729a.75.75 0 0 1-.528.468l-.744.186a1.25 1.25 0 0 0-.936 1.488l.186.744a.75.75 0 0 1 0 .708l-.186.744a1.25 1.25 0 0 0 .936 1.488l.744.186a.75.75 0 0 1 .528.468l.282.729a1.25 1.25 0 0 0 1.572.786l.727-.242a.75.75 0 0 1 .722.193l.53.53a1.25 1.25 0 0 0 1.768 0l.53-.53a.75.75 0 0 1 .722-.193l.727.242a1.25 1.25 0 0 0 1.572-.786l.282-.729a.75.75 0 0 1 .528-.468l.744-.186a1.25 1.25 0 0 0 .936-1.488l-.186-.744a.75.75 0 0 1 0-.708l.186-.744a1.25 1.25 0 0 0-.936-1.488l-.744-.186a.75.75 0 0 1-.528-.468l-.282-.729a1.25 1.25 0 0 0-1.572-.786l-.727.242a.75.75 0 0 1-.722-.193l-.53-.53z" />
    </svg>
  )
}
