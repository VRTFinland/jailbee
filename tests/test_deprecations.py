"""The deprecation inventory: every notice that promises a removal release.

Four spellings from before 1.3.0 are still accepted, each with a notice
telling the user where the thing moved. Before this file existed those
notices disagreed: `chrome:` promised removal in 1.4.0, `.gie/config.yaml`
in 2.0.0, and `jailbee chrome-pool` and `jailbee submodule checkout` named
no release at all — so a user could not tell how long any of them had, and
`chrome:` had been given exactly one minor release of grace despite living
in `~/.config/jailbee/global.yaml` on every host that ever enabled Chrome.

They now interpolate `constants.LEGACY_REMOVAL_VERSION`, and these tests
pin that each notice actually reaches the user carrying it. A notice that
regresses to a hardcoded release fails here; one whose text is reworded
does not, which is the intended sensitivity — the promise is the contract,
not the wording.

Not covered on purpose: `golden.python`, `dashboard:` and
`global.dashboard`. Those keys are already ignored rather than honoured, so
they cannot break when they stop being read. `test_they_are_cleanup_advice`
pins that they stay version-free.
"""

from typer.testing import CliRunner

from jailbee.cli import app
from jailbee.constants import LEGACY_REMOVAL_VERSION

runner = CliRunner()


def test_the_removal_release_is_the_one_that_drops_the_gie_directory():
    """`.gie/config.yaml` has promised 2.0.0 since 1.1.0 — that promise is
    the fixed point every other legacy spelling was aligned to, so it is
    the one thing here asserted against a literal.
    """
    assert LEGACY_REMOVAL_VERSION == "2.0.0"


def test_legacy_chrome_block_notice_names_the_removal_release(tmp_path, capsys):
    from jailbee.config.loader import load_config_from_text

    load_config_from_text(
        "container_prefix: myrepo\nchrome:\n  enabled: true\n",
        tmp_path / ".jailbee" / "config.yaml",
    )
    assert LEGACY_REMOVAL_VERSION in capsys.readouterr().err


def test_legacy_gie_dir_notice_names_the_removal_release(tmp_path, mocker):
    from jailbee import paths

    warn = mocker.patch("jailbee.tui.warn_plain")
    legacy = tmp_path / ".gie" / "config.yaml"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("defaults:\n  cpu: 3\n")

    paths.repo_config_path_warned(tmp_path)

    assert warn.call_count == 1
    assert LEGACY_REMOVAL_VERSION in warn.call_args[0][0]


def test_chrome_pool_alias_notice_names_the_removal_release(mocker, tmp_path):
    cfg_mock = mocker.MagicMock()
    cfg_mock.repo_root = tmp_path
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg_mock)
    warn = mocker.patch("jailbee.cli.warn")
    mocker.patch("jailbee.cli.pool_ls_cmd")

    result = runner.invoke(app, ["chrome-pool", "ls"])

    assert result.exit_code == 0, result.output
    assert warn.call_count == 1
    assert LEGACY_REMOVAL_VERSION in warn.call_args[0][0]


def test_submodule_checkout_alias_notice_names_the_removal_release(mocker, tmp_path):
    cfg_mock = mocker.MagicMock()
    cfg_mock.repo_root = tmp_path
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg_mock)
    mocker.patch("jailbee.sync.checkout_submodules_on_host", return_value=("master", []))

    result = runner.invoke(app, ["submodule", "checkout", "-b", "master"])

    assert result.exit_code == 0, result.output
    combined = (result.output or "") + (result.stderr or "")
    assert LEGACY_REMOVAL_VERSION in combined


def test_they_are_cleanup_advice(tmp_path):
    """`golden.python`, `dashboard:` and `global.dashboard` name no release.

    These are already ignored, so nothing changes the day they stop being
    read: the advice is "delete the key", not "you have until X". Naming a
    release would promise a behaviour change that will never arrive. If one
    of them ever does start mattering again, that is the point at which it
    earns a `LEGACY_REMOVAL_VERSION` — and this test is where you find out
    you have to decide.
    """
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path, golden={"python": "3.13"}, dashboard={"fields": ["name"]})
    advisories = [i for i in cfg.validate_runtime() if "deprecated" in i]

    assert advisories, "expected the golden.python and dashboard: advisories"
    for issue in advisories:
        assert LEGACY_REMOVAL_VERSION not in issue, issue
