"""Tests for the shared outbox digests and instance identity."""

import hashlib

import pytest

from jailbee.outbox_io import (
    ContainerIdentity,
    JournalError,
    container_identity,
    proposal_digest,
)


def _length_prefixed_digest(*values: str) -> str:
    digest = hashlib.sha256()
    for value in values:
        encoded = value.encode()
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _identity() -> ContainerIdentity:
    return ContainerIdentity(
        full_name="sample-feature",
        created_at="2026-09-18T09:30:00.123456789Z",
    )


def test_proposal_digest_length_prefixes_exact_manifest_and_referenced_bodies():
    expected = _length_prefixed_digest(
        "001.json",
        '{"body_file":"a.md"}\n',
        "a.md",
        "alpha",
        "z.md",
        "omega",
    )

    result = proposal_digest(
        "001.json",
        '{"body_file":"a.md"}\n',
        {"z.md": "omega", "a.md": "alpha"},
    )

    assert result == expected
    assert proposal_digest("ab", "c", {}) != proposal_digest("a", "bc", {})
    assert proposal_digest("x", "y", {"ab": "c"}) != proposal_digest("x", "y", {"a": "bc"})


def test_proposal_digest_changes_only_for_proposal_inputs():
    manifest = '{"body_file":"body.md"}'
    digest = proposal_digest("001.json", manifest, {"body.md": "body"})

    assert proposal_digest("001.json", manifest + "\n", {"body.md": "body"}) != digest
    assert proposal_digest("001.json", manifest, {"renamed.md": "body"}) != digest
    assert proposal_digest("001.json", manifest, {"body.md": "changed"}) != digest
    outbox = {"001.json": manifest, "body.md": "body", "unrelated.md": "ignored"}
    assert proposal_digest("001.json", outbox["001.json"], {"body.md": outbox["body.md"]}) == digest


def test_container_identity_uses_full_name_and_raw_creation_time(mocker):
    incus = mocker.MagicMock()
    incus.list_containers.return_value = [
        {"name": "sample-other", "created_at": "2025-01-01T00:00:00Z"},
        {
            "name": "sample-feature",
            "created_at": "2026-09-18T09:30:00.123456789Z",
        },
    ]

    assert container_identity(incus, "sample-feature") == _identity()
    incus.list_containers.assert_called_once_with()


@pytest.mark.parametrize(
    "containers",
    [
        [],
        [{"name": "sample-feature"}],
        [{"name": "sample-feature", "created_at": None}],
        [{"name": "sample-feature", "created_at": "0001-01-01T00:00:00Z"}],
    ],
)
def test_container_identity_rejects_missing_or_unstable_identity(mocker, containers):
    incus = mocker.MagicMock()
    incus.list_containers.return_value = containers

    with pytest.raises(JournalError, match=r"sample-feature.*(not found|creation time)"):
        container_identity(incus, "sample-feature")
