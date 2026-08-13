"""Tests for optional bind mounts."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from jailbee.config import load_config
from jailbee.mounts import (
    DEVICE_NAME_PREFIX,
    add_optional_mount,
    remove_optional_mount,
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
