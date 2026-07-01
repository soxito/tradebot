/*
 * JARVIS Voice Assistant — Content Script  v2.4.0
 *
 * Architecture: Extension owns the ONE microphone. Page executes commands.
 * NEW in v2.4 (extension v3.2.0):
 *  • Single-mic lock: extension is the ONLY listener when active; page mic/
 *    speaker are silenced via jarvis-robot-lock + robot-mode postMessage.
 *  • Wake relay: 'wake' events re-dispatched as 'jarvis-wake' CustomEvent so
 *    the walking robot avatar re-emerges from its hole on the page.
 *  • Voice engine panel is EXTENSION-POPUP ONLY — not injected into the page.
 *  • Walking cyborg robot animation on the page (handled by JarvisRobotAvatar).
 * From v2.3:
 *  • Self-voice guard: JARVIS's own TTS is never transcribed nor mirrored.
 *  • Voice-identity gate: requireVoiceMatch gates ALL transcription.
 *  • Conversation continuity: follow-ups need no wake word in the window.
 *  • Firefox support: Deepgram fallback loop.
 */

;(() => {
  'use strict'

  const TAG = '[JARVIS-EXT]'

  // ── Crash-safe API shim ────────────────────────────────────────────────────
  // Normalise chrome/browser namespace so this works in both Chrome and Firefox.
  const api = (typeof browser !== 'undefined' && browser.runtime) ? browser : chrome

  // Installed extension version (from manifest). Reported to the page so it can
  // detect when a NEWER version is available on the backend and prompt to update.
  const EXT_VERSION = (() => {
    try { return (api.runtime.getManifest && api.runtime.getManifest().version) || '' } catch { return '' }
  })()

  const SR = window.SpeechRecognition || window.webkitSpeechRecognition

  // Firefox has no Web Speech API. When MediaRecorder + getUserMedia exist we
  // can still provide voice via the Deepgram fallback loop, so "voice support"
  // means: native SpeechRecognition OR a usable mic+recorder for Deepgram.
  const FX_CAPABLE = (typeof MediaRecorder !== 'undefined') &&
    !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia)
  const VOICE_SUPPORTED = !!SR || FX_CAPABLE

  // ── Settings ───────────────────────────────────────────────────────────────
  // conversationWindowMs: after a successful command, JARVIS stays "in
  // conversation" for this long. While the window is open the wake word is NOT
  // required — the user can fire follow-up commands directly. Each turn (and
  // each JARVIS reply) refreshes the window so a back-and-forth never forces you
  // to say "Jarvis" again. requireVoiceMatch gates ALL transcription on the
  // user's stored voice profile so other people in the room are ignored.
  let settings = {
    enabled: true, wakeWord: 'jarvis', requireGreeting: false, lang: 'en-US',
    notifications: true, conversationWindowMs: 30000, requireVoiceMatch: true,
  }

  let recognition = null
  let listening    = false
  let restartTimer = null
  let manuallyStopped = false
  let restartDelay = 500  // exponential back-off starting value (ms)
  let pageSpeaking = false
  let pageVoiceMatch = true  // updated by page's voiceMatch loop; gates dispatch when false
  let learnedVocab = {}     // cached from chrome.storage, boosts pickBest() scoring

  // ── Face Vision sync state ──────────────────────────────────────────────────
  // Relayed from the popup (face-vision.js → background → here). Only "fresh"
  // while the popup camera is active. When fresh, the user's matched face acts
  // as a positive reinforcement for the voice gate (speech + face in sync) and
  // an unknown face tightens it. When stale (popup closed) speech is voice-only.
  let faceState = { present: false, talking: false, match: false, mar: 0, ts: 0 }
  const FACE_FRESH_MS = 2500
  function faceFresh() { return faceState.ts > 0 && (Date.now() - faceState.ts) < FACE_FRESH_MS }

  // Combined identity gate — single source of truth for "should we transcribe".
  // Blends the audio voice-ID (pageVoiceMatch) with the visual face signal.
  function passesIdentityGate() {
    const voiceOK = !settings.requireVoiceMatch || pageVoiceMatch
    if (!faceFresh()) return voiceOK                     // face vision off → voice-only (unchanged)
    if (faceState.present && faceState.match) return true // your matched face → trust it
    if (settings.requireVoiceMatch && faceState.present && !faceState.match) return false // stranger → block
    return voiceOK
  }

  // ── Conversation continuity ────────────────────────────────────────────────
  // While inConversation is true, follow-up speech is treated as a command
  // without needing the wake word again. The window auto-closes after
  // settings.conversationWindowMs of silence (refreshed on every turn/reply).
  let inConversation   = false
  let conversationEndTimer = null
  // Shared command-capture state (module scope so the Web Speech path AND the
  // Firefox Deepgram fallback path drive the SAME wake/conversation logic).
  let awaitingCommand = false
  let commandBuffer   = ''
  let commandTimer    = null
  let dispatchTimer   = null
  // ── Voice frequency analyser (shared for panel + popup relay) ─────────────
  let freqCtx = null
  let freqAnalyser = null
  let freqBuf = null
  let freqRafId = null
  let freqBands = Array(16).fill(0)
  let freqEnergy = 0
  // ── In-page binary engine panel ────────────────────────────────────────────
  let enginePanelEl = null
  let engineCanvas = null
  let enginePanelVisible = true
  let freqRelayTimer = null  // throttle background relay to ~20fps

  // ── Cost-aware Deepgram fallback (extension) ──────────────────────────────
  // Reuse the single freq-analyser mic stream (no second mic). A MediaRecorder
  // ring buffer keeps the last few seconds of audio so a *missed* command can be
  // re-checked once via cheap Deepgram pre-recorded STT (relayed by background.js).
  const DG_BUFFER_MS = 8000        // ~8s rolling window
  const DG_MIN_CLIP_BYTES = 1200   // skip empty/too-short clips (no spend)
  let dgStream = null              // mic stream shared with the freq analyser
  let dgRecorder = null
  let dgChunks = []                // [{ t, blob }] rolling buffer
  let dgInFlight = false           // one escalation at a time
  let dgPaused = false             // true once the backend reports the cap is reached
  // ── Firefox listen loop (Web Speech API unavailable) ──────────────────────
  // Energy-gated VAD drives a per-utterance MediaRecorder; each finished
  // utterance is sent to Deepgram pre-recorded STT and fed through the SAME
  // handleTranscript() pipeline so wake/conversation/voice-ID behave identically.
  let fxActive      = false
  let fxLoopTimer   = null
  let fxSpeaking    = false
  let fxLastVoiceTs = 0
  let fxRec         = null
  let fxBlobs       = []
  let fxInFlight    = false

  // ── Wake-phrase helpers ────────────────────────────────────────────────────
  // Small Levenshtein distance so minor mis-hearings of the name still wake
  // JARVIS — kept in sync with the in-page phoneticWakeMatch() helper.
  function lev(a, b) {
    const m = a.length, n = b.length
    if (!m) return n
    if (!n) return m
    let prev = Array.from({ length: n + 1 }, (_, i) => i)
    let curr = new Array(n + 1).fill(0)
    for (let i = 1; i <= m; i++) {
      curr[0] = i
      for (let j = 1; j <= n; j++) {
        const cost = a[i - 1] === b[j - 1] ? 0 : 1
        curr[j] = Math.min(curr[j - 1] + 1, prev[j] + 1, prev[j - 1] + cost)
      }
      const tmp = prev; prev = curr; curr = tmp
    }
    return prev[n]
  }
  // The short tokens "j" and "sir" are intentionally NOT accepted, to limit
  // false matches — kept in sync with the in-page nameLike() helper. Wake names:
  // jarvis (fuzzy), paul, and sox (with common mis-hearings).
  function nameLike(w) {
    if (!w) return false
    if (w === 'jarvis' || w === 'paul' || w === 'sox') return true
    if (lev(w, 'jarvis') <= 2) return true
    if (w.length >= 3 && lev(w, 'paul') <= 1) return true
    // Common speech-to-text mis-hearings of the short name "sox".
    if (w === 'socks' || w === 'sax' || w === 'sacks' || w === 'sachs') return true
    return false
  }
  const GREET = '(?:hi|hey|hello|ok|okay|yo|wake up|listen|attention|yes)'
  // Token form of the greeting words, used by the (punctuation-tolerant) matcher.
  const GREET_SET = new Set(['hi', 'hey', 'hello', 'ok', 'okay', 'yo', 'yes', 'wake', 'up', 'listen', 'attention'])
  // Split a transcript into lowercase word tokens, dropping punctuation so that
  // "Hey, Jarvis!" tokenises the same as "hey jarvis".
  function wakeTokens(s) {
    return (s || '').toLowerCase().replace(/[^a-z\s]/g, ' ').split(/\s+/).filter(Boolean)
  }
  // Wake on the bare name by default ("Jarvis, ..."). When requireGreeting is
  // on, a greeting word must precede the name (strict mode for noisy rooms).
  function hasWake(text) {
    const w = wakeTokens(text)
    if (!w.length) return false
    // Greeting immediately followed by a name-like token (works in both modes).
    for (let i = 0; i < w.length - 1; i++) {
      if (GREET_SET.has(w[i]) && nameLike(w[i + 1])) return true
    }
    if (settings.requireGreeting) return false
    // Bare-name branch: the utterance must START with a name-like token so a
    // stray name mid-sentence in ambient audio does not wake the assistant.
    return nameLike(w[0])
  }
  function stripWake(text) {
    let s = (text || '')
    // Remove a greeting + name prefix anywhere it appears (tolerating punctuation).
    s = s.replace(new RegExp(`\\b${GREET}\\b[\\s,.!?:-]+(jarvis|paul|sox)\\b[,.!?\\s]*`, 'gi'), '')
    // Fuzzy: remove a leading greeting + name-like token.
    let m = s.match(new RegExp(`^\\s*${GREET}\\b[\\s,.!?:-]+([a-z]+)\\b[,.!?\\s]*`, 'i'))
    if (m && nameLike(m[1].toLowerCase())) {
      s = s.slice(m[0].length)
    } else if (!settings.requireGreeting) {
      // Otherwise strip a leading bare name token ("Jarvis, help" -> "help").
      m = s.match(/^\s*([a-z]+)\b[,.!?\s]*/i)
      if (m && nameLike(m[1].toLowerCase())) s = s.slice(m[0].length)
    }
    return s.trim()
  }

  // ── Messaging helpers ──────────────────────────────────────────────────────
  function toPage(payload) {
    try { window.postMessage({ __jarvisExt: true, ...payload }, window.location.origin) } catch { /* noop */ }
  }
  function markPresence() {
    try {
      document.documentElement.setAttribute('data-jarvis-ext', '1')
      document.documentElement.setAttribute('data-jarvis-ext-voice', VOICE_SUPPORTED ? '1' : '0')
      // Expose the installed version so the page can detect available updates.
      if (EXT_VERSION) document.documentElement.setAttribute('data-jarvis-ext-version', EXT_VERSION)
    } catch { /* noop */ }
  }
  function status(extra = {}) {
    toPage({
      type: 'status',
      listening,
      enabled: !!settings.enabled,
      speechSupported: VOICE_SUPPORTED,
      voiceReady: VOICE_SUPPORTED && !!settings.enabled,
      ...extra,
    })
  }
  function connected() {
    markPresence()
    toPage({
      type: 'connected',
      version: EXT_VERSION,
      enabled: !!settings.enabled,
      speechSupported: VOICE_SUPPORTED,
      voiceReady: VOICE_SUPPORTED && !!settings.enabled,
      // Report the ACTUAL listening state so the page only cedes the mic when
      // recognition is truly running — never while the extension is merely
      // connected/enabled but stuck before onstart ("Starting…").
      listening: !!listening,
    })
  }
  function notify(title, body) {
    if (!settings.notifications) return
    try { api.runtime.sendMessage({ type: 'notify', title, body }).catch?.(() => {}) } catch { /* noop */ }
  }
  function badge(text, color) {
    try { api.runtime.sendMessage({ type: 'badge', text, color }).catch?.(() => {}) } catch { /* noop */ }
  }
  function mirrorTranscript(text, isFinal) {
    try { api.runtime.sendMessage({ type: 'transcript', text: (text || '').trim(), final: isFinal }).catch?.(() => {}) } catch { /* noop */ }
  }

  // ── Speak via Web Speech Synthesis ───────────────────────────────────────
  let speechQueue = []
  let speechBusy  = false
  // Universal-voice routing: track whether the page (PaulChat) acknowledged a
  // jarvis-speak request so we only fall back to local TTS when no chat is present.
  let pageSpeakAckAt = 0
  let pageSpeakFallbackTimer = null

  // Immediately mute the mic from both Web Speech and Deepgram before speaking.
  // This is called synchronously so no recognition event fires during TTS.
  function silenceMicNow() {
    // Stop Web Speech recognizer mid-result (prevents self-transcription)
    try { if (recognition && listening) { recognition.onresult = null; recognition.stop() } } catch { /* noop */ }
    listening = false
    // Abort any active Deepgram clip recording (Firefox path)
    fxAbort()
    fxSpeaking = false
  }

  function speakNow(text) {
    if (!text || !window.speechSynthesis) return
    const utt = new SpeechSynthesisUtterance(String(text))
    utt.rate   = 1.05
    utt.pitch  = 1.0
    utt.volume = 1.0
    // Prefer a male en-US voice if available (Jarvis-like)
    try {
      const voices = window.speechSynthesis.getVoices()
      const pref   = voices.find(v =>
        v.lang.startsWith('en') && /male|david|mark/i.test(v.name)
      ) || voices.find(v => v.lang.startsWith('en'))
      if (pref) utt.voice = pref
    } catch { /* noop */ }
    // Mute mic FIRST to prevent self-transcription of our own TTS.
    silenceMicNow()
    // Set pageSpeaking=true BEFORE speaking so onresult ignores our own TTS.
    // allowBargeIn:true keeps recognition alive so the user can cut in by voice.
    pageSpeaking = true
    toPage({ type: 'speak-status', speaking: true, allowBargeIn: true })
    utt.onend  = () => {
      speechBusy = false
      drainSpeechQueue()
      // If nothing else is queued, clear pageSpeaking after a longer echo-tail delay
      // (900ms to cover speaker echo + recognition lag before re-opening the mic).
      if (!speechBusy) {
        clearTimeout(restartTimer)
        restartTimer = setTimeout(() => {
          pageSpeaking = false
          toPage({ type: 'speak-status', speaking: false, allowBargeIn: false })
        }, 900)
      }
    }
    utt.onerror = () => {
      speechBusy = false
      drainSpeechQueue()
      if (!speechBusy) {
        clearTimeout(restartTimer)
        restartTimer = setTimeout(() => {
          pageSpeaking = false
          toPage({ type: 'speak-status', speaking: false, allowBargeIn: false })
        }, 900)
      }
    }
    speechBusy = true
    window.speechSynthesis.speak(utt)
  }

  function drainSpeechQueue() {
    if (speechBusy || speechQueue.length === 0) return
    pageSpeaking = true  // stay muted between consecutive TTS items
    silenceMicNow()      // abort any partial recognition between queue items
    const next = speechQueue.shift()
    speakNow(next)
  }

  function queueSpeak(text) {
    if (!text) return
    // ── Universal voice ──────────────────────────────────────────────────────
    // Prefer routing speech through the in-page JARVIS (PaulChat) so EVERY
    // utterance uses the voice the user selected in the chat (OpenAI aiVoice or
    // their chosen system voice). We post a `jarvis-speak` request; if the page
    // confirms it is speaking (via speak-status) we let it own the audio. Only
    // when the page does not respond (no chat on this tab) do we fall back to the
    // extension's own Web Speech synthesiser.
    try {
      pageSpeakAckAt = 0
      // PaulChat's jarvis-speak listener expects the __jarvisPage envelope, so we
      // post that directly (not via toPage, which uses __jarvisExt).
      window.postMessage({ __jarvisPage: true, type: 'jarvis-speak', text: String(text) }, window.location.origin)
      clearTimeout(pageSpeakFallbackTimer)
      pageSpeakFallbackTimer = setTimeout(() => {
        // If the page started speaking within the window, it owns this utterance.
        if (pageSpeaking || (Date.now() - pageSpeakAckAt) < 1200) return
        // No page response → speak locally so the user still hears JARVIS.
        speechQueue.push(text)
        drainSpeechQueue()
      }, 450)
      return
    } catch { /* fall through to local speech */ }
    speechQueue.push(text)
    drainSpeechQueue()
  }

  // ── Crypto position command patterns ─────────────────────────────────────
  // Returns true if the command was intercepted and handed to the backend.
  // The command is ALSO forwarded to the page (so it can update UI etc.).
  const CRYPTO_PATTERNS = [
    // ── ANALYSIS / MONITOR — must be first to block AI hallucination ───────
    // "monitor SOL", "analyze BTCUSDT", "find buy entries", "snipe ETHUSDT"
    /(?:monitor|watch|analyze|analyse|scan|sniper?|check)\s+\w{2,12}/i,
    /find\s+(?:(?:more|a|some)\s+)?(?:buy|sell|long|short)\s+entr(?:y|ies)/i,
    // ── ORDER EXECUTION — intercept before AI can hallucinate ────────────
    // "execute VELVETUSDT short 2 lot at 1.7000; set SL 1.7500; TP1 1.5500"
    /(?:execute|open|place|trade|enter)\s+\w{3,15}\s+(?:long|short|buy|sell)/i,
    // "short VELVETUSDT 2 lots at 1.7000"
    /^(?:long|short|buy|sell)\s+\w{3,15}\s+[\d.]+/i,
    // "go long/short BTCUSDT"
    /go\s+(?:long|short)(?:\s+on)?\s+\w{3,15}/i,
    // ── POSITION MANAGEMENT ────────────────────────────────────────────────
    // take / set TP by %
    /(?:take|set)\s+(?:a\s+)?\d+(?:\.\d+)?%\s*(?:profit|return|roi|tp|take[\s-]profit)/i,
    // set TP at price
    /(?:set\s+)?(?:tp|take[\s-]profit)\s+at\s+[\d.]+/i,
    // set SL by %
    /set\s+(?:a\s+)?(?:stop[\s-]loss|sl)\s+at\s+\d/i,
    // close position
    /close(?:\s+my)?\s+[A-Z]{3,15}(?:\s+position)?/i,
    // show positions
    /(?:show|list|what(?:\s+are)?|get)\s+(?:my\s+)?(?:open\s+)?positions?/i,
    // how is X doing
    /how\s+is\s+[A-Z]{3,15}(?:\s+doing)?/i,
  ]

  function isCryptoCommand(cmd) {
    return CRYPTO_PATTERNS.some((rx) => rx.test(cmd))
  }

  function interceptCryptoCommand(command) {
    if (!isCryptoCommand(command)) return false
    // Give immediate verbal acknowledgment for trade execution commands so the
    // user gets instant feedback while the backend API call is in flight.
    const isExecute = /(?:execute|open|place|trade|enter)\s+\w|^(?:short|long)\s+\w|go\s+(?:long|short)/i.test(command)
    const isAnalyze = /(?:monitor|watch|analyze|analyse|scan|sniper?|check)\s+\w|find\s+(?:buy|sell|long|short)/i.test(command)
    if (isExecute) queueSpeak('On it, Sir.')
    else if (isAnalyze) queueSpeak('Analysing now, Sir.')
    // Relay to background → backend for execution.
    // Result is spoken via TTS and forwarded to the page as 'jarvis-result'
    // (not as 'command', which would re-trigger the page's own handler).
    try {
      api.runtime.sendMessage({ type: 'jarvis-command', command }, (result) => {
        if (api.runtime.lastError) return
        const speech = (result && (result.speech || result.detail)) || ''
        if (speech) queueSpeak(speech)
        // Send a display-only event to the page — no re-processing.
        toPage({
          type: 'jarvis-result',
          ok:     result ? result.ok : false,
          action: result ? result.action : 'error',
          detail: result ? result.detail : '',
          speech,
          command,
        })
      })
    } catch { /* noop */ }
    return true   // intercepted — dispatchCommand will NOT also call toPage
  }

  // ── Core command dispatch ──────────────────────────────────────────────────
  function dispatchCommand(command) {
    if (!command || command.trim().length < 2) return
    console.log(TAG, 'command →', command)
    // A successful command opens/refreshes the conversation window so the user
    // can keep talking (follow-ups) without repeating the wake word.
    enterConversation()
    // Crypto position commands are handled entirely by the backend via
    // background.js. We do NOT also send them to the page, which would
    // trigger the page's own handler and cause DNS/network errors.
    let wasCrypto = false
    try { wasCrypto = interceptCryptoCommand(command) } catch { wasCrypto = false }
    if (!wasCrypto) {
      // Non-crypto command: let the page's JARVIS handler process it normally.
      try { toPage({ type: 'command', transcript: command.trim() }) } catch { /* noop */ }
    }
    try { notify('JARVIS heard you', command) } catch { /* noop */ }
  }

  // ── Voice frequency analyser (powers in-page panel + popup relay) ─────────
  // Opens a single AudioContext that samples mic frequency at ~20fps.
  // Bands array (16 values 0-1) drives the binary-engine canvas and is relayed
  // to the background service worker for the popup mini-canvas.
  function initFreqAnalyser() {
    if (freqCtx) return  // already running
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) return
    navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } })
      .then((stream) => {
        try {
          const ctx      = new (window.AudioContext || window.webkitAudioContext)()
          const analyser = ctx.createAnalyser()
          analyser.fftSize = 256
          analyser.smoothingTimeConstant = 0.75
          ctx.createMediaStreamSource(stream).connect(analyser)
          freqCtx      = ctx
          freqAnalyser = analyser
          freqBuf      = new Uint8Array(analyser.frequencyBinCount)

          // Attach the Deepgram fallback ring buffer to this same stream.
          startDgBuffer(stream)

          const BANDS = 16
          const tick = () => {
            freqRafId = requestAnimationFrame(tick)
            if (!freqAnalyser) return
            freqAnalyser.getByteFrequencyData(freqBuf)
            const binSize = Math.floor(freqBuf.length / BANDS)
            const raw = Array.from({ length: BANDS }, (_, b) => {
              let s = 0
              for (let j = b * binSize; j < Math.min((b + 1) * binSize, freqBuf.length); j++) s += freqBuf[j]
              return s / binSize
            })
            const mx = Math.max(...raw, 1)
            freqBands  = raw.map(v => v / mx)
            freqEnergy = raw.reduce((a, v) => a + v, 0) / raw.length / 255

            // v3.1: the in-page binary panel is removed — the 3D robot is the
            // visual now. We still relay freq data to the popup mini-canvas.

            // Relay to background for popup mini-canvas (~20fps throttled)
            if (!freqRelayTimer) {
              freqRelayTimer = setTimeout(() => {
                freqRelayTimer = null
                try {
                  api.runtime.sendMessage({
                    type: 'voice-freq',
                    bands: freqBands,
                    energy: freqEnergy,
                    isUserVoice: pageVoiceMatch,
                    isSpeaking: pageSpeaking,
                  }).catch?.(() => {})
                } catch { /* noop */ }
              }, 50)  // 20fps cap
            }
          }
          tick()
        } catch (e) { console.warn(TAG, 'freq analyser init failed', e) }
      })
      .catch(() => { /* mic denied — visualizer stays blank */ })
  }

  function stopFreqAnalyser() {
    if (freqRafId)  { cancelAnimationFrame(freqRafId); freqRafId = null }
    if (freqRelayTimer) { clearTimeout(freqRelayTimer); freqRelayTimer = null }
    try { if (freqCtx) freqCtx.close() } catch { /* noop */ }
    freqCtx = null; freqAnalyser = null; freqBuf = null
    stopDgBuffer()
  }

  // ── Deepgram fallback: ring buffer + miss escalation ──────────────────────
  // Record the shared mic stream in 1s slices, keeping only the most recent
  // ~DG_BUFFER_MS. On a miss we splice those slices into one short clip.
  function startDgBuffer(stream) {
    if (dgRecorder || typeof MediaRecorder === 'undefined') return
    try {
      dgStream = stream
      const mime = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus']
        .find(m => { try { return MediaRecorder.isTypeSupported(m) } catch { return false } })
      const rec = mime ? new MediaRecorder(stream, { mimeType: mime }) : new MediaRecorder(stream)
      rec.ondataavailable = (ev) => {
        if (!ev.data || ev.data.size === 0) return
        const now = Date.now()
        dgChunks.push({ t: now, blob: ev.data })
        const cutoff = now - DG_BUFFER_MS
        while (dgChunks.length && dgChunks[0].t < cutoff) dgChunks.shift()
      }
      dgRecorder = rec
      rec.start(1000)
    } catch (e) {
      console.warn(TAG, 'dg buffer init failed (non-fatal)', e)
      dgRecorder = null
    }
  }

  function stopDgBuffer() {
    try { dgRecorder && dgRecorder.stop() } catch { /* noop */ }
    dgRecorder = null
    dgStream = null
    dgChunks = []
  }

  // Convert a Blob to a base64 string (sendMessage cannot reliably clone Blobs).
  function blobToBase64(blob) {
    return new Promise((resolve, reject) => {
      const fr = new FileReader()
      fr.onloadend = () => {
        const s = String(fr.result || '')
        const comma = s.indexOf(',')
        resolve(comma >= 0 ? s.slice(comma + 1) : s)
      }
      fr.onerror = reject
      fr.readAsDataURL(blob)
    })
  }

  // Escalate one missed utterance to Deepgram pre-recorded STT via background.js.
  // Silent on cap/error: JARVIS just stays on the free Web Speech engine.
  async function escalateDeepgram(reason) {
    if (dgInFlight || dgPaused || !dgRecorder) return
    // Speaker gate: only ever send the calibrated user's OWN voice to Deepgram.
    // pageVoiceMatch is fed by the page's live voice-ID loop (voice-match-update);
    // when voice-ID is off the page never reports a non-match so this stays true
    // and all misses can still escalate, exactly like the free engine.
    if (!pageVoiceMatch) return
    if (!dgChunks.length) return
    const type = (dgRecorder && dgRecorder.mimeType) || 'audio/webm'
    const clip = new Blob(dgChunks.map(c => c.blob), { type })
    if (clip.size < DG_MIN_CLIP_BYTES) return  // too short → no spend
    dgInFlight = true
    try {
      const b64 = await blobToBase64(clip)
      api.runtime.sendMessage(
        { type: 'deepgram-stt', audio: b64, mime: type, reason: reason || 'miss' },
        (res) => {
          dgInFlight = false
          if (api.runtime.lastError) return  // background unreachable → silent
          if (!res) return
          if (res.used_deepgram && res.text && res.text.trim()) {
            dispatchCommand(res.text.trim())
          } else if (res.used_deepgram === false && res.reason === 'budget_capped') {
            dgPaused = true  // cap reached — stop escalating until reload
          }
        }
      )
    } catch {
      dgInFlight = false
    }
  }

  // ── Firefox listen loop (Deepgram fallback) ───────────────────────────────
  function startFirefoxListen() {
    if (fxActive) return
    fxActive = true
    initFreqAnalyser()      // ensures the mic stream + analyser are running
    listening = true
    fxLoopTimer = setInterval(fxTick, 200)
  }
  function stopFirefoxListen() {
    fxActive = false
    if (fxLoopTimer) { clearInterval(fxLoopTimer); fxLoopTimer = null }
    fxAbort()
    fxSpeaking = false
    listening = false
  }
  // Energy-gated voice-activity detector. Opens an utterance when the user
  // starts talking and closes it after a short silence; never records JARVIS.
  function fxTick() {
    if (!fxActive || !settings.enabled || !dgStream) return
    const now = Date.now()
    const ENERGY_ON = 0.045   // mic energy threshold for "speech present"
    const SILENCE_MS = 700    // quiet gap that ends an utterance
    if (pageSpeaking) { if (fxSpeaking) fxAbort(); return }  // ignore own TTS
    const loud = freqEnergy > ENERGY_ON
    if (loud) {
      fxLastVoiceTs = now
      if (!fxSpeaking) { fxSpeaking = true; fxBegin() }
      return
    }
    if (fxSpeaking && (now - fxLastVoiceTs) > SILENCE_MS) {
      fxSpeaking = false
      fxEnd()  // stop recorder → onstop sends the clip to Deepgram
    }
  }
  function fxBegin() {
    if (fxRec || typeof MediaRecorder === 'undefined' || !dgStream) return
    try {
      const mime = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus']
        .find(m => { try { return MediaRecorder.isTypeSupported(m) } catch { return false } })
      fxRec = mime ? new MediaRecorder(dgStream, { mimeType: mime }) : new MediaRecorder(dgStream)
      fxBlobs = []
      fxRec.ondataavailable = (ev) => { if (ev.data && ev.data.size) fxBlobs.push(ev.data) }
      fxRec.onstop = () => {
        const type  = (fxRec && fxRec.mimeType) || 'audio/webm'
        const blobs = fxBlobs
        fxRec = null; fxBlobs = []
        const clip = new Blob(blobs, { type })
        if (clip.size >= DG_MIN_CLIP_BYTES) fxSend(clip, type)
      }
      fxRec.start()
    } catch { fxRec = null }
  }
  function fxEnd() {
    try { if (fxRec && fxRec.state !== 'inactive') fxRec.stop() } catch { fxRec = null }
  }
  function fxAbort() {  // discard the in-progress utterance (e.g. JARVIS spoke)
    try { if (fxRec) { fxRec.onstop = null; if (fxRec.state !== 'inactive') fxRec.stop() } } catch { /* noop */ }
    fxRec = null; fxBlobs = []
  }
  async function fxSend(clip, type) {
    if (fxInFlight || dgPaused) return
    const voiceOK = passesIdentityGate()
    if (!voiceOK) return  // not the calibrated user → no spend, no transcript
    fxInFlight = true
    try {
      const b64 = await blobToBase64(clip)
      api.runtime.sendMessage(
        { type: 'deepgram-stt', audio: b64, mime: type, reason: 'firefox_listen' },
        (res) => {
          fxInFlight = false
          if (api.runtime.lastError) return
          if (!res) return
          if (res.used_deepgram && res.text && res.text.trim()) {
            const txt = cleanFiller(res.text.trim()) || res.text.trim()
            if (!pageSpeaking && voiceOK) mirrorTranscript(txt, true)
            handleTranscript(txt, true)
          } else if (res.used_deepgram === false && res.reason === 'budget_capped') {
            dgPaused = true
          }
        }
      )
    } catch { fxInFlight = false }
  }

  // ── In-page Binary Engine panel ────────────────────────────────────────────
  // Injects a small fixed-position overlay next to the PaulChat widget showing
  // the animated binary-engine frequency visualization.
  function injectVoiceEnginePanel() {
    if (document.getElementById('jarvis-voice-engine')) return  // already present

    const style = document.createElement('style')
    style.textContent = `
      #jarvis-voice-engine {
        position: fixed; bottom: 310px; right: 14px; z-index: 2147483646;
        width: 158px; background: rgba(2,6,23,0.92); border: 1px solid #1e293b;
        border-radius: 12px; padding: 8px 8px 6px; font-family: monospace;
        box-shadow: 0 4px 24px rgba(6,182,212,0.15); transition: opacity .3s;
        user-select: none;
      }
      #jarvis-voice-engine.je-hidden { opacity: 0; pointer-events: none; }
      #je-header {
        display: flex; align-items: center; justify-content: space-between;
        margin-bottom: 6px;
      }
      #je-title { font-size: 9px; letter-spacing: .1em; color: #64748b; text-transform: uppercase; }
      #je-match-ring {
        width: 10px; height: 10px; border-radius: 50%;
        background: #334155; transition: background .3s, box-shadow .3s;
      }
      #je-match-ring.match { background: #22c55e; box-shadow: 0 0 6px #22c55e; }
      #je-match-ring.no-match { background: #ef4444; box-shadow: 0 0 6px #ef4444; }
      #je-match-ring.speaking { background: #f59e0b; box-shadow: 0 0 6px #f59e0b; }
      #je-canvas { display: block; border-radius: 4px; }
      #je-stats { font-size: 8px; color: #475569; margin-top: 4px;
        display: flex; justify-content: space-between; }
      #je-collapse {
        position: absolute; top: -10px; right: 2px;
        background: #1e293b; border: 1px solid #334155; border-radius: 50%;
        width: 18px; height: 18px; cursor: pointer; font-size: 10px;
        display: flex; align-items: center; justify-content: center;
        color: #64748b; line-height: 1;
      }
      #je-collapse:hover { color: #94a3b8; }
      #jarvis-ve-reopen {
        position: fixed; bottom: 310px; right: 14px; z-index: 2147483646;
        width: 28px; height: 28px; background: rgba(6,182,212,0.15);
        border: 1px solid #06b6d4; border-radius: 50%; cursor: pointer;
        display: none; align-items: center; justify-content: center;
        font-size: 14px; color: #06b6d4;
      }
    `
    document.head.appendChild(style)

    const panel = document.createElement('div')
    panel.id = 'jarvis-voice-engine'
    panel.innerHTML = `
      <button id="je-collapse" title="Collapse">⊗</button>
      <div id="je-header">
        <span id="je-title">VOICE ENGINE</span>
        <div id="je-match-ring"></div>
      </div>
      <canvas id="je-canvas" width="142" height="60"></canvas>
      <div id="je-stats">
        <span id="je-status">IDLE</span>
        <span id="je-words">0 words</span>
      </div>
    `
    document.body.appendChild(panel)
    enginePanelEl = panel
    engineCanvas  = panel.querySelector('#je-canvas')

    // Reopen button (shown when panel collapsed)
    const reopen = document.createElement('div')
    reopen.id = 'jarvis-ve-reopen'
    reopen.title = 'Show Voice Engine'
    reopen.textContent = '◈'
    reopen.style.display = 'none'
    document.body.appendChild(reopen)

    panel.querySelector('#je-collapse').addEventListener('click', () => {
      enginePanelVisible = false
      panel.classList.add('je-hidden')
      reopen.style.display = 'flex'
      try { api.runtime.sendMessage({ type: 'engine-panel-state', visible: false }).catch?.(() => {}) } catch { /* noop */ }
    })
    reopen.addEventListener('click', () => {
      enginePanelVisible = true
      panel.classList.remove('je-hidden')
      reopen.style.display = 'none'
      try { api.runtime.sendMessage({ type: 'engine-panel-state', visible: true }).catch?.(() => {}) } catch { /* noop */ }
    })

    // Update learned-words count on the panel every 5s
    const refreshWords = () => {
      try {
        const count = Object.keys(learnedVocab).length
        const el = document.getElementById('je-words')
        if (el) el.textContent = count + ' words'
      } catch { /* noop */ }
    }
    refreshWords()
    setInterval(refreshWords, 5000)
  }

  // Draw binary-engine bars on the in-page panel canvas.
  // bands = 16 values 0-1; isUserVoice / isSpeaking control color.
  function drawEngineCanvas(bands, energy, isUserVoice, isSpeaking) {
    if (!engineCanvas) return
    const ctx2 = engineCanvas.getContext('2d')
    if (!ctx2) return
    const W = engineCanvas.width, H = engineCanvas.height
    ctx2.clearRect(0, 0, W, H)

    const BAR_W = 8, GAP = 1, CELL_H = 5, CELL_GAP = 1
    const totalCells = Math.floor((H + CELL_GAP) / (CELL_H + CELL_GAP))

    let activeColor = '#334155'
    if (isSpeaking)       activeColor = '#f59e0b'
    else if (isUserVoice) activeColor = '#06b6d4'
    else if (energy > 0.01) activeColor = '#8b5cf6'

    bands.forEach((band, i) => {
      const x = i * (BAR_W + GAP) + 1
      const litCells = Math.round(band * totalCells)
      for (let c = 0; c < totalCells; c++) {
        const y = H - (c + 1) * (CELL_H + CELL_GAP) + CELL_GAP
        ctx2.fillStyle = c < litCells ? activeColor : '#0f172a'
        ctx2.fillRect(x, y, BAR_W, CELL_H)
      }
    })

    // Update ring + status text
    const ring = document.getElementById('je-match-ring')
    const stat = document.getElementById('je-status')
    if (ring) {
      ring.className = isSpeaking ? 'speaking' : isUserVoice && energy > 0.01 ? 'match' : energy > 0.01 ? 'no-match' : ''
    }
    if (stat) {
      stat.textContent = isSpeaking ? 'SPEAKING' : !listening ? 'IDLE' : isUserVoice ? 'YOUR VOICE' : energy > 0.01 ? 'OTHER VOICE' : 'LISTENING'
    }
  }

  // Show or hide the in-page panel (called from popup toggle)
  function updateEnginePanel(visible) {
    if (!enginePanelEl) return
    enginePanelVisible = visible
    if (visible) enginePanelEl.classList.remove('je-hidden')
    else         enginePanelEl.classList.add('je-hidden')
    const reopen = document.getElementById('jarvis-ve-reopen')
    if (reopen) reopen.style.display = visible ? 'none' : 'flex'
  }

  // ── Conversation continuity ────────────────────────────────────────────────
  // Open/refresh the follow-up window after every successful turn. While open,
  // the user can keep talking without repeating the wake word.
  function enterConversation() {
    inConversation = true
    clearTimeout(conversationEndTimer)
    conversationEndTimer = setTimeout(() => {
      inConversation = false
      toPage({ type: 'conversation', active: false })
    }, settings.conversationWindowMs || 30000)
    toPage({ type: 'conversation', active: true })
  }

  // Strip filler words that pollute trading commands (shared by both engines).
  function cleanFiller(text) {
    return (text || '')
      .replace(/\b(um+|uh+|er+|ah+|hmm+|like|you know|i mean|actually|basically|literally|so|well)\b/gi, '')
      .replace(/\s+/g, ' ')
      .trim()
  }

  // ── Shared transcript handler ──────────────────────────────────────────────
  // The single source of truth for wake / conversation / command logic. Both
  // the Web Speech engine (Chrome/Edge) and the Deepgram fallback (Firefox)
  // funnel every transcript through here so behaviour is identical.
  //   • Self-voice guard: while JARVIS speaks, only a wake phrase (barge-in)
  //     from the calibrated user is honoured — its own TTS is never a command.
  //   • Voice-identity guard: when requireVoiceMatch is on, non-matching voices
  //     are ignored entirely (no wake, no command, no transcript).
  //   • Conversation guard: inside the open window, follow-ups skip the wake.
  function handleTranscript(transcript, isFinal) {
    if (!transcript) return
    const voiceOK = passesIdentityGate()

    // ── Barge-in while JARVIS is speaking ──────────────────────────────────
    if (pageSpeaking) {
      if (voiceOK && hasWake(transcript)) {
        toPage({ type: 'interrupt' })
        pageSpeaking = false
        awaitingCommand = true
        commandBuffer   = stripWake(transcript)
        toPage({ type: 'wake' })
        badge('▶', '#06b6d4')
        clearTimeout(commandTimer)
        if (isFinal && commandBuffer.length > 2) {
          dispatchCommand(commandBuffer); awaitingCommand = false; commandBuffer = ''; badge('●', '#22c55e')
        } else {
          commandTimer = setTimeout(() => {
            escalateDeepgram('wake_no_command'); awaitingCommand = false; commandBuffer = ''; badge('●', '#22c55e')
          }, 7000)
        }
      }
      return  // never treat JARVIS's own speech as input
    }

    // ── Voice-identity gate: ignore anyone who is not the calibrated user ──
    if (!voiceOK) return

    if (!awaitingCommand) {
      // Enter the command phase via the wake word OR — while the conversation
      // window is open — via any substantive follow-up (no wake required).
      const woke          = isFinal && hasWake(transcript)
      const convoFollowUp = isFinal && inConversation && !hasWake(transcript) && transcript.trim().length > 2
      if (woke || convoFollowUp) {
        awaitingCommand = true
        commandBuffer   = woke ? stripWake(transcript) : transcript.trim()
        if (woke) { notify('JARVIS', 'Listening for your command…'); toPage({ type: 'wake' }) }
        badge('▶', '#06b6d4')
        clearTimeout(commandTimer)
        if (commandBuffer.length > 2) {
          dispatchCommand(commandBuffer); awaitingCommand = false; commandBuffer = ''; badge('●', '#22c55e')
        } else {
          commandTimer = setTimeout(() => {
            escalateDeepgram('wake_no_command'); awaitingCommand = false; commandBuffer = ''; badge('●', '#22c55e')
          }, 7000)
        }
      }
    } else {
      // ── Command-capture phase ───────────────────────────────────────────
      const piece = transcript.trim()
      if (isFinal) {
        clearTimeout(dispatchTimer)
        commandBuffer = (commandBuffer + ' ' + piece).trim()
        clearTimeout(commandTimer)
        dispatchCommand(commandBuffer); awaitingCommand = false; commandBuffer = ''; badge('●', '#22c55e')
      } else if (piece) {
        const snapshot = (commandBuffer + ' ' + piece).trim()
        clearTimeout(dispatchTimer)
        dispatchTimer = setTimeout(() => {
          if (awaitingCommand && snapshot.length > 2) {
            clearTimeout(commandTimer)
            dispatchCommand(snapshot); awaitingCommand = false; commandBuffer = ''; badge('●', '#22c55e')
          }
        }, 600)
      }
    }
  }

  // ── Speech recognition lifecycle ──────────────────────────────────────────
  function startRecognition() {
    if (!SR) {
      // No Web Speech API (Firefox) — fall back to the Deepgram listen loop so
      // wake-word + commands still work. The mic/analyser is started separately.
      console.warn(TAG, 'Web Speech API not available — using Deepgram fallback (Firefox).')
      listening = false
      startFirefoxListen()
      // Advertise voice as READY (Deepgram engine) so the page cedes the mic and
      // shows the assistant as listening, exactly like the Web Speech path.
      try { document.documentElement.setAttribute('data-jarvis-ext-voice', '1') } catch { /* noop */ }
      status({ engine: 'deepgram', speechSupported: true, voiceReady: !!settings.enabled, listening: true })
      badge('◐', '#a855f7')
      return
    }
    if (!settings.enabled || listening || manuallyStopped) return
    // Stop any previous instance cleanly before creating a new one.
    try { if (recognition) { recognition.onend = null; recognition.onerror = null; recognition.stop() } } catch { /* noop */ }
    recognition = null

    let rec
    try { rec = new SR() } catch (e) { console.warn(TAG, 'SR constructor failed', e); return }

    rec.lang = settings.lang || 'en-US'
    rec.continuous    = true
    rec.interimResults = true
    rec.maxAlternatives = 5  // v2.1: 5 alternatives → pick the best one

    // ── Pick the highest-quality alternative using vocab + confidence ──────
    // v2.2: score by (confidence × 15) + (word count × 3) + learned-vocab boost.
    function pickBest(results) {
      if (!results || results.length === 0) return ''
      let best = results[0]?.transcript || ''
      let bestScore = -1
      for (let k = 0; k < results.length; k++) {
        const alt = results[k]
        if (!alt) continue
        const words = (alt.transcript || '').toLowerCase().match(/[a-z]{2,}/g) || []
        let score = words.length * 3 + (alt.confidence || 0) * 15
        // Boost score for words present in the learned vocabulary
        for (const w of words) {
          const freq = learnedVocab[w] || 0
          if (freq > 0) score += Math.min(freq * 2, 20)
        }
        if (score > bestScore) { bestScore = score; best = alt.transcript || best }
      }
      return best
    }

    // ── Remove filler words that pollute trading commands ─────────────────
    function cleanTranscript(text) {
      return cleanFiller(text)
    }

    // ── onstart ──────────────────────────────────────────────────────────────
    rec.onstart = () => {
      listening    = true
      restartDelay = 500  // reset back-off on clean start
      status()
      badge('●', '#22c55e')
    }

    // ── onresult ─────────────────────────────────────────────────────────────
    // Each result block is fully isolated in try/catch so a single bad result
    // never crashes the entire recognition session.
    rec.onresult = (e) => {
      if (!e || !e.results) return
      try {
        for (let i = e.resultIndex; i < e.results.length; i++) {
          const result     = e.results[i]
          if (!result || !result[0]) continue
          // v2.1: use best alternative, then strip filler words
          const rawTranscript = pickBest(result)
          const transcript    = cleanTranscript(rawTranscript) || rawTranscript
          const isFinal    = !!result.isFinal

          // Echo + voice gate for the popup transcript mirror: never show
          // JARVIS's own TTS, and (when voice-ID is on) never show other people.
          const voiceOK = passesIdentityGate()
          if (!pageSpeaking && voiceOK) mirrorTranscript(transcript, isFinal)

          // Single shared wake / conversation / command pipeline.
          handleTranscript(transcript, isFinal)
        }
      } catch (err) {
        console.warn(TAG, 'onresult error (non-fatal)', err)
      }
    }

    // ── onerror ───────────────────────────────────────────────────────────────
    rec.onerror = (e) => {
      try {
        const err = e && e.error ? e.error : String(e)
        console.warn(TAG, 'recognition error:', err)
        if (err === 'not-allowed' || err === 'service-not-allowed') {
          // Mic permanently blocked — stop trying
          manuallyStopped = true
          settings.enabled = false
          try { api.storage.local.set({ settings }) } catch { /* noop */ }
          toPage({ type: 'status', listening: false, error: 'mic-denied' })
          notify('JARVIS', 'Microphone access denied — allow it and reload.')
          badge('✕', '#ef4444')
        } else if (err === 'aborted') {
          // Controlled stop — onend will handle restart
        } else if (err === 'network') {
          // Transient network issue — increase back-off
          restartDelay = Math.min(restartDelay * 2, 8000)
        }
        // All other errors → onend auto-restarts
      } catch { /* noop */ }
    }

    // ── onend ─────────────────────────────────────────────────────────────────
    rec.onend = () => {
      try {
        clearTimeout(dispatchTimer)
        listening = false
        status()
        if (settings.enabled && !manuallyStopped) {
          clearTimeout(restartTimer)
          restartTimer = setTimeout(() => { if (settings.enabled && !listening) startRecognition() }, restartDelay)
        } else {
          badge('', '#64748b')
        }
      } catch { /* noop */ }
    }

    recognition = rec
    try { rec.start() } catch (e) { console.warn(TAG, 'rec.start() failed', e); listening = false }
  }

  function stopRecognition() {
    manuallyStopped = true
    clearTimeout(restartTimer)
    try { if (recognition) { recognition.onend = null; recognition.stop() } } catch { /* noop */ }
    recognition = null
    if (fxActive) stopFirefoxListen()  // also stop the Firefox Deepgram loop
    listening   = false
    badge('', '#64748b')
    status()
  }

  // ── Page → Extension message handler ──────────────────────────────────────
  window.addEventListener('message', (event) => {
    try {
      if (event.source !== window) return
      const d = event.data
      if (!d || d.__jarvisPage !== true) return
      switch (d.type) {
        case 'ping':
          connected(); break
        case 'voice-control':
          settings.enabled = !!d.enabled
          manuallyStopped = !settings.enabled
          try { api.storage.local.set({ settings }) } catch { /* noop */ }
          if (settings.enabled) {
            manuallyStopped = false
            startRecognition()
            initFreqAnalyser()
          } else {
            stopRecognition()
            stopFreqAnalyser()
          }
          connected()
          break
        case 'notify':
          notify(d.title || 'JARVIS', d.body || ''); break
        case 'save-zoom':
          try { api.storage.local.set({ brainZoom: d.data }) } catch { /* noop */ }; break
        case 'request-zoom':
          try { api.storage.local.get('brainZoom', (res) => { if (res?.brainZoom) toPage({ type: 'zoom-data', data: res.brainZoom }) }) } catch { /* noop */ }; break
        // v2.1: relay learned vocabulary to background (chrome.storage.local)
        // so it survives even if the page's localStorage is cleared.
        case 'voice-learning-save':
          try {
            api.storage.local.set({ voiceLearning: d.data, voiceLearningTs: Date.now() })
          } catch { /* noop */ }
          break
        case 'speak-status':
          pageSpeaking = !!d.speaking
          if (d.speaking) pageSpeakAckAt = Date.now()  // page is handling TTS (universal voice)
          // While JARVIS is talking we must never transcribe its own voice. Two
          // modes, chosen by the page via `allowBargeIn`:
          //   • allowBargeIn = false → fully stop the mic (zero self-hearing).
          //   • allowBargeIn = true  → KEEP listening, but onresult ignores
          //     everything except the wake phrase while `pageSpeaking` is true,
          //     and the page re-checks the user's stored-voice match before
          //     honouring the interrupt. This lets the user cut JARVIS off by
          //     voice while still rejecting the assistant's own speech.
          if (pageSpeaking) {
            if (d.allowBargeIn) {
              // Keep the recognizer running so the user can barge in by voice.
              if (settings.enabled && !listening) {
                manuallyStopped = false
                clearTimeout(restartTimer)
                restartTimer = setTimeout(() => { if (settings.enabled && !listening) startRecognition() }, 150)
              }
            } else {
              stopRecognition()
            }
          } else if (settings.enabled) {
            // JARVIS finished — resume normal listening shortly after (the small
            // delay swallows any audio echo tail). If a conversation is active,
            // refresh its window so the user can reply immediately without the
            // wake word.
            if (inConversation) enterConversation()
            manuallyStopped = false
            clearTimeout(restartTimer)
            restartTimer = setTimeout(() => { if (settings.enabled && !listening) startRecognition() }, 350)
          }
          break

        case 'voice-match-update':
          // Page's AudioContext voiceMatch loop informs us whether the current
          // speaker is the calibrated user. We use this to update the panel ring.
          pageVoiceMatch = !!d.isMatch
          break
      }
    } catch { /* noop */ }
  })

  // ── Popup / background message handler ────────────────────────────────────
  try {
    api.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
      try {
        switch (msg && msg.type) {
          case 'get-status':
            sendResponse({ listening, enabled: settings.enabled }); break
          case 'toggle':
            settings.enabled = !!msg.enabled
            try { api.storage.local.set({ settings }) } catch { /* noop */ }
            if (settings.enabled) { manuallyStopped = false; startRecognition() }
            else stopRecognition()
            sendResponse({ ok: true }); break
          case 'test-notify':
            notify('JARVIS Test', 'Notifications working, Sir.')
            sendResponse({ ok: true }); break
          case 'speak':
            // Background sends this when it wants TTS from this tab
            try { queueSpeak(msg.text) } catch { /* noop */ }
            sendResponse({ ok: true }); break
          case 'set-avatar':
            // Popup avatar picker → push the new style to the page robot
            try { toPage({ type: 'avatar-style', style: msg.style }) } catch { /* noop */ }
            sendResponse({ ok: true }); break
          case 'set-robot-mode':
            // Popup robot-mode toggle → tell the page to lock/unlock mic+speaker
            // and hide/show the chat panel. The page (PaulChat) listens for this.
            try { toPage({ type: 'robot-mode', active: !!msg.enabled }) } catch { /* noop */ }
            sendResponse({ ok: true }); break
          case 'positions-update':
            // Forward live positions to the page so UI widgets update
            try { toPage({ type: 'positions-update', positions: msg.positions }) } catch { /* noop */ }
            sendResponse({ ok: true }); break
          case 'voice-engine-panel':
            // Popup toggle: show/hide the in-page binary engine overlay
            updateEnginePanel(!!msg.visible)
            sendResponse({ ok: true, visible: enginePanelVisible }); break
          case 'face-vision-state':
            // Relayed from popup face-vision.js (via background). Keeps the
            // speech recogniser in sync with the visual talking/identity signal.
            faceState = {
              present: !!msg.facePresent,
              talking: !!msg.isTalking,
              match:   !!msg.identityMatch,
              mar:     msg.mar || 0,
              ts:      msg.ts || Date.now(),
            }
            // Mirror the visual "talking" onto the page robot so it reacts in
            // sync with the mouth (nice-to-have; page may ignore it).
            try { toPage({ type: 'face-talking', talking: faceState.talking, match: faceState.match }) } catch { /* noop */ }
            sendResponse({ ok: true }); break
          default: sendResponse({}); break
        }
      } catch (e) { sendResponse({ error: String(e) }) }
      return true  // keep message channel open for async
    })
  } catch (e) { console.warn(TAG, 'onMessage addListener failed', e) }

  // ── Settings hot-reload from popup ────────────────────────────────────────
  try {
    api.storage.onChanged.addListener((changes) => {
      try {
        if (changes.settings) {
          const prev = { ...settings }
          settings = { ...settings, ...changes.settings.newValue }
          if (settings.enabled && !prev.enabled) {
            manuallyStopped = false
            startRecognition()
            initFreqAnalyser()
          } else if (!settings.enabled && prev.enabled) {
            stopRecognition()
            stopFreqAnalyser()
          }
        }
        // Keep learnedVocab in sync when extension storage saves new vocab
        if (changes.voiceLearning && changes.voiceLearning.newValue) {
          learnedVocab = { ...learnedVocab, ...changes.voiceLearning.newValue }
        }
        // Relay avatar-style changes (from the popup picker) to the page robot
        if (changes.avatarStyle && changes.avatarStyle.newValue) {
          toPage({ type: 'avatar-style', style: changes.avatarStyle.newValue })
        }
      } catch { /* noop */ }
    })
  } catch (e) { console.warn(TAG, 'storage.onChanged addListener failed', e) }

  // Push the saved avatar style to the page robot on load
  try {
    api.storage.local.get('avatarStyle', (data) => {
      const style = (data && data.avatarStyle) || 'cyan'
      const relay = () => toPage({ type: 'avatar-style', style })
      relay()
      setTimeout(relay, 800); setTimeout(relay, 2000)  // beat React mount race
    })
  } catch { /* noop */ }

  // ── Boot ───────────────────────────────────────────────────────────────────
  markPresence()
  try {
    api.storage.local.get('settings', (data) => {
      try {
        if (data && data.settings) settings = { ...settings, ...data.settings }
        // Announce presence (repeat to beat React's mount race)
        const announce = () => { try { connected() } catch { /* noop */ } }
        announce();
        setTimeout(announce, 300); setTimeout(announce, 1000); setTimeout(announce, 2500)
        if (settings.enabled) {
          startRecognition()
          initFreqAnalyser()
          // Re-arm on first user gesture in case the browser blocked autostart
          const arm = () => {
            if (settings.enabled && !listening) startRecognition()
            if (settings.enabled && !freqCtx) initFreqAnalyser()
          }
          window.addEventListener('pointerdown', arm, { once: true, passive: true })
          window.addEventListener('keydown',     arm, { once: true })
        }
      } catch (e) { console.warn(TAG, 'boot error', e) }
    })
  } catch (e) { console.warn(TAG, 'storage.local.get failed', e) }

  if (!SR) console.warn(TAG, 'Web Speech API not available in this tab — using page mic fallback.')
  // Read version from manifest dynamically — never hardcode it
  const INSTALLED_VERSION = (() => {
    try { return api.runtime.getManifest().version || '1.0.0' } catch { return '1.0.0' }
  })()
  console.log(TAG, `content script v${INSTALLED_VERSION} loaded on`, window.location.origin)
  // v3.1: The in-page voice engine panel is REMOVED — the floating 3D JARVIS
  // robot (rendered by the page) is now the visual. The binary engine lives
  // only in the extension popup. We intentionally do NOT inject the panel.
  // (injectVoiceEnginePanel is retained for reference but no longer called.)

  // v2.1: On startup, load any previously saved voice learning from background
  // and post it to the page so speech recognition self-improves even after
  // localStorage is cleared or on a fresh browser profile.
  try {
    api.storage.local.get(['voiceLearning', 'enginePanelVisible'], (res) => {
      if (res && res.voiceLearning && Object.keys(res.voiceLearning).length > 0) {
        learnedVocab = { ...learnedVocab, ...res.voiceLearning }
        toPage({ type: 'voice-learning-restore', data: res.voiceLearning })
      }
      // v3.1: The 3D JARVIS robot (rendered by the page) now replaces the binary
      // engine panel as the primary visual. Hide the legacy panel UNLESS the user
      // has explicitly opted to keep it (enginePanelVisible === true).
      if (!res || res.enginePanelVisible !== true) {
        updateEnginePanel(false)
      }
    })
  } catch { /* noop */ }

  // ── Auto-update detection ────────────────────────────────────────────────
  // INSTALLED_VERSION is read from manifest above (never hardcode it!)
  const VERSION_CHECK_INTERVAL_MS = 24 * 60 * 60 * 1000  // 24 hours

  function semverNewer(a, b) {
    // Returns true if a > b (both in 'major.minor.patch' format)
    const pa = (a || '0.0.0').split('.').map(Number)
    const pb = (b || '0.0.0').split('.').map(Number)
    for (let i = 0; i < 3; i++) {
      if ((pa[i] || 0) > (pb[i] || 0)) return true
      if ((pa[i] || 0) < (pb[i] || 0)) return false
    }
    return false
  }

  function injectUpdateBanner(latestVersion, changelog) {
    if (document.getElementById('jarvis-update-banner')) return
    const installedVer = INSTALLED_VERSION
    const banner = document.createElement('div')
    banner.id = 'jarvis-update-banner'
    banner.style.cssText = [
      'position:fixed', 'top:0', 'left:0', 'right:0', 'z-index:999999',
      'background:linear-gradient(135deg,#1e1b4b 0%,#312e81 100%)',
      'border-bottom:2px solid #4c1d95',
      'padding:9px 16px', 'display:flex', 'align-items:center', 'gap:12px',
      'font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif',
      'font-size:12px', 'color:#e2e8f0', 'box-shadow:0 3px 16px rgba(0,0,0,.6)',
    ].join(';')

    const firstChange = (changelog && changelog[0]) || 'New features and improvements'
    banner.innerHTML = `
      <div style="width:28px;height:28px;border-radius:50%;background:linear-gradient(135deg,#06b6d4,#8b5cf6);
                  display:flex;align-items:center;justify-content:center;flex-shrink:0;font-size:15px;
                  box-shadow:0 0 10px rgba(6,182,212,.4)">🤖</div>
      <div style="flex:1;min-width:0">
        <span style="font-weight:700;color:#c4b5fd">JARVIS v${installedVer} → v${latestVersion}</span>
        <span style="color:#94a3b8;margin-left:8px;font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${firstChange}</span>
      </div>
      <button id="jarvis-update-how"
              style="background:#4c1d95;border:1px solid rgba(139,92,246,.5);border-radius:7px;color:#c4b5fd;
                     padding:5px 12px;font-size:11px;font-weight:600;cursor:pointer;white-space:nowrap;
                     transition:background .15s">
        Update now →
      </button>
      <button id="jarvis-update-dismiss"
              style="background:none;border:none;color:#475569;font-size:18px;cursor:pointer;
                     padding:0 4px;line-height:1;flex-shrink:0">×</button>
    `
    document.body.prepend(banner)
    document.documentElement.style.paddingTop = '47px'

    document.getElementById('jarvis-update-dismiss')?.addEventListener('click', () => {
      banner.remove()
      document.documentElement.style.paddingTop = ''
      try { api.storage.local.set({ updateBannerDismissed: Date.now() }) } catch { /* noop */ }
    })

    document.getElementById('jarvis-update-how')?.addEventListener('click', () => {
      const modal = document.createElement('div')
      modal.id = 'jarvis-update-modal'
      modal.style.cssText = [
        'position:fixed', 'inset:0', 'z-index:1000000',
        'background:rgba(0,0,0,.75)',
        'display:flex', 'align-items:center', 'justify-content:center',
        'padding:16px',
      ].join(';')
      const changes = (changelog || []).map(c =>
        `<li style="margin-bottom:5px;padding-left:4px">${c}</li>`
      ).join('')
      modal.innerHTML = `
        <div style="background:#0a1628;border:1px solid #4c1d95;border-radius:18px;
                    padding:24px;max-width:440px;width:100%;box-shadow:0 12px 48px rgba(0,0,0,.8);
                    font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#e2e8f0">
          <div style="display:flex;align-items:center;gap:12px;margin-bottom:18px">
            <div style="width:46px;height:46px;border-radius:50%;
                        background:linear-gradient(135deg,#06b6d4,#8b5cf6);
                        display:flex;align-items:center;justify-content:center;
                        font-size:22px;flex-shrink:0;box-shadow:0 0 16px rgba(6,182,212,.3)">🤖</div>
            <div>
              <div style="font-size:17px;font-weight:700">JARVIS v${latestVersion} Ready</div>
              <div style="font-size:11px;color:#64748b;margin-top:1px">
                Updating from v${installedVer} · No reinstall needed
              </div>
            </div>
          </div>

          <div style="font-size:10px;color:#64748b;text-transform:uppercase;
                      letter-spacing:.08em;margin-bottom:8px;font-weight:600">What's New</div>
          <ul style="padding-left:18px;font-size:12px;color:#94a3b8;
                     margin-bottom:20px;line-height:1.6;list-style:disc">${changes}</ul>

          <div style="font-size:10px;color:#64748b;text-transform:uppercase;
                      letter-spacing:.08em;margin-bottom:10px;font-weight:600">How to Update (30 seconds)</div>
          <div style="background:#111827;border:1px solid rgba(255,255,255,.06);
                      border-radius:10px;padding:14px;font-size:12px;color:#94a3b8;
                      line-height:2;margin-bottom:18px">
            <div><span style="background:#4c1d95;color:#c4b5fd;border-radius:4px;
                              padding:1px 7px;font-weight:700;font-size:10px">1</span>
              &nbsp;Press <kbd style="background:#1e293b;border:1px solid rgba(255,255,255,.1);
                                      border-radius:4px;padding:1px 6px;color:#c4b5fd;font-size:11px">
              Ctrl+Shift+J</kbd> to open Extensions
            </div>
            <div><span style="background:#4c1d95;color:#c4b5fd;border-radius:4px;
                              padding:1px 7px;font-weight:700;font-size:10px">2</span>
              &nbsp;Find <strong style="color:#e2e8f0">JARVIS Voice Assistant</strong>
            </div>
            <div><span style="background:#4c1d95;color:#c4b5fd;border-radius:4px;
                              padding:1px 7px;font-weight:700;font-size:10px">3</span>
              &nbsp;Click the <strong style="color:#22c55e">↺ Reload</strong> button
            </div>
            <div><span style="background:#22c55e;color:#052e16;border-radius:4px;
                              padding:1px 7px;font-weight:700;font-size:10px">✓</span>
              &nbsp;Return here — done! All settings preserved.
            </div>
          </div>

          <div style="background:#0c1f3d;border:1px solid rgba(6,182,212,.2);
                      border-radius:8px;padding:10px 14px;font-size:11px;
                      color:#67e8f9;margin-bottom:18px;line-height:1.5">
            <strong>💡 Tip:</strong> You can also type
            <code style="background:#0f172a;border-radius:3px;padding:0 4px;
                         color:#c4b5fd;font-size:11px">chrome://extensions</code>
            in the address bar, then click ↺ Reload on JARVIS.
          </div>

          <div style="display:flex;gap:8px">
            <button id="jarvis-copy-ext-path"
                    style="flex:1;background:rgba(139,92,246,.15);border:1px solid rgba(139,92,246,.3);
                           border-radius:9px;color:#c4b5fd;padding:9px;font-size:12px;
                           font-weight:600;cursor:pointer">
              📋 Copy extension path
            </button>
            <button onclick="document.getElementById('jarvis-update-modal').remove()"
                    style="background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.07);
                           border-radius:9px;color:#64748b;padding:9px 18px;
                           font-size:12px;cursor:pointer">
              Later
            </button>
          </div>
        </div>
      `
      document.body.appendChild(modal)
      modal.addEventListener('click', (e) => { if (e.target === modal) modal.remove() })

      // Copy extension folder path to clipboard so user can paste in File Explorer
      document.getElementById('jarvis-copy-ext-path')?.addEventListener('click', async () => {
        const extPath = 'jarvis-extension'  // relative path from project root
        try {
          // Ask background to get the extension path
          const url = api.runtime.getURL('manifest.json')
          const extFolder = url.replace('/manifest.json', '')
          await navigator.clipboard.writeText(extFolder)
          const btn = document.getElementById('jarvis-copy-ext-path')
          if (btn) { btn.textContent = '✓ Copied!'; setTimeout(() => { btn.textContent = '📋 Copy extension path' }, 2000) }
        } catch {
          const btn = document.getElementById('jarvis-copy-ext-path')
          if (btn) btn.textContent = 'Open chrome://extensions manually'
        }
      })
    })
  }

  async function checkForUpdate() {
    // Don't spam — respect 24h cooldown
    try {
      const stored = await new Promise(r => api.storage.local.get(['updateBannerDismissed', 'lastVersionCheck', 'lastKnownLatestVersion'], r))
      const now = Date.now()
      // Always check if the latest known version changed (user may have updated the backend)
      const cachedLatest = stored.lastKnownLatestVersion || '0.0.0'
      const dismissCooldown = stored.updateBannerDismissed
        ? (now - stored.updateBannerDismissed) < VERSION_CHECK_INTERVAL_MS
        : false
      // Skip only if recently dismissed AND no new version available in cache
      if (dismissCooldown && !semverNewer(cachedLatest, INSTALLED_VERSION)) return
      if (stored.lastVersionCheck && (now - stored.lastVersionCheck) < 60_000) return  // max once/min
      api.storage.local.set({ lastVersionCheck: now })
    } catch { /* noop */ }

    try {
      const res = await fetch('http://localhost:1448/api/v1/jarvis/extension-version', {
        signal: AbortSignal.timeout(5000)
      })
      if (!res.ok) return
      const data = await res.json()
      const latest = data.version || '0.0.0'
      // Cache the latest known version so subsequent checks detect version bumps quickly
      try { api.storage.local.set({ lastKnownLatestVersion: latest }) } catch { /* noop */ }
      if (semverNewer(latest, INSTALLED_VERSION)) {
        injectUpdateBanner(latest, data.changelog || [])
      }
    } catch { /* backend offline or not reachable */ }
  }

  // Run version check 2s after page load so it doesn't block anything
  setTimeout(checkForUpdate, 2000)

  })()

