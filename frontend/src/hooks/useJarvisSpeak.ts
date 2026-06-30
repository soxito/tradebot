/**
 * useJarvisSpeak — trigger JARVIS speech from any component.
 *
 * Posts a `jarvis-speak` message to PaulChat which handles voice synthesis
 * through the same pipeline as normal JARVIS responses (AI voice or Web
 * Speech API fallback with the configured voice/rate/pitch).
 *
 * Usage:
 *   const speakAsJarvis = useJarvisSpeak()
 *   speakAsJarvis("Analysis complete. Buy signal detected.")
 */
import { useCallback } from 'react'

export function useJarvisSpeak() {
  return useCallback((text: string) => {
    if (typeof window === 'undefined' || !text?.trim()) return
    window.postMessage(
      { __jarvisPage: true, type: 'jarvis-speak', text: text.trim() },
      window.location.origin,
    )
  }, [])
}
