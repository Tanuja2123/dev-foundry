import { useEffect, useState } from 'react'
import { Copy, Check, X, FileText } from 'lucide-react'
import { MarkdownRenderer } from './MarkdownRenderer'

type DocumentDrawerProps = {
  isOpen: boolean
  docType: 'PRD' | 'TRD' | null
  projectName: string
  content: string | null
  updatedAt?: string | null
  onClose: () => void
}

export function DocumentDrawer({
  isOpen,
  docType,
  projectName,
  content,
  updatedAt,
  onClose,
}: DocumentDrawerProps) {
  const [copied, setCopied] = useState(false)

  // Close on Escape key press & prevent background scroll
  useEffect(() => {
    if (!isOpen) return

    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === 'Escape') {
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
  }, [isOpen, onClose])

  if (!isOpen) return null

  async function handleCopy() {
    if (!content) return
    try {
      await navigator.clipboard.writeText(content)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch {
      // Ignore clipboard error
    }
  }

  const isPrd = docType === 'PRD'
  const docLabel = isPrd
    ? 'Product Requirements Document (PRD)'
    : 'Technical Requirements Document (TRD)'

  return (
    <div className="drawer-portal" role="dialog" aria-modal="true" aria-label={docLabel}>
      {/* Semi-transparent backdrop */}
      <div
        className="drawer-backdrop"
        onClick={onClose}
        aria-hidden="true"
      />

      {/* Slide-over drawer container */}
      <aside className="document-drawer">
        {/* Drawer Header */}
        <div className="drawer-header">
          <div className="drawer-header-left">
            <div className="drawer-badge-row">
              <span className={`drawer-type-badge ${isPrd ? 'prd' : 'trd'}`}>
                {docType}
              </span>
              <span className="drawer-project-name" title={projectName}>
                {projectName}
              </span>
            </div>
            <h2 className="drawer-title">{docLabel}</h2>
            {updatedAt && (
              <p className="drawer-timestamp">
                Last updated: {new Date(updatedAt).toLocaleString()}
              </p>
            )}
          </div>

          <div className="drawer-header-actions">
            {content && (
              <button
                type="button"
                className="drawer-btn-copy"
                onClick={() => void handleCopy()}
                title="Copy markdown content to clipboard"
              >
                {copied ? (
                  <>
                    <Check size={14} />
                    <span>Copied</span>
                  </>
                ) : (
                  <>
                    <Copy size={14} />
                    <span>Copy</span>
                  </>
                )}
              </button>
            )}
            <button
              type="button"
              className="drawer-btn-close"
              onClick={onClose}
              title="Close drawer (Esc)"
              aria-label="Close document drawer"
            >
              <X size={16} />
            </button>
          </div>
        </div>

        {/* Drawer Scrollable Content */}
        <div className="drawer-body">
          {content ? (
            <div className="drawer-markdown-wrapper">
              <MarkdownRenderer content={content} />
            </div>
          ) : (
            <div className="drawer-empty-state">
              <div className="drawer-empty-icon">
                <FileText size={36} />
              </div>
              <h3>Document Not Available</h3>
              <p>This document has not been generated for this project yet.</p>
            </div>
          )}
        </div>

        {/* Drawer Footer */}
        <div className="drawer-footer">
          <span className="drawer-footer-hint">Press <kbd>Esc</kbd> or click outside to close</span>
          <button
            type="button"
            className="drawer-footer-close-btn"
            onClick={onClose}
          >
            Done
          </button>
        </div>
      </aside>
    </div>
  )
}
