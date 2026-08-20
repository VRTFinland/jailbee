from jailbee.agents import enabled_agent_specs
from tests.conftest import make_cfg, with_agent


def test_disabled_agents_are_excluded(tmp_path):
    cfg = make_cfg(tmp_path, agents={"codex": {"enabled": False}})
    assert enabled_agent_specs(cfg) == []


def test_spec_carries_mounts_egress_and_install(tmp_path):
    cfg = make_cfg(tmp_path, agents={"codex": {"enabled": True}})
    (spec,) = enabled_agent_specs(cfg)
    assert spec.name == "codex"
    assert [c.name for c in spec.shared] == ["codex"]
    assert [c.container_path for c in spec.shared] == ["~/.codex"]
    assert spec.egress == ("api.openai.com:443",)
    assert spec.install == "npm i -g @openai/codex"
    assert spec.install_check == "command -v codex"
    assert spec.install_network == "strict"


def test_file_mount_becomes_a_seed_file_not_a_dir(tmp_path):
    cfg = make_cfg(tmp_path, agents={"aider": {"enabled": True}})
    (spec,) = enabled_agent_specs(cfg)
    assert spec.dir_subpaths == ()
    assert spec.seed_files == (("aider.conf.yml", ""),)


def test_claude_sorts_last(tmp_path):
    cfg = make_cfg(
        tmp_path,
        agents={
            "claude": {"enabled": True},
            "codex": {"enabled": True},
            "aider": {"enabled": True},
        },
    )
    assert [s.name for s in enabled_agent_specs(cfg)] == ["aider", "codex", "claude"]


def test_plugin_hosts_only_when_claude_plugins_enabled(tmp_path):
    from jailbee.constants import CLAUDE_PLUGIN_HOSTS

    on = make_cfg(tmp_path, agents={"claude": {"enabled": True}})
    (spec,) = enabled_agent_specs(on)
    assert CLAUDE_PLUGIN_HOSTS[0] in spec.egress

    off = with_agent(on, "claude", plugins_enabled=False)
    (spec_off,) = enabled_agent_specs(off)
    assert CLAUDE_PLUGIN_HOSTS[0] not in spec_off.egress


def test_grok_install_runs_loose(tmp_path):
    cfg = make_cfg(tmp_path, agents={"grok": {"enabled": True}})
    (spec,) = enabled_agent_specs(cfg)
    assert spec.install_network == "loose"


def test_spec_autostart_reflects_config(tmp_path):
    """Differential in both directions: catches a hardcoded True or False."""
    autostart_on = make_cfg(tmp_path, agents={"codex": {"enabled": True, "autostart": True}})
    (spec_on,) = enabled_agent_specs(autostart_on)
    assert spec_on.autostart is True

    autostart_off = make_cfg(tmp_path, agents={"codex": {"enabled": True, "autostart": False}})
    (spec_off,) = enabled_agent_specs(autostart_off)
    assert spec_off.autostart is False


def test_env_is_an_immutable_tuple_of_pairs(tmp_path):
    """Pins `env`'s shape against a regression back to a mutable dict.

    `AgentSpec` is `frozen=True`; a dict-typed `env` would silently defeat
    that guarantee via `spec.env["k"] = "v"`. A tuple has no `__setitem__`,
    so that specific escape hatch is closed — assert both the exact value
    and the absence of in-place mutation.
    """
    cfg = make_cfg(tmp_path, agents={"codex": {"enabled": True, "env": {"FOO": "bar"}}})
    (spec,) = enabled_agent_specs(cfg)
    assert spec.env == (("FOO", "bar"),)
    assert not hasattr(spec.env, "__setitem__")


# --- ensure_agents (Task 5): install/update dispatch -----------------------
#
# `shared_dir=tmp_path / "shared"` is passed explicitly on every `make_cfg`
# call below: `ensure_agents` takes a real host-side flock on
# `<shared_dir>/.agent-install.lock`, and the project's test-isolation rule
# (see CLAUDE.md) requires filesystem ops to stay under `tmp_path` rather
# than touching the real `~/.local/share/jailbee/shared/...`.


def test_install_runs_when_check_fails(tmp_path, mocker):
    from jailbee.agents import ensure_agents

    cfg = make_cfg(tmp_path, agents={"codex": {"enabled": True}}, shared_dir=tmp_path / "shared")
    incus = mocker.MagicMock()
    incus.exec.side_effect = Exception("not found")  # install_check fails
    apply_step = mocker.patch("jailbee.autostart._apply_step")

    ensure_agents(cfg, incus, "c1", "/home/dev/repo")

    (step,) = [c.args[3] for c in apply_step.call_args_list]
    assert step.run == "npm i -g @openai/codex"
    assert step.network is None  # codex installs under strict


def test_update_runs_when_check_succeeds_and_auto_update(tmp_path, mocker):
    from jailbee.agents import ensure_agents

    cfg = make_cfg(tmp_path, agents={"codex": {"enabled": True}}, shared_dir=tmp_path / "shared")
    incus = mocker.MagicMock()  # install_check succeeds
    apply_step = mocker.patch("jailbee.autostart._apply_step")

    ensure_agents(cfg, incus, "c1", "/home/dev/repo")

    (step,) = [c.args[3] for c in apply_step.call_args_list]
    assert step.run == "npm i -g @openai/codex@latest"


def test_no_update_when_auto_update_off(tmp_path, mocker):
    from jailbee.agents import ensure_agents
    from tests.conftest import with_agent

    cfg = with_agent(
        make_cfg(tmp_path, agents={"codex": {"enabled": True}}, shared_dir=tmp_path / "shared"),
        "codex",
        auto_update=False,
    )
    apply_step = mocker.patch("jailbee.autostart._apply_step")
    ensure_agents(cfg, mocker.MagicMock(), "c1", "/home/dev/repo")
    apply_step.assert_not_called()


def test_grok_install_step_swaps_to_loose(tmp_path, mocker):
    from jailbee.agents import ensure_agents

    cfg = make_cfg(tmp_path, agents={"grok": {"enabled": True}}, shared_dir=tmp_path / "shared")
    incus = mocker.MagicMock()
    incus.exec.side_effect = Exception("not found")
    apply_step = mocker.patch("jailbee.autostart._apply_step")

    ensure_agents(cfg, incus, "c1", "/home/dev/repo")

    (step,) = [c.args[3] for c in apply_step.call_args_list]
    assert step.network == "loose"


def test_install_runs_even_when_auto_update_off(tmp_path, mocker):
    """`auto_update=False` only gates the update path — a fresh install must
    still happen. Otherwise disabling auto-update would silently disable
    the agent entirely on a brand-new container."""
    from jailbee.agents import ensure_agents
    from tests.conftest import with_agent

    cfg = with_agent(
        make_cfg(tmp_path, agents={"codex": {"enabled": True}}, shared_dir=tmp_path / "shared"),
        "codex",
        auto_update=False,
    )
    incus = mocker.MagicMock()
    incus.exec.side_effect = Exception("not found")  # install_check fails: not installed
    apply_step = mocker.patch("jailbee.autostart._apply_step")

    ensure_agents(cfg, incus, "c1", "/home/dev/repo")

    (step,) = [c.args[3] for c in apply_step.call_args_list]
    assert step.run == "npm i -g @openai/codex"


def test_claude_step_resolves_bundled_script_and_auto_update_flag(tmp_path, mocker):
    """`__bundled__:ensure-claude.sh` resolves to the script's real text, and
    `JAILBEE_CLAUDE_AUTO_UPDATE` is threaded from `cfg.claude.auto_update` —
    the script's own "store already populated" branch keys `claude update`
    off that exact env var, independent of the install-vs-update dispatch.
    """
    from jailbee.agents import ensure_agents

    cfg = make_cfg(tmp_path, agents={"claude": {"enabled": True}}, shared_dir=tmp_path / "shared")
    incus = mocker.MagicMock()  # install_check succeeds -> installed=True -> "update" path
    apply_step = mocker.patch("jailbee.autostart._apply_step")

    ensure_agents(cfg, incus, "c1", "/home/dev/repo")

    (step,) = [c.args[3] for c in apply_step.call_args_list]
    assert "ensure-claude" in step.run  # the sentinel resolved to real script text
    assert step.env["JAILBEE_CLAUDE_AUTO_UPDATE"] == "true"  # auto_update defaults to True


def test_claude_auto_update_flag_reflects_config_off(tmp_path, mocker):
    """Not-yet-installed-in-this-container + auto_update=False: the script
    still runs (a fresh container always needs the symlink), but the env
    flag it reads must say "false" so its own update branch stays off."""
    from jailbee.agents import ensure_agents
    from tests.conftest import with_agent

    cfg = with_agent(
        make_cfg(tmp_path, agents={"claude": {"enabled": True}}, shared_dir=tmp_path / "shared"),
        "claude",
        auto_update=False,
    )
    incus = mocker.MagicMock()
    incus.exec.side_effect = Exception("not found")  # install_check fails: not installed
    apply_step = mocker.patch("jailbee.autostart._apply_step")

    ensure_agents(cfg, incus, "c1", "/home/dev/repo")

    (step,) = [c.args[3] for c in apply_step.call_args_list]
    assert step.env["JAILBEE_CLAUDE_AUTO_UPDATE"] == "false"


def test_step_failure_is_warned_not_raised(tmp_path, mocker):
    from jailbee.agents import ensure_agents

    cfg = make_cfg(tmp_path, agents={"codex": {"enabled": True}}, shared_dir=tmp_path / "shared")
    incus = mocker.MagicMock()
    incus.exec.side_effect = Exception("not found")
    mocker.patch("jailbee.autostart._apply_step", side_effect=Exception("boom"))
    warn = mocker.patch("jailbee.agents.warn")

    ensure_agents(cfg, incus, "c1", "/home/dev/repo")  # must not raise

    assert warn.called
