/*
 * JARVIS Face Vision — Camera Tab controller
 *
 * Runs inside camera.html (a full extension page opened in a browser tab).
 * A full tab reliably shows the camera permission prompt — unlike the MV3
 * popup, which is a transient context that often cannot prompt. Once the user
 * allows the camera here, the extension origin has permission and the popup can
 * display live status (relayed through background.js).
 *
 * Reuses window.JarvisFaceVision from face-vision.js for all camera + WS logic.
 */

;(() => {
  'use strict'

  const $ = (id) => document.getElementById(id)
  const video   = $('video')
  const overlay = $('overlay')
  const noCam   = $('noCam')
  const talking = $('talkingBadge')
  const marBar  = $('marBar')
  const marVal  = $('marVal')
  const speak   = $('speakVal')
  const idVal   = $('idVal')
  const backend = $('backendVal')
  const wsDot   = $('wsDot')
  const errMsg  = $('errMsg')
  const startBtn  = $('startBtn')
  const stopBtn   = $('stopBtn')
  const enrollBtn = $('enrollBtn')
  const clearBtn  = $('clearBtn')

  const FV = window.JarvisFaceVision
  if (!FV) { errMsg.textContent = 'face-vision.js failed to load.'; return }

  let running = false

  // ── Wire events from the face vision engine ──────────────────────────────
  FV.on('camera', ({ active }) => {
    running = active
    noCam.style.display   = active ? 'none' : 'flex'
    startBtn.disabled     = active
    stopBtn.disabled      = !active
    enrollBtn.disabled    = !active
    if (!active) talking.classList.remove('on')
  })

  FV.on('ws', ({ connected }) => {
    wsDot.classList.toggle('on', connected)
    backend.innerHTML = `<span class="ws-dot ${connected ? 'on' : ''}"></span>${connected ? 'connected' : 'offline'}`
    backend.className = 'v ' + (connected ? 'ok' : 'warn')
  })

  FV.on('frame', ({ facePresent, mar, isTalking, identityMatch }) => {
    const pct = Math.min(mar / 0.65, 1) * 100
    marBar.style.width      = pct + '%'
    marBar.style.background = isTalking ? '#22c55e' : '#06b6d4'
    marVal.textContent      = mar.toFixed(3)
    marVal.className        = 'v ' + (isTalking ? 'ok' : 'cyan')

    speak.textContent = isTalking ? 'TALKING' : 'silent'
    speak.className   = 'v ' + (isTalking ? 'ok' : 'dim')
    talking.classList.toggle('on', isTalking)

    const enrolled = FV.isEnrolled()
    if (!facePresent)      { idVal.textContent = 'no face';      idVal.className = 'v dim' }
    else if (!enrolled)    { idVal.textContent = 'not enrolled'; idVal.className = 'v warn' }
    else                   { idVal.textContent = identityMatch ? '✓ YOU' : '? unknown'
                             idVal.className   = 'v ' + (identityMatch ? 'ok' : 'warn') }
  })

  FV.on('enrolling', () => { enrollBtn.textContent = '⟳ Enrolling…'; enrollBtn.disabled = true })
  FV.on('enrolled', ({ success, cleared }) => {
    enrollBtn.disabled = false
    if (success) {
      enrollBtn.textContent = '✓ Enrolled!'
      setTimeout(() => { enrollBtn.textContent = '⊕ Re-Enroll My Face' }, 2500)
    } else if (cleared) {
      enrollBtn.textContent = '⊕ Enroll My Face'
      idVal.textContent = 'cleared'; idVal.className = 'v dim'
    } else {
      enrollBtn.textContent = '⊕ Enroll My Face'
    }
  })

  FV.on('error', ({ msg }) => {
    errMsg.textContent = msg || 'Camera error'
    enrollBtn.textContent = '⊕ Enroll My Face'
    enrollBtn.disabled = !running
  })

  // ── Buttons ──────────────────────────────────────────────────────────────
  startBtn.addEventListener('click', async () => {
    errMsg.textContent = ''
    await FV.init(video, overlay)
    await FV.startCamera()
  })
  stopBtn.addEventListener('click', () => FV.stopCamera())
  enrollBtn.addEventListener('click', () => { errMsg.textContent = ''; FV.enrollFace() })
  clearBtn.addEventListener('click', () => {
    if (confirm('Clear enrolled face?')) FV.clearEnrollment()
  })

  // Auto-init the engine so enrollment state loads from the backend
  FV.init(video, overlay).catch(() => {})

  // Stop the camera cleanly when the tab is closed/hidden
  window.addEventListener('beforeunload', () => { try { FV.stopCamera() } catch {} })
})()
