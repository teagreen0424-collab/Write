param()

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$backendPython = Join-Path $projectRoot 'backend\.venv\Scripts\python.exe'
$backendCheck = Join-Path $PSScriptRoot 'check_backend.py'
$frontendDirectory = Join-Path $projectRoot 'frontend'

if (-not (Test-Path -LiteralPath $backendPython -PathType Leaf)) {
    throw 'Backend virtual environment is missing. Follow README.md to install it.'
}
if (-not (Get-Command npm.cmd -ErrorAction SilentlyContinue)) {
    throw 'npm.cmd was not found. Install Node.js and reopen PowerShell.'
}
if (-not (Test-Path -LiteralPath (Join-Path $frontendDirectory 'node_modules') -PathType Container)) {
    throw 'Frontend dependencies are missing. Run npm install in frontend first.'
}

Write-Host '[1/2] Backend tests: temporary database, no .env, no external HTTP or models.'
& $backendPython $backendCheck
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host '[2/2] Frontend tests, type checking and build.'
Push-Location -LiteralPath $frontendDirectory
try {
    & npm.cmd run check
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} finally {
    Pop-Location
}

Write-Host '[OK] All local checks passed.'
exit 0
