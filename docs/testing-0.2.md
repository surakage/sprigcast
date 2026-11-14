# Trying 0.2.0 on your PC

Most of 0.2.0 passed the automated tests but hasn't been tried by hand yet. This guide walks you
through each new feature with a real example, what you should see, and what to send back if it
doesn't work. Each one takes 5 to 10 minutes.

When something goes wrong, note which step it was and what you saw. A screenshot helps. The logs
(File → Open logs folder) fill in the rest, so you don't need to copy them.

## Before you start

**Install the test build.** Download `SprigCast-Setup` from the Windows installer run on GitHub
(Actions → Windows installer → the newest run → Artifacts), unzip it and run the installer.
Help → About SprigCast should say 0.2.0.

**Back up your blooms.** File → Show scenes file location, and copy `scenes.json` somewhere safe.
The starter kit test can replace your blooms, and this lets you put them back.

**Sound check (2 minutes).** Do this first, so a muted mic isn't mistaken for a bug.

1. Open Wave Link and unmute the Wave XLR. Check the mic's level moves when you talk.
2. In SprigCast, Settings → Audio → Devices: the mic should be the Wave device you use. Your Wave Link
   channels should be listed first for desktop sound, named like "Game · Elgato Wave Link". If you pick
   Wave Link Stream (a mix that already holds your mic), turn the separate Mic off so you don't hear it twice.
3. On the Live screen, talk and watch the mic's meter on the Sound card move.
4. Press **Record**, count to ten out loud, press it again.
5. File → Open recordings folder, and play the new file. You should hear yourself.

If the meter moves but the file is silent, that's a bug: say so and skip ahead.

## 1. Starter kit

Builds a ready set of blooms from a few choices.

1. Tools → Starter kit…
2. **Camera**: your Facecam. **Screen (for Gaming)**: your main monitor. **Mic**: your Wave mic.
3. **Desktop sound**: try **By type** this time.
4. **Blooms to make**: leave all five on (Starting soon, Just Chatting, Gaming, Be right back, Ending).
5. **Extras**: tick **Vertical versions**. Leave **Replace the example blooms** off.
6. Press **Make my blooms**.

**Works if:** the library on the Build screen shows the five new blooms on both the Horizontal
and Vertical tabs. Just Chatting and Gaming have your camera, Gaming has your screen, and
Starting soon, Be right back and Ending have the sloth art. Your old blooms are still there.

**Report:** a missing bloom, a missing camera or screen, art that's cut off or off-centre, or
anything that touches the edges.

## 2. Build tabs and undo

1. Go to Build (Ctrl+2). Click the **Vertical** tab, then **Horizontal**. Each shows its own blooms.
2. Click the Gaming bloom you just made. Go to Live (Ctrl+1) and check that a *different* bloom is on
   air, then come back. Editing must never change what's on air.
3. In Gaming, drag your camera to another corner. Drag a corner handle to make it bigger.
   Hold Alt and drag a side handle to crop it.
4. **+ Add → Text**, double-click it in Inside, set the text to "Testing", and move it to the top.
5. Now press Ctrl+Z four times. Each press should undo one step: the text, then the crop, the
   resize and the move. The bottom of the window says "Undone. Ctrl+Y puts it back."
6. Press Ctrl+Y four times. Everything comes back in order.

**Works if:** every undo and redo takes back exactly one step, on the right tab, and Live never changed.

**Report:** a step that undid two things, or nothing; anything that jumped on the other tab; or
Live changing while you edited.

## 3. Media source

Videos sit in a bloom like a picture. Sound files live in Sound and wait until you play them.

**A video:**

1. In Build, pick Be right back. **+ Add → Video or sound file…** and pick a short MP4 (10 to 30 s).
2. Double-click it in Inside and make sure **Start over when shown** is on.
3. Go to Live and switch to Be right back. It plays. Switch away and back: it starts from the beginning.
4. Right-click it on the stage. **Pause**, **Play**, **Start over** and **Stop** should all work.

**A sound file:**

1. In Build, **+ Add → Sound (plays in every bloom) → Sound file…** and pick a short MP3 or WAV.
   It shows up in the Sound list on the left, and doesn't play yet.
2. Right-click it there and press **Play**. You hear it, and its meter moves on the Live Sound card.
3. Settings → Hotkeys, find **Videos**, and give "*your file*: play / pause" a key like Ctrl+Alt+P.
   Click into a game or another window and press it. It plays; press again to pause.

**Works if:** the video restarts each time it's shown, the sound file waits until you play it, and
the hotkey works while another window has focus.

**Report:** a video that keeps playing off screen, a sound that starts on its own, no sound from
the video, or a hotkey that does nothing.

## 4. Source record

Records a second copy of your show with some sources left out, like alerts or chat.

1. Settings → Output → **Recording** tab. In **Source record**, tick
   **Also record a copy without the sources ticked below**.
2. Under **Leave out**, tick one thing you can easily see and hear, like your Text or the video from step 3.
3. Press Apply. On Live, put that bloom on air and press **Record**. Talk for 30 seconds, then stop.
4. File → Open recordings folder.

**Works if:** you get two files per shape, for example `… horizontal.mkv` and `… horizontal source.mkv`.
The normal file has everything. The `source` file has your voice and game but not what you left out.

**Report:** a missing `source` file, the left-out thing still showing in it, missing sound, or the
two files being different lengths.

## 5. Virtual camera

Shows SprigCast as a webcam in Discord, Zoom or the Windows Camera app. Windows 11 only.

1. Settings → Output → **Virtual camera** tab. Press **Set up the camera** and say Yes on the Windows
   prompt. The status should change to "Set up."
2. Tick **Show SprigCast as a camera**. Leave **Shows** on "Matches the app" for now, and press Apply.
3. Open the Windows Camera app (or Discord → Settings → Voice & Video) and pick **SprigCast Camera**.
4. Now set **Shows** to **Vertical canvas** and press Apply. Open TikTok LIVE Studio and add
   SprigCast Camera as a camera source.
5. Try **Shows** set to one bloom, then to one source (your Facecam).

**Works if:** step 3 shows your bloom on air and changes when you switch blooms in Live. TikTok LIVE
Studio shows the vertical canvas (this used to fail to start). A single bloom or source fills the
picture, with bars at the sides if its shape is different.

**Report:** the camera missing from the list, a black or green picture, a picture that freezes, or an
error during setup (copy its wording).

## 6. TikTok chat

1. Start TikFinity and make sure it's connected to your TikTok LIVE.
2. In SprigCast's Chat panel, press **⋯ → Channels…**, type your handle next to TikTok
   (like `@yourname`) and press **Use**.
3. Go live on TikTok (TikTok LIVE Studio is fine) and have a friend, or a second phone, type a message.

**Works if:** the message shows in the Chat panel with a TikTok mark. Hovering the TikTok name at the
top says "reading chat via TikFinity".

**Then try it without TikFinity:** close TikFinity and wait a minute. The tooltip should switch to
"straight from TikTok", and new messages keep coming. (If it doesn't connect, **⋯ → TikTok key…**
takes an optional Euler Stream key that makes this steadier.)

**Report:** no messages, messages that arrive late, or the panel staying on "connecting…".

## 7. Game capture sound

Gives a game its own sound row, so you can turn the game down without touching Discord or music.

1. Start a game (or any app that makes sound, like a browser playing a video).
2. In Build, **+ Add → Window** and pick it.
3. Right-click the capture on the canvas → **Sound** → **The app's own sound**.
4. Check the Sound list: a new row named "*capture name* sound" in the Game group.
5. Play some music in another app. Turn the game's sound row down to nothing.
6. Try **Sound** again and pick a Wave Link channel instead, like "Game · Elgato Wave Link".

**Works if:** the game row's meter moves only with the game, turning it down mutes only the game, and
the row keeps its volume when you switch it to the Wave Link channel. Removing the capture removes its
sound row too.

**Report:** no new row, the row catching all your desktop sound instead of just the game, or sound
playing twice (once from the row and once from Desktop sound). If it plays twice, say which Desktop
sound device you use.

## Already tried on Windows

An hour-long Twitch stream, Twitch, YouTube and Kick chat together, the installer, and the full test
suite (558 passed on Windows 11).

**Not covered by Claude's run on your PC (14 Nov):** game and window capture sound (test 7) and moving
and resizing in Build (test 2, steps 3 and 4). These need you at the keyboard.

## Worth another look when you have time

- **Live screen:** switch blooms with their hotkeys, and try a stinger transition.
- **Clips:** press **Clips**, then save a 30 s clip. It should play with sound.
- **Stream delay:** Settings → Advanced, set 10 s and go live. The card says "Going out in N s", not Connecting.
- **Streamer.bot:** switch a bloom and start a recording from it, with SprigCast's own API and in Compatibility mode.
- **Settings → Output:** your NVIDIA encoders (H.264, HEVC, AV1) are all listed.
