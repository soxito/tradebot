# JARVIS Voice Assistant — Browser Extension

Reliable voice recognition + desktop notifications for the TradeBot JARVIS assistant.
Works in **Chrome**, **Edge**, **Brave**, and **Firefox**.

## Why use this?

The in-page Web Speech API is unreliable — it stops listening on page re-renders,
loses the mic, and gives no desktop alerts. This extension fixes all of that:

- **Persistent voice listening** that survives page navigation and re-renders
- **Native desktop notifications** (bottom-right of your screen) for JARVIS replies, trades, and alerts
- **A clean wake-word gate** so the TV / background voices don't trigger it
- **A popup** with on/off toggle, live transcript, and a "test notification" button

When installed, the JARVIS chat widget shows a green **EXT** badge and routes voice
through the extension automatically.

---

## Install in Chrome / Edge / Brave

1. Open `chrome://extensions` (or `edge://extensions`, `brave://extensions`)
2. Turn on **Developer mode** (top-right toggle)
3. Click **Load unpacked**
4. Select this folder: `tradebot/jarvis-extension`
5. The JARVIS icon appears in your toolbar — pin it
6. Open TradeBot (`http://localhost:3000`), click the icon, and **allow the microphone** when asked

## Install in Firefox

1. Open `about:debugging#/runtime/this-firefox`
2. Click **Load Temporary Add-on…**
3. Select the `manifest.json` file inside this folder
4. Open TradeBot and allow the microphone

> Firefox temporary add-ons are removed on restart. To make it permanent, package
> it (`web-ext build`) and install the signed `.xpi`, or use Firefox Developer Edition
> with `xpinstall.signatures.required = false` in `about:config`.

---

## How to use

1. Open TradeBot in the browser
2. Make sure the toolbar icon shows a green **●** dot (listening)
3. Just say one of the wake names — **"Jarvis"**, **"Paul"**, or **"Sox"** — then your command in one breath, e.g.:
   - "Jarvis, open MT5 Live"
   - "Paul, analyse Gold for sniper entries"
   - "Sox, what are my open positions"
   - "Jarvis, scroll down"
   ("Hey Jarvis" still works too.)
4. JARVIS replies in the chat **and** as a desktop notification

## Popup controls

| Control | What it does |
|---|---|
| **Voice listening** | Master on/off for the mic |
| **Require "Hey" greeting** | Default OFF — saying just a name ("Jarvis", "Paul", or "Sox") wakes it. Turn ON to require a greeting like "Hey Jarvis" (strict mode for noisy rooms). |
| **Desktop notifications** | Toggle the bottom-right desktop alerts |
| **Test desktop notification** | Fire a test alert to confirm notifications work |

---

## Microphone permission

The first time, the browser asks to allow the mic for `localhost:3000`. Click **Allow**.
If you accidentally blocked it:

- **Chrome**: click the 🔒/ⓘ icon in the address bar → Site settings → Microphone → Allow
- **Firefox**: click the 🔒 icon → Connection secure → More information → Permissions → Use the Microphone → Allow

## Cost-aware Deepgram fallback

The extension recognises commands with the **free** browser Web Speech API. When
it hears a wake word but can't make out the follow-up command, it sends a short
buffered audio clip (the last few seconds, reused from the same mic stream that
powers the visualizer — no second microphone) to the backend's budget-guarded
endpoint `POST /api/v1/voice/deepgram/stt`. The recovered command is then
dispatched normally.

- The raw Deepgram key never leaves the backend; `background.js` only relays the
  clip to `http://localhost:1448`.
- Spend is capped on the backend (`DEEPGRAM_MONTHLY_CAP_USD`, default $60). When
  the cap is reached the backend returns `used_deepgram=false` and the extension
  silently keeps using the free engine.
- When the extension owns the mic, the in-page widget does **not** also escalate,
  so a given miss is re-checked only once.
- **Your voice only**: if you have calibrated a voice profile and enabled voice
  match in the JARVIS widget, the extension only escalates clips that the page's
  live speaker-ID confirms are your voice (relayed via the `voice-match-update`
  signal). A TV or another person nearby is never sent to Deepgram. With voice
  match off, escalation is unrestricted like the free engine.

Verify: say "Jarvis" then mumble — exactly one `/voice/deepgram/stt` request
should appear in the backend logs and the recovered command should run.

## Troubleshooting

- **Popup stuck on "Starting…" / "Page mic active (extension idle)"**: the extension couldn't start the
  microphone in this tab (some browsers block the Web Speech API inside extensions). JARVIS automatically
  falls back to the **in-page microphone**, so the chat keeps working — just allow the mic for the page and
  reload. Toggling "Voice listening" off then on can also re-arm the extension recognizer.
- **No green dot / not listening**: click the extension icon → toggle "Voice listening" off then on
- **Hears the TV**: turn ON "Require Hey greeting"
- **No desktop notifications**: click "Test desktop notification". If nothing appears, enable
  notifications for your browser in macOS **System Settings → Notifications**
- **Badge shows ✕ (red)**: microphone is blocked — re-allow it (see above)

## Versioning & Updates

The extension version lives in **`manifest.json`** and is mirrored by the backend
constant **`_EXT_VERSION`** in `backend/app/api/jarvis.py`. The backend reports the
latest version at `GET /api/v1/jarvis/extension-version`; when a user's installed
version is older, TradeBot shows an **update banner** automatically.

**Every time you change the extension, bump the version** so the banner fires:

```bash
# Patch bump (3.0.0 → 3.0.1) — for fixes
./scripts/bump-extension.sh

# Minor bump (3.0.0 → 3.1.0) — for new features
./scripts/bump-extension.sh minor

# Major bump (3.0.0 → 4.0.0) — for breaking changes
./scripts/bump-extension.sh major

# Set an exact version
./scripts/bump-extension.sh 3.2.5

# Bump + add changelog entries (shown in the update modal)
./scripts/bump-extension.sh patch "Fixed mic muting" "Faster popup"
```

The script atomically:
1. Increments `manifest.json` version
2. Syncs the backend `_EXT_VERSION` + `_EXT_RELEASED`
3. Optionally prepends changelog entries
4. Rebuilds `frontend/public/jarvis-extension.zip` **and** the versioned
   `jarvis-extension-v<VERSION>.zip`

After bumping, **restart the backend** so it serves the new version as "latest".
