import pytest

from jailbee.attachments import AttachmentError, references, rewrite, validate_references


def test_inline_reference_is_rewritten_without_changing_title():
    body = '![alt]( <attachment:pic> "title" )'
    url = "https://github.com/user-attachments/assets/12345678-1234-1234-1234-123456789abc"
    refs = references(body)
    assert len(refs) == 1
    assert refs[0].id == "pic"
    validate_references([body], ["pic"])
    assert rewrite(body, {"pic": url}) == f'![alt]( <{url}> "title" )'


def test_skips_code_and_preserves_literal_mentions():
    body = (
        "attachment:prose `![x](attachment:no)`\n```\n[x](attachment:no)\n```\n"
        "    [x](attachment:no)\n[x](attachment:yes)"
    )
    assert [r.id for r in references(body)] == ["yes"]


def test_nested_labels_escaped_delimiters_and_many_references():
    body = r"\[no](attachment:no) " + " ".join(f"[a [b]](attachment:id{i})" for i in range(15))
    assert [r.id for r in references(body)] == [f"id{i}" for i in range(15)]


@pytest.mark.parametrize(
    "body",
    [
        "[x](attachment:)",
        "[x](attachment:bad.id)",
        "[x]: attachment:abc",
        '<img src="attachment:abc">',
    ],
)
def test_rejects_unsupported_or_malformed_active_references(body):
    with pytest.raises(AttachmentError):
        references(body)


def test_validate_across_bodies_and_reject_unused_or_missing():
    validate_references(["[x](attachment:a)", "[y](attachment:b)"], ["a", "b"])
    with pytest.raises(AttachmentError):
        validate_references(["[x](attachment:a)"], ["b"])


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/user-attachments/assets/12345678-1234-1234-1234-123456789abc",
        "https://github.com/user-attachments/assets/12345678-1234-1234-1234-123456789abc?x=1",
        "https://user@github.com/user-attachments/assets/12345678-1234-1234-1234-123456789abc",
    ],
)
def test_rejects_noncanonical_urls(url):
    with pytest.raises(AttachmentError):
        rewrite("[x](attachment:a)", {"a": url})


@pytest.mark.parametrize(
    "body",
    [
        "<attachment:abc>",
        '<img\n src="attachment:abc">',
        "[x]:\n  <attachment:abc>",
        "[x](attachment&#58;abc)",
        "[x](attachment:abc\\.suffix)",
    ],
)
def test_rejects_other_active_reserved_forms(body):
    with pytest.raises(AttachmentError):
        references(body)


def test_exact_backtick_runs_and_tilde_fence():
    body = "`` code ` [x](attachment:no) ``\n  ~~~\n[x](attachment:no)\n  ~~~\n[x](attachment:yes)"
    assert [r.id for r in references(body)] == ["yes"]


def test_reference_definition_rewritten_once_for_repeated_uses():
    body = "[one][img] ![two][img]\n\n[img]: <attachment:photo> 'title'"
    url = "https://github.com/user-attachments/assets/12345678-1234-1234-1234-123456789abc"
    assert [ref.id for ref in references(body)] == ["photo"]
    assert rewrite(body, {"photo": url}) == body.replace("attachment:photo", url)


def test_nested_blockquote_code_is_not_active():
    body = "> ```\n> [x](attachment:ignored)\n> ```\n> [x](attachment:used)"
    assert [ref.id for ref in references(body)] == ["used"]


def test_code_delimiters_do_not_change_active_destination():
    body = "`attachment:a` [x](attachment:b) ` [y](attachment:c) `"
    assert [r.id for r in references(body)] == ["b"]


def test_attachment_text_in_reference_label():
    body = "[attachment:foo]: attachment:bar\n\n[attachment:foo]"
    with pytest.raises(AttachmentError):
        references(body)


def test_attachment_mentions_in_https_paths_are_not_reserved_destinations():
    body = "[ordinary](https://example.com/attachment:bar)"
    assert references(body) == ()
    assert rewrite(body, {}) == body


def test_escaped_parentheses_in_destination_are_rejected():
    with pytest.raises(AttachmentError):
        references(r"[x](attachment:a\(b\))")
