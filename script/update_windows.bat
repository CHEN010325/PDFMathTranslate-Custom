@echo off
rem Update main, redeploy and start the workbench. Do not discard local files.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0update_windows.ps1" %*
if errorlevel 1 (
    pause
    exit /b 1
)
