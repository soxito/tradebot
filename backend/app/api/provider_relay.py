"""
Provider Relay — transparent HTTP relay that sits between the headroom proxy
and the actual AI provider endpoints.

Flow:
  ai_router.py  →  headroom proxy (port 8787, tracks tokens + savings)
               →  /api/v1/provider-relay/v1/chat/completions
               →  actual provider (Groq / Mistral / Cerebras / OpenAI / …)

The headroom proxy is configured with:
  OPENAI_TARGET_API_URL = http://127.0.0.1:1448/api/v1/provider-relay

ai_router.py routes every call through headroom but adds
  X-Target-Base: https://api.groq.com/openai/v1        ← actual provider URL

This relay reads X-Target-Base, strips it, and forwards the request to the
real provider — returning the unmodified response so headroom can parse the
model name and record accurate per-model stats.
"""
from __future__ import annotations

import httpx
from fastapi import APIRouter, Request, Response
from loguru import logger

router = APIRouter(prefix="/provider-relay", tags=["provider-relay"])

_TIMEOUT = 60.0
_DEFAULT_TARGET = "https://api.openai.com"


@router.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    include_in_schema=False,
)
async def relay(path: str, request: Request) -> Response:
    """
    Forward every request to the URL specified in the X-Target-Base header,
    falling back to the OpenAI API when the header is absent.
    """
    target_base = request.headers.get("x-target-base", _DEFAULT_TARGET).rstrip("/")
    target_url = f"{target_base}/{path}"

    # Build forwarded headers — strip hop-by-hop and our custom header
    skip = {"host", "x-target-base", "content-length", "transfer-encoding"}
    fwd_headers = {
        k: v for k, v in request.headers.items() if k.lower() not in skip
    }

    body = await request.body()
    params = dict(request.query_params)

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
            resp = await client.request(
                method=request.method,
                url=target_url,
                headers=fwd_headers,
                content=body,
                params=params,
            )

        logger.debug(
            f"[relay] {request.method} {target_url} → {resp.status_code} "
            f"({len(resp.content)} bytes)"
        )
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers={
                k: v for k, v in resp.headers.items()
                if k.lower() not in {"content-encoding", "transfer-encoding"}
            },
            media_type=resp.headers.get("content-type"),
        )

    except httpx.RequestError as exc:
        logger.warning(f"[relay] request error → {target_url}: {exc}")
        return Response(
            content=f'{{"error": "relay error: {exc}"}}',
            status_code=502,
            media_type="application/json",
        )
