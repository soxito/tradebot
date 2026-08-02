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
import io
import os
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


def _new_handler(handler_cls, **setup_kwargs):
    """Construct a speech-to-speech BaseHandler subclass outside its pipeline.

    Mirrors `speech_to_speech.baseHandler.BaseHandler.__init__` (stop_event,
    queue_in, queue_out, setup_kwargs) without starting the handler's `.run()`
    thread — we call `.process()` directly per-request instead.
    """
    return handler_cls(Event(), Queue(), Queue(), setup_kwargs=setup_kwargs)


def load_models() -> None:
    """Load STT/TTS models once at process startup and keep them warm.

    Cold model-load latency (multi-second) is the biggest UX risk on CPU/MPS,
    so this must run at startup, not on the first request.
    """
    global _stt_handler, _tts_handler
    logger.info(f"speech_engine: loading STT backend={STT_BACKEND}, TTS backend={TTS_BACKEND}")

    from speech_to_speech.STT.parakeet_tdt_handler import ParakeetTDTSTTHandler
    from speech_to_speech.TTS.qwen3_tts_handler import Qwen3TTSHandler

    _stt_handler = _new_handler(ParakeetTDTSTTHandler)
    # should_listen is normally the pipeline's mic-gate Event; unused in our
    # single-shot per-request call, so a standalone Event() is sufficient.
    _tts_handler = _new_handler(Qwen3TTSHandler, should_listen=Event())

    logger.info("speech_engine: models loaded and warm")


def is_ready() -> bool:
    return _stt_handler is not None and _tts_handler is not None


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
    for output in _stt_handler.process(vad_audio):
        if isinstance(output, Transcription):
            text = output.text
    return text


def synthesize(text: str) -> bytes:
    if _tts_handler is None:
        raise RuntimeError("TTS handler not loaded — call load_models() first")

    from speech_to_speech.pipeline.messages import TTSInput

    tts_input = TTSInput(text=text)
    chunks: list[np.ndarray] = []
    for output in _tts_handler.process(tts_input):
        if isinstance(output, (bytes, bytearray)):
            continue  # sentinel (e.g. AUDIO_RESPONSE_DONE), not audio data
        chunks.append(np.asarray(output))

    if not chunks:
        raise RuntimeError("Qwen3-TTS produced no audio for the given text")

    audio = np.concatenate(chunks)
    buf = io.BytesIO()
    sf.write(buf, audio, PIPELINE_SAMPLE_RATE, format="WAV", subtype="PCM_16")
    return buf.getvalue()
