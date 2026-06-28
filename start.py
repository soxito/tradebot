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
        import urllib.request
        urllib.request.urlopen(url, timeout=timeout)
        return True
    except Exception:
        return False


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


def _install_homebrew() -> bool:
    """Install Homebrew via the official install script."""
    info("Installing Homebrew (requires internet) …")
    curl = shutil.which("curl") or "/usr/bin/curl"
    script_url = "https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh"
    r = subprocess.run([curl, "-fsSL", script_url], capture_output=True, timeout=30)
    if r.returncode != 0:
        fail("Could not fetch Homebrew install script — check internet connection")
        return False
    script = r.stdout
    ok_inst, err = _spinner_run(
        ["/bin/bash", "-s"], "Installing Homebrew",
        timeout=600
    )
    # pipe script via stdin
    if not ok_inst:
        # Try direct pipe approach
        proc = subprocess.run(
            ["/bin/bash", "-c",
             f'{curl} -fsSL {script_url} | NONINTERACTIVE=1 /bin/bash'],
            timeout=600, capture_output=True
        )
        ok_inst = proc.returncode == 0
    if ok_inst:
        # Ensure brew is on PATH
        for bp in ["/opt/homebrew/bin/brew", "/usr/local/bin/brew"]:
            if Path(bp).exists():
                os.environ["PATH"] = str(Path(bp).parent) + ":" + os.environ["PATH"]
                break
        ok("Homebrew installed")
    else:
        fail("Homebrew install failed — install manually then re-run")
    return ok_inst


def _brew(formula: str, label: str = "") -> bool:
    """brew install formula, return True on success."""
    brew = shutil.which("brew") or "/opt/homebrew/bin/brew"
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
        py = shutil.which("python3") or "/opt/homebrew/bin/python3"
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
            ok_p, err_p = _spinner_run(
                [str(pip), "install", "-q", "-r", str(reqs_file)],
                "pip install -r requirements.txt",
                cwd=BACKEND_DIR, timeout=300
            )
            if ok_p:
                # Re-check
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
            else:
                _check("Python packages", False, "", f"pip install failed: {err_p[:100]}")
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
def start_postgres_brew() -> Tuple[bool, int]:
    pg_port = 5434
    info("Starting PostgreSQL (Homebrew) …")
    if port_open("localhost", pg_port, 0.5):
        ok(f"PostgreSQL already running on :{pg_port}")
        return True, pg_port
    run(["brew", "services", "start", "postgresql@16"])
    return wait_for_port("localhost", pg_port, "PostgreSQL"), pg_port


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
def ensure_venv() -> bool:
    if UVICORN_BIN.exists():
        return True
    info("Creating Python virtual environment …")
    py = shutil.which("python3") or "/opt/homebrew/bin/python3"
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
    # Quick check: if uvicorn is importable in the venv, skip reinstall
    r = run([str(PY_BIN), "-c", "import uvicorn"], cwd=BACKEND_DIR)
    if r.returncode == 0:
        ok("Python dependencies already installed")
        return True
    info("Installing Python dependencies (this may take a minute) …")
    pip = VENV / "bin" / "pip"
    r = run([str(pip), "install", "-q", "-r", str(reqs)], cwd=BACKEND_DIR)
    if r.returncode != 0:
        fail(f"pip install failed:\n{r.stderr[:400]}")
        return False
    ok("Python dependencies installed")
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
    ok("Done")


# ── Status ────────────────────────────────────────────────────────────────────
def status() -> None:
    header("TradeBot Service Status")
    checks = [
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

    # ── 1. Database ───────────────────────────────────────────────────────────
    header("1/5  Database")
    if mode == "brew":
        pg_ok, pg_port = start_postgres_brew()
    else:
        pg_ok, pg_port = start_postgres_docker()
    results["PostgreSQL"] = pg_ok
    if not pg_ok:
        fail("Cannot continue without a database.")
        sys.exit(1)

    # ── 2. Redis ──────────────────────────────────────────────────────────────
    header("2/5  Redis")
    if mode == "brew":
        redis_ok, redis_port = start_redis_brew()
    else:
        redis_ok, redis_port = start_redis_docker()
    results["Redis"] = redis_ok
    if not redis_ok:
        warn("Redis not available — backend may still start in degraded mode")

    # ── 3. Python environment ─────────────────────────────────────────────────
    header("3/5  Python environment")
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
    header("4/5  FastAPI backend")
    backend_ok = start_backend(pg_port, redis_port if redis_ok else 6379, mode)
    results["Backend"] = backend_ok

    # ── 5. Frontend ───────────────────────────────────────────────────────────
    header("5/5  Next.js frontend")
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
