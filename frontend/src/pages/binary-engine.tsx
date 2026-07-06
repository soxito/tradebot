/**
 * Binary Engine Studio — Voice Recognition Management
 *
 * Real-time voice frequency visualizer, speaker-ID engine, vocabulary vault,
 * codec/plugin browser, and live JARVIS voice-brain sync.
 *
 * Connects to:
 *   • Extension content script (via window.postMessage __jarvisExt messages)
 *   • Backend /api/v1/jarvis/voice-brain/* endpoints
 *   • PaulChat TTS speak-status events
 */

import Head from 'next/head'
import {
  useState, useEffect, useRef, useCallback,
} from 'react'
import {
  AudioWaveform, Mic, MicOff, Brain, Cpu, RefreshCw,
  Save, Upload, Download, CheckCircle, XCircle,
  ChevronRight, Search, Zap, Volume2, VolumeX,
  Activity, BarChart2, Settings2, Trash2, Plus,
  Info, Shield, Wifi, WifiOff,
} from 'lucide-react'
import { apiClient } from '@/services/api'
import { useDeepgramAgent, type DeepgramAgentConfig, JARVIS_DEFAULT_PROMPT, JARVIS_DEFAULT_GREETING } from '@/hooks/useDeepgramAgent'

// ── Types ──────────────────────────────────────────────────────────────────────
interface VoiceProfile {
  bands: number[]
  bandStdDev?: number[]
  centroid?: number
  sessions?: number
  calibratedAt?: number
}

interface BrainState {
  loaded: boolean
  words: Record<string, number>
  profile: VoiceProfile | null
  sessions: number
  lastSync: string | null
}

interface IdentifyResult {
  confidence: number
  match: boolean
  detail?: string
}

interface ExtFreqData {
  bands: number[]
  energy: number
  isUserVoice: boolean
  isSpeaking: boolean
}

interface DeepgramStatus {
  ok: boolean
  key_present?: boolean
  account_name?: string | null
  email?: string | null
  project_id?: string
  key_ok?: boolean
  credits_remaining?: number
  balances?: { amount?: number; units?: string }[]
  balance_error?: string
  error?: string
  required_fix?: string | null
}

// ── Codec / plugin catalogue (static, enriched with metadata) ─────────────────
// ── Codec configuration defaults ──────────────────────────────────────────────
const CODEC_DEFAULTS: Record<string, Record<string, unknown>> = {
  'fft-512':               { fftSize: 512, smoothing: 0.72 },
  'voice-profile-matching':{ sigmaMultiplier: 3.0, minEnergy: 0.02, emaAlpha: 0.05 },
  'deepgram-nova':         { apiKey: '', model: 'nova-3', language: 'en-US' },
  'webrtc-vad':            { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
}

const CODEC_CATALOGUE: {
  id: string; name: string; category: string; status: 'active' | 'available';
  description: string; tags: string[];
  guide: { step: string; detail: string }[];
  testType: 'mic' | 'api' | 'env' | 'latency' | null;
}[] = [
  {
    id: 'webrtc-vad',
    name: 'WebRTC VAD',
    category: 'Voice Activity Detection',
    status: 'active',
    description: 'Google WebRTC VAD — embedded in the browser AudioContext pipeline. Cuts processing on silence, reducing false wake-word triggers by ~40%.',
    tags: ['noise', 'vad', 'built-in'],
    testType: 'mic',
    guide: [
      { step: 'Already active', detail: 'WebRTC VAD runs automatically inside getUserMedia() — no setup needed. The mic stream is opened with echoCancellation, noiseSuppression and autoGainControl all set to true.' },
      { step: 'Configure constraints', detail: 'The three boolean constraints below are passed directly to getUserMedia(). Toggle any of them and click "Apply & Restart Mic" to see the effect on the frequency spectrum above.' },
      { step: 'Test', detail: 'Click "Test Mic Input" — the energy meter should respond immediately as you speak. If it stays at 0% your browser has blocked the mic (check the address bar lock icon).' },
    ],
  },
  {
    id: 'echo-cancel',
    name: 'AEC (Acoustic Echo Cancellation)',
    category: 'Echo / Feedback',
    status: 'active',
    description: 'Browser-native AEC. Prevents JARVIS TTS from feeding back into the mic and creating ghost transcripts.',
    tags: ['echo', 'feedback', 'built-in'],
    testType: 'mic',
    guide: [
      { step: 'Already active', detail: 'AEC is included in the WebRTC VAD configuration above (echoCancellation: true). The same getUserMedia() call activates it.' },
      { step: 'How it works', detail: 'The browser\'s AEC engine maintains a model of the speaker output and subtracts it from the mic signal in real time. This is why the spectrum shows silence while JARVIS is speaking — the echo is being cancelled before it reaches the FFT.' },
      { step: 'Verify', detail: 'Ask JARVIS a question. While it answers, watch the frequency canvas — bars should stay low. If they spike, your speaker output is louder than the AEC cancellation budget (lower speaker volume or use headphones).' },
    ],
  },
  {
    id: 'noise-suppress',
    name: 'Noise Suppression',
    category: 'Noise Reduction',
    status: 'active',
    description: 'Browser-native NS. Attenuates keyboard, fan and ambient noise before the FFT analyser runs.',
    tags: ['noise', 'denoising', 'built-in'],
    testType: 'mic',
    guide: [
      { step: 'Already active', detail: 'noiseSuppression: true is set in the getUserMedia() constraints alongside AEC and AGC.' },
      { step: 'Compare', detail: 'Toggle noiseSuppression OFF in the WebRTC VAD config, click Apply & Restart, then type loudly on your keyboard while watching the spectrum. You will see much higher mid-frequency energy. Re-enable it to restore noise reduction.' },
      { step: 'Limitation', detail: 'Browser NS is a lightweight statistical filter. For heavier environments (HVAC, open plan office) consider the RNNoise WASM plugin listed below which uses a deep neural network.' },
    ],
  },
  {
    id: 'auto-gain',
    name: 'Automatic Gain Control (AGC)',
    category: 'Level Management',
    status: 'active',
    description: 'Browser-native AGC. Normalises microphone gain so the binary engine sees a consistent 0–255 FFT range regardless of mic distance or hardware.',
    tags: ['gain', 'level', 'built-in'],
    testType: 'mic',
    guide: [
      { step: 'Already active', detail: 'autoGainControl: true normalises the mic stream gain to a comfortable level before it reaches the AnalyserNode.' },
      { step: 'When to disable', detail: 'If you are doing precise voice calibration and the Energy % swings wildly between quiet and loud frames, disable AGC (set autoGainControl to false in WebRTC VAD config). Your raw mic level will then drive the FFT directly.' },
      { step: 'Verify current level', detail: 'Watch the Energy % in the status bar above. With AGC on it should stabilise around 15–35% while you speak at a normal distance from the mic.' },
    ],
  },
  {
    id: 'fft-512',
    name: 'FFT-512 Analyser',
    category: 'Frequency Analysis',
    status: 'active',
    description: 'Web Audio AnalyserNode running at fftSize=512 (256 frequency bins). Extracts 16 normalised bands used by the speaker-ID engine and the binary engine canvas.',
    tags: ['fft', 'spectrum', 'built-in'],
    testType: 'mic',
    guide: [
      { step: 'Configuration', detail: 'fftSize must be a power of 2 between 32 and 32768. Larger values give better frequency resolution but slower time response. 512 (= 256 bins) is optimal for voice commands: good frequency resolution with <12ms time resolution.' },
      { step: 'Smoothing', detail: 'smoothingTimeConstant (0–1) is an exponential moving average on the FFT output. 0 = raw/jittery, 1 = extremely smooth but slow. 0.72 is the default — a good balance for voice detection. Increase to 0.85+ for JARVIS speaker environments where stability matters more than speed.' },
      { step: 'Apply', detail: 'After adjusting fftSize or smoothing, click "Apply & Restart Mic" to recreate the AnalyserNode with the new settings. The frequency canvas will update immediately.' },
    ],
  },
  {
    id: 'voice-profile-matching',
    name: 'Statistical Band-Distance Matcher',
    category: 'Speaker ID',
    status: 'active',
    description: 'Per-band mean ± Nσ comparison against the calibrated voice profile. Rejects TV/background voices. Adapts automatically via EMA with α=0.05 each high-confidence frame.',
    tags: ['speaker-id', 'fingerprint', 'adaptive'],
    testType: 'latency',
    guide: [
      { step: 'How to calibrate', detail: 'Go to the Voice Profile tab and click "Start Calibration". Speak naturally for 3 seconds (reading aloud works best). This captures 60 frames and computes the mean and σ per frequency band.' },
      { step: 'σ Multiplier', detail: 'The tolerance window around each band mean is: tolerance = stdDev × σMultiplier + 0.05. Increase σMultiplier if you get false rejections (your own voice marked as "other"). Decrease it if background voices or TV are being accepted.' },
      { step: 'Min Energy', detail: 'Frames below minEnergy are not scored — only actual speech is evaluated. Lower this if you speak very quietly; raise it if noise is being scored.' },
      { step: 'EMA Alpha', detail: 'α controls how fast the profile adapts. 0.05 (5%) means each high-confidence frame blends in at 5% weight. Lower for a more stable profile; higher for faster adaptation to changed mic/environment.' },
    ],
  },
  {
    id: 'whisper-webgpu',
    name: 'Whisper WebGPU (optional)',
    category: 'High-Accuracy STT',
    status: 'available',
    description: 'OpenAI Whisper small.en running on WebGPU. Replaces browser SpeechRecognition for dramatically better accuracy on trading commands. Requires Chrome 113+ with WebGPU enabled.',
    tags: ['whisper', 'webgpu', 'optional'],
    testType: 'env',
    guide: [
      { step: '1. Check WebGPU support', detail: 'Open chrome://flags and search for "WebGPU". Enable "Unsafe WebGPU". In Chrome 113+ it is on by default. Test: navigate to webgpureport.org — it should show your GPU.' },
      { step: '2. Install transformers.js', detail: 'In frontend/, run: npm install @xenova/transformers. This provides the Whisper model runner compiled to WASM+WebGPU.' },
      { step: '3. Download model (~150 MB)', detail: 'First call to WhisperProcessor will auto-download Xenova/whisper-small.en from HuggingFace. It is cached in the browser\'s IndexedDB. This only happens once.' },
      { step: '4. Replace SpeechRecognition', detail: 'In PaulChat.tsx, the SpeechRecognition instance in startDictation() can be swapped for the Whisper pipeline. See the integration branch at: github.com/tradebot/whisper-integration (not yet merged).' },
      { step: '5. Latency trade-off', detail: 'Whisper is ~300–800ms per utterance (GPU) vs <100ms for browser STT. For JARVIS voice commands this is acceptable — use browser STT for wake word detection and Whisper for the actual command text.' },
    ],
  },
  {
    id: 'deepgram-nova',
    name: 'Deepgram Nova-3',
    category: 'Cloud STT',
    status: 'available',
    description: 'Deepgram Nova-3 cloud API. Ultra-accurate, ~200ms latency. Requires DEEPGRAM_API_KEY in backend .env. Trades privacy for accuracy on financial commands.',
    tags: ['cloud', 'deepgram', 'optional'],
    testType: 'api',
    guide: [
      { step: '1. Get API key', detail: 'Create a free account at console.deepgram.com. Navigate to API Keys → Create a Key. The free tier gives 200 free hours of transcription.' },
      { step: '2. Add to backend .env', detail: 'Open backend/.env and add: DEEPGRAM_API_KEY=your_key_here — then restart the backend: kill $(lsof -ti:1448) && ./run-local.sh backend' },
      { step: '3. Verify backend', detail: 'Click "Test Connection" below — it will call GET /api/v1/jarvis/deepgram/test which validates the key and returns the account balance. A ✓ means the key is valid and the backend sees it.' },
      { step: '4. Enable in PaulChat', detail: 'In PaulChat.tsx set aiVoiceEnabled=true and configure the voice backend to use Deepgram. The voice input pipeline will route dictation recordings to the Deepgram Nova-3 streaming endpoint.' },
      { step: '5. Privacy note', detail: 'Audio is sent to Deepgram servers in real time. Do not enable this if you are dictating sensitive financial information or account credentials to JARVIS.' },
    ],
  },
  {
    id: 'rnnoise',
    name: 'RNNoise (WASM)',
    category: 'ML Noise Reduction',
    status: 'available',
    description: 'Mozilla RNNoise recurrent neural network noise suppressor compiled to WASM. Dramatically reduces keyboard/HVAC noise before the FFT stage, improving speaker-ID accuracy.',
    tags: ['ml', 'wasm', 'noise', 'optional'],
    testType: 'env',
    guide: [
      { step: '1. Install', detail: 'In frontend/, run: npm install rnnoise-wasm. The package bundles a pre-compiled WASM binary (~130 KB) — no build step needed.' },
      { step: '2. Create AudioWorklet', detail: 'RNNoise runs as an AudioWorkletProcessor that sits between the MediaStreamSource and the AnalyserNode. Add src/worklets/rnnoise-processor.ts which loads the WASM and processes 480-sample frames.' },
      { step: '3. Wire into binary-engine', detail: 'In startDirectMic(), insert the worklet node between ctx.createMediaStreamSource(stream) and the analyser: source → rnnoise → analyser. The worklet reduces noise energy by 10–40 dB.' },
      { step: '4. Test', detail: 'Type loudly on your keyboard while watching the Energy %. With browser NS only, you may see 5–15% energy. With RNNoise added, keyboard energy should drop to 1–3% while voice stays at 20–50%.' },
      { step: '5. Latency', detail: 'RNNoise adds ~10ms of latency (one 480-sample frame at 48 kHz). This is negligible for voice interaction but do not use it for real-time musical applications.' },
    ],
  },
  {
    id: 'pyannote',
    name: 'Pyannote Speaker Diarisation',
    category: 'Speaker Diarisation',
    status: 'available',
    description: 'Pyannote.audio via backend Python. Identifies how many distinct speakers are present and which segments belong to which speaker — enabling multi-user JARVIS sessions.',
    tags: ['diarisation', 'python', 'optional'],
    testType: 'api',
    guide: [
      { step: '1. Install Python deps', detail: 'In backend/, run: pip install pyannote.audio torchaudio. Pyannote requires Python 3.10+ and PyTorch. The models are ~1 GB and auto-download on first use.' },
      { step: '2. Get HuggingFace token', detail: 'The pyannote/speaker-diarization-3.1 model requires accepting its license at huggingface.co/pyannote/speaker-diarization-3.1. Then get an access token from huggingface.co/settings/tokens.' },
      { step: '3. Add to backend .env', detail: 'Add HUGGINGFACE_TOKEN=hf_your_token_here to backend/.env. The backend diarisation endpoint at /api/v1/jarvis/diarise reads this at startup.' },
      { step: '4. Record and diarise', detail: 'Click "Test Diarisation" below — it will record 10 seconds of mic audio, send it to the backend, and show a timeline of detected speaker segments. If multiple people are talking you will see SPEAKER_00, SPEAKER_01 etc.' },
      { step: '5. Multi-user mode', detail: 'Once calibrated, JARVIS can maintain separate voice profiles per speaker and only respond to the user whose profile matches — preventing accidental wake by other people in the room.' },
    ],
  },
]

const BAND_LABELS = ['63', '125', '250', '500', '1k', '2k', '4k', '8k',
                     '12k', '16k', '20k', '22k', 'env', 'env', 'env', 'env']

// ── Main component ─────────────────────────────────────────────────────────────
export default function BinaryEnginePage() {
  // Extension connection state (supplementary — page works without it)
  const [extConnected, setExtConnected]   = useState(false)
  const [extListening, setExtListening]   = useState(false)
  const [extSpeaking, setExtSpeaking]     = useState(false)

  // Microphone / AudioContext status
  const [micActive, setMicActive]         = useState(false)
  const [micError, setMicError]           = useState<string | null>(null)
  const [micPerm, setMicPerm]             = useState<'prompt' | 'granted' | 'denied'>('prompt')

  // Real-time frequency data (updated by page's own AudioContext at ~30fps)
  const [freqBands, setFreqBands]         = useState<number[]>(Array(16).fill(0))
  const [energy, setEnergy]               = useState(0)
  const [isUserVoice, setIsUserVoice]     = useState(false)

  // Voice Brain (backend)
  const [brain, setBrain]                 = useState<BrainState>({
    loaded: false, words: {}, profile: null, sessions: 0, lastSync: null,
  })
  const [identifyResult, setIdentifyResult] = useState<IdentifyResult | null>(null)
  const [syncing, setSyncing]             = useState(false)
  const [syncMsg, setSyncMsg]             = useState('')

  // Calibration
  const [calibrating, setCalibrating]     = useState(false)
  const [calibFrames, setCalibFrames]     = useState<number[][]>([])
  const [calibProgress, setCalibProgress] = useState(0)
  const CALIB_FRAMES = 60  // ~3 seconds at 20fps

  // Codec filter
  const [codecSearch, setCodecSearch]     = useState('')
  const [codecFilter, setCodecFilter]     = useState<'all' | 'active' | 'available'>('all')
  const [expandedCodec, setExpandedCodec] = useState<string | null>(null)
  // SSR-safe: initialise with deterministic defaults; hydrate from localStorage
  // in a post-mount effect to avoid a hydration mismatch.
  const [codecConfigs, setCodecConfigs]   = useState<Record<string, Record<string, unknown>>>({})
  const [codecTestState, setCodecTestState] = useState<Record<string, 'idle' | 'testing' | 'ok' | 'fail'>>({})
  const [codecTestMsg,   setCodecTestMsg]   = useState<Record<string, string>>({})

  // Vocab
  const [vocabSearch, setVocabSearch]     = useState('')
  const [activeTab, setActiveTab]         = useState<'engine' | 'profile' | 'vocab' | 'codecs' | 'deepgram'>('engine')

  // ── Deepgram Voice Agent tab state ────────────────────────────────────────
  const dg = useDeepgramAgent()
  const [dgSttModel,   setDgSttModel]   = useState('nova-3')
  const [dgLlmType,    setDgLlmType]    = useState('open_ai')
  const [dgLlmModel,   setDgLlmModel]   = useState('gpt-4o-mini')
  const [dgTtsModel,   setDgTtsModel]   = useState('aura-2-thalia-en')
  // Prefill JARVIS's persona prompt + greeting as built-in (editable) defaults so
  // the Voice Agent follows JARVIS's speech rules out of the box.
  const [dgPrompt,     setDgPrompt]     = useState(JARVIS_DEFAULT_PROMPT)
  const [dgGreeting,   setDgGreeting]   = useState(JARVIS_DEFAULT_GREETING)
  const [dgEot,        setDgEot]        = useState(0.7)
  const [dgEagerEot,   setDgEagerEot]   = useState(0)
  const [dgEotMs,      setDgEotMs]      = useState(5000)
  // SSR-safe: initialise with false; hydrate from localStorage post-mount.
  const [dgUpgradePaul, setDgUpgradePaul] = useState(false)
  const dgCanvasRef = useRef<HTMLCanvasElement>(null)
  const dgRafRef    = useRef<number>(0)

  // ── Deepgram account/profile + key health ─────────────────────────────────
  const [dgStatus, setDgStatus]               = useState<DeepgramStatus | null>(null)
  const [dgStatusLoading, setDgStatusLoading] = useState(false)
  const [dgStatusError, setDgStatusError]     = useState<string | null>(null)
  // Fallback budget usage — surfaced here to discourage draining the cap with
  // the far more expensive (~$0.08/min) Voice Agent.
  const [dgUsage, setDgUsage] = useState<{
    remaining: number; monthly_cap: number; projected_runway_days: number | null
  } | null>(null)
  const fetchDgUsage = useCallback(async () => {
    try {
      const u = await apiClient.deepgram.usage()
      setDgUsage({ remaining: u.remaining, monthly_cap: u.monthly_cap, projected_runway_days: u.projected_runway_days })
    } catch { /* backend offline / not configured → hide the figure */ }
  }, [])

  const fetchDgStatus = useCallback(async () => {
    setDgStatusLoading(true)
    setDgStatusError(null)
    try {
      const res = await apiClient.deepgram.status()
      setDgStatus(res.data as DeepgramStatus)
    } catch (e: any) {
      const detail = e?.response?.data?.detail || e?.response?.data?.error
      setDgStatusError(
        detail ||
        (e?.message?.includes('Network') ? 'Backend not running — start the backend, then retry.' : null) ||
        e?.message ||
        'Failed to load Deepgram account'
      )
      setDgStatus(null)
    } finally {
      setDgStatusLoading(false)
    }
  }, [])

  // Fetch on tab open
  useEffect(() => {
    if (activeTab === 'deepgram') { void fetchDgStatus(); void fetchDgUsage() }
  }, [activeTab, fetchDgStatus, fetchDgUsage])

  // Refresh once a session reaches "connected" so credits/health stay current
  useEffect(() => {
    if (dg.state === 'connected') { void fetchDgStatus(); void fetchDgUsage() }
  }, [dg.state, fetchDgStatus, fetchDgUsage])

  // Draw agent audio FFT on the Deepgram canvas
  useEffect(() => {
    if (activeTab !== 'deepgram') { cancelAnimationFrame(dgRafRef.current); return }
    const draw = () => {
      dgRafRef.current = requestAnimationFrame(draw)
      const canvas = dgCanvasRef.current
      if (!canvas) return
      const ctx = canvas.getContext('2d')
      if (!ctx) return
      const data = dg.getOutputFreqData()
      const W = canvas.width; const H = canvas.height
      ctx.clearRect(0, 0, W, H)
      const bars = 16; const bw = W / bars
      for (let i = 0; i < bars; i++) {
        const v = data ? (data[Math.floor(i * data.length / bars)] ?? 0) / 255 : 0
        const h = Math.max(4, v * H)
        const g = ctx.createLinearGradient(0, H - h, 0, H)
        g.addColorStop(0, dg.isSpeaking ? '#f59e0b' : '#06b6d4')
        g.addColorStop(1, '#0e7490')
        ctx.fillStyle = g
        ctx.beginPath()
        ctx.roundRect(i * bw + 2, H - h, bw - 4, h, [3, 3, 0, 0])
        ctx.fill()
      }
    }
    draw()
    return () => cancelAnimationFrame(dgRafRef.current)
  }, [activeTab, dg.isSpeaking, dg.getOutputFreqData])

  // Hydrate localStorage-backed state AFTER mount so the first client render
  // matches the server HTML (prevents hydration mismatch for codecConfigs and
  // dgUpgradePaul which must start with deterministic SSR-safe defaults).
  useEffect(() => {
    try {
      const storedCodecs = localStorage.getItem('paul.codecConfigs')
      if (storedCodecs) setCodecConfigs(JSON.parse(storedCodecs))
      setDgUpgradePaul(localStorage.getItem('jarvis.deepgramMode') === 'true')
    } catch { /* ignore private-mode / quota errors */ }
  }, [])

  // Sync "Upgrade PaulChat" toggle to localStorage
  useEffect(() => {
    try { localStorage.setItem('jarvis.deepgramMode', dgUpgradePaul ? 'true' : 'false') } catch {}
  }, [dgUpgradePaul])

  const canvasRef      = useRef<HTMLCanvasElement>(null)
  const rafRef         = useRef<number>(0)
  const freqRef        = useRef<number[]>(Array(16).fill(0))
  const energyRef      = useRef<number>(0)
  const identifyTimerRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  // ── Direct page AudioContext (works without extension) ──────────────────────
  const pageCtxRef     = useRef<AudioContext | null>(null)
  const pageAnalyserRef= useRef<AnalyserNode | null>(null)
  const pageBufRef     = useRef<Uint8Array | null>(null)
  const pageStreamRef  = useRef<MediaStream | null>(null)
  const stateThrottle  = useRef<number>(0)  // throttle React state updates to ~15fps

  // ── Extension message listener ──────────────────────────────────────────────
  useEffect(() => {
    const handler = (evt: MessageEvent) => {
      if (!evt.data?.__jarvisExt) return
      const d = evt.data
      if (d.type === 'connected' || d.type === 'status') {
        setExtConnected(true)
        setExtListening(!!d.listening)
      }
      if (d.type === 'voice-freq-page') {
        const bands: number[] = d.bands || Array(16).fill(0)
        freqRef.current = bands
        setFreqBands(bands)
        setEnergy(d.energy ?? 0)
        setIsUserVoice(!!d.isUserVoice)
        setExtSpeaking(!!d.isSpeaking)
      }
      if (d.type === 'speak-status') {
        setExtSpeaking(!!d.speaking)
      }
      if (d.type === 'status') {
        setExtListening(!!d.listening)
      }
    }
    window.addEventListener('message', handler)
    // Probe the extension
    window.postMessage({ __jarvisPage: true, type: 'ping' }, window.location.origin)
    return () => window.removeEventListener('message', handler)
  }, [])

  // ── Direct microphone AudioContext ──────────────────────────────────────────
  // This runs completely independently of the extension.
  // It gives true real-time frequency data whenever the user speaks.
  const startDirectMic = useCallback(async () => {
    if (pageCtxRef.current) return  // already running
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, sampleRate: 44100 }
      })
      pageStreamRef.current = stream
      const ctx = new (window.AudioContext || (window as any).webkitAudioContext)()
      const analyser = ctx.createAnalyser()
      analyser.fftSize = 512
      analyser.smoothingTimeConstant = 0.72
      ctx.createMediaStreamSource(stream).connect(analyser)
      pageCtxRef.current      = ctx
      pageAnalyserRef.current  = analyser
      pageBufRef.current       = new Uint8Array(analyser.frequencyBinCount)
      setMicActive(true)
      setMicError(null)
      setMicPerm('granted')
    } catch (err: any) {
      const msg = err?.name === 'NotAllowedError' ? 'Microphone access denied — click Allow in your browser' : String(err)
      setMicError(msg)
      setMicPerm(err?.name === 'NotAllowedError' ? 'denied' : 'prompt')
      setMicActive(false)
    }
  }, [])

  const stopDirectMic = useCallback(() => {
    pageStreamRef.current?.getTracks().forEach(t => t.stop())
    try { pageCtxRef.current?.close() } catch { /* noop */ }
    pageCtxRef.current = null; pageAnalyserRef.current = null; pageBufRef.current = null
    setMicActive(false)
  }, [])

  // Auto-start mic on mount; retry on first user interaction if auto-blocked
  useEffect(() => {
    startDirectMic()
    const onInteract = () => { if (!pageCtxRef.current) startDirectMic() }
    window.addEventListener('pointerdown', onInteract, { once: true, passive: true })
    window.addEventListener('keydown',     onInteract, { once: true })
    return () => {
      window.removeEventListener('pointerdown', onInteract)
      window.removeEventListener('keydown', onInteract)
      stopDirectMic()
    }
  }, [startDirectMic, stopDirectMic])

  // Extension supplementary relay (still useful for speaker-ID state)
  useEffect(() => {
    const t = setInterval(() => {
      if (extConnected) {
        window.postMessage({ __jarvisPage: true, type: 'voice-freq-request' }, window.location.origin)
      }
    }, 200)
    return () => clearInterval(t)
  }, [extConnected])

  // ── Unified analyser + canvas RAF loop ─────────────────────────────────────
  // Reads directly from the page's own AudioContext analyser every frame.
  // No extension required — works as long as the mic is granted.
  const extSpeakingRef  = useRef(false)
  const isUserVoiceRef  = useRef(false)
  useEffect(() => { extSpeakingRef.current = extSpeaking  }, [extSpeaking])
  useEffect(() => { isUserVoiceRef.current = isUserVoice  }, [isUserVoice])

  useEffect(() => {
    const BANDS = 16
    const tick = () => {
      rafRef.current = requestAnimationFrame(tick)

      // ── 1. Read frequency from page's own AudioContext (primary source) ────
      if (pageAnalyserRef.current && pageBufRef.current) {
        pageAnalyserRef.current.getByteFrequencyData(pageBufRef.current as Uint8Array<ArrayBuffer>)
        const buf = pageBufRef.current
        const binSize = Math.floor(buf.length / BANDS)
        const raw = Array.from({ length: BANDS }, (_, b) => {
          let s = 0
          for (let j = b * binSize; j < Math.min((b + 1) * binSize, buf.length); j++) s += buf[j]
          return s / binSize
        })
        const mx = Math.max(...raw, 1)
        freqRef.current  = raw.map(v => v / mx)
        energyRef.current = raw.reduce((a, v) => a + v, 0) / raw.length / 255

        // Throttle React state updates to ~15fps to avoid excessive re-renders
        const now = Date.now()
        if (now - stateThrottle.current > 66) {
          stateThrottle.current = now
          setFreqBands([...freqRef.current])
          setEnergy(energyRef.current)
        }
      }

      // ── 2. Draw canvas ───────────────────────────────────────────────────────
      const canvas = canvasRef.current
      if (!canvas) return
      const ctx = canvas.getContext('2d')
      if (!ctx) return
      const W = canvas.width, H = canvas.height
      ctx.clearRect(0, 0, W, H)

      const bands = freqRef.current
      const BAR_W = Math.floor((W - BANDS * 2) / BANDS)
      const CELL_H = 6, CELL_GAP = 1
      const totalCells = Math.floor((H + CELL_GAP) / (CELL_H + CELL_GAP))

      const isSpeakingNow = extSpeakingRef.current
      const userVoice     = isUserVoiceRef.current
      const eng           = energyRef.current

      let activeColor = '#1e293b'
      if (isSpeakingNow)           activeColor = '#f59e0b'
      else if (userVoice && eng > 0.01) activeColor = '#06b6d4'
      else if (eng > 0.01)         activeColor = '#8b5cf6'

      bands.forEach((band, i) => {
        const x = i * (BAR_W + 2) + 1
        const litCells = Math.round(band * totalCells)
        for (let c = 0; c < totalCells; c++) {
          const y = H - (c + 1) * (CELL_H + CELL_GAP) + CELL_GAP
          ctx.fillStyle = c < litCells ? activeColor : '#0f172a'
          ctx.fillRect(x, y, BAR_W, CELL_H)
        }
      })

      // Band labels
      ctx.fillStyle = '#334155'
      ctx.font = '8px monospace'
      ctx.textAlign = 'center'
      BAND_LABELS.forEach((lbl, i) => {
        const x = i * (BAR_W + 2) + 1 + BAR_W / 2
        ctx.fillText(lbl, x, H - 2)
      })
    }
    tick()
    return () => cancelAnimationFrame(rafRef.current)
  // Only re-create the loop if the canvas mounts
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // ── Load brain from backend ─────────────────────────────────────────────────
  const loadBrain = useCallback(async () => {
    try {
      const res = await apiClient.jarvis.voiceBrainLoad()
      const d = res.data
      setBrain({
        loaded: true,
        words: d.vocabulary || {},
        profile: d.profile || null,
        sessions: d.sessions || 0,
        lastSync: d.updated_at || null,
      })
    } catch (e) {
      console.warn('Brain load failed', e)
    }
  }, [])

  useEffect(() => { loadBrain() }, [loadBrain])

  // ── Sync brain to backend ───────────────────────────────────────────────────
  const syncBrain = useCallback(async () => {
    setSyncing(true)
    setSyncMsg('')
    try {
      // Get vocabulary from localStorage (paul.learnedWords.v1)
      let vocab: Record<string, number> = {}
      try {
        const raw = localStorage.getItem('paul.learnedWords.v1')
        if (raw) vocab = JSON.parse(raw)
      } catch { /* ignore */ }

      // Get voice profile from localStorage
      let profile: VoiceProfile | null = null
      try {
        const raw = localStorage.getItem('paul.voiceProfile')
        if (raw) profile = JSON.parse(raw)
      } catch { /* ignore */ }

      const res = await apiClient.jarvis.voiceBrainSync({
        vocabulary: vocab,
        profile: (profile as any) || undefined,
        sessions: brain.sessions + 1,
      })
      setSyncMsg(`✓ Synced — ${res.data.words_total} words, ${res.data.sessions} sessions`)
      await loadBrain()
    } catch (e: any) {
      setSyncMsg(`✗ Sync failed: ${e.message}`)
    }
    setSyncing(false)
  }, [brain.sessions, loadBrain])

  // ── Live identify (auto) ────────────────────────────────────────────────────
  useEffect(() => {
    if (!brain.profile || energy < 0.02) return  // works with or without extension
    clearTimeout(identifyTimerRef.current)
    identifyTimerRef.current = setTimeout(async () => {
      if (freqRef.current.every(v => v === 0)) return
      try {
        const res = await apiClient.jarvis.voiceBrainIdentify(freqRef.current.slice(0, 12))
        setIdentifyResult(res.data)
      } catch { /* silent */ }
    }, 300)
    return () => clearTimeout(identifyTimerRef.current)
  }, [freqBands, extConnected, brain.profile, energy])

  // ── Calibration ─────────────────────────────────────────────────────────────
  const startCalibration = useCallback(() => {
    setCalibrating(true)
    setCalibFrames([])
    setCalibProgress(0)
  }, [])

  useEffect(() => {
    if (!calibrating) return
    if (energy < 0.02) return  // wait for voice activity
    const bands = freqRef.current.slice()
    setCalibFrames(prev => {
      const next = [...prev, bands]
      setCalibProgress(Math.min(1, next.length / CALIB_FRAMES))
      if (next.length >= CALIB_FRAMES) {
        // Compute mean and std-dev per band
        const nBands = next[0].length
        const meanBands = Array(nBands).fill(0)
        for (const frame of next) {
          for (let b = 0; b < nBands; b++) meanBands[b] += (frame[b] ?? 0)
        }
        for (let b = 0; b < nBands; b++) meanBands[b] /= next.length
        const stdBands = Array(nBands).fill(0)
        for (const frame of next) {
          for (let b = 0; b < nBands; b++) {
            const dev = (frame[b] ?? 0) - meanBands[b]
            stdBands[b] += dev * dev
          }
        }
        for (let b = 0; b < nBands; b++) stdBands[b] = Math.sqrt(stdBands[b] / next.length)
        const centroid = meanBands.reduce((s, v, i) => s + v * i, 0) / (meanBands.reduce((s, v) => s + v, 0.001) * nBands)
        const minEnergy = next.reduce((s, fr) => s + fr.reduce((a, v) => a + v, 0) / fr.length, 0) / next.length

        const newProfile: VoiceProfile & { minEnergy: number } = {
          bands: meanBands,
          bandStdDev: stdBands,
          centroid,
          minEnergy,
          sessions: (brain.sessions ?? 0) + 1,
          calibratedAt: Date.now(),
        }
        try { localStorage.setItem('paul.voiceProfile', JSON.stringify(newProfile)) } catch { /* ignore */ }
        setBrain(b => ({ ...b, profile: newProfile }))
        setCalibrating(false)
        setSyncMsg('✓ Voice profile calibrated — click Sync Brain to persist it.')
      }
      return next
    })
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [freqBands, calibrating, energy])

  const clearProfile = useCallback(() => {
    localStorage.removeItem('paul.voiceProfile')
    setBrain(b => ({ ...b, profile: null }))
    setIdentifyResult(null)
    setSyncMsg('Profile cleared from local storage.')
  }, [])

  // ── Render helpers ──────────────────────────────────────────────────────────
  const visColorClass = extSpeaking
    ? 'text-amber-400'
    : isUserVoice && energy > 0.01
      ? 'text-cyan-400'
      : energy > 0.01
        ? 'text-purple-400'
        : 'text-slate-500'

  const statusLabel = extSpeaking
    ? 'JARVIS SPEAKING'
    : !micActive
      ? 'MIC NOT STARTED'
      : energy > 0.05 && isUserVoice
        ? 'YOUR VOICE DETECTED'
        : energy > 0.02
          ? 'AUDIO DETECTED'
          : 'LISTENING…'

  const filteredCodecs = CODEC_CATALOGUE.filter(c => {
    const matchSearch = !codecSearch || c.name.toLowerCase().includes(codecSearch.toLowerCase()) ||
      c.description.toLowerCase().includes(codecSearch.toLowerCase()) ||
      c.tags.some(t => t.includes(codecSearch.toLowerCase()))
    const matchFilter = codecFilter === 'all' || c.status === codecFilter
    return matchSearch && matchFilter
  })

  // ── Codec config helpers ──────────────────────────────────────────────────
  const getCodecCfg = (id: string): Record<string, unknown> => ({
    ...(CODEC_DEFAULTS[id] ?? {}),
    ...(codecConfigs[id] ?? {}),
  })

  const saveCodecCfg = (id: string, patch: Record<string, unknown>) => {
    const next = { ...codecConfigs, [id]: { ...getCodecCfg(id), ...patch } }
    setCodecConfigs(next)
    localStorage.setItem('paul.codecConfigs', JSON.stringify(next))
  }

  const testCodec = async (id: string) => {
    setCodecTestState(s => ({ ...s, [id]: 'testing' }))
    setCodecTestMsg(m => ({ ...m, [id]: '' }))
    try {
      if (id === 'deepgram-nova') {
        const key = (getCodecCfg(id) as { apiKey?: string }).apiKey ?? ''
        if (!key) { setCodecTestState(s => ({ ...s, [id]: 'fail' })); setCodecTestMsg(m => ({ ...m, [id]: 'No API key set.' })); return }
        const res = await fetch('/api/v1/jarvis/deepgram/test', {
          method: 'GET',
          headers: { 'X-Deepgram-Key': key },
        })
        if (res.ok) {
          const j = await res.json().catch(() => ({})) as { balance?: number; email?: string }
          setCodecTestState(s => ({ ...s, [id]: 'ok' }))
          setCodecTestMsg(m => ({ ...m, [id]: `Connected — ${j.email ?? 'account ok'}${j.balance != null ? ` · balance $${j.balance.toFixed(2)}` : ''}` }))
        } else {
          setCodecTestState(s => ({ ...s, [id]: 'fail' }))
          setCodecTestMsg(m => ({ ...m, [id]: `HTTP ${res.status} — check API key` }))
        }
      } else if (id === 'pyannote') {
        const res = await fetch('/api/v1/jarvis/diarise/test', { method: 'GET' })
        if (res.ok) {
          setCodecTestState(s => ({ ...s, [id]: 'ok' }))
          setCodecTestMsg(m => ({ ...m, [id]: 'Backend diarisation endpoint is ready.' }))
        } else {
          setCodecTestState(s => ({ ...s, [id]: 'fail' }))
          setCodecTestMsg(m => ({ ...m, [id]: `Endpoint returned HTTP ${res.status}. Check HUGGINGFACE_TOKEN in backend/.env.` }))
        }
      } else if (id === 'whisper-webgpu' || id === 'rnnoise') {
        const gpuAvail = !!(navigator as { gpu?: unknown }).gpu
        if (id === 'whisper-webgpu') {
          setCodecTestState(s => ({ ...s, [id]: gpuAvail ? 'ok' : 'fail' }))
          setCodecTestMsg(m => ({ ...m, [id]: gpuAvail ? 'WebGPU is available in this browser ✓' : 'WebGPU not detected. Enable it at chrome://flags/#enable-unsafe-webgpu or upgrade Chrome.' }))
        } else {
          const wasmAvail = typeof WebAssembly !== 'undefined'
          setCodecTestState(s => ({ ...s, [id]: wasmAvail ? 'ok' : 'fail' }))
          setCodecTestMsg(m => ({ ...m, [id]: wasmAvail ? 'WASM is available — install rnnoise-wasm and wire the AudioWorklet ✓' : 'WASM not supported in this browser.' }))
        }
      } else {
        // Active built-in codecs — test mic energy
        const e = energyRef.current
        if (e < 0.01) {
          setCodecTestState(s => ({ ...s, [id]: 'fail' }))
          setCodecTestMsg(m => ({ ...m, [id]: 'Mic energy is 0% — allow mic access and speak.' }))
        } else {
          setCodecTestState(s => ({ ...s, [id]: 'ok' }))
          setCodecTestMsg(m => ({ ...m, [id]: `Mic active — energy ${(e * 100).toFixed(0)}%. ${id === 'voice-profile-matching' ? `Speaker match: ${isUserVoice ? '✓ YOUR VOICE' : '✗ not matched'}` : 'Running normally ✓'}` }))
        }
      }
    } catch (err: unknown) {
      setCodecTestState(s => ({ ...s, [id]: 'fail' }))
      setCodecTestMsg(m => ({ ...m, [id]: String(err instanceof Error ? err.message : err) }))
    }
  }

  const sortedVocab = Object.entries(brain.words)
    .filter(([w]) => !vocabSearch || w.includes(vocabSearch.toLowerCase()))
    .sort((a, b) => b[1] - a[1])

  // ── JSX ─────────────────────────────────────────────────────────────────────
  return (
    <>
      <Head>
        <title>Binary Engine Studio — JARVIS Voice</title>
      </Head>

      <div className="p-4 max-w-7xl mx-auto space-y-4">

        {/* ── Page Header ── */}
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-cyan-500 to-violet-600 flex items-center justify-center shadow-lg">
              <AudioWaveform className="w-5 h-5 text-white" />
            </div>
            <div>
              <h1 className="text-xl font-bold text-white">Binary Engine Studio</h1>
              <p className="text-xs text-slate-400">Voice Recognition · Speaker ID · Real-time Frequency Analysis</p>
            </div>
          </div>

          {/* Extension status badge */}
          <div className={`flex items-center gap-2 px-3 py-1.5 rounded-full text-xs font-medium border ${
            extConnected ? 'bg-cyan-950/40 border-cyan-700/50 text-cyan-300' : 'bg-slate-800 border-slate-700 text-slate-400'
          }`}>
            {extConnected ? <Wifi className="w-3.5 h-3.5" /> : <WifiOff className="w-3.5 h-3.5" />}
            {extConnected ? 'Extension connected' : 'Extension not detected'}
          </div>
        </div>

        {/* ── Status Bar ── */}
        <div className="bg-slate-900/80 border border-slate-700/50 rounded-xl px-4 py-2.5 flex items-center gap-4 text-xs flex-wrap">
          {/* Mic status pill */}
          {micActive ? (
            <span className="flex items-center gap-1.5 font-semibold text-emerald-400">
              <Mic className="w-3 h-3" />
              <span>MIC LIVE</span>
            </span>
          ) : micError ? (
            <button
              onClick={startDirectMic}
              className="flex items-center gap-1.5 px-2.5 py-1 bg-red-900/40 border border-red-700/50 rounded-lg text-red-300 hover:bg-red-800/40 transition"
            >
              <MicOff className="w-3 h-3" />
              {micPerm === 'denied' ? 'Mic denied — click to retry' : 'Grant mic access'}
            </button>
          ) : (
            <button
              onClick={startDirectMic}
              className="flex items-center gap-1.5 px-2.5 py-1 bg-cyan-900/30 border border-cyan-700/40 rounded-lg text-cyan-300 hover:bg-cyan-800/30 transition"
            >
              <Mic className="w-3 h-3" />
              Start microphone
            </button>
          )}

          <span className={`flex items-center gap-1.5 font-semibold tracking-wide ${visColorClass}`}>
            <span className={`w-2 h-2 rounded-full ${
              extSpeaking ? 'bg-amber-400 shadow-[0_0_8px_#f59e0b]' :
              isUserVoice && energy > 0.01 ? 'bg-cyan-400 shadow-[0_0_8px_#06b6d4]' :
              energy > 0.01 ? 'bg-purple-400 animate-pulse' : 'bg-slate-600'
            }`} />
            {statusLabel}
          </span>
          <span className="text-slate-500">Energy: <span className={energy > 0.02 ? 'text-cyan-300 font-mono' : 'text-slate-400'}>{(energy * 100).toFixed(1)}%</span></span>
          <span className="text-slate-500">Brain: <span className={brain.loaded ? 'text-emerald-400' : 'text-slate-500'}>{brain.loaded ? `${Object.keys(brain.words).length} words` : 'not loaded'}</span></span>
          {identifyResult && (
            <span className="text-slate-500">
              Match: <span className={identifyResult.match ? 'text-emerald-400 font-semibold' : 'text-red-400'}>
                {(identifyResult.confidence * 100).toFixed(1)}% {identifyResult.match ? '✓' : '✗'}
              </span>
            </span>
          )}
          {micError && micPerm !== 'denied' && (
            <span className="text-amber-400 text-xs">{micError}</span>
          )}
          {brain.lastSync && (
            <span className="text-slate-500 ml-auto">Synced: <span className="text-slate-300">{new Date(brain.lastSync).toLocaleDateString()}</span></span>
          )}
        </div>

        {/* ── Tabs ── */}
        <div className="flex gap-1 bg-slate-900/60 rounded-xl p-1 border border-slate-700/50">
          {([
            { id: 'engine', label: 'Binary Engine', icon: Activity },
            { id: 'profile', label: 'Voice Profile', icon: Shield },
            { id: 'vocab', label: 'Vocabulary', icon: Brain },
            { id: 'codecs', label: 'Codecs & Plugins', icon: Cpu },
            { id: 'deepgram', label: 'Deepgram Agent', icon: Wifi },
          ] as { id: typeof activeTab; label: string; icon: React.FC<{className?: string}> }[]).map(tab => (
            <button
              key={tab.id}
              onClick={() => setActiveTab(tab.id)}
              className={`flex items-center gap-2 px-4 py-2 rounded-lg text-xs font-medium transition-all ${
                activeTab === tab.id
                  ? 'bg-cyan-600/20 text-cyan-300 border border-cyan-500/30'
                  : 'text-slate-400 hover:text-white hover:bg-slate-800/60'
              }`}
            >
              <tab.icon className="w-3.5 h-3.5" />
              {tab.label}
            </button>
          ))}
        </div>

        {/* ═══════════════════════════════════════════════════════════════════ */}
        {/*  TAB: Binary Engine                                                 */}
        {/* ═══════════════════════════════════════════════════════════════════ */}
        {activeTab === 'engine' && (
          <div className="grid grid-cols-1 xl:grid-cols-3 gap-4">

            {/* Main canvas */}
            <div className="xl:col-span-2 bg-slate-900/70 border border-slate-700/50 rounded-xl p-4 space-y-3">
              <div className="flex items-center justify-between">
                <h2 className="text-sm font-semibold text-white flex items-center gap-2">
                  <BarChart2 className="w-4 h-4 text-cyan-400" />
                  Real-time Frequency Spectrum
                </h2>
                <span className="text-xs text-slate-500 font-mono">16-band FFT @ 20fps</span>
              </div>

              <canvas
                ref={canvasRef}
                width={680}
                height={180}
                className="w-full rounded-lg bg-[#020617] border border-slate-800"
              />

              {/* Band energy meters */}
              <div className="grid grid-cols-16 gap-0.5 mt-1">
                {freqBands.map((v, i) => (
                  <div key={i} className="flex flex-col items-center gap-0.5">
                    <div className="w-full h-1 rounded-full bg-slate-800 overflow-hidden">
                      <div
                        className={`h-full rounded-full transition-all duration-75 ${
                          extSpeaking ? 'bg-amber-400' : isUserVoice ? 'bg-cyan-400' : 'bg-purple-500'
                        }`}
                        style={{ width: `${v * 100}%` }}
                      />
                    </div>
                    <span className="text-[8px] text-slate-600">{BAND_LABELS[i]}</span>
                  </div>
                ))}
              </div>

              {/* Legend */}
              <div className="flex gap-4 pt-1 text-xs text-slate-400 border-t border-slate-800">
                <span className="flex items-center gap-1.5"><span className="w-3 h-3 rounded-sm bg-cyan-400/80" />Your voice</span>
                <span className="flex items-center gap-1.5"><span className="w-3 h-3 rounded-sm bg-violet-500/80" />Other audio</span>
                <span className="flex items-center gap-1.5"><span className="w-3 h-3 rounded-sm bg-amber-400/80" />JARVIS speaking</span>
                <span className="flex items-center gap-1.5"><span className="w-3 h-3 rounded-sm bg-slate-700" />Silence</span>
              </div>
            </div>

            {/* Side panel: controls + identify */}
            <div className="space-y-4">
              {/* Identify card */}
              <div className="bg-slate-900/70 border border-slate-700/50 rounded-xl p-4 space-y-3">
                <h2 className="text-sm font-semibold text-white flex items-center gap-2">
                  <Shield className="w-4 h-4 text-emerald-400" />
                  Speaker Identification
                </h2>

                {brain.profile ? (
                  <>
                    <div className={`text-center py-3 rounded-lg border ${
                      identifyResult?.match
                        ? 'bg-emerald-950/40 border-emerald-700/50 text-emerald-300'
                        : identifyResult
                          ? 'bg-red-950/40 border-red-700/50 text-red-300'
                          : 'bg-slate-800/40 border-slate-700 text-slate-400'
                    }`}>
                      <div className="text-3xl font-bold font-mono">
                        {identifyResult ? `${(identifyResult.confidence * 100).toFixed(1)}%` : '—'}
                      </div>
                      <div className="text-xs mt-1">
                        {identifyResult
                          ? identifyResult.match ? '✓ Your voice' : '✗ Not your voice'
                          : 'Speak to identify'}
                      </div>
                    </div>

                    {brain.profile.bands && (
                      <div className="space-y-1">
                        <p className="text-xs text-slate-500">Profile fingerprint ({brain.profile.bands.length} bands)</p>
                        <div className="flex gap-0.5 h-8">
                          {brain.profile.bands.map((v, i) => (
                            <div key={i} className="flex-1 bg-slate-800 rounded-sm overflow-hidden flex items-end">
                              <div
                                className="w-full bg-cyan-600/60 rounded-sm"
                                style={{ height: `${v * 100}%` }}
                              />
                            </div>
                          ))}
                        </div>
                        <p className="text-xs text-slate-600">
                          Centroid: {brain.profile.centroid?.toFixed(3) || 'n/a'} ·
                          Sessions: {brain.sessions}
                        </p>
                      </div>
                    )}

                    <button
                      onClick={clearProfile}
                      className="w-full py-1.5 text-xs text-red-400 border border-red-900/50 rounded-lg hover:bg-red-950/30 transition"
                    >
                      <Trash2 className="w-3 h-3 inline mr-1" />
                      Clear profile
                    </button>
                  </>
                ) : (
                  <div className="text-center py-4 text-slate-500 text-xs">
                    <Shield className="w-6 h-6 mx-auto mb-2 opacity-30" />
                    No voice profile. Calibrate to enable speaker ID.
                  </div>
                )}
              </div>

              {/* Brain sync card */}
              <div className="bg-slate-900/70 border border-slate-700/50 rounded-xl p-4 space-y-3">
                <h2 className="text-sm font-semibold text-white flex items-center gap-2">
                  <Brain className="w-4 h-4 text-violet-400" />
                  Voice Brain Vault
                </h2>
                <div className="text-xs text-slate-400 space-y-1">
                  <div>Words learned: <span className="text-white">{Object.keys(brain.words).length}</span></div>
                  <div>Sessions: <span className="text-white">{brain.sessions}</span></div>
                  <div>Profile: <span className={brain.profile ? 'text-emerald-400' : 'text-slate-500'}>{brain.profile ? 'calibrated' : 'none'}</span></div>
                </div>
                <button
                  onClick={syncBrain}
                  disabled={syncing}
                  className={`w-full py-2 rounded-lg text-xs font-semibold transition flex items-center justify-center gap-2 ${
                    syncing ? 'bg-violet-900/40 text-violet-400 cursor-wait' : 'bg-violet-600 text-white hover:bg-violet-500'
                  }`}
                >
                  {syncing ? <RefreshCw className="w-3.5 h-3.5 animate-spin" /> : <Save className="w-3.5 h-3.5" />}
                  {syncing ? 'Syncing…' : 'Sync Brain to Vault'}
                </button>
                <button
                  onClick={loadBrain}
                  className="w-full py-1.5 rounded-lg text-xs font-medium bg-slate-800 text-slate-300 hover:bg-slate-700 transition flex items-center justify-center gap-2"
                >
                  <Download className="w-3.5 h-3.5" />
                  Reload from Vault
                </button>
                {syncMsg && (
                  <p className={`text-xs px-2 py-1 rounded ${syncMsg.startsWith('✓') ? 'text-emerald-400 bg-emerald-950/30' : 'text-red-400 bg-red-950/30'}`}>
                    {syncMsg}
                  </p>
                )}
              </div>
            </div>
          </div>
        )}

        {/* ═══════════════════════════════════════════════════════════════════ */}
        {/*  TAB: Voice Profile / Calibration                                  */}
        {/* ═══════════════════════════════════════════════════════════════════ */}
        {activeTab === 'profile' && (
          <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
            {/* Calibration panel */}
            <div className="bg-slate-900/70 border border-slate-700/50 rounded-xl p-5 space-y-4">
              <h2 className="text-sm font-semibold text-white flex items-center gap-2">
                <Mic className="w-4 h-4 text-cyan-400" />
                Voice Calibration
              </h2>

              <div className="text-xs text-slate-400 leading-relaxed">
                Calibration captures <strong className="text-white">3 seconds</strong> of your voice and computes a statistical fingerprint (mean + standard deviation per frequency band). The engine then uses this fingerprint to distinguish your voice from TV, background audio, or other speakers.
              </div>

              {!calibrating ? (
                <button
                  onClick={startCalibration}
                  className="w-full py-3 bg-cyan-600 hover:bg-cyan-500 text-white rounded-xl text-sm font-semibold transition flex items-center justify-center gap-2"
                >
                  <Mic className="w-4 h-4" />
                  Start Calibration (3 seconds)
                </button>
              ) : (
                <div className="space-y-3">
                  <div className="text-xs text-cyan-300 text-center animate-pulse font-medium">
                    🎙 Speak naturally — reading aloud works best
                  </div>
                  <div className="w-full bg-slate-800 rounded-full h-3 overflow-hidden">
                    <div
                      className="h-full bg-gradient-to-r from-cyan-600 to-violet-600 rounded-full transition-all duration-100"
                      style={{ width: `${calibProgress * 100}%` }}
                    />
                  </div>
                  <div className="text-center text-xs text-slate-400">
                    {Math.round(calibProgress * CALIB_FRAMES)} / {CALIB_FRAMES} frames captured
                  </div>
                  {energy < 0.02 && (
                    <div className="text-center text-xs text-amber-400">⚠ Speak louder — low energy detected</div>
                  )}
                </div>
              )}

              {brain.profile && (
                <div className="border-t border-slate-800 pt-3 space-y-2">
                  <p className="text-xs font-semibold text-slate-300">Current Profile</p>
                  <div className="grid grid-cols-2 gap-2 text-xs">
                    <div className="bg-slate-800/60 rounded-lg p-2">
                      <div className="text-slate-500">Bands captured</div>
                      <div className="text-white font-mono">{brain.profile.bands.length}</div>
                    </div>
                    <div className="bg-slate-800/60 rounded-lg p-2">
                      <div className="text-slate-500">Spectral centroid</div>
                      <div className="text-white font-mono">{brain.profile.centroid?.toFixed(4) || '—'}</div>
                    </div>
                    <div className="bg-slate-800/60 rounded-lg p-2">
                      <div className="text-slate-500">Sessions trained</div>
                      <div className="text-white font-mono">{brain.sessions}</div>
                    </div>
                    <div className="bg-slate-800/60 rounded-lg p-2">
                      <div className="text-slate-500">Calibrated</div>
                      <div className="text-white font-mono">
                        {brain.profile.calibratedAt
                          ? new Date(brain.profile.calibratedAt).toLocaleDateString()
                          : '—'}
                      </div>
                    </div>
                  </div>

                  <div className="text-xs text-slate-500">Band means (normalised 0–1):</div>
                  <div className="flex gap-0.5 h-10">
                    {brain.profile.bands.map((v, i) => (
                      <div key={i} className="flex-1 relative bg-slate-800 rounded-sm overflow-hidden" title={`Band ${BAND_LABELS[i]}: ${v.toFixed(3)}`}>
                        <div className="absolute bottom-0 w-full bg-cyan-600/70 rounded-sm" style={{ height: `${v * 100}%` }} />
                        {brain.profile?.bandStdDev && (
                          <div
                            className="absolute w-full bg-cyan-400/30"
                            style={{
                              bottom: `${Math.max(0, v - brain.profile.bandStdDev[i]) * 100}%`,
                              height: `${Math.min(100, brain.profile.bandStdDev[i] * 2) * 100}%`,
                            }}
                          />
                        )}
                      </div>
                    ))}
                  </div>
                  <p className="text-xs text-slate-600">Cyan fill = mean · translucent overlay = ±σ tolerance window</p>
                </div>
              )}
            </div>

            {/* How it works */}
            <div className="bg-slate-900/70 border border-slate-700/50 rounded-xl p-5 space-y-4">
              <h2 className="text-sm font-semibold text-white flex items-center gap-2">
                <Info className="w-4 h-4 text-amber-400" />
                How the Binary Engine Works
              </h2>

              <div className="space-y-3 text-xs text-slate-400 leading-relaxed">
                <p>The binary engine runs two parallel pipelines:</p>

                {[
                  {
                    step: '1. FFT Analysis',
                    desc: 'Browser AudioContext runs an AnalyserNode at fftSize=512. Every 50ms it extracts 256 frequency bins, which are bucketed into 16 normalised bands (0–1).',
                    color: 'text-cyan-400',
                  },
                  {
                    step: '2. Speaker ID',
                    desc: 'Each band value is compared to the calibrated profile mean with a ±3σ tolerance. Frames within tolerance count as "your voice". A 30-frame rolling window (≈1s) decides the final verdict.',
                    color: 'text-violet-400',
                  },
                  {
                    step: '3. Continuous Learning',
                    desc: 'High-confidence frames (similarity ≥ 0.72) are blended into the profile via Exponential Moving Average (α=0.05). The profile adapts to your voice as you talk — mic changes, colds, and room acoustics are tracked automatically.',
                    color: 'text-emerald-400',
                  },
                  {
                    step: '4. Self-Transcription Guard',
                    desc: 'When JARVIS speaks, pageSpeaking=true is set before speechSynthesis.speak() runs. The SpeechRecognition onresult handler rejects ALL transcripts except the wake phrase while speaking, preventing the extension from transcribing JARVIS\'s own voice.',
                    color: 'text-amber-400',
                  },
                  {
                    step: '5. Vocabulary Sync',
                    desc: 'Every recognised word is added to a learned vocabulary map (word → frequency count). The extension\'s pickBest() uses this map to boost recognition alternatives that contain known trading/command terms.',
                    color: 'text-pink-400',
                  },
                ].map(item => (
                  <div key={item.step} className="flex gap-3">
                    <span className={`font-semibold shrink-0 ${item.color}`}>{item.step}</span>
                    <span>{item.desc}</span>
                  </div>
                ))}
              </div>

              <div className="bg-slate-800/60 rounded-lg p-3 text-xs text-slate-300 space-y-1 border border-slate-700/50">
                <p className="font-semibold text-amber-300 flex items-center gap-1">
                  <Zap className="w-3 h-3" /> Improving accuracy
                </p>
                <ul className="list-disc list-inside space-y-0.5 text-slate-400">
                  <li>Calibrate in your normal mic position and room</li>
                  <li>Speak 30+ commands per day — the EMA adapts</li>
                  <li>Click Sync Brain after each session to persist learning</li>
                  <li>Increase σ-tolerance in settings if you get false rejections</li>
                </ul>
              </div>
            </div>
          </div>
        )}

        {/* ═══════════════════════════════════════════════════════════════════ */}
        {/*  TAB: Vocabulary                                                    */}
        {/* ═══════════════════════════════════════════════════════════════════ */}
        {activeTab === 'vocab' && (
          <div className="space-y-4">
            <div className="flex items-center gap-3">
              <div className="relative flex-1 max-w-xs">
                <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-slate-400" />
                <input
                  value={vocabSearch}
                  onChange={e => setVocabSearch(e.target.value)}
                  placeholder="Search vocabulary…"
                  className="w-full pl-8 pr-3 py-2 bg-slate-800 border border-slate-700 rounded-lg text-xs text-white placeholder:text-slate-500 focus:outline-none focus:border-cyan-500"
                />
              </div>
              <span className="text-xs text-slate-400">{sortedVocab.length} / {Object.keys(brain.words).length} words</span>
              <button
                onClick={syncBrain}
                className="ml-auto flex items-center gap-2 px-4 py-2 bg-violet-600 hover:bg-violet-500 text-white rounded-lg text-xs font-semibold transition"
              >
                <Upload className="w-3.5 h-3.5" />
                Sync to Vault
              </button>
            </div>

            <div className="bg-slate-900/70 border border-slate-700/50 rounded-xl overflow-hidden">
              <div className="grid grid-cols-3 text-xs text-slate-400 font-semibold px-4 py-2.5 border-b border-slate-700/50 bg-slate-800/40">
                <span>Word</span>
                <span className="text-right">Count</span>
                <span className="text-right">Strength</span>
              </div>

              <div className="max-h-[500px] overflow-y-auto">
                {sortedVocab.length === 0 ? (
                  <div className="text-center py-12 text-slate-500 text-sm">
                    <Brain className="w-8 h-8 mx-auto mb-2 opacity-20" />
                    No vocabulary yet. Talk to JARVIS to build it up!
                  </div>
                ) : (
                  sortedVocab.map(([word, count]) => {
                    const maxCount = sortedVocab[0][1] || 1
                    const pct = count / maxCount
                    return (
                      <div
                        key={word}
                        className="grid grid-cols-3 items-center px-4 py-2 border-b border-slate-800/50 hover:bg-slate-800/30 transition text-xs"
                      >
                        <span className="font-mono text-slate-200">{word}</span>
                        <span className="text-right font-mono text-slate-400">{count.toLocaleString()}</span>
                        <div className="flex items-center justify-end gap-2">
                          <div className="w-20 h-1.5 bg-slate-800 rounded-full overflow-hidden">
                            <div
                              className="h-full rounded-full bg-gradient-to-r from-cyan-600 to-violet-600"
                              style={{ width: `${pct * 100}%` }}
                            />
                          </div>
                          <span className="text-slate-500 w-8 text-right">{Math.round(pct * 100)}%</span>
                        </div>
                      </div>
                    )
                  })
                )}
              </div>
            </div>
          </div>
        )}

        {/* ═══════════════════════════════════════════════════════════════════ */}
        {/*  TAB: Codecs & Plugins  ─────────────────────────────────────────── */}
        {activeTab === 'codecs' && (
          <div className="space-y-4">
            {/* Search + filter bar */}
            <div className="flex items-center gap-3 flex-wrap">
              <div className="relative flex-1 min-w-[220px]">
                <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-slate-400" />
                <input
                  value={codecSearch}
                  onChange={e => setCodecSearch(e.target.value)}
                  placeholder="Search codecs and plugins…"
                  className="w-full pl-8 pr-3 py-2 bg-slate-800 border border-slate-700 rounded-lg text-xs text-white placeholder:text-slate-500 focus:outline-none focus:border-cyan-500"
                />
              </div>
              <div className="flex gap-1 bg-slate-800 rounded-lg p-1">
                {(['all', 'active', 'available'] as const).map(f => (
                  <button
                    key={f}
                    onClick={() => setCodecFilter(f)}
                    className={`px-3 py-1.5 rounded-md text-xs font-medium transition ${
                      codecFilter === f ? 'bg-cyan-600 text-white' : 'text-slate-400 hover:text-white'
                    }`}
                  >
                    {f === 'all' ? 'All' : f === 'active' ? '● Active' : '○ Available'}
                  </button>
                ))}
              </div>
              <span className="text-xs text-slate-500">{filteredCodecs.length} results</span>
            </div>

            {/* Codec cards */}
            <div className="space-y-3">
              {filteredCodecs.map(codec => {
                const cfg       = getCodecCfg(codec.id)
                const tstate    = codecTestState[codec.id] ?? 'idle'
                const tmsg      = codecTestMsg[codec.id] ?? ''
                const isOpen    = expandedCodec === codec.id

                return (
                  <div
                    key={codec.id}
                    className={`bg-slate-900/70 border rounded-xl overflow-hidden transition-all ${
                      codec.status === 'active' ? 'border-cyan-700/40' : 'border-slate-700/50'
                    } ${isOpen ? 'ring-1 ring-cyan-700/40' : ''}`}
                  >
                    {/* Card header (always visible) */}
                    <button
                      onClick={() => setExpandedCodec(isOpen ? null : codec.id)}
                      className="w-full p-4 flex items-start gap-3 text-left hover:bg-slate-800/30 transition"
                    >
                      <div className="flex-1 min-w-0">
                        <div className="flex items-center gap-2 flex-wrap">
                          <h3 className="text-sm font-semibold text-white">{codec.name}</h3>
                          <span className={`shrink-0 px-2 py-0.5 rounded-full text-[10px] font-medium ${
                            codec.status === 'active'
                              ? 'bg-emerald-900/50 text-emerald-400 border border-emerald-700/50'
                              : 'bg-slate-800 text-slate-400 border border-slate-700'
                          }`}>
                            {codec.status === 'active' ? '● Active' : '○ Available'}
                          </span>
                          {tstate === 'ok'   && <span className="text-[10px] text-emerald-400 font-mono">✓ tested</span>}
                          {tstate === 'fail' && <span className="text-[10px] text-red-400 font-mono">✗ fail</span>}
                        </div>
                        <p className="text-[11px] text-slate-500 mt-0.5">{codec.category}</p>
                        {!isOpen && <p className="text-xs text-slate-400 mt-1 leading-relaxed line-clamp-2">{codec.description}</p>}
                      </div>
                      <ChevronRight className={`w-4 h-4 text-slate-500 shrink-0 mt-0.5 transition-transform ${isOpen ? 'rotate-90' : ''}`} />
                    </button>

                    {/* Expanded panel */}
                    {isOpen && (
                      <div className="border-t border-slate-800 divide-y divide-slate-800">

                        {/* Description */}
                        <div className="px-4 py-3">
                          <p className="text-xs text-slate-400 leading-relaxed">{codec.description}</p>
                          <div className="flex flex-wrap gap-1 mt-2">
                            {codec.tags.map(tag => (
                              <span key={tag} className="px-1.5 py-0.5 text-[10px] rounded bg-slate-800 text-slate-500 font-mono">{tag}</span>
                            ))}
                          </div>
                        </div>

                        {/* Setup guide */}
                        <div className="px-4 py-3 space-y-3">
                          <p className="text-xs font-semibold text-slate-300 uppercase tracking-wider">Setup Guide</p>
                          {codec.guide.map((g, gi) => (
                            <div key={gi} className="flex gap-3">
                              <span className="shrink-0 w-5 h-5 rounded-full bg-cyan-900/60 border border-cyan-700/50 text-[10px] text-cyan-300 flex items-center justify-center font-bold">
                                {gi + 1}
                              </span>
                              <div>
                                <p className="text-xs font-medium text-slate-200">{g.step}</p>
                                <p className="text-[11px] text-slate-400 mt-0.5 leading-relaxed whitespace-pre-line">{g.detail}</p>
                              </div>
                            </div>
                          ))}
                        </div>

                        {/* Configuration controls */}
                        <div className="px-4 py-3 space-y-3">
                          <p className="text-xs font-semibold text-slate-300 uppercase tracking-wider">Configuration</p>

                          {/* WebRTC VAD / noise / echo / gain — getUserMedia constraints */}
                          {(codec.id === 'webrtc-vad' || codec.id === 'echo-cancel' || codec.id === 'noise-suppress' || codec.id === 'auto-gain') && (
                            <div className="space-y-2">
                              {(['echoCancellation', 'noiseSuppression', 'autoGainControl'] as const).map(k => {
                                const labels: Record<string, string> = { echoCancellation: 'Echo Cancellation (AEC)', noiseSuppression: 'Noise Suppression (NS)', autoGainControl: 'Auto Gain Control (AGC)' }
                                const vadCfg = getCodecCfg('webrtc-vad')
                                return (
                                  <label key={k} className="flex items-center justify-between">
                                    <span className="text-xs text-slate-300">{labels[k]}</span>
                                    <button
                                      onClick={() => saveCodecCfg('webrtc-vad', { [k]: !(vadCfg[k] ?? true) })}
                                      className={`relative w-9 h-5 rounded-full transition-colors ${(vadCfg[k] ?? true) ? 'bg-cyan-600' : 'bg-slate-700'}`}
                                    >
                                      <span className={`absolute top-0.5 w-4 h-4 bg-white rounded-full shadow transition-transform ${(vadCfg[k] ?? true) ? 'translate-x-4' : 'translate-x-0.5'}`} />
                                    </button>
                                  </label>
                                )
                              })}
                              <p className="text-[11px] text-slate-500">Changes apply when mic is restarted (click Restart Mic on the Engine tab).</p>
                            </div>
                          )}

                          {/* FFT-512 — fftSize + smoothing */}
                          {codec.id === 'fft-512' && (
                            <div className="space-y-3">
                              <div>
                                <div className="flex justify-between mb-1">
                                  <label className="text-xs text-slate-300">FFT Size</label>
                                  <span className="text-xs font-mono text-cyan-400">{String(cfg.fftSize ?? 512)}</span>
                                </div>
                                <div className="flex gap-1">
                                  {[256, 512, 1024, 2048].map(v => (
                                    <button
                                      key={v}
                                      onClick={() => saveCodecCfg('fft-512', { fftSize: v })}
                                      className={`flex-1 py-1 rounded text-xs font-mono transition ${(cfg.fftSize ?? 512) === v ? 'bg-cyan-700 text-white' : 'bg-slate-800 text-slate-400 hover:bg-slate-700'}`}
                                    >
                                      {v}
                                    </button>
                                  ))}
                                </div>
                              </div>
                              <div>
                                <div className="flex justify-between mb-1">
                                  <label className="text-xs text-slate-300">Smoothing Constant</label>
                                  <span className="text-xs font-mono text-cyan-400">{Number(cfg.smoothing ?? 0.72).toFixed(2)}</span>
                                </div>
                                <input
                                  type="range" min="0" max="0.99" step="0.01"
                                  value={Number(cfg.smoothing ?? 0.72)}
                                  onChange={e => saveCodecCfg('fft-512', { smoothing: parseFloat(e.target.value) })}
                                  className="w-full accent-cyan-500"
                                />
                                <div className="flex justify-between text-[10px] text-slate-600 mt-0.5">
                                  <span>0 (raw)</span><span>0.72 (default)</span><span>0.99 (smooth)</span>
                                </div>
                              </div>
                              <p className="text-[11px] text-slate-500">Apply by restarting the mic — the AnalyserNode will be recreated with the new settings.</p>
                            </div>
                          )}

                          {/* Speaker-ID — sigma multiplier + minEnergy */}
                          {codec.id === 'voice-profile-matching' && (
                            <div className="space-y-3">
                              <div>
                                <div className="flex justify-between mb-1">
                                  <label className="text-xs text-slate-300">σ Multiplier (tolerance)</label>
                                  <span className="text-xs font-mono text-cyan-400">{Number(cfg.sigmaMultiplier ?? 3.0).toFixed(1)}σ</span>
                                </div>
                                <input
                                  type="range" min="1" max="6" step="0.1"
                                  value={Number(cfg.sigmaMultiplier ?? 3.0)}
                                  onChange={e => saveCodecCfg('voice-profile-matching', { sigmaMultiplier: parseFloat(e.target.value) })}
                                  className="w-full accent-cyan-500"
                                />
                                <div className="flex justify-between text-[10px] text-slate-600 mt-0.5">
                                  <span>1σ (strict)</span><span>3σ (default)</span><span>6σ (permissive)</span>
                                </div>
                              </div>
                              <div>
                                <div className="flex justify-between mb-1">
                                  <label className="text-xs text-slate-300">Min Energy Threshold</label>
                                  <span className="text-xs font-mono text-cyan-400">{Number(cfg.minEnergy ?? 0.02).toFixed(3)}</span>
                                </div>
                                <input
                                  type="range" min="0.005" max="0.1" step="0.005"
                                  value={Number(cfg.minEnergy ?? 0.02)}
                                  onChange={e => saveCodecCfg('voice-profile-matching', { minEnergy: parseFloat(e.target.value) })}
                                  className="w-full accent-cyan-500"
                                />
                              </div>
                              <div>
                                <div className="flex justify-between mb-1">
                                  <label className="text-xs text-slate-300">EMA Alpha (adaptation rate)</label>
                                  <span className="text-xs font-mono text-cyan-400">{Number(cfg.emaAlpha ?? 0.05).toFixed(2)}</span>
                                </div>
                                <input
                                  type="range" min="0.01" max="0.2" step="0.01"
                                  value={Number(cfg.emaAlpha ?? 0.05)}
                                  onChange={e => saveCodecCfg('voice-profile-matching', { emaAlpha: parseFloat(e.target.value) })}
                                  className="w-full accent-cyan-500"
                                />
                              </div>
                              <p className="text-[11px] text-slate-500">Config is saved immediately. To recalibrate your voice profile, go to the Voice Profile tab.</p>
                            </div>
                          )}

                          {/* Deepgram — API key + model */}
                          {codec.id === 'deepgram-nova' && (
                            <div className="space-y-2">
                              <div>
                                <label className="block text-xs text-slate-300 mb-1">Deepgram API Key</label>
                                <input
                                  type="password"
                                  value={String(cfg.apiKey ?? '')}
                                  onChange={e => saveCodecCfg('deepgram-nova', { apiKey: e.target.value })}
                                  placeholder="dg_…"
                                  className="w-full px-3 py-1.5 bg-slate-800 border border-slate-700 rounded text-xs text-white font-mono placeholder:text-slate-600 focus:outline-none focus:border-cyan-500"
                                />
                              </div>
                              <div>
                                <label className="block text-xs text-slate-300 mb-1">Model</label>
                                <div className="flex gap-1">
                                  {['nova-3', 'nova-2', 'enhanced'].map(m => (
                                    <button
                                      key={m}
                                      onClick={() => saveCodecCfg('deepgram-nova', { model: m })}
                                      className={`px-3 py-1 rounded text-xs font-mono transition ${(cfg.model ?? 'nova-3') === m ? 'bg-cyan-700 text-white' : 'bg-slate-800 text-slate-400 hover:bg-slate-700'}`}
                                    >
                                      {m}
                                    </button>
                                  ))}
                                </div>
                              </div>
                              <div>
                                <label className="block text-xs text-slate-300 mb-1">Language</label>
                                <div className="flex gap-1">
                                  {['en-US', 'en-GB', 'auto'].map(l => (
                                    <button
                                      key={l}
                                      onClick={() => saveCodecCfg('deepgram-nova', { language: l })}
                                      className={`px-3 py-1 rounded text-xs font-mono transition ${(cfg.language ?? 'en-US') === l ? 'bg-cyan-700 text-white' : 'bg-slate-800 text-slate-400 hover:bg-slate-700'}`}
                                    >
                                      {l}
                                    </button>
                                  ))}
                                </div>
                              </div>
                              <p className="text-[11px] text-slate-500">Key is stored only in browser localStorage. Also add DEEPGRAM_API_KEY to backend/.env for server-side routing.</p>
                            </div>
                          )}

                          {/* Whisper / RNNoise / Pyannote — read-only env notes */}
                          {(codec.id === 'whisper-webgpu' || codec.id === 'rnnoise' || codec.id === 'pyannote') && (
                            <div className="text-[11px] text-slate-500 bg-slate-800/60 rounded-lg p-3 leading-relaxed">
                              {codec.id === 'whisper-webgpu' && <>Run <code className="text-cyan-400 font-mono">npm install @xenova/transformers</code> in frontend/ then wire WhisperProcessor into PaulChat.tsx to replace browser SpeechRecognition.</>}
                              {codec.id === 'rnnoise' && <>Run <code className="text-cyan-400 font-mono">npm install rnnoise-wasm</code> in frontend/ then create an AudioWorkletProcessor to insert it between MediaStreamSource and the AnalyserNode.</>}
                              {codec.id === 'pyannote' && <>Run <code className="text-cyan-400 font-mono">pip install pyannote.audio torchaudio</code> in backend/ and add <code className="text-cyan-400 font-mono">HUGGINGFACE_TOKEN=hf_…</code> to backend/.env.</>}
                            </div>
                          )}
                        </div>

                        {/* Test button + result */}
                        <div className="px-4 py-3 flex items-center gap-3">
                          <button
                            onClick={() => testCodec(codec.id)}
                            disabled={tstate === 'testing'}
                            className="px-4 py-1.5 rounded-lg text-xs font-medium transition disabled:opacity-50 bg-cyan-800/60 hover:bg-cyan-700/70 text-cyan-200 border border-cyan-700/50 flex items-center gap-1.5 shrink-0"
                          >
                            {tstate === 'testing'
                              ? <><span className="animate-spin inline-block w-3 h-3 border-2 border-cyan-400 border-t-transparent rounded-full" /> Testing…</>
                              : codec.testType === 'api' ? '↗ Test Connection' : codec.testType === 'env' ? '⬡ Check Environment' : codec.testType === 'latency' ? '⏱ Test Speaker ID' : '🎤 Test Mic Input'
                            }
                          </button>
                          {tmsg && (
                            <p className={`text-[11px] font-mono ${tstate === 'ok' ? 'text-emerald-400' : tstate === 'fail' ? 'text-red-400' : 'text-slate-400'}`}>
                              {tmsg}
                            </p>
                          )}
                        </div>
                      </div>
                    )}
                  </div>
                )
              })}
            </div>

            {/* Pipeline info footer */}
            <div className="bg-slate-900/50 border border-slate-700/30 rounded-xl p-4 text-xs text-slate-500 leading-relaxed">
              <p className="font-semibold text-slate-300 mb-1">About the codec pipeline</p>
              <p>
                The binary engine processes audio in a multi-stage pipeline. <strong className="text-slate-300">Active</strong> codecs are running now inside your browser — no setup needed, just open the Engine tab and allow mic access.
                {' '}<strong className="text-slate-300">Available</strong> codecs need one-time installation — click any card to expand its step-by-step integration guide, configure it, and run the built-in test.
                All configuration is persisted to <code className="text-slate-400 font-mono">paul.codecConfigs</code> in localStorage.
              </p>
            </div>
          </div>
        )}

      {/* ═══════════════════════════════════════════════════════════════════ */}
        {/*  TAB: Deepgram Voice Agent                                          */}
        {/* ═══════════════════════════════════════════════════════════════════ */}
        {activeTab === 'deepgram' && (
          <div className="space-y-4">

            {/* ── Connection row ──────────────────────────────────────────── */}
            <div className="flex flex-col gap-2 bg-slate-900/70 border border-slate-700/50 rounded-xl p-4">
              <div className="flex items-center gap-3">
                <div className={`w-2.5 h-2.5 rounded-full shrink-0 ${
                  dg.state === 'connected' ? 'bg-emerald-400 animate-pulse' :
                  dg.state === 'connecting' || dg.state === 'reconnecting' ? 'bg-amber-400 animate-pulse' :
                  dg.state === 'disconnected' && dg.error ? 'bg-red-500' :
                  'bg-slate-600'
                }`} />
                <span className="text-xs font-mono text-slate-300 capitalize">{dg.state}</span>
                {dg.latency && (
                  <span className="text-[10px] font-mono text-slate-500 ml-2">
                    latency: <span className="text-cyan-400">{(dg.latency.total * 1000).toFixed(0)}ms</span>
                    {' '}tts: <span className="text-amber-400">{(dg.latency.tts * 1000).toFixed(0)}ms</span>
                    {' '}llm: <span className="text-purple-400">{(dg.latency.ttt * 1000).toFixed(0)}ms</span>
                  </span>
                )}
                <div className="flex gap-2 ml-auto">
                  {dg.state === 'connected' ? (
                    <button
                      onClick={() => dg.disconnect()}
                      className="px-4 py-1.5 rounded-lg text-xs font-medium bg-red-800/60 hover:bg-red-700/70 text-red-200 border border-red-700/50 transition"
                    >
                      ■ Stop Session
                    </button>
                  ) : (
                    <button
                      onClick={() => {
                        const cfg: DeepgramAgentConfig = {
                          sttModel: dgSttModel,
                          llmProvider: dgLlmType,
                          llmModel: dgLlmModel,
                          ttsModel: dgTtsModel,
                          systemPrompt: dgPrompt || undefined,
                          greeting: dgGreeting || undefined,
                          ...(dgSttModel.startsWith('flux') && {
                            eotThreshold: dgEot,
                            ...(dgEagerEot > 0 && { eagerEotThreshold: dgEagerEot }),
                            eotTimeoutMs: dgEotMs,
                          }),
                        }
                        // Guard the expensive (~$0.08/min) agent when the cheap
                        // fallback budget is exhausted — confirm before connecting.
                        if (dgUsage && dgUsage.remaining <= 0 &&
                            !window.confirm('The monthly Deepgram fallback budget is used up. The Voice Agent costs ~$0.08/min (far more than the fallback). Connect anyway?')) {
                          return
                        }
                        dg.connect(cfg)
                      }}
                      disabled={dg.state === 'connecting' || dg.state === 'reconnecting'}
                      className="px-4 py-1.5 rounded-lg text-xs font-medium bg-cyan-700/70 hover:bg-cyan-600/80 text-cyan-100 border border-cyan-600/50 transition disabled:opacity-50"
                    >
                      {dg.state === 'connecting' ? 'Connecting…' : dg.state === 'reconnecting' ? 'Reconnecting…' : '▶ Start Session'}
                    </button>
                  )}
                  {dg.transcript.length > 0 && (
                    <button
                      onClick={() => dg.clearTranscript()}
                      className="px-3 py-1.5 rounded-lg text-xs text-slate-400 hover:text-white border border-slate-700 hover:border-slate-600 transition"
                    >
                      Clear
                    </button>
                  )}
                </div>
              </div>
              {/* Error banner */}
              {dg.error && (
                <div className="flex items-start gap-2 px-3 py-2 rounded-lg bg-red-900/40 border border-red-700/50 text-xs text-red-300">
                  <span className="shrink-0 mt-0.5">⚠</span>
                  <span>{dg.error}</span>
                </div>
              )}
              {/* Cost guard: the Voice Agent is ~$0.08/min — far pricier than the
                  cheap (~$0.0043/min) cost-aware fallback used by JARVIS commands. */}
              <div className="flex items-start gap-2 px-3 py-2 rounded-lg bg-amber-900/20 border border-amber-700/40 text-[11px] text-amber-200/90">
                <span className="shrink-0 mt-0.5">💸</span>
                <span>
                  This full Voice Agent costs <strong>~$0.08/min</strong> — ~15–20× more than the cost-aware
                  fallback JARVIS uses for missed commands. Use it sparingly.
                  {dgUsage && (
                    <> Fallback budget left this month: <strong>${dgUsage.remaining.toFixed(2)}</strong> / ${dgUsage.monthly_cap.toFixed(0)}
                    {dgUsage.projected_runway_days != null && <> · ~{Math.round(dgUsage.projected_runway_days)} days runway</>}.</>
                  )}
                </span>
              </div>
            </div>

            {/* ── Deepgram Account / profile panel ────────────────────────── */}
            <div className="bg-slate-900/70 border border-slate-700/50 rounded-xl p-4 space-y-3">
              <div className="flex items-center justify-between">
                <p className="text-xs font-semibold text-slate-300 uppercase tracking-wider">Deepgram Account</p>
                <button
                  onClick={() => { void fetchDgStatus() }}
                  disabled={dgStatusLoading}
                  className="text-[10px] px-2.5 py-1 rounded bg-slate-800 text-slate-300 border border-slate-700 hover:bg-slate-700 hover:text-white transition disabled:opacity-50 inline-flex items-center gap-1"
                >
                  <RefreshCw className={`w-3 h-3 ${dgStatusLoading ? 'animate-spin' : ''}`} />
                  {dgStatusLoading ? 'Checking…' : 'Refresh'}
                </button>
              </div>

              {dgStatusLoading && !dgStatus && (
                <p className="text-xs text-slate-500">Loading account…</p>
              )}

              {/* Missing key / error → actionable message */}
              {!dgStatusLoading && (dgStatusError || (dgStatus && !dgStatus.key_present)) && (
                <div className="flex items-start gap-2 px-3 py-2 rounded-lg bg-amber-900/30 border border-amber-700/50 text-xs text-amber-200">
                  <span className="shrink-0 mt-0.5">⚠</span>
                  <span>
                    {dgStatusError || dgStatus?.error ||
                      'Deepgram key not configured on the backend — add DEEPGRAM_API_KEY to .env and restart the backend.'}
                  </span>
                </div>
              )}

              {/* Profile details */}
              {dgStatus && dgStatus.key_present !== false && !dgStatusError && (
                <div className="space-y-2 text-xs">
                  <div className="flex items-center justify-between gap-3">
                    <span className="text-slate-500">Project</span>
                    <span className="text-slate-200 font-medium truncate" title={dgStatus.account_name || ''}>
                      {dgStatus.account_name || '—'}
                    </span>
                  </div>
                  {dgStatus.email && (
                    <div className="flex items-center justify-between gap-3">
                      <span className="text-slate-500">Account</span>
                      <span className="text-slate-300 font-mono truncate" title={dgStatus.email}>{dgStatus.email}</span>
                    </div>
                  )}
                  {dgStatus.project_id && (
                    <div className="flex items-center justify-between gap-3">
                      <span className="text-slate-500">Project ID</span>
                      <span className="text-slate-400 font-mono truncate" title={dgStatus.project_id}>{dgStatus.project_id}</span>
                    </div>
                  )}
                  <div className="flex items-center justify-between gap-3">
                    <span className="text-slate-500">Key health</span>
                    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-medium border ${
                      dgStatus.key_ok
                        ? 'bg-emerald-900/40 text-emerald-300 border-emerald-700/50'
                        : 'bg-red-900/40 text-red-300 border-red-700/50'
                    }`}>
                      {dgStatus.key_ok ? <CheckCircle className="w-3 h-3" /> : <XCircle className="w-3 h-3" />}
                      {dgStatus.key_ok ? 'Healthy' : 'Needs attention'}
                    </span>
                  </div>
                  <div className="flex items-center justify-between gap-3">
                    <span className="text-slate-500">Credits</span>
                    {typeof dgStatus.credits_remaining === 'number' ? (
                      <span className="text-cyan-300 font-mono">
                        ${dgStatus.credits_remaining.toFixed(2)}
                        {dgStatus.balances?.[0]?.units ? ` ${dgStatus.balances[0].units.toUpperCase()}` : ''}
                      </span>
                    ) : (
                      <span className="text-slate-500 italic">credits unavailable (needs Owner role)</span>
                    )}
                  </div>
                  {!dgStatus.key_ok && dgStatus.required_fix && (
                    <p className="text-[11px] text-amber-300/80 pt-1 border-t border-slate-800">{dgStatus.required_fix}</p>
                  )}
                </div>
              )}
            </div>

            <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">

              {/* ── Left column: config ─────────────────────────────────── */}
              <div className="space-y-4">

                {/* STT model */}
                <div className="bg-slate-900/70 border border-slate-700/50 rounded-xl p-4 space-y-2">
                  <p className="text-xs font-semibold text-slate-300 uppercase tracking-wider">Speech-to-Text (Listen)</p>
                  <div className="flex flex-wrap gap-1">
                    {['nova-3', 'nova-2', 'flux-general-en', 'flux-general-multi'].map(m => (
                      <button key={m} onClick={() => setDgSttModel(m)}
                        className={`px-2.5 py-1 rounded text-xs font-mono transition ${
                          dgSttModel === m ? 'bg-cyan-700 text-white' : 'bg-slate-800 text-slate-400 hover:bg-slate-700'
                        }`}>{m}</button>
                    ))}
                  </div>
                  {dgSttModel.startsWith('flux') && (
                    <div className="space-y-2 pt-1">
                      <label className="flex items-center justify-between">
                        <span className="text-xs text-slate-400">EOT Threshold: <span className="text-cyan-400 font-mono">{dgEot.toFixed(1)}</span></span>
                        <input type="range" min={0.5} max={0.9} step={0.05} value={dgEot}
                          onChange={e => setDgEot(parseFloat(e.target.value))}
                          className="w-32 accent-cyan-500" />
                      </label>
                      <label className="flex items-center justify-between">
                        <span className="text-xs text-slate-400">EOT Timeout: <span className="text-cyan-400 font-mono">{dgEotMs}ms</span></span>
                        <input type="range" min={1000} max={10000} step={500} value={dgEotMs}
                          onChange={e => setDgEotMs(parseInt(e.target.value))}
                          className="w-32 accent-cyan-500" />
                      </label>
                    </div>
                  )}
                </div>

                {/* LLM */}
                <div className="bg-slate-900/70 border border-slate-700/50 rounded-xl p-4 space-y-2">
                  <p className="text-xs font-semibold text-slate-300 uppercase tracking-wider">LLM (Think)</p>
                  <div className="flex flex-wrap gap-1">
                    {['open_ai', 'anthropic', 'google', 'groq'].map(p => (
                      <button key={p} onClick={() => setDgLlmType(p)}
                        className={`px-2.5 py-1 rounded text-xs font-mono transition ${
                          dgLlmType === p ? 'bg-purple-700 text-white' : 'bg-slate-800 text-slate-400 hover:bg-slate-700'
                        }`}>{p}</button>
                    ))}
                  </div>
                  <div className="flex flex-wrap gap-1">
                    {(dgLlmType === 'open_ai'
                      ? ['gpt-4o-mini', 'gpt-4o', 'gpt-4.1-mini', 'gpt-4.1']
                      : dgLlmType === 'anthropic'
                      ? ['claude-3-5-haiku-latest', 'claude-sonnet-4-20250514']
                      : dgLlmType === 'google'
                      ? ['gemini-2.0-flash', 'gemini-2.5-flash']
                      : ['llama-3.1-8b-instant', 'llama-3.3-70b-versatile']
                    ).map(m => (
                      <button key={m} onClick={() => setDgLlmModel(m)}
                        className={`px-2.5 py-1 rounded text-xs font-mono transition ${
                          dgLlmModel === m ? 'bg-purple-700 text-white' : 'bg-slate-800 text-slate-400 hover:bg-slate-700'
                        }`}>{m}</button>
                    ))}
                  </div>
                </div>

                {/* TTS */}
                <div className="bg-slate-900/70 border border-slate-700/50 rounded-xl p-4 space-y-2">
                  <p className="text-xs font-semibold text-slate-300 uppercase tracking-wider">Text-to-Speech (Speak)</p>
                  <select
                    value={dgTtsModel}
                    onChange={e => {
                      setDgTtsModel(e.target.value)
                      if (dg.state === 'connected') dg.updateVoice(e.target.value)
                    }}
                    className="w-full px-3 py-1.5 bg-slate-800 border border-slate-700 rounded text-xs text-white font-mono focus:outline-none focus:border-cyan-500"
                  >
                    {[
                      'aura-2-thalia-en','aura-2-asteria-en','aura-2-athena-en','aura-2-zeus-en',
                      'aura-2-orion-en','aura-2-luna-en','aura-2-mars-en','aura-2-helios-en',
                      'aura-2-hera-en','aura-2-juno-en','aura-asteria-en','aura-zeus-en',
                      'aura-2-sirio-es','aura-2-nestor-es',
                    ].map(v => <option key={v} value={v}>{v}</option>)}
                  </select>
                </div>

                {/* System prompt */}
                <div className="bg-slate-900/70 border border-slate-700/50 rounded-xl p-4 space-y-2">
                  <div className="flex items-center justify-between">
                    <p className="text-xs font-semibold text-slate-300 uppercase tracking-wider">System Prompt</p>
                    {dg.state === 'connected' && (
                      <button
                        onClick={() => dg.updatePrompt(dgPrompt)}
                        className="text-[10px] px-2 py-0.5 rounded bg-cyan-800/60 text-cyan-300 border border-cyan-700/50 hover:bg-cyan-700/70 transition"
                      >Apply live</button>
                    )}
                  </div>
                  <textarea
                    value={dgPrompt}
                    onChange={e => setDgPrompt(e.target.value)}
                    rows={5}
                    placeholder="Leave empty for JARVIS's default British-butler persona prompt…"
                    className="w-full px-3 py-2 bg-slate-800 border border-slate-700 rounded text-xs text-slate-300 placeholder:text-slate-600 font-mono resize-none focus:outline-none focus:border-cyan-500"
                  />
                  <p className="text-[10px] text-slate-600">{dgPrompt.length}/25000 chars (managed LLM limit)</p>
                </div>

                {/* Greeting */}
                <div className="bg-slate-900/70 border border-slate-700/50 rounded-xl p-4 space-y-2">
                  <p className="text-xs font-semibold text-slate-300 uppercase tracking-wider">Agent Greeting</p>
                  <input
                    value={dgGreeting}
                    onChange={e => setDgGreeting(e.target.value)}
                    placeholder="Leave empty for JARVIS's greeting (Good day, Sir. I'm PAUL…)"
                    className="w-full px-3 py-1.5 bg-slate-800 border border-slate-700 rounded text-xs text-white placeholder:text-slate-600 focus:outline-none focus:border-cyan-500"
                  />
                </div>

                {/* PaulChat upgrade toggle */}
                <div className="bg-slate-900/70 border border-slate-700/50 rounded-xl p-4">
                  <label className="flex items-center justify-between cursor-pointer">
                    <div>
                      <p className="text-xs font-semibold text-slate-300">Upgrade PaulChat to Deepgram</p>
                      <p className="text-[11px] text-slate-500 mt-0.5">Replace browser STT/TTS with the Deepgram Voice Agent pipeline. Start a session above first.</p>
                    </div>
                    <button
                      onClick={() => setDgUpgradePaul(p => !p)}
                      className={`relative w-11 h-6 rounded-full transition-colors shrink-0 ml-4 ${
                        dgUpgradePaul ? 'bg-cyan-600' : 'bg-slate-700'
                      }`}
                    >
                      <span className={`absolute top-1 w-4 h-4 bg-white rounded-full shadow transition-transform ${
                        dgUpgradePaul ? 'translate-x-6' : 'translate-x-1'
                      }`} />
                    </button>
                  </label>
                </div>

              </div>

              {/* ── Right column: transcript + canvas ───────────────────── */}
              <div className="space-y-4">

                {/* Agent audio FFT canvas */}
                <div className="bg-slate-900/70 border border-slate-700/50 rounded-xl p-4 space-y-2">
                  <p className="text-xs font-semibold text-slate-300 uppercase tracking-wider">
                    Agent Audio
                    {dg.isSpeaking && <span className="ml-2 text-amber-400 font-mono normal-case text-[10px] animate-pulse">● speaking</span>}
                    {dg.isUserSpeaking && <span className="ml-2 text-cyan-400 font-mono normal-case text-[10px] animate-pulse">● you</span>}
                  </p>
                  <canvas
                    ref={dgCanvasRef}
                    width={400}
                    height={80}
                    className="w-full h-20 rounded-lg bg-slate-950/80"
                  />
                </div>

                {/* Live transcript */}
                <div className="bg-slate-900/70 border border-slate-700/50 rounded-xl p-4 space-y-2 flex-1">
                  <p className="text-xs font-semibold text-slate-300 uppercase tracking-wider">Live Conversation</p>
                  <div className="space-y-2 max-h-64 overflow-y-auto pr-1">
                    {dg.transcript.length === 0 ? (
                      <p className="text-[11px] text-slate-600 italic">
                        {dg.state === 'connected' ? 'Listening… speak now' : 'Start a session to begin.'}
                      </p>
                    ) : (
                      dg.transcript.slice(-30).map(entry => (
                        <div key={entry.id} className={`flex gap-2 items-start ${
                          entry.role === 'user' ? 'flex-row-reverse' : ''
                        }`}>
                          <span className={`shrink-0 w-5 h-5 rounded-full text-[10px] flex items-center justify-center font-bold ${
                            entry.role === 'user' ? 'bg-cyan-800 text-cyan-200' : 'bg-amber-900 text-amber-300'
                          }`}>{entry.role === 'user' ? 'U' : 'J'}</span>
                          <div className={`max-w-[85%] px-3 py-2 rounded-xl text-xs leading-relaxed ${
                            entry.role === 'user'
                              ? 'bg-cyan-900/40 text-cyan-200 rounded-tr-none'
                              : 'bg-slate-800 text-slate-300 rounded-tl-none'
                          }`}>
                            {entry.content}
                          </div>
                        </div>
                      ))
                    )}
                  </div>
                </div>

                {/* Registered functions */}
                <div className="bg-slate-900/70 border border-slate-700/50 rounded-xl p-4 space-y-2">
                  <p className="text-xs font-semibold text-slate-300 uppercase tracking-wider">Function Calls</p>
                  <div className="space-y-1.5">
                    {[
                      { name: 'navigate_to_page', side: 'client', desc: 'Navigate dashboard pages' },
                      { name: 'get_active_signals', side: 'server', desc: 'Fetch current trading signals' },
                      { name: 'get_price', side: 'server', desc: 'Get live spot price for a symbol' },
                      { name: 'place_limit_order', side: 'server', desc: 'Place MT5 limit order' },
                      { name: 'get_account_balance', side: 'server', desc: 'Current exchange balance' },
                      { name: 'get_position_summary', side: 'server', desc: 'Open MT5 positions' },
                    ].map(fn => (
                      <div key={fn.name} className="flex items-center gap-2 px-3 py-2 rounded-lg bg-slate-800/60 border border-slate-700/30">
                        <span className={`shrink-0 text-[9px] px-1.5 py-0.5 rounded font-mono ${
                          fn.side === 'client' ? 'bg-cyan-900/60 text-cyan-400 border border-cyan-700/50' : 'bg-slate-700 text-slate-400 border border-slate-600'
                        }`}>{fn.side}</span>
                        <span className="text-xs font-mono text-slate-300 truncate">{fn.name}</span>
                        <span className="text-[10px] text-slate-500 ml-auto shrink-0 hidden sm:block">{fn.desc}</span>
                      </div>
                    ))}
                  </div>
                  <p className="text-[10px] text-slate-600">Server-side functions are called directly by Deepgram → backend. Client-side functions run in the browser.</p>
                </div>

              </div>
            </div>
          </div>
        )}

      </div>
    </>
  )
}
