"""Tests for the JetBrains IDE registry spec."""

from __future__ import annotations

import pytest

from jailbee.ide import SUPPORTED_LAUNCHERS, builtin_specs, resolve_launcher
from jailbee.incus import Incus
from tests.conftest import make_cfg


def test_no_spec_when_jetbrains_is_disabled(tmp_path):
    assert builtin_specs(make_cfg(tmp_path)) == []


def test_spec_is_named_ide_not_the_launcher(tmp_path, mocker):
    # The registry key stays `ide` so `jailbee ide` and the dashboard's `i`
    # keybinding keep pointing at one entry whichever IDE is configured.
    cfg = make_cfg(tmp_path, jetbrains={"enabled": True, "ide": "pycharm"})
    spec = builtin_specs(cfg)[0]
    assert spec.name == "ide"
    assert spec.cwd == "repo"
    assert spec.resolve_command is not None

    # Verify resolve_command actually works and returns the launcher for the
    # configured IDE (pycharm in this case, not idea).
    incus = Incus()
    path = "/opt/jetbrains-toolbox/apps/x/bin/pycharm\n"
    mocker.patch.object(Incus, "exec", return_value=path)
    result = spec.resolve_command(incus, "c1")
    assert result == ["/opt/jetbrains-toolbox/apps/x/bin/pycharm"]


def test_builtin_spec_resolve_command_forwards_container_user_uid_gid(tmp_path, mocker):
    """Regression guard for the common path: `jailbee ide`, GUI autostart,
    `apps run ide`, and the dashboard all launch through `builtin_specs`'
    `resolve_command` lambda — not the rarer one-off spec cli.py builds for
    `--app <ide other than cfg.jetbrains.ide>` (covered separately in
    tests/test_cli_apps.py). This wiring has already been dropped twice;
    distinct uid/gid values so a drop (uid=0/gid=0) or a swap
    (uid<->gid) both fail, not just a missing kwarg.
    """
    cfg = make_cfg(
        tmp_path,
        jetbrains={"enabled": True, "ide": "idea"},
        container_user={"uid": 4242, "gid": 4343},
    )
    spec = builtin_specs(cfg)[0]
    assert spec.resolve_command is not None

    incus = Incus()
    exec_mock = mocker.patch.object(
        Incus, "exec", return_value="/opt/jetbrains-toolbox/apps/x/bin/idea\n"
    )
    spec.resolve_command(incus, "c1")

    assert exec_mock.call_args.kwargs["uid"] == 4242
    assert exec_mock.call_args.kwargs["gid"] == 4343


def test_resolve_launcher_searches_the_toolbox_tree(mocker):
    incus = Incus()
    path = "/opt/jetbrains-toolbox/apps/x/bin/idea\n"
    run = mocker.patch.object(Incus, "exec", return_value=path)
    assert resolve_launcher(incus, "c1", "idea", uid=1000, gid=1000) == [
        "/opt/jetbrains-toolbox/apps/x/bin/idea"
    ]
    find_cmd = run.call_args.args[1][-1]
    assert "/opt/jetbrains-toolbox/apps" in find_cmd
    assert "-name 'idea'" in find_cmd


def test_resolve_launcher_rejects_an_unsupported_name(mocker):
    mocker.patch.object(Incus, "exec", return_value="")
    with pytest.raises(ValueError, match="notanide"):
        resolve_launcher(Incus(), "c1", "notanide", uid=1000, gid=1000)


def test_resolve_launcher_errors_when_nothing_is_installed(mocker):
    mocker.patch.object(Incus, "exec", return_value="")
    with pytest.raises(ValueError, match="jetbrains-toolbox"):
        resolve_launcher(Incus(), "c1", "idea", uid=1000, gid=1000)


def test_resolve_launcher_passes_uid_and_gid(mocker):
    # Regression guard: uid and gid must be passed to incus.exec so the search
    # runs as the dev user, not as root. Without them, the Toolbox tree (a
    # bind-mount of ~/.local/share/JetBrains/Toolbox) becomes unreadable.
    incus = Incus()
    path = "/opt/jetbrains-toolbox/apps/x/bin/idea\n"
    exec_mock = mocker.patch.object(Incus, "exec", return_value=path)
    resolve_launcher(incus, "c1", "idea", uid=1000, gid=1001)
    assert exec_mock.call_args.kwargs["uid"] == 1000
    assert exec_mock.call_args.kwargs["gid"] == 1001


def test_supported_launchers_track_the_ide_literal():
    from typing import get_args

    from jailbee.config import IdeName

    assert SUPPORTED_LAUNCHERS == frozenset(get_args(IdeName))
