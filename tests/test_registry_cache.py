"""Tests for registry_cache: turning the in-mirror scan's output into a report."""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import jailbee
from jailbee.registry import MIRROR_CONTAINER_NAME
from jailbee.registry_cache import (
    CacheProgress,
    format_progress,
    purge_entries,
    verify_cache,
)

KEY = "/v2/gisgro/typster/blobs/sha256:" + "0251fc1b" + "0" * 56
# Every count distinct, so a field mapped from the wrong key cannot pass.
SUMMARY = {
    "type": "summary",
    "checked": 11,
    "ok": 10,
    "corrupt": 1,
    "purged": 4,
    "skipped_no_digest": 3,
    "skipped_status": 5,
    "skipped_temp": 2,
    "errors": 6,
    "error_samples": ["0/00/x: no KEY line"],
    "bytes_checked": 300,
}
CORRUPT = {
    "type": "corrupt",
    "path": "2/f7/dbc824cfc1bb283fdf0d6a5544b49f72",
    "key": KEY,
    "expected": "0251fc1b" + "0" * 56,
    "actual": "67f8de43" + "0" * 56,
    "size": 19950938,
    "purged": False,
}


def _incus(*records: dict | str) -> MagicMock:
    incus = MagicMock()
    # A generator, like the real exec_lines — `verify_cache` closes it.
    incus.exec_lines.return_value = (
        r if isinstance(r, str) else json.dumps(r) for r in records
    )
    return incus


def test_verify_cache_runs_the_scan_module_inside_the_mirror():
    incus = _incus(SUMMARY)

    verify_cache(incus)

    name, cmd = incus.exec_lines.call_args.args
    assert name == MIRROR_CONTAINER_NAME
    assert cmd[:2] == ["python3", "-c"]
    source = (Path(jailbee.__file__).parent / "registry_cache_scan.py").read_text()
    assert cmd[2] == source
    assert cmd[3:] == ["scan"]


def test_verify_cache_asks_the_scan_to_purge():
    incus = _incus(SUMMARY)

    verify_cache(incus, purge=True)

    assert incus.exec_lines.call_args.args[1][3:] == ["scan", "--purge"]


def test_verify_cache_builds_the_report_from_the_scan_output():
    incus = _incus({"type": "total", "entries": 7, "bytes": 999}, CORRUPT, SUMMARY)

    report = verify_cache(incus)

    [entry] = report.corrupt
    assert entry.path == "2/f7/dbc824cfc1bb283fdf0d6a5544b49f72"
    assert entry.repo == "gisgro/typster"
    assert entry.kind == "blob"
    assert entry.expected.startswith("0251fc1b")
    assert entry.actual.startswith("67f8de43")
    assert entry.size == 19950938
    assert entry.purged is False
    assert report.checked == 11
    assert report.ok == 10
    assert report.purged == 4
    assert report.skipped_no_digest == 3
    assert report.skipped_status == 5
    assert report.skipped_temp == 2
    assert report.errors == 6
    assert report.error_samples == ("0/00/x: no KEY line",)
    assert report.bytes_checked == 300


def test_a_removed_entry_is_reported_as_purged():
    report = verify_cache(_incus({**CORRUPT, "purged": True}, SUMMARY), purge=True)

    assert report.corrupt[0].purged is True


def test_a_manifest_entry_reports_its_kind():
    manifest = {**CORRUPT, "key": "/v2/library/nginx/manifests/sha256:" + "a" * 64}
    report = verify_cache(_incus(manifest, SUMMARY))

    assert report.corrupt[0].kind == "manifest"
    assert report.corrupt[0].repo == "library/nginx"


def test_verify_cache_reports_progress_against_the_totals():
    seen: list[CacheProgress] = []
    incus = _incus(
        {"type": "total", "entries": 1352, "bytes": 18_000},
        {"type": "progress", "entries_done": 812, "bytes_done": 11_000},
        SUMMARY,
    )

    verify_cache(incus, on_progress=seen.append)

    assert seen == [CacheProgress(812, 1352, 11_000, 18_000)]


def test_purge_entries_hands_the_paths_to_the_scan():
    incus = _incus(SUMMARY)

    purge_entries(incus, ["2/f7/aaa", "0/00/bbb"])

    assert incus.exec_lines.call_args.args[1][3:] == ["purge", "2/f7/aaa", "0/00/bbb"]


def test_unparseable_scan_output_is_reported_as_a_format_mismatch():
    with pytest.raises(RuntimeError, match="format"):
        verify_cache(_incus("Traceback (most recent call last):", SUMMARY))


def test_an_unknown_record_type_is_reported_as_a_format_mismatch():
    with pytest.raises(RuntimeError, match="format"):
        verify_cache(_incus({"type": "banner", "text": "hello"}, SUMMARY))


def test_the_scan_is_stopped_when_its_output_cannot_be_used():
    """A mismatch raised mid-stream must close the exec, or the remote scan —
    possibly a purging one — runs on until the traceback is collected."""
    closed = []

    def lines():
        try:
            yield "not json"
            yield json.dumps(SUMMARY)
        finally:
            closed.append(True)

    incus = MagicMock()
    incus.exec_lines.return_value = lines()

    with pytest.raises(RuntimeError):
        verify_cache(incus)

    assert closed == [True]


def test_a_scan_that_ends_without_a_summary_is_an_error():
    with pytest.raises(RuntimeError, match="summary"):
        verify_cache(_incus({"type": "total", "entries": 1, "bytes": 1}))


def test_format_progress_reads_as_counts_and_sizes():
    progress = CacheProgress(812, 1352, 12_025_908_428, 19_327_352_832)

    assert format_progress(progress) == "812/1352 entries · 11.2 GB of 18.0 GB"
