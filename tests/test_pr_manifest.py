"""The shared PR schema is pure, with host branch validation still injectable."""

import json

import pytest


@pytest.mark.parametrize(
    ("ref", "valid"),
    [
        ("main", True),
        ("refs/heads/topic", True),
        ("feature/one", True),
        ("HEAD", False),
        ("-bad", False),
        ("a..b", False),
        ("a@{b", False),
        ("a.lock", False),
        ("a.lock/b", False),
        (".hidden/x", False),
        ("a//b", False),
        ("a/", False),
        ("a.", False),
        ("a b", False),
        ("a\tb", False),
        ("a\x7fb", False),
        ("a?b", False),
        ("a[b", False),
        ("a\\b", False),
        ("@", True),
        ("a" * 300, True),
        ("topic/ends./next", True),
    ],
)
def test_pure_branch_ref_validation(ref, valid):
    from jailbee.pr_manifest import valid_branch_ref

    assert valid_branch_ref(ref) is valid


@pytest.mark.parametrize("field,value", [("source", "refs/heads/a..b"), ("base", "HEAD")])
def test_pure_parser_rejects_invalid_refs(field, value):
    from jailbee.pr_manifest import ManifestError, parse_manifest

    raw = {
        "version": 1,
        "repo": "o/r",
        "pr": 7,
        "source": "refs/heads/topic",
        "source_sha": "a" * 40,
        "base": "main",
        "actions": [],
    }
    raw[field] = value
    with pytest.raises(ManifestError, match="not a valid"):
        parse_manifest("001-source.json", json.dumps(raw), {})


def test_host_wrapper_keeps_injected_git_validation(mocker):
    from jailbee import pr_manifest, pr_outbox

    raw = {
        "version": 1,
        "repo": "o/r",
        "pr": 7,
        "source": "refs/heads/topic",
        "source_sha": "a" * 40,
        "base": "main",
        "actions": [],
    }
    text = json.dumps(raw)
    mocker.patch("jailbee.pr_outbox.git.check_ref_format", return_value=True)
    assert pr_outbox.parse_manifest("001-source.json", text, {}) == pr_manifest.parse_manifest(
        "001-source.json", text, {}
    )
    mocker.patch("jailbee.git.check_ref_format", return_value=False)
    with pytest.raises(pr_outbox.ManifestError, match="source is not a valid"):
        pr_outbox.parse_manifest("001-source.json", text, {})
    assert pr_manifest.parse_manifest("001-source.json", text, {}).source == "refs/heads/topic"
    assert pr_outbox.Manifest is pr_manifest.Manifest
    assert pr_outbox.DescriptionAction is pr_manifest.DescriptionAction
