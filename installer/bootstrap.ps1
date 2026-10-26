# Jarvis first-run setup for the installed app (called by the app itself, see frontend/src/runtime.ts).
# Gets a private Python 3.11 with uv, installs Jarvis's packages and the speech model.
# Progress lines: "@@STEP n total text"; result: "@@DONE" or "@@FAIL message".
param(
    [Parameter(Mandatory = $true)][string]$Backend,
    [Parameter(Mandatory = $true)][string]$Runtime,
    [string]$Stamp = ""
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$Total = 5
function Step([int]$n, [string]$text) { Write-Output "@@STEP $n $Total $text" }
function Run([string]$exe, [string[]]$argv) {
    & $exe @argv 2>&1 | ForEach-Object { Write-Output "$_" }
    if ($LASTEXITCODE -ne 0) { throw "$([IO.Path]::GetFileName($exe)) failed (exit $LASTEXITCODE)" }
}

try {
    New-Item -ItemType Directory -Force -Path $Runtime | Out-Null
    $uvDir = Join-Path $Runtime "uv"
    $uv = Join-Path $uvDir "uv.exe"
    if (-not (Test-Path $uv)) {
        Step 1 "Downloading the Python manager"
        $zip = Join-Path $env:TEMP "jarvis-uv-$PID.zip"
        Invoke-WebRequest -UseBasicParsing -Uri "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip" -OutFile $zip
        Expand-Archive -Path $zip -DestinationPath $uvDir -Force
        Remove-Item $zip -Force
    }
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $Runtime "python"
    $env:UV_CACHE_DIR = Join-Path $Runtime "cache"
    $env:UV_NO_PROGRESS = "1"

    $venv = Join-Path $Runtime "venv"
    $py = Join-Path $venv "Scripts\python.exe"
    if (-not (Test-Path $py)) {
        Step 2 "Installing a private copy of Python 3.11"
        Run $uv @("venv", "--python", "3.11", "--seed", $venv)
    }

    Step 3 "Installing Jarvis's packages (the longest step)"
    Run $uv @("pip", "install", "--python", $py, "-r", (Join-Path $Backend "requirements.txt"), "-r", (Join-Path $Backend "requirements-voice.txt"))

    Step 4 "Downloading the speech recognition model"
    Run $py @("-c", "from faster_whisper import WhisperModel; WhisperModel('base.en', device='cpu', compute_type='int8'); print('whisper ready')")

    Step 5 "Finishing up"
    Set-Content -Path (Join-Path $venv "jarvis-requirements.stamp") -Value $Stamp -Encoding ascii
    Write-Output "@@DONE"
    exit 0
}
catch {
    Write-Output "@@FAIL $($_.Exception.Message)"
    exit 1
}
