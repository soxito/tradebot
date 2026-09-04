<#
.SYNOPSIS
    Kronos Forecast Plugin - fine-tuning setup (Windows / PowerShell).

.DESCRIPTION
    Windows port of setup_kronos_finetune.sh. Vendors the upstream finetune_csv
    pipeline (shiyu-coder/Kronos, MIT) into backend\vendor\finetune_csv so the
    plugin can fine-tune Kronos on your own OHLCV data. Idempotent (use -Force
    to re-vendor). setup_kronos.ps1 is a prerequisite (installs torch + model/).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File plugins\KronosForecastPlugin\scripts\setup_kronos_finetune.ps1

.EXAMPLE
    .\setup_kronos_finetune.ps1 -Force
#>
[CmdletBinding()]
param([switch]$Force)

$ErrorActionPreference = "Stop"

$ScriptDir    = Split-Path -Parent $MyInvocation.MyCommand.Path
$PluginDir    = (Resolve-Path (Join-Path $ScriptDir "..")).Path
$RepoRoot     = (Resolve-Path (Join-Path $PluginDir "..\..")).Path
$VendorDir    = Join-Path $PluginDir "backend\vendor"
$FinetuneDest = Join-Path $VendorDir "finetune_csv"
$KronosRepo   = "https://github.com/shiyu-coder/Kronos.git"

$VenvPy = Join-Path $RepoRoot "backend\.venv\Scripts\python.exe"
if (Test-Path $VenvPy) {
    $PY = $VenvPy
} else {
    $PY = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $PY) { $PY = (Get-Command python3 -ErrorAction SilentlyContinue).Source }
    if (-not $PY) { throw "No Python interpreter found. Install Python 3.11+ and re-run." }
}
Write-Host "==> Using Python: $PY"

if ((-not $Force) -and (Test-Path (Join-Path $FinetuneDest "train_sequential.py"))) {
    Write-Host "==> finetune_csv already vendored at $FinetuneDest - skipping (use -Force to re-vendor)."
    exit 0
}

$GitCmd = Get-Command git -ErrorAction SilentlyContinue
if (-not $GitCmd) {
    throw "Git not found on PATH. Install Git from https://git-scm.com/download/win and re-run."
}

Write-Host "==> Ensuring PyYAML is installed (config loader dependency)..."
& $PY -c "import yaml" 2>$null
if ($LASTEXITCODE -ne 0) { & $PY -m pip install pyyaml }

Write-Host "==> Vendoring Kronos finetune_csv pipeline into $FinetuneDest ..."
if (-not (Test-Path $VendorDir)) { New-Item -ItemType Directory -Path $VendorDir -Force | Out-Null }

$TmpDir = Join-Path ([System.IO.Path]::GetTempPath()) ("kronosft_" + [System.Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $TmpDir -Force | Out-Null
try {
    git clone --depth 1 $KronosRepo (Join-Path $TmpDir "Kronos")
    if ($LASTEXITCODE -ne 0) { throw "git clone of Kronos failed." }

    if (Test-Path $FinetuneDest) { Remove-Item -Recurse -Force $FinetuneDest }
    Copy-Item -Recurse (Join-Path $TmpDir "Kronos\finetune_csv") $FinetuneDest
    foreach ($light in @("examples", "data")) {
        $p = Join-Path $FinetuneDest $light
        if (Test-Path $p) { Remove-Item -Recurse -Force $p }
    }
    Write-Host "    vendored: $((Get-ChildItem $FinetuneDest | Select-Object -ExpandProperty Name) -join ', ')"
} finally {
    if (Test-Path $TmpDir) { Remove-Item -Recurse -Force $TmpDir }
}

Write-Host "==> Done. Fine-tuning is available via the Kronos page 'Finetune' tab and"
Write-Host "    POST /api/v1/plugins/kronos/finetune/start."
