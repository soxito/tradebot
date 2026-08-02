# Jarvis local speech engine

Self-hosted, offline-capable speech-to-text and text-to-speech for Jarvis,
based on [huggingface/speech-to-speech](https://github.com/huggingface/speech-to-speech).
Its STT model, **Parakeet-TDT**, originates from **NVIDIA NeMo** and is loaded
via Hugging Face Hub/Transformers (no `nemo_toolkit` install required). Its
TTS is **Qwen3-TTS**. On Apple Silicon both run through the `mlx`/`mlx-audio`
backends — no CUDA needed.

**This is the slow, offline-capable fallback tier, not the primary path.**
`backend/app/api/voice.py`'s `/voice/stt` and `/voice/tts` try **NVIDIA NIM
hosted speech first** (hosted Parakeet ASR + Magpie TTS — ~1-2s, since the
NVIDIA API key is already configured), fall back to this local engine if
NVIDIA is unavailable, and fall back to OpenAI Whisper/TTS last. On this
Mac's CPU/MPS this local engine takes ~30-60s per utterance (measured), so
enable it only as an offline/privacy fallback, not for interactive latency.

This runs as its own isolated service (own venv, own process) so its heavy ML
dependencies never touch `backend/requirements.txt`.

## Setup

```bash
cd speech_engine
python3.12 -m venv .venv   # misaki (a transitive TTS dep) requires Python <3.13
.venv/bin/pip install -r requirements.txt
```

## Run

```bash
cd speech_engine && .venv/bin/uvicorn main:app --host 0.0.0.0 --port 8790
```

(or use the `speech-engine` entry in `.claude/launch.json`).

Then enable it in the main backend's `.env`:

```
SPEECH_ENGINE_ENABLED=true
SPEECH_ENGINE_URL=http://localhost:8790
```

`voice.py` calls this service only after NVIDIA NIM has already failed/timed
out, with its own short timeout, then silently falls back to OpenAI on any
error — so it's safe to enable even while the engine is still warming up.

## Future GPU upgrade path

If GPU hardware becomes available, swap `STT_BACKEND=parakeet-mlx` for a full
NVIDIA NeMo-toolkit streaming model (e.g. Nemotron-Speech-Streaming, as low as
~160ms latency) via `nemo-toolkit[asr,tts]` — not implemented here since it
requires CUDA. Set `SPEECH_ENGINE_STT_BACKEND` / `SPEECH_ENGINE_TTS_BACKEND`
env vars as the switch point once that path is built.
