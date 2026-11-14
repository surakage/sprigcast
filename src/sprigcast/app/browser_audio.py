"""Browser source sound into SprigCast's mixer, instead of straight to your speakers.

A small script in each page sends what the page plays (Web Audio and <audio>/<video>) to Python
over a QWebChannel, and the page itself is muted. So an alert's sound gets its own row in the Sound
card: volume, mute, filters, monitoring and ducking under your mic, like any other sound.

Media from another site can only be heard by the page's own audio graph when that site allows it
(CORS). The script asks for that; when a site refuses, the page falls back to playing on your
desktop audio, exactly as before, and the Sound card says so.
"""

from __future__ import annotations

import base64
import logging
import threading

import numpy as np
from PySide6.QtCore import QObject, Slot

from sprigcast.sources.audio import AudioRing

log = logging.getLogger(__name__)

SAMPLE_RATE = 48000
PRIME = 2048  # samples to gather after a quiet spell before playing, so bursty delivery doesn't crackle

CAPTURE_JS = r"""
(() => {
  if (window.__sprigcastAudio) return;
  window.__sprigcastAudio = true;
  let bridge = null, gaveUp = false;
  const connect = () => {
    if (typeof QWebChannel === "undefined" || !window.qt || !qt.webChannelTransport) return setTimeout(connect, 50);
    new QWebChannel(qt.webChannelTransport, (ch) => { bridge = ch.objects.sprigcastAudio; });
  };
  connect();
  const giveUp = (why) => {  // the page plays on desktop audio from now on
    if (gaveUp) return;
    gaveUp = true;
    if (bridge) bridge.desktop(String(why)); else setTimeout(() => giveUp(why), 100);
  };
  const send = (rate, left, right) => {
    if (!bridge || gaveUp) return;
    let loud = false;
    for (let i = 0; i < left.length; i += 16) if (left[i] > 1e-4 || left[i] < -1e-4 || right[i] > 1e-4 || right[i] < -1e-4) { loud = true; break; }
    if (!loud) return;  // silence costs nothing to send
    const both = new Float32Array(left.length * 2);
    for (let i = 0; i < left.length; i++) { both[2 * i] = left[i]; both[2 * i + 1] = right[i]; }
    const bytes = new Uint8Array(both.buffer);
    let text = "";
    for (let i = 0; i < bytes.length; i += 8192) text += String.fromCharCode.apply(null, bytes.subarray(i, i + 8192));
    bridge.audio(rate, btoa(text));
  };
  const plainConnect = AudioNode.prototype.connect;
  const taps = new WeakMap();
  const tapFor = (ctx) => {
    let tap = taps.get(ctx);
    if (tap) return tap;
    tap = ctx.createGain();
    const grab = ctx.createScriptProcessor(2048, 2, 2);
    grab.onaudioprocess = (e) => {
      const input = e.inputBuffer;
      send(ctx.sampleRate, input.getChannelData(0), input.numberOfChannels > 1 ? input.getChannelData(1) : input.getChannelData(0));
    };
    plainConnect.call(tap, grab);
    plainConnect.call(grab, ctx.destination);  // keeps it running; it outputs silence
    plainConnect.call(tap, ctx.destination);
    taps.set(ctx, tap);
    return tap;
  };
  AudioNode.prototype.connect = function (target, ...rest) {
    if (target instanceof AudioDestinationNode) target = tapFor(target.context);
    return plainConnect.call(this, target, ...rest);
  };
  const plainPlay = HTMLMediaElement.prototype.play;
  // <audio> and <video>: ask other sites for permission to listen (CORS) before they load.
  const srcProp = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, "src");
  Object.defineProperty(HTMLMediaElement.prototype, "src", {
    configurable: true, enumerable: true,
    get() { return srcProp.get.call(this); },
    set(value) {
      if (!this.hasAttribute("crossorigin") && !this.__sprigcastPlain) {
        this.crossOrigin = "anonymous";
        if (!this.__sprigcastAsked) this.addEventListener("error", () => refused(this));
        this.__sprigcastAsked = true;
      }
      srcProp.set.call(this, value);
    },
  });
  const refused = (el) => {  // the site said no: load it the ordinary way, heard on the desktop
    if (!el.__sprigcastAsked || el.__sprigcastPlain) return;
    el.__sprigcastPlain = true;
    giveUp("a sound from a site that doesn't allow it");
    const src = srcProp.get.call(el);
    el.removeAttribute("crossorigin");
    if (src) srcProp.set.call(el, src);
    if (el.__sprigcastWantsPlay) plainPlay.call(el).catch(() => {});
  };
  const PlainAudio = window.Audio;
  window.Audio = function (src) {
    const el = new PlainAudio();
    if (src !== undefined) el.src = src;
    return el;
  };
  window.Audio.prototype = PlainAudio.prototype;
  const crossSite = (el) => {
    try {
      const url = new URL(el.currentSrc || el.src, location.href);
      if (url.protocol === "data:" || url.protocol === "blob:") return false;
      return url.origin !== location.origin;
    } catch (e) { return false; }
  };
  let mediaCtx = null;
  const routed = new WeakSet();
  const route = (el) => {
    if (gaveUp || routed.has(el) || !(el instanceof HTMLMediaElement) || el.__sprigcastPlain) return;
    if (el.__sprigcastAsked && el.readyState === 0) {
      // Wait until the site has said yes (it loads); a no fires "error" instead and it stays unrouted,
      // since a routed sound from a site that said no would be silent everywhere.
      el.addEventListener("loadedmetadata", () => route(el), {once: true});
      return;
    }
    if (crossSite(el) && !el.crossOrigin) { giveUp("a sound from another site"); return; }
    routed.add(el);
    try {
      mediaCtx = mediaCtx || new AudioContext();
      mediaCtx.createMediaElementSource(el).connect(mediaCtx.destination);
      if (mediaCtx.state !== "running") mediaCtx.resume();
    } catch (e) { giveUp(e && e.message || "media"); }
  };
  HTMLMediaElement.prototype.play = function () {
    this.__sprigcastWantsPlay = true;
    route(this);
    return plainPlay.apply(this, arguments);
  };
  document.addEventListener("play", (e) => route(e.target), true);
})();
"""


def qwebchannel_js() -> str:
    from PySide6.QtCore import QFile, QIODevice

    file = QFile(":/qtwebchannel/qwebchannel.js")
    if not file.open(QIODevice.OpenModeFlag.ReadOnly):
        return ""
    try:
        return bytes(file.readAll()).decode("utf-8")
    finally:
        file.close()


class PageSound(QObject):
    """One page's sound: the page script calls audio() and desktop(); the mixer reads read()."""

    def __init__(self, on_desktop=None):
        super().__init__()
        self.ring = AudioRing(capacity=SAMPLE_RATE)  # at most a second behind
        self.on_desktop = on_desktop
        self.on_desktop_reason = ""
        self._resamplers: dict[int, object] = {}
        self._primed = False
        self._lock = threading.Lock()

    @Slot(int, str)
    def audio(self, rate: int, data: str) -> None:
        try:
            samples = np.frombuffer(base64.b64decode(data), dtype=np.float32).reshape(-1, 2)
        except ValueError:
            return
        if rate != SAMPLE_RATE:
            stream = self._resamplers.get(rate)
            if stream is None:
                import soxr

                stream = self._resamplers[rate] = soxr.ResampleStream(rate, SAMPLE_RATE, 2, dtype="float32")
            samples = stream.resample_chunk(samples)
        self.ring.write(np.ascontiguousarray(samples, dtype=np.float32))

    @Slot(str)
    def desktop(self, reason: str) -> None:
        log.info("browser source sound goes to desktop audio: %s", reason)
        self.on_desktop_reason = reason
        if self.on_desktop is not None:
            self.on_desktop(reason)

    def read(self, n: int) -> np.ndarray:
        """n frames for the mixer. After a quiet spell, waits for a little sound to gather first."""
        with self._lock:
            if not self._primed:
                if len(self.ring) < max(n, PRIME):
                    return np.zeros((n, 2), dtype=np.float32)
                self._primed = True
            if len(self.ring) < n:
                self._primed = False  # ran dry: the next sound starts cleanly
            return self.ring.read(n)
