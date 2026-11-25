# SprigCast user guide

From install to your first stream, and what to do when something goes wrong.

## 1. Install

1. Download `SprigCast-Setup-<version>.exe` from the
   [SprigCast website](https://sprigcast.slothbloom.com).
2. Run it. SprigCast isn't signed yet, so Windows may say "Windows protected your PC". Choose **More info**,
   then **Run anyway**.
3. Setup installs for your Windows account. Leave **SprigCast Camera** ticked if you want SprigCast as a
   camera in other apps (TikTok LIVE Studio, Discord, Zoom); Windows asks your permission once for it.

You need Windows 10 (1809 or newer) or Windows 11, 64-bit, and a graphics card with OpenGL 3.3.

## 2. First start

SprigCast asks a few quick questions (what you stream, where to) and checks your PC. It then suggests
settings that fit, such as 720p30 on an older PC. You can run it again from **Tools → Get started**.

SprigCast has two screens:

- **Live** (Ctrl+1) is for running the show: the stage, your blooms, sound, chat and **Go live**.
- **Build** (Ctrl+2) is for making blooms: add a camera, a game, a picture, a web page and arrange them.

A **bloom** is a scene (Starting soon, Gaming, Be right back). A **sprig** is a part you reuse in many
blooms, like your camera frame. SprigCast has a horizontal and a vertical canvas, and each place you stream
to uses one of them.

## 3. Add the places you stream to

Open **Settings → Stream** and add a place for each platform.

- **Twitch, YouTube and Kick:** press **Sign in** and SprigCast fills in the stream key for you. YouTube's
  sign-in may show "Google hasn't verified this app" while Google reviews SprigCast. Choose **Advanced**, then
  **Go to SprigCast**, or paste your key instead.
- **Everyone else (TikTok, Facebook, others):** paste the server and stream key from the platform's own page.

Stream keys are kept in Windows Credential Manager, never in a file, and never in the logs.

## 4. Go live

1. On **Live**, pick your first bloom.
2. Press **Go live**. SprigCast first checks it can reach every place, then starts them all.
3. The **Stream doctor** card says how each place is doing. If your PC gets busy it names the apps using it.
4. Press **End stream** when you're done. A short summary of each place is shown and saved.

## 5. When something goes wrong

- **Help → Report a problem** sends a report to the SprigCast developer: what you write, plus (if you leave it ticked) your logs and PC
  details. Stream keys, passwords, your name, your PC's name, e-mail and IP addresses are removed first. Press
  **See what's sent** to look inside before sending. You get a number like SC-7F3K2M to quote.
- If SprigCast closes unexpectedly, it offers to send a report the next time it starts.
- **Help → Export report** saves the same report as a .zip on your Desktop, to send any way you like.
- **Help → Open logs folder** shows the logs themselves (Documents\SprigCast\Logs).
- After an unexpected close, SprigCast offers **safe mode** (the older, simpler engine, no plugins) on the
  next start.

Still stuck? Ask in **#help** on the [SprigCast Discord](https://discord.gg/fgMD33XrhM). If you sent a report, paste
its number there.

Reports are kept for 30 days, then deleted. See the
[privacy policy](https://slothbloom.com/privacy#sprigcast-reports).

## 6. Helping out as a tester

Thank you! A few things make your reports most useful:

- Say what you were doing, and roughly when.
- Send a report even for small things: a stutter, a sound gap, a button that felt slow.
- Under **Settings → General → Logs**, tick **Send stream summaries to the developer** to send a summary after every stream.
  It's off unless you turn it on, and holds nothing personal (see step 5).

## 7. Updates and uninstalling

SprigCast checks for updates at start and every 12 hours (Settings → General → Updates). It never updates
while you're live or recording. Uninstalling removes your saved stream keys and sign-ins. Tick **Also delete
my SprigCast data** to remove your settings and blooms too.

## 8. Licenses

SprigCast is free and open source under the GPL 3.0 or later. **Help → About SprigCast → Licenses** lists
every part it includes and their licenses.
