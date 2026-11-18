@echo off
rem Double-click after regenerating SprigCast's Kick client secret on kick.com.
rem Asks for the new secret once (hidden) and stores it in both Workers that use it:
rem the sign-in helper (integrations\signin-helper) and the Kick chat relay (integrations\kick-chat).
title Update the Kick secret for SprigCast
cd /d "%~dp0"
where npx >nul 2>nul
if errorlevel 1 (
  echo  [!] Node.js isn't installed, or npx isn't on the PATH. Install the LTS version from https://nodejs.org
  pause
  exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0update-kick-secret.ps1"
set result=%errorlevel%
echo.
pause
exit /b %result%
