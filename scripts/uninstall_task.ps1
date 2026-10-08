#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$TaskName = 'okx-reversal-sync'
)

$ErrorActionPreference = 'Stop'

if (-not (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) {
    Write-Host "Task '$TaskName' is not registered."
    exit 0
}

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "Removed task '$TaskName'. Data (data\) and logs (logs\) were kept." -ForegroundColor Green
