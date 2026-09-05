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
        global_text=(
            "github:\n"
            "  enabled: true\n"
            "  api_tokens:\n"
            "    github.com: ghp_short\n"
            "    gitlab.com: ghp_much_longer_token\n"
        ),
    )
    # Exact order, not just membership: longest first is the contract that
    # makes `redact` mask a token-containing-a-token whole rather than
    # leaving a tail behind (see test_redact_masks_longest_first).
    assert secret_values(layers.global_raw, global_specs()) == (
        "ghp_much_longer_token",
        "ghp_short",
    )


def test_redact_masks_longest_first():
    # Reversing this list's order (shortest first) would mask "ghp_abc" out
    # of "ghp_abcdef" first, leaving a literal "def" tail behind in the
    # output — "ghp_abc" not in masked and a "*" count would still both
    # pass, which is exactly the defect this test must catch. Asserting the
    # exact string (and, redundantly, the absent leaked fragment) closes
    # that gap.
    masked = redact("ghp_abcdef and ghp_abc", ["ghp_abcdef", "ghp_abc"])
    assert masked == "******** and ********"
    assert "def" not in masked


def test_secret_values_feeds_redact_in_an_order_that_masks_containment_whole(tmp_path):
    """`secret_values`'s order and `redact`'s masking, wired together.

    Unlike the two tests above — which each fix one end of the contract in
    isolation — this drives real stored tokens through `secret_values` and
    into `redact`. If `secret_values`'s sort key regressed to shortest-first,
    `ghp_abc` would come before `ghp_abcdef`, `redact` would mask the short
    token out of the long one first, and a literal "def" tail would leak
    into the result — which the exact-string assertion below would catch.
    """
    layers = _layers(
        tmp_path,
        global_text=(
            "github:\n"
            "  enabled: true\n"
            "  api_tokens:\n"
            "    a.example: ghp_abc\n"
            "    b.example: ghp_abcdef\n"
        ),
    )
    secrets = secret_values(layers.global_raw, global_specs())
    masked = redact("ghp_abcdef and ghp_abc", secrets)
    assert masked == "******** and ********"


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


def test_dropped_comments_reports_a_duplicate_that_only_partly_survives():
    """A hand-written comment repeated twice, one copy surviving: multiset math.

    A set-based comparison ("is this text present anywhere in the new file?")
    would see "# note" reappear once and call *both* old occurrences kept,
    silently losing the fact that one of the two was actually dropped. The
    multiset comparison must report exactly one dropped occurrence.
    """
    from jailbee.config_edit.save import _dropped_comments

    old_text = "# note\ngpg:\n  enabled: false\n# note\nssh:\n  enabled: true\n"
    new_text = "# note\ngpg:\n  enabled: true\nssh:\n  enabled: true\n"
    assert _dropped_comments(old_text, new_text) == ("# note",)


def test_regenerate_dropping_one_of_two_identical_comments_still_confirms(tmp_path, monkeypatch):
    """Same scenario as above, exercised through `build_plan` end to end.

    `render_layer` is monkeypatched so the "regenerated" text is exactly
    controlled: one of the two duplicated hand comments survives, one does
    not. `must_confirm` must still trip, and the dropped tuple must name the
    lost copy.
    """
    import jailbee.config_edit.save as save_module

    old_text = "# note\ngpg:\n  enabled: false\n# note\nssh:\n  enabled: true\n"
    layers = _layers(tmp_path, global_text=old_text)
    monkeypatch.setattr(
        save_module, "render_layer", lambda raw, layer: "# note\ngpg:\n  enabled: true\n"
    )
    plan = build_plan(
        layers, "global", (YamlChange(("gpg", "enabled"), True),), global_specs(), "regenerate"
    )
    assert plan.must_confirm is True
    assert plan.dropped_comments == ("# note",)


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


def test_commit_writes_the_file_and_keeps_a_backup(tmp_path):
    from jailbee.config_edit.save import commit

    layers = _layers(tmp_path, repo_text="gpg:\n  enabled: false\n")
    plan = build_plan(
        layers, "repo", (YamlChange(("gpg", "enabled"), True),), repo_specs(), "patch"
    )
    backup = commit(plan)

    assert plan.path.read_text() == plan.new_text
    assert backup is not None
    assert backup.name == "config.yaml.bak"
    assert backup.read_text() == "gpg:\n  enabled: false\n"


def test_commit_of_a_new_file_leaves_no_backup(tmp_path):
    from jailbee.config_edit.save import commit

    layers = _layers(tmp_path)
    plan = build_plan(
        layers, "repo", (YamlChange(("container_prefix",), "demo"),), repo_specs(), "patch"
    )
    assert commit(plan) is None
    assert plan.path.exists()
    assert not (plan.path.parent / "config.yaml.bak").exists()


def test_commit_never_widens_the_mode_of_a_token_bearing_file(tmp_path):
    import stat

    from jailbee.config_edit.save import commit

    # 0o640, not 0o600: 0o600 is also write_text_atomic's own fallback for a
    # brand-new file with no mode= given, so it can't tell a correct
    # mode=mode call apart from a regression that dropped the argument —
    # both would produce a 0o600 backup. 0o640 only appears in the backup if
    # the original's mode was actually threaded through.
    layers = _layers(tmp_path, global_text="github:\n  enabled: false\n")
    layers.global_path.chmod(0o640)
    plan = build_plan(
        layers, "global", (YamlChange(("github", "enabled"), True),), global_specs(), "patch"
    )
    backup = commit(plan)

    assert stat.S_IMODE(plan.path.stat().st_mode) == 0o640
    assert backup is not None
    assert stat.S_IMODE(backup.stat().st_mode) == 0o640


def test_commit_creates_a_missing_config_directory(tmp_path):
    from jailbee.config_edit.save import commit

    repo = tmp_path / "fresh" / ".jailbee" / "config.yaml"
    glob = tmp_path / "global.yaml"
    layers = read_layers(repo, glob)
    plan = build_plan(
        layers, "repo", (YamlChange(("container_prefix",), "fresh"),), repo_specs(), "patch"
    )
    commit(plan)
    assert repo.read_text().startswith("container_prefix:")
