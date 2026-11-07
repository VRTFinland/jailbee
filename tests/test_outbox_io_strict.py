"""Strict snapshot transport tests, including the real local shell helper."""

import base64
import io
import os
import subprocess
import tarfile

import pytest

from jailbee.incus import IncusError, IncusTimeoutError
from jailbee.outbox.io import READ_TIMEOUT, read_store, store_directory
from jailbee.outbox.models import OutboxExecutionError


def encoded_snapshot(files, *, rejected=(), metadata=None):
    record = (
        metadata
        if metadata is not None
        else b"v1\0present\0" + b"".join(b"R\0" + name.encode() + b"\0" for name in rejected)
    )
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for name, content in {".jailbee-snapshot": record, **files}.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(content)
            archive.addfile(entry, io.BytesIO(content))
    return base64.b64encode(stream.getvalue()).decode()


def test_skipped_progress_is_not_absent(mocker):
    incus = mocker.Mock()
    incus.exec.return_value = encoded_snapshot(
        {"001.json": b"{}"}, rejected=("001.json.progress.json", "applied.log")
    )
    got = read_store(incus, "c", "pr", uid=1000)
    assert got.files == (("001.json", "{}"),)
    assert got.rejected == ("001.json.progress.json", "applied.log")
    assert incus.exec.call_args.kwargs == {"uid": 1000, "timeout": READ_TIMEOUT}
    assert READ_TIMEOUT == 30


@pytest.mark.parametrize("failure", [IncusError("denied"), IncusTimeoutError("timeout")])
def test_execution_failure_is_not_empty(mocker, failure):
    incus = mocker.Mock()
    incus.exec.side_effect = failure
    with pytest.raises(OutboxExecutionError):
        read_store(incus, "c", "issue", uid=None)


@pytest.mark.parametrize(
    "metadata", [b"", b"v2\0present\0", b"v1\0present\0X\0x\0", b"v1\0missing\0R\0x\0"]
)
def test_metadata_fails_closed(mocker, metadata):
    incus = mocker.Mock()
    incus.exec.return_value = encoded_snapshot({}, metadata=metadata)
    with pytest.raises(OutboxExecutionError):
        read_store(incus, "c", "pr", uid=None)


@pytest.mark.parametrize(
    "content", [b"\xff", b"x" * (256 * 1024 + 1)], ids=["non_utf8", "oversized"]
)
def test_rejected_sidecars_are_retained(mocker, content):
    incus = mocker.Mock()
    incus.exec.return_value = encoded_snapshot({"001.json.progress.json": content})
    got = read_store(incus, "c", "pr", uid=None)
    assert got.files == ()
    assert got.rejected == ("001.json.progress.json",)
    assert got.warnings


def local_reader(mocker, tmp_path):
    directory = tmp_path / "home" / "dev" / ".jailbee" / "pr-outbox"
    mocker.patch("jailbee.outbox.io.store_directory", return_value=str(directory))
    incus = mocker.Mock()

    def execute(container, command, **kwargs):
        result = subprocess.run(command, capture_output=True, text=True, timeout=5)
        if result.returncode:
            raise IncusError(result.stderr)
        return result.stdout

    incus.exec.side_effect = execute
    return incus, directory


def test_local_missing_and_regular_files(mocker, tmp_path):
    incus, directory = local_reader(mocker, tmp_path)
    assert read_store(incus, "c", "pr", uid=None).files == ()
    assert not directory.exists()
    directory.mkdir(parents=True)
    (directory / "001.json").write_text("{}")
    (directory / "body.md").write_text("body")
    assert dict(read_store(incus, "c", "pr", uid=None).files) == {
        "001.json": "{}",
        "body.md": "body",
    }


@pytest.mark.parametrize("special", ["symlink", "fifo", "hardlink", "directory"])
def test_local_rejected_progress_inventory(mocker, tmp_path, special):
    incus, directory = local_reader(mocker, tmp_path)
    directory.mkdir(parents=True)
    outside = tmp_path / "secret"
    outside.write_text("credentials")
    sidecar = directory / "001.json.progress.json"
    if special == "symlink":
        sidecar.symlink_to(outside)
    elif special == "fifo":
        os.mkfifo(sidecar)
    elif special == "hardlink":
        os.link(outside, sidecar)
    else:
        sidecar.mkdir()
    got = read_store(incus, "c", "pr", uid=None)
    assert sidecar.name in got.rejected
    assert not got.files


@pytest.mark.parametrize("ancestor", ["home", "dev", ".jailbee", "pr-outbox"])
def test_local_symlink_ancestors_fail(mocker, tmp_path, ancestor):
    incus, directory = local_reader(mocker, tmp_path)
    directory.mkdir(parents=True)
    target = next(p for p in (directory, *directory.parents) if p.name == ancestor)
    moved = tmp_path / "moved"
    target.rename(moved)
    target.symlink_to(moved)
    with pytest.raises(OutboxExecutionError):
        read_store(incus, "c", "pr", uid=None)


def test_local_inaccessible_is_not_missing(mocker, tmp_path):
    incus, directory = local_reader(mocker, tmp_path)
    directory.mkdir(parents=True)
    directory.chmod(0)
    try:
        if os.geteuid() == 0:
            pytest.skip("root bypasses permission checks")
        with pytest.raises(OutboxExecutionError):
            read_store(incus, "c", "pr", uid=None)
    finally:
        directory.chmod(0o700)


def test_local_aggregate_overflow(mocker, tmp_path):
    incus, directory = local_reader(mocker, tmp_path)
    directory.mkdir(parents=True)
    for index in range(25):
        (directory / f"{index}.json").write_bytes(b"x" * (256 * 1024))
    with pytest.raises(OutboxExecutionError, match="overflow"):
        read_store(incus, "c", "pr", uid=None)


@pytest.mark.parametrize(
    "content", [b"\xff", b"x" * (256 * 1024 + 1)], ids=["non_utf8", "oversized"]
)
def test_local_invalid_sidecars_and_log(mocker, tmp_path, content):
    incus, directory = local_reader(mocker, tmp_path)
    directory.mkdir(parents=True)
    for name in ("001.json.progress.json", "applied.log"):
        (directory / name).write_bytes(content)
    got = read_store(incus, "c", "pr", uid=None)
    assert set(got.rejected) == {"001.json.progress.json", "applied.log"}
    assert got.files == ()


@pytest.mark.parametrize(
    "raw", ["", "not base64!", base64.b64encode(b"not tar").decode(), "A" * (8 * 1024 * 1024 + 1)]
)
def test_invalid_transport_fails_closed(mocker, raw):
    incus = mocker.Mock()
    incus.exec.return_value = raw
    with pytest.raises(OutboxExecutionError):
        read_store(incus, "c", "pr", uid=None)


def test_warning_metadata(mocker):
    incus = mocker.Mock()
    incus.exec.return_value = encoded_snapshot({}, metadata=b"v1\0present\0W\0notice\0")
    assert read_store(incus, "c", "pr", uid=None).warnings == ("notice",)


def test_store_paths():
    assert store_directory("pr") == "/home/dev/.jailbee/pr-outbox"
    with pytest.raises(OutboxExecutionError):
        store_directory("issue")  # issue proposals live in the host store
