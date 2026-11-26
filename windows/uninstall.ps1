# Removes Claude Pulse from Windows. Settings and history are kept unless you pass -Purge.
param([switch]$Purge)
$ErrorActionPreference = 'SilentlyContinue'

Get-CimInstance Win32_Process |
    Where-Object { $_.Name -like 'python*.exe' -and $_.CommandLine -like '*ClaudePulse*server.py*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }

Remove-Item -Force (Join-Path ([Environment]::GetFolderPath('Desktop')) 'Claude Pulse.lnk')
Remove-Item -Force (Join-Path ([Environment]::GetFolderPath('Programs')) 'Claude Pulse.lnk')
Remove-Item -Force (Join-Path ([Environment]::GetFolderPath('Startup')) 'Claude Pulse (background).lnk')

Start-Sleep -Milliseconds 500
Remove-Item -Recurse -Force (Join-Path $env:LOCALAPPDATA 'ClaudePulse\app')

if ($Purge) {
    Remove-Item -Recurse -Force (Join-Path $env:LOCALAPPDATA 'ClaudePulse')
    Remove-Item -Recurse -Force (Join-Path $env:APPDATA 'ClaudePulse')
    Write-Host 'Claude Pulse removed, including settings and history.'
} else {
    Write-Host 'Claude Pulse removed. Settings (%APPDATA%\ClaudePulse) and history (%LOCALAPPDATA%\ClaudePulse\data) were kept; run with -Purge to delete them.'
}
