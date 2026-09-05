"""The save path: which policy, what would change, what would be lost.

Everything here is a string transformation over `tmp_path` files, so the whole
policy and diff story is testable without a terminal.
"""

from __future__ import annotations

from jailbee.config_edit.layers import read_layers
from jailbee.config_edit.save import (
    SavePlan,
    build_plan,
    configured_policy,
    redact,
    render_layer,
    resolve_policy,
    secret_values,
)
from jailbee.config_edit.schema import global_specs, repo_specs
from jailbee.config_writer import DELETE, YamlChange


def _layers(tmp_path, repo_text="", global_text=""):
    repo = tmp_path / "repo" / ".jailbee" / "config.yaml"
    glob = tmp_path / "global.yaml"
    repo.parent.mkdir(parents=True, exist_ok=True)
    if repo_text:
        repo.write_text(repo_text)
    if global_text:
        glob.write_text(global_text)
    return read_layers(repo, glob)


def test_auto_picks_the_documented_default_per_layer():
    """global.yaml is jailbee's to own; a repo config is read as a PR diff."""
    assert resolve_policy("global", configured="auto") == "regenerate"
    assert resolve_policy("repo", configured="auto") == "patch"


def test_the_flag_beats_the_config_key():
    assert resolve_policy("global", flag="patch", configured="regenerate") == "patch"
    assert resolve_policy("repo", flag="regenerate", configured="auto") == "regenerate"


def test_the_config_key_beats_the_per_layer_default():
    assert resolve_policy("repo", configured="regenerate") == "regenerate"


def test_configured_policy_falls_back_to_auto_on_an_unreadable_global(tmp_path):
    """A repo-layer edit must not be blocked by a file it is not touching."""
    broken = tmp_path / "global.yaml"
    broken.write_text("docker_registry_mirror: [not, a, mapping]\n")
    assert configured_policy(broken) == "auto"
    assert configured_policy(tmp_path / "missing.yaml") == "auto"

    ok = tmp_path / "ok.yaml"
    ok.write_text("config_edit:\n  write_policy: patch\n")
    assert configured_policy(ok) == "patch"


def test_secret_values_finds_every_stored_token(tmp_path):
    layers = _layers(
        tmp_path,
        global_text="github:\n  enabled: true\n  api_tokens:\n    github.com: ghp_secret\n",
    )
    assert secret_values(layers.global_raw, global_specs()) == ("ghp_secret",)


def test_redact_masks_longest_first():
    masked = redact("ghp_abcdef and ghp_abc", ["ghp_abcdef", "ghp_abc"])
    assert "ghp_abc" not in masked
    assert masked.count("*") >= 8


def test_a_token_never_reaches_the_diff(tmp_path):
    layers = _layers(
        tmp_path,
        global_text="github:\n  enabled: false\n  api_tokens:\n    github.com: ghp_secret\n",
    )
    plan = build_plan(
        layers, "global", (YamlChange(("github", "enabled"), True),), global_specs(), "patch"
    )
    assert "ghp_secret" not in plan.diff
    assert "enabled" in plan.diff


def test_patch_keeps_comments_and_touches_one_key(tmp_path):
    text = "# keep me\ngpg:\n  enabled: false\nssh:\n  enabled: true\n"
    layers = _layers(tmp_path, repo_text=text)
    plan = build_plan(
        layers, "repo", (YamlChange(("gpg", "enabled"), True),), repo_specs(), "patch"
    )
    assert "# keep me" in plan.new_text
    assert "enabled: true" in plan.new_text
    assert plan.dropped_comments == ()
    assert plan.must_confirm is False


def test_regenerate_over_a_hand_commented_file_demands_confirmation(tmp_path):
    text = "# my own note\ngpg:\n  enabled: false\n"
    layers = _layers(tmp_path, global_text=text)
    plan = build_plan(
        layers, "global", (YamlChange(("gpg", "enabled"), True),), global_specs(), "regenerate"
    )
    assert plan.must_confirm is True
    assert "# my own note" in plan.dropped_comments
    assert "# my own note" not in plan.new_text


def test_regenerate_over_a_generated_file_needs_no_confirmation(tmp_path):
    """jailbee's own comments come back identical, so nothing is lost."""
    from jailbee.config_writer import render_global_yaml

    text = render_global_yaml({"gpg": {"enabled": False}})
    layers = _layers(tmp_path, global_text=text)
    plan = build_plan(
        layers, "global", (YamlChange(("gpg", "enabled"), True),), global_specs(), "regenerate"
    )
    assert plan.dropped_comments == ()
    assert plan.must_confirm is False


def test_a_delete_removes_the_key_under_both_policies(tmp_path):
    text = "gpg:\n  enabled: true\n"
    layers = _layers(tmp_path, repo_text=text)
    for policy in ("patch", "regenerate"):
        plan = build_plan(
            layers, "repo", (YamlChange(("gpg", "enabled"), DELETE),), repo_specs(), policy
        )
        assert "enabled" not in plan.new_text


def test_a_plan_over_a_missing_file_starts_from_nothing(tmp_path):
    layers = _layers(tmp_path)
    plan = build_plan(
        layers, "repo", (YamlChange(("container_prefix",), "demo"),), repo_specs(), "patch"
    )
    assert plan.old_text == ""
    assert "container_prefix: demo" in plan.new_text
    assert isinstance(plan, SavePlan)


def test_render_layer_uses_the_two_pass_renderer_for_global(tmp_path):
    text = render_layer({"gpg": {"enabled": True}, "scratch": {"enabled": True}}, "global")
    assert text.index("gpg:") < text.index("scratch:")
