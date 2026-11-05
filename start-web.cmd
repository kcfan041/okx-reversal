@echo off
rem Double-click to (re)start the web UI: closes an older okx-reversal web server on the same port, then starts a
rem fresh one and opens the browser. Extra arguments go to okxrev.web, e.g.  start-web.cmd --port 9000 --no-browser
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PORT=8765"
:args
if "%~1"=="" goto stop
if /i "%~1"=="--port" set "PORT=%~2"
shift
goto args

:stop
powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue | ForEach-Object { $p = Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue; if ($p -and $p.ProcessName -like 'python*') { Stop-Process -Id $p.Id -Force; Write-Host ('stopped old web server, PID ' + $p.Id); Start-Sleep -Seconds 1 } }"

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -m okxrev.web %*
) else (
    uv run python -m okxrev.web %*
)
if errorlevel 1 pause
