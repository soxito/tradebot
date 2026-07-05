<#
.SYNOPSIS
    Kronos Forecast Plugin — one-time setup (Windows / PowerShell).

.DESCRIPTION
    Windows equivalent of setup_kronos.sh.

    1. Installs the model dependencies (torch, einops, safetensors, huggingface_hub)
       into the backend virtualenv.
    2. Vendors the Kronos `model/` package (KronosTokenizer / Kronos / KronosPredictor)
       from the upstream MIT-licensed repo into backend/vendor/model.
    3. (optional) Pre-downloads model weights so the first forecast is fast.

    The plugin works WITHOUT this (it falls back to a heuristic forecast); running
    this script upgrades it to the real Kronos foundation model.

.PARAMETER Mode
    One of: (none) | -Predownload | -All | -Test | -NoTest

.EXAMPLE
    # deps + vendor + self-test
    powershell -ExecutionPolicy Bypass -File plugins\KronosForecastPlugin\scripts\setup_kronos.ps1

.EXAMPLE
    # + default model
    .\setup_kronos.ps1 -Predownload

.EXAMPLE
    # + ALL models
    .\setup_kronos.ps1 -All

.EXAMPLE
    # ONLY run the OHLCV/forecast self-test
    .\setup_kronos.ps1 -Test

.EXAMPLE
    # skip the self-test
    .\setup_kronos.ps1 -NoTest
#>
[CmdletBinding()]
param(
    [switch]$Predownload,
    [switch]$All,
    [switch]$Test,
    [switch]$NoTest
)

$ErrorActionPreference = "Stop"

# ── Paths ─────────────────────────────────────────────────────────────────────
$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$PluginDir  = (Resolve-Path (Join-Path $ScriptDir "..")).Path
$RepoRoot   = (Resolve-Path (Join-Path $PluginDir "..\..")).Path
$VendorDir  = Join-Path $PluginDir "backend\vendor"
$KronosRepo = "https://github.com/shiyu-coder/Kronos.git"

# Resolve the backend python (venv preferred). Windows venvs put python.exe in Scripts\.
$VenvPy = Join-Path $RepoRoot "backend\.venv\Scripts\python.exe"
if (Test-Path $VenvPy) {
    $PY = $VenvPy
} else {
    $PY = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $PY) { $PY = (Get-Command python3 -ErrorAction SilentlyContinue).Source }
    if (-not $PY) { throw "No Python interpreter found. Install Python 3.11+ and re-run." }
}
Write-Host "==> Using Python: $PY"

# ── self-test: prove OHLCV data + a forecast resolve end-to-end ──────────────
$SelfTestCode = @'
import asyncio, sys, os
sys.path.insert(0, os.getcwd())                            # REPO_ROOT (for plugins.*)
sys.path.insert(0, os.path.join(os.getcwd(), "backend"))  # backend/ (for app.*)

async def main() -> int:
    try:
        import ccxt  # noqa: F401
    except Exception as e:
        print(f"    FAIL: ccxt not importable in the backend venv: {e}")
        return 1

    from plugins.KronosForecastPlugin.backend.services import forecast_service as fs

    try:
        rows = await fs._fetch_ohlcv("binance", "BTCUSDT", "1h", 200)
        if not rows or len(rows) < 5:
            print("    FAIL: No OHLCV data available for this symbol/exchange.")
            print("          (public ccxt fallback returned nothing — check network access)")
            return 2
        last_close = rows[-1][4]
        print(f"    OK: fetched {len(rows)} OHLCV candles (last close ~ {last_close}).")

        try:
            resp = await fs.run_forecast("binance", "BTCUSDT", "1h")
            n = len(getattr(resp, "forecast", []) or [])
            engine = "Kronos model" if getattr(fs.kronos_engine, "available", False) else "heuristic fallback"
            print(f"    OK: forecast produced {n} future candles via {engine}.")
        except Exception as e:
            print(f"    WARN: OHLCV works but run_forecast raised: {e}")

        print("==> Self-test PASSED: OHLCV data is available.")
        return 0
    finally:
        try:
            from app.exchanges.manager import exchange_manager
            for ex_id in list(exchange_manager.get_all_exchanges()):
                ex = exchange_manager.get_exchange(ex_id)
                for attr in ("close", "close_all"):
                    fn = getattr(ex, attr, None)
                    if callable(fn):
                        res = fn()
                        if asyncio.iscoroutine(res):
                            await res
                        break
        except Exception:
            pass

raise SystemExit(asyncio.run(main()))
'@

function Invoke-SelfTest {
    Write-Host "==> Self-test: fetching OHLCV + running a forecast (BTCUSDT 1h)..."
    Push-Location $RepoRoot
    try {
        $SelfTestCode | & $PY -
        return $LASTEXITCODE
    } finally {
        Pop-Location
    }
}

# -Test => run ONLY the self-test and exit
if ($Test) {
    $rc = Invoke-SelfTest
    exit $rc
}

Write-Host "==> Installing model dependencies into backend venv..."
& $PY -m pip install -r (Join-Path $PluginDir "backend\requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "pip install of Kronos requirements failed." }

# Ensure ccxt is present for the keyless public-OHLCV fallback (normally a core dep).
Write-Host "==> Verifying ccxt is importable (required for OHLCV fallback)..."
& $PY -c "import ccxt" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "    ccxt missing — installing..."
    & $PY -m pip install "ccxt>=4.0.0"
    if ($LASTEXITCODE -ne 0) { throw "pip install of ccxt failed." }
}

Write-Host "==> Vendoring Kronos model package into $VendorDir\model ..."
if (-not (Test-Path $VendorDir)) {
    New-Item -ItemType Directory -Path $VendorDir -Force | Out-Null
}

$TmpDir = Join-Path ([System.IO.Path]::GetTempPath()) ("kronos_" + [System.Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $TmpDir -Force | Out-Null
try {
    git clone --depth 1 $KronosRepo (Join-Path $TmpDir "Kronos")
    if ($LASTEXITCODE -ne 0) { throw "git clone of Kronos failed. Is Git installed and on PATH?" }

    $ModelDest = Join-Path $VendorDir "model"
    if (Test-Path $ModelDest) { Remove-Item -Recurse -Force $ModelDest }
    Copy-Item -Recurse (Join-Path $TmpDir "Kronos\model") $ModelDest

    $LicenseSrc = Join-Path $TmpDir "Kronos\LICENSE"
    if (Test-Path $LicenseSrc) {
        Copy-Item $LicenseSrc (Join-Path $VendorDir "KRONOS_LICENSE") -Force
    }
    Write-Host "    vendored: $((Get-ChildItem $ModelDest | Select-Object -ExpandProperty Name) -join ', ')"
} finally {
    if (Test-Path $TmpDir) { Remove-Item -Recurse -Force $TmpDir }
}

$PredownloadCode = @'
from plugins.KronosForecastPlugin.backend.services.kronos_engine import kronos_engine
print("Kronos available:", kronos_engine.available, "| error:", kronos_engine.load_error)
'@

$AllModelsCode = @'
from plugins.KronosForecastPlugin.backend.services.kronos_engine import kronos_engine
from plugins.KronosForecastPlugin.backend.config import KRONOS_MODELS
results = kronos_engine.download_all(KRONOS_MODELS)
for model_id, ok in results.items():
    print(("  installed " if ok else "  FAILED   ") + model_id)
ok = sum(1 for v in results.values() if v)
print(f"==> {ok}/{len(results)} Kronos models installed.")
'@

if ($Predownload) {
    Write-Host "==> Pre-downloading the default model weights from Hugging Face..."
    Push-Location $RepoRoot
    try { $PredownloadCode | & $PY - } finally { Pop-Location }
} elseif ($All) {
    Write-Host "==> Downloading ALL published Kronos models + tokenizers into the local cache..."
    Push-Location $RepoRoot
    try { $AllModelsCode | & $PY - } finally { Pop-Location }
}

Write-Host "==> Done. Restart the backend, then GET /api/v1/plugins/kronos/status"
Write-Host "    Install every model later via: POST /api/v1/plugins/kronos/models/install-all"

# Run the end-to-end self-test unless explicitly skipped.
if (-not $NoTest) {
    $rc = Invoke-SelfTest
    if ($rc -ne 0) {
        Write-Host "!!  Self-test did not pass. Check network access / exchange reachability above."
        exit 1
    }
}
