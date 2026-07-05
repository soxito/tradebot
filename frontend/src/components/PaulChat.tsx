/**
 * PaulChat — JARVIS floating assistant widget
 *
 * Mounts once inside Layout.tsx; available on every page.
 * Opens as a slide-up panel over existing content.
 * Streams responses via SSE (Server-Sent Events).
 * Shows MT5 position alerts as dismissible cards.
 */
import { useCallback, useEffect, useRef, useState, memo } from 'react'
import { useRouter } from 'next/router'
import dynamic from 'next/dynamic'
import { apiClient } from '@/services/api'
import { interpretVoiceCommand, phoneticWakeMatch, stripWakePhrase, type VoiceAction } from '@/utils/voiceCommands'
import {
  Bot, X, Send, Minimize2, Bell, Trash2, ChevronDown,
  AlertTriangle, TrendingUp, TrendingDown, Zap,
  Mic, MicOff, Volume2, VolumeX, Ear, Settings, Play,
} from 'lucide-react'
import type { RobotState, AvatarStyle } from './JarvisRobot'
import { detectStaticTier } from '@/utils/devicePerformance'

// 3D robot avatar — loaded client-side only (Three.js needs the DOM/WebGL).
const JarvisRobotAvatar = dynamic(() => import('./JarvisRobotAvatar'), { ssr: false })

// A ranked SMC "Sniper" setup, rendered as an action card with a one-tap Execute
// button. Mirrors the fields needed to display the setup and to place the order
// via apiClient.mt5.smcPlace (same pending-limit + TP path as the chart UI).
interface SniperSetupAction {
  side: 'buy' | 'sell'
  entry: number
  stop_loss: number
  take_profit: number
  rr?: number
  confidence?: number
  zone_kind?: string
  volume?: number     // from signal.lot, fallback 0.01
  pointSize?: number  // signal.point_size — used to pick price decimals for display
}

// True when the user is asking JARVIS to analyse / show SMC "Sniper" setups.
// Requires the word "sniper" plus an action word so normal chat is never hijacked.
function isSniperIntent(text: string): boolean {
  const s = text.toLowerCase()
  return /\bsniper\b/.test(s)
    && /(setup|entr|analy|signal|scan|read|trade|idea|opportunit)/.test(s)
}

// True when the user is issuing a direct Bitget crypto trade command or asking
// for market analysis — these MUST be routed to the real /jarvis/command backend
// endpoint and NEVER to the AI chat (which would hallucinate fake order IDs).
function isBitgetCommand(text: string): boolean {
  const s = text.toLowerCase()
  return (
    // Direct execute: "execute SOLUSDT long 5 lot at 68.2; set SL 67.11; TP1 74.873"
    /(?:execute|open|place|trade|enter)\s+\w{3,15}\s+(?:long|short|buy|sell)/.test(s) ||
    // Side-first: "short BTCUSDT 2 at 65000"
    /^(?:long|short|buy|sell)\s+\w{3,15}\s+[\d$]/.test(s) ||
    // Go long/short: "go long SOLUSDT"
    /go\s+(?:long|short)(?:\s+on)?\s+\w{3,15}/.test(s) ||
    // Monitor / analysis: "monitor SOL", "analyze BTCUSDT", "find buy entries"
    /(?:monitor|watch|analyze|analyse|scan|sniper?|check)\s+\w{2,12}/.test(s) ||
    /find\s+(?:(?:more|a|some)\s+)?(?:buy|sell|long|short)\s+entr(?:y|ies)/.test(s) ||
    // Position management: "close GWEIUSDT", "how is SOLUSDT doing", "set SL..."
    /close(?:\s+my)?\s+[a-z]{3,15}(?:usdt?)?(?:\s+position)?/.test(s) ||
    /how\s+is\s+\w{3,15}(?:\s+doing)?/.test(s) ||
    /(?:set\s+)?(?:tp|take[\s-]profit)\s+at\s+[\d.]/.test(s) ||
    /set\s+(?:stop[\s-]loss|sl)\s+at/.test(s) ||
    // Show positions
    /(?:show|list|what(?:\s+are)?|get)\s+(?:my\s+)?(?:open\s+)?positions?/.test(s)
  )
}

// True when the user is telling JARVIS to EXECUTE / PLACE the limits or orders
// that were produced by the most recent sniper analysis.
function isExecuteSetupIntent(text: string): boolean {
  const s = text.toLowerCase()
  // Must have an execute-like verb + a reference to "limit", "order", "setup",
  // or "trade" — and NOT also trigger isSniperIntent (which analyses, not places).
  return /(execut|place|send|submit|put\s+in|fire\s+off|go\s+ahead|do\s+it)\b/.test(s)
    && /(limit|order|setup|trade|position|entry|entries|them|it)\b/.test(s)
    && !/(analy|scan|read|check|show|what|look)/.test(s)
}

// Parse a timeframe from natural language — e.g. "1h", "4 hour", "daily",
// "15 minute", "M15", "H4" — and normalise to the MT5 timeframe string (H1, etc).
// Returns 'H1' (60-minute chart) when no recognisable timeframe is present.
function extractTimeframe(text: string): string {
  const s = text.toLowerCase()
  // Explicit MT5 labels first (case-insensitive)
  const explicit = s.match(/\b(m1|m5|m15|m30|h1|h4|d1|w1)\b/i)
  if (explicit) return explicit[1].toUpperCase()
  // Numeric + unit patterns: "1 hour", "4h", "15 min", "30 minute"
  const numUnit = s.match(/\b(\d+)\s*(m(?:in(?:ute)?)?|h(?:our)?|d(?:ay)?|w(?:eek)?)/)
  if (numUnit) {
    const n = parseInt(numUnit[1], 10)
    const u = numUnit[2][0]
    if (u === 'w') return 'W1'
    if (u === 'd') return 'D1'
    if (u === 'h') {
      if (n === 1) return 'H1'
      if (n === 4) return 'H4'
      return 'H1'
    }
    if (u === 'm') {
      if (n <= 1)  return 'M1'
      if (n <= 5)  return 'M5'
      if (n <= 15) return 'M15'
      if (n <= 30) return 'M30'
    }
  }
  // Named patterns
  if (/\bdaily\b/.test(s) || /\b1\s*day\b/.test(s))       return 'D1'
  if (/\b4\s*hour/.test(s)  || /\bfour.hour/.test(s))     return 'H4'
  if (/\b1\s*hour/.test(s)  || /\bone.hour/.test(s))      return 'H1'
  if (/\bhourly\b/.test(s))                                return 'H1'
  if (/\b30\s*min/.test(s)  || /\bthirty.min/.test(s))    return 'M30'
  if (/\b15\s*min/.test(s)  || /\bfifteen.min/.test(s))   return 'M15'
  if (/\b5\s*min/.test(s)   || /\bfive.min/.test(s))      return 'M5'
  if (/\b1\s*min/.test(s)   || /\bone.min/.test(s))       return 'M1'
  if (/\bweekly\b/.test(s))                                return 'W1'
  return 'H1' // sensible default
}

// Format a price with sensible decimals: 5 for sub-unit FX (point < 0.001 or
// |price| < 20), else 2 — mirrors the sniper chart's display rule.
function formatSniperPrice(n: number, pointSize?: number): string {
  if (!isFinite(n)) return String(n)
  const dec = pointSize != null ? (pointSize < 0.001 ? 5 : 2) : (Math.abs(n) < 20 ? 5 : 2)
  return n.toFixed(dec)
}

interface Message {
  id: string
  role: 'user' | 'assistant'
  content: string
  pending?: boolean
  fromHistory?: boolean  // loaded from server history on mount — must NOT be auto-spoken
  sniperSetups?: SniperSetupAction[]  // ranked sniper setups → rendered as Execute cards
}

// ── Conversation persistence (survives page refresh) ──────────────────────
// The streaming chat persists to the backend, but locally-added messages
// (analyse cards, sniper setups, voice confirmations, errors) never reach the
// server. Mirroring the full visible conversation to localStorage guarantees a
// refresh restores the exact screen. Backend history stays the fallback for a
// fresh browser that has no local copy yet.
function chatStoreKey(): string {
  let sk = 'default'
  try { sk = localStorage.getItem('paul.session') || 'default' } catch { /* ignore */ }
  return `paul.chat.${sk}`
}

function loadStoredMessages(): Message[] | null {
  if (typeof window === 'undefined') return null
  try {
    const raw = localStorage.getItem(chatStoreKey())
    if (!raw) return null
    const arr = JSON.parse(raw)
    if (!Array.isArray(arr) || arr.length === 0) return null
    return arr as Message[]
  } catch { return null }
}

function saveStoredMessages(msgs: Message[]): void {
  if (typeof window === 'undefined') return
  try {
    const clean = msgs
      // Drop the stock welcome and any empty "⚡ Thinking…" bubble so a refresh
      // never restores a stuck placeholder.
      .filter(m => m.id !== 'welcome' && !(m.pending && !m.content))
      // Strip transient streaming state; flag restored msgs so TTS won't re-speak.
      .map(({ pending: _pending, ...rest }) => ({ ...rest, fromHistory: true }))
    if (clean.length === 0) { localStorage.removeItem(chatStoreKey()); return }
    localStorage.setItem(chatStoreKey(), JSON.stringify(clean.slice(-300)))
  } catch { /* quota exceeded — ignore */ }
}

interface Alert {
  id: string
  type: string
  message: string
  ts: number
  read: boolean
}

function alertIcon(type: string) {
  if (type === 'new_trade') return <TrendingUp className="w-3.5 h-3.5 text-green-400 shrink-0" />
  if (type === 'trade_closed') return <TrendingDown className="w-3.5 h-3.5 text-gray-400 shrink-0" />
  if (type === 'sl_approach') return <AlertTriangle className="w-3.5 h-3.5 text-red-400 shrink-0" />
  if (type === 'tp_approach') return <Zap className="w-3.5 h-3.5 text-green-400 shrink-0" />
  return <Bell className="w-3.5 h-3.5 text-amber-400 shrink-0" />
}

function nanoid() { return Math.random().toString(36).slice(2) }

// ── Voice profile / speaker identification ────────────────────────────────────
// Uses Web Audio API to build a frequency-band fingerprint of the user's voice.
// Only speech matching this fingerprint is accepted, cancelling out TV, AC, etc.

const VOICE_PROFILE_KEY = 'paul.voiceProfile.v2'
const AUDIO_BANDS = 12  // frequency band buckets for the fingerprint

interface VoiceProfile {
  bands: number[]          // average energy per band (normalised 0–1)
  bandStdDev?: number[]    // std deviation per band — natural voice variation
  centroid: number         // spectral centroid (0–1)
  minEnergy: number        // minimum energy threshold
  calibratedAt: number
}

function loadVoiceProfile(): VoiceProfile | null {
  if (typeof window === 'undefined') return null
  try { const s = localStorage.getItem(VOICE_PROFILE_KEY); return s ? JSON.parse(s) : null }
  catch { return null }
}
function saveVoiceProfile(p: VoiceProfile) {
  if (typeof window === 'undefined') return
  try { localStorage.setItem(VOICE_PROFILE_KEY, JSON.stringify(p)) } catch { /* ignore */ }
}
function deleteVoiceProfile() {
  if (typeof window === 'undefined') return
  try { localStorage.removeItem(VOICE_PROFILE_KEY) } catch { /* ignore */ }
}

/**
 * Statistical band-distance matching for speaker identification.
 * Each frequency band is checked: is the current value within N standard
 * deviations of the calibrated profile mean?
 * Returns 0–1 where 1 = every band exactly matches the profile.
 *
 * MUCH more robust than cosine similarity for rejecting TV/background voices
 * because it measures per-band deviation scaled to that person’s natural
 * voice variation, making it unique to the calibrated speaker.
 */
function voiceSimilarity(current: number[], profile: VoiceProfile): number {
  // Fall back to a conservative tolerance if calibrated without std dev
  const stdDev = profile.bandStdDev ?? Array(current.length).fill(0.25)  // generous fallback for legacy profiles
  let score = 0
  for (let i = 0; i < current.length; i++) {
    const deviation = Math.abs(current[i] - profile.bands[i])
    // 3.0 std-deviation tolerance + small fixed floor to avoid over-strict matching
    const tolerance = stdDev[i] * 3.0 + 0.05
    score += Math.max(0, 1 - deviation / tolerance)
  }
  return score / current.length
}

/** Extract normalised frequency-band energies from an AnalyserNode. */
function extractBands(analyser: AnalyserNode, buf: Uint8Array<ArrayBuffer>): number[] {
  analyser.getByteFrequencyData(buf)
  const binSize = Math.floor(buf.length / AUDIO_BANDS)
  const raw = Array.from({ length: AUDIO_BANDS }, (_, b) => {
    let sum = 0
    for (let j = b * binSize; j < Math.min((b + 1) * binSize, buf.length); j++) sum += buf[j]
    return sum / binSize
  })
  const max = Math.max(...raw, 1)
  return raw.map(v => v / max)
}

// ── Continuous voice-profile learning ─────────────────────────────────────────
// Slowly blends fresh, high-confidence frames of the user's voice into the stored
// profile so JARVIS keeps adapting to the user's voice (and mic/room) the more
// they talk to it. Uses an exponential moving average so a single noisy frame can
// never corrupt the fingerprint, and recomputes the per-band tolerance and
// spectral centroid from the updated bands.
function adaptVoiceProfile(profile: VoiceProfile, bands: number[], alpha = 0.05): VoiceProfile {
  const newBands = profile.bands.map((b, i) => b * (1 - alpha) + (bands[i] ?? b) * alpha)
  const std = profile.bandStdDev ?? Array(newBands.length).fill(0.1)
  const newStd = std.map((s, i) => {
    const dev = Math.abs((bands[i] ?? newBands[i]) - newBands[i])
    return Math.max(0.03, Math.min(0.5, s * (1 - alpha) + dev * alpha))
  })
  const sum = newBands.reduce((a, b) => a + b, 0) || 0.01
  const centroid = newBands.reduce((a, v, i) => a + v * i, 0) / (newBands.length * sum)
  return { ...profile, bands: newBands, bandStdDev: newStd, centroid }
}

// ── Learned vocabulary (per-user word adaptation) ─────────────────────────────
// Accumulates the words the user actually says so recognition output can be
// auto-corrected toward the user's real vocabulary over time — making word
// recognition steadily better the more they talk to JARVIS.
const LEARNED_WORDS_KEY = 'paul.learnedWords.v1'

// ── IndexedDB helper — permanent backup that survives localStorage clearing ────
function _idbOpen(): Promise<IDBDatabase | null> {
  return new Promise((resolve) => {
    if (typeof window === 'undefined' || !window.indexedDB) return resolve(null)
    const req = window.indexedDB.open('JarvisVoice', 1)
    req.onupgradeneeded = () => { try { req.result.createObjectStore('vocab', { keyPath: 'id' }) } catch { /* exists */ } }
    req.onsuccess = () => resolve(req.result)
    req.onerror   = () => resolve(null)
  })
}

async function _idbSave(words: Record<string, number>): Promise<void> {
  const db = await _idbOpen()
  if (!db) return
  return new Promise((resolve) => {
    try {
      const tx = db.transaction('vocab', 'readwrite')
      tx.objectStore('vocab').put({ id: 'learnedWords', data: words, ts: Date.now() })
      tx.oncomplete = () => { db.close(); resolve() }
      tx.onerror    = () => { db.close(); resolve() }
    } catch { db.close(); resolve() }
  })
}

async function _idbLoad(): Promise<Record<string, number> | null> {
  const db = await _idbOpen()
  if (!db) return null
  return new Promise((resolve) => {
    try {
      const tx  = db.transaction('vocab', 'readonly')
      const req = tx.objectStore('vocab').get('learnedWords')
      req.onsuccess = () => { db.close(); resolve(req.result?.data || null) }
      req.onerror   = () => { db.close(); resolve(null) }
    } catch { db.close(); resolve(null) }
  })
}

// Domain + command terms seeded so accuracy is already good in the first session.
// Expanding this list makes the self-correcting vocabulary smarter from day one.
const SEED_VOCAB = [
  // ── Core wake words ────────────────────────────────────────────────────────
  'jarvis', 'paul', 'sox',
  // ── Trading actions ────────────────────────────────────────────────────────
  'execute', 'open', 'close', 'cancel', 'confirm', 'place', 'enter', 'exit',
  'buy', 'sell', 'long', 'short', 'monitor', 'watch', 'analyse', 'analyze',
  'analysis', 'sniper', 'scan', 'check', 'find',
  // ── Order types / components ───────────────────────────────────────────────
  'limit', 'market', 'order', 'orders', 'position', 'positions', 'entry', 'entries',
  'stop', 'loss', 'profit', 'take', 'setup', 'setups', 'signal', 'signals',
  'sl', 'tp', 'tpone', 'tptwo', 'breakeven',
  // ── Sizing ─────────────────────────────────────────────────────────────────
  'lot', 'lots', 'contract', 'contracts', 'unit', 'units', 'volume', 'size',
  'pips', 'points', 'percent', 'percent',
  // ── Account ────────────────────────────────────────────────────────────────
  'balance', 'equity', 'margin', 'leverage', 'account', 'portfolio', 'risk', 'reward',
  // ── Crypto pairs ───────────────────────────────────────────────────────────
  'bitcoin', 'btcusdt', 'ethereum', 'ethusdt', 'solana', 'solusdt',
  'bnbusdt', 'xrpusdt', 'dogeusdt', 'adausdt', 'gweiusdt', 'velvetusdt',
  'pepeusdt', 'shibusdt', 'avaxusdt', 'maticusdt', 'linkusdt', 'dotusdt',
  // ── Forex / indices ────────────────────────────────────────────────────────
  'gold', 'xauusd', 'eurusd', 'gbpusd', 'usdjpy', 'nasdaq', 'sp500',
  // ── SMC concepts ──────────────────────────────────────────────────────────
  'bullish', 'bearish', 'momentum', 'bias', 'trend', 'reversal', 'breakout',
  'support', 'resistance', 'liquidity', 'orderblock', 'imbalance',
  // ── Direction / navigation ─────────────────────────────────────────────────
  'navigate', 'open', 'scroll', 'click', 'dashboard', 'settings', 'live',
  'chart', 'symbol', 'trade', 'trades', 'futures',
]

function loadLearnedWords(): Record<string, number> {
  const base: Record<string, number> = {}
  for (const w of SEED_VOCAB) base[w] = 3  // seed terms start "well established"
  if (typeof window === 'undefined') return base
  try {
    const s = localStorage.getItem(LEARNED_WORDS_KEY)
    if (s) {
      const parsed = JSON.parse(s)
      if (parsed && typeof parsed === 'object') {
        for (const [k, v] of Object.entries(parsed)) {
          if (typeof v === 'number') base[k] = (base[k] || 0) + v
        }
      }
    }
  } catch { /* ignore — fall back to seeds */ }
  return base
}

function persistLearnedWords(words: Record<string, number>) {
  if (typeof window === 'undefined') return
  try {
    const out: Record<string, number> = {}
    for (const [k, v] of Object.entries(words)) if (v > 0) out[k] = Math.min(v, 9999)
    localStorage.setItem(LEARNED_WORDS_KEY, JSON.stringify(out))
    // ── Cross-save to IndexedDB (survives localStorage clears) ───────────────
    _idbSave(out).catch(() => { /* best-effort */ })
    // ── Cross-save to extension background (chrome.storage.local) ────────────
    // The content script relays this into chrome.storage.local which is NOT
    // affected by page localStorage clearing, providing a third redundant copy.
    try {
      window.postMessage({ __jarvisPage: true, type: 'voice-learning-save', data: out }, window.location.origin)
    } catch { /* noop */ }
  } catch { /* ignore */ }
}

// Tokenise into lowercase alphabetic words (drops punctuation/numbers).
function tokenizeWords(text: string): string[] {
  return text.toLowerCase().match(/[a-z']{2,}/g) || []
}

// Record the words of a confirmed user utterance into the vocabulary.
function learnFromText(words: Record<string, number>, text: string): void {
  for (const w of tokenizeWords(text)) words[w] = (words[w] || 0) + 1
}

// Levenshtein distance, early-exiting once it exceeds 2 (cheap for short words).
function editDistance(a: string, b: string): number {
  const m = a.length, n = b.length
  if (Math.abs(m - n) > 2) return 3
  let prev = Array.from({ length: n + 1 }, (_, i) => i)
  for (let i = 1; i <= m; i++) {
    const cur = [i]
    let rowMin = i
    for (let j = 1; j <= n; j++) {
      const cost = a[i - 1] === b[j - 1] ? 0 : 1
      const v = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
      cur[j] = v
      if (v < rowMin) rowMin = v
    }
    if (rowMin > 2) return 3
    prev = cur
  }
  return prev[n]
}

// Correct a recognised transcript toward the learned vocabulary: any word that is
// unknown but is a near-miss (edit distance 1) of a well-established learned word
// is replaced. Conservative on purpose — only touches words of 4+ chars so short
// command words ("buy" vs "by") are never mangled.
function correctWithVocab(text: string, words: Record<string, number>): string {
  const known = new Set(Object.keys(words))
  return text.replace(/[A-Za-z']{4,}/g, (raw) => {
    const lw = raw.toLowerCase()
    if (known.has(lw)) return raw
    let best: string | null = null
    let bestCount = 1
    for (const cand of known) {
      if (Math.abs(cand.length - lw.length) > 1) continue
      const count = words[cand] || 0
      if (count < 2) continue  // only trust well-established words
      if (count > bestCount && editDistance(lw, cand) <= 1) { best = cand; bestCount = count }
    }
    if (!best) return raw
    return /^[A-Z]/.test(raw) ? best.charAt(0).toUpperCase() + best.slice(1) : best
  })
}

// ── Voice selection ─────────────────────────────────────────────────────────
// Pick the most natural, JARVIS-like voice from the system list. Prefers
// premium / neural / enhanced engines, British accent, and the requested
// gender. Works as a fallback when the user hasn't chosen a specific voice.
function pickJarvisVoice(voices: SpeechSynthesisVoice[], gender: 'male' | 'female'): SpeechSynthesisVoice | null {
  const en = voices.filter(v => /^en[-_]/i.test(v.lang) || /english/i.test(v.name))
  if (!en.length) return voices[0] || null
  const score = (v: SpeechSynthesisVoice): number => {
    const n = v.name.toLowerCase()
    let s = 0
    if (/premium|enhanced|neural|natural|siri/.test(n)) s += 45   // high-quality engines
    if (/google/.test(n)) s += 25                                 // Google voices sound smooth
    if (/en[-_]gb/i.test(v.lang)) s += 22                          // JARVIS = British
    else if (/en[-_](au|ie)/i.test(v.lang)) s += 9
    else if (/en[-_]us/i.test(v.lang)) s += 6
    const maleHints = /\b(daniel|arthur|oliver|george|rishi|james|jamie|tom|alex|aaron|fred|male|man)\b/
    const femaleHints = /\b(samantha|karen|serena|martha|moira|fiona|tessa|kate|stephanie|zoe|female|woman|victoria|allison|ava|susan)\b/
    if (gender === 'male') { if (maleHints.test(n)) s += 30; if (femaleHints.test(n)) s -= 50 }
    else { if (femaleHints.test(n)) s += 30; if (maleHints.test(n)) s -= 50 }
    return s
  }
  return [...en].sort((a, b) => score(b) - score(a))[0] || en[0]
}

// ── Hands-free DOM control ──────────────────────────────────────────────────
function labelOf(el: Element): string {
  const aria = el.getAttribute('aria-label') || el.getAttribute('title') || (el as HTMLInputElement).placeholder || ''
  const txt = (el.textContent || '').trim()
  return `${aria} ${txt}`.toLowerCase().replace(/\s+/g, ' ').trim()
}

// Find and click the best-matching clickable element by spoken label.
function clickByText(target: string): boolean {
  if (typeof document === 'undefined') return false
  const t = target.toLowerCase().trim()
  if (!t) return false
  const sel = 'button, a, [role="button"], [role="link"], [role="tab"], [role="menuitem"], [role="option"], input[type="button"], input[type="submit"], summary, label, [data-voice]'
  const nodes = Array.from(document.querySelectorAll(sel)) as HTMLElement[]
  let best: { el: HTMLElement; score: number } | null = null
  for (const el of nodes) {
    if (el.offsetParent === null && el.getClientRects().length === 0) continue  // hidden
    const label = labelOf(el)
    if (!label) continue
    let score = 0
    if (label === t) score = 100
    else if (label.includes(t)) score = 70 + Math.max(0, 20 - (label.length - t.length))
    else if (t.includes(label) && label.length >= 3) score = 50
    else {
      const tw = t.split(' ')
      const hit = tw.filter(w => w.length > 1 && label.includes(w)).length
      if (hit) score = 25 + hit * 6
    }
    if (score > 0 && (!best || score > best.score)) best = { el, score }
  }
  if (best && best.score >= 30) {
    const el = best.el
    try { el.scrollIntoView({ block: 'center', behavior: 'smooth' }) } catch { /* noop */ }
    const prevOutline = el.style.outline
    el.style.outline = '2px solid #22d3ee'
    el.style.outlineOffset = '2px'
    setTimeout(() => { try { el.style.outline = prevOutline } catch { /* noop */ } }, 1200)
    setTimeout(() => { try { el.click() } catch { /* noop */ } }, 200)
    return true
  }
  return false
}

// Set a value on a form field React-safely (works with controlled inputs).
function setNativeValue(field: HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement, value: string) {
  const proto = field instanceof HTMLTextAreaElement
    ? window.HTMLTextAreaElement.prototype
    : field instanceof HTMLSelectElement
      ? window.HTMLSelectElement.prototype
      : window.HTMLInputElement.prototype
  const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set
  try { setter?.call(field, value) } catch { field.value = value }
  field.dispatchEvent(new Event('input', { bubbles: true }))
  field.dispatchEvent(new Event('change', { bubbles: true }))
}

// Type text into the focused (or first visible) input/textarea — React-safe.
function typeIntoField(text: string): boolean {
  if (typeof document === 'undefined') return false
  const active = document.activeElement as HTMLElement | null
  let field: HTMLInputElement | HTMLTextAreaElement | null = null
  if (active && (active.tagName === 'INPUT' || active.tagName === 'TEXTAREA')) field = active as any
  if (!field) field = document.querySelector('input[type="text"], input[type="search"], input:not([type]), textarea') as any
  if (!field) return false
  field.focus()
  setNativeValue(field, text)
  return true
}

// Resolve a form field's spoken label from <label for>, wrapping <label>,
// aria-label, placeholder, name and id — so "amount", "stop loss", etc. match.
function fieldLabel(el: Element): string {
  let lbl = ''
  const id = el.getAttribute('id')
  if (id && typeof document !== 'undefined') {
    try {
      const safe = typeof CSS !== 'undefined' && CSS.escape ? CSS.escape(id) : id
      const forLabel = document.querySelector(`label[for="${safe}"]`)
      if (forLabel) lbl += ' ' + (forLabel.textContent || '')
    } catch { /* noop */ }
  }
  const wrap = el.closest('label')
  if (wrap) lbl += ' ' + (wrap.textContent || '')
  lbl += ' ' + (el.getAttribute('aria-label') || '')
  lbl += ' ' + ((el as HTMLInputElement).placeholder || '')
  lbl += ' ' + (el.getAttribute('name') || '')
  lbl += ' ' + (el.getAttribute('id') || '')
  return lbl.toLowerCase().replace(/\s+/g, ' ').trim()
}

function scoreLabel(label: string, t: string): number {
  if (!label) return 0
  if (label === t) return 100
  if (label.includes(t)) return 70 + Math.max(0, 20 - (label.length - t.length))
  if (t.includes(label) && label.length >= 3) return 50
  const tw = t.split(' ')
  const hit = tw.filter(w => w.length > 1 && label.includes(w)).length
  return hit ? 25 + hit * 6 : 0
}

// Find the best form field (input/textarea/select) by spoken name.
function findField(name: string): HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement | null {
  if (typeof document === 'undefined') return null
  const t = name.toLowerCase().trim()
  if (!t) return null
  const nodes = Array.from(document.querySelectorAll('input, textarea, select')) as (HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement)[]
  let best: { el: HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement; score: number } | null = null
  for (const el of nodes) {
    const type = (el.getAttribute('type') || '').toLowerCase()
    if (type === 'hidden' || type === 'submit' || type === 'button') continue
    if (el.offsetParent === null && el.getClientRects().length === 0) continue  // hidden
    const score = scoreLabel(fieldLabel(el), t)
    if (score > 0 && (!best || score > best.score)) best = { el, score }
  }
  return best && best.score >= 25 ? best.el : null
}

// Fill a named field by spoken label ("set amount to 100").
function setFieldByName(name: string, value: string): boolean {
  const f = findField(name)
  if (!f) return false
  try { f.scrollIntoView({ block: 'center', behavior: 'smooth' }) } catch { /* noop */ }
  ;(f as HTMLElement).focus?.()
  if (f instanceof HTMLSelectElement) {
    const opt = value.toLowerCase().trim()
    const match = Array.from(f.options).find(o =>
      o.text.toLowerCase().trim() === opt || o.value.toLowerCase().trim() === opt || o.text.toLowerCase().includes(opt))
    if (!match) return false
    setNativeValue(f, match.value)
    return true
  }
  setNativeValue(f, value)
  return true
}

// Select an option in a dropdown by text ("select BTCUSDT").
function setSelectByText(option: string, fieldName?: string): boolean {
  if (typeof document === 'undefined') return false
  const opt = option.toLowerCase().trim()
  if (!opt) return false
  let candidates = (Array.from(document.querySelectorAll('select')) as HTMLSelectElement[])
    .filter(s => s.offsetParent !== null || s.getClientRects().length > 0)
  if (fieldName) {
    const f = findField(fieldName)
    if (f instanceof HTMLSelectElement) candidates = [f]
  }
  for (const sel of candidates) {
    const match = Array.from(sel.options).find(o =>
      o.text.toLowerCase().trim() === opt || o.value.toLowerCase().trim() === opt || o.text.toLowerCase().includes(opt))
    if (match) {
      try { sel.scrollIntoView({ block: 'center', behavior: 'smooth' }) } catch { /* noop */ }
      setNativeValue(sel, match.value)
      return true
    }
  }
  // Custom (non-native) dropdown: open the trigger then click the option.
  if (fieldName) clickByText(fieldName)
  return clickByText(option)
}

// Toggle a checkbox / switch by label ("turn on auto trade").
function toggleByText(target: string): boolean {
  if (typeof document === 'undefined') return false
  const t = target.toLowerCase().trim()
  if (!t) return false
  const nodes = Array.from(document.querySelectorAll('input[type="checkbox"], input[type="radio"], [role="switch"], [role="checkbox"]')) as HTMLElement[]
  let best: { el: HTMLElement; score: number } | null = null
  for (const el of nodes) {
    if (el.offsetParent === null && el.getClientRects().length === 0) continue
    const score = scoreLabel(fieldLabel(el) || labelOf(el), t)
    if (score > 0 && (!best || score > best.score)) best = { el, score }
  }
  if (best && best.score >= 25) {
    try { best.el.scrollIntoView({ block: 'center', behavior: 'smooth' }) } catch { /* noop */ }
    try { best.el.click() } catch { /* noop */ }
    return true
  }
  return clickByText(target)  // some toggles are buttons
}

// Submit the active (or first) form.
function submitForm(): boolean {
  if (typeof document === 'undefined') return false
  const active = document.activeElement as HTMLElement | null
  let form: HTMLFormElement | null = (active?.closest('form') as HTMLFormElement | null) || null
  if (!form) form = document.querySelector('form')
  if (form) {
    const submitBtn = form.querySelector('button[type="submit"], input[type="submit"], button:not([type])') as HTMLElement | null
    if (submitBtn) { try { submitBtn.click() } catch { /* noop */ }; return true }
    try {
      if (typeof form.requestSubmit === 'function') form.requestSubmit()
      else form.submit()
      return true
    } catch { /* noop */ }
  }
  return clickByText('submit') || clickByText('save') || clickByText('confirm') || clickByText('place order')
}

// Dismiss a dialog / cancel — press Escape and click any cancel/close control.
function pressCancel(): boolean {
  if (typeof document === 'undefined') return false
  const ev = new KeyboardEvent('keydown', { key: 'Escape', code: 'Escape', bubbles: true })
  try { (document.activeElement || document.body).dispatchEvent(ev) } catch { /* noop */ }
  try { document.dispatchEvent(ev) } catch { /* noop */ }
  clickByText('cancel') || clickByText('close') || clickByText('dismiss')
  return true
}

// ── Face Vision gate constants ────────────────────────────────────────────────
// The camera's face state is only trusted while it is "fresh" (panel streaming).
const FACE_FRESH_MS = 2500   // face state older than this = camera off/stale
const MOUTH_WINDOW_MS = 1500 // keep hearing briefly after the last mouth motion

// ── Web Speech API (typed loosely — not in the default TS lib) ───────────────
type SpeechRecognitionLike = any
function getSpeechRecognition(): (new () => SpeechRecognitionLike) | null {
  if (typeof window === 'undefined') return null
  return (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition || null
}

// Strip emoji / markdown so TTS reads cleanly.
function cleanForSpeech(text: string): string {
  return text
    .replace(/[\u{1F000}-\u{1FFFF}\u{2600}-\u{27BF}]/gu, '')  // emoji
    .replace(/[*_`#>]/g, '')                                   // markdown
    .replace(/\[(.*?)\]\(.*?\)/g, '$1')                        // links
    .replace(/\s+/g, ' ')
    .trim()
}

// Render assistant message content with minimal markdown:
//   **text** → <strong>, lines with •/─ indented as bullets, ⚠️ lines styled.
//   Keeps the rest as plain pre-wrapped text.
function renderMarkdown(text: string): React.ReactNode {
  const segments: React.ReactNode[] = []
  const lines = text.split('\n')
  lines.forEach((line, i) => {
    // Parse inline **bold** within a single line
    const parseBold = (raw: string): React.ReactNode => {
      const parts = raw.split(/(\*\*[^*]+\*\*)/)
      if (parts.length === 1) return raw
      return parts.map((p, j) =>
        p.startsWith('**') && p.endsWith('**')
          ? <strong key={j} className="font-semibold text-white">{p.slice(2, -2)}</strong>
          : p
      )
    }

    const trimmed = line.trimStart()
    const indent = line.length - trimmed.length

    // AI verdict lines: "   AI: ✅ TAKE — note"
    if (/^\s+AI:\s/.test(line)) {
      const isGood = /✅|TAKE/.test(line)
      const isBad  = /❌|SKIP/.test(line)
      segments.push(
        <div key={i} className={`mt-0.5 ml-${Math.min(indent, 6)} text-[12px] font-medium ${
          isGood ? 'text-green-400' : isBad ? 'text-red-400' : 'text-yellow-400'
        }`}>{trimmed}</div>
      )
      return
    }

    // Bullet lines: "   • " or "   - "
    if (/^\s+[•\-]\s/.test(line)) {
      segments.push(
        <div key={i} className="flex gap-1.5 ml-2 mt-0.5">
          <span className="text-cyan-500 shrink-0">•</span>
          <span>{parseBold(trimmed.replace(/^[•\-]\s/, ''))}</span>
        </div>
      )
      return
    }

    // Warning lines: start with ⚠️
    if (trimmed.startsWith('⚠️')) {
      segments.push(
        <div key={i} className="mt-1.5 text-amber-400 text-[12px]">{parseBold(trimmed)}</div>
      )
      return
    }

    // Bold-only header lines (entire line is **...**)
    if (/^\*\*[^*]+\*\*$/.test(trimmed)) {
      segments.push(
        <div key={i} className="mt-2 font-semibold text-cyan-300 text-[13px]">{trimmed.slice(2, -2)}</div>
      )
      return
    }

    // Normal line — parse inline bold
    segments.push(
      i === 0
        ? <span key={i}>{parseBold(line)}</span>
        : <div key={i} className={line.trim() === '' ? 'h-1.5' : 'mt-0.5'}>{parseBold(line)}</div>
    )
  })
  return <>{segments}</>
}

// Detect a deliberate wake phrase. By default the bare name ("Jarvis, ...")
// wakes the assistant; when `requireGreeting` is true a greeting word MUST
// precede the name (strict mode for noisy rooms). Minor mis-hearings of the
// name ("jervis", "jarvas", "javis") are tolerated by the shared matcher.
function hasWakeWord(transcript: string, requireGreeting = false): boolean {
  return phoneticWakeMatch(transcript, requireGreeting)
}

const PaulChat = memo(function PaulChat({ hideRobot = false }: { hideRobot?: boolean } = {}) {
  const router = useRouter()
  const [open, setOpen] = useState(false)
  const [messages, setMessages] = useState<Message[]>(() => {
    // Restore the previous conversation immediately on mount so a page refresh
    // never wipes the chat. Falls back to the welcome message on a fresh browser.
    const stored = loadStoredMessages()
    if (stored && stored.length) return stored
    return [
      {
        id: 'welcome',
        role: 'assistant',
        content: "Good day, Sir. I'm PAUL — your personal trading assistant. How can I help you today? I can tell you about open positions, recent signals, live market news, or forecast any pair. Just say my name — \"Jarvis\", \"Paul\", or \"Sox\" — e.g. \"Jarvis, analyse Gold for sniper entries\".",
      },
    ]
  })
  const [input, setInput] = useState('')
  const [streaming, setStreaming] = useState(false)
  const [alerts, setAlerts] = useState<Alert[]>([])
  const [alertsOpen, setAlertsOpen] = useState(false)
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const abortRef = useRef<AbortController | null>(null)

  // ── Persistent session id (so JARVIS remembers across reloads) ────────────
  const sessionKeyRef = useRef<string>('default')
  if (typeof window !== 'undefined' && sessionKeyRef.current === 'default') {
    let sk = localStorage.getItem('paul.session')
    if (!sk) { sk = 'sess_' + Math.random().toString(36).slice(2) + Date.now().toString(36); localStorage.setItem('paul.session', sk) }
    sessionKeyRef.current = sk
  }

  // ── JARVIS brain: idle "thinking" indicator (OpenHuman subconscious) ──────
  // Polls the backend idle-status so the chat shows when JARVIS is quietly
  // researching the active goal between your messages.
  const [brainThinking, setBrainThinking] = useState(false)
  const [brainNote, setBrainNote] = useState<string>('')
  useEffect(() => {
    let alive = true
    const poll = async () => {
      try {
        const r = await apiClient.jarvis.idleStatus()
        if (!alive) return
        setBrainThinking(!!r.data?.thinking)
        if (r.data?.note) setBrainNote(String(r.data.note).slice(0, 80))
      } catch { /* backend offline — stay quiet */ }
    }
    poll()
    const id = setInterval(poll, 7000)
    return () => { alive = false; clearInterval(id) }
  }, [])

  // ── Speech state ──────────────────────────────────────────────────────────
  const [speechSupported, setSpeechSupported] = useState(false)
  const [listening, setListening] = useState(false)   // mic dictation active
  const [voiceEnabled, setVoiceEnabled] = useState(
    () => typeof window !== 'undefined' && localStorage.getItem('paul.voice') === '1'
  )
  // Wake word is ON by default — JARVIS listens and waits for "Hi Jarvis" at all
  // times, unless the user has explicitly turned it off.
  const [wakeEnabled, setWakeEnabled] = useState(
    () => typeof window === 'undefined' ? true : localStorage.getItem('paul.wake') !== '0'
  )
  // Voice selection (so JARVIS sounds real — male/female + multiple voices).
  const [voiceList, setVoiceList] = useState<SpeechSynthesisVoice[]>([])
  const [voiceURI, setVoiceURI] = useState<string>(
    () => (typeof window !== 'undefined' && localStorage.getItem('paul.voiceURI')) || ''
  )
  const [voiceGender, setVoiceGender] = useState<'male' | 'female'>(
    () => ((typeof window !== 'undefined' && localStorage.getItem('paul.voiceGender')) as 'male' | 'female') || 'male'
  )
  const [voiceRate, setVoiceRate] = useState<number>(
    () => Number((typeof window !== 'undefined' && localStorage.getItem('paul.voiceRate')) || 0.96)
  )
  const [voicePitch, setVoicePitch] = useState<number>(
    () => Number((typeof window !== 'undefined' && localStorage.getItem('paul.voicePitch')) || 0.9)
  )
  const [settingsOpen, setSettingsOpen] = useState(false)
  // Voice profile for speaker identification
  const [voiceProfile, setVoiceProfile] = useState<VoiceProfile | null>(() => loadVoiceProfile())
  const [calibrating, setCalibrating] = useState(false)
  const [calibCountdown, setCalibCountdown] = useState(0)
  const [voiceMatchEnabled, setVoiceMatchEnabled] = useState(
    () => typeof window !== 'undefined' && localStorage.getItem('paul.voiceMatchEnabled') === '1'
  )
  // Require a greeting word before the wake name. OFF by default — the bare
  // name ("Jarvis, ...") wakes JARVIS. Flip ON to restore the strict
  // greeting-gate in noisy environments.
  const [wakeRequireGreeting, setWakeRequireGreeting] = useState(
    () => typeof window !== 'undefined' && localStorage.getItem('paul.wakeRequireGreeting') === '1'
  )
  // Noise gate — only act on speech results whose recognition confidence is
  // above this threshold. Raised default to 0.65 (Siri-level) to prevent TV/
  // background voices from triggering JARVIS. 0 = accept everything; 1 = strict.
  const [noiseThreshold, setNoiseThreshold] = useState<number>(
    () => Number((typeof window !== 'undefined' && localStorage.getItem('paul.noiseThreshold')) || 0.65)
  )
  const [autoNoise, setAutoNoise] = useState(
    () => typeof window !== 'undefined' && localStorage.getItem('paul.autoNoise') === '1'
  )
  const [aiSpeechEnabled, setAiSpeechEnabled] = useState(
    () => typeof window !== 'undefined' && localStorage.getItem('paul.aiSpeechEnabled') === '1'
  )
  const [aiVoiceEnabled, setAiVoiceEnabled] = useState(
    () => typeof window !== 'undefined' && localStorage.getItem('paul.aiVoiceEnabled') === '1'
  )
  const [aiVoice, setAiVoice] = useState<string>(
    () => (typeof window !== 'undefined' && localStorage.getItem('paul.aiVoice')) || 'alloy'
  )
  // 3D robot avatar style (synced to/from the extension via chrome.storage relay)
  const [avatarStyle, setAvatarStyle] = useState<AvatarStyle>(
    () => ((typeof window !== 'undefined' && localStorage.getItem('paul.avatarStyle')) as AvatarStyle) || 'cyan'
  )
  // Only mount the heavy WebGL robot on capable machines (high/ultra tier) so
  // the app always loads on weaker laptops (e.g. Apple M2 8GB, which the WebGL
  // scene could otherwise hang at startup). The lightweight floating chat button
  // is always shown as a fallback. Override with localStorage 'paul.forceRobot'
  // = '1' (force on) or '0' (force off).
  const [robotAllowed, setRobotAllowed] = useState(false)
  useEffect(() => {
    try {
      const forced = localStorage.getItem('paul.forceRobot')
      if (forced === '1') { setRobotAllowed(true); return }
      if (forced === '0') { setRobotAllowed(false); return }
    } catch { /* noop */ }
    const t = detectStaticTier()
    setRobotAllowed(t === 'high' || t === 'ultra')
  }, [])
  // Live voice energy 0..1 (updated by the mini-engine RAF loop) for robot motion
  const robotEnergyRef = useRef(0)
  const [robotEnergy, setRobotEnergy] = useState(0)
  const [recording, setRecording] = useState(false)
  const noiseThresholdRef = useRef(0.65)
  // Rolling window of recently MEASURED recognition confidences — used to adapt
  // the noise gate to the ambient level instead of a single fixed threshold.
  const ambientConfRef = useRef<number[]>([])
  // Auto-stop dictation shortly after the user stops talking (faster turnaround).
  const silenceTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  // Confirmation gate for destructive trading verbs (e.g. "close all positions").
  const pendingCloseAllRef = useRef(false)
  const pendingCloseAllCmdRef = useRef('')

  const dictationRef = useRef<SpeechRecognitionLike>(null)
  const wakeRef = useRef<SpeechRecognitionLike>(null)
  // Transient runtime pause for the wake recognizer (e.g. the browser briefly
  // denies/blocks the mic). This is NOT the user's preference — it is cleared on
  // the next user gesture so a one-off glitch can never leave JARVIS permanently
  // deaf (which used to happen by persisting the wake word OFF to localStorage).
  const wakeErrorPausedRef = useRef(false)
  // Wake-recognizer liveness tracking. On heavy pages (charts, WebGL) the Web
  // Speech recognizer can silently die — rec.start() throws and is swallowed, or
  // onstart never fires — leaving JARVIS deaf until the next gesture. onstart
  // sets started=true, onend sets it false; a periodic watchdog re-arms wake if
  // it should be running but hasn't started within a few seconds.
  const wakeStartedRef = useRef(false)
  const wakeStartAtRef = useRef(0)
  // Re-arms the wake recognizer on the next user gesture (set up below).
  const rearmWakeOnGestureRef = useRef<() => void>(() => {})
  // Active Whisper capture (so it can be aborted the instant JARVIS starts to
  // speak — prevents the assistant from recording its own voice).
  const whisperRecorderRef = useRef<MediaRecorder | null>(null)
  const whisperStreamRef = useRef<MediaStream | null>(null)
  // True when a Whisper capture was aborted for speech — its onstop must discard
  // the audio instead of transcribing it (so JARVIS never hears itself).
  const whisperAbortedRef = useRef(false)
  const startDictationRef = useRef<() => void>(() => {})
  const startWakeRef = useRef<() => void>(() => {})
  const sendRef = useRef<(text?: string) => void>(() => {})
  // Voice / extension commands that arrive while a response is still streaming
  // are queued here (instead of being dropped) and flushed when the stream
  // ends — so "I talk to JARVIS and nothing happens" can no longer occur.
  const pendingSendQueueRef = useRef<string[]>([])
  const commandRef = useRef<(text: string) => boolean>(() => false)
  const resolveIntentRef = useRef<(text: string) => Promise<boolean>>(async () => false)
  const lastSpokenRef = useRef<string>('')
  const historyLoadedRef = useRef(false)  // true once server history is restored (don't auto-speak it)
  const lastAssistantRef = useRef<string>('')  // latest assistant reply text (for "read that again")
  const voiceEnabledRef = useRef(false)
  const wakeEnabledRef = useRef(false)
  const openRef = useRef(false)
  const listeningRef = useRef(false)
  const voiceURIRef = useRef('')
  const voiceRateRef = useRef(0.96)
  const voicePitchRef = useRef(0.9)
  const isSpeakingRef = useRef(false)  // true while TTS is playing
  const interruptRef = useRef(false)   // set true to abort current speech mid-sentence
  const autoNoiseCalibratingRef = useRef(false)  // true during auto-calibration
  // Post-speech mic blackout: mic stays gated for this many ms after speech ends
  // to swallow any audio echo before re-opening recognition. 900ms matches the
  // extension's echo-tail window so both are synchronised.
  const postSpeechGateRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const micGatedRef = useRef(false)  // true during post-speech blackout
  // ── Face Vision (camera) state — relayed from FaceVisionPanel postMessage ──
  // While the camera is live, the user's moving mouth is the ground truth for
  // "the user is talking": JARVIS's TTS can never move the user's mouth, so
  // gating transcription on it makes self-hearing physically impossible.
  const faceStateRef = useRef({ present: false, talking: false, match: false, enrolled: false, ts: 0 })
  const lastMouthActiveAtRef = useRef(0)
  // Voice profile — speaker ID for noise cancellation by voice pattern matching
  const voiceMatchRef = useRef(true)           // true = accept; false = not user's voice
  const voiceMatchEnabledRef = useRef(false)   // mirror of voiceMatchEnabled for event handlers/refs
  // Per-user learned vocabulary (loaded on mount). Used to auto-correct recognised
  // transcripts toward the words the user actually says, improving as they talk.
  const learnedWordsRef = useRef<Record<string, number>>({})
  const learnPersistTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  // ── Voice Brain sync counters ──────────────────────────────────────────────
  // Every BRAIN_SYNC_EVERY utterances, vocabulary + voice profile are pushed to
  // the permanent vault brain.  Also triggers on page unload so no data is lost.
  const utteranceCountRef    = useRef(0)
  const brainSyncPendingRef  = useRef(false)
  const BRAIN_SYNC_EVERY     = 15  // sync after every 15th utterance
  // Throttle for persisting the continuously-adapted voice profile.
  const profilePersistAtRef = useRef(0)
  const wakeRequireGreetingRef = useRef(false) // mirror of wakeRequireGreeting for event handlers
  const dispatchVoiceCommandRef = useRef<(text: string) => void>(() => {})
  const voiceMatchWindowRef = useRef<boolean[]>([])  // rolling 30-frame temporal window (~1s)
  const voiceAnalyserRef   = useRef<AnalyserNode | null>(null)
  const voiceAudioCtxRef   = useRef<AudioContext | null>(null)
  const miniCanvasRef      = useRef<HTMLCanvasElement | null>(null)  // mini freq visualizer
  const voiceStreamRef = useRef<MediaStream | null>(null)
  const voiceBufRef = useRef<Uint8Array | null>(null)
  const voiceRafRef     = useRef<number | null>(null)
  const miniRafRef      = useRef<number>(0)
  const miniThrottle    = useRef<number>(0)
  // Latest MT5 page context (account + symbol + timeframe) published by /mt5-live.
  // Persisted to sessionStorage so it survives navigation and lets JARVIS
  // analyse / place orders from ANY page, not just /mt5-live.
  const mt5ContextRef = useRef<{ accountId: number; symbol: string; timeframe?: string; balance?: number; currency?: string } | null>(
    (() => {
      try {
        const s = typeof window !== 'undefined' ? sessionStorage.getItem('__jarvis_mt5_ctx') : null
        return s ? JSON.parse(s) : null
      } catch { return null }
    })()
  )
  // Indirection so the (earlier-defined) send() / voice pipeline can trigger the
  // (later-defined) sniper analysis without ordering issues.  Accepts the raw
  // user text so the analysis can extract a requested timeframe.
  const runSniperAnalysisRef = useRef<(requestText?: string) => void>(() => {})
  // Indirection for the "execute all setups" intent (places all cards at once).
  const executeAllSetupsRef = useRef<(requestText?: string) => Promise<void>>(async () => {})
  // Per-setup Execute state, keyed by `${messageId}:${index}`.
  const [setupStatus, setSetupStatus] = useState<Record<string, { status: 'idle' | 'placing' | 'placed' | 'error'; msg?: string }>>({})

  // ── Browser-extension bridge state ────────────────────────────────────────
  // The JARVIS browser extension (jarvis-extension/) provides more reliable
  // speech recognition than the in-page Web Speech API. When installed, it posts
  // recognised commands here via window.postMessage. Declared up here (rather
  // than next to the bridge effect) so the voice-matching effect can react to
  // the extension owning the mic.
  const [extConnected, setExtConnected] = useState(false)
  const [extVoiceReady, setExtVoiceReady] = useState(false)
  // ── Robot-mode exclusive lock ─────────────────────────────────────────────
  // When the extension robot is activated, it becomes the sole owner of mic +
  // speaker. Page chat mic/speaker are deactivated and the chat panel hides.
  const [robotLocked, setRobotLocked] = useState(false)
  const robotLockedRef = useRef(false)
  const extConnectedRef = useRef(false)
  const extVoiceReadyRef = useRef(false)
  // Grace timer used before handing the mic back to the in-page recognizer when
  // the extension is connected but not actually listening (see syncExtVoiceReady).
  const extReleaseTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  // Sticky flag: set true when we hand the mic to the in-page recognizer because
  // the extension stalled ("Starting…") or reported its speech engine failed.
  // While true, bare status/connected(listening:false) must NOT re-claim the mic
  // — only real proof (listening:true, or a wake/command/interrupt) reclaims it.
  // This is what stops the page↔extension mic-ownership flapping that left JARVIS
  // deaf and the popup stuck on "Starting…".
  const extMicReleasedRef = useRef(false)

  // ── Cost-aware Deepgram fallback (in-page) ────────────────────────────────
  // The free Web Speech API stays primary; only a *missed* utterance is sent to
  // Deepgram pre-recorded STT (one short buffered clip). A rolling MediaRecorder
  // ring buffer keeps the last few seconds of mic audio so a clip is available
  // the instant a miss is detected. A backend budget guard caps spend — when
  // capped the escalation silently no-ops and JARVIS stays on Web Speech.
  const DG_BUFFER_MS = 8000          // keep ~8s of rolling audio
  const DG_MIN_CLIP_BYTES = 1200     // skip empty/too-short clips (no spend)
  const DG_MISS_ESCALATE_AFTER = 2   // escalate after N consecutive misses (or immediately on low-conf)
  const dgStreamRef = useRef<MediaStream | null>(null)
  const dgRecorderRef = useRef<MediaRecorder | null>(null)
  const dgChunksRef = useRef<{ t: number; blob: Blob }[]>([])
  const dgArmedRef = useRef(false)
  const dgInFlightRef = useRef(false)
  const dgMissCountRef = useRef(0)
  const dgEscalateRef = useRef<(reason: string) => void>(() => {})
  // 'off' = not listening, 'armed' = fallback ready, 'paused' = budget capped/disabled.
  const [dgFallbackState, setDgFallbackState] = useState<'off' | 'armed' | 'paused'>('off')
  // Latest Deepgram budget usage (remaining monthly $ + projected runway days).
  const [dgUsage, setDgUsage] = useState<{
    remaining: number; monthly_cap: number; projected_runway_days: number | null
  } | null>(null)

  const unreadCount = alerts.filter(a => !a.read).length

  // ── Scroll to bottom on new messages ─────────────────────────────────────
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  // ── Focus input when opened ───────────────────────────────────────────────
  useEffect(() => {
    if (open) setTimeout(() => inputRef.current?.focus(), 100)
  }, [open])

  // ── Mirror the conversation to localStorage (survives refresh) ────────────
  // Debounced so streaming token updates coalesce into a single write.
  useEffect(() => {
    const t = setTimeout(() => saveStoredMessages(messages), 400)
    return () => clearTimeout(t)
  }, [messages])

  // ── Load persisted conversation history once on mount ─────────────────────
  useEffect(() => {
    let cancelled = false
    const load = async () => {
      // A local mirror is the full-fidelity copy for THIS browser (it also
      // captures analyse/command/sniper messages the backend never stores).
      // When present, keep it and skip the server restore so nothing is lost.
      if (loadStoredMessages()) { historyLoadedRef.current = true; return }
      try {
        const res = await apiClient.jarvis.getHistory(sessionKeyRef.current)
        const hist = res.data?.messages || []
        if (!cancelled && hist.length > 0) {
          setMessages([
            ...hist.map((m: any) => ({
              id: nanoid(),
              role: m.role as 'user' | 'assistant',
              content: m.content,
              fromHistory: true,  // never auto-speak restored history after refresh
            })),
          ])
          // Mark the last restored message as already-spoken so the TTS effect
          // skips it. This guarantees JARVIS doesn't re-read the last response
          // when the page is refreshed.
          historyLoadedRef.current = true
        }
      } catch { /* no history yet */ }
    }
    load()
    // Continuously grow JARVIS long-term knowledge from live news (throttled 30m)
    try {
      const last = Number(localStorage.getItem('paul.newsIngest') || '0')
      if (Date.now() - last > 30 * 60 * 1000) {
        localStorage.setItem('paul.newsIngest', String(Date.now()))
        apiClient.jarvis.ingestNews().catch(() => {})
      }
    } catch { /* ignore */ }
    // ── JARVIS Intelligence Harvester ──────────────────────────────────────────
    // Periodically harvest sentiment, SMC signals, Telegram signals, AI decisions,
    // and news into the knowledge store so the Brain Map expands with live intel.
    const runHarvest = () => {
      const key = 'paul.lastHarvest'
      try {
        const last = Number(localStorage.getItem(key) || '0')
        if (Date.now() - last > 10 * 60 * 1000) {  // max once per 10 min
          localStorage.setItem(key, String(Date.now()))
          apiClient.aiAnalyst.harvestIntelligence().catch(() => {})
        }
      } catch { /* ignore */ }
    }
    runHarvest()  // immediate first run
    const harvestInterval = setInterval(runHarvest, 10 * 60 * 1000)
    return () => { cancelled = true; clearInterval(harvestInterval) }
  }, [])

  // ── Start a brand-new conversation (archive the old one) ──────────────────
  const newChat = useCallback(async () => {
    try { await apiClient.jarvis.newChat(sessionKeyRef.current) } catch { /* ignore */ }
    try { localStorage.removeItem(chatStoreKey()) } catch { /* ignore */ }
    setMessages([{
      id: 'welcome',
      role: 'assistant',
      content: "New session started, Sir. Previous conversation archived. How can I help?",
    }])
  }, [])

  // ── Poll alerts every 15 s ────────────────────────────────────────────────
  useEffect(() => {
    const poll = async () => {
      try {
        const res = await apiClient.jarvis.getAlerts()
        setAlerts(res.data?.alerts || [])
      } catch { /* ignore */ }
    }
    poll()
    const t = setInterval(poll, 15000)
    return () => clearInterval(t)
  }, [])

  // ── Page-unload voice brain sync — never lose the session's learning ───────
  // When the tab is closed or navigated away, flush whatever has been learned
  // this session to the permanent vault brain via navigator.sendBeacon.
  // sendBeacon is the only reliable way to fire a network request on unload.
  useEffect(() => {
    const onHide = () => {
      const vocab   = learnedWordsRef.current
      const profile = typeof window !== 'undefined' ? loadVoiceProfile() : null
      const payload = JSON.stringify({
        vocabulary: vocab,
        profile:    profile
          ? { bands: profile.bands, bandStdDev: profile.bandStdDev, centroid: profile.centroid }
          : undefined,
        sessions: utteranceCountRef.current,
      })
      // Use sendBeacon to guarantee delivery even as the page is closing
      if (typeof navigator !== 'undefined' && navigator.sendBeacon) {
        const url = `${process.env.NEXT_PUBLIC_API_URL || 'http://localhost:1448/api/v1'}/jarvis/voice-brain/sync`
        navigator.sendBeacon(url, new Blob([payload], { type: 'application/json' }))
      }
    }
    if (typeof window !== 'undefined') {
      window.addEventListener('pagehide', onHide)
      document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'hidden') onHide()
      })
    }
    return () => {
      if (typeof window !== 'undefined') window.removeEventListener('pagehide', onHide)
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Broadcast JARVIS activity (listening / thinking) so other surfaces — e.g.
  // the JARVIS Room's animated energy core — can react to the voice pipeline
  // exactly like the chat does. Talking is already broadcast via `speak-status`.
  useEffect(() => {
    if (typeof window === 'undefined') return
    try {
      window.postMessage(
        { __jarvisPage: true, type: 'jarvis-activity', listening, thinking: streaming },
        window.location.origin,
      )
    } catch { /* noop */ }
  }, [listening, streaming])

  // ── Send message ──────────────────────────────────────────────────────────
  const send = useCallback(async (overrideText?: string) => {
    const text = (overrideText ?? input).trim()
    if (!text) return
    if (streaming) {
      // A response is still streaming. Don't silently drop spoken/extension
      // commands (overrideText is set for those) — queue them and flush once
      // the current stream finishes. Typed input is left in the box instead.
      if (overrideText != null) pendingSendQueueRef.current.push(text)
      return
    }
    setInput('')

    // ── Sniper-analysis intent ──────────────────────────────────────────────
    // "analyse sniper setups", "sniper entries", … → run the SMC engine for the
    // account/symbol selected on /mt5-live instead of falling through to chat.
    if (isSniperIntent(text)) {
      setMessages(prev => [...prev, { id: nanoid(), role: 'user', content: text }])
      runSniperAnalysisRef.current(text)
      return
    }

    // ── Execute-all-setups intent ───────────────────────────────────────────
    // "Execute the limits", "place the orders", "go ahead and execute", …
    // → find the most recent assistant message with sniperSetups and place them all.
    if (isExecuteSetupIntent(text)) {
      setMessages(prev => [...prev, { id: nanoid(), role: 'user', content: text }])
      void executeAllSetupsRef.current(text)
      return
    }

    // ── Bitget crypto command (execute / analyze / manage position) ─────────
    // Route to the real /jarvis/command backend.  NEVER let the AI respond to
    // these — it will fabricate fake order IDs and claim SUCCESS without placing
    // any orders.  This fires for both typed AND voice-transcribed commands.
    if (isBitgetCommand(text)) {
      const userMsg: Message = { id: nanoid(), role: 'user', content: text }
      // Empty content → renders the animated "⚡ Thinking…" indicator while the
      // backend fetches live data / runs the analysis.
      const loadingMsg: Message = { id: nanoid(), role: 'assistant', content: '', pending: true }
      setMessages(prev => [...prev, userMsg, loadingMsg])
      try {
        const res = await apiClient.jarvis.executeCommand(text)
        const d = res.data
        let reply: string
        if (d.ok) {
          const o = d.order || {}
          if (d.action === 'analyze') {
            // Rich analysis card — prefer JARVIS's AI-composed human narrative,
            // then layer in Kronos forecast, volume flow, news headlines and the
            // proposed trade levels.
            const lines: string[] = []
            if (o.narrative) {
              lines.push(o.narrative)
            } else {
              lines.push(`**${o.symbol || 'Analysis'}** — Live Bitget Data`)
              if (o.trend)  lines.push(`Trend: ${o.trend.toUpperCase()}`)
              if (o.rsi)    lines.push(`RSI: ${Number(o.rsi).toFixed(0)}`)
              if (o.ema50)  lines.push(`EMA 50: ${o.ema50}  |  EMA 200: ${o.ema200}`)
            }
            // Kronos ML forecast
            if (o.kronos && o.kronos.direction) {
              const pct = Number(o.kronos.pct_change)
              lines.push('')
              lines.push(`🔮 Kronos ML: ${String(o.kronos.direction).toUpperCase()} ${pct >= 0 ? '+' : ''}${pct.toFixed(2)}% → ${o.kronos.target_price} (${Math.round((o.kronos.confidence || 0) * 100)}% conf)`)
            }
            // Volume flow
            if (o.volume && typeof o.volume.buy_pressure_pct === 'number') {
              lines.push(`📊 Volume: ${o.volume.buy_pressure_pct.toFixed(0)}% buy / ${o.volume.sell_pressure_pct.toFixed(0)}% sell (${Number(o.volume.volume_spike_x).toFixed(1)}× avg)`)
            }
            // Your open position on this pair
            if (o.position && o.position.side) {
              const pnl = Number(o.position.pnl || 0)
              const pnlPct = Number(o.position.pnl_pct || 0)
              const arrow = pnl >= 0 ? '▲' : '▼'
              lines.push(`📌 Your position: ${String(o.position.side).toUpperCase()} ${o.position.size} @ ${o.position.entry_price} · PnL ${arrow} ${Math.abs(pnl).toFixed(2)} USDT (${pnlPct >= 0 ? '+' : ''}${pnlPct.toFixed(2)}%)`)
            }
            // News headlines + sentiment
            if (Array.isArray(o.news) && o.news.length) {
              lines.push('')
              lines.push(`📰 News (${o.news_count || o.news.length} · ${(o.sentiment_label || 'neutral').toUpperCase()})`)
              o.news.slice(0, 4).forEach((n: any) => {
                const sc = n.sentiment_score
                const lbl = n.sentiment_label || (sc > 0.1 ? 'BULLISH' : sc < -0.1 ? 'BEARISH' : 'NEUTRAL')
                lines.push(`• [${String(lbl).toUpperCase()}] ${n.title}${n.source ? ` — ${n.source}` : ''}`)
              })
            }
            // Proposed trade levels
            if (o.proposed_entry) {
              lines.push('')
              lines.push(`Entry:  ${o.proposed_entry}  (${o.side?.toUpperCase()})`)
              if (o.sl)             lines.push(`SL:     ${o.sl}`)
              if (o.tp1)            lines.push(`TP1:    ${o.tp1}`)
              if (o.tp2)            lines.push(`TP2:    ${o.tp2}`)
            }
            if (o.confirm_command) {
              lines.push('')
              lines.push(`To trade, type:`)
              lines.push(`\`${o.confirm_command}\``)
            }
            reply = lines.join('\n')
          } else if (d.action === 'execute') {
            // Clean order confirmation card
            const sym    = o.symbol  || ''
            const side   = (o.side   || '').toUpperCase()
            const sz     = o.size    || ''
            const px     = o.price   ? `@ ${o.price}` : '@ market'
            const oid    = o.id      || '—'
            const lines  = [
              `**${sym} ${side}**  ${sz} contracts ${px}`,
            ]
            if (o.sl)  lines.push(`SL   ${o.sl}`)
            if (o.tp1) lines.push(`TP1  ${o.tp1}`)
            if (o.tp2) lines.push(`TP2  ${o.tp2}${o.tp2_id === 'pending' ? '  (being set)' : ''}`)
            lines.push('')
            lines.push(`Order ID: ${oid}`)
            if (d.detail && d.detail.includes('Auto-resized')) {
              // Show auto-resize note if it happened
              const noteMatch = d.detail.match(/Auto-resized[^.]+\.$/)
              if (noteMatch) lines.push(noteMatch[0])
            }
            reply = lines.join('\n')
          } else {
            // set_tp / set_sl / close / list_positions / analyze
            reply = d.speech || d.detail || 'Done.'
          }
        } else {
          // Error — clean, no raw code dump
          const msg = d.speech || d.detail || 'Unknown error'
          reply = `Could not execute: ${msg}`
        }
        setMessages(prev => prev.map(m =>
          m.id === loadingMsg.id ? { ...m, content: reply, pending: false } : m
        ))
      } catch (err: any) {
        setMessages(prev => prev.map(m =>
          m.id === loadingMsg.id
            ? { ...m, content: `Could not reach backend: ${err?.message || err}`, pending: false }
            : m
        ))
      }
      return
    }

    const userMsg: Message = { id: nanoid(), role: 'user', content: text }
    const assistantMsg: Message = { id: nanoid(), role: 'assistant', content: '', pending: true }

    setMessages(prev => [...prev, userMsg, assistantMsg])
    setStreaming(true)

    const historyForApi = [...messages, userMsg]
      .filter(m => !m.pending)
      .slice(-12)  // keep last 12 for context window
      .map(m => ({ role: m.role, content: m.content }))

    try {
      abortRef.current = new AbortController()
      const resp = await fetch(
        `${process.env.NEXT_PUBLIC_API_URL || 'http://localhost:1448/api/v1'}/plugins/agent-paul/chat`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ messages: historyForApi, pathname: router.pathname, session_key: sessionKeyRef.current }),
          signal: abortRef.current.signal,
        }
      )

      if (!resp.ok || !resp.body) throw new Error('Stream failed')

      const reader = resp.body.getReader()
      const decoder = new TextDecoder()
      let accumulated = ''

      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        const chunk = decoder.decode(value)
        for (const line of chunk.split('\n')) {
          if (!line.startsWith('data:')) continue
          try {
            const data = JSON.parse(line.slice(5).trim())
            if (data.delta) {
              accumulated += data.delta
              setMessages(prev => prev.map(m =>
                m.id === assistantMsg.id ? { ...m, content: accumulated, pending: false } : m
              ))
            }
            if (data.error) {
              accumulated = `⚠️ ${data.error}`
              setMessages(prev => prev.map(m =>
                m.id === assistantMsg.id ? { ...m, content: accumulated, pending: false } : m
              ))
            }
            if (data.done) break
          } catch { /* ignore malformed */ }
        }
      }

      // ── Self-learning: capture this exchange to the Obsidian vault ─────────
      // Fire-and-forget so it never blocks the UI. Only for substantial answers.
      if (accumulated && accumulated.length > 100 && text.length > 5 && !accumulated.startsWith('⚠️')) {
        apiClient.obsidian.jarvisLearn({
          question: text,
          answer: accumulated,
          page: router.pathname,
        }).catch(() => { /* silent */ })
      }
    } catch (err: any) {
      if (err?.name !== 'AbortError') {
        setMessages(prev => prev.map(m =>
          m.id === assistantMsg.id
            ? { ...m, content: '⚠️ Connection error. Is the backend running?', pending: false }
            : m
        ))
      }
    } finally {
      setStreaming(false)
    }
  }, [input, streaming, messages, router.pathname])

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() }
    if (e.key === 'Escape') setOpen(false)
  }

  // ── Keep refs in sync for use inside speech callbacks ─────────────────────
  useEffect(() => { sendRef.current = (t?: string) => { void send(t) } }, [send])
  // Flush any voice/extension command that arrived while the previous response
  // was streaming, the moment streaming ends — so queued speech is never lost.
  useEffect(() => {
    if (streaming) return
    if (pendingSendQueueRef.current.length === 0) return
    const next = pendingSendQueueRef.current.shift()
    if (next) {
      const t = setTimeout(() => { sendRef.current(next) }, 250)
      return () => clearTimeout(t)
    }
  }, [streaming])
  useEffect(() => {
    voiceEnabledRef.current = voiceEnabled
    if (typeof window !== 'undefined') localStorage.setItem('paul.voice', voiceEnabled ? '1' : '0')
  }, [voiceEnabled])
  useEffect(() => {
    wakeEnabledRef.current = wakeEnabled
    if (typeof window !== 'undefined') localStorage.setItem('paul.wake', wakeEnabled ? '1' : '0')
  }, [wakeEnabled])
  useEffect(() => {
    wakeRequireGreetingRef.current = wakeRequireGreeting
    if (typeof window !== 'undefined') localStorage.setItem('paul.wakeRequireGreeting', wakeRequireGreeting ? '1' : '0')
  }, [wakeRequireGreeting])
  useEffect(() => { voiceMatchEnabledRef.current = voiceMatchEnabled }, [voiceMatchEnabled])

  // ── Load the per-user learned vocabulary once on mount ────────────────────
  // Priority order (highest wins): localStorage → IndexedDB → Vault brain.
  // The vault brain is the ultimate fallback — it persists even if IDB and LS
  // are both cleared, ensuring learning is NEVER permanently lost.
  useEffect(() => {
    const ls = loadLearnedWords()
    learnedWordsRef.current = ls

    // Layer 2: IndexedDB backup
    _idbLoad().then(async (idbData) => {
      const merged = { ...learnedWordsRef.current }
      if (idbData) {
        for (const [k, v] of Object.entries(idbData)) {
          if (typeof v === 'number' && v > (merged[k] || 0)) merged[k] = v
        }
      }

      // Layer 3: Vault brain (ultimate permanent storage)
      try {
        const brainRes = await apiClient.jarvis.voiceBrainLoad()
        const brainVocab: Record<string, number> = brainRes.data?.vocabulary || {}
        for (const [k, v] of Object.entries(brainVocab)) {
          if (typeof v === 'number' && v > (merged[k] || 0)) merged[k] = v
        }
        // Restore voice profile from brain if no local profile exists
        const brainProfile = brainRes.data?.profile
        if (brainProfile?.bands && !loadVoiceProfile()) {
          saveVoiceProfile({
            bands:        brainProfile.bands,
            bandStdDev:   brainProfile.bandStdDev,
            centroid:     brainProfile.centroid ?? 0,
            minEnergy:    0,
            calibratedAt: brainProfile.calibratedAt ?? Date.now(),
          })
        }
      } catch { /* backend offline — continue with local data */ }

      learnedWordsRef.current = merged
      const lsTotal     = Object.values(ls).reduce((a, b) => a + b, 0)
      const mergedTotal = Object.values(merged).reduce((a, b) => a + b, 0)
      if (mergedTotal > lsTotal) persistLearnedWords(merged)
    }).catch(() => { /* IDB unavailable — localStorage only */ })
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // ── Voice Brain sync — pushes vocabulary + fingerprint to the permanent vault ─
  // Runs every BRAIN_SYNC_EVERY utterances and on page unload.
  // The vault note is NEVER deleted; each sync merges (takes the maximum count
  // per word) so the brain only grows smarter, never loses data.
  const syncVoiceBrain = useCallback(async () => {
    if (brainSyncPendingRef.current) return
    brainSyncPendingRef.current = true
    try {
      const vocab   = learnedWordsRef.current
      const profile = loadVoiceProfile()
      await apiClient.jarvis.voiceBrainSync({
        vocabulary: vocab,
        profile: profile
          ? {
              bands:      profile.bands,
              bandStdDev: profile.bandStdDev,
              centroid:   profile.centroid,
              sessions:   profile.calibratedAt ? 1 : 0,
            }
          : undefined,
        sessions: utteranceCountRef.current,
      })
    } catch { /* best-effort — never block voice recognition for a sync failure */ }
    finally { brainSyncPendingRef.current = false }
  }, [])

  // Correct a freshly recognised transcript toward the user's learned vocabulary,
  // then record its words so future recognition keeps improving. Returns the
  // corrected text. This is the single entry point for every voice path so the
  // assistant learns your words and self-corrects the more you talk to it.
  const learnAndCorrect = useCallback((text: string): string => {
    const t = (text || '').trim()
    if (!t) return t
    const words = learnedWordsRef.current
    const corrected = correctWithVocab(t, words)
    learnFromText(words, corrected)
    if (learnPersistTimerRef.current) clearTimeout(learnPersistTimerRef.current)
    learnPersistTimerRef.current = setTimeout(() => persistLearnedWords(words), 1500)
    // ── Increment utterance counter; sync brain every BRAIN_SYNC_EVERY calls ─
    utteranceCountRef.current += 1
    if (utteranceCountRef.current % BRAIN_SYNC_EVERY === 0) {
      syncVoiceBrain()  // fire-and-forget
    }
    return corrected
  }, [syncVoiceBrain])

  // From a multi-alternative SpeechRecognition result, pick the transcript that
  // contains the most words the user is known to use — cleaner, more accurate
  // recognition that improves as the learned vocabulary grows. Falls back to the
  // engine's top guess when no alternative is clearly better.
  const pickAlternative = useCallback((
    result: ArrayLike<{ transcript?: string; confidence?: number }> | undefined,
  ): string => {
    if (!result || !result.length) return ''
    const words = learnedWordsRef.current
    let best = result[0]?.transcript || ''
    let bestScore = -1
    for (let k = 0; k < result.length; k++) {
      const alt = result[k]
      if (!alt) continue
      const toks = tokenizeWords(alt.transcript || '')
      const known = toks.reduce((n, w) => n + ((words[w] || 0) > 0 ? 1 : 0), 0)
      // Prefer more known words; tie-break on the engine's own confidence.
      const score = known * 10 + (typeof alt.confidence === 'number' ? alt.confidence : 0)
      if (score > bestScore) { bestScore = score; best = alt.transcript || best }
    }
    return best
  }, [])

  // ── Deepgram fallback: rolling audio buffer + miss escalation ─────────────
  // Arm a MediaRecorder ring buffer on a dedicated mic stream so a short clip of
  // the *just-spoken* utterance is always available. Idempotent + best-effort:
  // any failure leaves JARVIS on the free Web Speech API with no error surfaced.
  const armDeepgramBuffer = useCallback(async () => {
    if (dgArmedRef.current || typeof navigator === 'undefined' || !navigator.mediaDevices) return
    if (typeof MediaRecorder === 'undefined') return
    dgArmedRef.current = true
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      })
      dgStreamRef.current = stream
      const mime = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus']
        .find(m => { try { return MediaRecorder.isTypeSupported(m) } catch { return false } })
      const rec = mime ? new MediaRecorder(stream, { mimeType: mime }) : new MediaRecorder(stream)
      rec.ondataavailable = (ev: BlobEvent) => {
        if (!ev.data || ev.data.size === 0) return
        const now = Date.now()
        dgChunksRef.current.push({ t: now, blob: ev.data })
        // Drop chunks older than the ring-buffer window.
        const cutoff = now - DG_BUFFER_MS
        while (dgChunksRef.current.length && dgChunksRef.current[0].t < cutoff) {
          dgChunksRef.current.shift()
        }
      }
      dgRecorderRef.current = rec
      rec.start(1000)  // 1s timeslice → steady ring buffer
      setDgFallbackState('armed')
    } catch {
      // No mic permission / unsupported — disable the fallback silently.
      dgArmedRef.current = false
      setDgFallbackState('off')
    }
  }, [])

  const disarmDeepgramBuffer = useCallback(() => {
    dgArmedRef.current = false
    try { dgRecorderRef.current?.stop() } catch { /* noop */ }
    try { dgStreamRef.current?.getTracks().forEach(t => t.stop()) } catch { /* noop */ }
    dgRecorderRef.current = null
    dgStreamRef.current = null
    dgChunksRef.current = []
    dgMissCountRef.current = 0
    setDgFallbackState('off')
  }, [])

  // Escalate a single missed utterance to Deepgram pre-recorded STT. Sends only
  // the buffered clip; on a successful transcript it is dispatched through the
  // normal voice-command path (which learns + auto-corrects). Silent on cap/err.
  const escalateToDeepgram = useCallback(async (_reason: string) => {
    if (!dgArmedRef.current || dgInFlightRef.current) return
    // The extension owns the mic when connected — let it handle escalation.
    if (extConnectedRef.current) return
    // Speaker gate: only ever send the user's OWN voice to Deepgram. When
    // voice-ID is enabled and its analyser is live, require a positive match so
    // a TV / other person's speech is never transcribed (or charged) by the
    // fallback. With voice-ID off (no profile) this is a no-op and all misses
    // can still escalate, exactly like the free engine.
    if (voiceMatchEnabledRef.current && voiceAudioCtxRef.current && !voiceMatchRef.current) {
      dgMissCountRef.current = 0
      return
    }
    const chunks = dgChunksRef.current.map(c => c.blob)
    if (!chunks.length) return
    const type = dgRecorderRef.current?.mimeType || 'audio/webm'
    const clip = new Blob(chunks, { type })
    if (clip.size < DG_MIN_CLIP_BYTES) return  // too short → no spend
    dgInFlightRef.current = true
    try {
      const res = await apiClient.deepgram.sttFallback(clip)
      if (res?.used_deepgram && res.text && res.text.trim()) {
        dgMissCountRef.current = 0
        setDgFallbackState('armed')
        dispatchVoiceCommandRef.current(res.text.trim())
      } else if (res && res.used_deepgram === false && res.reason === 'budget_capped') {
        setDgFallbackState('paused')  // cap reached — stay on free engine
      }
    } catch {
      /* network/Deepgram error → silent degrade to the free engine */
    } finally {
      dgInFlightRef.current = false
    }
  }, [])
  dgEscalateRef.current = escalateToDeepgram

  // Record a recognition miss; escalate to Deepgram on a clear low-confidence
  // miss or after N consecutive misses. A success elsewhere resets the counter.
  const noteVoiceMiss = useCallback((reason: 'low_confidence' | 'empty' | 'short_command' | 'manual') => {
    if (!dgArmedRef.current || extConnectedRef.current) return
    dgMissCountRef.current += 1
    if (reason === 'low_confidence' || reason === 'manual' || dgMissCountRef.current >= DG_MISS_ESCALATE_AFTER) {
      dgEscalateRef.current(reason)
    }
  }, [])
  const noteVoiceMissRef = useRef(noteVoiceMiss)
  noteVoiceMissRef.current = noteVoiceMiss

  // Arm/disarm the ring buffer with the listening lifecycle. The extension, when
  // connected, owns the mic and its own escalation — so we don't double-listen.
  useEffect(() => {
    const shouldArm = (voiceEnabled || wakeEnabled) && !extConnected
    if (shouldArm) armDeepgramBuffer()
    else disarmDeepgramBuffer()
    return () => { /* cleanup handled by disarm on dep change / unmount below */ }
  }, [voiceEnabled, wakeEnabled, extConnected, armDeepgramBuffer, disarmDeepgramBuffer])

  // Final safety net: release the mic on unmount.
  useEffect(() => () => { disarmDeepgramBuffer() }, [disarmDeepgramBuffer])

  // Refresh the Deepgram budget summary when the chat opens and after each
  // escalation result (the status badge transitions). Best-effort + silent.
  const refreshDgUsage = useCallback(async () => {
    try {
      const u = await apiClient.deepgram.usage()
      setDgUsage({
        remaining: u.remaining,
        monthly_cap: u.monthly_cap,
        projected_runway_days: u.projected_runway_days,
      })
    } catch { /* backend offline / not configured → leave as-is */ }
  }, [])
  useEffect(() => { if (open) refreshDgUsage() }, [open, dgFallbackState, refreshDgUsage])

  useEffect(() => { openRef.current = open }, [open])
  useEffect(() => { listeningRef.current = listening }, [listening])

  // Persist + mirror voice-selection prefs into refs (used inside speak()).
  useEffect(() => {
    voiceURIRef.current = voiceURI
    if (typeof window !== 'undefined') localStorage.setItem('paul.voiceURI', voiceURI)
  }, [voiceURI])
  useEffect(() => {
    voiceRateRef.current = voiceRate
    if (typeof window !== 'undefined') localStorage.setItem('paul.voiceRate', String(voiceRate))
  }, [voiceRate])
  useEffect(() => {
    voicePitchRef.current = voicePitch
    if (typeof window !== 'undefined') localStorage.setItem('paul.voicePitch', String(voicePitch))
  }, [voicePitch])
  useEffect(() => {
    if (typeof window !== 'undefined') localStorage.setItem('paul.voiceGender', voiceGender)
  }, [voiceGender])

  // Persist + sync noise gate threshold
  useEffect(() => {
    noiseThresholdRef.current = noiseThreshold
    if (typeof window !== 'undefined') localStorage.setItem('paul.noiseThreshold', String(noiseThreshold))
  }, [noiseThreshold])
  useEffect(() => {
    if (typeof window !== 'undefined') localStorage.setItem('paul.autoNoise', autoNoise ? '1' : '0')
  }, [autoNoise])

  useEffect(() => {
    if (typeof window !== 'undefined') localStorage.setItem('paul.aiSpeechEnabled', aiSpeechEnabled ? '1' : '0')
  }, [aiSpeechEnabled])
  useEffect(() => {
    if (typeof window !== 'undefined') localStorage.setItem('paul.aiVoiceEnabled', aiVoiceEnabled ? '1' : '0')
  }, [aiVoiceEnabled])
  useEffect(() => {
    if (typeof window !== 'undefined') localStorage.setItem('paul.aiVoice', aiVoice)
  }, [aiVoice])
  // Persist + sync the robot avatar style. Also relay to the extension so the
  // in-page robot and the popup avatar picker stay in agreement.
  useEffect(() => {
    if (typeof window === 'undefined') return
    localStorage.setItem('paul.avatarStyle', avatarStyle)
    try {
      window.postMessage({ __jarvisPage: true, type: 'avatar-style', style: avatarStyle }, window.location.origin)
    } catch { /* noop */ }
  }, [avatarStyle])

  // Load the system voice list (async — fires onvoiceschanged) and auto-pick a
  // great JARVIS voice the first time if the user hasn't chosen one yet.
  useEffect(() => {
    if (typeof window === 'undefined' || !('speechSynthesis' in window)) return
    const load = () => {
      const all = window.speechSynthesis.getVoices()
      const en = all.filter(v => /^en[-_]/i.test(v.lang) || /english/i.test(v.name))
      setVoiceList(en.length ? en : all)
      if (!voiceURIRef.current && en.length) {
        const pick = pickJarvisVoice(en, voiceGender)
        if (pick) setVoiceURI(pick.voiceURI)
      }
    }
    load()
    window.speechSynthesis.onvoiceschanged = load
    return () => { try { window.speechSynthesis.onvoiceschanged = null } catch { /* noop */ } }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // ── Detect Web Speech API support on mount ────────────────────────────────
  useEffect(() => {
    setSpeechSupported(!!getSpeechRecognition() && typeof window !== 'undefined' && 'speechSynthesis' in window)
  }, [])

  // Barge-in is possible only when we can reliably tell the user apart from
  // JARVIS's own voice — i.e. speaker-ID (voice match) is enabled AND a profile
  // exists. Without it we fall back to fully muting the mic during speech.
  const canBargeIn = useCallback(() => {
    return voiceMatchEnabledRef.current && !!loadVoiceProfile()
  }, [])

  // ── Face Vision (camera) gates ────────────────────────────────────────────
  // FaceVisionPanel broadcasts lip/identity state via postMessage. While the
  // camera is live ("fresh"), the user's moving mouth is the definitive signal
  // that the USER — not JARVIS's TTS — is talking.
  const faceFresh = useCallback(() => {
    const f = faceStateRef.current
    return f.ts > 0 && Date.now() - f.ts < FACE_FRESH_MS
  }, [])
  // True when the camera positively sees the user talking right now. An
  // enrolled profile with a non-matching face means a stranger → not the user.
  const cameraSeesUserTalking = useCallback(() => {
    const f = faceStateRef.current
    if (!faceFresh() || !f.talking) return false
    return f.match || !f.enrolled
  }, [faceFresh])
  // Camera off/stale → no visual gating (audio-only behaviour). Camera live →
  // hear ONLY while the mouth is (or was just) moving on camera.
  const mouthGateOpen = useCallback(() => {
    if (!faceFresh()) return true
    return faceStateRef.current.talking || Date.now() - lastMouthActiveAtRef.current < MOUTH_WINDOW_MS
  }, [faceFresh])

  // ── Mic gating while JARVIS speaks ────────────────────────────────────────
  // Always aborts any active dictation/Whisper capture so a command-in-progress
  // never records JARVIS's own voice. When speaker-ID is enabled we KEEP the wake
  // recognizer alive so the user can still interrupt by voice — the stored-voice
  // gate rejects JARVIS's own TTS, so it never self-triggers. When speaker-ID is
  // off we fully stop the wake recognizer (zero self-hearing). The extension is
  // told whether barge-in is allowed via the `speak-status` message in speak().
  const muteMicForSpeech = useCallback(() => {
    const bargeIn = canBargeIn()
    // Abort active capture either way (never record our own voice mid-command).
    try { dictationRef.current?.stop() } catch { /* noop */ }
    try {
      const rec = whisperRecorderRef.current
      if (rec && rec.state !== 'inactive') {
        whisperAbortedRef.current = true  // discard the capture in onstop
        rec.stop()
      }
    } catch { /* noop */ }
    try { whisperStreamRef.current?.getTracks().forEach(t => t.stop()) } catch { /* noop */ }
    setRecording(false)
    // Arm the post-speech mic blackout gate so any echo after onend is discarded.
    micGatedRef.current = true
    clearTimeout(postSpeechGateRef.current ?? undefined)
    if (bargeIn) {
      // Keep the wake listener running so the user's voice can interrupt JARVIS.
      if (wakeEnabledRef.current && !extVoiceReadyRef.current && !wakeRef.current) {
        try { startWakeRef.current() } catch { /* noop */ }
      }
    } else {
      try { wakeRef.current?.stop() } catch { /* noop */ }
      setListening(false)
      listeningRef.current = false
    }
  }, [canBargeIn])

  // ── Speak (TTS) — JARVIS-grade, using the chosen voice + tuned prosody ────
  const speak = useCallback(async (text: string, onEnd?: () => void) => {
    if (typeof window === 'undefined') return
    const clean = cleanForSpeech(text)
    if (!clean) return
    // Silence the mic for the duration of speech. When speaker-ID is on we keep
    // listening for the user's voice so they can interrupt (barge-in); otherwise
    // we go fully deaf so JARVIS never hears itself.
    muteMicForSpeech()
    const allowBargeIn = canBargeIn()
    const emitSpeaking = (speaking: boolean) => {
      try {
        // `allowBargeIn` tells the extension whether to keep its mic open during
        // speech (so the user can cut in) or fully stop until JARVIS finishes.
        window.postMessage(
          { __jarvisPage: true, type: 'speak-status', speaking, allowBargeIn, text: speaking ? clean : undefined },
          window.location.origin,
        )
      } catch { /* noop */ }
    }

    // A: High-quality AI Voice (OpenAI TTS)
    if (aiVoiceEnabled) {
      emitSpeaking(true)
      isSpeakingRef.current = true
      interruptRef.current = false
      try {
        const formData = new FormData()
        formData.append('text', clean)
        formData.append('voice', aiVoice)

        const resp = await fetch(
          `${process.env.NEXT_PUBLIC_API_URL || 'http://localhost:1448/api/v1'}/voice/tts`,
          {
            method: 'POST',
            body: formData,
          }
        )
        if (!resp.ok) throw new Error('AI TTS failed')
        const blob = await resp.blob()
        const url = URL.createObjectURL(blob)
        const audio = new Audio(url)
        audio.onerror = () => {
          isSpeakingRef.current = false
          interruptRef.current = false
          emitSpeaking(false)
          try { URL.revokeObjectURL(url) } catch { /* noop */ }
          // Recover the mic after echo-tail blackout (900ms = matches extension gate)
          clearTimeout(postSpeechGateRef.current ?? undefined)
          postSpeechGateRef.current = setTimeout(() => {
            micGatedRef.current = false
            if (wakeEnabledRef.current && !listeningRef.current) startWake()
          }, 900)
        }
        audio.onended = () => {
          isSpeakingRef.current = false
          interruptRef.current = false
          emitSpeaking(false)
          URL.revokeObjectURL(url)
          onEnd?.()
          clearTimeout(postSpeechGateRef.current ?? undefined)
          postSpeechGateRef.current = setTimeout(() => {
            micGatedRef.current = false
            if (wakeEnabledRef.current && !listeningRef.current) startWake()
          }, 900)
        }
        audio.play()
        const checker = setInterval(() => {
          if (interruptRef.current) {
            audio.pause()
            clearInterval(checker)
            isSpeakingRef.current = false
            emitSpeaking(false)
            URL.revokeObjectURL(url)
          }
        }, 50)
        return
      } catch (err) {
        console.error('AI TTS failed, falling back to system voice', err)
      }
    }

    // B: System voice fallback (Web Speech API)
    if (!('speechSynthesis' in window)) return
    window.speechSynthesis.cancel()
    emitSpeaking(true)
    interruptRef.current = false
    const u = new SpeechSynthesisUtterance(clean)
    const voices = window.speechSynthesis.getVoices()
    let chosen = voiceURIRef.current ? voices.find(v => v.voiceURI === voiceURIRef.current) : undefined
    if (!chosen) chosen = pickJarvisVoice(voices, 'male') || undefined
    if (chosen) { u.voice = chosen; u.lang = chosen.lang }
    else u.lang = 'en-GB'
    u.rate = voiceRateRef.current || 0.96
    u.pitch = voicePitchRef.current || 0.9
    u.volume = 1
    isSpeakingRef.current = true
    u.onend = () => {
      isSpeakingRef.current = false
      interruptRef.current = false
      emitSpeaking(false)
      onEnd?.()
      // Resume wake listening after echo-tail blackout (900ms matches extension gate)
      clearTimeout(postSpeechGateRef.current ?? undefined)
      postSpeechGateRef.current = setTimeout(() => {
        micGatedRef.current = false
        if (wakeEnabledRef.current && !listeningRef.current) startWake()
      }, 900)
    }
    u.onerror = () => {
      isSpeakingRef.current = false
      emitSpeaking(false)
      clearTimeout(postSpeechGateRef.current ?? undefined)
      postSpeechGateRef.current = setTimeout(() => {
        micGatedRef.current = false
        if (wakeEnabledRef.current && !listeningRef.current) startWake()
      }, 900)
    }
    window.speechSynthesis.speak(u)
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [aiVoiceEnabled, aiVoice])

  // Interrupt JARVIS mid-speech and immediately start listening (human-like interruptibility)
  const interruptSpeech = useCallback(() => {
    if (typeof window === 'undefined') return
    // Abort both speech paths: AI <audio> (watched via interruptRef) and the
    // Web Speech synthesiser.
    interruptRef.current = true
    if ('speechSynthesis' in window) window.speechSynthesis.cancel()
    isSpeakingRef.current = false
    // Tell the extension JARVIS has stopped talking so it resumes normal
    // listening immediately. Without this the extension keeps pageSpeaking=true
    // (because synthesis.cancel() fires no onend) and goes deaf.
    try {
      window.postMessage({ __jarvisPage: true, type: 'speak-status', speaking: false }, window.location.origin)
    } catch { /* noop */ }
  }, [])

  // ── Face Vision sync + camera barge-in ─────────────────────────────────────
  // Consumes FaceVisionPanel's `jarvis-face-state` broadcasts. When the camera
  // sees the USER start talking while JARVIS is reading a reply out loud, the
  // TTS is cut immediately (barge-in) and dictation starts — so JARVIS stops
  // reading and transcribes the user instead of its own voice.
  useEffect(() => {
    const onFaceMsg = (ev: MessageEvent) => {
      const d: any = ev.data
      if (ev.source !== window || !d || !d.__jarvisPage || d.type !== 'jarvis-face-state') return
      faceStateRef.current = {
        present: !!d.facePresent,
        talking: !!d.isTalking,
        match: !!d.identityMatch,
        enrolled: !!d.enrolled,
        ts: Date.now(),
      }
      if (d.isTalking) lastMouthActiveAtRef.current = Date.now()
      // Camera barge-in: the user's mouth is moving while JARVIS is speaking →
      // stop reading NOW and hand the mic to the user.
      if (isSpeakingRef.current && cameraSeesUserTalking()) {
        interruptSpeech()
        micGatedRef.current = false  // camera confirmed it's the user — skip the echo blackout
        if (!extVoiceReadyRef.current && !listeningRef.current) {
          setTimeout(() => startDictationRef.current(), 150)
        }
      }
    }
    window.addEventListener('message', onFaceMsg)
    return () => window.removeEventListener('message', onFaceMsg)
  }, [cameraSeesUserTalking, interruptSpeech])


  // ── Mini binary engine + robot energy feed ─────────────────────────────────
  // Reads directly from voiceAnalyserRef (same stream as speaker-ID).
  // Always computes a 0..1 energy value to drive the 3D robot, and additionally
  // draws the legacy frequency bars when the (optional) mini canvas is present.
  useEffect(() => {
    const BANDS = 16
    let energySmooth = 0
    const tick = () => {
      miniRafRef.current = requestAnimationFrame(tick)
      const analyser = voiceAnalyserRef.current
      const buf      = voiceBufRef.current as Uint8Array<ArrayBuffer> | null

      // Compute live energy (drives the robot) even when no canvas is mounted
      if (analyser && buf) {
        analyser.getByteFrequencyData(buf)
        let sum = 0
        for (let j = 0; j < buf.length; j++) sum += buf[j]
        const avg = sum / buf.length / 255          // 0..1
        energySmooth += (avg - energySmooth) * 0.25
      } else {
        energySmooth += (0 - energySmooth) * 0.1
      }
      robotEnergyRef.current = energySmooth
      // Throttle React state writes for the robot (it reads via prop ~ every frame
      // is fine but we only need ~20fps for smoothness without re-render churn)
      setRobotEnergy(prev => (Math.abs(prev - energySmooth) > 0.02 ? energySmooth : prev))

      const canvas = miniCanvasRef.current
      if (!canvas) return
      const ctx = canvas.getContext('2d')
      if (!ctx) return
      if (!analyser || !buf) {
        ctx.clearRect(0, 0, canvas.width, canvas.height)
        return
      }
      const W = canvas.width, H = canvas.height
      ctx.clearRect(0, 0, W, H)
      const binSize = Math.floor(buf.length / BANDS)
      const raw = Array.from({ length: BANDS }, (_, b) => {
        let s = 0
        for (let j = b * binSize; j < Math.min((b + 1) * binSize, buf.length); j++) s += buf[j]
        return s / binSize
      })
      const mx = Math.max(...raw, 1)
      const isSpeaking = isSpeakingRef.current
      raw.forEach((val, i) => {
        const norm  = val / mx
        const barH  = Math.round(norm * H)
        const x     = Math.round(i * (W / BANDS))
        const bw    = Math.max(1, Math.floor(W / BANDS) - 1)
        const color = isSpeaking
          ? `rgba(245,158,11,${0.4 + norm * 0.6})`
          : `rgba(6,182,212,${0.35 + norm * 0.65})`
        ctx.fillStyle = color
        ctx.fillRect(x, H - barH, bw, barH)
      })
    }
    tick()
    return () => cancelAnimationFrame(miniRafRef.current)
  // Only recreate when the canvas mounts; reads refs directly
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // ── Voice profile: start background speaker-ID analyser ───────────────────
  // Runs an AudioContext analyser in parallel with SpeechRecognition.
  // Sets voiceMatchRef.current = true when audio matches the stored profile.
  const startVoiceMatching = useCallback(async () => {
    const profile = loadVoiceProfile()
    if (!profile || !voiceMatchEnabled) { voiceMatchRef.current = true; return }
    if (voiceAudioCtxRef.current) return  // already running
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true }
      })
      voiceStreamRef.current = stream
      const ctx = new (window.AudioContext || (window as any).webkitAudioContext)()
      voiceAudioCtxRef.current = ctx
      const analyser = ctx.createAnalyser()
      analyser.fftSize = 512
      analyser.smoothingTimeConstant = 0.80
      voiceAnalyserRef.current = analyser
      const buf = new Uint8Array(analyser.frequencyBinCount)
      voiceBufRef.current = buf
      ctx.createMediaStreamSource(stream).connect(analyser)
      // Last match state broadcast to the extension — only post on change so the
      // extension's `pageVoiceMatch` (which gates its Deepgram escalation) stays
      // in sync without flooding the page with messages every frame.
      let lastSentMatch: boolean | null = null
      const check = () => {
        if (!voiceAudioCtxRef.current) return
        const bands = extractBands(analyser, buf)
        const energy = (voiceBufRef.current as Uint8Array).reduce((s, v) => s + v, 0) / (voiceBufRef.current as Uint8Array).length
        // Only score frames with clear speech energy — ignore silence/noise floor
        if (energy >= profile.minEnergy * 0.55) {
          const sim = voiceSimilarity(bands, profile)
          const frameMatch = sim >= 0.58  // statistical threshold — balanced for real-world use
          // Rolling 30-frame window (~1 second at 30fps) — temporal consistency
          // Prevents TV/background voices matching a single unlucky frame
          voiceMatchWindowRef.current.push(frameMatch)
          if (voiceMatchWindowRef.current.length > 30) voiceMatchWindowRef.current.shift()

          // ── Continuous learning ──────────────────────────────────────────
          // When a frame matches the user strongly AND JARVIS is not speaking
          // (so we never train on the assistant's own TTS), gently blend it into
          // the stored profile so the fingerprint keeps adapting to the user's
          // voice over time. Persist at most once every 5s to avoid churn.
          if (sim >= 0.72 && !isSpeakingRef.current) {
            const adapted = adaptVoiceProfile(profile, bands)
            profile.bands = adapted.bands
            profile.bandStdDev = adapted.bandStdDev
            profile.centroid = adapted.centroid
            const now = Date.now()
            if (now - profilePersistAtRef.current > 5000) {
              profilePersistAtRef.current = now
              saveVoiceProfile(profile)
            }
          }
        }
        // Require 62% of recent frames to match (Siri-style temporal averaging)
        const win = voiceMatchWindowRef.current
        if (win.length >= 12) {
          voiceMatchRef.current = win.filter(Boolean).length / win.length >= 0.55
        } else if (energy >= profile.minEnergy * 0.55) {
          // Window still filling — use a strict single-frame threshold
          voiceMatchRef.current = voiceSimilarity(bands, profile) >= 0.60
        }
        // Mirror the live speaker-ID result to the extension so its Deepgram
        // escalation only fires for the calibrated user's voice too.
        if (extConnectedRef.current && voiceMatchRef.current !== lastSentMatch) {
          lastSentMatch = voiceMatchRef.current
          try { window.postMessage({ __jarvisPage: true, type: 'voice-match-update', isMatch: voiceMatchRef.current }, window.location.origin) } catch { /* noop */ }
        }
        voiceRafRef.current = requestAnimationFrame(check)
      }
      voiceMatchWindowRef.current = []  // reset rolling window on start
      check()
    } catch {
      voiceMatchRef.current = true  // no mic access — accept all
      // Tell the extension to accept all (no speaker-ID available here).
      if (extConnectedRef.current) {
        try { window.postMessage({ __jarvisPage: true, type: 'voice-match-update', isMatch: true }, window.location.origin) } catch { /* noop */ }
      }
    }
  }, [voiceMatchEnabled])

  const stopVoiceMatching = useCallback(() => {
    if (voiceRafRef.current) cancelAnimationFrame(voiceRafRef.current)
    try { voiceAudioCtxRef.current?.close() } catch { /* noop */ }
    try { voiceStreamRef.current?.getTracks().forEach(t => t.stop()) } catch { /* noop */ }
    voiceAudioCtxRef.current = null; voiceStreamRef.current = null
    voiceAnalyserRef.current = null; voiceRafRef.current = null
    voiceMatchWindowRef.current = []  // clear rolling window
    voiceMatchRef.current = true  // reset to accept-all when stopped
  }, [])

  // ── Voice profile calibration ─────────────────────────────────────────────
  // Records the user's voice for 8 seconds → builds a frequency fingerprint.
  // IMPORTANT: We must NOT call speak() while the mic is recording because
  // TTS audio bleeds into the mic and corrupts the voice profile fingerprint.
  // Instead we use a silent 3-second visual countdown before recording starts.
  const calibrateVoice = useCallback(async () => {
    if (calibrating) return
    setCalibrating(true)
    // Stop any current TTS immediately — we cannot record while speaking
    if (typeof window !== 'undefined' && 'speechSynthesis' in window) {
      window.speechSynthesis.cancel()
      isSpeakingRef.current = false
    }
    // Also stop the wake listener so it doesn't compete with the calibration mic
    try { wakeRef.current?.stop() } catch { /* noop */ }
    try { dictationRef.current?.stop() } catch { /* noop */ }

    // 3-second visual countdown before mic opens — gives TTS output time to
    // fully clear and lets the user prepare to speak.
    let countdown = 11  // 3s pre-count + 8s record
    setCalibCountdown(countdown)
    const preCountTick = setInterval(() => {
      countdown--
      setCalibCountdown(countdown)
      if (countdown <= 8) clearInterval(preCountTick)
    }, 1000)
    await new Promise<void>(resolve => setTimeout(resolve, 3000))
    clearInterval(preCountTick)

    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: true }
      })
      const ctx = new (window.AudioContext || (window as any).webkitAudioContext)()
      const analyser = ctx.createAnalyser()
      analyser.fftSize = 512; analyser.smoothingTimeConstant = 0.85
      ctx.createMediaStreamSource(stream).connect(analyser)
      const buf = new Uint8Array(analyser.frequencyBinCount)
      const samples: number[][] = []
      const energySamples: number[] = []
      let recCountdown = 8
      setCalibCountdown(recCountdown)
      const tick = setInterval(() => { recCountdown--; setCalibCountdown(recCountdown) }, 1000)
      const samplingInterval = setInterval(() => {
        const bands = extractBands(analyser, buf)
        const energy = buf.reduce((s, v) => s + v, 0) / buf.length
        if (energy > 8) { samples.push(bands); energySamples.push(energy) }
      }, 80)
      await new Promise<void>(resolve => setTimeout(resolve, 8000))
      clearInterval(samplingInterval); clearInterval(tick); setCalibCountdown(0)
      stream.getTracks().forEach(t => t.stop()); ctx.close()
      if (samples.length < 15) {
        // Now safe to speak — recording has fully stopped
        speak("I didn't capture enough voice data. Please try again and speak clearly, Sir.")
        setCalibrating(false); return
      }
      // Average the band energies across all samples
      const avgBands = Array(AUDIO_BANDS).fill(0).map((_, b) =>
        samples.reduce((s, sample) => s + sample[b], 0) / samples.length
      )
      const maxBand = Math.max(...avgBands, 1)
      const normalizedBands = avgBands.map(v => v / maxBand)

      // Per-band standard deviation — captures the natural variation range in
      // the user's voice. Used by voiceSimilarity() to scale tolerance per band,
      // so TV/other voices that differ in even ONE characteristic band are rejected.
      const bandStdDev = Array(AUDIO_BANDS).fill(0).map((_, b) => {
        const mean = normalizedBands[b]
        const variance = samples.reduce((s, sample) => s + (sample[b] / maxBand - mean) ** 2, 0) / samples.length
        return Math.max(0.03, Math.sqrt(variance))  // floor at 0.03 to avoid over-strict matching
      })

      const centroid = normalizedBands.reduce((s, v, i) => s + v * i, 0) /
        (AUDIO_BANDS * Math.max(normalizedBands.reduce((s, v) => s + v, 0), 0.01))
      const avgEnergy = energySamples.reduce((s, v) => s + v, 0) / energySamples.length
      const profile: VoiceProfile = {
        bands: normalizedBands,
        bandStdDev,
        centroid,
        minEnergy: avgEnergy * 0.25,
        calibratedAt: Date.now(),
      }
      saveVoiceProfile(profile)
      setVoiceProfile(profile)
      setVoiceMatchEnabled(true)
      localStorage.setItem('paul.voiceMatchEnabled', '1')
      // Safe to speak now — mic stream is fully stopped
      speak(`Voice profile saved, Sir. I captured ${samples.length} voice samples. I will now only respond to your specific voice and ignore background noise.`)
    } catch {
      speak('Could not access your microphone for calibration, Sir.')
    } finally {
      setCalibrating(false)
      // Re-arm the wake listener after calibration finishes
      if (wakeEnabledRef.current) setTimeout(() => startWake(), 800)
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [calibrating, speak])

  // ── Voice profile: test recognition against stored profile (3-second test) ──
  // Records 3 seconds and checks if the voice matches the stored profile.
  const [profileTesting, setProfileTesting] = useState(false)
  const [profileTestResult, setProfileTestResult] = useState<'match' | 'no_match' | null>(null)
  const testVoiceProfile = useCallback(async () => {
    if (profileTesting || !voiceProfile) return
    setProfileTesting(true)
    setProfileTestResult(null)
    // Stop TTS so it doesn't bleed into the test recording
    if (typeof window !== 'undefined' && 'speechSynthesis' in window) window.speechSynthesis.cancel()
    try { wakeRef.current?.stop() } catch { /* noop */ }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      const ctx = new (window.AudioContext || (window as any).webkitAudioContext)()
      const analyser = ctx.createAnalyser()
      analyser.fftSize = 512; analyser.smoothingTimeConstant = 0.80
      ctx.createMediaStreamSource(stream).connect(analyser)
      const buf = new Uint8Array(analyser.frequencyBinCount)
      const sims: number[] = []
      const samplingInterval = setInterval(() => {
        const bands = extractBands(analyser, buf)
        const energy = buf.reduce((s, v) => s + v, 0) / buf.length
        if (energy > voiceProfile.minEnergy * 0.4) {
          sims.push(voiceSimilarity(bands, voiceProfile))
        }
      }, 80)
      await new Promise<void>(resolve => setTimeout(resolve, 3000))
      clearInterval(samplingInterval)
      stream.getTracks().forEach(t => t.stop()); ctx.close()
      const avgSim = sims.length > 0 ? sims.reduce((a, b) => a + b, 0) / sims.length : 0
      const matched = avgSim > 0.65
      setProfileTestResult(matched ? 'match' : 'no_match')
      speak(matched
        ? `Voice match confirmed. Similarity ${Math.round(avgSim * 100)}%, Sir.`
        : `Voice match failed. Similarity only ${Math.round(avgSim * 100)}%. Try recalibrating, Sir.`
      )
    } catch {
      speak('Could not access your microphone for testing, Sir.')
    } finally {
      setProfileTesting(false)
      if (wakeEnabledRef.current) setTimeout(() => startWake(), 800)
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [profileTesting, voiceProfile, speak])

  // ── Start/stop voice matching when wake mode changes ──────────────────────
  // Runs the background speaker-ID analyser whenever JARVIS is listening for the
  // user — via the in-page wake recognizer OR the browser extension. Keeping it
  // running under the extension lets the page classify "is this the user or
  // JARVIS/background?" in real time and gate the extension's transcripts.
  useEffect(() => {
    if ((wakeEnabled || extVoiceReady) && voiceMatchEnabled && loadVoiceProfile()) {
      startVoiceMatching()
    } else {
      stopVoiceMatching()
    }
    return () => { stopVoiceMatching() }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wakeEnabled, extVoiceReady, voiceMatchEnabled])

  // ── Speak finished assistant replies when voice mode is on ────────────────
  // CRITICAL: never speak messages restored from history (fromHistory). JARVIS
  // only reads NEW responses generated live in this session — so a page refresh
  // never re-reads the previous answer. The user can explicitly ask “read that
  // again” to re-hear it (handled as a voice command).
  useEffect(() => {
    if (streaming) return
    const last = messages[messages.length - 1]
    if (!last || last.role !== 'assistant' || last.pending || !last.content || last.id === 'welcome') return
    // Always remember the latest assistant reply so "read that again" works,
    // even for replies restored from history.
    lastAssistantRef.current = last.content
    // Only auto-speak NEW (non-history) replies when voice is enabled.
    if (voiceEnabled && !last.fromHistory && last.id !== lastSpokenRef.current) {
      lastSpokenRef.current = last.id
      speak(last.content)
    }
  }, [messages, streaming, voiceEnabled, speak])

  // ── Auto-resolve MT5 context ───────────────────────────────────────────────
  // When no context has been published by /mt5-live (e.g. user navigated to
  // another page before asking JARVIS), fall back to the first available MT5
  // account with a default XAUUSD symbol so orders still work from any page.
  const resolveCtx = useCallback(async (requestText?: string) => {
    if (mt5ContextRef.current) return mt5ContextRef.current
    try {
      const res = await apiClient.mt5.getAccounts()
      const accounts: any[] = Array.isArray(res.data) ? res.data : []
      if (!accounts.length) return null
      const a = accounts[0]
      // Try to extract a symbol from the user's message
      let symbol = 'XAUUSD'
      if (requestText) {
        const symMatch = requestText.toUpperCase().match(/\b(XAUUSD|XAGUSD|EURUSD|GBPUSD|USDJPY|NAS100|US30|BTC|ETH|SOL|XRP)\b/)
        if (symMatch) symbol = symMatch[0]
      }
      const ctx = {
        accountId: a.id,
        symbol,
        timeframe: 'H1',
        balance: typeof a.balance === 'number' ? a.balance : undefined,
        currency: typeof a.currency === 'string' ? a.currency : undefined,
      }
      mt5ContextRef.current = ctx
      try { sessionStorage.setItem('__jarvis_mt5_ctx', JSON.stringify(ctx)) } catch { /* noop */ }
      return ctx
    } catch {
      return null
    }
  }, [])

  // ── Sniper analysis: run the SMC engine for the active MT5 account+symbol ──
  // Works from ANY page — resolves MT5 account automatically via sessionStorage
  // (persisted from /mt5-live) or by fetching the first configured account.
  //
  // requestText: the raw user message — used to extract a requested timeframe
  // (e.g. "analyse for 4h" → H4, "1h sniper entries" → H1). Falls back to the
  // chart's current timeframe from mt5Context, then to H1 as default.
  const runSniperAnalysis = useCallback(async (requestText?: string) => {
    const ctx = await resolveCtx(requestText)
    if (!ctx) {
      const msg = 'I need an MT5 account configured, Sir. Please add one on the MT5 Live page first.'
      setMessages(prev => [...prev, { id: nanoid(), role: 'assistant', content: msg }])
      speak(msg)
      return
    }
    const symbol = ctx.symbol
    // Determine timeframe: message text > chart context > default H1
    const timeframe = (requestText ? extractTimeframe(requestText) : null)
      ?? ctx.timeframe
      ?? 'H1'
    const tfLabel = timeframe.toLowerCase().replace('h', 'H').replace('m', 'M').replace('d', 'D')
    speak(`Analysing ${symbol} on the ${tfLabel} timeframe for sniper setups, Sir. One moment.`)
    const pendingId = nanoid()
    setMessages(prev => [...prev, {
      id: pendingId, role: 'assistant',
      content: `Analysing ${symbol} (${timeframe}) for sniper setups…`, pending: true,
    }])

    // Helper: finalise the pending message and read it aloud exactly once.
    const finalise = (content: string, sniperSetups?: SniperSetupAction[]) => {
      lastSpokenRef.current = pendingId  // suppress the auto-speak effect (avoid double TTS)
      setMessages(prev => prev.map(m =>
        m.id === pendingId ? { ...m, content, pending: false, sniperSetups } : m))
      speak(content)
    }

    try {
      const res = await apiClient.mt5.smcAnalyze(ctx.accountId, symbol, { timeframe, count: 400 })
      const a: any = res.data || {}
      if (a.error) {
        finalise(`I couldn't analyse ${symbol} on the ${timeframe} timeframe, Sir: ${String(a.error)}.`)
        return
      }
      const sigs: any[] = Array.isArray(a.signals) ? a.signals : []
      const setups: SniperSetupAction[] = sigs.slice(0, 3).map((s: any) => ({
        side: s.side === 'sell' ? 'sell' : 'buy',
        entry: Number(s.entry),
        stop_loss: Number(s.stop_loss),
        take_profit: Number(s.take_profit),
        rr: typeof s.rr === 'number' ? s.rr : undefined,
        confidence: typeof s.confidence === 'number' ? s.confidence : undefined,
        zone_kind: typeof s.zone_kind === 'string' ? s.zone_kind : undefined,
        volume: (typeof s.lot === 'number' && s.lot > 0) ? s.lot : 0.01,
        pointSize: typeof s.point_size === 'number' ? s.point_size : undefined,
      }))
      const bias = a.bias || 'neutral'
      const mom = a.momentum ? `, momentum ${a.momentum}` : ''
      const rsi  = typeof a.rsi === 'number' ? ` · RSI ${a.rsi.toFixed(1)}` : ''

      // ── AI review block ────────────────────────────────────────────────────
      // Build a lookup map: entry price → AI rated-signal so each setup line
      // can display "AI: TAKE / WATCH / SKIP — <note>" inline.
      const aiBlock: any = (a.ai && a.ai.available) ? a.ai : null
      const aiByEntry = new Map<number, {verdict: string; note: string; confidence: number}>()
      if (aiBlock?.rated_signals) {
        for (const r of aiBlock.rated_signals) {
          aiByEntry.set(Number(r.entry), {
            verdict: String(r.verdict ?? 'watch').toUpperCase(),
            note:    String(r.note ?? '').trim(),
            confidence: Number(r.confidence ?? 0),
          })
        }
      }
      // AI provider label (e.g. "Mistral", "Groq", "Gemini")
      const aiProviderLabel = aiBlock
        ? `${String(aiBlock.model || aiBlock.provider || 'AI').replace(/-\w+$/, '')}`.replace(/^\w/, c => c.toUpperCase())
        : null

      if (setups.length === 0) {
        // No setups — still show AI market read if available
        const noSetupMsg = `I analysed ${symbol} (${timeframe}): bias is ${bias}${mom}${rsi}, but there are no qualifying sniper setups right now, Sir.`
        if (aiBlock?.market_read) {
          finalise(
            `${noSetupMsg}\n\n**AI Review (${aiProviderLabel})**\n${aiBlock.market_read}` +
            (aiBlock.risk_warning ? `\n\n⚠️ ${aiBlock.risk_warning}` : ''),
          )
        } else {
          finalise(noSetupMsg)
        }
        return
      }

      // ── Per-setup lines (full words, AI verdict inline) ───────────────────
      const lines = setups.map((s, i) => {
        const px = (n: number) => formatSniperPrice(n, s.pointSize)
        const rrStr  = s.rr       != null ? `Risk:Reward ${s.rr}:1` : ''
        const confStr = s.confidence != null ? `${(s.confidence * 100).toFixed(0)}% confidence` : ''
        const zone   = s.zone_kind ? ` (${s.zone_kind})` : ''

        // Find the AI verdict for this exact entry
        const aiRating = aiByEntry.get(s.entry)
        let aiLine = ''
        if (aiRating) {
          const verdictEmoji = aiRating.verdict === 'TAKE' ? '✅' : aiRating.verdict === 'SKIP' ? '❌' : '👀'
          aiLine = `\n   AI: ${verdictEmoji} ${aiRating.verdict}` +
                   (aiRating.note ? ` — ${aiRating.note}` : '')
        }

        const meta = [rrStr, confStr].filter(Boolean).join(' · ')
        return (
          `${i + 1}. **${s.side.toUpperCase()}** limit at ${px(s.entry)}${zone}\n` +
          `   • Stop Loss: ${px(s.stop_loss)}\n` +
          `   • Take Profit: ${px(s.take_profit)}\n` +
          (meta ? `   • ${meta}\n` : '') +
          aiLine
        ).trimEnd()
      })

      // ── AI review section (full market read + risk warning) ───────────────
      let aiSection = ''
      if (aiBlock) {
        const providerLine = aiProviderLabel ? `**AI Review (${aiProviderLabel})**` : '**AI Review**'
        const biasComment  = aiBlock.bias_comment ? `${aiBlock.bias_comment}\n\n` : ''
        const marketRead   = aiBlock.market_read  ? `${aiBlock.market_read}\n`   : ''
        const topPick = aiBlock.top_pick_entry != null
          ? `\nTop pick entry: ${formatSniperPrice(Number(aiBlock.top_pick_entry), setups[0]?.pointSize)}\n`
          : ''
        const warning = aiBlock.risk_warning
          ? `\n⚠️ **Risk warning:** ${aiBlock.risk_warning}`
          : ''
        aiSection = `\n\n${providerLine}\n${biasComment}${marketRead}${topPick}${warning}`
      }

      // ── Header & final message ────────────────────────────────────────────
      const head = `**${symbol} Sniper Analysis** — ${timeframe} · bias ${bias}${mom}${rsi}`
      const fullMsg = `${head}\n\n${lines.join('\n\n')}${aiSection}`

      // Spoken summary: short version so TTS doesn't read paragraphs
      const spokenHead = `${symbol} ${timeframe} sniper — bias ${bias}. ${setups.length} setup${setups.length > 1 ? 's' : ''}.`
      const spokenSetups = setups.slice(0, 2).map((s, i) => {
        const px = (n: number) => formatSniperPrice(n, s.pointSize)
        const aiR = aiByEntry.get(s.entry)
        const verdict = aiR ? ` AI says ${aiR.verdict}.` : ''
        return `Setup ${i + 1}: ${s.side} at ${px(s.entry)}, Stop Loss ${px(s.stop_loss)}, Take Profit ${px(s.take_profit)}.${verdict}`
      }).join(' ')
      const spokenMsg = `${spokenHead} ${spokenSetups}` +
        (aiBlock?.risk_warning ? ` Warning: ${aiBlock.risk_warning}` : '')

      // Update message with rich text; speak concise summary (stripped of markdown)
      lastSpokenRef.current = pendingId
      setMessages(prev => prev.map(m =>
        m.id === pendingId ? { ...m, content: fullMsg, pending: false, sniperSetups: setups } : m))
      speak(cleanForSpeech(spokenMsg))

    } catch (e: any) {
      // Surface the real cause instead of always blaming MT5 — the backend
      // falls back to an exchange feed (XAU/USDT) when MT5 history is empty,
      // so a thrown error is almost always something else (timeout, backend
      // down, account not found, or an upstream 500).
      const status = e?.response?.status
      const detail = e?.response?.data?.detail || e?.response?.data?.error || e?.message || ''
      const isTimeout = e?.code === 'ECONNABORTED' || /timeout/i.test(String(detail))
      const isNetwork = e?.code === 'ERR_NETWORK' || (!status && /network/i.test(String(e?.message || '')))
      let msg: string
      if (isTimeout) {
        msg = `The ${symbol} ${timeframe} analysis timed out, Sir. The market feed was slow to respond — please try again in a moment.`
      } else if (isNetwork) {
        msg = `I couldn't reach the trading backend to analyse ${symbol}, Sir. The service may be starting up — please try again shortly.`
      } else if (status === 404) {
        msg = `I couldn't find that MT5 account while analysing ${symbol}, Sir. Please re-select an account on the MT5 Live page.`
      } else {
        msg = `I ran into an error analysing ${symbol} (${timeframe}), Sir${detail ? `: ${detail}` : '.'}`
      }
      finalise(msg)
    }
  }, [resolveCtx, speak])

  // Keep the ref in sync so the earlier-defined send()/voice pipeline can call it.
  useEffect(() => {
    runSniperAnalysisRef.current = (requestText?: string) => { void runSniperAnalysis(requestText) }
  }, [runSniperAnalysis])

  // ── Execute a sniper setup from the chat ──────────────────────────────────
  // Places a pending limit + SL/TP via the same smcPlace path as the chart's
  // "Place Limit + TP" button. Works from ANY page — resolves MT5 account via
  // persisted sessionStorage context or auto-fetches the first configured account.
  const placeSniperSetup = useCallback(async (messageId: string, idx: number, s: SniperSetupAction) => {
    const key = `${messageId}:${idx}`
    setSetupStatus(prev => ({ ...prev, [key]: { status: 'placing' } }))
    const ctx = await resolveCtx()
    if (!ctx) {
      setSetupStatus(prev => ({ ...prev, [key]: { status: 'error', msg: 'No MT5 account found. Please add one on the MT5 Live page, Sir.' } }))
      return
    }
    try {
      const res = await apiClient.mt5.smcPlace({
        account_id: ctx.accountId,
        symbol: ctx.symbol,
        side: s.side,
        entry: s.entry,
        stop_loss: s.stop_loss,
        take_profit: s.take_profit,
        volume: (s.volume && s.volume > 0) ? s.volume : 0.01,
        comment: `SMC ${s.zone_kind ?? 'sniper'}`,
      })
      const ticket = res.data?.ticket
      const px = formatSniperPrice(s.entry, s.pointSize)
      const ticketStr = ticket ? ` · Ticket #${ticket}` : ''
      setSetupStatus(prev => ({ ...prev, [key]: {
        status: 'placed',
        msg: `✅ ${s.side.toUpperCase()} limit @ ${px} placed on ${ctx.symbol}${ticketStr}. Check MT5 Live pending orders.`,
      } }))

      // ── Force immediate orders refresh on /mt5-live ─────────────────────
      // 1. Trigger the /mt5-live page's listener if the user is on that page.
      try { window.postMessage({ __jarvisPage: true, type: 'mt5-refresh' }, window.location.origin) } catch { /* noop */ }

      // 2. Pro-actively sync + fetch orders and broadcast result so the page
      //    gets updated even if the postMessage race-conditions.
      try {
        await apiClient.mt5.syncAccount(ctx.accountId)
        // Post a second refresh after the sync completes so the page has fresh data.
        try { window.postMessage({ __jarvisPage: true, type: 'mt5-refresh' }, window.location.origin) } catch { /* noop */ }
      } catch { /* best-effort */ }

    } catch (e: any) {
      const detail = e?.response?.data?.detail || e?.message || 'Order failed'
      setSetupStatus(prev => ({ ...prev, [key]: { status: 'error', msg: String(detail) } }))
    }
  }, [resolveCtx])

  // ── Execute ALL setups from the most recent sniper analysis ───────────────
  // Triggered by "execute the limits", "place the orders", etc.
  // Finds the latest message that has sniperSetups and places all of them.
  const executeAllSetups = useCallback(async (_requestText?: string) => {
    // Find the most recent assistant message that contains sniper setups.
    const lastWithSetups = [...messages].reverse().find(m => m.role === 'assistant' && m.sniperSetups && m.sniperSetups.length > 0)
    if (!lastWithSetups || !lastWithSetups.sniperSetups?.length) {
      const noMsg = 'I don\'t see any sniper setups to execute, Sir. Please run an SMC analysis first.'
      setMessages(prev => [...prev, { id: nanoid(), role: 'assistant', content: noMsg }])
      speak(noMsg)
      return
    }
    const setups = lastWithSetups.sniperSetups
    const confirmId = nanoid()
    const placing = `Executing ${setups.length} limit order${setups.length > 1 ? 's' : ''} on ${(await resolveCtx())?.symbol ?? 'MT5'}, Sir. Standby.`
    setMessages(prev => [...prev, { id: confirmId, role: 'assistant', content: placing, pending: true }])
    if (voiceEnabledRef.current) speak(placing)

    const results: string[] = []
    for (let i = 0; i < setups.length; i++) {
      await placeSniperSetup(lastWithSetups.id, i, setups[i])
      const st = Object.values({}) // just a pause between calls
      results.push(`Setup ${i + 1}: ${setups[i].side.toUpperCase()} @ ${formatSniperPrice(setups[i].entry, setups[i].pointSize)}`)
    }

    const doneMsg = `✅ Placed ${setups.length} limit order${setups.length > 1 ? 's' : ''}:\n${results.join('\n')}\nCheck MT5 Live for pending orders, Sir.`
    setMessages(prev => prev.map(m =>
      m.id === confirmId ? { ...m, content: doneMsg, pending: false } : m
    ))
    if (voiceEnabledRef.current) speak(`Done. Placed ${setups.length} limit order${setups.length > 1 ? 's' : ''}. Check your pending orders, Sir.`)
  }, [messages, resolveCtx, placeSniperSetup, speak])

  // Keep ref in sync for use in send() which is defined earlier.
  useEffect(() => {
    executeAllSetupsRef.current = executeAllSetups
  }, [executeAllSetups])

  // ── Hands-free command execution (navigate / click / scroll / type …) ─────
  // Returns true when the transcript was a recognised command (so it is NOT
  // also sent to chat). Lets a user with no hands fully drive the app by voice.
  // Run a trade command through the backend voice-trade executor and surface
  // the spoken result. Used by the trading-verb voice actions.
  const runVoiceTrade = useCallback((command: string, busy: string, fallback: string) => {
    if (voiceEnabledRef.current) speak(busy)
    apiClient.jarvis.voiceTrade(command).then(res => {
      const reply = res.data?.response || fallback
      speak(reply)
      setMessages(prev => [...prev, { id: nanoid(), role: 'assistant', content: reply }])
    }).catch(() => speak('I encountered an error with that trade request, Sir.'))
  }, [speak])

  const executeVoiceAction = useCallback((transcript: string, preParsed?: VoiceAction): boolean => {
    const say = (msg: string) => { if (msg && voiceEnabledRef.current) speak(msg) }

    // ── Confirmation gate for destructive trading verbs (close all) ─────────
    if (!preParsed && pendingCloseAllRef.current) {
      const tl = transcript.toLowerCase().trim()
      if (/\b(yes|yeah|yep|confirm|confirmed|do it|go ahead|proceed|affirmative|correct)\b/.test(tl)) {
        const cmd = pendingCloseAllCmdRef.current || 'close all positions'
        pendingCloseAllRef.current = false
        pendingCloseAllCmdRef.current = ''
        setInput('')
        runVoiceTrade(cmd, 'Closing all positions now, Sir.', 'All positions closed, Sir.')
        return true
      }
      if (/\b(no|nope|cancel|stop|never ?mind|abort|negative)\b/.test(tl)) {
        pendingCloseAllRef.current = false
        pendingCloseAllCmdRef.current = ''
        setInput('')
        say('Cancelled, Sir. I left your positions untouched.')
        return true
      }
      // Any unrelated utterance silently clears the pending state and is
      // processed normally below.
      pendingCloseAllRef.current = false
      pendingCloseAllCmdRef.current = ''
    }

    // ── Sniper-analysis intent (voice) ──────────────────────────────────────
    // Catch "analyse sniper setups" / "sniper entries" before generic command
    // parsing so it triggers the SMC analysis instead of an unrelated action.
    if (isSniperIntent(transcript)) {
      setInput('')
      runSniperAnalysisRef.current(transcript)
      return true
    }

    // ── Execute-all-setups intent (voice) ────────────────────────────────────
    // "Execute the limits", "place those orders", "go ahead and execute", …
    if (isExecuteSetupIntent(transcript)) {
      setInput('')
      void executeAllSetupsRef.current(transcript)
      return true
    }

    const action = preParsed || interpretVoiceCommand(transcript)
    if (!action) return false
    setInput('')   // clear the dictated text from the box
    switch (action.type) {
      case 'navigate':
        if (action.path) { say(action.say); router.push(action.path).catch(() => {}) }
        return true
      case 'click': {
        const ok = clickByText(action.target || '')
        say(ok ? 'Done, Sir.' : `I couldn't find "${action.target}" on this page, Sir.`)
        return true
      }
      case 'type':
        typeIntoField(action.text || ''); say(action.say); return true
      case 'scroll':
        window.scrollBy({ top: action.direction === 'up' ? -window.innerHeight * 0.8 : window.innerHeight * 0.8, behavior: 'smooth' })
        say(action.say); return true
      case 'top': window.scrollTo({ top: 0, behavior: 'smooth' }); say(action.say); return true
      case 'bottom': window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' }); say(action.say); return true
      case 'back': say(action.say); router.back(); return true
      case 'forward': say(action.say); window.history.forward(); return true
      case 'reload': say(action.say); setTimeout(() => window.location.reload(), 500); return true
      case 'open_chat': setOpen(true); say(action.say); return true
      case 'close_chat': say(action.say); setOpen(false); return true
      case 'new_chat': say(action.say); void newChat(); return true
      case 'stop_listening': say(action.say); setWakeEnabled(false); return true
      case 'repeat': {
        // Re-read the most recent assistant response on demand.
        const lastReply = lastAssistantRef.current
        if (lastReply) {
          speak(lastReply)
        } else {
          say("I haven't said anything yet, Sir.")
        }
        return true
      }
      case 'help':
        say(action.say)
        setMessages(prev => [...prev, { id: nanoid(), role: 'assistant', content: action.say }])
        return true

      // ── Hands-free form / UI control ──────────────────────────────────────
      case 'set_field': {
        const ok = setFieldByName(action.field || '', action.value || '')
        say(ok ? action.say : `I couldn't find the ${action.field} field on this page, Sir.`)
        return true
      }
      case 'select_option': {
        const ok = setSelectByText(action.target || '', action.field)
        say(ok ? `Selected ${action.target}.` : `I couldn't find "${action.target}", Sir.`)
        return true
      }
      case 'toggle': {
        const ok = toggleByText(action.target || '')
        say(ok ? 'Done, Sir.' : `I couldn't find "${action.target}", Sir.`)
        return true
      }
      case 'switch_tab': {
        const ok = clickByText(action.target || '')
        say(ok ? `Opening the ${action.target} tab.` : `I couldn't find the ${action.target} tab, Sir.`)
        return true
      }
      case 'set_timeframe': {
        const v = action.value || ''
        const num = v.replace(/[^\d]/g, '')
        const ok = clickByText(v) || clickByText(num)
        say(ok ? action.say : `I couldn't find the ${v} timeframe on this chart, Sir.`)
        return true
      }
      case 'submit_form': {
        const ok = submitForm()
        say(ok ? action.say : "I couldn't find a form to submit here, Sir.")
        return true
      }
      case 'cancel':
        pressCancel(); say(action.say); return true

      // ── Trading verbs ─────────────────────────────────────────────────────
      case 'set_leverage': {
        const ok = setFieldByName('leverage', action.value || '')
        say(ok ? action.say : `I couldn't find the leverage control on this page, Sir.`)
        return true
      }
      case 'set_amount': {
        const ok = setFieldByName('amount', action.value || '')
          || setFieldByName('size', action.value || '')
          || setFieldByName('quantity', action.value || '')
        say(ok ? action.say : `I couldn't find the amount field on this page, Sir.`)
        return true
      }
      case 'close_position':
        runVoiceTrade(transcript, 'Closing the position, Sir.', 'Position closed, Sir.')
        return true
      case 'close_all':
        // Destructive — require an explicit spoken confirmation before acting.
        pendingCloseAllRef.current = true
        pendingCloseAllCmdRef.current = transcript
        say('This will close all open positions, Sir. Say "confirm" to proceed, or "cancel".')
        return true

      default:
        return false
    }
  }, [router, speak, newChat, runVoiceTrade])

  useEffect(() => { commandRef.current = executeVoiceAction }, [executeVoiceAction])

  // ── AI intent fallback ────────────────────────────────────────────────────
  // When the instant local interpreter misses, ask the backend NLU to map the
  // natural-language utterance to a structured action and run it through the
  // SAME executor. Bounded by a short timeout so it never blocks chat; any
  // failure/timeout degrades gracefully to chat (returns false).
  const resolveIntentRemote = useCallback(async (text: string): Promise<boolean> => {
    const t = (text || '').trim()
    if (!t) return false
    try {
      const res = await Promise.race([
        apiClient.jarvis.parseIntent(t, router.pathname),
        new Promise<never>((_, rej) => setTimeout(() => rej(new Error('intent-timeout')), 1500)),
      ]) as { data?: VoiceAction & { type?: string } }
      const action = res?.data
      if (action && action.type && (action.type as string) !== 'none') {
        return executeVoiceAction(t, action as VoiceAction)
      }
    } catch { /* fall through to chat */ }
    return false
  }, [router, executeVoiceAction])

  useEffect(() => { resolveIntentRef.current = resolveIntentRemote }, [resolveIntentRemote])

  // ── Voice ownership ───────────────────────────────────────────────────────
  // When the extension has working speech enabled it OWNS the microphone — one reliable
  // recognizer that survives page navigations / React re-renders. The in-page
  // recognizers (startWake / startDictation) are then SUPPRESSED so there is
  // exactly ONE recognizer; two would fight for the mic and crash voice. The
  // extension posts recognised commands here, and we run them through the SAME
  // pipeline as in-page voice (navigate / click / type / scroll / open chat /
  // trade / chat) so JARVIS keeps every capability while driven by the
  // extension. With no extension present, the in-page mic is the fallback.

  // Stop any in-page recognizer (used when the extension takes over the mic).
  const stopVoiceRecognizers = useCallback(() => {
    try { wakeRef.current?.stop() } catch { /* noop */ }
    try { dictationRef.current?.stop() } catch { /* noop */ }
    setListening(false)
  }, [])

  // Let the extension own the mic — suppress the in-page recognizers so the two
  // never fight for the single microphone device.
  const claimMicForExt = useCallback(() => {
    extVoiceReadyRef.current = true
    setExtVoiceReady(true)
    stopVoiceRecognizers()
  }, [stopVoiceRecognizers])

  // Hand the mic back to the in-page recognizer (fallback) when the extension is
  // connected but never actually starts listening (e.g. stuck on "Starting…").
  const releaseMicToPage = useCallback(() => {
    extVoiceReadyRef.current = false
    setExtVoiceReady(false)
    // Resume the in-page wake listener so JARVIS keeps hearing the user.
    if (wakeEnabledRef.current && !listeningRef.current) {
      try { startWakeRef.current() } catch { /* noop */ }
    }
  }, [])

  // Reconcile mic ownership with the extension's state.
  //
  // The extension owns the ONE microphone, so we optimistically suppress the
  // in-page recognizers as soon as it connects (this prevents the two from
  // fighting for the device). BUT previously ownership was tied to the
  // extension being merely "ready" (enabled), reported true on boot before its
  // recognizer ever started. If the extension then got stuck before onstart
  // (popup shows "Starting…"), the in-page fallback stayed suppressed forever
  // and JARVIS went completely deaf.
  //
  // Fix: a watchdog. When the extension confirms it is ACTUALLY listening we
  // keep the in-page recognizer suppressed and cancel the watchdog. While it is
  // only connected-but-not-yet-listening we arm a one-shot watchdog; if no
  // "actually listening" confirmation arrives in time, we hand the mic back to
  // the in-page fallback so JARVIS still works. The extension's normal restart
  // gaps are far shorter than the watchdog, so healthy operation never flaps.
  const syncExtVoiceReady = useCallback((info: { listening: boolean; voiceReady?: boolean; proof?: boolean }) => {
    const { listening: isListening, voiceReady, proof } = info
    // 1) Extension explicitly reported its speech engine FAILED to start → the
    //    page owns the mic. Sticky until the extension proves it is listening.
    if (voiceReady === false) {
      extMicReleasedRef.current = true
      if (extReleaseTimerRef.current) { clearTimeout(extReleaseTimerRef.current); extReleaseTimerRef.current = null }
      releaseMicToPage()
      return
    }
    // 2) Positive proof the extension owns & uses the mic — a confirmed
    //    listening:true, or a wake/command/interrupt event just arrived.
    if (isListening || proof) {
      extMicReleasedRef.current = false
      if (extReleaseTimerRef.current) { clearTimeout(extReleaseTimerRef.current); extReleaseTimerRef.current = null }
      claimMicForExt()
      return
    }
    // 3) Connected/ready but not yet confirmed listening. If we already released
    //    the mic to the page (a prior stall/failure), do NOT re-claim on bare
    //    status(false) — that flapping is what left JARVIS deaf. Wait for proof.
    if (extMicReleasedRef.current) return
    // 4) First optimistic claim on connect: suppress the in-page recognizer and
    //    arm a one-shot watchdog. If no "actually listening" confirmation lands,
    //    hand the mic back to the page and latch released so we don't re-grab.
    claimMicForExt()
    if (!extReleaseTimerRef.current) {
      extReleaseTimerRef.current = setTimeout(() => {
        extReleaseTimerRef.current = null
        extMicReleasedRef.current = true
        releaseMicToPage()
      }, 6000)
    }
  }, [claimMicForExt, releaseMicToPage])

  // Run a command from the extension through the full JARVIS pipeline:
  //   1) hands-free UI action (navigate / click / type / scroll / open chat …)
  //   2) voice-trade (execute / place order / buy now …)
  //   3) otherwise → send it to chat
  const processExtCommand = useCallback((text: string) => {
    // Speaker gate: when voice-ID is on, only act on transcripts captured while
    // the page's parallel analyser confirms it's the user's voice (not JARVIS's
    // own TTS or background voices). This gives the extension the same realtime
    // "is this the user talking?" protection the in-page recognizer has.
    //
    // IMPORTANT: only enforce the gate while that analyser is ACTUALLY running
    // (voiceAudioCtxRef set). When the extension owns the mic the in-page
    // analyser often isn't running, so `voiceMatchRef` would be stale and this
    // gate used to silently swallow valid commands ("I talk and nothing
    // happens"). Without a live analyser we trust the extension's own capture.
    if (voiceMatchEnabledRef.current && voiceAudioCtxRef.current && !voiceMatchRef.current) return
    // Learn the user's words + auto-correct toward their vocabulary.
    const t = learnAndCorrect(text)
    if (!t) return
    if (!openRef.current) setOpen(true)
    const handled = commandRef.current(t)
    if (handled) return
    const tradePattern = /\b(execute|place order|open trade|buy now|sell now|based on (signals|sniper)|trade this)\b/i
    if (tradePattern.test(t)) {
      speak('Analysing signals and executing, Sir. One moment.')
      apiClient.jarvis.voiceTrade(t).then(res => {
        const reply = res.data?.response || 'Trade request processed, Sir.'
        speak(reply)
        setMessages(prev => [...prev,
          { id: nanoid(), role: 'user', content: t },
          { id: nanoid(), role: 'assistant', content: reply },
        ])
      }).catch(() => speak('I encountered an error placing the trade, Sir.'))
      return
    }
    // AI intent fallback: try to resolve natural phrasing to an action before chat.
    resolveIntentRef.current(t).then(done => { if (!done) sendRef.current(t) })
  }, [speak, learnAndCorrect])

  // Send a notification to the extension → shows as a desktop notification.
  const notifyExtension = useCallback((title: string, body: string) => {
    if (typeof window === 'undefined' || !extConnectedRef.current) return
    window.postMessage({ __jarvisPage: true, type: 'notify', title, body }, window.location.origin)
  }, [])

  const setExtensionVoiceEnabled = useCallback((enabled: boolean) => {
    if (typeof window === 'undefined' || !extConnectedRef.current) return
    window.postMessage({ __jarvisPage: true, type: 'voice-control', enabled }, window.location.origin)
  }, [])

  // ── MT5 page context bridge ───────────────────────────────────────────────
  // /mt5-live publishes the selected account + chart symbol so JARVIS can run
  // sniper analysis / place orders for the right account.
  //
  // Intentionally works on ALL pages (no pathname guard) and persists the last
  // received context to sessionStorage so the user can navigate away from
  // /mt5-live, ask JARVIS to "execute the limits", and it still knows which
  // account and symbol to use.
  useEffect(() => {
    if (typeof window === 'undefined') return
    const onMsg = (event: MessageEvent) => {
      const d = event.data
      if (!d || d.__jarvisPage !== true || d.type !== 'mt5-context') return
      if (typeof d.accountId !== 'number' || typeof d.symbol !== 'string') return
      const ctx = {
        accountId: d.accountId,
        symbol: d.symbol,
        timeframe: typeof d.timeframe === 'string' ? d.timeframe : undefined,
        balance: typeof d.balance === 'number' ? d.balance : undefined,
        currency: typeof d.currency === 'string' ? d.currency : undefined,
      }
      mt5ContextRef.current = ctx
      // Persist so orders can be placed from any page after navigating away.
      try { sessionStorage.setItem('__jarvis_mt5_ctx', JSON.stringify(ctx)) } catch { /* noop */ }
    }
    window.addEventListener('message', onMsg)
    return () => window.removeEventListener('message', onMsg)
  }, []) // no pathname dependency — active on every page

  // ── External speak bridge (universal voice) ───────────────────────────────
  // Any component OR the browser extension can trigger JARVIS speech by posting:
  //   window.postMessage({ __jarvisPage: true, type: 'jarvis-speak', text: '...' }, origin)
  // The text is spoken through the SAME pipeline as normal responses, so the
  // user's chosen voice (OpenAI aiVoice or the selected system voice) is used
  // for EVERY JARVIS utterance — chat, sniper analysis, and the extension alike.
  // This is an EXPLICIT request, so it speaks even if passive voice replies are off.
  useEffect(() => {
    if (typeof window === 'undefined') return
    const onSpeakRequest = (event: MessageEvent) => {
      const d = event.data
      if (!d || d.__jarvisPage !== true || d.type !== 'jarvis-speak') return
      if (typeof d.text !== 'string' || !d.text.trim()) return
      speak(d.text.trim())
    }
    window.addEventListener('message', onSpeakRequest)
    return () => window.removeEventListener('message', onSpeakRequest)
  }, [speak])

  // ── Robot-lock listener ────────────────────────────────────────────────────
  // When the extension robot activates it emits 'jarvis-robot-lock' to claim the
  // sole mic/speaker. We stop in-page recognition and mute TTS while locked.
  useEffect(() => {
    if (typeof window === 'undefined') return
    const onRobotLock = (e: Event) => {
      const locked = (e as CustomEvent).detail?.locked === true
      robotLockedRef.current = locked
      setRobotLocked(locked)
      if (locked) {
        // Stop all in-page speech recognition
        try { dictationRef.current?.stop() } catch { /* noop */ }
        try { wakeRef.current?.stop() } catch { /* noop */ }
        // Stop any active TTS
        if (typeof window !== 'undefined' && window.speechSynthesis) {
          window.speechSynthesis.cancel()
        }
        isSpeakingRef.current = false
      }
    }
    // Also listen for extension robot-mode message
    const onExtMsg = (e: MessageEvent) => {
      if (!e.data || e.data.__jarvisExt !== true) return
      if (e.data.type === 'robot-mode') {
        const locked = !!e.data.active
        robotLockedRef.current = locked
        setRobotLocked(locked)
        if (locked) {
          try { dictationRef.current?.stop() } catch { /* noop */ }
          try { wakeRef.current?.stop() } catch { /* noop */ }
          if (typeof window !== 'undefined' && window.speechSynthesis) {
            window.speechSynthesis.cancel()
          }
          isSpeakingRef.current = false
        }
      }
      // Relay wake-word events to robot avatar
      if (e.data.type === 'wake') {
        window.dispatchEvent(new CustomEvent('jarvis-wake', { detail: { type: 'wake', wakeWord: 'jarvis' } }))
      }
    }
    window.addEventListener('jarvis-robot-lock', onRobotLock)
    window.addEventListener('message', onExtMsg)
    return () => {
      window.removeEventListener('jarvis-robot-lock', onRobotLock)
      window.removeEventListener('message', onExtMsg)
    }
  }, [])

  useEffect(() => {
    if (typeof window === 'undefined') return

    const markExtConnected = (payload?: any) => {
      extConnectedRef.current = true
      setExtConnected(true)
      // Reconcile mic ownership from the extension's reported state:
      //   • voiceReady === false → engine failed, the page takes the mic.
      //   • listening === true or proof → the extension owns the mic.
      //   • otherwise (connected/ready, unconfirmed) → optimistic claim + watchdog.
      syncExtVoiceReady({
        listening: payload?.listening === true,
        voiceReady: payload?.voiceReady,
        proof: payload?.proof === true,
      })
    }

    // 1. Synchronous DOM-attribute check (set by the content script on load)
    if (document.documentElement.getAttribute('data-jarvis-ext') === '1') {
      markExtConnected({
        voiceReady: document.documentElement.getAttribute('data-jarvis-ext-voice') === '1',
      })
    }

    const onMsg = (event: MessageEvent) => {
      if (event.source !== window) return
      const d = event.data
      if (!d || d.__jarvisExt !== true) return
      switch (d.type) {
        case 'connected':
          markExtConnected(d)
          break
        case 'avatar-style':
          // Extension popup changed the robot avatar — apply it to the 3D robot
          if (typeof d.style === 'string') {
            const valid = ['cyan', 'purple', 'gold', 'crimson', 'emerald']
            if (valid.includes(d.style)) setAvatarStyle(d.style as AvatarStyle)
          }
          break
        case 'interrupt':
          // User said the wake phrase while JARVIS was talking — stop instantly
          // so they can barge in (human-like interruptibility) via the extension.
          // Receiving this is proof the extension owns and is using the mic.
          markExtConnected({ ...d, listening: true, proof: true })
          // Speaker gate: only the user's stored voice may interrupt JARVIS, so
          // its own TTS (or the TV) can never cut it off mid-sentence.
          if (voiceMatchEnabledRef.current && !voiceMatchRef.current) break
          interruptSpeech()
          break
        case 'wake':
          // Extension heard the wake phrase — open the chat and acknowledge.
          // Receiving this is proof the extension owns and is using the mic.
          markExtConnected({ ...d, listening: true, proof: true })
          // Speaker gate: ignore wakes that aren't the user's stored voice.
          if (voiceMatchEnabledRef.current && !voiceMatchRef.current) break
          // Always cut off any in-progress speech first so JARVIS yields the
          // floor the moment it is addressed.
          interruptSpeech()
          if (!openRef.current) setOpen(true)
          if (voiceEnabledRef.current) speak('Yes Sir.')
          break
        case 'command':
          // Extension captured a full command — execute it via the pipeline.
          // Receiving this is proof the extension owns and is using the mic.
          markExtConnected({ ...d, listening: true, proof: true })
          if (typeof d.transcript === 'string') processExtCommand(d.transcript)
          break
        case 'voice-learning-restore':
          // Extension sends back previously saved vocabulary (from chrome.storage.local)
          // when the page loads — merging it ensures learning survives localStorage clearing.
          if (d.data && typeof d.data === 'object') {
            const restored = d.data as Record<string, number>
            const current  = learnedWordsRef.current
            let updated = false
            for (const [k, v] of Object.entries(restored)) {
              if (typeof v === 'number' && v > (current[k] || 0)) {
                current[k] = v
                updated = true
              }
            }
            if (updated) {
              learnedWordsRef.current = { ...current }
              persistLearnedWords(learnedWordsRef.current)
            }
          }
          break
        case 'status':
          // Mirror the extension's ACTUAL listening state onto mic ownership and
          // the indicator — ownership follows listening + voiceReady, handled by
          // markExtConnected (which reads d.listening and d.voiceReady).
          markExtConnected(d)
          setListening(!!d.listening)
          break
      }
    }
    window.addEventListener('message', onMsg)

    // 2. Ping the extension a few times in case its 'connected' fired before
    //    this listener mounted.
    const ping = () => {
      try { window.postMessage({ __jarvisPage: true, type: 'ping' }, window.location.origin) } catch { /* noop */ }
      if (document.documentElement.getAttribute('data-jarvis-ext') === '1') {
        markExtConnected({
          voiceReady: document.documentElement.getAttribute('data-jarvis-ext-voice') === '1',
        })
      }
    }
    ping()
    const p1 = setTimeout(ping, 500)
    const p2 = setTimeout(ping, 1500)

    return () => {
      window.removeEventListener('message', onMsg)
      clearTimeout(p1); clearTimeout(p2)
      if (extReleaseTimerRef.current) clearTimeout(extReleaseTimerRef.current)
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [processExtCommand, speak, stopVoiceRecognizers, syncExtVoiceReady, interruptSpeech])

  // ── JARVIS self-learning: extract critical info → brain map knowledge nodes ─
  // After each new (non-history) assistant reply, detect market-critical content
  // (symbols, signals, patterns, risk levels, strategies) and save it as a
  // knowledge node so the Brain Map grows and learns from every conversation.
  // The intelligence page already polls these and renders them as live nodes.
  useEffect(() => {
    if (streaming) return
    const last = messages[messages.length - 1]
    if (!last || last.role !== 'assistant' || last.pending || !last.content
      || last.id === 'welcome' || last.fromHistory) return

    const content = last.content.trim()
    if (content.length < 40) return  // too short to be informative

    // Detect if this reply contains actionable market intelligence worth storing.
    const marketSignals   = /\b(buy|sell|long|short|bullish|bearish|hold|entry|exit|signal|BUY|SELL)\b/i
    const riskInfo        = /\b(stop.?loss|take.?profit|sl|tp|support|resistance|level|zone|breakout|breakdown)\b/i
    const symbolMention   = /\b([A-Z]{2,8}\/USDT|[A-Z]{2,8}\/BTC|BTC|ETH|BNB|SOL|XRP|ADA|DOT|AVAX|MATIC|LINK)\b/
    const analysisKw      = /\b(RSI|MACD|EMA|SMA|Bollinger|ATR|volume|trend|momentum|divergence|confluence|liquidity|SMC|order.?block|fair.?value.?gap|FVG)\b/i
    const patternKw       = /\b(pattern|triangle|wedge|flag|channel|double.?top|double.?bottom|head.?and.?shoulders|cup|breakout|consolidation)\b/i
    const criticalSignal  = /\b(critical|important|alert|warning|urgent|significant|key.?level|major)\b/i

    const isMarketCritical = marketSignals.test(content) || riskInfo.test(content)
      || analysisKw.test(content) || patternKw.test(content)
    if (!isMarketCritical && !criticalSignal.test(content)) return

    // Extract the most prominent symbol mentioned (if any)
    const symbolMatch = content.match(symbolMention)
    const symbol = symbolMatch ? symbolMatch[0] : undefined

    // Build a concise title (first meaningful sentence, ≤ 80 chars)
    const firstSentence = content.replace(/^[\s#*_]+/, '').split(/\.\s+/)[0].slice(0, 80)
    const title = firstSentence.length > 10 ? firstSentence : undefined

    // Determine kind
    const kind = marketSignals.test(content) ? 'signal'
      : riskInfo.test(content) ? 'risk'
      : patternKw.test(content) ? 'pattern'
      : 'insight'

    // Weight by urgency/confidence indicators
    const weight = criticalSignal.test(content) ? 1.8 : 1.2

    // Fire-and-forget — never block the UI
    apiClient.aiAnalyst.addKnowledge({
      content: content.slice(0, 800),
      title,
      kind,
      symbol,
      agent_role: 'jarvis_chat',
      weight,
      source: 'jarvis_conversation',
    }).catch(() => { /* plugin unavailable — silently ignore */ })
  }, [messages, streaming])

  // Relay finished assistant replies + alerts to the extension as desktop notifications.
  // Skip messages restored from history so a refresh doesn't re-notify old replies.
  useEffect(() => {
    if (!extConnected) return
    const last = messages[messages.length - 1]
    if (last && last.role === 'assistant' && !last.pending && last.content
        && last.id !== 'welcome' && !last.fromHistory) {
      notifyExtension('JARVIS', cleanForSpeech(last.content).slice(0, 120))
    }
  }, [messages, extConnected, notifyExtension])

  // ── Mic dictation (single utterance → fills input → auto-sends) ────────────
  const stopDictation = useCallback(() => {
    try { dictationRef.current?.stop() } catch { /* noop */ }
    setListening(false)
    setRecording(false)
  }, [])

  // Adaptive noise gate: record measured confidences and derive an effective
  // threshold from the recent ambient baseline. Quiet rooms (consistently high
  // confidence) relax the gate so soft speech still passes; noisy rooms (low,
  // scattered confidence) tighten it to reject background chatter.
  const noteAndGetThreshold = useCallback((conf: number): number => {
    const base = noiseThresholdRef.current
    const w = ambientConfRef.current
    if (conf > 0) { w.push(conf); if (w.length > 24) w.shift() }
    if (w.length < 5) return base
    const sorted = [...w].sort((a, b) => a - b)
    const median = sorted[Math.floor(sorted.length / 2)]
    if (median >= 0.7) return Math.max(0.2, base - 0.2)   // quiet/clear → more sensitive
    if (median <= 0.4) return Math.min(0.9, base + 0.1)   // noisy → stricter
    return base
  }, [])

  const startDictation = useCallback(() => {
    if (extVoiceReadyRef.current) return  // extension owns the mic — no in-page recognizer

    // A: AI Speech (Whisper) — uses MediaRecorder for high-fidelity capture
    if (aiSpeechEnabled) {
      void (async () => {
        try {
          const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
          const recorder = new MediaRecorder(stream)
          whisperRecorderRef.current = recorder
          whisperStreamRef.current = stream
          const chunks: Blob[] = []
          setListening(true)
          setRecording(true)
          listeningRef.current = true

          // Stop wake listener while actively dictating
          try { wakeRef.current?.stop() } catch { /* noop */ }

          recorder.ondataavailable = (e) => { if (e.data.size > 0) chunks.push(e.data) }
          recorder.onstop = async () => {
            setRecording(false)
            setListening(false)
            listeningRef.current = false
            // Aborted because JARVIS started speaking — discard the capture so it
            // is never transcribed (and don't resume wake; speak() handles that).
            if (whisperAbortedRef.current) {
              whisperAbortedRef.current = false
              try { stream.getTracks().forEach(t => t.stop()) } catch { /* noop */ }
              whisperRecorderRef.current = null
              whisperStreamRef.current = null
              return
            }
            const blob = new Blob(chunks, { type: 'audio/webm' })
            const formData = new FormData()
            formData.append('file', blob, 'audio.webm')

            try {
              const resp = await fetch(`${process.env.NEXT_PUBLIC_API_URL || 'http://localhost:1448/api/v1'}/voice/stt`, {
                method: 'POST',
                body: formData
              })
              const data = await resp.json()
              if (data.text) {
                // Learn the user's words + auto-correct toward their vocabulary.
                const text = learnAndCorrect(data.text)
                setInput(text)
                const handled = commandRef.current(text)
                if (!handled) {
                  const tradePattern = /\b(execute|place order|open trade|buy now|sell now|based on (signals|sniper)|trade this)\b/i
                  if (tradePattern.test(text)) {
                    speak('Analysing signals and executing, Sir. One moment.')
                    apiClient.jarvis.voiceTrade(text).then(res => {
                      const reply = res.data?.response || 'Trade request processed, Sir.'
                      speak(reply)
                      setMessages(prev => [...prev,
                        { id: nanoid(), role: 'user', content: text },
                        { id: nanoid(), role: 'assistant', content: reply },
                      ])
                    }).catch(() => speak('I encountered an error placing the trade, Sir.'))
                  } else {
                    // AI intent fallback before chat.
                    resolveIntentRef.current(text).then(done => { if (!done) sendRef.current(text) })
                  }
                }
              }
            } catch (err) {
              console.error('Whisper STT failed', err)
              speak('I had trouble hearing you clearly via AI, Sir.')
            }
            stream.getTracks().forEach(t => t.stop())
            whisperRecorderRef.current = null
            whisperStreamRef.current = null
            // Don't auto-resume wake while JARVIS is talking — speak() resumes it
            // after it finishes so the assistant never records its own voice.
            if (wakeEnabledRef.current && !isSpeakingRef.current) setTimeout(() => startWakeRef.current(), 600)
          }

          recorder.start()
          // Automatically stop recording after a period of capture (command length).
          // Shorter cap = faster speech→action turnaround for typical commands.
          setTimeout(() => { if (recorder.state === 'recording') recorder.stop() }, 4500)
        } catch (err) {
          console.error('AI recording failed', err)
          setListening(false)
          setRecording(false)
          listeningRef.current = false
          if (wakeEnabledRef.current) startWakeRef.current()
        }
      })()
      return
    }

    // B: Standard Web Speech API (Fast, low-bandwidth)
    const SR = getSpeechRecognition()
    if (!SR) return
    // Stop wake listener while actively dictating (avoid double-capture).
    try { wakeRef.current?.stop() } catch { /* noop */ }
    const rec = new SR()
    rec.lang = 'en-US'
    rec.continuous = false
    rec.interimResults = true
    rec.maxAlternatives = 3   // give the vocab-aware picker alternatives to choose from
    let finalText = ''
    let lowConfSeen = false   // a measured-but-below-threshold final was dropped
    rec.onresult = (e: any) => {
      // Self-hearing hard gate: NEVER transcribe while JARVIS is talking (or in
      // the post-speech echo tail) unless the camera can SEE the user's mouth
      // moving — JARVIS's own TTS can never move the user's mouth.
      if ((isSpeakingRef.current || micGatedRef.current) && !cameraSeesUserTalking()) return
      // Camera gate: when the camera is live, only accept speech while the
      // user's mouth is (or was just) moving.
      if (!mouthGateOpen()) return
      let interim = ''
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const t = e.results[i][0].transcript
        const conf = typeof e.results[i][0].confidence === 'number' ? e.results[i][0].confidence : 1.0
        if (e.results[i].isFinal) {
          // Accept: conf=0 (Chrome "unmeasured") or at/above the adaptive gate.
          // Reject: explicitly measured AND below threshold (genuine noise).
          const threshold = noteAndGetThreshold(conf)
          // Pick the alternative richest in the user's known words (cleaner match).
          if (conf === 0 || conf >= threshold) finalText += pickAlternative(e.results[i]) || t
          else lowConfSeen = true  // dropped a genuine low-confidence guess → miss
        }
        else interim += t
      }
      setInput((finalText + interim).trimStart())
      // Faster end-of-speech: once we have some final text, auto-stop shortly
      // after speech activity ceases instead of waiting for the browser's long
      // built-in timeout — so the command dispatches sooner.
      if (silenceTimerRef.current) clearTimeout(silenceTimerRef.current)
      if (finalText.trim()) {
        silenceTimerRef.current = setTimeout(() => { try { rec.stop() } catch { /* noop */ } }, 900)
      }
    }
    rec.onerror = () => setListening(false)
    rec.onend = () => {
      if (silenceTimerRef.current) { clearTimeout(silenceTimerRef.current); silenceTimerRef.current = null }
      setListening(false)
      // Learn the user's words + auto-correct toward their vocabulary.
      const text = learnAndCorrect(finalText)
      if (text) {
        dgMissCountRef.current = 0  // heard a command cleanly → reset miss streak
        // 1. Try a hands-free navigation/UI command first
        const handled = commandRef.current(text)
        if (!handled) {
          // 2. Check for voice-trade command — execute trade via backend
          const tradePattern = /\b(execute|place order|open trade|buy now|sell now|based on (signals|sniper)|trade this)\b/i
          if (tradePattern.test(text)) {
            speak('Analysing signals and executing, Sir. One moment.')
            apiClient.jarvis.voiceTrade(text).then(res => {
              const reply = res.data?.response || 'Trade request processed, Sir.'
              speak(reply)
              setMessages(prev => [...prev,
                { id: nanoid(), role: 'user', content: text },
                { id: nanoid(), role: 'assistant', content: reply },
              ])
            }).catch(() => {
              speak('I encountered an error placing the trade, Sir.')
            })
          } else {
            // 3. AI intent fallback, then fall through to chat
            resolveIntentRef.current(text).then(done => { if (!done) sendRef.current(text) })
          }
        }
      } else {
        // Heard nothing usable — escalate the buffered clip to Deepgram. A
        // measured-but-rejected guess is a clear low-confidence miss (escalates
        // immediately); a totally empty result waits for the consecutive-miss gate.
        noteVoiceMissRef.current(lowConfSeen ? 'low_confidence' : 'empty')
      }
      if (wakeEnabledRef.current) setTimeout(() => startWakeRef.current(), 600)
    }
    dictationRef.current = rec
    setListening(true)
    try { rec.start() } catch { setListening(false) }
  }, [aiSpeechEnabled, speak, noteAndGetThreshold, learnAndCorrect, pickAlternative, cameraSeesUserTalking, mouthGateOpen])

  // ── Shared one-shot command dispatcher ────────────────────────────────────
  // Mirrors the dictation pipeline so a single-utterance wake ("Jarvis,
  // <command>") runs the command immediately: hands-free command → voice-trade
  // → AI intent → chat fallback.
  const dispatchVoiceCommand = useCallback((text: string) => {
    // Learn the user's words + auto-correct toward their vocabulary.
    const cmd = learnAndCorrect(text)
    if (!cmd) return
    dgMissCountRef.current = 0  // a dispatched command is a hit → reset miss streak
    setInput(cmd)
    const handled = commandRef.current(cmd)
    if (handled) return
    const tradePattern = /\b(execute|place order|open trade|buy now|sell now|based on (signals|sniper)|trade this)\b/i
    if (tradePattern.test(cmd)) {
      speak('Analysing signals and executing, Sir. One moment.')
      apiClient.jarvis.voiceTrade(cmd).then(res => {
        const reply = res.data?.response || 'Trade request processed, Sir.'
        speak(reply)
        setMessages(prev => [...prev,
          { id: nanoid(), role: 'user', content: cmd },
          { id: nanoid(), role: 'assistant', content: reply },
        ])
      }).catch(() => speak('I encountered an error placing the trade, Sir.'))
    } else {
      resolveIntentRef.current(cmd).then(done => { if (!done) sendRef.current(cmd) })
    }
  }, [speak, learnAndCorrect])
  dispatchVoiceCommandRef.current = dispatchVoiceCommand

  // ── Wake-word listener ("Hi Jarvis") — continuous background recognition ──
  // Also handles mid-speech interruption: any recognised speech while JARVIS
  // is talking cancels the current TTS and starts dictation immediately.
  const startWake = useCallback(() => {
    const SR = getSpeechRecognition()
    if (!SR || !wakeEnabledRef.current) return
    if (extVoiceReadyRef.current) return  // extension owns the mic — no in-page recognizer
    if (wakeErrorPausedRef.current) return  // paused after a mic error — re-armed on next gesture
    // Don't start recognition if we're still in the post-speech echo-tail blackout
    if (micGatedRef.current) return
    try { wakeRef.current?.stop() } catch { /* noop */ }
    const rec = new SR()
    rec.lang = 'en-US'
    rec.continuous = true
    rec.interimResults = true
    rec.onresult = (e: any) => {
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const t = e.results[i][0].transcript as string
        const conf = typeof e.results[i][0].confidence === 'number' ? e.results[i][0].confidence : 1.0
        const threshold = noteAndGetThreshold(conf)
        // Noise gate: only block results that have a MEASURED confidence below threshold.
        // conf===0 means Chrome didn't measure confidence (“unmeasured”) — let those through.
        if (conf > 0 && conf < threshold) continue

        // Voice profile gate: when speaker ID is enabled, reject non-matching voices.
        // This is the PRIMARY defence against TV/background voices.
        if (voiceMatchEnabled && !voiceMatchRef.current) continue

        // Camera gate: when the camera is live, only the user's moving mouth
        // opens hearing — JARVIS's own TTS can never pass this, so it cannot
        // wake (or interrupt) itself while it reads a reply.
        if (!mouthGateOpen()) continue
        // While JARVIS is speaking with the camera live, require the camera to
        // actually SEE the user talking before honouring any speech.
        if (isSpeakingRef.current && faceFresh() && !cameraSeesUserTalking()) continue

        // Interrupt gate: user says the wake name while JARVIS is speaking →
        // interrupt. Requires the wake phrase (not any speech) to prevent TV
        // voices interrupting.
        if (isSpeakingRef.current && hasWakeWord(t, wakeRequireGreetingRef.current)) {
          interruptSpeech()
          try { rec.stop() } catch { /* noop */ }
          if (!openRef.current) setOpen(true)
          const cmd = stripWakePhrase(t, wakeRequireGreetingRef.current)
          if (cmd.length >= 3) {
            // Name + command in one breath — run it straight away.
            dispatchVoiceCommandRef.current(cmd)
          } else if (voiceEnabledRef.current) {
            speak('Yes Sir.', () => setTimeout(() => startDictationRef.current(), 200))
          } else {
            setTimeout(() => startDictationRef.current(), 200)
          }
          return
        }

        // Wake gate: the deliberate activation phrase (bare name by default, or
        // greeting + name) triggers listening. When the same utterance already
        // carries a command, run it in one breath instead of re-prompting.
        if (hasWakeWord(t, wakeRequireGreetingRef.current)) {
          try { rec.stop() } catch { /* noop */ }
          if (!openRef.current) setOpen(true)
          const cmd = stripWakePhrase(t, wakeRequireGreetingRef.current)
          if (cmd.length >= 3) {
            // One-utterance activation: "Jarvis, <command>".
            dispatchVoiceCommandRef.current(cmd)
          } else if (voiceEnabledRef.current) {
            // Name only — confirm, then capture the follow-up command.
            // Delay dictation until the confirmation finishes so the mic
            // doesn't pick up JARVIS's own voice.
            speak('Yes Sir, I am listening.', () => setTimeout(() => startDictationRef.current(), 200))
          } else {
            setTimeout(() => startDictationRef.current(), 350)
          }
          return
        }
      }
    }
    rec.onerror = (e: any) => {
      // A denied/blocked mic must NOT permanently disable the wake word — doing so
      // (by persisting OFF) was leaving JARVIS deaf across reloads after a single
      // transient glitch. Instead, pause this session and re-arm on the next user
      // gesture; the user's wake-word preference stays ON.
      if (e?.error === 'not-allowed' || e?.error === 'service-not-allowed') {
        wakeErrorPausedRef.current = true
        try { rec.stop() } catch { /* noop */ }
        rearmWakeOnGestureRef.current()
      }
    }
    rec.onend = () => {
      // Wake recognizer is no longer running — the watchdog may re-arm it.
      wakeStartedRef.current = false
      // While JARVIS is speaking we normally keep the mic muted (speak()'s onend
      // resumes wake afterwards). BUT when speaker-ID barge-in is enabled we keep
      // the wake recognizer alive during speech so the user can interrupt — the
      // stored-voice gate above rejects JARVIS's own voice.
      const speakingBlocks = isSpeakingRef.current && !canBargeIn()
      if (wakeEnabledRef.current && !listeningRef.current && !speakingBlocks && !wakeErrorPausedRef.current) {
        setTimeout(() => {
          const stillBlocked = isSpeakingRef.current && !canBargeIn()
          if (wakeEnabledRef.current && !listeningRef.current && !stillBlocked && !wakeErrorPausedRef.current) startWake()
        }, 400)
      }
    }
    rec.onstart = () => { wakeStartedRef.current = true }
    wakeRef.current = rec
    wakeStartedRef.current = false
    wakeStartAtRef.current = Date.now()
    try { rec.start() } catch { /* noop */ }
  }, [speak, interruptSpeech, noteAndGetThreshold, canBargeIn, mouthGateOpen, faceFresh, cameraSeesUserTalking])

  startDictationRef.current = startDictation
  startWakeRef.current = startWake

  // ── Toggle wake mode on/off ───────────────────────────────────────────────
  useEffect(() => {
    if (wakeEnabled) {
      startWake()
    } else {
      try { wakeRef.current?.stop() } catch { /* noop */ }
    }
    return () => { try { wakeRef.current?.stop() } catch { /* noop */ } }
  }, [wakeEnabled, startWake])

  // ── In-page wake watchdog (heavy chart pages) ─────────────────────────────
  // On chart / WebGL pages the Web Speech recognizer can silently die (start()
  // throws and is swallowed, or onstart never fires) so JARVIS goes deaf until a
  // gesture. This 3s backstop re-arms the wake recognizer whenever it SHOULD be
  // running (page owns the mic, wake on, not in dictation / speaking / blackout /
  // robot-lock) but hasn't reached onstart within a few seconds. It never fires
  // while the extension owns the mic or during normal listening/speaking.
  useEffect(() => {
    if (!speechSupported) return
    const id = setInterval(() => {
      if (!wakeEnabledRef.current) return
      if (extVoiceReadyRef.current) return          // extension owns the mic
      if (listeningRef.current) return              // capturing a command
      if (robotLockedRef.current) return            // robot-mode exclusive lock
      if (wakeErrorPausedRef.current) return        // paused after mic denial (gesture re-arms)
      if (micGatedRef.current) return               // post-speech echo blackout
      if (isSpeakingRef.current && !canBargeIn()) return  // muted while JARVIS talks
      // Healthy recognizer → nothing to do. Only re-arm when a start never
      // succeeded within 3s (silent failure), avoiding restarts during silence.
      if (wakeStartedRef.current) return
      if (Date.now() - wakeStartAtRef.current < 3000) return
      try { startWakeRef.current() } catch { /* noop */ }
    }, 3000)
    return () => clearInterval(id)
  }, [speechSupported, canBargeIn])

  // ── Ensure wake listening starts after the first user gesture ─────────────
  // Browsers block mic access until the user interacts with the page, so the
  // auto-start above may be a no-op on a fresh load. This re-arms it on the
  // first click/keydown so JARVIS is always listening thereafter.
  useEffect(() => {
    if (!speechSupported) return
    const arm = () => {
      // A fresh user gesture clears any transient mic-error pause and lets the
      // browser re-prompt for the mic, so JARVIS self-heals instead of staying
      // deaf after a one-off denial.
      wakeErrorPausedRef.current = false
      if (wakeEnabledRef.current && !listeningRef.current) {
        try { startWake() } catch { /* noop */ }
      }
    }
    // Re-arm on the next gesture (used on first load AND after a mic error). The
    // listeners are one-shot; `rearmWakeOnGestureRef` lets the error handler add
    // a new pair so recovery always needs exactly one fresh user interaction.
    const rearm = () => {
      window.addEventListener('pointerdown', arm, { once: true })
      window.addEventListener('keydown', arm, { once: true })
    }
    rearmWakeOnGestureRef.current = rearm
    rearm()
    return () => {
      window.removeEventListener('pointerdown', arm)
      window.removeEventListener('keydown', arm)
    }
  }, [speechSupported, startWake])

  // ── Cleanup speech on unmount ─────────────────────────────────────────────
  useEffect(() => () => {
    try { dictationRef.current?.stop() } catch { /* noop */ }
    try { wakeRef.current?.stop() } catch { /* noop */ }
    if (typeof window !== 'undefined' && 'speechSynthesis' in window) window.speechSynthesis.cancel()
  }, [])

  const toggleMic = () => {
    if (listening) stopDictation()
    else startDictation()
  }

  const toggleVoice = () => {
    setVoiceEnabled(v => {
      if (v && typeof window !== 'undefined' && 'speechSynthesis' in window) window.speechSynthesis.cancel()
      return !v
    })
  }

  // Quick-pick the best male/female JARVIS voice from the system list.
  const pickGenderVoice = (g: 'male' | 'female') => {
    setVoiceGender(g)
    const v = pickJarvisVoice(voiceList, g)
    if (v) setVoiceURI(v.voiceURI)
  }

  // Speak a sample line so the user can audition the current voice settings.
  const testVoice = () => {
    speak("All systems online, Sir. I'm PAUL — your personal trading assistant. How may I help you today?")
  }

  // ── Live voice detection diagnostic ──────────────────────────────────────
  // Shows the last few transcripts and whether they were blocked/allowed,
  // so the user can see exactly why TV or ambient sounds are rejected.
  const [voiceDiagLog, setVoiceDiagLog] = useState<Array<{t: string; conf: number; action: 'wake'|'blocked_conf'|'blocked_profile'|'ignored'}>>([])
  const voiceDiagEnabled = useRef(false)

  // Diagnostic speech recognition — runs alongside the normal wake listener
  // ONLY when the settings panel is open and diagMode is enabled.
  const [diagMode, setDiagMode] = useState(false)
  const diagRecRef = useRef<SpeechRecognitionLike>(null)
  useEffect(() => {
    voiceDiagEnabled.current = diagMode
    if (!diagMode) {
      try { diagRecRef.current?.stop() } catch { /* noop */ }
      return
    }
    const SR = getSpeechRecognition()
    if (!SR) return
    try { diagRecRef.current?.stop() } catch { /* noop */ }
    const rec = new SR()
    rec.lang = 'en-US'; rec.continuous = true; rec.interimResults = false
    rec.onresult = (e: any) => {
      for (let i = e.resultIndex; i < e.results.length; i++) {
        if (!e.results[i].isFinal) continue
        const t = e.results[i][0].transcript as string
        const conf = typeof e.results[i][0].confidence === 'number' ? e.results[i][0].confidence : 0
        const threshold = noiseThresholdRef.current
        let action: 'wake'|'blocked_conf'|'blocked_profile'|'ignored' = 'ignored'
        if (conf > 0 && conf < threshold) action = 'blocked_conf'
        else if (voiceMatchEnabled && !voiceMatchRef.current) action = 'blocked_profile'
        else if (hasWakeWord(t)) action = 'wake'
        setVoiceDiagLog(prev => [{t: t.slice(0, 60), conf: Math.round(conf * 100), action}, ...prev.slice(0, 9)])
      }
    }
    rec.onerror = () => { /* ignore diag errors */ }
    rec.onend = () => { if (voiceDiagEnabled.current) setTimeout(() => { try { rec.start() } catch { /* noop */ } }, 400) }
    diagRecRef.current = rec
    try { rec.start() } catch { /* noop */ }
    return () => { try { rec.stop() } catch { /* noop */ }; voiceDiagEnabled.current = false }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [diagMode, voiceMatchEnabled])

  const dismissAlert = async (id: string) => {
    try {
      await apiClient.jarvis.markAlertRead(id)
      setAlerts(prev => prev.map(a => a.id === id ? { ...a, read: true } : a))
    } catch { /* ignore */ }
  }

  // Derive the robot animation state from the live voice pipeline.
  const robotState: RobotState =
    isSpeakingRef.current ? 'talking'
    : streaming ? 'thinking'
    : (listening || recording) ? 'listening'
    : 'idle'

  return (
    <>
      {/* ── 3D JARVIS robot avatar — floats on every page, click to open chat ──
           Suppressed where the host page renders its own JARVIS visual (e.g. the
           JARVIS Room's energy core), so the robot doesn't float over it. */}
      {!hideRobot && robotAllowed && (
        <JarvisRobotAvatar
          state={robotState}
          energy={robotEnergy}
          avatarStyle={avatarStyle}
          extRobotActive={robotLocked}
          onClick={() => !robotLocked && setOpen(o => !o)}
        />
      )}

      {/* ── Floating button — hidden when robot has exclusive mic/speaker ─── */}
      <button
        onClick={() => setOpen(o => !o)}
        style={{ display: robotLocked ? 'none' : undefined, width: 52, height: 52 }}
        aria-label="Open PAUL JARVIS assistant"
        className={`fixed bottom-5 right-5 z-50 w-13 h-13 rounded-full shadow-lg flex items-center justify-center transition-all duration-200 border ${
          open
            ? 'bg-gray-800 border-gray-600 scale-95'
            : 'bg-cyan-600 hover:bg-cyan-500 border-cyan-500'
        }`}
      >
        {open ? <ChevronDown className="w-5 h-5 text-white" /> : <Bot className="w-5 h-5 text-white" />}
        {/* Unread badge */}
        {!open && unreadCount > 0 && (
          <span className="absolute -top-1 -right-1 w-5 h-5 bg-red-500 text-white text-[10px] font-bold rounded-full flex items-center justify-center">
            {unreadCount > 9 ? '9+' : unreadCount}
          </span>
        )}
      </button>

      {/* ── Chat panel — hidden when robot holds exclusive mic/speaker ─────── */}
      {open && !robotLocked && (
        <div className="fixed bottom-20 right-5 z-50 w-[380px] max-h-[600px] flex flex-col bg-gray-900 border border-gray-700 rounded-2xl shadow-2xl overflow-hidden">

          {/* Header */}
          <div className="flex items-center gap-2.5 px-4 py-3 border-b border-gray-700/50 bg-gray-900/90 shrink-0">
            <div className="w-7 h-7 rounded-full bg-cyan-600 flex items-center justify-center">
              <Bot className="w-4 h-4 text-white" />
            </div>
            <div className="flex-1 min-w-0">
              <div className="text-sm font-semibold text-white flex items-center gap-1.5">
                PAUL
                {extConnected && (
                  <span className="inline-flex items-center gap-0.5 text-[8px] px-1 py-0.5 rounded bg-green-500/20 text-green-400 font-medium" title="JARVIS browser extension connected — reliable voice + desktop alerts">
                    <span className="w-1 h-1 rounded-full bg-green-400 animate-pulse" /> EXT
                  </span>
                )}
                {brainThinking && (
                  <span className="inline-flex items-center gap-0.5 text-[8px] px-1 py-0.5 rounded bg-amber-500/20 text-amber-300 font-medium" title={brainNote || 'JARVIS is researching your active goal in the background'}>
                    <span className="w-1 h-1 rounded-full bg-amber-300 animate-pulse" /> THINKING
                  </span>
                )}
              </div>
              <div className="text-[10px] text-gray-400 truncate">
                {brainThinking ? (brainNote ? `🧠 ${brainNote}` : '🧠 Working on your goal…')
                  : listening ? 'Listening…' : extConnected ? 'Say "Jarvis" (extension)' : wakeEnabled ? 'Say "Jarvis", "Paul" or "Sox"…' : 'Your JARVIS trading assistant'}
              </div>
            </div>
            {speechSupported && (
              <>
                {/* Wake-word toggle */}
                <button
                  onClick={() => setWakeEnabled(w => !w)}
                  className={`p-1.5 rounded transition ${wakeEnabled ? 'bg-cyan-600/30 text-cyan-300' : 'hover:bg-gray-700 text-gray-400'}`}
                  aria-label="Toggle 'Hi Jarvis' wake word"
                  title={wakeEnabled ? 'Wake word ON — say "Hi Jarvis"' : 'Enable "Hi Jarvis" wake word'}
                >
                  <Ear className="w-4 h-4" />
                </button>
                {/* Voice (TTS) toggle */}
                <button
                  onClick={toggleVoice}
                  className={`p-1.5 rounded transition ${voiceEnabled ? 'bg-cyan-600/30 text-cyan-300' : 'hover:bg-gray-700 text-gray-400'}`}
                  aria-label="Toggle voice replies"
                  title={voiceEnabled ? 'Voice replies ON' : 'Enable voice replies'}
                >
                  {voiceEnabled ? <Volume2 className="w-4 h-4" /> : <VolumeX className="w-4 h-4" />}
                </button>
                {/* Voice settings (pick male/female + voice + rate/pitch) */}
                <button
                  onClick={() => setSettingsOpen(o => !o)}
                  className={`p-1.5 rounded transition ${settingsOpen ? 'bg-cyan-600/30 text-cyan-300' : 'hover:bg-gray-700 text-gray-400'}`}
                  aria-label="Voice settings"
                  title="Voice settings"
                >
                  <Settings className="w-4 h-4" />
                </button>
              </>
            )}
            {/* New chat */}
            <button
              onClick={newChat}
              className="p-1.5 hover:bg-gray-700 rounded transition"
              aria-label="Start a new chat"
              title="New chat (archives current)"
            >
              <Trash2 className="w-4 h-4 text-gray-400" />
            </button>
            {/* Alert toggle */}
            <button
              onClick={() => setAlertsOpen(o => !o)}
              className="relative p-1.5 hover:bg-gray-700 rounded transition"
              aria-label="Toggle alerts"
            >
              <Bell className="w-4 h-4 text-gray-400" />
              {unreadCount > 0 && (
                <span className="absolute -top-0.5 -right-0.5 w-4 h-4 bg-red-500 text-white text-[9px] font-bold rounded-full flex items-center justify-center">
                  {unreadCount > 9 ? '9+' : unreadCount}
                </span>
              )}
            </button>
            <button onClick={() => setOpen(false)} className="p-1.5 hover:bg-gray-700 rounded transition">
              <X className="w-4 h-4 text-gray-400" />
            </button>
          </div>

          {/* Alerts panel */}
          {alertsOpen && (
            <div className="border-b border-gray-700/50 bg-gray-950/60 max-h-40 overflow-y-auto shrink-0">
              {alerts.filter(a => !a.read).length === 0 ? (
                <div className="px-4 py-3 text-[11px] text-gray-500">No pending alerts.</div>
              ) : (
                alerts.filter(a => !a.read).map(alert => (
                  <div key={alert.id} className="flex items-start gap-2 px-3 py-2 border-b border-gray-800/60 last:border-0">
                    {alertIcon(alert.type)}
                    <span className="flex-1 text-[11px] text-gray-200">{alert.message}</span>
                    <button onClick={() => dismissAlert(alert.id)} className="shrink-0">
                      <X className="w-3 h-3 text-gray-600 hover:text-white" />
                    </button>
                  </div>
                ))
              )}
            </div>
          )}

          {/* Voice settings panel */}
          {settingsOpen && (
            <div className="border-b border-gray-700/50 bg-gray-950/70 px-4 py-3 space-y-3 shrink-0 max-h-72 overflow-y-auto">
              <div className="text-[11px] font-semibold text-cyan-300 uppercase tracking-wide">JARVIS Voice</div>

              {/* AI Models Toggle — Whisper and OpenAI TTS */}
              <div className="space-y-2 p-2 bg-cyan-950/20 border border-cyan-500/20 rounded-lg">
                <label className="flex items-center justify-between cursor-pointer group">
                  <div className="flex flex-col">
                    <span className="text-[10px] text-cyan-100 font-medium">AI Hearing (Whisper)</span>
                    <span className="text-[8px] text-gray-500">Superior accuracy, handles noise better</span>
                  </div>
                  <button
                    onClick={() => setAiSpeechEnabled(!aiSpeechEnabled)}
                    className={`relative w-8 h-4 rounded-full transition-colors ${aiSpeechEnabled ? 'bg-cyan-500' : 'bg-gray-700'}`}
                  >
                    <span className={`absolute top-0.5 w-3 h-3 bg-white rounded-full shadow transition-transform ${aiSpeechEnabled ? 'translate-x-4.5' : 'translate-x-0.5'}`} />
                  </button>
                </label>
                
                <label className="flex items-center justify-between cursor-pointer group">
                  <div className="flex flex-col">
                    <span className="text-[10px] text-cyan-100 font-medium">AI Voice (OpenAI TTS)</span>
                    <span className="text-[8px] text-gray-500">More natural, professional voices</span>
                  </div>
                  <button
                    onClick={() => setAiVoiceEnabled(!aiVoiceEnabled)}
                    className={`relative w-8 h-4 rounded-full transition-colors ${aiVoiceEnabled ? 'bg-cyan-500' : 'bg-gray-700'}`}
                  >
                    <span className={`absolute top-0.5 w-3 h-3 bg-white rounded-full shadow transition-transform ${aiVoiceEnabled ? 'translate-x-4.5' : 'translate-x-0.5'}`} />
                  </button>
                </label>
                
                {aiVoiceEnabled && (
                  <div className="pt-1">
                    <label className="block text-[9px] text-gray-500 mb-1">AI Voice Model</label>
                    <div className="grid grid-cols-3 gap-1">
                      {['alloy', 'echo', 'fable', 'onyx', 'nova', 'shimmer'].map(v => (
                        <button
                          key={v}
                          onClick={() => setAiVoice(v)}
                          className={`py-1 rounded border text-[9px] capitalize transition ${aiVoice === v ? 'bg-cyan-600/30 border-cyan-500 text-cyan-100' : 'border-gray-800 text-gray-500 hover:bg-gray-900'}`}
                        >
                          {v}
                        </button>
                      ))}
                    </div>
                  </div>
                )}
              </div>

              {/* Male / Female quick pick */}
              <div className="flex gap-2">
                <button
                  onClick={() => pickGenderVoice('male')}
                  className={`flex-1 py-1.5 rounded-lg text-xs font-medium transition border ${
                    voiceGender === 'male'
                      ? 'bg-cyan-600/30 border-cyan-500 text-cyan-200'
                      : 'border-gray-700 text-gray-400 hover:bg-gray-800'
                  }`}
                >
                  ♂ Male
                </button>
                <button
                  onClick={() => pickGenderVoice('female')}
                  className={`flex-1 py-1.5 rounded-lg text-xs font-medium transition border ${
                    voiceGender === 'female'
                      ? 'bg-pink-600/30 border-pink-500 text-pink-200'
                      : 'border-gray-700 text-gray-400 hover:bg-gray-800'
                  }`}
                >
                  ♀ Female
                </button>
              </div>

              {/* Voice picker (all installed English voices, Siri-style choice) */}
              <div>
                <label className="block text-[10px] text-gray-500 mb-1">Voice ({voiceList.length} available)</label>
                <select
                  value={voiceURI}
                  onChange={e => setVoiceURI(e.target.value)}
                  className="w-full bg-gray-800 border border-gray-700 rounded-lg px-2 py-1.5 text-xs text-white focus:outline-none focus:border-cyan-600"
                >
                  {voiceList.length === 0 && <option value="">Loading voices…</option>}
                  {voiceList.map(v => (
                    <option key={v.voiceURI} value={v.voiceURI}>
                      {v.name} — {v.lang}{/premium|enhanced|neural|natural|siri/i.test(v.name) ? ' ★' : ''}
                    </option>
                  ))}
                </select>
                <p className="text-[9px] text-gray-600 mt-1">★ = premium / neural voice (most natural). Tip: install extra system voices for an even better JARVIS.</p>
              </div>

              {/* Rate */}
              <div>
                <label className="flex justify-between text-[10px] text-gray-500 mb-1">
                  <span>Speed</span><span>{voiceRate.toFixed(2)}×</span>
                </label>
                <input
                  type="range" min={0.7} max={1.2} step={0.02}
                  value={voiceRate}
                  onChange={e => setVoiceRate(Number(e.target.value))}
                  className="w-full accent-cyan-500"
                />
              </div>

              {/* Pitch */}
              <div>
                <label className="flex justify-between text-[10px] text-gray-500 mb-1">
                  <span>Pitch</span><span>{voicePitch.toFixed(2)}</span>
                </label>
                <input
                  type="range" min={0.6} max={1.3} step={0.02}
                  value={voicePitch}
                  onChange={e => setVoicePitch(Number(e.target.value))}
                  className="w-full accent-cyan-500"
                />
              </div>

              <button
                onClick={testVoice}
                className="w-full flex items-center justify-center gap-1.5 py-1.5 rounded-lg bg-cyan-600 hover:bg-cyan-500 text-white text-xs font-medium transition"
              >
                <Play className="w-3.5 h-3.5" /> Test voice
              </button>

              {/* ── Noise Reduction ─────────────────────────────── */}
              <div className="border-t border-gray-800 pt-3 space-y-2">
                <div className="text-[11px] font-semibold text-amber-300 uppercase tracking-wide">Noise Reduction</div>
                <p className="text-[9px] text-gray-500 leading-relaxed">
                  Controls how confident the speech engine must be before JARVIS reacts.
                  Raise the threshold in noisy environments to stop false triggers.
                </p>

                {/* Auto-detect toggle */}
                <label className="flex items-center justify-between cursor-pointer">
                  <span className="text-[10px] text-gray-400">Auto-detect noise level</span>
                  <button
                    onClick={() => {
                      const next = !autoNoise
                      setAutoNoise(next)
                      if (next) {
                        // Run a short silent listening period to estimate ambient noise level.
                        // We measure the average confidence of spurious (no-speech) results
                        // and set the threshold slightly above it.
                        const SR = getSpeechRecognition()
                        if (!SR) return
                        const rec = new SR()
                        rec.lang = 'en-US'; rec.continuous = true; rec.interimResults = true
                        let samples: number[] = []; let timeout: any
                        rec.onresult = (e: any) => {
                          for (let i = e.resultIndex; i < e.results.length; i++) {
                            const conf = typeof e.results[i][0].confidence === 'number' ? e.results[i][0].confidence : 0.5
                            samples.push(conf)
                          }
                        }
                        rec.onend = () => {
                          if (samples.length > 2) {
                            const avg = samples.reduce((a, b) => a + b, 0) / samples.length
                            const auto = Math.min(0.95, Math.round((avg + 0.1) * 20) / 20)
                            setNoiseThreshold(auto)
                            speak(`Noise level calibrated, Sir. Threshold set to ${Math.round(auto * 100)}%.`)
                          }
                        }
                        try { rec.start() } catch { /* noop */ }
                        timeout = setTimeout(() => { try { rec.stop() } catch { /* noop */ } }, 5000)
                        return () => clearTimeout(timeout)
                      }
                    }}
                    className={`relative w-9 h-5 rounded-full transition-colors ${autoNoise ? 'bg-amber-500' : 'bg-gray-600'}`}
                  >
                    <span className={`absolute top-0.5 w-4 h-4 bg-white rounded-full shadow transition-transform ${autoNoise ? 'translate-x-4' : 'translate-x-0.5'}`} />
                  </button>
                </label>

                {/* Manual threshold slider */}
                <div>
                  <label className="flex justify-between text-[10px] text-gray-500 mb-1">
                    <span>Confidence Threshold</span>
                    <span className="text-amber-300">{Math.round(noiseThreshold * 100)}%
                      {noiseThreshold < 0.3 ? ' (sensitive)' : noiseThreshold > 0.7 ? ' (strict)' : ' (balanced)'}
                    </span>
                  </label>
                  <input
                    type="range" min={0.1} max={0.95} step={0.05}
                    value={noiseThreshold}
                    onChange={e => setNoiseThreshold(Number(e.target.value))}
                    className="w-full accent-amber-500"
                  />
                  <div className="flex justify-between text-[9px] text-gray-600 mt-0.5">
                    <span>Sensitive</span><span>Balanced</span><span>Strict</span>
                  </div>
                </div>

                {/* Preset buttons */}
                <div className="flex gap-2">
                  {([['Quiet', 0.40], ['Normal', 0.65], ['Noisy', 0.80], ['Very Noisy', 0.90]] as [string, number][]).map(([label, val]) => (
                    <button key={label} onClick={() => setNoiseThreshold(val)}
                      className={`flex-1 py-1 rounded text-[9px] font-medium transition border ${Math.abs(noiseThreshold - val) < 0.06 ? 'bg-amber-600/30 border-amber-500 text-amber-200' : 'border-gray-700 text-gray-500 hover:bg-gray-800'}`}
                    >
                      {label}
                    </button>
                  ))}
                </div>
              </div>

              {/* ── Wake activation: require greeting ─────────────────── */}
              <div className="border-t border-gray-800 pt-3 space-y-2">
                <label className="flex items-center justify-between cursor-pointer">
                  <span className="text-[10px] text-gray-400">Require greeting (say “Hey Jarvis”)</span>
                  <button
                    onClick={() => setWakeRequireGreeting(v => !v)}
                    className={`relative w-9 h-5 rounded-full transition-colors ${wakeRequireGreeting ? 'bg-green-500' : 'bg-gray-600'}`}
                  >
                    <span className={`absolute top-0.5 w-4 h-4 bg-white rounded-full shadow transition-transform ${wakeRequireGreeting ? 'translate-x-4' : 'translate-x-0.5'}`} />
                  </button>
                </label>
                <p className="text-[9px] text-gray-600 leading-relaxed">
                  Off (default): just say a name — “Jarvis”, “Paul”, or “Sox” — to wake JARVIS. On:
                  a greeting word must precede the name — useful in noisy rooms to cut false triggers.
                </p>
              </div>

              {/* ── Voice Profile / Speaker ID ───────────────────────── */}
              <div className="border-t border-gray-800 pt-3 space-y-2">
                <div className="text-[11px] font-semibold text-green-300 uppercase tracking-wide">
                  Voice Profile <span className="text-[9px] normal-case text-gray-500 font-normal">(Siri-style speaker ID)</span>
                </div>
                <p className="text-[9px] text-gray-500 leading-relaxed">
                  Trains JARVIS to recognise YOUR voice specifically using statistical band-distance
                  matching + temporal consistency (30-frame rolling window). Once calibrated,
                  TV, music, other people, and background noise are silently ignored.
                  Only your voice triggers JARVIS.
                </p>

                {voiceProfile ? (
                  <div className="rounded bg-green-900/20 border border-green-600/30 px-2 py-1.5 flex items-center justify-between">
                    <div>
                      <span className="text-[10px] text-green-300 font-medium">✓ Voice profile active</span>
                      <span className="text-[9px] text-gray-500 ml-2">
                        {new Date(voiceProfile.calibratedAt).toLocaleDateString()}
                      </span>
                    </div>
                    <button onClick={() => { deleteVoiceProfile(); setVoiceProfile(null); setVoiceMatchEnabled(false); localStorage.setItem('paul.voiceMatchEnabled','0') }}
                      className="text-[9px] text-red-400 hover:text-red-300">
                      Delete
                    </button>
                  </div>
                ) : (
                  <div className="rounded bg-gray-800/50 border border-gray-700 px-2 py-1.5 text-[9px] text-gray-500">
                    No voice profile. Calibrate to enable speaker recognition.
                  </div>
                )}

                {/* Test voice recognition against stored profile */}
                {voiceProfile && (
                  <div className="space-y-1">
                    <button
                      disabled={profileTesting}
                      onClick={testVoiceProfile}
                      className={`w-full flex items-center justify-center gap-2 py-1.5 rounded-lg text-xs font-medium transition border ${
                        profileTesting
                          ? 'bg-cyan-700/40 border-cyan-600/50 text-cyan-200 cursor-wait'
                          : 'border-cyan-700 text-cyan-300 hover:bg-cyan-700/20'
                      }`}
                    >
                      {profileTesting ? (
                        <>
                          <span className="w-1.5 h-1.5 bg-cyan-400 rounded-full animate-ping" />
                          Listening 3s — speak now…
                        </>
                      ) : (
                        <>🎧 Test voice recognition (3s)</>
                      )}
                    </button>
                    {profileTestResult === 'match' && (
                      <p className="text-[9px] text-green-400 text-center">✓ Voice matched — profile working</p>
                    )}
                    {profileTestResult === 'no_match' && (
                      <p className="text-[9px] text-red-400 text-center">✗ Voice not matched — try recalibrating</p>
                    )}
                  </div>
                )}

                {/* Enable / disable speaker ID */}
                {voiceProfile && (
                  <label className="flex items-center justify-between cursor-pointer">
                    <span className="text-[10px] text-gray-400">Require my voice to activate JARVIS</span>
                    <button
                      onClick={() => {
                        const next = !voiceMatchEnabled
                        setVoiceMatchEnabled(next)
                        localStorage.setItem('paul.voiceMatchEnabled', next ? '1' : '0')
                      }}
                      className={`relative w-9 h-5 rounded-full transition-colors ${voiceMatchEnabled ? 'bg-green-500' : 'bg-gray-600'}`}
                    >
                      <span className={`absolute top-0.5 w-4 h-4 bg-white rounded-full shadow transition-transform ${voiceMatchEnabled ? 'translate-x-4' : 'translate-x-0.5'}`} />
                    </button>
                  </label>
                )}

                {/* Calibration button */}
                <button
                  disabled={calibrating}
                  onClick={calibrateVoice}
                  className={`w-full flex items-center justify-center gap-2 py-2 rounded-lg text-xs font-medium transition ${
                    calibrating ? 'bg-green-700/50 text-green-200 cursor-wait' : 'bg-green-600 hover:bg-green-500 text-white'
                  }`}
                >
                  {calibrating ? (
                    <>
                      <span className="w-2 h-2 bg-green-300 rounded-full animate-ping" />
                      {calibCountdown > 8
                        ? `Get ready… starting in ${calibCountdown - 8}s`
                        : calibCountdown > 0
                          ? `Recording… ${calibCountdown}s — speak naturally`
                          : 'Processing profile…'
                      }
                    </>
                  ) : (
                    <>{voiceProfile ? '🔄 Recalibrate voice' : '🎤 Calibrate my voice (8s)'}</>
                  )}
                </button>
                <p className="text-[9px] text-gray-600">Tip: speak naturally for 8 seconds in your normal environment — talk about trading, markets, anything. The more natural the better. JARVIS stays completely silent during recording.</p>
              </div>

              {/* ── Deepgram cost-aware fallback ──────────────────────── */}
              <div className="border-t border-gray-800 pt-3 space-y-2">
                <div className="text-[11px] font-semibold text-cyan-300 uppercase tracking-wide">
                  Deepgram Fallback <span className="text-[9px] normal-case text-gray-500 font-normal">(only when JARVIS mis-hears)</span>
                </div>
                <div className="rounded bg-gray-800/50 border border-gray-700 px-2 py-1.5 flex items-center justify-between">
                  <span className="text-[10px] text-gray-400">Status</span>
                  {dgFallbackState === 'armed' ? (
                    <span className="text-[10px] text-green-300 font-medium flex items-center gap-1">
                      <span className="w-1.5 h-1.5 bg-green-400 rounded-full" /> Armed
                    </span>
                  ) : dgFallbackState === 'paused' ? (
                    <span className="text-[10px] text-amber-300 font-medium flex items-center gap-1">
                      <span className="w-1.5 h-1.5 bg-amber-400 rounded-full" /> Paused (budget)
                    </span>
                  ) : (
                    <span className="text-[10px] text-gray-500 font-medium flex items-center gap-1">
                      <span className="w-1.5 h-1.5 bg-gray-500 rounded-full" /> Off
                    </span>
                  )}
                </div>
                {dgUsage && (
                  <div className="rounded bg-gray-800/50 border border-gray-700 px-2 py-1.5 space-y-1">
                    <div className="flex items-center justify-between">
                      <span className="text-[10px] text-gray-400">Budget left this month</span>
                      <span className="text-[10px] text-cyan-300 font-medium">
                        ${dgUsage.remaining.toFixed(2)} / ${dgUsage.monthly_cap.toFixed(0)}
                      </span>
                    </div>
                    {dgUsage.projected_runway_days != null && (
                      <div className="flex items-center justify-between">
                        <span className="text-[10px] text-gray-400">Projected runway</span>
                        <span className="text-[10px] text-gray-300">~{Math.round(dgUsage.projected_runway_days)} days</span>
                      </div>
                    )}
                  </div>
                )}
                <p className="text-[9px] text-gray-600 leading-relaxed">
                  The free browser engine stays primary. Only a missed command is re-checked once via
                  cheap Deepgram speech-to-text, and it silently pauses when the monthly budget cap is reached.
                  {voiceProfile && voiceMatchEnabled
                    ? ' With voice match on, Deepgram only ever hears your calibrated voice — not the TV or other people.'
                    : ' Calibrate your voice above to restrict Deepgram to your voice only.'}
                </p>
              </div>

              {/* ── Voice Detection Diagnostics ──────────────────────── */}
              <div className="border-t border-gray-800 pt-3 space-y-2">
                <div className="flex items-center justify-between">
                  <div className="text-[11px] font-semibold text-orange-300 uppercase tracking-wide">
                    Voice Detection Test
                  </div>
                  <button
                    onClick={() => { setDiagMode(d => !d); setVoiceDiagLog([]) }}
                    className={`relative w-9 h-5 rounded-full transition-colors ${diagMode ? 'bg-orange-500' : 'bg-gray-600'}`}
                  >
                    <span className={`absolute top-0.5 w-4 h-4 bg-white rounded-full shadow transition-transform ${diagMode ? 'translate-x-4' : 'translate-x-0.5'}`} />
                  </button>
                </div>
                <p className="text-[9px] text-gray-500">Enable to see what JARVIS hears and why sounds are blocked. Speak, play TV nearby, or test wake phrase "Hey Jarvis". Green = wake detected, red = blocked.</p>
                {diagMode && (
                  <div className="space-y-1 max-h-32 overflow-y-auto">
                    {voiceDiagLog.length === 0 && <p className="text-[9px] text-gray-600 italic">Waiting for speech…</p>}
                    {voiceDiagLog.map((entry, i) => (
                      <div key={i} className={`flex items-start gap-1.5 px-2 py-1 rounded text-[9px] ${
                        entry.action === 'wake' ? 'bg-green-900/30 border border-green-600/40' :
                        entry.action === 'blocked_conf' ? 'bg-red-900/20 border border-red-600/30' :
                        entry.action === 'blocked_profile' ? 'bg-amber-900/20 border border-amber-600/30' :
                        'bg-gray-800/50 border border-gray-700/30'
                      }`}>
                        <span className={`shrink-0 font-bold ${
                          entry.action === 'wake' ? 'text-green-400' :
                          entry.action === 'blocked_conf' ? 'text-red-400' :
                          entry.action === 'blocked_profile' ? 'text-amber-400' :
                          'text-gray-500'
                        }`}>
                          {entry.action === 'wake' ? '✓ WAKE' :
                           entry.action === 'blocked_conf' ? '✗ LOW CONF' :
                           entry.action === 'blocked_profile' ? '✗ VOICE≠PROFILE' :
                           '— ignored'}
                        </span>
                        <span className="text-gray-300 break-all flex-1">"{entry.t}"</span>
                        {entry.conf > 0 && <span className="text-gray-500 shrink-0">{entry.conf}%</span>}
                      </div>
                    ))}
                  </div>
                )}
              </div>

              <div className="text-[9px] text-gray-600 leading-relaxed border-t border-gray-800 pt-2">
                <span className="text-gray-500 font-medium">Hands-free:</span> say "Jarvis, …" in one breath, e.g.
                "Jarvis, open MT5 Live", "Jarvis, analyse Gold for sniper entries", "Jarvis, scroll down", or just ask a question. ("Hey Jarvis" still works.)
              </div>
            </div>
          )}

          {/* Messages */}
          <div className="flex-1 overflow-y-auto px-4 py-3 space-y-3 min-h-0">
            {messages.map(msg => (
              <div key={msg.id} className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}>
                {msg.role === 'assistant' && (
                  <div className="w-5 h-5 rounded-full bg-cyan-700 flex items-center justify-center mr-1.5 mt-0.5 shrink-0">
                    <Bot className="w-3 h-3 text-white" />
                  </div>
                )}
                <div className={`max-w-[85%] flex flex-col gap-1.5 ${msg.role === 'user' ? 'items-end' : 'items-start'}`}>
                  <div
                    className={`px-3 py-2 rounded-xl text-[13px] leading-relaxed whitespace-pre-wrap ${
                      msg.role === 'user'
                        ? 'bg-cyan-600 text-white rounded-tr-sm'
                        : 'bg-gray-800 text-gray-100 rounded-tl-sm'
                    }`}
                  >
                    {msg.role === 'assistant' ? renderMarkdown(msg.content) : msg.content}
                    {msg.pending && !msg.content && (
                      <span className="inline-flex items-center gap-1.5 text-cyan-300">
                        <span className="animate-pulse text-sm leading-none">⚡</span>
                        <span className="font-medium">Thinking</span>
                        <span className="inline-flex gap-0.5">
                          {[0,1,2].map(i => (
                            <span key={i} className="w-1 h-1 bg-cyan-300 rounded-full animate-bounce" style={{ animationDelay: `${i * 0.15}s` }} />
                          ))}
                        </span>
                      </span>
                    )}
                    {msg.pending && msg.content && (
                      <span className="inline-flex gap-0.5 ml-1 align-middle">
                        {[0,1,2].map(i => (
                          <span key={i} className="w-1 h-1 bg-cyan-300 rounded-full animate-bounce" style={{ animationDelay: `${i * 0.15}s` }} />
                        ))}
                      </span>
                    )}
                  </div>

                  {/* Sniper setup action cards (Execute = place pending limit + TP) */}
                  {msg.role === 'assistant' && msg.sniperSetups && msg.sniperSetups.length > 0 && (
                    <div className="w-full space-y-1.5">
                      {msg.sniperSetups.map((s, i) => {
                        const st = setupStatus[`${msg.id}:${i}`] || { status: 'idle' as const }
                        const px = (n: number) => formatSniperPrice(n, s.pointSize)
                        return (
                          <div key={i} className="rounded-lg border border-gray-700 bg-gray-900/60 px-2.5 py-2 text-[11px]">
                            <div className="flex items-center justify-between mb-1.5">
                              <span className={`px-1.5 py-0.5 rounded font-bold ${
                                s.side === 'buy' ? 'bg-green-600/25 text-green-300' : 'bg-red-600/25 text-red-300'
                              }`}>
                                {s.side.toUpperCase()} LIMIT
                              </span>
                              <span className="text-gray-400">
                                {s.rr != null ? `RR ${s.rr}` : ''}{s.confidence != null ? `${s.rr != null ? ' · ' : ''}${(s.confidence * 100).toFixed(0)}%` : ''}
                              </span>
                            </div>
                            <div className="grid grid-cols-3 gap-1 mb-2">
                              <div><div className="text-gray-500">Entry</div><div className="text-gray-200">{px(s.entry)}</div></div>
                              <div><div className="text-gray-500">SL</div><div className="text-red-400">{px(s.stop_loss)}</div></div>
                              <div><div className="text-gray-500">TP</div><div className="text-green-400">{px(s.take_profit)}</div></div>
                            </div>
                            <button
                              onClick={() => placeSniperSetup(msg.id, i, s)}
                              disabled={st.status === 'placing' || st.status === 'placed'}
                              className={`w-full py-1.5 rounded-md font-semibold transition disabled:cursor-not-allowed ${
                                st.status === 'placed'
                                  ? 'bg-green-700/40 text-green-300'
                                  : st.status === 'placing'
                                    ? 'bg-gray-700 text-gray-300'
                                    : 'bg-cyan-600 hover:bg-cyan-500 text-white'
                              }`}
                            >
                              {st.status === 'placing' ? 'Placing…' : st.status === 'placed' ? 'Placed ✓' : 'Execute'}
                            </button>
                            {st.status === 'placed' && st.msg && <div className="text-green-400 mt-1">{st.msg}</div>}
                            {st.status === 'error' && st.msg && <div className="text-red-400 mt-1">{st.msg}</div>}
                          </div>
                        )
                      })}
                    </div>
                  )}
                </div>
              </div>
            ))}
            <div ref={messagesEndRef} />
          </div>

          {/* Input */}
          <div className="flex items-center gap-2 px-3 py-2.5 border-t border-gray-700/50 shrink-0 relative">
            <input
              ref={inputRef}
              value={input}
              onChange={e => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder={listening ? (recording ? 'Recording…' : 'Listening…') : 'Ask PAUL anything…'}
              disabled={streaming}
              className="flex-1 bg-gray-800 border border-gray-700 rounded-xl px-3 py-2 text-sm text-white placeholder-gray-500 focus:outline-none focus:border-cyan-600 disabled:opacity-50"
            />
            {/* ── Mini Binary Engine — shows voice frequency bars when mic is active ── */}
            {voiceEnabled && (
              <canvas
                ref={miniCanvasRef}
                width={96}
                height={20}
                className="absolute left-3 top-1/2 -translate-y-1/2 rounded opacity-80 pointer-events-none"
                style={{ mixBlendMode: 'screen' }}
                aria-hidden="true"
              />
            )}
            {recording && (
              <div className="absolute right-14 flex items-center gap-1 px-1.5 py-0.5 bg-red-900/40 border border-red-500/40 rounded text-[8px] text-red-300 font-bold animate-pulse">
                <div className="w-1.5 h-1.5 rounded-full bg-red-500" /> REC
              </div>
            )}
            {speechSupported && (
              <button
                onClick={toggleMic}
                disabled={streaming}
                aria-label={listening ? 'Stop listening' : 'Speak to PAUL'}
                title={listening ? 'Stop listening' : 'Speak to PAUL'}
                className={`w-8 h-8 rounded-xl flex items-center justify-center transition disabled:opacity-40 ${
                  listening
                    ? 'bg-red-600 hover:bg-red-500 animate-pulse'
                    : 'bg-gray-700 hover:bg-gray-600'
                }`}
              >
                {listening ? <MicOff className="w-3.5 h-3.5 text-white" /> : <Mic className="w-3.5 h-3.5 text-white" />}
              </button>
            )}
            <button
              onClick={() => send()}
              disabled={!input.trim() || streaming}
              className="w-8 h-8 rounded-xl bg-cyan-600 hover:bg-cyan-500 disabled:opacity-40 flex items-center justify-center transition"
            >
              <Send className="w-3.5 h-3.5 text-white" />
            </button>
          </div>
        </div>
      )}
    </>
  )
})

export default PaulChat
