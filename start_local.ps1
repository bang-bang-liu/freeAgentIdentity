param(
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonExe = Join-Path $Root ".venv\Scripts\python.exe"
$NodeExe = Join-Path $Root ".tools\node\node.exe"
$NpmExe = Join-Path $Root ".tools\node\npm.cmd"

if (-not (Test-Path -LiteralPath $PythonExe)) {
    throw "Python environment is missing: $PythonExe"
}
if (-not (Test-Path -LiteralPath $NodeExe)) {
    throw "Local Node.js runtime is missing: $NodeExe"
}

$Runtime = Join-Path $Root ".runtime"
$env:LOCALAPPDATA = Join-Path $Runtime "appdata"
$env:APPDATA = Join-Path $Runtime "appdata"
$env:USERPROFILE = Join-Path $Runtime "home"
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $Runtime "browsers"
$env:PATCHRIGHT_BROWSERS_PATH = $env:PLAYWRIGHT_BROWSERS_PATH
$env:CAMOUFOX_HOME = Join-Path $Runtime "appdata\camoufox"
$env:PYTHONUTF8 = "1"
$env:ACCOUNT_MANAGER_DATABASE_URL = "sqlite:///" + ((Join-Path $Root "data\account_manager.db") -replace "\\", "/")

foreach ($path in @($env:LOCALAPPDATA, $env:USERPROFILE, $env:PLAYWRIGHT_BROWSERS_PATH, (Join-Path $Root "data"), (Join-Path $Root "logs"))) {
    New-Item -ItemType Directory -Force -Path $path | Out-Null
}

Set-Location -LiteralPath $Root
Write-Host "Local app: http://127.0.0.1:$Port"
Write-Host "Database:  $env:ACCOUNT_MANAGER_DATABASE_URL"
Write-Host "Python:    $PythonExe"
Write-Host "Node:      $(& $NodeExe --version)"
Write-Host "CA bundle: certifi/default system configuration"
Write-Host "Press Ctrl+C to stop."

& $PythonExe -m uvicorn main:app --host 0.0.0.0 --port $Port
exit $LASTEXITCODE
