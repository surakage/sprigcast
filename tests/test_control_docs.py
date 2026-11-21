"""docs/control-api.md keeps up with the API: every action has a row and every feature is explained. Other work
adds actions and features all the time; this is what says so when the page is left behind."""

from pathlib import Path

from sprigcast.control.native import FEATURES, NativeApi

DOC = (Path(__file__).parent.parent / "docs" / "control-api.md").read_text(encoding="utf-8")


def _actions():
    found = (getattr(getattr(NativeApi, name), "_action", None) for name in dir(NativeApi))
    return [a for a in found if a]


def test_every_action_has_a_row():
    missing = [name for name, _about, _fields in _actions() if f"| `{name}` |" not in DOC]
    assert not missing, f"docs/control-api.md has no row for: {', '.join(missing)}"


def test_every_field_of_an_action_is_in_its_row():
    rows = {line.split("`")[1]: line for line in DOC.splitlines() if line.startswith("| `")}
    wrong = [f"{name}: {field}" for name, _about, fields in _actions() for field in fields
             if f"`{field}`" not in rows.get(name, "")]
    assert not wrong, f"docs/control-api.md rows miss fields: {', '.join(wrong)}"


def test_every_feature_is_explained():
    missing = [name for name in (*FEATURES, "browser.h264") if f"| `{name}` |" not in DOC]
    assert not missing, f"docs/control-api.md doesn't explain the features: {', '.join(missing)}"
