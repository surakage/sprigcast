from sprigcast.app import page_requests as pr


def test_only_scheme_and_host_are_written():
    assert pr.origin_of("https://source.soundalerts.com/alert/abc123?key=secret") == "https://source.soundalerts.com"
    assert pr.origin_of("") == "-"
    assert pr.origin_of("https://user:pw@a.com/x") == "https://a.com"
    text = pr.line("POST", "identitytoolkit.googleapis.com", "/v1/accounts:signUp",
                   "https://source.soundalerts.com/alert/abc123", "", "https://source.soundalerts.com",
                   "https://source.soundalerts.com", "Xhr")
    assert "abc123" not in text and "key" not in text
    assert "Referer -" in text and "Origin https://source.soundalerts.com" in text
    assert "POST identitytoolkit.googleapis.com/v1/accounts:signUp from https://source.soundalerts.com" in text


def test_a_page_that_keeps_trying_never_fills_the_log():
    counter = pr.Counter()
    assert sum(counter.take("https://a.com") for _ in range(10)) == pr.PER_PAGE
    assert sum(counter.take(f"https://{i}.com") for i in range(100)) == pr.TOTAL - pr.PER_PAGE


def test_a_change_hours_in_is_still_written():
    counter = pr.Counter()
    good = ("-", "https://source.soundalerts.com", "https://source.soundalerts.com")
    assert sum(counter.take("https://a.com", good) for _ in range(50)) == pr.PER_PAGE
    assert counter.take("https://a.com", ("-", "-", "-"))  # Origin gone: written, past the cap
    assert not counter.take("https://a.com", ("-", "-", "-"))  # the same again: not
    assert counter.take("https://a.com", good)  # and back
