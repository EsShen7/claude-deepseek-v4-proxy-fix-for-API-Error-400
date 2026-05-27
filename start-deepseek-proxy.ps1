param(
    [string]$PythonPath = ""
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Script = Join-Path $Root "deepseek_anthropic_proxy.py"
$PidFile = Join-Path $Root "deepseek-proxy.pid"
$OutLog = Join-Path $Root "deepseek-proxy.out.log"
$ErrLog = Join-Path $Root "deepseek-proxy.err.log"

if ($PythonPath) {
    $Python = $PythonPath
} else {
    $PythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $PythonCommand) {
        $PythonCommand = Get-Command python3 -ErrorAction SilentlyContinue
    }
    if (-not $PythonCommand) {
        Write-Error "Python was not found. Install Python 3 or run: .\start-deepseek-proxy.ps1 -PythonPath C:\path\to\python.exe"
    }
    $Python = $PythonCommand.Source
}

if (Test-Path -LiteralPath $PidFile) {
    $ExistingPid = Get-Content -Raw -LiteralPath $PidFile
    if ($ExistingPid -match '^\s*\d+\s*$') {
        $Existing = Get-Process -Id ([int]$ExistingPid) -ErrorAction SilentlyContinue
        if ($Existing) {
            Write-Host "DeepSeek proxy already running on PID $ExistingPid"
            exit 0
        }
    }
}

$Process = Start-Process `
    -FilePath $Python `
    -ArgumentList @($Script, "--host", "127.0.0.1", "--port", "8765") `
    -WorkingDirectory $Root `
    -WindowStyle Hidden `
    -RedirectStandardOutput $OutLog `
    -RedirectStandardError $ErrLog `
    -PassThru

$Process.Id | Set-Content -LiteralPath $PidFile
Start-Sleep -Seconds 1
$Process.Refresh()

if ($Process.HasExited) {
    Write-Error "DeepSeek proxy failed to start. Check $ErrLog"
}

Write-Host "DeepSeek proxy started on http://127.0.0.1:8765/anthropic with PID $($Process.Id)"
