

def test_the_window_is_put_back_fitted_to_its_screen():
    """PC test, 2026-11-20: "Unable to set geometry 1918x1006-1919+97" at start-up: the saved window, with its
    frame, was bigger than the screen's free area. It's fitted before it's set."""
    from sprigcast.app.main_window import fit_window

    free = (-1920, 64, 1920, 1032)  # the left monitor's free area (taskbar taken off)
    x, y, width, height = fit_window((-1919, 97, 1918, 1006), free)
    assert width + 16 <= 1920 and height + 40 <= 1032  # frame and title bar fit too
    assert x - 8 >= -1920 and x + width + 8 <= 0 and y - 32 >= 64 and y + height + 8 <= 64 + 1032
    assert fit_window((100, 100, 1280, 720), (0, 0, 1920, 1040)) == (100, 100, 1280, 720)  # one that fits stays
    assert fit_window((5000, 5000, 1280, 720), (0, 0, 1920, 1040))[:2] == (1920 - 8 - 1280, 1040 - 8 - 720)
