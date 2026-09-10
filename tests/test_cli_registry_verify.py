"""`jailbee registry verify`: flow, prompts and exit codes (verify_cache mocked)."""

import pytest
from typer.testing import CliRunner

from jailbee.cli import app
from jailbee.incus import IncusError
from jailbee.registry import MirrorStatus
from jailbee.registry_cache import CacheProgress, CacheReport, CorruptEntry

PATH = "2/f7/dbc824cfc1bb283fdf0d6a5544b49f72"


def _corrupt(*, purged: bool = False) -> CorruptEntry:
    return CorruptEntry(
        path=PATH,
        key="/v2/gisgro/typster/blobs/sha256:0251fc1b" + "0" * 56,
        expected="0251fc1b" + "0" * 56,
        actual="67f8de43" + "0" * 56,
        size=19_950_938,
        purged=purged,
    )


def _report(*corrupt: CorruptEntry, ok: int = 1351) -> CacheReport:
    return CacheReport(
        corrupt=corrupt,
        checked=ok + len(corrupt),
        ok=ok,
        purged=sum(c.purged for c in corrupt),
        skipped_no_digest=259,
        skipped_status=0,
        skipped_temp=0,
        errors=0,
        error_samples=(),
        bytes_checked=19_327_352_832,
    )


@pytest.fixture
def mirror(mocker):
    """A running mirror; tests set what the scan finds."""
    mocker.patch("jailbee.cli._load_or_exit")
    mocker.patch("jailbee.incus.Incus")
    mocker.patch("jailbee.registry.registry_status", return_value=MirrorStatus.RUNNING)
    return mocker


def test_a_sound_cache_exits_zero(mirror):
    mirror.patch("jailbee.registry_cache.verify_cache", return_value=_report())

    result = CliRunner().invoke(app, ["registry", "verify"])

    assert result.exit_code == 0, result.output
    assert "1351 cache entries verified" in result.output
    assert "none corrupt" in result.output


def test_corrupt_entries_without_a_terminal_are_listed_and_left(mirror):
    mirror.patch("jailbee.registry_cache.verify_cache", return_value=_report(_corrupt()))
    mirror.patch("jailbee.cli._is_tty", return_value=False)
    purge = mirror.patch("jailbee.registry_cache.purge_entries")

    result = CliRunner().invoke(app, ["registry", "verify"])

    assert result.exit_code == 1
    assert "gisgro/typster" in result.output
    assert "sha256:0251fc1b" in result.output
    assert "jailbee registry verify --purge" in result.output
    purge.assert_not_called()


def test_purge_flag_removes_during_the_scan(mirror):
    verify = mirror.patch(
        "jailbee.registry_cache.verify_cache", return_value=_report(_corrupt(purged=True))
    )

    result = CliRunner().invoke(app, ["registry", "verify", "--purge"])

    assert result.exit_code == 0, result.output
    assert verify.call_args.kwargs["purge"] is True
    assert "Removed 1 entry" in result.output


def test_purge_flag_fails_when_an_entry_stays(mirror):
    mirror.patch("jailbee.registry_cache.verify_cache", return_value=_report(_corrupt()))

    result = CliRunner().invoke(app, ["registry", "verify", "--purge"])

    assert result.exit_code == 1
    assert PATH in result.output


def test_confirming_on_a_terminal_removes_what_is_still_corrupt(mirror):
    mirror.patch("jailbee.registry_cache.verify_cache", return_value=_report(_corrupt()))
    mirror.patch("jailbee.cli._is_tty", return_value=True)
    confirm = mirror.patch("jailbee.cli.default_confirm", return_value=True)
    purge = mirror.patch(
        "jailbee.registry_cache.purge_entries", return_value=_report(_corrupt(purged=True), ok=0)
    )

    result = CliRunner().invoke(app, ["registry", "verify"])

    assert result.exit_code == 0, result.output
    assert "Remove 1 corrupt entry?" in confirm.call_args.args[0]
    assert purge.call_args.args[1] == [PATH]
    assert "Removed 1 entry" in result.output


def test_declining_on_a_terminal_removes_nothing(mirror):
    mirror.patch("jailbee.registry_cache.verify_cache", return_value=_report(_corrupt()))
    mirror.patch("jailbee.cli._is_tty", return_value=True)
    mirror.patch("jailbee.cli.default_confirm", return_value=False)
    purge = mirror.patch("jailbee.registry_cache.purge_entries")

    result = CliRunner().invoke(app, ["registry", "verify"])

    assert result.exit_code == 1
    purge.assert_not_called()


def test_a_stopped_mirror_is_reported_without_scanning(mirror):
    mirror.patch("jailbee.registry.registry_status", return_value=MirrorStatus.STOPPED)
    verify = mirror.patch("jailbee.registry_cache.verify_cache")

    result = CliRunner().invoke(app, ["registry", "verify"])

    assert result.exit_code == 1
    assert "stopped" in result.output
    assert "jailbee registry up" in result.output
    verify.assert_not_called()


def test_a_failing_scan_is_reported_without_a_traceback(mirror):
    mirror.patch(
        "jailbee.registry_cache.verify_cache",
        side_effect=IncusError("`incus exec …` failed (exit 127): Command not found"),
    )

    result = CliRunner().invoke(app, ["registry", "verify"])

    assert result.exit_code == 1
    assert "Command not found" in result.output
    assert "Traceback" not in result.output


def test_scan_progress_reaches_the_status_line(mirror):
    def fake_verify(incus, *, purge, on_progress):
        on_progress(CacheProgress(812, 1352, 12_025_908_428, 19_327_352_832))
        return _report()

    mirror.patch("jailbee.registry_cache.verify_cache", side_effect=fake_verify)
    status = mirror.MagicMock()
    status.__enter__.return_value = status
    mirror.patch("jailbee.tui.console.status", return_value=status)

    result = CliRunner().invoke(app, ["registry", "verify"])

    assert result.exit_code == 0, result.output
    updates = [call.args[0] for call in status.update.call_args_list]
    assert any("812/1352 entries" in u for u in updates)
