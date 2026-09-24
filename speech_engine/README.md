# Jarvis local speech engine

Self-hosted, offline-capable speech-to-text and text-to-speech for Jarvis,
based on [huggingface/speech-to-speech](https://github.com/huggingface/speech-to-speech).
Its STT model, **Parakeet-TDT**, originates from **NVIDIA NeMo** and is loaded
via Hugging Face Hub/Transformers (no `nemo_toolkit` install required). Its
TTS is **Qwen3-TTS**. On Apple Silicon both run through the `mlx`/`mlx-audio`
backends — no CUDA needed.

**Measured on an M2 Pro / 16 GB (2026-09-23):** STT 0.1–1.3s per utterance and
the most accurate engine available here, so `/voice/stt` tries it **first**
(NVIDIA hosted ASR, then Deepgram/Whisper, are the fallbacks). TTS renders
faster than real time (~2–3s for a sentence), and the web chat plays replies
sentence by sentence so speech starts within a few seconds.

**Lite mode (default below 32 GB RAM).** Both models warm cost ~5.5 GB. In lite
mode only STT (~2.8 GB) stays resident; TTS loads on first use, unloads after
`SPEECH_ENGINE_TTS_IDLE_S` (600s) idle, and refuses to load when macOS reports
under `SPEECH_ENGINE_MIN_FREE_PCT` (15%) memory free — that reply then uses the
next voice in the chain. MLX's buffer cache is capped (`SPEECH_ENGINE_MLX_CACHE_MB`,
256) and released after every request.

**start.py runs it for you** on Apple-Silicon/CUDA machines of the high tier with
16 GB+, via `supervise.py`: nice +10, CPU helper threads capped at 2, restarted
automatically if it crashes (with back-off), and stopped by `start.py --stop`
through `speech-engine.pid`.

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
cd speech_engine && .venv/bin/python supervise.py --port 8790   # supervised, low priority
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
