/**
 * useRoomAnnouncements — JARVIS reports the outcome of a completed board meeting.
 *
 * Speaks through the existing JARVIS voice pipeline and raises a browser
 * notification so a completed analysis still reaches the user on another tab.
 */
import { useEffect, useRef } from 'react'
import { useJarvisSpeak } from '@/hooks/useJarvisSpeak'
import type { RoomSeat, RoomSession } from '@/hooks/useTradingRoom'

function buildScript(session: RoomSession, seats: RoomSeat[]): string {
  const symbol = session.symbol ?? 'the pair'
  const action = (session.final_action ?? 'hold').toUpperCase()
  const consensus = session.consensus
  const confidence = Math.round((session.final_confidence ?? 0) * 100)

  if (!consensus) return `Analysis complete on ${symbol}. Recommendation: ${action}.`

  const votes = consensus.tally
  const total = votes.buy + votes.sell + votes.hold
  const leaderVotes = votes[consensus.leader as keyof typeof votes] ?? 0

  if (total > 1 && leaderVotes === total) {
    return `Unanimous on ${symbol}. All ${total} agents recommend ${action}, confidence ${confidence} percent.`
  }
  if (consensus.agreement < 0.6) {
    const dissent = seats
      .filter((s) => s.last_decision && s.session_id === session.session_id)
      .slice(0, 2)
      .map((s) => s.human_name)
      .join(' and ')
    return `Split decision on ${symbol}. ${leaderVotes} of ${total} agents favour ${action}${
      dissent ? `, with ${dissent} on the record` : ''
    }. Confidence ${confidence} percent.`
  }
  return `Analysis complete on ${symbol}. ${leaderVotes} of ${total} agents agree on ${action}, confidence ${confidence} percent.`
}

function notify(session: RoomSession, body: string) {
  if (typeof window === 'undefined' || !('Notification' in window)) return
  if (Notification.permission !== 'granted') return
  try {
    new Notification(`${session.symbol} — ${(session.final_action ?? 'hold').toUpperCase()}`, {
      body,
      tag: session.session_id,
    })
  } catch {
    /* some browsers block constructor notifications outside a service worker */
  }
}

export function useRoomAnnouncements(
  session: RoomSession | null,
  { enabled, seats }: { enabled: boolean; seats: RoomSeat[] },
) {
  const speak = useJarvisSpeak()
  const announced = useRef<string | null>(null)
  const seatsRef = useRef(seats)
  useEffect(() => { seatsRef.current = seats }, [seats])

  // Ask once, on mount, so the permission prompt is tied to opening the room.
  useEffect(() => {
    if (typeof window === 'undefined' || !('Notification' in window)) return
    if (Notification.permission === 'default') void Notification.requestPermission()
  }, [])

  useEffect(() => {
    if (!session || session.status !== 'complete') return
    if (announced.current === session.session_id) return
    announced.current = session.session_id

    const script = buildScript(session, seatsRef.current)
    notify(session, session.final_reasoning || script)
    if (enabled) speak(script)
  }, [session, enabled, speak])
}
