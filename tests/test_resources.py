def test_private_working_set_is_cheap_and_safe_off_windows():
    import sys

    from sprigcast.app import resources

    if sys.platform != "win32":
        assert resources.windows_private_working_set() is None  # no kernel32: falls back quietly
    else:
        value = resources.windows_private_working_set()
        assert value is not None and value > 10_000_000
