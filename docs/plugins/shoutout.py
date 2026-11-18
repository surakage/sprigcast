"""Shout-out: a text card that says "Go follow NAME!", changed from Streamer.bot, a hotkey or a right-click.

Copy this file into SprigCast's plugins folder (Tools → Plugin store → Your plugins → Open plugins folder) and restart SprigCast.
Then add it with + → Plugins → Shout-out.
"""

from sprigcast.control.native import ApiError, register_action, send_event
from sprigcast.core.source import register
from sprigcast.plugin_hooks import register_hotkeys, register_item_menu
from sprigcast.sources.text import TextSource

KIND = "shoutout"


@register(KIND)
class ShoutoutSource(TextSource):
    label = "Shout-out"  # its name under + → Plugins
    defaults = {**TextSource.defaults, "text": "", "name": "", "before": "Go follow "}  # its Settings fields

    def _current_text(self):
        name = str(self.settings.get("name", "")).strip()
        return f"{self.settings.get('before', '')}{name}!" if name else ""


def _cards(api):
    cards = [s for s in api.col.sources if s.kind == KIND]
    if not cards:
        raise ApiError("not_found", "Add a Shout-out first (+ → Plugins → Shout-out).")
    return cards


def show(api, data):
    name = str(data.get("name", "")).strip().lstrip("@")
    if not name:
        raise ApiError("bad_request", "name is empty.")
    for card in _cards(api):
        api.run("source.set", {"source": card.id, "settings": {"name": name}})
    send_event(api, "shoutout.shown", {"name": name})
    return {"name": name}


def clear(api, data):
    for card in _cards(api):
        api.run("source.set", {"source": card.id, "settings": {"name": ""}})
    send_event(api, "shoutout.cleared", {})
    return {}


register_action("shoutout.show", "Show a shout-out for someone.", {"name": "who to shout out"}, show)
register_action("shoutout.clear", "Hide the shout-out.", {}, clear)


def _hotkeys(collection):
    return [("shoutout:clear", "Shout-out: Clear")]


def _press(api, hotkey_id):
    if hotkey_id == "shoutout:clear":
        clear(api, {})


register_hotkeys(_hotkeys, _press)


def _menu(api, source, add):
    if source.kind == KIND:
        add("Clear shout-out", lambda: clear(api, {}))


register_item_menu(_menu)
