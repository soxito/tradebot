"""Keep the local speech engine running, gently.

start.py launches this instead of uvicorn directly. It:

* runs the engine at low CPU priority (nice 10) with capped BLAS/tokenizer
  threads, so a transcription never competes with the trading backend, the UI
  or Ollama for the machine;
* restarts the engine if it crashes or is killed, with a growing back-off so
  a broken install can't spin the CPU restarting it;
* stops the engine and exits on SIGTERM/SIGINT — `start.py --stop` kills this
  process (its PID is in speech-engine.pid) and the port owner.

Run from speech_engine/ with its own venv:
    .venv/bin/python supervise.py --port 8790
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PID_FILE = HERE.parent / "speech-engine.pid"

# Restarts after a run shorter than this count as a crash loop and back off.
HEALTHY_RUN_S = 120
BACKOFF_S = (2, 5, 15, 30, 60, 120)

_stopping = False
_child: subprocess.Popen | None = None


def _log(msg: str) -> None:
    print(f"[speech-supervisor] {time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def _stop(signum, _frame) -> None:
    global _stopping
    _stopping = True
    if _child and _child.poll() is None:
        _child.terminate()


def _engine_env() -> dict[str, str]:
    env = dict(os.environ)
    # MLX runs the models on the GPU; these cap the CPU-side helpers (audio
    # decoding, tokenisers, numpy BLAS) that otherwise grab every core.
    for var in ("OMP_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "OPENBLAS_NUM_THREADS",
                "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        env.setdefault(var, "2")
    env.setdefault("TOKENIZERS_PARALLELISM", "false")
    env.setdefault("PYTHONUNBUFFERED", "1")
    return env


def main() -> int:
    global _child
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.getenv("SPEECH_ENGINE_PORT", "8790")))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--nice", type=int, default=10)
    args = ap.parse_args()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        os.nice(args.nice)  # inherited by the engine
    except OSError:
        pass
    PID_FILE.write_text(str(os.getpid()))

    cmd = [sys.executable, "-m", "uvicorn", "main:app", "--host", args.host, "--port", str(args.port)]

    failures = 0
    try:
        while not _stopping:
            started = time.monotonic()
            _log(f"starting engine on :{args.port} (nice +{args.nice})")
            _child = subprocess.Popen(cmd, cwd=str(HERE), env=_engine_env())
            code = _child.wait()
            if _stopping:
                break
            ran = time.monotonic() - started
            failures = 0 if ran >= HEALTHY_RUN_S else failures + 1
            delay = BACKOFF_S[min(failures, len(BACKOFF_S) - 1)] if failures else 2
            _log(f"engine exited (code {code}) after {ran:.0f}s — restarting in {delay}s")
            for _ in range(delay * 10):
                if _stopping:
                    break
                time.sleep(0.1)
    finally:
        if _child and _child.poll() is None:
            _child.terminate()
            try:
                _child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                _child.kill()
        try:
            if PID_FILE.read_text().strip() == str(os.getpid()):
                PID_FILE.unlink()
        except OSError:
            pass
        _log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
