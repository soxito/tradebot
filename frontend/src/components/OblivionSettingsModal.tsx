import { useEffect, useMemo, useState } from 'react';
import { X, RotateCcw, Save } from 'lucide-react';
import { api } from '@/services/api';
import { Toggle, NumberField, SelectField, SessionWindowField } from '@/components/settings/Fields';

type Field = { group: string; key: string; label: string; type: string; min?: number; max?: number; step?: number; options?: string[]; hint?: string };

export default function OblivionSettingsModal({ open, onClose, onSaved }: { open: boolean; onClose: () => void; onSaved?: (version: number) => void }) {
  const [fields, setFields] = useState<Field[]>([]);
  const [settings, setSettings] = useState<Record<string, any>>({});
  const [defaults, setDefaults] = useState<Record<string, any>>({});
  const [version, setVersion] = useState(1);
  const [initial, setInitial] = useState<Record<string, any>>({});
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [confirmReset, setConfirmReset] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [savedAt, setSavedAt] = useState<string | null>(null);
  const [activeGroup, setActiveGroup] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setLoading(true); setErr(null);
    api.get('/oblivion/settings').then(r => {
      const d = r.data as any;
      setFields(d.fields || []); setSettings(d.settings || {}); setDefaults(d.defaults || {}); setVersion(d.version || 1);
      setInitial(d.settings || {}); setActiveGroup(d.fields?.[0]?.group || null);
    }).catch(e => setErr(e?.response?.data?.detail || e?.message || 'Failed to load settings')).finally(() => setLoading(false));
  }, [open]);

  // Escape closes the dialog, like every other overlay in the app.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  const groups = useMemo(() => {
    const m = new Map<string, Field[]>();
    for (const f of fields) { if (!m.has(f.group)) m.set(f.group, []); m.get(f.group)!.push(f); }
    return Array.from(m.entries());
  }, [fields]);

  const dirty = useMemo(() => JSON.stringify(settings) !== JSON.stringify(initial), [settings, initial]);
  const setKey = (k: string, v: any) => setSettings(s => ({ ...s, [k]: v }));

  const handleSave = async () => {
    setSaving(true); setErr(null);
    const patch: Record<string, any> = {};
    for (const k of Object.keys(settings)) if (JSON.stringify(settings[k]) !== JSON.stringify(initial[k])) patch[k] = settings[k];
    if (Object.keys(patch).length === 0) { setSaving(false); onClose(); return; }
    try {
      const r = await api.put('/oblivion/settings', { settings: patch });
      const d = r.data as any;
      setSettings(d.settings); setDefaults(d.defaults); setVersion(d.version); setInitial(d.settings);
      const now = new Date(); setSavedAt(`${String(now.getHours()).padStart(2,'0')}:${String(now.getMinutes()).padStart(2,'0')}`);
      window.dispatchEvent(new CustomEvent('oblivion-settings-changed', { detail: { version: d.version } }));
      onSaved?.(d.version);
    } catch (e: any) { setErr(e?.response?.data?.detail || e?.message || 'Save failed'); } finally { setSaving(false); }
  };

  const handleReset = async () => {
    // Two-step inline confirm rather than window.confirm(): a native dialog is
    // suppressed in embedded/preview browsers, which silently swallowed the click.
    if (!confirmReset) { setConfirmReset(true); setTimeout(() => setConfirmReset(false), 4000); return; }
    setConfirmReset(false);
    setSaving(true); setErr(null);
    try {
      const r = await api.post('/oblivion/settings/reset');
      const d = r.data as any;
      setSettings(d.settings); setInitial(d.settings); setVersion(d.version);
      window.dispatchEvent(new CustomEvent('oblivion-settings-changed', { detail: { version: d.version } }));
      onSaved?.(d.version); setSavedAt(new Date().toLocaleTimeString().slice(0,5));
    } catch (e: any) { setErr(e?.response?.data?.detail || e?.message || 'Reset failed'); } finally { setSaving(false); }
  };

  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={onClose}>
      <div className="flex max-h-[90vh] w-full max-w-5xl flex-col rounded-xl border border-slate-700 bg-slate-900 shadow-xl" onClick={e => e.stopPropagation()}>
        <div className="flex items-center justify-between border-b border-slate-700 px-4 py-3">
          <div><h2 className="text-sm font-bold text-slate-100">Oblivion settings</h2><p className="text-[11px] text-slate-500">Global — every consumer (agents, MT5, Kronos, Telegram, charts) uses these. v{version}</p></div>
          <div className="flex items-center gap-2">
            {savedAt && <span className="text-[11px] px-2 py-1 rounded bg-lime-500/15 text-lime-300 border border-lime-500/30">Saved {savedAt}</span>}
            {dirty && <span className="text-[11px] px-2 py-1 rounded bg-amber-500/15 text-amber-300 border border-amber-500/30">Unsaved changes</span>}
            <button onClick={onClose} className="p-1.5 rounded hover:bg-slate-800 text-slate-400"><X className="w-4 h-4" /></button>
          </div>
        </div>
        {loading ? <div className="p-8 text-sm text-slate-500">Loading…</div> : (
          <div className="flex flex-1 overflow-hidden">
            <div className="w-48 shrink-0 overflow-y-auto border-r border-slate-700 bg-slate-900/50 p-2 space-y-1">
              {groups.map(([g]) => (
                <button key={g} onClick={() => setActiveGroup(g)} className={`w-full text-left text-xs px-2 py-1.5 rounded ${activeGroup===g ? 'bg-lime-600 text-black font-semibold' : 'text-slate-400 hover:bg-slate-800 hover:text-slate-200'}`}>{g}</button>
              ))}
            </div>
            <div className="flex-1 overflow-y-auto p-4 space-y-6">
              {err && <div className="rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-300">{err}</div>}
              {groups.filter(([g]) => !activeGroup || g===activeGroup).map(([g, flds]) => (
                <div key={g}>
                  <h3 className="text-xs font-bold tracking-wider text-lime-400 mb-3">{g}</h3>
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                    {flds.map(f => {
                      const v = settings[f.key];
                      if (f.type === 'bool') return <Toggle key={f.key} label={f.label} hint={f.hint} checked={!!v} onChange={nv => setKey(f.key, nv)} />;
                      if (f.type === 'select') return <SelectField key={f.key} label={f.label} hint={f.hint} value={String(v ?? '')} onChange={nv => setKey(f.key, nv)} options={f.options || []} />;
                      if (f.type === 'session') return <SessionWindowField key={f.key} label={f.label} value={String(v ?? '0000-0000')} onChange={nv => setKey(f.key, nv)} />;
                      if (f.type === 'int' || f.type === 'float') return <NumberField key={f.key} label={f.label} hint={f.hint} value={Number(v ?? 0)} min={f.min ?? 0} max={f.max ?? 100} step={f.step ?? (f.type==='float'?0.01:1)} onChange={nv => setKey(f.key, nv)} />;
                      return <div key={f.key} className="text-xs text-slate-500">{f.label}: {String(v)}</div>;
                    })}
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}
        <div className="flex items-center justify-between border-t border-slate-700 px-4 py-3">
          <button onClick={handleReset} disabled={saving} className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-slate-700 text-xs text-slate-300 hover:bg-slate-800 disabled:opacity-40"><RotateCcw className="w-3.5 h-3.5" /> {confirmReset ? 'Click again to confirm' : 'Reset to defaults'}</button>
          <div className="flex gap-2">
            <button onClick={onClose} className="px-4 py-1.5 rounded-lg border border-slate-700 text-xs text-slate-300 hover:bg-slate-800">Cancel</button>
            <button onClick={handleSave} disabled={saving || !dirty} className="flex items-center gap-1.5 px-4 py-1.5 rounded-lg bg-lime-600 hover:bg-lime-500 disabled:opacity-40 text-black text-xs font-semibold"><Save className="w-3.5 h-3.5" /> Save</button>
          </div>
        </div>
      </div>
    </div>
  )
}
