# Trying 0.2.0 on your PC

Most of 0.2.0 passed the automated tests but hasn't been tried by hand on a real Windows PC yet.
These are the checks that matter most, in order. Each takes a few minutes. Note anything odd and
where it happened; the logs in `Documents\SprigCast\Logs` fill in the rest.

## Still untested by hand

These are new and nobody has tried them on a PC yet. Start here.

1. **Starter kit.** Tools → Starter kit… on a fresh scenes file. It should build blooms for both shapes,
   with your camera and Desktop sound.
2. **Build tabs and undo.** On the Build screen, switch the Horizontal and Vertical tabs, make a bloom,
   drop a sprig in, move and resize things, crop with Alt, then undo and redo every step.
   Editing must not change what's on air.
3. **Media source.** A video file in a bloom: it should start over each time the bloom is shown. A sound
   file played from its right-click menu and from its hotkey.
4. **Source record.** Settings → Output → Recording: tick a source to leave out (an alert, say). Record a
   minute. You should get a second file per shape, `… horizontal source.mkv`, without that source's
   picture or sound.
5. **Virtual camera.** Settings → Output → Virtual camera → Set up (one admin prompt, Windows 11 only).
   Pick "SprigCast Camera" in Discord or the Windows Camera app. It should show your bloom.
6. **TikTok chat.** Through TikFinity, and once directly. Messages should land in the Chat panel.

## Worth another look

7. **Install over the old copy.** Run the new installer. It should ask to close SprigCast, then start
   with your blooms, sounds and stream keys still there. Help → About says 0.2.0.
8. **Live screen.** Switch blooms with the buttons and their hotkeys. Try two transitions, including a
   stinger clip. Check nothing touches the edges at your normal window size.
9. **Sound.** Turn on Mic first and talk over music. Open a Tune page and add a filter. Set a sound to
   the Stream track only and check the recording doesn't have it.
10. **Record, replay and clips.** Record both shapes for a minute, save a replay, save a 30 s clip. All
    the files should play with sound.
11. **Go live.** A short Twitch stream at 6000 kbps with a recording beside it. Try a 10 s stream delay
    (Settings → Advanced): the card should say "Going out in N s", not Connecting.
12. **Chat extras.** Reply on Twitch, and add a chat source to a bloom.
13. **Browser source.** A page with sound, and right-click Interact… on it.
14. **Streamer.bot.** Switch a bloom and start a recording from Streamer.bot, with SprigCast's own API
    and then in Compatibility mode.
15. **Settings.** Open every page. Output should list your NVIDIA encoders (H.264, HEVC, AV1).

Already tried on Windows: an hour-long Twitch stream, Twitch, YouTube and Kick chat together, the
installer, and the full test suite (528 passed).
