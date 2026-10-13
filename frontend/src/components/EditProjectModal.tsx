import { useEffect, useRef, useState, type FormEvent } from 'react'
import { Folder, GitBranch, Loader2, X, AlertCircle } from 'lucide-react'

type EditProjectModalProps = {
  isOpen: boolean
  initialName: string
  isAdoLinked?: boolean
  onSave: (newName: string) => Promise<void>
  onClose: () => void
}

export function EditProjectModal({
  isOpen,
  initialName,
  isAdoLinked = false,
  onSave,
  onClose,
}: EditProjectModalProps) {
  const [name, setName] = useState(initialName)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (isOpen) {
      setName(initialName)
      setError(null)
      setSaving(false)
      // Focus and select input after modal opens
      setTimeout(() => {
        inputRef.current?.focus()
        inputRef.current?.select()
      }, 50)
    }
  }, [isOpen, initialName])

  useEffect(() => {
    if (!isOpen) return

    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === 'Escape' && !saving) {
        onClose()
      }
    }

    window.addEventListener('keydown', handleKeyDown)
    const originalOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'

    return () => {
      window.removeEventListener('keydown', handleKeyDown)
      document.body.style.overflow = originalOverflow
    }
  }, [isOpen, saving, onClose])

  if (!isOpen) return null

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    const trimmed = name.trim()
    if (!trimmed) {
      setError('Project name cannot be empty.')
      return
    }
    if (trimmed === initialName.trim()) {
      onClose()
      return
    }

    setSaving(true)
    setError(null)
    try {
      await onSave(trimmed)
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update project name.')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="modal-portal" role="dialog" aria-modal="true" aria-labelledby="edit-modal-title">
      <div
        className="modal-backdrop"
        onClick={() => {
          if (!saving) onClose()
        }}
        aria-hidden="true"
      />
      <div className="modal-dialog edit-project-modal-dialog">
        <div className="modal-header">
          <div className="modal-header-title-row">
            <span className="modal-header-icon">
              <Folder size={18} />
            </span>
            <h3 id="edit-modal-title" className="modal-title">
              Edit Project Name
            </h3>
          </div>
          <button
            type="button"
            className="modal-close-btn"
            onClick={onClose}
            disabled={saving}
            title="Close dialog (Esc)"
            aria-label="Close dialog"
          >
            <X size={16} />
          </button>
        </div>

        <form onSubmit={(e) => void handleSubmit(e)} className="edit-project-form">
          <div className="modal-body">
            {error && (
              <div className="modal-error-banner" role="alert">
                <AlertCircle size={15} className="modal-error-icon" />
                <span>{error}</span>
              </div>
            )}

            <div className="modal-form-group">
              <label htmlFor="edit-project-name-input" className="modal-label">
                Project Name
              </label>
              <input
                id="edit-project-name-input"
                ref={inputRef}
                type="text"
                className="modal-input"
                value={name}
                onChange={(e) => {
                  setName(e.target.value)
                  if (error) setError(null)
                }}
                disabled={saving}
                placeholder="Enter project name…"
                maxLength={64}
                required
              />
            </div>

            {isAdoLinked ? (
              <div className="modal-info-box modal-info-box--ado">
                <div className="modal-info-badge">
                  <GitBranch size={13} />
                  <span>Azure DevOps Synced</span>
                </div>
                <p className="modal-info-text">
                  This project is provisioned in Azure DevOps. Saving will update both your DevFoundry workspace
                  and rename the project in Azure DevOps.
                </p>
              </div>
            ) : (
              <p className="modal-hint-text">
                Renaming this project will keep the portfolio overview and sidebar conversation strictly in sync.
              </p>
            )}
          </div>

          <div className="modal-actions">
            <button
              type="button"
              className="modal-btn modal-btn--secondary"
              onClick={onClose}
              disabled={saving}
            >
              Cancel
            </button>
            <button
              type="submit"
              className="modal-btn modal-btn--primary"
              disabled={saving || !name.trim() || name.trim() === initialName.trim()}
            >
              {saving ? (
                <>
                  <Loader2 size={14} className="spin-icon" />
                  <span>{isAdoLinked ? 'Updating in Azure DevOps…' : 'Saving…'}</span>
                </>
              ) : (
                'Save Changes'
              )}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
