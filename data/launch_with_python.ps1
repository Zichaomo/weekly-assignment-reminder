param([Parameter(Mandatory)][ValidateSet('console', 'setup', 'login')][string]$Launcher)

function Test-PythonPath([string]$Path) {
    return $Path -match '^[a-z]:[\\/](?:[^:"%<>|?*\x00-\x1f]+[\\/])?python\.exe$' -and
        $Path -notmatch '[\\/]WindowsApps[\\/]' -and
        (Test-Path -LiteralPath $Path -PathType Leaf)
}

$configPath = $env:HOMEWORK_CONFIG_PATH
if (-not $configPath) { $configPath = Join-Path $PSScriptRoot 'config.json' }
$python = $null
try {
    $python = (Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 -ErrorAction Stop |
        ConvertFrom-Json -ErrorAction Stop).python_path
} catch {
    # A fresh checkout has no settings file yet.
}
if (-not (Test-PythonPath $python)) {
    $python = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python314\python.exe'
}
if (-not (Test-PythonPath $python)) {
    $python = Get-Command python.exe -CommandType Application -All -ErrorAction SilentlyContinue |
        Where-Object { Test-PythonPath $_.Source } |
        Select-Object -First 1 -ExpandProperty Source
}
if (-not (Test-PythonPath $python)) {
    Write-Host 'Python was not detected. Install Python 3.10 or newer, or enter an existing interpreter.'
    $python = (Read-Host 'Full path to python.exe (Enter to cancel)').Trim().Trim('"')
    if (-not (Test-PythonPath $python)) { exit 1 }
}

# Keep the path Unicode throughout. The child batch file reads it from the environment.
$env:HOMEWORK_LAUNCH_PYTHON = $python
& (Join-Path (Split-Path -Parent $PSScriptRoot) "$Launcher.bat")
exit $LASTEXITCODE
