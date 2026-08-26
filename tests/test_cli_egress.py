"""Tests for the `jailbee net egress` command group."""

from __future__ import annotations

import json

import yaml
from typer.testing import CliRunner

from jailbee.cli import app

runner = CliRunner()


def _repo(tmp_path, mocker, *, egress_allow=None, extras=None):
    from tests.conftest import make_config

    repo_root = tmp_path / "myrepo"
    repo_root.mkdir()
    cfg_dir = repo_root / ".jailbee"
    cfg_dir.mkdir()
    (cfg_dir / "config.yaml").write_text(
        yaml.safe_dump({"egress_allow": list(egress_allow or [])})
    )
    cfg = make_config(repo_root, egress_allow=list(egress_allow or []))

    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.cli._resolve_config_path", return_value=cfg_dir / "config.yaml")
    incus = mocker.MagicMock()
    incus.config_get.return_value = None
    mocker.patch("jailbee.incus.Incus", return_value=incus)
    mocker.patch("jailbee.cli._resolve_existing", return_value=(incus, "myrepo-feat"))
    mocker.patch("jailbee.egress_scope.container_extras", return_value=list(extras or []))
    mocker.patch("jailbee.egress_scope.apply_container_acl")
    mocker.patch("jailbee.cli._repin_hosts_quietly")
    return cfg, incus


# --- add ---------------------------------------------------------------


def test_add_rejects_a_malformed_entry(tmp_path, mocker):
    _repo(tmp_path, mocker)
    result = runner.invoke(
        app, ["net", "egress", "add", "nexus.corp:notaport"], env={"COLUMNS": "250"}
    )
    assert result.exit_code == 2
    assert "not an integer" in result.output


def test_add_writes_nothing_when_the_host_does_not_resolve(tmp_path, mocker):
    from jailbee.egress import NetworkResolveError

    _repo(tmp_path, mocker)
    mocker.patch(
        "jailbee.egress_scope.resolve_entries",
        side_effect=NetworkResolveError("nexus.corp"),
    )
    setc = mocker.patch("jailbee.egress_scope.set_container_extras")

    result = runner.invoke(app, ["net", "egress", "add", "nexus.corp:443"])

    assert result.exit_code == 1
    setc.assert_not_called()


def test_add_stores_a_container_entry_by_default(tmp_path, mocker):
    _repo(tmp_path, mocker)
    mocker.patch("jailbee.egress_scope.resolve_entries", return_value=[])
    setc = mocker.patch("jailbee.egress_scope.set_container_extras")

    result = runner.invoke(app, ["net", "egress", "add", "nexus.corp:443"])

    assert result.exit_code == 0
    setc.assert_called_once()
    assert setc.call_args[0][2] == ["nexus.corp:443"]


def test_add_repo_stores_a_repo_entry(tmp_path, mocker):
    _repo(tmp_path, mocker)
    mocker.patch("jailbee.egress_scope.resolve_entries", return_value=[])
    add = mocker.patch("jailbee.egress_scope.add_repo_extra", return_value=True)

    result = runner.invoke(app, ["net", "egress", "add", "--repo", "nexus.corp:443"])

    assert result.exit_code == 0
    assert add.call_args[0][2] == "nexus.corp:443"


def test_add_of_a_config_entry_is_a_no_op(tmp_path, mocker):
    _repo(tmp_path, mocker, egress_allow=["github.com"])
    mocker.patch("jailbee.egress_scope.resolve_entries", return_value=[])
    setc = mocker.patch("jailbee.egress_scope.set_container_extras")

    result = runner.invoke(app, ["net", "egress", "add", "github.com"])

    assert result.exit_code == 0
    assert "already" in result.output.lower()
    setc.assert_not_called()


def test_add_to_a_loose_container_stores_the_label_without_a_local_eth0(tmp_path, mocker):
    """Correction 2: `apply_container_acl` must materialise under the
    container's CURRENT mode, not a hardcoded "strict" — else adding an
    override to a loose container would silently create a container-local
    `eth0` device (which shadows the assigned network profile), pinning it
    back to `incusbr0` with the strict allowlist enforced while `jailbee ls`
    still reports it loose. `apply_container_acl` is NOT mocked here (unlike
    `_repo()`'s default setup) so its own tear-down branch is what's under
    test.

    Fix round 1 (C1): the container's *existing* override label is non-empty
    (`nexus.corp:443`) and a *different* entry is being added
    (`other.corp:443`). With an EMPTY label, `apply_container_acl`'s own
    `if mode != "strict" or not extras:` guard takes the teardown branch
    regardless of `mode` — so the original version of this test (empty
    label) passed under the exact hardcoded-`mode="strict"` mutation it
    exists to catch. A non-empty label makes the branch depend solely on
    `mode`. `config_device_remove` being called (not just
    `config_device_override`/`config_device_set` NOT being called) proves
    the teardown branch actually ran, rather than nothing happening at all.

    Verified by temporarily hardcoding `mode="strict"` in both call sites in
    `cli.py` and confirming this test fails (see fix-round-1 report)."""
    from tests.conftest import make_config

    repo_root = tmp_path / "myrepo"
    repo_root.mkdir()
    cfg_dir = repo_root / ".jailbee"
    cfg_dir.mkdir()
    (cfg_dir / "config.yaml").write_text(yaml.safe_dump({"egress_allow": []}))
    cfg = make_config(repo_root, egress_allow=[])

    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.cli._resolve_config_path", return_value=cfg_dir / "config.yaml")
    incus = mocker.MagicMock()
    incus.config_get.return_value = json.dumps(["nexus.corp:443"])
    mocker.patch("jailbee.incus.Incus", return_value=incus)
    mocker.patch("jailbee.cli._resolve_existing", return_value=(incus, "myrepo-feat"))
    mocker.patch("jailbee.cli._repin_hosts_quietly")
    mocker.patch("jailbee.egress_scope.resolve_entries", return_value=[])
    mocker.patch("jailbee.lifecycle.current_network_mode", return_value="loose")
    setc = mocker.patch("jailbee.egress_scope.set_container_extras")

    result = runner.invoke(app, ["net", "egress", "add", "other.corp:443"])

    assert result.exit_code == 0, result.output
    setc.assert_called_once()
    incus.config_device_remove.assert_called_once()
    incus.config_device_override.assert_not_called()
    incus.config_device_set.assert_not_called()


# --- rm ------------------------------------------------------------------


def test_rm_of_a_config_entry_points_at_the_config_file(tmp_path, mocker):
    """A config-only entry — never stored as an override anywhere — still
    refuses and points the user at config.yaml."""
    _repo(tmp_path, mocker, egress_allow=["github.com"])

    result = runner.invoke(
        app, ["net", "egress", "rm", "github.com"], env={"COLUMNS": "250"}
    )

    assert result.exit_code == 1
    assert "config.yaml" in result.output


def test_rm_of_a_promoted_container_override_succeeds(tmp_path, mocker):
    """Fix round 1 (I2): an entry that is BOTH in config.yaml AND stored as
    a container override (e.g. after `jailbee net egress export` was
    promoted into config.yaml, or simply re-added on purpose) must remove
    normally — the override row is what's deleted, not the config line, so
    additive-only still holds. Before this fix, `rm` refused ANY entry
    present in config regardless of override status, making a promoted
    override permanently undeletable and stuck showing "redundant" in `ls`
    forever, and making `export`'s own printed advice ("drop the
    now-redundant overrides with `jailbee net egress rm`") unfollowable."""
    cfg, incus = _repo(tmp_path, mocker, egress_allow=["github.com"], extras=["github.com"])
    setc = mocker.patch("jailbee.egress_scope.set_container_extras")

    result = runner.invoke(app, ["net", "egress", "rm", "github.com"])

    assert result.exit_code == 0, result.output
    setc.assert_called_once_with(incus, "myrepo-feat", [])


def test_rm_of_a_promoted_repo_override_succeeds(tmp_path, mocker):
    """Same as above, for a --repo scope override."""
    _repo(tmp_path, mocker, egress_allow=["github.com"])
    remove = mocker.patch("jailbee.egress_scope.remove_repo_extra", return_value=True)

    result = runner.invoke(app, ["net", "egress", "rm", "--repo", "github.com"])

    assert result.exit_code == 0, result.output
    remove.assert_called_once()


def test_rm_repo_scope_config_only_entry_still_refuses(tmp_path, mocker):
    """The --repo counterpart of test_rm_of_a_config_entry_points_at_the_config_file:
    an entry present in config but never stored as a repo override still
    refuses and points at config.yaml, rather than the generic "not an
    override" message."""
    _repo(tmp_path, mocker, egress_allow=["github.com"])
    mocker.patch("jailbee.egress_scope.remove_repo_extra", return_value=False)

    result = runner.invoke(
        app,
        ["net", "egress", "rm", "--repo", "github.com"],
        env={"COLUMNS": "250"},
    )

    assert result.exit_code == 1
    assert "config.yaml" in result.output


# --- ls --------------------------------------------------------------------


def test_ls_shows_the_source_of_each_entry(tmp_path, mocker):
    _repo(tmp_path, mocker, egress_allow=["github.com"], extras=["nexus.corp:443"])

    result = runner.invoke(app, ["net", "egress", "ls", "myrepo-feat"])

    assert result.exit_code == 0
    assert "github.com" in result.output
    assert "nexus.corp:443" in result.output
    assert "container" in result.output


def test_ls_resolves_the_container_argument_through_the_same_resolver_as_add(tmp_path, mocker):
    """Fix round 1 (I3): `ls <name>` must resolve `name` the same way
    `add`/`rm` do (branch or short name -> full container name), not pass
    the raw argument straight to `container_extras` — otherwise a branch
    name silently looks up the wrong (or no) container and shows an empty
    picture instead of an error or the right container's overrides."""
    cfg, incus = _repo(tmp_path, mocker, egress_allow=["github.com"], extras=["nexus.corp:443"])
    resolve = mocker.patch("jailbee.cli._resolve_existing", return_value=(incus, "myrepo-feat"))
    container_extras = mocker.patch(
        "jailbee.egress_scope.container_extras", return_value=["nexus.corp:443"]
    )

    result = runner.invoke(app, ["net", "egress", "ls", "feat"])

    assert result.exit_code == 0, result.output
    resolve.assert_called_once_with(cfg, "feat")
    container_extras.assert_called_once_with(incus, "myrepo-feat")


def test_ls_with_no_container_hints_on_stderr_that_overrides_are_not_shown(tmp_path, mocker):
    """Fix round 1 (I3): with no container name, `ls` must not silently show
    an empty picture that hides the container-scope entries the user just
    added — it must say so. The notice goes to stderr, not stdout, so
    `--format json` output stays pipeable."""
    _repo(tmp_path, mocker, egress_allow=["github.com"])

    result = runner.invoke(app, ["net", "egress", "ls"])

    assert result.exit_code == 0, result.output
    assert "container" in result.stderr.lower()
    assert "container" not in result.stdout.lower()


# --- export ------------------------------------------------------------


def test_export_emits_file_entries_plus_overrides(tmp_path, mocker):
    _repo(tmp_path, mocker, egress_allow=["github.com"])
    mocker.patch("jailbee.egress_scope.repo_extras", return_value=["nexus.corp:443"])

    result = runner.invoke(app, ["net", "egress", "export"])

    assert result.exit_code == 0
    parsed = yaml.safe_load(result.stdout)
    assert parsed == {"egress_allow": ["github.com", "nexus.corp:443"]}


def test_export_resolves_the_container_argument_through_the_same_resolver_as_add(tmp_path, mocker):
    """Fix round 1 (I3): same resolver requirement as `ls`, for `export`."""
    cfg, incus = _repo(tmp_path, mocker, egress_allow=["github.com"])
    resolve = mocker.patch("jailbee.cli._resolve_existing", return_value=(incus, "myrepo-feat"))
    container_extras = mocker.patch(
        "jailbee.egress_scope.container_extras", return_value=["nexus.corp:443"]
    )
    mocker.patch("jailbee.egress_scope.repo_extras", return_value=[])

    result = runner.invoke(app, ["net", "egress", "export", "feat"])

    assert result.exit_code == 0, result.output
    resolve.assert_called_once_with(cfg, "feat")
    container_extras.assert_called_once_with(incus, "myrepo-feat")
    parsed = yaml.safe_load(result.stdout)
    assert parsed == {"egress_allow": ["github.com", "nexus.corp:443"]}


def test_export_with_no_container_hints_on_stderr_and_keeps_stdout_pure(tmp_path, mocker):
    """Fix round 1 (I3): the no-container notice must land on stderr, never
    stdout — stdout here is the literal replacement block a user pastes
    into config.yaml, so any stray text on that stream would corrupt it."""
    _repo(tmp_path, mocker, egress_allow=["github.com"])
    mocker.patch("jailbee.egress_scope.repo_extras", return_value=["nexus.corp:443"])

    result = runner.invoke(app, ["net", "egress", "export"])

    assert result.exit_code == 0, result.output
    assert "container" in result.stderr.lower()
    parsed = yaml.safe_load(result.stdout)
    assert parsed == {"egress_allow": ["github.com", "nexus.corp:443"]}


def test_export_never_emits_a_feature_auto_added_host(tmp_path, mocker):
    """`effective_egress_allow()` folds in claude/github hosts; the file's own
    list must not gain them."""
    from tests.conftest import make_config

    repo_root = tmp_path / "myrepo"
    repo_root.mkdir()
    (repo_root / ".jailbee").mkdir()
    (repo_root / ".jailbee" / "config.yaml").write_text(
        yaml.safe_dump({"egress_allow": ["github.com"]})
    )
    cfg = make_config(repo_root, egress_allow=["github.com"], github={"enabled": True})
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo_root / ".jailbee" / "config.yaml",
    )
    mocker.patch("jailbee.egress_scope.repo_extras", return_value=["nexus.corp:443"])
    mocker.patch("jailbee.incus.Incus", return_value=mocker.MagicMock())

    result = runner.invoke(app, ["net", "egress", "export"])

    assert result.exit_code == 0, result.output
    assert "api.github.com" not in result.stdout


def test_export_never_emits_the_global_layers_own_egress_allow(tmp_path, mocker, monkeypatch):
    """Fix round 1 (I4), the hazard `repo_file_egress_allow` exists to avoid:
    `cfg.egress_allow` is `deep_merge`d with the GLOBAL layer's own
    `egress_allow` (global first, repo appended — see
    `config.load_config_from_text`), so using it instead of
    `repo_file_egress_allow` here would push one machine's host-level
    policy into the team's committed config the moment someone pastes the
    export output. This is not exercised by
    `test_export_never_emits_a_feature_auto_added_host` (that host comes
    from a *feature flag*, not from a real second config layer) — mutating
    the `render_config_block` call to pass `cfg.egress_allow` instead of
    `egress_scope.repo_file_egress_allow(config_path)` leaves every other
    test in this file green. `_load_or_exit` is deliberately NOT mocked
    here so the real global+repo merge runs."""
    xdg = tmp_path / ".config"
    (xdg / "jailbee").mkdir(parents=True)
    (xdg / "jailbee" / "global.yaml").write_text(
        yaml.safe_dump({"egress_allow": ["globalhost.example"]})
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))

    repo_root = tmp_path / "myrepo"
    (repo_root / ".git").mkdir(parents=True)
    (repo_root / ".jailbee").mkdir()
    (repo_root / ".jailbee" / "config.yaml").write_text(
        yaml.safe_dump({"egress_allow": ["github.com"]})
    )
    mocker.patch(
        "jailbee.cli._resolve_config_path",
        return_value=repo_root / ".jailbee" / "config.yaml",
    )
    # Non-empty, so render_config_block emits the real replacement block
    # rather than its "nothing to promote" comment — see
    # render_config_block's early return for an empty `overrides`.
    mocker.patch("jailbee.egress_scope.repo_extras", return_value=["nexus.corp:443"])
    mocker.patch("jailbee.incus.Incus", return_value=mocker.MagicMock())

    result = runner.invoke(app, ["net", "egress", "export"])

    assert result.exit_code == 0, result.output
    parsed = yaml.safe_load(result.stdout)
    assert parsed == {"egress_allow": ["github.com", "nexus.corp:443"]}
    assert "globalhost.example" not in result.stdout


def test_exported_block_replaces_the_key_and_reloads_cleanly(tmp_path, mocker):
    """The test that matters: paste the output in place of the existing key."""
    from jailbee.config import load_config

    _repo(tmp_path, mocker, egress_allow=["github.com"])
    mocker.patch("jailbee.egress_scope.repo_extras", return_value=["nexus.corp:443"])

    block = runner.invoke(app, ["net", "egress", "export"]).stdout

    path = tmp_path / "myrepo" / ".jailbee" / "config.yaml"
    path.write_text(block)
    reloaded = load_config(path)

    assert "github.com" in reloaded.egress_allow
    assert "nexus.corp:443" in reloaded.egress_allow


# --- hidden alias --------------------------------------------------------


def test_hidden_alias_reaches_the_same_command(tmp_path, mocker):
    _repo(tmp_path, mocker, egress_allow=["github.com"])
    assert runner.invoke(app, ["egress", "ls"]).exit_code == 0


def test_alias_is_hidden_from_root_help_but_named_in_the_group_help():
    """Fix round 1 (I5): `"\\n  egress" not in root` can never fail — Rich's
    `--help` renders commands inside a box (`│ net          …`), never with
    a bare two-space indent, so the assertion passed regardless of whether
    `hidden=True` was ever applied. Assert on the group's own help text
    instead, the house pattern from `tests/test_cli_aliases.py`."""
    root = runner.invoke(app, ["--help"]).output
    group = runner.invoke(app, ["net", "egress", "--help"]).output
    assert "Allow a container" not in root
    assert "jailbee egress" in group


# --- visibility in `config show` and `net status` ------------------------


def test_config_show_effective_includes_repo_overrides(tmp_path, mocker):
    _repo(tmp_path, mocker, egress_allow=["github.com"])
    mocker.patch("jailbee.egress_scope.repo_extras", return_value=["nexus.corp:443"])

    result = runner.invoke(app, ["config", "show"])

    assert "nexus.corp:443" in result.output


def test_config_show_repo_layer_is_untouched_by_overrides(tmp_path, mocker):
    _repo(tmp_path, mocker, egress_allow=["github.com"])
    mocker.patch("jailbee.egress_scope.repo_extras", return_value=["nexus.corp:443"])

    result = runner.invoke(app, ["config", "show", "--layer", "repo"])

    assert "nexus.corp:443" not in result.output


def test_net_status_lists_containers_carrying_overrides(tmp_path, mocker, capsys):
    from jailbee.cli import _print_egress_override_status

    cfg, incus = _repo(tmp_path, mocker, extras=["nexus.corp:443"])
    mocker.patch("jailbee.egress_scope.repo_extras", return_value=[])
    # `_print_egress_override_status` loads its config via `load_config(
    # find_repo_config())` directly (matching its sibling `_print_loose_status`'s
    # best-effort-silent shape), not via `_load_or_exit` — so patching that,
    # as `_repo()` does for the other commands in this file, does not reach
    # it. Patch the two symbols it actually calls.
    mocker.patch("jailbee.cli.load_config", return_value=cfg)
    mocker.patch("jailbee.cli.find_repo_config", return_value=tmp_path / "unused.yaml")
    mocker.patch(
        "jailbee.cli._list_containers_for_status",
        return_value=["myrepo-feat"],
    )

    _print_egress_override_status()

    assert "myrepo-feat" in capsys.readouterr().out
