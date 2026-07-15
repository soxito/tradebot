"""OpenManusPlugin — Install service.

Manages downloading, installing, and starting/stopping the OpenManus process.
All operations are best-effort and idempotent.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from loguru import logger

from plugins.OpenManusPlugin.backend.config import openmanus_config

OPENMANUS_REPO = "https://github.com/FoundationAgents/OpenManus.git"
OPENMANUS_BRANCH = "main"

_openmanus_proc: Optional[subprocess.Popen] = None


def _install_dir() -> Path:
    return Path(openmanus_config.install_dir).expanduser().resolve()


def is_installed() -> bool:
    """Return True if the OpenManus repo exists in install_dir."""
    d = _install_dir()
    return (d / "run_mcp.py").exists() or (d / "main.py").exists()


def installed_version() -> Optional[str]:
    """Return the git short hash of the installed OpenManus, or None."""
    d = _install_dir()
    if not d.exists():
        return None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(d),
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip() or None
    except Exception:  # noqa: BLE001
        return None


async def install() -> dict:
    """Clone/update OpenManus into install_dir and install its dependencies.

    Returns {"ok": bool, "message": str}.
    """
    d = _install_dir()
    try:
        if d.exists() and (d / ".git").exists():
            # Pull latest
            logger.info("[OpenManus] Updating existing install at {}", d)
            proc = await asyncio.create_subprocess_exec(
                "git", "pull", "--ff-only",
                cwd=str(d),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
            if proc.returncode != 0:
                return {"ok": False, "message": stderr.decode()[:400]}
        else:
            # Fresh clone
            d.parent.mkdir(parents=True, exist_ok=True)
            logger.info("[OpenManus] Cloning OpenManus → {}", d)
            proc = await asyncio.create_subprocess_exec(
                "git", "clone", "--depth=1", "--branch", OPENMANUS_BRANCH,
                OPENMANUS_REPO, str(d),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            if proc.returncode != 0:
                return {"ok": False, "message": stderr.decode()[:400]}

        # Install Python dependencies
        req_file = d / "requirements.txt"
        if req_file.exists():
            logger.info("[OpenManus] Installing requirements.txt")
            proc = await asyncio.create_subprocess_exec(
                openmanus_config.python_bin, "-m", "pip", "install",
                "-q", "-r", str(req_file),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)
            if proc.returncode != 0:
                msg = stderr.decode()[:400]
                logger.warning("[OpenManus] pip install warning: {}", msg)

        version = installed_version() or "unknown"
        return {"ok": True, "message": f"Installed at {d} (commit {version})"}

    except asyncio.TimeoutError:
        return {"ok": False, "message": "Installation timed out"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": str(exc)[:400]}


async def start_mcp_server(transport: str = "sse", port: int = 8765) -> dict:
    """Start OpenManus MCP server as a background subprocess.

    Returns {"ok": bool, "pid": int|None, "url": str|None, "error": str|None}.
    """
    global _openmanus_proc

    if _openmanus_proc is not None and _openmanus_proc.poll() is None:
        return {
            "ok": True,
            "pid": _openmanus_proc.pid,
            "url": f"http://127.0.0.1:{port}",
            "error": None,
        }

    d = _install_dir()
    mcp_script = d / "run_mcp.py"
    if not mcp_script.exists():
        return {"ok": False, "pid": None, "url": None, "error": "OpenManus not installed"}

    cmd = [
        openmanus_config.python_bin,
        str(mcp_script),
        "--transport", transport,
        "--port", str(port),
    ]

    try:
        env = {**os.environ, "PYTHONPATH": str(d)}
        _openmanus_proc = subprocess.Popen(
            cmd,
            cwd=str(d),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        logger.info("[OpenManus] MCP server started PID={} on port {}", _openmanus_proc.pid, port)
        return {
            "ok": True,
            "pid": _openmanus_proc.pid,
            "url": f"http://127.0.0.1:{port}",
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "pid": None, "url": None, "error": str(exc)[:300]}


def stop_mcp_server() -> bool:
    """Terminate the OpenManus MCP server subprocess if running."""
    global _openmanus_proc
    if _openmanus_proc is not None:
        try:
            _openmanus_proc.terminate()
            _openmanus_proc.wait(timeout=5)
            logger.info("[OpenManus] MCP server stopped")
        except Exception:  # noqa: BLE001
            try:
                _openmanus_proc.kill()
            except Exception:  # noqa: BLE001
                pass
        finally:
            _openmanus_proc = None
        return True
    return False


def server_running() -> bool:
    """Return True if the managed MCP subprocess is alive."""
    return _openmanus_proc is not None and _openmanus_proc.poll() is None
