"""Tests for the `jailbee net egress` command group."""

from __future__ import annotations

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
    test."""
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
    incus.config_get.return_value = None
    mocker.patch("jailbee.incus.Incus", return_value=incus)
    mocker.patch("jailbee.cli._resolve_existing", return_value=(incus, "myrepo-feat"))
    mocker.patch("jailbee.cli._repin_hosts_quietly")
    mocker.patch("jailbee.egress_scope.resolve_entries", return_value=[])
    mocker.patch("jailbee.lifecycle.current_network_mode", return_value="loose")
    setc = mocker.patch("jailbee.egress_scope.set_container_extras")

    result = runner.invoke(app, ["net", "egress", "add", "nexus.corp:443"])

    assert result.exit_code == 0
    setc.assert_called_once()
    incus.config_device_override.assert_not_called()
    incus.config_device_set.assert_not_called()


def test_rm_of_a_config_entry_points_at_the_config_file(tmp_path, mocker):
    _repo(tmp_path, mocker, egress_allow=["github.com"])

    result = runner.invoke(
        app, ["net", "egress", "rm", "github.com"], env={"COLUMNS": "250"}
    )

    assert result.exit_code == 1
    assert "config.yaml" in result.output


def test_ls_shows_the_source_of_each_entry(tmp_path, mocker):
    _repo(tmp_path, mocker, egress_allow=["github.com"], extras=["nexus.corp:443"])

    result = runner.invoke(app, ["net", "egress", "ls", "myrepo-feat"])

    assert result.exit_code == 0
    assert "github.com" in result.output
    assert "nexus.corp:443" in result.output
    assert "container" in result.output


def test_export_emits_file_entries_plus_overrides(tmp_path, mocker):
    _repo(tmp_path, mocker, egress_allow=["github.com"])
    mocker.patch("jailbee.egress_scope.repo_extras", return_value=["nexus.corp:443"])

    result = runner.invoke(app, ["net", "egress", "export"])

    assert result.exit_code == 0
    parsed = yaml.safe_load(result.output)
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

    assert "api.github.com" not in result.output


def test_exported_block_replaces_the_key_and_reloads_cleanly(tmp_path, mocker):
    """The test that matters: paste the output in place of the existing key."""
    from jailbee.config import load_config

    _repo(tmp_path, mocker, egress_allow=["github.com"])
    mocker.patch("jailbee.egress_scope.repo_extras", return_value=["nexus.corp:443"])

    block = runner.invoke(app, ["net", "egress", "export"]).output

    path = tmp_path / "myrepo" / ".jailbee" / "config.yaml"
    path.write_text(block)
    reloaded = load_config(path)

    assert "github.com" in reloaded.egress_allow
    assert "nexus.corp:443" in reloaded.egress_allow


def test_hidden_alias_reaches_the_same_command(tmp_path, mocker):
    _repo(tmp_path, mocker, egress_allow=["github.com"])
    assert runner.invoke(app, ["egress", "ls"]).exit_code == 0


def test_alias_is_hidden_from_root_help_but_named_in_the_group_help():
    root = runner.invoke(app, ["--help"]).output
    group = runner.invoke(app, ["net", "egress", "--help"]).output
    assert "\n  egress" not in root
    assert "jailbee egress" in group
