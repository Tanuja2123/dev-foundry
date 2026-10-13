import { useEffect, useRef, useState } from 'react'

export type TypewriterTextProps = {
  text: string
  speed?: number
  showCursor?: boolean
  /** Type the new text in when `text` changes (otherwise it is swapped instantly). */
  animateOnChange?: boolean
  /** Type the text in when the component first appears (e.g. a conversation that was just created). */
  animateOnMount?: boolean
  className?: string
}

export function TypewriterText({
  text,
  speed = 30,
  showCursor = true,
  animateOnChange = true,
  animateOnMount = false,
  className = '',
}: TypewriterTextProps) {
  const [displayedText, setDisplayedText] = useState(animateOnMount ? '' : text)
  const [isTyping, setIsTyping] = useState(false)
  // The text that is fully displayed. A re-run of the effect for the same text (e.g. React StrictMode mounting
  // twice in development, or an interrupted animation) restarts the animation instead of leaving it half-typed.
  const shownTextRef = useRef<string | null>(animateOnMount ? null : text)
  const isFirstRunRef = useRef(true)

  useEffect(() => {
    const firstRun = isFirstRunRef.current
    isFirstRunRef.current = false
    if (shownTextRef.current === text) {
      return
    }

    const reducedMotion =
      typeof window !== 'undefined' && window.matchMedia('(prefers-reduced-motion: reduce)').matches
    const shouldAnimate = firstRun ? animateOnMount : animateOnChange
    if (!text || reducedMotion || !shouldAnimate) {
      shownTextRef.current = text
      setDisplayedText(text)
      return
    }

    setIsTyping(true)
    let charIndex = 0
    setDisplayedText('')

    const timer = setInterval(() => {
      charIndex++
      setDisplayedText(text.slice(0, charIndex))
      if (charIndex >= text.length) {
        clearInterval(timer)
        shownTextRef.current = text
        setIsTyping(false)
      }
    }, speed)

    return () => {
      clearInterval(timer)
      setIsTyping(false)
    }
  }, [text, speed, animateOnChange, animateOnMount])

  return (
    <span className={`typewriter-container ${className}`.trim()}>
      <span className="typewriter-text">{displayedText}</span>
      {isTyping && showCursor && <span className="typewriter-caret" aria-hidden="true" />}
    </span>
  )
}
