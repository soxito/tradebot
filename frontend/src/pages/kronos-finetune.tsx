import Head from 'next/head';
import Link from 'next/link';
import { useEffect, useState, useRef } from 'react';
import {
  Sparkles, Loader2, Download, Zap, Check, X, RefreshCw,
  AlertTriangle, ChevronLeft, Layers, HardDrive, FlaskConical,
  ExternalLink, Cpu, Database, Play, Upload, Telescope,
} from 'lucide-react';
import { apiClient } from '@/services/api';

interface FinetuneJob {
  id: number;
  exchange: string;
  symbol: string;
  timeframe: string;
  model_name?: string | null;
  symbols_combined?: string[] | null;
  status: 'pending' | 'exporting' | 'running' | 'completed' | 'failed' | 'cancelled';
  rows_exported?: number | null;
  disk_size_mb?: number | null;
  error?: string | null;
  output_dir?: string | null;
  log_tail?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  created_at?: string | null;
}

const FT_STATUS_STYLE: Record<string, string> = {
  pending: 'bg-gray-500/15 text-gray-300',
  exporting: 'bg-blue-500/15 text-blue-300',
  running: 'bg-amber-500/15 text-amber-300',
  completed: 'bg-green-500/15 text-green-300',
  failed: 'bg-red-500/15 text-red-300',
  cancelled: 'bg-gray-500/15 text-gray-400',
};

const inputCls =
  'bg-gray-800 border border-gray-700 rounded-lg px-2 py-1.5 text-xs text-white focus:outline-none focus:border-fuchsia-500';

function fmtDuration(start?: string | null, end?: string | null): string {
  if (!start) return '—';
  const s = new Date(start).getTime();
  const e = end ? new Date(end).getTime() : Date.now();
  const secs = Math.max(0, Math.round((e - s) / 1000));
  if (secs < 60) return `${secs}s`;
  const m = Math.floor(secs / 60);
  const rem = secs % 60;
  if (m < 60) return `${m}m ${rem}s`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}
function fmtWhen(iso?: string | null): string {
  if (!iso) return '—';
  try { return new Date(iso).toLocaleString(); } catch { return iso; }
}

export default function KronosFinetunePage() {
  const [mounted, setMounted] = useState(false);
  useEffect(()=> setMounted(true), []);
  const proxyUrl = mounted ? apiClient.soup.uiProxyUrl() : 'http://127.0.0.1:1448/api/v1/plugins/soup/ui/proxy';
  const [tab, setTab] = useState<'kronos'|'soup'>('kronos');
  const [setup, setSetup] = useState<{ vendored: boolean; setup_command?: string } | null>(null);
  const [soupStatus, setSoupStatus] = useState<any>(null);

  // New-run form (Kronos)
  const [ftSymbol, setFtSymbol] = useState('BTC/USD');
  const [ftExchange, setFtExchange] = useState('bitget');
  const [ftTimeframe, setFtTimeframe] = useState('1h');
  const [modelName, setModelName] = useState('');
  const [limit, setLimit] = useState(5000);
  const [lookback, setLookback] = useState(512);
  const [predLen, setPredLen] = useState(120);
  const [tokEpochs, setTokEpochs] = useState(10);
  const [predEpochs, setPredEpochs] = useState(20);
  const [batchSize, setBatchSize] = useState(16);
  const [usePatternFeatures, setUsePatternFeatures] = useState(true);

  const [exporting, setExporting] = useState(false);
  const [exportResult, setExportResult] = useState<{ ok: boolean; msg: string } | null>(null);
  const [starting, setStarting] = useState(false);
  const [startErr, setStartErr] = useState<string | null>(null);

  const [jobs, setJobs] = useState<FinetuneJob[]>([]);
  const [selected, setSelected] = useState<FinetuneJob | null>(null);
  const [loadingId, setLoadingId] = useState<number | null>(null);
  const [combining, setCombining] = useState(false);
  const [combineName, setCombineName] = useState('');
  const [combineMsg, setCombineMsg] = useState<{ ok: boolean; msg: string } | null>(null);
  const [pruning, setPruning] = useState(false);

  // Soup inline tab state
  const [soupBase, setSoupBase] = useState('Qwen/Qwen2.5-0.5B-Instruct');
  const [soupTask, setSoupTask] = useState('sft');
  const [soupDataset, setSoupDataset] = useState('');
  const [soupUploadMsg, setSoupUploadMsg] = useState<string|null>(null);
  const [soupStarting, setSoupStarting] = useState(false);
  const [soupJobs, setSoupJobs] = useState<any[]>([]);
  const [soupSelected, setSoupSelected] = useState<any>(null);
  const [showSoupUi, setShowSoupUi] = useState(true);
  const fileRef = useRef<HTMLInputElement>(null);

  const refreshSetup = async () => {
    try { const r = await apiClient.kronos.finetune.setupStatus(); setSetup(r.data); }
    catch { setSetup(null); }
  };
  const refreshJobs = async () => {
    try { const r = await apiClient.kronos.finetune.list(); setJobs(r.data?.jobs ?? []); }
    catch { /* keep last */ }
  };
  const refreshSoupStatus = async()=>{ try{ const r=await apiClient.soup.status(); setSoupStatus(r.data);}catch{} };
  const refreshSoupJobs = async()=>{ try{ const r=await apiClient.soup.jobs(); setSoupJobs(r.data?.jobs??[]);}catch{} };

  useEffect(() => {
    refreshSetup(); refreshJobs(); refreshSoupStatus(); refreshSoupJobs();
    const id = setInterval(refreshJobs, 5000);
    const id2 = setInterval(refreshSoupJobs, 7000);
    const id3 = setInterval(refreshSoupStatus, 30000);
    return () => { clearInterval(id); clearInterval(id2); clearInterval(id3); };
  }, []);
  // Poll selected Kronos job
  useEffect(() => {
    if (!selected) return;
    const active = selected.status === 'running' || selected.status === 'exporting' || selected.status === 'pending';
    if (!active) return;
    const id = setInterval(async () => {
      try { const r = await apiClient.kronos.finetune.job(selected.id); setSelected(r.data); }
      catch { /* ignore */ }
    }, 3000);
    return () => clearInterval(id);
  }, [selected?.id, selected?.status]);
  // Poll selected Soup job
  useEffect(()=>{
    if(!soupSelected) return;
    if(soupSelected.status!=='running' && soupSelected.status!=='pending') return;
    const id=setInterval(async()=>{ try{ const r=await apiClient.soup.job(soupSelected.id,80); setSoupSelected(r.data);}catch{} },3000);
    return()=>clearInterval(id);
  },[soupSelected?.id, soupSelected?.status]);

  const completedCount = jobs.filter((j) => j.status === 'completed').length;

  const doExport = async () => {
    setExporting(true); setExportResult(null);
    try {
      const r = await apiClient.kronos.finetune.export({ exchange: ftExchange, symbol: ftSymbol, timeframe: ftTimeframe, limit, use_pattern_features: usePatternFeatures });
      const d = r.data;
      const pat = d?.enriched ? ` • pattern-enriched (${d.pattern_coverage?.candle_pattern_bars ?? 0} candle bars, ${d.pattern_coverage?.chart_pattern_bars ?? 0} chart bars)` : '';
      setExportResult(d?.ok ? { ok: true, msg: `Exported ${d.rows ?? '?'} rows → ${d.path ?? 'CSV'}${pat}` } : { ok: false, msg: d?.error || 'Export failed' });
    } catch (e: any) {
      setExportResult({ ok: false, msg: e?.response?.data?.detail || e?.message || 'Export failed' });
    } finally { setExporting(false); }
  };
  const doStart = async () => {
    setStarting(true); setStartErr(null);
    try {
      const r = await apiClient.kronos.finetune.start({
        exchange: ftExchange, symbol: ftSymbol, timeframe: ftTimeframe, limit,
        model_name: modelName.trim() || undefined,
        lookback, pred_len: predLen, tokenizer_epochs: tokEpochs,
        predictor_epochs: predEpochs, batch_size: batchSize,
        use_pattern_features: usePatternFeatures,
      });
      setSelected(r.data); refreshJobs();
    } catch (e: any) {
      setStartErr(e?.response?.data?.detail || e?.message || 'Could not start fine-tune');
    } finally { setStarting(false); }
  };
  const doLoad = async (id: number) => {
    setLoadingId(id);
    try { await apiClient.kronos.finetune.load(id); refreshJobs(); }
    catch (e: any) { setStartErr(e?.response?.data?.detail || e?.message || 'Load failed'); }
    finally { setLoadingId(null); }
  };
  const doCancel = async (id: number) => {
    try { await apiClient.kronos.finetune.cancel(id); refreshJobs(); } catch { /* ignore */ }
  };
  const doDelete = async (id: number, symbol: string) => {
    const label = symbol ? `${symbol} (#${id})` : `job #${id}`;
    if (!window.confirm(`Delete trained model ${label} and free its disk space?\n\nThis permanently removes its checkpoints from disk and its row from the DB.`)) return;
    try {
      await apiClient.kronos.finetune.delete(id);
      if (selected?.id === id) setSelected(null);
      refreshJobs();
    } catch (e: any) { setStartErr(e?.response?.data?.detail || e?.message || 'Delete failed'); }
  };
  const doCombine = async () => {
    setCombining(true); setCombineMsg(null);
    try {
      const r = await apiClient.kronos.finetune.combine(combineName.trim() || undefined);
      const d = r.data;
      setCombineMsg(d?.ok ? { ok: true, msg: `Combining ${d.symbols_combined?.length ?? 0} models (${d.rows_merged} rows) → training job #${d.job_id}. When it finishes, the new major model will auto-load and all source models will be auto-deleted to free space.` } : { ok: false, msg: d?.error || 'Combine failed' });
      refreshJobs();
    } catch (e: any) { setCombineMsg({ ok: false, msg: e?.response?.data?.detail || e?.message || 'Combine failed' }); }
    finally { setCombining(false); }
  };
  const doPrune = async () => {
    if (!window.confirm('Delete every non-combined fine-tune model and its files from disk? The combined model is kept.')) return;
    setPruning(true); setCombineMsg(null);
    try {
      const r = await apiClient.kronos.finetune.prune();
      const d = r.data;
      setCombineMsg({ ok: true, msg: `Pruned ${d?.removed_job_ids?.length ?? 0} non-combined model(s), freed ~${d?.freed_mb ?? 0} MB` });
      refreshJobs();
    } catch (e: any) { setCombineMsg({ ok: false, msg: e?.response?.data?.detail || e?.message || 'Prune failed' }); }
    finally { setPruning(false); }
  };
  // Soup inline helpers
  const doSoupUpload = async(file: File)=>{
    setSoupUploadMsg(null);
    try{ const r=await apiClient.soup.upload(file); setSoupDataset(r.data?.path||''); setSoupUploadMsg(`Uploaded ${r.data?.rows ?? '?'} rows → ${r.data?.path}`);}catch(e:any){ setSoupUploadMsg(e?.response?.data?.detail || e?.message); }
  };
  const doSoupStart = async()=>{
    setSoupStarting(true);
    try{
      const rec = soupStatus?.recommendation;
      const r=await apiClient.soup.start({
        base: soupBase, task: soupTask,
        dataset_path: soupDataset || undefined,
        quantization: rec?.quantization || '4bit',
        stream_layers: !!rec?.stream_layers,
        lora_r: rec?.lora_r || 64,
      } as any);
      setSoupSelected(r.data); refreshSoupJobs();
    }catch(e:any){ setSoupUploadMsg(e?.response?.data?.detail || e?.message); }
    finally{ setSoupStarting(false); }
  };

  const setupPending = setup !== null && !setup.vendored;

  return (
    <>
      <Head><title>Kronos Fine-tune Lab</title></Head>
      <div className="space-y-5">
        {/* Header + breadcrumb */}
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="p-2 rounded-xl bg-fuchsia-500/15"><Sparkles className="w-6 h-6 text-fuchsia-400" /></div>
            <div>
              <h1 className="text-xl font-bold text-white">Fine-tune Lab</h1>
              <p className="text-xs text-gray-500">Train custom Kronos models on your own live data, then combine them — plus Soup LLM finetuning (MakazhanAlpamys/Soup)</p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <Link href="/soup-finetune" className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-fuchsia-600 hover:bg-fuchsia-500 text-white text-xs font-medium"><FlaskConical className="w-3.5 h-3.5"/> Full Soup Lab <ExternalLink className="w-3 h-3"/></Link>
            <Link href="/kronos-forecast" className="flex items-center gap-1 text-xs text-gray-400 hover:text-white"><ChevronLeft className="w-4 h-4" /> Forecast</Link>
          </div>
        </div>

        {/* Tabs */}
        <div className="flex items-center gap-1 bg-gray-900/60 border border-gray-700/50 rounded-xl p-1 w-fit">
          <button onClick={()=>setTab('kronos')} className={`px-4 py-1.5 rounded-lg text-xs font-semibold flex items-center gap-1.5 ${tab==='kronos'?'bg-fuchsia-600 text-white':'text-gray-400 hover:text-white hover:bg-gray-800'}`}><Telescope className="w-3.5 h-3.5"/> Kronos (OHLCV)</button>
          <button onClick={()=>setTab('soup')} className={`px-4 py-1.5 rounded-lg text-xs font-semibold flex items-center gap-1.5 ${tab==='soup'?'bg-fuchsia-600 text-white':'text-gray-400 hover:text-white hover:bg-gray-800'}`}><FlaskConical className="w-3.5 h-3.5"/> Soup (LLM) <span className="text-[10px] bg-white/15 px-1 rounded">trysoup.dev</span></button>
        </div>

        {tab==='kronos' && (
          <>
            {setupPending && (
              <div className="flex items-start gap-2 bg-amber-500/10 border border-amber-500/30 rounded-lg px-3 py-2 text-xs text-amber-200">
                <AlertTriangle className="w-4 h-4 shrink-0 mt-0.5" />
                <div><p className="font-medium">Training pipeline not installed yet.</p><p className="mt-1">Run this once on the server, then refresh:</p><code className="mt-1 block bg-black/40 rounded px-2 py-1 text-amber-100 break-all">{setup?.setup_command || 'bash plugins/KronosForecastPlugin/scripts/setup_kronos_finetune.sh'}</code></div>
              </div>
            )}
            <div className="bg-gray-900/60 border border-gray-700/50 rounded-xl p-4 space-y-4">
              <div>
                <p className="text-[11px] font-semibold text-gray-400 uppercase tracking-wide mb-2">1 · New model</p>
                <div className="flex flex-wrap items-end gap-3">
                  <Field label="Model name"><input value={modelName} onChange={(e) => setModelName(e.target.value)} placeholder="e.g. super-base" className={`${inputCls} w-40`} /></Field>
                  <Field label="Symbol"><input value={ftSymbol} onChange={(e) => setFtSymbol(e.target.value)} className={`${inputCls} w-32`} /></Field>
                  <Field label="Exchange"><input value={ftExchange} onChange={(e) => setFtExchange(e.target.value)} className={`${inputCls} w-28`} /></Field>
                  <Field label="Timeframe"><input value={ftTimeframe} onChange={(e) => setFtTimeframe(e.target.value)} className={`${inputCls} w-20`} /></Field>
                  <Field label="Candles"><input type="number" min={256} value={limit} onChange={(e) => setLimit(Math.max(256, Number(e.target.value) || 0))} className={`${inputCls} w-24`} /></Field>
                  <button onClick={doExport} disabled={exporting} className="flex items-center gap-1.5 px-3 py-2 rounded-lg bg-gray-800 hover:bg-gray-700 text-gray-200 text-xs font-medium disabled:opacity-50">{exporting ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Download className="w-3.5 h-3.5" />} Export & preview</button>
                </div>
                {exportResult && <p className={`mt-2 text-[11px] ${exportResult.ok ? 'text-green-300' : 'text-red-300'}`}>{exportResult.msg}</p>}
              </div>
              <div>
                <p className="text-[11px] font-semibold text-gray-400 uppercase tracking-wide mb-2">2 · Training</p>
                <div className="flex flex-wrap items-end gap-3">
                  <Field label="Lookback"><input type="number" min={64} value={lookback} onChange={(e) => setLookback(Math.max(64, Number(e.target.value) || 0))} className={`${inputCls} w-20`} /></Field>
                  <Field label="Pred len"><input type="number" min={1} value={predLen} onChange={(e) => setPredLen(Math.max(1, Number(e.target.value) || 0))} className={`${inputCls} w-20`} /></Field>
                  <Field label="Tok epochs"><input type="number" min={1} value={tokEpochs} onChange={(e) => setTokEpochs(Math.max(1, Number(e.target.value) || 0))} className={`${inputCls} w-16`} /></Field>
                  <Field label="Pred epochs"><input type="number" min={1} value={predEpochs} onChange={(e) => setPredEpochs(Math.max(1, Number(e.target.value) || 0))} className={`${inputCls} w-16`} /></Field>
                  <Field label="Batch"><input type="number" min={1} value={batchSize} onChange={(e) => setBatchSize(Math.max(1, Number(e.target.value) || 0))} className={`${inputCls} w-16`} /></Field>
                  <label className="flex items-center gap-1.5 text-[11px] text-gray-300 cursor-pointer select-none"><input type="checkbox" checked={usePatternFeatures} onChange={(e) => setUsePatternFeatures(e.target.checked)} className="rounded border-gray-600 bg-gray-800 text-fuchsia-500" /> Pattern-aware</label>
                  <button onClick={doStart} disabled={starting || setupPending} className="flex items-center gap-1.5 px-4 py-2 rounded-lg bg-fuchsia-600 hover:bg-fuchsia-500 text-white text-xs font-semibold disabled:opacity-50">{starting ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Zap className="w-3.5 h-3.5" />} Start fine-tune</button>
                </div>
                <p className="mt-1 text-[10px] text-gray-500">When enabled, the export checks the previous 20 candles for the 50 candle + 12 chart patterns and writes per-bar pattern scores so the model learns pattern-conditioned moves.</p>
                {startErr && <p className="mt-2 text-[11px] text-red-300 flex items-center gap-1"><AlertTriangle className="w-3.5 h-3.5" /> {startErr}</p>}
              </div>
            </div>
            <div className="bg-gray-900/60 border border-gray-700/50 rounded-xl p-4">
              <div className="flex items-center justify-between mb-3"><p className="text-sm font-semibold text-white">All runs <span className="text-gray-500 font-normal">({jobs.length})</span></p><button onClick={refreshJobs} className="text-gray-500 hover:text-gray-300"><RefreshCw className="w-4 h-4" /></button></div>
              {jobs.length === 0 ? <p className="text-xs text-gray-600">No fine-tune runs yet.</p> : (
                <div className="overflow-x-auto">
                  <table className="w-full text-xs">
                    <thead><tr className="text-gray-500 uppercase tracking-wide text-[10px] border-b border-gray-800"><th className="text-left py-2 pr-3">#</th><th className="text-left py-2 pr-3">Name</th><th className="text-left py-2 pr-3">Symbol</th><th className="text-left py-2 pr-3">TF</th><th className="text-left py-2 pr-3">Status</th><th className="text-right py-2 pr-3">Rows</th><th className="text-right py-2 pr-3">Size</th><th className="text-left py-2 pr-3">Started</th><th className="text-right py-2 pr-3">Duration</th><th className="text-right py-2">Actions</th></tr></thead>
                    <tbody>
                      {jobs.map((j) => (
                        <tr key={j.id} onClick={() => setSelected(j)} className={`cursor-pointer border-b border-gray-800/60 ${selected?.id === j.id ? 'bg-fuchsia-500/[0.06]' : 'hover:bg-gray-800/40'}`}>
                          <td className="py-2 pr-3 text-gray-500">{j.id}</td>
                          <td className="py-2 pr-3 text-fuchsia-300 font-medium">{j.model_name || '—'}</td>
                          <td className="py-2 pr-3 text-white">{j.symbol}{j.symbols_combined && j.symbols_combined.length > 0 && <span title={`Trained & improved on: ${j.symbols_combined.join(', ')}`} className="ml-1.5 inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full bg-fuchsia-500/15 text-fuchsia-300 text-[10px] align-middle"><Layers className="w-2.5 h-2.5" />{j.symbols_combined.length} pairs</span>}</td>
                          <td className="py-2 pr-3 text-gray-400">{j.timeframe}</td>
                          <td className="py-2 pr-3"><span className={`px-1.5 py-0.5 rounded-full text-[10px] font-semibold uppercase ${FT_STATUS_STYLE[j.status] || ''}`}>{j.status}</span></td>
                          <td className="py-2 pr-3 text-right tabular-nums text-gray-300">{j.rows_exported ?? '—'}</td>
                          <td className="py-2 pr-3 text-right tabular-nums text-gray-300">{j.status === 'completed' && j.disk_size_mb ? `${j.disk_size_mb} MB` : '—'}</td>
                          <td className="py-2 pr-3 text-gray-500" suppressHydrationWarning>{fmtWhen(j.started_at || j.created_at)}</td>
                          <td className="py-2 pr-3 text-right tabular-nums text-gray-400" suppressHydrationWarning>{fmtDuration(j.started_at, j.finished_at)}</td>
                          <td className="py-2 text-right"><span className="inline-flex items-center gap-1.5">
                            {j.status === 'completed' && <button onClick={(e) => { e.stopPropagation(); doLoad(j.id); }} disabled={loadingId === j.id} title="Load this model for forecasts" className="p-1 rounded bg-green-600/80 hover:bg-green-500 text-white disabled:opacity-50">{loadingId === j.id ? <Loader2 className="w-3 h-3 animate-spin" /> : <Check className="w-3 h-3" />}</button>}
                            {(j.status === 'running' || j.status === 'exporting' || j.status === 'pending') && <button onClick={(e) => { e.stopPropagation(); doCancel(j.id); }} title="Cancel" className="p-1 rounded bg-red-600/70 hover:bg-red-500 text-white"><X className="w-3 h-3" /></button>}
                            <button onClick={(e) => { e.stopPropagation(); doDelete(j.id, j.symbol); }} disabled={j.status === 'running' || j.status === 'exporting'} title={j.status === 'running' || j.status === 'exporting' ? 'Cancel first' : `Delete ${j.model_name || j.symbol}`} className="p-1 rounded bg-gray-700 hover:bg-red-600 text-gray-300 hover:text-white disabled:opacity-30"><HardDrive className="w-3 h-3" /></button>
                          </span></td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
            <div className="bg-gray-900/60 border border-gray-700/50 rounded-xl p-4">
              <p className="text-[11px] font-semibold text-gray-400 uppercase tracking-wide mb-2">{selected ? `Log — run #${selected.id} (${selected.status})` : 'Log'}</p>
              {selected ? <><pre className="bg-black/50 border border-gray-800 rounded-lg p-2 text-[10px] leading-relaxed text-gray-300 h-52 overflow-auto whitespace-pre-wrap">{selected.log_tail || '(no log yet)'}</pre>{selected.error && <p className="mt-1 text-[11px] text-red-300">{selected.error}</p>}</> : <p className="text-xs text-gray-600">Select a run to watch its training log.</p>}
            </div>
            <div className="bg-gray-900/60 border border-gray-700/50 rounded-xl p-4">
              <div className="flex items-start gap-3"><div className="p-1.5 rounded-lg bg-blue-500/15"><Layers className="w-5 h-5 text-blue-400" /></div><div className="flex-1"><p className="text-sm font-semibold text-white">Combine into one model</p><p className="text-[11px] text-gray-500 mt-0.5">Merge all {completedCount} completed runs into one major model that fuses every brain's knowledge. Needs ≥2 completed runs.</p>
                <div className="flex flex-wrap items-end gap-3 mt-3">
                  <Field label="Combined name"><input value={combineName} onChange={(e) => setCombineName(e.target.value)} placeholder="e.g. combined" className={`${inputCls} w-40`} /></Field>
                  <button onClick={doCombine} disabled={combining || completedCount < 2 || setupPending} className="flex items-center gap-1.5 px-4 py-2 rounded-lg bg-blue-600 hover:bg-blue-500 text-white text-xs font-semibold disabled:opacity-50">{combining ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <HardDrive className="w-3.5 h-3.5" />} Combine {completedCount} models</button>
                  <button onClick={doPrune} disabled={pruning || completedCount < 1} className="flex items-center gap-1.5 px-4 py-2 rounded-lg bg-red-600/80 hover:bg-red-500 text-white text-xs font-semibold disabled:opacity-50">{pruning ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <X className="w-3.5 h-3.5" />} Delete non-combined</button>
                </div>{combineMsg && <p className={`mt-2 text-[11px] ${combineMsg.ok ? 'text-green-300' : 'text-red-300'}`}>{combineMsg.msg}</p>}
              </div></div>
            </div>
            {/* Quick Soup teaser */}
            <div className="bg-fuchsia-500/10 border border-fuchsia-500/20 rounded-xl p-3 flex items-center justify-between">
              <div className="text-xs"><p className="font-semibold text-fuchsia-300 flex items-center gap-1"><FlaskConical className="w-3.5 h-3.5"/> Want LLM finetuning?</p><p className="text-gray-400">Switch to the Soup tab here or open the full Soup Lab — any HF model, LoRA/QLoRA, layer-streaming on 4GB.</p>{soupStatus?.recommendation && <p className="text-[11px] text-gray-500 mt-1">{soupStatus.recommendation.reason} · {soupStatus?.installed ? 'installed ✓' : 'not installed — run setup_soup.sh'}</p>}</div>
              <button onClick={()=>setTab('soup')} className="px-3 py-1.5 rounded-lg bg-fuchsia-600 hover:bg-fuchsia-500 text-white text-xs font-medium">Open Soup tab</button>
            </div>
          </>
        )}

        {tab==='soup' && (
          <div className="space-y-4">
            {/* Soup status */}
            <div className="bg-gray-900/60 border border-gray-700/50 rounded-xl p-3 flex items-center justify-between">
              <div className="text-xs">
                <p className="font-semibold text-white flex items-center gap-1"><FlaskConical className="w-3.5 h-3.5 text-fuchsia-400"/> Soup LLM — <span className="text-gray-400 font-normal">trysoup.dev</span> <span className={`ml-1 px-1.5 py-0.5 rounded-full text-[10px] ${soupStatus?.installed?'bg-green-500/15 text-green-300':'bg-amber-500/15 text-amber-300'}`}>{soupStatus?.installed?'installed ✓':'setup required'}</span> {soupStatus?.update_available && <span className="ml-1 bg-blue-500/15 text-blue-300 px-1.5 py-0.5 rounded-full text-[10px]">update available</span>}</p>
                {soupStatus?.recommendation && <p className="text-[11px] text-gray-500 mt-0.5">{soupStatus.recommendation.reason} · {soupStatus?.version?.commit ? `${String(soupStatus.version.commit).slice(0,7)} on ${soupStatus.version.branch}`:''}</p>}
                {!soupStatus?.installed && <p className="text-[11px] text-amber-300 mt-1">Run <code className="bg-black/40 px-1 rounded">{soupStatus?.setup_command || 'bash plugins/SoupPlugin/scripts/setup_soup.sh'}</code> on the server · <span className="text-gray-500">Isolated venv ~/.tradebot/soup-venv</span></p>}
              </div>
              <div className="flex items-center gap-2">
                <button onClick={refreshSoupStatus} className="p-1.5 rounded bg-gray-800 text-gray-400 hover:text-white"><RefreshCw className="w-3.5 h-3.5"/></button>
                <Link href="/soup-finetune" className="px-3 py-1.5 rounded-lg bg-fuchsia-600 hover:bg-fuchsia-500 text-white text-xs font-medium inline-flex items-center gap-1">Full Soup Lab <ExternalLink className="w-3 h-3"/></Link>
              </div>
            </div>

            {/* Quick Soup train */}
            <div className="bg-gray-900/60 border border-gray-700/50 rounded-xl p-4 space-y-3">
              <p className="text-[11px] font-semibold text-gray-400 uppercase tracking-wide">Quick Soup train (for LLM datasets). Full controls in Soup Lab.</p>
              <div className="flex flex-wrap items-end gap-3">
                <Field label="Base HF id"><input value={soupBase} onChange={e=>setSoupBase(e.target.value)} placeholder="Qwen/Qwen2.5-0.5B-Instruct" className={`${inputCls} w-72`}/></Field>
                <Field label="Task"><select value={soupTask} onChange={e=>setSoupTask(e.target.value)} className={`${inputCls} w-28`}><option value="sft">sft</option><option value="dpo">dpo</option><option value="grpo">grpo</option></select></Field>
                <Field label="Dataset path"><input value={soupDataset} onChange={e=>setSoupDataset(e.target.value)} placeholder="path or leave empty + upload" className={`${inputCls} w-72`}/></Field>
                <input ref={fileRef} type="file" accept=".jsonl,.json,.csv" className="hidden" onChange={e=>{ const f=e.target.files?.[0]; if(f) doSoupUpload(f); }} />
                <button onClick={()=>fileRef.current?.click()} className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-gray-800 hover:bg-gray-700 text-gray-200 text-xs"><Upload className="w-3.5 h-3.5"/> Upload</button>
                <button onClick={doSoupStart} disabled={soupStarting || !soupBase} className="flex items-center gap-1.5 px-4 py-1.5 rounded-lg bg-fuchsia-600 hover:bg-fuchsia-500 text-white text-xs font-semibold disabled:opacity-50">{soupStarting? <Loader2 className="w-3.5 h-3.5 animate-spin"/>: <Play className="w-3.5 h-3.5"/>} Start Soup</button>
              </div>
              {soupUploadMsg && <p className="text-[11px] text-emerald-300">{soupUploadMsg}</p>}
              {soupStatus?.recommendation && <p className="text-[10px] text-gray-500 flex items-center gap-1"><Cpu className="w-3 h-3"/> {soupStatus.recommendation.reason} → quant {soupStatus.recommendation.quantization||'bf16'} · stream {String(soupStatus.recommendation.stream_layers)} — auto-applied.</p>}
            </div>

            {/* Soup jobs inline */}
            <div className="bg-gray-900/60 border border-gray-700/50 rounded-xl p-4">
              <div className="flex items-center justify-between mb-2"><p className="text-sm font-semibold text-white">Soup jobs <span className="text-gray-500 font-normal">({soupJobs.length})</span></p><button onClick={refreshSoupJobs} className="text-gray-500 hover:text-gray-300"><RefreshCw className="w-4 h-4"/></button></div>
              {soupJobs.length===0? <p className="text-xs text-gray-600">No Soup jobs yet — start one above or in the full lab.</p> : (
                <div className="overflow-x-auto"><table className="w-full text-xs"><thead><tr className="text-gray-500 uppercase text-[10px] border-b border-gray-800"><th className="text-left py-2 pr-3">#</th><th className="text-left py-2 pr-3">Base</th><th className="text-left py-2 pr-3">Task</th><th className="text-left py-2 pr-3">Status</th><th className="text-right py-2 pr-3">Dur</th></tr></thead><tbody>{soupJobs.slice(0,8).map((j:any)=><tr key={j.id} onClick={()=>setSoupSelected(j)} className={`cursor-pointer border-b border-gray-800/60 ${soupSelected?.id===j.id?'bg-fuchsia-500/[0.06]':''}`}><td className="py-1.5 pr-3 text-gray-500">{j.id}</td><td className="py-1.5 pr-3 text-fuchsia-300 truncate max-w-[14rem]">{j.base_model?.split('/').pop()}</td><td className="py-1.5 pr-3 text-gray-400">{j.task}</td><td className="py-1.5 pr-3"><span className={`px-1 py-0.5 rounded-full text-[10px] ${FT_STATUS_STYLE[j.status]||''}`}>{j.status}</span></td><td className="py-1.5 pr-3 text-right text-gray-500" suppressHydrationWarning>{fmtDuration(j.started_at,j.finished_at)}</td></tr>)}</tbody></table></div>
              )}
              {soupSelected && <pre className="mt-2 bg-black/50 border border-gray-800 rounded-lg p-2 text-[10px] text-gray-300 h-32 overflow-auto whitespace-pre-wrap">{soupSelected.log_tail || '(no log yet)'}</pre>}
            </div>

            {/* Embedded Soup UI proxy */}
            <div className="bg-gray-900/60 border border-gray-700/50 rounded-xl p-3">
              <div className="flex items-center justify-between mb-2">
                <p className="text-[11px] font-semibold text-gray-400 uppercase tracking-wide flex items-center gap-1"><Database className="w-3.5 h-3.5"/> Soup UI (proxied — token never leaks)</p>
                <div className="flex items-center gap-1">
                  <button onClick={()=>setShowSoupUi(v=>!v)} className="text-xs px-2 py-1 rounded bg-gray-800 hover:bg-gray-700 text-gray-300">{showSoupUi?'Hide':'Show'}</button>
                  <Link href="/soup-finetune" className="text-xs px-2 py-1 rounded bg-fuchsia-600 text-white">Full Lab</Link>
                </div>
              </div>
              {showSoupUi && <div className="border border-gray-800 rounded-lg overflow-hidden bg-white" style={{height: 420}}><iframe src={proxyUrl} title="Soup UI" className="w-full h-full border-0" sandbox="allow-same-origin allow-scripts allow-forms allow-popups" suppressHydrationWarning/></div>}
              <p className="mt-1 text-[10px] text-gray-600">Via <code className="bg-black/40 px-1 rounded">/api/v1/plugins/soup/ui/proxy</code> — server injects <code className="bg-black/40 px-1 rounded">SOUP_UI_TOKEN</code>. Direct UI also at <code className="bg-black/40 px-1 rounded">http://localhost:7860</code> when running.</p>
            </div>

            {/* Experimental OHLCV bridge teaser */}
            <div className="bg-blue-500/10 border border-blue-500/20 rounded-xl p-3">
              <p className="text-xs font-semibold text-blue-300 flex items-center gap-1"><Database className="w-3.5 h-3.5"/> Experimental: OHLCV → LLM (Soup)</p>
              <p className="text-[11px] text-gray-400 mt-1">Converts your Kronos candles (MT5→Yahoo→ccxt cascade) into ShareGPT JSONL windows and trains a small causal LM via Soup — results shown side-by-side with Kronos as a dashed forecast band (not a replacement). Full controls in <Link href="/soup-finetune" className="underline text-fuchsia-300">Soup Lab · OHLCV bridge</Link>. When you generate an OHLCV JSONL there, come back to <Link href="/kronos-forecast" className="underline">Kronos Forecast</Link> to compare.</p>
            </div>
          </div>
        )}
      </div>
    </>
  );
}
function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (<div><label className="block text-[10px] text-gray-500 mb-1 uppercase tracking-wide">{label}</label>{children}</div>);
}
