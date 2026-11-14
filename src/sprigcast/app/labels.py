"""Text for Qt buttons, tick boxes and menu items, which read a single & as "underline the next letter"."""


def literal(text: str) -> str:
    """A user's name (a sound called "SFX & alerts", a bloom called "Q&A") shown as typed."""
    return text.replace("&", "&&")
