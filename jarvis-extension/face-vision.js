/**
 * JARVIS Face Vision Module  v1.0.0
 *
 * GPU-accelerated face detection, recognition, and lip tracking for JARVIS.
 *
 * Architecture
 * ────────────
 *  1. Camera captured via getUserMedia in the extension popup
 *  2. Frames captured at ~10fps (320×240 JPEG) and sent to the backend
 *     WebSocket at ws://127.0.0.1:1448/api/v1/vision/face-stream
 *  3. Backend uses MediaPipe Python with GPU (CUDA/Metal) for:
 *       • Face detection  (presence, bounding box)
 *       • 478-point face mesh  (lips, jaw, nose, eyes)
 *       • Mouth Aspect Ratio  (MAR) → talking / silent
 *       • Face identity recognition (compare to enrolled descriptor)
 *  4. Lip landmarks drawn back on canvas overlay in real-time
 *  5. Face state (present / talking / identity) broadcast to background.js
 *     so speech recognition can be gated on face activity
 *
 * GitHub references
 * ─────────────────
 *  • MediaPipe  – google-ai-edge/mediapipe  (36k ⭐)
 *  • face_recognition – ageitgey/face_recognition  (53k ⭐)
 *  • BMehar98/Lip-Reading-Web-Application  – combined MediaPipe + STT
 */

;(() => {
  'use strict'

  const VERSION = '1.0.0'
  const TAG     = '[JARVIS-FACE]'
  const BACKEND_WS   = 'ws://127.0.0.1:1448/api/v1/vision/face-stream'
  const BACKEND_HTTP = 'http://127.0.0.1:1448/api/v1/vision'

  // ── Detection parameters ────────────────────────────────────────────────
  const CAPTURE_FPS        = 10        // frames/sec sent to backend
  const CAPTURE_W          = 320       // send at lower res for speed
  const CAPTURE_H          = 240
  const JPEG_QUALITY       = 0.65      // JPEG compression 0–1
  const MAR_TALKING        = 0.30      // mouth open ratio → talking
  const MAR_SILENCE        = 0.10      // mouth closed → definitely silent
  const WS_RECONNECT_MS    = 3000      // reconnect wait
  const LIP_HISTORY_MAX    = 300       // rolling lip buffer (≈30s at 10fps)

  // ── DOM refs (set by init()) ─────────────────────────────────────────────
  let videoEl    = null
  let overlayCanvas = null   // overlay drawn here
  let captureCanvas = null   // offscreen, for JPEG capture
  let captureCtx    = null
  let overlayCtx    = null

  // ── Camera state ─────────────────────────────────────────────────────────
  let cameraStream   = null
  let cameraActive   = false
  let captureTimer   = null

  // ── WebSocket state ──────────────────────────────────────────────────────
  let ws             = null
  let wsReady        = false
  let wsReconnTimer  = null
  let framesInFlight = 0

  // ── Face + lip state ─────────────────────────────────────────────────────
  let facePresent     = false
  let identityMatch   = false
  let isEnrolled      = false
  let currentMAR      = 0
  let isTalking       = false
  let lastBox         = null          // {x,y,w,h} pixel coords from backend
  let lastLandmarks   = []            // last received landmark array
  let lipHistory      = []            // [{t, mar, pts}] for learning
  let lastLipSentMs   = 0

  // ── Event bus ────────────────────────────────────────────────────────────
  const listeners = {}
  function emit (ev, data) {
    (listeners[ev] || []).forEach(fn => fn(data))
  }

  // ─────────────────────────────────────────────────────────────────────────
  //  Public API
  // ─────────────────────────────────────────────────────────────────────────
  window.JarvisFaceVision = {

    /** Register event handler. Returns unsubscribe fn. */
    on (ev, fn) {
      ;(listeners[ev] ??= []).push(fn)
      return () => { listeners[ev] = (listeners[ev] || []).filter(f => f !== fn) }
    },

    /**
     * Initialise the module.
     * @param {HTMLVideoElement}  videoElement   – live camera feed
     * @param {HTMLCanvasElement} canvasElement  – overlay canvas (same size)
     */
    async init (videoElement, canvasElement) {
      videoEl       = videoElement
      overlayCanvas = canvasElement
      overlayCtx    = canvasElement.getContext('2d')

      // Offscreen canvas for JPEG capture (resized to CAPTURE_W × CAPTURE_H)
      captureCanvas     = document.createElement('canvas')
      captureCanvas.width  = CAPTURE_W
      captureCanvas.height = CAPTURE_H
      captureCtx        = captureCanvas.getContext('2d')

      // Load enrollment status from backend
      await _loadProfile()

      console.log(`${TAG} v${VERSION} initialised`)
      emit('ready', { enrolled: isEnrolled })
    },

    async startCamera () {
      if (cameraActive) return
      try {
        cameraStream = await navigator.mediaDevices.getUserMedia({
          video: {
            width:     { ideal: 640, max: 1280 },
            height:    { ideal: 480, max: 720  },
            frameRate: { ideal: 15,  max: 30   },
            facingMode: 'user',
          },
          audio: false,
        })
        videoEl.srcObject = cameraStream
        await videoEl.play()
        cameraActive = true

        videoEl.addEventListener('loadedmetadata', () => {
          overlayCanvas.width  = videoEl.videoWidth
          overlayCanvas.height = videoEl.videoHeight
        }, { once: true })

        _connectWS()

        // Start sending frames after a short warm-up
        setTimeout(() => {
          captureTimer = setInterval(_sendFrame, 1000 / CAPTURE_FPS)
        }, 500)

        emit('camera', { active: true })
        console.log(`${TAG} Camera started`)
      } catch (err) {
        console.error(`${TAG} Camera error:`, err)
        emit('error', { msg: 'Camera access denied — allow camera in browser settings', err })
      }
    },

    stopCamera () {
      clearInterval(captureTimer)
      captureTimer = null
      clearTimeout(wsReconnTimer)
      wsReconnTimer = null
      if (ws) { ws.close(); ws = null }
      wsReady = false
      cameraStream?.getTracks().forEach(t => t.stop())
      cameraStream = null
      cameraActive = false
      facePresent  = false
      isTalking    = false
      overlayCtx?.clearRect(0, 0, overlayCanvas.width, overlayCanvas.height)
      emit('camera', { active: false })
      _notifyBackground({ facePresent: false, isTalking: false })
      console.log(`${TAG} Camera stopped`)
    },

    /** Capture current face and store as enrolled identity. */
    async enrollFace () {
      if (!cameraActive) {
        emit('error', { msg: 'Start camera first before enrolling' })
        return false
      }
      if (!wsReady) {
        emit('error', { msg: 'Backend WebSocket not connected' })
        return false
      }
      emit('enrolling', { stage: 'start' })
      try {
        const frame = await _captureJpeg()
        const resp  = await fetch(`${BACKEND_HTTP}/enroll-face`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ frame }),
          signal: AbortSignal.timeout(10000),
        })
        const json = await resp.json()
        if (json.enrolled) {
          isEnrolled = true
          emit('enrolled', { success: true })
          console.log(`${TAG} Face enrolled successfully`)
          return true
        }
        emit('error', { msg: json.detail || 'Enrollment failed' })
      } catch (err) {
        emit('error', { msg: 'Enrollment failed — ensure backend is running', err })
      }
      return false
    },

    async clearEnrollment () {
      await fetch(`${BACKEND_HTTP}/profile`, { method: 'DELETE' }).catch(() => {})
      isEnrolled = false
      emit('enrolled', { success: false, cleared: true })
    },

    // State accessors
    isActive:      () => cameraActive,
    isEnrolled:    () => isEnrolled,
    isFacePresent: () => facePresent,
    isUserTalking: () => isTalking,
    getMAR:        () => currentMAR,
    getLipHistory: () => [...lipHistory],
    wsConnected:   () => wsReady,
  }

  // ─────────────────────────────────────────────────────────────────────────
  //  WebSocket
  // ─────────────────────────────────────────────────────────────────────────

  function _connectWS () {
    if (ws && ws.readyState <= 1) return  // already connecting/open
    try {
      ws = new WebSocket(BACKEND_WS)
    } catch (e) {
      _scheduleReconnect()
      return
    }

    ws.onopen = () => {
      wsReady = true
      framesInFlight = 0
      console.log(`${TAG} WebSocket connected`)
      emit('ws', { connected: true })
    }

    ws.onmessage = ev => {
      framesInFlight = Math.max(0, framesInFlight - 1)
      try {
        const data = JSON.parse(ev.data)
        _handleBackendResult(data)
      } catch { /* ignore malformed */ }
    }

    ws.onerror = () => { wsReady = false }

    ws.onclose = () => {
      wsReady = false
      emit('ws', { connected: false })
      if (cameraActive) _scheduleReconnect()
    }
  }

  function _scheduleReconnect () {
    clearTimeout(wsReconnTimer)
    wsReconnTimer = setTimeout(() => {
      if (cameraActive) _connectWS()
    }, WS_RECONNECT_MS)
  }

  // ─────────────────────────────────────────────────────────────────────────
  //  Frame capture & sending
  // ─────────────────────────────────────────────────────────────────────────

  async function _sendFrame () {
    if (!cameraActive || !wsReady || videoEl.paused || framesInFlight > 2) return
    try {
      const b64 = await _captureJpeg()
      ws.send(JSON.stringify({ frame: b64, ts: Date.now() }))
      framesInFlight++
    } catch { /* skip frame */ }
  }

  function _captureJpeg () {
    return new Promise((resolve, reject) => {
      captureCtx.drawImage(videoEl, 0, 0, CAPTURE_W, CAPTURE_H)
      captureCanvas.toBlob(blob => {
        if (!blob) { reject(new Error('toBlob failed')); return }
        const reader = new FileReader()
        reader.onload = () => resolve(reader.result.split(',')[1])
        reader.onerror = reject
        reader.readAsDataURL(blob)
      }, 'image/jpeg', JPEG_QUALITY)
    })
  }

  // ─────────────────────────────────────────────────────────────────────────
  //  Handle backend results
  // ─────────────────────────────────────────────────────────────────────────

  function _handleBackendResult (data) {
    if (data.error) {
      console.warn(`${TAG} Backend error:`, data.error)
      return
    }

    const wasFacePresent = facePresent
    const wasTalking     = isTalking
    const wasIdentity    = identityMatch

    facePresent   = !!data.face
    currentMAR    = data.mar || 0
    identityMatch = !!data.identity_match
    lastLandmarks = data.landmarks || []
    lastBox       = data.box || null

    // Hysteresis for talking state
    if (currentMAR > MAR_TALKING)    isTalking = true
    else if (currentMAR < MAR_SILENCE) isTalking = false
    // Between thresholds: keep current state (hysteresis)

    // Record lip history
    const now = Date.now()
    if (facePresent && lastLandmarks.length > 0) {
      lipHistory.push({ t: now, mar: currentMAR, pts: lastLandmarks.slice(0, 20) })
      if (lipHistory.length > LIP_HISTORY_MAX) lipHistory.shift()
    }

    // Send lip data to backend every 5s for learning
    if (now - lastLipSentMs > 5000 && lipHistory.length > 10) {
      _sendLipLearningData()
      lastLipSentMs = now
    }

    // Emit events on state changes
    if (facePresent !== wasFacePresent) {
      emit('face', { present: facePresent, mar: currentMAR, matched: identityMatch })
    }
    if (isTalking !== wasTalking) {
      emit('talking', { talking: isTalking, mar: currentMAR })
    }
    if (identityMatch !== wasIdentity) {
      emit('identity', { matched: identityMatch })
    }

    // Always emit frame update for canvas draw
    emit('frame', { facePresent, mar: currentMAR, isTalking, identityMatch })

    // Notify background.js
    _notifyBackground({ facePresent, isTalking, mar: currentMAR, identityMatch })

    // Draw canvas overlay
    _drawOverlay()
  }

  // ─────────────────────────────────────────────────────────────────────────
  //  Canvas overlay drawing
  // ─────────────────────────────────────────────────────────────────────────

  function _drawOverlay () {
    const cw = overlayCanvas.width
    const ch = overlayCanvas.height

    overlayCtx.clearRect(0, 0, cw, ch)

    if (!facePresent || !lastBox) return

    const { x, y, w, h } = lastBox
    // Scale from CAPTURE_W/H to overlay canvas size
    const sx = cw / CAPTURE_W
    const sy = ch / CAPTURE_H

    // ── Face bounding box ─────────────────────────────────────────────────
    const boxColor = identityMatch ? '#06b6d4' : (isTalking ? '#22c55e' : '#8b5cf6')
    overlayCtx.save()
    overlayCtx.strokeStyle = boxColor
    overlayCtx.lineWidth   = 2
    overlayCtx.shadowColor = boxColor
    overlayCtx.shadowBlur  = 10
    overlayCtx.strokeRect(x * sx, y * sy, w * sx, h * sy)
    overlayCtx.shadowBlur  = 0

    // ── All landmarks (dim) ───────────────────────────────────────────────
    overlayCtx.fillStyle = 'rgba(100,116,139,0.45)'
    lastLandmarks.forEach(pt => {
      overlayCtx.beginPath()
      overlayCtx.arc(pt.x * sx, pt.y * sy, 1.3, 0, Math.PI * 2)
      overlayCtx.fill()
    })

    // ── Lip landmarks (highlighted) ───────────────────────────────────────
    // MediaPipe FaceMesh lip indices: 0-19 in our trimmed array are lip points
    // Full: outer upper=61-67, outer lower=146-152, inner=78-87
    const lipColor = isTalking ? '#22c55e' : '#06b6d4'
    overlayCtx.fillStyle  = lipColor
    overlayCtx.shadowColor = lipColor
    overlayCtx.shadowBlur  = 5
    // Draw first 20 landmarks as lips (backend sends lip-first order)
    lastLandmarks.slice(0, 20).forEach(pt => {
      overlayCtx.beginPath()
      overlayCtx.arc(pt.x * sx, pt.y * sy, 2.8, 0, Math.PI * 2)
      overlayCtx.fill()
    })
    overlayCtx.shadowBlur = 0

    // ── MAR bar under face box ────────────────────────────────────────────
    const barX  = x * sx
    const barY  = (y + h) * sy + 5
    const barW  = w * sx
    const marFill = Math.min(currentMAR / 0.65, 1)
    overlayCtx.fillStyle = 'rgba(0,0,0,0.55)'
    overlayCtx.fillRect(barX, barY, barW, 7)
    overlayCtx.fillStyle = isTalking ? '#22c55e' : '#06b6d4'
    overlayCtx.fillRect(barX, barY, barW * marFill, 7)

    // ── Corner brackets (sci-fi look) ─────────────────────────────────────
    overlayCtx.strokeStyle = boxColor
    overlayCtx.lineWidth = 2
    const blen = Math.min(w * sx, h * sy) * 0.18
    ;[[x * sx, y * sy, 1, 1],
      [(x + w) * sx, y * sy, -1, 1],
      [x * sx, (y + h) * sy, 1, -1],
      [(x + w) * sx, (y + h) * sy, -1, -1],
    ].forEach(([cx, cy, dx, dy]) => {
      overlayCtx.beginPath()
      overlayCtx.moveTo(cx + dx * blen, cy)
      overlayCtx.lineTo(cx, cy)
      overlayCtx.lineTo(cx, cy + dy * blen)
      overlayCtx.stroke()
    })

    // ── Identity label ────────────────────────────────────────────────────
    overlayCtx.font      = '10px monospace'
    overlayCtx.fillStyle = identityMatch ? '#06b6d4' : '#ef4444'
    const idLabel = identityMatch ? '✓ YOU'
      : isEnrolled ? '? UNKNOWN'
      : '⊕ ENROLL FACE'
    overlayCtx.fillText(idLabel, x * sx + 4, y * sy - 5)

    // ── Talking badge ─────────────────────────────────────────────────────
    if (isTalking) {
      overlayCtx.fillStyle = 'rgba(34,197,94,0.85)'
      const bw = 58, bh = 16
      const bx = (x + w) * sx - bw - 2
      const by = y * sy + 2
      overlayCtx.beginPath()
      overlayCtx.roundRect(bx, by, bw, bh, 4)
      overlayCtx.fill()
      overlayCtx.fillStyle = '#fff'
      overlayCtx.font = '9px monospace'
      overlayCtx.fillText('🗣 TALKING', bx + 5, by + 11)
    }

    overlayCtx.restore()
  }

  // ─────────────────────────────────────────────────────────────────────────
  //  Background messaging
  // ─────────────────────────────────────────────────────────────────────────

  function _notifyBackground (data) {
    try {
      chrome.runtime.sendMessage({ type: 'face-vision-update', ...data })
    } catch { /* popup may have closed */ }
  }

  // ─────────────────────────────────────────────────────────────────────────
  //  Lip learning data submission
  // ─────────────────────────────────────────────────────────────────────────

  async function _sendLipLearningData () {
    if (lipHistory.length < 10) return
    try {
      const snap = lipHistory.slice(-50)
      await fetch(`${BACKEND_HTTP}/lip-data`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          samples: snap.map(s => ({ t: s.t, mar: s.mar })),
          mar_avg: snap.reduce((a, b) => a + b.mar, 0) / snap.length,
        }),
        signal: AbortSignal.timeout(2000),
      })
    } catch { /* backend offline — silently skip */ }
  }

  // ─────────────────────────────────────────────────────────────────────────
  //  Profile load
  // ─────────────────────────────────────────────────────────────────────────

  async function _loadProfile () {
    try {
      const resp = await fetch(`${BACKEND_HTTP}/profile`,
        { signal: AbortSignal.timeout(2000) })
      if (resp.ok) {
        const j = await resp.json()
        isEnrolled = !!j.enrolled
        console.log(`${TAG} Profile loaded — enrolled: ${isEnrolled}`)
      }
    } catch { /* backend offline at startup — ok */ }
  }

})()
