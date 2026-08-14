"""Tests for branch_config — diffing a branch's autostart against the host's."""

from __future__ import annotations

from jailbee.branch_config import (
    assess_escalation,
    diff_autostart,
    format_deviation,
    format_escalation,
    load_branch_autostart,
)
from jailbee.config import Autostart, AutostartStep


def _step(name: str, **kw: object) -> AutostartStep:
    kw.setdefault("run", "make")
    return AutostartStep(name=name, **kw)  # type: ignore[arg-type]  # kwargs are field values


def test_identical_autostart_has_no_deviation():
    a = Autostart(on_create=[_step("build")])
    b = Autostart(on_create=[_step("build")])

    dev = diff_autostart(a, b)

    assert dev.any_change is False
    assert dev.widens_network is False
    assert dev.attached_mounts == ()


def test_added_step_is_reported():
    host = Autostart(on_create=[_step("build")])
    branch = Autostart(on_create=[_step("build"), _step("seed")])

    dev = diff_autostart(host, branch)

    assert dev.added == ("on_create[seed]",)
    assert dev.removed == ()
    assert dev.any_change is True
    assert dev.widens_network is False


def test_removed_step_is_reported():
    host = Autostart(on_create=[_step("build"), _step("seed")])
    branch = Autostart(on_create=[_step("build")])

    dev = diff_autostart(host, branch)

    assert dev.removed == ("on_create[seed]",)
    assert dev.added == ()


def test_changed_run_names_the_field():
    host = Autostart(on_create=[_step("build", run="make")])
    branch = Autostart(on_create=[_step("build", run="make -j8")])

    dev = diff_autostart(host, branch)

    assert len(dev.changed) == 1
    assert dev.changed[0].name == "on_create[build]"
    assert dev.changed[0].fields == ("run",)
    assert dev.widens_network is False


def test_all_step_fields_are_compared_not_a_subset():
    """A newly added AutostartStep field must be covered automatically."""
    host = Autostart(on_create=[_step("build", working_dir="/a", timeout=10)])
    branch = Autostart(on_create=[_step("build", working_dir="/b", timeout=20)])

    dev = diff_autostart(host, branch)

    assert set(dev.changed[0].fields) == {"working_dir", "timeout"}


def test_block_level_changes_are_reported_with_values():
    host = Autostart(step_timeout=600, env={"A": "1"})
    branch = Autostart(step_timeout=900, env={"A": "2"})

    dev = diff_autostart(host, branch)

    by_name = {b.name: b.detail for b in dev.block_changes}
    assert by_name["step_timeout"] == "600 → 900"
    assert by_name["env"] == "A"
    assert dev.any_change is True


def test_env_block_change_lists_every_affected_key_sorted():
    host = Autostart(env={"KEEP": "x", "CHANGED": "1", "GONE": "y"})
    branch = Autostart(env={"KEEP": "x", "CHANGED": "2", "ADDED": "z"})

    dev = diff_autostart(host, branch)

    by_name = {b.name: b.detail for b in dev.block_changes}
    assert by_name["env"] == "ADDED, CHANGED, GONE"


def test_strict_to_loose_is_a_network_widening():
    host = Autostart(on_create=[_step("build", network="strict")])
    branch = Autostart(on_create=[_step("build", network="loose")])

    dev = diff_autostart(host, branch)

    assert dev.widening_steps == ("on_create[build]",)
    assert dev.widens_network is True


def test_unset_to_loose_is_a_network_widening():
    host = Autostart(on_create=[_step("build")])  # network is None
    branch = Autostart(on_create=[_step("build", network="loose")])

    assert diff_autostart(host, branch).widens_network is True


def test_new_loose_step_is_a_network_widening():
    host = Autostart(on_create=[])
    branch = Autostart(on_create=[_step("seed", network="loose")])

    assert diff_autostart(host, branch).widening_steps == ("on_create[seed]",)


def test_loose_in_both_is_not_a_widening():
    """Already-loose is not a widening — only the transition is."""
    host = Autostart(on_create=[_step("build", network="loose", run="a")])
    branch = Autostart(on_create=[_step("build", network="loose", run="b")])

    dev = diff_autostart(host, branch)

    assert dev.any_change is True
    assert dev.widens_network is False


def test_loose_to_strict_is_not_a_widening():
    host = Autostart(on_create=[_step("build", network="loose")])
    branch = Autostart(on_create=[_step("build", network="strict")])

    assert diff_autostart(host, branch).widens_network is False


def test_new_step_naming_a_mount_attaches_it():
    """A brand-new step has no host counterpart, so every mount it names is new."""
    host = Autostart(on_create=[])
    branch = Autostart(on_create=[_step("seed", mounts=["aws"])])

    dev = diff_autostart(host, branch)

    assert dev.attached_mounts == ("aws",)


def test_mount_added_to_an_existing_step_is_reported():
    host = Autostart(on_create=[_step("build", mounts=["m2"])])
    branch = Autostart(on_create=[_step("build", mounts=["m2", "aws"])])

    dev = diff_autostart(host, branch)

    assert dev.attached_mounts == ("aws",)  # only the added one, sorted
    assert dev.widens_network is False


def test_identical_mounts_are_not_reported():
    host = Autostart(on_create=[_step("build", mounts=["aws"], run="a")])
    branch = Autostart(on_create=[_step("build", mounts=["aws"], run="b")])

    dev = diff_autostart(host, branch)

    assert dev.any_change is True
    assert dev.attached_mounts == ()


def test_removing_a_mount_is_not_a_widening():
    """Dropping a host mount narrows the blast radius; nothing to confirm."""
    host = Autostart(on_create=[_step("build", mounts=["aws", "m2"])])
    branch = Autostart(on_create=[_step("build", mounts=["m2"])])

    dev = diff_autostart(host, branch)

    assert dev.attached_mounts == ()


def test_mounts_from_several_steps_are_collected_sorted():
    host = Autostart(on_create=[_step("build")])
    branch = Autostart(
        on_create=[_step("build", mounts=["m2"])],
        on_start=[_step("serve", mounts=["aws", "m2"])],
    )

    assert diff_autostart(host, branch).attached_mounts == ("aws", "m2")


def test_on_start_trigger_is_diffed_too():
    host = Autostart(on_start=[_step("serve")])
    branch = Autostart(on_start=[_step("serve", network="loose")])

    dev = diff_autostart(host, branch)

    assert dev.widening_steps == ("on_start[serve]",)


def test_same_step_name_in_different_triggers_is_not_conflated():
    """on_create[build] and on_start[build] are distinct steps."""
    host = Autostart(on_create=[_step("build", run="a")], on_start=[_step("build", run="b")])
    branch = Autostart(on_create=[_step("build", run="a")], on_start=[_step("build", run="c")])

    dev = diff_autostart(host, branch)

    assert len(dev.changed) == 1
    assert dev.changed[0].name == "on_start[build]"


def test_format_deviation_renders_source_and_markers():
    host = Autostart(on_create=[_step("build", run="make"), _step("gone")], step_timeout=600)
    branch = Autostart(
        on_create=[_step("build", run="make -j8", network="loose"), _step("new")],
        step_timeout=900,
    )

    text = format_deviation(diff_autostart(host, branch), source="refs/heads/f (abc1234)")

    assert "refs/heads/f (abc1234)" in text
    assert "+ on_create[new]" in text
    assert "- on_create[gone]" in text
    assert "~ on_create[build]" in text
    assert "network" in text
    assert "! step_timeout: 600 → 900" in text


def test_format_deviation_does_not_render_the_privilege_verdict():
    """The explanatory diff explains a surprise; it does not judge privileges.

    The ⚠ lines belong to `format_escalation`, which compares against the
    reviewed baseline instead of the checkout — printing them here too would
    let the two disagree in the one place a user reads them.
    """
    host = Autostart(on_create=[_step("build", network="strict")])
    branch = Autostart(on_create=[_step("build", network="loose", mounts=["aws"])])

    text = format_deviation(diff_autostart(host, branch), source="f")

    assert "⚠" not in text
    assert "~ on_create[build]" in text


def test_format_deviation_omits_empty_sections():
    """No stray blank markers when only one kind of change exists."""
    host = Autostart(on_create=[_step("build", run="a")])
    branch = Autostart(on_create=[_step("build", run="b")])

    text = format_deviation(diff_autostart(host, branch), source="f")

    assert "+ " not in text
    assert "! " not in text
    assert "~ on_create[build]: run changed" in text


def test_returns_none_when_branch_has_no_config(mocker, tmp_path):
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path)
    mocker.patch("jailbee.git.show_file_at_ref", return_value=None)
    warned = mocker.patch("jailbee.tui.warn_plain")

    assert load_branch_autostart(cfg, "refs/heads/f", source_label="refs/heads/f") is None
    warned.assert_not_called()  # a branch without its own config is not a problem


def test_warns_and_returns_none_on_invalid_branch_config(mocker, tmp_path):
    from jailbee.config import ConfigError
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path)
    mocker.patch("jailbee.git.show_file_at_ref", return_value="autostart: [bad")
    mocker.patch(
        "jailbee.config.load_config_from_text",
        side_effect=ConfigError("Invalid YAML in ..."),
    )
    warned = mocker.patch("jailbee.tui.warn_plain")

    assert load_branch_autostart(cfg, "refs/heads/f", source_label="refs/heads/f") is None
    assert warned.call_count == 1
    assert "Invalid YAML" in warned.call_args[0][0]


def test_grafts_branch_autostart_onto_host_config(mocker, tmp_path):
    from jailbee.config import Autostart, AutostartStep
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path)
    branch_autostart = Autostart(on_create=[AutostartStep(name="seed", run="./seed.sh")])
    branch_cfg = cfg.model_copy(update={"autostart": branch_autostart})

    mocker.patch("jailbee.git.show_file_at_ref", return_value="autostart: {}")
    mocker.patch("jailbee.config.load_config_from_text", return_value=branch_cfg)

    result = load_branch_autostart(cfg, "refs/heads/f", source_label="refs/heads/f (abc1234)")

    assert result is not None
    assert [s.name for s in result.cfg.autostart.on_create] == ["seed"]
    # Everything outside autostart still comes from the host config.
    assert result.cfg.repo_root == cfg.repo_root
    assert result.cfg.container_prefix == cfg.container_prefix
    assert result.cfg.defaults.network == cfg.defaults.network
    assert result.source == "refs/heads/f (abc1234)"
    assert result.deviation.added == ("on_create[seed]",)


def test_load_branch_autostart_falls_back_to_legacy_gie_dir(mocker, tmp_path):
    """A branch that only carries the legacy `.gie/config.yaml` must still be
    read — `.jailbee/config.yaml` is tried first and comes back empty."""
    from jailbee.config import Autostart, AutostartStep
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path)
    branch_autostart = Autostart(on_create=[AutostartStep(name="seed", run="./seed.sh")])
    branch_cfg = cfg.model_copy(update={"autostart": branch_autostart})

    def fake_show(repo_root, ref, rel):
        return None if rel == ".jailbee/config.yaml" else "autostart: {}"

    show = mocker.patch("jailbee.git.show_file_at_ref", side_effect=fake_show)
    mocker.patch("jailbee.config.load_config_from_text", return_value=branch_cfg)

    result = load_branch_autostart(cfg, "refs/heads/f", source_label="refs/heads/f")

    assert result is not None
    assert [s.name for s in result.cfg.autostart.on_create] == ["seed"]
    assert [c.args[2] for c in show.call_args_list] == [
        ".jailbee/config.yaml",
        ".gie/config.yaml",
    ]


def test_host_config_is_not_mutated(mocker, tmp_path):
    from jailbee.config import Autostart, AutostartStep
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path)
    before = cfg.autostart.model_copy(deep=True)
    branch_cfg = cfg.model_copy(
        update={"autostart": Autostart(on_create=[AutostartStep(name="x", run="y")])}
    )
    mocker.patch("jailbee.git.show_file_at_ref", return_value="autostart: {}")
    mocker.patch("jailbee.config.load_config_from_text", return_value=branch_cfg)

    load_branch_autostart(cfg, "refs/heads/f", source_label="f")

    assert cfg.autostart == before


def test_graft_introducing_validation_issues_is_rejected(mocker, tmp_path):
    """A branch step naming an optional_mount the host doesn't define."""
    from jailbee.config import Autostart, AutostartStep
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path)
    branch_cfg = cfg.model_copy(
        update={
            "autostart": Autostart(
                on_create=[AutostartStep(name="build", run="make", mounts=["nope"])]
            )
        }
    )
    mocker.patch("jailbee.git.show_file_at_ref", return_value="autostart: {}")
    mocker.patch("jailbee.config.load_config_from_text", return_value=branch_cfg)
    warned = mocker.patch("jailbee.tui.warn_plain")

    assert load_branch_autostart(cfg, "refs/heads/f", source_label="f") is None
    assert warned.call_count == 1
    assert "nope" in warned.call_args[0][0]


def test_preexisting_host_validation_issues_do_not_block_the_graft(mocker, tmp_path):
    """Only issues the graft *introduces* matter — set difference, not truthiness."""
    from jailbee.config import Autostart, AutostartStep
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path)
    # Give the host config a pre-existing issue by pointing a host mount at a
    # path that does not exist.
    cfg = cfg.model_copy(update={"repo_root": tmp_path / "gone"})
    assert cfg.validate_runtime()  # sanity: the host config already has issues

    branch_cfg = cfg.model_copy(
        update={"autostart": Autostart(on_create=[AutostartStep(name="seed", run="./s")])}
    )
    mocker.patch("jailbee.git.show_file_at_ref", return_value="autostart: {}")
    mocker.patch("jailbee.config.load_config_from_text", return_value=branch_cfg)

    result = load_branch_autostart(cfg, "refs/heads/f", source_label="f")

    assert result is not None  # not blocked by the host's own pre-existing issues


# --- the escalation gate ---------------------------------------------------
#
# The gate answers a different question than the deviation above: not "does
# this differ from what I have checked out" but "does this grant privileges the
# repo's reviewed baseline does not already grant". Baseline =
# `refs/remotes/origin/<default_branch>`.


def _baseline(mocker, cfg, autostart: Autostart | None):
    """Make the origin/<default_branch> config read return `autostart`.

    `None` makes the baseline unavailable (no committed config at that ref).
    """
    if autostart is None:
        return mocker.patch("jailbee.git.show_file_at_ref", return_value=None)

    show = mocker.patch("jailbee.git.show_file_at_ref", return_value="autostart: {}")
    mocker.patch(
        "jailbee.config.load_config_from_text",
        return_value=cfg.model_copy(update={"autostart": autostart}),
    )
    return show


def test_baseline_is_the_default_branch_not_the_checkout(mocker, tmp_path):
    """The regression: a checkout older than origin/<default> is not an escalation.

    The host checkout has no loose step, the baseline already has it, and the
    branch merely inherits the baseline's. Nothing is being granted.
    """
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path)  # checkout: no loose steps at all
    loose = Autostart(on_start=[_step("warmup", network="loose")])
    _baseline(mocker, cfg, loose)

    verdict = assess_escalation(cfg, loose, untrusted=False)

    assert verdict.any_widening is False
    assert verdict.prompts is False


def test_network_widening_from_your_own_repo_warns_without_prompting(mocker, tmp_path):
    """`loose` is not a confidentiality boundary once the branch's code runs."""
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path)
    _baseline(mocker, cfg, Autostart(on_start=[_step("warmup")]))

    verdict = assess_escalation(
        cfg, Autostart(on_start=[_step("warmup", network="loose")]), untrusted=False
    )

    assert verdict.widening_steps == ("on_start[warmup]",)
    assert verdict.any_widening is True
    assert verdict.prompts is False


def test_network_widening_from_an_untrusted_head_prompts(mocker, tmp_path):
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path)
    _baseline(mocker, cfg, Autostart(on_start=[_step("warmup")]))

    verdict = assess_escalation(
        cfg, Autostart(on_start=[_step("warmup", network="loose")]), untrusted=True
    )

    assert verdict.prompts is True


def test_attached_host_mount_prompts_even_from_your_own_repo(mocker, tmp_path):
    """A mount creates the asset: strict/loose is moot if ~/.aws isn't there."""
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path)
    _baseline(mocker, cfg, Autostart(on_create=[_step("build")]))

    verdict = assess_escalation(
        cfg, Autostart(on_create=[_step("build", mounts=["aws"])]), untrusted=False
    )

    assert verdict.attached_mounts == ("aws",)
    assert verdict.prompts is True


def test_mount_already_in_the_baseline_does_not_prompt(mocker, tmp_path):
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path)
    _baseline(mocker, cfg, Autostart(on_create=[_step("build", mounts=["aws"])]))

    verdict = assess_escalation(
        cfg, Autostart(on_create=[_step("build", mounts=["aws"], run="other")]), untrusted=False
    )

    assert verdict.attached_mounts == ()
    assert verdict.prompts is False


def test_a_branch_that_cannot_widen_reads_no_git(mocker, tmp_path):
    """No loose step and no step mounts — nothing to compare, so don't look."""
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path)
    show = mocker.patch("jailbee.git.show_file_at_ref")

    verdict = assess_escalation(cfg, Autostart(on_create=[_step("build")]), untrusted=True)

    show.assert_not_called()
    assert verdict.any_widening is False
    assert verdict.prompts is False


def test_missing_baseline_falls_back_to_the_checkout(mocker, tmp_path):
    """No origin/<default> config: compare against the checkout rather than
    silently granting everything."""
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path)
    _baseline(mocker, cfg, None)

    verdict = assess_escalation(
        cfg, Autostart(on_create=[_step("build", mounts=["aws"])]), untrusted=False
    )

    assert verdict.attached_mounts == ("aws",)
    assert verdict.prompts is True
    assert "checkout" in verdict.baseline_source


def test_invalid_baseline_warns_and_falls_back(mocker, tmp_path):
    from jailbee.config import ConfigError
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path)
    mocker.patch("jailbee.git.show_file_at_ref", return_value="autostart: [bad")
    mocker.patch(
        "jailbee.config.load_config_from_text",
        side_effect=ConfigError("Invalid YAML in ..."),
    )
    warned = mocker.patch("jailbee.tui.warn_plain")

    verdict = assess_escalation(
        cfg, Autostart(on_create=[_step("build", mounts=["aws"])]), untrusted=False
    )

    assert warned.call_count == 1
    assert verdict.prompts is True
    assert "checkout" in verdict.baseline_source


def test_baseline_is_read_at_the_origin_default_branch_ref(mocker, tmp_path):
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path, default_branch="dev")
    show = _baseline(mocker, cfg, Autostart())

    assess_escalation(cfg, Autostart(on_create=[_step("build", network="loose")]), untrusted=False)

    assert show.call_args.args[1] == "refs/remotes/origin/dev"
    assert show.call_args.args[2] == ".jailbee/config.yaml"


def test_format_escalation_names_the_baseline_and_the_reasons(mocker, tmp_path):
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path, default_branch="dev")
    _baseline(mocker, cfg, Autostart())

    verdict = assess_escalation(
        cfg,
        Autostart(on_start=[_step("warmup", network="loose", mounts=["aws", "m2"])]),
        untrusted=True,
    )
    text = format_escalation(verdict)

    assert "refs/remotes/origin/dev" in text
    assert "on_start[warmup]" in text
    assert "aws, m2" in text
    assert "⚠" in text


def test_format_escalation_is_empty_without_a_widening(mocker, tmp_path):
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path)
    _baseline(mocker, cfg, Autostart(on_create=[_step("build")]))

    verdict = assess_escalation(cfg, Autostart(on_create=[_step("build")]), untrusted=True)

    assert format_escalation(verdict) == ""


def test_baseline_is_read_at_the_configured_upstream_remote(mocker, tmp_path):
    """The reviewed baseline lives on whatever the repo calls its upstream."""
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path, default_branch="dev", upstream_remote="public")
    show = _baseline(mocker, cfg, Autostart())

    assess_escalation(cfg, Autostart(on_create=[_step("build", network="loose")]), untrusted=False)

    assert show.call_args.args[1] == "refs/remotes/public/dev"


def test_unreachable_baseline_ref_warns_instead_of_degrading_silently(mocker, tmp_path):
    """The gate getting weaker must be said out loud.

    When the baseline ref does not resolve at all — no such remote, or a default
    branch never fetched — the privilege baseline silently becomes the caller's
    own checkout, which is exactly the config a branch could have written for
    itself. A repo that simply carries no `.jailbee/config.yaml` on its default
    branch is a different, benign case and stays quiet (covered separately).
    """
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path, default_branch="dev", upstream_remote="public")
    mocker.patch("jailbee.git.show_file_at_ref", return_value=None)
    mocker.patch("jailbee.git.remote_ref_exists", return_value=False)
    warned = mocker.patch("jailbee.tui.warn_plain")

    verdict = assess_escalation(
        cfg, Autostart(on_create=[_step("build", mounts=["aws"])]), untrusted=False
    )

    assert warned.call_count == 1
    assert "refs/remotes/public/dev" in warned.call_args.args[0]
    assert "checkout" in verdict.baseline_source
    assert "cannot be read" in verdict.baseline_source


def test_absent_baseline_config_on_a_reachable_ref_stays_quiet(mocker, tmp_path):
    """A repo that just doesn't keep a config on its default branch is benign."""
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path)
    mocker.patch("jailbee.git.show_file_at_ref", return_value=None)
    mocker.patch("jailbee.git.remote_ref_exists", return_value=True)
    warned = mocker.patch("jailbee.tui.warn_plain")

    verdict = assess_escalation(
        cfg, Autostart(on_create=[_step("build", mounts=["aws"])]), untrusted=False
    )

    warned.assert_not_called()
    assert "checkout" in verdict.baseline_source
