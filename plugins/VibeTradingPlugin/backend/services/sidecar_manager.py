"""
VibeTradingPlugin — Sidecar Manager

Manages the lifecycle of the `vibe-trading serve` process.
Uses an idempotent ensure_started() pattern: checks if :8899 is reachable
and spawns the sidecar if not.

All side effects are fire-and-forget; the caller never waits more than a
few seconds for the health check.
"""
from __future__ import annotations

import asyncio
import os
import sys
from typing import Optional
from loguru import logger

from plugins.VibeTradingPlugin.backend.config import vibe_config
from plugins.VibeTradingPlugin.backend.services import vibe_client


_sidecar_proc: Optional[asyncio.subprocess.Process] = None
_start_lock = asyncio.Lock()
_started = False


async def is_reachable() -> bool:
    """Return True if vibe-trading serve is answering on the configured URL."""
    result = await vibe_client.health()
    return "error" not in result


async def ensure_started() -> bool:
    """
    Idempotent: check reachability; if not reachable and auto_start is
    enabled, spawn `vibe-trading serve` in the background.

    Returns True if the service is (or became) reachable within ~10s.
    """
    global _started, _sidecar_proc

    if not vibe_config.auto_start:
        return await is_reachable()

    async with _start_lock:
        if await is_reachable():
            _started = True
            return True

        if _sidecar_proc is not None and _sidecar_proc.returncode is None:
            # Already spawned — give it a few more seconds
            for _ in range(10):
                await asyncio.sleep(1)
                if await is_reachable():
                    _started = True
                    return True
            return False

        logger.info("[VibeSidecar] vibe-trading serve not reachable — attempting to start sidecar")

        vt_cmd = _find_vibe_trading_executable()
        if not vt_cmd:
            logger.warning("[VibeSidecar] vibe-trading executable not found; skipping auto-start")
            return False

        env = os.environ.copy()
        if vibe_config.api_auth_key:
            env["API_AUTH_KEY"] = vibe_config.api_auth_key
        if vibe_config.enable_scheduler:
            env["VIBE_TRADING_ENABLE_SCHEDULER"] = "1"

        port = _extract_port(vibe_config.url)

        try:
            _sidecar_proc = await asyncio.create_subprocess_exec(
                vt_cmd, "serve", "--port", str(port),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
            )
            logger.info(f"[VibeSidecar] started (PID {_sidecar_proc.pid}) on port {port}")
        except Exception as exc:
            logger.error(f"[VibeSidecar] failed to start: {exc}")
            return False

        # Wait up to 15 s for it to become ready
        for _ in range(15):
            await asyncio.sleep(1)
            if await is_reachable():
                _started = True
                logger.info("[VibeSidecar] ready")
                return True

        logger.warning("[VibeSidecar] started but not reachable within 15 s")
        return False


def _find_vibe_trading_executable() -> Optional[str]:
    """Locate the vibe-trading CLI in the backend venv or PATH."""
    candidates = [
        os.path.join(os.path.dirname(sys.executable), "vibe-trading"),
        os.path.join(os.path.dirname(sys.executable), "vibe-trading.exe"),
        "vibe-trading",
    ]
    for c in candidates:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    # Also try shutil.which
    import shutil
    found = shutil.which("vibe-trading")
    return found


def _extract_port(url: str) -> int:
    try:
        return int(url.rstrip("/").split(":")[-1])
    except (ValueError, IndexError):
        return 8899


async def stop_sidecar() -> None:
    global _sidecar_proc, _started
    if _sidecar_proc and _sidecar_proc.returncode is None:
        try:
            _sidecar_proc.terminate()
            await asyncio.wait_for(_sidecar_proc.wait(), timeout=5)
        except Exception:
            pass
    _sidecar_proc = None
    _started = False
    logger.info("[VibeSidecar] stopped")
