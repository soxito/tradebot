/**
 * OblivionDashboard v5 — Pine study's dashboard rebuilt as a panel.
 * Shows dual scalp/sniper ticks, HTF/VWAP/Zone/OTE grid, Outcomes card and plan ladder.
 * Fed by the compact engine read (oblivion from any oblivion-overlay endpoint).
 */
export interface OblivionRead {
  engine?: string
  mode?: string
  timeframe?: string
  summary?: string
  score?: { buy: number; sell: number; bull: number; bear: number; required: number; scalp_min?: number; sniper_min?: number; components?: { buy: Record<string, number>; bear: Record<string, number>; bull: Record<string, number>; bear2?: any }; reasons?: string[]; setups?: any }
  market_bias?: number
  bias_label?: string
  trend?: { ema_fast?: number; ema_medium?: number; ema_slow?: number; ema_major?: number; bull?: boolean; bear?: boolean; major_bull?: boolean; major_bear?: boolean; now?: string }
  structure?: { now?: string; micro_high?: number; micro_low?: number; major_high?: number; major_low?: number; last_swing_high?: number | null; last_swing_low?: number | null; last_high_type?: string; last_low_type?: string; recent_events?: any[]; labels?: any[]; events?: any[] }
  pd?: { range_high?: number; range_low?: number; equilibrium?: number; zone?: string }
  fvg?: { bull?: { top: number; bottom: number } | null; bear?: { top: number; bottom: number } | null; inside_bull?: boolean; inside_bear?: boolean; now?: string; boxes?: any[] }
  order_blocks?: { bull?: { top: number; bottom: number } | null; bear?: { top: number; bottom: number } | null; inside_bull?: boolean; inside_bear?: boolean; boxes?: any[] }
  liquidity?: { prev_high?: number | null; prev_low?: number | null; bull_sweep?: boolean; bear_sweep?: boolean; recent_bull_sweep?: boolean; recent_bear_sweep?: boolean; now?: string; sweeps?: any[] }
  ote?: { bull?: { '618': number; '705': number; '786': number } | null; bear?: any; in_bull?: boolean; in_bear?: boolean; now?: string }
  fib?: { f382?: number | null; f500?: number | null; f618?: number | null; upper?: number | null; lower?: number | null; in_zone?: boolean; swing_high?: number | null; swing_low?: number | null }
  vwap?: { value?: number | null; available?: boolean; now?: string; bull?: boolean; bear?: boolean }
  htf?: { timeframe?: string; available?: boolean; bull?: boolean; bear?: boolean; close?: number; fast?: number; slow?: number; now?: string }
  equilibrium?: number | null
  premium?: boolean
  discount?: boolean
  session?: { name?: string; active?: boolean; overlap?: boolean }
  news?: { enabled?: boolean; in_window?: boolean; blocked?: boolean; window?: string; tz?: string }
  volume?: { level?: string; ratio?: number | null; expansion?: boolean }
  candle?: Record<string, boolean>
  signal?: { action: string; kind?: string | null; new?: boolean; confirmed?: boolean; score?: number; entry?: number | null; stop_loss?: number | null; risk?: number | null; tp1?: number | null; tp2?: number | null; tp3?: number | null; time?: number; blocked_by_news?: boolean; gate?: string; reasons?: string[] }
  last_signal?: any
  outcomes?: { count: number; tp1: number; tp2: number; tp3: number; sl: number; open: number; win_rate?: number | null; avg_r?: number | null; scalp?: number; sniper?: number }
  dashboard?: Record<string, string>
  config?: any
}

const fmt = (v: number | null | undefined) => {
  if (v == null || Number.isNaN(v)) return '—'
  const a = Math.abs(v)
  if (a >= 1000) return v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })
  if (a >= 1) return v.toFixed(4).replace(/\.?0+$/, '')
  return v.toFixed(6).replace(/\.?0+$/, '')
}

function ScoreBar({ label, value, scalpMin, sniperMin, color, comps }: { label: string; value: number; scalpMin: number; sniperMin: number; color: 'lime' | 'red'; comps: Record<string, number> }) {
  const fill = color === 'lime' ? 'bg-lime-500' : 'bg-red-500'
  const text = color === 'lime' ? 'text-lime-400' : 'text-red-400'
  const chip = color === 'lime' ? 'border-lime-500/40 bg-lime-500/10 text-lime-300' : 'border-red-500/40 bg-red-500/10 text-red-300'
  const items = Object.entries(comps || {})
  return (
    <div className="mb-4">
      <div className="flex items-center gap-3">
        <span className={`w-12 font-bold ${text}`}>{label}</span>
        <div className="relative flex-1 h-4 rounded bg-gray-800">
          <div className={`h-4 rounded ${fill}/80`} style={{ width: `${Math.min(12, value) / 12 * 100}%` }} />
          <div className="absolute -top-1 h-6 w-0.5 bg-yellow-400/70" style={{ left: `${scalpMin / 12 * 100}%` }} title={`scalp min ${scalpMin}`} />
          <div className="absolute -top-1 h-6 w-0.5 bg-fuchsia-400" style={{ left: `${sniperMin / 12 * 100}%` }} title={`sniper min ${sniperMin}`} />
        </div>
        <span className={`font-mono font-bold ${text}`}>{value}<span className="text-gray-500 text-xs font-normal">/12</span></span>
      </div>
      <div className="mt-1.5 ml-[60px] flex flex-wrap gap-1.5">
        {items.length === 0 && <span className="text-[11px] text-gray-600">no confluence</span>}
        {items.map(([k, v]) => <span key={k} className={`text-[11px] px-1.5 py-0.5 rounded border ${chip}`}>{k} +{v}</span>)}
      </div>
    </div>
  )
}

function Pill({ value, tone }: { value: string; tone: 'lime' | 'red' | 'gray' | 'yellow' | 'aqua' | 'orange' | 'white' | 'fuchsia' | 'cyan' }) {
  const cls: Record<string, string> = {
    lime: 'border-lime-500/40 bg-lime-500/10 text-lime-300',
    red: 'border-red-500/40 bg-red-500/10 text-red-300',
    gray: 'border-gray-600 bg-gray-800/60 text-gray-300',
    yellow: 'border-yellow-500/40 bg-yellow-500/10 text-yellow-300',
    aqua: 'border-cyan-500/40 bg-cyan-500/10 text-cyan-300',
    cyan: 'border-cyan-500/40 bg-cyan-500/10 text-cyan-300',
    orange: 'border-orange-500/40 bg-orange-500/10 text-orange-300',
    white: 'border-gray-500 bg-gray-700/40 text-gray-100',
    fuchsia: 'border-fuchsia-500/40 bg-fuchsia-500/10 text-fuchsia-300',
  }
  return <span className={`inline-block text-[11px] font-semibold px-2 py-0.5 rounded border ${cls[tone] || cls.white}`}>{value}</span>
}

function toneFor(key: string, value: string, read: OblivionRead): any {
  const v = (value || '').toUpperCase()
  switch (key) {
    case 'Market Bias': return v === 'BULLISH' ? 'lime' : v === 'BEARISH' ? 'red' : 'gray'
    case 'Trend': return v === 'BULL' ? 'lime' : v === 'BEAR' ? 'red' : 'gray'
    case 'HTF': return v === 'BULL' ? 'lime' : v === 'BEAR' ? 'red' : 'gray'
    case 'VWAP': return v === 'ABOVE' ? 'lime' : v === 'BELOW' ? 'red' : 'gray'
    case 'Zone': return v === 'DISCOUNT' ? 'lime' : v === 'PREMIUM' ? 'red' : v === 'EQUILIBRIUM' ? 'yellow' : 'gray'
    case 'OTE': return v.includes('BULL') ? 'lime' : v.includes('BEAR') ? 'red' : 'gray'
    case 'FVG': return v.startsWith('BULL') ? 'lime' : v.startsWith('BEAR') ? 'red' : 'gray'
    case 'Liquidity': return v.startsWith('BUY') ? 'aqua' : v.startsWith('SELL') ? 'orange' : 'gray'
    case 'Volume': return v === 'VERY HIGH' ? 'yellow' : v === 'HIGH' ? 'orange' : 'gray'
    case 'News': return v === 'BLOCKED' ? 'orange' : 'lime'
    case 'Signal': return v.startsWith('BUY') ? 'lime' : v.startsWith('SELL') ? 'red' : 'gray'
    case 'Structure': return v.includes('↑') ? 'lime' : v.includes('↓') ? 'red' : 'gray'
    default: return 'white'
  }
}

export default function OblivionDashboard({ read, symbol, timeframe }: { read: OblivionRead | null; symbol: string; timeframe: string }) {
  if (!read || !read.score) {
    return <div className="rounded-xl border border-gray-700/50 bg-gray-900/60 p-4 text-sm text-gray-500">The engine needs at least 60 closed candles on this timeframe — waiting for the chart to load.</div>
  }
  const sc: any = read.score
  const dash = read.dashboard || {}
  const sig: any = read.signal || { action: 'wait' }
  const plan = sig.entry != null ? sig : read.last_signal || null
  const action = (sig.action || 'wait').toUpperCase()
  const kind = sig.kind ? ` (${sig.kind})` : ''
  const mode = (read as any).mode || read.config?.trade_mode || 'HYBRID'
  const scalpMin = sc.scalp_min ?? sc.required ?? 6
  const sniperMin = sc.sniper_min ?? 9
  const bullComps = sc.components?.buy ?? sc.components?.bull ?? {}
  const bearComps = sc.components?.sell ?? sc.components?.bear ?? {}
  const grid: [string, string][] = [
    ['Market Bias', dash['Market Bias'] ?? read.bias_label ?? 'NEUTRAL'],
    ['Trend', dash['Trend'] ?? read.trend?.now ?? (read.trend?.bull ? 'BULL' : read.trend?.bear ? 'BEAR' : 'MIXED')],
    ['HTF', dash['HTF'] ?? read.htf?.now ?? 'N/A'],
    ['VWAP', dash['VWAP'] ?? read.vwap?.now ?? 'N/A'],
    ['Zone', dash['Zone'] ?? read.pd?.zone ?? (read.premium ? 'PREMIUM' : read.discount ? 'DISCOUNT' : 'EQUILIBRIUM')],
    ['OTE', dash['OTE'] ?? read.ote?.now ?? 'NONE'],
    ['Structure', dash['Structure'] ?? read.structure?.now ?? 'WAIT'],
    ['FVG', dash['FVG'] ?? read.fvg?.now ?? 'NONE'],
    ['Liquidity', dash['Liquidity'] ?? read.liquidity?.now ?? 'NONE'],
    ['Session', dash['Session'] ?? read.session?.name ?? 'OFF'],
    ['Volume', dash['Volume'] ?? read.volume?.level ?? 'N/A'],
    ['News', dash['News'] ?? (read.news?.blocked ? 'BLOCKED' : 'CLEAR')],
    ['Signal', dash['Signal'] ?? `${action}${kind}`],
    ['Mode', dash['Mode'] ?? mode],
  ]
  const entry = plan?.entry ?? null
  const stop = plan?.stop_loss ?? null
  const tp1 = plan?.tp1 ?? null
  const tp2 = plan?.tp2 ?? null
  const tp3 = plan?.tp3 ?? null
  const risk = entry != null && stop != null ? Math.abs(entry - stop) : null
  const planColor = plan?.action === 'sell' ? 'text-red-400' : 'text-lime-400'
  const outcomes = read.outcomes
  const winRate = outcomes?.win_rate != null ? `${Math.round(outcomes.win_rate*100)}%` : 'n/a'
  const rr1 = read.config?.tp1_rr ?? 0.5
  const rr2 = read.config?.tp2_rr ?? 1.0
  const rr3 = read.config?.tp3_rr ?? 1.5

  return (
    <div className="rounded-xl border border-gray-700/50 bg-gray-900/60 p-4" data-testid="oblivion-dashboard">
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <div>
          <div className="flex items-baseline gap-3 mb-3">
            <h3 className="text-xs font-bold tracking-wider text-gray-400">SIGNAL SCORE</h3>
            <span className="text-[11px] text-gray-600">{mode} · scalp ≥{scalpMin} · sniper ≥{sniperMin}</span>
          </div>
          <ScoreBar label="BULL" value={sc.buy ?? sc.bull ?? 0} scalpMin={scalpMin} sniperMin={sniperMin} color="lime" comps={bullComps} />
          <ScoreBar label="BEAR" value={sc.sell ?? sc.bear ?? 0} scalpMin={scalpMin} sniperMin={sniperMin} color="red" comps={bearComps} />
          <p className="text-[11px] text-gray-600">yellow = scalp min · fuchsia = sniper min · a side must clear its min AND beat the other</p>
        </div>
        <div className="lg:border-l lg:border-gray-800 lg:pl-6">
          <h3 className="text-xs font-bold tracking-wider text-gray-400 mb-3">MARKET READ</h3>
          <div className="grid grid-cols-2 gap-x-4 gap-y-2">
            {grid.map(([k, v]) => <div key={k} className="flex items-center justify-between gap-2"><span className="text-[11px] text-gray-500">{k}</span><Pill value={v} tone={toneFor(k, v, read)} /></div>)}
          </div>
          {(read.fib?.f382 != null || read.pd?.equilibrium != null) && (
            <p className="mt-3 font-mono text-[11px] text-gray-500">
              {read.fib?.f382 != null && <>fib .382 {fmt(read.fib.f382)} · .5 {fmt(read.fib.f500)} · .618 {fmt(read.fib.f618)}</>}
              {read.pd?.equilibrium != null && <> · EQ {fmt(read.pd.equilibrium)} ({(read.pd.zone || '').toLowerCase()})</>}
              {read.ote?.bull && <> · OTE {fmt(read.ote.bull['618'])}–{fmt(read.ote.bull['786'])}</>}
            </p>
          )}
          {outcomes && (
            <div className="mt-4 rounded-lg border border-slate-700/50 bg-slate-800/30 p-2.5">
              <h4 className="text-[11px] font-bold tracking-wider text-slate-400 mb-1.5">OUTCOMES <span className="font-normal text-slate-500">· last {outcomes.count} signals</span></h4>
              <div className="grid grid-cols-3 gap-2 text-[11px]">
                <div className="text-slate-400">Win rate <span className="text-slate-200 font-semibold">{winRate}</span></div>
                <div className="text-slate-400">Avg R <span className="text-slate-200 font-semibold">{outcomes.avg_r ?? '—'}</span></div>
                <div className="text-slate-400">Open <span className="text-slate-200">{outcomes.open}</span></div>
                <div className="text-lime-300">TP1 {outcomes.tp1} · TP2 {outcomes.tp2} · TP3 {outcomes.tp3}</div>
                <div className="text-red-300">SL {outcomes.sl}</div>
                <div className="text-slate-500">scalp {outcomes.scalp ?? '—'} · sniper {outcomes.sniper ?? '—'}</div>
              </div>
            </div>
          )}
        </div>
        <div className="lg:border-l lg:border-gray-800 lg:pl-6">
          {/* The plan is always for one instrument — name it, so a switch is
              visible on the panel itself rather than only in the chart header. */}
          <h3 className="text-xs font-bold tracking-wider text-gray-400 mb-3">
            TRADE PLAN <span className="font-normal text-gray-600">· {symbol} {timeframe}</span>
          </h3>
          {plan && entry != null ? (
            <>
              <div className={`font-bold mb-2 ${planColor}`}>{(plan.action || '').toUpperCase()} {plan.kind ? <span className="text-[11px] px-1.5 py-0.5 rounded border border-lime-500/30 bg-lime-500/10 text-lime-300 align-middle">{plan.kind}</span> : null}<span className="text-gray-400 font-normal text-sm">{' · '}{plan === sig ? (sig.confirmed ? 'confirmed' : 'forming bar') : `last signal ${(plan as any).bars_since ?? '?'} bars ago`}</span></div>
              <dl className="grid grid-cols-[80px_1fr] gap-y-1 font-mono text-sm">
                <dt className="text-gray-500 text-xs">Entry</dt><dd className="text-gray-100">{fmt(entry)}</dd>
                <dt className="text-gray-500 text-xs">Stop</dt><dd className="text-red-400">{fmt(stop)} <span className="text-[11px] text-gray-500">±0.25 ATR, clamp {read.config?.min_risk_atr ?? 0.25}–{read.config?.max_risk_atr ?? 2.5} ATR</span></dd>
                <dt className="text-gray-500 text-xs">TP1 {rr1}R</dt><dd className="text-green-400">{fmt(tp1)}</dd>
                <dt className="text-gray-500 text-xs">TP2 {rr2}R</dt><dd className="text-green-400">{fmt(tp2)}</dd>
                <dt className="text-gray-500 text-xs">TP3 {rr3}R</dt><dd className="text-green-400">{fmt(tp3)}</dd>
                <dt className="text-gray-500 text-xs">Risk</dt><dd className="text-gray-400">{risk != null ? `${fmt(risk)}  (${(risk / entry * 100).toFixed(2)}%)` : '—'}</dd>
              </dl>
              {sig.gate && <p className="mt-2 text-xs text-amber-300">Gate: {sig.gate}</p>}
              {sig.blocked_by_news && <p className="mt-2 text-xs text-orange-400">entry blocked by the news window</p>}
            </>
          ) : (
            <>
              <div className="font-bold text-gray-400 mb-2">WAIT — no active signal</div>
              <p className="text-xs text-gray-600">The engine needs a side to reach the minimum score and beat the other side on a closed bar.</p>
              {sig.gate && <p className="mt-2 text-xs text-amber-300">Gate: {sig.gate}</p>}
            </>
          )}
        </div>
      </div>
      {read.summary && <p className="mt-4 pt-3 border-t border-gray-800 text-xs text-gray-300" title={`${symbol} ${timeframe}`}>{read.summary}</p>}
    </div>
  )
}
