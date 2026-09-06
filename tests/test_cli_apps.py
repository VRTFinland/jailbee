"""Tests for `jailbee apps`."""

from __future__ import annotations

from typer.testing import CliRunner

from jailbee.cli import app

runner = CliRunner()


def test_apps_ls_lists_configured_apps(tmp_path, mocker, monkeypatch):
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path, apps={"figma": {"command": "/opt/f/f", "description": "Figma"}})
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    result = runner.invoke(app, ["apps", "ls"])
    assert result.exit_code == 0
    assert "figma" in result.output
    assert "config" in result.output


def test_apps_ls_omits_status_without_a_container(tmp_path, mocker):
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path, apps={"figma": {"command": "/opt/f/f"}})
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    probe = mocker.patch("jailbee.apps.probe")
    result = runner.invoke(app, ["apps", "ls"])
    assert "STATUS" not in result.output
    assert not probe.called


def test_apps_ls_probes_when_given_a_container(tmp_path, mocker):
    from jailbee.apps import get_app
    from jailbee.incus import Incus
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path, apps={"figma": {"command": "/opt/f/f"}})
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    incus = Incus()
    mocker.patch("jailbee.cli._resolve_attachable", return_value=(incus, "c1"))
    probe = mocker.patch("jailbee.apps.probe", autospec=True, return_value="missing")
    result = runner.invoke(app, ["apps", "ls", "c1"])
    assert "STATUS" in result.output
    assert "missing" in result.output
    # Finding 3: pin the full cfg-first call, not just the return value.
    # `probe`'s `cfg` parameter exists so it can run as the container user
    # rather than as an unprivileged container root that cannot stat
    # host-mounted browsers — a swap of cfg/incus/container still
    # type-checks, so only an exact-call assertion (autospec catches an
    # arity/name mismatch; assert_called_once_with catches a swap) closes
    # that gap.
    probe.assert_called_once_with(cfg, incus, "c1", get_app(cfg, "figma"))


def test_apps_ls_shows_status_column_with_a_container_even_if_no_apps_are_configured(
    tmp_path, mocker
):
    """The STATUS column must be gated on "was a container named", not on
    dict truthiness. `apps_ls_cmd` used to build the column list with
    `if status:` — with zero configured apps, `status` is `{}` regardless of
    whether a container was given, so that check drops STATUS for the wrong
    reason (it happens to look right only because there are no rows either
    way). Pin the real condition: an empty registry plus a named container
    must still render the STATUS header.
    """
    from jailbee.incus import Incus
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path)
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.cli._resolve_attachable", return_value=(Incus(), "c1"))
    result = runner.invoke(app, ["apps", "ls", "c1"])
    assert "STATUS" in result.output


def test_apps_ls_empty_registry_says_so(tmp_path, mocker):
    from tests.conftest import make_cfg

    mocker.patch("jailbee.cli._load_or_exit", return_value=make_cfg(tmp_path))
    result = runner.invoke(app, ["apps", "ls"])
    assert result.exit_code == 0
    assert "No GUI apps" in result.output


def test_apps_run_launches_the_named_app(tmp_path, mocker):
    from jailbee.apps import get_app
    from jailbee.incus import Incus
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path, apps={"figma": {"command": "/opt/f/f"}})
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    incus = Incus()
    resolve_attachable = mocker.patch("jailbee.cli._resolve_attachable", return_value=(incus, "c1"))
    launch = mocker.patch("jailbee.apps.launch", autospec=True)
    result = runner.invoke(app, ["apps", "run", "figma", "--container", "c1", "--", "--flag"])
    assert result.exit_code == 0
    # RULING 24: container is `--container`, not a positional — assert it
    # actually reached `_resolve_attachable` as the option value.
    assert resolve_attachable.call_args.args[1] == "c1"
    # Finding 3: pin the full cfg-first call, not just spec/args — `launch`'s
    # leading cfg/incus/container exist for the same container-user-not-root
    # reason as `probe`'s, and a swap among them still type-checks.
    launch.assert_called_once_with(cfg, incus, "c1", get_app(cfg, "figma"), ["--flag"])


def test_apps_run_unknown_name_exits_2_and_lists_options(tmp_path, mocker):
    from jailbee.incus import Incus
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path, apps={"figma": {"command": "/opt/f/f"}})
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.cli._resolve_attachable", return_value=(Incus(), "c1"))
    result = runner.invoke(app, ["apps", "run", "nope", "--container", "c1"])
    assert result.exit_code == 2
    assert "figma" in result.output


def test_apps_run_omitting_container_resolves_default(tmp_path, mocker):
    """App-first, container optional: `jailbee apps run figma` alone must resolve.

    Distinguishes app-first from container-first-optional: under the brief's
    original (wrong) order, this exact invocation would bind "figma" to the
    container slot and the app slot would be None, which errors instead of
    launching.
    """
    from jailbee.apps import get_app
    from jailbee.incus import Incus
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path, apps={"figma": {"command": "/opt/f/f"}})
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    incus = Incus()
    resolve_attachable = mocker.patch("jailbee.cli._resolve_attachable", return_value=(incus, "c1"))
    launch = mocker.patch("jailbee.apps.launch", autospec=True)
    result = runner.invoke(app, ["apps", "run", "figma"])
    assert result.exit_code == 0
    # No --container was given, so `_resolve_attachable` must have been
    # called with container=None, not "figma".
    assert resolve_attachable.call_args.args[1] is None
    launch.assert_called_once_with(cfg, incus, "c1", get_app(cfg, "figma"), [])


def test_apps_run_args_after_double_dash_are_not_swallowed_as_container(tmp_path, mocker):
    """RULING 24 — the whole point of moving the container behind `--container`.

    With three positionals (app_name, [name], [args]...) and no container
    given, `jailbee apps run figma -- --flag` used to bind "--flag" to the
    middle `name` (container) slot, since Click fills fixed-arity positionals
    before the trailing variadic one regardless of what the user meant to
    skip. Pin the fix directly: with the container behind `--container`,
    the same invocation must put "--flag" in `args` and leave the container
    unset (`_resolve_attachable` called with `None`), not the reverse.
    """
    from jailbee.apps import get_app
    from jailbee.incus import Incus
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path, apps={"figma": {"command": "/opt/f/f"}})
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    incus = Incus()
    resolve_attachable = mocker.patch("jailbee.cli._resolve_attachable", return_value=(incus, "c1"))
    launch = mocker.patch("jailbee.apps.launch", autospec=True)
    result = runner.invoke(app, ["apps", "run", "figma", "--", "--flag"])
    assert result.exit_code == 0
    assert resolve_attachable.call_args.args[1] is None
    launch.assert_called_once_with(cfg, incus, "c1", get_app(cfg, "figma"), ["--flag"])
