# Trying 0.2.0 on your PC

Most of 0.2.0 passed the automated tests but hasn't been tried on a real Windows PC yet.
These are the checks that matter most, in order. Each takes a few minutes. Note anything odd and
where it happened; the logs in `Documents\SprigCast\Logs` fill in the rest.

1. **Install over the old copy.** Run the new installer. It should ask to close SprigCast, then start
   with your blooms, sounds and stream keys still there. Help → About says 0.2.0.
2. **Starter kit.** Tools → Starter kit… on a fresh scenes file. It should build blooms for both shapes,
   with your camera and Desktop sound.
3. **Build screen.** Make a bloom, drop a sprig in, move and resize things, crop with Alt, undo and redo.
   Editing must not change what's on air.
4. **Live screen.** Switch blooms with the buttons and their hotkeys. Try two transitions, including a
   stinger clip. Check nothing touches the edges at your normal window size.
5. **Sound.** Mic, Desktop sound and one more. Turn on Mic first and talk over music. Open a Tune page and
   add a filter. Set a sound to Stream only and check the recording doesn't have it.
6. **Record, replay and clips.** Record both shapes for a minute, save a replay, save a 30 s clip. All the
   files should play with sound.
7. **Go live.** A short Twitch stream at 6000 kbps, with a recording running beside it. The You're live
   card shows, and the stream report lands in the Logs folder.
8. **Chat.** Twitch, YouTube and Kick together (tested before), plus TikTok through TikFinity. Reply on
   Twitch, and add a chat source to a bloom.
9. **Browser and media sources.** A browser page with sound, right-click Interact… on it. A video file in
   a bloom and a sound file played from its right-click menu and its hotkey.
10. **Color looks and shapes.** Put a look and a round shape on your camera.
11. **Streamer.bot.** Switch a bloom and start a recording from Streamer.bot, first with SprigCast's own
    API, then in Compatibility mode.
12. **Settings.** Open every page. Output should list your NVIDIA encoders (H.264, HEVC, AV1). Change the
    Video output size and confirm the stream uses it.

Already tried on Windows: an hour-long Twitch stream, Twitch/YouTube/Kick chat, and the installer.
