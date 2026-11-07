"""Tests for the bounded, text-only container outbox reader."""

import base64
import hashlib
import io
import tarfile

import pytest

from jailbee.incus import IncusError
from jailbee.outbox_io import (
    ContainerIdentity,
    JournalError,
    OutboxReadError,
    container_identity,
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
        "/home/dev/.jailbee/pr-outbox",
        uid=1000,
        max_file_bytes=256 * 1024,
    )

    assert result == {"001.json": "{}", "body.md": "text"}
    assert incus.exec.call_args.args[1][-1] == "/home/dev/.jailbee/pr-outbox"


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
    assert (
        """find . -maxdepth 1 -type f -name "$1" ! -name '.*' -size "-${size}c" -print0""" in script
    )
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
        "/home/dev/.jailbee/pr-outbox",
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
