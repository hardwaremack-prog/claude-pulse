# Claude Pulse installer for Windows 10/11.
# Installs to %LOCALAPPDATA%\ClaudePulse, starts at sign-in (hidden, in the background),
# and adds "Claude Pulse" icons to the desktop and Start menu.
# Usually run by get.ps1 (one-line install) or install.cmd (double-click).
param(
    [string]$Source = (Split-Path -Parent $PSScriptRoot),
    [switch]$NoPrompt
)
$ErrorActionPreference = 'Stop'
$AppName    = 'Claude Pulse'
$InstallDir = Join-Path $env:LOCALAPPDATA 'ClaudePulse\app'
$AppDir     = Join-Path $InstallDir 'claude_pulse'
$ConfDir    = Join-Path $env:APPDATA 'ClaudePulse'
$Conf       = Join-Path $ConfDir 'config.json'

function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }

function Find-Python {
    # Returns the full path of a Python 3.9+ python.exe, or $null.
    $tries = @()
    if (Get-Command py -ErrorAction SilentlyContinue)     { $tries += ,@('py', '-3') }
    if (Get-Command python -ErrorAction SilentlyContinue) { $tries += ,@('python') }
    foreach ($t in $tries) {
        try {
            $exe = $t[0]
            $pyArgs = @($t | Select-Object -Skip 1) + @('-c', 'import sys; print(sys.executable if sys.version_info >= (3, 9) else 0)')
            $out = & $exe @pyArgs 2>$null
            if ($LASTEXITCODE -eq 0 -and $out) {
                $path = ([string]($out | Select-Object -Last 1)).Trim()
                if ($path -and (Test-Path $path)) { return $path }
            }
        } catch { }
    }
    $found = Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe" -ErrorAction SilentlyContinue |
             Sort-Object FullName -Descending | Select-Object -First 1
    if ($found) { return $found.FullName }
    return $null
}

function Stop-Pulse {
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -like 'python*.exe' -and $_.CommandLine -like '*ClaudePulse*server.py*' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

function Read-Secret($prompt) {
    $sec = Read-Host -Prompt $prompt -AsSecureString
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
}

function New-Shortcut($path, $target, $arguments, $icon, $workdir, $desc) {
    $ws = New-Object -ComObject WScript.Shell
    $s = $ws.CreateShortcut($path)
    $s.TargetPath = $target
    $s.Arguments = $arguments
    $s.IconLocation = $icon
    $s.WorkingDirectory = $workdir
    $s.Description = $desc
    $s.Save()
}

Write-Host ""
Write-Host "  $AppName for Windows" -ForegroundColor White
Write-Host ""

if (-not (Test-Path (Join-Path $Source 'claude_pulse\server.py'))) {
    throw "Can't find the app files in '$Source'. Run this from the claude-pulse folder."
}

# --- Python
Write-Step 'Looking for Python 3.9+'
$py = Find-Python
if (-not $py) {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Step 'Installing Python 3.12 with winget (just for you, no admin needed)'
        winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
        $py = Find-Python
    }
    if (-not $py) {
        Write-Host ''
        Write-Host 'Python 3.9 or newer is needed. Install it from https://www.python.org/downloads/' -ForegroundColor Yellow
        Write-Host '(tick "Add python.exe to PATH"), then run this installer again.' -ForegroundColor Yellow
        exit 1
    }
}
$pyw = Join-Path (Split-Path $py) 'pythonw.exe'
if (-not (Test-Path $pyw)) { $pyw = $py }
Write-Host "    using $py"

# Windows has no built-in time-zone database; Python needs the tzdata package.
Write-Step 'Installing time-zone data (tzdata)'
& $py -m pip install --user --quiet --disable-pip-version-check --upgrade tzdata
if ($LASTEXITCODE -ne 0) { Write-Host '    Could not install tzdata. Try:  py -m pip install tzdata' -ForegroundColor Yellow }

# --- app files
Write-Step "Installing to $InstallDir"
Stop-Pulse
Start-Sleep -Milliseconds 500
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
if (Test-Path $AppDir) { Remove-Item -Recurse -Force $AppDir }
Copy-Item -Recurse -Force (Join-Path $Source 'claude_pulse') $InstallDir
foreach ($f in @('README.md', 'config.example.json')) {
    $p = Join-Path $Source $f
    if (Test-Path $p) { Copy-Item -Force $p $InstallDir }
}
Copy-Item -Force (Join-Path $Source 'windows\uninstall.ps1') $InstallDir

# --- config (kept on reinstall)
New-Item -ItemType Directory -Force -Path $ConfDir | Out-Null
if (-not (Test-Path $Conf)) {
    # Same defaults as the Pi version, but only reachable from this PC (no firewall prompt).
    & $py -c "import json,sys; c=json.load(open(sys.argv[1])); c['bind']='127.0.0.1'; json.dump(c,open(sys.argv[2],'w'),indent=2)" (Join-Path $Source 'config.example.json') $Conf
    Write-Step "Wrote default settings to $Conf"
} else {
    Write-Step "Keeping your settings in $Conf"
}

function Test-Key($name) {
    & $py -c "import json,sys; sys.exit(0 if json.load(open(sys.argv[1])).get(sys.argv[2]) else 1)" $Conf $name
    return ($LASTEXITCODE -eq 0)
}
function Save-Key($name, $value) {
    $env:CP_KEY = $value
    & $py -c "import json,os,sys; p=sys.argv[1]; c=json.load(open(p)); c[sys.argv[2]]=os.environ['CP_KEY'].strip(); json.dump(c,open(p,'w'),indent=2)" $Conf $name
    Remove-Item Env:CP_KEY -ErrorAction SilentlyContinue
}

if (-not $NoPrompt) {
    if (-not (Test-Key 'openrouter_api_key')) {
        Write-Host ''
        Write-Host 'Recommended: a FREE OpenRouter key (no credits needed) adds live speed for every Claude model.'
        Write-Host '  Get one at https://openrouter.ai/settings/keys  (sign in, then Create Key)'
        Write-Host 'Paste it and press Enter (it stays hidden), or just press Enter to skip.'
        $k = Read-Secret 'OpenRouter key'
        if ($k) { Save-Key 'openrouter_api_key' $k; Write-Step 'OpenRouter key saved.' }
    }
    if (-not (Test-Key 'anthropic_api_key')) {
        Write-Host ''
        Write-Host 'Optional: an Anthropic API key (sk-ant-...) adds API rate limits and instant new-model alerts.'
        Write-Host 'Most people can skip this. Press Enter to skip.'
        $k = Read-Secret 'Anthropic key'
        if ($k) { Save-Key 'anthropic_api_key' $k; Write-Step 'Anthropic key saved.' }
    }
}

# --- shortcuts
Write-Step 'Adding desktop, Start menu and sign-in shortcuts'
$launch = '"' + (Join-Path $AppDir 'launch.pyw') + '"'
$server = '"' + (Join-Path $AppDir 'server.py') + '"'
$icon   = Join-Path $AppDir 'static\icon.ico'
$desktop = [Environment]::GetFolderPath('Desktop')
New-Shortcut (Join-Path $desktop 'Claude Pulse.lnk') $pyw $launch $icon $AppDir 'Open the Claude Pulse dashboard'
New-Shortcut (Join-Path ([Environment]::GetFolderPath('Programs')) 'Claude Pulse.lnk') $pyw $launch $icon $AppDir 'Open the Claude Pulse dashboard'
New-Shortcut (Join-Path ([Environment]::GetFolderPath('Startup')) 'Claude Pulse (background).lnk') $pyw $server $icon $AppDir 'Collects Claude Pulse data in the background'

# --- start it
Write-Step 'Starting Claude Pulse in the background'
Start-Process -FilePath $pyw -ArgumentList $server -WorkingDirectory $AppDir -WindowStyle Hidden
$port = (& $py -c "import json,sys; print(json.load(open(sys.argv[1])).get('port',8787))" $Conf | Select-Object -Last 1)
$ok = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Milliseconds 500
    try {
        $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 "http://127.0.0.1:$port/api/health"
        if ($r.StatusCode -eq 200) { $ok = $true; break }
    } catch { }
}

Write-Host ''
if ($ok) {
    Write-Host "  $AppName is running:  http://localhost:$port" -ForegroundColor Green
    Write-Host '  Opening the dashboard. Next time, use the Claude Pulse icon on your desktop.'
    Start-Process -FilePath $pyw -ArgumentList $launch -WorkingDirectory $AppDir
} else {
    Write-Host "  The background service didn't answer yet. Try the desktop icon in a moment." -ForegroundColor Yellow
    Write-Host "  Log: $env:LOCALAPPDATA\ClaudePulse\data\claude-pulse.log"
}
Write-Host "  Settings: $Conf (changes are picked up within a minute)"
Write-Host "  Uninstall: powershell -ExecutionPolicy Bypass -File `"$InstallDir\uninstall.ps1`""
Write-Host ''
