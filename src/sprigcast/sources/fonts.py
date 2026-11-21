"""The fonts on this PC as families with their bold and italic files, for the text leaf.

A text's `font` setting is a font file ("segoeui.ttf") as it always was; Bold and Italic pick that family's bold or
italic file when it has one (segoeuib.ttf, segoeuii.ttf), and are drawn thicker or slanted when it hasn't.

On Windows the list comes from the registry's font table (all users and this user's own), a few milliseconds and
no font file opened. Elsewhere (tests) the usual font folders are looked through.
"""

from __future__ import annotations

import os
import re
import sys
from functools import lru_cache
from pathlib import Path

STYLES = ("regular", "bold", "italic", "bold italic")
_STYLE_WORDS = (  # the end of a registry name -> which file it is; longest first
    ("bold italic", "bold italic"), ("bold oblique", "bold italic"), ("italic", "italic"), ("oblique", "italic"),
    ("bold", "bold"), ("regular", "regular"),
)
FONT_FILES = (".ttf", ".otf", ".ttc")


def _windows_fonts_folder() -> Path:
    return Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"


def split_name(name: str) -> tuple[str, str]:
    """A registry font name -> (family, style): "Arial Bold Italic (TrueType)" -> ("Arial", "bold italic")."""
    name = re.sub(r"\s*\((TrueType|OpenType|All res)\)\s*$", "", name, flags=re.IGNORECASE).strip()
    name = name.split(" & ")[0].strip()  # a collection: "Cambria & Cambria Math" -> its first family
    low = name.lower()
    for words, style in _STYLE_WORDS:
        if low.endswith(" " + words):
            return name[: -len(words) - 1].strip(), style
    return name, "regular"


def _registry_fonts() -> list[tuple[str, str]]:
    import winreg

    out: list[tuple[str, str]] = []
    key_path = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            key = winreg.OpenKey(root, key_path)
        except OSError:
            continue
        with key:
            i = 0
            while True:
                try:
                    name, value, _ = winreg.EnumValue(key, i)
                except OSError:
                    break
                i += 1
                if isinstance(value, str) and value.lower().endswith(FONT_FILES):
                    out.append((name, value))
    return out


def _scanned_fonts() -> list[tuple[str, str]]:
    from PIL import ImageFont

    out: list[tuple[str, str]] = []
    folders = [Path("/usr/share/fonts"), Path("/usr/local/share/fonts"), Path.home() / ".fonts",
               Path("/Library/Fonts"), Path("/System/Library/Fonts")]
    for folder in folders:
        if not folder.is_dir():
            continue
        for path in sorted(folder.rglob("*")):
            if path.suffix.lower() not in FONT_FILES[:2]:
                continue
            try:
                family, style = ImageFont.truetype(str(path), 12).getname()
            except OSError:
                continue
            style = (style or "Regular").lower().replace("oblique", "italic")
            out.append((f"{family} {style.title()}" if style != "regular" else family, str(path)))
    return out


@lru_cache(maxsize=1)
def families() -> dict[str, dict[str, str]]:
    """{family: {style: font file}}, families A to Z. Files under the Windows fonts folder are bare names."""
    try:
        listed = _registry_fonts() if sys.platform == "win32" else _scanned_fonts()
    except Exception:
        listed = []
    found: dict[str, dict[str, str]] = {}
    for name, file in listed:
        family, style = split_name(name)
        if not family:
            continue
        found.setdefault(family, {}).setdefault(style, file)
    for styles in found.values():  # a family with only Bold, say: its first file stands in as its regular
        if "regular" not in styles:
            styles["regular"] = next(iter(styles.values()))
    return dict(sorted(found.items(), key=lambda kv: kv[0].lower()))


def _same_file(a: str, b: str) -> bool:
    return Path(a).name.lower() == Path(b).name.lower()


def family_of(font: str) -> str | None:
    """The family a font file belongs to ("arialbd.ttf" -> "Arial"), or None when it isn't installed."""
    for family, styles in families().items():
        if any(_same_file(font, f) for f in styles.values()):
            return family
    return None


@lru_cache(maxsize=64)
def resolve(font: str, bold: bool, italic: bool) -> tuple[str, bool, bool]:
    """(the file to draw with, draw it thicker, draw it slanted) for a font file and its Bold and Italic."""
    if not bold and not italic:
        return font, False, False
    family = family_of(font) if font else None
    styles = families().get(family, {}) if family else {}
    want = "bold italic" if bold and italic else "bold" if bold else "italic"
    if want in styles:
        return styles[want], False, False
    if bold and italic and "bold" in styles:
        return styles["bold"], False, True
    if bold and italic and "italic" in styles:
        return styles["italic"], True, False
    return font, bold, italic
