"""Tests for the bounded, text-only container outbox reader."""

import base64
import fcntl
import hashlib
import io
import json
import multiprocessing
import os
import stat
import tarfile
from multiprocessing.connection import Connection
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


def test_read_text_outbox_default_argv_is_unchanged(mocker):
    """The issue/PR outbox callers keep the plain whole-directory archive."""
    incus = mocker.MagicMock()
    incus.exec.return_value = ""
    read_text_outbox(incus, "c", "/outbox", uid=1000, max_file_bytes=10)
    assert incus.exec.call_args.args[1] == [
        "bash",
        "-c",
        'cd "$1" 2>/dev/null || exit 0; tar -cf - . | base64 -w0',
        "bash",
        "/outbox",
    ]


def test_read_text_outbox_bounded_filters_in_the_container(mocker):
    incus = mocker.MagicMock()
    incus.exec_capped.return_value = _archive({"001.json": b"{}"})
    result = read_text_outbox(
        incus,
        "c",
        "/outbox",
        uid=1000,
        max_file_bytes=100,
        select=[("*.json", 3), ("*.md", 5)],
    )
    assert result == {"001.json": "{}"}
    incus.exec.assert_not_called()
    argv = incus.exec_capped.call_args.args[1]
    script = argv[2]
    assert argv[:2] == ["bash", "-c"]
    total = (3 + 5) * (100 + 2048) + 10240
    assert argv[3:] == ["bash", "/outbox", "101", str(total), "*.json", "3", "*.md", "5"]
    # Per (glob, count) pair: regular top-level files below the cap, first N by name.
    assert 'find . -maxdepth 1 -type f -name "$1" -size "-${size}c" -print0' in script
    assert 'LC_ALL=C sort -z | head -z -n "$2"; shift 2; done' in script
    assert "tar --null --no-recursion -T - -cf -" in script
    assert '| head -c "$total" |' in script
    # Values never reach the script text.
    assert "/outbox" not in script and "101" not in script and "*.json" not in script


def _bounded(incus, **kw):
    return read_text_outbox(
        incus, "c", "/outbox", uid=1000, max_file_bytes=100, select=[("*.json", 2)], **kw
    )


def test_read_text_outbox_bounded_caps_captured_stdout(mocker):
    """The host-side cap is the base64 size of the archive cap, enforced while reading."""
    incus = mocker.MagicMock()
    incus.exec_capped.return_value = ""
    _bounded(incus)
    total = 2 * (100 + 2048) + 10240
    assert incus.exec_capped.call_args.kwargs["max_bytes"] == 4 * -(-total // 3)
    assert incus.exec_capped.call_args.kwargs["timeout"] == 15


def test_read_text_outbox_bounded_oversized_stdout_is_a_read_error(mocker):
    incus = mocker.MagicMock()
    incus.exec_capped.side_effect = IncusError("output exceeded 1 bytes")
    with pytest.raises(OutboxReadError, match="exceeded"):
        _bounded(incus)


@pytest.mark.parametrize("mode", ["w:gz", "w:xz", "w:bz2"])
def test_read_text_outbox_bounded_refuses_compressed_archives(mocker, mode):
    """A small compressed archive can expand enormously; bounded reads take plain tar only."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode=mode) as tar:
        info = tarfile.TarInfo(name="./001.json")
        info.size = 2
        tar.addfile(info, io.BytesIO(b"{}"))
    incus = mocker.MagicMock()
    incus.exec_capped.return_value = base64.b64encode(buf.getvalue()).decode()
    with pytest.raises(OutboxReadError, match="corrupt"):
        _bounded(incus)


def test_read_text_outbox_bounded_refuses_too_many_members(mocker):
    """More members than the selection could ever archive: the container lied."""
    incus = mocker.MagicMock()
    incus.exec_capped.return_value = _archive({f"{i}.json": b"{}" for i in range(5)})
    with pytest.raises(OutboxReadError, match="more than 2 members"):
        _bounded(incus)


def test_read_text_outbox_bounded_accepts_the_selection_count(mocker):
    incus = mocker.MagicMock()
    incus.exec_capped.return_value = _archive({"1.json": b"{}", "2.json": b"{}"})
    assert _bounded(incus) == {"1.json": "{}", "2.json": "{}"}


@pytest.mark.parametrize("select", [[("", 1)], [("a/*.md", 1)], [("*.md", 0)]])
def test_read_text_outbox_rejects_bad_selection(mocker, select):
    incus = mocker.MagicMock()
    with pytest.raises(ValueError, match="invalid outbox selection"):
        read_text_outbox(incus, "c", "/o", uid=1, max_file_bytes=1, select=select)
    incus.exec.assert_not_called()


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


def _probe_exclusive_lock(path: str, connection: Connection) -> None:
    descriptor = os.open(path, os.O_RDWR)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            connection.send(False)
        else:
            connection.send(True)
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)
        connection.close()


def _other_process_can_lock(path: Path) -> bool:
    context = multiprocessing.get_context("fork")
    receive, send = context.Pipe(duplex=False)
    process = context.Process(target=_probe_exclusive_lock, args=(str(path), send))
    process.start()
    send.close()
    assert receive.poll(5), "lock probe process did not report"
    result = receive.recv()
    process.join(5)
    assert process.exitcode == 0
    receive.close()
    return result


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


def test_journal_lock_serializes_other_process_and_is_reentrant(tmp_path):
    key = journal_key(_identity(), "001.json")
    store = JournalStore(tmp_path)
    store.create(key, proposal_digest("001.json", "{}", {}), action_count=1)

    with store.lock(key):
        (lock_path,) = tmp_path.rglob("*.lock")
        assert not _other_process_can_lock(lock_path)
        store.mark_prepared(key, 0, repo="acme/app")
        store.mark_applied(
            key,
            0,
            repo="acme/app",
            url="https://github.com/acme/app/issues/9",
            issue=9,
        )

    assert _other_process_can_lock(lock_path)
    loaded = store.load(key)
    assert loaded is not None
    assert loaded.actions[0].state == "applied"


def test_transition_uses_the_public_journal_lock(tmp_path, mocker):
    key = journal_key(_identity(), "001.json")
    store = JournalStore(tmp_path)
    store.create(key, proposal_digest("001.json", "{}", {}), action_count=1)
    lock = mocker.spy(store, "lock")

    store.mark_prepared(key, 0, repo="acme/app")

    lock.assert_called_once_with(key)


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


@pytest.mark.parametrize(
    "detail",
    [
        "Authorization: Bearer bearer-super-secret",
        "failed at https://user:url-secret@github.com/acme/app",
        "gh: arbitrary sensitive stderr with request={private payload}",
        "token=github_pat_super_secret\nfull stderr and payload follow",
    ],
)
def test_mark_uncertain_persists_only_whitelisted_diagnostics(tmp_path, detail):
    key = journal_key(_identity(), "001.json")
    store = JournalStore(tmp_path)
    store.create(key, proposal_digest("001.json", "{}", {}), action_count=1)
    store.mark_prepared(key, 0, repo="acme/app")

    journal = store.mark_uncertain(
        key,
        0,
        repo="acme/app",
        detail=detail,
    )

    persisted = _journal_path(tmp_path).read_text()
    assert detail not in persisted
    assert "super-secret" not in persisted
    assert "url-secret" not in persisted
    assert "private payload" not in persisted
    assert journal.actions[0].detail == "GitHub mutation outcome is uncertain"


def test_mark_uncertain_preserves_a_whitelisted_safe_summary(tmp_path):
    key = journal_key(_identity(), "001.json")
    store = JournalStore(tmp_path)
    store.create(key, proposal_digest("001.json", "{}", {}), action_count=1)
    store.mark_prepared(key, 0, repo="acme/app")

    journal = store.mark_uncertain(
        key,
        0,
        repo="acme/app",
        detail="GitHub mutation transport failed after dispatch",
    )

    assert journal.actions[0].detail == "GitHub mutation transport failed after dispatch"


def test_journal_load_rejects_an_untrusted_uncertain_detail(tmp_path):
    key = journal_key(_identity(), "001.json")
    store = JournalStore(tmp_path)
    store.create(key, proposal_digest("001.json", "{}", {}), action_count=1)
    store.mark_prepared(key, 0, repo="acme/app")
    path = _journal_path(tmp_path)
    data = json.loads(path.read_text())
    data["actions"][0].update(
        state="uncertain",
        detail="Authorization: Bearer journal-secret",
    )
    path.write_text(json.dumps(data))

    with pytest.raises(JournalError, match="detail"):
        store.load(key)


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


def test_append_applied_log_passes_content_as_base64_argument(mocker):
    from jailbee import outbox_io

    incus = mocker.Mock()
    lines = ['{"ref":"$(touch /tmp/injected)`id`", "url":"https://github.com/a/b/issues/1"}']
    outbox_io.append_applied_log(incus, "box", "/outbox ' odd", lines, uid=42)
    args = incus.exec.call_args.args
    command = args[1]
    assert args[0] == "box"
    assert command[:2] == ["bash", "-c"]
    assert "/outbox ' odd" not in command[2]
    assert "touch /tmp/injected" not in command[2]
    assert command[-2] == "/outbox ' odd"
    assert base64.b64decode(command[-1]).decode() == lines[0] + "\n"
    assert incus.exec.call_args.kwargs["uid"] == 42


def test_delete_outbox_files_passes_validated_names_after_option_separator(mocker):
    from jailbee import outbox_io

    incus = mocker.Mock()
    outbox_io.delete_outbox_files(
        incus, "box", "/outbox ' odd", ["a.json", "-body.md", "$(id).md"], uid=None
    )
    assert incus.exec.call_args.args == (
        "box",
        [
            "rm",
            "-f",
            "--",
            "/outbox ' odd/a.json",
            "/outbox ' odd/-body.md",
            "/outbox ' odd/$(id).md",
        ],
    )
    assert incus.exec.call_args.kwargs["uid"] is None


@pytest.mark.parametrize("name", ["", ".", "..", "../secret", "/secret", "dir/file", "bad\x00name"])
def test_delete_outbox_files_rejects_paths_before_any_deletion(mocker, name):
    from jailbee import outbox_io

    incus = mocker.Mock()
    with pytest.raises(ValueError):
        outbox_io.delete_outbox_files(incus, "box", "/outbox", ["ok.json", name], uid=None)
    incus.exec.assert_not_called()


@pytest.mark.parametrize("count", [3, 10**30])
@pytest.mark.parametrize("owned_log", [False, True])
def test_persisted_declared_count_never_allocates_inspection_or_preparation(
    tmp_path, mocker, make_cfg, count, owned_log
):
    from jailbee import issue_github, issue_outbox
    from jailbee.outbox.inspect import build_views
    from jailbee.outbox_io import issue_receipt_line
    from tests.outbox_support import store

    name = "one.json"
    action = (
        {"type": "comment", "repo": ".", "issue": 7, "body_file": "applied.log"}
        if owned_log
        else {"type": "comment", "repo": ".", "issue": 7, "body": "Original"}
    )
    text = json.dumps({"version": 1, "actions": [action]})
    files = {name: text, **({"applied.log": "Original"} if owned_log else {})}
    key = journal_key(_identity(), name)
    journals = JournalStore(tmp_path / "journals")
    bodies = {"applied.log": "Original"} if owned_log else {}
    original_digest = proposal_digest(name, text, bodies)
    journals.create(key, original_digest, count)
    journals.mark_prepared(key, 0, repo="acme/app")
    journal = journals.mark_applied(key, 0, repo="acme/app", url="https://receipt", issue=7)
    if owned_log:
        files["applied.log"] += (
            issue_receipt_line(name, journal.actions[0], "2026-10-01T12:00:00Z") + "\n"
        )
    fresh = JournalStore(journals.root)
    assert fresh.load(key).action_count == count
    views = build_views(key.identity, (store("issue", files),), journal_store=fresh)
    assert views[0].state == "uncertain"
    assert views[0].error and views[0].edit_block
    cfg = make_cfg(tmp_path)
    incus = mocker.Mock()
    incus.list_containers.return_value = [
        {"name": key.identity.full_name, "created_at": key.identity.created_at}
    ]
    mocker.patch.object(
        issue_outbox, "read_issue_outbox", return_value=issue_outbox.OutboxSnapshot(files)
    )
    mocker.patch.object(
        issue_outbox,
        "resolve_repo_targets",
        return_value={".": issue_outbox.RepoTarget(".", cfg.repo_root, "acme/app")},
    )
    remote = mocker.patch.object(
        issue_github, "current_login", side_effect=AssertionError("remote reached")
    )
    with pytest.raises(
        issue_outbox.IssueGateError, match=r"count differs|changed after recorded progress"
    ):
        issue_outbox.prepare_batch(
            cfg, incus, key.identity.full_name, (name,), uid=1000, journal_store=fresh
        )
    remote.assert_not_called()
    assert fresh.load(key).digest == original_digest


def _settled_log_journal(original, count=2):
    from jailbee.outbox_io import IssueJournal, issue_receipt_line

    name = "one space pr=7.json"
    key = journal_key(_identity(), name)
    bodies = {"applied.log": original, "other.md": "Other"}
    text = '{"version":1,"actions":[]}'
    actions = tuple(
        JournalAction(i, "applied", "acme/app", "https://receipt", 7) for i in range(count)
    )
    journal = IssueJournal(key.identity, name, proposal_digest(name, text, bodies), count, actions)
    block = "".join(issue_receipt_line(name, a, "2026-10-01T12:00:00Z") + "\n" for a in actions)
    return key, text, bodies, journal, block


@pytest.mark.parametrize("original", ["", "Original", "Original\n", "äö\r\nEnd"])
@pytest.mark.parametrize("repetitions", [1, 3])
def test_owned_issue_log_proof_restores_exact_original_digest(original, repetitions):
    from jailbee.outbox_io import issue_proposal_digest

    key, text, bodies, journal, block = _settled_log_journal(original)
    changed = bodies | {"applied.log": original + block * repetitions}
    assert proposal_digest(key.manifest_name, text, changed) != journal.digest
    assert issue_proposal_digest(key, text, changed, journal) == journal.digest
    assert changed["applied.log"] == original + block * repetitions


@pytest.mark.parametrize(
    "change",
    [
        "identity",
        "name",
        "partial",
        "uncertain",
        "prepared",
        "empty",
        "count",
        "huge-count",
        "order",
        "duplicate-index",
        "missing-index",
        "manifest",
        "other-body",
        "original",
        "append",
        "prepend",
        "insert",
        "url",
        "issue",
        "repo",
        "index",
        "timestamp",
        "format",
        "extra-field",
        "incomplete",
    ],
)
def test_owned_issue_log_proof_rejects_unproven_changes(change):
    from dataclasses import replace

    from jailbee.outbox_io import issue_proposal_digest

    key, text, bodies, journal, block = _settled_log_journal("Original")
    changed = bodies | {"applied.log": "Original" + block}
    if change == "identity":
        key = replace(key, identity=replace(key.identity, created_at="other"))
    elif change == "name":
        journal = replace(journal, manifest_name="other.json")
    elif change in ("partial", "empty"):
        journal = replace(journal, actions=journal.actions[:1] if change == "partial" else ())
    elif change in ("uncertain", "prepared"):
        journal = replace(
            journal, actions=(replace(journal.actions[0], state=change), journal.actions[1])
        )
    elif change in ("count", "huge-count"):
        journal = replace(journal, action_count=3 if change == "count" else 10**30)
    elif change == "order":
        journal = replace(journal, actions=tuple(reversed(journal.actions)))
    elif change in ("duplicate-index", "missing-index"):
        journal = replace(
            journal,
            actions=(
                journal.actions[0],
                replace(journal.actions[1], index=0 if change == "duplicate-index" else 2),
            ),
        )
    elif change == "manifest":
        text += "\n"
    elif change == "other-body":
        changed["other.md"] += "Changed"
    elif change in ("original", "append", "prepend", "insert"):
        changed["applied.log"] = {
            "original": "Changed" + block,
            "append": "Original" + block + "external\n",
            "prepend": "external\nOriginal" + block,
            "insert": "Original" + block + "external\n" + block,
        }[change]
    elif change == "incomplete":
        changed["applied.log"] = "Original" + block.splitlines(keepends=True)[0]
    else:
        lines = block.splitlines()
        record = json.loads(lines[-1])
        if change == "format":
            lines[-1] = json.dumps(record, separators=(",", ":"))
        else:
            record.update(
                {
                    "url": {"url": "https://other"},
                    "issue": {"issue": 99},
                    "repo": {"repo": "other/app"},
                    "index": {"index": True},
                    "timestamp": {"timestamp": "2026-99-99T00:00:00Z"},
                    "extra-field": {"extra": "unproven"},
                }[change]
            )
            lines[-1] = json.dumps(record, sort_keys=True)
        changed["applied.log"] = "Original" + "\n".join(lines) + "\n"
    if change in ("order", "duplicate-index", "missing-index"):
        from jailbee.outbox_io import issue_receipt_line

        # Keep bytes canonical for this journal so only completeness rejects it.
        mutated_block = "".join(
            issue_receipt_line(key.manifest_name, action, "2026-10-01T12:00:00Z") + "\n"
            for action in journal.actions
        )
        changed["applied.log"] = "Original" + mutated_block
        assert len(journal.actions) == journal.action_count
        assert journal.digest == proposal_digest(key.manifest_name, text, bodies)
    raw = proposal_digest(key.manifest_name, text, changed)
    assert issue_proposal_digest(key, text, changed, journal) == raw
    assert raw != journal.digest
