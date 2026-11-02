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

# Runs a program, streams every line it prints to the log, and fails with ITS OWN words (the last few lines).
# Windows PowerShell 5.1 turns anything a native tool writes to stderr into a terminating error while
# $ErrorActionPreference is "Stop", and uv writes harmless warnings there. So stderr is read under "Continue"
# and only the exit code decides whether the step failed.
function Run([string]$exe, [string[]]$argv) {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $lines = New-Object System.Collections.Generic.List[string]
    try {
        & $exe @argv 2>&1 | ForEach-Object {
            $line = "$_"
            $lines.Add($line)
            Write-Output $line
        }
        $code = $LASTEXITCODE
    }
    finally { $ErrorActionPreference = $prev }
    if ($code -ne 0) {
        $why = ($lines | Where-Object { $_.Trim() } | Select-Object -Last 4) -join " / "
        throw "$([IO.Path]::GetFileName($exe)) failed (exit $code): $why"
    }
}

# True when this Python really starts. A Python environment left half-built by an earlier attempt, or whose
# base Python was removed, still has a python.exe on disk but can't run, and uv then refuses to use it.
function Test-Python([string]$exe) {
    if (-not (Test-Path $exe)) { return $false }
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $out = & $exe -c "import sys; print('py' + str(sys.version_info[0]))" 2>&1
        return (($LASTEXITCODE -eq 0) -and ("$out".Trim() -eq "py3"))
    }
    catch { return $false }
    finally { $ErrorActionPreference = $prev }
}

# Why a Python won't start, in the operating system's own words (e.g. "blocked by an Application Control policy").
function Get-PythonError([string]$exe) {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $text = (& $exe -c "pass" 2>&1 | Out-String).Trim()
        if (-not $text) { $text = "it exited without a message" }
    }
    catch { $text = $_.Exception.Message }
    finally { $ErrorActionPreference = $prev }
    $text = ($text -replace "\s+", " ")
    return $text.Substring(0, [Math]::Min(200, $text.Length))
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
    # Jarvis's own Python 3.11, never whichever Python happens to be installed on this PC: that one can be
    # uninstalled later, or be the Microsoft Store stub.
    $env:UV_PYTHON_PREFERENCE = "only-managed"

    $venv = Join-Path $Runtime "venv"
    $py = Join-Path $venv "Scripts\python.exe"
    if (-not (Test-Python $py)) {
        if (Test-Path $venv) {
            Write-Output "The Python environment from an earlier attempt can't start. Rebuilding it."
            Remove-Item -Recurse -Force $venv
        }
        Step 2 "Installing a private copy of Python 3.11"
        Run $uv @("venv", "--python", "3.11", "--seed", $venv)
        if (-not (Test-Python $py)) {
            Write-Output "A fresh Python environment still can't start. Downloading Python again."
            Remove-Item -Recurse -Force $venv, $env:UV_PYTHON_INSTALL_DIR -ErrorAction SilentlyContinue
            Run $uv @("venv", "--python", "3.11", "--seed", $venv)
            if (-not (Test-Python $py)) {
                throw "Jarvis's Python was installed but won't start: $(Get-PythonError $py). Antivirus or Windows Smart App Control may be blocking it; allow $Runtime and try again."
            }
        }
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
    $msg = ($_.Exception.Message -replace "[\r\n]+", " / ")
    Write-Output "@@FAIL $msg"
    exit 1
}
