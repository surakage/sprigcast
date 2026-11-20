"""On-screen text uses American spelling (user, 2026-11-19: "centre is spelled center in America").

Checks every one-line string in the app's code. Names the code reads back (saved values, aliases people may type,
log lines) are listed below and stay as they are."""
import re
import tokenize
from pathlib import Path

BRITISH = re.compile(r"(?i)\b(centre[sd]?|centring|colour(s|ed|ful|ing)?|behaviour[s]?|minimis\w*|maximis\w*|"
                     r"optimis\w*|organis\w*|recognis\w*|customis\w*|favourite\w*|greyed|cancelled|cancelling|"
                     r"labelled|neighbour\w*|licence)\b")
# file:line: why it stays
KEEP = {
    "update.py:140",  # an internal error, never shown
    "output/nvenc_d3d11.py:417",  # a log line
    "core/style.py:60",  # custom looks also accept "colour" when people type it
}


def test_on_screen_text_is_american():
    root = Path(__file__).resolve().parent.parent / "src" / "sprigcast"
    found = []
    for path in sorted(root.rglob("*.py")):
        with open(path, encoding="utf-8") as fh:
            for tok in tokenize.generate_tokens(fh.readline):
                kinds = (tokenize.STRING, getattr(tokenize, "FSTRING_MIDDLE", -1))
                body = tok.string.lstrip("rbfuRBFU")
                if tok.type not in kinds or tok.start[0] != tok.end[0] or body.startswith(('"""', "'''")):
                    continue  # docstrings are notes for people reading the code
                where = f"{path.relative_to(root).as_posix()}:{tok.start[0]}"
                if where not in KEEP and BRITISH.search(tok.string):
                    found.append(f"{where}: {tok.string}")
    assert not found, "British spelling in on-screen text:\n" + "\n".join(found)
