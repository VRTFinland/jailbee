"""Tests for the registry cache scan that runs inside the mirror container.

The module's *source* is what jailbee ships into the mirror
(`python3 -c <source> …`), so these tests run exactly that — a real Python
subprocess against nginx-shaped cache files in `tmp_path`. No Incus involved.
"""

import gzip
import hashlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from jailbee import registry_cache_scan

SOURCE = Path(registry_cache_scan.__file__).read_text()

GOOD_LAYER = b"layer-bytes-" * 5000
BLOB_KEY = "/v2/gisgro/typster/blobs/sha256:" + hashlib.sha256(GOOD_LAYER).hexdigest()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _entry(
    root: Path,
    key: str,
    body: bytes,
    *,
    status: str = "200 OK",
    name: str | None = None,
    extra_headers: str = "",
) -> Path:
    """Write a file shaped like an nginx proxy_cache entry; return its path.

    Layout: nginx's binary `ngx_http_file_cache_header_t`, then `\\nKEY: <key>\\n`,
    then the upstream response header verbatim (`HTTP/1.1 …\\r\\n…\\r\\n\\r\\n`),
    then the body. The binary prefix here deliberately contains `\\r\\n\\r\\n`:
    the scan must look for the header terminator *after* the key, and a sound
    entry reading as sound proves the body offset is right.
    """
    md5 = hashlib.md5(key.encode()).hexdigest()
    directory = root / md5[-1] / md5[-3:-1]
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (name or md5)
    header = (
        f"HTTP/1.1 {status}\r\nContent-Length: {len(body)}\r\n{extra_headers}"
        "Server: AmazonS3\r\n\r\n"
    ).encode()
    binary_prefix = b"\x05" + b"\x00" * 40 + b"\r\n\r\n" + b"\x00" * 300
    path.write_bytes(binary_prefix + b"\nKEY: " + key.encode() + b"\n" + header + body)
    return path


def _flip_one_byte(data: bytes) -> bytes:
    middle = len(data) // 2
    return data[:middle] + bytes([data[middle] ^ 0xFF]) + data[middle + 1 :]


def _run(*args: str) -> tuple[list[dict], subprocess.CompletedProcess[str]]:
    proc = subprocess.run(
        [sys.executable, "-c", SOURCE, *args],
        capture_output=True,
        text=True,
        check=False,
    )
    records = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    return records, proc


def _of(records: list[dict], kind: str) -> list[dict]:
    return [r for r in records if r["type"] == kind]


def test_scan_finds_nothing_wrong_with_a_sound_blob(tmp_path):
    _entry(tmp_path, BLOB_KEY, GOOD_LAYER)

    records, proc = _run("scan", "--root", str(tmp_path))

    assert proc.returncode == 0, proc.stderr
    assert _of(records, "corrupt") == []
    summary = _of(records, "summary")[0]
    assert summary["ok"] == 1
    assert summary["checked"] == 1
    assert summary["corrupt"] == 0


def test_scan_reports_a_blob_whose_body_does_not_match_its_key(tmp_path):
    """The bug report's exact shape: same length, one damaged byte."""
    bad = _flip_one_byte(GOOD_LAYER)
    path = _entry(tmp_path, BLOB_KEY, bad)

    records, proc = _run("scan", "--root", str(tmp_path))

    assert proc.returncode == 0, proc.stderr
    [corrupt] = _of(records, "corrupt")
    assert corrupt["key"] == BLOB_KEY
    assert corrupt["expected"] == _sha(GOOD_LAYER)
    assert corrupt["actual"] == _sha(bad)
    assert corrupt["path"] == str(path.relative_to(tmp_path))
    assert corrupt["size"] == path.stat().st_size
    assert corrupt["purged"] is False
    assert path.exists(), "a plain scan must not remove anything"
    summary = _of(records, "summary")[0]
    assert summary["corrupt"] == 1
    assert summary["bytes_checked"] == path.stat().st_size


def test_scan_verifies_manifests_fetched_by_digest(tmp_path):
    manifest = b'{"schemaVersion": 2}'
    key = "/v2/library/nginx/manifests/sha256:" + _sha(manifest)
    _entry(tmp_path, key, b'{"schemaVersion": 3}')

    records, _ = _run("scan", "--root", str(tmp_path))

    [corrupt] = _of(records, "corrupt")
    assert corrupt["key"] == key


def test_scan_skips_an_entry_whose_key_names_no_digest(tmp_path):
    """A manifest fetched by tag carries no digest to check against."""
    _entry(tmp_path, "/v2/library/nginx/manifests/1.19.8", b"anything at all")

    records, _ = _run("scan", "--root", str(tmp_path))

    summary = _of(records, "summary")[0]
    assert summary["skipped_no_digest"] == 1
    assert summary["checked"] == 0
    assert _of(records, "corrupt") == []


def test_scan_skips_a_response_that_is_not_http_200(tmp_path):
    """rpardini caches 206s too; a partial body never hashes to the full digest."""
    _entry(tmp_path, BLOB_KEY, GOOD_LAYER[:100], status="206 Partial Content")

    records, _ = _run("scan", "--root", str(tmp_path))

    summary = _of(records, "summary")[0]
    assert summary["skipped_status"] == 1
    assert _of(records, "corrupt") == []


def test_scan_skips_a_file_nginx_is_still_writing(tmp_path):
    """`use_temp_path=off`: in-flight temp files share the directory, named
    `<md5>.<digits>`, and are incomplete by definition."""
    md5 = hashlib.md5(BLOB_KEY.encode()).hexdigest()
    _entry(tmp_path, BLOB_KEY, GOOD_LAYER[:10], name=f"{md5}.0000000001")

    records, _ = _run("scan", "--root", str(tmp_path))

    summary = _of(records, "summary")[0]
    assert summary["skipped_temp"] == 1
    assert _of(records, "corrupt") == []


def test_scan_counts_an_unparseable_file_and_keeps_going(tmp_path):
    _entry(tmp_path, BLOB_KEY, GOOD_LAYER)
    junk = tmp_path / "0" / "00" / ("0" * 32)
    junk.parent.mkdir(parents=True)
    junk.write_bytes(b"not a cache entry")

    records, proc = _run("scan", "--root", str(tmp_path))

    assert proc.returncode == 0, proc.stderr
    summary = _of(records, "summary")[0]
    assert summary["errors"] == 1
    assert summary["error_samples"][0].startswith("0/00/" + "0" * 32)
    assert summary["ok"] == 1


def test_scan_reports_totals_first_and_a_final_progress_line(tmp_path):
    first = _entry(tmp_path, BLOB_KEY, GOOD_LAYER)
    manifest = b'{"schemaVersion": 2}'
    second = _entry(tmp_path, "/v2/a/manifests/sha256:" + _sha(manifest), manifest)
    total_bytes = first.stat().st_size + second.stat().st_size

    records, _ = _run("scan", "--root", str(tmp_path))

    assert records[0] == {"type": "total", "entries": 2, "bytes": total_bytes}
    assert records[-1]["type"] == "summary"
    assert _of(records, "progress")[-1] == {
        "type": "progress",
        "entries_done": 2,
        "bytes_done": total_bytes,
    }
    assert records[-1]["bytes_checked"] == total_bytes


def test_scan_fails_when_the_cache_root_is_missing(tmp_path):
    records, proc = _run("scan", "--root", str(tmp_path / "nope"))

    assert proc.returncode == 2
    assert "nope" in proc.stderr
    assert records == []


def test_scan_purge_removes_only_the_corrupt_entry(tmp_path):
    corrupt = _entry(tmp_path, BLOB_KEY, _flip_one_byte(GOOD_LAYER))
    manifest = b'{"schemaVersion": 2}'
    sound = _entry(tmp_path, "/v2/a/manifests/sha256:" + _sha(manifest), manifest)

    records, proc = _run("scan", "--purge", "--root", str(tmp_path))

    assert proc.returncode == 0, proc.stderr
    assert not corrupt.exists()
    assert sound.exists()
    [record] = _of(records, "corrupt")
    assert record["purged"] is True
    assert _of(records, "summary")[0]["purged"] == 1


def test_an_entry_replaced_after_hashing_is_not_removed(tmp_path):
    """nginx swaps a fresh copy in by rename; deleting that copy because the
    *previous* file was corrupt would throw away a sound entry."""
    path = _entry(tmp_path, BLOB_KEY, _flip_one_byte(GOOD_LAYER))
    verdict = registry_cache_scan._inspect(str(path))
    assert verdict.outcome == "corrupt"
    fresh = path.with_name(path.name + ".0000000007")
    fresh.write_bytes(path.read_bytes())
    fresh.replace(path)  # new inode at the same name, as nginx's rename does
    cand = registry_cache_scan._Candidate(
        rel=str(path.relative_to(tmp_path)), path=str(path), size=path.stat().st_size
    )
    tally = registry_cache_scan._Tally()

    registry_cache_scan._record(tally, cand, verdict, remove=True)

    assert path.exists()
    assert tally.purged == 0


def test_an_entry_rewritten_in_place_after_hashing_is_not_removed(tmp_path):
    """The mtime half of the check: same inode, new content (ext4 can hand an
    evicted entry's inode to the next file written)."""
    path = _entry(tmp_path, BLOB_KEY, _flip_one_byte(GOOD_LAYER))
    verdict = registry_cache_scan._inspect(str(path))
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))

    assert registry_cache_scan._unlink_if_unchanged(str(path), verdict) is False
    assert path.exists()


def test_purge_removes_a_path_that_is_still_corrupt(tmp_path):
    path = _entry(tmp_path, BLOB_KEY, _flip_one_byte(GOOD_LAYER))
    rel = str(path.relative_to(tmp_path))

    records, proc = _run("purge", "--root", str(tmp_path), rel)

    assert proc.returncode == 0, proc.stderr
    assert not path.exists()
    [record] = _of(records, "corrupt")
    assert record["path"] == rel
    assert record["purged"] is True


def test_purge_keeps_a_path_that_is_sound_again(tmp_path):
    """Between the scan and the user's "yes", a pull may have replaced the
    entry with a good copy — that one stays."""
    path = _entry(tmp_path, BLOB_KEY, _flip_one_byte(GOOD_LAYER))
    rel = str(path.relative_to(tmp_path))
    _entry(tmp_path, BLOB_KEY, GOOD_LAYER)  # same key → same file, now sound

    records, _ = _run("purge", "--root", str(tmp_path), rel)

    assert path.exists()
    summary = _of(records, "summary")[0]
    assert summary["ok"] == 1
    assert summary["purged"] == 0
    assert _of(records, "corrupt") == []


def test_purge_refuses_a_path_outside_the_cache(tmp_path):
    """The outside file is a corrupt entry the purge *would* delete if the
    path check were missing — otherwise the test proves nothing."""
    root = tmp_path / "cache"
    root.mkdir()
    outside = _entry(tmp_path / "elsewhere", BLOB_KEY, _flip_one_byte(GOOD_LAYER))
    relative = os.path.relpath(outside, root)

    records, proc = _run("purge", "--root", str(root), relative, str(outside))

    assert proc.returncode == 0, proc.stderr
    assert outside.exists()
    assert _of(records, "summary")[0]["errors"] == 2
    assert _of(records, "corrupt") == []


def test_scan_skips_a_content_encoded_response(tmp_path):
    """A gzip-encoded body never hashes to the digest of the content: counting
    it corrupt would purge it, refetch it encoded, and flag it again forever."""
    _entry(
        tmp_path,
        BLOB_KEY,
        gzip.compress(GOOD_LAYER),
        extra_headers="Content-Encoding: gzip\r\n",
    )

    records, _ = _run("scan", "--root", str(tmp_path))

    assert _of(records, "summary")[0]["skipped_status"] == 1
    assert _of(records, "corrupt") == []


def test_an_entry_evicted_during_the_scan_is_not_an_error(tmp_path):
    """nginx's cache manager evicts while we walk; a vanished file is simply no
    longer part of the cache."""
    cand = registry_cache_scan._Candidate(
        rel="0/00/" + "0" * 32, path=str(tmp_path / "gone"), size=10
    )
    tally = registry_cache_scan._Tally()

    registry_cache_scan._process(cand, tally, remove=False)

    assert tally.errors == 0


_NEEDS_NON_ROOT = pytest.mark.skipif(
    os.geteuid() == 0, reason="root reads through mode 000 directories"
)


@_NEEDS_NON_ROOT
def test_an_unreadable_directory_is_an_error_not_a_clean_pass(tmp_path):
    """If the mirror's root cannot read nginx's directories, saying "none
    corrupt" would be a lie."""
    _entry(tmp_path, BLOB_KEY, GOOD_LAYER)
    locked = tmp_path / "z" / "zz"
    locked.mkdir(parents=True)
    locked.chmod(0)
    try:
        records, proc = _run("scan", "--root", str(tmp_path))
    finally:
        locked.chmod(0o755)

    assert proc.returncode == 0, proc.stderr
    summary = _of(records, "summary")[0]
    assert summary["errors"] == 1
    assert summary["error_samples"][0].startswith("z/zz")


@_NEEDS_NON_ROOT
def test_an_unreadable_cache_root_fails_the_scan(tmp_path):
    root = tmp_path / "cache"
    root.mkdir()
    root.chmod(0)
    try:
        records, proc = _run("scan", "--root", str(root))
    finally:
        root.chmod(0o755)

    assert proc.returncode == 2
    assert "cache" in proc.stderr
    assert records == []


def test_every_record_is_flushed_as_it_is_written(monkeypatch):
    """Under `incus exec` stdout is a pipe, block-buffered: without a flush per
    record, progress would reach the host in 8 KiB bursts or only at the end."""

    class Recorder(io.StringIO):
        unflushed = 0

        def write(self, s: str) -> int:
            self.unflushed += 1
            return super().write(s)

        def flush(self) -> None:
            self.unflushed = 0

    out = Recorder()
    monkeypatch.setattr(sys, "stdout", out)

    registry_cache_scan._emit({"type": "total", "entries": 0, "bytes": 0})

    assert out.getvalue()
    assert out.unflushed == 0
