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

# curl_cffi/libcurl may fail to open a CA bundle whose path contains non-ASCII
# characters. Keep a local copy at a stable ASCII path and explicitly use it.
$CaSource = Join-Path $Root ".venv\Lib\site-packages\certifi\cacert.pem"
$CaDirectory = Join-Path $env:PUBLIC "freeAgentIdentity"
$CaBundle = Join-Path $CaDirectory "cacert.pem"
if (-not (Test-Path -LiteralPath $CaSource)) {
    throw "CA bundle is missing: $CaSource"
}
New-Item -ItemType Directory -Force -Path $CaDirectory | Out-Null
Copy-Item -LiteralPath $CaSource -Destination $CaBundle -Force
$env:CURL_CA_BUNDLE = $CaBundle
$env:REQUESTS_CA_BUNDLE = $CaBundle
$env:SSL_CERT_FILE = $CaBundle

foreach ($path in @($env:LOCALAPPDATA, $env:USERPROFILE, $env:PLAYWRIGHT_BROWSERS_PATH, (Join-Path $Root "data"), (Join-Path $Root "logs"))) {
    New-Item -ItemType Directory -Force -Path $path | Out-Null
}

Set-Location -LiteralPath $Root
Write-Host "Local app: http://127.0.0.1:$Port"
Write-Host "Database:  $env:ACCOUNT_MANAGER_DATABASE_URL"
Write-Host "Python:    $PythonExe"
Write-Host "Node:      $(& $NodeExe --version)"
Write-Host "CA bundle: $CaBundle"
Write-Host "Press Ctrl+C to stop."

& $PythonExe -m uvicorn main:app --host 0.0.0.0 --port $Port
exit $LASTEXITCODE
