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


def test_browser_opens_the_single_enabled_browser(tmp_path, mocker):
    from jailbee.incus import Incus
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path, browsers={"firefox": {"enabled": True}})
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.cli._resolve_attachable", return_value=(Incus(), "c1"))
    launch = mocker.patch("jailbee.apps.launch")
    assert runner.invoke(app, ["browser", "c1"]).exit_code == 0
    assert launch.call_args.args[3].name == "firefox"


def test_browser_with_two_enabled_and_no_default_explains(tmp_path, mocker):
    from tests.conftest import make_cfg

    cfg = make_cfg(
        tmp_path,
        browsers={"chrome": {"enabled": True}, "firefox": {"enabled": True}},
    )
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    result = runner.invoke(app, ["browser", "c1"])
    assert result.exit_code == 2
    assert "browsers.default" in result.output
    assert "chrome" in result.output and "firefox" in result.output


def test_browser_with_none_enabled_says_so(tmp_path, mocker):
    from tests.conftest import make_cfg

    mocker.patch("jailbee.cli._load_or_exit", return_value=make_cfg(tmp_path))
    result = runner.invoke(app, ["browser", "c1"])
    assert result.exit_code == 2
    assert "No browser is enabled" in result.output


def test_firefox_command_errors_when_disabled(tmp_path, mocker):
    from tests.conftest import make_cfg

    mocker.patch("jailbee.cli._load_or_exit", return_value=make_cfg(tmp_path))
    result = runner.invoke(app, ["firefox", "c1"])
    assert result.exit_code == 2
    assert "browsers.firefox.enabled" in result.output


def test_chrome_url_argument_still_overrides_config(tmp_path, mocker):
    from jailbee.incus import Incus
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path, browsers={"chrome": {"enabled": True, "url": "https://cfg.test"}})
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.cli._resolve_attachable", return_value=(Incus(), "c1"))
    launch = mocker.patch("jailbee.apps.launch")
    runner.invoke(app, ["chrome", "c1", "https://call.test"])
    assert launch.call_args.args[4] == ["https://call.test"]


def test_chrome_with_no_explicit_url_passes_none_and_lets_the_spec_supply_it(tmp_path, mocker):
    """Regression guard for the Task 10 double-URL bug: `chrome_cmd` must
    pass only the explicit URL (or None) to `launch`, never
    `url or cfg.chrome.url` — the spec's own `default_url` already carries
    the configured URL, and `apps.launch` appends it only when `args` is
    falsy. Asserting the configured URL's *count* on the launched argv (via
    `launch`'s own `args` parameter here, count semantics owned by
    `apps.launch` and covered in `test_apps.py`) is what would catch a
    regression that passed the URL twice.
    """
    from jailbee.incus import Incus
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path, browsers={"chrome": {"enabled": True, "url": "https://cfg.test"}})
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.cli._resolve_attachable", return_value=(Incus(), "c1"))
    launch = mocker.patch("jailbee.apps.launch")
    runner.invoke(app, ["chrome", "c1"])
    assert launch.call_args.args[4] is None
    assert launch.call_args.args[3].default_url == "https://cfg.test"


def test_ide_app_flag_one_off_spec_passes_container_user_uid_gid(tmp_path, mocker):
    """The one-off `AppSpec` built for `--app <ide other than cfg.jetbrains.ide>`
    must forward the configured container uid/gid to `resolve_launcher` — it
    runs the Toolbox search as the container user because container root is
    an unprivileged subuid that cannot read the host-mounted Toolbox tree.
    This exact wiring has already been dropped twice; `launch` runs for real
    here (not mocked) so `spec.resolve_command`'s closure actually executes,
    which a mocked `apps.launch` would skip entirely.
    """
    from jailbee.incus import Incus
    from tests.conftest import make_cfg

    cfg = make_cfg(
        tmp_path,
        jetbrains={"enabled": True, "ide": "idea"},
        container_user={"uid": 4242, "gid": 4343},
    )
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.cli._resolve_attachable", return_value=(Incus(), "c1"))
    resolve_launcher = mocker.patch(
        "jailbee.ide.resolve_launcher", return_value=["/opt/x/bin/pycharm"]
    )
    mocker.patch("jailbee.gui.launch_detached")
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")

    result = runner.invoke(app, ["ide", "c1", "--app", "pycharm"])
    assert result.exit_code == 0, result.output
    assert resolve_launcher.call_args.kwargs["uid"] == 4242
    assert resolve_launcher.call_args.kwargs["gid"] == 4343


def test_ide_reports_missing_launcher_instead_of_a_traceback(tmp_path, mocker):
    """Finding 1 (review round): a container built before the Toolbox mount
    existed, or `toolbox_host_path: null`, is an ordinary state — not a
    crash. `resolve_launcher`'s `ValueError` must be caught around the
    `launch` call and reported with exit 2, not reach Typer unhandled (which
    would print a traceback with no exit code jailbee itself chose).
    """
    from jailbee.incus import Incus
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path, jetbrains={"enabled": True, "ide": "idea"})
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    incus = Incus()
    mocker.patch("jailbee.cli._resolve_attachable", return_value=(incus, "c1"))
    mocker.patch.object(Incus, "exec", return_value="")  # no launcher found
    detached = mocker.patch("jailbee.gui.launch_detached")

    result = runner.invoke(app, ["ide", "c1"])

    assert result.exit_code == 2
    assert "idea" in result.output
    assert "jetbrains-toolbox" in result.output
    assert not detached.called


def test_apps_run_ide_reports_missing_launcher_instead_of_a_traceback(tmp_path, mocker):
    """The same hole existed in `apps run ide`, predating Task 14 — same fix,
    same shape of test.
    """
    from jailbee.incus import Incus
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path, jetbrains={"enabled": True, "ide": "idea"})
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    incus = Incus()
    mocker.patch("jailbee.cli._resolve_attachable", return_value=(incus, "c1"))
    mocker.patch.object(Incus, "exec", return_value="")
    detached = mocker.patch("jailbee.gui.launch_detached")

    result = runner.invoke(app, ["apps", "run", "ide", "--container", "c1"])

    assert result.exit_code == 2
    assert "idea" in result.output
    assert not detached.called


def test_ide_app_flag_one_off_spec_reports_missing_launcher_instead_of_a_traceback(
    tmp_path, mocker
):
    """The one-off `AppSpec` built for `--app <ide other than cfg.jetbrains.ide>`
    has its own `resolve_command` closure — same failure mode, same fix,
    verified separately since it is not built via `apps.get_app`.
    """
    from jailbee.incus import Incus
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path, jetbrains={"enabled": True, "ide": "idea"})
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    incus = Incus()
    mocker.patch("jailbee.cli._resolve_attachable", return_value=(incus, "c1"))
    mocker.patch.object(Incus, "exec", return_value="")
    detached = mocker.patch("jailbee.gui.launch_detached")

    result = runner.invoke(app, ["ide", "c1", "--app", "pycharm"])

    assert result.exit_code == 2
    assert "pycharm" in result.output
    assert not detached.called
