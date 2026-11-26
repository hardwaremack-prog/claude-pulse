# One-line installer / updater for Claude Pulse on Windows. In PowerShell, run:
#   irm https://raw.githubusercontent.com/hardwaremack-prog/claude-pulse/main/windows/get.ps1 | iex
# Run it again any time to update. Your settings and history are kept.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # makes Invoke-WebRequest much faster on Windows PowerShell 5.1
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$repo   = 'hardwaremack-prog/claude-pulse'
$branch = 'main'
$tmp = Join-Path $env:TEMP ('claude-pulse-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $tmp | Out-Null
try {
    Write-Host "==> Downloading Claude Pulse ($repo@$branch)" -ForegroundColor Cyan
    $zip = Join-Path $tmp 'src.zip'
    Invoke-WebRequest -UseBasicParsing "https://codeload.github.com/$repo/zip/refs/heads/$branch" -OutFile $zip
    Expand-Archive -Path $zip -DestinationPath $tmp -Force
    $root = Get-ChildItem $tmp -Directory | Where-Object { $_.Name -like 'claude-pulse-*' } | Select-Object -First 1
    if (-not $root) { throw 'The download looks wrong (no claude-pulse folder inside).' }
    # A separate PowerShell with -ExecutionPolicy Bypass, so the default "scripts disabled" policy doesn't block it.
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root.FullName 'windows\install.ps1') -Source $root.FullName
} finally {
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
}
