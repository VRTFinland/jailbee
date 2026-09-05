"""Tests for global config loader."""

from pathlib import Path

import pytest
import yaml

from jailbee.config import ConfigError
from jailbee.global_config import (
    GlobalConfig,
    default_global_config_path,
    load_global_config,
)


def test_default_global_config_path_uses_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert default_global_config_path() == tmp_path / "jailbee" / "global.yaml"


def test_default_global_config_path_uses_jailbee_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert default_global_config_path() == tmp_path / "jailbee" / "global.yaml"


def test_default_global_config_path_falls_back_to_home(monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert default_global_config_path() == Path.home() / ".config" / "jailbee" / "global.yaml"


def test_load_global_config_missing_returns_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    cfg, warnings = load_global_config(tmp_path / "does-not-exist.yaml")

    assert isinstance(cfg, GlobalConfig)
    assert warnings == []
    assert cfg.docker_registry_mirror.port == 3128
    assert cfg.docker_registry_mirror.data_dir == (
        Path.home() / ".local" / "share" / "jailbee" / "registry"
    )


def test_load_global_config_registry_data_dir_honors_xdg(tmp_path, monkeypatch):
    """$XDG_DATA_HOME overrides the default registry data_dir base."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    cfg, _ = load_global_config(tmp_path / "does-not-exist.yaml")

    assert cfg.docker_registry_mirror.data_dir == (tmp_path / "xdg-data" / "jailbee" / "registry")


def test_load_global_config_with_overrides(tmp_path):
    path = tmp_path / "global.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "docker_registry_mirror": {"port": 25000, "data_dir": "/srv/registry"},
            }
        )
    )

    cfg, _ = load_global_config(path)

    assert cfg.docker_registry_mirror.port == 25000
    assert cfg.docker_registry_mirror.data_dir == Path("/srv/registry")


def test_load_global_config_partial_override(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    path = tmp_path / "global.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "docker_registry_mirror": {"port": 17000},
            }
        )
    )

    cfg, _ = load_global_config(path)

    assert cfg.docker_registry_mirror.port == 17000
    # Unset field falls back to default
    assert cfg.docker_registry_mirror.data_dir == (
        Path.home() / ".local" / "share" / "jailbee" / "registry"
    )


def test_load_global_config_invalid_yaml_raises(tmp_path):
    path = tmp_path / "global.yaml"
    path.write_text("docker_registry_mirror: { port: not-a-number")  # broken

    with pytest.raises(ConfigError):
        load_global_config(path)


def test_load_global_config_unknown_key_in_host_block_raises(tmp_path):
    """Typo protection survives for host-level keys (docker_registry_mirror).

    Unlike an unknown *column name* (recovered from — see
    ``test_load_global_config_recovers_from_an_unknown_column_name`` below),
    a malformed host-level block has nothing sensible to recover to and
    stays a load-time ``ConfigError``.

    Unknown *top-level* keys are silently passed through — they belong to
    the Config overlay (gpg, ssh, jetbrains, host_mounts, ...) and any
    typos there surface at `load_config()` validation time.
    """
    path = tmp_path / "global.yaml"
    path.write_text(yaml.safe_dump({"docker_registry_mirror": {"unknown_nested": True}}))

    with pytest.raises(ConfigError):
        load_global_config(path)


def test_load_global_config_passes_through_overlay_keys(tmp_path):
    """Config-overlay keys in global.yaml don't trip GlobalConfig validation."""
    path = tmp_path / "global.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "gpg": {"enabled": True},
                "chrome": {"enabled": True, "url": "https://example.com"},
                "egress_allow": ["api.anthropic.com:443"],
            }
        )
    )

    cfg, _ = load_global_config(path)

    # No raise; host-level defaults apply.
    assert cfg.docker_registry_mirror.enabled == "auto"


def test_load_global_config_empty_file_returns_defaults(tmp_path):
    path = tmp_path / "global.yaml"
    path.write_text("")

    cfg, warnings = load_global_config(path)

    assert warnings == []
    assert cfg.docker_registry_mirror.port == 3128


def test_docker_registry_mirror_defaults_to_port_3128():
    gcfg = GlobalConfig()
    assert gcfg.docker_registry_mirror.port == 3128


def test_docker_registry_mirror_defaults_to_rpardini_image():
    gcfg = GlobalConfig()
    assert gcfg.docker_registry_mirror.image == "rpardini/docker-registry-proxy:0.6.5"


def test_docker_registry_mirror_accepts_overrides(tmp_path):
    cfg_file = tmp_path / "global.yaml"
    cfg_file.write_text(
        "docker_registry_mirror:\n"
        "  port: 4000\n"
        "  image: rpardini/docker-registry-proxy:0.7.0\n"
        "  enabled: false\n"
    )
    gcfg, _ = load_global_config(cfg_file)
    assert gcfg.docker_registry_mirror.port == 4000
    assert gcfg.docker_registry_mirror.image == "rpardini/docker-registry-proxy:0.7.0"
    assert gcfg.docker_registry_mirror.enabled is False


# ---------- column-block recovery (unknown/empty/duplicate names)
#
# A typo (or an empty/duplicate `fields` list) in `global.yaml`'s `ls:` /
# `dashboard:` blocks is a personal display preference, not a reason to
# break an unrelated command: `load_global_config` recovers from it rather
# than raising. `gie config validate` still reports it as an error — see
# `test_global_config_issues` below and `tests/test_cli.py`.


def test_load_global_config_recovers_from_an_unknown_column_name(tmp_path):
    path = tmp_path / "global.yaml"
    path.write_text("ls:\n  fields: [name, nosuchfield]\n")

    gcfg, warnings = load_global_config(path)

    assert gcfg.ls.fields == ["name"]
    assert any("nosuchfield" in w for w in warnings)
    assert any("allowed:" in w for w in warnings)


def test_load_global_config_recovers_from_an_empty_fields_list(tmp_path):
    path = tmp_path / "global.yaml"
    path.write_text("dashboard:\n  fields: []\n")

    gcfg, warnings = load_global_config(path)

    assert gcfg.dashboard.fields is None
    assert any("empty" in w for w in warnings)


def test_load_global_config_recovers_from_a_duplicated_field(tmp_path):
    path = tmp_path / "global.yaml"
    path.write_text("ls:\n  fields: [name, name]\n")

    gcfg, warnings = load_global_config(path)

    assert gcfg.ls.fields == ["name"]
    assert any("duplicate" in w for w in warnings)


def test_load_global_config_recovers_when_every_name_was_invalid(tmp_path):
    """`fields` reduced to empty by dropping unknown/duplicate names falls
    back to the built-in default set, same as an explicit `fields: []`."""
    path = tmp_path / "global.yaml"
    path.write_text("ls:\n  fields: [nosuchfield]\n")

    gcfg, warnings = load_global_config(path)

    assert gcfg.ls.fields is None
    assert any("no valid column names remained" in w for w in warnings)


def test_load_global_config_accepts_valid_column_blocks(tmp_path):
    path = tmp_path / "global.yaml"
    path.write_text("ls:\n  fields: [name, local_diff]\ndashboard:\n  hide: [ip]\n")

    gcfg, warnings = load_global_config(path)

    assert gcfg.ls.fields == ["name", "local_diff"]
    assert warnings == []


def test_global_config_issues_reports_an_unknown_column_name(tmp_path):
    """`gie config validate`'s check — the same typo `load_global_config`
    recovers from is still an error here, with the allowed names listed."""
    from jailbee.global_config import global_config_issues

    path = tmp_path / "global.yaml"
    path.write_text("ls:\n  fields: [name, nosuchfield]\n")

    issues = global_config_issues(path)

    assert any("nosuchfield" in i for i in issues)
    assert any("allowed:" in i for i in issues)


def test_global_config_issues_empty_for_a_missing_file(tmp_path):
    from jailbee.global_config import global_config_issues

    assert global_config_issues(tmp_path / "does-not-exist.yaml") == []


def test_global_config_issues_reraises_a_host_level_schema_error(tmp_path):
    from jailbee.global_config import global_config_issues

    path = tmp_path / "global.yaml"
    path.write_text(yaml.safe_dump({"docker_registry_mirror": {"unknown_nested": True}}))

    with pytest.raises(ConfigError):
        global_config_issues(path)


def test_mirror_enabled_defaults_to_auto():
    """The default is docker-detection, not force-on. Non-Docker users must
    not need the mirror container at all."""
    assert GlobalConfig().docker_registry_mirror.enabled == "auto"


@pytest.mark.parametrize("raw,expected", [("auto", "auto"), ("true", True), ("false", False)])
def test_mirror_enabled_accepts_auto_and_bools(tmp_path, raw, expected):
    path = tmp_path / "global.yaml"
    path.write_text(f"docker_registry_mirror:\n  enabled: {raw}\n")
    gcfg, warnings = load_global_config(path)
    assert gcfg.docker_registry_mirror.enabled == expected
    assert warnings == []


def test_mirror_enabled_rejects_other_strings(tmp_path):
    path = tmp_path / "global.yaml"
    path.write_text("docker_registry_mirror:\n  enabled: sometimes\n")
    with pytest.raises(ConfigError):
        load_global_config(path)


def test_global_config_parses_claude_credentials(tmp_path):
    from jailbee.global_config import load_global_config

    path = tmp_path / "global.yaml"
    path.write_text(
        "claude_credentials:\n  group: work\n  repos:\n    side: personal\n    solo: null\n"
    )

    gcfg, warnings = load_global_config(path)

    assert warnings == []
    assert gcfg.claude_credentials.group == "work"
    assert gcfg.claude_credentials.repos == {"side": "personal", "solo": None}


def test_claude_credentials_is_a_host_level_key():
    """It must never reach the Config layer's `deep_merge`: `Config` has
    `extra='forbid'` and no such field, so an unsplit key would make every
    load fail for anyone who sets it."""
    from jailbee.config import _HOST_LEVEL_KEYS, _split_host_keys

    assert "claude_credentials" in _HOST_LEVEL_KEYS
    host, config_level = _split_host_keys({"claude_credentials": {"group": "work"}, "gpg": {}})
    assert "claude_credentials" in host
    assert "claude_credentials" not in config_level


def test_scratch_block_reaches_global_config(tmp_path) -> None:
    p = tmp_path / "global.yaml"
    p.write_text("scratch:\n  enabled: false\n  config:\n    defaults:\n      memory: 4GiB\n")

    gcfg, warnings = load_global_config(p)

    assert gcfg.scratch.enabled is False
    assert gcfg.scratch.config == {"defaults": {"memory": "4GiB"}}
    assert warnings == []


def test_scratch_defaults_to_enabled_with_no_overrides(tmp_path) -> None:
    gcfg, _ = load_global_config(tmp_path / "no-such-file.yaml")

    assert gcfg.scratch.enabled is True
    assert gcfg.scratch.config == {}


def test_scratch_rejects_unknown_keys(tmp_path) -> None:
    """`extra="forbid"`: a typo in the block name must be reported, not ignored."""
    p = tmp_path / "global.yaml"
    p.write_text("scratch:\n  enabld: true\n")

    with pytest.raises(ConfigError):
        load_global_config(p)


def test_config_edit_is_host_level_and_defaults_to_auto(tmp_path):
    """`config_edit` is routed to GlobalConfig, not into the Config overlay."""
    from jailbee.config import _split_host_keys
    from jailbee.global_config import validate_global_raw

    raw = {"config_edit": {"write_policy": "patch"}, "gpg": {"enabled": True}}
    host, overlay = _split_host_keys(raw)
    assert "config_edit" in host
    assert "config_edit" not in overlay

    gcfg = validate_global_raw(raw, tmp_path / "global.yaml")
    assert gcfg.config_edit.write_policy == "patch"
    assert GlobalConfig().config_edit.write_policy == "auto"


def test_config_edit_rejects_an_unknown_policy(tmp_path):
    from jailbee.config import ConfigError
    from jailbee.global_config import validate_global_raw

    with pytest.raises(ConfigError):
        validate_global_raw({"config_edit": {"write_policy": "clobber"}}, tmp_path / "g.yaml")


def test_config_edit_is_rejected_in_a_repo_config(tmp_path):
    """A repo config cannot dictate how the user's own files are written."""
    from jailbee.config import ConfigError, load_config_from_layers

    with pytest.raises(ConfigError):
        load_config_from_layers(
            {}, {"config_edit": {"write_policy": "patch"}}, tmp_path / "config.yaml",
            origin=str(tmp_path),
        )
