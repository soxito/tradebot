from fastapi import APIRouter, UploadFile, File, HTTPException, Response, Form
from typing import Optional, Any
import os
import io
import wave
import asyncio
import httpx
from loguru import logger

from app.core.config import settings

try:
    from openai import AsyncOpenAI
except ImportError:
    AsyncOpenAI = None

try:
    import riva.client
except ImportError:
    riva = None  # type: ignore

router = APIRouter(prefix="/voice", tags=["voice"])

# ── Deepgram helpers ──────────────────────────────────────────────────────────

DEEPGRAM_TOKEN_URL = "https://api.deepgram.com/v1/auth/grant"


def _dg_key() -> str:
    """Read the Deepgram API key at request time.

    Reads from pydantic ``Settings`` first (which loads the root ``.env``) and
    falls back to the process environment. Reading per-request (instead of once
    at import) means a key added/changed in ``.env`` is picked up on the next
    request after a ``--reload`` code reload, without a stale import-time value.
    """
    return settings.DEEPGRAM_API_KEY or os.getenv("DEEPGRAM_API_KEY", "")


def _dg_headers() -> dict:
    return {
        "Authorization": f"Token {_dg_key()}",
        "Content-Type": "application/json",
    }


# ── Deepgram token endpoint ───────────────────────────────────────────────────

@router.get("/deepgram/token")
async def deepgram_token():
    """Issue a short-lived Deepgram JWT for the browser Voice Agent SDK."""
    if not _dg_key():
        raise HTTPException(
            status_code=503,
            detail="DEEPGRAM_API_KEY not configured — add it to .env and restart the backend",
        )
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            DEEPGRAM_TOKEN_URL,
            headers=_dg_headers(),
            json={"ttl_seconds": 30},
        )
    if resp.status_code == 403:
        raise HTTPException(
            status_code=403,
            detail="Deepgram key lacks 'usage:write' scope. Recreate the key with Member role in console.deepgram.com → Settings → API Keys."
        )
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    data = resp.json()
    # Return the bare JWT string so tokenFactory can call .text()
    return Response(content=data.get("access_token", ""), media_type="text/plain")


@router.get("/deepgram/status")
async def deepgram_status():
    """Return Deepgram key health, project info, and credit balance."""
    key = _dg_key()
    if not key:
        return {
            "ok": False,
            "key_present": False,
            "error": "DEEPGRAM_API_KEY not configured — add it to .env and restart the backend",
            "required_fix": "Add DEEPGRAM_API_KEY to .env then restart the backend.",
        }

    result: dict = {"ok": False, "key_present": True, "key_length": len(key)}

    async with httpx.AsyncClient(timeout=10) as client:
        # 1. List projects — basic connectivity
        r_projects = await client.get(
            "https://api.deepgram.com/v1/projects",
            headers=_dg_headers(),
        )
        if r_projects.status_code != 200:
            result["error"] = f"Projects API returned {r_projects.status_code}: {r_projects.text}"
            return result

        projects = r_projects.json().get("projects", [])
        result["projects"] = [{"id": p["project_id"], "name": p["name"]} for p in projects]

        if not projects:
            result["error"] = "No projects found"
            return result

        project_id = projects[0]["project_id"]
        result["project_id"] = project_id
        # Clean profile fields for the UI. The project name is typically
        # "<email>'s Project", so surface both the raw name and the email.
        account_name = projects[0].get("name", "")
        result["account_name"] = account_name
        result["email"] = account_name.split("'s ")[0] if "'s " in account_name else None

        # 2. Try token grant (requires usage:write / Member role)
        r_token = await client.post(
            DEEPGRAM_TOKEN_URL,
            headers=_dg_headers(),
            json={"ttl_seconds": 30},
        )
        result["token_grant"] = {
            "status": r_token.status_code,
            "ok": r_token.status_code == 200,
            "error": None if r_token.status_code == 200 else "Key needs Member role (usage:write scope) — see console.deepgram.com → Settings → API Keys",
        }

        # 3. Try billing balance (requires billing:read / Owner role)
        r_balance = await client.get(
            f"https://api.deepgram.com/v1/projects/{project_id}/balances",
            headers=_dg_headers(),
        )
        if r_balance.status_code == 200:
            balances = r_balance.json().get("balances", [])
            result["balances"] = balances
            result["credits_remaining"] = sum(b.get("amount", 0) for b in balances)
        else:
            result["balance_error"] = "Key needs Owner role (billing:read scope) to view credits"

        # 4. Try usage summary
        r_usage = await client.get(
            f"https://api.deepgram.com/v1/projects/{project_id}/usage",
            headers=_dg_headers(),
        )
        if r_usage.status_code == 200:
            result["usage"] = r_usage.json()
        else:
            result["usage_error"] = "Key needs usage:read scope"

    result["ok"] = result["token_grant"]["ok"]
    result["key_ok"] = result["token_grant"]["ok"]
    result["required_fix"] = None if result["ok"] else (
        "Go to console.deepgram.com → Settings → API Keys → Create a new key with 'Member' role. "
        "Replace DEEPGRAM_API_KEY in .env then restart the backend."
    )
    return result


# ── Deepgram cost-aware fallback (pre-recorded STT) ───────────────────────────

DEEPGRAM_LISTEN_URL = "https://api.deepgram.com/v1/listen"


@router.post("/deepgram/stt")
async def deepgram_stt(file: UploadFile = File(...)):
    """Cost-aware Deepgram pre-recorded STT for a single missed JARVIS clip.

    The free browser Web Speech API stays primary; this endpoint is only hit
    when the client detects a recognition "miss". A backend budget guard caps
    spend — when capped, key-missing, or on any Deepgram/network error this
    returns ``used_deepgram=false`` (HTTP 200) so the client silently stays on
    the free engine instead of surfacing an error.
    """
    from app.services import deepgram_budget

    # 1. Budget guard — silently stay on the free engine when capped/disabled.
    if not deepgram_budget.can_spend():
        return {
            "used_deepgram": False,
            "reason": "budget_capped",
            "budget": deepgram_budget.summary(),
        }

    key = _dg_key()
    if not key:
        return {
            "used_deepgram": False,
            "reason": "key_missing",
            "budget": deepgram_budget.summary(),
        }

    try:
        audio = await file.read()
    except Exception:
        audio = b""

    # Empty/too-short clip → skip the call entirely (no spend).
    if not audio or len(audio) < 1024:
        return {
            "used_deepgram": False,
            "reason": "empty_audio",
            "budget": deepgram_budget.summary(),
        }

    content_type = file.content_type or "audio/webm"
    params = {"model": settings.DEEPGRAM_STT_MODEL, "smart_format": "true"}

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                DEEPGRAM_LISTEN_URL,
                params=params,
                headers={"Authorization": f"Token {key}", "Content-Type": content_type},
                content=audio,
            )
        if resp.status_code != 200:
            logger.warning(f"Deepgram STT returned {resp.status_code}: {resp.text[:200]}")
            return {
                "used_deepgram": False,
                "reason": f"deepgram_{resp.status_code}",
                "budget": deepgram_budget.summary(),
            }

        data = resp.json()
        channel = (data.get("results", {}).get("channels") or [{}])[0]
        alt = (channel.get("alternatives") or [{}])[0]
        text = (alt.get("transcript") or "").strip()
        confidence = float(alt.get("confidence") or 0.0)
        duration = float(data.get("metadata", {}).get("duration") or 0.0)

        # Record actual usage (clip duration → cost) only on a successful call.
        budget = deepgram_budget.record_usage(duration)
        return {
            "used_deepgram": True,
            "text": text,
            "confidence": confidence,
            "duration": duration,
            "budget": budget,
        }
    except Exception as e:
        logger.error(f"Deepgram STT fallback error: {e}")
        return {
            "used_deepgram": False,
            "reason": "error",
            "budget": deepgram_budget.summary(),
        }


@router.get("/deepgram/usage")
async def deepgram_usage():
    """Report month/day spend, caps, remaining budget and projected runway."""
    from app.services import deepgram_budget
    return deepgram_budget.summary()


# ── Deepgram function-call webhook handlers ───────────────────────────────────

async def _fn_response(result: Any) -> dict:
    return {"result": result}


@router.post("/deepgram/fn/get_active_signals")
async def dg_fn_get_active_signals():
    """Return active trading signals (server-side Deepgram function call)."""
    try:
        from app.api.signals import get_signals_summary  # lazy import
        signals = await get_signals_summary()
        return await _fn_response(signals)
    except Exception as e:
        logger.error(f"dg_fn_get_active_signals error: {e}")
        return await _fn_response({"error": str(e)})


@router.post("/deepgram/fn/get_price")
async def dg_fn_get_price(payload: dict = None):
    """Return current spot price for a trading symbol."""
    try:
        symbol = (payload or {}).get("symbol", "BTCUSDT")
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}")
        if r.status_code == 200:
            data = r.json()
            return await _fn_response({"symbol": symbol, "price": data.get("price")})
        return await _fn_response({"error": f"Could not fetch price for {symbol}"})
    except Exception as e:
        return await _fn_response({"error": str(e)})


@router.post("/deepgram/fn/place_limit_order")
async def dg_fn_place_limit_order(payload: dict = None):
    """Place a limit order via MT5 plugin."""
    args = payload or {}
    symbol = args.get("symbol", "")
    side = args.get("side", "")
    volume = args.get("volume", 0.01)
    price = args.get("price", 0)
    if not symbol or not side:
        return await _fn_response({"error": "symbol and side are required"})
    try:
        # Route through the MT5 plugin endpoint
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                "http://localhost:8000/api/v1/plugins/mt5-trading/order",
                json={"symbol": symbol, "order_type": "limit", "side": side,
                      "volume": volume, "price": price},
            )
        return await _fn_response(r.json() if r.status_code == 200 else {"error": r.text})
    except Exception as e:
        return await _fn_response({"error": str(e)})


@router.post("/deepgram/fn/get_account_balance")
async def dg_fn_get_account_balance():
    """Return current exchange account balance."""
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get("http://localhost:8000/api/v1/exchanges/balance")
        return await _fn_response(r.json() if r.status_code == 200 else {"error": r.text})
    except Exception as e:
        return await _fn_response({"error": str(e)})


@router.post("/deepgram/fn/get_position_summary")
async def dg_fn_get_position_summary():
    """Return MT5 open positions."""
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get("http://localhost:8000/api/v1/plugins/mt5-trading/positions")
        return await _fn_response(r.json() if r.status_code == 200 else {"error": r.text})
    except Exception as e:
        return await _fn_response({"error": str(e)})

async def get_client():
    if AsyncOpenAI is None:
        return None
    api_key = settings.OPENAI_API_KEY or os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        return None
    return AsyncOpenAI(api_key=api_key)


# ── NVIDIA NIM hosted speech (fast, cloud) ────────────────────────────────────
# Tried first — hosted Parakeet ASR / Magpie TTS inference is far faster than
# the local MLX engine below on this Mac's CPU/MPS. Any error/timeout falls
# straight through to the local engine, then OpenAI, so a stale function-id or
# a network hiccup degrades quietly instead of failing the request.

_riva_tts_service = None


def _nvidia_timeout() -> float:
    return max(settings.NVIDIA_SPEECH_TIMEOUT_MS, 1) / 1000.0


def _get_riva_tts_service():
    global _riva_tts_service
    if riva is None or not settings.NVIDIA_API_KEY:
        return None
    if _riva_tts_service is None:
        auth = riva.client.Auth(
            uri="grpc.nvcf.nvidia.com:443",
            use_ssl=True,
            metadata_args=[
                ["function-id", settings.NVIDIA_TTS_FUNCTION_ID],
                ["authorization", f"Bearer {settings.NVIDIA_API_KEY}"],
            ],
        )
        _riva_tts_service = riva.client.SpeechSynthesisService(auth)
    return _riva_tts_service


async def _nvidia_stt(content: bytes, filename: str) -> Optional[str]:
    if not settings.NVIDIA_ASR_ENABLED or not settings.NVIDIA_API_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=_nvidia_timeout()) as client:
            resp = await client.post(
                settings.NVIDIA_ASR_INVOKE_URL,
                headers={"Authorization": f"Bearer {settings.NVIDIA_API_KEY}"},
                data={"language": settings.NVIDIA_ASR_LANGUAGE},
                files={"file": (filename, content)},
            )
        if resp.status_code != 200:
            logger.warning(f"NVIDIA NIM ASR returned {resp.status_code}: {resp.text[:200]}")
            return None
        data = resp.json()
        text = data.get("text")
        if text is None and isinstance(data.get("results"), list) and data["results"]:
            # Defensive fallback in case the hosted response is segments-shaped
            # rather than OpenAI-Whisper-shaped.
            first = data["results"][0]
            text = first.get("text") or (first.get("alternatives") or [{}])[0].get("transcript")
        return text
    except Exception as e:
        logger.warning(f"NVIDIA NIM ASR unavailable, trying next engine: {e}")
        return None


def _nvidia_tts_sync(text: str) -> bytes:
    service = _get_riva_tts_service()
    if service is None:
        raise RuntimeError("riva client not available or NVIDIA_API_KEY missing")
    resp = service.synthesize(
        text=text,
        voice_name=settings.NVIDIA_TTS_VOICE,
        language_code=settings.NVIDIA_TTS_LANGUAGE,
        sample_rate_hz=settings.NVIDIA_TTS_SAMPLE_RATE_HZ,
        encoding=riva.client.AudioEncoding.LINEAR_PCM,
    )
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(settings.NVIDIA_TTS_SAMPLE_RATE_HZ)
        wav_file.writeframes(resp.audio)
    return buf.getvalue()


async def _nvidia_tts(text: str) -> Optional[tuple[bytes, str]]:
    if not settings.NVIDIA_TTS_ENABLED or not settings.NVIDIA_API_KEY or riva is None:
        return None
    try:
        audio = await asyncio.wait_for(asyncio.to_thread(_nvidia_tts_sync, text), timeout=_nvidia_timeout())
        return audio, "audio/wav"
    except Exception as e:
        logger.warning(f"NVIDIA NIM TTS unavailable, trying next engine: {e}")
        return None


def _warm_nvidia_tts_in_background() -> None:
    """Pre-establish the Riva gRPC/TLS connection at process start.

    The first synthesize() call on a fresh channel is slow/occasionally flaky
    (cold TLS handshake); every call after that is fast (~1s). Paying that
    cost once at startup, off the request path, means the first real user
    request doesn't eat it.
    """
    if not settings.NVIDIA_TTS_ENABLED or not settings.NVIDIA_API_KEY or riva is None:
        return

    def _run():
        try:
            _nvidia_tts_sync("warm up")
            logger.info("NVIDIA NIM TTS: connection warmed")
        except Exception as e:
            logger.warning(f"NVIDIA NIM TTS warmup failed (will retry lazily on first request): {e}")

    import threading
    threading.Thread(target=_run, daemon=True).start()


_warm_nvidia_tts_in_background()


# ── Local speech engine (huggingface/speech-to-speech, Parakeet-TDT + Qwen3-TTS) ─
# Tried after NVIDIA NIM (offline/private fallback — much slower on CPU/MPS);
# any error/timeout falls straight through to the existing OpenAI Whisper/TTS
# path below.

def _speech_engine_timeout() -> float:
    return max(settings.SPEECH_ENGINE_TIMEOUT_MS, 1) / 1000.0


async def _local_stt(content: bytes, filename: str) -> Optional[str]:
    if not settings.SPEECH_ENGINE_ENABLED:
        return None
    try:
        async with httpx.AsyncClient(timeout=_speech_engine_timeout()) as client:
            resp = await client.post(
                f"{settings.SPEECH_ENGINE_URL}/stt",
                files={"file": (filename, content)},
            )
        if resp.status_code != 200:
            logger.warning(f"speech_engine STT returned {resp.status_code}: {resp.text[:200]}")
            return None
        return resp.json().get("text")
    except Exception as e:
        logger.warning(f"speech_engine STT unavailable, falling back to OpenAI: {e}")
        return None


async def _local_tts(text: str) -> Optional[tuple[bytes, str]]:
    if not settings.SPEECH_ENGINE_ENABLED:
        return None
    try:
        async with httpx.AsyncClient(timeout=_speech_engine_timeout()) as client:
            resp = await client.post(
                f"{settings.SPEECH_ENGINE_URL}/tts",
                data={"text": text},
            )
        if resp.status_code != 200:
            logger.warning(f"speech_engine TTS returned {resp.status_code}: {resp.text[:200]}")
            return None
        return resp.content, resp.headers.get("content-type", "audio/wav")
    except Exception as e:
        logger.warning(f"speech_engine TTS unavailable, falling back to OpenAI: {e}")
        return None


@router.post("/stt")
async def speech_to_text(file: UploadFile = File(...)):
    """Convert speech to text — NVIDIA NIM (fast) -> local engine -> OpenAI Whisper."""
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Empty audio file")

    nvidia_text = await _nvidia_stt(content, file.filename or "audio.wav")
    if nvidia_text is not None:
        return {"text": nvidia_text, "engine": "nvidia"}

    local_text = await _local_stt(content, file.filename or "audio.wav")
    if local_text is not None:
        return {"text": local_text, "engine": "local"}

    client = await get_client()
    if not client:
        raise HTTPException(status_code=500, detail="OpenAI API key not configured or openai package missing")

    try:
        buffer = io.BytesIO(content)
        buffer.name = file.filename or "audio.wav"  # Whisper requires a named file
        transcript = await client.audio.transcriptions.create(
            model="whisper-1",
            file=buffer
        )
        return {"text": transcript.text, "engine": "openai"}
    except Exception as e:
        logger.error(f"STT error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/tts")
async def text_to_speech(text: str = Form(...), voice: str = Form("alloy")):
    """Convert text to speech — NVIDIA NIM (fast) -> local engine -> OpenAI TTS."""
    nvidia_result = await _nvidia_tts(text)
    if nvidia_result is not None:
        nvidia_audio, content_type = nvidia_result
        return Response(content=nvidia_audio, media_type=content_type)

    local_result = await _local_tts(text)
    if local_result is not None:
        local_audio, content_type = local_result
        return Response(content=local_audio, media_type=content_type)

    client = await get_client()
    if not client:
        raise HTTPException(status_code=500, detail="OpenAI API key not configured or openai package missing")

    try:
        response = await client.audio.speech.create(
            model="tts-1",
            voice=voice,
            input=text
        )
        return Response(content=response.content, media_type="audio/mpeg")
    except Exception as e:
        logger.error(f"TTS error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
