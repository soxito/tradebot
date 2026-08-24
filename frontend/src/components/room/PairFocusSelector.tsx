/**
 * PairFocusSelector — pin the room to one or more pairs, or release it back to
 * free roaming (signals from Telegram / Kronos and the auto-rotation).
 */
import { useMemo, useState } from 'react'
import { Search, X } from 'lucide-react'
import { POPULAR_PAIRS, searchPairs } from '@/constants/tradingPairs'

interface Props {
  focusSymbols: string[]
  onToggle: (symbol: string) => void
  onClear: () => void
}

export default function PairFocusSelector({ focusSymbols, onToggle, onClear }: Props) {
  const [query, setQuery] = useState('')
  const results = useMemo(() => (query.trim() ? searchPairs(query, 24) : []), [query])
  const isPinned = (symbol: string) => focusSymbols.some((s) => s.toUpperCase() === symbol.toUpperCase())

  return (
    <div className="space-y-2">
      <div className="relative">
        <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-500" />
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search pairs to focus…"
          className="w-full rounded-lg border border-slate-700 bg-slate-900/70 py-2 pl-9 pr-3 text-sm text-slate-100 placeholder:text-slate-500 focus:border-cyan-500 focus:outline-none"
        />
      </div>

      {focusSymbols.length > 0 ? (
        <div className="rounded-lg border border-cyan-500/50 bg-cyan-500/10 px-3 py-2">
          <div className="flex items-center gap-2">
            <span className="text-[11px] text-cyan-300">
              Room focused on {focusSymbols.length} pair{focusSymbols.length === 1 ? '' : 's'}
            </span>
            <button
              type="button"
              onClick={onClear}
              className="ml-auto rounded px-1.5 py-0.5 text-[10px] text-cyan-300 hover:bg-cyan-500/20"
              title="Release focus — agents follow incoming signals"
            >
              Clear all
            </button>
          </div>
          <div className="mt-1.5 flex flex-wrap gap-1.5">
            {focusSymbols.map((symbol) => (
              <button
                key={symbol}
                type="button"
                onClick={() => onToggle(symbol)}
                className="flex items-center gap-1 rounded-md border border-cyan-400/60 bg-cyan-500/20 px-2 py-1 font-mono text-[11px] text-cyan-200 hover:border-cyan-300"
                title="Remove from focus"
              >
                {symbol}
                <X className="h-3 w-3" />
              </button>
            ))}
          </div>
        </div>
      ) : (
        <p className="px-1 text-[11px] text-slate-500">
          Free roaming — agents work incoming signals and rotate pairs on their own.
          Tap pairs to pin several; the room rotates through them on the focus cadence.
        </p>
      )}

      <div className="flex flex-wrap gap-1.5">
        {(results.length ? results : POPULAR_PAIRS).map((symbol) => (
          <button
            key={symbol}
            type="button"
            onClick={() => { onToggle(symbol); setQuery('') }}
            className={`rounded-md border px-2 py-1 font-mono text-[11px] transition ${
              isPinned(symbol)
                ? 'border-cyan-400 bg-cyan-500/20 text-cyan-200'
                : 'border-slate-700 bg-slate-900/60 text-slate-300 hover:border-slate-500'
            }`}
          >
            {symbol}
          </button>
        ))}
      </div>
    </div>
  )
}
