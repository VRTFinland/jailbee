"""Host-only LiteLLM CLI commands and their user-visible diagnostics."""

from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from jailbee import litellm as ll
from jailbee.cli import app
from jailbee.global_config import GlobalConfig

runner = CliRunner()


@pytest.fixture
def context(mocker):
    gcfg = GlobalConfig.model_validate({"litellm": {"enabled": True}})
    return mocker.patch("jailbee.cli._litellm_context", return_value=(MagicMock(), gcfg))


def _accounts(context, **litellm):
    gcfg = GlobalConfig.model_validate({"litellm": {"enabled": True, **litellm}})
    context.return_value = (context.return_value[0], gcfg)


def test_up_says_what_to_do_for_an_account_without_a_login(mocker, context):
    mocker.patch(
        "jailbee.litellm.litellm_up",
        return_value=ll.UpResult(
            ip="10.0.0.3",
            ports={"default": 4100},
            restarted=[],
            retired=[],
            installed=True,
            awaiting_login=["default"],
        ),
    )
    result = runner.invoke(app, ["litellm", "up"])
    out = " ".join(result.output.split())
    assert result.exit_code == 0, result.output
    assert "running on" not in out
    assert "jailbee litellm login <account>" in out and "jailbee litellm up" in out


def test_up_prints_endpoint_and_next_steps(mocker, context):
    up = mocker.patch(
        "jailbee.litellm.litellm_up",
        return_value=ll.UpResult(
            ip="10.0.0.3",
            ports={"default": 4100},
            restarted=["default"],
            retired=[],
            installed=True,
            issues=["/x/repos/broken.yaml is broken"],
        ),
    )
    result = runner.invoke(app, ["litellm", "up", "--reinstall"])
    out = " ".join(result.output.split())
    assert result.exit_code == 0, result.output
    assert "10.0.0.3" in out and ":4100" in out
    assert "jailbee apply" in out
    assert "in-flight" in out
    assert "broken.yaml" in out
    assert up.call_args.kwargs["reinstall"] is True
    assert callable(up.call_args.kwargs["on_step"])


def test_up_says_when_routes_were_reloaded_without_a_restart(mocker, context):
    mocker.patch(
        "jailbee.litellm.litellm_up",
        return_value=ll.UpResult(
            ip="10.0.0.3",
            ports={"default": 4100},
            restarted=[],
            retired=[],
            installed=False,
            reloaded=["default"],
        ),
    )
    out = " ".join(runner.invoke(app, ["litellm", "up"]).output.split())
    assert "Reloaded the routes of default without a restart" in out
    assert "interrupted" not in out


def test_up_explains_a_reload_that_fell_back_to_a_restart(mocker, context):
    mocker.patch(
        "jailbee.litellm.litellm_up",
        return_value=ll.UpResult(
            ip="10.0.0.3",
            ports={"default": 4100},
            restarted=["default"],
            retired=[],
            installed=False,
            fallbacks={"default": "the proxy did not acknowledge the new routes in time"},
        ),
    )
    out = " ".join(runner.invoke(app, ["litellm", "up"]).output.split())
    assert "default could not reload live (the proxy did not acknowledge" in out


def test_up_disabled_is_exit_1_with_message(mocker, context):
    mocker.patch("jailbee.litellm.litellm_up", side_effect=ValueError("LiteLLM is disabled"))
    result = runner.invoke(app, ["litellm", "up"])
    assert result.exit_code == 1 and "disabled" in result.output
    assert "Traceback" not in result.output


def test_up_warns_if_install_is_unlocked(mocker, context):
    context.return_value[1].litellm.version = "1.104.0"
    mocker.patch(
        "jailbee.litellm.litellm_up",
        return_value=ll.UpResult(
            ip="10.0.0.3",
            ports={"default": 4100},
            restarted=[],
            retired=[],
            installed=False,
        ),
    )
    result = runner.invoke(app, ["litellm", "up"])
    assert result.exit_code == 0
    assert "hash-locked" in result.output
    assert "in-flight" not in result.output


def test_up_names_restarted_and_retired_accounts(mocker, context):
    mocker.patch(
        "jailbee.litellm.litellm_up",
        return_value=ll.UpResult(
            ip="10.79.115.3",
            ports={"a": 4100, "b": 4101},
            restarted=["b"],
            retired=["old"],
            installed=False,
        ),
    )
    result = runner.invoke(app, ["litellm", "up"])
    out = " ".join(result.output.split())
    assert result.exit_code == 0, result.output
    assert "Restarted b" in out
    assert "Stopped old" in out and "logins are kept" in out


def test_up_reports_a_missing_secret_as_a_clean_error(mocker, context):
    from jailbee.litellm_inputs import LiteLLMInputError

    mocker.patch(
        "jailbee.litellm.litellm_up",
        side_effect=LiteLLMInputError("OPENROUTER_API_KEY is not set in secrets.env"),
    )
    result = runner.invoke(app, ["litellm", "up"])
    assert result.exit_code == 1
    assert "OPENROUTER_API_KEY" in result.output
    assert "Traceback" not in result.output


def test_down_removes_proxy_but_keeps_login(mocker, context):
    down = mocker.patch("jailbee.litellm.litellm_down")
    result = runner.invoke(app, ["litellm", "down"])
    assert result.exit_code == 0, result.output
    assert "logins and settings are kept" in " ".join(result.output.split())
    down.assert_called_once_with(context.return_value[0], purge=False)


def test_status_never_prints_tokens(mocker, context):
    mocker.patch(
        "jailbee.litellm.litellm_status",
        return_value=ll.LiteLLMStatus(
            ll.ContainerState.RUNNING,
            "10.0.0.3",
            "1.103.1",
            [ll.InstanceStatus("default", 4100, True, True, "present")],
        ),
    )
    result = runner.invoke(app, ["litellm", "status"])
    assert result.exit_code == 0, result.output
    assert "running" in result.output and "logged in" in result.output
    assert "10.0.0.3" in result.output and "1.103.1" in result.output
    assert "4100" in result.output


@pytest.mark.parametrize(
    "status",
    [
        ll.LiteLLMStatus(ll.ContainerState.MISSING, None, None, []),
        ll.LiteLLMStatus(ll.ContainerState.STOPPED, "10.0.0.3", None, []),
        ll.LiteLLMStatus(ll.ContainerState.RUNNING, "10.0.0.3", "1.103.1", []),
        ll.LiteLLMStatus(
            ll.ContainerState.RUNNING,
            "10.0.0.3",
            "1.103.1",
            [ll.InstanceStatus("default", 4100, True, False, "missing")],
        ),
    ],
)
def test_status_exits_nonzero_when_proxy_is_unavailable(mocker, context, status):
    mocker.patch("jailbee.litellm.litellm_status", return_value=status)
    result = runner.invoke(app, ["litellm", "status"])
    assert result.exit_code == 1
    assert status.container.value in result.output
    if status.instances:
        assert "not logged in" in result.output
        assert "jailbee litellm login" in result.output


_TWO = {"accounts": ["personal", "work"], "profiles": {"codex": {"account": "personal"}}}


@pytest.mark.parametrize("command", ["login", "logout", "logs"])
def test_an_unknown_account_is_rejected_before_side_effects(mocker, context, command):
    mocker.patch("jailbee.litellm.login_providers", return_value=())
    target = {"login": "litellm_login", "logout": "litellm_logout", "logs": "litellm_logs"}[command]
    called = mocker.patch(f"jailbee.litellm.{target}")
    result = runner.invoke(app, ["litellm", command, "nope"])
    assert result.exit_code == 2
    assert "Unknown LiteLLM account 'nope'" in " ".join(result.output.split())
    called.assert_not_called()


@pytest.mark.parametrize("command", ["login", "logout", "logs"])
def test_several_accounts_off_a_tty_name_the_candidates(mocker, context, command):
    _accounts(context, **_TWO)
    mocker.patch("jailbee.prompting.is_interactive", return_value=False)
    mocker.patch("jailbee.litellm.login_providers", return_value=())
    target = {"login": "litellm_login", "logout": "litellm_logout", "logs": "litellm_logs"}[command]
    called = mocker.patch(f"jailbee.litellm.{target}")
    result = runner.invoke(app, ["litellm", command])
    assert result.exit_code == 2
    assert "Candidates: personal, work" in " ".join(result.output.split())
    called.assert_not_called()


def test_several_accounts_on_a_tty_pick_one(mocker, context):
    _accounts(context, **_TWO)
    mocker.patch("jailbee.litellm.login_providers", return_value=())
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)
    mocker.patch("jailbee.prompting._select", return_value="work")
    logs = mocker.patch("jailbee.litellm.litellm_logs", return_value=0)
    result = runner.invoke(app, ["litellm", "logs"])
    assert result.exit_code == 0, result.output
    logs.assert_called_once_with(context.return_value[0], "work", follow=False)


def test_a_single_account_is_taken_and_announced(mocker, context):
    _accounts(context, accounts=["only"], profiles={"codex": {"account": "only"}})
    mocker.patch("jailbee.prompting.is_interactive", return_value=False)
    logs = mocker.patch("jailbee.litellm.litellm_logs", return_value=0)
    result = runner.invoke(app, ["litellm", "logs"])
    assert result.exit_code == 0, result.output
    assert "Using LiteLLM account only" in " ".join(result.output.split())
    logs.assert_called_once_with(context.return_value[0], "only", follow=False)


def test_cancelling_the_account_pick_exits_1_without_side_effects(mocker, context):
    _accounts(context, **_TWO)
    mocker.patch("jailbee.litellm.login_providers", return_value=())
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)
    mocker.patch("jailbee.prompting._select", return_value=None)
    called = mocker.patch("jailbee.litellm.litellm_logout")
    result = runner.invoke(app, ["litellm", "logout"])
    assert result.exit_code == 1
    called.assert_not_called()


def test_a_named_account_is_passed_through(mocker, context):
    _accounts(context, **_TWO)
    logs = mocker.patch("jailbee.litellm.litellm_logs", return_value=0)
    runner.invoke(app, ["litellm", "logs", "work", "-f"])
    logs.assert_called_once_with(context.return_value[0], "work", follow=True)


def test_login_returns_device_flow_exit_code(mocker, context):
    mocker.patch("jailbee.litellm.login_providers", return_value=())
    login = mocker.patch("jailbee.litellm.litellm_login", return_value=19)
    result = runner.invoke(app, ["litellm", "login"])
    assert result.exit_code == 19
    login.assert_called_once_with(context.return_value[0], "default")


def test_logout(mocker, context):
    mocker.patch("jailbee.litellm.login_providers", return_value=())
    mocker.patch("jailbee.litellm.litellm_logout", return_value=True)
    result = runner.invoke(app, ["litellm", "logout"])
    assert result.exit_code == 0 and "Logged out" in result.output


def test_logout_without_existing_auth(mocker, context):
    mocker.patch("jailbee.litellm.login_providers", return_value=())
    mocker.patch("jailbee.litellm.litellm_logout", return_value=False)
    result = runner.invoke(app, ["litellm", "logout"])
    assert result.exit_code == 0 and "Not logged in" in result.output


def test_logs_passes_follow_and_exit_code(mocker, context):
    logs = mocker.patch("jailbee.litellm.litellm_logs", return_value=17)
    result = runner.invoke(app, ["litellm", "logs", "-f"])
    assert result.exit_code == 17
    logs.assert_called_once_with(context.return_value[0], "default", follow=True)


def test_down_purge_is_passed_through(mocker, context):
    down = mocker.patch("jailbee.litellm.litellm_down")
    result = runner.invoke(app, ["litellm", "down", "--purge"])
    assert result.exit_code == 0, result.output
    down.assert_called_once_with(context.return_value[0], purge=True)
    assert "logins are gone" in " ".join(result.output.split())


def test_logout_needs_a_running_proxy(mocker, context):
    mocker.patch("jailbee.litellm.login_providers", return_value=())
    mocker.patch(
        "jailbee.litellm.litellm_logout", side_effect=RuntimeError("run jailbee litellm up")
    )
    result = runner.invoke(app, ["litellm", "logout"])
    assert result.exit_code == 1
    assert "jailbee litellm up" in result.output


def test_ls_lists_host_and_repo_blocks(mocker, tmp_path, monkeypatch):
    import yaml

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    repos = tmp_path / "jailbee" / "repos"
    repos.mkdir(parents=True)
    (repos / "myrepo.yaml").write_text(
        yaml.safe_dump({"litellm": {"routes": {"sol-high": {"effort": "max"}}}})
    )
    (repos / "broken.yaml").write_text(yaml.safe_dump({"litellm": {"default_profile": "nope"}}))
    mocker.patch(
        "jailbee.cli._load_global",
        return_value=GlobalConfig.model_validate({"litellm": {"enabled": True}}),
    )
    result = runner.invoke(app, ["litellm", "ls"])
    assert result.exit_code == 0, result.output
    assert "codex*" in result.output
    assert "repo myrepo" in result.output and "jb-myrepo.<profile>.<level>" in result.output
    # Rich folds long paths at the terminal width; compare without whitespace.
    assert "repos/broken.yaml" in "".join(result.output.split())


def test_ls_says_when_litellm_is_disabled(mocker, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    mocker.patch("jailbee.cli._load_global", return_value=GlobalConfig())
    result = runner.invoke(app, ["litellm", "ls"])
    assert result.exit_code == 0, result.output
    assert "disabled" in result.output and "codex*" in result.output


def test_up_prints_an_issue_with_brackets_verbatim(mocker, context):
    issue = "/x/repos/a.yaml: routes.kimi [type=missing, input_type=dict] skipped"
    mocker.patch(
        "jailbee.litellm.litellm_up",
        return_value=ll.UpResult(
            ip="10.0.0.3",
            ports={"default": 4100},
            restarted=[],
            retired=[],
            installed=False,
            issues=[issue],
        ),
    )
    result = runner.invoke(app, ["litellm", "up"])
    assert " ".join(issue.split()) in " ".join(result.output.split())


def test_login_with_the_only_needed_provider_runs_the_xai_flow(mocker, context):
    mocker.patch("jailbee.litellm.login_providers", return_value=("xai",))
    login = mocker.patch("jailbee.litellm.litellm_login_xai", return_value=0)
    chatgpt = mocker.patch("jailbee.litellm.litellm_login")
    result = runner.invoke(app, ["litellm", "login"])
    assert result.exit_code == 0, result.output
    login.assert_called_once_with(
        context.return_value[0], context.return_value[1].litellm, "default"
    )
    chatgpt.assert_not_called()
    assert "litellm up" not in result.output and "Logged in" not in result.output


def test_login_without_any_needed_provider_keeps_the_chatgpt_flow(mocker, context):
    mocker.patch("jailbee.litellm.login_providers", return_value=())
    login = mocker.patch("jailbee.litellm.litellm_login", return_value=0)
    result = runner.invoke(app, ["litellm", "login"])
    assert result.exit_code == 0
    login.assert_called_once_with(context.return_value[0], "default")
    assert "jailbee litellm up" in result.output and "Logged in" not in result.output


def test_login_with_both_providers_needed_requires_a_flag(mocker, context):
    mocker.patch("jailbee.litellm.login_providers", return_value=("chatgpt", "xai"))
    login = mocker.patch("jailbee.litellm.litellm_login")
    xai = mocker.patch("jailbee.litellm.litellm_login_xai")
    result = runner.invoke(app, ["litellm", "login"])
    assert result.exit_code == 2
    assert "--provider" in result.output
    login.assert_not_called()
    xai.assert_not_called()


def test_an_unknown_provider_is_exit_2(mocker, context):
    mocker.patch("jailbee.litellm.login_providers", return_value=())
    result = runner.invoke(app, ["litellm", "login", "--provider", "grok"])
    assert result.exit_code == 2 and "chatgpt, xai" in result.output


def test_logout_passes_the_provider(mocker, context):
    mocker.patch("jailbee.litellm.login_providers", return_value=("chatgpt", "xai"))
    logout = mocker.patch("jailbee.litellm.litellm_logout", return_value=True)
    result = runner.invoke(app, ["litellm", "logout", "--provider", "xai"])
    assert result.exit_code == 0, result.output
    logout.assert_called_once_with(context.return_value[0], "default", "xai")


def test_up_names_accounts_started_without_their_xai_login(mocker, context):
    mocker.patch(
        "jailbee.litellm.litellm_up",
        return_value=ll.UpResult(
            ip="10.0.0.3",
            ports={"default": 4100},
            restarted=[],
            retired=[],
            installed=False,
            missing_xai_login=["default"],
        ),
    )
    result = runner.invoke(app, ["litellm", "up"])
    out = " ".join(result.output.split())
    assert result.exit_code == 0, result.output
    assert "without an xAI login: default" in out
    assert "jailbee litellm login default --provider xai" in out


def test_status_shows_the_xai_login_line_only_when_needed(mocker, context):
    def status(xai):
        return ll.LiteLLMStatus(
            ll.ContainerState.RUNNING,
            "10.0.0.3",
            "1.103.1",
            [ll.InstanceStatus("default", 4100, True, True, "present", xai_login=xai)],
        )

    mocker.patch("jailbee.litellm.litellm_status", return_value=status(None))
    assert "login (xai)" not in runner.invoke(app, ["litellm", "status"]).output
    mocker.patch("jailbee.litellm.litellm_status", return_value=status("missing"))
    out = runner.invoke(app, ["litellm", "status"]).output
    assert "login (xai): not logged in" in out and "--provider xai" in out
