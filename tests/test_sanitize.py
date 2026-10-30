from contacts_mcp.sanitize import clean, clean_json


def test_clean_strips_controls_and_format_chars() -> None:
    assert clean("a\u200bb\u202ec\x00d\x9fe\tf\ng") == "abcde\tf\ng"


def test_clean_strips_tag_characters() -> None:
    assert clean("ok\U000e0041\U000e0042\U000e007f") == "ok"


def test_clean_strips_invisible_fillers_and_lone_surrogates() -> None:
    assert clean("a\u034fb\u3164c\u115fd\ud800e") == "abcde"


def test_clean_keeps_one_variation_selector_after_a_character() -> None:
    assert clean("\u2764\ufe0f") == "\u2764\ufe0f"
    assert clean("x\ufe0f\ufe0e\ufe01y") == "x\ufe0fy"
    assert clean("\ufe0fstart") == "start"
    assert clean("x\U000e0100\U000e01efy") == "xy"


def test_clean_turns_line_and_paragraph_separators_into_newlines() -> None:
    assert clean("a\u2028b\u2029c") == "a\nb\nc"


def test_clean_keeps_printable_text() -> None:
    assert clean("Ada Lovelace, née Byron") == "Ada Lovelace, née Byron"


def test_clean_json_cleans_keys_and_nested_values() -> None:
    dirty = {"na\u200bme": ["x\u2066y", {"k": "v\x07"}], "n": 3}
    assert clean_json(dirty) == {"name": ["xy", {"k": "v"}], "n": 3}
