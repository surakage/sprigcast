

def test_a_long_name_is_cut_in_the_file_name(tmp_path):
    """A 300 letter bloom name made a file name no disk takes (sim PC, 2026-11-28)."""
    from sprigcast.app import screenshot

    path = screenshot.path_for(tmp_path, "x" * 300)
    assert len(path.name) < 110
    path.write_bytes(b"")  # the disk takes it
    assert screenshot.path_for(tmp_path, "horizontal").name.startswith("screenshot horizontal ")
