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

const BACKEND_OLD         = 'http://localhost:1448/api/v1'    // legacy crypto API
const BACKEND             = 'http://localhost:8000/api/v1'    // MT5 + unified
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
let defaultMt5Account  = null  // first configured MT5 account ID

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
  const cryptoPositions = data.crypto_positions || []
  const mt5Accounts     = data.mt5_accounts || []

  // ── Track crypto position changes ─────────────────────────────────────────
  const newCryptoSnap = {}
  for (const p of cryptoPositions) {
    newCryptoSnap[posKey(p)] = p
  }

  for (const [key, p] of Object.entries(newCryptoSnap)) {
    if (!positionSnapshot[key]) {
      showNotification(`📈 New Crypto: ${p.symbol}`,
        `${p.side.toUpperCase()} @ ${p.entry_price} | ${p.exchange.toUpperCase()}`)
      speakText(`New ${p.side} position opened on ${p.symbol} at ${p.entry_price}.`)
    }
  }
  for (const [key, prev] of Object.entries(positionSnapshot)) {
    if (!newCryptoSnap[key]) {
      const sign = (prev.pnl || 0) >= 0 ? 'PROFIT' : 'LOSS'
      showNotification(`🚪 Closed: ${prev.symbol}`,
        `${sign}: ${(prev.pnl || 0) >= 0 ? '+' : ''}${(prev.pnl || 0).toFixed(2)} USDT`, true)
      speakText(`${prev.symbol} closed. ${(prev.pnl || 0) >= 0 ? 'Profit' : 'Loss'} ${Math.abs(prev.pnl || 0).toFixed(2)}.`)
    }
  }
  for (const [key, p] of Object.entries(newCryptoSnap)) {
    const prev = positionSnapshot[key]
    if (!prev) continue
    const pctDelta = Math.abs((p.pnl_pct || 0) - (prev.pnl_pct || 0))
    const usdDelta = Math.abs((p.pnl || 0) - (prev.pnl || 0))
    if (pctDelta >= PNL_THRESHOLD_PCT || usdDelta >= PNL_THRESHOLD_USD) {
      const dir = (p.pnl || 0) >= 0 ? '▲' : '▼'
      showNotification(`${dir} ${p.symbol}: ${(p.pnl_pct || 0).toFixed(2)}%`,
        `PnL ${(p.pnl || 0) >= 0 ? '+' : ''}${(p.pnl || 0).toFixed(2)} USDT`)
      speakText(`${p.symbol} is ${(p.pnl_pct || 0) >= 0 ? 'up' : 'down'} ${Math.abs(p.pnl_pct || 0).toFixed(1)} percent.`)
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
