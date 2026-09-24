import Head from 'next/head';
import Link from 'next/link';
import { useEffect, useState, useRef } from 'react';
import {
  FlaskConical, Loader2, Upload, Play, Check, X, RefreshCw,
  AlertTriangle, ChevronLeft, Layers, HardDrive, Cpu, Database,
  ExternalLink, Sparkles, Telescope, Zap, Download, ShieldCheck, ShieldAlert, ShieldX, Info, Brain, BarChart3, Clock, Package, StopCircle,
} from 'lucide-react';
import { apiClient, getApiBaseUrl } from '@/services/api';

interface SoupJob {
  id: number;
  base_model: string;
  task: string;
  status: 'pending'|'running'|'completed'|'failed'|'cancelled';
  quantization?: string | null;
  stream_layers?: boolean;
  output_dir?: string | null;
  log_tail?: string | null;
  error?: string | null;
  disk_size_mb?: number;
  created_at?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  ohlcv_symbol?: string | null;
  epochs?: number;
}
interface SoupStatus {
  installed: boolean;
  soup_bin_exists: boolean;
  soup_bin?: string | null;
  repo_cloned: boolean;
  version: any;
  recommendation: any;
  remote_commit?: string | null;
  update_available?: boolean;
  setup_command?: string;
  models?: any[];
  kronos_models?: any[];
  kronos_finetuned?: any[];
  ui_url?: string;
  api_url?: string;
}

const STATUS_STYLE: Record<string,string> = {
  pending: 'bg-gray-500/15 text-gray-300',
  running: 'bg-amber-500/15 text-amber-300',
  completed: 'bg-green-500/15 text-green-300',
  failed: 'bg-red-500/15 text-red-300',
  cancelled: 'bg-gray-500/15 text-gray-400',
};
const inputCls = 'bg-gray-800 border border-gray-700 rounded-lg px-2 py-1.5 text-xs text-white focus:outline-none focus:border-fuchsia-500';
const labelCls = 'block text-[10px] text-gray-500 mb-1 uppercase tracking-wide';
function fmtWhen(iso?: string|null){ if(!iso) return '—'; try{return new Date(iso).toLocaleString();}catch{return iso;} }
function fmtDuration(s?:string|null,e?:string|null){
  if(!s) return '—';
  const a=new Date(s).getTime(); const b=e?new Date(e).getTime():Date.now();
  const secs=Math.max(0,Math.round((b-a)/1000));
  if(secs<60) return `${secs}s`; const m=Math.floor(secs/60); const rem=secs%60; if(m<60) return `${m}m ${rem}s`; return `${Math.floor(m/60)}h ${m%60}m`;
}
function Field({label, children}:{label:string, children:any}){
  return <div suppressHydrationWarning><label className={labelCls}>{label}</label>{children}</div>;
}
function dashboardCompatExtra(c:any){
  const v=c?.hardware?.vram_gb;
  if(v!=null) return ` · VRAM ${v} GB`;
  if(c?.hardware?.is_apple_silicon) return ' · unified';
  return '';
}

export default function SoupFinetunePage(){
  const [mounted, setMounted] = useState(false); useEffect(()=>setMounted(true),[]); const proxyUrl = mounted ? apiClient.soup.uiProxyUrl() : 'http://127.0.0.1:1448/api/v1/plugins/soup/ui/proxy';
  const [status, setStatus] = useState<SoupStatus|null>(null);
  const [recipes, setRecipes] = useState<string[]>([]);
  const [kronosOverview, setKronosOverview] = useState<any>(null);

  // form
  const [base, setBase] = useState('Qwen/Qwen2.5-0.5B-Instruct');
  const baseInitRef = useRef(false);
  const [task, setTask] = useState('sft');
  const [template, setTemplate] = useState('');
  const [epochs, setEpochs] = useState(3);
  const [lr, setLr] = useState(2e-5);
  const [batch, setBatch] = useState('auto');
  const [loraR, setLoraR] = useState(64);
  const [loraAlpha, setLoraAlpha] = useState(16);
  const [quant, setQuant] = useState('4bit');
  const [streamLayers, setStreamLayers] = useState(false);
  const [datasetPath, setDatasetPath] = useState('');
  const [ohlcvSymbols, setOhlcvSymbols] = useState<string[]>(['BTC/USD']);
  const [ohlcvInput, setOhlcvInput] = useState('');
  const [ohlcvTf, setOhlcvTf] = useState('1h');
  const [ohlcvLimit, setOhlcvLimit] = useState(2000);
  const [uploading, setUploading] = useState(false);
  const [uploadMsg, setUploadMsg] = useState<string|null>(null);
  const [starting, setStarting] = useState(false);
  const [startErr, setStartErr] = useState<string|null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const [jobs, setJobs] = useState<SoupJob[]>([]);
  const [selected, setSelected] = useState<SoupJob|null>(null);
  const [uiHealthy, setUiHealthy] = useState<any>(null);
  const [showUi, setShowUi] = useState(true);

  // enhanced state
  const [installedModels, setInstalledModels] = useState<any[]>([]);
  const [catalogue, setCatalogue] = useState<any>(null);
  const [baseMode, setBaseMode] = useState<'select'|'custom'>('select');
  const [dlModel, setDlModel] = useState('');
  const [dlBusy, setDlBusy] = useState(false);
  const [dlMsg, setDlMsg] = useState<string|null>(null);
  const [dlCompat, setDlCompat] = useState<any>(null);
  const [dlProgress, setDlProgress] = useState<number|null>(null);
  const [dlCompatLoading, setDlCompatLoading] = useState(false);
  const dlPollRef = useRef<ReturnType<typeof setInterval>|null>(null);
  const [bestInfo, setBestInfo] = useState<any>(null);
  const [tradebotLocalState, setTradebotLocalState] = useState<any>(null);
  const [tradebotBusy, setTradebotBusy] = useState(false);
  // pair catalog for OHLCV multi-select — searchable, fetched from /api/v1/jarvis/pairs
  const [availablePairs, setAvailablePairs] = useState<any[]>([]);
  const [pairSearch, setPairSearch] = useState('');
  const [pairsLoading, setPairsLoading] = useState(false);

  function normalizeSymbolInput(s:string){
    let t=s.trim().toUpperCase();
    if(!t) return '';
    if(t.includes('/')) return t;
    if(t.endsWith('USDT')) return t.slice(0,-4)+'/USDT';
    if(t.endsWith('USD')) return t.slice(0,-3)+'/USD';
    if(t.endsWith('BTC')) return t.slice(0,-3)+'/BTC';
    return t;
  }
  function addOhlcvSymbols(raw?:string){
    const src = raw ?? ohlcvInput;
    if(!src || !src.trim()) return;
    const parts = src.split(/[\s,]+/).map(p=>normalizeSymbolInput(p)).filter(Boolean);
    if(parts.length===0) return;
    setOhlcvSymbols(prev=>{
      const seen=new Set(prev);
      const next=[...prev];
      for(const p of parts){ if(!seen.has(p)){ seen.add(p); next.push(p); } }
      return next;
    });
    setOhlcvInput('');
  }
  function removeOhlcvSymbol(s:string){
    setOhlcvSymbols(prev=>prev.filter(x=>x!==s));
  }

  const refreshStatus = async()=>{
    try{
      const r=await apiClient.soup.status(); setStatus(r.data);
      if(r.data?.recommendation){
        const rec=r.data.recommendation;
        if(!baseInitRef.current && rec.base_model){
          setBase(rec.base_model);
          baseInitRef.current=true;
        }
        if(!quant && rec.quantization) setQuant(rec.quantization);
        if(rec.stream_layers!=null) setStreamLayers(!!rec.stream_layers);
        if(rec.batch_size) setBatch(String(rec.batch_size));
        if(rec.lora_r) setLoraR(rec.lora_r);
      }
    }catch{}
  };
  const refreshRecipes = async()=>{ try{ const r=await apiClient.soup.recipes(); setRecipes(r.data?.recipes||[]);}catch{} };
  const refreshJobs = async()=>{ try{ const r=await apiClient.soup.jobs(); setJobs(r.data?.jobs||[]);}catch{} };
  const refreshKronos = async()=>{ try{ const r=await apiClient.soup.kronosOverview(); setKronosOverview(r.data);}catch{} };
  const refreshUi = async()=>{ try{ const r=await apiClient.soup.uiStatus(); setUiHealthy(r.data);}catch{} };
  const refreshModels = async()=>{ try{ const r=await apiClient.soup.models(); setInstalledModels(r.data?.installed||r.data?.models||[]); }catch{} };
  const refreshCatalogue = async()=>{
    try{
      const r=await apiClient.soup.modelsCatalogue();
      setCatalogue(r.data);
      // If installedModels empty, seed from catalogue.installed (single round-trip fallback so dropdown never empty)
      if(r.data?.installed && Array.isArray(r.data.installed) && r.data.installed.length>0){
        setInstalledModels(prev=> prev && prev.length>0 ? prev : r.data.installed);
      }
    }catch{}
  };
  const refreshCompat = async(model?:string)=>{
    const m=(model ?? dlModel ?? base).trim();
    if(!m) return;
    setDlCompatLoading(true);
    try{ const r=await apiClient.soup.compat(m); setDlCompat(r.data); }catch(e:any){ setDlCompat(null); } finally{ setDlCompatLoading(false); }
  };
  const refreshBest = async()=>{ try{ const r=await apiClient.soup.bestModel(); setBestInfo(r.data);}catch{} };
  const refreshTradebotLocal = async()=>{ try{ const r=await apiClient.soup.tradebotLocalStatus(); setTradebotLocalState(r.data);}catch{} };
  const refreshPairs = async(q?:string)=>{
    setPairsLoading(true);
    try{
      const r=await (apiClient as any).jarvis.pairs(q || '', 100);
      setAvailablePairs(r.data?.pairs||[]);
    }catch{ 
      // fallback to direct fetch
      try{
        const { getApiBaseUrl } = await import('@/services/api');
        const base = getApiBaseUrl().replace('/api/v1','');
        const res = await fetch(`${base}/api/v1/jarvis/pairs?q=${encodeURIComponent(q||'')}&limit=100`);
        const j = await res.json();
        setAvailablePairs(j?.pairs||[]);
      }catch{ setAvailablePairs([]); }
    } finally{ setPairsLoading(false); }
  };
  const doPullModel = async()=>{
    const m=dlModel.trim();
    if(!m) { setDlMsg('Enter a model id (e.g. Qwen/Qwen2.5-0.5B-Instruct or qwen2.5:0.5b)'); return; }
    setDlBusy(true); setDlMsg(null); setDlProgress(0);
    try{
      const r=await apiClient.soup.pullModel(m);
      setDlMsg(r.data?.message || `Pulling ${m} in background`);
      if(dlPollRef.current) clearInterval(dlPollRef.current);
      dlPollRef.current=setInterval(async()=>{
        try{
          const s=await apiClient.soup.pullStatus(m);
          setDlCompat(s.data);
          setDlProgress(s.data?.progress ?? null);
          if(s.data?.done || s.data?.phase==='done' || s.data?.phase==='cancelled' || s.data?.phase==='error'){
            if(dlPollRef.current) clearInterval(dlPollRef.current);
            dlPollRef.current=null;
            setDlBusy(false);
            refreshModels(); refreshCatalogue(); refreshBest();
          }
        }catch{}
      },2000);
      refreshCompat(m);
    }catch(e:any){ setDlMsg(e?.response?.data?.detail || e?.message || 'Pull failed'); setDlBusy(false); }
  };
  const doCancelPull = async()=>{
    const m=dlModel.trim();
    if(!m) return;
    try{
      const r=await apiClient.soup.pullCancel(m);
      setDlMsg(r.data?.message || 'Cancelled — partial files kept for resume');
      setDlBusy(false);
      if(dlPollRef.current){ clearInterval(dlPollRef.current); dlPollRef.current=null; }
      refreshTradebotLocal();
    }catch(e:any){ setDlMsg(e?.response?.data?.detail || e?.message || 'Cancel failed'); }
  };
  const doTradebotLocalTrain = async()=>{
    setTradebotBusy(true); setStartErr(null);
    try{
      const r=await apiClient.soup.tradebotLocalTrain({ base: base||undefined, epochs, auto_download: true, symbols: ohlcvSymbols.length? ohlcvSymbols: undefined });
      setTradebotLocalState(r.data);
      if(r.data?.compat) setDlCompat(r.data.compat);
      if(r.data?.phase==='training' && r.data?.job) { refreshJobs(); }
      refreshBest(); refreshModels();
    }catch(e:any){ setStartErr(e?.response?.data?.detail || e?.message || 'Tradebot-local train failed'); }
    finally{ setTradebotBusy(false); }
  };

  useEffect(()=>{
    refreshStatus(); refreshRecipes(); refreshJobs(); refreshKronos(); refreshUi(); refreshModels(); refreshCatalogue(); refreshBest(); refreshTradebotLocal(); refreshPairs('');
    const id=setInterval(()=>{ refreshJobs(); refreshStatus(); },7000);
    const id2=setInterval(refreshUi,15000);
    const id3=setInterval(refreshTradebotLocal,5000);
    return()=>{clearInterval(id); clearInterval(id2); clearInterval(id3); if(dlPollRef.current) clearInterval(dlPollRef.current);};
  },[]);
  useEffect(()=>{
    if(!selected) return;
    const active=selected.status==='running'||selected.status==='pending';
    if(!active) return;
    const id=setInterval(async()=>{
      try{ const r=await apiClient.soup.job(selected.id, 80); setSelected(r.data);}catch{}
    },3000);
    return()=>clearInterval(id);
  },[selected?.id, selected?.status]);

  // auto-refresh compat when dlModel or base changes (debounced) — covers both selectors
  useEffect(()=>{
    const m = (dlModel || base).trim();
    if(!m) return;
    const t=setTimeout(()=>{ refreshCompat(m); },400);
    return()=>clearTimeout(t);
  },[dlModel, base]);

  const doUpload = async(file: File)=>{
    setUploading(true); setUploadMsg(null);
    try{
      const r=await apiClient.soup.upload(file);
      setDatasetPath(r.data?.path||'');
      setUploadMsg(`Uploaded ${r.data?.rows ?? '?'} rows → ${r.data?.path}`);
    }catch(e:any){ setUploadMsg(e?.response?.data?.detail || e?.message || 'Upload failed'); }
    finally{ setUploading(false); }
  };
  const doOhlcvExport = async()=>{
    setUploading(true); setUploadMsg(null);
    try{
      const payload:any={ timeframe: ohlcvTf, limit: ohlcvLimit };
      if(ohlcvSymbols.length>0) payload.symbols=ohlcvSymbols;
      else payload.symbol='BTC/USD';
      const r=await apiClient.soup.ohlcvExport(payload);
      setDatasetPath(r.data?.path||'');
      if(r.data?.per_symbol) setUploadMsg(`OHLCV → JSONL: ${r.data?.windows ?? '?'} windows (${r.data?.rows} rows) across ${r.data?.symbols?.length||ohlcvSymbols.length} symbols → ${r.data?.path}`);
      else setUploadMsg(`OHLCV → JSONL: ${r.data?.windows ?? '?'} windows (${r.data?.rows} rows) → ${r.data?.path}`);
    }catch(e:any){ setUploadMsg(e?.response?.data?.detail || e?.message || 'OHLCV export failed'); }
    finally{ setUploading(false); }
  };
  const doStart = async()=>{
    setStarting(true); setStartErr(null);
    try{
      const r=await apiClient.soup.start({
        base, task, template: template||undefined,
        dataset_path: datasetPath||undefined,
        dataset_format: 'alpaca',
        epochs, learning_rate: lr, batch_size: batch,
        lora_r: loraR, lora_alpha: loraAlpha,
        quantization: quant==='none' ? null : quant,
        stream_layers: streamLayers,
        ohlcv_symbol: undefined,
      });
      setSelected(r.data); refreshJobs();
    }catch(e:any){ setStartErr(e?.response?.data?.detail || e?.message || 'Start failed'); }
    finally{ setStarting(false); }
  };
  const doCancel = async(id:number)=>{ try{ await apiClient.soup.cancel(id); refreshJobs(); }catch{} };
  const doDelete = async(id:number)=>{
    if(!window.confirm(`Delete Soup job #${id} and free its disk space?`)) return;
    try{ await apiClient.soup.delete(id); if(selected?.id===id) setSelected(null); refreshJobs(); }catch(e:any){ setStartErr(e?.response?.data?.detail || e?.message); }
  };

  const setupPending = status!==null && (!status.installed || !status.repo_cloned);
  const updateAvail = !!status?.update_available;
  const rec = status?.recommendation;

  return (
    <>
      <Head><title>Soup Lab — LLM Finetuning</title></Head>
      <div className="space-y-5" suppressHydrationWarning>
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="p-2 rounded-xl bg-fuchsia-500/15"><FlaskConical className="w-6 h-6 text-fuchsia-400"/></div>
            <div>
              <h1 className="text-xl font-bold text-white flex items-center gap-2">Soup Lab <span className="text-[11px] font-normal text-gray-500">MakazhanAlpamys/Soup — trysoup.dev</span></h1>
              <p className="text-xs text-gray-500">Fine-tune any HF LLM (SFT/DPO/GRPO, LoRA/QLoRA) + experimental OHLCV → LLM bridge. Auto-updates on every start.</p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <Link href="/kronos-finetune" className="flex items-center gap-1 text-xs text-gray-400 hover:text-white"><Telescope className="w-4 h-4"/> Kronos Lab</Link>
            <Link href="/kronos-forecast" className="flex items-center gap-1 text-xs text-gray-400 hover:text-white"><ChevronLeft className="w-4 h-4"/> Forecast</Link>
          </div>
        </div>

        {setupPending && (
          <div className="flex items-start gap-2 bg-amber-500/10 border border-amber-500/30 rounded-lg px-3 py-2 text-xs text-amber-200">
            <AlertTriangle className="w-4 h-4 shrink-0 mt-0.5"/>
            <div>
              <p className="font-medium">Soup not fully installed.</p>
              <p className="mt-1">Run this once on the server, then refresh:</p>
              <code className="mt-1 block bg-black/40 rounded px-2 py-1 text-amber-100 break-all">{status?.setup_command || 'bash plugins/SoupPlugin/scripts/setup_soup.sh'}</code>
              <p className="mt-1 text-amber-300/80">Isolated venv at ~/.tradebot/soup-venv · repo at integrations/soup · <span className="underline">TRADEBOT_SKIP_SOUP_SETUP=1 to skip</span></p>
            </div>
          </div>
        )}
        {updateAvail && (
          <div className="flex items-center justify-between bg-blue-500/10 border border-blue-500/30 rounded-lg px-3 py-2 text-xs text-blue-200">
            <span className="flex items-center gap-2"><RefreshCw className="w-4 h-4 animate-spin"/>Soup update available: {String(status?.version?.commit||'').slice(0,7)} → {String(status?.remote_commit||'').slice(0,7)}</span>
            <button onClick={async()=>{ try{ await apiClient.soup.pullSoupUpdate(); setTimeout(()=>refreshStatus(),2000);}catch(e:any){ alert(e?.response?.data?.detail||e?.message||'Update failed'); }}} className="px-3 py-1 rounded bg-blue-600 hover:bg-blue-500 text-white text-xs">Update Soup now</button>
          </div>
        )}

        {/* Recommendation + health bar */}
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-3">
          <div className="bg-gray-900/60 border border-gray-700/50 rounded-xl p-3">
            <p className="text-[11px] font-semibold text-gray-400 uppercase tracking-wide mb-1 flex items-center gap-1"><Cpu className="w-3.5 h-3.5"/> Best for this hardware</p>
            {rec ? (
              <div className="text-xs text-gray-300 space-y-1">
                <p className="text-fuchsia-300">{rec.reason || 'auto-tuned'}</p>
                <p>RAM {rec.ram_gb ?? '?'}GB{rec.vram_gb ? ` · VRAM ${Number(rec.vram_gb).toFixed(0)}GB` : ''} · quant: <span className="text-white">{rec.quantization || 'bf16'}</span> · stream_layers: <span className="text-white">{String(rec.stream_layers)}</span> · batch: <span className="text-white">{rec.batch_size}</span></p>
                <p className="text-[11px] text-gray-500">Applied to form defaults — edit as needed. Stream-layers lets an 8B train on 4GB (Soup docs).</p>
              </div>
            ) : <p className="text-xs text-gray-600">Detecting…</p>}
          </div>
          <div className="bg-gray-900/60 border border-gray-700/50 rounded-xl p-3">
            <p className="text-[11px] font-semibold text-gray-400 uppercase tracking-wide mb-1 flex items-center gap-1"><Database className="w-3.5 h-3.5"/> Models (unified)</p>
            <div className="text-[11px] leading-relaxed text-gray-400 max-h-28 overflow-auto">
              <p className="text-gray-300 font-medium">Kronos catalogue ({status?.kronos_models?.length ?? kronosOverview?.kronos?.catalogue?.length ?? 3}) + Soup</p>
              {(status?.kronos_models || kronosOverview?.kronos?.catalogue || []).slice(0,6).map((m:any)=><span key={m.id} className="inline-flex items-center gap-1 mr-1 mb-1 px-1.5 py-0.5 rounded-full bg-gray-800 text-gray-300">{m.id.split('/').pop()} {m.installed ? '●' : '○'}</span>)}
              {status?.kronos_finetuned?.length ? <p className="mt-1">Finetuned: {status.kronos_finetuned.map((j:any)=>j.id).join(', ')}</p> : null}
              {status?.models?.length ? <p className="mt-1">Soup/HF cached: {status.models.slice(0,4).map((m:any)=>m.id).join(', ')}{status.models.length>4?' …':''}</p> : null}
              {installedModels.length ? <p className="mt-1">Installed: {installedModels.slice(0,4).map((m:any)=>m.id).join(', ')}{installedModels.length>4?' …':''}</p> : null}
            </div>
          </div>
          <div className="bg-gray-900/60 border border-gray-700/50 rounded-xl p-3">
            <p className="text-[11px] font-semibold text-gray-400 uppercase tracking-wide mb-1">Soup services</p>
            <div className="text-xs space-y-1">
              <p className={status?.installed ? 'text-green-300':'text-amber-300'}>Binary: {status?.soup_bin_exists ? '✓ '+String(status?.soup_bin).split('/').pop() : 'missing — run setup_soup.sh'}</p>
              <p className={status?.repo_cloned ? 'text-green-300':'text-amber-300'}>Repo: {status?.repo_cloned ? `✓ ${String(status?.version?.commit||'').slice(0,7)} on ${status?.version?.branch||'main'}` : 'not cloned'}</p>
              <p className={uiHealthy?.port_open ? 'text-green-300':'text-gray-400'} suppressHydrationWarning>UI :{status?'7860':'—'} {uiHealthy?.port_open?'● listening':'○ not listening'} · <a href={proxyUrl} target="_blank" rel="noreferrer" className="underline inline-flex items-center gap-1">proxy <ExternalLink className="w-3 h-3"/></a></p>
              <p className="text-gray-500 text-[11px]">Proxied auth — token never leaks to browser (6.5). Direct UI also at http://localhost:7860 when running.</p>
            </div>
          </div>
        </div>

        {/* Download a base model - always rendered hydration-safe */}
        <div suppressHydrationWarning style={mounted?undefined:{display:'none'}} className="bg-gray-900/60 border border-gray-700/50 rounded-xl p-4 space-y-3">
          <p className="text-sm font-semibold text-white flex items-center gap-2"><Download className="w-4 h-4 text-emerald-400"/> Download a base model <span className="text-[11px] font-normal text-gray-500">HF id or Ollama tag — QLoRA-ready, resumes on retry</span></p>
          <div className="flex flex-wrap items-end gap-2">
            <Field label="Model id">
              <input
                suppressHydrationWarning
                value={dlModel}
                onChange={e=>setDlModel(e.target.value)}
                list="soup-dl-cat"
                placeholder="Qwen/Qwen2.5-0.5B-Instruct or qwen2.5:0.5b"
                className={`${inputCls} w-80`}
              />
              <datalist id="soup-dl-cat">
                {(catalogue?.bases||[]).map((b:any)=><option key={b.id} value={b.id}>{b.label}</option>)}
                <option value="qwen2.5:0.5b"/><option value="qwen2.5:1.5b"/><option value="qwen2.5:3b"/><option value="llama3.2:1b"/><option value="phi3:mini"/><option value="mistral:7b"/><option value="gemma2:2b"/>
              </datalist>
            </Field>
            <button onClick={doPullModel} disabled={dlBusy || !dlModel.trim()} className="flex items-center gap-1.5 px-3 py-2 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white text-xs disabled:opacity-50">{dlBusy? <Loader2 className="w-3.5 h-3.5 animate-spin"/>: <Download className="w-3.5 h-3.5"/>} Download</button>
            <button onClick={()=>refreshCompat()} disabled={dlCompatLoading || !dlModel.trim()} className="flex items-center gap-1.5 px-3 py-2 rounded-lg bg-gray-800 hover:bg-gray-700 text-gray-200 text-xs disabled:opacity-50">{dlCompatLoading? <Loader2 className="w-3.5 h-3.5 animate-spin"/>: <BarChart3 className="w-3.5 h-3.5"/>} Check compat</button>
            <button onClick={refreshBest} className="p-2 rounded bg-gray-800 hover:bg-gray-700 text-gray-400"><RefreshCw className="w-4 h-4"/></button>
          </div>
          {dlMsg && <p className="text-[11px] text-emerald-300" suppressHydrationWarning>{dlMsg}</p>}
          {catalogue?.bases && <p className="text-[10px] text-gray-500">Catalogue: {catalogue.bases.slice(0,6).map((b:any)=>b.id).join(' · ')}{catalogue.bases.length>6?' …':''} — cached marked ●</p>}
        </div>

        {/* Compatibility card */}
        {dlCompat && (
          <div className="bg-gray-900/60 border border-gray-700/50 rounded-xl p-4 space-y-2">
            <p className="text-[11px] font-semibold text-gray-400 uppercase tracking-wide flex items-center gap-1"><ShieldCheck className="w-3.5 h-3.5"/> Compatibility — {dlCompat.model} <span className="font-normal text-gray-500">· {dlCompat.spec?.params_b}B · {dlCompat.spec?.family} · cutoff {dlCompat.spec?.cutoff}</span></p>
            <div className="grid grid-cols-2 lg:grid-cols-4 gap-2 text-xs">
              <div className="bg-black/30 rounded-lg p-2">
                <p className="text-[10px] text-gray-500 uppercase tracking-wide flex items-center gap-1"><Package className="w-3 h-3"/> model</p>
                <p className="text-gray-200 truncate" title={dlCompat.hf_id}>{dlCompat.hf_id}</p>
                <p className="text-[11px] text-gray-500">{dlCompat.spec?.download_gb} GB download · {dlCompat.kind}</p>
              </div>
              <div className="bg-black/30 rounded-lg p-2">
                <p className="text-[10px] text-gray-500 uppercase tracking-wide flex items-center gap-1"><HardDrive className="w-3 h-3"/> disk</p>
                <p className={dlCompat.compatibility?.disk_level==='incompatible'?'text-red-300': dlCompat.compatibility?.disk_level==='tight'?'text-amber-300':'text-green-300'}>{dlCompat.resources?.disk_needed_gb} GB needed {dlCompat.compatibility?.is_cached ? '(cached)' : ''}</p>
                <p className="text-[11px] text-gray-500">{dlCompat.hardware?.free_disk_gb} GB free · hf cache {dlCompat.hardware?.hf_cache_free_disk_gb} GB{dashboardCompatExtra(dlCompat)}</p>
              </div>
              <div className="bg-black/30 rounded-lg p-2">
                <p className="text-[10px] text-gray-500 uppercase tracking-wide flex items-center gap-1"><Cpu className="w-3 h-3"/> ram</p>
                <p className={dlCompat.compatibility?.ram_level==='incompatible'?'text-red-300': dlCompat.compatibility?.ram_level==='tight'?'text-amber-300':'text-green-300'}>{dlCompat.resources?.ram_needed_gb} GB needed</p>
                <p className="text-[11px] text-gray-500">{dlCompat.hardware?.ram_gb} GB{dashboardCompatExtra(dlCompat)} · {dlCompat.hardware?.is_apple_silicon?'Apple Silicon':'x86/CUDA'}</p>
              </div>
              <div className="bg-black/30 rounded-lg p-2">
                <p className="text-[10px] text-gray-500 uppercase tracking-wide flex items-center gap-1"><BarChart3 className="w-3 h-3"/> training config</p>
                <p className="text-gray-200">quant {dlCompat.resources?.quantization} · batch {dlCompat.resources?.batch_size}</p>
                <p className="text-[11px] text-gray-500">stream_layers {String(dlCompat.resources?.stream_layers)} · lora_r {dlCompat.resources?.lora_r} · {dlCompat.resources?.backend}</p>
              </div>
            </div>
            <div className="flex items-center gap-1.5 text-xs">
              {dlCompat.compatibility?.level==='ok' && <ShieldCheck className="w-4 h-4 text-green-400"/>}
              {dlCompat.compatibility?.level==='tight' && <ShieldAlert className="w-4 h-4 text-amber-400"/>}
              {dlCompat.compatibility?.level==='incompatible' && <ShieldX className="w-4 h-4 text-red-400"/>}
              <span className={dlCompat.compatibility?.level==='ok'?'text-green-300': dlCompat.compatibility?.level==='tight'?'text-amber-300':'text-red-300'} suppressHydrationWarning>{dlCompat.compatibility?.reason} — {dlCompat.compatibility?.level}</span>
              <span className="text-gray-500">{dlCompat.compatibility?.compatible?'compatible':'not compatible'}</span>
            </div>
            {dlCompat.compatibility?.warnings?.length>0 && (
              <ul className="text-[11px] text-amber-300/90 list-disc pl-4 space-y-0.5">
                {dlCompat.compatibility.warnings.map((w:string,i:number)=><li key={i}>{w}</li>)}
              </ul>
            )}
            {dlCompat.improvement && (
              <div className="mt-2 text-[11px] text-gray-400 bg-black/20 rounded-lg p-2 space-y-1">
                <p className="font-medium text-gray-300 flex items-center gap-1"><Info className="w-3 h-3"/> What improves after fine-tuning tradebot-local — improvement detail</p>
                <p><span className="text-gray-300">Base strength:</span> {dlCompat.improvement.base_strength}</p>
                <p><span className="text-gray-300">Expected improvement:</span> {dlCompat.improvement.expected_improvement}</p>
                <p><span className="text-gray-300">Adds:</span> {dlCompat.improvement.what_finetune_adds}</p>
                <p className="text-[10px] text-gray-500">Sources: {dlCompat.improvement.training_sources?.slice(0,3).join(' · ')} — {dlCompat.improvement.data_window}</p>
                <p className="text-[10px] text-gray-500">Serving: {dlCompat.improvement.serving}</p>
              </div>
            )}
            <div className="flex gap-2 text-[11px]">
              <a href={dlCompat.improvement?.base_model_link} target="_blank" rel="noreferrer" className="underline text-gray-400 inline-flex items-center gap-1">HF model <ExternalLink className="w-3 h-3"/></a>
              <a href={dlCompat.improvement?.soup_docs} target="_blank" rel="noreferrer" className="underline text-gray-400">Soup docs</a>
            </div>
          </div>
        )}

        {/* Download progress bar with cancel */}
        {(dlBusy || (dlProgress!==null && dlProgress!==undefined)) && (
          <div className="bg-gray-900/60 border border-gray-700/50 rounded-xl p-3 space-y-2" suppressHydrationWarning>
            <div className="flex items-center justify-between text-xs">
              <span className="text-gray-300 flex items-center gap-1.5"><Clock className="w-3.5 h-3.5"/> Downloading {dlModel} — {dlProgress ?? 0}% {dlCompat?.speed ? `· ${dlCompat.speed}` : ''}</span>
              <button onClick={doCancelPull} disabled={!dlBusy} className="flex items-center gap-1 px-2 py-1 rounded bg-red-600 hover:bg-red-500 text-white text-[11px] disabled:opacity-40"><StopCircle className="w-3 h-3"/> Cancel</button>
            </div>
            <div className="w-full h-2 bg-gray-800 rounded-full overflow-hidden">
              <div className="h-full bg-emerald-500 transition-all duration-500" style={{width: `${Math.min(100, Math.max(0, dlProgress ?? 0))}%`}} />
            </div>
            <p className="text-[10px] text-gray-500 truncate">{dlCompat?.pull_state?.log_tail?.slice(-200) || dlCompat?.log?.slice(-200) || dlCompat?.phase || (dlBusy?'downloading…':'idle')}</p>
          </div>
        )}

        {/* Best for tradebot-local card */}
        <div className="bg-gray-900/60 border border-gray-700/50 rounded-xl p-4 space-y-2">
          <p className="text-[11px] font-semibold text-gray-400 uppercase tracking-wide flex items-center gap-1"><Brain className="w-3.5 h-3.5"/> Best for tradebot-local</p>
          {bestInfo ? (
            <div className="space-y-2">
              <div className="flex flex-wrap items-center gap-2 text-xs">
                <span className="px-2 py-1 rounded-full bg-fuchsia-500/20 text-fuchsia-300 font-medium">{bestInfo.best?.id}</span>
                <span className="text-gray-400">{bestInfo.best?.label}</span>
                <span className={`px-1.5 py-0.5 rounded-full text-[10px] ${bestInfo.cached?'bg-green-500/15 text-green-300':'bg-gray-700 text-gray-400'}`}>{bestInfo.cached?'cached':'not cached'}</span>
                <span className="text-gray-500">{bestInfo.compat?.spec?.download_gb} GB · compat {bestInfo.compat?.compatibility?.level}{dashboardCompatExtra(bestInfo.compat)}</span>
              </div>
              <p className="text-xs text-gray-300">{bestInfo.improvement?.expected_improvement}</p>
              <p className="text-[11px] text-gray-500">Hardware: {bestInfo.hardware?.ram_gb} GB RAM{dashboardCompatExtra(bestInfo)} · {bestInfo.hardware?.free_disk_gb} GB free</p>
              <div className="flex flex-wrap gap-2">
                <button onClick={()=>{ setBase(bestInfo.best.id); setDlModel(bestInfo.best.id); refreshCompat(bestInfo.best.id); }} className="px-3 py-1.5 rounded-lg bg-gray-800 hover:bg-gray-700 text-gray-200 text-xs">Use this base</button>
                <button onClick={doTradebotLocalTrain} disabled={tradebotBusy} className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-fuchsia-600 hover:bg-fuchsia-500 text-white text-xs disabled:opacity-50">{tradebotBusy? <Loader2 className="w-3.5 h-3.5 animate-spin"/>: <Brain className="w-3.5 h-3.5"/>} {tradebotBusy?'Training…':'Train tradebot-local'}</button>
                <button onClick={refreshBest} className="p-2 rounded bg-gray-800 hover:bg-gray-700 text-gray-400"><RefreshCw className="w-4 h-4"/></button>
              </div>
              {bestInfo.improvement?.training_sources && <p className="text-[10px] text-gray-600">Training sources: {bestInfo.improvement.training_sources.slice(0,3).join(' · ')}</p>}
            </div>
          ) : <p className="text-xs text-gray-600">Loading best model for this hardware…</p>}
          {tradebotLocalState && (
            <div className="mt-2 text-[11px] bg-black/20 rounded-lg p-2 space-y-1" suppressHydrationWarning>
              <p className="font-medium text-gray-300 flex items-center gap-1"><Clock className="w-3 h-3"/> tradebot-local pipeline — phase: <span className="text-fuchsia-300">{tradebotLocalState.phase}</span> {tradebotLocalState.model && <span className="text-gray-400">· {tradebotLocalState.model}</span>}</p>
              {tradebotLocalState.job && <p className="text-gray-400">Job #{tradebotLocalState.job.id} — {tradebotLocalState.job.status} {tradebotLocalState.job.log_tail ? `· ${String(tradebotLocalState.job.log_tail).slice(-120)}` : ''}</p>}
              {tradebotLocalState.download && <p className="text-gray-500">Download {tradebotLocalState.download.progress ?? 0}% — {tradebotLocalState.download.phase} {tradebotLocalState.download.speed? `· ${tradebotLocalState.download.speed}`:''}</p>}
              {tradebotLocalState.log && <p className="text-amber-300/80">{tradebotLocalState.log}</p>}
            </div>
          )}
        </div>

        {/* Training form */}
        <div className="bg-gray-900/60 border border-gray-700/50 rounded-xl p-4 space-y-4">
          <p className="text-sm font-semibold text-white flex items-center gap-2"><Zap className="w-4 h-4 text-fuchsia-400"/> New Soup job <span className="text-[11px] font-normal text-gray-500">base, task, data, lora, quant — maps to soup.yaml</span></p>
          <div className="flex flex-wrap items-end gap-3">
            <Field label="Base model (HF id)">
              <div className="flex items-center gap-2">
                {baseMode==='select' ? (
                  <select value={base} onChange={e=>{ setBase(e.target.value); setDlModel(e.target.value); }} className={`${inputCls} w-72`}>
                    {/* Ensure current base is always selectable even if not in any list */}
                    {base && !installedModels.some((m:any)=>m.id===base) && !(catalogue?.bases||[]).some((b:any)=>b.id===base) && !(catalogue?.ollama_tags||[]).includes(base) && !(catalogue?.hf_cache||[]).some((m:any)=>m.id===base) && <option value={base}>{base} — current selection</option>}
                    <optgroup label="Ollama — all local models (★ = tradebot-local, used as default)">
                      {installedModels.filter((m:any)=>m.source==='ollama').map((m:any)=><option key={m.id} value={m.id}>{m.label||m.id}{m.is_trained?' ★ trained':''}</option>)}
                      {installedModels.filter((m:any)=>m.source==='ollama').length===0 && (catalogue?.ollama_tags||[]).map((t:string)=><option key={t} value={t}>Ollama: {t}</option>)}
                      {installedModels.filter((m:any)=>m.source==='ollama').length===0 && (catalogue?.ollama_tags||[]).length===0 && <option value="qwen2.5:0.5b">qwen2.5:0.5b (example)</option>}
                    </optgroup>
                    <optgroup label="Trained">
                      {installedModels.filter((m:any)=>m.source==='trained_adapter' || m.source==='trained_gguf').map((m:any)=><option key={m.id} value={m.id}>{m.label||m.id}</option>)}
                      {installedModels.filter((m:any)=>m.source==='trained_adapter' || m.source==='trained_gguf').length===0 && <option disabled value="">(no trained yet — train tradebot-local to create one)</option>}
                    </optgroup>
                    <optgroup label="Catalogue — downloadable HF bases (● = cached)">
                      {(catalogue?.bases||[]).map((b:any)=><option key={b.id} value={b.id}>{b.label} {b.cached?'●':''}</option>)}
                    </optgroup>
                    <optgroup label="HF cache — already downloaded">
                      {installedModels.filter((m:any)=>m.source==='hf_cache').map((m:any)=><option key={m.id} value={m.id}>{m.id} {m.size_mb? `(${m.size_mb} MB)`:''}</option>)}
                      {(catalogue?.hf_cache||[]).filter((m:any)=>!installedModels.some((x:any)=>x.id===m.id)).map((m:any)=><option key={m.id} value={m.id}>{m.id} {m.size_mb? `(${m.size_mb} MB)`:''}</option>)}
                      {installedModels.filter((m:any)=>m.source==='hf_cache').length===0 && (catalogue?.hf_cache||[]).length===0 && <option disabled value="">(no cache yet — download a base below)</option>}
                    </optgroup>
                    <optgroup label="Kronos">
                      {(status?.kronos_models||[]).slice(0,20).map((m:any)=><option key={m.id} value={m.id}>{m.id}</option>)}
                    </optgroup>
                    {recipes.slice(0,10).map(r=><option key={r} value={r}>{r}</option>)}
                  </select>
                ) : (
                  <input value={base} onChange={e=>setBase(e.target.value)} placeholder="Qwen/Qwen2.5-0.5B-Instruct or custom HF id" className={`${inputCls} w-72`} />
                )}
                <button onClick={()=>setBaseMode(m=>m==='select'?'custom':'select')} className="px-2 py-1 rounded bg-gray-800 hover:bg-gray-700 text-gray-300 text-xs">{baseMode==='select'?'Custom':'Select'}</button>
              </div>
            </Field>
            <Field label="Task"><select value={task} onChange={e=>setTask(e.target.value)} className={`${inputCls} w-28`}><option value="sft">sft</option><option value="dpo">dpo</option><option value="grpo">grpo</option><option value="ppo">ppo</option><option value="kto">kto</option><option value="orpo">orpo</option></select></Field>
            <Field label="Template"><select value={template} onChange={e=>setTemplate(e.target.value)} className={`${inputCls} w-36`}><option value="">(auto)</option>{recipes.map(r=><option key={r} value={r}>{r}</option>)}</select></Field>
            <Field label="Epochs"><input type="number" min={1} value={epochs} onChange={e=>setEpochs(Math.max(1,Number(e.target.value)||1))} className={`${inputCls} w-16`}/></Field>
            <Field label="LR"><input type="number" step="0.00001" value={lr} onChange={e=>setLr(Number(e.target.value)||2e-5)} className={`${inputCls} w-24`}/></Field>
            <Field label="Batch"><input value={batch} onChange={e=>setBatch(e.target.value)} className={`${inputCls} w-20`}/></Field>
            <Field label="LoRA r"><input type="number" min={1} value={loraR} onChange={e=>setLoraR(Math.max(1,Number(e.target.value)||1))} className={`${inputCls} w-16`}/></Field>
            <Field label="α"><input type="number" min={1} value={loraAlpha} onChange={e=>setLoraAlpha(Math.max(1,Number(e.target.value)||1))} className={`${inputCls} w-16`}/></Field>
            <Field label="Quant"><select value={quant} onChange={e=>setQuant(e.target.value)} className={`${inputCls} w-24`}><option value="4bit">4bit (QLoRA)</option><option value="8bit">8bit</option><option value="none">bf16</option></select></Field>
            <label className="flex items-center gap-1.5 text-[11px] text-gray-300 cursor-pointer select-none"><input type="checkbox" checked={streamLayers} onChange={e=>setStreamLayers(e.target.checked)} className="rounded border-gray-600 bg-gray-800 text-fuchsia-500"/> stream_layers</label>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <button onClick={doTradebotLocalTrain} disabled={tradebotBusy || !base} className="flex items-center gap-1.5 px-3 py-2 rounded-lg bg-fuchsia-700 hover:bg-fuchsia-600 text-white text-xs disabled:opacity-50">{tradebotBusy? <Loader2 className="w-3.5 h-3.5 animate-spin"/>: <Brain className="w-3.5 h-3.5"/>} Continue tradebot-local</button>
            <span className="text-[10px] text-gray-500">Trains your local fallback from recent data (download best if needed, visible in jobs).</span>
          </div>
          <div className="flex flex-wrap items-end gap-3">
            <Field label="Dataset path (or upload / OHLCV)"><input value={datasetPath} onChange={e=>setDatasetPath(e.target.value)} placeholder="path to .jsonl/.csv or leave empty and use upload/OHLCV" className={`${inputCls} w-[32rem]`}/></Field>
            <input ref={fileRef} type="file" accept=".jsonl,.json,.csv,.parquet" className="hidden" onChange={e=>{ const f=e.target.files?.[0]; if(f) doUpload(f); }} />
            <button onClick={()=>fileRef.current?.click()} disabled={uploading} className="flex items-center gap-1.5 px-3 py-2 rounded-lg bg-gray-800 hover:bg-gray-700 text-gray-200 text-xs disabled:opacity-50">{uploading? <Loader2 className="w-3.5 h-3.5 animate-spin"/>: <Upload className="w-3.5 h-3.5"/>} Upload dataset</button>
          </div>
          {uploadMsg && <p className="text-[11px] text-emerald-300">{uploadMsg}</p>}
          <div className="flex flex-wrap items-end gap-3 border-t border-gray-800 pt-3">
            <p className="text-[11px] font-semibold text-blue-300 w-full flex items-center gap-1"><Layers className="w-3.5 h-3.5"/> Experimental OHLCV → LLM bridge (side-by-side with Kronos, not replacement)</p>
            <div className="flex flex-wrap items-center gap-2 w-full">
              <div className="flex flex-wrap gap-1 max-w-[42rem]">
                {ohlcvSymbols.map(s=>(
                  <span key={s} className="inline-flex items-center gap-1 px-2 py-1 rounded-full bg-blue-500/15 text-blue-300 text-xs border border-blue-500/30">{s}<button onClick={()=>removeOhlcvSymbol(s)} className="ml-1 hover:text-white"><X className="w-3 h-3"/></button></span>
                ))}
                {ohlcvSymbols.length===0 && <span className="text-xs text-gray-600">No symbols — select below or type</span>}
              </div>
              <input value={ohlcvInput} onChange={e=>setOhlcvInput(e.target.value)} onKeyDown={e=>{ if(e.key==='Enter'){ e.preventDefault(); addOhlcvSymbols(); }}} placeholder="Add symbol e.g. BTC/USD, ETH/USD" className={`${inputCls} w-48`} />
              <button onClick={()=>addOhlcvSymbols()} className="px-2 py-1 rounded bg-gray-800 hover:bg-gray-700 text-gray-300 text-xs">Add</button>
              <button onClick={()=>addOhlcvSymbols('BTC/USD,ETH/USD,SOL/USD')} className="px-2 py-1 rounded bg-gray-800 hover:bg-gray-700 text-gray-300 text-xs">+BTC/ETH/SOL</button>
              <span className="text-[10px] text-gray-500" suppressHydrationWarning>{ohlcvSymbols.length} selected</span>
            </div>
            {/* Searchable pair catalog — select more pairs for training */}
            <div className="w-full bg-black/20 rounded-lg p-2 border border-gray-800/50 space-y-2">
              <div className="flex flex-wrap items-center gap-2">
                <input value={pairSearch} onChange={e=>{ setPairSearch(e.target.value); if(e.target.value.trim().length>=1) refreshPairs(e.target.value.trim()); }} onKeyDown={e=>{ if(e.key==='Enter'){ e.preventDefault(); refreshPairs(pairSearch.trim()); }}} placeholder="Search pairs (e.g. BTC, SOL, pepe) — selectable for training" className={`${inputCls} w-64`} />
                <button onClick={()=>refreshPairs(pairSearch.trim()||'')} disabled={pairsLoading} className="px-2 py-1 rounded bg-gray-800 hover:bg-gray-700 text-gray-300 text-xs disabled:opacity-50">{pairsLoading? <Loader2 className="w-3 h-3 animate-spin"/>: <Database className="w-3 h-3"/>} Search</button>
                <button onClick={()=>{ const top = availablePairs.slice(0,20).map((p:any)=>p.symbol); setOhlcvSymbols(prev=> Array.from(new Set([...prev, ...top]))); }} className="px-2 py-1 rounded bg-fuchsia-900/30 hover:bg-fuchsia-800/40 text-fuchsia-300 text-xs border border-fuchsia-700/30">+ Top 20</button>
                <button onClick={()=>{ setOhlcvSymbols([]); }} className="px-2 py-1 rounded bg-gray-800 hover:bg-gray-700 text-gray-400 text-xs">Clear</button>
                <span className="text-[10px] text-gray-500">{availablePairs.length} matches · {ohlcvSymbols.length} selected for next train (also used by Tradebot-local)</span>
              </div>
              {availablePairs.length>0 && (
                <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-1 max-h-40 overflow-auto">
                  {availablePairs.slice(0,40).map((p:any)=>{
                    const sel = ohlcvSymbols.includes(p.symbol);
                    return (
                      <button key={p.symbol} onClick={()=>{ if(sel) removeOhlcvSymbol(p.symbol); else addOhlcvSymbols(p.symbol); }} className={`text-left px-2 py-1 rounded text-xs border ${sel? 'bg-fuchsia-500/20 text-fuchsia-300 border-fuchsia-500/30':'bg-gray-800/50 text-gray-400 border-gray-700/50 hover:bg-gray-700/50 hover:text-gray-300'}`} title={`${p.name} · rank #${p.market_cap_rank||'?'} `}>
                        <span className="font-medium">{p.symbol}</span> <span className="text-[10px] text-gray-500">{p.name?.slice(0,14)}</span> {sel? '✓':''}
                      </button>
                    );
                  })}
                </div>
              )}
              {availablePairs.length===0 && !pairsLoading && <p className="text-[11px] text-gray-600">Type to search tradeable pairs from the app catalog. Selected pairs feed both OHLCV export and Tradebot-local training (pair catalog + OHLCV windows).</p>}
            </div>
            <Field label="Timeframe"><input value={ohlcvTf} onChange={e=>setOhlcvTf(e.target.value)} className={`${inputCls} w-20`}/></Field>
            <Field label="Candles"><input type="number" min={256} value={ohlcvLimit} onChange={e=>setOhlcvLimit(Math.max(256, Number(e.target.value)||0))} className={`${inputCls} w-24`}/></Field>
            <button onClick={doOhlcvExport} disabled={uploading} className="flex items-center gap-1.5 px-3 py-2 rounded-lg bg-blue-600 hover:bg-blue-500 text-white text-xs disabled:opacity-50">{uploading? <Loader2 className="w-3.5 h-3.5 animate-spin"/>: <Database className="w-3.5 h-3.5"/>} OHLCV → JSONL ({ohlcvSymbols.length||1})</button>
            <span className="text-[10px] text-gray-500">Converts Kronos-exported candles (MT5→Yahoo→ccxt cascade) into ShareGPT JSONL windows for Soup SFT. Preview in Kronos forecast as dashed band. Supports multi-symbol.</span>
          </div>
          <div className="flex items-center gap-3">
            <button onClick={doStart} disabled={starting || !base} className="flex items-center gap-1.5 px-4 py-2 rounded-lg bg-fuchsia-600 hover:bg-fuchsia-500 text-white text-xs font-semibold disabled:opacity-50">{starting? <Loader2 className="w-3.5 h-3.5 animate-spin"/>: <Play className="w-3.5 h-3.5"/>} Start Soup train</button>
            <button onClick={refreshJobs} className="p-2 rounded bg-gray-800 hover:bg-gray-700 text-gray-400"><RefreshCw className="w-4 h-4"/></button>
            {startErr && <span className="text-[11px] text-red-300 flex items-center gap-1"><AlertTriangle className="w-3.5 h-3.5"/>{startErr}</span>}
          </div>
          <p className="text-[10px] text-gray-500">Maps to <code className="bg-black/40 px-1 rounded">soup train --config soup.yaml</code> per <a href="https://github.com/MakazhanAlpamys/Soup" target="_blank" rel="noreferrer" className="underline">Soup docs</a>. Output at <code className="bg-black/30 px-1 rounded">plugins/SoupPlugin/backend/data/soup/job&lt;id&gt;/output</code>. Serve via <code className="bg-black/30 px-1 rounded">soup serve --model output</code> (proxied).</p>
        </div>

        {/* Jobs table */}
        <div className="bg-gray-900/60 border border-gray-700/50 rounded-xl p-4">
          <div className="flex items-center justify-between mb-3">
            <p className="text-sm font-semibold text-white">Soup jobs <span className="text-gray-500 font-normal">({jobs.length})</span></p>
            <button onClick={refreshJobs} className="text-gray-500 hover:text-gray-300"><RefreshCw className="w-4 h-4"/></button>
          </div>
          {jobs.length===0? <p className="text-xs text-gray-600">No Soup jobs yet — start one above or upload a dataset.</p> : (
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead><tr className="text-gray-500 uppercase tracking-wide text-[10px] border-b border-gray-800"><th className="text-left py-2 pr-3">#</th><th className="text-left py-2 pr-3">Base</th><th className="text-left py-2 pr-3">Task</th><th className="text-left py-2 pr-3">Status</th><th className="text-right py-2 pr-3">Epochs</th><th className="text-right py-2 pr-3">Size</th><th className="text-left py-2 pr-3">Started</th><th className="text-right py-2 pr-3">Dur</th><th className="text-right py-2">Actions</th></tr></thead>
                <tbody>
                  {jobs.map(j=>(
                    <tr key={j.id} onClick={()=>setSelected(j)} className={`cursor-pointer border-b border-gray-800/60 ${selected?.id===j.id?'bg-fuchsia-500/[0.06]':'hover:bg-gray-800/40'}`}>
                      <td className="py-2 pr-3 text-gray-500">{j.id}</td>
                      <td className="py-2 pr-3 text-fuchsia-300 font-medium truncate max-w-[16rem]" title={j.base_model}>{j.base_model.split('/').pop()}</td>
                      <td className="py-2 pr-3 text-gray-400">{j.task}</td>
                      <td className="py-2 pr-3"><span className={`px-1.5 py-0.5 rounded-full text-[10px] font-semibold uppercase ${STATUS_STYLE[j.status]||''}`}>{j.status}</span></td>
                      <td className="py-2 pr-3 text-right tabular-nums text-gray-300">{j.epochs}</td>
                      <td className="py-2 pr-3 text-right tabular-nums text-gray-300">{j.status==='completed'&&j.disk_size_mb? `${j.disk_size_mb} MB` : '—'}</td>
                      <td suppressHydrationWarning className="py-2 pr-3 text-gray-500">{fmtWhen(j.started_at||j.created_at)}</td>
                      <td suppressHydrationWarning className="py-2 pr-3 text-right tabular-nums text-gray-400">{fmtDuration(j.started_at,j.finished_at)}</td>
                      <td className="py-2 text-right">
                        <span className="inline-flex items-center gap-1.5">
                          {(j.status==='running'||j.status==='pending') && <button onClick={e=>{e.stopPropagation(); doCancel(j.id);}} className="p-1 rounded bg-red-600/70 hover:bg-red-500 text-white"><X className="w-3 h-3"/></button>}
                          <button onClick={e=>{e.stopPropagation(); doDelete(j.id);}} disabled={j.status==='running'} className="p-1 rounded bg-gray-700 hover:bg-red-600 text-gray-300 hover:text-white disabled:opacity-30"><HardDrive className="w-3 h-3"/></button>
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>

        {/* Log */}
        <div className="bg-gray-900/60 border border-gray-700/50 rounded-xl p-4">
          <p className="text-[11px] font-semibold text-gray-400 uppercase tracking-wide mb-2">{selected? `Log — Soup job #${selected.id} (${selected.status})` : 'Log'}</p>
          {selected ? (
            <>
              {selected.error && <p className="mb-1 text-[11px] text-red-300">{selected.error}</p>}
              <pre className="bg-black/50 border border-gray-800 rounded-lg p-2 text-[10px] leading-relaxed text-gray-300 h-52 overflow-auto whitespace-pre-wrap">{selected.log_tail || '(no log output yet — polling…)'} </pre>
              <div className="mt-2 flex items-center gap-2 text-[11px] text-gray-500" suppressHydrationWarning>Output: <code className="bg-black/40 px-1 rounded">{selected.output_dir||'—'}</code></div>
            </>
          ) : <p className="text-xs text-gray-600">Select a job to watch its training log.</p>}
        </div>

        {/* Embedded Soup UI (proxied) */}
        <div className="bg-gray-900/60 border border-gray-700/50 rounded-xl p-4">
          <div className="flex items-center justify-between mb-2">
            <p className="text-sm font-semibold text-white flex items-center gap-2"><Sparkles className="w-4 h-4 text-fuchsia-400"/> Soup UI (proxied) <span className="text-[11px] font-normal text-gray-500">token never leaks — via /api/v1/plugins/soup/ui/proxy</span></p>
            <div className="flex items-center gap-2">
              <button onClick={()=>setShowUi(v=>!v)} className="text-xs px-2 py-1 rounded bg-gray-800 hover:bg-gray-700 text-gray-300">{showUi?'Hide':'Show'}</button>
              <a href={proxyUrl} target="_blank" rel="noreferrer" className="text-xs px-2 py-1 rounded bg-fuchsia-600 hover:bg-fuchsia-500 text-white inline-flex items-center gap-1">Open proxy <ExternalLink className="w-3 h-3"/></a>
              <button onClick={refreshUi} className="p-1 rounded bg-gray-800 text-gray-400 hover:text-white"><RefreshCw className="w-3.5 h-3.5"/></button>
            </div>
          </div>
          {!uiHealthy?.port_open && <p className="text-[11px] text-amber-300 mb-2">Soup UI not listening on :7860 — run the UI via <code className="bg-black/40 px-1 rounded">~/.tradebot/soup-venv/bin/soup ui --host 127.0.0.1 --port 7860</code> or <code className="bg-black/40 px-1 rounded">docker compose --profile soup up -d soup</code>. The proxy will show 502 until it is up — the rest of Soup Lab still works.</p>}
          {showUi && (
            <div className="border border-gray-800 rounded-lg overflow-hidden bg-black/30" style={{height: 560}}>
              <iframe src={proxyUrl} title="Soup UI" className="w-full h-full border-0 bg-white" sandbox="allow-same-origin allow-scripts allow-forms allow-popups" suppressHydrationWarning />
            </div>
          )}
          <p className="mt-2 text-[10px] text-gray-600">Docs: <a href="https://github.com/MakazhanAlpamys/Soup" target="_blank" rel="noreferrer" className="underline">github.com/MakazhanAlpamys/Soup</a> · <a href="https://trysoup.dev" target="_blank" rel="noreferrer" className="underline">trysoup.dev</a> · CLI <code className="bg-black/30 px-1 rounded">soup train --config soup.yaml</code> · <code className="bg-black/30 px-1 rounded">soup serve --model output</code> · <code className="bg-black/30 px-1 rounded">soup doctor</code></p>
        </div>

        {/* Kronos unified view (6.2) */}
        <div className="bg-gray-900/60 border border-gray-700/50 rounded-xl p-4">
          <p className="text-[11px] font-semibold text-gray-400 uppercase tracking-wide mb-2 flex items-center gap-1"><Telescope className="w-3.5 h-3.5"/> Unified models — Kronos + Soup</p>
          <div className="grid md:grid-cols-2 gap-3 text-xs">
            <div>
              <p className="font-medium text-gray-300 mb-1">Kronos catalogue</p>
              {(kronosOverview?.kronos?.catalogue || status?.kronos_models || []).map((m:any)=><div key={m.id} className="flex items-center justify-between py-1 border-b border-gray-800/50"><span className="text-gray-300">{m.label || m.id}</span><span className={`text-[10px] px-1.5 py-0.5 rounded-full ${m.installed?'bg-green-500/15 text-green-300':'bg-gray-700 text-gray-400'}`}>{m.installed?'installed':'not cached'}</span></div>)}
              {(kronosOverview?.kronos_jobs||[]).slice(0,3).map((j:any)=><div key={j.id} className="text-[11px] text-gray-500">Finetuned #{j.id}: {j.symbol} {j.timeframe} — {j.status}</div>)}
            </div>
            <div>
              <p className="font-medium text-gray-300 mb-1">Soup / HF cache</p>
              {(kronosOverview?.soup_models || status?.models||[]).slice(0,8).map((m:any)=><div key={m.id} className="flex items-center justify-between py-1 border-b border-gray-800/50"><span className="text-gray-400 truncate mr-2">{m.id}</span><span className="text-[10px] text-gray-500">{m.size_mb? `${m.size_mb} MB`:''}</span></div>)}
              {!((kronosOverview?.soup_models||[]).length|| (status?.models||[]).length) && <p className="text-gray-600">No HF cache yet — train a Soup job or download a Kronos model.</p>}
              {installedModels.length>0 && <div className="mt-2 text-[11px] text-gray-500">Installed count: {installedModels.length} — {installedModels.slice(0,3).map((m:any)=>m.id).join(', ')}</div>}
            </div>
          </div>
        </div>
      </div>
    </>
  );
}
