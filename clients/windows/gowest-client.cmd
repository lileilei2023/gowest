@echo off
rem gowest Windows client helper launcher (ASCII only; the .ps1 is UTF-8 with BOM).
rem ExecutionPolicy Bypass applies to this one process only; no system policy is changed.
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0gowest-client.ps1" %*
set "rc=%ERRORLEVEL%"
echo.
pause
exit /b %rc%
