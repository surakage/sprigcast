from sprigcast.app import safe_zones


def test_zones_scale_and_only_apply_to_portrait():
    assert safe_zones.zones("TikTok", 1920, 1080) == []
    assert safe_zones.zones("Off", 1080, 1920) == []
    tiktok = safe_zones.zones("TikTok", 540, 960)  # half size
    assert tiktok[0] == ("Top tabs", 0.0, 0.0, 540.0, 80.0)


def test_all_platforms_merges_into_three_boxes_and_leaves_a_clear_middle():
    boxes = safe_zones.zones(safe_zones.ALL, 1080, 1920)
    assert [b[0] for b in boxes] == ["Top bars", "Side buttons", "Captions and names"]
    top, side, bottom = boxes
    assert top[4] == 220 and bottom[2] == 1500 and side[1] == 920
    x, y, w, h = safe_zones.clear_area(safe_zones.ALL, 1080, 1920)
    assert (x, y, w, h) == (160, 220, 760, 1280)
