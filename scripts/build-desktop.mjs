#!/usr/bin/env node
/**
 * Build the TradeBot desktop app for the host platform.
 *
 *   node scripts/build-desktop.mjs            # full build → dist-desktop/
 *   node scripts/build-desktop.mjs --dir      # unpacked app only, much faster
 *   node scripts/build-desktop.mjs --stage    # stage the payload, don't package
 *
 * Steps, in order:
 *   1. fetch a relocatable CPython for this platform
 *   2. install the backend's dependencies into it
 *   3. export the frontend to static files
 *   4. stage everything under desktop/resources/
 *   5. run the secret-leak guard
 *   6. hand off to electron-builder
 *
 * There is no cross-compilation: the Python runtime and its native wheels are
 * platform-specific, so a macOS build must run on macOS, Windows on Windows and
 * Linux on Linux. The CI matrix in .github/workflows/desktop-release.yml does
 * exactly that.
 */
import { spawnSync } from 'node:child_process'
import { existsSync, mkdirSync, rmSync, cpSync, readdirSync, statSync } from 'node:fs'
import { join, dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const DESKTOP = join(ROOT, 'desktop')
const RESOURCES = join(DESKTOP, 'resources')
const VENDOR = join(ROOT, '.build-cache')

// ── Python runtime ──────────────────────────────────────────────────────────
//
// Pinned for reproducible builds. Bump both together; the release list is at
// https://github.com/astral-sh/python-build-standalone/releases
// Override with PBS_RELEASE / PYTHON_VERSION when testing a newer runtime.
//
// 3.12 rather than the 3.13 used in development: every wheel this project needs
// (numpy, pandas, ccxt, cryptography, asyncpg) ships a 3.12 binary on all three
// platforms, so nothing has to compile from source on a build agent.
const PBS_RELEASE = process.env.PBS_RELEASE || '20241219'
const PYTHON_VERSION = process.env.PYTHON_VERSION || '3.12.8'

const PBS_TRIPLES = {
  'darwin-arm64': 'aarch64-apple-darwin',
  'darwin-x64': 'x86_64-apple-darwin',
  'win32-x64': 'x86_64-pc-windows-msvc',
  'linux-x64': 'x86_64-unknown-linux-gnu',
  'linux-arm64': 'aarch64-unknown-linux-gnu',
}

const PLATFORM_KEY = `${process.platform}-${process.arch}`
const IS_WINDOWS = process.platform === 'win32'

// ── Helpers ─────────────────────────────────────────────────────────────────

function step(msg) {
  console.log(`\n\x1b[36m▶\x1b[0m ${msg}`)
}
function ok(msg) {
  console.log(`  \x1b[32m✓\x1b[0m ${msg}`)
}
function die(msg) {
  console.error(`\n\x1b[31m✗ ${msg}\x1b[0m\n`)
  process.exit(1)
}

function run(cmd, args, opts = {}) {
  const res = spawnSync(cmd, args, {
    stdio: 'inherit',
    shell: IS_WINDOWS, // npm/npx are .cmd shims on Windows
    ...opts,
  })
  if (res.error) die(`${cmd} failed to launch: ${res.error.message}`)
  if (res.status !== 0) die(`${cmd} ${args.join(' ')} exited with ${res.status}`)
}

function dirSizeMb(dir) {
  let total = 0
  const stack = [dir]
  while (stack.length) {
    const current = stack.pop()
    for (const entry of readdirSync(current, { withFileTypes: true })) {
      const full = join(current, entry.name)
      if (entry.isDirectory()) stack.push(full)
      else if (entry.isFile()) {
        try {
          total += statSync(full).size
        } catch {
          /* vanished mid-walk */
        }
      }
    }
  }
  return (total / 1024 / 1024).toFixed(0)
}

// ── 1. Python runtime ───────────────────────────────────────────────────────

function fetchPython() {
  step(`Fetching CPython ${PYTHON_VERSION} for ${PLATFORM_KEY}`)

  const triple = PBS_TRIPLES[PLATFORM_KEY]
  if (!triple) {
    die(
      `No prebuilt Python for ${PLATFORM_KEY}.\n` +
        `  Supported: ${Object.keys(PBS_TRIPLES).join(', ')}`,
    )
  }

  const archive = `cpython-${PYTHON_VERSION}+${PBS_RELEASE}-${triple}-install_only.tar.gz`
  const url = `https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_RELEASE}/${archive}`
  const cached = join(VENDOR, archive)

  mkdirSync(VENDOR, { recursive: true })

  if (existsSync(cached)) {
    ok(`cached: ${archive}`)
  } else {
    console.log(`  downloading ${url}`)
    run('curl', ['-fL', '--retry', '3', '-o', cached, url])
    ok(`downloaded ${archive}`)
  }

  const target = join(RESOURCES, 'python')
  rmSync(target, { recursive: true, force: true })
  mkdirSync(RESOURCES, { recursive: true })

  // The install_only archive contains a single top-level `python/` directory.
  run('tar', ['-xzf', cached, '-C', RESOURCES])
  if (!existsSync(target)) die(`Expected ${target} after extracting ${archive}`)

  ok(`extracted to ${target}`)
  return IS_WINDOWS
    ? join(target, 'python.exe')
    : join(target, 'bin', 'python3')
}

// ── 2. Backend dependencies ─────────────────────────────────────────────────

function installBackendDeps(python) {
  step('Installing backend dependencies into the bundled runtime')

  const requirements = join(ROOT, 'backend', 'requirements.txt')
  if (!existsSync(requirements)) die(`Missing ${requirements}`)

  run(python, ['-m', 'pip', 'install', '--upgrade', 'pip', '--no-warn-script-location'])

  // Pure-Python packages that publish no wheel at all. Each must be verified
  // as needing no compiler before being added here — the point of
  // `--only-binary=:all:` is that a build agent fails loudly instead of
  // silently compiling C or Rust (and producing a broken or huge bundle).
  //   sgmllib3k — a ~5KB shim pulled in by feedparser; sdist-only since 2010.
  const SDIST_ALLOWED = ['sgmllib3k']

  run(python, [
    '-m',
    'pip',
    'install',
    '--no-warn-script-location',
    '--only-binary=:all:',
    ...SDIST_ALLOWED.flatMap((pkg) => ['--no-binary', pkg]),
    '-r',
    requirements,
  ])

  // Trim what the desktop app will never run. pytest and its plugins are ~30MB
  // of test tooling; ngrok is a dev tunnelling helper that has no place in a
  // packaged consumer app.
  const drop = [
    'pytest',
    'pytest-asyncio',
    'ngrok',
  ]
  spawnSync(python, ['-m', 'pip', 'uninstall', '-y', ...drop], { stdio: 'inherit' })

  ok('dependencies installed')
}

// ── 3. Frontend static export ───────────────────────────────────────────────

function buildFrontend() {
  step('Exporting the frontend to static files')

  const frontend = join(ROOT, 'frontend')
  const out = join(frontend, 'out')

  // A stale out/ gets picked up by the TypeScript pass on the next build.
  rmSync(out, { recursive: true, force: true })

  if (!existsSync(join(frontend, 'node_modules'))) {
    run('npm', ['ci'], { cwd: frontend })
  }

  run('npx', ['next', 'build'], {
    cwd: frontend,
    env: {
      ...process.env,
      TRADEBOT_DESKTOP: '1',
      // Resolve the API from window.location: the backend serves this bundle,
      // and the port is only known at launch.
      NEXT_PUBLIC_API_SAME_ORIGIN: '1',
      // Must be blank, or a developer's .env.local bakes in a fixed port and
      // the packaged app talks to the wrong address.
      NEXT_PUBLIC_API_URL: '',
      NODE_OPTIONS: '--max-old-space-size=4096',
    },
  })

  if (!existsSync(join(out, 'index.html'))) {
    die(`next build produced no ${join(out, 'index.html')}`)
  }
  ok(`exported (${dirSizeMb(out)} MB)`)
  return out
}

// ── 4. Stage the payload ────────────────────────────────────────────────────

function stage(frontendOut) {
  step('Staging the app payload')

  // Copy only what the backend actually imports at runtime. Deliberately
  // narrow: `backend/` also holds .venv, logs, a Telegram session and ad-hoc
  // scripts, none of which may ship.
  const backendSrc = join(ROOT, 'backend', 'app')
  const backendDst = join(RESOURCES, 'backend', 'app')
  rmSync(join(RESOURCES, 'backend'), { recursive: true, force: true })
  mkdirSync(dirname(backendDst), { recursive: true })
  cpSync(backendSrc, backendDst, {
    recursive: true,
    filter: (src) => !src.includes('__pycache__') && !src.endsWith('.pyc'),
  })

  const pluginsDst = join(RESOURCES, 'plugins')
  rmSync(pluginsDst, { recursive: true, force: true })
  cpSync(join(ROOT, 'plugins'), pluginsDst, {
    recursive: true,
    filter: (src) =>
      !src.includes('__pycache__') &&
      !src.includes('node_modules') &&
      !src.includes('/tests/') &&
      !src.endsWith('.pyc') &&
      !src.endsWith('.session') &&
      !src.endsWith('.log'),
  })

  const frontendDst = join(RESOURCES, 'frontend')
  rmSync(frontendDst, { recursive: true, force: true })
  cpSync(frontendOut, frontendDst, { recursive: true })

  // Seed data the backend reads on first run (core/database.py).
  cpSync(join(ROOT, 'pinescripts.json'), join(RESOURCES, 'pinescripts.json'))

  ok(`staged into ${RESOURCES} (${dirSizeMb(RESOURCES)} MB)`)
}

// ── 5. Guard ────────────────────────────────────────────────────────────────

function guard() {
  step('Checking the staged payload for secrets')
  run('node', [join(ROOT, 'scripts', 'check-no-secrets.mjs'), RESOURCES])
}

// ── 6. Package ──────────────────────────────────────────────────────────────

function packageApp(dirOnly) {
  step(dirOnly ? 'Packaging (unpacked)' : 'Packaging installers')

  if (!existsSync(join(DESKTOP, 'node_modules'))) {
    run('npm', ['ci'], { cwd: DESKTOP })
  }

  const args = ['electron-builder']
  if (dirOnly) args.push('--dir')
  run('npx', args, { cwd: DESKTOP })

  ok(`output in ${join(ROOT, 'dist-desktop')}`)
}

// ── Main ────────────────────────────────────────────────────────────────────

const args = new Set(process.argv.slice(2))
const stageOnly = args.has('--stage')
const dirOnly = args.has('--dir')

console.log(`\nTradeBot desktop build — ${PLATFORM_KEY}`)

const python = fetchPython()
installBackendDeps(python)
const frontendOut = buildFrontend()
stage(frontendOut)
guard()

if (stageOnly) {
  console.log('\n--stage: payload staged and verified; skipping packaging.\n')
  process.exit(0)
}

packageApp(dirOnly)
console.log('\n\x1b[32mDone.\x1b[0m\n')
