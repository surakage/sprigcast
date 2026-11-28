"""The start-up line also says how long ago the program itself was started (test build 16: 86 s was logged for a
start that took three and a half minutes from the double-click)."""


def test_since_launch_counts_from_the_process_start():
    import re

    from sprigcast.app.main import since_launch

    said = since_launch()
    found = re.fullmatch(r"; (\d+\.\d) s since the program was started", said)
    assert found and 0 <= float(found.group(1)) < 3600
