/*
 * JARVIS Voice Assistant — Popup UI
 *
 * Unified monitor: crypto + MT5 accounts, real-time balances, 15-min analysis,
 * on-demand analysis, voice engine visualizer in popup (not on page), face
 * vision with lip tracking, auto-update detection.
 * The displayed version is always read from manifest.json — never hardcoded.
 */

'use strict'

const api = typeof browser !== 'undefined' ? browser : chrome

// Always read from the manifest — never hardcode the version string
const INSTALLED_VERSION = (() => {
  try { return api.runtime.getManifest().version || '1.0.0' } catch { return '1.0.0' }
})()

const els = {
  dot:               document.getElementById('dot'),
  statusPill:        document.getElementById('statusPill'),
  statusText:        document.getElementById('statusText'),
  enableSwitch:      document.getElementById('enableSwitch'),
  greetingSwitch:    document.getElementById('greetingSwitch'),
  notifSwitch:       document.getElementById('notifSwitch'),
  monitorSwitch:     document.getElementById('monitorSwitch'),
  ttsSwitch:         document.getElementById('ttsSwitch'),
  transcript:        document.getElementById('transcript'),
  testNotify:        document.getElementById('testNotify'),
  openApp:           document.getElementById('openApp'),
  positionsList:     document.getElementById('positionsList'),
  posCount:          document.getElementById('posCount'),
  refreshBtn:        document.getElementById('refreshBtn'),
  // Voice engine elements
  veCanvas:          document.getElementById('veCanvas'),
  veRing:            document.getElementById('veRing'),
  veStatus:          document.getElementById('veStatus'),
  veWords:           document.getElementById('veWords'),
  enginePanelSwitch: document.getElementById('enginePanelSwitch'),
  robotModeSwitch:   document.getElementById('robotModeSwitch'),
  // Version
  versionBadge:      document.getElementById('versionBadge'),
  versionText:       document.getElementById('versionText'),
}

// Single source of truth: always display the manifest version immediately on
// load — NEVER hardcode a version string in the HTML (it drifts on every bump).
if (els.versionText) els.versionText.textContent = 'v' + INSTALLED_VERSION

const DEFAULTS = {
  enabled:        true,
  requireGreeting: false,
  notifications:  true,
  wakeWord:       'jarvis',
  lang:           'en-US',
}

let settings           = { ...DEFAULTS }
let monitorEnabled     = true
let ttsEnabled         = true
let enginePanelEnabled = false

// ── Voice Binary Engine canvas ───────────────────────────────────────────────
const veCtx = els.veCanvas ? els.veCanvas.getContext('2d') : null
let lastFreqBands = Array(16).fill(0)

function drawVeCanvas(bands, energy, isUserVoice, isSpeaking) {
  if (!veCtx) return
  const W = els.veCanvas.width, H = els.veCanvas.height
  veCtx.clearRect(0, 0, W, H)

  const BANDS = 16
  const BAR_W = Math.floor((W - BANDS) / BANDS)
  const GAP   = 2
  const CELL_H = 4, CELL_GAP = 1
  const totalCells = Math.floor((H + CELL_GAP) / (CELL_H + CELL_GAP))

  let activeColor = 'rgba(255,255,255,.05)'
  if (isSpeaking)         activeColor = '#f59e0b'
  else if (isUserVoice)   activeColor = '#06b6d4'
  else if (energy > 0.01) activeColor = '#8b5cf6'

  ;(bands || lastFreqBands).forEach((band, i) => {
    const x = i * (BAR_W + GAP) + 1
    const litCells = Math.round(band * totalCells)
    for (let c = 0; c < totalCells; c++) {
      const y = H - (c + 1) * (CELL_H + CELL_GAP) + CELL_GAP
      veCtx.fillStyle = c < litCells ? activeColor : 'rgba(255,255,255,.04)'
      veCtx.fillRect(x, y, BAR_W, CELL_H)
    }
  })

  // Update ring + status
  if (els.veRing) {
    els.veRing.className = 've-ring' + (isSpeaking ? ' speaking' : isUserVoice && energy > 0.01 ? ' match' : energy > 0.01 ? ' no-match' : '')
  }
  if (els.veStatus) {
    const label = isSpeaking ? 'SPEAKING' : isUserVoice && energy > 0.01 ? 'YOUR VOICE' : energy > 0.01 ? 'LISTENING' : 'IDLE'
    els.veStatus.textContent = label
    els.veStatus.style.color = isSpeaking ? '#f59e0b' : isUserVoice && energy > 0.01 ? '#22c55e' : energy > 0.01 ? '#06b6d4' : '#475569'
  }
}

// Draw idle state on load
drawVeCanvas(Array(16).fill(0), 0, false, false)

// ── Status polling ───────────────────────────────────────────────────────────
let statusPollTimer = null
let statusPolls     = 0
const STATUS_MAX_POLLS = 6

function applySwitch(el, on) { el.classList.toggle('on', !!on) }

function setStatus(listening, error, stalled) {
  if (els.dot) {
    els.dot.className = 'pulse-dot' + (error ? ' err' : listening ? ' on' : '')
  }
  if (els.statusPill) {
    els.statusPill.className = 'status-pill' + (error ? ' error' : listening ? ' listening' : '')
  }
  if (els.statusText) {
    els.statusText.textContent =
      error === 'mic-denied'   ? 'Microphone blocked' :
      listening                ? (settings.requireGreeting ? 'Listening for "Hey Jarvis"…' : 'Listening…') :
      !settings.enabled        ? 'Voice off' :
      stalled                  ? 'Page mic active' :
                                 'Starting…'
  }
}

function loadSettings() {
  api.storage.local.get(['settings', 'monitorEnabled', 'ttsEnabled', 'enginePanelVisible', 'voiceLearning'], (data) => {
    settings       = { ...DEFAULTS, ...(data.settings || {}) }
    monitorEnabled = !!(data.monitorEnabled ?? true)
    ttsEnabled     = !!(data.ttsEnabled     ?? true)
    enginePanelEnabled = !!(data.enginePanelVisible ?? false)

    applySwitch(els.enableSwitch,       settings.enabled)
    applySwitch(els.greetingSwitch,     settings.requireGreeting)
    applySwitch(els.notifSwitch,        settings.notifications)
    applySwitch(els.monitorSwitch,      monitorEnabled)
    applySwitch(els.ttsSwitch,          ttsEnabled)
    applySwitch(els.enginePanelSwitch,  enginePanelEnabled)

    const wordCount = data.voiceLearning ? Object.keys(data.voiceLearning).length : 0
    if (els.veWords) els.veWords.textContent = wordCount + ' words learned'

    setStatus(false)
    refreshStatus()
    loadPositionsFromBackground()
    checkVersionInPopup()
  })
}

function saveSettings() {
  api.storage.local.set({ settings })
}

function refreshStatus(repeat) {
  if (!repeat) {
    statusPolls = 0
    clearTimeout(statusPollTimer)
    statusPollTimer = null
  }
  api.tabs.query({ active: true, currentWindow: true }, (tabs) => {
    const tab = tabs && tabs[0]
    if (!tab || !tab.id) { setStatus(false); return }
    try {
      api.tabs.sendMessage(tab.id, { type: 'get-status' }, (resp) => {
        if (api.runtime.lastError) {
          if (els.statusText) els.statusText.textContent = 'Open TradeBot to start'
          if (els.dot) els.dot.className = 'pulse-dot'
          return
        }
        if (!resp) { setStatus(false); return }
        if (resp.listening) {
          clearTimeout(statusPollTimer)
          statusPollTimer = null
          setStatus(true)
          return
        }
        statusPolls++
        const stalled = settings.enabled && statusPolls >= STATUS_MAX_POLLS
        setStatus(false, null, stalled)
        if (settings.enabled && !stalled) {
          statusPollTimer = setTimeout(() => refreshStatus(true), 1000)
        }
      })
    } catch { setStatus(false) }
  })
}

// ── Version check in popup ────────────────────────────────────────────────────
function semverNewer(a, b) {
  const pa = (a || '0.0.0').split('.').map(Number)
  const pb = (b || '0.0.0').split('.').map(Number)
  for (let i = 0; i < 3; i++) {
    if ((pa[i] || 0) > (pb[i] || 0)) return true
    if ((pa[i] || 0) < (pb[i] || 0)) return false
  }
  return false
}

async function checkVersionInPopup() {
  if (els.versionText) els.versionText.textContent = 'v' + INSTALLED_VERSION
  try {
    const res = await fetch('http://localhost:1448/api/v1/jarvis/extension-version', {
      signal: AbortSignal.timeout(3000),
    })
    if (!res.ok) return
    const data = await res.json()
    const latest = data.version || INSTALLED_VERSION
    if (semverNewer(latest, INSTALLED_VERSION)) {
      if (els.versionBadge) els.versionBadge.classList.add('update-available')
      if (els.versionText)  els.versionText.textContent = `v${INSTALLED_VERSION} → v${latest}`
    }
  } catch { /* backend offline */ }
}

// Version badge click — show update instructions modal in popup
if (els.versionBadge) {
  els.versionBadge.addEventListener('click', async () => {
    try {
      const res = await fetch('http://localhost:1448/api/v1/jarvis/extension-version', {
        signal: AbortSignal.timeout(3000),
      })
      const data = res.ok ? await res.json() : {}
      const latest = data.version || INSTALLED_VERSION
      const isNew = semverNewer(latest, INSTALLED_VERSION)
      const changes = (data.changelog || []).map(c => `• ${c}`).join('\n')
      if (isNew) {
        alert(`JARVIS v${latest} is available!\n\nWhat's new:\n${changes}\n\nTo update:\n1. The files are already updated\n2. Open chrome://extensions\n3. Click ↺ Reload on JARVIS`)
      } else {
        alert(`JARVIS v${INSTALLED_VERSION} — you're up to date! ✓`)
      }
    } catch {
      alert(`JARVIS v${INSTALLED_VERSION}`)
    }
  })
}

// ── Positions rendering ──────────────────────────────────────────────────────
function renderPositions(positions) {
  const list = els.positionsList
  const n    = (positions || []).length

  els.posCount.textContent = String(n)
  els.posCount.className   = n > 0 ? 'count-chip' : ''

  if (n === 0) {
    list.innerHTML = '<div class="pos-empty">No open positions</div>'
    return
  }

  list.innerHTML = positions.map((p) => {
    const profit    = p.pnl >= 0
    const pnlSign   = profit ? '+' : ''
    const pnlClass  = profit ? 'pos' : 'neg'
    const cardClass = profit ? 'pos-card profit' : 'pos-card loss'
    const sideClass = (p.side || 'long').toLowerCase() === 'long' ? 'long' : 'short'

    const lev = p.leverage ? ` ${p.leverage}x` : ''
    const mm  = p.margin_mode ? ` ${p.margin_mode}` : ''
    const entry = Number(p.entry_price).toPrecision(6)
    const mark  = Number(p.mark_price).toPrecision(6)
    const pnl   = Number(p.pnl).toFixed(2)
    const pct   = Number(p.pnl_pct).toFixed(2)

    return `
      <div class="${cardClass}">
        <div class="pos-row">
          <span class="pos-symbol">${escapeHtml(p.symbol)}</span>
          <span class="pos-side ${sideClass}">${sideClass.toUpperCase()}${lev}</span>
        </div>
        <div class="pos-meta">entry ${entry} → mark ${mark}${mm}</div>
        <div class="pos-pnl ${pnlClass}">${pnlSign}${pnl} USDT (${pct}%)</div>
        <div class="pos-ex">${escapeHtml(p.exchange).toUpperCase()}</div>
      </div>`
  }).join('')
}

const BACKEND_URL = 'http://127.0.0.1:1448/api/v1'

// Direct fetch from backend — bypasses the background service worker so MV3
// restart races can never leave the popup stuck at "Loading accounts…".
// The popup extension page is whitelisted in the manifest connect-src.
async function fetchUnifiedMonitor(sync = false) {
  const url = `${BACKEND_URL}/jarvis/unified-monitor${sync ? '?sync=true' : ''}`
  const resp = await fetch(url, { signal: AbortSignal.timeout(8000) })
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
  return resp.json()
}

function loadPositionsFromBackground() {
  // Step 1: sync settings/switches/analysis from background service worker
  // (lightweight — no backend fetch, returns instantly from SW memory).
  api.runtime.sendMessage({ type: 'get-state' }, (resp) => {
    if (api.runtime.lastError || !resp) return
    monitorEnabled = !!resp.monitorEnabled
    ttsEnabled     = !!resp.ttsEnabled
    applySwitch(els.monitorSwitch, monitorEnabled)
    applySwitch(els.ttsSwitch,     ttsEnabled)
    if (resp.lastAnalysisResult) renderAnalysis(resp.lastAnalysisResult)
    // Use cached unified data if available (instant, no network)
    if (resp.lastUnifiedData) renderMonitorData(resp.lastUnifiedData)
  })
  // Step 2: fetch live account data DIRECTLY from backend (bypasses SW so MV3
  // service-worker restarts can never block or drop this response).
  fetchUnifiedMonitor(false)
    .then(data => renderMonitorData(data))
    .catch(() => {
      // Backend offline or slow — try once more via the SW path as fallback
      api.runtime.sendMessage({ type: 'refresh-positions' }, (resp) => {
        if (api.runtime.lastError || !resp) return
        if (resp.data) renderMonitorData(resp.data)
      })
    })
}

function refreshPositions() {
  els.refreshBtn.textContent = '…'
  // Direct fetch with sync=true (live MT5 balance pull) — bypasses the SW
  fetchUnifiedMonitor(true)
    .then(data => { els.refreshBtn.textContent = '⟳'; renderMonitorData(data) })
    .catch(() => {
      // Fallback to SW path
      api.runtime.sendMessage({ type: 'refresh-positions' }, (resp) => {
        els.refreshBtn.textContent = '⟳'
        if (api.runtime.lastError || !resp) return
        if (resp.data) renderMonitorData(resp.data)
      })
    })
}

// ── Unified monitor rendering ────────────────────────────────────────────────

function renderMonitorData(data) {
  // Update account balance bar
  const balanceEl = document.getElementById('accountBalances')
  if (balanceEl && data) {
    let balHtml = ''
    // Crypto
    if (data.crypto_total_pnl !== undefined) {
      const cpnl = Number(data.crypto_total_pnl || 0).toFixed(2)
      const cpnlClass = data.crypto_total_pnl >= 0 ? 'pos' : 'neg'
      const cpnlSign = data.crypto_total_pnl >= 0 ? '+' : ''
      balHtml += `<div class="bal-row">
        <span class="bal-label">Crypto PnL</span>
        <span class="bal-value ${cpnlClass}">${cpnlSign}${cpnl} USDT</span>
      </div>`
    }
    // MT5 accounts
    for (const acct of (data.mt5_accounts || [])) {
      const eq = Number(acct.equity || 0).toFixed(2)
      const fpnl = Number(acct.floating_pnl || 0).toFixed(2)
      const fpnlClass = acct.floating_pnl >= 0 ? 'pos' : 'neg'
      const fpnlSign = acct.floating_pnl >= 0 ? '+' : ''
      balHtml += `<div class="bal-row">
        <span class="bal-label">MT5 ${escapeHtml(acct.name || acct.login)}</span>
        <span class="bal-value">${acct.currency} ${eq}</span>
        <span class="bal-fpnl ${fpnlClass}">${fpnlSign}${fpnl}</span>
      </div>`
    }
    balanceEl.innerHTML = balHtml || '<div class="pos-empty" style="padding:4px">No accounts connected</div>'
  }

  // Tabs: crypto vs MT5
  const activeTab = document.querySelector('.pos-tab.active')
  const tabName = activeTab ? activeTab.dataset.tab : 'crypto'
  if (tabName === 'mt5') {
    const mt5Pos = (data.mt5_accounts || []).flatMap(a =>
      (a.positions || []).map(p => ({ ...p, account_name: a.name, currency: a.currency }))
    )
    renderMt5Positions(mt5Pos)
  } else {
    renderPositions(data.crypto_positions || [])
  }

  const totalCount = (data.total_position_count || 0)
  if (els.posCount) {
    els.posCount.textContent = String(totalCount)
    els.posCount.className = totalCount > 0 ? 'count-chip' : ''
  }
}

function renderMt5Positions(positions) {
  const list = els.positionsList
  if (!list) return
  if (!positions || positions.length === 0) {
    list.innerHTML = '<div class="pos-empty">No open MT5 positions</div>'
    return
  }
  list.innerHTML = positions.map((p) => {
    const profit = (p.profit || 0)
    const profitClass = profit >= 0 ? 'pos' : 'neg'
    const profitSign  = profit >= 0 ? '+' : ''
    const cardClass   = profit >= 0 ? 'pos-card profit' : 'pos-card loss'
    const sideClass   = (p.side || 'buy').toLowerCase() === 'buy' ? 'long' : 'short'
    return `
      <div class="${cardClass}">
        <div class="pos-row">
          <span class="pos-symbol">${escapeHtml(p.symbol)}</span>
          <span class="pos-side ${sideClass}">${sideClass.toUpperCase()}</span>
        </div>
        <div class="pos-meta">${(p.volume||0)} lot · open ${Number(p.price_open||0).toFixed(2)} → ${Number(p.price_current||0).toFixed(2)}</div>
        <div class="pos-pnl ${profitClass}">${profitSign}${profit.toFixed(2)} ${escapeHtml(p.currency||'')}</div>
        <div class="pos-ex">${escapeHtml(p.account_name || '')} · MT5</div>
      </div>`
  }).join('')
}

// ── Analysis panel rendering ─────────────────────────────────────────────────
function renderAnalysis(result) {
  const lastTime = document.getElementById('analysisLastTime')
  const analysisList = document.getElementById('analysisList')
  if (!analysisList) return

  if (!result || (!result.analyses?.length && !result.summary)) {
    analysisList.innerHTML = '<div class="pos-empty">No analysis available. Click "🔍 Analyze Now".</div>'
    return
  }

  if (lastTime && result.analyzed_at) {
    const d = new Date(result.analyzed_at)
    lastTime.textContent = d.toLocaleTimeString()
  }

  if (!result.analyses || result.analyses.length === 0) {
    analysisList.innerHTML = `<div class="analysis-summary">${escapeHtml(result.summary || '')}</div>`
    return
  }

  analysisList.innerHTML = result.analyses.map((a) => {
    const hasSug = a.has_suggestion
    const badge = hasSug ? `<span class="analysis-badge suggest">💡 Suggestion</span>` : ''
    const tp = a.tp_suggestion ? `TP: ${Number(a.tp_suggestion).toFixed(2)}` : ''
    const sl = a.sl_suggestion ? `SL: ${Number(a.sl_suggestion).toFixed(2)}` : ''
    const levels = [tp, sl].filter(Boolean).join(' · ')
    return `
      <div class="analysis-card">
        <div class="pos-row">
          <span class="pos-symbol" style="font-size:11px">${escapeHtml(a.symbol)}</span>
          <div style="display:flex;gap:4px;align-items:center">
            <span class="pos-side ${a.side === 'buy' ? 'long' : 'short'}">${(a.side || '').toUpperCase()}</span>
            ${badge}
          </div>
        </div>
        <div class="analysis-text">${escapeHtml(a.analysis_text || '')}</div>
        ${levels ? `<div class="analysis-levels">${escapeHtml(levels)}</div>` : ''}
        ${a.ai_verdict ? `<div class="analysis-verdict">🤖 ${escapeHtml(a.ai_verdict)}</div>` : ''}
      </div>`
  }).join('')
}

// ── Toggle handlers ──────────────────────────────────────────────────────────
els.enableSwitch.addEventListener('click', () => {
  settings.enabled = !settings.enabled
  applySwitch(els.enableSwitch, settings.enabled)
  saveSettings()
  setStatus(settings.enabled)
  api.tabs.query({ active: true, currentWindow: true }, (tabs) => {
    if (tabs && tabs[0] && tabs[0].id) {
      api.tabs.sendMessage(tabs[0].id, { type: 'toggle', enabled: settings.enabled }, () => {
        if (api.runtime.lastError) { /* tab not a tradebot page */ }
        setTimeout(refreshStatus, 400)
      })
    }
  })
})

els.greetingSwitch.addEventListener('click', () => {
  settings.requireGreeting = !settings.requireGreeting
  applySwitch(els.greetingSwitch, settings.requireGreeting)
  saveSettings()
  refreshStatus()
})

els.notifSwitch.addEventListener('click', () => {
  settings.notifications = !settings.notifications
  applySwitch(els.notifSwitch, settings.notifications)
  saveSettings()
})

els.monitorSwitch.addEventListener('click', () => {
  monitorEnabled = !monitorEnabled
  applySwitch(els.monitorSwitch, monitorEnabled)
  api.runtime.sendMessage({ type: 'set-monitor', enabled: monitorEnabled }, () => {
    if (!monitorEnabled) {
      els.positionsList.innerHTML = '<div class="pos-empty">Monitor off</div>'
      els.posCount.textContent = '0'
    }
  })
})

els.ttsSwitch.addEventListener('click', () => {
  ttsEnabled = !ttsEnabled
  applySwitch(els.ttsSwitch, ttsEnabled)
  api.runtime.sendMessage({ type: 'set-tts', enabled: ttsEnabled })
})

// Engine panel toggle — show/hide the in-page floating binary engine overlay
// ── Robot mode toggle ───────────────────────────────────────────────────────
// When ON: the 3D JARVIS robot owns the mic + speaker. The extension and chat
// recognizers/TTS stand down so only the robot listens and responds.
let robotModeEnabled = false
function applyRobotMode(on, save) {
  robotModeEnabled = on
  if (els.robotModeSwitch) applySwitch(els.robotModeSwitch, on)
  if (save) api.storage.local.set({ robotMode: on })
  // Tell every TradeBot tab to enter/exit robot mode
  api.tabs.query({ url: ['http://localhost:3000/*', 'http://127.0.0.1:3000/*'] }, (tabs) => {
    for (const tab of (tabs || [])) {
      api.tabs.sendMessage(tab.id, { type: 'set-robot-mode', enabled: on }).catch?.(() => {})
    }
  })
}
if (els.robotModeSwitch) {
  els.robotModeSwitch.addEventListener('click', () => applyRobotMode(!robotModeEnabled, true))
  api.storage.local.get(['robotMode'], (d) => applyRobotMode(!!(d && d.robotMode), false))
}

els.testNotify.addEventListener('click', () => {
  api.tabs.query({ active: true, currentWindow: true }, (tabs) => {
    if (tabs && tabs[0] && tabs[0].id) {
      api.tabs.sendMessage(tabs[0].id, { type: 'test-notify' }, () => {
        if (api.runtime.lastError) {
          api.runtime.sendMessage({ type: 'notify', title: 'JARVIS Test', body: 'Desktop notifications are working, Sir.' })
        }
      })
    } else {
      api.runtime.sendMessage({ type: 'notify', title: 'JARVIS Test', body: 'Desktop notifications are working, Sir.' })
    }
  })
})

els.openApp.addEventListener('click', () => {
  api.tabs.create({ url: 'http://localhost:3000' })
})

els.refreshBtn.addEventListener('click', refreshPositions)

// ── Analyze Now button ───────────────────────────────────────────────────────
const analyzeNowBtn = document.getElementById('analyzeNowBtn')
if (analyzeNowBtn) {
  analyzeNowBtn.addEventListener('click', () => {
    analyzeNowBtn.textContent = '⏳ Analyzing…'
    analyzeNowBtn.disabled = true
    api.runtime.sendMessage({ type: 'analyze-now' }, (resp) => {
      analyzeNowBtn.textContent = '🔍 Analyze Now'
      analyzeNowBtn.disabled = false
      if (api.runtime.lastError || !resp) return
      if (resp.result) renderAnalysis(resp.result)
    })
  })
}

// ── Position tab switching ────────────────────────────────────────────────────
document.querySelectorAll('.pos-tab').forEach((tab) => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.pos-tab').forEach(t => t.classList.remove('active'))
    tab.classList.add('active')
    // Re-render with current data
    api.runtime.sendMessage({ type: 'get-state' }, (resp) => {
      if (!resp?.data) return
      const tabName = tab.dataset.tab
      if (tabName === 'mt5') {
        const mt5Pos = (resp.data.mt5_accounts || []).flatMap(a =>
          (a.positions || []).map(p => ({ ...p, account_name: a.name, currency: a.currency }))
        )
        renderMt5Positions(mt5Pos)
      } else {
        renderPositions(resp.data.crypto_positions || [])
      }
    })
  })
})

// ── Live transcript from content script ──────────────────────────────────────
api.runtime.onMessage.addListener((msg) => {
  if (msg.type === 'transcript') {
    els.transcript.innerHTML = msg.final
      ? `<span class="final">${escapeHtml(msg.text)}</span>`
      : escapeHtml(msg.text)
  }
  if (msg.type === 'positions-update') {
    renderPositions(msg.positions || [])
  }
  // v3.0: unified monitor update from background
  if (msg.type === 'monitor-update' && msg.data) {
    renderMonitorData(msg.data)
  }
  // v3.0: position analysis update from background (15-min alarm or on-demand)
  if (msg.type === 'analysis-update' && msg.result) {
    renderAnalysis(msg.result)
  }
  // v2.2: real-time voice frequency data from content script (via background relay)
  if (msg.type === 'voice-freq-popup') {
    lastFreqBands = msg.bands || lastFreqBands
    drawVeCanvas(msg.bands, msg.energy || 0, !!msg.isUserVoice, !!msg.isSpeaking)
    // Update learned-words count if available
    api.storage.local.get(['voiceLearning'], (data) => {
      const wordCount = data.voiceLearning ? Object.keys(data.voiceLearning).length : 0
      if (els.veWords) els.veWords.textContent = wordCount + ' words learned'
    })
  }
})

function escapeHtml(s) {
  return String(s || '').replace(/[&<>"']/g,
    (c) => ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;' }[c]))
}

loadSettings()

// ── Avatar picker ──────────────────────────────────────────────────────────
const AVATAR_NAMES = { cyan:'Cyan', purple:'Purple', gold:'Gold', crimson:'Crimson', emerald:'Emerald' }
function applyAvatarSelection(style) {
  document.querySelectorAll('.avatar-chip').forEach(chip => {
    chip.classList.toggle('active', chip.dataset.style === style)
  })
  const nameEl = document.getElementById('avatarName')
  if (nameEl) nameEl.textContent = AVATAR_NAMES[style] || 'Cyan'
}
// Restore saved avatar
api.storage.local.get(['avatarStyle'], (data) => {
  applyAvatarSelection((data && data.avatarStyle) || 'cyan')
})
// Wire chip clicks → save to storage (content.js relays it to the page robot)
document.querySelectorAll('.avatar-chip').forEach(chip => {
  chip.addEventListener('click', () => {
    const style = chip.dataset.style
    applyAvatarSelection(style)
    api.storage.local.set({ avatarStyle: style })
    // Also push directly to any open TradeBot tabs for instant feedback
    api.tabs.query({ url: ['http://localhost:3000/*', 'http://127.0.0.1:3000/*'] }, (tabs) => {
      for (const tab of (tabs || [])) {
        api.tabs.sendMessage(tab.id, { type: 'set-avatar', style }).catch?.(() => {})
      }
    })
  })
})


;(async function checkHeadroomProxy() {
  const dot   = document.getElementById('headroomDot')
  const label = document.getElementById('headroomLabel')
  if (!dot || !label) return
  try {
    const resp = await fetch('http://127.0.0.1:8787/health', { signal: AbortSignal.timeout(2000) })
    if (resp.ok) {
      const data = await resp.json().catch(() => ({}))
      dot.classList.remove('off')
      label.classList.remove('off')
      label.textContent = 'headroom proxy active' + (data.version ? ' · v' + data.version : '')
    } else {
      throw new Error('non-ok')
    }
  } catch {
    dot.classList.add('off')
    label.textContent = 'headroom proxy offline'
  }
})()

// Auto-refresh every 10 s while popup is open.
// Direct backend fetch keeps account balances current regardless of whether the
// background service worker has polled recently.
setInterval(() => {
  // Keep settings/switches/analysis in sync from SW (cheap, no backend)
  api.runtime.sendMessage({ type: 'get-state' }, (resp) => {
    if (!resp) return
    if (resp.monitorEnabled !== undefined) {
      monitorEnabled = !!resp.monitorEnabled
      ttsEnabled = !!resp.ttsEnabled
      applySwitch(els.monitorSwitch, monitorEnabled)
      applySwitch(els.ttsSwitch, ttsEnabled)
    }
    if (resp.lastAnalysisResult) renderAnalysis(resp.lastAnalysisResult)
  })
  // Refresh account data directly from backend
  fetchUnifiedMonitor(false)
    .then(data => renderMonitorData(data))
    .catch(() => { /* backend offline — keep existing display */ })
}, 10_000)

// ─────────────────────────────────────────────────────────────────────────────
//  Face Vision Integration
//
//  The camera runs in a full extension TAB (camera.html) or in the JARVIS Room
//  page — both are stable origins where the browser reliably prompts for camera
//  permission. MV3 popups cannot prompt, so the popup only OPENS those surfaces
//  and shows the live face status relayed through background.js.
// ─────────────────────────────────────────────────────────────────────────────
;(function initFaceVision () {
  const fvSwitch      = document.getElementById('faceVisionSwitch')
  const fvBody        = document.getElementById('fvBody')
  const fvOpenCamera  = document.getElementById('fvOpenCameraBtn')
  const fvOpenRoom    = document.getElementById('fvOpenRoomBtn')
  const fvMarBar      = document.getElementById('fvMarBar')
  const fvMarVal      = document.getElementById('fvMarVal')
  const fvSpeakVal    = document.getElementById('fvSpeakVal')
  const fvIdentityVal = document.getElementById('fvIdentityVal')
  const fvBackendVal  = document.getElementById('fvBackendVal')
  const fvWsDot       = document.getElementById('fvWsDot')
  const fvTalking     = document.getElementById('fvTalkingBadge')

  if (!fvSwitch) return

  let fvEnabled = false
  let statusTimer = null

  // ── Persist on/off across popup opens ──────────────────────────────────
  api.storage.local.get(['faceVisionEnabled'], (r) => {
    fvEnabled = !!r.faceVisionEnabled
    applySwitch(fvSwitch, fvEnabled)   // reflect the saved state on the switch
    applyState(fvEnabled)
  })

  applySwitch(fvSwitch, fvEnabled)

  fvSwitch.addEventListener('click', () => {
    fvEnabled = !fvEnabled
    applySwitch(fvSwitch, fvEnabled)
    api.storage.local.set({ faceVisionEnabled: fvEnabled })
    applyState(fvEnabled)
    // Turning the feature OFF stops any running camera (extension tab / room).
    if (!fvEnabled) {
      try { api.runtime.sendMessage({ type: 'face-camera-stop' }).catch?.(() => {}) } catch { /* noop */ }
    }
  })

  function applyState (on) {
    fvBody.style.display = on ? 'block' : 'none'
    clearInterval(statusTimer)
    statusTimer = null
    if (on) {
      pollStatus()
      statusTimer = setInterval(pollStatus, 400)  // live relayed status
    } else {
      fvTalking.classList.remove('visible')
      renderOffline()
    }
  }

  // ── Open the camera surfaces ───────────────────────────────────────────
  fvOpenCamera.addEventListener('click', () => {
    try { api.tabs.create({ url: api.runtime.getURL('camera.html') }) } catch (e) {
      console.warn('open camera tab failed', e)
    }
  })
  fvOpenRoom.addEventListener('click', () => {
    api.tabs.query({ url: ['http://localhost:3000/*', 'http://127.0.0.1:3000/*'] }, (tabs) => {
      const roomUrl = 'http://localhost:3000/jarvis-room'
      if (tabs && tabs[0]) {
        api.tabs.update(tabs[0].id, { active: true, url: roomUrl })
        if (tabs[0].windowId != null) api.windows.update(tabs[0].windowId, { focused: true })
      } else {
        api.tabs.create({ url: roomUrl })
      }
    })
  })

  // ── Poll live face status from background (relayed from camera tab/room) ─
  function pollStatus () {
    api.runtime.sendMessage({ type: 'get-face-state' }, (s) => {
      if (api.runtime.lastError || !s) { renderOffline(); return }
      const fresh = s.lastUpdateMs && (Date.now() - s.lastUpdateMs) < 2000
      if (!fresh) { renderOffline(); return }

      // WS / camera dot
      fvWsDot.classList.add('on')
      fvBackendVal.textContent = 'live'
      fvBackendVal.className   = 'fv-stat-value ok'

      // MAR bar
      const mar = s.mar || 0
      const pct = Math.min(mar / 0.65, 1) * 100
      fvMarBar.style.width      = pct + '%'
      fvMarBar.style.background = s.isTalking ? '#22c55e' : '#06b6d4'
      fvMarVal.textContent      = mar.toFixed(3)
      fvMarVal.className        = 'fv-stat-value ' + (s.isTalking ? 'ok' : 'cyan')

      // Talking
      fvSpeakVal.textContent = s.isTalking ? 'TALKING' : 'silent'
      fvSpeakVal.className   = 'fv-stat-value ' + (s.isTalking ? 'ok' : 'dim')
      fvTalking.classList.toggle('visible', !!s.isTalking)

      // Identity
      if (!s.facePresent) {
        fvIdentityVal.textContent = 'no face'; fvIdentityVal.className = 'fv-stat-value dim'
      } else if (s.identityMatch) {
        fvIdentityVal.textContent = '✓ YOU';   fvIdentityVal.className = 'fv-stat-value ok'
      } else {
        fvIdentityVal.textContent = '? unknown'; fvIdentityVal.className = 'fv-stat-value warn'
      }
    })
  }

  function renderOffline () {
    fvWsDot.classList.remove('on')
    fvBackendVal.textContent = 'off'
    fvBackendVal.className   = 'fv-stat-value dim'
    fvSpeakVal.textContent   = 'silent'
    fvSpeakVal.className      = 'fv-stat-value dim'
    fvTalking.classList.remove('visible')
    fvMarBar.style.width = '0%'
    fvMarVal.textContent = '0.00'
    fvMarVal.className    = 'fv-stat-value dim'
    fvIdentityVal.textContent = '—'
    fvIdentityVal.className    = 'fv-stat-value dim'
  }
})()

