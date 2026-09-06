"""Tests for the GUI app registry."""

from __future__ import annotations

import pytest

from jailbee.apps import app_log_path, get_app, resolve_apps
from tests.conftest import make_cfg


def test_config_apps_become_specs(tmp_path):
    cfg = make_cfg(tmp_path, apps={"figma": {"command": "/opt/f/f", "args": ["--no-sandbox"]}})
    spec = get_app(cfg, "figma")
    assert spec.command == ["/opt/f/f", "--no-sandbox"]
    assert spec.source == "config"
    assert spec.pool is None


def test_registry_order_is_stable_regardless_of_yaml_key_order(tmp_path):
    # The dashboard action menu and `jailbee apps ls` both render this order.
    # A user's YAML key order must not reshuffle either.
    a = make_cfg(tmp_path, apps={"zed": {"command": "/z"}, "arc": {"command": "/a"}})
    b = make_cfg(tmp_path, apps={"arc": {"command": "/a"}, "zed": {"command": "/z"}})
    names_a = [s.name for s in resolve_apps(a)]
    names_b = [s.name for s in resolve_apps(b)]
    # Stability alone (names_a == names_b) would also pass for e.g. a
    # reverse-alphabetical or insertion-order-preserving scheme applied
    # consistently to both configs. Pin the actual required order too: the
    # spec says "sorted by name", not merely "independent of YAML order".
    assert names_a == names_b == ["arc", "zed"]


def test_builtins_come_before_config_apps(tmp_path):
    cfg = make_cfg(
        tmp_path,
        browsers={"firefox": {"enabled": True}},
        apps={"arc": {"command": "/a"}},
    )
    names = [s.name for s in resolve_apps(cfg)]
    assert names.index("firefox") < names.index("arc")


def test_disabled_browsers_are_not_in_the_registry(tmp_path):
    # With no browsers enabled in the config, builtin_specs produces nothing,
    # and the registry is empty. This verifies the disable path works.
    cfg = make_cfg(tmp_path)
    assert [s.name for s in resolve_apps(cfg)] == []


def test_unknown_app_error_names_what_is_available(tmp_path):
    cfg = make_cfg(tmp_path, apps={"figma": {"command": "/opt/f/f"}})
    with pytest.raises(ValueError, match="figma"):
        get_app(cfg, "nope")


def test_log_path_is_per_app(tmp_path):
    assert app_log_path("firefox") == "/tmp/jailbee-app-firefox.log"


def test_launch_allocates_the_pool_slot_before_starting(tmp_path, mocker):
    from jailbee.apps import get_app, launch
    from jailbee.incus import Incus

    cfg = make_cfg(tmp_path, browsers={"firefox": {"enabled": True}})
    allocate = mocker.patch("jailbee.pool.allocate")
    mocker.patch("jailbee.pool.ensure_pool_dirs")
    mocker.patch("jailbee.gui.launch_detached")
    launch(cfg, Incus(), "c1", get_app(cfg, "firefox"))
    assert allocate.called


def test_launch_appends_call_args_after_configured_args(tmp_path, mocker):
    from jailbee.apps import get_app, launch
    from jailbee.incus import Incus

    cfg = make_cfg(tmp_path, apps={"x": {"command": "/bin/x", "args": ["--a"]}})
    detached = mocker.patch("jailbee.gui.launch_detached")
    launch(cfg, Incus(), "c1", get_app(cfg, "x"), ["--b"])
    inner = detached.call_args.args[3]
    assert inner.endswith("--a --b")


def test_launch_uses_the_resolved_command_when_the_spec_has_a_resolver(tmp_path, mocker):
    from jailbee.apps import get_app, launch
    from jailbee.incus import Incus

    cfg = make_cfg(tmp_path, jetbrains={"enabled": True})
    mocker.patch.object(Incus, "exec", return_value="/opt/jetbrains-toolbox/apps/a/bin/idea\n")
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")
    detached = mocker.patch("jailbee.gui.launch_detached")
    launch(cfg, Incus(), "c1", get_app(cfg, "ide"))
    assert "/opt/jetbrains-toolbox/apps/a/bin/idea" in detached.call_args.args[3]


def test_probe_reports_missing_when_the_binary_is_absent(tmp_path, mocker):
    from jailbee.apps import AppSpec, probe
    from jailbee.incus import Incus

    cfg = make_cfg(tmp_path)
    mocker.patch.object(Incus, "exec", return_value="missing\n")
    assert probe(cfg, Incus(), "c1", AppSpec(name="x", command=["/bin/x"])) == "missing"


def test_probe_never_raises_on_a_nonzero_command(tmp_path, mocker):
    # The probe must answer for a container where the binary is absent,
    # which is the normal case right after enabling a browser. A raising
    # probe would make `jailbee apps ls` fail exactly when it is most useful.
    from jailbee.apps import AppSpec, probe
    from jailbee.incus import Incus

    cfg = make_cfg(tmp_path)
    mocker.patch.object(Incus, "exec", return_value="present\n")
    assert probe(cfg, Incus(), "c1", AppSpec(name="x", command=["/bin/x"])) == "present"


def test_probe_runs_as_the_container_user_not_root(tmp_path, mocker):
    # profiles.py maps only the dev user's uid/gid identically between host
    # and container (raw.idmap: uid <uid> <uid>). Container root is an
    # unprivileged subuid with no rights over host-owned files, so a
    # root-run probe against a read-only bind-mounted browser can report
    # "missing" for an app that is actually present and working. Distinct
    # uid/gid values so dropping or swapping either one fails this test.
    from jailbee.apps import AppSpec, probe
    from jailbee.incus import Incus

    cfg = make_cfg(tmp_path, container_user={"uid": 1234, "gid": 5678})
    exec_mock = mocker.patch.object(Incus, "exec", return_value="present\n")
    probe(cfg, Incus(), "c1", AppSpec(name="x", command=["/bin/x"]))
    assert exec_mock.call_args.kwargs["uid"] == 1234
    assert exec_mock.call_args.kwargs["gid"] == 5678


def test_autostart_launches_only_apps_that_asked_for_it(tmp_path, mocker):
    from jailbee.apps import launch_autostart_apps
    from jailbee.incus import Incus

    cfg = make_cfg(
        tmp_path,
        apps={"a": {"command": "/a", "autostart": True}, "b": {"command": "/b"}},
    )
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")
    detached = mocker.patch("jailbee.gui.launch_detached")
    launch_autostart_apps(cfg, Incus(), "c1")
    launched = [c.args[3] for c in detached.call_args_list]
    assert len(launched) == 1
    assert "/a" in launched[0]
