"""Text-to-image generation via FLUX.1-dev on NVIDIA NIM.

This does not go through :mod:`ai_router`: FLUX lives on a different host
(``ai.api.nvidia.com/v1/genai``) with a plain REST payload and a base64 image
response, not the OpenAI-compatible chat-completions shape every other provider
in this app speaks. It reuses the same ``NVIDIA_API_KEY`` as the chat models.

Scope — text-to-image only, deliberately. FLUX.1-dev on NIM cannot edit a
supplied image: ``mode`` accepts only ``base``/``canny``/``depth``, there is no
img2img, and the ``image`` field rejects both inline base64 and uploaded NVCF
assets (it wants an NVIDIA ``example_id``). Even where canny/depth do apply they
*regenerate* an image guided by edges rather than annotating the original, which
for a chart would mean plausible-looking but invented price levels. Chart markup
therefore goes through :mod:`chart_annotate`, which draws on the real pixels.
"""
from __future__ import annotations

import base64

import httpx
from loguru import logger

from app.core.config import settings

INVOKE_URL = "https://ai.api.nvidia.com/v1/genai/black-forest-labs/flux.1-dev"

#: FLUX only serves this fixed set of output sizes.
SUPPORTED_SIZES = {
    (1024, 1024),
    (768, 1344),
    (1344, 768),
    (1216, 832),
    (832, 1216),
}


async def generate_image(
    prompt: str,
    *,
    width: int = 1024,
    height: int = 1024,
    cfg_scale: float = 5,
    steps: int = 30,
    seed: int | None = None,
    timeout: float = 180.0,
) -> bytes | None:
    """Generate an image from ``prompt``. Returns PNG bytes, or None on failure.

    Returning None rather than raising keeps this usable as an optional
    enhancement: a caller that wanted a picture alongside its text answer should
    still deliver the text when image generation is unavailable.
    """
    if not settings.NVIDIA_API_KEY:
        logger.warning("flux: NVIDIA_API_KEY not configured, skipping generation")
        return None

    if (width, height) not in SUPPORTED_SIZES:
        logger.warning(f"flux: unsupported size {width}x{height}, using 1024x1024")
        width = height = 1024

    payload: dict = {
        "prompt": prompt,
        "mode": "base",
        "cfg_scale": cfg_scale,
        "width": width,
        "height": height,
        "steps": steps,
    }
    if seed is not None:
        payload["seed"] = seed

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                INVOKE_URL,
                json=payload,
                headers={
                    "Authorization": f"Bearer {settings.NVIDIA_API_KEY}",
                    "Accept": "application/json",
                },
            )
            resp.raise_for_status()
            artifacts = resp.json().get("artifacts") or []
    except Exception as exc:
        logger.warning(f"flux: generation failed: {exc}")
        return None

    if not artifacts:
        logger.warning("flux: response contained no artifacts")
        return None

    encoded = artifacts[0].get("base64")
    if not encoded:
        logger.warning("flux: artifact carried no base64 payload")
        return None

    try:
        return base64.b64decode(encoded)
    except Exception as exc:
        logger.warning(f"flux: could not decode artifact: {exc}")
        return None
