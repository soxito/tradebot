/*
 * JARVIS Voice Assistant — Background Service Worker  v3.0.0
 *
 * Responsibilities:
 *  1. Desktop notifications (OS-level)
 *  2. Toolbar badge management
 *  3. Unified monitor — polls /api/v1/jarvis/unified-monitor every 10 s,
 *     pulls crypto + MT5 positions + balances, detects changes,
 *     fires desktop notifications AND reads aloud via TTS.
 *  4. 15-minute position analysis alarm — runs AI/SMC analysis on all
 *     open MT5 positions and caches the result in storage.
 *  5. On-demand analysis — triggered by user clicking "Analyze Now".
 *  6. Crypto command relay — receives parsed command from content.js,
 *     POSTs to /api/v1/jarvis/command, speaks the result back.
 */

'use strict'

const api = typeof browser !== 'undefined' ? browser : chrome

const BACKEND_OLD         = 'http://localhost:1448/api/v1'    // legacy crypto API (kept for fallback)
const BACKEND             = 'http://localhost:1448/api/v1'    // unified backend (crypto + MT5 + jarvis)
const POLL_MS             = 10_000   // realtime monitor poll interval (10 s)
const ANALYSIS_ALARM      = 'jarvis-position-analysis'        // alarm name
const ANALYSIS_PERIOD_MIN = 15                                // every 15 minutes
const PNL_THRESHOLD_PCT   = 3        // % PnL change triggers alert
const PNL_THRESHOLD_USD   = 20       // or $20 absolute change

// ── State ─────────────────────────────────────────────────────────────────────
let lastNotifyId       = 0
let positionSnapshot   = {}   // { "exchange:SYMBOL": Position }
let mt5Snapshot        = {}   // { "accountId:SYMBOL:ticket": position }
let monitorEnabled     = false
let ttsEnabled         = true
let pollTimer          = null
let lastAnalysisResult = null  // cached from most recent 15-min or on-demand run
let lastUnifiedData    = null  // last successful unified-monitor response (for instant popup load)
let defaultMt5Account  = null  // first configured MT5 account ID
let coinNames          = {}    // { "BTCUSDT": "Bitcoin", "BTC/USDT": "Bitcoin", ... }
let coinNamesFetchedAt = 0     // epoch ms of last name-map fetch

// ── Face Vision state (updated by popup face-vision.js) ──────────────────────
// Background keeps last-known state so other modules can query it.
let faceVisionState = {
  facePresent:   false,
  isTalking:     false,
  mar:           0,
  identityMatch: false,
  lastUpdateMs:  0,
}

// Restore settings on startup
api.storage.local.get(
  ['monitorEnabled', 'ttsEnabled', 'lastAnalysisResult', 'defaultMt5Account'],
  (res) => {
    monitorEnabled     = !!(res.monitorEnabled ?? true)
    ttsEnabled         = !!(res.ttsEnabled     ?? true)
    lastAnalysisResult = res.lastAnalysisResult || null
    defaultMt5Account  = res.defaultMt5Account || null
    if (monitorEnabled) scheduleNextPoll()
  }
)

// ── Desktop notification helper ───────────────────────────────────────────────
function showNotification(title, body, urgent = false) {
  const id = 'jarvis-' + (++lastNotifyId)
  try {
    api.notifications.create(id, {
      type: 'basic',
      iconUrl: api.runtime.getURL('icons/icon128.png'),
      title: title || 'JARVIS',
      message: body || '',
      priority: urgent ? 2 : 1,
      silent: false,
    })
    setTimeout(() => { try { api.notifications.clear(id) } catch { /* noop */ } }, 8000)
  } catch (e) {
    console.warn('[JARVIS-BG] notification failed', e)
  }
}

// ── Toolbar badge ─────────────────────────────────────────────────────────────
function setBadge(text, color) {
  try { api.action.setBadgeText({ text: text || '' }) } catch { /* noop */ }
  if (color) { try { api.action.setBadgeBackgroundColor({ color }) } catch { /* noop */ } }
}

// ── TTS: send to all TradeBot tabs (content script handles speechSynthesis) ───
function speakText(text) {
  if (!ttsEnabled || !text) return
  const urls = ['http://localhost:3000/*', 'http://127.0.0.1:3000/*']
  api.tabs.query({ url: urls }, (tabs) => {
    for (const tab of (tabs || [])) {
      api.tabs.sendMessage(tab.id, { type: 'speak', text }).catch?.(() => {})
    }
  })
}

// ── Fetch helpers ─────────────────────────────────────────────────────────────
async function apiFetch(path, opts = {}, backend = BACKEND) {
  const resp = await fetch(`${backend}${path}`, {
    signal: AbortSignal.timeout(8000),
    ...opts,
  })
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
  return resp.json()
}

// Legacy crypto API (port 1448)
async function legacyApiFetch(path, opts = {}) {
  return apiFetch(path, opts, BACKEND_OLD)
}

// ── Coin name map ─────────────────────────────────────────────────────────────
// Fetch the compact { symbol: name } map so notifications/TTS say real coin
// names ("Bitcoin") instead of raw symbols ("BTCUSDT"). Keyed by BOTH BTC/USDT
// and BTCUSDT server-side, so monitor payloads (glued form) map directly.
async function refreshCoinNames(force = false) {
  const STALE_MS = 6 * 60 * 60 * 1000  // 6h
  if (!force && coinNamesFetchedAt && (Date.now() - coinNamesFetchedAt) < STALE_MS) return
  // Try the unified backend first, then the legacy crypto backend.
  for (const backend of [BACKEND, BACKEND_OLD]) {
    try {
      const data = await apiFetch('/jarvis/pairs/names', {}, backend)
      if (data && data.names && Object.keys(data.names).length) {
        coinNames = data.names
        coinNamesFetchedAt = Date.now()
        return
      }
    } catch { /* try next backend */ }
  }
}

// Resolve a position symbol to its real coin name, gracefully falling back to
// the symbol when unknown. Handles glued (BTCUSDT), slashed (BTC/USDT) and
// ccxt swap (BTC/USDT:USDT) forms.
function coinName(symbol) {
  if (!symbol) return symbol
  const s = String(symbol)
  if (coinNames[s]) return coinNames[s]
  const noSwap = s.split(':')[0]
  if (coinNames[noSwap]) return coinNames[noSwap]
  const glued = noSwap.replace(/\//g, '')
  if (coinNames[glued]) return coinNames[glued]
  return s
}

// ── Cost-aware Deepgram fallback relay ────────────────────────────────────────
// Forwards a short buffered audio clip (base64 from the content script) to the
// backend's budget-guarded pre-recorded STT endpoint. The raw Deepgram key never
// leaves the backend. Always resolves to the endpoint JSON (used_deepgram may be
// false when the cap is reached or on any backend/Deepgram error).
async function deepgramStt(b64, mime) {
  const bin = atob(b64 || '')
  const bytes = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i)
  const type = mime || 'audio/webm'
  const ext = type.includes('ogg') ? 'ogg' : 'webm'
  const form = new FormData()
  form.append('file', new Blob([bytes], { type }), `jarvis-miss.${ext}`)
  const resp = await fetch(`${BACKEND}/voice/deepgram/stt`, {
    method: 'POST',
    body: form,
    signal: AbortSignal.timeout(15000),
  })
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
  return resp.json()
}

// ── Positions polling (unified: crypto + MT5) ─────────────────────────────────
function scheduleNextPoll() {
  clearTimeout(pollTimer)
  pollTimer = setTimeout(doPoll, POLL_MS)
}

async function doPoll() {
  if (!monitorEnabled) return
  // Keep the coin-name map fresh (no-op unless stale). Non-blocking on failure.
  refreshCoinNames().catch(() => {})
  try {
    const data = await apiFetch('/jarvis/unified-monitor')
    handleUnifiedUpdate(data)
    broadcastMonitor(data)
  } catch {
    // Try legacy backend if new backend is offline
    try {
      const positions = await legacyApiFetch('/jarvis/positions')
      const legacy = {
        crypto_positions: positions,
        crypto_total_pnl: positions.reduce((s, p) => s + (p.pnl || 0), 0),
        mt5_accounts: [],
        mt5_total_balance: 0,
        mt5_total_equity: 0,
        mt5_total_floating_pnl: 0,
        mt5_position_count: 0,
        total_position_count: positions.length,
        total_pnl: positions.reduce((s, p) => s + (p.pnl || 0), 0),
        fetched_at: new Date().toISOString(),
      }
      handleUnifiedUpdate(legacy)
      broadcastMonitor(legacy)
    } catch { /* both backends offline — stay quiet */ }
  }
  scheduleNextPoll()
}

function posKey(p) {
  return `${p.exchange}:${p.symbol}`
}

function mt5PosKey(acctId, p) {
  return `${acctId}:${p.symbol}:${p.ticket || 0}`
}

function handleUnifiedUpdate(data) {
  lastUnifiedData = data  // cache for instant popup loads via get-state
  const cryptoPositions = data.crypto_positions || []
  const mt5Accounts     = data.mt5_accounts || []

  // ── Track crypto position changes ─────────────────────────────────────────
  const newCryptoSnap = {}
  for (const p of cryptoPositions) {
    newCryptoSnap[posKey(p)] = p
  }

  for (const [key, p] of Object.entries(newCryptoSnap)) {
    if (!positionSnapshot[key]) {
      // First sight of this position — announce the current value, NO bogus delta.
      const nm = coinName(p.symbol)
      showNotification(`📈 New Crypto: ${nm}`,
        `${p.side.toUpperCase()} @ ${p.entry_price} | ${p.exchange.toUpperCase()}`)
      speakText(`New ${p.side} position opened on ${nm} at ${p.entry_price}.`)
    }
  }
  for (const [key, prev] of Object.entries(positionSnapshot)) {
    if (!newCryptoSnap[key]) {
      const nm = coinName(prev.symbol)
      const sign = (prev.pnl || 0) >= 0 ? 'PROFIT' : 'LOSS'
      showNotification(`🚪 Closed: ${nm}`,
        `${sign}: ${(prev.pnl || 0) >= 0 ? '+' : ''}${(prev.pnl || 0).toFixed(2)} USDT`, true)
      speakText(`${nm} closed. ${(prev.pnl || 0) >= 0 ? 'Profit' : 'Loss'} ${Math.abs(prev.pnl || 0).toFixed(2)}.`)
    }
  }
  for (const [key, p] of Object.entries(newCryptoSnap)) {
    const prev = positionSnapshot[key]
    if (!prev) continue  // first-sight handled above — never announce a delta here
    const curPct = p.pnl_pct || 0
    const prevPct = prev.pnl_pct || 0
    const delta = curPct - prevPct                 // change since the last reading
    const pctDelta = Math.abs(delta)
    const usdDelta = Math.abs((p.pnl || 0) - (prev.pnl || 0))
    if (pctDelta >= PNL_THRESHOLD_PCT || usdDelta >= PNL_THRESHOLD_USD) {
      const nm = coinName(p.symbol)
      // Current direction = sign of the position's own PnL; change direction =
      // sign of the delta (new − previous). This fixes the old bug where a
      // position dropping from +600% to +500% was announced as simply "up".
      const curDir = curPct >= 0 ? 'up' : 'down'
      const chgDir = delta >= 0 ? 'up' : 'down'
      const arrow = delta >= 0 ? '▲' : '▼'
      showNotification(`${arrow} ${nm}: ${curPct.toFixed(2)}%`,
        `${chgDir === 'up' ? '+' : '-'}${pctDelta.toFixed(2)}% since last | PnL ${(p.pnl || 0) >= 0 ? '+' : ''}${(p.pnl || 0).toFixed(2)} USDT`)
      speakText(
        `${nm} is ${curDir} ${Math.abs(curPct).toFixed(1)} percent, ` +
        `a change of ${pctDelta.toFixed(1)} percent ${chgDir} from the last reading.`
      )
    }
  }
  positionSnapshot = newCryptoSnap

  // ── Track MT5 position changes ─────────────────────────────────────────────
  const newMt5Snap = {}
  for (const acct of mt5Accounts) {
    for (const p of (acct.positions || [])) {
      const key = mt5PosKey(acct.account_id, p)
      newMt5Snap[key] = { ...p, account_id: acct.account_id, currency: acct.currency }
    }
  }
  for (const [key, p] of Object.entries(newMt5Snap)) {
    if (!mt5Snapshot[key]) {
      showNotification(`📈 MT5: ${p.symbol}`,
        `${p.side.toUpperCase()} ${p.volume} @ ${p.price_open} | ${p.currency}`)
      speakText(`New MT5 ${p.side} position on ${p.symbol}.`)
    }
  }
  for (const [key, prev] of Object.entries(mt5Snapshot)) {
    if (!newMt5Snap[key]) {
      const sign = (prev.profit || 0) >= 0 ? 'profit' : 'loss'
      showNotification(`🚪 MT5 Closed: ${prev.symbol}`,
        `${sign}: ${Math.abs(prev.profit || 0).toFixed(2)} ${prev.currency}`, true)
      speakText(`MT5 ${prev.symbol} closed. ${sign} ${Math.abs(prev.profit || 0).toFixed(2)} ${prev.currency}.`)
    }
  }
  mt5Snapshot = newMt5Snap

  // Badge: total open positions across all accounts
  const totalPositions = cryptoPositions.length + (data.mt5_position_count || 0)
  if (monitorEnabled) {
    setBadge(totalPositions > 0 ? String(totalPositions) : '👁', totalPositions > 0 ? '#8b5cf6' : '#64748b')
  }

  // Store default MT5 account ID (first account found)
  if (mt5Accounts.length > 0 && !defaultMt5Account) {
    defaultMt5Account = mt5Accounts[0].account_id
    api.storage.local.set({ defaultMt5Account })
  }
}

// ── Broadcast unified data to popup + content scripts ─────────────────────────
function broadcastMonitor(data) {
  api.runtime.sendMessage({ type: 'monitor-update', data }).catch?.(() => {})
  api.tabs.query({ url: ['http://localhost:3000/*', 'http://127.0.0.1:3000/*'] }, (tabs) => {
    for (const tab of (tabs || [])) {
      api.tabs.sendMessage(tab.id, { type: 'positions-update', positions: data.crypto_positions || [] }).catch?.(() => {})
    }
  })
}

// ── 15-minute position analysis alarm ─────────────────────────────────────────
async function runPositionAnalysis(onDemand = false) {
  // Auto-discover the first MT5 account if we haven't polled yet
  let acctId = defaultMt5Account
  if (!acctId) {
    try {
      const data = await apiFetch('/jarvis/unified-monitor')
      const mt5Accounts = data.mt5_accounts || []
      if (mt5Accounts.length > 0) {
        acctId = mt5Accounts[0].account_id
        defaultMt5Account = acctId
        api.storage.local.set({ defaultMt5Account })
      }
    } catch { /* ignore */ }
  }
  if (!acctId) {
    lastAnalysisResult = {
      summary: 'No MT5 accounts found. Make sure your MT5 account is connected in TradeBot.',
      analyses: [], analyzed_at: new Date().toISOString()
    }
    api.storage.local.set({ lastAnalysisResult })
    broadcastAnalysis(lastAnalysisResult)
    return
  }
  try {
    const result = await apiFetch(`/jarvis/analyze-positions?account_id=${acctId}`)
    lastAnalysisResult = result
    api.storage.local.set({ lastAnalysisResult })
    broadcastAnalysis(result)

    if (result.summary && (onDemand || result.positions_analyzed > 0)) {
      showNotification(
        `🔍 JARVIS Analysis${onDemand ? ' (On-demand)' : ''}`,
        result.summary.slice(0, 120)
      )
      speakText(result.summary.slice(0, 400))
    }
  } catch (e) {
    const msg = `Position analysis failed: ${e.message || e}`
    lastAnalysisResult = { summary: msg, analyses: [], analyzed_at: new Date().toISOString() }
    api.storage.local.set({ lastAnalysisResult })
    broadcastAnalysis(lastAnalysisResult)
  }
}

function broadcastAnalysis(result) {
  api.runtime.sendMessage({ type: 'analysis-update', result }).catch?.(() => {})
}

// Set up / refresh the 15-minute alarm
function setupAnalysisAlarm() {
  api.alarms.get(ANALYSIS_ALARM, (alarm) => {
    if (!alarm) {
      api.alarms.create(ANALYSIS_ALARM, {
        delayInMinutes: ANALYSIS_PERIOD_MIN,
        periodInMinutes: ANALYSIS_PERIOD_MIN,
      })
    }
  })
}

api.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === ANALYSIS_ALARM && monitorEnabled) {
    runPositionAnalysis(false)
  }
})

// ── Execute Jarvis crypto command via backend ─────────────────────────────────
async function executeJarvisCommand(command, exchange) {
  // Instant notification for trade execution (before network round-trip)
  const isExecute = /(?:execute|open|place|trade|enter)\s+\w|^(?:short|long)\s+\w|go\s+(?:long|short)/i.test(command)
  if (isExecute) {
    showNotification('⚡ Executing…', command.split(';')[0].trim().slice(0, 80))
  }
  // Try new backend first, then legacy
  for (const [be, path] of [[BACKEND, '/jarvis/command'], [BACKEND_OLD, '/jarvis/command']]) {
    try {
      const result = await apiFetch(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ command, exchange: exchange || null }),
      }, be)
      showNotification(
        result.ok ? `✅ ${result.action}` : `❌ ${result.action}`,
        result.detail, !result.ok
      )
      speakText(result.speech || result.detail)
      return result
    } catch (e) {
      // Try next backend
    }
  }
  const msg = 'Command failed — both backends offline'
  showNotification('❌ JARVIS Error', msg, true)
  speakText('Sorry Sir, the command failed.')
  return { ok: false, action: 'error', detail: msg, speech: msg }
}

// ── Message router ────────────────────────────────────────────────────────────
api.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  const respond = (data) => { try { sendResponse(data) } catch { /* noop */ } }

  switch (msg && msg.type) {

    case 'notify':
      showNotification(msg.title, msg.body, msg.urgent)
      break

    case 'badge':
      setBadge(msg.text, msg.color)
      break

    case 'ping':
      respond({ ok: true })
      break

    case 'set-monitor':
      monitorEnabled = !!msg.enabled
      api.storage.local.set({ monitorEnabled })
      if (monitorEnabled) {
        scheduleNextPoll()
        setupAnalysisAlarm()
        setBadge('👁', '#8b5cf6')
      } else {
        clearTimeout(pollTimer)
        api.alarms.clear(ANALYSIS_ALARM)
        setBadge('', '#64748b')
        positionSnapshot = {}
        mt5Snapshot = {}
      }
      respond({ ok: true, monitorEnabled })
      break

    case 'set-tts':
      ttsEnabled = !!msg.enabled
      api.storage.local.set({ ttsEnabled })
      respond({ ok: true, ttsEnabled })
      break

    // v2.1: voice-learning persistence across localStorage clears
    case 'save-voice-learning':
      try { api.storage.local.set({ voiceLearning: msg.data, voiceLearningTs: Date.now() }) } catch { /* noop */ }
      respond({ ok: true })
      break
    // v2.2: relay voice frequency data to popup for mini-canvas visualization
    case 'voice-freq':
      // Forward to popup while it is open (port not available; use broadcast)
      api.runtime.sendMessage({ type: 'voice-freq-popup', bands: msg.bands, energy: msg.energy, isUserVoice: msg.isUserVoice, isSpeaking: msg.isSpeaking }).catch?.(() => {})
      break

    // v2.2: engine panel visibility state sync
    case 'engine-panel-state':
      api.storage.local.set({ enginePanelVisible: !!msg.visible })
      break

    case 'voice-freq-popup':
      // silently consumed (loop prevention if popup relays back)
      break

    case 'load-voice-learning':
      api.storage.local.get(['voiceLearning'], (res) => {
        respond({ data: res?.voiceLearning || null })
      })
      return true  // async

    case 'get-state':
      respond({
        monitorEnabled,
        ttsEnabled,
        positionCount: Object.keys(positionSnapshot).length + Object.keys(mt5Snapshot).length,
        positions: Object.values(positionSnapshot),
        lastAnalysisResult,
        lastUnifiedData,
        defaultMt5Account,
      })
      break

    case 'refresh-positions':
      // Manual refresh → sync=true pulls live MT5 balance/positions from mtapi-io
      apiFetch('/jarvis/unified-monitor?sync=true')
        .then((data) => {
          handleUnifiedUpdate(data)
          respond({ data })
        })
        .catch((e) => respond({ error: String(e) }))
      return true   // async

    case 'analyze-now':
      // On-demand analysis triggered by popup "Analyze Now" button
      runPositionAnalysis(true)
        .then(() => respond({ ok: true, result: lastAnalysisResult }))
        .catch((e) => respond({ ok: false, error: String(e) }))
      return true   // async

    case 'set-mt5-account':
      defaultMt5Account = msg.accountId || null
      api.storage.local.set({ defaultMt5Account })
      respond({ ok: true, defaultMt5Account })
      break

    case 'get-last-analysis':
      respond({ result: lastAnalysisResult })
      break

    // Clear the update banner dismiss cooldown so the banner shows again on next page load
    case 'clear-update-cooldown':
      api.storage.local.remove(['updateBannerDismissed', 'lastVersionCheck'])
      respond({ ok: true })
      break

    case 'jarvis-command':
      executeJarvisCommand(msg.command, msg.exchange)
        .then(respond)
        .catch((e) => respond({ ok: false, detail: String(e) }))
      return true   // async

    // Cost-aware Deepgram fallback: transcribe a missed clip via the backend.
    case 'deepgram-stt':
      deepgramStt(msg.audio, msg.mime)
        .then(respond)
        .catch(() => respond({ used_deepgram: false, reason: 'error' }))
      return true   // async

    // ── Face Vision updates from popup face-vision.js ─────────────────────
    // Face state is tracked so background can gate speech recognition on
    // whether the user's face is actually present and talking. It is also
    // relayed to content.js so the page-level speech recogniser can use the
    // visual "talking" signal to stay in sync with the microphone.
    case 'face-vision-update': {
      const { facePresent, isTalking, mar, identityMatch, enrolled } = msg
      faceVisionState.facePresent    = !!facePresent
      faceVisionState.isTalking      = !!isTalking
      faceVisionState.mar            = mar || 0
      faceVisionState.identityMatch  = !!identityMatch
      faceVisionState.lastUpdateMs   = Date.now()

      // Relay to the TradeBot content tabs so speech + face stay in sync.
      try {
        api.tabs.query(
          { url: ['http://localhost:3000/*', 'http://127.0.0.1:3000/*'] },
          (tabs) => {
            (tabs || []).forEach((t) => {
              api.tabs.sendMessage(t.id, {
                type: 'face-vision-state',
                facePresent, isTalking, mar, identityMatch, enrolled,
                ts: faceVisionState.lastUpdateMs,
              }).catch?.(() => {})
            })
          }
        )
      } catch { /* no tabs */ }
      break
    }

    // Popup requests current face state (e.g. to initialise UI on reopen)
    case 'get-face-state':
      respond({ ...faceVisionState })
      break

    default:
      break
  }
  return true
})

// ── Notification click → focus TradeBot ──────────────────────────────────────
api.notifications.onClicked.addListener(() => {
  api.tabs.query({ url: ['http://localhost:3000/*', 'http://127.0.0.1:3000/*'] }, (tabs) => {
    if (tabs && tabs[0]) {
      api.tabs.update(tabs[0].id, { active: true })
      if (tabs[0].windowId != null) api.windows.update(tabs[0].windowId, { focused: true })
    } else {
      api.tabs.create({ url: 'http://localhost:3000' })
    }
  })
})

api.runtime.onInstalled.addListener((details) => {
  setBadge('', '#64748b')
  // Clear any stale update-banner cooldown so it re-evaluates on next page load
  api.storage.local.remove(['updateBannerDismissed', 'lastVersionCheck'])
  const isUpdate = details.reason === 'update'
  const version = (() => { try { return api.runtime.getManifest().version } catch { return '?' } })()
  showNotification(
    isUpdate ? `JARVIS updated to v${version} ✓` : `JARVIS v${version} ready`,
    isUpdate
      ? 'Unified monitor: crypto + MT5 accounts, 15-min analysis, voice narration.'
      : 'Position monitor ready. Say "Jarvis, show positions".'
  )
  setupAnalysisAlarm()
})

api.runtime.onStartup.addListener(() => {
  api.storage.local.get(
    ['monitorEnabled', 'ttsEnabled', 'lastAnalysisResult', 'defaultMt5Account'],
    (res) => {
      monitorEnabled     = !!(res.monitorEnabled ?? true)
      ttsEnabled         = !!(res.ttsEnabled     ?? true)
      lastAnalysisResult = res.lastAnalysisResult || null
      defaultMt5Account  = res.defaultMt5Account || null
      if (monitorEnabled) {
        scheduleNextPoll()
        setupAnalysisAlarm()
      }
    }
  )
})

console.log('[JARVIS-BG] v3.0 ready — unified crypto+MT5 monitor + 15-min analysis')

// ── Memory Tree surfacing (OpenHuman-style) ───────────────────────────────────
// Poll JARVIS's Memory Tree for NEW high-importance facts (news, insights it
// learned via the 15-min auto-fetch loop) and surface them as desktop
// notifications + spoken alerts — so JARVIS proactively tells you what changed.
const MEMORY_POLL_MS   = 90_000   // 90 s
const MEMORY_MIN_IMP   = 0.72     // only genuinely important memories
const seenMemoryIds    = new Set()
let memoryPrimed       = false    // first pass just records ids (no backlog spam)

async function doMemoryPoll() {
  if (!monitorEnabled) return
  try {
    const data = await apiFetch(
      `/plugins/agent-paul/jarvis/memory/new?since_minutes=30&min_importance=${MEMORY_MIN_IMP}`
    )
    const items = (data && data.items) || []
    if (!memoryPrimed) {
      for (const it of items) seenMemoryIds.add(it.id)
      memoryPrimed = true
      return
    }
    for (const it of items) {
      if (seenMemoryIds.has(it.id)) continue
      seenMemoryIds.add(it.id)
      const title = it.symbol ? `🧠 ${coinName(it.symbol)} — new insight` : '🧠 JARVIS learned something'
      const body = (it.summary || it.title || '').slice(0, 140)
      if (body) {
        showNotification(title, body)
        speakText(body.slice(0, 220))
      }
    }
    // keep the seen-set bounded
    if (seenMemoryIds.size > 500) {
      const trim = Array.from(seenMemoryIds).slice(-300)
      seenMemoryIds.clear(); trim.forEach((id) => seenMemoryIds.add(id))
    }
  } catch { /* backend offline — stay quiet */ }
}

setInterval(doMemoryPoll, MEMORY_POLL_MS)

// ── Subconscious activity surfacing (OpenHuman heartbeat) ─────────────────────
// Poll JARVIS's subconscious activity feed and speak/notify when it makes real
// progress on a goal while you're away ("keeps thinking after you stop typing").
const ACTIVITY_POLL_MS = 120_000  // 2 min
const seenActivityIds  = new Set()
let activityPrimed     = false

async function doActivityPoll() {
  if (!monitorEnabled) return
  try {
    const data = await apiFetch('/plugins/agent-paul/jarvis/activity?limit=15')
    const items = (data && data.items) || []
    if (!activityPrimed) {
      for (const it of items) seenActivityIds.add(it.id)
      activityPrimed = true
      return
    }
    for (const it of items) {
      if (seenActivityIds.has(it.id)) continue
      seenActivityIds.add(it.id)
      // Only surface meaningful progress: goal work that acted, or approvals needed.
      const isGoalAct = it.kind === 'goal' && it.state === 'acted'
      const needsApproval = it.state === 'awaiting_approval'
      if (!isGoalAct && !needsApproval) continue
      const body = (it.summary || it.task_name || '').slice(0, 150)
      if (!body) continue
      const title = needsApproval ? '🫀 JARVIS needs your approval' : '🫀 JARVIS made progress'
      showNotification(title, body)
      speakText(body.slice(0, 200))
    }
    if (seenActivityIds.size > 400) {
      const trim = Array.from(seenActivityIds).slice(-250)
      seenActivityIds.clear(); trim.forEach((id) => seenActivityIds.add(id))
    }
  } catch { /* backend offline — stay quiet */ }
}

setInterval(doActivityPoll, ACTIVITY_POLL_MS)

