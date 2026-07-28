#!/usr/bin/env python3
"""
Self-hosted Headroom Proxy Server
=================================
Replaces the Cloudflare Workers headroom proxy with a local FastAPI service.
No rate limits, runs in Docker or locally.

API Endpoints:
- POST /p/{project}/v1/chat/completions  (Cloudflare Workers format)
- POST /v1/chat/completions              (Local headroom binary format)

Environment Variables:
- HEADROOM_PROXY_PORT: Port to listen on (default: 8787)
- HEADROOM_PROXY_HOST: Host to bind (default: 0.0.0.0)
- HEADROOM_OPENAI_API_KEY: Default OpenAI API key (can be overridden per-request)
- HEADROOM_OPENAI_BASE_URL: Default OpenAI base URL (default: https://api.openai.com/v1)
- HEADROOM_LOG_LEVEL: Log level (default: INFO)
"""

import os
import json
import asyncio
import logging
from typing import Any, Dict, List, Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings
import httpx
import uvicorn

# Headroom compression
try:
    from headroom import compress as headroom_compress
    HEADROOM_AVAILABLE = True
except ImportError:
    HEADROOM_AVAILABLE = False
    headroom_compress = None

# Configure logging
logging.basicConfig(
    level=os.getenv("HEADROOM_LOG_LEVEL", "INFO"),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("headroom-proxy")


class Settings(BaseSettings):
    port: int = 8787
    host: str = "0.0.0.0"
    # Support both HEADROOM_OPENAI_* and OPENAI_* env vars for flexibility
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    request_timeout: float = 60.0
    max_concurrent_requests: int = 100
    
    class Config:
        env_prefix = "HEADROOM_"
        case_sensitive = False
        extra = "allow"

    def __init__(self, **kwargs):
        # Allow fallback to OPENAI_* env vars
        super().__init__(**kwargs)
        if not self.openai_api_key:
            self.openai_api_key = os.getenv("OPENAI_API_KEY", "")
        if self.openai_base_url == "https://api.openai.com/v1":
            self.openai_base_url = os.getenv("OPENAI_BASE_URL", self.openai_base_url)


settings = Settings()

# Semaphore for concurrency control
request_semaphore = asyncio.Semaphore(settings.max_concurrent_requests)


class ChatCompletionRequest(BaseModel):
    model: str
    messages: List[Dict[str, Any]]
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    max_completion_tokens: Optional[int] = None
    stream: bool = False
    response_format: Optional[Dict[str, str]] = None
    # Extra fields passed through
    extra: Dict[str, Any] = Field(default_factory=dict, alias="__extra__")
    
    class Config:
        extra = "allow"
        populate_by_name = True


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: List[Dict[str, Any]]
    usage: Dict[str, int]
    # Headroom-specific headers (in body for streaming)
    x_headroom_savings: Optional[int] = None
    x_headroom_original_tokens: Optional[int] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"Starting Headroom Proxy on {settings.host}:{settings.port}")
    logger.info(f"Headroom available: {HEADROOM_AVAILABLE}")
    logger.info(f"Default OpenAI base URL: {settings.openai_base_url}")
    yield
    logger.info("Shutting down Headroom Proxy")


app = FastAPI(
    title="Headroom Proxy",
    description="Self-hosted token compression proxy for LLM requests",
    version="1.0.0",
    lifespan=lifespan
)


def compress_messages(messages: List[Dict[str, Any]]) -> tuple[List[Dict[str, Any]], int]:
    """
    Compress messages using headroom.
    Returns (compressed_messages, tokens_saved_estimate)
    """
    if not HEADROOM_AVAILABLE or not messages:
        return messages, 0
    
    try:
        original_chars = sum(len(str(m.get("content", ""))) for m in messages)
        result = headroom_compress(messages)
        
        # Handle different return types
        if isinstance(result, list):
            compressed = result
        elif hasattr(result, "messages"):
            compressed = result.messages
        elif hasattr(result, "__iter__"):
            compressed = list(result)
        else:
            return messages, 0
        
        if not isinstance(compressed, list) or not compressed:
            return messages, 0
        
        compressed_chars = sum(len(str(m.get("content", ""))) for m in compressed)
        # Rough token estimate: 4 chars = 1 token
        tokens_saved = max(0, (original_chars - compressed_chars) // 4)
        
        logger.debug(f"Headroom compressed {original_chars} -> {compressed_chars} chars (~{tokens_saved} tokens saved)")
        return compressed, tokens_saved
        
    except Exception as e:
        logger.warning(f"Headroom compression failed: {e}")
        return messages, 0


async def forward_request(
    url: str,
    headers: Dict[str, str],
    payload: Dict[str, Any],
    stream: bool = False
) -> httpx.Response:
    """Forward request to upstream provider."""
    async with httpx.AsyncClient(timeout=settings.request_timeout) as client:
        if stream:
            # For streaming, we need to handle the response differently
            req = client.build_request("POST", url, headers=headers, json=payload)
            response = await client.send(req, stream=True)
            return response
        else:
            response = await client.post(url, headers=headers, json=payload)
            return response


async def _handle_chat_completions(
    project: str,
    request: Request,
    authorization: Optional[str] = None,
    content_type: Optional[str] = None
):
    """Shared handler for both /p/{project}/v1/ and /v1/ endpoints."""
    async with request_semaphore:
        try:
            # Parse request body
            body = await request.json()
            chat_request = ChatCompletionRequest(**body)
            
            # Extract API key from Authorization header or use default
            api_key = None
            if authorization and authorization.startswith("Bearer "):
                api_key = authorization[7:]
            elif settings.openai_api_key:
                api_key = settings.openai_api_key
            
            if not api_key:
                raise HTTPException(401, "No API key provided (Authorization header or HEADROOM_OPENAI_API_KEY)")
            
            # Determine target URL
            # If the request includes a base_url in extra, use it
            target_base = chat_request.extra.pop("base_url", settings.openai_base_url)
            target_url = f"{target_base.rstrip('/')}/chat/completions"
            
            # Compress messages using headroom
            original_messages = chat_request.messages
            compressed_messages, tokens_saved = compress_messages(original_messages)
            
            # Build payload for upstream
            upstream_payload = {
                "model": chat_request.model,
                "messages": compressed_messages,
                "stream": chat_request.stream,
            }
            
            # Add optional parameters
            if chat_request.temperature is not None:
                upstream_payload["temperature"] = chat_request.temperature
            if chat_request.max_tokens is not None:
                upstream_payload["max_tokens"] = chat_request.max_tokens
            if chat_request.max_completion_tokens is not None:
                upstream_payload["max_completion_tokens"] = chat_request.max_completion_tokens
            if chat_request.response_format:
                upstream_payload["response_format"] = chat_request.response_format
            
            # Include any extra fields
            for key, value in chat_request.extra.items():
                if key not in upstream_payload:
                    upstream_payload[key] = value
            
            # Headers for upstream
            upstream_headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            
            logger.info(f"[{project}] Proxying request to {chat_request.model} (compressed: {tokens_saved} tokens saved)")
            
            # Forward to upstream
            response = await forward_request(
                target_url,
                upstream_headers,
                upstream_payload,
                stream=chat_request.stream
            )
            
            # Handle errors
            if response.status_code >= 400:
                error_text = await response.aread() if hasattr(response, 'aread') else response.text
                logger.error(f"[{project}] Upstream error {response.status_code}: {error_text}")
                raise HTTPException(response.status_code, f"Upstream error: {error_text.decode() if isinstance(error_text, bytes) else error_text}")
            
            if chat_request.stream:
                # Streaming response - add headroom headers
                async def stream_with_headers():
                    # Yield initial headers as first chunk (SSE format)
                    yield f"data: {json.dumps({'x_headroom_savings': tokens_saved})}\n\n"
                    async for chunk in response.aiter_bytes():
                        yield chunk
                
                return StreamingResponse(
                    stream_with_headers(),
                    media_type="text/event-stream",
                    headers={
                        "X-Headroom-Savings": str(tokens_saved),
                        "X-Headroom-Original-Tokens": str(sum(len(str(m.get("content", ""))) for m in original_messages) // 4),
                    }
                )
            else:
                # Non-streaming response
                data = response.json()
                
                # Add headroom info to response
                data["x_headroom_savings"] = tokens_saved
                original_tokens = sum(len(str(m.get("content", ""))) for m in original_messages) // 4
                data["x_headroom_original_tokens"] = original_tokens
                
                # Add headers for tracking
                headers = {
                    "X-Headroom-Savings": str(tokens_saved),
                    "X-Headroom-Original-Tokens": str(original_tokens),
                }
                
                return JSONResponse(content=data, headers=headers)
                
        except HTTPException:
            raise
        except Exception as e:
            logger.exception(f"[{project}] Proxy error")
            raise HTTPException(500, f"Proxy error: {str(e)}")


@app.post("/p/{project}/v1/chat/completions")
async def chat_completions_project(
    project: str,
    request: Request,
    authorization: Optional[str] = Header(None),
    content_type: Optional[str] = Header(None)
):
    """
    OpenAI-compatible chat completions endpoint with headroom compression.
    
    The project parameter is used for tracking/analytics (e.g., "tradebot").
    """
    return await _handle_chat_completions(project, request, authorization, content_type)


@app.post("/v1/chat/completions")
async def chat_completions_simple(
    request: Request,
    authorization: Optional[str] = Header(None),
    content_type: Optional[str] = Header(None)
):
    """
    OpenAI-compatible chat completions endpoint (no project prefix).
    Compatible with local headroom binary format.
    """
    return await _handle_chat_completions("default", request, authorization, content_type)


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "headroom_available": HEADROOM_AVAILABLE,
        "project": "tradebot"
    }


@app.get("/p/{project}/health")
async def project_health(project: str):
    """Project-specific health check (compatible with headroom dashboard)."""
    return {
        "status": "healthy",
        "project": project,
        "headroom_available": HEADROOM_AVAILABLE
    }


if __name__ == "__main__":
    uvicorn.run(
        "headroom_proxy:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        log_level=settings.log_level.lower() if hasattr(settings, 'log_level') else "info"
    )