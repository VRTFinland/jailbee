import json
from dataclasses import FrozenInstanceError

import pytest

from jailbee.issue_manifest import IssueManifestError, parse_manifest


def manifest(body, attachments=None):
    root = {
        "version": 1,
        "actions": [{"type": "create", "repo": ".", "ref": "a", "title": "T", "body": body}],
    }
    if attachments is not None:
        root["attachments"] = attachments
    return parse_manifest("test.json", json.dumps(root), {})


def descriptor(**changes):
    return {
        "blob": "a" * 32,
        "name": "image.png",
        "content_type": "image/png",
        "size": 100,
        "sha256": "a" * 64,
    } | changes


def test_attachment_descriptor_and_repeated_references():
    result = manifest("![a](attachment:img) ![b](attachment:img)", {"img": descriptor()})
    assert result.attachments["img"].to_dict() == descriptor()
    with pytest.raises(FrozenInstanceError):
        result.attachments["img"].size = 1


def test_undeclared_reference_rejected_but_code_ignored():
    with pytest.raises(IssueManifestError):
        manifest("![x](attachment:img)")
    manifest("`![x](attachment:img)`")


@pytest.mark.parametrize(
    "change",
    [
        {"blob": "../file"},
        {"name": "../a.png"},
        {"name": "a.jpg"},
        {"content_type": "image/svg+xml"},
        {"size": 0},
        {"size": 10000001},
        {"size": True},
        {"sha256": "A" * 64},
        {"extra": 1},
    ],
)
def test_invalid_metadata_rejected(change):
    with pytest.raises(IssueManifestError):
        manifest("![x](attachment:img)", {"img": descriptor(**change)})


def test_unused_declarations_rejected_without_an_attachment_count_quota():
    with pytest.raises(IssueManifestError):
        manifest("plain", {"img": descriptor()})
    # Simulated declared accounting exceeds 100 MB without allocating image bytes.
    declarations = {f"i{i}": descriptor(size=10_000_000) for i in range(15)}
    body = " ".join(f"![x](attachment:i{i})" for i in range(15))
    assert len(manifest(body, declarations).attachments) == 15


def test_expected_body_is_not_an_attachment_reference():
    root = {
        "version": 1,
        "actions": [
            {
                "type": "edit",
                "repo": ".",
                "issue": 1,
                "body": "new",
                "expected": {"body": "![x](attachment:old)"},
            }
        ],
    }
    parse_manifest("test.json", json.dumps(root), {})
