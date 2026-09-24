"""Thin adapter around huggingface/speech-to-speech's STT and TTS handlers.

We deliberately do NOT run the package's full VAD -> STT -> LLM -> TTS pipeline
or its bundled server (raw-websocket / OpenAI-Realtime protocol) — Jarvis keeps
its own trading-command parser in the middle, so we only need single-shot
transcribe() / synthesize() calls, not a conversational loop.

speech-to-speech's STT/TTS handlers are `BaseHandler` subclasses built for a
threaded, queue-fed pipeline (see `speech_to_speech.baseHandler.BaseHandler`)
rather than a plain function-call API. We construct them the same way the
pipeline does (stop_event/queue_in/queue_out + setup_kwargs) but call
`.process(...)` directly as a generator instead of running `.run()` in a
thread — `process()` is a self-contained generator that doesn't touch the
queues except to read `queue_in.mutex`/`queue_in.queue` for text-coalescing
(TTS side), which a real, empty `queue.Queue()` satisfies fine.

STT_BACKEND / TTS_BACKEND are placeholders for a future GPU box: swapping the
"parakeet-mlx" / mlx-audio Qwen3-TTS backends used here for a full NVIDIA
NeMo-toolkit streaming model (e.g. Nemotron-Speech-Streaming) needs CUDA we
don't have on Mac today, so that path is documented, not implemented, here.
"""
import gc
import io
import os
import subprocess
import threading
import time
from queue import Queue
from threading import Event

import numpy as np
import soundfile as sf
from loguru import logger

STT_BACKEND = os.getenv("SPEECH_ENGINE_STT_BACKEND", "parakeet-mlx")
TTS_BACKEND = os.getenv("SPEECH_ENGINE_TTS_BACKEND", "qwen3-tts-mlx")

PIPELINE_SAMPLE_RATE = 16000

_stt_handler = None
_tts_handler = None

# ── Keeping a 16 GB laptop responsive ────────────────────────────────────────
# Both models warm cost ~5.5 GB (6.2 GB peak), measured on an M2 Pro. STT is
# the small, latency-critical half (0.1s warm, and the most accurate engine for
# this user's voice), so it stays loaded. TTS is the big half and only the
# first voice in a fallback chain, so in LITE mode it loads on first use,
# unloads after sitting idle, and refuses to load when macOS is short of memory
# — the backend then uses NVIDIA's hosted voice for that reply instead.
LITE = os.getenv("SPEECH_ENGINE_LITE", "1").strip().lower() in ("1", "true", "yes", "on")
TTS_IDLE_UNLOAD_S = float(os.getenv("SPEECH_ENGINE_TTS_IDLE_S", "600"))
# Minimum % of RAM macOS must report free before the TTS model may load.
MIN_FREE_PCT_FOR_TTS = int(os.getenv("SPEECH_ENGINE_MIN_FREE_PCT", "15"))
# MLX keeps freed GPU buffers in a cache for reuse; unbounded, that cache is
# most of the gap between the steady footprint and the peak.
MLX_CACHE_MB = int(os.getenv("SPEECH_ENGINE_MLX_CACHE_MB", "256"))

_tts_lock = threading.Lock()
_tts_last_used = 0.0


class LowMemory(RuntimeError):
    """The machine is too short of memory to load the TTS model right now."""


def _mx():
    try:
        import mlx.core as mx
        return mx
    except Exception:  # noqa: BLE001 — non-Apple host
        return None


def _release_cache() -> None:
    mx = _mx()
    if mx is not None:
        try:
            mx.clear_cache()
        except Exception:  # noqa: BLE001
            pass


def free_memory_pct() -> int | None:
    """macOS's own "memory free" figure (what `memory_pressure` prints), or None."""
    try:
        out = subprocess.run(["sysctl", "-n", "kern.memorystatus_level"],
                             capture_output=True, text=True, timeout=2).stdout.strip()
        return int(out)
    except Exception:  # noqa: BLE001 — not macOS / sysctl missing
        return None


def _new_handler(handler_cls, **setup_kwargs):
    """Construct a speech-to-speech BaseHandler subclass outside its pipeline.

    Mirrors `speech_to_speech.baseHandler.BaseHandler.__init__` (stop_event,
    queue_in, queue_out, setup_kwargs) without starting the handler's `.run()`
    thread — we call `.process()` directly per-request instead.
    """
    return handler_cls(Event(), Queue(), Queue(), setup_kwargs=setup_kwargs)


def load_models() -> None:
    """Load the models at process startup.

    STT always loads here — cold-load latency is the biggest UX risk and it is
    the half every voice turn needs. TTS loads here too unless LITE, in which
    case ensure_tts() loads it on the first request.
    """
    global _stt_handler
    mx = _mx()
    if mx is not None:
        try:
            mx.set_cache_limit(MLX_CACHE_MB * 1024 * 1024)
        except Exception:  # noqa: BLE001
            pass
    logger.info(f"speech_engine: loading STT backend={STT_BACKEND} (lite={LITE})")

    from speech_to_speech.STT.parakeet_tdt_handler import ParakeetTDTSTTHandler

    _stt_handler = _new_handler(ParakeetTDTSTTHandler)
    # Warm-up: the first transcription compiles the MLX kernels (~5s measured),
    # which would otherwise land on the user's first real utterance and blow
    # the backend's 4s timeout. Half a second of silence pays it here instead.
    try:
        from speech_to_speech.pipeline.messages import VADAudio
        for _ in _stt_handler.process(VADAudio(audio=np.zeros(PIPELINE_SAMPLE_RATE // 2, dtype=np.float32), mode="final")):
            pass
    except Exception as e:  # noqa: BLE001 — warm-up is best effort
        logger.warning(f"speech_engine: STT warm-up skipped: {e}")
    if not LITE:
        ensure_tts()
    _release_cache()
    logger.info("speech_engine: STT loaded and warm")
    threading.Thread(target=_idle_unloader, name="tts-idle-unloader", daemon=True).start()


def ensure_tts() -> None:
    """Load the TTS model if it isn't loaded. Raises LowMemory when unsafe."""
    global _tts_handler, _tts_last_used
    with _tts_lock:
        _tts_last_used = time.monotonic()
        if _tts_handler is not None:
            return
        free = free_memory_pct()
        if LITE and free is not None and free < MIN_FREE_PCT_FOR_TTS:
            raise LowMemory(f"only {free}% memory free (< {MIN_FREE_PCT_FOR_TTS}%)")
        logger.info(f"speech_engine: loading TTS backend={TTS_BACKEND} ({free}% memory free)")
        from speech_to_speech.TTS.qwen3_tts_handler import Qwen3TTSHandler
        # should_listen is normally the pipeline's mic-gate Event; unused in our
        # single-shot per-request call, so a standalone Event() is sufficient.
        _tts_handler = _new_handler(Qwen3TTSHandler, should_listen=Event())
        _release_cache()
        logger.info("speech_engine: TTS loaded")


def _idle_unloader() -> None:
    """Drop the TTS model after it has sat unused, giving the RAM back."""
    global _tts_handler
    if not LITE:
        return
    while True:
        time.sleep(30)
        with _tts_lock:
            if _tts_handler is None or time.monotonic() - _tts_last_used < TTS_IDLE_UNLOAD_S:
                continue
            _tts_handler = None
        gc.collect()
        _release_cache()
        logger.info(f"speech_engine: TTS unloaded after {TTS_IDLE_UNLOAD_S:.0f}s idle")


def is_ready() -> bool:
    """Ready to serve: STT loaded (TTS is loaded on demand in LITE mode)."""
    return _stt_handler is not None and (LITE or _tts_handler is not None)


def tts_loaded() -> bool:
    return _tts_handler is not None


def transcribe(audio_bytes: bytes, filename: str = "audio.wav") -> str:
    if _stt_handler is None:
        raise RuntimeError("STT handler not loaded — call load_models() first")

    import librosa
    from speech_to_speech.pipeline.messages import VADAudio, Transcription

    # librosa decodes whatever container the browser sent (webm/opus, wav, ...)
    # and resamples/downmixes to the 16kHz mono float32 the pipeline expects.
    audio, _sr = librosa.load(io.BytesIO(audio_bytes), sr=PIPELINE_SAMPLE_RATE, mono=True)
    vad_audio = VADAudio(audio=audio.astype(np.float32), mode="final")

    text = ""
    try:
        for output in _stt_handler.process(vad_audio):
            if isinstance(output, Transcription):
                text = output.text
    finally:
        _release_cache()
    return text


def synthesize(text: str) -> bytes:
    global _tts_last_used
    ensure_tts()

    from speech_to_speech.pipeline.messages import TTSInput

    tts_input = TTSInput(text=text)
    chunks: list[np.ndarray] = []
    # Held for the whole clip so the idle unloader can't drop the model mid-way.
    with _tts_lock:
        handler = _tts_handler
        if handler is None:
            raise RuntimeError("TTS model was unloaded — retry")
        try:
            for output in handler.process(tts_input):
                if isinstance(output, (bytes, bytearray)):
                    continue  # sentinel (e.g. AUDIO_RESPONSE_DONE), not audio data
                chunks.append(np.asarray(output))
        finally:
            _tts_last_used = time.monotonic()
            _release_cache()

    if not chunks:
        raise RuntimeError("Qwen3-TTS produced no audio for the given text")

    audio = np.concatenate(chunks)
    buf = io.BytesIO()
    sf.write(buf, audio, PIPELINE_SAMPLE_RATE, format="WAV", subtype="PCM_16")
    return buf.getvalue()
