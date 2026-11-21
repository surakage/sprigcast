"""Nothing personal reaches a log: stream keys, tokens, passwords, names, paths, devices (core/privacy.py)."""

import io
import logging
import re
from datetime import datetime
from pathlib import Path

import pytest

from sprigcast.core import privacy

TWITCH_KEY = "live_123456789_AbCdEfGhIjKlMnOpQrStUvWx"
# Twitch Enhanced Broadcasting's sign-in for one stream: it holds a "?" of its own (the 2026-11-20 leak).
ENHANCED_AUTH = "v2_1b5eXaMpLeToKeN0123456789abcdef?sig=QWERTYUIOPASDFGHJKL0123456789&nonce=zxcvbnm0987654321"
ENHANCED_KEY = ENHANCED_AUTH + "&clientConfigId=0d3c0a5e-1111-2222-3333-444455556666"
YOUTUBE_KEY = "abcd-efgh-ijkl-mnop-qrst"
KICK_KEY = "sk_us-west-2_aBcDeFgHiJkLmNoP"
PASSWORD = "Tr0ub4dor&3-horse-battery"
TOKEN = "ya29.a0AfB_byC-SoMeOAuthAccessToken1234567890"
CHANNEL = "sloth_streams_daily"

LEAKS = [TWITCH_KEY, ENHANCED_AUTH, ENHANCED_KEY, "v2_1b5eXaMpLeToKeN", "QWERTYUIOPASDFGHJKL0123456789",
         YOUTUBE_KEY, KICK_KEY, PASSWORD, TOKEN, CHANNEL, "surar", "someone@example.com", "203.0.113.77",
         "7&2c3e9b1&0&0000"]


def _leaks(text: str) -> list[str]:
    return [leak for leak in LEAKS if leak.lower() in text.lower()]


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    privacy.forget_all()
    monkeypatch.setattr(privacy, "pc_names", lambda: ["surar", "DESKTOP-SLOTH"])
    yield
    privacy.forget_all()


LINES = [
    # The line that leaked on 2026-11-20, as Streamer.run writes it when redact misses.
    "stream Twitch Enhanced (2 qualities, + vertical) dropped: quality 1: [Errno 10053] Error number -10053 "
    f"occurred: 'rtmp://ingest.global-contribute.live-video.net/app/{ENHANCED_KEY}'",
    f"link Kick (horizontal): closing: [Errno 5] End of file: 'rtmps://fa723fc1b171.global-contribute.live-video"
    f".net/app/{KICK_KEY}'",
    f"upload test to rtmp://a.rtmp.youtube.com/live2/{YOUTUBE_KEY}?bandwidthtest=true failed",
    f"the key is {TWITCH_KEY} and the auth is {ENHANCED_AUTH}",
    f"control: password={PASSWORD}",
    f"stream details: GET https://www.googleapis.com/youtube/v3/liveBroadcasts?access_token={TOKEN}",
    f"Authorization: Bearer {TOKEN}",
    f"chat twitch {CHANNEL}: live",
    r"SprigCast 0.2.0 started; logs in C:\Users\surar\Documents\SprigCast\Logs",
    "on DESKTOP-SLOTH, signed in as someone@example.com from 203.0.113.77",
    r"libav.dshow: real-time buffer [@device_pnp_\\?\usb#vid_0fd9&pid_0078&mi_00#7&2c3e9b1&0&0000#{65e8773d-8f56}"
    r"\global] [video input] too full",
]


@pytest.mark.parametrize("line", LINES)
def test_scrub_hides_everything_personal(line):
    privacy.remember_secret(PASSWORD, TOKEN)
    privacy.remember_name(CHANNEL)
    assert _leaks(privacy.scrub(line)) == []


def test_scrub_keeps_what_the_log_is_for():
    privacy.remember_name(CHANNEL)
    text = privacy.scrub("stream Twitch dropped: [Errno 10053] at rtmp://ingest.example.net/app/x1234567890; "
                         "encoder NVENC, 6000 kbps, 192.168.1.20")
    assert "rtmp://ingest.example.net/" in text and "[Errno 10053]" in text
    assert "NVENC, 6000 kbps" in text and "192.168.1.20" in text  # a local address says nothing about you


def test_a_remembered_secret_is_hidden_in_every_form():
    privacy.remember_secret(ENHANCED_KEY)
    from urllib.parse import quote

    for form in (ENHANCED_KEY, quote(ENHANCED_KEY, safe=""), "QWERTYUIOPASDFGHJKL0123456789"):
        assert form not in privacy.scrub(f"failed with {form} here")


def test_a_json_sign_in_hides_its_tokens_not_its_names():
    privacy.remember_secret('{"access_token": "%s", "refresh_token": "r3fr3shT0k3nValue", "name": "StreamBridge"}'
                            % TOKEN)
    text = privacy.scrub(f"StreamBridge sent {TOKEN} and r3fr3shT0k3nValue")
    assert TOKEN not in text and "r3fr3shT0k3nValue" not in text and "StreamBridge" in text


def test_mask_for_the_screen_hides_the_enhanced_key_with_its_own_question_mark():
    url = f"rtmp://ingest.global-contribute.live-video.net/app/{ENHANCED_KEY}"
    message = f"quality 1: [Errno 10053] Error number -10053 occurred: '{url}'"
    shown = privacy.mask(message, [ENHANCED_KEY, url, TWITCH_KEY])
    assert _leaks(shown) == []
    assert "rtmp://ingest.global-contribute.live-video.net/app/****" in shown


def test_streamer_redact_hides_the_key_and_the_address_path():
    from sprigcast.output.stream import Streamer

    st = Streamer.__new__(Streamer)
    st._key = TWITCH_KEY
    text = st.redact(f"couldn't connect: 'rtmp://live.twitch.tv/app/{TWITCH_KEY}?bandwidthtest=true'")
    assert TWITCH_KEY not in text and "bandwidthtest" not in text


def test_enhanced_redact_hides_twitchs_sign_in_whole():
    from sprigcast.output.enhanced_stream import EnhancedStreamer

    st = EnhancedStreamer.__new__(EnhancedStreamer)
    st._secrets = [ENHANCED_KEY, f"rtmp://ingest.example.net/app/{ENHANCED_KEY}", TWITCH_KEY]
    text = st.redact(f"[Errno 10053] occurred: 'rtmp://ingest.example.net/app/{ENHANCED_KEY}' {ENHANCED_AUTH}")
    assert _leaks(text) == []


# Every way into the log -------------------------------------------------------------------------------------


@pytest.fixture
def day_log(tmp_path, monkeypatch):
    pytest.importorskip("PySide6.QtCore")
    from sprigcast.app import logbook

    monkeypatch.setenv("SPRIGCAST_LOG_DIR", str(tmp_path))
    monkeypatch.setattr(logbook, "_crash_file", object())  # leave pytest's own faulthandler alone
    monkeypatch.setattr(logbook, "remember_personal", lambda: privacy.remember_name(CHANNEL))
    path = logbook.setup(datetime(2026, 11, 21, 9, 0))
    try:
        yield path
    finally:
        logging.getLogger().removeHandler(logbook._handler)
        logbook._handler.close()
        logbook._handler = None


def _read(path: Path) -> str:
    for handler in logging.getLogger().handlers:
        handler.flush()
    return path.read_text(encoding="utf-8")


def test_no_key_or_token_reaches_the_day_log(day_log):
    privacy.remember_secret(PASSWORD, TOKEN)
    log = logging.getLogger("sprigcast.output.stream")
    for line in LINES:
        log.warning("%s", line)
    log.warning("stream %s dropped: %s", "Twitch", f"'rtmp://live.twitch.tv/app/{TWITCH_KEY}'")
    logging.getLogger("sprigcast.native").info("engine: writer: opening rtmp://x.example.net/app/%s", KICK_KEY)
    logging.getLogger("libav.tcp").warning("Connection to tcp://203.0.113.77:1935 failed: %s", TOKEN)
    text = _read(day_log)
    assert "dropped" in text and "engine: writer" in text
    assert _leaks(text) == []


def test_a_traceback_with_the_key_in_it_is_scrubbed_too(day_log):
    try:
        raise ConnectionAbortedError(f"[Errno 10053] occurred: 'rtmp://ingest.example.net/app/{ENHANCED_KEY}'")
    except ConnectionAbortedError:
        logging.getLogger("sprigcast.output.enhanced_stream").exception("track 1 of stream Twitch failed")
    text = _read(day_log)
    assert "Traceback" in text and "ConnectionAbortedError" in text
    assert _leaks(text) == []


def test_every_root_handler_scrubs(day_log):
    stream = io.StringIO()
    console = logging.StreamHandler(stream)
    logging.getLogger().addHandler(console)
    try:
        privacy.guard_root()
        for handler in logging.getLogger().handlers:
            assert isinstance(handler.formatter, privacy.ScrubbingFormatter), handler
        logging.getLogger("sprigcast").warning("key %s", TWITCH_KEY)
        assert TWITCH_KEY not in stream.getvalue()
    finally:
        logging.getLogger().removeHandler(console)


def test_channel_names_from_settings_are_hidden_from_the_start(day_log):
    logging.getLogger("sprigcast.chat.youtube").info("youtube chat @%s: Operation timed out", CHANNEL)
    assert CHANNEL not in _read(day_log)


def test_the_stream_report_is_scrubbed(tmp_path):
    pytest.importorskip("PySide6.QtCore")
    from sprigcast.app import logbook

    class _Session:
        streamers: list = []
        shared: list = []

    report = logbook.StreamReport.__new__(logbook.StreamReport)
    report.session = _Session()
    report.path = tmp_path / "stream.txt"
    report._file = open(report.path, "w", encoding="utf-8")
    report._write(f"  0:15  Twitch Enhanced: disconnected: 'rtmp://ingest.example.net/app/{ENHANCED_KEY}'")
    report._file.close()
    assert _leaks(report.path.read_text(encoding="utf-8")) == []


def test_sweep_scrubs_what_faulthandler_wrote_and_reads_only_whats_new(tmp_path):
    crash = tmp_path / "crash.log"
    crash.write_text('  File "C:\\Users\\surar\\AppData\\Local\\Programs\\SprigCast\\main.py", line 4 in run\n')
    old = tmp_path / "sprigcast 2026-11-20.log"
    old.write_text(f"WARNING stream dropped: 'rtmp://ingest.example.net/app/{ENHANCED_KEY}'\n", encoding="utf-8")
    notes = tmp_path / "my notes.txt"
    notes.write_text(TWITCH_KEY)
    assert privacy.sweep(tmp_path) == 2
    assert _leaks(crash.read_text()) == [] and "C:\\Users\\you\\AppData" in crash.read_text()
    assert _leaks(old.read_text(encoding="utf-8")) == []
    assert notes.read_text() == TWITCH_KEY  # not ours: left alone
    with open(crash, "a") as f:
        f.write('  File "C:\\Users\\surar\\x.py", line 1\n')
    assert privacy.sweep(tmp_path) == 1
    assert "surar" not in crash.read_text()
    assert privacy.sweep(tmp_path) == 0


def test_stderr_of_a_windowed_start_is_scrubbed():
    out = io.StringIO()
    stream = privacy.ScrubbingStream(out)
    stream.write(f"Traceback: rtmp://live.twitch.tv/app/{TWITCH_KEY}\nC:\\Users\\surar\\x")
    stream.flush()
    assert _leaks(out.getvalue()) == []


def test_secrets_read_from_the_store_are_remembered(monkeypatch):
    from sprigcast.control import secret_store

    assert secret_store.unprotect(PASSWORD) == PASSWORD  # a plain value (not Windows): as it is
    assert PASSWORD not in privacy.scrub(f"wrong: {PASSWORD}")


def test_stream_keys_read_from_the_credential_store_are_remembered(monkeypatch):
    import keyring

    from sprigcast.output import keys

    monkeypatch.setattr(keyring, "get_password", lambda service, ref: "plainlookingsecret1234")
    assert keys.get_key("output-1") == "plainlookingsecret1234"
    assert "plainlookingsecret1234" not in privacy.scrub("sent plainlookingsecret1234")


# No code path puts them there in the first place ------------------------------------------------------------

SRC = Path(__file__).resolve().parents[1] / "src" / "sprigcast"
_LOG_CALL = re.compile(r"\blog(?:ger)?\.(?:debug|info|warning|error|exception|critical|log)\((?:[^()]|\([^()]*\))*\)",
                       re.DOTALL)
_PRIVATE_ARGS = re.compile(r"self\._key\b|self\._url\b|\.channel\b(?!s)|\bstream_key\b|\baccess_token\b|"
                           r"\brefresh_token\b|self\.password\b")


def test_no_log_call_passes_a_key_url_token_or_channel_name():
    found = []
    for path in SRC.rglob("*.py"):
        if "tools" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for call in _LOG_CALL.finditer(text):
            if _PRIVATE_ARGS.search(call.group(0)):
                line = text.count("\n", 0, call.start()) + 1
                found.append(f"{path.relative_to(SRC)}:{line}: {call.group(0)[:120]}")
    assert found == []


def test_start_up_cleans_the_logs_already_there(tmp_path, monkeypatch):
    pytest.importorskip("PySide6.QtCore")
    import threading

    from sprigcast.app import logbook

    leaked = f"WARNING stream Twitch dropped: 'rtmp://ingest.example.net/app/{ENHANCED_KEY}'\n"
    old = tmp_path / "sprigcast 2026-11-20.log"
    old.write_text(leaked * 5000, encoding="utf-8")  # big enough to be done a piece at a time
    report = tmp_path / "stream 2026-11-21 00-04-23.txt"
    report.write_text(leaked, encoding="utf-8")
    today = tmp_path / "sprigcast 2026-11-21.log"
    today.write_text('  File "C:\\Users\\surar\\x.py", line 1 in run\n', encoding="utf-8")
    monkeypatch.setenv("SPRIGCAST_LOG_DIR", str(tmp_path))
    monkeypatch.setattr(logbook, "_crash_file", object())
    monkeypatch.setattr(logbook, "keep_days", lambda: 0)
    path = logbook.setup(datetime(2026, 11, 21, 9, 0))
    try:
        assert "surar" not in today.read_text(encoding="utf-8")  # before this run writes to it
        for thread in [t for t in threading.enumerate() if t.name == "log-sweep"]:
            thread.join(30)
        assert _leaks(old.read_text(encoding="utf-8")) == [] and old.read_text().count("WARNING") == 5000
        assert _leaks(report.read_text(encoding="utf-8")) == []
    finally:
        logging.getLogger().removeHandler(logbook._handler)
        logbook._handler.close()
        logbook._handler = None
    assert path == today
