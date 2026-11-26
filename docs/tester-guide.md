# SprigCast tester guide

Thanks for testing SprigCast! It's a free, open-source streaming app for Windows that sends a horizontal and a
vertical picture at the same time, to as many places as you like. This is a test build, so things may break.
When they do, your reports are what get them fixed.

## 1. Download and install

1. Download **SprigCast-Setup-….exe** (for example SprigCast-Setup-0.4.0.exe) from the tester page,
   https://sprigcast.slothbloom.com/test . It always has the newest test build.
2. Run it. SprigCast isn't signed yet, so Windows will probably say **"Windows protected your PC"**. Click
   **More info**, then **Run anyway**. (Your antivirus may ask too. That's the same reason.)
3. Setup installs for your Windows account only. Leave **SprigCast Camera** ticked if you want to use SprigCast
   as a camera in other apps (TikTok LIVE Studio, Discord, Zoom); Windows asks your permission once for it.

You need Windows 10 (version 1809 or newer) or Windows 11, 64-bit. Most graphics cards from the last ten years
work.

## 2. First setup (about 5 minutes)

1. When SprigCast opens, answer the few **Get started** questions. It checks your PC and picks settings that
   suit it.
2. Open **Settings → Stream** and add the places you stream to. Twitch, YouTube and Kick have a **Sign in**
   button that fills in your stream key. For the others, paste the server and stream key.
   - YouTube's sign-in may say **"Google hasn't verified this app"** while Google reviews SprigCast. Click
     **Advanced**, then **Go to SprigCast**, or paste your key instead.
3. On the **Build** screen (Ctrl+2), make a bloom (a scene): add your camera, game or screen, and a picture or
   two. On the **Live** screen (Ctrl+1), pick it.

Your stream keys stay in Windows Credential Manager. They're never written to a file or a log.

## 3. Test safely first

Before your first real stream, do a private test:
- **Twitch:** in **Settings → Stream**, turn on the **Test only** switch at the top of your Twitch place. The stream reaches Twitch but viewers don't see it.
- **YouTube:** set the stream to **Unlisted** or **Private** in YouTube Studio.
- Or just press **Record** and watch the file afterwards.

## 4. What to try

Anything you normally do on stream is useful. These matter most right now:

- Going live to **two or more places** at once, and streaming for **an hour or longer**.
- Switching blooms while live, and stopping and starting **one place** while the others stay live.
- **Recording** and **saving a clip** while live.
- Your **camera, capture card, mic, game and browser sources** (alerts, chat boxes, overlays).
- Running alongside your usual tools (Streamer.bot, TikTok LIVE Studio, Stream Deck and so on).
- Older or slower PCs, laptops, and non-NVIDIA graphics cards.

## 5. Sending reports

Please send one for anything that feels off, even small things like a stutter, a sound gap or a slow button.

- **Help → Report a problem.** Write what you were doing and roughly when, then press **Send report**. Leave
  "Include the log and PC details" ticked. Press **See what's sent** first if you'd like to look inside. You get
  a number like **SC-7F3K2M**. Post that number in **#bug-reports** on the
  [SprigCast Discord](https://discord.gg/fgMD33XrhM) so I can find your report.
- **After a crash**, SprigCast offers to send a report the next time it starts. Please say yes.
- **Help improve SprigCast (optional):** after your first stream, SprigCast asks whether to send a summary
  after each stream. Saying yes is the most helpful thing you can do: I see how every stream went without you
  writing anything. You can change it any time in **Settings → General → Logs**.
- **Can't send from the app?** Use **Help → Export report**. It saves a .zip to your Desktop that you can send
  me any way you like, or attach it to your post in #bug-reports.

**What's in a report:** SprigCast's logs, how each stream went, and your PC's details (Windows version,
processor, graphics card, memory). **Removed before anything is sent:** stream keys, passwords, sign-in tokens,
your name, your PC's name, e-mail addresses and IP addresses. Chat messages are never logged. Reports are kept
for 30 days, then deleted.

## 6. Known gaps in this build

- Twitch **Enhanced Broadcasting** sends the horizontal picture only. Vertical on Twitch is switched off until
  it works.
- **TikTok:** stream with TikTok LIVE Studio, using SprigCast Camera as its camera. TikTok chat isn't in
  SprigCast.
- **Kick chat** is being rebuilt.
- SprigCast checks for updates by itself, but test builds aren't offered as updates. I'll send you a new link
  for each test build.

## 7. If something goes really wrong

- If SprigCast closes unexpectedly, it offers **safe mode** (the older, simpler engine) on the next start.
- To uninstall, use **Windows Settings → Apps**. Your stream keys and sign-ins are always removed. Tick **Also
  delete my SprigCast data** to remove your settings and scenes too.

Thank you for helping! 🦥
