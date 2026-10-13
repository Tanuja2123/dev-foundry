import { useEffect } from 'react'
import { AlertTriangle, Loader2 } from 'lucide-react'

type ConfirmModalProps = {
  isOpen: boolean
  title: string
  message: string
  confirmLabel?: string
  cancelLabel?: string
  isDestructive?: boolean
  isLoading?: boolean
  onConfirm: () => void | Promise<void>
  onCancel: () => void
}

export function ConfirmModal({
  isOpen,
  title,
  message,
  confirmLabel = 'Delete',
  cancelLabel = 'Cancel',
  isDestructive = true,
  isLoading = false,
  onConfirm,
  onCancel,
}: ConfirmModalProps) {
  useEffect(() => {
    if (!isOpen) return

    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === 'Escape' && !isLoading) {
        onCancel()
      }
    }

    window.addEventListener('keydown', handleKeyDown)
    const originalOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'

    return () => {
      window.removeEventListener('keydown', handleKeyDown)
      document.body.style.overflow = originalOverflow
    }
  }, [isOpen, isLoading, onCancel])

  if (!isOpen) return null

  return (
    <div className="modal-portal" role="dialog" aria-modal="true" aria-labelledby="confirm-modal-title">
      <div
        className="modal-backdrop"
        onClick={() => {
          if (!isLoading) onCancel()
        }}
        aria-hidden="true"
      />
      <div className="modal-dialog confirm-modal-dialog">
        <div className="confirm-modal-header">
          <div className="confirm-modal-icon-wrapper">
            <AlertTriangle size={22} className="confirm-modal-icon" />
          </div>
          <div className="confirm-modal-title-col">
            <h3 id="confirm-modal-title" className="confirm-modal-title">
              {title}
            </h3>
            <p className="confirm-modal-message">{message}</p>
          </div>
        </div>

        <div className="modal-actions">
          <button
            type="button"
            className="modal-btn modal-btn--secondary"
            onClick={onCancel}
            disabled={isLoading}
          >
            {cancelLabel}
          </button>
          <button
            type="button"
            className={`modal-btn ${isDestructive ? 'modal-btn--danger' : 'modal-btn--primary'}`}
            onClick={() => void onConfirm()}
            disabled={isLoading}
          >
            {isLoading ? (
              <>
                <Loader2 size={14} className="spin-icon" />
                <span>Deleting…</span>
              </>
            ) : (
              confirmLabel
            )}
          </button>
        </div>
      </div>
    </div>
  )
}
