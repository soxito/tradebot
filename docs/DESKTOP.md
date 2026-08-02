# TradeBot Desktop

A double-clickable TradeBot for macOS, Windows and Linux. No Python, Node,
PostgreSQL, Redis or Docker to install — the app carries its own runtime and
stores everything in a per-user data folder.

This is a packaging layer, not a fork: `python start.py` and `docker-compose up`
behave exactly as before. Every desktop-specific code path is gated behind the
`TRADEBOT_DATA_DIR` / `TRADEBOT_DESKTOP` environment variables, which nothing
else sets.

---

## Installing

Download the installer for your platform from the
[Releases page](https://github.com/soxito/tradebot/releases).

| Platform | File |
| --- | --- |
| macOS (Apple Silicon) | `TradeBot-<version>-arm64.dmg` |
| macOS (Intel) | `TradeBot-<version>.dmg` |
| Windows | `TradeBot-Setup-<version>.exe` |
| Linux | `TradeBot-<version>.AppImage` or `.deb` |

### First launch

Builds are **not code-signed yet** (no certificates have been purchased), so
each OS will warn you once:

- **macOS** — right-click the app → **Open** → **Open**. Double-clicking gives
  "cannot be opened because the developer cannot be verified"; the right-click
  route is the documented Gatekeeper override. Only needed the first time.
- **Windows** — SmartScreen shows "Windows protected your PC". Click
  **More info** → **Run anyway**.
- **Linux** — `chmod +x TradeBot-*.AppImage`, then run it.

First launch takes 30–60 seconds: the app creates its database and loads the
plugins. Later launches are much faster.

### Where your data lives

**Help → Open Data Folder**, or:

| Platform | Path |
| --- | --- |
| macOS | `~/Library/Application Support/TradeBot/` |
| Windows | `%APPDATA%\TradeBot\` |
| Linux | `~/.config/TradeBot/` |

It holds `tradebot.db` (SQLite) and `backend.log`. Uninstalling does **not**
delete it — your trade history and API keys survive reinstalls and updates.

---

## What differs from the server setup

| | Server / `start.py` | Desktop |
| --- | --- | --- |
| Database | PostgreSQL | SQLite in your data folder |
| Cache / pub-sub | Redis | In-process fan-out |
| Frontend | Next.js dev server on `:3000` | Static export served by the backend |
| Processes | 4 | 2 (Electron + Python) |
| Binds | `0.0.0.0` (LAN-reachable) | `127.0.0.1` only |
| Port | fixed `1448` | `1448`, scanning upward if taken |

Three consequences worth knowing about:

**Voice control needs a Deepgram key.** The browser version uses the free Web
Speech API. Electron ships Chromium's speech API without the Google API key it
requires, so it constructs successfully and then never returns a result — worse
than not existing. The app detects the desktop runtime and routes straight to
the Deepgram fallback the backend already implements. Set a Deepgram key in
Settings to use voice; everything else works without one.

**Kronos ML forecasting uses its heuristic fallback.** Bundling PyTorch would
add roughly 2GB to a 700MB download. The plugin already degrades gracefully.

**The API is loopback-only.** `start.py` binds `0.0.0.0` deliberately, so you
can open the dashboard from your phone. Shipping that default would put an
unauthenticated API that can place live trades on every network your laptop
joins, so the desktop build does not.

MT5 works unchanged — it talks to mtapi-io over REST and has no native
MetaTrader5 dependency.

---

## Building it yourself

Requires Node 20+ and network access. **There is no cross-compilation**: the
embedded CPython and its compiled wheels are platform-specific, so build macOS
on a Mac, Windows on Windows, Linux on Linux.

```bash
node scripts/build-desktop.mjs
```

Useful variants:

```bash
node scripts/build-desktop.mjs --stage
```

```bash
node scripts/build-desktop.mjs --dir
```

`--stage` prepares and verifies the payload without packaging; `--dir` produces
an unpacked app (much faster than building installers). Output lands in
`dist-desktop/`.

To iterate on the Electron shell alone, against your existing dev tree:

```bash
cd frontend && TRADEBOT_DESKTOP=1 NEXT_PUBLIC_API_SAME_ORIGIN=1 NEXT_PUBLIC_API_URL= npx next build
```

```bash
cd desktop && npm start
```

Unpackaged, the shell uses `backend/.venv` and `frontend/out` from the checkout
instead of the bundled runtime, so there is no need to re-download CPython.

### How it fits together

```
Electron main process
  └─ spawns → uvicorn (bundled CPython, 127.0.0.1:<port>)
                ├─ FastAPI  /api/v1/*
                ├─ StaticFiles at /   ← the exported Next.js frontend
                └─ SQLite   <userData>/tradebot.db
```

One origin for UI and API, which removes CORS entirely and lets the frontend
discover the backend from `window.location` — necessary because the port is only
known at launch and cannot be baked into a static bundle.

The build steps, in order:

1. Fetch a relocatable CPython 3.12 from
   [python-build-standalone](https://github.com/astral-sh/python-build-standalone)
   (pinned in `scripts/build-desktop.mjs`).
2. `pip install -r backend/requirements.txt` into it, wheels only.
3. Export the frontend to static files (`output: 'export'`).
4. Stage `backend/app`, `plugins/`, the runtime and the export into
   `desktop/resources/`.
5. Run `scripts/check-no-secrets.mjs`.
6. Hand off to electron-builder.

Expect ~700MB installed. That is the cost of embedding CPython with
pandas/numpy/ccxt alongside Chromium.

### Why Electron rather than Tauri

Tauri would be far smaller, but it renders on the OS webview — WKWebView on
macOS, WebView2 on Windows, WebKitGTK on Linux. This UI leans hard on WebGL and
workers: Three.js, a ~980-particle S.O.X orb driven off-thread through
OffscreenCanvas, `react-force-graph-3d`, and the Deepgram browser SDK. WebKitGTK
in particular is unreliable for that. Electron ships one Chromium everywhere, so
what gets tested is what every user runs. Next to a bundled CPython, Chromium's
~150MB is not the deciding factor.

### Why a relocatable CPython rather than PyInstaller

The plugin loader resolves modules by string at runtime
(`plugins.<name>.backend.router`, see `backend/app/plugins/loader.py`).
PyInstaller's static analysis cannot see those, so every plugin and every
dynamic import inside `ccxt` would need a hand-maintained `hiddenimports` entry
— a permanent source of "works in dev, broken in the installer" bugs. Shipping a
real CPython keeps imports behaving exactly as they do in development and keeps
`plugins/` a real directory the loader can walk.

---

## Releasing

Push a `v*` tag. `.github/workflows/desktop-release.yml` builds all four targets
(macOS arm64 + x64, Windows x64, Linux x64) and opens a **draft** release for
review.

```bash
git tag v0.1.0 && git push origin v0.1.0
```

### Signing

Builds are unsigned. Adding these repository secrets enables signing with no
workflow edit:

| Secret | Purpose |
| --- | --- |
| `CSC_LINK` | base64 `.p12` — Apple Developer ID |
| `CSC_KEY_PASSWORD` | its password |
| `APPLE_ID`, `APPLE_APP_SPECIFIC_PASSWORD`, `APPLE_TEAM_ID` | notarization |
| `WIN_CSC_LINK` | base64 `.pfx` — Windows code-signing cert |
| `WIN_CSC_KEY_PASSWORD` | its password |

For macOS also set `notarize: true` in `desktop/electron-builder.yml`.

### Auto-update

Windows and Linux auto-update through `electron-updater` against GitHub
Releases. **macOS auto-update is disabled** and stays that way until a Developer
ID certificate exists — electron-updater cannot verify an unsigned macOS update
and would fail on every launch. The check is skipped rather than allowed to
fail; see the guard in `desktop/main.js`.

---

## Troubleshooting

**"Backend did not become ready"** — read `backend.log` in the data folder
(**Help → View Backend Log**). Usually a plugin failing to import.

**Window opens blank** — the frontend export is missing or incomplete. Rebuild;
`scripts/build-desktop.mjs` fails loudly if `out/index.html` is absent.

**"No free port"** — ports 1448–1487 are all taken. Close whatever is holding
them, most likely a `start.py` instance.

**App won't start after an update** — delete `tradebot.db` from the data folder
to reset the database. You lose local trade history; API keys are stored
separately and survive.
