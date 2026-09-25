@echo off
rem PDFMathTranslate-Custom one-click deploy launcher (ASCII stub).
rem All logic lives in setup_windows.ps1 to avoid codepage issues.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_windows.ps1"
if errorlevel 1 pause
