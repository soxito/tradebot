import React from 'react';

export function Toggle({ label, hint, checked, onChange }: { label: string; hint?: string; checked: boolean; onChange: (v: boolean) => void }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      onClick={() => onChange(!checked)}
      className={`flex items-start gap-3 rounded-lg border px-3 py-2.5 text-left transition ${checked ? 'border-lime-500/40 bg-lime-500/10' : 'border-slate-700 bg-slate-900/50 hover:border-slate-600'}`}
    >
      <span className={`mt-0.5 flex h-5 w-9 shrink-0 items-center rounded-full p-0.5 transition ${checked ? 'bg-lime-500' : 'bg-slate-600'}`}><span className={`block h-4 w-4 rounded-full bg-white transition ${checked ? 'translate-x-4' : ''}`} /></span>
      <span className="min-w-0"><span className="block text-sm font-medium text-slate-100">{label}</span>{hint && <span className="mt-0.5 block text-[11px] leading-snug text-slate-400">{hint}</span>}</span>
    </button>
  )
}

export function NumberField({ label, hint, value, onChange, min, max, step = 1, suffix }: { label: string; hint?: string; value: number; onChange: (v: number) => void; min: number; max: number; step?: number; suffix?: string }) {
  return (
    <label className="block">
      <span className="text-[11px] font-medium uppercase tracking-wide text-slate-400">{label}</span>
      <span className="mt-1 flex items-center gap-2">
        <input type="number" value={value} min={min} max={max} step={step} onChange={(e) => onChange(Number(e.target.value))} className="w-full rounded-lg border border-slate-700 bg-slate-900/70 px-3 py-2 text-sm text-slate-100 focus:border-lime-500 focus:outline-none" />
        {suffix && <span className="text-xs text-slate-500">{suffix}</span>}
      </span>
      {hint && <span className="mt-1 block text-[11px] text-slate-500">{hint}</span>}
    </label>
  )
}

export function SelectField({ label, hint, value, onChange, options }: { label: string; hint?: string; value: string; onChange: (v: string) => void; options: string[] }) {
  return (
    <label className="block">
      <span className="text-[11px] font-medium uppercase tracking-wide text-slate-400">{label}</span>
      <select value={value} onChange={(e) => onChange(e.target.value)} className="mt-1 w-full rounded-lg border border-slate-700 bg-slate-900/70 px-3 py-2 text-sm text-slate-100 focus:border-lime-500 focus:outline-none">
        {options.map(o => <option key={o} value={o}>{o}</option>)}
      </select>
      {hint && <span className="mt-1 block text-[11px] text-slate-500">{hint}</span>}
    </label>
  )
}

export function SessionWindowField({ label, value, onChange }: { label: string; value: string; onChange: (v: string) => void }) {
  const a = value?.slice(0,4) || '0000'
  const b = value?.slice(5,9) || '0000'
  const toTime = (s: string) => `${s.slice(0,2)}:${s.slice(2,4)}`
  const fromTimeVal = (t: string) => t.replace(':','')
  const start = toTime(a)
  const end = toTime(b)
  return (
    <label className="block">
      <span className="text-[11px] font-medium uppercase tracking-wide text-slate-400">{label}</span>
      <span className="mt-1 flex items-center gap-2">
        <input type="time" value={start} onChange={(e) => onChange(`${fromTimeVal(e.target.value)}-${b}`)} className="flex-1 rounded-lg border border-slate-700 bg-slate-900/70 px-2 py-2 text-sm text-slate-100 focus:border-lime-500 focus:outline-none" />
        <span className="text-slate-500">—</span>
        <input type="time" value={end} onChange={(e) => onChange(`${a}-${fromTimeVal(e.target.value)}`)} className="flex-1 rounded-lg border border-slate-700 bg-slate-900/70 px-2 py-2 text-sm text-slate-100 focus:border-lime-500 focus:outline-none" />
      </span>
      <span className="mt-1 block text-[11px] text-slate-500 font-mono">{value}</span>
    </label>
  )
}
