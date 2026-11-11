"""The words the app uses for its two kinds of scene, kept in one place so they can change in one line.

In code and in saved files the kinds stay "show" (goes on air) and "part" (a reusable piece placed
inside other scenes); only what people read uses these words.
"""

SHOW, SHOWS = "bloom", "blooms"  # a scene that goes on air
PART, PARTS = "sprig", "sprigs"  # a reusable piece placed inside blooms (or other sprigs)
PART_MARK = "❧"  # shown before a sprig's name in lists


def cap(word: str) -> str:
    return word[:1].upper() + word[1:]


def a(word: str) -> str:
    """The word with "a" or "an" in front."""
    return ("an " if word[:1].lower() in "aeiou" else "a ") + word
