@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0papertrail.ps1" %*
exit /b %ERRORLEVEL%
