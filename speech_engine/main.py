"""Local Jarvis speech engine — standalone FastAPI service.

Wraps huggingface/speech-to-speech's Parakeet-TDT STT and Qwen3-TTS handlers
as two single-shot HTTP endpoints with the same request/response contract as
backend/app/api/voice.py's existing OpenAI-backed /voice/stt and /voice/tts,
so the main backend can call this service first and fall back to OpenAI on
any error/timeout with zero frontend changes.

Run (own venv — see requirements.txt), from inside speech_engine/:
    .venv/bin/uvicorn main:app --host 0.0.0.0 --port 8790
"""
import asyncio

from fastapi import FastAPI, UploadFile, File, HTTPException, Response, Form
from loguru import logger

import engine

app = FastAPI(title="Jarvis Local Speech Engine")

# The MLX STT/TTS handlers are shared singletons and not reentrant, so serialise
# access per handler. Inference still runs off the event loop (asyncio.to_thread),
# so /health and queued requests stay responsive while one clip is generating.
_stt_lock = asyncio.Lock()
_tts_lock = asyncio.Lock()


@app.on_event("startup")
async def _startup():
    try:
        engine.load_models()
    except Exception as e:
        # Don't crash the process — /health reports not-ready and the backend
        # falls back to OpenAI until this is fixed and the service restarted.
        logger.error(f"speech_engine: failed to load models: {e}")


@app.get("/health")
async def health():
    return {"ready": engine.is_ready(), "stt_backend": engine.STT_BACKEND, "tts_backend": engine.TTS_BACKEND}


@app.post("/stt")
async def stt(file: UploadFile = File(...)):
    if not engine.is_ready():
        raise HTTPException(status_code=503, detail="speech engine models not loaded")
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Empty audio file")
    try:
        # Parakeet inference is synchronous and CPU/MPS-bound; run it in a worker
        # thread so the event loop keeps serving /health and concurrent requests.
        async with _stt_lock:
            text = await asyncio.to_thread(
                engine.transcribe, content, filename=file.filename or "audio.wav"
            )
        return {"text": text}
    except Exception as e:
        logger.error(f"speech_engine STT error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/tts")
async def tts(text: str = Form(...), voice: str = Form("default")):
    if not engine.is_ready():
        raise HTTPException(status_code=503, detail="speech engine models not loaded")
    try:
        # Qwen3-TTS synthesis blocks for the whole clip; offload it to a worker
        # thread so the sidecar never freezes (health checks + barge-in stay live).
        async with _tts_lock:
            audio = await asyncio.to_thread(engine.synthesize, text)
        return Response(content=audio, media_type="audio/wav")
    except Exception as e:
        logger.error(f"speech_engine TTS error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
