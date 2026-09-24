"""Records of which JailBee version each running SSH server serves."""

from __future__ import annotations

import os

from jailbee.remote_ssh import running


def test_record_and_read_back_a_live_server() -> None:
    running.record_running("1.2.3")

    assert running.running_servers() == {os.getpid(): running.RunningServer(os.getpid(), "1.2.3")}


def test_clear_removes_only_this_process(monkeypatch) -> None:
    running.record_running("1.2.3")
    running.clear_running()

    assert running.running_servers() == {}


def test_a_dead_server_record_is_ignored_and_pruned(monkeypatch) -> None:
    path = running.record_running("1.0.0", pid=424242)
    monkeypatch.setattr(running, "_alive", lambda pid: pid != 424242)

    assert running.running_servers() == {}
    assert not path.exists()


def test_a_malformed_record_is_skipped() -> None:
    running.record_running("1.2.3")
    (running._records_dir() / "junk.json").write_text("{not json")

    assert list(running.running_servers()) == [os.getpid()]


def test_records_are_private() -> None:
    path = running.record_running("1.2.3")

    assert path.stat().st_mode & 0o777 == 0o600


def test_installed_version_reads_the_metadata_afresh(monkeypatch) -> None:
    monkeypatch.setattr(running, "version", lambda name: "9.9.9")

    assert running.installed_version() == "9.9.9"


def test_installed_version_is_none_without_metadata(monkeypatch) -> None:
    """Mid-upgrade the dist-info can be briefly gone: that is "unknown", not a
    version the server could mistake for a change."""
    from importlib.metadata import PackageNotFoundError

    def missing(name):
        raise PackageNotFoundError(name)

    monkeypatch.setattr(running, "version", missing)

    assert running.installed_version() is None
