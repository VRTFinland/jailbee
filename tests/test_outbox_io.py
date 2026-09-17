"""Tests for the bounded, text-only container outbox reader."""

import base64
import io
import tarfile

import pytest

from jailbee.incus import IncusError
from jailbee.outbox_io import OutboxReadError, read_text_outbox


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
