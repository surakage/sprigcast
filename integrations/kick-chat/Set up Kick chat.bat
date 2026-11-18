@echo off
setlocal
title Set up Kick chat for SprigCast
rem Double-click this once. It puts SprigCast's Kick chat relay online (kickchat.slothbloom.com).
rem Steps: check Node, sign in to Cloudflare if needed, deploy, store the Kick client secret, then
rem open Kick's developer settings and show what to paste there. Safe to run again.

cd /d "%~dp0"

echo.
echo  SprigCast - Kick chat setup
echo  ===========================
echo.

where npx >nul 2>nul
if errorlevel 1 (
  echo  [!] Node.js isn't installed, or npx isn't on the PATH.
  echo      Install the LTS version from https://nodejs.org , then double-click this file again.
  start "" "https://nodejs.org/"
  goto failed
)

echo  1/3  Checking your Cloudflare sign-in...
call npx --yes wrangler whoami 2>nul | findstr /i /c:"You are logged in" >nul
if errorlevel 1 (
  echo       Not signed in yet: a browser window opens. Sign in with the account that has slothbloom.com.
  call npx --yes wrangler login
  if errorlevel 1 (
    echo  [!] Cloudflare sign-in didn't finish.
    goto failed
  )
)

echo.
echo  2/3  Putting the Kick chat relay online...
call npx --yes wrangler deploy
if errorlevel 1 (
  echo  [!] Deploying didn't work. The message above says why.
  goto failed
)

echo.
echo  3/3  Kick client secret
echo       Paste the Client Secret of your SprigCast app from kick.com/settings/developer
echo       (the same one the sign-in helper uses). It won't show while you paste. Then press Enter.
call npx --yes wrangler secret put KICK_CLIENT_SECRET
if errorlevel 1 (
  echo  [!] The secret wasn't saved. Run this file again to retry.
  goto failed
)

echo.
echo  Done with Cloudflare. Last step, on Kick (opening it now):
echo.
echo    kick.com  ^>  Settings  ^>  Developer  ^>  your SprigCast app  ^>  Edit
echo      - Enable webhooks: ON
echo      - Webhook URL:     https://kickchat.slothbloom.com/v1/kick/webhook
echo      - Scopes: make sure  events:subscribe  is ticked
echo      - Save
echo.
echo  Then check https://kickchat.slothbloom.com/v1/health says {"ok":true},
echo  and add a Kick name in SprigCast: Chat  ^>  Channels...
start "" "https://kick.com/settings/developer"
echo.
pause
exit /b 0

:failed
echo.
echo  Setup stopped. Nothing else was changed.
pause
exit /b 1
