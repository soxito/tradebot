# Soup Plugin — installer (Windows)
# Mirrors setup_soup.sh for PowerShell 5.1+ / pwsh 7+
param(
  [switch]$Force
)

$ErrorActionPreference = "Stop"
$PluginDir = Split-Path -Parent $PSScriptRoot
$RepoRoot  = Resolve-Path (Join-Path $PluginDir "..\..")
$SoupRepo  = "https://github.com/MakazhanAlpamys/Soup.git"
$SoupDir   = Join-Path $RepoRoot "integrations\soup"
$SoupVersionFile = Join-Path $RepoRoot "integrations\soup.version.json"
$SoupVenv  = if ($env:TRADEBOT_SOUP_VENV) { $env:TRADEBOT_SOUP_VENV } else { Join-Path $env:USERPROFILE ".tradebot\soup-venv" }
$SoupPy    = Join-Path $SoupVenv "Scripts\python.exe"
$SoupPip   = Join-Path $SoupVenv "Scripts\pip.exe"
$SoupBin   = Join-Path $SoupVenv "Scripts\soup.exe"

Write-Host "==> Soup setup — repo=$SoupRepo  dir=$SoupDir  venv=$SoupVenv" -ForegroundColor Cyan

if (-not $Force -and (Test-Path $SoupBin) -and (Test-Path (Join-Path $SoupDir ".git"))) {
  Write-Host "==> soup already installed at $SoupVenv — skipping (use -Force to reinstall)." -ForegroundColor Green
  exit 0
}

# Find Python 3.11/3.12
$basePy = $null
foreach ($c in @("python3.12","python3.11","python","py")) {
  try {
    $p = (Get-Command $c -ErrorAction SilentlyContinue).Source
    if ($p) {
      $ok = & $p -c "import sys; assert 10 <= sys.version_info.minor <= 12" 2>$null
      if ($LASTEXITCODE -eq 0) { $basePy = $p; break }
    }
  } catch {}
}
if (-not $basePy) { $basePy = "python" }
Write-Host "==> Using base Python: $basePy" -ForegroundColor Cyan

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
  Write-Host "!! git not found — install git to enable Soup. Skipping." -ForegroundColor Yellow
  exit 0
}

New-Item -ItemType Directory -Force -Path (Split-Path $SoupDir) | Out-Null
if (Test-Path (Join-Path $SoupDir ".git")) {
  Write-Host "==> Updating existing soup repo …" -ForegroundColor Cyan
  & git -C $SoupDir fetch --depth 1 origin 2>&1 | Select-Object -First 20
  & git -C $SoupDir pull --ff-only 2>&1 | Select-Object -First 20
} else {
  Write-Host "==> Cloning Soup → $SoupDir …" -ForegroundColor Cyan
  if (Test-Path $SoupDir) { Remove-Item -Recurse -Force $SoupDir }
  & git clone --depth 1 $SoupRepo $SoupDir
  if ($LASTEXITCODE -ne 0) { Write-Host "!! clone failed." -ForegroundColor Red; exit 0 }
}

if (-not (Test-Path $SoupPy)) {
  Write-Host "==> Creating Soup venv at $SoupVenv with $basePy …" -ForegroundColor Cyan
  New-Item -ItemType Directory -Force -Path (Split-Path $SoupVenv) | Out-Null
  & $basePy -m venv $SoupVenv
  if ($LASTEXITCODE -ne 0) { Write-Host "!! venv creation failed." -ForegroundColor Red; exit 0 }
}

Write-Host "==> Upgrading pip …" -ForegroundColor Cyan
& $SoupPip install --quiet --upgrade pip 2>&1 | Select-Object -Last 5

Write-Host "==> Installing soup-cli[train,ui,serve,fast] …" -ForegroundColor Cyan
& $SoupPip install --quiet --prefer-binary --upgrade "soup-cli[train,ui,serve,fast]" 2>&1 | Select-Object -Last 20
if ($LASTEXITCODE -ne 0) {
  Write-Host "!! full extras failed — trying minimal soup-cli …" -ForegroundColor Yellow
  & $SoupPip install --quiet --prefer-binary --upgrade "soup-cli" 2>&1 | Select-Object -Last 20
}

if (Test-Path $SoupBin) {
  Write-Host "==> soup binary: $SoupBin" -ForegroundColor Green
  & $SoupBin --version 2>&1 | Select-Object -First 5
}
try {
  $sha = & git -C $SoupDir rev-parse HEAD 2>$null
  $ts = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
  @{commit=$sha; installed_at=$ts; repo=$SoupRepo; venv=$SoupVenv} | ConvertTo-Json | Set-Content $SoupVersionFile -Encoding utf8
  Write-Host "==> Wrote $SoupVersionFile" -ForegroundColor Green
} catch {}
Write-Host "==> Done. Soup available via: $SoupBin" -ForegroundColor Green
