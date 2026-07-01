/**
 * Voice command interpreter — hands-free control of the app.
 *
 * Parses a spoken transcript and returns a structured action the JARVIS
 * widget executes (navigate, click, scroll, back, etc.). Designed so a user
 * with no hands can drive the entire app by voice.
 *
 * The interpreter is pure (no DOM/router access) — the component executes
 * the returned action.
 */

export interface VoiceAction {
  type:
    | 'navigate'
    | 'click'
    | 'type'
    | 'scroll'
    | 'back'
    | 'forward'
    | 'reload'
    | 'top'
    | 'bottom'
    | 'open_chat'
    | 'close_chat'
    | 'new_chat'
    | 'stop_listening'
    | 'repeat'
    | 'help'
    // ── Hands-free form / UI control ──
    | 'set_field'
    | 'select_option'
    | 'toggle'
    | 'switch_tab'
    | 'set_timeframe'
    | 'submit_form'
    | 'cancel'
    // ── Trading verbs ──
    | 'set_leverage'
    | 'set_amount'
    | 'close_position'
    | 'close_all'
  path?: string
  target?: string
  field?: string
  value?: string
  text?: string
  direction?: 'up' | 'down'
  say: string // spoken confirmation
}

// ── App routes with natural-language aliases ─────────────────────────────────
export const NAV_ROUTES: { label: string; path: string; aliases: string[] }[] = [
  { label: 'Dashboard', path: '/', aliases: ['dashboard', 'home', 'main', 'overview'] },
  { label: 'Trading', path: '/trading', aliases: ['trading', 'trade', 'orders', 'spot'] },
  { label: 'Futures', path: '/futures', aliases: ['futures', 'perpetuals', 'perps'] },
  { label: 'Signals', path: '/signals', aliases: ['signals', 'signal feed', 'tradingview signals'] },
  { label: 'Telegram Signals', path: '/telegram-signals', aliases: ['telegram signals', 'telegram feed', 'channel signals'] },
  { label: 'Trending', path: '/trending', aliases: ['trending', 'trends', 'hot coins'] },
  { label: 'Strategies', path: '/strategies', aliases: ['strategies', 'strategy', 'pine scripts', 'pine'] },
  { label: 'Sentiment', path: '/sentiment', aliases: ['sentiment', 'news sentiment', 'mood'] },
  { label: 'Pump Monitor', path: '/pump-monitor', aliases: ['pump monitor', 'pumps', 'pump scanner'] },
  { label: 'Rug Pulled', path: '/rug-pulled', aliases: ['rug pulled', 'rug pulls', 'rugs'] },
  { label: 'Sniper Signals', path: '/sniper-signals', aliases: ['sniper signals', 'sniper', 'snipe'] },
  { label: 'Smart Money Concepts', path: '/smart-money-concepts', aliases: ['smart money concepts', 'smart money', 'smc', 'order blocks'] },
  { label: 'Delistings', path: '/delistings', aliases: ['delistings', 'delisting', 'delisted'] },
  { label: 'AI Agents', path: '/agents', aliases: ['ai agents', 'agents'] },
  { label: 'Custom Agents', path: '/custom-agents', aliases: ['custom agents', 'my agents'] },
  { label: 'Agent Paul', path: '/agent-paul', aliases: ['agent paul', 'paul page', 'paul loop', 'paul decisions'] },
  { label: 'Intelligence', path: '/intelligence', aliases: ['intelligence', 'brain map', 'cyber brain', 'the brain', 'neural map'] },
  { label: 'Insights', path: '/insights', aliases: ['insights', 'insight'] },
  { label: 'MT5 Live', path: '/mt5-live', aliases: ['mt5 live', 'm t 5 live', 'mt5', 'metatrader', 'live account', 'live trading'] },
  { label: 'MT5 Replay', path: '/mt5-replay', aliases: ['mt5 replay', 'replay', 'trade replay'] },
  { label: 'MT5 Copy Sim', path: '/mt5-copy-sim', aliases: ['mt5 copy sim', 'copy sim', 'copy trading'] },
  { label: 'AI Analyst', path: '/ai-analysis', aliases: ['ai analyst', 'ai analysis', 'market analyst', 'analyst'] },
  { label: 'Telegram', path: '/telegram', aliases: ['telegram', 'connect telegram', 'connect ai'] },
  { label: 'AI Profiles', path: '/ai-agents-admin', aliases: ['ai profiles', 'profiles', 'agent profiles', 'providers'] },
  { label: 'Trade History', path: '/history', aliases: ['trade history', 'history', 'past trades'] },
  { label: 'Settings', path: '/settings', aliases: ['settings', 'preferences', 'config', 'configuration'] },
]

const NAV_VERBS = /\b(go to|goto|open|navigate to|navigate|show me|show|take me to|take me|bring up|switch to|jump to|visit)\b/

function normalize(s: string): string {
  return s.toLowerCase().replace(/[^\w\s]/g, ' ').replace(/\s+/g, ' ').trim()
}

// Match a spoken phrase to a route.
//  • Default (lenient) mode is for when the user clearly issued a navigation
//    verb ("open X", "go to X").
//  • strict mode is for bare page names with NO verb — here the command must be
//    essentially JUST the page name, so a long sentence that merely mentions a
//    route word never triggers an unwanted navigation (the old `.includes()`
//    substring match caused JARVIS to wrongly "open the dashboard").
function bestRoute(phrase: string, opts?: { strict?: boolean }): { label: string; path: string } | null {
  const strict = !!opts?.strict
  const p = normalize(phrase)
  if (!p) return null
  const pTokens = p.split(' ').filter(Boolean)
  const pSet = new Set(pTokens)
  let best: { label: string; path: string; score: number; aliasTokens: number } | null = null
  for (const r of NAV_ROUTES) {
    const candidates = [r.label.toLowerCase(), ...r.aliases]
    for (const c of candidates) {
      const cn = normalize(c)
      if (!cn) continue
      const ct = cn.split(' ').filter(Boolean)
      // Generic short single-word aliases (e.g. "home", "main") are dangerous —
      // they appear inside many unrelated commands. They may ONLY match when
      // they ARE the whole phrase, never as one word inside a longer command.
      const genericShort = ct.length === 1 && cn.length <= 5
      let score = 0
      if (p === cn) {
        score = 100
      } else if (!genericShort) {
        // Whole-word / whole-phrase containment (never a substring inside a word).
        const esc = cn.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
        const boundary = new RegExp(`(^|\\s)${esc}(\\s|$)`)
        if (boundary.test(p)) {
          score = 70 + cn.length
        } else {
          // Full token-set match: every alias token present as a whole word.
          const hit = ct.filter(tk => pSet.has(tk)).length
          if (hit > 0 && hit === ct.length) score = 50 + hit * 2
        }
      }
      if (score > 0 && (!best || score > best.score)) {
        best = { label: r.label, path: r.path, score, aliasTokens: ct.length }
      }
    }
  }
  if (!best) return null
  if (strict) {
    // Bare-name navigation: reject when the phrase is much longer than the
    // matched page name (i.e. it's a sentence, not a page name).
    if (best.score < 100 && pTokens.length > best.aliasTokens + 1) return null
    if (best.score < 60) return null
  } else if (best.score < 50) {
    return null
  }
  return { label: best.label, path: best.path }
}

/**
 * Interpret a transcript into a hands-free action, or null if it isn't a
 * recognised command (so the caller can fall back to chat Q&A).
 */
export function interpretVoiceCommand(raw: string): VoiceAction | null {
  const t = normalize(raw)
  if (!t) return null

  // ── Global widget controls ──────────────────────────────────────────────
  if (/\b(stop listening|stop the mic|mute the mic|stand down|that('?| i)s all)\b/.test(t))
    return { type: 'stop_listening', say: 'Standing by, Sir.' }
  // Re-read the last response on demand ("read that again", "repeat", etc.)
  if (/\b(read (that|it|this)? ?again|read me (that|it|the response|the answer)( again)?|repeat( that| it| the (response|answer))?|say (that|it) again|what did you say|come again)\b/.test(t))
    return { type: 'repeat', say: '' }
  if (/\b(new chat|start (a )?new chat|clear (the )?chat|reset (the )?conversation)\b/.test(t))
    return { type: 'new_chat', say: 'Starting a new session.' }
  if (/\b(close (the )?chat|hide (the )?chat|close yourself|minimi[sz]e)\b/.test(t))
    return { type: 'close_chat', say: 'Closing the panel.' }
  if (/\b(open (the )?chat|show (the )?chat|open yourself)\b/.test(t))
    return { type: 'open_chat', say: 'Panel open.' }

  // ── Scrolling / page motion ─────────────────────────────────────────────
  if (/\b(scroll (to (the )?)?top|go to (the )?top)\b/.test(t)) return { type: 'top', say: 'Top of the page.' }
  if (/\b(scroll (to (the )?)?bottom|go to (the )?bottom)\b/.test(t)) return { type: 'bottom', say: 'Bottom of the page.' }
  if (/\b(scroll up|page up)\b/.test(t)) return { type: 'scroll', direction: 'up', say: 'Scrolling up.' }
  if (/\b(scroll down|page down)\b/.test(t)) return { type: 'scroll', direction: 'down', say: 'Scrolling down.' }
  if (/\b(go back|navigate back|previous page)\b/.test(t)) return { type: 'back', say: 'Going back.' }
  if (/\b(go forward|navigate forward|next page)\b/.test(t)) return { type: 'forward', say: 'Going forward.' }
  if (/\b(reload|refresh)( the page)?\b/.test(t)) return { type: 'reload', say: 'Refreshing.' }

  if (/\b(what can i (say|do)|help|commands|list commands)\b/.test(t))
    return { type: 'help', say: 'I can navigate any page, click buttons, fill forms, switch tabs, change the timeframe, place or close trades, scroll, and answer questions. Say for example: open MT5 Live, set amount to 100, switch to 15 minute, close all positions, or ask me anything.' }

  // ── Dialog / form control: cancel & submit ──────────────────────────────
  if (/\b(cancel|dismiss|never ?mind|close (the )?(dialog|modal|popup|window)|escape|go back to the form)\b/.test(t))
    return { type: 'cancel', say: 'Cancelled, Sir.' }
  if (/\b(submit|submit (the )?form|send (the )?form|confirm (the )?form|go ahead|do it)\b/.test(t))
    return { type: 'submit_form', say: 'Submitting, Sir.' }

  // ── Trading verbs ───────────────────────────────────────────────────────
  // Close everything (destructive — the executor asks for confirmation first).
  if (/\b(close|exit|flatten)\s+(all|everything)\b/.test(t) || /\bclose all (positions|trades|orders)\b/.test(t) || /\bflatten\b/.test(t))
    return { type: 'close_all', say: 'This will close all open positions, Sir.' }
  // Close the current single position.
  if (/\b(close|exit)\s+(the\s+)?(current\s+|this\s+)?(position|trade|order)\b/.test(t))
    return { type: 'close_position', say: 'Closing the position, Sir.' }
  // Leverage: "set leverage to 10", "leverage 10x".
  let mTrade = raw.match(/\b(?:set\s+)?(?:the\s+)?leverage\s*(?:to|at|of|=)?\s*(\d+)\s*x?\b/i)
  if (mTrade && mTrade[1])
    return { type: 'set_leverage', value: mTrade[1], say: `Setting leverage to ${mTrade[1]}x.` }
  // Amount / size: "set amount to 100", "size 0.5".
  mTrade = raw.match(/\b(?:set\s+)?(?:the\s+)?(?:amount|size|quantity|qty|volume|lot|lots)\s*(?:to|at|of|=)?\s*([\d.]+)\b/i)
  if (mTrade && mTrade[1])
    return { type: 'set_amount', value: mTrade[1], say: `Setting amount to ${mTrade[1]}.` }

  // ── Chart timeframe: "switch to 15 minute", "1 hour chart", "set timeframe 4h" ──
  if (/\b(time ?frame|chart|candles?)\b/.test(t) || /\b(switch to|change to|set)\b/.test(t)) {
    const tf = raw.match(/\b(\d+)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days|w|week|weeks)\b/i)
    if (tf) {
      const num = tf[1]
      const u = tf[2].toLowerCase()
      const unit = u.startsWith('m') ? 'm' : u.startsWith('h') ? 'h' : u.startsWith('d') ? 'd' : 'w'
      const word = unit === 'm' ? 'minute' : unit === 'h' ? 'hour' : unit === 'd' ? 'day' : 'week'
      return { type: 'set_timeframe', value: `${num}${unit}`, say: `Switching to the ${num} ${word} chart.` }
    }
  }

  // ── Tabs: "switch to orders tab", "open positions tab", "history tab" ────
  let mTab = raw.match(/\b(?:switch to|go to|open|show|select)\s+(?:the\s+)?(.+?)\s+tab\b/i)
  if (!mTab) mTab = raw.match(/\b(.+?)\s+tab\b/i)
  if (mTab && mTab[1]) {
    const target = mTab[1].replace(/\b(the|that|this)\b/gi, '').trim()
    if (target) return { type: 'switch_tab', target, say: '' }
  }

  // ── Toggles / switches: "turn on X", "enable X", "toggle X", "disable X" ─
  const mToggle = raw.match(/\b(?:toggle|enable|disable|turn on|turn off|switch on|switch off)\b(?:\s+the)?\s+(.+)/i)
  if (mToggle && mToggle[1]) {
    const target = mToggle[1].replace(/\b(button|switch|toggle|option|setting)\b/gi, '').trim()
    if (target) return { type: 'toggle', target, say: '' }
  }

  // ── Dropdown / option select: "select BTCUSDT", "choose X from the pair menu" ─
  const mSel = raw.match(/\b(?:select|choose|pick)\b(?:\s+(?:the|a))?\s+(.+)/i)
  if (mSel && mSel[1]) {
    let rest = mSel[1].trim()
    let field: string | undefined
    const fm = rest.match(/^(.+?)\s+(?:from|in|on)\s+(?:the\s+)?(.+?)(?:\s+(?:dropdown|menu|list|select|field))?$/i)
    if (fm) { rest = fm[1].trim(); field = fm[2].replace(/\b(dropdown|menu|list|select|field)\b/gi, '').trim() }
    rest = rest.replace(/\b(option|dropdown|menu|list)\b/gi, '').trim()
    if (rest) return { type: 'select_option', target: rest, field, say: '' }
  }

  // ── Named field set: "set stop loss to 50", "enter 1.2345 into the price field" ─
  let mField = raw.match(/\b(?:enter|type|put|fill in?)\s+(.+?)\s+(?:in|into)\s+(?:the\s+)?([\w\s]+?)(?:\s+field)?$/i)
  if (mField && mField[1] && mField[2])
    return { type: 'set_field', field: mField[2].trim(), value: mField[1].trim(), say: `Set ${mField[2].trim()} to ${mField[1].trim()}.` }
  mField = raw.match(/\bset\s+(?:the\s+)?([\w\s]+?)\s+(?:field\s+)?(?:to|as|=)\s+(.+)/i)
  if (mField && mField[1] && mField[2])
    return { type: 'set_field', field: mField[1].trim(), value: mField[2].trim(), say: `Set ${mField[1].trim()} to ${mField[2].trim()}.` }

  // ── Type into a field: "type X" / "enter X" / "search for X" ────────────
  let m = raw.match(/\b(?:type|enter|input|write|search for)\b\s+(.+)/i)
  if (m && m[1]) {
    const text = m[1].trim()
    return { type: 'type', text, say: `Typing ${text}.` }
  }

  // ── Click: "click X" / "press X" / "tap X" / "select X" ─────────────────
  m = raw.match(/\b(?:click|press|tap|select|hit|push|choose|toggle|activate)\b(?:\s+(?:on|the))?\s+(.+)/i)
  if (m && m[1]) {
    const target = m[1].replace(/\b(button|link|tab|option|icon)\b/gi, '').trim() || m[1].trim()
    return { type: 'click', target, say: `` }
  }

  // ── Navigation: "go to X" / "open X" / bare page name ───────────────────
  if (NAV_VERBS.test(t)) {
    const phrase = t.replace(NAV_VERBS, ' ').replace(/\b(the|page|screen|tab|section)\b/g, ' ').trim()
    const r = bestRoute(phrase)
    if (r) return { type: 'navigate', path: r.path, say: `Opening ${r.label}.` }
  }

  // Bare page name (no verb) — only if it strongly matches a route.
  // IMPORTANT: If the transcript contains analytical/question intent words,
  // skip navigation and fall through to chat so JARVIS answers the question
  // instead of blindly opening a page (e.g. "analyse MT5 Live signals" must
  // NOT navigate — it should be answered by the AI).
  const analyticalIntent = /\b(analys[ei]|tell me|explain|describe|compare|recommend(ation)?|give me|forecast|predict|review|summari[sz]e|report|calculate|evaluate|assess|what (are|is|can)|how (do|can|does)|why|thoughts|opinion|insight|based on|sniper setup|setup|recommendation|advice|signal|signals|trade idea|ideas)\b/.test(t)
  if (!analyticalIntent) {
    // strict: the command must be essentially the page name itself, so an
    // unrelated sentence that merely mentions a route word never navigates.
    const bare = bestRoute(t, { strict: true })
    if (bare) return { type: 'navigate', path: bare.path, say: `Opening ${bare.label}.` }
  }

  return null
}

// ── Wake-phrase matching (fuzzy / phonetic) ──────────────────────────────────
// Small Levenshtein distance for tolerating minor speech-to-text mis-hearings.
function lev(a: string, b: string): number {
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
    ;[prev, curr] = [curr, prev]
  }
  return prev[n]
}

// True when a single spoken token sounds like one of the JARVIS wake names —
// "jarvis", "paul", or "sox" — even if slightly mis-heard ("jervis", "jarvas",
// "javis", "poll", "socks", "sax"). The short tokens "j" and "sir" are
// intentionally NOT accepted, to limit false matches.
function nameLike(w: string): boolean {
  if (!w) return false
  if (w === 'jarvis' || w === 'paul' || w === 'sox') return true
  if (lev(w, 'jarvis') <= 2) return true
  if (w.length >= 3 && lev(w, 'paul') <= 1) return true
  // Common speech-to-text mis-hearings of the short name "sox".
  if (w === 'socks' || w === 'sax' || w === 'sacks' || w === 'sachs') return true
  return false
}

// Greeting words that may optionally precede the wake name.
const WAKE_GREETING = '(?:hi|hey|hello|ok|okay|yo|yes|wake up|listen|attention)'
// Token form of the greeting words, used by the (punctuation-tolerant) matcher.
const WAKE_GREETING_SET = new Set([
  'hi', 'hey', 'hello', 'ok', 'okay', 'yo', 'yes', 'wake', 'up', 'listen', 'attention',
])

// Split a transcript into lowercase word tokens, dropping punctuation so that
// "Hey, Jarvis!" tokenises the same as "hey jarvis".
function wakeTokens(s: string): string[] {
  return (s || '').toLowerCase().replace(/[^a-z\s]/g, ' ').split(/\s+/).filter(Boolean)
}

/**
 * Detect a deliberate wake phrase, tolerating minor mis-hearings and any
 * punctuation between the greeting and the name ("Hey, Jarvis").
 *
 * - When `requireGreeting` is true, a greeting word MUST precede the name (the
 *   strict "greeting-gate"), so bare mentions of the name in TV/ambient audio
 *   do NOT trigger the assistant.
 * - When `requireGreeting` is false (default), the bare name at the START of
 *   the utterance also wakes ("Jarvis, ..."), while greeting + name keeps
 *   working too. False triggers are guarded by the confidence/noise gate and
 *   the optional speaker-ID voiceprint, not by the greeting requirement.
 */
export function phoneticWakeMatch(transcript: string, requireGreeting = false): boolean {
  const w = wakeTokens(transcript)
  if (!w.length) return false
  // Greeting immediately followed by a name-like token (works in both modes).
  for (let i = 0; i < w.length - 1; i++) {
    if (WAKE_GREETING_SET.has(w[i]) && nameLike(w[i + 1])) return true
  }
  if (requireGreeting) return false
  // Bare-name branch: the utterance must START with a name-like token so that
  // a stray name mid-sentence in ambient audio does not wake the assistant.
  return nameLike(w[0])
}

/**
 * Remove a leading wake phrase (greeting + name, or a bare name when allowed)
 * from a transcript and return the remaining command text. Used so the wake
 * name does not leak into the question sent to the AI.
 */
export function stripWakePhrase(transcript: string, requireGreeting = false): string {
  let t = (transcript || '').trim()
  if (!t) return ''
  // Try to strip a leading greeting + (fuzzy) name first, tolerating any
  // punctuation between them ("Hey, Jarvis" / "ok jarvis").
  const gn = t.match(new RegExp(`^[^a-z]*\\b${WAKE_GREETING}\\b[\\s,.:;!?-]+([a-z]+)\\b`, 'i'))
  if (gn && nameLike(gn[1].toLowerCase())) {
    t = t.slice(gn[0].length)
  } else if (!requireGreeting) {
    // Otherwise strip a leading bare name token.
    const bare = t.match(/^[^a-z]*([a-z]+)\b/i)
    if (bare && nameLike(bare[1].toLowerCase())) {
      t = t.slice(bare[0].length)
    }
  }
  // Drop leftover separators ("jarvis, help me" -> "help me").
  return t.replace(/^[\s,.:;!?-]+/, '').trim()
}
