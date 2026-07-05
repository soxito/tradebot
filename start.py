#!/usr/bin/env python3
"""
TradeBot Startup Script
=======================
Starts all services in order, waits for each one to be healthy,
then prints a final status table.

Usage:
    python3 start.py                   # auto-detect DB mode (brew preferred)
    python3 start.py --brew            # force Homebrew postgres/redis
    python3 start.py --docker          # force Docker postgres/redis
    python3 start.py --stop            # stop all services
    python3 start.py --status          # show what's running

Services started:
  1. PostgreSQL (Homebrew port 5434 or Docker port 5433)
  2. Redis     (port 6379 or 6380)
  3. FastAPI backend  (port 1448)
  4. Next.js frontend (port 3000)
"""

import argparse
import os
import subprocess
import sys
import time
import socket
import shutil
import json
import platform
import urllib.request
from pathlib import Path
from typing import Optional, Dict, Tuple, List

# ── Colours ────────────────────────────────────────────────────────────────────
class C:
    RED    = "\033[0;31m"
    GREEN  = "\033[0;32m"
    YELLOW = "\033[1;33m"
    CYAN   = "\033[0;36m"
    BOLD   = "\033[1m"
    RESET  = "\033[0m"

def ok(msg):    print(f"  {C.GREEN}✓{C.RESET}  {msg}")
def fail(msg):  print(f"  {C.RED}✗{C.RESET}  {C.RED}{msg}{C.RESET}")
def info(msg):  print(f"  {C.CYAN}▶{C.RESET}  {msg}")
def warn(msg):  print(f"  {C.YELLOW}!{C.RESET}  {C.YELLOW}{msg}{C.RESET}")
def header(msg): print(f"\n{C.BOLD}{C.CYAN}{msg}{C.RESET}")
def sep():       print(f"  {'─' * 56}")

# ── Paths ──────────────────────────────────────────────────────────────────────
ROOT        = Path(__file__).parent.resolve()
BACKEND_DIR = ROOT / "backend"
FRONTEND_DIR = ROOT / "frontend"
VENV        = BACKEND_DIR / ".venv"
# Windows virtualenvs put executables in "Scripts" with a ".exe" suffix;
# POSIX venvs use "bin" with no suffix. Compute both so every subprocess call
# targets a file that actually exists (avoids WinError 2 on Windows).
IS_WINDOWS  = os.name == "nt"
_VENV_BIN   = "Scripts" if IS_WINDOWS else "bin"
_EXE        = ".exe" if IS_WINDOWS else ""
VENV_BIN    = VENV / _VENV_BIN
UVICORN_BIN = VENV_BIN / f"uvicorn{_EXE}"
PY_BIN      = VENV_BIN / f"python{_EXE}"
PIP_BIN     = VENV_BIN / f"pip{_EXE}"
PG_BIN      = Path("/opt/homebrew/opt/postgresql@16/bin")
MODE_FILE   = ROOT / ".db-mode"
HOME        = Path.home()

# ── Integration paths/config ──────────────────────────────────────────────────
HEADROOM_PORT = int(os.environ.get("HEADROOM_PORT", "8787"))
HEADROOM_URL = f"http://127.0.0.1:{HEADROOM_PORT}"
HEADROOM_LOG = HOME / "Library" / "Logs" / "headroom.log"
HEADROOM_PLIST = HOME / "Library" / "LaunchAgents" / "ai.headroomlabs.headroom.plist"
HEADROOM_DATA_DIR = HOME / ".headroom"
HEADROOM_PROXY_KEY = os.environ.get("HEADROOM_PROXY_KEY", "tradebot-local-headroom")

OBSIDIAN_APP_PATH = Path("/Applications/Obsidian.app")
OBSIDIAN_VAULT_DIR = (ROOT / "obsidian-vault").resolve()
OBSIDIAN_SETUP_FILE = OBSIDIAN_VAULT_DIR / "SETUP.md"

# ── Ports ─────────────────────────────────────────────────────────────────────
BACKEND_PORT  = int(os.environ.get("BACKEND_PORT", "1448"))
FRONTEND_PORT = int(os.environ.get("FRONTEND_PORT", "3000"))

# ── MT5 REST config (read from .env if present, else defaults) ────────────────
def _read_dotenv() -> Dict[str, str]:
    env: Dict[str, str] = {}
    dotenv = ROOT / ".env"
    if dotenv.exists():
        for line in dotenv.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env

_DOTENV = _read_dotenv()

# ── System resource detection ─────────────────────────────────────────────────
# Detect the host's CPU cores + total RAM once at startup and derive settings so
# the app runs within the machine's means (Node heap size for the frontend, ML
# thread caps for the backend, and a UI quality-tier hint for the browser).
_RESOURCES: Optional[Dict[str, object]] = None


def _total_ram_bytes() -> int:
    system = platform.system().lower()
    try:
        if system == "darwin":
            r = subprocess.run(["sysctl", "-n", "hw.memsize"],
                               capture_output=True, text=True, timeout=5)
            if r.returncode == 0 and r.stdout.strip().isdigit():
                return int(r.stdout.strip())
        elif system == "linux":
            for ln in Path("/proc/meminfo").read_text().splitlines():
                if ln.startswith("MemTotal:"):
                    return int(ln.split()[1]) * 1024  # kB → bytes
        elif system == "windows":
            # POSIX os.sysconf doesn't exist on Windows, so query the kernel
            # directly via GlobalMemoryStatusEx (stdlib ctypes, no deps). Without
            # this a 16 GB Windows box falls through to the 8 GB assumption below.
            import ctypes

            class _MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = _MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                return int(stat.ullTotalPhys)
    except Exception:
        pass
    # Portable fallback (POSIX)
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except Exception:
        return 8 * 1024 ** 3  # assume 8 GB if truly unknown


def _physical_cores(logical: int) -> int:
    system = platform.system().lower()
    try:
        if system == "darwin":
            r = subprocess.run(["sysctl", "-n", "hw.physicalcpu"],
                               capture_output=True, text=True, timeout=5)
            if r.returncode == 0 and r.stdout.strip().isdigit():
                return int(r.stdout.strip())
        elif system == "linux":
            import re
            ids = set(re.findall(r"core id\s*:\s*(\d+)", Path("/proc/cpuinfo").read_text()))
            if ids:
                return len(ids)
        elif system == "windows":
            # os.cpu_count() reports logical CPUs (4 on a 2-core/4-thread
            # i5-4300U), which would over-provision ML/BLAS threads. Query the
            # real physical core count via CIM (PowerShell), then legacy wmic.
            import re
            for cmd in (
                ["powershell", "-NoProfile", "-Command",
                 "(Get-CimInstance Win32_Processor | "
                 "Measure-Object -Property NumberOfCores -Sum).Sum"],
                ["wmic", "cpu", "get", "NumberOfCores"],
            ):
                try:
                    r = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
                except (OSError, subprocess.SubprocessError):
                    continue
                if r.returncode == 0:
                    nums = [int(x) for x in re.findall(r"\d+", r.stdout)]
                    total = sum(nums)
                    if total > 0:
                        return total
    except Exception:
        pass
    # Cross-platform last resort: psutil if it happens to be importable.
    try:
        import psutil  # type: ignore
        pc = psutil.cpu_count(logical=False)
        if pc:
            return int(pc)
    except Exception:
        pass
    return logical


def detect_resources() -> Dict[str, object]:
    """Detect CPU + RAM and derive resource-aware settings for the app."""
    logical = os.cpu_count() or 4
    physical = _physical_cores(logical)
    ram_gb = _total_ram_bytes() / (1024 ** 3)

    # Node heap for the Next.js dev server — leave plenty of RAM for the OS and
    # the browser (which is the real memory hog on 8 GB laptops).
    if ram_gb <= 9:          # ~8 GB machines (e.g. Apple M2 2022)
        node_heap = 1536
    elif ram_gb <= 17:       # ~16 GB machines (e.g. M2 Pro)
        node_heap = 3072
    else:
        node_heap = 4096

    # ML/BLAS thread cap — leave 2 cores for the event loop + OS so torch/numpy
    # can't saturate the CPU and stall the API on smaller machines.
    if physical > 4:
        ml_threads = max(2, physical - 2)
    else:
        # Low-core machines (≤4 physical, e.g. a dual-core i5-4300U reporting
        # 2 physical / 4 logical): keep ML work from starving the async event
        # loop + OS by leaving ~2 logical cores free (but always ≥1 thread).
        ml_threads = max(1, min(physical, logical - 2))

    # UI quality-tier hint for the frontend (mirrors devicePerformance.ts).
    lc = logical
    if lc <= 4:
        ui_tier = "low"
    elif lc <= 8:
        ui_tier = "medium"
    elif lc <= 10:
        ui_tier = "high"
    else:
        ui_tier = "ultra"
    if ram_gb <= 9 and ui_tier in ("high", "ultra"):
        ui_tier = "medium"

    return {
        "logical": logical,
        "physical": physical,
        "ram_gb": round(ram_gb, 1),
        "node_heap_mb": node_heap,
        "ml_threads": ml_threads,
        "ui_tier": ui_tier,
        # Hard 3D/WebGL kill-switch for very weak GPUs (e.g. the Intel HD 4400
        # in an i5-4300U): on the 'low' tier we tell the browser to skip the
        # Three.js robot / SOX orb / force-graph-3d entirely instead of relying
        # only on the adaptive FPS downgrade.
        "disable_3d": ui_tier == "low",
    }


def get_resources() -> Dict[str, object]:
    global _RESOURCES
    if _RESOURCES is None:
        _RESOURCES = detect_resources()
    return _RESOURCES


def print_resources() -> None:
    r = get_resources()
    header("System resources")
    ok(f"CPU: {r['physical']} physical / {r['logical']} logical cores")
    ok(f"RAM: {r['ram_gb']} GB")
    info(f"Frontend Node heap: {r['node_heap_mb']} MB  ·  backend ML threads: {r['ml_threads']}  ·  UI tier: {str(r['ui_tier']).upper()}")
    if r.get("disable_3d"):
        info("Low-power GPU profile: 3D/WebGL effects disabled (robot · orb · 3D graph)")

MT5_API_URL   = _DOTENV.get("MT5_API_URL", os.environ.get("MT5_API_URL", "http://localhost:8092"))
MT5_IMAGE     = "timurila/mt5rest"     # Docker image for mtapi-io REST bridge
MT5_CONTAINER = "mt5rest"

# ── Kronos forecaster + OpenHuman (Agent Paul subconscious) config ─────────────
# Make the ML forecaster and the "keeps thinking" idle brain explicit at launch.
# A user's shell / .env still wins — these are applied to the backend env with
# setdefault(), so they only fill in values that aren't already set.
KRONOS_MODEL_NAME = _DOTENV.get("KRONOS_MODEL_NAME", os.environ.get("KRONOS_MODEL_NAME", "NeoQuasar/Kronos-base"))
KRONOS_DEVICE     = _DOTENV.get("KRONOS_DEVICE", os.environ.get("KRONOS_DEVICE", ""))  # "" = auto (cuda/mps/cpu)
PAUL_HEARTBEAT_ENABLED = _DOTENV.get("PAUL_HEARTBEAT_ENABLED", os.environ.get("PAUL_HEARTBEAT_ENABLED", "1"))
PAUL_HEARTBEAT_GOAL_CONTINUATION = _DOTENV.get("PAUL_HEARTBEAT_GOAL_CONTINUATION", os.environ.get("PAUL_HEARTBEAT_GOAL_CONTINUATION", "1"))
PAUL_HEARTBEAT_TICK_SECONDS = _DOTENV.get("PAUL_HEARTBEAT_TICK_SECONDS", os.environ.get("PAUL_HEARTBEAT_TICK_SECONDS", "300"))

# ── Helpers ───────────────────────────────────────────────────────────────────
def port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_for_port(host: str, port: int, label: str, max_wait: int = 60) -> bool:
    info(f"Waiting for {label} on {host}:{port} …")
    for i in range(max_wait):
        if port_open(host, port):
            ok(f"{label} ready  (:{port})")
            return True
        time.sleep(1)
        if (i + 1) % 10 == 0:
            warn(f"  still waiting … {i + 1}s elapsed")
    fail(f"{label} did not become ready after {max_wait}s")
    return False


def _augment_path() -> None:
    """
    Make sure common tool locations are on PATH for THIS process.

    `python3 start.py` is frequently launched from an IDE / GUI / launchd
    context whose PATH is minimal, so tools that are actually installed
    (Homebrew in /opt/homebrew, Node via nvm, Postgres@16, etc.) are invisible
    to `shutil.which()` and `subprocess` — which made pre-flight report them as
    "not found" and bail out with a wall of unresolved issues.

    This prepends every standard tool directory that exists (including the most
    recent nvm Node install and `brew shellenv`) so detection + auto-install
    work regardless of how the script was started. It only adds paths; it never
    removes anything.
    """
    candidates: List[str] = [
        "/opt/homebrew/bin", "/opt/homebrew/sbin",
        "/usr/local/bin", "/usr/local/sbin",
        "/opt/local/bin", "/opt/local/sbin",
        str(PG_BIN),
        str(HOME / ".local" / "bin"),
        "/usr/bin", "/bin", "/usr/sbin", "/sbin",
    ]

    # Discover the newest nvm-managed Node install (node/npm/npx live here).
    nvm_versions = HOME / ".nvm" / "versions" / "node"
    if nvm_versions.is_dir():
        try:
            newest = sorted(
                (d for d in nvm_versions.iterdir() if (d / "bin").is_dir()),
                key=lambda d: d.name, reverse=True,
            )
            if newest:
                candidates.insert(0, str(newest[0] / "bin"))
        except Exception:
            pass

    # Pull in Homebrew's own shellenv (covers non-standard prefixes too).
    brew = shutil.which("brew")
    if not brew:
        for bp in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew"):
            if Path(bp).exists():
                brew = bp
                break
    if brew and Path(brew).exists():
        try:
            r = subprocess.run([brew, "shellenv"], capture_output=True, text=True, timeout=10)
            for ln in r.stdout.splitlines():
                # Lines look like: export PATH="/opt/homebrew/bin:...";
                if ln.startswith("export PATH="):
                    val = ln.split("=", 1)[1].strip().strip('";')
                    for part in val.split(":"):
                        if part and "$PATH" not in part:
                            candidates.append(part)
        except Exception:
            pass

    existing = os.environ.get("PATH", "").split(os.pathsep)
    seen = set(existing)
    prepend: List[str] = []
    for c in candidates:
        if c and c not in seen and Path(c).is_dir():
            prepend.append(c)
            seen.add(c)
    if prepend:
        os.environ["PATH"] = os.pathsep.join(prepend + existing)


def run(cmd: List[str], cwd=None, env=None) -> subprocess.CompletedProcess:
    merged = {**os.environ, **(env or {})}
    return subprocess.run(cmd, cwd=cwd, env=merged,
                          capture_output=True, text=True)


def pgrep(pattern: str) -> List[int]:
    # pgrep is Unix-only; on Windows (or if absent) return no matches rather
    # than crashing with FileNotFoundError / WinError 2.
    try:
        r = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True)
    except (OSError, subprocess.SubprocessError):
        return []
    return [int(p) for p in r.stdout.split() if p.strip().isdigit()]


def pkill(pattern: str) -> None:
    try:
        subprocess.run(["pkill", "-f", pattern], capture_output=True)
    except (OSError, subprocess.SubprocessError):
        pass


def _pid_alive(pid: int) -> bool:
    """True if a process with `pid` is currently running (cross-platform)."""
    if IS_WINDOWS:
        try:
            r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"],
                               capture_output=True, text=True)
            return r.returncode == 0 and str(pid) in r.stdout
        except (OSError, subprocess.SubprocessError):
            return False
    try:
        os.kill(pid, 0)  # signal 0 = existence check, doesn't kill
        return True
    except OSError:
        return False


def _kill_pid_tree(pid: int) -> bool:
    """Terminate a process and its children (cross-platform)."""
    try:
        if IS_WINDOWS:
            # taskkill /T also kills the child processes (next dev spawns them).
            r = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, text=True)
            return r.returncode == 0
        os.kill(pid, 15)  # SIGTERM
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _npm_cmd() -> str:
    # On Windows npm/npx are ``npm.cmd``/``npx.cmd``; bare "npm" isn't found by
    # subprocess (no PATHEXT resolution) → WinError 2. shutil.which resolves the
    # real path (incl. the .cmd suffix), which subprocess can launch directly.
    return shutil.which("npm") or ("npm.cmd" if IS_WINDOWS else "npm")


def _npx_cmd() -> str:
    return shutil.which("npx") or ("npx.cmd" if IS_WINDOWS else "npx")


def http_ok(url: str, timeout: float = 5.0) -> bool:
    try:
        urllib.request.urlopen(url, timeout=timeout)
        return True
    except Exception:
        return False


def http_json(url: str, timeout: float = 5.0) -> Optional[dict]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except Exception:
        return None


def ensure_dir(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        return True
    except Exception as ex:
        fail(f"Could not create directory {path}: {ex}")
        return False


def _on_macos() -> bool:
    return platform.system().lower() == "darwin"


def _headroom_bin() -> Optional[str]:
    candidates = [
        shutil.which("headroom"),
        str(HOME / ".local" / "bin" / "headroom"),
        "/opt/homebrew/bin/headroom",
        "/usr/local/bin/headroom",
    ]
    for c in candidates:
        if c and Path(c).exists():
            return c
    return None


def _obsidian_installed() -> bool:
    if OBSIDIAN_APP_PATH.exists():
        return True
    r = subprocess.run(["open", "-Ra", "Obsidian"], capture_output=True)
    return r.returncode == 0


def ensure_obsidian() -> bool:
    if _obsidian_installed():
        ok("Obsidian is installed")
        return True

    warn("Obsidian not found — attempting auto-install …")
    if not _on_macos():
        fail("Auto-install for Obsidian is only implemented on macOS")
        return False

    brew = shutil.which("brew") or "/opt/homebrew/bin/brew"
    if not Path(brew).exists():
        fail("Homebrew not found; install Obsidian manually from https://obsidian.md")
        return False

    ok_inst, err = _spinner_run([brew, "install", "--cask", "obsidian"], "brew install --cask obsidian")
    if not ok_inst:
        fail(f"Failed to install Obsidian: {err[:200]}")
        return False

    if _obsidian_installed():
        ok("Obsidian installed")
        return True

    fail("Obsidian install command completed but app not detected")
    return False


def ensure_env_obsidian() -> None:
    """Inject missing Obsidian env vars into the root .env file.

    Only adds keys that are not already present — never overwrites existing
    values (e.g. a user-supplied REST token).  The OBSIDIAN_REST_TOKEN line is
    added as a placeholder comment so the user knows exactly where to paste the
    token they copy from the Local REST API plugin.
    """
    dotenv_path = ROOT / ".env"
    if not dotenv_path.exists():
        return  # .env hasn't been created yet — nothing to patch

    existing_text = dotenv_path.read_text()
    existing_keys = {
        line.partition("=")[0].strip()
        for line in existing_text.splitlines()
        if line.strip() and not line.strip().startswith("#") and "=" in line
    }

    # Defaults keyed on env-var name.  Order matters for the appended block.
    defaults = [
        ("OBSIDIAN_VAULT_PATH", str(OBSIDIAN_VAULT_DIR)),
        ("OBSIDIAN_REST_URL",   "https://localhost:27124"),
        # Token intentionally left blank — user must paste it from the plugin.
        ("OBSIDIAN_REST_TOKEN", ""),
        ("OBSIDIAN_AUTO_SYNC_MINUTES", "15"),
        ("OBSIDIAN_EXPORT_DECISIONS",  "true"),
        ("OBSIDIAN_EXPORT_SIGNALS",    "true"),
        ("OBSIDIAN_EXPORT_COMMUNITIES","true"),
        ("OBSIDIAN_INJECT_CONTEXT",    "false"),
    ]

    missing = [(k, v) for k, v in defaults if k not in existing_keys]
    if not missing:
        return  # all keys already present

    lines = [
        "\n# ── Obsidian Knowledge Plugin ──────────────────────────────────────",
        "# Copy the API token from Obsidian → Settings → Local REST API,",
        "# then set OBSIDIAN_REST_TOKEN below.",
    ]
    for k, v in missing:
        lines.append(f"{k}={v}")

    with dotenv_path.open("a") as fh:
        fh.write("\n".join(lines) + "\n")

    added = [k for k, _ in missing]
    ok(f".env ← added Obsidian keys: {', '.join(added)}")
    if "OBSIDIAN_REST_TOKEN" in added:
        warn("OBSIDIAN_REST_TOKEN is blank — paste the token from the Obsidian Local REST API plugin settings")


def start_obsidian() -> bool:
    if not ensure_dir(OBSIDIAN_VAULT_DIR):
        return False

    # Inject missing Obsidian env vars before anything else.
    ensure_env_obsidian()

    # Never delete vault data. Only ensure setup scaffold exists.
    if not OBSIDIAN_SETUP_FILE.exists():
        OBSIDIAN_SETUP_FILE.write_text(
            "# Obsidian Vault\n\n"
            "This vault is managed by TradeBot start.py bootstrap.\n"
            "Data is persistent and never deleted by startup automation.\n"
        )

    r = subprocess.run(
        ["open", "-a", "Obsidian", str(OBSIDIAN_VAULT_DIR)],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        fail(f"Failed to start Obsidian: {r.stderr.strip()[:200]}")
        return False

    ok("Obsidian launched with TradeBot vault")
    return True


def ensure_headroom_installed() -> bool:
    if _headroom_bin():
        ok("Headroom is installed")
        return True

    warn("Headroom not found — attempting auto-install …")
    uv_bin = shutil.which("uv")
    if uv_bin:
        ok_inst, err = _spinner_run(
            [uv_bin, "tool", "install", "--python", "3.13", "headroom-ai[all]"],
            "uv tool install headroom-ai[all]",
            timeout=900,
        )
        if ok_inst and _headroom_bin():
            ok("Headroom installed via uv")
            return True
        warn(f"uv install failed: {err[:180]}")

    pipx_bin = shutil.which("pipx")
    if pipx_bin:
        ok_inst, err = _spinner_run(
            [pipx_bin, "install", "headroom-ai[all]"],
            "pipx install headroom-ai[all]",
            timeout=900,
        )
        if ok_inst and _headroom_bin():
            ok("Headroom installed via pipx")
            return True
        warn(f"pipx install failed: {err[:180]}")

    fail("Could not auto-install Headroom. Install manually with uv or pipx.")
    return False


def _headroom_launch_command() -> str:
    headroom_bin = _headroom_bin() or "headroom"
    return (
        'export PATH="/Users/sakhilematsimela/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"; '
        "unset HEADROOM_DEPLOYMENT_PROFILE HEADROOM_DEPLOYMENT_NAME HEADROOM_DEPLOYMENT_ID HEADROOM_DEPLOYMENT_MODE; "
        f'export OPENAI_TARGET_API_URL="http://127.0.0.1:{BACKEND_PORT}/api/v1/provider-relay"; '
        "export HEADROOM_COMPRESS_SYSTEM_MESSAGES=1; "
        "export HEADROOM_COMPRESS_USER_MESSAGES=1; "
        "export HEADROOM_MIN_TOKENS=1; "
        "export HEADROOM_TARGET_RATIO=0.4; "
        "export HEADROOM_FORCE_KOMPRESS=1; "
        "export HEADROOM_COMPRESSION_STABLE_AFTER_TURN=0; "
        "export HEADROOM_STALE_READ_COMPRESS_AFTER_TURNS=0; "
        "export HEADROOM_PROTECT_RECENT=0; "
        "export HEADROOM_PROTECT_ANALYSIS_CONTEXT=0; "
        "export HEADROOM_INTERCEPT_TOOL_RESULTS=1; "
        f'export HEADROOM_DATA_DIR="{HEADROOM_DATA_DIR}"; '
        f'export ANTHROPIC_API_KEY="{HEADROOM_PROXY_KEY}"; '
        f"exec {headroom_bin} proxy --port {HEADROOM_PORT}"
    )


def ensure_headroom_launchagent() -> bool:
    if not _on_macos():
        fail("Headroom LaunchAgent management is only implemented on macOS")
        return False

    if not ensure_dir(HEADROOM_PLIST.parent):
        return False
    if not ensure_dir(HEADROOM_LOG.parent):
        return False
    if not ensure_dir(HEADROOM_DATA_DIR):
        return False

    launch_cmd = _headroom_launch_command()
    plist_content = f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>ai.headroomlabs.headroom</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string>
    <string>-lc</string>
    <string>{launch_cmd}</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{HEADROOM_LOG}</string>
  <key>StandardErrorPath</key><string>{HEADROOM_LOG}</string>
</dict>
</plist>
'''

    existing = HEADROOM_PLIST.read_text() if HEADROOM_PLIST.exists() else ""
    if existing != plist_content:
        HEADROOM_PLIST.write_text(plist_content)
        ok("Headroom LaunchAgent configured")
    else:
        ok("Headroom LaunchAgent already configured")
    return True


def start_headroom() -> bool:
    if port_open("127.0.0.1", HEADROOM_PORT, 0.6):
        health = http_json(f"{HEADROOM_URL}/health?include_config=1", timeout=3)
        if health:
            ok(f"Headroom already running on :{HEADROOM_PORT}")
            return True

    if not ensure_headroom_launchagent():
        return False

    launch_id = f"gui/{os.getuid()}/ai.headroomlabs.headroom"
    subprocess.run(["launchctl", "unload", str(HEADROOM_PLIST)], capture_output=True)
    load_res = subprocess.run(["launchctl", "load", str(HEADROOM_PLIST)], capture_output=True, text=True)
    if load_res.returncode != 0:
        fail(f"Failed to load Headroom LaunchAgent: {load_res.stderr.strip()[:200]}")
        return False

    subprocess.run(["launchctl", "kickstart", "-k", launch_id], capture_output=True)

    if not wait_for_port("127.0.0.1", HEADROOM_PORT, "Headroom proxy", max_wait=40):
        return False

    health = http_json(f"{HEADROOM_URL}/health?include_config=1", timeout=5)
    if not health:
        fail("Headroom is listening but health endpoint failed")
        return False

    config = health.get("config", {})
    if not config.get("compress_user_messages"):
        fail("Headroom started but compress_user_messages is disabled")
        return False
    if not config.get("compress_system_messages"):
        fail("Headroom started but compress_system_messages is disabled")
        return False

    ok("Headroom healthy with compression enabled")
    return True


def setup_integrations() -> bool:
    """Install/configure/start integrations needed by TradeBot startup.

    Safety: This function never deletes Headroom or Obsidian data.
    """
    header("0/7  Integrations (Headroom + Obsidian)")

    # Headroom (macOS LaunchAgent + launchctl + os.getuid) and Obsidian
    # (/Applications, `brew --cask`, `open -a`) bootstrapping are macOS-only.
    # On Windows/Linux these are optional conveniences and must never block the
    # core DB/backend/frontend from starting.
    if not _on_macos():
        if port_open("127.0.0.1", HEADROOM_PORT, 0.6) and \
                http_json(f"{HEADROOM_URL}/health?include_config=1", timeout=3):
            ok(f"Headroom already running on :{HEADROOM_PORT}")
        else:
            warn("Headroom auto-start is macOS-only — skipping (optional).")
        warn("Obsidian auto-launch is macOS-only — skipping (optional).")
        return True

    if not ensure_headroom_installed():
        return False
    if not start_headroom():
        return False

    if not ensure_obsidian():
        return False
    if not start_obsidian():
        return False

    return True


# ── Auto-install helpers ─────────────────────────────────────────────────────

def _spinner_run(cmd: List[str], label: str, cwd=None,
                 timeout: int = 300) -> Tuple[bool, str]:
    """Run cmd, streaming a progress dot every 2s, return (success, stderr)."""
    print(f"  {C.CYAN}⟳{C.RESET}  {label} ", end="", flush=True)
    try:
        proc = subprocess.Popen(
            cmd, cwd=cwd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        import threading
        stop_flag = threading.Event()

        def ticker():
            while not stop_flag.wait(2):
                print(".", end="", flush=True)

        t = threading.Thread(target=ticker, daemon=True)
        t.start()
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        finally:
            stop_flag.set()
        if proc.returncode == 0:
            print(f" {C.GREEN}done{C.RESET}")
            return True, ""
        print(f" {C.RED}failed{C.RESET}")
        return False, (stderr or stdout).decode(errors="replace")[-300:]
    except subprocess.TimeoutExpired:
        proc.kill()
        print(f" {C.RED}timed out{C.RESET}")
        return False, "timed out"
    except FileNotFoundError:
        print(f" {C.RED}command not found{C.RESET}")
        return False, f"{cmd[0]!r} not found"


def _brew_path() -> Optional[str]:
    """Return the brew binary path if Homebrew is present, else None."""
    brew = shutil.which("brew")
    if brew and Path(brew).exists():
        return brew
    for bp in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew"):
        if Path(bp).exists():
            return bp
    return None


def _install_homebrew() -> bool:
    """
    Install Homebrew via the official, fully non-interactive install script.

    The previous implementation ran `/bin/bash -s` without ever feeding it the
    fetched script (a no-op) and didn't set NONINTERACTIVE, so it stalled or
    silently did nothing. This pipes the official installer straight into bash
    with NONINTERACTIVE=1 (no TTY prompts) and then puts brew on PATH.
    """
    # Already installed but just off PATH? Adopt it and succeed.
    existing = _brew_path()
    if existing:
        os.environ["PATH"] = str(Path(existing).parent) + os.pathsep + os.environ["PATH"]
        ok(f"Homebrew already installed ({existing})")
        return True

    info("Installing Homebrew (requires internet) …")
    script_url = "https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh"
    # NONINTERACTIVE=1 makes the installer skip the "press RETURN" prompt; it
    # still uses sudo for /opt/homebrew creation, which works when the invoking
    # user has admin rights (the common case on a personal Mac).
    cmd = ["/bin/bash", "-c",
           f'/usr/bin/curl -fsSL {script_url} | NONINTERACTIVE=1 /bin/bash']
    ok_inst, err = _spinner_run(cmd, "Installing Homebrew", timeout=900)

    if ok_inst or _brew_path():
        brew = _brew_path()
        if brew:
            os.environ["PATH"] = str(Path(brew).parent) + os.pathsep + os.environ["PATH"]
            ok("Homebrew installed")
            return True

    fail("Homebrew install failed — run this once manually, then re-run start.py:")
    fail('  /bin/bash -c "$(curl -fsSL '
         'https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"')
    if err:
        warn(f"  installer said: {err[:200]}")
    return False


def _brew(formula: str, label: str = "") -> bool:
    """brew install formula, return True on success."""
    brew = _brew_path() or "/opt/homebrew/bin/brew"
    tag = label or formula
    ok_inst, err = _spinner_run([brew, "install", formula], f"brew install {tag}")
    if not ok_inst:
        fail(f"  brew install {formula} failed: {err[:200]}")
    return ok_inst


def _npm_global(pkg: str) -> bool:
    """npm install -g pkg."""
    npm = _npm_cmd()
    ok_inst, err = _spinner_run([npm, "install", "-g", pkg], f"npm install -g {pkg}")
    if not ok_inst:
        fail(f"  npm install -g {pkg} failed: {err[:200]}")
    return ok_inst


# ── Pre-flight checks (detect + auto-install) ─────────────────────────────────

def _cmd_version(cmd: str) -> str:
    """Return first line of `cmd --version`, or empty string on failure."""
    # Resolve via shutil.which so Windows .cmd/.bat shims (npm, npx) are found —
    # subprocess only auto-appends ".exe", so bare ["npm", ...] would fail there.
    exe = shutil.which(cmd) or cmd
    try:
        r = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=5)
        return (r.stdout or r.stderr).splitlines()[0].strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def _check(label: str, ok_cond: bool, found_str: str = "", fix: str = "") -> bool:
    if ok_cond:
        suffix = f"  {C.YELLOW}({found_str}){C.RESET}" if found_str else ""
        print(f"  {C.GREEN}✓{C.RESET}  {label:<38}{suffix}")
    else:
        print(f"  {C.RED}✗{C.RESET}  {C.RED}{label}{C.RESET}")
        if fix:
            print(f"      {C.YELLOW}→ fix:{C.RESET} {fix}")
    return ok_cond


def _fixed(label: str) -> None:
    print(f"  {C.GREEN}✔{C.RESET}  {C.GREEN}{label} (auto-installed){C.RESET}")


def _ensure_vc_redist() -> Tuple[bool, bool]:
    """
    Ensure the Microsoft Visual C++ Redistributable (2015-2022, x64) is present.

    Compiled Python wheels (pydantic-core, asyncpg, numpy, torch, ccxt deps …)
    link against vcruntime140.dll / vcruntime140_1.dll, which ship with this
    redistributable. The 'pythoncore' nuget/embeddable Python build does NOT
    bundle it (unlike the python.org installer), so a fresh Windows box fails
    with "VCRUNTIME140.dll was not found" the moment the backend imports one of
    those packages.

    Returns (available, auto_installed).
    """
    if not IS_WINDOWS:
        return True, False

    import ctypes

    def _loaded() -> bool:
        # System32 is always on the DLL search path, so a successful load here
        # means the redistributable is installed for every child process too.
        for dll in ("vcruntime140.dll", "vcruntime140_1.dll"):
            try:
                ctypes.WinDLL(dll)
            except OSError:
                return False
        return True

    if _loaded():
        return True, False

    # winget ships with Windows 10 1709+/11 — use it for a silent install.
    winget = shutil.which("winget")
    if winget:
        warn("Visual C++ Redistributable missing — installing via winget …")
        _spinner_run(
            [winget, "install", "--id", "Microsoft.VCRedist.2015+.x64", "-e",
             "--accept-source-agreements", "--accept-package-agreements"],
            "winget install Microsoft Visual C++ Redistributable (x64)",
            timeout=300,
        )
        if _loaded():
            return True, True

    return False, False


def _winget_install(package_id: str, label: str) -> bool:
    """
    Silently install a package via winget (Windows 10 1709+/11). Returns True
    on success. Adds nothing and never raises on platforms without winget.
    """
    if not IS_WINDOWS:
        return False
    winget = shutil.which("winget")
    if not winget:
        return False
    ok_inst, err = _spinner_run(
        [winget, "install", "--id", package_id, "-e",
         "--accept-source-agreements", "--accept-package-agreements"],
        f"winget install {label}",
        timeout=600,
    )
    if not ok_inst:
        warn(f"  winget install {package_id} failed: {err[:160]}")
    return ok_inst


def preflight_check(mode: str) -> bool:
    """
    Detect every requirement, auto-install anything that's missing and
    installable, then return True only when all critical items are satisfied.
    """
    header("Pre-flight checks  (auto-install enabled)")
    sep()
    # Surface tools that are installed but hidden by a minimal launch PATH
    # BEFORE any detection runs, so we don't try to (re)install what's there.
    _augment_path()
    unfixable: List[str] = []   # critical, can't auto-fix
    fixed_items: List[str] = []  # were missing, successfully installed

    # ── Python (this process) ────────────────────────────────────────────────
    py_ver = sys.version.split()[0]
    py_tuple = tuple(int(x) for x in py_ver.split(".")[:2])
    py_ok = py_tuple >= (3, 9)
    if not _check("Python ≥ 3.9", py_ok, f"python {py_ver}"):
        fail("  Python 3.9+ is required to run this script.")
        fail("  Install: brew install python@3.11  then re-run with python3.11 start.py")
        unfixable.append("Python < 3.9 (current runner)")
    elif py_tuple >= (3, 14):
        # The dependency lockfile is frozen against 3.13; some exact pins have no
        # 3.14 wheel yet. The installer falls back to resolved versions, but 3.13
        # is the smoothest path if pip still fails.
        warn(f"Python {py_ver} is very new — some pinned wheels may be missing. "
             "If pip fails below, install Python 3.13 and re-run.")

    # ── Visual C++ runtime (Windows only) ────────────────────────────────────
    # Compiled wheels (pydantic-core, asyncpg, numpy …) need vcruntime140.dll.
    # The nuget/embeddable Python build doesn't ship it, so detect + auto-install
    # the redistributable BEFORE the backend subprocess tries to import them.
    if IS_WINDOWS:
        vc_ok, vc_fixed = _ensure_vc_redist()
        if vc_fixed:
            fixed_items.append("Microsoft Visual C++ Redistributable")
        _check("Visual C++ runtime (vcruntime140.dll)", vc_ok,
               "installed" if vc_ok else "",
               "install https://aka.ms/vs/17/release/vc_redist.x64.exe then re-run")
        if not vc_ok:
            unfixable.append(
                "Microsoft Visual C++ Redistributable missing (vcruntime140.dll) — "
                "install https://aka.ms/vs/17/release/vc_redist.x64.exe"
            )

    # ── Homebrew ─────────────────────────────────────────────────────────────
    # Homebrew is only used to install/run the brew-mode postgres/redis and to
    # auto-install node on macOS. It does not exist on Windows/Linux, so treat
    # it as a hard requirement ONLY when the user is actually in brew mode.
    brew_ok = bool(shutil.which("brew") or Path("/opt/homebrew/bin/brew").exists())
    if not brew_ok:
        if sys.platform == "darwin":
            warn("Homebrew not found — attempting auto-install …")
            brew_ok = _install_homebrew()
            if brew_ok:
                fixed_items.append("Homebrew")
            elif mode == "brew":
                unfixable.append("Homebrew (required for brew-mode deps)")
        elif mode == "brew":
            _check("Homebrew (brew)", False, "", "only available on macOS — use --docker instead")
            unfixable.append("Homebrew (brew mode requires macOS)")
        else:
            _check("Homebrew (brew)", True, "not needed in docker mode")
    else:
        _check("Homebrew (brew)", True, shutil.which("brew") or "/opt/homebrew/bin/brew")

    # ── Node.js / npm / npx ──────────────────────────────────────────────────
    node_ver = _cmd_version("node")
    node_ok  = bool(node_ver)
    if not node_ok and brew_ok:
        warn("Node.js not found — auto-installing via Homebrew …")
        if _brew("node", "Node.js"):
            node_ver = _cmd_version("node")
            node_ok  = bool(node_ver)
            if node_ok:
                fixed_items.append("Node.js")
    if not node_ok and IS_WINDOWS:
        warn("Node.js not found — auto-installing via winget …")
        if _winget_install("OpenJS.NodeJS.LTS", "Node.js LTS"):
            # winget won't refresh this process's PATH — add the default install
            # dir so node/npm/npx are visible without a shell restart.
            node_dir = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "nodejs"
            if node_dir.is_dir():
                os.environ["PATH"] = str(node_dir) + os.pathsep + os.environ.get("PATH", "")
            node_ver = _cmd_version("node")
            node_ok  = bool(node_ver)
            if node_ok:
                fixed_items.append("Node.js")
    _check("Node.js", node_ok, node_ver,
           "install https://nodejs.org/en/download (or: winget install OpenJS.NodeJS.LTS)"
           if IS_WINDOWS else "brew install node  (or nvm install --lts)")
    if not node_ok:
        unfixable.append("Node.js not found")

    npm_ver = _cmd_version("npm")
    npm_ok  = bool(npm_ver)
    _check("npm", npm_ok, npm_ver, "reinstall node (npm comes bundled)")
    if not npm_ok:
        unfixable.append("npm not found")

    npx_ok = bool(shutil.which("npx"))
    if not npx_ok and npm_ok and not IS_WINDOWS:
        warn("npx not found — installing globally …")
        if _npm_global("npx"):
            npx_ok = bool(shutil.which("npx"))
            if npx_ok:
                fixed_items.append("npx")
    _check("npx", npx_ok, "", "npx ships with npm — reinstall Node.js")
    if not npx_ok:
        unfixable.append("npx not found")

    git_ver = _cmd_version("git")
    if not git_ver and brew_ok:
        warn("git not found — auto-installing via Homebrew …")
        if _brew("git"):
            git_ver = _cmd_version("git")
            if git_ver:
                fixed_items.append("git")
    _check("git", bool(git_ver), git_ver, "brew install git")  # advisory

    # ── DB-mode tools ─────────────────────────────────────────────────────────
    sep()
    print(f"  {C.BOLD}Database  [{mode.upper()} mode]{C.RESET}")
    sep()

    if mode == "brew":
        pg_bin_ok = (PG_BIN / "pg_isready").exists()
        if not pg_bin_ok and brew_ok:
            warn("postgresql@16 not found — auto-installing via Homebrew …")
            if _brew("postgresql@16", "PostgreSQL 16"):
                pg_bin_ok = (PG_BIN / "pg_isready").exists()
                if pg_bin_ok:
                    fixed_items.append("postgresql@16")
        _check("postgresql@16  (brew formula)", pg_bin_ok, str(PG_BIN),
               "brew install postgresql@16")
        if not pg_bin_ok:
            unfixable.append("postgresql@16 not installed")

        redis_ok = _redis_installed()
        if not redis_ok and brew_ok:
            warn("redis not found — auto-installing via Homebrew …")
            if _brew("redis", "Redis"):
                redis_ok = _redis_installed()
                if redis_ok:
                    fixed_items.append("redis")
        _check("redis  (brew formula)", redis_ok, "", "brew install redis")
        if not redis_ok:
            unfixable.append("redis not installed")

    else:  # docker mode
        docker_cli_ok = bool(shutil.which("docker"))
        _check("Docker CLI", docker_cli_ok, _cmd_version("docker"),
               "install Docker Desktop: winget install Docker.DockerDesktop"
               if IS_WINDOWS else
               "https://www.docker.com/products/docker-desktop  (manual install)")
        if not docker_cli_ok:
            unfixable.append("Docker CLI not found (install Docker Desktop)")

        if docker_cli_ok:
            # Docker Desktop on Windows uses WSL2/HyperV which can take
            # 20-30 s to respond on first call; use a longer timeout there.
            _docker_info_timeout = 30 if IS_WINDOWS else 5
            r = subprocess.run(["docker", "info"], capture_output=True,
                               timeout=_docker_info_timeout)
            daemon_ok = r.returncode == 0
            if not daemon_ok:
                info("Docker daemon not running — attempting to start Docker Desktop …")
                # Launching Docker Desktop is platform-specific; wrap each call
                # so a missing launcher (e.g. `open` on Windows) can't crash the
                # whole preflight with WinError 2 / FileNotFoundError.
                try:
                    if sys.platform == "darwin":
                        subprocess.run(["open", "-a", "Docker"], capture_output=True)
                    elif IS_WINDOWS:
                        docker_desktop = Path(
                            os.environ.get("ProgramFiles", r"C:\Program Files")
                        ) / "Docker" / "Docker" / "Docker Desktop.exe"
                        if docker_desktop.exists():
                            subprocess.Popen([str(docker_desktop)])
                    # On Linux the daemon is usually a systemd service; leave it
                    # to the user to start (no reliable cross-distro launcher).
                except (OSError, subprocess.SubprocessError):
                    pass
                for _ in range(30):
                    time.sleep(2)
                    if subprocess.run(["docker", "info"],
                                      capture_output=True,
                                      timeout=10 if IS_WINDOWS else 3).returncode == 0:
                        daemon_ok = True
                        fixed_items.append("Docker daemon")
                        break
                    print("  .", end="", flush=True)
                print()
            _check("Docker daemon", daemon_ok, "",
                   "open Docker Desktop and wait for it to start")
            if not daemon_ok:
                unfixable.append("Docker daemon not running")

            r2 = subprocess.run(["docker", "compose", "version"],
                                capture_output=True, text=True,
                                timeout=30 if IS_WINDOWS else 5)
            compose_ok = r2.returncode == 0
            ver2 = r2.stdout.strip().splitlines()[0] if compose_ok else ""
            _check("docker compose (v2 plugin)", compose_ok, ver2,
                   "upgrade Docker Desktop to ≥ 4.x")
            if not compose_ok:
                unfixable.append("docker compose v2 not available")

            dc_ok = (ROOT / "docker-compose.yml").exists()
            _check("docker-compose.yml found", dc_ok, "")

    # ── Project structure ─────────────────────────────────────────────────────
    sep()
    print(f"  {C.BOLD}Project files{C.RESET}")
    sep()

    backend_main_ok = (BACKEND_DIR / "app" / "main.py").exists()
    if not _check("backend/app/main.py", backend_main_ok,
                  "", f"re-clone the repo into {ROOT}"):
        unfixable.append("backend/app/main.py missing — repo may be incomplete")

    reqs_ok = (BACKEND_DIR / "requirements.txt").exists()
    if not _check("backend/requirements.txt", reqs_ok):
        unfixable.append("backend/requirements.txt missing")

    pkg_ok = (FRONTEND_DIR / "package.json").exists()
    if not _check("frontend/package.json", pkg_ok):
        unfixable.append("frontend/package.json missing")

    env_ok = (ROOT / ".env").exists()
    if not env_ok:
        # Auto-generate .env from .env.example if it exists
        env_ex = ROOT / ".env.example"
        if env_ex.exists():
            import shutil as _sh
            _sh.copy(env_ex, ROOT / ".env")
            env_ok = True
            fixed_items.append(".env (copied from .env.example)")
    _check(".env file", env_ok,
           str(ROOT / ".env") if env_ok else "not found — create from .env.example")

    # ── Python venv + packages ────────────────────────────────────────────────
    sep()
    print(f"  {C.BOLD}Python environment{C.RESET}")
    sep()

    # Compiled wheels only cover CPython 3.11–3.13, so make sure we build the
    # venv with a supported interpreter (auto-installing Python 3.13 on Windows
    # if the box only has 3.14+). If a previous run already created a 3.14 venv,
    # recreate it — otherwise pandas/numpy/pydantic-core fail to build.
    supported_py = _ensure_supported_python()

    venv_exists = VENV.exists() and PY_BIN.exists()
    if venv_exists and supported_py:
        venv_ver = _python_version(str(PY_BIN))
        if venv_ver and venv_ver > _MAX_SUPPORTED_PY:
            warn(f"Existing venv uses Python {venv_ver[0]}.{venv_ver[1]} — "
                 "unsupported for prebuilt wheels; recreating with a supported "
                 "interpreter …")
            try:
                shutil.rmtree(VENV)
            except OSError as e:
                warn(f"  could not remove old venv: {e}")
            venv_exists = VENV.exists() and PY_BIN.exists()

    if not venv_exists:
        info("Python venv not found — creating now …")
        py = supported_py or _best_python()
        if not supported_py:
            pv = _python_version(py)
            if pv and pv > _MAX_SUPPORTED_PY:
                warn(f"No Python 3.11–3.13 available — creating venv with "
                     f"{pv[0]}.{pv[1]}; some dependencies may fail to build. "
                     "Install Python 3.13 for a clean setup.")
        ok_v, err_v = _spinner_run([py, "-m", "venv", str(VENV)],
                                   "Creating Python venv")
        if ok_v:
            venv_exists = True
            fixed_items.append("Python venv")
        else:
            fail(f"  venv creation failed: {err_v}")
            unfixable.append("Python venv could not be created")
    _check("Python venv", venv_exists,
           str(VENV) if venv_exists else "failed to create")

    if venv_exists:
        core_pkgs = [
            ("fastapi", "fastapi"), ("uvicorn", "uvicorn"),
            ("sqlalchemy", "sqlalchemy"), ("asyncpg", "asyncpg"),
            ("redis", "redis"), ("pydantic", "pydantic"),
            ("httpx", "httpx"), ("loguru", "loguru"),
        ]
        missing_pkgs = [
            name for imp, name in core_pkgs
            if subprocess.run([str(PY_BIN), "-c", f"import {imp}"],
                              capture_output=True, timeout=5).returncode != 0
        ]
        if missing_pkgs:
            info(f"Missing pip packages: {', '.join(missing_pkgs)} — installing …")
            pip = PIP_BIN
            reqs_file = BACKEND_DIR / "requirements.txt"
            lock_file = BACKEND_DIR / "requirements-lock.txt"
            # Upgrade pip itself first — old pip versions fail on binary wheels.
            _spinner_run([str(pip), "install", "--quiet", "--upgrade", "pip"],
                         "Upgrading pip", timeout=60)
            # Prefer the fully-pinned lockfile: --no-deps skips resolution entirely,
            # avoiding the "dependency graph too complex" error from ccxt/openai
            # trees. The lockfile is frozen against Python 3.13, though — on newer
            # runtimes (e.g. 3.14) some exact pins have no wheel (e.g.
            # litellm==1.89.3), so fall back to resolving requirements.txt, which
            # lets pip choose versions compatible with the current interpreter.
            ok_p, err_p = False, ""
            if lock_file.exists():
                ok_p, err_p = _spinner_run(
                    [str(pip), "install", "--prefer-binary", "--no-deps",
                     "-r", str(lock_file)],
                    "pip install -r requirements-lock.txt",
                    cwd=BACKEND_DIR, timeout=360
                )
                if not ok_p:
                    warn("Lockfile install failed (often a newer-Python wheel gap) "
                         "— retrying with resolved requirements.txt …")
            if not ok_p:
                ok_p, err_p = _spinner_run(
                    [str(pip), "install", "--prefer-binary", "--upgrade",
                     "-r", str(reqs_file)],
                    "pip install -r requirements.txt",
                    cwd=BACKEND_DIR, timeout=600
                )
            if ok_p:
                # Re-check every package individually
                still_missing = [
                    name for imp, name in core_pkgs
                    if subprocess.run([str(PY_BIN), "-c", f"import {imp}"],
                                      capture_output=True, timeout=5).returncode != 0
                ]
                if still_missing:
                    _check(f"Python packages ({', '.join(still_missing)})", False)
                    unfixable.append(f"pip install incomplete: {', '.join(still_missing)}")
                else:
                    _check("Python packages (fastapi, uvicorn, sqlalchemy …)", True,
                           "installed by auto-setup")
                    fixed_items.append("pip packages")
                    # tradingagents is optional; install without deps so its
                    # broken chainlit/Python-3.12 requirements don't block us.
                    _spinner_run(
                        [str(pip), "install", "--quiet", "--no-deps",
                         "--upgrade", "tradingagents==0.6.0"],
                        "tradingagents (no-deps)", timeout=60
                    )
            else:
                _check("Python packages", False, "",
                       f"pip install failed: {err_p[:200]}")
                fail(f"pip stderr:\n{err_p[:400]}")
                unfixable.append("pip install failed")
        else:
            _check("Python packages (fastapi, uvicorn, sqlalchemy …)", True,
                   "all core packages present")

    # ── npm / Node modules ────────────────────────────────────────────────────
    sep()
    print(f"  {C.BOLD}Frontend dependencies{C.RESET}")
    sep()

    nm = FRONTEND_DIR / "node_modules"
    critical_nm = ["next", "react", "react-dom"]
    nm_ok = nm.exists() and all((nm / d).exists() for d in critical_nm)
    if not nm_ok and npm_ok:
        info("node_modules missing or incomplete — running npm install …")
        ok_nm, err_nm = _spinner_run(
            [_npm_cmd(), "install", "--legacy-peer-deps"],
            "npm install", cwd=FRONTEND_DIR, timeout=300
        )
        if ok_nm:
            nm_ok = all((nm / d).exists() for d in critical_nm)
            if nm_ok:
                fixed_items.append("npm packages")
            else:
                _check("node_modules (critical packages)", False)
                unfixable.append("npm install ran but critical packages still missing")
        else:
            fail(f"  npm install failed: {err_nm[:200]}")
            unfixable.append("npm install failed")
    if nm_ok:
        _check("node_modules (next, react, react-dom)", True, "installed")
    elif not npm_ok:
        _check("node_modules", False, "", "install Node.js first")

    # ── MT5 REST bridge ───────────────────────────────────────────────────────
    sep()
    print(f"  {C.BOLD}MT5 REST bridge (mtapi-io){C.RESET}")
    sep()

    mt5_port_val = _mt5_port()
    mt5_up = port_open("localhost", mt5_port_val, 0.5)
    if mt5_up:
        _check(f"MT5 REST running  (:{mt5_port_val})", True, MT5_API_URL)
    else:
        docker_avail = bool(shutil.which("docker"))
        daemon_up = docker_avail and \
            subprocess.run(["docker", "info"], capture_output=True, timeout=5).returncode == 0
        if daemon_up:
            r = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.Status}}", MT5_CONTAINER],
                capture_output=True, text=True, timeout=5
            )
            state = r.stdout.strip() if r.returncode == 0 else "not_found"
            if state in ("exited", "created"):
                info(f"MT5 container '{MT5_CONTAINER}' stopped — starting it now …")
                subprocess.run(["docker", "start", MT5_CONTAINER],
                               capture_output=True, timeout=20)
                time.sleep(3)
                mt5_up = port_open("localhost", mt5_port_val, 1.0)
            elif state == "not_found":
                info(f"MT5 container '{MT5_CONTAINER}' not found — pulling & creating …")
                subprocess.run(["docker", "pull", MT5_IMAGE],
                               capture_output=True, timeout=120)
                subprocess.run([
                    "docker", "run", "-d", "--name", MT5_CONTAINER,
                    "--restart", "always", "-p", f"{mt5_port_val}:80", MT5_IMAGE,
                ], capture_output=True, timeout=20)
                time.sleep(5)
                mt5_up = port_open("localhost", mt5_port_val, 2.0)
            if mt5_up:
                fixed_items.append("MT5 REST container")
            _check(f"MT5 REST (:{mt5_port_val})", mt5_up,
                   "running" if mt5_up else "failed to start — accounts will be offline")
        else:
            warn(f"MT5 REST :{mt5_port_val} not running  (Docker unavailable)")
            warn("  Install Docker Desktop to enable live MT5 account sync")
        # MT5 is optional — not added to unfixable

    # ── Summary ───────────────────────────────────────────────────────────────
    sep()
    if fixed_items:
        print(f"  {C.GREEN}{C.BOLD}Auto-installed / fixed:{C.RESET}")
        for item in fixed_items:
            print(f"    {C.GREEN}+{C.RESET}  {item}")
        sep()
    if unfixable:
        print(f"  {C.RED}{C.BOLD}Cannot start — {len(unfixable)} unresolved issue(s):{C.RESET}")
        for iss in unfixable:
            print(f"    {C.RED}•{C.RESET} {iss}")
        print()
        return False
    print(f"  {C.GREEN}{C.BOLD}Pre-flight passed — all requirements met{C.RESET}")
    print()
    return True


# ── MT5 REST (mtapi-io Docker container) ─────────────────────────────────────

def _mt5_port() -> int:
    """Extract port from MT5_API_URL (e.g. http://localhost:8092 → 8092)."""
    try:
        from urllib.parse import urlparse
        parsed = urlparse(MT5_API_URL)
        return parsed.port or 8092
    except Exception:
        return 8092


def _docker_available() -> bool:
    return bool(shutil.which("docker")) and \
           subprocess.run(["docker", "info"], capture_output=True, timeout=5).returncode == 0


def _container_running(name: str) -> bool:
    r = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Running}}", name],
        capture_output=True, text=True, timeout=5
    )
    return r.stdout.strip() == "true"


def _container_exists(name: str) -> bool:
    r = subprocess.run(
        ["docker", "inspect", "--format", "{{.Name}}", name],
        capture_output=True, text=True, timeout=5
    )
    return r.returncode == 0


def start_mt5rest() -> bool:
    """
    Ensure the mtapi-io MT5 REST bridge container is running.
    Returns True when the service is healthy on the configured port.
    """
    port = _mt5_port()

    # 1. Already responding on port?
    if port_open("localhost", port, 0.5):
        ok(f"MT5 REST already running on :{port}  ({MT5_API_URL})")
        return True

    if not _docker_available():
        fail("Docker not available — cannot start MT5 REST container")
        warn("Install Docker Desktop and re-run, or start mt5rest manually")
        return False

    # 2. Container exists but stopped — restart it
    if _container_exists(MT5_CONTAINER):
        info(f"Restarting stopped container '{MT5_CONTAINER}' …")
        r = subprocess.run(["docker", "start", MT5_CONTAINER],
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            fail(f"docker start {MT5_CONTAINER} failed: {r.stderr.strip()[:200]}")
            return False
    else:
        # 3. No container at all — create and start fresh
        info(f"Pulling {MT5_IMAGE} and creating container '{MT5_CONTAINER}' …")
        # Pull first (silent)
        subprocess.run(["docker", "pull", MT5_IMAGE],
                       capture_output=True, timeout=120)
        r = subprocess.run([
            "docker", "run", "-d",
            "--name", MT5_CONTAINER,
            "--restart", "always",
            "-p", f"{port}:80",
            MT5_IMAGE,
        ], capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            fail(f"docker run failed: {r.stderr.strip()[:300]}")
            return False

    return wait_for_port("localhost", port, "MT5 REST (mtapi-io)", max_wait=30)


# ── DB mode ───────────────────────────────────────────────────────────────────
def detect_mode(forced: Optional[str]) -> str:
    # Brew mode (Homebrew postgres/redis) only works on macOS. On Windows/Linux
    # coerce any brew request — forced flag, saved .db-mode, or auto-detect — to
    # docker so a stale/committed "brew" mode can't wedge the whole preflight.
    def _coerce(m: str) -> str:
        if m == "brew" and sys.platform != "darwin":
            warn("Brew mode is macOS-only — switching to Docker mode.")
            return "docker"
        return m

    if forced:
        return _coerce(forced)
    if MODE_FILE.exists():
        saved = MODE_FILE.read_text().strip()
        warn(f"Using last saved DB mode: {C.BOLD}{saved}{C.RESET}")
        return _coerce(saved)
    # Auto-detect: prefer brew if pg_isready binary exists
    if (PG_BIN / "pg_isready").exists():
        return "brew"
    if shutil.which("docker"):
        return "docker"
    # Brew mode is macOS-only; on Windows/Linux default to docker so preflight
    # gives actionable "install Docker Desktop" guidance instead of brew errors.
    return "brew" if sys.platform == "darwin" else "docker"


def save_mode(mode: str) -> None:
    MODE_FILE.write_text(mode)


# ── PostgreSQL ────────────────────────────────────────────────────────────────
def _pg_data_dir() -> Path:
    """Homebrew's data directory for postgresql@16."""
    return Path("/opt/homebrew/var/postgresql@16")


def _clear_stale_pg_pid() -> None:
    """
    Remove a stale postmaster.pid.

    If PostgreSQL was killed uncleanly (crash, hard reboot, `kill -9`), the
    lock file `postmaster.pid` is left behind pointing at a dead PID. brew's
    launchd job then loops with 'lock file already exists' and never starts.
    We detect that the referenced PID is not alive and remove the file.
    """
    pid_file = _pg_data_dir() / "postmaster.pid"
    if not pid_file.exists():
        return
    try:
        first_line = pid_file.read_text().splitlines()[0].strip()
        pid = int(first_line)
    except Exception:
        # Unparseable → treat as stale
        pid = None
    alive = False
    if pid:
        try:
            os.kill(pid, 0)   # signal 0 = existence check
            alive = True
        except OSError:
            alive = False
    if not alive:
        try:
            pid_file.unlink()
            warn("Removed stale PostgreSQL lock file (postmaster.pid)")
        except Exception:
            pass


def _ensure_postgres_installed() -> bool:
    """Install postgresql@16 via Homebrew if it's not present."""
    if (PG_BIN / "postgres").exists() or shutil.which("postgres"):
        return True
    brew = _brew_path()
    if not brew:
        fail("Homebrew not found; cannot auto-install PostgreSQL.")
        return False
    warn("postgresql@16 not found — installing via Homebrew …")
    ok_i, err_i = _spinner_run([brew, "install", "postgresql@16"],
                               "brew install postgresql@16", timeout=600)
    if not ok_i:
        fail(f"Failed to install postgresql@16: {err_i[:200]}")
        return False
    ok("postgresql@16 installed")
    return True


def _ensure_pg_port(pg_port: int) -> None:
    """Make sure postgresql.conf listens on the expected port."""
    conf = _pg_data_dir() / "postgresql.conf"
    if not conf.exists():
        return
    try:
        text = conf.read_text()
        import re as _re
        if _re.search(rf"^\s*port\s*=\s*{pg_port}\b", text, _re.MULTILINE):
            return  # already correct
        # Replace an existing (commented or active) port line, else append.
        new, n = _re.subn(r"^\s*#?\s*port\s*=.*$", f"port = {pg_port}",
                          text, count=1, flags=_re.MULTILINE)
        if n == 0:
            new = text.rstrip() + f"\nport = {pg_port}\n"
        conf.write_text(new)
        warn(f"Set PostgreSQL port to {pg_port} in postgresql.conf")
    except Exception:
        pass


def _ensure_pg_role_and_db(pg_port: int) -> None:
    """
    Create the tradebot role + database if they don't exist.

    A fresh Homebrew PostgreSQL install only has a superuser role named after the
    current macOS user — it has no `tradebot` role and no `tradebot` database. The
    backend's DATABASE_URL authenticates as tradebot/tradebot_password against a
    `tradebot` DB, so without this the backend can never connect and start.py
    times out waiting for the API port to bind.

    We connect to the default `postgres` database as the OS superuser (peer/trust
    auth on a local Homebrew install) and idempotently create the role + DB.
    """
    psql = PG_BIN / "psql"
    if not psql.exists():
        alt = shutil.which("psql")
        if not alt:
            return
        psql = Path(alt)

    def _psql(sql: str, db: str = "postgres") -> subprocess.CompletedProcess:
        return run([str(psql), "-h", "localhost", "-p", str(pg_port),
                    "-d", db, "-tAc", sql])

    # 1) Role
    r = _psql("SELECT 1 FROM pg_roles WHERE rolname='tradebot'")
    if r.returncode == 0 and "1" not in r.stdout:
        cr = _psql("CREATE ROLE tradebot LOGIN PASSWORD 'tradebot_password' CREATEDB")
        if cr.returncode == 0:
            ok("Created PostgreSQL role 'tradebot'")
        else:
            warn(f"Could not create role 'tradebot': {cr.stderr.strip()[:160]}")

    # 2) Database (owned by tradebot)
    r = _psql("SELECT 1 FROM pg_database WHERE datname='tradebot'")
    if r.returncode == 0 and "1" not in r.stdout:
        cd = _psql("CREATE DATABASE tradebot OWNER tradebot")
        if cd.returncode == 0:
            ok("Created PostgreSQL database 'tradebot'")
        else:
            warn(f"Could not create database 'tradebot': {cd.stderr.strip()[:160]}")

    # 3) Make sure the password is set (in case the role predates this logic)
    _psql("ALTER ROLE tradebot LOGIN PASSWORD 'tradebot_password'")


def start_postgres_brew() -> Tuple[bool, int]:
    pg_port = 5434
    info("Starting PostgreSQL (Homebrew) …")
    if port_open("localhost", pg_port, 0.5):
        ok(f"PostgreSQL already running on :{pg_port}")
        _ensure_pg_role_and_db(pg_port)
        return True, pg_port

    # Clean-install robustness: install if missing, fix port, clear stale lock.
    if not _ensure_postgres_installed():
        return False, pg_port
    _ensure_pg_port(pg_port)
    _clear_stale_pg_pid()

    run(["brew", "services", "start", "postgresql@16"])
    if wait_for_port("localhost", pg_port, "PostgreSQL", max_wait=30):
        _ensure_pg_role_and_db(pg_port)
        return True, pg_port

    # First attempt failed — likely a stale lock or errored service. Clear the
    # lock, restart the service, and wait again before giving up.
    warn("PostgreSQL slow to start — clearing lock and restarting …")
    run(["brew", "services", "stop", "postgresql@16"])
    time.sleep(2)
    _clear_stale_pg_pid()
    run(["brew", "services", "start", "postgresql@16"])
    if wait_for_port("localhost", pg_port, "PostgreSQL", max_wait=45):
        _ensure_pg_role_and_db(pg_port)
        return True, pg_port
    return False, pg_port


def start_postgres_docker() -> Tuple[bool, int]:
    pg_port = 5433
    info("Starting PostgreSQL (Docker) …")
    if port_open("localhost", pg_port, 0.5):
        ok(f"PostgreSQL already running on :{pg_port}")
        return True, pg_port
    run(["docker", "compose", "up", "-d", "postgres"], cwd=ROOT)
    return wait_for_port("localhost", pg_port, "PostgreSQL"), pg_port


# ── Redis ─────────────────────────────────────────────────────────────────────
def _redis_installed() -> bool:
    """True if a Redis server is installed (on PATH or as a Homebrew formula)."""
    if shutil.which("redis-server") or shutil.which("redis-cli"):
        return True
    brew = _brew_path()
    if brew:
        # A brew keg that isn't linked onto PATH still counts as installed.
        return run([brew, "list", "redis"]).returncode == 0
    return False


def _redis_ping(port: int) -> bool:
    """True if Redis answers PING on the given port (real health, not just an open socket)."""
    cli = shutil.which("redis-cli")
    if not cli:
        # Fall back to a raw RESP PING if the CLI isn't on PATH.
        try:
            with socket.create_connection(("localhost", port), timeout=1.0) as s:
                s.sendall(b"PING\r\n")
                return b"PONG" in s.recv(64).upper()
        except OSError:
            return False
    try:
        r = run([cli, "-p", str(port), "ping"])
        return r.returncode == 0 and "PONG" in (r.stdout or "").upper()
    except Exception:
        return False


def ensure_redis_installed() -> bool:
    """Ensure a Redis server is installed; auto-install via Homebrew when missing."""
    if _redis_installed():
        return True
    brew = _brew_path()
    if not brew:
        warn("Redis not installed and Homebrew unavailable — install it manually (brew install redis)")
        return False
    warn("Redis not found — installing via Homebrew …")
    if _brew("redis", "Redis"):
        ok("Redis installed")
        return _redis_installed()
    return False


def start_redis_brew() -> Tuple[bool, int]:
    redis_port = 6379
    info("Starting Redis (Homebrew) …")

    # Already up AND answering PING → nothing to do.
    if _redis_ping(redis_port):
        ok(f"Redis already running and healthy on :{redis_port}")
        return True, redis_port

    # Install the server if it isn't present at all.
    if not ensure_redis_installed():
        warn("Redis could not be installed — backend falls back to in-memory SSE fan-out")
        return False, redis_port

    # Start (and register) the brew service so it persists across reboots.
    brew = _brew_path() or "brew"
    run([brew, "services", "start", "redis"])

    if not wait_for_port("localhost", redis_port, "Redis"):
        # A bare `redis-server` fallback for environments without brew services.
        server = shutil.which("redis-server")
        if server:
            info("brew services unavailable — launching redis-server directly …")
            subprocess.Popen(
                [server, "--port", str(redis_port), "--daemonize", "yes"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            wait_for_port("localhost", redis_port, "Redis")

    # Verify it truly answers PING, not just that the socket is open.
    if _redis_ping(redis_port):
        ok("Redis PING → PONG (configured & healthy)")
        return True, redis_port
    if port_open("localhost", redis_port, 0.5):
        warn("Redis port open but not answering PING yet — continuing")
        return True, redis_port
    return False, redis_port


def start_redis_docker() -> Tuple[bool, int]:
    redis_port = 6380
    info("Starting Redis (Docker) …")
    if _redis_ping(redis_port):
        ok(f"Redis already running and healthy on :{redis_port}")
        return True, redis_port
    run(["docker", "compose", "up", "-d", "redis"], cwd=ROOT)
    if not wait_for_port("localhost", redis_port, "Redis"):
        return False, redis_port
    if _redis_ping(redis_port):
        ok("Redis PING → PONG (configured & healthy)")
    return True, redis_port


# ── Python venv ───────────────────────────────────────────────────────────────
# Compiled dependency wheels (pandas, numpy, pydantic-core, asyncpg …) are only
# published for CPython 3.11–3.13. On Python 3.14+ pip can't find wheels and
# falls back to source builds that fail ("metadata-generation-failed"). The venv
# must therefore be built with an interpreter in this supported window.
_MIN_SUPPORTED_PY = (3, 11)
_MAX_SUPPORTED_PY = (3, 13)


def _python_version(exe: str) -> Optional[Tuple[int, int]]:
    """Return (major, minor) for a python executable, or None if it won't run."""
    try:
        r = subprocess.run(
            [exe, "-c", "import sys;print('%d.%d' % sys.version_info[:2])"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0 and r.stdout.strip():
            major, minor = r.stdout.strip().split(".")[:2]
            return (int(major), int(minor))
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return None


def _is_supported_py(ver: Optional[Tuple[int, int]]) -> bool:
    return bool(ver) and _MIN_SUPPORTED_PY <= ver <= _MAX_SUPPORTED_PY


def _find_supported_python() -> Optional[str]:
    """
    Locate an installed interpreter in [3.11, 3.13], best-first (3.13 → 3.11).
    Returns the executable path, or None if none is installed.
    """
    # 1) Versioned commands on PATH (POSIX, and some Windows installs).
    for name in ("python3.13", "python3.12", "python3.11"):
        p = shutil.which(name)
        if p and _is_supported_py(_python_version(p)):
            return p
    # 2) The interpreter running this script, if it's already supported.
    if _is_supported_py(_python_version(sys.executable)):
        return sys.executable
    if IS_WINDOWS:
        # 3) The `py` launcher — resolve the REAL exe path so the returned value
        #    stays a single token (no "py -3.13" string with an embedded space).
        launcher = shutil.which("py")
        if launcher:
            for flag in ("-3.13", "-3.12", "-3.11"):
                try:
                    r = subprocess.run(
                        [launcher, flag, "-c", "import sys;print(sys.executable)"],
                        capture_output=True, text=True, timeout=10,
                    )
                    exe = r.stdout.strip()
                    if r.returncode == 0 and exe and Path(exe).exists():
                        return exe
                except (OSError, subprocess.SubprocessError):
                    pass
        # 4) Standard per-user / system install locations.
        bases = [
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Python",
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")),
            Path("C:/"),
        ]
        for base in bases:
            for ver in ("313", "312", "311"):
                cand = base / f"Python{ver}" / "python.exe"
                if cand.exists():
                    return str(cand)
    else:
        for cand in ("/opt/homebrew/bin/python3.13", "/opt/homebrew/bin/python3.12",
                     "/opt/homebrew/bin/python3.11", "/usr/local/bin/python3.13",
                     "/usr/local/bin/python3.12", "/usr/local/bin/python3.11"):
            if Path(cand).exists():
                return cand
    return None


def _ensure_supported_python() -> Optional[str]:
    """
    Return a path to a supported interpreter (3.11–3.13), auto-installing
    Python 3.13 via winget on Windows if none is present. Returns None when no
    supported interpreter is available and one couldn't be installed.
    """
    found = _find_supported_python()
    if found:
        return found
    if IS_WINDOWS:
        warn("No Python 3.11–3.13 found (compiled wheels don't cover 3.14+) — "
             "installing Python 3.13 via winget …")
        if _winget_install("Python.Python.3.13", "Python 3.13"):
            found = _find_supported_python()
            if found:
                return found
    return None


def _best_python() -> str:
    """
    Return the best interpreter for creating the venv.

    Compiled dependency wheels only cover CPython 3.11–3.13, so prefer a
    supported interpreter (3.13 → 3.11). Fall back to the running interpreter
    only when nothing better is available.
    """
    supported = _find_supported_python()
    if supported:
        return supported
    if IS_WINDOWS:
        return sys.executable or shutil.which("python") or shutil.which("python3") or "python"
    return shutil.which("python3") or shutil.which("python") or sys.executable


def ensure_venv() -> bool:
    if UVICORN_BIN.exists():
        return True
    info("Creating Python virtual environment …")
    py = _best_python()
    r = run([py, "-m", "venv", str(VENV)])
    if r.returncode != 0:
        fail(f"Failed to create venv: {r.stderr[:200]}")
        return False
    ok("Venv created")
    return True


def ensure_pip_deps() -> bool:
    reqs = BACKEND_DIR / "requirements.txt"
    if not reqs.exists():
        fail("backend/requirements.txt not found")
        return False

    # Check ALL core packages — not just uvicorn — so a partial/broken venv is
    # caught here and reinstalled rather than causing a cryptic backend crash.
    core_imports = [
        "fastapi", "uvicorn", "sqlalchemy", "asyncpg",
        "redis", "pydantic", "httpx", "loguru",
    ]
    missing = [
        pkg for pkg in core_imports
        if run([str(PY_BIN), "-c", f"import {pkg}"], cwd=BACKEND_DIR).returncode != 0
    ]

    if not missing:
        ok("Python dependencies already installed")
        return True

    info(f"Installing Python dependencies: {', '.join(missing)} …")
    pip = PIP_BIN

    # Upgrade pip first — stale pip misses binary wheels for asyncpg, etc.
    run([str(pip), "install", "--quiet", "--upgrade", "pip"], cwd=BACKEND_DIR)

    # Use the fully-pinned lockfile when available — it lets pip skip dependency
    # resolution entirely (--no-deps) which avoids the "graph too complex" error
    # caused by ccxt + openai having very deep transitive trees. The lockfile is
    # frozen against Python 3.13; on newer runtimes (e.g. 3.14) some exact pins
    # have no wheel, so fall back to resolving requirements.txt in that case.
    lock = BACKEND_DIR / "requirements-lock.txt"
    r = None
    if lock.exists():
        r = run([str(pip), "install", "--prefer-binary", "--no-deps",
                 "-r", str(lock)], cwd=BACKEND_DIR)
        if r.returncode != 0:
            warn("Lockfile install failed (often a newer-Python wheel gap) — "
                 "retrying with resolved requirements.txt …")
    if r is None or r.returncode != 0:
        r = run([str(pip), "install", "--prefer-binary", "--upgrade",
                 "-r", str(reqs)], cwd=BACKEND_DIR)
    if r.returncode != 0:
        fail(f"pip install failed:\n{r.stderr[:600]}")
        return False

    # Verify every package is now importable
    still_missing = [
        pkg for pkg in core_imports
        if run([str(PY_BIN), "-c", f"import {pkg}"], cwd=BACKEND_DIR).returncode != 0
    ]
    if still_missing:
        fail(f"Packages still not importable after install: {', '.join(still_missing)}")
        return False

    ok("Python dependencies installed")

    # tradingagents has an unpublished dep (chainlit>=2.11.1) and requires
    # Python>=3.12, so it can't be in requirements.txt.  Install the wheel
    # alone (--no-deps) — the conditional import in orchestrator.py works fine
    # without chainlit/langgraph being present.
    pip = PIP_BIN
    run([str(pip), "install", "--quiet", "--no-deps", "--upgrade",
         "tradingagents==0.6.0"], cwd=BACKEND_DIR)

    # headroom-ai (context compression) is OPTIONAL — imported conditionally and
    # falls back silently if absent (backend/app/utils/headroom_compress.py). It
    # can fail to build on newer/unsupported Python (e.g. 3.14) or Windows, so
    # install it best-effort with prebuilt wheels only and never fail the run.
    r_hr = run([str(pip), "install", "--quiet", "--no-deps", "--only-binary=:all:",
                "--upgrade", "headroom-ai==0.27.0"], cwd=BACKEND_DIR)
    if r_hr.returncode != 0:
        warn("headroom-ai unavailable for this Python — context compression "
             "disabled (optional, app runs normally).")

    return True


# ── npm deps ─────────────────────────────────────────────────────────────────
def ensure_frontend_env() -> None:
    """
    Create frontend/.env.local if it doesn't exist.

    This file is gitignored so fresh clones won't have it. Without it,
    NEXT_PUBLIC_API_URL is undefined and every API call falls back to
    the wrong port (the code had localhost:8000 as a stale hardcoded
    default; the real backend runs on :1448) — causing HTTP 500 / ECONNREFUSED
    on every page load.
    """
    env_local = FRONTEND_DIR / ".env.local"
    if env_local.exists():
        return
    env_local.write_text(
        f"NEXT_PUBLIC_API_URL=http://localhost:{BACKEND_PORT}/api/v1\n"
    )
    ok(f"Created frontend/.env.local (NEXT_PUBLIC_API_URL=http://localhost:{BACKEND_PORT}/api/v1)")


def ensure_npm_deps() -> bool:
    ensure_frontend_env()
    nm = FRONTEND_DIR / "node_modules"
    if nm.exists() and (nm / ".package-lock.json").exists():
        ok("npm dependencies already installed")
        return True
    info("Installing npm dependencies (this may take a minute) …")
    r = run([_npm_cmd(), "install", "--legacy-peer-deps"], cwd=FRONTEND_DIR)
    if r.returncode != 0:
        fail(f"npm install failed:\n{r.stderr[:400]}")
        return False
    ok("npm dependencies installed")
    return True


# ── Backend ───────────────────────────────────────────────────────────────────
def warmup_kronos_and_openhuman() -> None:
    """Confirm the Kronos forecaster is loaded and kick the OpenHuman subconscious.

    - Kronos: /plugins/kronos/status reports the active model + whether the real
      weights loaded (vs the heuristic fallback).
    - OpenHuman: hitting /plugins/agent-paul/jarvis/subconscious/status calls the
      loop's ensure_started() so the idle "keeps thinking" brain starts now
      instead of waiting for the first UI poll.
    """
    base = f"http://localhost:{BACKEND_PORT}/api/v1"

    ks = http_json(f"{base}/plugins/kronos/status", timeout=8)
    if ks:
        model = ks.get("model_name") or KRONOS_MODEL_NAME
        engine = ks.get("engine") or ("kronos" if ks.get("available") else "heuristic")
        device = ks.get("device") or "auto"
        if ks.get("available"):
            ok(f"Kronos forecaster ready — {model} on {device} ({engine})")
        else:
            warn(f"Kronos in heuristic fallback ({model}) — run setup_kronos.sh to load weights")
    else:
        warn("Kronos status not ready yet (model may still be warm-loading)")

    ss = http_json(f"{base}/plugins/agent-paul/jarvis/subconscious/status", timeout=8)
    if ss is not None:
        if ss.get("running"):
            tick = ss.get("tick_seconds", "?")
            ok(f"OpenHuman subconscious running (Agent Paul idle brain, tick {tick}s)")
        elif ss.get("enabled"):
            warn("OpenHuman subconscious enabled but not running yet (will start on next tick)")
        else:
            warn("OpenHuman subconscious disabled (set PAUL_HEARTBEAT_ENABLED=1)")
    else:
        warn("OpenHuman subconscious status not reachable yet (plugin still loading)")


def start_backend(pg_port: int, redis_port: int, mode: str) -> bool:
    if port_open("localhost", BACKEND_PORT, 0.5):
        ok(f"Backend already running on :{BACKEND_PORT}")
        return True

    info(f"Starting FastAPI backend on :{BACKEND_PORT} …")

    log_file = ROOT / "backend.log"

    # Start with a copy of the current shell environment.
    env = {**os.environ, "PYTHONPATH": f"{BACKEND_DIR}:{ROOT}"}

    # Load .env — user-set values WIN over shell environment (so keys like
    # DATABASE_URL, REDIS_URL, and all API keys are taken verbatim from the
    # file the user edited, not silently overridden by computed defaults).
    dotenv = ROOT / ".env"
    if dotenv.exists():
        for line in dotenv.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()

    # Computed fallbacks — only used when .env (and shell) haven't set the key.
    _db_url = (
        f"postgresql+asyncpg://tradebot:tradebot_password@localhost:{pg_port}/tradebot"
    )
    env.setdefault("DATABASE_URL", _db_url)
    env.setdefault("REDIS_URL", f"redis://localhost:{redis_port}/0")

    # Always inject MT5_API_URL so backend can reach the REST bridge
    env.setdefault("MT5_API_URL", MT5_API_URL)

    # Kronos forecaster + OpenHuman subconscious — explicit config so the ML
    # forecaster and the idle "keeps thinking" brain are always configured.
    # (env/.env already loaded above still win via setdefault.)
    env.setdefault("KRONOS_MODEL_NAME", KRONOS_MODEL_NAME)
    if KRONOS_DEVICE:
        env.setdefault("KRONOS_DEVICE", KRONOS_DEVICE)
    env.setdefault("PAUL_HEARTBEAT_ENABLED", PAUL_HEARTBEAT_ENABLED)
    env.setdefault("PAUL_HEARTBEAT_GOAL_CONTINUATION", PAUL_HEARTBEAT_GOAL_CONTINUATION)
    env.setdefault("PAUL_HEARTBEAT_TICK_SECONDS", PAUL_HEARTBEAT_TICK_SECONDS)

    # Resource-aware ML/BLAS thread caps so torch/numpy (Kronos, sentiment) can't
    # saturate the CPU and stall the API on smaller machines (e.g. 8-core M2).
    _res = get_resources()
    _ml = str(_res["ml_threads"])
    for _k in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS",
               "OPENBLAS_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "TORCH_NUM_THREADS"):
        env.setdefault(_k, _ml)
    env.setdefault("TOKENIZERS_PARALLELISM", "false")

    # On low-core machines (≤2 physical cores, e.g. the dual-core i5-4300U)
    # uvicorn's --reload is a needless tax: it spawns a second supervisor
    # process AND a constant filesystem watcher that steals CPU from the API.
    # Skip it there and run a single lean process. Override with TRADEBOT_RELOAD.
    _reload_override = os.environ.get("TRADEBOT_RELOAD")
    if _reload_override is not None:
        use_reload = _reload_override.strip().lower() in ("1", "true", "yes", "on")
    else:
        use_reload = int(_res["physical"]) > 2
    uvicorn_cmd = [str(UVICORN_BIN), "app.main:app",
                   "--host", "0.0.0.0", "--port", str(BACKEND_PORT)]
    if use_reload:
        uvicorn_cmd += ["--reload",
                        "--reload-dir", str(BACKEND_DIR / "app"),
                        "--reload-dir", str(ROOT / "plugins")]
    else:
        info("Low-core machine — starting backend without --reload (set "
             "TRADEBOT_RELOAD=1 to force auto-reload)")

    with open(log_file, "w") as lf:
        proc = subprocess.Popen(
            uvicorn_cmd,
            cwd=BACKEND_DIR,
            env=env,
            stdout=lf,
            stderr=subprocess.STDOUT,
        )

    # Write PID
    (ROOT / "backend.pid").write_text(str(proc.pid))

    if wait_for_port("localhost", BACKEND_PORT, "FastAPI backend", max_wait=60):
        # Extra: hit /health or /api/v1 to confirm it responds
        time.sleep(1)
        if http_ok(f"http://localhost:{BACKEND_PORT}/api/v1/health", timeout=5):
            ok("Backend /health endpoint OK")
        else:
            warn("Backend port open but /health not ready yet (still loading plugins)")
        warmup_kronos_and_openhuman()
        return True
    # Print last lines of log on failure
    try:
        lines = log_file.read_text().splitlines()
        fail("Backend failed to start. Last log lines:")
        for ln in lines[-20:]:
            print(f"    {ln}")
    except Exception:
        pass
    return False


# ── Frontend ─────────────────────────────────────────────────────────────────
def start_frontend() -> bool:
    if port_open("localhost", FRONTEND_PORT, 0.5):
        ok(f"Frontend already running on :{FRONTEND_PORT}")
        return True

    info(f"Starting Next.js frontend on :{FRONTEND_PORT} …")
    log_file = ROOT / "frontend.log"

    # Size the Node heap + pass a UI quality-tier hint based on the machine's RAM
    # and cores, so the dev server doesn't OOM/GC-thrash on smaller laptops and
    # the browser can start at an appropriate graphics tier.
    _res = get_resources()
    _node_opts = os.environ.get("NODE_OPTIONS", "")
    if "max-old-space-size" not in _node_opts:
        _node_opts = f"{_node_opts} --max-old-space-size={_res['node_heap_mb']}".strip()

    env = {
        **os.environ,
        "NEXT_PUBLIC_API_URL": f"http://localhost:{BACKEND_PORT}/api/v1",
        "PORT": str(FRONTEND_PORT),
        "NODE_OPTIONS": _node_opts,
        "NEXT_PUBLIC_PERF_TIER": str(_res["ui_tier"]),
        # Hard-disable WebGL on weak GPUs (low tier) — see detect_resources().
        "NEXT_PUBLIC_DISABLE_3D": "1" if _res.get("disable_3d") else "0",
    }

    with open(log_file, "w") as lf:
        proc = subprocess.Popen(
            [_npx_cmd(), "next", "dev", "--port", str(FRONTEND_PORT)],
            cwd=FRONTEND_DIR,
            env=env,
            stdout=lf,
            stderr=subprocess.STDOUT,
        )

    (ROOT / "frontend.pid").write_text(str(proc.pid))

    if wait_for_port("localhost", FRONTEND_PORT, "Next.js frontend", max_wait=90):
        ok("Frontend ready")
        return True
    try:
        lines = log_file.read_text().splitlines()
        fail("Frontend failed to start. Last log lines:")
        for ln in lines[-20:]:
            print(f"    {ln}")
    except Exception:
        pass
    return False


# ── Stop ──────────────────────────────────────────────────────────────────────
def stop_all() -> None:
    header("Stopping TradeBot services")
    for pattern in ["uvicorn app.main:app", "next dev"]:
        pids = pgrep(pattern)
        if pids:
            pkill(pattern)
            info(f"Stopped: {pattern} (pids {pids})")
        else:
            warn(f"Not running: {pattern}")

    # PID-file fallback — REQUIRED on Windows where pgrep/pkill are no-ops, and
    # a safety net elsewhere. start_backend/start_frontend write these files.
    for label, pidfile in (("backend", ROOT / "backend.pid"),
                           ("frontend", ROOT / "frontend.pid")):
        if not pidfile.exists():
            continue
        try:
            pid = int(pidfile.read_text().strip())
        except (ValueError, OSError):
            pidfile.unlink(missing_ok=True)
            continue
        if _pid_alive(pid) and _kill_pid_tree(pid):
            info(f"Stopped {label} via PID file (pid {pid})")
        pidfile.unlink(missing_ok=True)

    # MT5 REST container
    if _docker_available() and _container_running(MT5_CONTAINER):
        subprocess.run(["docker", "stop", MT5_CONTAINER], capture_output=True, timeout=15)
        info(f"Stopped Docker container: {MT5_CONTAINER}")
    # Intentionally do NOT stop Headroom/Obsidian here to preserve continuous context tooling.
    # Their data and processes are managed independently and are never deleted by this script.
    ok("Done")


# ── Status ────────────────────────────────────────────────────────────────────
def status() -> None:
    header("TradeBot Service Status")
    checks = [
        ("Headroom proxy", "127.0.0.1", HEADROOM_PORT),
        ("PostgreSQL (brew)", "localhost", 5434),
        ("PostgreSQL (docker)", "localhost", 5433),
        ("Redis (brew)", "localhost", 6379),
        ("Redis (docker)", "localhost", 6380),
        ("MT5 REST (mtapi-io)", "localhost", _mt5_port()),
        ("FastAPI backend", "localhost", BACKEND_PORT),
        ("Next.js frontend", "localhost", FRONTEND_PORT),
    ]
    for label, host, port in checks:
        running = port_open(host, port, 0.5)
        symbol = f"{C.GREEN}●{C.RESET}" if running else f"{C.RED}○{C.RESET}"
        state = "running" if running else "stopped"
        print(f"  {symbol}  {label:<26}  :{port}  {state}")
    obsidian_open = bool(pgrep("Obsidian"))
    obs_symbol = f"{C.GREEN}●{C.RESET}" if obsidian_open else f"{C.YELLOW}○{C.RESET}"
    obs_state = "running" if obsidian_open else "not running"
    print(f"  {obs_symbol}  {'Obsidian app':<26}  {'-':>5}  {obs_state}")


# ── Summary table ─────────────────────────────────────────────────────────────
def print_sox_header() -> None:
    """SOX ASCII logo with an animated JARVIS energy orb rendered beside it.

    The orb is a pulsing core ringed by an orbiting spark, drawn to the right of
    the logo. Animates in-place via ANSI cursor moves when stdout is a TTY;
    falls back to a single static frame when piped/redirected (keeps logs clean).
    """
    CY, GD, DIM, RS, B = C.CYAN, "\033[38;5;214m", "\033[2m", C.RESET, C.BOLD

    logo = [
        "███████╗ ██████╗ ██╗  ██╗",
        "██╔════╝██╔═══██╗╚██╗██╔╝",
        "███████╗██║   ██║ ╚███╔╝ ",
        "╚════██║██║   ██║ ██╔██╗ ",
        "███████║╚██████╔╝██╔╝ ██╗",
        "╚══════╝ ╚═════╝ ╚═╝  ╚═╝",
    ]

    # Orb geometry: 8 rim slots (clockwise) around a pulsing core, on a 7x5 grid.
    rim = [(0, 3), (1, 5), (2, 6), (3, 5), (4, 3), (3, 1), (2, 0), (1, 1)]
    core_pulse = ['·', '•', '●', '◉', '●', '•']
    OW, OH = 7, 5

    def colorize(ch: str) -> str:
        if ch == '✦':  return f"{B}{GD}✦{RS}"       # orbiting spark
        if ch in '◉●': return f"{B}{CY}{ch}{RS}"     # bright core
        if ch == '•':  return f"{CY}•{RS}"           # comet trail / soft core
        if ch == '·':  return f"{DIM}{CY}·{RS}"      # dim rim
        return ' '

    def render(f: int) -> List[str]:
        grid = [[' '] * OW for _ in range(OH)]
        for (r, c) in rim:
            grid[r][c] = '·'
        tr, tc = rim[(f - 1) % len(rim)]             # trailing comet dot
        if grid[tr][tc] == '·':
            grid[tr][tc] = '•'
        sr, sc = rim[f % len(rim)]                   # bright spark head
        grid[sr][sc] = '✦'
        grid[2][3] = core_pulse[f % len(core_pulse)] # pulsing core
        lines = []
        for r in range(6):
            orow = r - 1  # vertically centre the 5-row orb against the 6-row logo
            orb = ''.join(colorize(c) for c in grid[orow]) if 0 <= orow < OH else ' ' * OW
            lines.append(f"    {B}{CY}{logo[r]}{RS}     {orb}")
        return lines

    print()
    try:
        animate = sys.stdout.isatty()
    except Exception:
        animate = False

    if animate:
        FRAMES, DELAY = 24, 0.06
        block = render(0)
        print("\n".join(block))
        for f in range(1, FRAMES):
            time.sleep(DELAY)
            sys.stdout.write(f"\033[{len(block)}A")   # move cursor up to redraw
            for ln in render(f):
                sys.stdout.write("\r\033[K" + ln + "\n")
            sys.stdout.flush()
    else:
        for ln in render(3):
            print(ln)

    print(f"    {B}{GD}S O X   T R A D E   B O T{RS}")
    print(f"    {DIM}{CY}Strategic Operations eXchange{RS}")
    print()


def print_sox_banner(mode: str, pg_port: int, redis_port: int) -> None:
    """Branded SOX TRADE BOT status display shown after a successful start."""
    r = get_resources()
    CY, GN, B, RS, GD, DIM = C.CYAN, C.GREEN, C.BOLD, C.RESET, "\033[38;5;214m", "\033[2m"

    print_sox_header()


    W = 58  # visible width between the box borders

    def line(left: str = "╠", fill: str = "═", right: str = "╣") -> str:
        return f"{CY}{left}{fill * W}{right}{RS}"

    def row(label: str, value: str, vcol: str = "", dot: str = "") -> str:
        # Pad using the plain (uncoloured) text so alignment stays correct.
        plain = f"  {label:<11}{dot}{value}"
        pad = max(0, W - len(plain))
        dot_c = f"{GN}{dot}{RS}" if dot else ""
        return (
            f"{CY}║{RS}  {CY}{label:<11}{RS}{dot_c}{vcol}{value}{RS}"
            f"{' ' * pad}{CY}║{RS}"
        )

    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    tz = _DOTENV.get("TZ", "")

    print(line("╔", "═", "╗"))
    print(row("STATUS", "ALL SYSTEMS ONLINE", GN + B, dot="● "))
    print(row("STARTED", f"{ts}{('  ' + tz) if tz else ''}"))
    print(line())
    print(row("CPU", f"{r['physical']} cores / {r['logical']} threads"))
    print(row("RAM", f"{r['ram_gb']} GB"))
    print(row("GRAPHICS", f"{str(r['ui_tier']).upper()} tier (auto)"))
    print(row("DB MODE", f"{mode}  ·  pg :{pg_port}  ·  redis :{redis_port}"))
    print(line())
    print(row("FRONTEND", f"http://localhost:{FRONTEND_PORT}", CY))
    print(row("BACKEND", f"http://localhost:{BACKEND_PORT}/api/v1", CY))
    print(row("API DOCS", f"http://localhost:{BACKEND_PORT}/docs", CY))
    print(row("MT5 REST", MT5_API_URL, CY))
    print(row("HEADROOM", f"{HEADROOM_URL}/dashboard", CY))
    print(line("╚", "═", "╝"))
    print(f"\n  {DIM}Logs:{RS} {ROOT}/backend.log  |  {ROOT}/frontend.log\n")


def print_summary(results: Dict[str, bool], mode: str, pg_port: int, redis_port: int) -> None:
    header("TradeBot Startup Summary")
    sep()
    all_ok = all(results.values())
    for svc, success in results.items():
        symbol = f"{C.GREEN}✓{C.RESET}" if success else f"{C.RED}✗{C.RESET}"
        print(f"  {symbol}  {svc}")
    sep()
    if all_ok:
        print_sox_banner(mode, pg_port, redis_port)
    else:
        failed = [k for k, v in results.items() if not v]
        print(f"\n  {C.RED}{C.BOLD}Some services failed to start:{C.RESET} {', '.join(failed)}")
        print(f"  Check logs: {ROOT}/backend.log  {ROOT}/frontend.log\n")
        sys.exit(1)


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="TradeBot startup script")
    parser.add_argument("--brew",   action="store_true", help="Use Homebrew postgres/redis")
    parser.add_argument("--docker", action="store_true", help="Use Docker postgres/redis")
    parser.add_argument("--stop",   action="store_true", help="Stop all services")
    parser.add_argument("--status", action="store_true", help="Show service status")
    args = parser.parse_args()

    if args.stop:
        stop_all()
        return

    if args.status:
        status()
        return

    forced = "brew" if args.brew else ("docker" if args.docker else None)
    mode = detect_mode(forced)
    save_mode(mode)

    # ── Pre-flight ────────────────────────────────────────────────────────────
    if not preflight_check(mode):
        print(f"  {C.YELLOW}Fix the issues above, then re-run:  python3 start.py{C.RESET}\n")
        sys.exit(1)

    header(f"TradeBot Startup  [{mode.upper()} mode]")
    print_resources()
    results: Dict[str, bool] = {}

    # ── 0. Integrations ───────────────────────────────────────────────────────
    integrations_ok = setup_integrations()
    results["Headroom + Obsidian"] = integrations_ok
    if not integrations_ok:
        fail("Cannot continue without required integrations (Headroom/Obsidian).")
        sys.exit(1)

    # ── 1. Database ───────────────────────────────────────────────────────────
    header("1/6  Database")
    if mode == "brew":
        pg_ok, pg_port = start_postgres_brew()
    else:
        pg_ok, pg_port = start_postgres_docker()
    results["PostgreSQL"] = pg_ok
    if not pg_ok:
        fail("Cannot continue without a database.")
        sys.exit(1)

    # ── 2. Redis ──────────────────────────────────────────────────────────────
    header("2/6  Redis")
    # Honour a custom REDIS_URL in .env — if the user already points at a
    # managed/remote Redis we skip local provisioning and just verify it.
    _dotenv_redis = _DOTENV.get("REDIS_URL", "")
    _using_custom_redis = bool(
        _dotenv_redis and not _dotenv_redis.startswith("redis://localhost")
    )
    if _using_custom_redis:
        info(f"Using custom REDIS_URL from .env: {_dotenv_redis}")
        # Parse the port from the URL for health-check purposes
        try:
            import urllib.parse as _up
            _parsed = _up.urlparse(_dotenv_redis)
            redis_port = _parsed.port or 6379
            redis_ok = _redis_ping(redis_port)
        except Exception:
            redis_port = 6379
            redis_ok = False
        if redis_ok:
            ok("Custom Redis is reachable (PING → PONG)")
        else:
            warn("Custom Redis did not answer PING — backend may start degraded")
        results["Redis"] = redis_ok
    elif mode == "brew":
        redis_ok, redis_port = start_redis_brew()
        results["Redis"] = redis_ok
    else:
        redis_ok, redis_port = start_redis_docker()
        results["Redis"] = redis_ok
    if not redis_ok:
        warn("Redis not available — backend may still start in degraded mode")

    # ── 3. Python environment ─────────────────────────────────────────────────
    header("3/6  Python environment")
    venv_ok = ensure_venv()
    if not venv_ok:
        fail("Cannot start backend without Python venv.")
        sys.exit(1)
    deps_ok = ensure_pip_deps()
    results["Python deps"] = deps_ok
    if not deps_ok:
        fail("Cannot start backend without Python deps.")
        sys.exit(1)

    # ── 4. Backend ────────────────────────────────────────────────────────────
    header("4/6  FastAPI backend")
    backend_ok = start_backend(pg_port, redis_port if redis_ok else 6379, mode)
    results["Backend"] = backend_ok

    # ── 5. Frontend ───────────────────────────────────────────────────────────
    header("5/6  Next.js frontend")
    npm_ok = ensure_npm_deps()
    results["npm deps"] = npm_ok
    frontend_ok = start_frontend() if npm_ok else False
    results["Frontend"] = frontend_ok

    # ── Summary ───────────────────────────────────────────────────────────────
    print_summary(results, mode, pg_port, redis_port if redis_ok else 0)

    # Keep process alive so Ctrl+C cleanly stops the script
    # (child processes run independently via nohup/Popen)
    print(f"  Press {C.BOLD}Ctrl+C{C.RESET} to stop watching (services keep running)\n")
    try:
        while True:
            time.sleep(5)
            # Periodic health check
            be = port_open("localhost", BACKEND_PORT, 0.5)
            fe = port_open("localhost", FRONTEND_PORT, 0.5)
            if not be:
                warn("Backend is no longer responding!")
            if not fe:
                warn("Frontend is no longer responding!")
    except KeyboardInterrupt:
        print(f"\n  {C.YELLOW}Detached. Services are still running.{C.RESET}")
        print(f"  Stop them with:  {C.BOLD}python3 start.py --stop{C.RESET}\n")


if __name__ == "__main__":
    main()
