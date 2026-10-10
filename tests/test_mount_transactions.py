"""Live-state regression tests for mount transactions and stale timer discovery."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from threading import Event, Thread

import pytest
import yaml

from jailbee.mount_revert import check_and_revert_mounts
from jailbee.mounts import attach, remove_optional_mount, until_key
from tests.conftest import make_cfg

NOW = datetime(2026, 10, 10, 12, tzinfo=UTC)


class LiveIncus:
    def __init__(self, cfg, *, present=True):
        self.row = {
            "name": "app-x",
            "profiles": [f"{cfg.container_prefix}-base"],
            "config": {until_key("aws"): (NOW - timedelta(minutes=1)).isoformat()},
            "devices": {"optional-aws": {"source": "/aws"}} if present else {},
        }
        self.write_error = False
        self.remove_error = False
        self.after_listing = lambda: None
        self.during_remove = lambda: None

    def list_containers(self):
        snapshot = deepcopy(self.row)
        self.after_listing()
        return [snapshot]

    def config_show(self, name):
        return yaml.safe_dump(self.row)

    def config_device_get(self, name, device, key):
        return self.row["devices"].get(device, {}).get(key)

    def config_device_add(self, name, device, kind, props):
        self.row["devices"][device] = props

    def config_device_remove(self, name, device):
        if self.remove_error:
            raise RuntimeError("rollback busy")
        del self.row["devices"][device]
        self.during_remove()

    def config_set(self, name, key, value):
        if self.write_error:
            raise RuntimeError("write failed")
        self.row["config"][key] = value

    def config_unset(self, name, key):
        self.config_unset_checked(name, key)

    def config_unset_checked(self, name, key):
        if self.write_error:
            raise RuntimeError("clear failed")
        self.row["config"].pop(key, None)


@pytest.fixture
def live(tmp_path, mocker):
    cfg = make_cfg(tmp_path, optional_mounts={"aws": {"host": "/aws", "container": "/aws"}})
    mocker.patch("jailbee.mount_revert._autostart_holds", return_value=False)
    return cfg, LiveIncus(cfg)


@pytest.mark.parametrize("present", [True, False])
@pytest.mark.parametrize("deadline", [None, NOW + timedelta(hours=4)])
def test_listing_is_discovery_not_authority(live, present, deadline):
    cfg, incus = live
    if not present:
        incus.row["devices"].clear()
    incus.after_listing = lambda: attach(cfg, incus, "app-x", "aws", deadline)
    assert check_and_revert_mounts(cfg, incus, now=NOW) == []
    assert "optional-aws" in incus.row["devices"]
    assert incus.row["config"].get(until_key("aws")) == (deadline.isoformat() if deadline else None)


@pytest.mark.parametrize("present", [True, False])
@pytest.mark.parametrize("deadline", [None, NOW + timedelta(hours=4)])
def test_failed_deadline_preserves_existing_or_rolls_back_new(live, present, deadline):
    cfg, incus = live
    if not present:
        incus.row["devices"].clear()
    before = deepcopy(incus.row)
    incus.write_error = True
    with pytest.raises(RuntimeError, match="failed"):
        attach(cfg, incus, "app-x", "aws", deadline)
    assert incus.row == before


def test_failed_rollback_reports_both_failures(live):
    cfg, incus = live
    incus.row["devices"].clear()
    incus.write_error = incus.remove_error = True
    with pytest.raises(Exception, match=r"write failed.*rollback.*busy"):
        attach(cfg, incus, "app-x", "aws", NOW)
    assert "optional-aws" in incus.row["devices"]


@pytest.mark.parametrize("timer", [True, False])
def test_remove_and_label_cleanup_serialize_concurrent_attach(live, timer):
    cfg, incus = live
    started, finished = Event(), Event()
    errors = []
    deadline = NOW + timedelta(hours=4)

    def writer():
        started.set()
        try:
            attach(cfg, incus, "app-x", "aws", deadline)
        except Exception as exc:
            errors.append(exc)
        finally:
            finished.set()

    thread = Thread(target=writer)

    def interleave():
        thread.start()
        assert started.wait(2)
        assert not finished.wait(0.1), "writer bypassed mount transaction lock"

    incus.during_remove = interleave
    try:
        if timer:
            result = check_and_revert_mounts(cfg, incus, now=NOW)
            assert not any(r.error for r in result), result
        else:
            remove_optional_mount(cfg, incus, "app-x", "aws")
    finally:
        thread.join(3)
    assert finished.is_set()
    assert not errors
    assert "optional-aws" in incus.row["devices"]
    assert incus.row["config"][until_key("aws")] == deadline.isoformat()


def test_timer_clear_failure_is_reported_and_retried(live):
    cfg, incus = live
    incus.write_error = True
    result = check_and_revert_mounts(cfg, incus, now=NOW)
    assert any(r.error and "clear failed" in r.error for r in result)
    assert until_key("aws") in incus.row["config"]
    incus.write_error = False
    check_and_revert_mounts(cfg, incus, now=NOW)
    assert until_key("aws") not in incus.row["config"]


def test_successful_removal_is_observable_without_info_logging(live, capsys):
    cfg, incus = live
    check_and_revert_mounts(cfg, incus, now=NOW)
    assert "detached aws (TTL expired)" in capsys.readouterr().err


def test_malformed_discovery_does_not_clear_replacement(live):
    cfg, incus = live
    incus.row["config"][until_key("aws")] = "bad"
    deadline = NOW + timedelta(hours=4)
    incus.after_listing = lambda: attach(cfg, incus, "app-x", "aws", deadline)
    assert check_and_revert_mounts(cfg, incus, now=NOW) == []
    assert "optional-aws" in incus.row["devices"]
    assert incus.row["config"][until_key("aws")] == deadline.isoformat()


def test_terminal_help_explains_mount_deadline():
    from jailbee.dashboard.tui.frame import help_lines

    text = "\n".join(help_lines())
    assert "MOUNT: ◆ attached" in text
    assert "latest deadline" in text
    assert "∞ no auto-unmount" in text
