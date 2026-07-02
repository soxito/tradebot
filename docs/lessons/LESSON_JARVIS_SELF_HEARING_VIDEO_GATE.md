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
