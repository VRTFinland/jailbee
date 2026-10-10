"""Tests for optional bind mounts."""

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from jailbee.config import load_config
from jailbee.mounts import (
    DEVICE_NAME_PREFIX,
    MOUNT_UNTIL_PREFIX,
    add_optional_mount,
    attach,
    is_attached,
    mount_until,
    remove_optional_mount,
    until_key,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_device_name_prefix():
    assert DEVICE_NAME_PREFIX == "optional-"


def test_add_optional_mount_calls_device_add():
    cfg = load_config(FIXTURES / "full_config.yaml")
    incus = MagicMock()
    add_optional_mount(cfg, incus, "feat-foo", "aws")
    args = incus.config_device_add.call_args
    assert args.args[0] == "feat-foo"
    assert args.args[1] == "optional-aws"
    assert args.args[2] == "disk"
    props = args.args[3]
    assert "source" in props
    assert props["path"] == "/home/dev/.aws"
    assert props["readonly"] == "true"


def test_add_optional_mount_unknown_kind_raises():
    cfg = load_config(FIXTURES / "full_config.yaml")
    incus = MagicMock()
    with pytest.raises(ValueError, match="Unknown optional mount"):
        add_optional_mount(cfg, incus, "feat-foo", "nope")


def test_remove_optional_mount_calls_device_remove():
    cfg = load_config(FIXTURES / "full_config.yaml")
    incus = MagicMock()
    remove_optional_mount(cfg, incus, "feat-foo", "aws")
    incus.config_device_remove.assert_called_once_with("feat-foo", "optional-aws")


def test_attached_kinds_strips_the_prefix_and_sorts():
    from jailbee.mounts import attached_kinds

    devices = {f"{DEVICE_NAME_PREFIX}gcp": {}, "eth0": {}, f"{DEVICE_NAME_PREFIX}aws": {}}
    assert attached_kinds(devices) == ("aws", "gcp")


def test_until_key():
    assert until_key("aws") == "user.jailbee.mount_until.aws"
    assert MOUNT_UNTIL_PREFIX == "user.jailbee.mount_until."


def test_mount_until_parses_valid_labels_and_skips_the_rest():
    t = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
    config = {
        "user.jailbee.mount_until.aws": t.isoformat(),
        "user.jailbee.mount_until.bad": "yesterday",
        "user.jailbee.mount_until.empty": "",
        "user.jailbee.mount_until.naive": "2026-10-10T12:00:00",
        "user.jailbee.loose_until": t.isoformat(),
    }
    assert mount_until(config) == {"aws": t}


def test_is_attached_reads_the_device():
    incus = MagicMock()
    incus.config_device_get.return_value = "/home/u/.aws"
    assert is_attached(incus, "c", "aws") is True
    incus.config_device_get.assert_called_once_with("c", "optional-aws", "source")
    incus.config_device_get.return_value = None
    assert is_attached(incus, "c", "aws") is False


def test_attach_new_mount_adds_device_and_sets_label():
    cfg = load_config(FIXTURES / "full_config.yaml")
    incus = MagicMock()
    incus.config_device_get.return_value = None
    until = datetime(2026, 10, 10, 12, 15, tzinfo=UTC)
    assert attach(cfg, incus, "c", "aws", until) is True
    incus.config_device_add.assert_called_once()
    assert [c[0] for c in incus.mock_calls] == ["config_device_get", "config_device_add", "config_set"]
    incus.config_set.assert_called_once_with("c", "user.jailbee.mount_until.aws", until.isoformat())


def test_attach_already_attached_only_retimes():
    cfg = load_config(FIXTURES / "full_config.yaml")
    incus = MagicMock()
    incus.config_device_get.return_value = "/home/u/.aws"
    until = datetime(2026, 10, 10, 14, 0, tzinfo=UTC)
    assert attach(cfg, incus, "c", "aws", until) is False
    incus.config_device_add.assert_not_called()
    incus.config_set.assert_called_once_with("c", "user.jailbee.mount_until.aws", until.isoformat())


def test_attach_without_ttl_clears_label():
    cfg = load_config(FIXTURES / "full_config.yaml")
    incus = MagicMock()
    incus.config_device_get.return_value = "/home/u/.aws"
    attach(cfg, incus, "c", "aws", None)
    incus.config_unset_checked.assert_called_once_with("c", "user.jailbee.mount_until.aws")
    incus.config_set.assert_not_called()


def test_attach_unknown_kind_raises_before_touching_incus():
    cfg = load_config(FIXTURES / "full_config.yaml")
    incus = MagicMock()
    with pytest.raises(ValueError, match="Unknown optional mount"):
        attach(cfg, incus, "c", "nope", None)
    assert incus.mock_calls == []


def test_remove_optional_mount_clears_the_label_after_the_device():
    cfg = load_config(FIXTURES / "full_config.yaml")
    incus = MagicMock()
    remove_optional_mount(cfg, incus, "c", "aws")
    names = [call[0] for call in incus.mock_calls]
    assert names == ["config_device_remove", "config_unset_checked"]
    incus.config_unset_checked.assert_called_once_with("c", "user.jailbee.mount_until.aws")


def test_add_failure_does_not_write_or_clear_deadline():
    cfg = load_config(FIXTURES / "full_config.yaml")
    incus = MagicMock()
    incus.config_device_get.return_value = None
    incus.config_device_add.side_effect = RuntimeError("add failed")
    with pytest.raises(RuntimeError, match="add failed"):
        attach(cfg, incus, "c", "aws", None)
    assert [c[0] for c in incus.mock_calls] == ["config_device_get", "config_device_add"]
