#!/usr/bin/env node
/**
 * Fail the desktop build if anything secret was staged for packaging.
 *
 * The repo root holds a real `.env` with live exchange and LLM credentials, and
 * `tradebot_telegram.session` is an authenticated Telegram session. Either one
 * shipped inside an installer is a credential disclosure to every user who
 * downloads it — and installers are published to GitHub Releases, so it is not
 * recoverable by deleting the file afterwards.
 *
 * electron-builder's `files`/`extraResources` allowlist is the primary defence.
 * This is the independent check on top of it, because an allowlist entry like
 * `resources/backend` copies a whole tree and will happily pick up whatever was
 * left lying in it.
 *
 * Usage:  node scripts/check-no-secrets.mjs <staging-dir> [...more dirs]
 * Exits non-zero, loudly, on the first violation.
 */
import { readdirSync, statSync, readFileSync } from 'node:fs'
import { join, relative, basename, extname } from 'node:path'

// Files that must never be packaged, by exact name.
const FORBIDDEN_NAMES = new Set([
  '.env',
  '.env.local',
  '.env.docker',
  '.env.production',
  '.env.development',
  'tradebot_telegram.session',
  'credentials.json',
  'secrets.json',
  'id_rsa',
  '.npmrc',
  '.netrc',
])

// …and by extension.
const FORBIDDEN_EXTENSIONS = new Set(['.session', '.pem', '.key', '.p12', '.pfx', '.keystore'])

// Directories that carry dev-only state or build-machine paths. `.venv`
// matters most: it hardcodes absolute paths from the build machine and would
// shadow the relocatable runtime we actually ship.
//
// `__pycache__` is deliberately NOT here. It is not a secret, and inside the
// bundled runtime the precompiled bytecode is wanted — it measurably speeds up
// backend startup. Stale bytecode of *our* code is stripped at the copy step in
// build-desktop.mjs instead, which is where that distinction can be made.
const FORBIDDEN_DIRS = new Set([
  '.venv',
  '.venv313',
  'node_modules',
  '.git',
  '.pytest_cache',
  '.next',
])

// Vendored third-party trees. Filename and directory rules still apply, but the
// credential content-scan does not: pinned dependencies like `cryptography`,
// `ecdsa`, `rsa` and `ccxt` legitimately contain PEM header strings in their
// parsing code and test fixtures. Flagging those trains people to ignore this
// check, which is worse than not running it. The guard's job is our secrets.
const VENDOR_MARKERS = ['site-packages', 'dist-packages', 'node_modules']

// Public trust stores, not private material.
const ALLOWED_PEM_FILES = new Set(['cacert.pem', 'ca-bundle.pem', 'cert.pem'])

// Masked example values in documentation — `sk-xxxxxxxx`, `YOUR_API_KEY_HERE`.
// A real credential has entropy; these do not.
const PLACEHOLDER_RE = /^(x{6,}|X{6,}|\.{3,}|<.*>|YOUR[_-]|your[_-]|.*(?:xxxx|XXXX|1234567890|abcdef)).*$/

// Content scan for credentials pasted into files with innocent names. Matches
// the shapes of real keys, not the word "key", so placeholder config doesn't
// trip it.
const SECRET_PATTERNS = [
  { name: 'OpenAI/Anthropic-style API key', re: /\b(sk-[A-Za-z0-9_-]{20,}|sk-ant-[A-Za-z0-9_-]{20,})\b/ },
  { name: 'AWS access key id', re: /\bAKIA[0-9A-Z]{16}\b/ },
  { name: 'GitHub token', re: /\bgh[pousr]_[A-Za-z0-9]{36,}\b/ },
  { name: 'Slack token', re: /\bxox[abprs]-[A-Za-z0-9-]{10,}\b/ },
  { name: 'private key block', re: /-----BEGIN (RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----/ },
  { name: 'Telegram bot token', re: /\b\d{8,10}:AA[A-Za-z0-9_-]{33}\b/ },
]

// Only text-ish files are worth scanning for pasted credentials.
const SCANNABLE_EXTENSIONS = new Set([
  '.py', '.js', '.mjs', '.cjs', '.ts', '.tsx', '.json', '.yaml', '.yml',
  '.toml', '.ini', '.cfg', '.txt', '.md', '.sh', '.env', '.html',
])
const MAX_SCAN_BYTES = 2 * 1024 * 1024

const violations = []

function walk(dir, root) {
  let entries
  try {
    entries = readdirSync(dir, { withFileTypes: true })
  } catch {
    return // unreadable — nothing to package from it either
  }

  for (const entry of entries) {
    const full = join(dir, entry.name)
    const rel = relative(root, full)

    if (entry.isDirectory()) {
      if (FORBIDDEN_DIRS.has(entry.name)) {
        violations.push(`${rel}  →  forbidden directory "${entry.name}"`)
        continue // don't descend; one report is enough
      }
      walk(full, root)
      continue
    }

    if (!entry.isFile()) continue // skip symlinks, sockets, fifos

    const name = basename(entry.name)
    const ext = extname(entry.name).toLowerCase()

    if (FORBIDDEN_NAMES.has(name)) {
      violations.push(`${rel}  →  forbidden filename "${name}"`)
      continue
    }
    if (FORBIDDEN_EXTENSIONS.has(ext) && !ALLOWED_PEM_FILES.has(name)) {
      violations.push(`${rel}  →  forbidden extension "${ext}"`)
      continue
    }
    // `.env.example` is a template of placeholders and is safe; `.env.anything`
    // else is not.
    if (name.startsWith('.env') && name !== '.env.example') {
      violations.push(`${rel}  →  environment file`)
      continue
    }

    if (!SCANNABLE_EXTENSIONS.has(ext)) continue
    if (VENDOR_MARKERS.some((marker) => rel.includes(marker))) continue

    let size
    try {
      size = statSync(full).size
    } catch {
      continue
    }
    if (size > MAX_SCAN_BYTES) continue

    let text
    try {
      text = readFileSync(full, 'utf8')
    } catch {
      continue
    }
    for (const { name: label, re } of SECRET_PATTERNS) {
      const hit = text.match(re)
      if (!hit) continue
      // Strip the well-known prefix before judging: `sk-xxxxxxxx` is a doc
      // placeholder, `sk-` followed by real entropy is not.
      const body = hit[0].replace(/^(sk-ant-|sk-or-|sk-|AKIA|gh[pousr]_|xox[abprs]-)/, '')
      if (PLACEHOLDER_RE.test(body)) continue
      violations.push(`${rel}  →  looks like a ${label}`)
      break
    }
  }
}

const targets = process.argv.slice(2)
if (targets.length === 0) {
  console.error('usage: node scripts/check-no-secrets.mjs <dir> [...dirs]')
  process.exit(2)
}

for (const target of targets) {
  try {
    if (!statSync(target).isDirectory()) {
      console.error(`✗ not a directory: ${target}`)
      process.exit(2)
    }
  } catch {
    console.error(`✗ missing staging directory: ${target}`)
    console.error('  Run the staging step before this check.')
    process.exit(2)
  }
  walk(target, target)
}

if (violations.length > 0) {
  console.error('\n✗ Secret-leak check FAILED — refusing to package.\n')
  for (const v of violations) console.error(`   ${v}`)
  console.error(
    `\n   ${violations.length} problem(s) found in: ${targets.join(', ')}\n` +
      '   Remove these from the staged payload, or tighten the copy step in\n' +
      '   scripts/build-desktop.mjs, then build again.\n',
  )
  process.exit(1)
}

console.log(`✓ Secret-leak check passed (${targets.join(', ')})`)
