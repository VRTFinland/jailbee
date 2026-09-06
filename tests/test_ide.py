"""Tests for the JetBrains IDE registry spec."""

from __future__ import annotations

import pytest

from jailbee.ide import SUPPORTED_LAUNCHERS, builtin_specs, resolve_launcher
from jailbee.incus import Incus
from tests.conftest import make_cfg


def test_no_spec_when_jetbrains_is_disabled(tmp_path):
    assert builtin_specs(make_cfg(tmp_path)) == []


def test_spec_is_named_ide_not_the_launcher(tmp_path):
    # The registry key stays `ide` so `jailbee ide` and the dashboard's `i`
    # keybinding keep pointing at one entry whichever IDE is configured.
    cfg = make_cfg(tmp_path, jetbrains={"enabled": True, "ide": "pycharm"})
    spec = builtin_specs(cfg)[0]
    assert spec.name == "ide"
    assert spec.cwd == "repo"
    assert spec.resolve_command is not None


def test_resolve_launcher_searches_the_toolbox_tree(mocker):
    incus = Incus()
    path = "/opt/jetbrains-toolbox/apps/x/bin/idea\n"
    run = mocker.patch.object(Incus, "exec", return_value=path)
    assert resolve_launcher(incus, "c1", "idea") == ["/opt/jetbrains-toolbox/apps/x/bin/idea"]
    assert "/opt/jetbrains-toolbox/apps" in run.call_args.args[1][-1]


def test_resolve_launcher_rejects_an_unsupported_name(mocker):
    mocker.patch.object(Incus, "exec", return_value="")
    with pytest.raises(ValueError, match="notanide"):
        resolve_launcher(Incus(), "c1", "notanide")


def test_resolve_launcher_errors_when_nothing_is_installed(mocker):
    mocker.patch.object(Incus, "exec", return_value="")
    with pytest.raises(ValueError, match="jetbrains-toolbox"):
        resolve_launcher(Incus(), "c1", "idea")


def test_supported_launchers_track_the_ide_literal():
    from typing import get_args

    from jailbee.config import IdeName

    assert SUPPORTED_LAUNCHERS == frozenset(get_args(IdeName))
