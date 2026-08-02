/**
 * Preload — the only bridge between the renderer and Electron.
 *
 * Deliberately tiny. The renderer is a full trading UI that talks to a local
 * HTTP API; it has no business touching the filesystem, the shell, or arbitrary
 * IPC, so nothing of the sort is exposed here.
 *
 * Everything is read synchronously from `additionalArguments` rather than over
 * async IPC: `services/api.ts` builds its axios instance at module scope, so a
 * value that arrives after a promise resolves would land too late.
 *
 *   __TRADEBOT_DESKTOP__  marks the packaged app. `PaulChat.tsx` reads it to
 *                         skip Web Speech — Electron ships Chromium's speech
 *                         API without the Google API key it needs, so it fails
 *                         silently; the Deepgram fallback is used instead.
 *
 *   __TRADEBOT_API_URL__  the backend's address. The port is chosen at launch,
 *                         so it cannot be baked into the static frontend.
 *                         `getApiBaseUrl()` in services/api.ts reads it first.
 */
const { contextBridge } = require('electron')

function readArg(flag) {
  const prefix = `--${flag}=`
  const hit = process.argv.find((arg) => arg.startsWith(prefix))
  return hit ? hit.slice(prefix.length) : null
}

contextBridge.exposeInMainWorld('__TRADEBOT_DESKTOP__', true)

const apiUrl = readArg('tradebot-api-url')
if (apiUrl) {
  contextBridge.exposeInMainWorld('__TRADEBOT_API_URL__', apiUrl)
}

contextBridge.exposeInMainWorld('__TRADEBOT_INFO__', {
  version: readArg('tradebot-version'),
  dataDir: readArg('tradebot-data-dir'),
  platform: process.platform,
})
