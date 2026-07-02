/*
 * FaceVisionPanel — JARVIS Room face vision (React)
 *
 * Runs in the page origin (localhost:3000) where the browser reliably prompts
 * for camera permission. Captures the webcam, streams JPEG frames to the
 * backend FaceLandmarker WebSocket, draws the live lip / face overlay, and lets
 * the user enroll their identity. Broadcasts face state to the JARVIS extension
 * (via window.postMessage → content.js) so the popup can mirror it.
 *
 * Backend:
 *   WS   ws(s)://<api-host>/api/v1/vision/face-stream
 *   REST http(s)://<api-host>/api/v1/vision/{enroll-face,profile}
 */
import React, { useCallback, useEffect, useRef, useState } from 'react'

// ── Resolve backend base + WS URL from the same env the app uses ─────────────
const API_BASE = (process.env.NEXT_PUBLIC_API_URL || 'http://localhost:1448/api/v1').replace(/\/$/, '')
const WS_URL = API_BASE.replace(/^http/, 'ws') + '/vision/face-stream'

const CAPTURE_FPS = 10
const CAPTURE_W = 320
const CAPTURE_H = 240
const JPEG_Q = 0.65
const MAR_TALKING = 0.30
const MAR_SILENCE = 0.10

type LandmarkPt = { x: number; y: number; z?: number }
type Box = { x: number; y: number; w: number; h: number }

export default function FaceVisionPanel() {
  const videoRef = useRef<HTMLVideoElement | null>(null)
  const overlayRef = useRef<HTMLCanvasElement | null>(null)
  const captureRef = useRef<HTMLCanvasElement | null>(null)
  const wsRef = useRef<WebSocket | null>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const captureTimer = useRef<ReturnType<typeof setInterval> | null>(null)
  const inFlight = useRef(0)
  const talkingRef = useRef(false)

  const [active, setActive] = useState(false)
  const [wsOk, setWsOk] = useState(false)
  const [enrolled, setEnrolled] = useState(false)
  const [facePresent, setFacePresent] = useState(false)
  const [identityMatch, setIdentityMatch] = useState(false)
  const [mar, setMar] = useState(0)
  const [isTalking, setIsTalking] = useState(false)
  const [err, setErr] = useState('')
  const [enrolling, setEnrolling] = useState(false)

  // ── Load enrollment status once ────────────────────────────────────────
  useEffect(() => {
    fetch(`${API_BASE}/vision/profile`, { signal: AbortSignal.timeout(2500) })
      .then(r => r.ok ? r.json() : null)
      .then(j => { if (j) setEnrolled(!!j.enrolled) })
      .catch(() => {})
  }, [])

  // ── Capture a JPEG frame → base64 ──────────────────────────────────────
  const captureJpeg = useCallback((): Promise<string> => {
    return new Promise((resolve, reject) => {
      const v = videoRef.current, cap = captureRef.current
      if (!v || !cap) return reject(new Error('no video'))
      const ctx = cap.getContext('2d')
      if (!ctx) return reject(new Error('no ctx'))
      ctx.drawImage(v, 0, 0, CAPTURE_W, CAPTURE_H)
      cap.toBlob(
        (blob) => {
          if (!blob) return reject(new Error('toBlob failed'))
          const reader = new FileReader()
          reader.onload = () => resolve(String(reader.result).split(',')[1])
          reader.onerror = reject
          reader.readAsDataURL(blob)
        },
        'image/jpeg',
        JPEG_Q,
      )
    })
  }, [])

  // ── Draw the overlay ───────────────────────────────────────────────────
  const drawOverlay = useCallback(
    (box: Box | null, landmarks: LandmarkPt[], talking: boolean, matched: boolean, marVal: number) => {
      const cv = overlayRef.current, v = videoRef.current
      if (!cv || !v) return
      if (cv.width !== v.videoWidth) { cv.width = v.videoWidth; cv.height = v.videoHeight }
      const ctx = cv.getContext('2d')
      if (!ctx) return
      ctx.clearRect(0, 0, cv.width, cv.height)
      if (!box) return

      const sx = cv.width / CAPTURE_W
      const sy = cv.height / CAPTURE_H
      const color = matched ? '#06b6d4' : (talking ? '#22c55e' : '#8b5cf6')

      // Face box + glow
      ctx.save()
      ctx.strokeStyle = color; ctx.lineWidth = 2
      ctx.shadowColor = color; ctx.shadowBlur = 10
      ctx.strokeRect(box.x * sx, box.y * sy, box.w * sx, box.h * sy)
      ctx.shadowBlur = 0

      // Face mesh points (dim)
      ctx.fillStyle = 'rgba(100,116,139,0.4)'
      landmarks.forEach(p => { ctx.beginPath(); ctx.arc(p.x * sx, p.y * sy, 1.2, 0, Math.PI * 2); ctx.fill() })

      // Lips (first 52 are lip points from the backend)
      const lipColor = talking ? '#22c55e' : '#06b6d4'
      ctx.fillStyle = lipColor; ctx.shadowColor = lipColor; ctx.shadowBlur = 5
      landmarks.slice(0, 52).forEach(p => { ctx.beginPath(); ctx.arc(p.x * sx, p.y * sy, 2.6, 0, Math.PI * 2); ctx.fill() })
      ctx.shadowBlur = 0

      // MAR bar under the box
      const bx = box.x * sx, by = (box.y + box.h) * sy + 5, bw = box.w * sx
      ctx.fillStyle = 'rgba(0,0,0,0.5)'; ctx.fillRect(bx, by, bw, 6)
      ctx.fillStyle = talking ? '#22c55e' : '#06b6d4'
      ctx.fillRect(bx, by, bw * Math.min(marVal / 0.65, 1), 6)

      // Identity label
      ctx.font = '12px monospace'
      ctx.fillStyle = matched ? '#06b6d4' : '#ef4444'
      ctx.fillText(matched ? '✓ YOU' : (enrolled ? '? UNKNOWN' : '⊕ ENROLL FACE'), box.x * sx + 4, box.y * sy - 6)
      ctx.restore()
    },
    [enrolled],
  )

  // ── Relay face state to the JARVIS extension (for popup mirroring) ─────
  const relayToExtension = useCallback(
    (present: boolean, talking: boolean, matched: boolean, marVal: number) => {
      try {
        window.postMessage(
          { __jarvisPage: true, type: 'jarvis-face-state', facePresent: present, isTalking: talking, identityMatch: matched, enrolled, mar: marVal },
          window.location.origin,
        )
      } catch { /* noop */ }
    },
    [enrolled],
  )

  // ── Handle a backend result ────────────────────────────────────────────
  const handleResult = useCallback(
    (data: any) => {
      const present = !!data.face
      const marVal = data.mar || 0
      const matched = !!data.identity_match
      setFacePresent(present)
      setIdentityMatch(matched)
      setMar(marVal)

      // Hysteresis on talking
      let talking = talkingRef.current
      if (marVal > MAR_TALKING || (data.jaw_open || 0) > 0.25) talking = true
      else if (marVal < MAR_SILENCE) talking = false
      talkingRef.current = talking
      setIsTalking(talking)

      drawOverlay(data.box || null, data.landmarks || [], talking, matched, marVal)
      relayToExtension(present, talking, matched, marVal)
    },
    [drawOverlay, relayToExtension],
  )

  // ── WebSocket ──────────────────────────────────────────────────────────
  const connectWs = useCallback(() => {
    if (wsRef.current && wsRef.current.readyState <= 1) return
    let ws: WebSocket
    try { ws = new WebSocket(WS_URL) } catch { return }
    wsRef.current = ws
    ws.onopen = () => { setWsOk(true); inFlight.current = 0 }
    ws.onmessage = (ev) => {
      inFlight.current = Math.max(0, inFlight.current - 1)
      try { handleResult(JSON.parse(ev.data)) } catch { /* noop */ }
    }
    ws.onerror = () => setWsOk(false)
    ws.onclose = () => {
      setWsOk(false)
      if (streamRef.current) setTimeout(connectWs, 3000)  // reconnect while camera on
    }
  }, [handleResult])

  // ── Frame sender ───────────────────────────────────────────────────────
  const sendFrame = useCallback(async () => {
    const ws = wsRef.current
    if (!ws || ws.readyState !== 1 || inFlight.current > 2) return
    try {
      const b64 = await captureJpeg()
      ws.send(JSON.stringify({ frame: b64, ts: Date.now() }))
      inFlight.current++
    } catch { /* skip */ }
  }, [captureJpeg])

  // ── Start / stop camera ────────────────────────────────────────────────
  const startCamera = useCallback(async () => {
    setErr('')
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        video: { width: { ideal: 640 }, height: { ideal: 480 }, frameRate: { ideal: 15 }, facingMode: 'user' },
        audio: false,
      })
      streamRef.current = stream
      const v = videoRef.current!
      v.srcObject = stream
      await v.play()
      setActive(true)
      connectWs()
      captureTimer.current = setInterval(sendFrame, 1000 / CAPTURE_FPS)
    } catch (e: any) {
      const name = e?.name || ''
      setErr(
        name === 'NotAllowedError'
          ? 'Camera permission denied. Click the camera icon in the address bar and allow it, then retry.'
          : name === 'NotFoundError'
          ? 'No camera found on this device.'
          : 'Could not start the camera: ' + (e?.message || name),
      )
    }
  }, [connectWs, sendFrame])

  const stopCamera = useCallback(() => {
    if (captureTimer.current) { clearInterval(captureTimer.current); captureTimer.current = null }
    if (wsRef.current) { try { wsRef.current.close() } catch {} wsRef.current = null }
    streamRef.current?.getTracks().forEach(t => t.stop())
    streamRef.current = null
    setActive(false); setWsOk(false); setFacePresent(false); setIsTalking(false)
    talkingRef.current = false
    const cv = overlayRef.current
    cv?.getContext('2d')?.clearRect(0, 0, cv.width, cv.height)
    relayToExtension(false, false, false, 0)
  }, [relayToExtension])

  // Cleanup on unmount
  useEffect(() => () => stopCamera(), [stopCamera])

  // ── Enroll / clear ─────────────────────────────────────────────────────
  const enroll = useCallback(async () => {
    if (!active) { setErr('Start the camera first.'); return }
    setEnrolling(true); setErr('')
    try {
      const frame = await captureJpeg()
      const r = await fetch(`${API_BASE}/vision/enroll-face`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ frame }),
        signal: AbortSignal.timeout(10000),
      })
      const j = await r.json()
      if (j.enrolled) { setEnrolled(true) }
      else setErr(j.detail || 'Enrollment failed — face the camera in good light.')
    } catch {
      setErr('Enrollment failed — is the backend running?')
    } finally {
      setEnrolling(false)
    }
  }, [active, captureJpeg])

  const clearEnroll = useCallback(async () => {
    await fetch(`${API_BASE}/vision/profile`, { method: 'DELETE' }).catch(() => {})
    setEnrolled(false)
  }, [])

  // ── UI ─────────────────────────────────────────────────────────────────
  const stat = (v: string, cls: string) => ({
    fontSize: 13, fontWeight: 700,
    color: cls === 'ok' ? '#22c55e' : cls === 'cyan' ? '#06b6d4' : cls === 'warn' ? '#f59e0b' : '#64748b',
  })
  const idText = !facePresent ? 'no face' : !enrolled ? 'not enrolled' : identityMatch ? '✓ YOU' : '? unknown'
  const idCls = !facePresent ? 'dim' : !enrolled ? 'warn' : identityMatch ? 'ok' : 'warn'

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
      {/* Video stage */}
      <div style={{ position: 'relative', width: '100%', aspectRatio: '4 / 3', background: '#000', borderRadius: 12, overflow: 'hidden', border: '1px solid rgba(45,226,197,0.28)' }}>
        <video ref={videoRef} autoPlay muted playsInline
          style={{ width: '100%', height: '100%', objectFit: 'cover', transform: 'scaleX(-1)', display: active ? 'block' : 'none' }} />
        <canvas ref={overlayRef}
          style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', transform: 'scaleX(-1)', pointerEvents: 'none', display: active ? 'block' : 'none' }} />
        <canvas ref={captureRef} width={CAPTURE_W} height={CAPTURE_H} style={{ display: 'none' }} />
        {!active && (
          <div style={{ position: 'absolute', inset: 0, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: 8, color: '#64748b', fontFamily: 'monospace', textAlign: 'center', padding: 16 }}>
            <div style={{ fontSize: 40 }}>📷</div>
            <div style={{ fontSize: 12, maxWidth: 240, lineHeight: 1.6 }}>Click <b style={{ color: '#7df3dd' }}>Start Camera</b> and allow access. Frames go only to your local backend.</div>
          </div>
        )}
        {isTalking && (
          <div style={{ position: 'absolute', top: 10, right: 10, transform: 'scaleX(-1)', background: 'rgba(34,197,94,0.2)', border: '1px solid rgba(34,197,94,0.5)', color: '#86efac', borderRadius: 20, padding: '3px 10px', fontSize: 11, fontFamily: 'monospace', fontWeight: 700 }}>🗣 TALKING</div>
        )}
      </div>

      {/* Stats */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 6, fontFamily: 'monospace' }}>
        <div><div style={{ fontSize: 9, color: '#64748b' }}>IDENTITY</div><div style={stat(idText, idCls)}>{idText}</div></div>
        <div><div style={{ fontSize: 9, color: '#64748b' }}>MOUTH</div><div style={stat('', isTalking ? 'ok' : 'cyan')}>{mar.toFixed(3)}</div></div>
        <div><div style={{ fontSize: 9, color: '#64748b' }}>SPEAKING</div><div style={stat('', isTalking ? 'ok' : 'dim')}>{isTalking ? 'talking' : 'silent'}</div></div>
        <div><div style={{ fontSize: 9, color: '#64748b' }}>BACKEND</div><div style={stat('', wsOk ? 'ok' : 'warn')}>{wsOk ? 'live' : 'off'}</div></div>
      </div>

      {/* MAR bar */}
      <div style={{ height: 6, borderRadius: 3, background: 'rgba(255,255,255,0.06)', overflow: 'hidden' }}>
        <div style={{ height: '100%', width: `${Math.min(mar / 0.65, 1) * 100}%`, background: isTalking ? '#22c55e' : '#06b6d4', transition: 'width .1s, background .15s' }} />
      </div>

      {/* Controls */}
      <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
        {!active ? (
          <button onClick={startCamera} style={btn('cyan')}>📷 Start Camera</button>
        ) : (
          <button onClick={stopCamera} style={btn()}>⏹ Stop</button>
        )}
        <button onClick={enroll} disabled={!active || enrolling} style={btn('green', !active || enrolling)}>
          {enrolling ? '⟳ Enrolling…' : enrolled ? '⊕ Re-Enroll' : '⊕ Enroll Face'}
        </button>
        <button onClick={clearEnroll} style={btn('red')}>✕ Clear</button>
      </div>

      {err && <div style={{ fontSize: 11, color: '#f59e0b', fontFamily: 'monospace', lineHeight: 1.5 }}>{err}</div>}
    </div>
  )
}

function btn(kind?: 'cyan' | 'green' | 'red', disabled = false): React.CSSProperties {
  const colors: Record<string, string> = {
    cyan: '#06b6d4', green: '#22c55e', red: '#ef4444', default: '#94a3b8',
  }
  const c = colors[kind || 'default']
  return {
    flex: 1, minWidth: 96, padding: '8px 6px', borderRadius: 9,
    fontFamily: 'monospace', fontSize: 11, fontWeight: 700, cursor: disabled ? 'default' : 'pointer',
    background: `${c}1a`, border: `1px solid ${c}66`, color: c, opacity: disabled ? 0.5 : 1,
  }
}
