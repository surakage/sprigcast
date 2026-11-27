"""Auto updater: finding a release, downloading and checking its installer, and never updating on air."""

import hashlib
import io
import os
import urllib.error
from pathlib import Path

import pytest

from sprigcast import __version__, update

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

SETUP = b"pretend installer " * 1000
SETUP_HASH = hashlib.sha256(SETUP).hexdigest()


def _release_json(version="9.0.0", **extra):
    data = {
        "tag_name": f"v{version}",
        "draft": False,
        "prerelease": False,
        "body": "### Live screen\n\n- Something nice.",
        "html_url": f"https://github.com/surakage/sprigcast/releases/tag/v{version}",
        "assets": [
            {"name": f"SprigCast-Setup-{version}.exe", "size": len(SETUP),
             "browser_download_url": f"https://example.test/SprigCast-Setup-{version}.exe"},
            {"name": "SHA256.txt", "size": 200, "browser_download_url": "https://example.test/SHA256.txt"},
        ],
    }
    data.update(extra)
    return data


class _Reply(io.BytesIO):
    def __init__(self, data: bytes):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}


def _opener(pages: dict[str, bytes]):
    def open_(request, timeout=None):
        url = request.full_url
        if url not in pages:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return _Reply(pages[url])
    return open_


# Versions and releases ----------------------------------------------------------------


def test_versions_compare_by_number():
    assert update.parse_version("v0.2.10") == (0, 2, 10)
    assert update.parse_version("0.3.0-beta") is None
    assert update.is_newer("0.2.10", "0.2.9")
    assert not update.is_newer("0.2.0", "0.2.0")
    assert not update.is_newer("0.1.9", "0.2.0")
    assert not update.is_newer("nightly", "0.2.0")


def test_a_release_needs_its_installer_and_hashes():
    release = update.release_from_json(_release_json())
    assert release.version == "9.0.0"
    assert release.installer_name == "SprigCast-Setup-9.0.0.exe"
    assert release.installer_size == len(SETUP)
    assert "Something nice" in release.notes
    no_hashes = _release_json()
    no_hashes["assets"] = no_hashes["assets"][:1]
    assert update.release_from_json(no_hashes) is None
    assert update.release_from_json(_release_json(prerelease=True)) is None
    assert update.release_from_json(_release_json(draft=True)) is None


def test_no_releases_yet_is_not_an_error():
    assert update.fetch_latest(opener=_opener({})) is None
    import json

    pages = {update.LATEST_URL: json.dumps(_release_json()).encode()}
    assert update.fetch_latest(opener=_opener(pages)).version == "9.0.0"


def test_hash_for_reads_the_workflows_sha256_file():
    text = (f"{'A' * 64}  sprigcast_camera.dll\n{SETUP_HASH.upper()}  SprigCast-Setup-9.0.0.exe\n"
            "Defender found nothing\n")
    assert update.hash_for(text, "SprigCast-Setup-9.0.0.exe") == SETUP_HASH
    assert update.hash_for(text, "SprigCast-Setup-1.0.0.exe") is None


# Download and checks ------------------------------------------------------------------


def test_download_hashes_and_never_leaves_a_half_file(tmp_path):
    seen = []
    target = tmp_path / "SprigCast-Setup-9.0.0.exe"
    got = update.download("https://example.test/s.exe", target, progress=lambda d, t: seen.append((d, t)),
                          opener=_opener({"https://example.test/s.exe": SETUP}))
    assert got == SETUP_HASH and target.read_bytes() == SETUP
    assert seen[-1] == (len(SETUP), len(SETUP))
    with pytest.raises(InterruptedError):
        update.download("https://example.test/s.exe", tmp_path / "b.exe", cancelled=lambda: True,
                        opener=_opener({"https://example.test/s.exe": SETUP}))
    assert sorted(p.name for p in tmp_path.iterdir()) == ["SprigCast-Setup-9.0.0.exe"]


def test_verify_wants_the_listed_hash_and_an_unbroken_signature(tmp_path):
    path = tmp_path / "s.exe"
    ok = lambda _p: "NotSigned"  # noqa: E731 (unsigned builds pass until signing starts)
    assert update.verify(path, SETUP_HASH, SETUP_HASH, signature=ok) is None
    assert update.verify(path, SETUP_HASH, SETUP_HASH, signature=lambda _p: "Valid") is None
    assert "fingerprint" in update.verify(path, SETUP_HASH, "0" * 64, signature=ok)
    assert "fingerprint" in update.verify(path, SETUP_HASH, None, signature=ok)
    assert "signature" in update.verify(path, SETUP_HASH, SETUP_HASH, signature=lambda _p: "HashMismatch")


def test_setup_waits_for_sprigcast_to_close_then_runs_silently():
    command = update.installer_command(Path("C:/Users/O'Neil/Updates/SprigCast-Setup-9.0.0.exe"), 4242, restart=True)
    script = command[-1]
    assert command[0] == "powershell.exe" and "Hidden" in command
    assert script.index("Get-Process -Id 4242") < script.index("Start-Process")
    assert "O''Neil" in script  # quoted for PowerShell
    assert "'/SILENT'" in script and "'/restartapp=1'" in script
    assert "restartapp" not in update.installer_command(Path("s.exe"), 1, restart=False)[-1]


def test_only_an_installed_copy_updates_itself(tmp_path):
    (tmp_path / "python").mkdir()
    exe = tmp_path / "python" / "pythonw.exe"
    exe.write_bytes(b"")
    assert update.installed_dir(str(exe)) is None  # running from source
    (tmp_path / "unins000.exe").write_bytes(b"")
    assert update.installed_dir(str(exe)) == tmp_path.resolve()


def test_setup_restarts_the_app_only_for_in_app_updates():
    iss = (Path(__file__).parent.parent / "packaging" / "sprigcast.iss").read_text(encoding="utf-8")
    assert "skipifnotsilent; Check: RestartAfterUpdate" in iss
    assert "{param:restartapp|0}" in iss


def test_release_notes_come_from_the_changelog():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "release_notes", Path(__file__).parent.parent / "packaging" / "release_notes.py")
    notes = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(notes)
    text = "# Changelog\n\n## 0.3.0\n\n- New.\n\n## 0.2.0 (tested)\n\n- Old.\n"
    assert notes.section(text, "0.3.0") == "- New."
    assert notes.section(text, "0.2.0") == "- Old."
    assert notes.section(text, "0.2") is None
    beta = "## Beta 0.4 (not released yet)\n\n### Added\n\n- Newer.\n\n## Version 1 (2027-01-01)\n\n- Whole.\n"
    assert notes.section(beta, "0.4.0") == "### Added\n\n- Newer."
    assert notes.section(beta, "1.0.0") == "- Whole."
    assert notes.section(beta, "0.4.1") is None
    changelog = (Path(__file__).parent.parent / "CHANGELOG.md").read_text(encoding="utf-8")
    assert notes.section(changelog, __version__), "every released version needs a CHANGELOG section"


# The window side ----------------------------------------------------------------------


@pytest.fixture
def qt(monkeypatch):
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtCore import QCoreApplication, QSettings
    from PySide6.QtWidgets import QApplication, QMainWindow

    app = QApplication.instance() or QApplication([])
    QCoreApplication.setOrganizationName("SprigCastTests")
    QCoreApplication.setApplicationName("UpdatesTest")
    QSettings().clear()
    window = QMainWindow()
    yield app, window
    QSettings().clear()


def _updater(qt, monkeypatch, busy=False):
    from sprigcast.app import updates

    _app, window = qt
    state = {"busy": busy}
    updater = updates.Updater(window, busy=lambda: state["busy"], installed=True)
    shown, messages = [], []
    monkeypatch.setattr(updater, "show_prompt", shown.append)
    monkeypatch.setattr(updater, "_message", lambda title, text: messages.append(title))
    return updater, state, shown, messages


def test_a_new_version_waits_until_you_are_off_air(qt, monkeypatch):
    updater, state, shown, _ = _updater(qt, monkeypatch, busy=True)
    release = update.release_from_json(_release_json())
    updater._on_checked(release, False, "")
    assert shown == [] and updater._waiting is release
    updater._show_waiting()
    assert shown == []  # still live
    state["busy"] = False
    updater._show_waiting()
    assert shown == [release] and updater._waiting is None


def test_skipped_versions_only_hide_automatic_checks(qt, monkeypatch):
    from PySide6.QtCore import QSettings

    from sprigcast.app import updates

    updater, _state, shown, messages = _updater(qt, monkeypatch)
    release = update.release_from_json(_release_json())
    QSettings().setValue(updates.SKIPPED, "9.0.0")
    updater._on_checked(release, False, "")
    assert shown == []
    updater._on_checked(release, True, "")
    assert shown == [release]
    current = update.release_from_json(_release_json(__version__))
    updater._on_checked(current, True, "")
    updater._on_checked(None, True, "offline")
    assert messages == ["You're up to date", "Couldn't check for updates"]
    updater._on_checked(None, False, "offline")  # automatic checks stay quiet
    assert len(messages) == 2


def test_automatic_checks_respect_the_setting_and_the_wait(qt, monkeypatch):
    from PySide6.QtCore import QSettings

    from sprigcast.app import updates

    updater, *_ = _updater(qt, monkeypatch)
    started = []
    monkeypatch.setattr(updates.threading, "Thread", lambda **kw: type("T", (), {
        "start": lambda self: started.append(kw["args"])})())
    QSettings().setValue(updates.AUTO_CHECK, False)
    updater.check(manual=False)
    assert started == []
    QSettings().setValue(updates.AUTO_CHECK, True)
    updater.check(manual=False)
    assert started == [(False,)]
    updater._checking = False
    updater.check(manual=False)  # checked moments ago
    assert started == [(False,)]
    updater.check(manual=True)  # asking always checks
    assert started == [(False,), (True,)]


def test_a_finished_download_while_live_installs_on_quit(qt, monkeypatch, tmp_path):
    from sprigcast.app import updates

    updater, state, *_ = _updater(qt, monkeypatch)
    installer = tmp_path / "SprigCast-Setup-9.0.0.exe"
    installer.write_bytes(SETUP)
    updater.restart = True
    state["busy"] = True  # went live while it downloaded
    updater._on_downloaded(str(installer), "")
    assert updater.restart is False and updater.pending == installer
    launched = []
    monkeypatch.setattr(updates.update, "launch_installer", lambda path, pid, restart: launched.append(restart))
    assert updater.install_on_quit() is True
    assert launched == [False] and updater.pending is None
    assert updater.install_on_quit() is False


def test_a_bad_download_is_never_kept(qt, monkeypatch, tmp_path):
    updater, *_ = _updater(qt, monkeypatch)
    updater._on_downloaded(str(tmp_path / "x.exe"), "The download doesn't match the release's fingerprint.")
    assert updater.pending is None
    assert updater.install_on_quit() is False


def test_the_prompt_never_offers_update_now_on_air(qt):
    from sprigcast.app.updates import UpdatePrompt

    release = update.release_from_json(_release_json())
    assert UpdatePrompt(release, busy=False, installed=True).go_button.text() == "Update now"
    assert UpdatePrompt(release, busy=True, installed=True).go_button.text() == "When I close"
    assert UpdatePrompt(release, busy=False, installed=False).go_button.text() == "Open download page"


def test_settings_has_check_now_and_the_auto_switch(qt, monkeypatch):
    import keyring
    from PySide6.QtCore import QSettings

    from sprigcast.app import updates
    from sprigcast.app.settings_window import SettingsWindow
    from sprigcast.core.scene import Canvas, SceneCollection

    monkeypatch.setattr(keyring, "get_password", lambda svc, ref: None)
    asked = []
    col = SceneCollection(canvases=[Canvas(id="horizontal", width=1920, height=1080)])
    window = SettingsWindow(col, default_recordings_folder="C:/Videos", encoder="NVENC", audio_devices={},
                            check_updates=lambda: asked.append(1))
    window.check_updates_button.click()
    assert asked == [1]
    window.auto_updates.setChecked(False)
    assert window.apply()
    assert QSettings().value(updates.AUTO_CHECK, True, type=bool) is False


def test_the_update_popup_holds_its_notes_and_buttons():
    """It opened at the pop-up's smallest size, the notes over the buttons and no sloth (2026-11-22)."""
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QWidget

    from sprigcast import update
    from sprigcast.app import updates

    QApplication.instance() or QApplication([])
    release = update.Release("9.0.0", "### Added\n\n- One thing\n- Another", "https://example.invalid", "x", 1, "y")
    host = QWidget()
    seen = {}

    def fake_modal(dialog):
        dialog.show()
        QApplication.processEvents()
        prompt = dialog.findChild(updates.UpdatePrompt)
        seen["size"] = (dialog.width(), dialog.height())
        seen["notes_below_title"] = prompt.notes.geometry().top() > 0
        seen["buttons_below_notes"] = prompt.go_button.geometry().top() >= prompt.notes.geometry().bottom()
        seen["html"] = prompt.notes.toHtml()
        dialog.reject()

    old = updates.modal
    updates.modal = fake_modal
    try:
        updates.Updater(host, lambda: False, installed=True).show_prompt(release)
    finally:
        updates.modal = old
    assert seen["size"][0] >= updates.UpdatePrompt.WIDTH and seen["size"][1] > updates.UpdatePrompt.NOTES_HEIGHT + 150
    assert seen["buttons_below_notes"] and "Another" in seen["html"]


# Test builds and channels --------------------------------------------------------------


def _test_json(minor=5, number=1, **extra):
    data = _release_json("0.%d.0" % minor, tag_name=f"test-0.{minor}-{number}", prerelease=True,
                         body=f"Test build {number}")
    data.update(extra)
    return data


def test_test_builds_count_only_on_the_test_channels():
    assert update.release_from_json(_test_json()) is None  # the stable reading skips pre-releases
    build = update.release_from_json(_test_json(number=3), tests=True)
    assert build.version == "0.5.0" and build.test == 3 and build.label == "Beta 0.5 test build 3"
    assert build.skip_id == "test-0.5-3" and build.installer_name == "SprigCast-Setup-0.5.0.exe"
    assert update.release_from_json(_test_json(tag_name="test-2026-11-27j"), tests=True) is None  # old date tags
    assert update.release_from_json(_test_json(number=0, tag_name="test-0.5-0"), tests=True) is None


def test_a_published_version_beats_every_test_build_of_it():
    one, nine = (update.release_from_json(_test_json(number=n), tests=True) for n in (1, 9))
    stable = update.release_from_json(_release_json("0.5.0"))
    next_test = update.release_from_json(_test_json(minor=6, number=1), tests=True)
    assert one.key < nine.key < stable.key < next_test.key
    test_two = update.current_key("0.5.0", {"kind": "test", "test": 2})
    assert update.newer_than_this(nine, test_two) and not update.newer_than_this(one, test_two)
    assert update.newer_than_this(stable, test_two)
    assert not update.newer_than_this(nine, update.current_key("0.5.0", {}))  # a release copy never goes back
    assert update.newer_than_this(stable, update.current_key("0.5.0", {"kind": "test", "run": "449"}))
    assert update.pick([one, nine, stable, next_test], update.STABLE) is stable
    assert update.pick([one, nine, stable], update.TESTS) is nine
    assert update.pick([one, nine, stable], update.BOTH) is stable
    assert update.pick([], update.BOTH) is None


def test_the_test_channels_read_the_release_list():
    import json

    listing = [_test_json(number=2), _test_json(number=1), _release_json("0.4.0"),
               _test_json(tag_name="test-2026-11-27j"), {"draft": True, "tag_name": "test-0.5-3", "prerelease": True}]
    opener = _opener({update.RELEASES_URL: json.dumps(listing).encode(),
                      update.LATEST_URL: json.dumps(_release_json("0.4.0")).encode()})
    assert update.fetch_newest(update.BOTH, opener=opener).tag == "test-0.5-2"
    assert update.fetch_newest(update.TESTS, opener=opener).test == 2
    assert update.fetch_newest(update.STABLE, opener=opener).version == "0.4.0"
    assert update.fetch_newest("nonsense", opener=opener).version == "0.4.0"
    assert update.fetch_newest(update.BOTH, opener=_opener({})) is None


def test_test_builds_default_to_both_and_check_more_often(monkeypatch):
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QSettings

    from sprigcast.app import updates

    assert update.default_channel({}) == update.STABLE
    assert update.default_channel({"kind": "test", "test": 1}) == update.BOTH
    monkeypatch.setattr(update, "build_stamp", lambda: {"kind": "test", "test": 4})
    QSettings().remove(updates.CHANNEL)
    assert updates.channel() == update.BOTH and updates.check_every_s() == updates.TEST_CHECK_EVERY_S
    QSettings().setValue(updates.CHANNEL, update.TESTS)
    assert updates.channel() == update.TESTS
    QSettings().setValue(updates.CHANNEL, "weird")
    assert updates.channel() == update.BOTH
    QSettings().remove(updates.CHANNEL)
    assert not updates.channel_choice_shown()  # it joins Settings with Version 1
    monkeypatch.setattr(updates, "__version__", "1.0.0")
    assert updates.channel_choice_shown()


def test_the_prompt_names_test_builds(qt, monkeypatch):
    from PySide6.QtCore import QSettings

    from sprigcast.app import updates

    monkeypatch.setattr(updates, "test_build", lambda: 2)
    prompt = updates.UpdatePrompt(update.release_from_json(_test_json(number=3), tests=True), False, True)
    texts = [label.text() for label in prompt.findChildren(updates.QLabel)]
    assert "SprigCast Beta 0.5 test build 3 is ready" in texts
    assert any(t.startswith("You have Beta 0.5 test build 2") for t in texts)
    updater, _state, shown, _ = _updater(qt, monkeypatch)
    build = update.release_from_json(_test_json(number=3), tests=True)
    monkeypatch.setattr(update, "build_stamp", lambda: {"kind": "test", "test": 2})
    QSettings().setValue(updates.SKIPPED, "test-0.5-3")
    updater._on_checked(build, False, "")
    assert shown == []
    QSettings().remove(updates.SKIPPED)
    updater._on_checked(build, False, "")
    assert shown == [build]


def test_a_tester_who_skipped_builds_sees_every_change_since_theirs():
    builds = [update.release_from_json(_test_json(number=n, body=f"- Change {n}"), tests=True) for n in (1, 2, 3, 4)]
    on_two = update.current_key("0.5.0", {"kind": "test", "test": 2})
    newest = update.with_notes_since(builds, builds[3], on_two)
    assert newest.test == 4 and newest.notes.index("test build 4") < newest.notes.index("test build 3")
    assert "Change 3" in newest.notes and "Change 2" not in newest.notes
    assert update.with_notes_since(builds, builds[3], update.current_key("0.5.0", {"kind": "test", "test": 3})) \
        is builds[3]  # one build behind: its own notes
    stable = update.release_from_json(_release_json("0.5.0"))
    assert update.with_notes_since(builds + [stable], stable, on_two) is stable
