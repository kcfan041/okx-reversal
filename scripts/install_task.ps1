#Requires -Version 5.1
<#
.SYNOPSIS
  Register the okx-reversal-sync scheduled task (every 5 minutes, low priority, S4U).
#>
[CmdletBinding()]
param(
    [string]$TaskName = 'okx-reversal-sync',
    [int]$IntervalMinutes = 5
)

$ErrorActionPreference = 'Stop'

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Pythonw = Join-Path $RepoRoot '.venv\Scripts\pythonw.exe'

if (-not (Test-Path -LiteralPath $Pythonw)) {
    Write-Host "Not found: $Pythonw" -ForegroundColor Red
    Write-Host "Create the virtual environment first:  cd `"$RepoRoot`"; uv sync"
    exit 1
}

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)

$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name

$action = New-ScheduledTaskAction -Execute $Pythonw -Argument '-m okxrev.sync' -WorkingDirectory $RepoRoot
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -Priority 7
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType S4U -RunLevel Limited

try {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
        -Principal $principal -Description 'OKX USDT-SWAP 5m candle sync (okx-reversal)' -Force | Out-Null
}
catch {
    Write-Host "Failed to register task: $($_.Exception.Message)" -ForegroundColor Red
    if (-not $isAdmin) {
        Write-Host 'S4U tasks usually require an elevated PowerShell. Re-run this script as Administrator:' -ForegroundColor Yellow
        Write-Host "  powershell -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    }
    exit 1
}

$info = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Host "Registered task : $TaskName" -ForegroundColor Green
Write-Host "Runs as         : $user (S4U, runs whether logged on or not)"
Write-Host "Command         : $Pythonw -m okxrev.sync"
Write-Host "Working dir     : $RepoRoot"
Write-Host "Next run time   : $($info.NextRunTime)"
Write-Host ''
Write-Host 'Watch log       : Get-Content logs\sync.log -Tail 20 -Wait'
Write-Host 'Status          : uv run python -m okxrev.sync --status'
Write-Host "Run now         : Start-ScheduledTask -TaskName $TaskName"
Write-Host "Remove          : powershell -ExecutionPolicy Bypass -File scripts\uninstall_task.ps1"
