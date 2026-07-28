# Lesson — JARVIS Self-Hearing Fix: Gate the Mic With the Camera, Not Just Audio

**Date:** 2026-07-02
**Area:** `frontend/src/components/PaulChat.tsx`, `frontend/src/components/FaceVisionPanel.tsx`, `jarvis-extension/content.js`

## The Problem

While JARVIS was reading a reply out loud (TTS), the microphone kept transcribing —
and what it transcribed was JARVIS's **own voice** coming back through the speakers.
The transcript then got dispatched as if the user had spoken, creating feedback
loops (JARVIS answering itself) and garbage commands.

## Root Cause

Audio-only defences are inherently leaky:

1. **The chat dictation recognizer had no speaking gate.** `startDictation`'s
   `rec.onresult` in `PaulChat.tsx` appended every result to `finalText` even if
   TTS started playing mid-capture (e.g. the interim "Analysing signals…" speech).
2. **Echo tails and voice-ID are probabilistic.** The 900 ms post-speech blackout,
   confidence thresholds, and speaker-ID all *reduce* self-hearing but cannot make
   it impossible — a loud speaker or an imperfect voice profile still leaks.
3. **The camera signal existed but was unused by the chat.** `FaceVisionPanel`
   already computes lip MAR / jaw-open and broadcasts `jarvis-face-state`
   (`isTalking`, `identityMatch`, `enrolled`) via `window.postMessage`, and the
   extension consumed it — but `PaulChat.tsx` ignored it completely.

## The Key Insight

> **JARVIS's TTS can never move the user's mouth.**

Video is a physically independent channel. If transcription is only allowed while
the camera can *see* the user's lips moving, self-transcription becomes
**physically impossible** — no tuning, thresholds, or echo timing required.

## The Fix

### 1. PaulChat consumes the camera signal (`PaulChat.tsx`)

- New refs `faceStateRef` / `lastMouthActiveAtRef` + helpers:
  - `faceFresh()` — trust the camera only while frames are streaming (< 2.5 s old).
  - `cameraSeesUserTalking()` — mouth moving **and** identity OK (matched face, or
    no enrolment; an enrolled-but-unmatched face is a stranger → never trusted).
  - `mouthGateOpen()` — camera off → no visual gating (audio-only fallback);
    camera live → hear only while the mouth is (or was just, ≤ 1.5 s) moving.
- A `message` listener ingests `jarvis-face-state` broadcasts from `FaceVisionPanel`.

### 2. Hard gates on both recognizers (`PaulChat.tsx`)

- **Dictation** `rec.onresult`: results are dropped while `isSpeakingRef` /
  `micGatedRef` is set **unless** `cameraSeesUserTalking()` — so TTS starting
  mid-capture can no longer be transcribed. `mouthGateOpen()` also applies.
- **Wake recognizer** `rec.onresult`: `mouthGateOpen()` must pass, and while
  JARVIS speaks with the camera live, speech is honoured only if the camera
  actually sees the user talking.

### 3. Camera barge-in — talk and JARVIS stops reading

- **Chat:** when the camera sees the user start talking while `isSpeakingRef` is
  true, `interruptSpeech()` cancels the TTS immediately, the echo blackout is
  skipped (the camera already confirmed it's the user), and dictation starts —
  JARVIS stops reading and transcribes the user.
- **Extension (`content.js`):** new `maybeCameraBargeIn()` runs on every face-state
  update; if `pageSpeaking && faceState.talking` (identity-checked), it cancels
  `speechSynthesis`, clears the speech queue, resets `pageSpeaking`, arms a short
  300 ms echo guard, and notifies the page (`interrupt` + `speak-status`).

## Behaviour Matrix

| Camera | JARVIS speaking | User's mouth moving | Result |
|---|---|---|---|
| off/stale | no | — | normal audio-only listening (voice-ID etc. still apply) |
| off/stale | yes | — | mic hard-gated; wake-word barge-in via voice-ID only |
| live | no | no | transcripts dropped (mouth gate closed) |
| live | no | yes | transcribe normally |
| live | yes | no | nothing transcribed — TTS cannot hear itself |
| live | yes | yes (identity OK) | **TTS stops instantly; user gets transcribed** |
| live | yes | yes (enrolled + stranger) | ignored — a stranger cannot cut JARVIS off |

## Lessons Learned

1. **Cross-modal gating beats same-channel filtering.** Echo filters, confidence
   thresholds, and voice profiles all fight the leak inside the same audio channel.
   A second modality (video) gives a ground-truth signal the leak cannot forge.
2. **Gate at the result handler, not just at start-up.** Muting the mic before
   `speak()` is not enough — recognition sessions already in flight deliver
   results *after* TTS starts. Every `onresult` must re-check the speaking state.
3. **Fail open when the sensor is absent.** All camera gates check `faceFresh()`
   first and fall back to audio-only behaviour, so turning Face Vision off (or the
   backend dying) can never make JARVIS deaf.
4. **Identity-check the interrupt path too.** Barge-in is a privileged action;
   an enrolled profile with an unmatched face (a stranger on camera) must not be
   able to silence or command JARVIS.

---

# Part 2 — The mic stuck on "Listening…" (2026-07-27)

**Area:** `frontend/src/components/PaulChat.tsx`, `jarvis-extension/content.js`,
`frontend/src/pages/jarvis-room.tsx`

## The Problem

With nobody speaking, the mic indicator sat on "Listening…" forever and the
JARVIS Room orb glowed `listening` permanently — while JARVIS was, in fact,
**deaf**. This had been "fixed" at least four times (`917fe8b`, `ce08983`,
`d636f38`, `dd05e70`, `b2b2152`) and came back every time, because every one of
those fixes improved *when audio is accepted* or *who owns the mic* and none of
them touched the state variable itself.

## Root Cause

`listening` was one boolean with **two writers that meant different things by
it**, plus recognisers with **no disposal guard**.

1. **Dual ownership.** The page wrote `listening` to mean *"my dictation
   recogniser is capturing a user utterance"* — transient, per-utterance. The
   extension wrote it, via `setListening(!!d.listening)` in PaulChat's `status`
   handler, to mean *"my recogniser process is armed"* — which is the
   extension's **permanent idle state** (`content.js` sets `listening = true` in
   `rec.onstart` and restarts on every `onend`). Enable the extension and the
   page latched `true` forever.

2. **The latch also caused the deafness.** `listeningRef` is the guard on *every*
   in-page recogniser re-arm path. Stuck `true` meant the fallback recogniser was
   permanently suppressed — so the UI said "listening" precisely because nothing
   was.

3. **Teardown resurrected the recogniser.** `rec.stop()` **fires `onend`**, and
   `onend` is where the auto-restart lives. Every teardown path called a bare
   `stop()`, so unmount/route-change *created* a fresh recogniser 400–600 ms
   later that nothing held a reference to and nothing could ever stop.

4. **Two recognisers, one tracked.** The wake effect depended on `startWake`, a
   `useCallback` that changes identity on ordinary re-renders. Each re-run
   stopped the old recogniser (→ zombie restart) and started another. `wakeRef`
   held only the newest. StrictMode's double mount hit this every time.

## The Invariants (never break these)

> **1. `listening` means "a user utterance is being captured RIGHT NOW" — never
> "a recogniser is armed".** An armed recogniser waiting in silence is the
> resting state. These are two different facts and they need two different
> fields. The extension reports them separately: `listening` (armed → decides mic
> ownership only) and `capturing` (an utterance → the only thing the page's
> indicator and re-arm guards may read).

> **2. One writer.** All writes go through `markCapturing(on, owner)`. A claim is
> released only by the surface that made it, or by the reconciler.

> **3. Stopping a recogniser is not tearing it down.** `stop()` fires `onend`,
> and `onend` restarts. Detach the handlers *first* (`detachRecognizer`) or the
> teardown becomes a restart. Every restart path also checks `voiceDisposedRef`.

> **4. Every state a surface renders must be retractable.** A consumer that only
> ever hears "true" latches. PaulChat broadcasts an explicit idle on every
> teardown; the Room drops to idle on tab-hide on its own.

## Why It Cannot Regress The Same Way

`frontend/src/hooks/__tests__/useDeepgramAgent.listeningLifecycle.test.tsx`
pins all four invariants and was verified to **fail** when each defect is
re-introduced — the extension-armed write, the missing disposal guards, and the
`startWake` dependency churn. A reconciler (`CAPTURE_STALE_MS`) is the backstop,
never the fix: a capture claim not backed by a live recogniser or a recent
refresh is dropped.

## Relationship to Part 1

Part 1's camera gate decides **whether a transcript is accepted**. Part 2 decides
**whether a recogniser exists and what the UI says about it**. They are
independent — which is exactly why four rounds of Part-1-style hardening never
touched this bug.
