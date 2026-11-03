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
