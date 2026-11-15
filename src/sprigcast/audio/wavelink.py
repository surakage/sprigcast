"""Keep SprigCast's sound strips and Elgato Wave Link's channels in step.

A sound that captures a Wave Link channel ("Game (Elgato Virtual Audio)") gets the same mute and volume
as that channel in Wave Link's stream mix: change it on either side and the other follows. SprigCast
captures the channel before Wave Link's fader, so each side's level only acts on its own path, and the
streamer has one control instead of two.

This talks to Wave Link 3's local WebSocket (JSON-RPC 2.0, the one Elgato's Stream Deck plugin uses).
It isn't an official public API, so anything unexpected just leaves the two apps unsynced. Nothing runs
unless Settings → Sound → Keep Wave Link in step is on (off by default) and a sound captures a Wave Link channel: no
connection, no timers. Wave Link pushes changes, so there's no polling either.

WaveLinkState is the logic (tested without Wave Link); WaveLinkSync is the Qt connection.
"""

from __future__ import annotations

import json
import logging
import math
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QSettings, QTimer, QUrl, Signal
from PySide6.QtNetwork import QNetworkRequest

from sprigcast.sources.audio import _ELGATO_NAME, is_mixer_app_device

log = logging.getLogger(__name__)

SETTING = "wavelink/sync"  # off until switched on: when it connects, SprigCast's strips take Wave Link's levels
PORTS = range(1884, 1894)  # Wave Link 3; ws-info.json says which one
RETRY_S = (10, 30, 60, 120, 300)  # Wave Link not running: look again less and less often
SOUND_KINDS = ("audio_output", "audio_input")
EPSILON_DB = 0.25


def ws_info_port() -> int | None:
    """The port Wave Link 3 wrote to its ws-info.json, if it's there."""
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        return None
    for folder in Path(base, "Packages").glob("Elgato.WaveLink_*"):
        try:
            port = int(json.loads((folder / "LocalState" / "ws-info.json").read_text(encoding="utf-8"))["port"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if 1 <= port <= 65535:
            return port
    return None


def channel_of(device: str) -> str | None:
    """'Game (Elgato Virtual Audio)' -> 'game'; None for anything that isn't a Wave Link channel."""
    if not device or not is_mixer_app_device(device):
        return None
    match = _ELGATO_NAME.match(device)
    return match.group("channel").strip().lower() if match else None


def level_to_db(level: float) -> float:
    return -100.0 if level <= 0.00001 else max(-100.0, round(20 * math.log10(min(1.0, level)), 1))


def db_to_level(db: float) -> float:
    return 0.0 if db <= -100 else min(1.0, 10 ** (db / 20))


class WaveLinkState:
    """What Wave Link last told us, and what to send or apply when either side changes."""

    def __init__(self):
        self.mix_id: str | None = None  # the stream mix
        self.channels: dict[str, dict[str, Any]] = {}  # channel id -> {"name", "muted", "db"} in the stream mix

    def set_mixes(self, mixes: Iterable[dict]) -> None:
        mixes = [m for m in mixes if isinstance(m, dict) and m.get("id")]
        stream = next((m for m in mixes if "stream" in str(m.get("name", "")).lower()), None)
        self.mix_id = str((stream or (mixes[0] if mixes else {})).get("id") or "") or None

    def set_channels(self, channels: Iterable[dict]) -> None:
        self.channels = {}
        for channel in channels:
            self._take(channel)

    def _take(self, channel: dict) -> str | None:
        """Update one channel from a full or partial Wave Link channel; its id, or None."""
        if not isinstance(channel, dict) or not channel.get("id"):
            return None
        cid = str(channel["id"])
        known = self.channels.setdefault(cid, {"name": "", "muted": False, "db": 0.0})
        if channel.get("name"):
            known["name"] = str(channel["name"])
        for mix in channel.get("mixes") or []:
            if isinstance(mix, dict) and self.mix_id and str(mix.get("mixId")) == self.mix_id:
                if "isMuted" in mix:
                    known["muted"] = bool(mix["isMuted"])
                if isinstance(mix.get("level"), (int, float)):
                    known["db"] = level_to_db(float(mix["level"]))
        return cid

    def channel_for(self, device: str) -> str | None:
        wanted = channel_of(device)
        if wanted is None:
            return None
        for cid, info in self.channels.items():
            name = info["name"].strip().lower()
            if name == wanted or name.replace("wave link ", "") == wanted:
                return cid
        return None

    def outgoing(self, sounds: Iterable[Any]) -> list[dict]:
        """setChannel params for sounds whose mute or volume differs from Wave Link's channel."""
        if not self.mix_id:
            return []
        out = []
        for source in sounds:
            cid = self.channel_for(str(source.settings.get("device", "")))
            if cid is None:
                continue
            known = self.channels[cid]
            muted = bool(source.settings.get("muted", False))
            db = float(source.settings.get("volume_db", 0.0))
            mix: dict[str, Any] = {"mixId": self.mix_id}
            if muted != known["muted"]:
                mix["isMuted"] = known["muted"] = muted
            if abs(min(db, 0.0) - min(known["db"], 0.0)) > EPSILON_DB:
                mix["level"] = round(db_to_level(db), 4)
                known["db"] = min(db, 0.0)
            if len(mix) > 1:
                out.append({"id": cid, "mixes": [mix]})
        return out

    def incoming(self, channel: dict, sounds: Iterable[Any]) -> list[tuple[Any, dict]]:
        """(source, settings to change) for sounds on a channel Wave Link just changed."""
        cid = self._take(channel)
        if cid is None:
            return []
        known = self.channels[cid]
        changes = []
        for source in sounds:
            if self.channel_for(str(source.settings.get("device", ""))) != cid:
                continue
            change: dict[str, Any] = {}
            if bool(source.settings.get("muted", False)) != known["muted"]:
                change["muted"] = known["muted"]
            db = float(source.settings.get("volume_db", 0.0))
            if abs(min(db, 0.0) - known["db"]) > EPSILON_DB:
                change["volume_db"] = known["db"]
            if change:
                changes.append((source, change))
        return changes


def enabled() -> bool:
    return QSettings().value(SETTING, False, type=bool)


class WaveLinkSync(QObject):
    """The connection: opened only while a sound captures a Wave Link channel, closed when none does."""

    apply = Signal(object, dict)  # (source, settings) Wave Link changed: the window applies them

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.state = WaveLinkState()
        self.socket = None
        self.status = "off"
        self._sounds: list[Any] = []
        self._ports: list[int] = []
        self._next_id = 0
        self._pending: dict[int, str] = {}
        self._tries = 0
        self._retry = QTimer(self)
        self._retry.setSingleShot(True)
        self._retry.timeout.connect(self._open)

    def wanted_sounds(self, sounds: Iterable[Any]) -> list[Any]:
        return [s for s in sounds if s.kind in SOUND_KINDS and channel_of(str(s.settings.get("device", "")))]

    def update(self, sounds: Iterable[Any]) -> None:
        """After every edit: connect, disconnect, or tell Wave Link what changed."""
        self._sounds = self.wanted_sounds(sounds) if enabled() else []
        if not self._sounds:
            self.close()
            return
        if self.socket is None and not self._retry.isActive():
            self._ports = []
            self._open()
        elif self.status == "on":
            for params in self.state.outgoing(self._sounds):
                self._call("setChannel", params)

    def close(self) -> None:
        self._retry.stop()
        if self.socket is not None:
            socket, self.socket = self.socket, None
            try:
                socket.disconnected.disconnect()
            except (RuntimeError, TypeError):
                pass
            socket.abort()
            socket.deleteLater()
        self.status = "off"

    # Connecting --------------------------------------------------------------------------------

    def _open(self) -> None:
        from PySide6.QtWebSockets import QWebSocket

        if not self._sounds:
            return
        if not self._ports:
            first = ws_info_port()
            self._ports = ([first] if first else []) + [p for p in PORTS if p != first]
        port = self._ports.pop(0)
        self.status = "connecting"
        socket = self.socket = QWebSocket()
        socket.connected.connect(self._up)
        socket.disconnected.connect(self._down)
        socket.textMessageReceived.connect(self._received)
        request = QNetworkRequest(QUrl(f"ws://127.0.0.1:{port}"))
        request.setRawHeader(b"Origin", b"streamdeck://")  # what Elgato's own plugin sends
        socket.open(request)

    def _up(self) -> None:
        self._tries = 0
        self.status = "on"
        self.state = WaveLinkState()
        self._call("getApplicationInfo", None)
        self._call("getMixes", None)
        self._call("getChannels", None)
        log.info("wave link: connected")

    def _down(self) -> None:
        was_on = self.status == "on"
        self.close()
        if not self._sounds:
            return
        if self._ports and not was_on:
            self._open()  # try the next port
            return
        self._ports = []
        wait = RETRY_S[min(self._tries, len(RETRY_S) - 1)]
        self._tries += 1
        self.status = "waiting"
        self._retry.start(wait * 1000)
        if was_on:
            log.info("wave link: closed, looking again in %d s", wait)

    # Talking -----------------------------------------------------------------------------------

    def _call(self, method: str, params: dict | None) -> None:
        if self.socket is None:
            return
        self._next_id += 1
        self._pending[self._next_id] = method
        self.socket.sendTextMessage(json.dumps({"jsonrpc": "2.0", "id": self._next_id, "method": method,
                                                "params": params}))

    def _received(self, text: str) -> None:
        try:
            message = json.loads(text)
        except ValueError:
            return
        if not isinstance(message, dict):
            return
        if "id" in message:
            method = self._pending.pop(message.get("id"), "")
            result = message.get("result")
            if "error" in message:
                log.info("wave link: %s failed: %s", method, message["error"])
            elif method == "getApplicationInfo" and isinstance(result, dict):
                log.info("wave link: %s (interface %s)", result.get("name"), result.get("interfaceRevision"))
            elif method == "getMixes" and isinstance(result, dict):
                self.state.set_mixes(result.get("mixes") or [])
            elif method == "getChannels" and isinstance(result, dict):
                self.state.set_channels(result.get("channels") or [])
                self._adopt_all()
            return
        params = message.get("params") or {}
        method = message.get("method")
        if method == "mixesChanged" and isinstance(params, dict):
            self.state.set_mixes(params.get("mixes") or [])
        elif method == "channelsChanged" and isinstance(params, dict):
            self.state.set_channels(params.get("channels") or [])
            self._adopt_all()
        elif method == "channelChanged" and isinstance(params, dict):
            for source, change in self.state.incoming(params, self._sounds):
                self.apply.emit(source, change)

    def _adopt_all(self) -> None:
        """Just connected: SprigCast's strips take Wave Link's values (it's the app people set up first)."""
        for cid, info in self.state.channels.items():
            channel = {"id": cid, "mixes": [{"mixId": self.state.mix_id, "isMuted": info["muted"],
                                              "level": db_to_level(info["db"])}]}
            for source, change in self.state.incoming(channel, self._sounds):
                self.apply.emit(source, change)
