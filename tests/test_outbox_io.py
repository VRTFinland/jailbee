"""Tests for the bounded, text-only container outbox reader."""

import base64
import hashlib
import io
import json
import os
import stat
import tarfile
from pathlib import Path

import pytest

from jailbee.incus import IncusError
from jailbee.outbox_io import (
    ContainerIdentity,
    JournalAction,
    JournalError,
    JournalStore,
    OutboxReadError,
    container_identity,
    journal_key,
    proposal_digest,
    read_text_outbox,
)


def _archive(files: dict[str, bytes], *, extra: list[tarfile.TarInfo] | None = None) -> str:
    """Build a base64 tar as ``tar -cf - . | base64 -w0`` would emit it."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        root = tarfile.TarInfo(name="./")
        root.type = tarfile.DIRTYPE
        tar.addfile(root)
        for name, data in files.items():
            info = tarfile.TarInfo(name=f"./{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        for info in extra or []:
            tar.addfile(info)
    return base64.b64encode(buf.getvalue()).decode()


def _archive_with_hostile_members() -> str:
    absolute = tarfile.TarInfo(name="/etc/passwd")
    escape = tarfile.TarInfo(name="../../.ssh/id_ed25519")
    symlink = tarfile.TarInfo(name="./link.md")
    symlink.type = tarfile.SYMTYPE
    symlink.linkname = "/etc/shadow"
    nested = tarfile.TarInfo(name="./nested/body.md")
    device = tarfile.TarInfo(name="./null")
    device.type = tarfile.CHRTYPE
    device.devmajor, device.devminor = 1, 3
    return _archive({"001.json": b"{}"}, extra=[absolute, escape, symlink, nested, device])


def test_read_text_outbox_returns_plain_utf8_files_in_one_exec(mocker):
    incus = mocker.MagicMock()
    incus.exec.return_value = _archive({"001.json": b"{}", "body.md": b"text"})

    result = read_text_outbox(
        incus,
        "sample-feat",
        "/home/dev/.jailbee/issue-outbox",
        uid=1000,
        max_file_bytes=256 * 1024,
    )

    assert result == {"001.json": "{}", "body.md": "text"}
    assert incus.exec.call_args.args[1][-1] == "/home/dev/.jailbee/issue-outbox"


def test_read_text_outbox_skips_hostile_members_once(mocker):
    warn = mocker.Mock()
    incus = mocker.MagicMock(return_value=None)
    incus.exec.return_value = _archive_with_hostile_members()

    result = read_text_outbox(
        incus,
        "sample-feat",
        "/home/dev/.jailbee/issue-outbox",
        uid=1000,
        max_file_bytes=256 * 1024,
        warn_fn=warn,
    )

    assert result == {"001.json": "{}"}
    warn.assert_called_once_with("sample-feat: skipped 5 hostile outbox member(s)")


def test_read_text_outbox_is_empty_when_directory_is_missing(mocker):
    incus = mocker.MagicMock()
    incus.exec.return_value = ""

    assert read_text_outbox(incus, "c", "/missing", uid=1000, max_file_bytes=1) == {}


@pytest.mark.parametrize("raw", ["not base64 at all !!!", base64.b64encode(b"not a tar").decode()])
def test_read_text_outbox_rejects_invalid_archives(mocker, raw):
    incus = mocker.MagicMock()
    incus.exec.return_value = raw

    with pytest.raises(OutboxReadError, match=r"unreadable|corrupt"):
        read_text_outbox(incus, "c", "/outbox", uid=1000, max_file_bytes=1)


def test_read_text_outbox_skips_oversized_and_non_utf8_files(mocker):
    warn = mocker.Mock()
    incus = mocker.MagicMock()
    incus.exec.return_value = _archive({"ok": b"ok", "huge": b"xxx", "bad": b"\xff"})

    assert read_text_outbox(incus, "c", "/outbox", uid=1000, max_file_bytes=2, warn_fn=warn) == {
        "ok": "ok"
    }
    warn.assert_called_once_with("c: skipped 2 hostile outbox member(s)")


def test_read_text_outbox_preserves_root_exception_and_rejects_path_escapes(mocker):
    warn = mocker.Mock()
    incus = mocker.MagicMock()
    dotdot = tarfile.TarInfo(name="./../escape")
    incus.exec.return_value = _archive({"ok": b"ok"}, extra=[dotdot])

    assert read_text_outbox(incus, "c", "/outbox", uid=1000, max_file_bytes=2, warn_fn=warn) == {
        "ok": "ok"
    }
    warn.assert_called_once_with("c: skipped 1 hostile outbox member(s)")


def test_read_text_outbox_wraps_incus_error(mocker):
    incus = mocker.MagicMock()
    incus.exec.side_effect = IncusError("exit 1: Instance is not running")

    with pytest.raises(OutboxReadError, match="not running"):
        read_text_outbox(incus, "c", "/outbox", uid=1000, max_file_bytes=1)


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


def _journal_path(root: Path) -> Path:
    paths = [path for path in root.rglob("*.json") if "archive" not in path.parts]
    assert len(paths) == 1
    return paths[0]


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


def test_journal_round_trip_and_atomic_file_durability(tmp_path, mocker):
    fsync = mocker.spy(os, "fsync")
    replace_file = mocker.spy(os, "replace")
    key = journal_key(_identity(), "001.json")
    digest = proposal_digest("001.json", "{}", {})
    store = JournalStore(tmp_path)

    created = store.create(key, digest, action_count=3)
    prepared = store.mark_prepared(key, 0, repo="acme/app")
    applied = store.mark_applied(
        key,
        0,
        repo="acme/app",
        url="https://github.com/acme/app/issues/9",
        issue=9,
    )

    assert created.actions == ()
    assert prepared.actions == (JournalAction(index=0, state="prepared", repo="acme/app"),)
    assert applied.actions == (
        JournalAction(
            index=0,
            state="applied",
            repo="acme/app",
            url="https://github.com/acme/app/issues/9",
            issue=9,
        ),
    )
    assert store.load(key) == applied
    assert stat.S_IMODE(_journal_path(tmp_path).stat().st_mode) == 0o600
    assert replace_file.call_count == 3
    assert fsync.call_count >= 6  # file and containing directory for every write


def test_failed_atomic_replace_leaves_last_valid_journal(tmp_path, mocker):
    key = journal_key(_identity(), "001.json")
    digest = proposal_digest("001.json", "{}", {})
    store = JournalStore(tmp_path)
    original = store.create(key, digest, action_count=1)
    mocker.patch("jailbee.outbox_io.os.replace", side_effect=OSError("disk failure"))

    with pytest.raises(JournalError, match="write"):
        store.mark_prepared(key, 0, repo="acme/app")

    assert store.load(key) == original
    assert json.loads(_journal_path(tmp_path).read_text())["actions"] == []


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        (lambda data: data.update(schema=2), "schema"),
        (lambda data: data["identity"].update(full_name="other"), "identity"),
        (lambda data: data.update(manifest_name="other.json"), "manifest"),
        (lambda data: data.update(digest="not-a-digest"), "digest"),
        (lambda data: data.update(action_count=True), "action_count"),
        (
            lambda data: data["actions"].append(
                {
                    "index": 1,
                    "state": "prepared",
                    "repo": "acme/app",
                    "url": None,
                    "issue": None,
                    "detail": None,
                }
            ),
            "index",
        ),
    ],
)
def test_journal_load_rejects_invalid_schema_and_key_mismatches(tmp_path, mutation, match):
    key = journal_key(_identity(), "001.json")
    store = JournalStore(tmp_path)
    store.create(key, proposal_digest("001.json", "{}", {}), action_count=1)
    path = _journal_path(tmp_path)
    data = json.loads(path.read_text())
    mutation(data)
    path.write_text(json.dumps(data))

    with pytest.raises(JournalError, match=match):
        store.load(key)


def test_journal_create_rejects_digest_mismatch(tmp_path):
    key = journal_key(_identity(), "001.json")
    store = JournalStore(tmp_path)
    original = store.create(key, proposal_digest("001.json", "old", {}), action_count=1)

    with pytest.raises(JournalError, match="digest"):
        store.create(key, proposal_digest("001.json", "new", {}), action_count=1)

    assert store.load(key) == original


def test_journal_rejects_indices_and_illegal_transitions(tmp_path):
    key = journal_key(_identity(), "001.json")
    store = JournalStore(tmp_path)
    store.create(key, proposal_digest("001.json", "{}", {}), action_count=1)

    with pytest.raises(JournalError, match="index"):
        store.mark_prepared(key, 1, repo="acme/app")
    store.mark_prepared(key, 0, repo="acme/app")
    with pytest.raises(JournalError, match="transition"):
        store.mark_prepared(key, 0, repo="acme/app")
    with pytest.raises(JournalError, match="repo"):
        store.mark_applied(
            key,
            0,
            repo="acme/other",
            url="https://github.com/acme/other/issues/9",
            issue=9,
        )
    applied = store.mark_applied(
        key,
        0,
        repo="acme/app",
        url="https://github.com/acme/app/issues/9",
        issue=9,
    )
    with pytest.raises(JournalError, match="transition"):
        store.clear_prepared(key, 0)
    with pytest.raises(JournalError, match="transition"):
        store.resolve_retry(key, 0)
    assert store.load(key) == applied


def test_leftover_prepared_loads_as_uncertain_without_rewriting(tmp_path, mocker):
    key = journal_key(_identity(), "001.json")
    store = JournalStore(tmp_path)
    store.create(key, proposal_digest("001.json", "{}", {}), action_count=1)
    store.mark_prepared(key, 0, repo="acme/app")
    path = _journal_path(tmp_path)
    before = path.read_bytes()
    replace_file = mocker.spy(os, "replace")

    loaded = store.load(key)

    assert loaded is not None
    assert loaded.actions[0].state == "uncertain"
    assert loaded.actions[0].detail is not None
    assert path.read_bytes() == before
    replace_file.assert_not_called()


def test_uncertain_actions_can_be_resolved_as_applied_or_retried(tmp_path):
    key = journal_key(_identity(), "001.json")
    store = JournalStore(tmp_path)
    store.create(key, proposal_digest("001.json", "{}", {}), action_count=2)
    store.mark_prepared(key, 0, repo="acme/app")
    resolved = store.resolve_applied(
        key,
        0,
        url="https://github.com/acme/app/issues/9",
        issue=9,
    )
    assert resolved.actions[0].state == "applied"

    store.mark_prepared(key, 1, repo="acme/app")
    retried = store.resolve_retry(key, 1)
    assert [action.index for action in retried.actions] == [0]


def test_mark_uncertain_bounds_persisted_diagnostics(tmp_path):
    key = journal_key(_identity(), "001.json")
    store = JournalStore(tmp_path)
    store.create(key, proposal_digest("001.json", "{}", {}), action_count=1)
    store.mark_prepared(key, 0, repo="acme/app")
    token = "github_pat_super_secret"

    journal = store.mark_uncertain(
        key,
        0,
        repo="acme/app",
        detail=f"transport failed token={token}\nfull stderr and payload follow",
    )

    persisted = _journal_path(tmp_path).read_text()
    assert token not in persisted
    assert "full stderr" not in persisted
    assert journal.actions[0].detail is not None


def test_archive_moves_settled_journal_without_overwriting_history(tmp_path, mocker):
    key = journal_key(_identity(), "001.json")
    digest = proposal_digest("001.json", "{}", {})
    store = JournalStore(tmp_path)
    store.create(key, digest, action_count=2)
    store.mark_prepared(key, 0, repo="acme/app")
    store.mark_applied(
        key,
        0,
        repo="acme/app",
        url="https://github.com/acme/app/issues/9",
        issue=9,
    )
    source = _journal_path(tmp_path)
    contents = source.read_bytes()
    replace_file = mocker.spy(os, "replace")

    archived = store.archive(key)

    assert archived.parent.name == "archive"
    assert digest in archived.name
    assert archived.read_bytes() == contents
    assert not source.exists()
    assert replace_file.call_args.args == (source, archived)


def test_archive_refuses_uncertainty(tmp_path):
    key = journal_key(_identity(), "001.json")
    store = JournalStore(tmp_path)
    store.create(key, proposal_digest("001.json", "{}", {}), action_count=1)
    store.mark_prepared(key, 0, repo="acme/app")

    with pytest.raises(JournalError, match="uncertain"):
        store.archive(key)

    assert _journal_path(tmp_path).exists()
