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
UVICORN_BIN = VENV / "bin" / "uvicorn"
PY_BIN      = VENV / "bin" / "python"
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

MT5_API_URL   = _DOTENV.get("MT5_API_URL", os.environ.get("MT5_API_URL", "http://localhost:8092"))
MT5_IMAGE     = "timurila/mt5rest"     # Docker image for mtapi-io REST bridge
MT5_CONTAINER = "mt5rest"

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
    r = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True)
    return [int(p) for p in r.stdout.split() if p.strip().isdigit()]


def pkill(pattern: str) -> None:
    subprocess.run(["pkill", "-f", pattern], capture_output=True)


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


def start_obsidian() -> bool:
    if not ensure_dir(OBSIDIAN_VAULT_DIR):
        return False

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
    npm = shutil.which("npm") or "npm"
    ok_inst, err = _spinner_run([npm, "install", "-g", pkg], f"npm install -g {pkg}")
    if not ok_inst:
        fail(f"  npm install -g {pkg} failed: {err[:200]}")
    return ok_inst


# ── Pre-flight checks (detect + auto-install) ─────────────────────────────────

def _cmd_version(cmd: str) -> str:
    """Return first line of `cmd --version`, or empty string on failure."""
    try:
        r = subprocess.run([cmd, "--version"], capture_output=True, text=True, timeout=5)
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
    py_ok = tuple(int(x) for x in py_ver.split(".")[:2]) >= (3, 9)
    if not _check("Python ≥ 3.9", py_ok, f"python {py_ver}"):
        fail("  Python 3.9+ is required to run this script.")
        fail("  Install: brew install python@3.11  then re-run with python3.11 start.py")
        unfixable.append("Python < 3.9 (current runner)")

    # ── Homebrew ─────────────────────────────────────────────────────────────
    brew_ok = bool(shutil.which("brew") or Path("/opt/homebrew/bin/brew").exists())
    if not brew_ok:
        if sys.platform == "darwin":
            warn("Homebrew not found — attempting auto-install …")
            brew_ok = _install_homebrew()
            if brew_ok:
                fixed_items.append("Homebrew")
            else:
                unfixable.append("Homebrew (required for all brew-mode deps)")
        else:
            _check("Homebrew (brew)", False, "", "only available on macOS")
            unfixable.append("Homebrew (macOS only)")
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
    _check("Node.js", node_ok, node_ver,
           "brew install node  (or nvm install --lts)")
    if not node_ok:
        unfixable.append("Node.js not found")

    npm_ver = _cmd_version("npm")
    npm_ok  = bool(npm_ver)
    _check("npm", npm_ok, npm_ver, "reinstall node (npm comes bundled)")
    if not npm_ok:
        unfixable.append("npm not found")

    npx_ok = bool(shutil.which("npx"))
    if not npx_ok and npm_ok:
        warn("npx not found — installing globally …")
        if _npm_global("npx"):
            npx_ok = bool(shutil.which("npx"))
            if npx_ok:
                fixed_items.append("npx")
    _check("npx", npx_ok, "", "npm install -g npx")
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

        redis_ok = bool(shutil.which("redis-cli"))
        if not redis_ok and brew_ok:
            warn("redis not found — auto-installing via Homebrew …")
            if _brew("redis", "Redis"):
                redis_ok = bool(shutil.which("redis-cli"))
                if redis_ok:
                    fixed_items.append("redis")
        _check("redis  (brew formula)", redis_ok, "", "brew install redis")
        if not redis_ok:
            unfixable.append("redis not installed")

    else:  # docker mode
        docker_cli_ok = bool(shutil.which("docker"))
        _check("Docker CLI", docker_cli_ok, _cmd_version("docker"),
               "https://www.docker.com/products/docker-desktop  (manual install)")
        if not docker_cli_ok:
            unfixable.append("Docker CLI not found (install Docker Desktop manually)")

        if docker_cli_ok:
            r = subprocess.run(["docker", "info"], capture_output=True, timeout=5)
            daemon_ok = r.returncode == 0
            if not daemon_ok:
                info("Docker daemon not running — attempting to open Docker Desktop …")
                subprocess.run(["open", "-a", "Docker"], capture_output=True)
                for _ in range(30):
                    time.sleep(2)
                    if subprocess.run(["docker", "info"],
                                      capture_output=True, timeout=3).returncode == 0:
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
                                capture_output=True, text=True, timeout=5)
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

    venv_exists = VENV.exists() and (VENV / "bin" / "python").exists()
    if not venv_exists:
        info("Python venv not found — creating now …")
        py = _best_python()
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
            pip = VENV / "bin" / "pip"
            reqs_file = BACKEND_DIR / "requirements.txt"
            lock_file = BACKEND_DIR / "requirements-lock.txt"
            # Upgrade pip itself first — old pip versions fail on binary wheels.
            _spinner_run([str(pip), "install", "--quiet", "--upgrade", "pip"],
                         "Upgrading pip", timeout=60)
            # Prefer the fully-pinned lockfile: --no-deps skips resolution entirely,
            # avoiding the "dependency graph too complex" error from ccxt/openai trees.
            if lock_file.exists():
                ok_p, err_p = _spinner_run(
                    [str(pip), "install", "--prefer-binary", "--no-deps",
                     "-r", str(lock_file)],
                    "pip install -r requirements-lock.txt",
                    cwd=BACKEND_DIR, timeout=360
                )
            else:
                ok_p, err_p = _spinner_run(
                    [str(pip), "install", "--prefer-binary", "--upgrade",
                     "-r", str(reqs_file)],
                    "pip install -r requirements.txt",
                    cwd=BACKEND_DIR, timeout=360
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
            ["npm", "install", "--legacy-peer-deps"],
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
    if forced:
        return forced
    if MODE_FILE.exists():
        saved = MODE_FILE.read_text().strip()
        warn(f"Using last saved DB mode: {C.BOLD}{saved}{C.RESET}")
        return saved
    # Auto-detect: prefer brew if pg_isready binary exists
    if (PG_BIN / "pg_isready").exists():
        return "brew"
    if shutil.which("docker"):
        return "docker"
    return "brew"  # will fail gracefully below


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
def start_redis_brew() -> Tuple[bool, int]:
    redis_port = 6379
    info("Starting Redis (Homebrew) …")
    if port_open("localhost", redis_port, 0.5):
        ok(f"Redis already running on :{redis_port}")
        return True, redis_port
    run(["brew", "services", "start", "redis"])
    return wait_for_port("localhost", redis_port, "Redis"), redis_port


def start_redis_docker() -> Tuple[bool, int]:
    redis_port = 6380
    info("Starting Redis (Docker) …")
    if port_open("localhost", redis_port, 0.5):
        ok(f"Redis already running on :{redis_port}")
        return True, redis_port
    run(["docker", "compose", "up", "-d", "redis"], cwd=ROOT)
    return wait_for_port("localhost", redis_port, "Redis"), redis_port


# ── Python venv ───────────────────────────────────────────────────────────────
def _best_python() -> str:
    """
    Return the newest Python ≥3.11 available.

    The dependency lockfile is frozen against modern CPython, and several core
    packages (pydantic-core, etc.) only ship pre-built wheels for 3.11–3.13.
    If the venv is created with an old interpreter (e.g. macOS system Python
    3.9), pip falls back to building those wheels from Rust/C source and fails.
    Prefer 3.13 → 3.12 → 3.11, then any python3 as a last resort.
    """
    for name in ("python3.13", "python3.12", "python3.11"):
        p = shutil.which(name)
        if p:
            return p
    for cand in ("/opt/homebrew/bin/python3.13",
                 "/opt/homebrew/bin/python3.12",
                 "/opt/homebrew/bin/python3.11"):
        if Path(cand).exists():
            return cand
    return shutil.which("python3") or "/opt/homebrew/bin/python3"


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
    pip = VENV / "bin" / "pip"

    # Upgrade pip first — stale pip misses binary wheels for asyncpg, etc.
    run([str(pip), "install", "--quiet", "--upgrade", "pip"], cwd=BACKEND_DIR)

    # Use the fully-pinned lockfile when available — it lets pip skip dependency
    # resolution entirely (--no-deps) which avoids the "graph too complex" error
    # caused by ccxt + openai + headroom-ai having very deep transitive trees.
    lock = BACKEND_DIR / "requirements-lock.txt"
    if lock.exists():
        r = run([str(pip), "install", "--prefer-binary", "--no-deps",
                 "-r", str(lock)], cwd=BACKEND_DIR)
    else:
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
    pip = VENV / "bin" / "pip"
    run([str(pip), "install", "--quiet", "--no-deps", "--upgrade",
         "tradingagents==0.6.0"], cwd=BACKEND_DIR)

    return True


# ── npm deps ─────────────────────────────────────────────────────────────────
def ensure_npm_deps() -> bool:
    nm = FRONTEND_DIR / "node_modules"
    if nm.exists() and (nm / ".package-lock.json").exists():
        ok("npm dependencies already installed")
        return True
    info("Installing npm dependencies (this may take a minute) …")
    r = run(["npm", "install", "--legacy-peer-deps"], cwd=FRONTEND_DIR)
    if r.returncode != 0:
        fail(f"npm install failed:\n{r.stderr[:400]}")
        return False
    ok("npm dependencies installed")
    return True


# ── Backend ───────────────────────────────────────────────────────────────────
def start_backend(pg_port: int, redis_port: int, mode: str) -> bool:
    if port_open("localhost", BACKEND_PORT, 0.5):
        ok(f"Backend already running on :{BACKEND_PORT}")
        return True

    info(f"Starting FastAPI backend on :{BACKEND_PORT} …")

    log_file = ROOT / "backend.log"
    env = {
        **os.environ,
        "PYTHONPATH": f"{BACKEND_DIR}:{ROOT}",
        "DATABASE_URL": (
            f"postgresql+asyncpg://tradebot:tradebot_password@localhost:{pg_port}/tradebot"
            if mode == "docker"
            else f"postgresql+asyncpg://tradebot:tradebot_password@localhost:{pg_port}/tradebot"
        ),
        "REDIS_URL": f"redis://localhost:{redis_port}/0",
    }

    # Load .env from root if present
    dotenv = ROOT / ".env"
    if dotenv.exists():
        for line in dotenv.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env.setdefault(k.strip(), v.strip())
    # Always inject MT5_API_URL so backend can reach the REST bridge
    env.setdefault("MT5_API_URL", MT5_API_URL)

    with open(log_file, "w") as lf:
        proc = subprocess.Popen(
            [str(UVICORN_BIN), "app.main:app",
             "--host", "0.0.0.0", "--port", str(BACKEND_PORT), "--reload",
             "--reload-dir", str(BACKEND_DIR / "app"),
             "--reload-dir", str(ROOT / "plugins")],
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

    env = {
        **os.environ,
        "NEXT_PUBLIC_API_URL": f"http://localhost:{BACKEND_PORT}/api/v1",
        "PORT": str(FRONTEND_PORT),
    }

    with open(log_file, "w") as lf:
        proc = subprocess.Popen(
            ["npx", "next", "dev", "--port", str(FRONTEND_PORT)],
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
    obsidian_open = subprocess.run(["pgrep", "-f", "Obsidian"], capture_output=True).returncode == 0
    obs_symbol = f"{C.GREEN}●{C.RESET}" if obsidian_open else f"{C.YELLOW}○{C.RESET}"
    obs_state = "running" if obsidian_open else "not running"
    print(f"  {obs_symbol}  {'Obsidian app':<26}  {'-':>5}  {obs_state}")


# ── Summary table ─────────────────────────────────────────────────────────────
def print_summary(results: Dict[str, bool], mode: str, pg_port: int, redis_port: int) -> None:
    header("TradeBot Startup Summary")
    sep()
    all_ok = all(results.values())
    for svc, success in results.items():
        symbol = f"{C.GREEN}✓{C.RESET}" if success else f"{C.RED}✗{C.RESET}"
        print(f"  {symbol}  {svc}")
    sep()
    if all_ok:
        print(f"\n  {C.BOLD}{C.GREEN}All services started successfully!{C.RESET}\n")
        print(f"  {C.CYAN}Headroom →{C.RESET}  {HEADROOM_URL}/dashboard")
        print(f"  {C.CYAN}Obsidian →{C.RESET}  vault at {OBSIDIAN_VAULT_DIR}")
        print(f"  {C.CYAN}Frontend  →{C.RESET}  http://localhost:{FRONTEND_PORT}")
        print(f"  {C.CYAN}Backend   →{C.RESET}  http://localhost:{BACKEND_PORT}/api/v1")
        print(f"  {C.CYAN}API docs  →{C.RESET}  http://localhost:{BACKEND_PORT}/docs")
        print(f"  {C.CYAN}MT5 REST  →{C.RESET}  {MT5_API_URL}")
        print(f"  {C.CYAN}DB mode   →{C.RESET}  {mode} (pg :{pg_port}, redis :{redis_port})")
        print(f"\n  Logs: {ROOT}/backend.log  |  {ROOT}/frontend.log\n")
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
    if mode == "brew":
        redis_ok, redis_port = start_redis_brew()
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
