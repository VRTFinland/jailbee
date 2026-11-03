from jailbee.inbox.render import jq_tostring, ls_line, pretty, tsv


def test_tsv_escapes_like_jq():
    assert tsv(["a\\b", "c\td", "e\nf\r"]) == "a\\\\b\tc\\td\te\\nf\\r"


def test_pretty_escapes_del():
    assert pretty({"k": "a\x7fb"}) == '{\n  "k": "a\\u007fb"\n}'


def test_jq_tostring_compact():
    assert jq_tostring({"a": [1, 2]}) == '{"a":[1,2]}'
    assert jq_tostring(None) == "null"


def test_ls_line_truncates_codepoints():
    assert ls_line({"body": "ä" * 70}).split("\t")[-1] == "ä" * 60


def test_ls_line_false_kind_is_empty():
    assert ls_line({"kind": False}).split("\t")[3] == ""


def test_compact_json_escapes_del_before_ls_cleaning():
    assert jq_tostring({"key": "a\x7fb"}) == '{"key":"a\\u007fb"}'
    assert ls_line({"body": {"key": "a\x7fb"}}).split("\t")[-1] == '{"key":"a\\\\u007fb"}'


def test_numeric_exponent_matches_jq():
    assert jq_tostring({"x": 1e-7}) == '{"x":1E-7}'
    assert pretty({"x": 1e-7}) == '{\n  "x": 1E-7\n}'
