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
    # `allocate.called` alone doesn't establish ordering: a regression that
    # allocated the slot *after* starting the detached process would pass
    # unchanged. Both mocks record into one shared list so the actual call
    # order is asserted.
    from jailbee.apps import get_app, launch
    from jailbee.incus import Incus

    cfg = make_cfg(tmp_path, browsers={"firefox": {"enabled": True}})
    calls: list[str] = []
    mocker.patch("jailbee.pool.allocate", side_effect=lambda *a, **k: calls.append("allocate"))
    mocker.patch("jailbee.pool.ensure_pool_dirs")
    mocker.patch(
        "jailbee.gui.launch_detached", side_effect=lambda *a, **k: calls.append("launch_detached")
    )
    launch(cfg, Incus(), "c1", get_app(cfg, "firefox"))
    assert calls == ["allocate", "launch_detached"]


def test_launch_announces_the_app_name_and_log_path(tmp_path, mocker, capsys):
    """The only thing telling a user where to look when a launched window
    never appears — dropping this `info(...)` call ships green otherwise.
    """
    from jailbee.apps import app_log_path, get_app, launch
    from jailbee.incus import Incus

    # A plain config app (no `pool`) so this test doesn't need to mock
    # `jailbee.pool` — that's `test_launch_allocates_the_pool_slot_before_starting`'s job.
    cfg = make_cfg(tmp_path, apps={"figma": {"command": "/opt/f/f"}})
    mocker.patch("jailbee.gui.launch_detached")
    launch(cfg, Incus(), "c1", get_app(cfg, "figma"))
    out = capsys.readouterr().out
    assert "figma" in out
    assert app_log_path("figma") in out


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


def test_probe_treats_any_non_present_output_as_missing(tmp_path, mocker):
    # The container script is meant to only ever print "present" or
    # "missing", but probe's own contract is narrower and safer than that:
    # anything that is not exactly "present" reads as absent. Malformed
    # stdout (a stray shell warning on the first line, a truncated read)
    # must not be misread as "present" by an implementation that, say,
    # checks `"present" in out` instead of equality.
    from jailbee.apps import AppSpec, probe
    from jailbee.incus import Incus

    cfg = make_cfg(tmp_path)
    mocker.patch.object(Incus, "exec", return_value="garbage\n")
    assert probe(cfg, Incus(), "c1", AppSpec(name="x", command=["/bin/x"])) == "missing"


def test_probe_uses_the_resolver_when_the_spec_has_one(tmp_path, mocker):
    # The JetBrains IDE spec's `command` is a display placeholder ("idea"),
    # never a real container path — the Toolbox launcher lives under
    # /opt/jetbrains-toolbox/apps/<id>/bin/, never on PATH. A probe that fell
    # through to the `command -v`/`test -x` shell check here would always
    # answer "missing" for a working install; asserting `incus.exec` was
    # never called pins that the resolver path is what actually ran, not
    # merely that the return value happens to match.
    from jailbee.apps import AppSpec, probe
    from jailbee.incus import Incus

    cfg = make_cfg(tmp_path)
    exec_mock = mocker.patch.object(Incus, "exec")
    resolver = mocker.Mock(return_value=["/opt/jetbrains-toolbox/apps/a/bin/idea"])
    spec = AppSpec(name="ide", command=["idea"], resolve_command=resolver)
    assert probe(cfg, Incus(), "c1", spec) == "present"
    resolver.assert_called_once_with(mocker.ANY, "c1")
    assert not exec_mock.called


def test_probe_treats_a_resolver_value_error_as_missing(tmp_path, mocker):
    # resolve_launcher raises ValueError when no matching launcher is found
    # inside the container (see ide.py). That must read as "missing", not
    # escape probe as an uncaught exception and take down `jailbee apps ls`.
    from jailbee.apps import AppSpec, probe
    from jailbee.incus import Incus

    cfg = make_cfg(tmp_path)
    exec_mock = mocker.patch.object(Incus, "exec")
    resolver = mocker.Mock(side_effect=ValueError("no launcher found"))
    spec = AppSpec(name="ide", command=["idea"], resolve_command=resolver)
    assert probe(cfg, Incus(), "c1", spec) == "missing"
    assert not exec_mock.called


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


def test_launch_appends_default_url_when_no_args_given(tmp_path, mocker):
    from jailbee.apps import AppSpec, launch
    from jailbee.incus import Incus

    cfg = make_cfg(tmp_path)
    detached = mocker.patch("jailbee.gui.launch_detached")
    spec = AppSpec(name="x", command=["/bin/x"], default_url="https://cfg.test")
    launch(cfg, Incus(), "c1", spec)
    assert detached.call_args.args[3].split()[-1] == "https://cfg.test"


def test_launch_prefers_explicit_args_over_default_url(tmp_path, mocker):
    # The regression this guards: browsers.py used to bake `default_url`
    # into `command` unconditionally, so a caller-supplied URL landed
    # *alongside* the configured one instead of replacing it (both ended up
    # on the launched argv). Assert the exact count, not containment — a
    # substring check can't tell one URL from two of the same string.
    from jailbee.apps import AppSpec, launch
    from jailbee.incus import Incus

    cfg = make_cfg(tmp_path)
    detached = mocker.patch("jailbee.gui.launch_detached")
    spec = AppSpec(name="x", command=["/bin/x"], default_url="https://cfg.test")
    launch(cfg, Incus(), "c1", spec, ["https://override.test"])
    inner = detached.call_args.args[3]
    assert inner.split() == ["/bin/x", "https://override.test"]


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


def test_launch_autostart_apps_continues_after_one_raises(tmp_path, mocker):
    """Per-app error containment lives here now — moved out of cli.py's two
    inline blocks (Task 14) since every autostart caller now goes through
    this one function. One spec's `launch` raising `ValueError` (e.g.
    `ide.resolve_launcher` finding no matching JetBrains Toolbox launcher in
    a freshly built image) must not stop a later app in the list, and must
    not propagate to the caller — there is no CLI invocation left to exit
    non-zero from at this point; the container is already up.
    """
    from jailbee.apps import launch_autostart_apps
    from jailbee.incus import Incus

    cfg = make_cfg(
        tmp_path,
        apps={
            "a": {"command": "/a", "autostart": True},
            "b": {"command": "/b", "autostart": True},
        },
    )

    def fake_launch(cfg, incus, container, spec, args=None):
        if spec.name == "a":
            raise ValueError("No launcher found for 'a'")

    launch = mocker.patch("jailbee.apps.launch", side_effect=fake_launch)
    error_mock = mocker.patch("jailbee.tui.error")

    # Must not raise: one app's resolver failing must not abort the caller.
    launch_autostart_apps(cfg, Incus(), "c1")

    launched = {c.args[3].name for c in launch.call_args_list}
    assert launched == {"a", "b"}
    error_mock.assert_called_once()
    assert "'a'" in error_mock.call_args.args[0]
