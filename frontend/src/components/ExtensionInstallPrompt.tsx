/**
 * ExtensionInstallPrompt — detects the browser and prompts the user to install
 * the JARVIS Voice Assistant extension for reliable voice + desktop alerts.
 *
 * Flow:
 *  1. On mount, listen for the extension's "connected" handshake message.
 *  2. If it doesn't arrive within ~2.5s, the extension isn't installed → show a
 *     dismissible banner bottom-left.
 *  3. Clicking "Install extension" opens a modal with browser-specific steps,
 *     a one-click download of the extension zip, and a "Done — recheck" button.
 *
 * Browsers don't allow a web page to silently install an unpacked extension, so
 * the flow downloads the package and walks the user through load-unpacked. Once
 * the extension connects, the banner/modal auto-hides forever (per browser).
 */
import { useEffect, useState, useCallback } from 'react'
import { Download, X, Mic, Bell, Zap, ShieldCheck, CheckCircle2 } from 'lucide-react'
import { getApiBaseUrl } from '../services/api'

type Browser = 'chrome' | 'edge' | 'brave' | 'firefox' | 'safari' | 'other'

const DISMISS_KEY = 'jarvis.extPrompt.dismissed'
// Per-version snooze for the "update available" prompt, so a NEW release always
// re-prompts even if the user dismissed the previous update notice.
const UPDATE_DISMISS_PREFIX = 'jarvis.extPrompt.updateDismissed.'

// Compare two dotted version strings (e.g. "3.2.0" vs "3.10.1").
// Returns <0 if a<b, 0 if equal, >0 if a>b.
function cmpVersions(a: string, b: string): number {
  const pa = String(a).split('.').map(n => parseInt(n, 10) || 0)
  const pb = String(b).split('.').map(n => parseInt(n, 10) || 0)
  const len = Math.max(pa.length, pb.length)
  for (let i = 0; i < len; i++) {
    const d = (pa[i] || 0) - (pb[i] || 0)
    if (d !== 0) return d
  }
  return 0
}
// Resolve the backend base from the configured API URL (e.g. the backend runs
// on :1448, not :8000) so the version check and download always hit the live
// server instead of a hardcoded, possibly-dead address.
const apiBase = () => getApiBaseUrl().replace(/\/$/, '')
// Dynamic backend endpoint — always serves the latest extension files with the
// version baked into the filename (e.g. jarvis-extension-v3.0.0.zip).
// Falls back to the static file if the backend is not running.
const zipUrl = () => `${apiBase()}/jarvis/extension-download`
const ZIP_URL_FALLBACK = '/jarvis-extension.zip'
const versionUrl = () => `${apiBase()}/jarvis/extension-version`

function detectBrowser(): Browser {
  if (typeof navigator === 'undefined') return 'other'
  const ua = navigator.userAgent.toLowerCase()
  // Order matters — Brave/Edge also contain "chrome"
  if ((navigator as any).brave) return 'brave'
  if (ua.includes('edg/')) return 'edge'
  if (ua.includes('firefox')) return 'firefox'
  if (ua.includes('chrome') || ua.includes('chromium')) return 'chrome'
  if (ua.includes('safari')) return 'safari'
  return 'other'
}

const BROWSER_LABEL: Record<Browser, string> = {
  chrome: 'Chrome', edge: 'Edge', brave: 'Brave', firefox: 'Firefox', safari: 'Safari', other: 'your browser',
}

// Chromium-family load-unpacked steps vs Firefox temporary add-on steps.
function installSteps(browser: Browser): { url: string; urlLabel: string; steps: string[] } {
  if (browser === 'firefox') {
    return {
      url: 'about:debugging#/runtime/this-firefox',
      urlLabel: 'about:debugging',
      steps: [
        'Download the extension (button below) and unzip it',
        'Open about:debugging#/runtime/this-firefox',
        'Click "Load Temporary Add-on…"',
        'Select the manifest.json inside the unzipped folder',
        'Allow the microphone when prompted, then come back and click "Recheck"',
      ],
    }
  }
  if (browser === 'safari') {
    return {
      url: '',
      urlLabel: '',
      steps: [
        'Safari requires extensions to be packaged via Xcode and is not supported for this dev build.',
        'Please use Chrome, Edge, Brave, or Firefox for the JARVIS voice extension.',
      ],
    }
  }
  // Chromium family
  const extUrl =
    browser === 'edge' ? 'edge://extensions'
    : browser === 'brave' ? 'brave://extensions'
    : 'chrome://extensions'
  return {
    url: extUrl,
    urlLabel: extUrl,
    steps: [
      'Download the extension (button below) and unzip it',
      `Open ${extUrl} in a new tab`,
      'Turn on "Developer mode" (top-right toggle)',
      'Click "Load unpacked" and select the unzipped folder',
      'Allow the microphone when prompted, then come back and click "Recheck"',
    ],
  }
}

export default function ExtensionInstallPrompt() {
  const [browser, setBrowser] = useState<Browser>('other')
  const [extConnected, setExtConnected] = useState(false)
  const [show, setShow] = useState(false)        // banner visible
  const [modalOpen, setModalOpen] = useState(false)
  const [downloaded, setDownloaded] = useState(false)
  const [latestVersion, setLatestVersion] = useState<string | null>(null)
  const [installedVersion, setInstalledVersion] = useState<string | null>(null)
  const [downloading, setDownloading] = useState(false)

  // The installed extension is OUT OF DATE when we know both versions and the
  // installed one is lower than the latest the backend serves.
  const outdated = !!(extConnected && installedVersion && latestVersion &&
    cmpVersions(installedVersion, latestVersion) < 0)

  // Fetch the latest version from the backend so the download button always shows
  // the correct version and uses the versioned filename.
  // Use a short 1.5s timeout so the version resolves well before the 3s banner.
  useEffect(() => {
    fetch(versionUrl(), {
      signal: AbortSignal.timeout(1500),
    })
      .then(r => r.ok ? r.json() : null)
      .then(d => {
        // Use the version from the manifest (served by backend) — never hardcode it.
        if (d?.version) setLatestVersion(d.version)
        else setLatestVersion('3.3.0')   // offline fallback — matches manifest
      })
      .catch(() => setLatestVersion('3.3.0'))   // backend offline fallback
  }, [])

  // Detect browser + listen for the extension handshake
  useEffect(() => {
    setBrowser(detectBrowser())
    if (typeof window === 'undefined') return

    let connected = false
    const markConnected = (version?: string) => {
      connected = true
      setExtConnected(true)
      // Record the installed version (from the handshake or the DOM attribute) so
      // we can detect when a newer release is available. We do NOT force-hide the
      // prompt here — the render guard decides based on `outdated`.
      const v = version || document.documentElement.getAttribute('data-jarvis-ext-version') || ''
      if (v) setInstalledVersion(v)
    }

    // 1. Synchronous check: the content script sets a DOM attribute on load.
    //    This detects the extension instantly with no race condition.
    if (document.documentElement.getAttribute('data-jarvis-ext') === '1') {
      markConnected(document.documentElement.getAttribute('data-jarvis-ext-version') || undefined)
      return
    }

    // 2. Listen for the postMessage handshake (covers slower content scripts)
    const onMsg = (e: MessageEvent) => {
      if (e.source !== window) return
      if (e.data && e.data.__jarvisExt === true && e.data.type === 'connected') {
        markConnected(typeof e.data.version === 'string' ? e.data.version : undefined)
      }
    }
    window.addEventListener('message', onMsg)

    // 3. Actively ping the extension a few times in case its 'connected' fired
    //    before our listener mounted.
    const ping = () => {
      try { window.postMessage({ __jarvisPage: true, type: 'ping' }, window.location.origin) } catch { /* noop */ }
      // Also re-check the DOM attribute each time
      if (document.documentElement.getAttribute('data-jarvis-ext') === '1') markConnected()
    }
    ping()
    const p1 = setTimeout(ping, 400)
    const p2 = setTimeout(ping, 1200)

    // If still no handshake after 3s and the user hasn't dismissed → show prompt
    const dismissed = localStorage.getItem(DISMISS_KEY) === '1'
    const t = setTimeout(() => {
      if (!connected && !dismissed) setShow(true)
    }, 3000)

    return () => {
      window.removeEventListener('message', onMsg)
      clearTimeout(t); clearTimeout(p1); clearTimeout(p2)
    }
  }, [])

  // When the installed extension is OUT OF DATE, surface the update banner —
  // unless the user already snoozed THIS specific new version.
  useEffect(() => {
    if (!outdated || !latestVersion) return
    let snoozed = false
    try { snoozed = localStorage.getItem(UPDATE_DISMISS_PREFIX + latestVersion) === '1' } catch { /* ignore */ }
    if (!snoozed) setShow(true)
  }, [outdated, latestVersion])

  const dismiss = useCallback(() => {
    setShow(false); setModalOpen(false)
    try {
      if (outdated && latestVersion) {
        // Snooze only this version's update notice; a future release re-prompts.
        localStorage.setItem(UPDATE_DISMISS_PREFIX + latestVersion, '1')
      } else {
        localStorage.setItem(DISMISS_KEY, '1')
      }
    } catch { /* ignore */ }
  }, [outdated, latestVersion])

  const download = useCallback(async () => {
    setDownloading(true)
    const filename = `jarvis-extension-v${latestVersion}.zip`
    try {
      // Try dynamic backend endpoint first (always current files + versioned name)
      const res = await fetch(`${zipUrl()}?v=${latestVersion}`, {
        signal: AbortSignal.timeout(15000),
      })
      if (!res.ok) throw new Error('backend unavailable')
      const blob = await res.blob()
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = filename
      document.body.appendChild(a)
      a.click()
      a.remove()
      URL.revokeObjectURL(url)
    } catch {
      // Fallback to static file
      const a = document.createElement('a')
      a.href = ZIP_URL_FALLBACK
      a.download = filename
      document.body.appendChild(a)
      a.click()
      a.remove()
    }
    setDownloading(false)
    setDownloaded(true)
  }, [latestVersion])

  const recheck = useCallback(() => {
    // The content script posts "connected" on load; reloading re-triggers it.
    window.location.reload()
  }, [])

  // Allow any part of the app (e.g. the sidebar "Extension" entry) to open this
  // install dialog on demand — even after the user dismissed the auto-banner.
  // We clear the dismiss flag and force the modal open regardless of state.
  useEffect(() => {
    if (typeof window === 'undefined') return
    const openNow = () => {
      try { localStorage.removeItem(DISMISS_KEY) } catch { /* ignore */ }
      setExtConnected(false)
      setShow(true)
      setModalOpen(true)
    }
    window.addEventListener('jarvis-open-extension-install', openNow as EventListener)
    return () => window.removeEventListener('jarvis-open-extension-install', openNow as EventListener)
  }, [])

  // When opened on demand we still render the modal even if a stale "connected"
  // flag was set, so the menu entry is always actionable. We also keep rendering
  // when the installed extension is outdated, so the update prompt can show.
  if (extConnected && !outdated && !modalOpen) return null

  const info = installSteps(browser)

  return (
    <>
      {/* ── Banner (bottom-left) ─────────────────────────────────────────── */}
      {show && !modalOpen && (
        <div
          className="fixed bottom-5 left-5 z-50 w-[340px] bg-gray-900 border border-cyan-700/50 rounded-2xl shadow-2xl overflow-hidden"
          style={{ animation: 'jarvisFadeIn 0.3s ease' }}
        >
          <div className="flex items-start gap-3 p-4">
            <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-cyan-500 to-violet-500 flex items-center justify-center shrink-0">
              <Mic className="w-5 h-5 text-white" />
            </div>
            <div className="flex-1 min-w-0">
              <div className="text-sm font-semibold text-white">
                {outdated ? 'Update JARVIS Voice' : 'Enable JARVIS Voice'}
              </div>
              <p className="text-[11px] text-gray-400 mt-0.5 leading-relaxed">
                {outdated
                  ? `A new version (v${latestVersion}) is available — you have v${installedVersion}. Update for the latest features and fixes.`
                  : `Install the ${BROWSER_LABEL[browser]} extension for reliable voice recognition and desktop notifications.`}
              </p>
            </div>
            <button onClick={dismiss} className="p-1 hover:bg-gray-800 rounded shrink-0" aria-label="Dismiss">
              <X className="w-4 h-4 text-gray-500" />
            </button>
          </div>
          <div className="flex gap-2 px-4 pb-4">
            <button
              onClick={() => { download(); setModalOpen(true) }}
              disabled={downloading}
              className="flex-1 flex items-center justify-center gap-1.5 py-2 rounded-lg bg-cyan-600 hover:bg-cyan-500 disabled:opacity-60 text-white text-xs font-medium transition"
            >
              <Download className="w-3.5 h-3.5" /> {outdated ? `Update to v${latestVersion}` : `Install v${latestVersion}`}
            </button>
            <button
              onClick={dismiss}
              className="px-3 py-2 rounded-lg text-gray-400 hover:text-white hover:bg-gray-800 text-xs transition"
            >
              Not now
            </button>
          </div>
        </div>
      )}

      {/* ── Install modal ─────────────────────────────────────────────────── */}
      {modalOpen && (
        <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/60 backdrop-blur-sm p-4" onClick={() => setModalOpen(false)}>
          <div
            className="w-full max-w-md bg-gray-900 border border-gray-700 rounded-2xl shadow-2xl overflow-hidden max-h-[90vh] overflow-y-auto"
            onClick={e => e.stopPropagation()}
          >
            {/* Header */}
            <div className="flex items-center gap-3 px-5 py-4 border-b border-gray-700/50 bg-gradient-to-r from-cyan-900/30 to-violet-900/30">
              <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-cyan-500 to-violet-500 flex items-center justify-center">
                <Mic className="w-5 h-5 text-white" />
              </div>
              <div className="flex-1">
                <h2 className="text-base font-semibold text-white">JARVIS Voice Assistant</h2>
                <p className="text-[11px] text-gray-400">{BROWSER_LABEL[browser]} extension</p>
              </div>
              <button onClick={() => setModalOpen(false)} className="p-1.5 hover:bg-gray-800 rounded">
                <X className="w-4 h-4 text-gray-400" />
              </button>
            </div>

            {/* What it does */}
            <div className="px-5 py-4 space-y-3">
              <div className="text-[11px] font-semibold text-cyan-300 uppercase tracking-wide">What you get</div>
              <div className="grid gap-2.5">
                {[
                  { icon: <Mic className="w-4 h-4 text-cyan-400" />, title: 'Reliable voice recognition', desc: 'Listens persistently — survives page reloads and never loses the mic' },
                  { icon: <ShieldCheck className="w-4 h-4 text-green-400" />, title: 'Ignores TV & background noise', desc: 'Only the "Hey Jarvis" wake phrase activates it' },
                  { icon: <Bell className="w-4 h-4 text-amber-400" />, title: 'Desktop notifications', desc: 'JARVIS replies, trades & alerts pop up bottom-right of your screen' },
                  { icon: <Zap className="w-4 h-4 text-violet-400" />, title: 'Hands-free control', desc: 'Navigate pages, click buttons and place trades by voice' },
                ].map((f, i) => (
                  <div key={i} className="flex items-start gap-2.5 p-2.5 rounded-lg bg-gray-800/50 border border-gray-700/40">
                    <div className="mt-0.5 shrink-0">{f.icon}</div>
                    <div>
                      <div className="text-xs font-medium text-white">{f.title}</div>
                      <div className="text-[10px] text-gray-400 leading-relaxed">{f.desc}</div>
                    </div>
                  </div>
                ))}
              </div>
            </div>

            {/* Steps */}
            <div className="px-5 pb-3 space-y-3">
              <div className="text-[11px] font-semibold text-cyan-300 uppercase tracking-wide">Install in {BROWSER_LABEL[browser]}</div>
              <ol className="space-y-2">
                {info.steps.map((step, i) => (
                  <li key={i} className="flex items-start gap-2.5 text-[11px] text-gray-300">
                    <span className="w-5 h-5 rounded-full bg-cyan-600/20 text-cyan-300 flex items-center justify-center text-[10px] font-bold shrink-0 mt-0.5">{i + 1}</span>
                    <span className="leading-relaxed">{step}</span>
                  </li>
                ))}
              </ol>
              {info.url && (
                <div className="flex items-center gap-2 px-3 py-2 rounded-lg bg-gray-800 border border-gray-700">
                  <code className="text-[11px] text-cyan-300 flex-1 truncate">{info.urlLabel}</code>
                  <button
                    onClick={() => { navigator.clipboard?.writeText(info.url).catch(() => {}) }}
                    className="text-[10px] px-2 py-0.5 rounded bg-gray-700 hover:bg-gray-600 text-gray-300 transition"
                  >
                    Copy
                  </button>
                </div>
              )}
            </div>

            {/* Actions */}
            <div className="px-5 py-4 border-t border-gray-700/50 space-y-2">
              {/* Honest note: browsers don't allow silent extension install */}
              {browser !== 'safari' && (
                <div className="flex items-start gap-2 px-3 py-2 rounded-lg bg-amber-900/20 border border-amber-700/30 mb-1">
                  <ShieldCheck className="w-3.5 h-3.5 text-amber-400 shrink-0 mt-0.5" />
                  <p className="text-[10px] text-amber-200/80 leading-relaxed">
                    For your security, browsers don't let any website silently install extensions.
                    The download is automatic — loading it takes one 10-second step (above). You only do this once.
                  </p>
                </div>
              )}
              {browser !== 'safari' && (
                <button
                  onClick={download}
                  disabled={downloading}
                  className="w-full flex items-center justify-center gap-2 py-2.5 rounded-lg bg-cyan-600 hover:bg-cyan-500 disabled:opacity-60 text-white text-sm font-medium transition"
                >
                  <Download className="w-4 h-4" />
                  {downloading
                    ? 'Downloading…'
                    : downloaded
                    ? `Re-download v${latestVersion}`
                    : `Download extension v${latestVersion}`}
                </button>
              )}
              {downloaded && (
                <button
                  onClick={recheck}
                  className="w-full flex items-center justify-center gap-2 py-2.5 rounded-lg bg-green-600 hover:bg-green-500 text-white text-sm font-medium transition"
                >
                  <CheckCircle2 className="w-4 h-4" /> I've loaded it — Recheck
                </button>
              )}
              <button
                onClick={dismiss}
                className="w-full py-2 rounded-lg text-gray-400 hover:text-white hover:bg-gray-800 text-xs transition"
              >
                Don't show this again
              </button>
            </div>
          </div>
        </div>
      )}

      <style jsx global>{`
        @keyframes jarvisFadeIn { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: translateY(0); } }
      `}</style>
    </>
  )
}
