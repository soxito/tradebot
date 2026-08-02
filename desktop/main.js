/**
 * TradeBot desktop — Electron main process.
 *
 * Owns exactly one child process: the Python backend (`app.desktop_main`),
 * which serves both the API and the exported frontend on a single loopback
 * port. The window then just loads that port.
 *
 * The ordering matters and is not arbitrary:
 *   1. take the single-instance lock  (two copies would fight over the SQLite
 *      DB and run the trading loops twice)
 *   2. pick a free port
 *   3. spawn the backend
 *   4. poll /health until it answers, showing a splash meanwhile
 *   5. load the UI
 */
const { app, BrowserWindow, Menu, shell, dialog } = require('electron')
const { spawn, spawnSync } = require('node:child_process')
const path = require('node:path')
const fs = require('node:fs')
const net = require('node:net')
const http = require('node:http')

const IS_WINDOWS = process.platform === 'win32'
const IS_MAC = process.platform === 'darwin'
const IS_PACKAGED = app.isPackaged

// Prefer the port the docs and the dev setup use, so a user's bookmarks and any
// external webhook config keep working; fall back upward if it is taken.
const PREFERRED_PORT = 1448
const PORT_SCAN_LIMIT = 40
const BACKEND_READY_TIMEOUT_MS = 120_000

let mainWindow = null
let splashWindow = null
let backend = null
let backendPort = null
let shuttingDown = false

// ── Paths ───────────────────────────────────────────────────────────────────

/**
 * Where the staged payload lives.
 *
 * Packaged: `process.resourcesPath` (Contents/Resources on macOS).
 * Unpackaged (`npm start` in desktop/): the repo checkout, so the shell can be
 * exercised against a normal dev tree without running the full build.
 */
function resourceRoot() {
  return IS_PACKAGED ? process.resourcesPath : path.resolve(__dirname, '..')
}

function pythonExecutable() {
  const root = resourceRoot()
  if (IS_PACKAGED) {
    return IS_WINDOWS
      ? path.join(root, 'python', 'python.exe')
      : path.join(root, 'python', 'bin', 'python3')
  }
  // Dev: reuse the backend virtualenv that start.py builds.
  const venv = path.join(root, 'backend', '.venv')
  return IS_WINDOWS
    ? path.join(venv, 'Scripts', 'python.exe')
    : path.join(venv, 'bin', 'python')
}

function backendDir() {
  return path.join(resourceRoot(), 'backend')
}

function staticDir() {
  const root = resourceRoot()
  return IS_PACKAGED
    ? path.join(root, 'frontend')
    : path.join(root, 'frontend', 'out')
}

// ── Port selection ──────────────────────────────────────────────────────────

function isPortFree(port) {
  return new Promise((resolve) => {
    const server = net.createServer()
    server.once('error', () => resolve(false))
    server.once('listening', () => server.close(() => resolve(true)))
    // Bind the same interface the backend will, or the check proves nothing.
    server.listen(port, '127.0.0.1')
  })
}

async function findFreePort() {
  for (let port = PREFERRED_PORT; port < PREFERRED_PORT + PORT_SCAN_LIMIT; port++) {
    if (await isPortFree(port)) return port
  }
  throw new Error(
    `No free port in ${PREFERRED_PORT}–${PREFERRED_PORT + PORT_SCAN_LIMIT - 1}`,
  )
}

// ── Backend lifecycle ───────────────────────────────────────────────────────

function userDataDir() {
  const dir = app.getPath('userData')
  fs.mkdirSync(dir, { recursive: true })
  return dir
}

function startBackend(port) {
  const python = pythonExecutable()
  if (!fs.existsSync(python)) {
    throw new Error(
      `Python runtime missing at ${python}.\n` +
        (IS_PACKAGED
          ? 'The installation looks incomplete — please reinstall TradeBot.'
          : 'Run `python start.py` once to build backend/.venv, or run the desktop build script.'),
    )
  }

  const dataDir = userDataDir()
  const logPath = path.join(dataDir, 'backend.log')
  const logStream = fs.createWriteStream(logPath, { flags: 'a' })

  const env = {
    ...process.env,
    TRADEBOT_PORT: String(port),
    TRADEBOT_DATA_DIR: dataDir,
    TRADEBOT_STATIC_DIR: staticDir(),
    PYTHONUNBUFFERED: '1',
    // The plugin loader imports `plugins.<name>...` by string, so the directory
    // holding plugins/ must be importable.
    PYTHONPATH: resourceRoot(),
  }
  // Never inherit a developer's API URL override into the packaged app.
  delete env.NEXT_PUBLIC_API_URL

  const child = spawn(python, ['-m', 'app.desktop_main'], {
    cwd: backendDir(),
    env,
    stdio: ['ignore', 'pipe', 'pipe'],
    // Own process group, so killing it takes any grandchildren with it.
    detached: !IS_WINDOWS,
    windowsHide: true,
  })

  child.stdout.pipe(logStream)
  child.stderr.pipe(logStream)

  child.on('exit', (code, signal) => {
    if (shuttingDown) return
    dialog.showErrorBox(
      'TradeBot backend stopped',
      `The backend exited unexpectedly (code ${code}, signal ${signal}).\n\n` +
        `Log: ${logPath}`,
    )
    app.quit()
  })

  return child
}

function waitForBackend(port, timeoutMs) {
  const deadline = Date.now() + timeoutMs
  return new Promise((resolve, reject) => {
    const attempt = () => {
      if (shuttingDown) return reject(new Error('shutting down'))
      const req = http.get(
        { host: '127.0.0.1', port, path: '/health', timeout: 2000 },
        (res) => {
          res.resume()
          if (res.statusCode === 200) return resolve()
          retry()
        },
      )
      req.on('error', retry)
      req.on('timeout', () => {
        req.destroy()
        retry()
      })
    }
    const retry = () => {
      if (Date.now() > deadline) {
        return reject(
          new Error(`Backend did not become ready within ${timeoutMs / 1000}s`),
        )
      }
      setTimeout(attempt, 400)
    }
    attempt()
  })
}

// How long the backend gets to shut down cleanly before it is killed outright.
const BACKEND_SHUTDOWN_GRACE_MS = 5000

function signalBackend(signal) {
  if (!backend || backend.exitCode !== null) return
  const pid = backend.pid
  try {
    if (IS_WINDOWS) {
      // A bare kill() on Windows leaves grandchildren running and the port
      // held; /T walks the tree, /F forces.
      spawnSync('taskkill', ['/pid', String(pid), '/T', '/F'], { windowsHide: true })
    } else {
      // Negative PID targets the whole process group — the backend was spawned
      // detached specifically so this reaches anything it started.
      process.kill(-pid, signal)
    }
  } catch {
    /* already gone */
  }
}

/**
 * Stop the backend, escalating to SIGKILL, then call `done`.
 *
 * The escalation timer only works if the app is still alive to run it, which is
 * why `before-quit` defers the quit until this finishes. An earlier version
 * fired SIGTERM and exited immediately: uvicorn closed its socket but a
 * non-daemon worker thread kept the process alive, so every quit orphaned a
 * Python process still holding the SQLite database — and the next launch, now
 * finding the port free, started a second backend writing the same file.
 */
function stopBackend(done = () => {}) {
  if (!backend || backend.exitCode !== null) return done()

  let finished = false
  const finish = () => {
    if (finished) return
    finished = true
    clearTimeout(timer)
    done()
  }

  const timer = setTimeout(() => {
    signalBackend('SIGKILL')
    finish()
  }, BACKEND_SHUTDOWN_GRACE_MS)

  backend.once('exit', finish)
  signalBackend('SIGTERM')
}

/**
 * Last-resort synchronous kill for paths that cannot wait — `process.on('exit')`
 * runs no timers and no async work, so politeness is not an option there.
 */
function killBackendNow() {
  signalBackend('SIGKILL')
}

// ── Windows ─────────────────────────────────────────────────────────────────

function createSplash() {
  splashWindow = new BrowserWindow({
    width: 420,
    height: 260,
    frame: false,
    resizable: false,
    center: true,
    show: true,
    backgroundColor: '#0b1220',
    webPreferences: { contextIsolation: true, nodeIntegration: false },
  })
  splashWindow.loadFile(path.join(__dirname, 'splash.html'))
}

function createMainWindow(port) {
  mainWindow = new BrowserWindow({
    width: 1600,
    height: 1000,
    minWidth: 1024,
    minHeight: 700,
    show: false,
    backgroundColor: '#0b1220',
    title: 'TradeBot',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      // Read synchronously by the preload, before any page script runs — the
      // frontend resolves its API base URL at module scope.
      additionalArguments: [
        `--tradebot-api-url=http://127.0.0.1:${port}/api/v1`,
        `--tradebot-version=${app.getVersion()}`,
        `--tradebot-data-dir=${userDataDir()}`,
      ],
      // The UI is WebGL-heavy (Three.js, the S.O.X particle orb, force graphs)
      // and renders off-thread via OffscreenCanvas.
      backgroundThrottling: false,
    },
  })

  mainWindow.once('ready-to-show', () => {
    splashWindow?.destroy()
    splashWindow = null
    mainWindow.show()
  })

  // External links open in the user's real browser, never in the app shell.
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url)
    return { action: 'deny' }
  })
  mainWindow.webContents.on('will-navigate', (event, url) => {
    if (!url.startsWith(`http://127.0.0.1:${port}`)) {
      event.preventDefault()
      shell.openExternal(url)
    }
  })

  mainWindow.on('closed', () => {
    mainWindow = null
  })

  mainWindow.loadURL(`http://127.0.0.1:${port}/`)
}

function buildMenu(port) {
  const template = [
    ...(IS_MAC ? [{ role: 'appMenu' }] : []),
    { role: 'fileMenu' },
    { role: 'editMenu' },
    {
      label: 'View',
      submenu: [
        { role: 'reload' },
        { role: 'forceReload' },
        { role: 'toggleDevTools' },
        { type: 'separator' },
        { role: 'resetZoom' },
        { role: 'zoomIn' },
        { role: 'zoomOut' },
        { type: 'separator' },
        { role: 'togglefullscreen' },
      ],
    },
    { role: 'windowMenu' },
    {
      role: 'help',
      submenu: [
        {
          label: 'Open Data Folder',
          click: () => shell.openPath(userDataDir()),
        },
        {
          label: 'View Backend Log',
          click: () => shell.openPath(path.join(userDataDir(), 'backend.log')),
        },
        { type: 'separator' },
        {
          label: 'API Documentation',
          click: () => shell.openExternal(`http://127.0.0.1:${port}/docs`),
        },
      ],
    },
  ]
  Menu.setApplicationMenu(Menu.buildFromTemplate(template))
}

// ── Startup ─────────────────────────────────────────────────────────────────

// Two instances would run the trading loops twice against one SQLite file.
if (!app.requestSingleInstanceLock()) {
  app.quit()
} else {
  app.on('second-instance', () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore()
      mainWindow.focus()
    }
  })

  app.whenReady().then(async () => {
    createSplash()

    try {
      backendPort = await findFreePort()
      backend = startBackend(backendPort)
      await waitForBackend(backendPort, BACKEND_READY_TIMEOUT_MS)
    } catch (err) {
      splashWindow?.destroy()
      splashWindow = null
      dialog.showErrorBox(
        'TradeBot failed to start',
        `${err.message}\n\nLog: ${path.join(userDataDir(), 'backend.log')}`,
      )
      app.quit()
      return
    }

    buildMenu(backendPort)
    createMainWindow(backendPort)

    // Auto-update. Skipped on macOS until a Developer ID signature exists —
    // electron-updater cannot verify an unsigned macOS update and would fail
    // noisily on every launch.
    if (IS_PACKAGED && !IS_MAC) {
      try {
        const { autoUpdater } = require('electron-updater')
        autoUpdater.checkForUpdatesAndNotify().catch(() => {})
      } catch {
        /* updater unavailable — not fatal */
      }
    }

    app.on('activate', () => {
      if (BrowserWindow.getAllWindows().length === 0 && backendPort) {
        createMainWindow(backendPort)
      }
    })
  })

  app.on('window-all-closed', () => {
    if (!IS_MAC) app.quit()
  })

  // Hold the quit open until the backend is actually gone. Without this the
  // app exits first and the escalation timer never runs.
  let backendStopped = false
  app.on('before-quit', (event) => {
    if (backendStopped) return
    shuttingDown = true
    event.preventDefault()
    stopBackend(() => {
      backendStopped = true
      app.quit()
    })
  })

  // A crash or Ctrl-C must not leave an orphaned backend holding the database.
  // Route signals through app.quit() so they take the graceful path above;
  // process.on('exit') is the synchronous backstop for everything else.
  process.on('exit', killBackendNow)
  for (const signal of ['SIGINT', 'SIGTERM', 'SIGHUP']) {
    process.on(signal, () => {
      shuttingDown = true
      app.quit()
    })
  }
}
