/**
 * Tradeable instrument catalogue for the MT5 workspace.
 *
 * The broker's own symbol list (`/plugins/mt5/scalp/symbols/search`) is the
 * source of truth when the MT5 bridge is reachable, but it returns an empty
 * list whenever the account is offline / the bridge is down — which left the
 * pair pickers with nothing to select and the chart stuck on its default
 * symbol.  This catalogue is the offline fallback so a trader can always pick
 * any FX pair (majors, crosses, exotics), metal, index, energy or crypto.
 */

export interface PairGroup {
  /** Group label shown as a sticky heading in the picker dropdown. */
  label: string
  /** Symbols in broker (no-slash, uppercase) notation. */
  symbols: string[]
}

export const PAIR_GROUPS: PairGroup[] = [
  {
    label: 'Metals',
    symbols: ['XAUUSD', 'XAGUSD', 'XPTUSD', 'XPDUSD', 'XAUEUR', 'XAUGBP', 'XAUAUD', 'XAGEUR'],
  },
  {
    label: 'FX Majors',
    symbols: ['EURUSD', 'GBPUSD', 'USDJPY', 'USDCHF', 'AUDUSD', 'USDCAD', 'NZDUSD'],
  },
  {
    label: 'FX Crosses — EUR',
    symbols: ['EURGBP', 'EURJPY', 'EURCHF', 'EURAUD', 'EURCAD', 'EURNZD'],
  },
  {
    label: 'FX Crosses — GBP',
    symbols: ['GBPJPY', 'GBPCHF', 'GBPAUD', 'GBPCAD', 'GBPNZD'],
  },
  {
    label: 'FX Crosses — AUD / NZD / CAD / CHF',
    symbols: [
      'AUDJPY', 'AUDCHF', 'AUDCAD', 'AUDNZD',
      'NZDJPY', 'NZDCHF', 'NZDCAD',
      'CADJPY', 'CADCHF', 'CHFJPY',
    ],
  },
  {
    label: 'FX Exotics',
    symbols: [
      'USDZAR', 'USDMXN', 'USDTRY', 'USDSEK', 'USDNOK', 'USDDKK', 'USDPLN',
      'USDHUF', 'USDCZK', 'USDSGD', 'USDHKD', 'USDCNH', 'USDTHB', 'USDILS',
      'EURZAR', 'EURTRY', 'EURSEK', 'EURNOK', 'EURPLN', 'EURHUF', 'EURCZK',
      'GBPZAR', 'GBPTRY', 'GBPSEK', 'GBPNOK', 'GBPSGD',
      'AUDSGD', 'CHFSGD', 'NZDSGD', 'ZARJPY', 'SGDJPY', 'TRYJPY', 'MXNJPY',
      'NOKSEK', 'NOKJPY', 'SEKJPY',
    ],
  },
  {
    label: 'Indices',
    symbols: [
      'US30', 'US500', 'NAS100', 'US2000', 'GER40', 'UK100', 'FRA40',
      'EU50', 'ESP35', 'ITA40', 'SUI20', 'AUS200', 'JPN225', 'HK50', 'CHINA50',
    ],
  },
  {
    label: 'Energy & Softs',
    symbols: ['USOIL', 'UKOIL', 'NGAS', 'COCOA', 'COFFEE', 'SUGAR', 'COTTON', 'WHEAT'],
  },
  {
    label: 'Crypto',
    symbols: [
      'BTCUSD', 'ETHUSD', 'SOLUSD', 'XRPUSD', 'BNBUSD', 'ADAUSD',
      'DOGEUSD', 'AVAXUSD', 'LTCUSD', 'LINKUSD', 'DOTUSD', 'TRXUSD',
      'BCHUSD', 'XLMUSD',
    ],
  },
]

/** Flat, de-duplicated list of every catalogue symbol. */
export const ALL_PAIRS: string[] = Array.from(
  new Set(PAIR_GROUPS.flatMap(g => g.symbols))
)

/** Symbols shown as one-click chips above the picker list. */
export const POPULAR_PAIRS = [
  'XAUUSD', 'EURUSD', 'GBPUSD', 'USDJPY', 'GBPJPY', 'AUDUSD', 'USDCAD',
  'NAS100', 'US30', 'BTCUSD',
]

/** Normalise user / broker input to the catalogue's notation (EUR/USD → EURUSD). */
export function normalizePair(sym: string): string {
  return (sym || '').trim().toUpperCase().replace(/[\s/_-]/g, '')
}

/**
 * Broker symbols often carry a suffix (EURUSD.m, EURUSDpro, XAUUSD_i).
 * Strip it so a broker result matches a catalogue entry for grouping/dedupe.
 */
export function stripBrokerSuffix(sym: string): string {
  const s = normalizePair(sym)
  const m = s.match(/^([A-Z0-9]{6,8})(?:\.|M$|MICRO$|PRO$|ECN$|C$|I$|R$)/)
  return m ? m[1] : s
}

/**
 * Filter the catalogue by a free-text query.
 * Ranks exact match → prefix match → contains, so typing "EUR" lists
 * EURUSD / EURGBP … before ZARJPY-style incidental matches.
 */
export function searchPairs(query: string, limit = 60): string[] {
  const q = normalizePair(query)
  if (!q) return ALL_PAIRS.slice(0, limit)
  return ALL_PAIRS
    .filter(s => s.includes(q))
    .sort((a, b) => {
      const rank = (s: string) => (s === q ? 0 : s.startsWith(q) ? 1 : 2)
      return rank(a) - rank(b) || a.localeCompare(b)
    })
    .slice(0, limit)
}

/** Group a flat symbol list back into catalogue sections (unknown → "Other"). */
export function groupPairs(symbols: string[]): PairGroup[] {
  const remaining = new Set(symbols)
  const out: PairGroup[] = []
  for (const g of PAIR_GROUPS) {
    const hit = g.symbols.filter(s => remaining.has(s))
    hit.forEach(s => remaining.delete(s))
    if (hit.length) out.push({ label: g.label, symbols: hit })
  }
  // Preserve the caller's ordering for anything not in the catalogue
  // (i.e. broker-specific symbols such as EURUSD.m or US30.cash).
  const other = symbols.filter(s => remaining.has(s))
  if (other.length) out.push({ label: 'Broker symbols', symbols: other })
  return out
}
